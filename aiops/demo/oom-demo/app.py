"""oom-demo: a tiny HTTP service whose memory demand is set by ALLOC_MB.

It touches ALLOC_MB of memory at start-up (every page is written, so it counts against the cgroup),
then serves /health. With a container limit below ALLOC_MB + the interpreter's own ~10 MiB the kernel
OOM-kills it (reason OOMKilled, exit 137); with enough headroom it stays Ready. Nothing else.
"""
import http.server
import os

ALLOC_MB = int(os.environ.get("ALLOC_MB", "12"))
_ballast = bytearray(ALLOC_MB * 1024 * 1024)
for i in range(0, len(_ballast), 4096):      # touch every page
    _ballast[i] = 1


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok\n" if self.path == "/health" else f"oom-demo alloc={ALLOC_MB}MiB\n".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("oom-demo", fmt % args, flush=True)


print(f"oom-demo started alloc={ALLOC_MB}MiB", flush=True)
http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
