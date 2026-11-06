import http.server, json, sys, time
SEEN = open(sys.argv[2], "a")
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0); body = self.rfile.read(n)
        tok = self.headers.get("authorization", "").removeprefix("Bearer ")
        SEEN.write(json.dumps({"tok": tok, "path": self.path, "beta": self.headers.get("anthropic-beta"),
            "leaked": [k for k in self.headers if k.lower() in ("x-api-key", "tailscale-user-login", "x-forwarded-for")],
            "body_len": len(body)}) + "\n"); SEEN.flush()
        now = time.time()
        if tok.endswith("LIMITED"):
            return self.reply(429, {"anthropic-ratelimit-unified-status": "rejected", "anthropic-ratelimit-unified-reset": str(int(now + 7200)),
                                    "anthropic-ratelimit-unified-5h-utilization": "1.0", "anthropic-ratelimit-unified-representative-claim": "five_hour"},
                              b'{"type":"error","error":{"type":"rate_limit_error","message":"limit"}}')
        if tok.endswith("THROTTLE"):
            return self.reply(429, {}, b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}')
        if tok.endswith("BAD"):
            return self.reply(401, {}, b'{"type":"error","error":{"type":"authentication_error","message":"invalid"}}')
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("anthropic-ratelimit-unified-status", "allowed")
        self.send_header("anthropic-ratelimit-unified-5h-utilization", "0.42")
        self.send_header("anthropic-ratelimit-unified-5h-reset", str(int(now + 3000)))
        self.send_header("anthropic-ratelimit-unified-7d-utilization", "0.10")
        self.send_header("anthropic-ratelimit-unified-7d-reset", str(int(now + 90000)))
        self.send_header("transfer-encoding", "chunked"); self.end_headers()
        for i in range(3):
            c = f"event: delta\ndata: {{\"i\": {i}, \"acct\": \"{tok[-4:]}\"}}\n\n".encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(c), c)); self.wfile.flush(); time.sleep(0.4)
        self.wfile.write(b"0\r\n\r\n")
    def reply(self, code, headers, body):
        self.send_response(code)
        for k, v in headers.items(): self.send_header(k, v)
        self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
