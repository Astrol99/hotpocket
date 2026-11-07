"""hotpocket: one Claude Code endpoint for the team, backed by a pool of
Claude accounts.

Each request goes to the account with the most headroom (a session stays on
its account while it has room). When an account hits its usage limit, the
request is retried on the next account before the developer sees anything.
Account tokens never leave the gateway; developers get their own keys.

Server
  hotpocket serve                 listens on HOTPOCKET_HOST:HOTPOCKET_PORT
                                       (default 127.0.0.1:8080)

Admin (run on the gateway host)
  hotpocket account add LABEL     token on stdin (from `claude setup-token`)
  hotpocket account import        `label=token` lines on stdin
  hotpocket account rm LABEL
  hotpocket account list
  hotpocket key add NAME          create a developer key (shown once)
  hotpocket key rm NAME
  hotpocket key list
  hotpocket admin add LOGIN       let a Tailscale login manage accounts and keys
  hotpocket admin rm LOGIN          from the status page (https://<gateway>/_gateway)
  hotpocket admin list
  hotpocket status                accounts, usage and developers

Developers get a key from the status page (Get my key, for anyone on the tailnet)
or from `hotpocket key add`, and point Claude Code at the gateway (e.g. in
~/.claude/settings.json "env"):
  ANTHROPIC_BASE_URL=https://<gateway host>
  ANTHROPIC_AUTH_TOKEN=<developer key>
"""

import asyncio
import fcntl
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import time
import urllib.request
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import aiohttp
from aiohttp import web
from yarl import URL

DATA = Path(os.environ.get("HOTPOCKET_DATA", "/var/lib/hotpocket"))
ACCOUNTS = DATA / "accounts.json"   # {label: token}, in priority order
KEYS = DATA / "keys.json"           # {developer name: key}
ADMINS = DATA / "admins.json"       # {Tailscale login: added at}; may manage from the web page
STATE = DATA / "state.json"         # usage and blocks, survives restarts
UPSTREAM = os.environ.get("HOTPOCKET_UPSTREAM", "https://api.anthropic.com").rstrip("/")
HOST = os.environ.get("HOTPOCKET_HOST", "127.0.0.1")
PORT = int(os.environ.get("HOTPOCKET_PORT", "8080"))

OAUTH_BETA = "oauth-2025-04-20"
UNIFIED = "anthropic-ratelimit-unified-"
WINDOWS = {"five_hour": "5h", "seven_day": "7d"}  # our field name -> header infix
STICKY_MAX_UTIL = 0.95      # move a session off its account above this utilization
THROTTLE_COOLDOWN = 60      # an ordinary 429 that gives no retry-after
AUTH_FAIL_COOLDOWN = 600    # a token the API rejected (401)
FALLBACK_BLOCK = 3600       # usage-limit rejection without a reset time
SESSION_TTL = 6 * 3600
MAX_BODY = 32 * 1024 * 1024  # the Messages API's own request size limit
REASONS = {"auth": "token rejected (401)", "limit": "usage limit reached", "throttle": "throttled (429)"}

# Account labels, developer names and admin logins. Developer names are often Tailscale logins
# (you@example.com, you@github); all-dot names are refused because "." and ".." break URLs.
NAME_RE = re.compile(r"(?!\.+$)[\w.%+@-]{1,254}", re.ASCII)
TOKEN_RE = re.compile(r"sk-ant-oat[0-9]*-[A-Za-z0-9_-]+")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
NOT_UPSTREAM = HOP | {"authorization", "x-api-key", "accept-encoding", "anthropic-beta"}
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


def pct(v):
    return "-" if v is None else f"{v * 100:.0f}%"


def num(headers, name):
    try:
        return float(headers[name])
    except (KeyError, ValueError):
        return None


def used(a, window, now):
    """An account's use of a window (0-1, None if unknown). Once its reset time passes it is back to 0."""
    reset = a.get(window + "_reset")
    return 0 if reset is not None and reset <= now else a.get(window)


def fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- files

def read_json(path, default):
    """A missing file is `default`; a corrupt one raises ValueError so it is never overwritten.

    Every data file holds one JSON object; anything else (say a hand edit that left a bare
    string, where `in` would match substrings) counts as corrupt.
    """
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON ({e}); fix or remove it") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a JSON object, not {type(data).__name__}; fix or remove it")
    return data


def write_json(path, data):
    """Atomic 0600 write. When root edits the files, keep them owned by the service user."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    st = path.parent.stat()
    if os.geteuid() == 0 and st.st_uid != 0:
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)


class Watched:
    """A JSON file that is re-read whenever it changes, so admin edits apply live."""

    def __init__(self, path, keep_last_good=True):
        self.path, self.mtime, self.data, self.keep_last_good = path, None, {}, keep_last_good

    def get(self):
        try:
            st = self.path.stat()
            mtime = (st.st_mtime_ns, st.st_ino, st.st_size)  # write_json always makes a new inode
        except FileNotFoundError:
            mtime = None
        if mtime != self.mtime:
            self.mtime = mtime
            try:
                self.data = read_json(self.path, {})
            except ValueError as e:
                if self.keep_last_good:
                    log(f"{e}; still using the last good copy")
                else:
                    self.data = {}
                    log(f"{e}; treating it as empty")
        return self.data


# --------------------------------------------------------------------------- pool
# Everything here runs on the event loop's single thread, so no locks are needed.

class Pool:
    def __init__(self):
        try:
            saved = read_json(STATE, {})
        except ValueError as e:
            log(f"{e}; starting with empty usage state")
            saved = {}
        self.accounts = saved.get("accounts", {})  # label -> usage, block and request count
        self.devs = saved.get("devs", {})
        self.sessions = {}                          # session id -> (label, last seen)
        self.dirty = False

    def _acct(self, label):
        return self.accounts.setdefault(label, {"requests": 0})

    def blocked(self, label, token, now):
        # A block belongs to the token that earned it, so replacing a token clears it.
        a = self.accounts.get(label, {})
        return a.get("blocked_until", 0) > now and a.get("blocked_token") == fingerprint(token)

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
                    a[key], self.dirty = value, True

    def block(self, label, token, kind, until, claim=None):
        self._acct(label).update(blocked_kind=kind, blocked_until=until, blocked_claim=claim,
                                 blocked_token=fingerprint(token))
        self.dirty = True
        hint = "; replace its token on the status page or with `hotpocket account add`" if kind == "auth" else ""
        log(f"account {label}: {REASONS[kind]}{hint}; back in {until_text(until)}")

    def served(self, label, dev):
        self._acct(label)["requests"] += 1
        d = self.devs.setdefault(dev, {"requests": 0})
        d["requests"] += 1
        d["last_seen"], d["last_account"] = time.time(), label
        self.dirty = True

    def snapshot(self, accounts, keys):
        now = time.time()
        active = Counter(l for l, seen in self.sessions.values() if seen > now - 3600)
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
                "requests": a.get("requests", 0), "active_sessions": active[label],
            })
        names = [*keys, *sorted(self.devs.keys() - keys)]  # key holders, then past developers
        devs = [{"name": n, "has_key": n in keys, "requests": 0, **self.devs.get(n, {})} for n in names]
        return {"accounts": rows, "available": sum(r["available"] for r in rows), "developers": devs}

    def tick(self):
        """Every few seconds: forget idle sessions and save state if it changed."""
        cutoff = time.time() - SESSION_TTL
        self.sessions = {s: v for s, v in self.sessions.items() if v[1] > cutoff}
        if self.dirty:
            write_json(STATE, {"accounts": self.accounts, "devs": self.devs})
            self.dirty = False


ACCOUNT_FILE = Watched(ACCOUNTS)
KEY_FILE = Watched(KEYS)
ADMIN_FILE = Watched(ADMINS, keep_last_good=False)  # a broken edit must not keep a removed admin
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
    auth = headers.get("authorization", "")
    presented = (auth[7:] if auth.lower().startswith("bearer ") else headers.get("x-api-key", "")).strip()
    if presented:
        presented = presented.encode()
        for name, key in KEY_FILE.get().items():
            if hmac.compare_digest(presented, str(key).encode()):
                return name
    return None


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
    accounts = ACCOUNT_FILE.get()
    if not accounts:
        return api_error(503, "api_error", "hotpocket: no accounts configured.")
    body = await request.read()  # buffered so the request can be resent on another account
    session = request.headers.get("x-claude-code-session-id") or f"dev:{dev}"
    headers = upstream_headers(request.headers)
    url = URL(UPSTREAM + request.raw_path, encoded=True)
    tried = []
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
                    await resp.read()  # drain the short error body so the connection can be reused
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass  # the status alone decides what happens next
                if resp.status == 401:
                    POOL.block(label, token, "auth", time.time() + AUTH_FAIL_COOLDOWN)
                elif resp.headers.get(UNIFIED + "status") == "rejected":
                    POOL.block(label, token, "limit", reset_time(resp.headers),
                               resp.headers.get(UNIFIED + "representative-claim"))
                else:  # an ordinary rate limit: wait as long as the API asks
                    retry = num(resp.headers, "retry-after")
                    POOL.block(label, token, "throttle",
                               time.time() + (max(retry, 1) if retry is not None else THROTTLE_COOLDOWN))
                continue
            out = await relay(request, resp, f"{tag} -> {label}")
        POOL.served(label, dev)
        switched = f" (switched from {', '.join(tried[:-1])})" if len(tried) > 1 else ""
        log(f"{tag} -> {label} {resp.status} {time.time() - started:.1f}s{switched}")
        return out
    return all_blocked(tag, accounts)


async def relay(request, resp, what):
    """Stream the upstream response through as it arrives (SSE-friendly)."""
    # Per-account usage headers would make Claude Code warn about one account's
    # limit while the pool still has room, so they are dropped.
    headers = [(k, v) for k, v in resp.headers.items()
               if k.lower() not in NOT_DOWNSTREAM and not k.lower().startswith(UNIFIED)]
    out = web.StreamResponse(status=resp.status, reason=resp.reason, headers=headers)
    await out.prepare(request)
    try:
        async for chunk in resp.content.iter_any():
            await out.write(chunk)
        await out.write_eof()
    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as e:
        log(f"{what}: stream ended early ({e!r})")  # upstream or developer dropped mid-stream
        # Drop the developer's connection without the final chunk, so a truncated
        # body is seen as a failure instead of a complete response.
        if request.transport is not None:
            request.transport.close()
    return out


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
    if "limit" in kinds:
        # Shaped like a subscription usage limit so Claude Code shows its normal
        # "limit reached, resets at ..." message (and can wait for the reset).
        claim = next((b["blocked_claim"] for b in timed if b.get("blocked_claim")), "five_hour")
        return api_error(429, "rate_limit_error",
                         f"hotpocket: all {len(blocks)} team accounts are at their usage limit "
                         f"or unavailable. The first one is back in {until_text(reset)}.",
                         headers={UNIFIED + "status": "rejected", UNIFIED + "reset": str(math.ceil(reset)),
                                  UNIFIED + "representative-claim": claim, **retry})
    return api_error(429, "rate_limit_error",
                     f"hotpocket: all team accounts are briefly throttled. Retry in {until_text(reset)}.",
                     headers=retry)


# --------------------------------------------------------------------------- status page

UI_FILE = Path(__file__).with_name("ui.html")
UI_PAGE = b""  # read by serve(), so the admin CLI doesn't need it
PAGE_HEADERS = {
    "cache-control": "no-store", "referrer-policy": "no-referrer",
    # No framing (clickjacking), no third-party anything, and forms only submit through the page's script.
    "content-security-policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
}


def viewer(remote, headers):
    """Who is asking (per Tailscale serve), over HTTPS or not, and what they may do: a
    "member" (signed in to Tailscale, over HTTPS) may get their own key; an "admin" may
    manage the gateway.

    Tailscale serve replaces these headers on every request it forwards, so they are
    only believed on connections from this host, where it runs.
    """
    if remote not in ("127.0.0.1", "::1"):
        return {"login": None, "https": False, "member": False, "admin": False}
    login = (headers.get("tailscale-user-login") or "").lower() or None
    https = headers.get("x-forwarded-proto") == "https"
    member = bool(login and https)
    return {"login": login, "https": https, "member": member, "admin": member and login in ADMIN_FILE.get()}


async def health(_request):
    snap = POOL.snapshot(ACCOUNT_FILE.get(), KEY_FILE.get())
    return web.json_response({"ok": True, "accounts": len(snap["accounts"]), "available": snap["available"]})


async def status_json(request):
    snap = POOL.snapshot(ACCOUNT_FILE.get(), KEY_FILE.get())
    snap["viewer"] = viewer(request.remote, request.headers)
    return web.json_response(snap, dumps=lambda d: json.dumps(d, indent=2))


async def status_page(_request):
    return web.Response(body=UI_PAGE, content_type="text/html", charset="utf-8", headers=PAGE_HEADERS)


async def hello(_request):
    # Claude Code's keyless connectivity check; answer it the way the API does.
    return web.json_response({"message": "hello"})


ROLES = {  # role -> (why it's refused, for the log; what the page shows)
    "admin": ("not an admin", "Only hotpocket admins can do this, over HTTPS."),
    "member": ("not signed in to Tailscale over HTTPS", "Open this page at its https://….ts.net address on Tailscale."),
}


def page_api(role):
    """An endpoint the status page calls: by a viewer with `role`, from the page itself.

    The handler gets the URL's {name}, if any, as an argument and the viewer as request["who"].
    """
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
            request["who"] = who
            try:
                done, result = await handler(request, **request.match_info)
            except AdminError as e:
                return web.json_response({"error": str(e)}, status=400)
            log(f"{'admin ' if role == 'admin' else ''}{who['login']}: {done}")
            return web.json_response(result, headers={"cache-control": "no-store"})  # may hold a new key
        return wrapped
    return wrap


@page_api("admin")
async def put_account(request, name):
    try:
        token = str((await request.json())["token"])
    except (ValueError, KeyError, TypeError):
        raise AdminError('Expected a JSON body with "token".') from None
    total = save_account(name, token)
    return f"saved account {name}", {"accounts": total}


@page_api("admin")
async def delete_account(_request, name):
    remove(ACCOUNTS, name, "account")
    return f"removed account {name}", {}


@page_api("admin")
async def post_key(_request, name):
    return f"created a key for {name.lower()}", {"key": new_key(name)}


@page_api("admin")
async def delete_key(_request, name):
    revoke_key(name)
    return f"removed the key for {name.lower()}", {}


@page_api("member")
async def post_my_key(request):
    # Anyone on the tailnet can get (or replace) the key named after their own login, and only that one.
    login = request["who"]["login"]
    if not NAME_RE.fullmatch(login):
        raise AdminError(f"Your Tailscale login ({login}) can't name a key here; ask a hotpocket admin for one.")
    return "created their own key", {"name": login, "key": new_key(login)}


# --------------------------------------------------------------------------- server

def serve(argv):
    global POOL, UI_PAGE
    if argv:
        die("`serve` takes no arguments; set HOTPOCKET_HOST and HOTPOCKET_PORT instead.")
    UI_PAGE = UI_FILE.read_bytes()
    POOL = Pool()

    async def ticker():
        while True:
            await asyncio.sleep(5)
            try:
                POOL.tick()
            except OSError as e:
                log(f"could not save state: {e}")

    async def lifespan(_app):
        global CLIENT
        CLIENT = aiohttp.ClientSession(
            auto_decompress=False,  # pass bytes through untouched
            skip_auto_headers=("Accept-Encoding", "User-Agent"),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=900),
            connector=aiohttp.TCPConnector(limit=0))
        task = asyncio.create_task(ticker())
        log(f"listening on http://{HOST}:{PORT} -> {UPSTREAM} "
            f"({len(ACCOUNT_FILE.get())} accounts, {len(KEY_FILE.get())} developer keys)")
        yield
        task.cancel()
        await CLIENT.close()
        POOL.tick()
        log("stopped")

    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_get("/_gateway", status_page)
    app.router.add_get("/_gateway/status", status_json)
    app.router.add_get("/_gateway/health", health)
    app.router.add_get("/api/hello", hello)
    app.router.add_put("/_gateway/api/accounts/{name}", put_account)
    app.router.add_delete("/_gateway/api/accounts/{name}", delete_account)
    app.router.add_post("/_gateway/api/keys/{name}", post_key)
    app.router.add_post("/_gateway/api/my-key", post_my_key)
    app.router.add_delete("/_gateway/api/keys/{name}", delete_key)
    app.router.add_route("*", "/{tail:.*}", proxy)  # everything else goes to the API
    app.cleanup_ctx.append(lifespan)
    web.run_app(app, host=HOST, port=PORT, shutdown_timeout=15, access_log=None, print=None)


# --------------------------------------------------------------------------- admin CLI

def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


class AdminError(Exception):
    """A problem to show the admin, on the command line or on the status page."""


def clean_token(raw):
    token = "".join(raw.split())
    return token if TOKEN_RE.fullmatch(token) else None


def read_token():
    if sys.stdin.isatty():
        return getpass.getpass("Paste the token from `claude setup-token` (hidden): ")
    return sys.stdin.read()


def read_admin_json(path):
    """For edits: refuse to touch a corrupt file instead of replacing its contents."""
    try:
        return read_json(path, {})
    except ValueError as e:
        raise AdminError(str(e)) from None


@contextmanager
def editing(path):
    """Read-modify-write an admin file; nothing is written if the body raises.

    The data directory is locked meanwhile, so an edit from the CLI and one from the
    status page can't both read the old file and one silently undo the other.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = read_admin_json(path)
        yield data
        write_json(path, data)
    finally:
        os.close(fd)


