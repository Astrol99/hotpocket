"""hotpocket: one Claude Code endpoint for the team, backed by a pool of Claude accounts.

Make yourself an admin (`hotpocket admin add you@example.com`, your Tailscale
login), then manage accounts and keys on the status page, https://<gateway>.
Developers get their key there too and point Claude Code at the gateway:
  ANTHROPIC_BASE_URL=https://<gateway>  ANTHROPIC_AUTH_TOKEN=<their key>
"""

import argparse
import asyncio
import functools
import hashlib
import json
import math
import os
import re
import secrets
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
HOST = os.environ.get("HOTPOCKET_HOST", "127.0.0.1")
PORT = int(os.environ.get("HOTPOCKET_PORT", "8080"))

OAUTH_BETA = "oauth-2025-04-20"
UNIFIED = "anthropic-ratelimit-unified-"
WINDOWS = {"five_hour": "5h", "seven_day": "7d"}  # our field name -> header infix
STICKY_MAX_UTIL = 0.95      # move a session off its account above this utilization
AUTH_FAIL_COOLDOWN = 600    # a token the API rejected (401)
FALLBACK_BLOCK = 3600       # usage-limit rejection without a reset time
SESSION_TTL = 6 * 3600
MAX_BODY = 32 * 1024 * 1024  # the Messages API's own request size limit
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
REASONS = {"auth": "token rejected (401)", "limit": "usage limit reached"}

# Account labels, developer names and admin logins. Developer names are often Tailscale logins
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
    if not ts:
        return "-"
    secs = int(ts - time.time())
    if secs <= 0:
        return "now"
    if secs < 60:
        return f"{secs}s"
    h, m = divmod((secs + 59) // 60, 60)
    return f"{h // 24}d{h % 24}h" if h >= 24 else f"{h}h{m:02d}m" if h else f"{m}m"


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
                model, usage = message.get("model", model), {**usage, **message["usage"]}
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
# One SQLite database, shared by the server and the admin CLI. Every change is a single
# statement (or one transaction), so edits from both sides land whole and in order.

SCHEMA = """
-- Accounts are tried in `position` order when their usage is tied.
CREATE TABLE IF NOT EXISTS accounts (label TEXT PRIMARY KEY, token TEXT NOT NULL, position INTEGER NOT NULL);
-- Only a hash of each developer key is kept; the key itself is shown once and never stored.
CREATE TABLE IF NOT EXISTS keys (name TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS admins (login TEXT PRIMARY KEY, added REAL NOT NULL);  -- Tailscale logins
CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, value TEXT NOT NULL);    -- the pool's saved usage (JSON)
"""


@functools.cache
def db():
    """This process's connection (creating the database on first use)."""
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / "hotpocket.db", timeout=10)  # waits, rather than fails, while another process writes
    con.execute("PRAGMA journal_mode = WAL")                  # the server reads while the CLI writes
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

CHANGED = None  # set (and replaced) whenever usage changes; open status pages wait on it. None when stopped.


def changed():
    global CHANGED
    if CHANGED:
        CHANGED.set()
        CHANGED = asyncio.Event()

# Everything here runs on the event loop's single thread, so no locks are needed.

