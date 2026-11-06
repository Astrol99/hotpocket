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
  hotpocket status                accounts, usage and developers

Developers point Claude Code at it (e.g. in ~/.claude/settings.json "env"):
  ANTHROPIC_BASE_URL=https://<gateway host>
  ANTHROPIC_AUTH_TOKEN=<developer key>
"""

import asyncio
import getpass
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

import aiohttp
from aiohttp import web
from yarl import URL

DATA = Path(os.environ.get("HOTPOCKET_DATA", "/var/lib/hotpocket"))
ACCOUNTS = DATA / "accounts.json"   # {label: token}, in priority order
KEYS = DATA / "keys.json"           # {developer name: key}
STATE = DATA / "state.json"         # usage and blocks, survives restarts
UPSTREAM = os.environ.get("HOTPOCKET_UPSTREAM", "https://api.anthropic.com").rstrip("/")
HOST = os.environ.get("HOTPOCKET_HOST", "127.0.0.1")
PORT = int(os.environ.get("HOTPOCKET_PORT", "8080"))

OAUTH_BETA = "oauth-2025-04-20"
UNIFIED = "anthropic-ratelimit-unified-"
WINDOWS = {"five_hour": "5h", "seven_day": "7d"}  # our field name -> header infix
STICKY_MAX_UTIL = 0.95      # move a session off its account above this utilization
THROTTLE_COOLDOWN = 60      # a 429 that isn't a usage-limit rejection
AUTH_FAIL_COOLDOWN = 600    # a token the API rejected (401)
FALLBACK_BLOCK = 3600       # usage-limit rejection without a reset time
SESSION_TTL = 6 * 3600
MAX_BODY = 32 * 1024 * 1024  # the Messages API's own request size limit
REASONS = {"auth": "token rejected (401)", "limit": "usage limit reached", "throttle": "throttled (429)"}

LABEL_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
TOKEN_RE = re.compile(r"sk-ant-oat[0-9]*-[A-Za-z0-9_-]+")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
NOT_UPSTREAM = HOP | {"authorization", "x-api-key", "accept-encoding", "anthropic-beta"}
NOT_DOWNSTREAM = HOP | {"server", "date"}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def until_text(ts):
    if not ts:
        return "-"
    secs = int(ts - time.time())
    if secs <= 0:
        return "now"
    h, m = divmod((secs + 59) // 60, 60)
    return f"{h // 24}d{h % 24}h" if h >= 24 else f"{h}h{m:02d}m" if h else f"{m}m"


def pct(v):
    return "-" if v is None else f"{v * 100:.0f}%"


def num(headers, name):
    try:
        return float(headers[name])
    except (KeyError, ValueError):
        return None


def fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- files

def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


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

    def __init__(self, path):
        self.path, self.mtime, self.data = path, None, {}

    def get(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if mtime != self.mtime:
            self.data, self.mtime = read_json(self.path, {}), mtime
        return self.data


# --------------------------------------------------------------------------- pool
# Everything here runs on the event loop's single thread, so no locks are needed.

class Pool:
    def __init__(self):
        saved = read_json(STATE, {})
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

        def used(window):  # a window whose reset time has passed is back to 0%
            reset = a.get(window + "_reset")
            return 0 if reset is not None and reset <= now else a.get(window) or 0

        return max(used(w) for w in WINDOWS)

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
        hint = "; replace it with `hotpocket account add`" if kind == "auth" else ""
        log(f"account {label}: {REASONS[kind]}{hint}; back in {until_text(until)}")

    def served(self, label, dev):
        self._acct(label)["requests"] += 1
        d = self.devs.setdefault(dev, {"requests": 0})
        d["requests"] += 1
        d["last_seen"], d["last_account"] = time.time(), label
        self.dirty = True

    def snapshot(self, accounts):
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
                **{k: a.get(k) for w in WINDOWS for k in (w, w + "_reset")},
                "requests": a.get("requests", 0), "active_sessions": active[label],
            })
        return {"accounts": rows, "available": sum(r["available"] for r in rows),
                "developers": [{"name": n, **d} for n, d in sorted(self.devs.items())]}

    def tick(self):
        """Every few seconds: forget idle sessions and save state if it changed."""
        cutoff = time.time() - SESSION_TTL
        self.sessions = {s: v for s, v in self.sessions.items() if v[1] > cutoff}
        if self.dirty:
            self.dirty = False
            write_json(STATE, {"accounts": self.accounts, "devs": self.devs})


ACCOUNT_FILE = Watched(ACCOUNTS)
KEY_FILE = Watched(KEYS)
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
        who = request.headers.get("tailscale-user-login") or request.remote
        log(f"refused {request.method} {request.path} from {who}: missing or unknown developer key")
        # The gateway never sends 401: Claude Code retries a 401 for minutes before giving up.
        return api_error(403, "permission_error", "hotpocket: missing or unknown developer key. "
                         "Set ANTHROPIC_AUTH_TOKEN to the key your admin gave you.")
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
                await resp.read()  # drain the short error body so the connection can be reused
                if resp.status == 401:
                    POOL.block(label, token, "auth", time.time() + AUTH_FAIL_COOLDOWN)
                elif resp.headers.get(UNIFIED + "status") == "rejected":
                    POOL.block(label, token, "limit", reset_time(resp.headers),
                               resp.headers.get(UNIFIED + "representative-claim"))
                else:
                    POOL.block(label, token, "throttle", time.time() + THROTTLE_COOLDOWN)
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
    return out


def all_blocked(tag, accounts):
    """Every account is blocked: answer according to why."""
    blocks = [POOL.accounts.get(label, {}) for label in accounts]
    kinds = {b.get("blocked_kind") for b in blocks}
    reset = max(min(b.get("blocked_until", 0) for b in blocks), time.time() + 1)
    retry = {"retry-after": str(int(reset - time.time()))}
    log(f"{tag}: all {len(blocks)} accounts blocked ({', '.join(sorted(map(str, kinds)))}); "
        f"first back in {until_text(reset)}")
    if kinds == {"auth"}:
        return api_error(403, "permission_error", "hotpocket: every team account token was "
                         "rejected. Ask your admin to replace them.")
    if "limit" in kinds:
        # Shaped like a subscription usage limit so Claude Code shows its normal
        # "limit reached, resets at ..." message (and can wait for the reset).
        claim = next((b["blocked_claim"] for b in blocks if b.get("blocked_claim")), "five_hour")
        return api_error(429, "rate_limit_error",
                         f"hotpocket: all {len(blocks)} team accounts are at their usage limit "
                         f"or unavailable. The first one is back in {until_text(reset)}.",
                         headers={UNIFIED + "status": "rejected", UNIFIED + "reset": str(int(reset)),
                                  UNIFIED + "representative-claim": claim, **retry})
    return api_error(429, "rate_limit_error",
                     f"hotpocket: all team accounts are briefly throttled. Retry in {until_text(reset)}.",
                     headers=retry)


# --------------------------------------------------------------------------- status page

async def health(_request):
    snap = POOL.snapshot(ACCOUNT_FILE.get())
    return web.json_response({"ok": True, "accounts": len(snap["accounts"]), "available": snap["available"]})


async def status_json(_request):
    return web.json_response(POOL.snapshot(ACCOUNT_FILE.get()), dumps=lambda d: json.dumps(d, indent=2))


async def status_page(_request):
    return web.Response(text=render_html(POOL.snapshot(ACCOUNT_FILE.get())), content_type="text/html",
                        headers={"cache-control": "no-store"})


async def hello(_request):
    # Claude Code's keyless connectivity check; answer it the way the API does.
    return web.json_response({"message": "hello"})


def render_html(snap):
    def when(ts):
        return f'<time data-ts="{int(ts)}"></time>' if ts else "-"

    rows = []
    for a in snap["accounts"]:
        state = ('<span class="ok">ready</span>' if a["available"] else
                 f'<span class="bad">{html.escape(a["blocked_reason"] or "limited")}</span> until {when(a["blocked_until"])}')
        rows.append(f"<tr><td>{html.escape(a['label'])}</td><td>{state}</td>"
                    f"<td>{pct(a['five_hour'])}</td><td>{when(a['five_hour_reset'])}</td>"
                    f"<td>{pct(a['seven_day'])}</td><td>{when(a['seven_day_reset'])}</td>"
                    f"<td>{a['active_sessions']}</td><td>{a['requests']}</td></tr>")
    devs = "".join(f"<tr><td>{html.escape(d['name'])}</td><td>{d['requests']}</td>"
                   f"<td>{when(d.get('last_seen'))}</td><td>{html.escape(d.get('last_account') or '-')}</td></tr>"
                   for d in snap["developers"]) or '<tr><td colspan="4">No requests yet</td></tr>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="30">
<title>hotpocket</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--mute:#6e6e73;--line:#e5e5ea;--ok:#1a7f37;--bad:#b42318}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#f2f2f2;--mute:#a1a1a6;--line:#2c2c2e;--ok:#4ac26b;--bad:#ff6b5e}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:960px;margin:auto}}
h1{{font-size:20px;margin:0 0 4px}}h2{{font-size:16px;margin:28px 0 8px}}p{{color:var(--mute);margin:0}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{color:var(--mute);font-weight:500;font-size:13px}}.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}
</style></head><body>
<h1>hotpocket</h1><p>{snap['available']} of {len(snap['accounts'])} accounts ready · refreshes every 30s</p>
<h2>Accounts</h2><div class="wrap"><table><tr><th>Account</th><th>State</th><th>5h used</th><th>5h resets</th>
<th>7d used</th><th>7d resets</th><th>Active sessions</th><th>Requests</th></tr>{''.join(rows)}</table></div>
<h2>Developers</h2><div class="wrap"><table><tr><th>Name</th><th>Requests</th><th>Last seen</th><th>Last account</th></tr>{devs}</table></div>
<script>
for (const t of document.querySelectorAll('time')) {{
  const d = new Date(t.dataset.ts * 1000), sameDay = d.toDateString() === new Date().toDateString();
  t.textContent = d.toLocaleString([], sameDay ? {{hour:'numeric', minute:'2-digit'}} : {{weekday:'short', hour:'numeric', minute:'2-digit'}});
}}
</script></body></html>"""


