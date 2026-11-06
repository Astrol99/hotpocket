"""End-to-end tests for claude-gateway against a fake Anthropic API. Run: uv run tests/test_gateway.py"""
import http.client, json, os, subprocess, sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
GW = [sys.executable, "-m", "claude_gateway"]  # the installed package (uv run installs it)
G = tempfile.mkdtemp(prefix="gw-test-")
env = dict(os.environ, CLAUDE_GATEWAY_DATA=f"{G}/data", CLAUDE_GATEWAY_UPSTREAM="http://127.0.0.1:18901", CLAUDE_GATEWAY_PORT="18900")
def cli(*args, stdin=None):
    r = subprocess.run([*GW, *args], env=env, input=stdin, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()
def seen():
    try: return [json.loads(l) for l in open(f"{G}/seen.log")]
    except FileNotFoundError: return []
ok = lambda cond, name: print(("PASS " if cond else "FAIL ") + name) or cond
results = []

up = subprocess.Popen([sys.executable, str(HERE / "fake_upstream.py"), "18901", f"{G}/seen.log"])
rc, out = cli("account", "import", stdin="# pool\nlimited=sk-ant-oat01-LIMITED\ngood=sk-ant-oat01-GOOD\n")
results.append(ok(rc == 0, f"account import: {out}"))
rc, bad = cli("account", "import", stdin="x=sk-ant-oat01-X\noops=nope\n")
results.append(ok(rc != 0 and "nothing was imported" in bad, "bad import rejected"))
rc, key = cli("key", "add", "alice"); key = key.splitlines()[0]
results.append(ok(key.startswith("cgw-"), "key add"))
modes = oct(os.stat(f"{G}/data/accounts.json").st_mode & 0o777), oct(os.stat(f"{G}/data/keys.json").st_mode & 0o777)
results.append(ok(modes == ("0o600", "0o600"), f"secret files are 0600 {modes}"))
gw = subprocess.Popen([*GW, "serve", "--port", "18900"], env=env, stdout=open(f"{G}/gw.log", "w"), stderr=subprocess.STDOUT)
time.sleep(1)

def call(headers, body=b'{"model":"x"}', conn=None, chunked=False):
    c = conn or http.client.HTTPConnection("127.0.0.1", 18900, timeout=30)
    t0 = time.time()
    if chunked:
        c.putrequest("POST", "/v1/messages?beta=true"); [c.putheader(k, v) for k, v in headers.items()]
        c.putheader("transfer-encoding", "chunked"); c.endheaders()
        for part in (body[:5], body[5:]): c.send(b"%x\r\n%s\r\n" % (len(part), part))
        c.send(b"0\r\n\r\n")
    else:
        c.request("POST", "/v1/messages?beta=true", body=body, headers=headers)
    r = c.getresponse()
    first = r.read1(65536); t_first = time.time() - t0
    rest = r.read(); return r, first + rest, t_first, time.time() - t0, c

H = {"authorization": f"Bearer {key}", "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14",
     "x-claude-code-session-id": "s1", "tailscale-user-login": "alice@example.com", "content-type": "application/json"}

c = http.client.HTTPConnection("127.0.0.1", 18900); c.request("HEAD", "/api/hello"); hr = c.getresponse(); hr.read()
results.append(ok(hr.status == 200, "keyless HEAD /api/hello connectivity check answered locally"))
r, body, _, _, _ = call({"content-type": "application/json"})
results.append(ok(r.status == 403 and b"developer key" in body, "no key -> 403"))
r, body, _, _, _ = call(dict(H, authorization="Bearer cgw-wrong"))
results.append(ok(r.status == 403, "wrong key -> 403"))

n0 = len(seen())
r, body, t_first, t_total, conn = call(H)
s = seen()[n0:]
results.append(ok(r.status == 200 and body.count(b"event: delta") == 3, "limited account skipped, request served"))
results.append(ok([x["tok"][-4:] for x in s] == ["ITED", "GOOD"], f"tried limited then good: {[x['tok'] for x in s]}"))
results.append(ok(t_first < 0.3 and t_total > 0.9, f"streams incrementally (first chunk {t_first:.2f}s, total {t_total:.2f}s)"))
results.append(ok(not any(k.lower().startswith("anthropic-ratelimit-unified") for k in r.headers), "per-account usage headers stripped"))
up_h = s[-1]
results.append(ok(up_h["beta"] == "claude-code-20250219,interleaved-thinking-2025-05-14,oauth-2025-04-20", f"betas merged: {up_h['beta']}"))
results.append(ok(up_h["leaked"] == [], "dev key / tailscale headers not forwarded"))

n0 = len(seen())
r, body, *_ = call(H, conn=conn)   # same keep-alive connection
results.append(ok(r.status == 200 and [x["tok"][-4:] for x in seen()[n0:]] == ["GOOD"], "limited account remembered; keep-alive works"))
r, body, *_ = call(H, body=b'{"model":"chunked-body"}', chunked=True)
results.append(ok(r.status == 200 and seen()[-1]["body_len"] == len(b'{"model":"chunked-body"}'), "chunked request body"))

c = http.client.HTTPConnection("127.0.0.1", 18900); c.request("GET", "/_gateway/status"); snap = json.loads(c.getresponse().read())
acc = {a["label"]: a for a in snap["accounts"]}
results.append(ok(not acc["limited"]["available"] and acc["limited"]["blocked_until"] > time.time() + 7000, "limited blocked until its reset header"))
results.append(ok(acc["good"]["five_hour"] == 0.42 and acc["good"]["requests"] == 3, f"usage tracked from headers ({acc['good']['five_hour']}, {acc['good']['requests']} req)"))
results.append(ok(snap["developers"][0]["name"] == "alice" and snap["developers"][0]["requests"] == 3, "per-developer counts"))
c = http.client.HTTPConnection("127.0.0.1", 18900); c.request("GET", "/_gateway"); page = c.getresponse().read().decode()
results.append(ok("<table>" in page and "alice" in page and "usage limit reached" in page, "HTML status page"))
rc, out = cli("status"); print("    " + out.replace("\n", "\n    "))
results.append(ok(rc == 0 and "usage limit reached" in out, "CLI status"))

# live account edits: drop 'good' -> only the limited one is left -> synthesized 429
cli("account", "rm", "good")
r, body, *_ = call(H)
msg = json.loads(body)["error"]["message"]
results.append(ok(r.status == 429 and r.headers.get("anthropic-ratelimit-unified-status") == "rejected"
                  and int(r.headers.get("anthropic-ratelimit-unified-reset")) > time.time() + 7000, f"all limited -> 429 shaped like a usage limit: {msg}"))

# 401 failover + throttle handling, with accounts swapped live
cli("account", "import", stdin="bad=sk-ant-oat01-BAD\nthrottle=sk-ant-oat01-THROTTLE\ngood=sk-ant-oat01-GOOD\n")
n0 = len(seen())
r, body, *_ = call(dict(H, **{"x-claude-code-session-id": "s2"}))
order = [x["tok"].split("-")[-1] for x in seen()[n0:]]
results.append(ok(r.status == 200 and order == ["BAD", "THROTTLE", "GOOD"], f"revoked + throttled accounts skipped: {order}"))
c = http.client.HTTPConnection("127.0.0.1", 18900); c.request("GET", "/_gateway/status"); acc = {a["label"]: a for a in json.loads(c.getresponse().read())["accounts"]}
results.append(ok("401" in acc["bad"]["blocked_reason"] and acc["throttle"]["blocked_until"] < time.time() + 61, "401 -> 10 min block, throttle -> 60s block"))

gw.terminate(); gw.wait(5)
st = json.load(open(f"{G}/data/state.json"))
results.append(ok(st["accounts"]["limited"]["blocked_until"] > time.time(), "state persisted on shutdown"))
up.terminate()
print("\n--- gateway log"); print(open(f"{G}/gw.log").read().strip())
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
