"""Claude Code's API calls: keys, choosing and switching accounts, and what comes back."""

import asyncio
import time

import aiohttp
import pytest

from hotpocket import store
from hotpocket.usage import UNIFIED as U
from hotpocket.usage import next_month

from conftest import block, call, dev, status, toks, use_accounts


async def test_keyless_requests_never_reach_the_api(gw, caplog):
    assert (await gw.head("/api/hello")).status == 200  # Claude Code's connectivity check
    assert (await gw.get("/favicon.ico")).status == 403
    assert "favicon" not in caplog.text  # browser probes leave no log line
    assert gw.seen == []


async def test_missing_or_wrong_key_is_403_never_401(gw):
    # Claude Code retries a 401 for minutes before giving up.
    resp, body = await call(gw, headers={})
    assert resp.status == 403 and b"developer key" in body
    assert (await call(gw, headers=dev("hp-wrong")))[0].status == 403


async def test_limited_account_is_skipped_then_remembered(gw):
    resp, body = await call(gw)
    assert resp.status == 200 and body.count(b"event: ") == 3
    await call(gw)
    assert toks(gw) == ["LIMITED", "GOOD", "GOOD"]


async def test_a_session_moves_only_when_another_account_serves_it(gw):
    use_accounts("GOOD", "THROTTLE")
    await call(gw)  # s1 is served by good
    block(gw, "GOOD", "limit", time.time() + 60)
    assert (await call(gw))[0].status == 429  # throttle refuses it, so nothing served it
    assert gw.pool.sessions["s1"][0] == "good"  # and its prompt cache is still on good


async def test_responses_stream_through_as_they_arrive(gw):
    use_accounts("SLOW")
    started = time.monotonic()
    resp = await gw.post("/v1/messages", headers=gw.dev, data=b"{}")
    await resp.content.readany()
    first = time.monotonic() - started
    await resp.read()
    assert first < 0.25 and time.monotonic() - started > 0.3