# --------------------------------------------------------------------------- server

def serve(argv):
    global POOL
    if argv:
        die("`serve` takes no arguments; set HOTPOCKET_HOST and HOTPOCKET_PORT instead.")
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
    app.router.add_route("*", "/{tail:.*}", proxy)  # everything else goes to the API
    app.cleanup_ctx.append(lifespan)
    web.run_app(app, host=HOST, port=PORT, shutdown_timeout=15, access_log=None, print=None)


# --------------------------------------------------------------------------- admin CLI

def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def clean_token(raw):
    token = "".join(raw.split())
    return token if TOKEN_RE.fullmatch(token) else None


def read_token():
    if sys.stdin.isatty():
        return getpass.getpass("Paste the token from `claude setup-token` (hidden): ")
    return sys.stdin.read()


def remove(path, name, what):
    entries = read_json(path, {})
    if entries.pop(name, None) is None:
        die(f"No {what} named '{name}'.")
    write_json(path, entries)


def cmd_account(argv):
    sub, args = (argv[0], argv[1:]) if argv else ("list", [])
    accounts = read_json(ACCOUNTS, {})
    if sub == "add" and len(args) == 1 and LABEL_RE.fullmatch(args[0]):
        token = clean_token(read_token())
        if not token:
            die("That doesn't look like a Claude Code token (expected sk-ant-oat...).")
        accounts[args[0]] = token
        write_json(ACCOUNTS, accounts)
        print(f"Saved account '{args[0]}' ({len(accounts)} total).")
    elif sub == "import":
        entries = []
        for n, line in enumerate(sys.stdin.read().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            label, sep, token = line.partition("=")
            label, token = label.strip(), clean_token(token)
            if not sep or not LABEL_RE.fullmatch(label) or not token:
                die(f"Line {n} isn't `label=sk-ant-oat...` (nothing was imported).")
            entries.append((label, token))
        if not entries:
            die("No accounts found on stdin.")
        accounts.update(entries)
        write_json(ACCOUNTS, accounts)
        print(f"Imported {', '.join(l for l, _ in entries)} ({len(accounts)} total).")
    elif sub == "rm" and len(args) == 1:
        remove(ACCOUNTS, args[0], "account")
        print(f"Removed account '{args[0]}'.")
    elif sub == "list":
        for label, token in accounts.items():
            print(f"{label:20} {token[:14]}…{token[-4:]}")
        if not accounts:
            print("No accounts. Add one with: hotpocket account add LABEL")
    else:
        die("Usage: hotpocket account add LABEL | import | rm LABEL | list")


def cmd_key(argv):
    sub, args = (argv[0], argv[1:]) if argv else ("list", [])
    keys = read_json(KEYS, {})
    if sub == "add" and len(args) == 1 and LABEL_RE.fullmatch(args[0]):
        keys[args[0]] = "hp-" + secrets.token_urlsafe(24)
        write_json(KEYS, keys)
        print(keys[args[0]])
        print(f"Key for '{args[0]}' (shown once; `key add` again to replace it).", file=sys.stderr)
    elif sub == "rm" and len(args) == 1:
        remove(KEYS, args[0], "developer key")
        print(f"Removed key for '{args[0]}'. Their requests are refused from now on.")
    elif sub == "list":
        print("\n".join(keys) or "No developer keys. Create one with: hotpocket key add NAME")
    else:
        die("Usage: hotpocket key add NAME | rm NAME | list")


def cmd_status(_argv):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/_gateway/status", timeout=5) as r:
            snap = json.load(r)
    except OSError as e:
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
          f"Developers: {', '.join(d['name'] for d in snap['developers']) or 'none yet'}")


def main():
    argv = sys.argv[1:]
    commands = {"serve": serve, "account": cmd_account, "key": cmd_key, "status": cmd_status}
    if not argv or argv[0] not in commands:
        print(__doc__.strip())
        return 0 if argv[:1] in ([], ["-h"], ["--help"]) else 1
    return commands[argv[0]](argv[1:]) or 0
