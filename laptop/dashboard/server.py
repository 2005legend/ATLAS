"""Local dashboard for the personal assistant. Run on the laptop:  python dashboard/server.py
Then open http://127.0.0.1:8765

Serves index.html and proxies a fixed allow-list of API calls to the assistant service on VM 1 over Tailscale.
The bearer token stays in this process (read from %USERPROFILE%/.hermes-dashboard/token), never in the browser.
Listens on 127.0.0.1 only, so nothing else on your network can use it.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PA_URL = os.environ.get("PA_URL", "http://VM1_TAILSCALE_IP:8100")
TOKEN_FILE = Path(os.environ.get("PA_TOKEN_FILE", Path.home() / ".hermes-dashboard" / "token"))
PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))

# (method, path regex) the browser may call; everything else is refused
ALLOWED = [
    ("GET", r"/system"), ("GET", r"/agenda(\?days=\d+)?"), ("GET", r"/tasks(\?[\w=&%+.-]*)?"),
    ("GET", r"/inbox(\?[\w=&%+.-]*)?"), ("GET", r"/reminders(\?[\w=&%+.-]*)?"), ("GET", r"/habits(\?[\w=&%+.-]*)?"),
    ("GET", r"/expenses/summary(\?[\w=&%+.-]*)?"), ("GET", r"/expenses(\?[\w=&%+.-]*)?"),
    ("POST", r"/tasks"), ("POST", r"/tasks/\d+/done"), ("PATCH", r"/tasks/\d+"), ("DELETE", r"/tasks/\d+"),
    ("POST", r"/inbox"), ("POST", r"/reminders"), ("POST", r"/reminders/r[0-9a-f]{8}/(cancel|snooze)"),
    ("POST", r"/habits/\d+/checkin"), ("POST", r"/expenses"), ("POST", r"/parse_time"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "pa-dashboard/1.0"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method: str):
        path = self.path[len("/api"):]
        if not any(m == method and re.fullmatch(p, path) for m, p in ALLOWED):
            return self._send(403, b'{"detail":"not allowed from the dashboard"}')
        # simple CSRF guard: only our own page (same origin) may call the API
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self._send(403, b'{"detail":"bad origin"}')
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, 64_000)) if length else None
        req = urllib.request.Request(PA_URL + path, data=body, method=method, headers={
            "Authorization": "Bearer " + TOKEN_FILE.read_text().strip(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return self._send(r.status, r.read())
        except urllib.error.HTTPError as e:
            return self._send(e.code, e.read())
        except OSError as e:
            return self._send(503, json.dumps({"detail": f"VM 1 unreachable ({type(e).__name__}). "
                                               "Is Tailscale on, and is VM 1 awake? Run .\\wake-vms.ps1"}).encode())

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path.startswith("/api/"):
            return self._proxy("GET")
        self._send(404, b'{"detail":"not found"}')

    def do_POST(self):
        self._proxy("POST") if self.path.startswith("/api/") else self._send(404, b"{}")

    def do_PATCH(self):
        self._proxy("PATCH") if self.path.startswith("/api/") else self._send(404, b"{}")

    def do_DELETE(self):
        self._proxy("DELETE") if self.path.startswith("/api/") else self._send(404, b"{}")


if __name__ == "__main__":
    if not TOKEN_FILE.exists():
        sys.exit(f"Token file missing: {TOKEN_FILE}")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Dashboard on {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
