"""hotpocket: one Claude Code endpoint for the team, backed by a pool of Claude accounts."""

import asyncio
import datetime
import functools
import hashlib
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import time
from collections import Counter
from pathlib import Path

import aiohttp
from aiohttp import web
from genai_prices import extract_usage
from multidict import CIMultiDict
from yarl import URL

DATA = Path(os.environ.get("HOTPOCKET_DATA", "/var/lib/hotpocket"))  # holds hotpocket.db
UPSTREAM = os.environ.get("HOTPOCKET_UPSTREAM", "https://api.anthropic.com").rstrip("/")
# Tailscale serve's way in, and the only one: whoever reaches it is believed about who they are.
SOCKET = os.environ.get("HOTPOCKET_SOCKET", "/run/hotpocket/hotpocket.sock")
ADMIN_CAP = "github.com/astrol99/hotpocket/admin"  # an app capability the tailnet policy grants admins

OAUTH_BETA = "oauth-2025-04-20"
UNIFIED = "anthropic-ratelimit-unified-"
WINDOWS = {"five_hour": "5h", "seven_day": "7d"}  # our field name -> header infix
STICKY_MAX_UTIL = 0.95      # move a session off its account above this utilization
AUTH_FAIL_COOLDOWN = 600    # a token the API rejected (401)
FALLBACK_BLOCK = 3600       # usage-limit rejection without a reset time
SESSION_TTL = 6 * 3600
MAX_BODY = 32 * 1024 * 1024  # the Messages API's own request size limit
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
REASONS = {"auth": "token rejected", "limit": "usage limit reached"}

# Account labels and developer names. Developer names are often Tailscale logins
# (you@example.com, you@github); all-dot names are refused because "." and ".." break URLs.
NAME_RE = re.compile(r"(?!\.+$)[\w.%+@-]{1,254}", re.ASCII)
TOKEN_RE = re.compile(r"sk-ant-oat[0-9]*-[A-Za-z0-9_-]+")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
NOT_UPSTREAM = HOP | {"authorization", "x-api-key", "x-hotpocket-key", "accept-encoding", "anthropic-beta"}
NOT_DOWNSTREAM = HOP | {"server", "date"}
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def log(msg):
    # Escaped, so a request path like /v1/%0A... can't forge lines in the (audit) log.
    msg = CONTROL_RE.sub(lambda m: f"\\x{ord(m[0]):02x}", msg)
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def until_text(ts):
    return str(datetime.timedelta(seconds=max(0, round(ts - time.time()))))  # 1:59:59, or 1 day, 2:00:00


def num(headers, name):
    try:
        return float(headers[name])
    except (KeyError, ValueError):
        return None


def used(a, window, now):
    """An account's use of a window (0-1, None if unknown). Once its reset time passes it is back to 0."""
    reset = a.get(window + "_reset")
    return 0 if reset is not None and reset <= now else a.get(window)


def usage_of(reply):
    """The model and token usage in a reply. Streamed (SSE), message_start has them and message_delta
    updates the usage; any other reply is one JSON object. Replies without usage give {}."""
    model, usage = None, {}
    for line in reply.splitlines() if reply.startswith(b"event:") else [reply]:
        if b'"usage"' in line:
            try:
                event = json.loads(line.removeprefix(b"data:"))
                message = event.get("message", event)
                # A later event's null or 0 doesn't erase a count already heard (Claude Code merges them so too).
                fresh = {k: v for k, v in message["usage"].items() if v or k not in usage}
                model, usage = message.get("model", model), {**usage, **fresh}
            except (ValueError, AttributeError, KeyError, TypeError):
                pass
    return model, usage


def api_cost(model, usage):
    """What a reply would have cost at API list prices, from genai-prices (update it for new models).
    The seats themselves are flat-rate; 0 for a model it doesn't know or a reply without usage."""
    try:
        return float(extract_usage({"model": model, "usage": usage}, provider_id="anthropic").calc_price().total_price)
    except (LookupError, ValueError):
        return 0.0


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


# --------------------------------------------------------------------------- storage

