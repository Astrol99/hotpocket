"""The API side: Claude Code's requests, checked against the developer keys and sent on through an account
from the pool, trying the next account when one turns a request down, with the reply streamed back."""

import asyncio
import json
import logging
import math
import os
import time

import aiohttp
from aiohttp import web
from humanize import naturaldelta
from multidict import CIMultiDict
from yarl import URL

from .pool import POOL
from .store import is_api_key, key_owner, load_accounts
from .tailscale import refused
from .usage import OUT_OF_CREDIT, UNIFIED, next_month, reset_time, usage_of

log = logging.getLogger(__name__)

UPSTREAM = os.environ.get("HOTPOCKET_UPSTREAM", "https://api.anthropic.com").rstrip("/")
OAUTH_BETA = "oauth-2025-04-20"  # the beta that lets subscription (OAuth) tokens call the API
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
NOT_UPSTREAM = HOP | {"authorization", "x-api-key", "x-hotpocket-key", "accept-encoding", "anthropic-beta"}
NOT_DOWNSTREAM = HOP | {"server", "date"}

CLIENT = web.AppKey("client", aiohttp.ClientSession)  # the app's HTTP client for the API
routes = web.RouteTableDef()  # added after the page's: the last one takes every path


async def client(app):
    """The HTTP client for the API, open while the app runs. No Accept-Encoding goes up, so replies come
    back uncompressed, are passed on untouched, and their usage can be read. Cookies are never kept, so
    none ride along from one account or developer to another."""
    async with aiohttp.ClientSession(auto_decompress=False, skip_auto_headers=("Accept-Encoding", "User-Agent"),
                                     timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=900),
                                     connector=aiohttp.TCPConnector(limit=0),
                                     cookie_jar=aiohttp.DummyCookieJar()) as app[CLIENT]:
        yield


def api_error(status, etype, message, headers=None):
    """An error shaped like the API's own, which Claude Code knows how to show."""
    return web.json_response({"type": "error", "error": {"type": etype, "message": message}},
                             status=status, headers=headers)


@routes.get("/api/hello")
async def hello(_request):
    # Claude Code's keyless connectivity check; answer it the way the API does.
    return web.json_response({"message": "hello"})


@routes.route("*", "/{tail:.*}")
async def proxy(request):
    """Every request that isn't for the status page: an API call, sent on to an account from the pool."""
    started, pool = time.time(), request.app[POOL]
    dev = developer(request.headers)
    if dev is None:
        # Log refused API calls, not browsers asking for /favicon.ico and the like.
        if request.path.startswith("/v1/") or "authorization" in request.headers or "x-api-key" in request.headers:
            refused(request, "missing or unknown developer key")
        # The gateway never sends 401: Claude Code retries a 401 for minutes before giving up.
        return api_error(403, "permission_error", "hotpocket: missing or unknown developer key. "
                         "Get a new one on the hotpocket status page and paste its setup prompt into Claude Code.")
    tag = f"{dev} {request.method} {request.path}"
    accounts = load_accounts()
    if not accounts:
        return api_error(503, "api_error", "hotpocket: no accounts configured.")
    body = await request.read()  # buffered so the request can be resent on another account
    session = request.headers.get("x-claude-code-session-id") or f"dev:{dev}"
    url = URL(UPSTREAM + request.rel_url.raw_path_qs, encoded=True)  # even "http://host/..." comes as just a path
    tried, turned = [], None
    while (label := pool.choose(accounts, session, tried)) is not None:
        tried.append(label)
        token = accounts[label]
        try:
            resp = await request.app[CLIENT].request(request.method, url, data=body or None, allow_redirects=False,
                                                     headers=upstream_headers(request.headers, token))
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning(f"{tag} -> {label}: upstream error {e!r}")
            return api_error(502, "api_error", f"hotpocket: could not reach the Anthropic API ({type(e).__name__}).")
        async with resp:
            pool.observe(label, resp.headers)
            if resp.status in (401, 403, 429) or (resp.status in (400, 402) and is_api_key(token)):
                turned = await turned_down(pool, label, token, resp) or turned
                continue
            out, reply = await relay(request, resp, f"{tag} -> {label}")
        pool.served(label, session, dev, *usage_of(reply))
        switched = f" (switched from {', '.join(tried[:-1])})" if len(tried) > 1 else ""
        log.info(f"{tag} -> {label} {resp.status} {time.time() - started:.1f}s{switched}")
        return out
    if turned:
        return refused_by_all(tag, tried, *turned)
    return all_blocked(pool, tag, accounts)


def developer(headers):
    """The developer whose key a request carries, or None."""
    # A developer still logged in to claude.ai (so Artifacts and connectors keep working) sends the key
    # in its own header, and their own claude.ai token as the bearer, which is dropped. Otherwise the
    # key is the bearer (or x-api-key).
    auth = headers.get("authorization", "")
    presented = (headers.get("x-hotpocket-key")
                 or (auth[7:] if auth.lower().startswith("bearer ") else headers.get("x-api-key", ""))).strip()
    return key_owner(presented) if presented else None


