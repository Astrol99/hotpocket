"""hotpocket's tests: the gateway in-process, against a fake Anthropic API. Run: uv run pytest"""
import asyncio
import os
import sqlite3
import subprocess
import sys
import time

import aiohttp
import pytest
from aiohttp import web

import hotpocket

U = "anthropic-ratelimit-unified-"
SEEN = web.AppKey("seen", list)  # what the fake API was sent
TOKEN = "sk-ant-oat01-NEW"


async def fake_api(request):
    """Answers by the token's suffix: LIMITED (usage limit), THROTTLE and SLOWDOWN (plain 429s, the
    second with retry-after), BAD (401), TRUNC (drops mid-stream), SLOW. Others stream 3 events."""
    tok = request.headers.get("authorization", "").rsplit("-", 1)[-1]
    request.app[SEEN].append({"tok": tok, "path": request.path_qs, "headers": request.headers,
                              "body": await request.read()})
    now = time.time()
    status, headers = {"LIMITED": (429, {U + "status": "rejected", U + "reset": str(int(now + 7200))}),
                       "THROTTLE": (429, {}), "SLOWDOWN": (429, {"retry-after": "2"}), "BAD": (401, {})
                       }.get(tok, (200, {}))
    if status != 200:
        return web.json_response({"type": "error", "error": {}}, status=status, headers=headers)
    resp = web.StreamResponse(headers={
        "content-type": "text/event-stream", U + "status": "allowed",
        U + "5h-utilization": "0.42", U + "5h-reset": str(int(now + 3000)),
        U + "7d-utilization": "0.10", U + "7d-reset": str(int(now + 90000))})
    await resp.prepare(request)
    for _ in range(3):
        await resp.write(b"event: delta\ndata: {}\n\n")
        if tok == "TRUNC":
            request.transport.close()  # gone mid-stream, without the final chunk
            return resp
        await asyncio.sleep(0.3 if tok == "SLOW" else 0)
    await resp.write_eof()
    return resp


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A fresh, empty database for the in-process gateway and helpers."""
    monkeypatch.setattr(hotpocket, "DATA", tmp_path)
    monkeypatch.setattr(hotpocket, "SCHEMA", "PRAGMA synchronous = OFF;" + hotpocket.SCHEMA)  # throwaway data
    hotpocket.db.cache_clear()
    yield tmp_path
    if hotpocket.db.cache_info().currsize:
        hotpocket.db().close()
    hotpocket.db.cache_clear()


@pytest.fixture
async def gw(data, aiohttp_server, aiohttp_client, monkeypatch):
    """A gateway with two accounts (the first one at its usage limit), a key for alice and an admin."""
    api = web.Application()
    api[SEEN] = []
    api.router.add_route("*", "/{tail:.*}", fake_api)
    monkeypatch.setattr(hotpocket, "UPSTREAM", str((await aiohttp_server(api)).make_url("")).rstrip("/"))
    use_accounts("LIMITED", "GOOD")
    hotpocket.write("INSERT INTO admins VALUES ('admin@example.com', 0)")
    client = await aiohttp_client(hotpocket.make_app())
    client.seen, client.dev = api[SEEN], dev(hotpocket.new_key("alice"))
    return client


def use_accounts(*suffixes):
    for label in hotpocket.load_accounts():
        hotpocket.remove("accounts", "label", label, "account")
    for suffix in suffixes:
        hotpocket.save_account(suffix.lower(), f"sk-ant-oat01-{suffix}")


def dev(key, session="s1"):
    """A developer's request headers, as Claude Code (and Tailscale) send them."""
    return {"authorization": f"Bearer {key}", "x-claude-code-session-id": session,
            "anthropic-beta": "claude-code-20250219", "tailscale-user-login": "alice@example.com"}


async def call(gw, headers=None, data=b'{"model": "x"}'):
    resp = await gw.post("/v1/messages?beta=true", headers=gw.dev if headers is None else headers, data=data)
    return resp, await resp.read()


async def status(gw, headers=None):
    snap = await (await gw.get("/_gateway/status", headers=headers)).json()
    return {**snap, "accounts": {a["label"]: a for a in snap["accounts"]}}


def as_user(gw, login="admin@example.com", https=True, origin="page"):
    """What Tailscale serve and the browser add when someone uses the status page."""
    headers = {"x-forwarded-proto": https and "https", "tailscale-user-login": login,
               "origin": f"https://{gw.host}:{gw.port}" if origin == "page" else origin}
    return {k: v for k, v in headers.items() if v}