SCHEMA = """
-- Accounts are tried in `position` order when their usage is tied.
CREATE TABLE IF NOT EXISTS accounts (label TEXT PRIMARY KEY, token TEXT NOT NULL, position INTEGER NOT NULL);
-- Only a hash of each developer key is kept; the key itself is shown once and never stored.
CREATE TABLE IF NOT EXISTS keys (name TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, value TEXT NOT NULL);    -- the pool's saved usage (JSON)
"""


@functools.cache
def db():
    """The database connection (creating the database on first use)."""
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Never waits long: a stuck writer (say an open sqlite3 shell) would stall every stream. A failed
    # usage save retries at the next tick.
    con = sqlite3.connect(DATA / "hotpocket.db", timeout=1)
    con.execute("PRAGMA journal_mode = WAL")  # someone reading it in a sqlite3 shell doesn't hold up writes
    con.executescript(SCHEMA)
    return con


def write(sql, *params):
    """Run one change; returns how many rows it touched."""
    with db() as con:
        return con.execute(sql, params).rowcount


def load_accounts():
    """{label: token}, in the order the accounts were added."""
    return dict(db().execute("SELECT label, token FROM accounts ORDER BY position"))


def key_names():
    return [name for (name,) in db().execute("SELECT name FROM keys ORDER BY created")]


# --------------------------------------------------------------------------- pool

# Everything here runs on the event loop's single thread, so no locks are needed.

