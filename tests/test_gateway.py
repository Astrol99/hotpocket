"""End-to-end tests for claude-gateway against a fake Anthropic API. Run: uv run tests/test_gateway.py"""
import http.client, json, os, subprocess, sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
GW = [sys.executable, "-m", "claude_gateway"]  # the installed package (uv run installs it)
G = tempfile.mkdtemp(prefix="gw-test-")
env = dict(os.environ, CLAUDE_GATEWAY_DATA=f"{G}/data", CLAUDE_GATEWAY_UPSTREAM="http://127.0.0.1:18901", CLAUDE_GATEWAY_PORT="18900")
results = []


def ok(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    results.append(bool(cond))


def cli(*args, stdin=None):
    r = subprocess.run([*GW, *args], env=env, input=stdin, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def seen():
    try: return [json.loads(l) for l in open(f"{G}/seen.log")]
    except FileNotFoundError: return []


def get(path, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", 18900, timeout=10)
    c.request(method, path)
    r = c.getresponse()
    return r, r.read()


def status():
    snap = json.loads(get("/_gateway/status")[1])
    return snap, {a["label"]: a for a in snap["accounts"]}


def call(headers, body=b'{"model":"x"}', conn=None, chunked=False):
    c = conn or http.client.HTTPConnection("127.0.0.1", 18900, timeout=30)
    t0 = time.time()
    # A tuple body has no length, so http.client sends it chunked, one chunk per item.
    c.request("POST", "/v1/messages?beta=true", body=(body[:5], body[5:]) if chunked else body, headers=headers)
    r = c.getresponse()
    first = r.read1(65536); t_first = time.time() - t0
    rest = r.read(); return r, first + rest, t_first, time.time() - t0, c


up = subprocess.Popen([sys.executable, str(HERE / "fake_upstream.py"), "18901", f"{G}/seen.log"])
rc, out = cli("account", "import", stdin="# pool\nlimited=sk-ant-oat01-LIMITED\ngood=sk-ant-oat01-GOOD\n")
ok(rc == 0, f"account import: {out}")
rc, bad = cli("account", "import", stdin="x=sk-ant-oat01-X\noops=nope\n")
ok(rc != 0 and "nothing was imported" in bad, "bad import rejected")
rc, key = cli("key", "add", "alice"); key = key.splitlines()[0]
ok(key.startswith("cgw-"), "key add")
modes = oct(os.stat(f"{G}/data/accounts.json").st_mode & 0o777), oct(os.stat(f"{G}/data/keys.json").st_mode & 0o777)
ok(modes == ("0o600", "0o600"), f"secret files are 0600 {modes}")
rc, out = cli("serve", "--port", "1")
ok(rc != 0 and "takes no arguments" in out, "serve rejects flags instead of ignoring them")

gw = subprocess.Popen([*GW, "serve"], env=env, stdout=open(f"{G}/gw.log", "w"), stderr=subprocess.STDOUT)
for _ in range(100):  # wait until it answers
    try: get("/_gateway/health"); break
    except OSError: time.sleep(0.05)

H = {"authorization": f"Bearer {key}", "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14",
     "x-claude-code-session-id": "s1", "tailscale-user-login": "alice@example.com", "content-type": "application/json"}

ok(get("/api/hello", "HEAD")[0].status == 200, "keyless HEAD /api/hello connectivity check answered locally")
r, body, *_ = call({"content-type": "application/json"})
ok(r.status == 403 and b"developer key" in body, "no key -> 403")
r, body, *_ = call(dict(H, authorization="Bearer cgw-wrong"))
ok(r.status == 403, "wrong key -> 403")

n0 = len(seen())
r, body, t_first, t_total, conn = call(H)
s = seen()[n0:]
ok(r.status == 200 and body.count(b"event: delta") == 3, "limited account skipped, request served")
ok([x["tok"][-4:] for x in s] == ["ITED", "GOOD"], f"tried limited then good: {[x['tok'] for x in s]}")
ok(t_first < 0.3 and t_total > 0.9, f"streams incrementally (first chunk {t_first:.2f}s, total {t_total:.2f}s)")
ok(not any(k.lower().startswith("anthropic-ratelimit-unified") for k in r.headers), "per-account usage headers stripped")
ok(s[-1]["beta"] == "claude-code-20250219,interleaved-thinking-2025-05-14,oauth-2025-04-20", f"betas merged: {s[-1]['beta']}")
ok(s[-1]["leaked"] == [], "dev key / tailscale headers not forwarded")

n0 = len(seen())
r, body, *_ = call(H, conn=conn)   # same keep-alive connection
ok(r.status == 200 and [x["tok"][-4:] for x in seen()[n0:]] == ["GOOD"], "limited account remembered; keep-alive works")
r, body, *_ = call(H, body=b'{"model":"chunked-body"}', chunked=True)
ok(r.status == 200 and seen()[-1]["body_len"] == len(b'{"model":"chunked-body"}'), "chunked request body")

snap, acc = status()
ok(acc["limited"]["blocked_kind"] == "limit" and acc["limited"]["blocked_until"] > time.time() + 7000, "limited blocked until its reset header")
ok(acc["good"]["five_hour"] == 0.42 and acc["good"]["requests"] == 3, f"usage tracked from headers ({acc['good']['five_hour']}, {acc['good']['requests']} req)")
ok(snap["developers"][0]["name"] == "alice" and snap["developers"][0]["requests"] == 3, "per-developer counts")
page = get("/_gateway")[1].decode()
ok("<table>" in page and "alice" in page and "usage limit reached" in page, "HTML status page")
rc, out = cli("status"); print("    " + out.replace("\n", "\n    "))
ok(rc == 0 and "usage limit reached" in out, "CLI status")

# live account edits: drop 'good' -> only the limited one is left -> synthesized 429
cli("account", "rm", "good")
r, body, *_ = call(H)
ok(r.status == 429 and r.headers.get("anthropic-ratelimit-unified-status") == "rejected"
   and int(r.headers.get("anthropic-ratelimit-unified-reset")) > time.time() + 7000,
   f"all limited -> 429 shaped like a usage limit: {json.loads(body)['error']['message']}")

# 401 failover + throttle handling, with accounts swapped live
cli("account", "import", stdin="bad=sk-ant-oat01-BAD\nthrottle=sk-ant-oat01-THROTTLE\ngood=sk-ant-oat01-GOOD\n")
n0 = len(seen())
r, body, *_ = call(dict(H, **{"x-claude-code-session-id": "s2"}))
order = [x["tok"].split("-")[-1] for x in seen()[n0:]]
ok(r.status == 200 and order == ["BAD", "THROTTLE", "GOOD"], f"revoked + throttled accounts skipped: {order}")
_, acc = status()
ok(acc["bad"]["blocked_kind"] == "auth" and acc["throttle"]["blocked_kind"] == "throttle"
   and acc["throttle"]["blocked_until"] < time.time() + 61, "401 -> 10 min auth block, throttle -> 60s block")

# every remaining account has a rejected token -> 403 (never 401, which Claude Code retries for minutes)
for label in ("limited", "throttle", "good"):
    cli("account", "rm", label)
r, body, *_ = call(H)
ok(r.status == 403 and b"rejected" in body, f"all tokens rejected -> 403: {json.loads(body)['error']['message']}")

# only throttled accounts -> plain retryable 429, not a fake usage limit
cli("account", "import", stdin="throttle=sk-ant-oat01-THROTTLE\n"); cli("account", "rm", "bad")
r, body, *_ = call(H)
ok(r.status == 429 and "anthropic-ratelimit-unified-status" not in r.headers and int(r.headers["retry-after"]) <= 60,
   f"all throttled -> plain 429 with retry-after {r.headers.get('retry-after')}")

# replacing a rejected token clears its block immediately
cli("account", "import", stdin="bad=sk-ant-oat01-BAD\n")
cli("account", "add", "bad", stdin="sk-ant-oat01-FIXED\n")
n0 = len(seen())
r, body, *_ = call(H)
ok(r.status == 200 and [x["tok"] for x in seen()[n0:]] == ["sk-ant-oat01-FIXED"], "replaced token is used right away")

gw.terminate(); gw.wait(5)
st = json.load(open(f"{G}/data/state.json"))
ok(st["accounts"]["limited"]["blocked_until"] > time.time(), "state persisted on shutdown")
up.terminate()
print("\n--- gateway log"); print(open(f"{G}/gw.log").read().strip())
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