def upstream_headers(headers, token):
    """The developer's headers, less their key, Tailscale's and the hop-by-hop ones, with the account's token
    as the bearer: an API key as is, a seat's token with the beta that lets it call the API."""
    out = {k: v for k, v in headers.items()
           if k.lower() not in NOT_UPSTREAM and not k.lower().startswith(("tailscale-", "x-forwarded-"))}
    # A developer logged in to claude.ai sends the OAuth beta too; it goes up only with a seat's token.
    betas = [b for v in headers.getall("anthropic-beta", []) for b in map(str.strip, v.split(","))
             if b and b != OAUTH_BETA]
    out["authorization"] = f"Bearer {token}"
    if not is_api_key(token):
        betas.append(OAUTH_BETA)
    if betas:
        out["anthropic-beta"] = ",".join(dict.fromkeys(betas))
    return out


def downstream_headers(headers):
    # Per-account usage headers would make Claude Code warn about one account's
    # limit while the pool still has room, so they are dropped.
    return CIMultiDict((k, v) for k, v in headers.items()
                       if k.lower() not in NOT_DOWNSTREAM and not k.lower().startswith(UNIFIED))


async def turned_down(pool, label, token, resp):
    """An account turned a request down. Blocks the account if it's about the account; returns the API's
    answer if it may be about the request, for when no account takes it."""
    try:
        error = await resp.read()  # also lets the connection be reused
    except (aiohttp.ClientError, asyncio.TimeoutError):
        error = b""
    if resp.status == 401 or b"OAuth token has been revoked" in error:  # a revoked token gets a 403
        pool.block(label, token, "auth")
    elif resp.headers.get(UNIFIED + "status") == "rejected":  # a seat's usage limit (API keys have no such header)
        pool.block(label, token, "limit", reset_time(resp.headers), resp.headers.get(UNIFIED + "representative-claim"))
    elif is_api_key(token) and (resp.status == 402 or ("retry-after" not in resp.headers
                                                       and any(s in error for s in OUT_OF_CREDIT))):
        pool.block(label, token, "credit", next_month())  # its monthly credits or spend limit are used up
    else:
        # Any other refusal can be about this request (say, a model or a long context the plan doesn't
        # cover) or this account (its organization), so the account isn't blocked for everyone; the
        # request just tries the next one.
        return resp.status, downstream_headers(resp.headers), error
    return None


async def relay(request, resp, what):
    """Stream the upstream response through as it arrives (SSE-friendly); also returns a copy of it,
    for the token counts in it."""
    out = web.StreamResponse(status=resp.status, reason=resp.reason, headers=downstream_headers(resp.headers))
    reply, keep = bytearray(), resp.content_type in ("text/event-stream", "application/json")  # not downloads
    try:
        await out.prepare(request)
        async for chunk in resp.content.iter_any():
            if keep:
                reply += chunk
            await out.write(chunk)
        await out.write_eof()
    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
        log.warning(f"{what}: stream ended early ({e!r})")  # upstream or developer dropped mid-stream
        # Drop the developer's connection without the final chunk, so a truncated
        # body is seen as a failure instead of a complete response.
        if request.transport is not None:
            request.transport.close()
    return out, reply


def refused_by_all(tag, tried, status, headers, error):
    """No account took a request, and one turned it down for a reason that may be the request's own."""
    try:
        message = str(json.loads(error)["error"]["message"])
    except (ValueError, KeyError, TypeError):
        message = None
    log.warning(f"{tag}: refused by {', '.join(tried)} ({status}: {(message or error.decode(errors='replace'))[:200]})")
    if message is not None:  # Anthropic's own answer, with its retry-after and x-should-retry
        headers.setdefault("content-type", "application/json")
        return web.Response(status=status, body=error, headers=headers)
    # Not Anthropic's JSON error (empty, or a proxy's HTML page): Claude Code gets one it can read.
    headers.popall("content-type", None)
    return api_error(status, "rate_limit_error" if status == 429 else "permission_error",
                     f"hotpocket: every team account refused this request ({status}).", headers=headers)


def all_blocked(pool, tag, accounts):
    """Every account is blocked: answer according to why."""
    if not (first := pool.first_back(accounts)):
        log.warning(f"{tag}: all {len(accounts)} account tokens rejected")
        return api_error(403, "permission_error", "hotpocket: every team account token was "
                         "rejected. Ask your admin to replace them.")
    until, claim = first
    reset = max(until, time.time() + 1)
    back = naturaldelta(reset - time.time())
    log.warning(f"{tag}: all {len(accounts)} accounts blocked; first back in {back}")
    # Shaped like a subscription usage limit so Claude Code shows its normal
    # "limit reached, resets at ..." message (and can wait for the reset).
    return api_error(429, "rate_limit_error",
                     f"hotpocket: all {len(accounts)} team accounts are at their usage limit "
                     f"or unavailable. The first one is back in {back}.",
                     headers={UNIFIED + "status": "rejected", UNIFIED + "reset": str(math.ceil(reset)),
                              UNIFIED + "representative-claim": claim or "five_hour",
                              "retry-after": str(math.ceil(reset - time.time()))})