class Pool:
    def __init__(self):
        row = db().execute("SELECT value FROM state WHERE name = 'pool'").fetchone()
        saved = json.loads(row[0]) if row else {}
        self.accounts = saved.get("accounts", {})  # label -> usage, block and request count
        self.devs = saved.get("devs", {})
        self.sessions = {}                          # session id -> (label, last seen)
        self.wake = asyncio.Event()  # set (and replaced) on every change; open status pages wait on it
        self.since = saved.get("since") or time.time()  # when token counting started
        self.dirty = False

    def touch(self, stopping=False):
        """Something the status page shows changed: save it at the next tick, and wake open pages now.
        When stopping, they are woken one last time, to close."""
        self.dirty = True
        if self.wake:
            self.wake.set()
            self.wake = None if stopping else asyncio.Event()

    def _acct(self, label):
        return self.accounts.setdefault(label, {"requests": 0})

    def blocked(self, label, token, now):
        # A block belongs to the token that earned it, so replacing a token clears it.
        a = self.accounts.get(label, {})
        return a.get("blocked_until", 0) > now and a.get("blocked_token") == key_hash(token)[:16]

    def _util(self, label):
        a, now = self.accounts.get(label, {}), time.time()
        return max(used(a, w, now) or 0 for w in WINDOWS)

    def choose(self, accounts, session, exclude):
        now = time.time()
        left = [l for l in accounts if l not in exclude]
        ready = [l for l in left if not self.blocked(l, accounts[l], now)]
        if not ready:
            # A rejected token gets another try when nothing else is left (the request would fail anyway),
            # so if Anthropic rejected every token at once, the pool is back as soon as it recovers. Only one
            # per request, the longest-rejected first: revoked tokens cost a request one call, not one each.
            rejected = [l for l in left if self.accounts[l].get("blocked_kind") == "auth"]
            if not rejected or any(self.accounts.get(l, {}).get("blocked_kind") == "auth" for l in exclude):
                return None
            return min(rejected, key=lambda l: self.accounts[l]["blocked_until"])
        sticky = self.sessions.get(session, (None,))[0]
        return (sticky if sticky in ready and self._util(sticky) < STICKY_MAX_UTIL
                else min(ready, key=self._util))  # ties go to the earlier-added account

    def observe(self, label, headers):
        """Record the account's live usage from the API's response headers."""
        a, heard = self._acct(label), False
        for window, infix in WINDOWS.items():
            for key, value in ((window, num(headers, f"{UNIFIED}{infix}-utilization")),
                               (window + "_reset", num(headers, f"{UNIFIED}{infix}-reset"))):
                if value is not None:
                    a[key], a["usage_at"], heard = value, time.time(), True  # when it was last heard
        if heard:
            self.touch()

    def block(self, label, token, kind, until, claim=None):
        self._acct(label).update(blocked_kind=kind, blocked_until=until, blocked_claim=claim,
                                 blocked_token=key_hash(token)[:16])  # which token earned it
        self.touch()
        hint = "; replace its token on the status page" if kind == "auth" else ""
        log(f"account {label}: {REASONS[kind]}{hint}; back in {until_text(until)}")

    def served(self, label, dev, model, usage):
        d, a, cost, now = self.devs.setdefault(dev, {"requests": 0}), self._acct(label), api_cost(model, usage), time.time()
        # It works again, so a rejection is over; a usage limit another request hit meanwhile stays.
        if a.get("blocked_until", 0) <= now or a.get("blocked_kind") == "auth":
            a["blocked_kind"], a["blocked_until"] = None, 0
        windows = a.setdefault("spent", {})
        for w in WINDOWS:  # what each developer spent in the account's current windows, to split its use by
            spent = windows.get(w)
            if not spent or (spent["reset"] or 0) <= now:  # that window is over: this request starts the next
                spent = windows[w] = {"reset": a.get(w + "_reset"), "devs": {}}
            spent["devs"][dev] = spent["devs"].get(dev, 0) + cost
        for entry in (a, d):  # the same tally per account and per developer
            entry["requests"] += 1
            entry["cost"] = entry.get("cost", 0) + cost
            entry["tokens"] = Counter(entry.get("tokens"))
            entry["tokens"].update({f: usage.get(f) or 0 for f in TOKEN_FIELDS})
        d["last_seen"], d["last_account"] = now, label
        self.touch()

    def snapshot(self, accounts, keys):
        now, rows, shares = time.time(), [], {w: Counter() for w in WINDOWS}
        for label, token in accounts.items():
            a = self.accounts.get(label, {})
            # Shown as rejected until replaced or working again, though its block lapses so it's retried.
            rejected = a.get("blocked_kind") == "auth" and a.get("blocked_token") == key_hash(token)[:16]
            blocked = rejected or self.blocked(label, token, now)
            rows.append({
                "label": label, "available": not blocked,
                "blocked_kind": a["blocked_kind"] if blocked else None,
                "blocked_reason": REASONS[a["blocked_kind"]] if blocked else None,
                "blocked_until": a["blocked_until"] if blocked else None,
                **{w: used(a, w, now) for w in WINDOWS}, **{w + "_reset": a.get(w + "_reset") for w in WINDOWS},
                "usage_at": a.get("usage_at"), "requests": a.get("requests", 0),
                "tokens": a.get("tokens", {}), "cost": a.get("cost", 0),
            })
            # Developers' shares of the pool's limits: the account's use in its current window, split between
            # the developers who used it by what their requests would cost at API prices.
            for w in WINDOWS:
                spent, use = a.get("spent", {}).get(w), min(1, used(a, w, now) or 0)
                if spent and (spent["reset"] or 0) > now and (total := sum(spent["devs"].values())):
                    for dev, cost in spent["devs"].items():
                        shares[w][dev] += use * cost / total / len(accounts)
        devs = [{"name": n, "requests": 0, **self.devs.get(n, {}), **{w: shares[w][n] for w in WINDOWS}} for n in keys]
        return {"accounts": rows, "developers": devs, "since": self.since}

    def tick(self):
        """Every few seconds: forget idle sessions and removed accounts and developers, and save if anything changed."""
        cutoff = time.time() - SESSION_TTL
        self.sessions = {s: v for s, v in self.sessions.items() if v[1] > cutoff}
        for kept, current in ((self.accounts, load_accounts()), (self.devs, key_names())):
            for gone in kept.keys() - set(current):
                del kept[gone]
                self.dirty = True
        if self.dirty:
            write("INSERT OR REPLACE INTO state VALUES ('pool', ?)",
                  json.dumps({"accounts": self.accounts, "devs": self.devs, "since": self.since}))
            self.dirty = False


POOL: Pool = None
CLIENT: aiohttp.ClientSession = None


