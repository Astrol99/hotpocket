"""The status page: its files, its live data, who it says is viewing, and the edits made on it."""

import asyncio
import json
import sqlite3

import pytest

from hotpocket import store

from conftest import HOST, as_user, call, dev, manage, status

TOKEN = "sk-ant-oat01-NEW"


async def test_the_page_and_its_files(gw):
    resp = await gw.get("/")
    assert "<title>hotpocket</title>" in await resp.text()
    csp = resp.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'self';" in csp and "style-src 'self';" in csp  # nothing inline
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    for name, kind in (("page.css", "css"), ("page.js", "javascript")):  # checked again on every load
        resp = await gw.get("/_gateway/" + name)
        assert resp.status == 200 and resp.headers["cache-control"] == "no-cache"
        assert resp.content_type.endswith(kind)  # a stylesheet with the wrong type isn't applied
    assert (await gw.get("/_gateway/page.html")).status == 403  # only at /, with its headers
    assert gw.seen == []


async def test_open_pages_get_their_data_and_every_change(gw):
    assert (await gw.head("/_gateway/events")).status == 403  # not the stream: its reply would end at the headers
    events = await gw.get("/_gateway/events", headers=as_user())
    assert events.headers["content-type"] == "text/event-stream"
    data = lambda: asyncio.wait_for(events.content.readuntil(b"\n\n"), 5)
    assert json.loads((await data()).removeprefix(b"data: "))["viewer"]["admin"]  # at once, for whoever opened it
    await call(gw)
    assert [d["requests"] for d in json.loads((await data()).removeprefix(b"data: "))["developers"]] == [1]


async def test_the_page_learns_who_is_viewing_and_who_is_an_admin_from_tailscale(gw):
    for login, viewer in (("Admin@Example.com", {"login": "admin@example.com", "admin": True}),
                          ("bob@example.com", {"login": "bob@example.com", "admin": False}),
                          (None, {"login": None, "admin": False})):  # a tagged device
        assert (await status(gw, headers=as_user(login)))["viewer"] == viewer


# --------------------------------------------------------------------------- edits

async def test_editing_needs_https_from_the_page_and_admin_for_accounts(gw):
    for who in ({"origin": f"http://{HOST}"}, {"origin": "https://evil.example"}, {"origin": None},
                {"login": None}):
        for method, path in (("PUT", "accounts/web"), ("POST", "my-key")):
            assert (await manage(gw, method, path, {"token": TOKEN}, **who))[0] == 403, (path, who)
    assert (await manage(gw, "PUT", "accounts/web", {"token": TOKEN}, login="bob@x.com"))[0] == 403
    assert await manage(gw, "PUT", "accounts/web", {"token": f" {TOKEN}\n"}) == (200, {})


async def test_admin_edits_accounts_and_keys(gw, caplog):
    assert (await manage(gw, "PUT", "accounts/web", {"token": "nope"}))[0] == 400
    status_, res = await manage(gw, "PUT", "accounts/bad%20label", {"token": TOKEN})
    assert status_ == 400 and "names are" in res["error"]["message"]
    assert (await manage(gw, "PUT", "accounts/.", {"token": TOKEN}))[0] in (400, 403, 404)
    with pytest.raises(store.EditError):
        store.new_key("..")
    _, res = await manage(gw, "POST", "keys/bob")
    assert (await call(gw, dev(res["key"], "bob")))[0].status == 200
    assert (await manage(gw, "DELETE", "keys/BOB"))[0] == 200  # names ignore case, like Tailscale logins
    assert (await call(gw, dev(res["key"], "bob")))[0].status == 403
    assert "bob" not in (await status(gw))["developers"]  # removed, history and all
    assert (await manage(gw, "DELETE", "accounts/good"))[0] == 200
    assert await manage(gw, "DELETE", "accounts/good") == (400, {"type": "error", "error": {"type": "invalid_request_error", "message": "No account named 'good'."}})
    log = caplog.text
    assert "admin admin@example.com: created a key for bob" in log
    assert "admin admin@example.com: removed account good" in log
    assert "admin admin@example.com: removed developer bob" in log  # as stored, not as typed