class Pool:
    def __init__(self):
        row = db().execute("SELECT value FROM state WHERE name = 'pool'").fetchone()
        saved = json.loads(row[0]) if row else {}
        self.accounts = saved.get("accounts", {})  # label -> usage, block and request count
        self.devs = saved.get("devs", {})
        self.sessions = {}                          # session id -> (label, last seen)
        self.since = saved.get("since") or time.time()  # when token counting started
        self.dirty = False

    def _acct(self, label):
        return self.accounts.setdefault(label, {"requests": 0})

    def blocked(self, label, token, now):
        # A block belongs to the token that earned it, so replacing a token clears it. A kind no longer
        # in REASONS (an old "throttle" block from saved state) doesn't count.
        a = self.accounts.get(label, {})
        return (a.get("blocked_until", 0) > now and a.get("blocked_token") == key_hash(token)[:16]
                and a.get("blocked_kind") in REASONS)

    def _util(self, label):
        a, now = self.accounts.get(label, {}), time.time()
        return max(used(a, w, now) or 0 for w in WINDOWS)

    def choose(self, accounts, session, exclude):
        now = time.time()
        ready = [l for l, token in accounts.items()
                 if l not in exclude and not self.blocked(l, token, now)]
        if not ready:
            return None
        sticky = self.sessions.get(session, (None,))[0]
        choice = (sticky if sticky in ready and self._util(sticky) < STICKY_MAX_UTIL
                  else min(ready, key=self._util))  # ties go to the earlier-added account
        self.sessions[session] = (choice, now)
        return choice

    def observe(self, label, headers):
        """Record the account's live usage from the API's response headers."""
        a = self._acct(label)
        for window, infix in WINDOWS.items():
            for key, value in ((window, num(headers, f"{UNIFIED}{infix}-utilization")),
                               (window + "_reset", num(headers, f"{UNIFIED}{infix}-reset"))):
                if value is not None:
                    a[key], a["usage_at"], self.dirty = value, time.time(), True  # when it was last heard

    def block(self, label, token, kind, until, claim=None):
        self._acct(label).update(blocked_kind=kind, blocked_until=until, blocked_claim=claim,
                                 blocked_token=key_hash(token)[:16])  # which token earned it
        self.dirty = True
        changed()
        hint = "; replace its token on the status page" if kind == "auth" else ""
        log(f"account {label}: {REASONS[kind]}{hint}; back in {until_text(until)}")

    def served(self, label, dev, model, usage):
        d, cost = self.devs.setdefault(dev, {"requests": 0}), api_cost(model, usage)
        for entry in (self._acct(label), d):  # the same tally per account and per developer
            entry["requests"] += 1
            entry["cost"] = entry.get("cost", 0) + cost
            entry["tokens"] = Counter(entry.get("tokens"))
            entry["tokens"].update({f: usage.get(f) or 0 for f in TOKEN_FIELDS})
        d["last_seen"], d["last_account"] = time.time(), label
        self.dirty = True
        changed()

    def snapshot(self, accounts, keys):
        now = time.time()
        rows = []
        for label, token in accounts.items():
            a = self.accounts.get(label, {})
            blocked = self.blocked(label, token, now)
            rows.append({
                "label": label, "available": not blocked,
                "blocked_kind": a["blocked_kind"] if blocked else None,
                "blocked_reason": REASONS.get(a["blocked_kind"]) if blocked else None,
                "blocked_until": a["blocked_until"] if blocked else None,
                **{w: used(a, w, now) for w in WINDOWS}, **{w + "_reset": a.get(w + "_reset") for w in WINDOWS},
                "usage_at": a.get("usage_at"), "requests": a.get("requests", 0),
                "tokens": a.get("tokens", {}), "cost": a.get("cost", 0),
            })
        devs = [{"name": n, "requests": 0, **self.devs.get(n, {})} for n in keys]
        return {"accounts": rows, "developers": devs, "since": self.since}

    def tick(self):
        """Every few seconds: forget idle sessions and save state if it changed."""
        cutoff = time.time() - SESSION_TTL
        self.sessions = {s: v for s, v in self.sessions.items() if v[1] > cutoff}
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
            who = viewer(request.remote, request.headers)["login"] or request.remote
            log(f"refused {request.method} {request.rel_url.raw_path} from {who}: missing or unknown developer key")
        # The gateway never sends 401: Claude Code retries a 401 for minutes before giving up.
        return api_error(403, "permission_error", "hotpocket: missing or unknown developer key. "
                         "Set ANTHROPIC_AUTH_TOKEN to the key your admin gave you.")
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
    tried, plain_429 = [], None
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
            if resp.status in (401, 429):
                try:
                    error = await resp.read()  # also lets the connection be reused
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    error = b""
                if resp.status == 401:
                    POOL.block(label, token, "auth", time.time() + AUTH_FAIL_COOLDOWN)
                elif resp.headers.get(UNIFIED + "status") == "rejected":
                    POOL.block(label, token, "limit", reset_time(resp.headers),
                               resp.headers.get(UNIFIED + "representative-claim"))
                else:
                    # Any other 429 can be about this request (say, a long context the plan doesn't
                    # cover) rather than the account, so the account isn't blocked for everyone;
                    # the request just tries the next one, and gets this answer if none takes it.
                    plain_429 = (downstream_headers(resp.headers), error)
                continue
            out, reply = await relay(request, resp, f"{tag} -> {label}")
        POOL.served(label, dev, *usage_of(reply))
        switched = f" (switched from {', '.join(tried[:-1])})" if len(tried) > 1 else ""
        log(f"{tag} -> {label} {resp.status} {time.time() - started:.1f}s{switched}")
        return out
    if plain_429:
        headers, error = plain_429
        try:
            message = str(json.loads(error)["error"]["message"])
        except (ValueError, KeyError, TypeError):
            message = None
        log(f"{tag}: refused by {', '.join(tried)} "
            f"(429: {(message or error.decode(errors='replace'))[:200]})")
        if message is not None:  # Anthropic's own answer, with its retry-after and x-should-retry
            headers.setdefault("content-type", "application/json")
            return web.Response(status=429, body=error, headers=headers)
        # Not Anthropic's JSON error (empty, or a proxy's HTML page): Claude Code gets one it can read.
        headers.popall("content-type", None)
        return api_error(429, "rate_limit_error", "hotpocket: every team account refused this request "
                         "(429). Try again shortly.", headers=headers)
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
    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as e:
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
    reset = max(min(b.get("blocked_until", 0) for b in timed), time.time() + 1)
    retry = {"retry-after": str(math.ceil(reset - time.time()))}
    log(f"{tag}: all {len(blocks)} accounts blocked ({', '.join(sorted(map(str, kinds)))}); "
        f"first back in {until_text(reset)}")
    if kinds == {"auth"}:
        return api_error(403, "permission_error", "hotpocket: every team account token was "
                         "rejected. Ask your admin to replace them.")
    # Shaped like a subscription usage limit so Claude Code shows its normal
    # "limit reached, resets at ..." message (and can wait for the reset).
    claim = next((b["blocked_claim"] for b in timed if b.get("blocked_claim")), "five_hour")
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


