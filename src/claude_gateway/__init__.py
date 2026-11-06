"""claude-gateway: one Claude Code endpoint for the team, backed by a pool of
Claude accounts.

Each request goes to the account with the most headroom (a session stays on
its account while it has room). When an account hits its usage limit, the
request is retried on the next account before the developer sees anything.
Account tokens never leave the gateway; developers get their own keys.

Server
  claude-gateway serve [--host 127.0.0.1] [--port 8080]

Admin (run on the gateway host)
  claude-gateway account add LABEL     token on stdin (from `claude setup-token`)
  claude-gateway account import        `label=token` lines on stdin
  claude-gateway account rm LABEL
  claude-gateway account list
  claude-gateway key add NAME          create a developer key (shown once)
  claude-gateway key rm NAME
  claude-gateway key list
  claude-gateway status                accounts, usage and developers

Developers point Claude Code at it (e.g. in ~/.claude/settings.json "env"):
  ANTHROPIC_BASE_URL=https://<gateway host>
  ANTHROPIC_AUTH_TOKEN=<developer key>
"""

import asyncio
import getpass
import hmac
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.request
from pathlib import Path

import aiohttp
from aiohttp import web
from yarl import URL

DATA = Path(os.environ.get("CLAUDE_GATEWAY_DATA", "/var/lib/claude-gateway"))
ACCOUNTS = DATA / "accounts.json"   # {label: token}, in priority order
KEYS = DATA / "keys.json"           # {developer name: key}
STATE = DATA / "state.json"         # usage and blocks, survives restarts
UPSTREAM = os.environ.get("CLAUDE_GATEWAY_UPSTREAM", "https://api.anthropic.com").rstrip("/")
DEFAULT_PORT = int(os.environ.get("CLAUDE_GATEWAY_PORT", "8080"))

OAUTH_BETA = "oauth-2025-04-20"
UNIFIED = "anthropic-ratelimit-unified-"
STICKY_MAX_UTIL = 0.95      # move a session off its account above this utilization
THROTTLE_COOLDOWN = 60      # a 429 that isn't a usage-limit rejection
AUTH_FAIL_COOLDOWN = 600    # a token the API rejected (401)
FALLBACK_BLOCK = 3600       # usage-limit rejection without a reset time
SESSION_TTL = 6 * 3600
MAX_BODY = 100 * 1024 * 1024