def reset_time(headers):
    """When a usage-limit rejection lifts, from the response headers."""
    rejected = [num(headers, f"{UNIFIED}{infix}-reset") for infix in WINDOWS.values()
                if headers.get(f"{UNIFIED}{infix}-status") == "rejected"]
    retry = num(headers, "retry-after")
    return (num(headers, UNIFIED + "reset") or max(filter(None, rejected), default=None)
            or time.time() + (retry if retry is not None else FALLBACK_BLOCK))


# --------------------------------------------------------------------------- HTTP

def api_error(status, etype, message, headers=None):
    return web.json_response({"type": "error", "error": {"type": etype, "message": message}},
                             status=status, headers=headers)


def developer(headers):
    # A developer still logged in to claude.ai (so Artifacts and connectors keep working) sends the key
    # in its own header, and their own claude.ai token as the bearer, which is dropped. Otherwise the
    # key is the bearer (or x-api-key).
    auth = headers.get("authorization", "")
    presented = (headers.get("x-hotpocket-key")
                 or (auth[7:] if auth.lower().startswith("bearer ") else headers.get("x-api-key", ""))).strip()
    if not presented:
        return None
    row = db().execute("SELECT name FROM keys WHERE hash = ?", (key_hash(presented),)).fetchone()
    return row and row[0]


def upstream_headers(headers):
    out = {k: v for k, v in headers.items()
           if k.lower() not in NOT_UPSTREAM and not k.lower().startswith(("tailscale-", "x-forwarded-"))}
    betas = [b.strip() for v in headers.getall("anthropic-beta", []) for b in v.split(",")]
    out["anthropic-beta"] = ",".join(dict.fromkeys([b for b in betas if b] + [OAUTH_BETA]))
    return out


async def proxy(request):
    started = time.time()
    dev = developer(request.headers)
    if dev is None:
        # Log refused API calls, not browsers asking for /favicon.ico and the like.
        if request.path.startswith("/v1/") or "authorization" in request.headers or "x-api-key" in request.headers:
            log(f"refused {request.method} {request.rel_url.raw_path} from {source(request)}: "
                "missing or unknown developer key")
        # The gateway never sends 401: Claude Code retries a 401 for minutes before giving up.
        return api_error(403, "permission_error", "hotpocket: missing or unknown developer key. "
                         "Get a new one on the hotpocket status page and paste its setup prompt into Claude Code.")
    if not request.raw_path.startswith("/"):
        # An absolute-form target ("http://host/...") would be glued onto UPSTREAM's host.
        return api_error(400, "invalid_request_error", "hotpocket: request target must be a path.")
    tag = f"{dev} {request.method} {request.path}"
    accounts = load_accounts()
    if not accounts:
        return api_error(503, "api_error", "hotpocket: no accounts configured.")
    body = await request.read()  # buffered so the request can be resent on another account
    session = request.headers.get("x-claude-code-session-id") or f"dev:{dev}"
    headers = upstream_headers(request.headers)
    url = URL(UPSTREAM + request.raw_path, encoded=True)
    tried, refused = [], None
    while (label := POOL.choose(accounts, session, tried)) is not None:
        tried.append(label)
        token = accounts[label]
        headers["authorization"] = f"Bearer {token}"
        try:
            resp = await CLIENT.request(request.method, url, headers=headers, data=body or None,
                                        allow_redirects=False)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log(f"{tag} -> {label}: upstream error {e!r}")
            return api_error(502, "api_error",
                             f"hotpocket: could not reach the Anthropic API ({type(e).__name__}).")
        async with resp:
            POOL.observe(label, resp.headers)
            if resp.status in (401, 403, 429):
                try:
                    error = await resp.read()  # also lets the connection be reused
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    error = b""
                if resp.status == 401 or b"OAuth token has been revoked" in error:  # a revoked token gets a 403
                    POOL.block(label, token, "auth", time.time() + AUTH_FAIL_COOLDOWN)
                elif resp.headers.get(UNIFIED + "status") == "rejected":
                    POOL.block(label, token, "limit", reset_time(resp.headers),
                               resp.headers.get(UNIFIED + "representative-claim"))
                else:
                    # Any other 403 or 429 can be about this request (say, a model or a long context the
                    # plan doesn't cover) or this account (its organization), so the account isn't blocked
                    # for everyone; the request just tries the next one, and gets this answer if none takes it.
                    refused = (resp.status, downstream_headers(resp.headers), error)
                continue
            out, reply = await relay(request, resp, f"{tag} -> {label}")
        POOL.sessions[session] = (label, time.time())  # the session stays where it was served
        POOL.served(label, dev, *usage_of(reply))
        switched = f" (switched from {', '.join(tried[:-1])})" if len(tried) > 1 else ""
        log(f"{tag} -> {label} {resp.status} {time.time() - started:.1f}s{switched}")
        return out
    if refused:
        status, headers, error = refused
        try:
            message = str(json.loads(error)["error"]["message"])
        except (ValueError, KeyError, TypeError):
            message = None
        log(f"{tag}: refused by {', '.join(tried)} "
            f"({status}: {(message or error.decode(errors='replace'))[:200]})")
        if message is not None:  # Anthropic's own answer, with its retry-after and x-should-retry
            headers.setdefault("content-type", "application/json")
            return web.Response(status=status, body=error, headers=headers)
        # Not Anthropic's JSON error (empty, or a proxy's HTML page): Claude Code gets one it can read.
        headers.popall("content-type", None)
        return api_error(status, "rate_limit_error" if status == 429 else "permission_error",
                         f"hotpocket: every team account refused this request ({status}).", headers=headers)
    return all_blocked(tag, accounts)