def check_name(name, what):
    if not NAME_RE.fullmatch(name):
        raise AdminError(f"{what} names are letters, digits and . _ - @ + % (not only dots, at most 254).")


def save_account(label, raw_token):
    check_name(label, "Account")
    token = clean_token(raw_token)
    if not token:
        raise AdminError("That doesn't look like a Claude Code token (expected sk-ant-oat...).")
    with editing(ACCOUNTS) as accounts:
        accounts[label] = token
    return len(accounts)


def new_key(name):
    name = name.lower()  # developer names are usually Tailscale logins, which ignore case
    check_name(name, "Developer")
    with editing(KEYS) as keys:
        keys[name] = key = "hp-" + secrets.token_urlsafe(24)
    return key


def revoke_key(name):
    remove(KEYS, name.lower(), "developer key")


def remove(path, name, what):
    with editing(path) as entries:
        if entries.pop(name, None) is None:
            raise AdminError(f"No {what} named '{name}'.")


def cmd_account(argv):
    sub, args = (argv[0], argv[1:]) if argv else ("list", [])
    if sub == "add" and len(args) == 1:
        check_name(args[0], "Account")  # before asking for the token
        total = save_account(args[0], read_token())
        print(f"Saved account '{args[0]}' ({total} total).")
    elif sub == "import":
        entries = []
        for n, line in enumerate(sys.stdin.read().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            label, sep, token = line.partition("=")
            label, token = label.strip(), clean_token(token)
            if not sep or not NAME_RE.fullmatch(label) or not token:
                raise AdminError(f"Line {n} isn't `label=sk-ant-oat...` (nothing was imported).")
            entries.append((label, token))
        if not entries:
            raise AdminError("No accounts found on stdin.")
        with editing(ACCOUNTS) as accounts:
            accounts.update(entries)
        print(f"Imported {', '.join(l for l, _ in entries)} ({len(accounts)} total).")
    elif sub == "rm" and len(args) == 1:
        remove(ACCOUNTS, args[0], "account")
        print(f"Removed account '{args[0]}'.")
    elif sub == "list":
        accounts = read_admin_json(ACCOUNTS)
        for label, token in accounts.items():
            print(f"{label:20} {token[:14]}…{token[-4:]}")
        if not accounts:
            print("No accounts. Add one with: hotpocket account add LABEL")
    else:
        die("Usage: hotpocket account add LABEL | import | rm LABEL | list")


def cmd_key(argv):
    sub, args = (argv[0], argv[1:]) if argv else ("list", [])
    name = args[0].lower() if len(args) == 1 else None
    if sub == "add" and name:
        print(new_key(name))
        print(f"Key for '{name}' (shown once; `key add` again to replace it).", file=sys.stderr)
    elif sub == "rm" and name:
        revoke_key(name)
        print(f"Removed the key for '{name}'. Anyone on the tailnet can still make their own key, "
              "so to keep someone out, remove them from the tailnet.")
    elif sub == "list":
        print("\n".join(read_admin_json(KEYS)) or "No developer keys. Create one with: hotpocket key add NAME")
    else:
        die("Usage: hotpocket key add NAME | rm NAME | list")


def cmd_admin(argv):
    sub, args = (argv[0], argv[1:]) if argv else ("list", [])
    login = args[0].lower() if len(args) == 1 else None
    if sub == "add" and login:
        if not NAME_RE.fullmatch(login):
            raise AdminError("Use the Tailscale login exactly as the status page shows it, e.g. you@example.com.")
        with editing(ADMINS) as admins:
            admins[login] = int(time.time())
        print(f"{login} can now manage accounts and keys at https://<gateway>/_gateway.")
    elif sub == "rm" and login:
        remove(ADMINS, login, "admin")
        print(f"{login} can no longer manage hotpocket from the web.")
    elif sub == "list":
        print("\n".join(read_admin_json(ADMINS)) or "No admins. Add one with: hotpocket admin add LOGIN")
    else:
        die("Usage: hotpocket admin add LOGIN | rm LOGIN | list")


def cmd_status(_argv):
    host = "127.0.0.1" if HOST in ("", "0.0.0.0", "::") else HOST
    host = f"[{host}]" if ":" in host else host
    try:
        with urllib.request.urlopen(f"http://{host}:{PORT}/_gateway/status", timeout=5) as r:
            snap = json.load(r)
    except (OSError, ValueError) as e:
        die(f"Gateway isn't answering on port {PORT} ({e}). Check: systemctl status hotpocket")
    rows = [("ACCOUNT", "STATE", "5H", "5H RESET", "7D", "7D RESET", "SESSIONS", "REQUESTS")]
    for a in snap["accounts"]:
        state = "ready" if a["available"] else f"{a['blocked_reason']} ({until_text(a['blocked_until'])})"
        rows.append((a["label"], state, pct(a["five_hour"]), until_text(a["five_hour_reset"]),
                     pct(a["seven_day"]), until_text(a["seven_day_reset"]),
                     str(a["active_sessions"]), str(a["requests"])))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    print(f"\n{snap['available']} of {len(snap['accounts'])} accounts ready. "
          f"Developers: {', '.join(d['name'] + ('' if d['has_key'] else ' (revoked)') for d in snap['developers']) or 'none yet'}")


def main():
    argv = sys.argv[1:]
    commands = {"serve": serve, "account": cmd_account, "key": cmd_key, "admin": cmd_admin,
                "status": cmd_status}
    if not argv or argv[0] not in commands:
        print(__doc__.strip())
        return 0 if argv[:1] in ([], ["-h"], ["--help"]) else 1
    try:
        return commands[argv[0]](argv[1:]) or 0
    except AdminError as e:
        die(str(e))