def viewer(remote, headers):
    """Who is asking and what they may do: a "member" (signed in to Tailscale, over HTTPS) may
    get their own key; an "admin" may manage the gateway. Tailscale serve sets these headers
    on every request it forwards, and runs on this host, so they're only believed from here."""
    if remote not in ("127.0.0.1", "::1"):
        return {"login": None, "https": False, "member": False, "admin": False}
    login = (headers.get("tailscale-user-login") or "").lower() or None
    https = headers.get("x-forwarded-proto") == "https"
    member = bool(login and https)
    admin = member and db().execute("SELECT 1 FROM admins WHERE login = ?", (login,)).fetchone() is not None
    return {"login": login, "https": https, "member": member, "admin": admin}


@routes.get("/_gateway/events")
async def events(request):
    """Server-sent events: tells an open status page when usage changes, so it refreshes right away."""
    resp = web.StreamResponse(headers={"content-type": "text/event-stream", "cache-control": "no-store"})
    await resp.prepare(request)
    wake = CHANGED
    while wake:
        await wake.wait()
        wake = CHANGED  # the next change to wait for (taken before writing, so none is missed); None at shutdown
        if wake:
            await resp.write(b"data: changed\n\n")
    return resp


@routes.get("/_gateway/status")
async def status_json(request):
    snap = POOL.snapshot(load_accounts(), key_names())
    snap["viewer"] = viewer(request.remote, request.headers)
    return web.json_response(snap, dumps=lambda d: json.dumps(d, indent=2))