def downstream_headers(headers):
    # Per-account usage headers would make Claude Code warn about one account's
    # limit while the pool still has room, so they are dropped.
    return CIMultiDict((k, v) for k, v in headers.items()
                       if k.lower() not in NOT_DOWNSTREAM and not k.lower().startswith(UNIFIED))


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
        log(f"{what}: stream ended early ({e!r})")  # upstream or developer dropped mid-stream
        # Drop the developer's connection without the final chunk, so a truncated
        # body is seen as a failure instead of a complete response.
        if request.transport is not None:
            request.transport.close()
    return out, reply


def all_blocked(tag, accounts):
    """Every account is blocked: answer according to why."""
    blocks = [POOL.accounts.get(label, {}) for label in accounts]
    kinds = {b.get("blocked_kind") for b in blocks}
    # A rejected token stays rejected when its block lapses, so it only sets the
    # reset time when every account is in that state.
    timed = [b for b in blocks if b.get("blocked_kind") != "auth"] or blocks
    first = min(timed, key=lambda b: b.get("blocked_until", 0))  # the first back, whose limit is the one named
    reset = max(first.get("blocked_until", 0), time.time() + 1)
    retry = {"retry-after": str(math.ceil(reset - time.time()))}
    log(f"{tag}: all {len(blocks)} accounts blocked ({', '.join(sorted(map(str, kinds)))}); "
        f"first back in {until_text(reset)}")
    if kinds == {"auth"}:
        return api_error(403, "permission_error", "hotpocket: every team account token was "
                         "rejected. Ask your admin to replace them.")
    # Shaped like a subscription usage limit so Claude Code shows its normal
    # "limit reached, resets at ..." message (and can wait for the reset).
    claim = first.get("blocked_claim") or "five_hour"
    return api_error(429, "rate_limit_error",
                     f"hotpocket: all {len(blocks)} team accounts are at their usage limit "
                     f"or unavailable. The first one is back in {until_text(reset)}.",
                     headers={UNIFIED + "status": "rejected", UNIFIED + "reset": str(math.ceil(reset)),
                              UNIFIED + "representative-claim": claim, **retry})


# --------------------------------------------------------------------------- status page

routes = web.RouteTableDef()
PAGE_HEADERS = {
    "cache-control": "no-store", "referrer-policy": "no-referrer",
    # No framing (clickjacking), no third-party anything, and forms only submit through the page's script.
    "content-security-policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
}


def viewer(headers):
    """Who is asking and what they may do: a "member" (signed in to Tailscale) may get their own key;
    an "admin" may manage the gateway. Tailscale serve replaces these headers on every request: the login
    (none for tagged devices), and the capabilities it is told to pass on, with what each grant says."""
    login = (headers.get("tailscale-user-login") or "").lower() or None
    admin = ADMIN_CAP in json.loads(headers.get("tailscale-app-capabilities") or "{}")
    return {"login": login, "member": bool(login), "admin": admin}


