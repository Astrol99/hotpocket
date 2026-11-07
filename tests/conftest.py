"""What the tests share: the gateway in-process, against a fake Anthropic API, and helpers to use it.
Run them with: uv run pytest"""

import asyncio
import json
import logging
import time

import pytest
from aiohttp import web

from hotpocket import proxy, server, store
from hotpocket.pool import POOL
from hotpocket.tailscale import ADMIN_CAP
from hotpocket.usage import UNIFIED as U

SEEN = web.AppKey("seen", list)  # what the fake API was sent
HOST = "hotpocket.example.ts.net"


async def fake_api(request):
    """Answers by the token's suffix: LIMITED (usage limit), THROTTLE and SLOWDOWN (plain 429s, the
    second with retry-after), GARBLED (a 429 that isn't JSON), BAD (401), REVOKED and NOMODEL (403s),
    TRUNC (drops mid-stream), SLOW, PLAIN (a reply that isn't streamed); for API keys, SPENT (the monthly spend
    cap's 429), BROKE (a 402), LOWBAL and CAPPED (out of credits, or at a set spend limit: 400s) and INVALID
    (a 400 about the request). Others stream a reply with token usage."""
    tok = (request.headers.get("x-api-key") or request.headers.get("authorization", "")).rsplit("-", 1)[-1]
    request.app[SEEN].append({"tok": tok, "path": request.path_qs, "headers": request.headers,
                              "body": await request.read()})
    now = time.time()
    status, headers = {"LIMITED": (429, {U + "status": "rejected", U + "reset": str(int(now + 7200))}),
                       "THROTTLE": (429, {}), "SLOWDOWN": (429, {"retry-after": "2", "x-should-retry": "false"}),
                       "BAD": (401, {}), "REVOKED": (403, {}), "NOMODEL": (403, {}),
                       "SPENT": (429, {}), "BROKE": (402, {}), "LOWBAL": (400, {}), "CAPPED": (400, {}),
                       "INVALID": (400, {})}.get(tok, (200, {}))
    if tok == "GARBLED":
        return web.Response(status=429, text="<html>slow down</html>", content_type="text/html")
    if status != 200:
        message = {"REVOKED": "OAuth token has been revoked.",  # the API's words
                   "SPENT": "You have reached your API usage limits: your organization has crossed its monthly API usage "
                            "threshold, set based on your organization's API tier.",
                   "LOWBAL": "Your credit balance is too low to access the Anthropic API.",
                   "CAPPED": "You have reached your specified API usage limits."}.get(tok, f"{tok} says no")
        details = {"details": {"error_code": "enforced_spend_limit_reached"}} if tok == "SPENT" else {}
        return web.json_response({"type": "error", "error": {"message": message, **details}}, status=status, headers=headers)
    if tok == "PLAIN":
        return web.json_response({"model": "claude-sonnet-5-5", "usage": {"input_tokens": 1000, "output_tokens": 100}})
    resp = web.StreamResponse(headers={
        "content-type": "text/event-stream", U + "status": "allowed",
        U + "5h-utilization": "0.42", U + "5h-reset": str(int(now + 3000)),
        U + "7d-utilization": "0.10", U + "7d-reset": str(int(now + 90000))})
    await resp.prepare(request)
    usage = '{"input_tokens": 100, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 1000, "output_tokens": 1}'
    for event in (f'message_start\ndata: {{"type": "message_start", "message": {{"model": "claude-opus-5-5", "usage": {usage}}}}}',
                  'content_block_delta\ndata: {"type": "content_block_delta"}',
                  'message_delta\ndata: {"type": "message_delta", "usage": {"output_tokens": 50}}'):
        await resp.write(f"event: {event}\n\n".encode())
        if tok == "TRUNC":
            request.transport.close()  # gone mid-stream, without the final chunk
            return resp
        await asyncio.sleep(0.3 if tok == "SLOW" and "start" in event else 0)
    await resp.write_eof()
    return resp


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A fresh, empty database for the in-process gateway and helpers."""
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "SCHEMA", "PRAGMA synchronous = OFF;" + store.SCHEMA)  # throwaway data
    store.db.cache_clear()
    yield tmp_path
    if store.db.cache_info().currsize:
        store.db().close()
    store.db.cache_clear()


@pytest.fixture
async def gw(data, aiohttp_server, aiohttp_client, monkeypatch, caplog):
    """A gateway with two accounts (the first one at its usage limit) and a key for alice. Also has what the
    fake API was sent (seen), the gateway's pool, and alice's request headers (dev)."""
    caplog.set_level(logging.INFO, logger="hotpocket")  # as server.main sets it
    api = web.Application()
    api[SEEN] = []
    api.router.add_route("*", "/{tail:.*}", fake_api)
    monkeypatch.setattr(proxy, "UPSTREAM", str((await aiohttp_server(api)).make_url("")).rstrip("/"))
    use_accounts("LIMITED", "GOOD")
    app = server.make_app()
    client = await aiohttp_client(app)
    client.seen, client.pool, client.dev = api[SEEN], app[POOL], dev(store.new_key("alice"))
    return client


def use_accounts(*suffixes):
    """Replace the pool's accounts with ones whose tokens end in these (see fake_api), labeled in lowercase."""
    store.write("DELETE FROM accounts")
    for suffix in suffixes:
        store.save_account(suffix.lower(), f"sk-ant-oat01-{suffix}")


def block(gw, suffix, *args):
    """Block the account use_accounts made for this suffix (see Pool.block)."""
    gw.pool.block(suffix.lower(), f"sk-ant-oat01-{suffix}", *args)


def dev(key, session="s1"):
    """A developer's request headers, as Claude Code (and Tailscale) send them."""
    return {"authorization": f"Bearer {key}", "x-claude-code-session-id": session,
            "anthropic-beta": "claude-code-20250219", "tailscale-user-login": "alice@example.com"}


async def call(gw, headers=None, data=b'{"model": "x"}'):
    resp = await gw.post("/v1/messages?beta=true", headers=gw.dev if headers is None else headers, data=data)
    return resp, await resp.read()


def toks(gw, since=0):
    """Which accounts (by token suffix) the fake API has heard from."""
    return [s["tok"] for s in gw.seen[since:]]


async def status(gw, headers=None):
    """The status page's data, with accounts and developers by name."""
    snap = await (await gw.get("/_gateway/status", headers=headers)).json()
    return {**snap, "accounts": {a["label"]: a for a in snap["accounts"]},
            "developers": {d["name"]: d for d in snap["developers"]}}


def as_user(login="admin@example.com", origin="page"):
    """What Tailscale serve and the browser add when someone uses the status page. The tailnet policy
    grants admin@example.com the admin capability."""
    caps = {ADMIN_CAP: [{}]} if (login or "").lower() == "admin@example.com" else {}
    headers = {"tailscale-user-login": login, "tailscale-app-capabilities": json.dumps(caps),
               "x-forwarded-host": HOST, "origin": f"https://{HOST}" if origin == "page" else origin}
    return {k: v for k, v in headers.items() if v}


async def manage(gw, method, path, body=None, **who):
    """An edit from the status page, by admin@example.com unless `who` says otherwise."""
    resp = await gw.request(method, "/_gateway/api/" + path, json=body, headers=as_user(**who))
    assert resp.status != 200 or resp.headers["cache-control"] == "no-store"  # may hold a new key
    return resp.status, await resp.json()