async def test_headers_going_up_and_coming_back(gw):
    resp, _ = await call(gw)
    # One account's usage would make Claude Code warn while the pool still has room.
    assert not any(k.lower().startswith(U) for k in resp.headers)
    assert gw.seen[-1]["path"] == "/v1/messages?beta=true"  # the query string goes along
    sent = gw.seen[-1]["headers"]
    assert sent["authorization"] == "Bearer sk-ant-oat01-GOOD"
    assert sent["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
    assert not {"x-api-key", "x-hotpocket-key", "tailscale-user-login", "x-forwarded-for"} & {k.lower() for k in sent}


async def test_key_in_its_own_header_keeps_the_developers_claude_login_out(gw):
    # Claude Code still logged in to claude.ai sends its own token as the bearer and the key alongside.
    key = gw.dev["authorization"].removeprefix("Bearer ")
    headers = dict(gw.dev, authorization="Bearer sk-ant-oat01-ALICES-OWN-LOGIN", **{"x-hotpocket-key": key})
    assert (await call(gw, headers))[0].status == 200
    sent = gw.seen[-1]["headers"]
    assert sent["authorization"] == "Bearer sk-ant-oat01-GOOD" and "x-hotpocket-key" not in sent
    assert "ALICES-OWN-LOGIN" not in str(dict(sent))  # the developer's own token never reaches upstream
    assert (await call(gw, dict(headers, **{"x-hotpocket-key": "hp-wrong"})))[0].status == 403


async def test_chunked_request_body(gw):
    async def body():
        yield b'{"model":'
        yield b' "chunked"}'
    assert (await call(gw, data=body()))[0].status == 200
    assert gw.seen[-1]["body"] == b'{"model": "chunked"}'


async def test_absolute_form_target_goes_to_the_api_not_its_host(gw):
    # "http://evil.example/..." glued onto the upstream URL would send a token elsewhere.
    reader, writer = await asyncio.open_connection(gw.host, gw.port)
    writer.write(f"POST http://evil.example/v1/messages HTTP/1.1\r\nHost: x\r\n"
                 f"authorization: {gw.dev['authorization']}\r\ncontent-length: 2\r\n\r\n{{}}".encode())
    status_line = await reader.readline()
    writer.close()
    assert b" 200 " in status_line and gw.seen[-1]["path"] == "/v1/messages"


async def test_truncated_upstream_stream_reaches_the_developer_as_an_error(gw, caplog):
    use_accounts("TRUNC")
    resp = await gw.post("/v1/messages", headers=gw.dev, data=b"{}")
    with pytest.raises(aiohttp.ClientPayloadError):
        await resp.read()
    assert "stream ended early" in caplog.text


# --------------------------------------------------------------------------- accounts that turn a request down

async def test_all_accounts_limited_looks_like_a_usage_limit(gw):
    use_accounts("LIMITED")
    resp, _ = await call(gw)
    assert resp.status == 429 and resp.headers[U + "status"] == "rejected"
    assert int(resp.headers[U + "reset"]) > time.time() + 7000
    # With several, Claude Code hears about the one back first: when, and which of its limits it hit.
    use_accounts("A", "B")
    block(gw, "A", "limit", time.time() + 5 * 86400, "seven_day")
    block(gw, "B", "limit", time.time() + 7200, "five_hour")
    h = (await call(gw))[0].headers
    assert h[U + "representative-claim"] == "five_hour" and int(h[U + "reset"]) < time.time() + 7300


async def test_rejected_and_throttled_accounts_are_skipped(gw):
    use_accounts("BAD", "THROTTLE", "GOOD")
    assert (await call(gw))[0].status == 200 and toks(gw) == ["BAD", "THROTTLE", "GOOD"]
    acc = (await status(gw))["accounts"]
    assert acc["bad"]["blocked_kind"] == "auth" and "blocked_kind" not in acc["throttle"]  # a plain 429 blocks no one


@pytest.mark.parametrize("suffixes", [["THROTTLE"], ["THROTTLE", "SLOWDOWN"]])
async def test_a_429_from_every_account_comes_back_as_is_and_blocks_no_one(gw, caplog, suffixes):
    # Such a 429 can be about the request itself, so one request mustn't lock everyone out.
    use_accounts(*suffixes)
    resp, body = await call(gw)
    last = suffixes[-1]
    assert resp.status == 429 and f"{last} says no".encode() in body and U + "status" not in resp.headers
    assert resp.headers.get("retry-after") == ("2" if last == "SLOWDOWN" else None)  # Anthropic's, if any
    assert resp.headers.get("x-should-retry") == ("false" if last == "SLOWDOWN" else None)
    assert resp.content_type == "application/json"
    assert not any("blocked_kind" in a for a in (await status(gw))["accounts"].values())
    assert f"(429: {last} says no)" in caplog.text


async def test_a_429_that_isnt_json_comes_back_as_an_api_error(gw, caplog):
    use_accounts("GARBLED")
    resp, _ = await call(gw)
    assert resp.status == 429 and (await resp.json())["error"]["type"] == "rate_limit_error"
    assert "(429: <html>slow down</html>)" in caplog.text


async def test_a_revoked_token_is_blocked_and_any_other_403_tries_the_next_account(gw):
    # Anthropic answers a revoked token with a 403, which Claude Code would take for its own login revoked.
    use_accounts("REVOKED", "NOMODEL", "GOOD")
    assert (await call(gw))[0].status == 200 and toks(gw) == ["REVOKED", "NOMODEL", "GOOD"]
    acc = (await status(gw))["accounts"]
    assert acc["revoked"]["blocked_kind"] == "auth" and "blocked_kind" not in acc["nomodel"]  # that 403 was about the request
    use_accounts("NOMODEL")
    resp, body = await call(gw)
    assert resp.status == 403 and b"NOMODEL says no" in body  # no account takes it: Anthropic's own answer


async def test_rejected_tokens_get_a_try_only_when_nothing_else_is_left(gw):
    use_accounts("BAD", "GOOD")
    block(gw, "BAD", "auth")
    gw.pool.accounts["bad"]["blocked_until"] = 0  # however long ago that was
    await call(gw)
    assert toks(gw) == ["GOOD"]
    # So if Anthropic rejected every token at once, the first request after it recovers brings the pool back.
    use_accounts("GOOD")
    block(gw, "GOOD", "auth")
    assert (await call(gw))[0].status == 200
    assert "blocked_kind" not in (await status(gw))["accounts"]["good"]
    # Tokens that stay rejected cost each request one try, the longest-rejected first, not one per token.
    use_accounts("BAD", "REVOKED")
    for tok in ("BAD", "REVOKED"):
        block(gw, tok, "auth")
    for expected in ("BAD", "REVOKED", "BAD"):
        seen = len(gw.seen)
        assert (await call(gw))[0].status == 403 and toks(gw, seen) == [expected]


async def test_rejected_token_is_403_and_its_replacement_used_at_once(gw):
    use_accounts("BAD")
    resp, body = await call(gw)
    assert resp.status == 403 and b"rejected" in body
    store.save_account("bad", "sk-ant-oat01-FIXED")  # same label, new token: a block belongs to the token
    assert (await call(gw))[0].status == 200 and toks(gw)[-1] == "FIXED"


# --------------------------------------------------------------------------- an API key's credits

async def test_an_api_key_is_used_only_when_every_seat_is_out(gw):
    store.save_account("credits", "sk-ant-usr01-CREDITS")  # a personal key (others are sk-ant-api...)
    await call(gw)
    assert toks(gw) == ["LIMITED", "GOOD"]  # seats first
    block(gw, "GOOD", "limit", time.time() + 60)
    oauth = {**dev(store.new_key("bob")), "anthropic-beta": "claude-code-20250219,oauth-2025-04-20"}  # on claude.ai
    assert (await call(gw, oauth))[0].status == 200 and toks(gw)[-1] == "CREDITS"
    sent = gw.seen[-1]["headers"]
    assert sent["authorization"] == "Bearer sk-ant-usr01-CREDITS"
    assert sent["anthropic-beta"] == "claude-code-20250219"  # an API key needs no OAuth beta
    assert (await status(gw))["accounts"]["credits"]["month_cost"] > 0
    gw.pool.accounts["good"]["blocked_until"] = 0  # the seat is back
    await call(gw)
    assert toks(gw)[-1] == "GOOD"  # and the session with it


async def test_a_seat_that_only_throttles_keeps_the_request_off_the_credits(gw):
    use_accounts("THROTTLE")
    store.save_account("credits", "sk-ant-api03-CREDITS")
    assert (await call(gw))[0].status == 429 and toks(gw) == ["THROTTLE"]


# Out of credits (the spend cap's 429, a 402, or a 400 saying so), the key waits for next month, and every account being out
# looks like a usage limit; any other 400 is about the request, and comes back as is.
@pytest.mark.parametrize("suffix, code, kind", [("SPENT", 429, "credit"), ("BROKE", 429, "credit"), ("LOWBAL", 429, "credit"),
                                                ("CAPPED", 429, "credit"), ("INVALID", 400, None)])
async def test_an_api_key_out_of_credits_waits_for_next_month(gw, suffix, code, kind):
    use_accounts("LIMITED")
    store.save_account("credits", f"sk-ant-api03-{suffix}")
    assert (await call(gw))[0].status == code and toks(gw) == ["LIMITED", suffix]
    credits = (await status(gw))["accounts"]["credits"]
    assert credits.get("blocked_kind") == kind and credits.get("blocked_until", next_month()) == next_month()


async def test_a_rejected_seat_gets_a_try_before_credits(gw):
    # So if Anthropic rejects every seat's token for a while, credits fill in, and the seats are back once it recovers.
    use_accounts("BAD")
    store.save_account("credits", "sk-ant-api03-CREDITS")
    for _ in range(2):
        seen = len(gw.seen)
        assert (await call(gw))[0].status == 200 and toks(gw, seen) == ["BAD", "CREDITS"]
    use_accounts("GOOD")
    store.save_account("credits", "sk-ant-api03-CREDITS")
    block(gw, "GOOD", "auth")
    seen = len(gw.seen)
    assert (await call(gw))[0].status == 200 and toks(gw, seen) == ["GOOD"]