LABEL_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
TOKEN_RE = re.compile(r"sk-ant-oat[0-9]*-[A-Za-z0-9_-]+")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
NOT_UPSTREAM = HOP | {"authorization", "x-api-key", "accept-encoding", "anthropic-beta"}
NOT_DOWNSTREAM = HOP | {"server", "date"}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def until_text(ts):
    secs = int(ts - time.time())
    if secs <= 0:
        return "now"
    h, m = divmod((secs + 59) // 60, 60)
    return f"{h // 24}d{h % 24}h" if h >= 24 else f"{h}h{m:02d}m" if h else f"{m}m"


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
# Everything below runs on the event loop's single thread, so no locks are needed.

class Pool:
    def __init__(self):
        saved = read_json(STATE, {})
        self.accounts = saved.get("accounts", {})
        self.devs = saved.get("devs", {})
        self.sessions = {}
        self.dirty = False

    def _acct(self, label):
        return self.accounts.setdefault(label, {"requests": 0})

    def _util(self, label):
        a, now = self.accounts.get(label, {}), time.time()
        fresh = lambda reset: reset is not None and reset <= now  # window has reset since
        u5 = 0 if fresh(a.get("r5")) else a.get("u5", 0)
        u7 = 0 if fresh(a.get("r7")) else a.get("u7", 0)
        return max(u5, u7)

    def choose(self, labels, session, exclude):
        now = time.time()
        ready = [l for l in labels
                 if l not in exclude and self._acct(l).get("blocked_until", 0) <= now]
        if not ready:
            return None
        sticky = self.sessions.get(session, (None, 0))[0]
        if sticky in ready and self._util(sticky) < STICKY_MAX_UTIL:
            choice = sticky
        else:
            choice = min(ready, key=lambda l: (self._util(l), labels.index(l)))
        self.sessions[session] = (choice, now)
        if len(self.sessions) > 5000:
            self.sessions = {s: v for s, v in self.sessions.items() if v[1] > now - SESSION_TTL}
        return choice

    def observe(self, label, headers):
        """Record the account's live usage from the API's response headers."""
        if not any(k.startswith(UNIFIED) for k in headers):
            return
        a = self._acct(label)
        for key, name in (("u5", "5h-utilization"), ("u7", "7d-utilization"),
                          ("r5", "5h-reset"), ("r7", "7d-reset")):
            try:
                a[key] = float(headers[UNIFIED + name])
            except (KeyError, ValueError):
                pass
        a["status"] = headers.get(UNIFIED + "status", a.get("status"))
        a["seen"] = time.time()
        self.dirty = True

    def block(self, label, until, reason):
        a = self._acct(label)
        a["blocked_until"], a["blocked_reason"] = until, reason
        self.dirty = True
        log(f"account {label}: {reason}; back in {until_text(until)}")

    def served(self, label, dev):
        now = time.time()
        a = self._acct(label)
        a["requests"] = a.get("requests", 0) + 1
        a["last_used"] = now
        d = self.devs.setdefault(dev, {"requests": 0})
        d["requests"] += 1
        d["last_seen"], d["last_account"] = now, label
        self.dirty = True

    def earliest_reset(self, labels):
        times = [self.accounts.get(l, {}).get("blocked_until", 0) for l in labels]
        times = [t for t in times if t > time.time()]
        return min(times) if times else None

    def snapshot(self, labels):
        now = time.time()
        active = {}
        for label, seen in self.sessions.values():
            if seen > now - 3600:
                active[label] = active.get(label, 0) + 1
        accounts = []
        for l in labels:
            a = self.accounts.get(l, {})
            blocked = a.get("blocked_until", 0) > now
            accounts.append({
                "label": l, "available": not blocked,
                "blocked_until": a.get("blocked_until") if blocked else None,
                "blocked_reason": a.get("blocked_reason") if blocked else None,
                "five_hour": a.get("u5"), "five_hour_reset": a.get("r5"),
                "seven_day": a.get("u7"), "seven_day_reset": a.get("r7"),
                "requests": a.get("requests", 0), "last_used": a.get("last_used"),
                "active_sessions": active.get(l, 0),
            })
        devs = [{"name": n, **d} for n, d in sorted(self.devs.items())]
        return {"now": now, "accounts": accounts, "developers": devs,
                "available": sum(a["available"] for a in accounts)}

    def persist(self):
        if self.dirty:
            self.dirty = False
            write_json(STATE, {"accounts": self.accounts, "devs": self.devs})


ACCOUNT_FILE = Watched(ACCOUNTS)
KEY_FILE = Watched(KEYS)
POOL: Pool = None
CLIENT: aiohttp.ClientSession = None


def reset_time(headers):
    """When a usage-limit rejection lifts, from the response headers."""
    try:
        return float(headers[UNIFIED + "reset"])
    except (KeyError, ValueError):
        pass
    resets = []
    for window in ("5h", "7d"):
        if headers.get(f"{UNIFIED}{window}-status") == "rejected":
            try:
                resets.append(float(headers[f"{UNIFIED}{window}-reset"]))
            except (KeyError, ValueError):
                pass
    if resets:
        return max(resets)
    try:
        return time.time() + float(headers["retry-after"])
    except (KeyError, ValueError):
        return time.time() + FALLBACK_BLOCK


# --------------------------------------------------------------------------- HTTP

def api_error(status, etype, message, headers=None):
    return web.json_response({"type": "error", "error": {"type": etype, "message": message}},
                             status=status, headers=headers)


def developer(headers):
    auth = headers.get("authorization", "")
    presented = auth[7:].strip() if auth.lower().startswith("bearer ") \
        else headers.get("x-api-key", "").strip()
    if presented:
        for name, key in KEY_FILE.get().items():
            if hmac.compare_digest(presented.encode(), str(key).encode()):
                return name
    return None


def upstream_headers(headers):
    out = {k: v for k, v in headers.items()
           if k.lower() not in NOT_UPSTREAM and not k.lower().startswith(("tailscale-", "x-forwarded-"))}
    betas = [b.strip() for v in headers.getall("anthropic-beta", []) for b in v.split(",")]
    out["anthropic-beta"] = ",".join(dict.fromkeys([b for b in betas if b] + [OAUTH_BETA]))
    return out


async def handle(request):
    path = request.path.rstrip("/")
    if path in ("/_gateway", "/_gateway/status", "/_gateway/health"):
        return gateway_page(path)
    if path == "/api/hello":  # Claude Code's keyless connectivity check; answer like the API does
        return web.json_response({"message": "hello"})
    return await proxy(request)


async def proxy(request):
    started, method, path = time.time(), request.method, request.path
    dev = developer(request.headers)
    if dev is None:
        who = request.headers.get("tailscale-user-login") or request.remote
        log(f"refused {method} {path} from {who}: missing or unknown developer key")
        # 403 rather than 401: Claude Code retries a 401 for minutes before giving up.
        return api_error(403, "permission_error", "claude-gateway: missing or unknown developer key. "
                         "Set ANTHROPIC_AUTH_TOKEN to the key your admin gave you.")
    accounts = ACCOUNT_FILE.get()
    labels = list(accounts)
    if not labels:
        return api_error(503, "api_error", "claude-gateway: no accounts configured.")
    body = await request.read()  # buffered so the request can be resent on another account
    session = request.headers.get("x-claude-code-session-id") or f"dev:{dev}"
    headers = upstream_headers(request.headers)
    url = URL(UPSTREAM + request.raw_path, encoded=True)
    tried, last = [], None
    while (label := POOL.choose(labels, session, tried)) is not None:
        tried.append(label)
        headers["authorization"] = f"Bearer {accounts[label]}"
        try:
            resp = await CLIENT.request(method, url, headers=headers, data=body or None,
                                        allow_redirects=False)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log(f"{dev} {method} {path} -> {label}: upstream error {e!r}")
            return api_error(502, "api_error",
                             f"claude-gateway: could not reach the Anthropic API ({type(e).__name__}).")
        async with resp:
            got = {k.lower(): v for k, v in resp.headers.items()}
            POOL.observe(label, got)
            if resp.status in (401, 429):
                await resp.read()  # drain the short error body so the connection can be reused
                if resp.status == 401:
                    POOL.block(label, time.time() + AUTH_FAIL_COOLDOWN,
                               "token rejected (401); replace it with `claude-gateway account add`")
                elif got.get(UNIFIED + "status") == "rejected":
                    POOL.block(label, reset_time(got), "usage limit reached")
                else:
                    POOL.block(label, time.time() + THROTTLE_COOLDOWN, "throttled (429)")
                last = (resp.status, got)
                continue
            out = await relay(request, resp, f"{dev} {method} {path} -> {label}")
        POOL.served(label, dev)
        switched = f" (switched from {', '.join(tried[:-1])})" if len(tried) > 1 else ""
        log(f"{dev} {method} {path} -> {label} {resp.status} {time.time() - started:.1f}s{switched}")
        return out
    return all_blocked(dev, method, path, labels, last)


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


def all_blocked(dev, method, path, labels, last):
    if last and last[0] == 401 and all(
            "401" in (POOL.accounts.get(l, {}).get("blocked_reason") or "") for l in labels):
        log(f"{dev} {method} {path}: every account token was rejected")
        return api_error(401, "authentication_error",
                         "claude-gateway: every team account token was rejected. Ask your admin to replace them.")
    reset = POOL.earliest_reset(labels) or time.time() + FALLBACK_BLOCK
    log(f"{dev} {method} {path}: all {len(labels)} accounts limited; first back in {until_text(reset)}")
    # Shaped like a subscription usage limit so Claude Code shows its normal
    # "limit reached, resets at ..." message (and can wait for the reset).
    claim = (last[1].get(UNIFIED + "representative-claim") if last else None) or "five_hour"
    return api_error(429, "rate_limit_error",
                     f"claude-gateway: all {len(labels)} team accounts are at their usage limit. "
                     f"The first one is back in {until_text(reset)}.",
                     headers={UNIFIED + "status": "rejected",
                              UNIFIED + "reset": str(int(reset)),
                              UNIFIED + "representative-claim": claim,
                              "retry-after": str(max(1, int(reset - time.time())))})


# --------------------------------------------------------------------------- status page

def gateway_page(path):
    snap = POOL.snapshot(list(ACCOUNT_FILE.get()))
    if path.endswith("/health"):
        return web.json_response({"ok": True, "accounts": len(snap["accounts"]),
                                  "available": snap["available"]})
    if path.endswith("/status"):
        return web.json_response(snap, dumps=lambda d: json.dumps(d, indent=2))
    return web.Response(text=render_html(snap), content_type="text/html",
                        headers={"cache-control": "no-store"})


def render_html(snap):
    def pct(v):
        return "–" if v is None else f"{v * 100:.0f}%"

    def when(ts):
        return f'<time data-ts="{int(ts)}"></time>' if ts else "–"

    rows = []
    for a in snap["accounts"]:
        state = ('<span class="ok">ready</span>' if a["available"] else
                 f'<span class="bad">{html.escape(a["blocked_reason"] or "limited")}</span> until {when(a["blocked_until"])}')
        rows.append(f"<tr><td>{html.escape(a['label'])}</td><td>{state}</td>"
                    f"<td>{pct(a['five_hour'])}</td><td>{when(a['five_hour_reset'])}</td>"
                    f"<td>{pct(a['seven_day'])}</td><td>{when(a['seven_day_reset'])}</td>"
                    f"<td>{a['active_sessions']}</td><td>{a['requests']}</td></tr>")
    devs = "".join(f"<tr><td>{html.escape(d['name'])}</td><td>{d.get('requests', 0)}</td>"
                   f"<td>{when(d.get('last_seen'))}</td><td>{html.escape(d.get('last_account') or '–')}</td></tr>"
                   for d in snap["developers"]) or '<tr><td colspan="4">No requests yet</td></tr>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="30">
<title>Claude gateway</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--mute:#6e6e73;--line:#e5e5ea;--ok:#1a7f37;--bad:#b42318}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#f2f2f2;--mute:#a1a1a6;--line:#2c2c2e;--ok:#4ac26b;--bad:#ff6b5e}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:960px;margin:auto}}
h1{{font-size:20px;margin:0 0 4px}}h2{{font-size:16px;margin:28px 0 8px}}p{{color:var(--mute);margin:0}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}
th{{color:var(--mute);font-weight:500;font-size:13px}}.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}
</style></head><body>
<h1>Claude gateway</h1><p>{snap['available']} of {len(snap['accounts'])} accounts ready · refreshes every 30s</p>
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
    host, port = "127.0.0.1", DEFAULT_PORT
    for flag, value in zip(argv[::2], argv[1::2]):
        if flag == "--host":
            host = value
        elif flag == "--port":
            port = int(value)
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    POOL = Pool()

    async def persist_loop():
        while True:
            await asyncio.sleep(5)
            try:
                POOL.persist()
            except OSError as e:
                log(f"could not save state: {e}")

    async def lifespan(_app):
        global CLIENT
        CLIENT = aiohttp.ClientSession(
            auto_decompress=False,  # pass bytes through untouched
            skip_auto_headers=("Accept-Encoding", "User-Agent"),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=900),
            connector=aiohttp.TCPConnector(limit=0))
        saver = asyncio.create_task(persist_loop())
        log(f"listening on http://{host}:{port} -> {UPSTREAM} "
            f"({len(ACCOUNT_FILE.get())} accounts, {len(KEY_FILE.get())} developer keys)")
        yield
        saver.cancel()
        await CLIENT.close()
        POOL.persist()
        log("stopped")

    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_route("*", "/{tail:.*}", handle)
    app.cleanup_ctx.append(lifespan)
    # backlog: Claude Code sends bursts of parallel requests (subagents).
    web.run_app(app, host=host, port=port, backlog=128, shutdown_timeout=15,
                access_log=None, print=None)