async def manage(gw, method, path, body=None, **who):
    resp = await gw.request(method, "/_gateway/api/" + path, json=body, headers=as_user(gw, **who))
    assert resp.status != 200 or resp.headers["cache-control"] == "no-store"  # may hold a new key
    return resp.status, await resp.json()


def cli(data, *args):
    r = subprocess.run([sys.executable, "-m", "hotpocket", *args], capture_output=True, text=True,
                       env=dict(os.environ, HOTPOCKET_DATA=str(data)))
    return r.returncode, r.stdout + r.stderr


# --------------------------------------------------------------------------- proxying

async def test_routes_that_never_reach_the_api(gw, capsys):
    assert (await gw.head("/api/hello")).status == 200  # Claude Code's keyless connectivity check
    assert (await gw.get("/favicon.ico")).status == 403
    assert "favicon" not in capsys.readouterr().out  # browser probes leave no log line
    for path in ("/", "/_gateway"):  # both lead to the page, at /_gateway/ where its relative links work
        assert (await gw.get(path, allow_redirects=False)).headers["location"] == "/_gateway/"
    to_https = "/_gateway/https/hotpocket.example.ts.net/"  # where Tailscale serve sends plain HTTP
    assert (await gw.get(to_https, allow_redirects=False)).headers["location"] == "https://hotpocket.example.ts.net/"
    assert (await gw.get(to_https, headers={"x-forwarded-proto": "https"})).status == 404  # no open redirect
    assert (await gw.post(to_https + "v1/messages", headers=gw.dev, allow_redirects=False)).status == 302
    assert gw.seen == []  # an API call over plain HTTP never reaches Anthropic
    resp = await gw.get("/")
    assert "<title>hotpocket</title>" in await resp.text()
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["content-type"] == "text/html; charset=utf-8"


async def test_missing_or_wrong_key_is_403_never_401(gw):
    # Claude Code retries a 401 for minutes before giving up.
    resp, body = await call(gw, headers={})
    assert resp.status == 403 and b"developer key" in body
    assert (await call(gw, headers=dev("hp-wrong")))[0].status == 403


async def test_limited_account_is_skipped_then_remembered(gw):
    resp, body = await call(gw)
    assert resp.status == 200 and body.count(b"event: delta") == 3
    await call(gw)
    assert [s["tok"] for s in gw.seen] == ["LIMITED", "GOOD", "GOOD"]


async def test_responses_stream_through_as_they_arrive(gw):
    use_accounts("SLOW")
    started = time.monotonic()
    resp = await gw.post("/v1/messages", headers=gw.dev, data=b"{}")
    await resp.content.readany()
    first = time.monotonic() - started
    await resp.read()
    assert first < 0.25 and time.monotonic() - started > 0.8