@routes.get("/")
@routes.get("/_gateway")
async def to_status_page(_request):
    raise web.HTTPFound("/_gateway/")  # where the page's relative links resolve


@routes.route("*", "/_gateway/https/{domain:[^/]+}{rest:.*}")  # any method: API calls too
async def to_https(request):
    # Plain HTTP (http://hotpocket) lands here, with Tailscale serve filling in its own HTTPS
    # name. Only plain HTTP is redirected, so this can't send anyone to another site.
    if request.headers.get("x-forwarded-proto") == "https":
        raise web.HTTPNotFound()
    raise web.HTTPFound(f"https://{request.match_info['domain']}/")


@routes.get("/_gateway/")
async def status_page(_request):
    return web.FileResponse(Path(__file__).with_name("ui.html"),
                            headers={**PAGE_HEADERS, "content-type": "text/html; charset=utf-8"})


@routes.get("/api/hello")
async def hello(_request):
    # Claude Code's keyless connectivity check; answer it the way the API does.
    return web.json_response({"message": "hello"})


ROLES = {  # role -> (why it's refused, for the log; what the page shows)
    "admin": ("not an admin", "Only hotpocket admins can do this, over HTTPS."),
    "member": ("not signed in to Tailscale over HTTPS", "Open this page at its https://….ts.net address on Tailscale."),
}


def page_api(role):
    """An endpoint the status page calls, by a viewer with `role`; handlers get the URL's {name}."""
    def wrap(handler):
        async def wrapped(request):
            who = viewer(request.remote, request.headers)
            origin = request.headers.get("origin")
            why = None
            if not who[role]:
                why = ROLES[role][0]
            elif origin != f"https://{request.host}":  # stops other sites making a signed-in browser call this
                why = f"request came from {origin or 'no page'}, not the status page"
            if why:
                log(f"refused {request.method} {request.rel_url.raw_path} from {who['login'] or request.remote}: {why}")
                return web.json_response({"error": ROLES[role][1]}, status=403)
            try:
                done, result = await handler(request, **request.match_info)
            except AdminError as e:
                return web.json_response({"error": str(e)}, status=400)
            except sqlite3.OperationalError as e:  # e.g. locked by a long write elsewhere
                log(f"refused {request.method} {request.rel_url.raw_path}: database error ({e})")
                return web.json_response({"error": "The database is busy; try again in a moment."}, status=503)
            log(f"{'admin ' if role == 'admin' else ''}{who['login']}: {done}")
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
    total = save_account(name, token)
    return f"saved account {name}", {"accounts": total}


@routes.delete("/_gateway/api/accounts/{name}")
@page_api("admin")
async def delete_account(_request, name):
    remove("accounts", "label", name, "account")
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
    remove("keys", "name", name, "developer key")
    if POOL.devs.pop(name, None):  # their history goes with them
        POOL.dirty = True
    return f"removed developer {name}", {}