# --------------------------------------------------------------------------- admin CLI

def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def read_token():
    if sys.stdin.isatty():
        return getpass.getpass("Paste the token from `claude setup-token` (hidden): ")
    return sys.stdin.read()


def cmd_account(argv):
    sub, args = (argv[0], argv[1:]) if argv else ("list", [])
    accounts = read_json(ACCOUNTS, {})
    if sub == "add" and len(args) == 1 and LABEL_RE.fullmatch(args[0]):
        token = re.sub(r"\s+", "", read_token())
        if not TOKEN_RE.fullmatch(token):
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
            label, token = label.strip(), re.sub(r"\s+", "", token)
            if not sep or not LABEL_RE.fullmatch(label) or not TOKEN_RE.fullmatch(token):
                die(f"Line {n} isn't `label=sk-ant-oat...` (nothing was imported).")
            entries.append((label, token))
        if not entries:
            die("No accounts found on stdin.")
        accounts.update(entries)
        write_json(ACCOUNTS, accounts)
        print(f"Imported {', '.join(l for l, _ in entries)} ({len(accounts)} total).")
    elif sub == "rm" and len(args) == 1:
        if accounts.pop(args[0], None) is None:
            die(f"No account named '{args[0]}'.")
        write_json(ACCOUNTS, accounts)
        print(f"Removed account '{args[0]}'.")
    elif sub == "list":
        for label, token in accounts.items():
            print(f"{label:20} {token[:14]}…{token[-4:]}")
        if not accounts:
            print("No accounts. Add one with: claude-gateway account add LABEL")
    else:
        die("Usage: claude-gateway account add LABEL | import | rm LABEL | list")