async def test_headers_going_up_and_coming_back(gw):
    resp, _ = await call(gw)
    # One account's usage would make Claude Code warn while the pool still has room.
    assert not any(k.lower().startswith(U) for k in resp.headers)
    assert gw.seen[-1]["path"] == "/v1/messages?beta=true"  # the query string goes along
    sent = gw.seen[-1]["headers"]
    assert sent["authorization"] == "Bearer sk-ant-oat01-GOOD"
    assert sent["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
    assert not {"x-api-key", "tailscale-user-login", "x-forwarded-for"} & {k.lower() for k in sent}


async def test_chunked_request_body(gw):
    async def body():
        yield b'{"model":'
        yield b' "chunked"}'
    assert (await call(gw, data=body()))[0].status == 200
    assert gw.seen[-1]["body"] == b'{"model": "chunked"}'


async def test_absolute_form_target_is_refused_before_reaching_upstream(gw):
    # "http://evil.example/..." glued onto the upstream URL would send a token elsewhere.
    reader, writer = await asyncio.open_connection(gw.host, gw.port)
    writer.write(f"POST http://evil.example/v1/messages HTTP/1.1\r\nHost: x\r\n"
                 f"authorization: {gw.dev['authorization']}\r\ncontent-length: 2\r\n\r\n{{}}".encode())
    status_line = await reader.readline()
    writer.close()
    assert b" 400 " in status_line and gw.seen == []


async def test_all_accounts_limited_looks_like_a_usage_limit(gw):
    use_accounts("LIMITED")
    resp, _ = await call(gw)
    assert resp.status == 429 and resp.headers[U + "status"] == "rejected"
    assert int(resp.headers[U + "reset"]) > time.time() + 7000


async def test_rejected_and_throttled_accounts_are_skipped(gw):
    use_accounts("BAD", "THROTTLE", "GOOD")
    assert (await call(gw))[0].status == 200 and [s["tok"] for s in gw.seen] == ["BAD", "THROTTLE", "GOOD"]
    acc = (await status(gw))["accounts"]
    assert acc["bad"]["blocked_kind"] == "auth" and acc["throttle"]["blocked_kind"] == "throttle"


@pytest.mark.parametrize("suffix, wait", [("THROTTLE", 60), ("SLOWDOWN", 2)])  # SLOWDOWN sends retry-after
async def test_all_throttled_is_a_plain_retryable_429(gw, suffix, wait):
    use_accounts(suffix)
    resp, _ = await call(gw)
    assert resp.status == 429 and U + "status" not in resp.headers and int(resp.headers["retry-after"]) <= wait
    assert (await status(gw))["accounts"][suffix.lower()]["blocked_until"] < time.time() + wait + 1


async def test_rejected_token_is_403_and_its_replacement_used_at_once(gw):
    use_accounts("BAD")
    resp, body = await call(gw)
    assert resp.status == 403 and b"rejected" in body
    hotpocket.save_account("bad", "sk-ant-oat01-FIXED")
    assert (await call(gw))[0].status == 200 and gw.seen[-1]["tok"] == "FIXED"


async def test_truncated_upstream_stream_reaches_the_developer_as_an_error(gw, capsys):
    use_accounts("TRUNC")
    resp = await gw.post("/v1/messages", headers=gw.dev, data=b"{}")
    with pytest.raises(aiohttp.ClientPayloadError):
        await resp.read()
    assert "stream ended early" in capsys.readouterr().out


# --------------------------------------------------------------------------- status

async def test_status_shows_usage_blocks_and_developers_but_no_tokens_and_survives_a_restart(gw):
    await call(gw)
    snap = await status(gw)
    acc = snap["accounts"]
    assert acc["limited"]["blocked_kind"] == "limit" and acc["limited"]["blocked_until"] > time.time() + 7000
    assert acc["good"]["five_hour"] == 0.42 and acc["good"]["requests"] == 1
    assert [(d["name"], d["requests"]) for d in snap["developers"]] == [("alice", 1)]
    assert "sk-ant" not in str(snap)
    await gw.close()  # shutting down saves usage
    assert hotpocket.Pool().accounts["limited"]["blocked_until"] > time.time()


def test_a_window_past_its_reset_reads_zero():
    assert hotpocket.used({"five_hour": 0.9, "five_hour_reset": time.time() - 1}, "five_hour", time.time()) == 0
    assert hotpocket.used({}, "five_hour", time.time()) is None


# --------------------------------------------------------------------------- managing from the page

async def test_the_page_learns_who_is_viewing_only_from_this_host(gw):
    snap = await status(gw, headers=as_user(gw, login="Admin@Example.com"))
    assert snap["viewer"] == {"login": "admin@example.com", "https": True, "member": True, "admin": True}
    assert not hotpocket.viewer("100.64.0.9", as_user(gw))["admin"]


async def test_managing_needs_https_from_the_page_and_admin_for_accounts(gw):
    for who in ({"https": False}, {"origin": "https://evil.example"}, {"origin": None}, {"login": None}):
        for method, path in (("PUT", "accounts/web"), ("POST", "my-key")):
            assert (await manage(gw, method, path, {"token": TOKEN}, **who))[0] == 403, (path, who)
    assert (await manage(gw, "PUT", "accounts/web", {"token": TOKEN}, login="bob@x.com"))[0] == 403
    assert await manage(gw, "PUT", "accounts/web", {"token": f" {TOKEN}\n"}) == (200, {"accounts": 3})


async def test_admin_edits_accounts_and_keys(gw, capsys):
    assert (await manage(gw, "PUT", "accounts/web", {"token": "nope"}))[0] == 400
    status_, res = await manage(gw, "PUT", "accounts/bad%20label", {"token": TOKEN})
    assert status_ == 400 and "names are" in res["error"]
    assert (await manage(gw, "PUT", "accounts/.", {"token": TOKEN}))[0] in (400, 403, 404)
    with pytest.raises(hotpocket.AdminError):
        hotpocket.new_key("..")
    _, res = await manage(gw, "POST", "keys/bob")
    assert (await call(gw, dev(res["key"], "bob")))[0].status == 200
    assert (await manage(gw, "DELETE", "keys/BOB"))[0] == 200  # names ignore case, like Tailscale logins
    assert (await call(gw, dev(res["key"], "bob")))[0].status == 403
    assert "bob" not in {d["name"] for d in (await status(gw))["developers"]}  # removed, history and all
    assert (await manage(gw, "DELETE", "accounts/good"))[0] == 200
    assert await manage(gw, "DELETE", "accounts/good") == (400, {"error": "No account named 'good'."})
    log = capsys.readouterr().out
    assert "admin admin@example.com: created a key for bob" in log
    assert "admin admin@example.com: removed account good" in log
    assert "admin admin@example.com: removed developer bob" in log  # as stored, not as typed


async def test_members_get_and_replace_only_their_own_key(gw, capsys):
    status_, first = await manage(gw, "POST", "my-key", login="Bob@Example.com")
    assert status_ == 200 and first["name"] == "bob@example.com"
    assert (await call(gw, dev(first["key"])))[0].status == 200
    _, second = await manage(gw, "POST", "my-key", login="bob@example.com")
    assert (await call(gw, dev(first["key"])))[0].status == 403
    assert (await call(gw, dev(second["key"])))[0].status == 200
    assert (await manage(gw, "POST", "keys/alice", login="bob@example.com"))[0] == 403
    assert (await manage(gw, "POST", "my-key", login="o'brien@example.com"))[0] == 400  # can't name a key
    assert "bob@example.com: created their own key" in capsys.readouterr().out


async def test_request_paths_cannot_forge_log_lines(gw, capsys):
    forged = "/_gateway/api/keys/x%0A2026-01-01%2000:00:00%20admin%20evil:%20removed%20the%20key%20for%20bob"
    assert (await gw.post(forged)).status == 403
    assert not any(line.startswith("2026-01-01") for line in capsys.readouterr().out.splitlines())


async def test_locked_database_gives_the_page_a_503(gw):
    hotpocket.db().execute("PRAGMA busy_timeout = 50")  # the test needn't wait out the full second
    locker = sqlite3.connect(hotpocket.DATA / "hotpocket.db", isolation_level=None)  # say, an open sqlite3 shell
    locker.execute("BEGIN IMMEDIATE")
    status_, res = await manage(gw, "POST", "keys/busy")
    locker.close()
    assert status_ == 503 and "busy" in res["error"]


async def test_cli_and_page_edits_at_the_same_time_all_land(gw, data):
    results = await asyncio.gather(*(asyncio.to_thread(cli, data, "admin", "add", f"cli{i}@x.com") for i in range(5)),
                                   *(manage(gw, "POST", f"keys/web{i}") for i in range(5)))
    assert [r[0] for r in results] == [0] * 5 + [200] * 5
    assert {f"web{i}" for i in range(5)} <= set(hotpocket.key_names())
    admins = {login for (login,) in hotpocket.db().execute("SELECT login FROM admins")}
    assert {f"cli{i}@x.com" for i in range(5)} <= admins


async def test_removed_admin_can_no_longer_manage(gw, data):
    assert cli(data, "admin", "add", "Carol@Example.com")[0] == 0
    assert (await manage(gw, "POST", "keys/x", login="carol@example.com"))[0] == 200
    assert cli(data, "admin", "list") == (0, "admin@example.com\ncarol@example.com\n")
    assert cli(data, "admin", "rm", "Carol@Example.com")[0] == 0
    assert (await manage(gw, "POST", "keys/y", login="carol@example.com"))[0] == 403


# --------------------------------------------------------------------------- storage and command line

def test_keys_are_stored_only_as_hashes(data):
    key = hotpocket.new_key("alice")
    stored = b"".join(f.read_bytes() for f in data.iterdir())
    assert key.encode() not in stored and hotpocket.key_hash(key).encode() in stored


def test_command_line(tmp_path):
    assert cli(tmp_path, "admin", "list") == (0, "No admins. Add one with: hotpocket admin add LOGIN\n")
    rc, out = cli(tmp_path, "admin", "add", "not a login")
    assert rc == 1 and "names are" in out
    assert cli(tmp_path, "admin", "add", "a@example.com")[0] == 0
    assert {oct(f.stat().st_mode & 0o777) for f in tmp_path.iterdir()} == {"0o600"}  # owner-only files