@routes.post("/_gateway/api/my-key")
@page_api("member")
async def post_my_key(request):
    # Anyone on the tailnet can get (or replace) the key named after their own login, and only that one.
    login = viewer(request.remote, request.headers)["login"]
    if not NAME_RE.fullmatch(login):
        raise AdminError(f"Your Tailscale login ({login}) can't name a key here; ask a hotpocket admin for one.")
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
        global POOL, CLIENT, CHANGED
        # The server never waits long for the database: a stuck writer elsewhere (say an open
        # sqlite3 shell) would otherwise stall every stream. A failed usage save retries next tick.
        db().execute("PRAGMA busy_timeout = 1000")
        POOL, CHANGED = Pool(), asyncio.Event()
        CLIENT = aiohttp.ClientSession(
            auto_decompress=False,  # pass bytes through untouched
            skip_auto_headers=("Accept-Encoding", "User-Agent"),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=900),
            connector=aiohttp.TCPConnector(limit=0))
        task = asyncio.create_task(ticker())
        log(f"listening on http://{HOST}:{PORT} -> {UPSTREAM} "
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
        global CHANGED
        CHANGED, wake = None, CHANGED
        wake.set()

    app.cleanup_ctx.append(lifespan)
    app.on_shutdown.append(stop_events)
    return app


# --------------------------------------------------------------------------- admin edits

class AdminError(Exception):
    """A problem to show the admin, on the command line or on the status page."""


def check_name(name, what):
    if not NAME_RE.fullmatch(name):
        raise AdminError(f"{what} names are letters, digits and . _ - @ + % (not only dots, at most 254).")


def save_account(label, raw_token):
    """Add an account or replace its token (a replaced account keeps its place); returns the count."""
    check_name(label, "Account")
    token = "".join(raw_token.split())
    if not TOKEN_RE.fullmatch(token):
        raise AdminError("That doesn't look like a Claude Code token (expected sk-ant-oat...).")
    with db() as con:
        con.execute("INSERT INTO accounts VALUES (?, ?, (SELECT IFNULL(MAX(position), 0) + 1 FROM accounts)) "
                    "ON CONFLICT (label) DO UPDATE SET token = excluded.token", (label, token))
        return con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


def new_key(name):
    name = name.lower()  # developer names are usually Tailscale logins, which ignore case
    check_name(name, "Developer")
    key = "hp-" + secrets.token_urlsafe(24)
    write("INSERT OR REPLACE INTO keys VALUES (?, ?, ?)", name, key_hash(key), time.time())
    return key


def remove(table, column, value, what):
    if not write(f"DELETE FROM {table} WHERE {column} = ?", value):
        raise AdminError(f"No {what} named '{value}'.")


# --------------------------------------------------------------------------- command line
# Only serving and the admin list live here: making someone an admin needs access to the
# gateway's host. Everything else is on the status page.

def admin_add(args):
    login = args.login.lower()
    check_name(login, "Admin")
    write("INSERT OR IGNORE INTO admins VALUES (?, ?)", login, time.time())
    print(f"{login} can now manage accounts and keys at https://<gateway>.")


def admin_rm(args):
    login = args.login.lower()
    remove("admins", "login", login, "admin")
    print(f"{login} can no longer manage hotpocket from the web.")


def admin_list(_args):
    logins = [login for (login,) in db().execute("SELECT login FROM admins ORDER BY added")]
    print("\n".join(logins) or "No admins. Add one with: hotpocket admin add LOGIN")


def main():
    os.umask(0o077)  # the database holds account tokens: owner-only files
    parser = argparse.ArgumentParser(prog="hotpocket", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(required=True, metavar="COMMAND")
    commands.add_parser("serve", help="run the gateway on HOTPOCKET_HOST:HOTPOCKET_PORT "
                        "(default 127.0.0.1:8080)").set_defaults(run=lambda _: web.run_app(
        make_app(), host=HOST, port=PORT, shutdown_timeout=15, access_log=None, print=None))
    admins = commands.add_parser("admin", help="Tailscale logins that may manage accounts and keys "
                                 "on the status page").add_subparsers(required=True, metavar="ACTION")
    for name, run, help in (("add", admin_add, "make a Tailscale login an admin"),
                            ("rm", admin_rm, "take that away")):
        action = admins.add_parser(name, help=help)
        action.add_argument("login")
        action.set_defaults(run=run)
    admins.add_parser("list", help="list admins").set_defaults(run=admin_list)

    args = parser.parse_args()
    try:
        args.run(args)
    except AdminError as e:
        parser.exit(1, f"{e}\n")
    except sqlite3.OperationalError as e:
        parser.exit(1, f"Can't use the database in {DATA} ({e}).\n")