async def test_anyone_gets_and_replaces_only_their_own_key(gw, caplog):
    status_, first = await manage(gw, "POST", "my-key", login="Bob@Example.com")
    assert status_ == 200 and "bob@example.com" in (await status(gw))["developers"]
    assert (await call(gw, dev(first["key"])))[0].status == 200
    _, second = await manage(gw, "POST", "my-key", login="bob@example.com")
    assert (await call(gw, dev(first["key"])))[0].status == 403
    assert (await call(gw, dev(second["key"])))[0].status == 200
    assert (await manage(gw, "POST", "keys/alice", login="bob@example.com"))[0] == 403
    assert (await manage(gw, "POST", "my-key", login="o'brien@example.com"))[0] == 400  # can't name a key
    assert "bob@example.com: created their own key" in caplog.text


async def test_anyone_sees_their_own_key_again_and_only_theirs(gw, caplog):
    _, made = await manage(gw, "POST", "my-key", login="bob@example.com")
    touched = gw.pool.wake
    shown = await manage(gw, "POST", "my-key/show", login="Bob@Example.com")
    assert shown == (200, {"key": made["key"]})  # the same key, for another device
    assert not touched.is_set()  # a read: open pages aren't sent anything
    assert (await manage(gw, "POST", "my-key/show", login="bob@example.com", origin="https://evil.example"))[0] == 403
    assert await manage(gw, "POST", "my-key/show", login="eve@example.com") == (200, {})  # has none yet
    assert await manage(gw, "POST", "my-key/show") == (200, {})  # an admin sees only their own, and has none
    _, new = await manage(gw, "POST", "my-key", login="bob@example.com")
    assert await manage(gw, "POST", "my-key/show", login="bob@example.com") == (200, {"key": new["key"]})
    assert "bob@example.com: showed their own key" in caplog.text


async def test_a_key_from_before_keys_were_kept_works_but_cant_be_shown(gw):
    store.write("INSERT INTO keys (name, hash, created) VALUES (?, ?, 0)", "old@example.com", store.key_hash("hp-old"))
    assert (await call(gw, dev("hp-old")))[0].status == 200
    assert await manage(gw, "POST", "my-key/show", login="old@example.com") == (200, {"key": None})


def test_an_old_database_gets_somewhere_to_keep_keys(data):
    con = sqlite3.connect(data / "hotpocket.db")  # as hotpocket made it before keys were kept
    con.execute("CREATE TABLE keys (name TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, created REAL NOT NULL)")
    con.execute("INSERT INTO keys VALUES ('old@example.com', 'abc', 0)")
    con.commit()
    con.close()
    key = store.new_key("new@example.com")
    assert store.stored_key("new@example.com") == key and store.key_owner(key) == "new@example.com"
    assert store.stored_key("old@example.com") is None


def test_a_key_shown_again_is_always_the_one_that_works(data):
    store.new_key("bob")
    store.write("INSERT OR REPLACE INTO keys VALUES ('bob', ?, 0)", store.key_hash("hp-other"))  # an older hotpocket's
    assert store.stored_key("bob") is None  # not the kept key, which no longer works
    store.remove_key("bob")
    assert store.db().execute("SELECT count(*) FROM kept_keys").fetchone() == (0,)  # removed keys aren't kept


async def test_locked_database_gives_the_page_a_503(gw):
    store.db().execute("PRAGMA busy_timeout = 50")  # the test needn't wait out the full second
    locker = sqlite3.connect(store.DATA / "hotpocket.db", isolation_level=None)  # say, an open sqlite3 shell
    locker.execute("BEGIN IMMEDIATE")
    status_, res = await manage(gw, "POST", "keys/busy")
    locker.close()
    assert status_ == 503 and "busy" in res["error"]["message"]