def cmd_key(argv):
    sub, args = (argv[0], argv[1:]) if argv else ("list", [])
    keys = read_json(KEYS, {})
    if sub == "add" and len(args) == 1 and LABEL_RE.fullmatch(args[0]):
        keys[args[0]] = "cgw-" + secrets.token_urlsafe(24)
        write_json(KEYS, keys)
        print(keys[args[0]])
        print(f"Key for '{args[0]}' (shown once; `key add` again to replace it).", file=sys.stderr)
    elif sub == "rm" and len(args) == 1:
        if keys.pop(args[0], None) is None:
            die(f"No key for '{args[0]}'.")
        write_json(KEYS, keys)
        print(f"Removed key for '{args[0]}'. Their requests are refused from now on.")
    elif sub == "list":
        print("\n".join(keys) or "No developer keys. Create one with: claude-gateway key add NAME")
    else:
        die("Usage: claude-gateway key add NAME | rm NAME | list")


def cmd_status(_argv):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DEFAULT_PORT}/_gateway/status", timeout=5) as r:
            snap = json.load(r)
    except OSError as e:
        die(f"Gateway isn't answering on port {DEFAULT_PORT} ({e}). Check: systemctl status claude-gateway")
    pct = lambda v: "-" if v is None else f"{v * 100:.0f}%"
    rows = [("ACCOUNT", "STATE", "5H", "5H RESET", "7D", "7D RESET", "SESSIONS", "REQUESTS")]
    for a in snap["accounts"]:
        state = "ready" if a["available"] else f"{a['blocked_reason']} ({until_text(a['blocked_until'])})"
        rows.append((a["label"], state, pct(a["five_hour"]),
                     until_text(a["five_hour_reset"]) if a["five_hour_reset"] else "-",
                     pct(a["seven_day"]),
                     until_text(a["seven_day_reset"]) if a["seven_day_reset"] else "-",
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