def source(request):
    """Who to name in a log line: the login, or for a tagged device its tailnet address."""
    return viewer(request.headers)["login"] or request.headers.get("x-forwarded-for")


@routes.get("/_gateway/events", allow_head=False)  # a HEAD reply ends at the headers, while this would write on
async def events(request):
    """Server-sent events: tells an open status page when usage changes, so it refreshes right away."""
    resp = web.StreamResponse(headers={"content-type": "text/event-stream", "cache-control": "no-store"})
    await resp.prepare(request)
    wake = POOL.wake
    try:
        while wake:
            try:
                await asyncio.wait_for(wake.wait(), 30)
            except asyncio.TimeoutError:  # a comment now and then: it fails, ending this, once the page is gone
                await resp.write(b": idle\n\n")
                continue
            wake = POOL.wake  # the next change to wait for (taken before writing, so none is missed); None at shutdown
            if wake:
                await resp.write(b"data: changed\n\n")
    except ConnectionError:  # the page closed
        pass
    return resp


@routes.get("/_gateway/status")
async def status_json(request):
    snap = POOL.snapshot(load_accounts(), key_names())
    snap["viewer"] = viewer(request.headers)
    return web.json_response(snap, dumps=lambda d: json.dumps(d, indent=2))


@routes.get("/")
@routes.get("/_gateway")
async def to_status_page(_request):
    raise web.HTTPFound("/_gateway/")  # where the page's relative links resolve


@routes.get("/_gateway/")
async def status_page(_request):
    return web.FileResponse(Path(__file__).with_name("ui.html"),
                            headers={**PAGE_HEADERS, "content-type": "text/html; charset=utf-8"})


@routes.get("/api/hello")
async def hello(_request):
    # Claude Code's keyless connectivity check; answer it the way the API does.
    return web.json_response({"message": "hello"})


ROLES = {"admin": "Only hotpocket admins can do this.",  # why a viewer without the role is refused
         "member": "Tailscale didn't say who you are (a tagged device?), so open this page on your own device."}


def page_api(role):
    """An endpoint the status page calls, by a viewer with `role`; handlers get the URL's {name}."""
    def wrap(handler):
        async def wrapped(request):
            origin = request.headers.get("origin")
            why = None
            if not viewer(request.headers)[role]:
                why = ROLES[role]
            # Stops other sites making a signed-in browser call this. (Serve passes on the host the browser asked for.)
            elif origin != f"https://{request.headers.get('x-forwarded-host')}":
                why = f"request came from {origin or 'no page'}, not the status page"
            if why:
                log(f"refused {request.method} {request.rel_url.raw_path} from {source(request)}: {why}")
                return api_error(403, "permission_error", ROLES[role])
            try:
                done, result = await handler(request, **request.match_info)
            except AdminError as e:
                return api_error(400, "invalid_request_error", str(e))
            except sqlite3.OperationalError as e:  # e.g. locked by a long write elsewhere
                log(f"refused {request.method} {request.rel_url.raw_path}: database error ({e})")
                return api_error(503, "overloaded_error", "The database is busy; try again in a moment.")
            log(f"{'admin ' if role == 'admin' else ''}{source(request)}: {done}")
            POOL.touch()  # other open status pages show the change right away
            return web.json_response(result, headers={"cache-control": "no-store"})  # may hold a new key
        return wrapped
    return wrap


@routes.put("/_gateway/api/accounts/{name}")
@page_api("admin")
async def put_account(request, name):
    try:
        token = str((await request.json())["token"])
    except (ValueError, KeyError, TypeError):
        raise AdminError('Expected a JSON body with "token".') from None
    save_account(name, token)
    return f"saved account {name}", {}


@routes.delete("/_gateway/api/accounts/{name}")
@page_api("admin")
async def delete_account(_request, name):
    if not write("DELETE FROM accounts WHERE label = ?", name):
        raise AdminError(f"No account named '{name}'.")
    return f"removed account {name}", {}


@routes.post("/_gateway/api/keys/{name}")
@page_api("admin")
async def post_key(_request, name):
    name = name.lower()  # as new_key stores it, so the log names the key that exists
    return f"created a key for {name}", {"key": new_key(name)}


@routes.delete("/_gateway/api/keys/{name}")
@page_api("admin")
async def delete_key(_request, name):
    name = name.lower()
    if not write("DELETE FROM keys WHERE name = ?", name):  # their history goes at the next tick
        raise AdminError(f"No developer key named '{name}'.")
    return f"removed developer {name}", {}


@routes.post("/_gateway/api/my-key")
@page_api("member")
async def post_my_key(request):
    # Anyone on the tailnet can get (or replace) the key named after their own login, and only that one.
    login = viewer(request.headers)["login"]
    return "created their own key", {"name": login, "key": new_key(login)}


# --------------------------------------------------------------------------- server

def make_app():
    async def ticker():
        while True:
            await asyncio.sleep(5)
            try:
                POOL.tick()
            except sqlite3.Error as e:
                log(f"could not save usage: {e}")

    async def lifespan(_app):
        global POOL, CLIENT
        POOL = Pool()
        CLIENT = aiohttp.ClientSession(
            auto_decompress=False,  # pass bytes through untouched
            skip_auto_headers=("Accept-Encoding", "User-Agent"),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=900),
            connector=aiohttp.TCPConnector(limit=0),
            cookie_jar=aiohttp.DummyCookieJar())  # one account's or developer's cookies never ride along on another's
        task = asyncio.create_task(ticker())
        log(f"listening on {SOCKET} -> {UPSTREAM} "
            f"({len(load_accounts())} accounts, {len(key_names())} developer keys)")
        yield
        task.cancel()
        await CLIENT.close()
        POOL.tick()
        log("stopped")

    app = web.Application(client_max_size=MAX_BODY)
    app.add_routes(routes)
    app.router.add_route("*", "/{tail:.*}", proxy)  # everything else goes to the API

    async def stop_events(_app):  # open status pages would otherwise hold up a restart for its full timeout
        POOL.touch(stopping=True)

    app.cleanup_ctx.append(lifespan)
    app.on_shutdown.append(stop_events)
    return app


def main():
    # Binding replaces the socket file, so a second copy (say `docker compose exec hotpocket hotpocket`)
    # would take Tailscale serve's way in from the running one, and leave none when it exits.
    with socket.socket(socket.AF_UNIX) as s:
        if s.connect_ex(SOCKET) == 0:
            raise SystemExit(f"hotpocket is already running on {SOCKET}.")
    os.umask(0o077)  # owner-only files: the database holds account tokens, and the socket believes whoever connects
    # aiohttp waits shutdown_timeout twice for a request still running, which must fit in Docker's 10 s
    # stop grace for the final save to run.
    web.run_app(make_app(), path=SOCKET, shutdown_timeout=4, access_log=None, print=None)


# --------------------------------------------------------------------------- admin edits

class AdminError(Exception):
    """A problem with an edit, to show on the status page."""


def check_name(name, what):
    if not NAME_RE.fullmatch(name):
        raise AdminError(f"{what} names are letters, digits and . _ - @ + % (not only dots, at most 254).")


def save_account(label, raw_token):
    """Add an account or replace its token (a replaced account keeps its place)."""
    check_name(label, "Account")
    token = "".join(raw_token.split())
    if not TOKEN_RE.fullmatch(token):
        raise AdminError("That doesn't look like a Claude Code token (expected sk-ant-oat...).")
    write("INSERT INTO accounts VALUES (?, ?, (SELECT IFNULL(MAX(position), 0) + 1 FROM accounts)) "
          "ON CONFLICT (label) DO UPDATE SET token = excluded.token", label, token)


def new_key(name):
    name = name.lower()  # developer names are usually Tailscale logins, which ignore case
    check_name(name, "Developer")
    key = "hp-" + secrets.token_urlsafe(24)
    write("INSERT OR REPLACE INTO keys VALUES (?, ?, ?)", name, key_hash(key), time.time())
    return key
