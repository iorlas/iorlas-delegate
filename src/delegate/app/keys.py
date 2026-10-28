"""Handing over the credit lane's API key: a terminal prompt (`set-key`), stdin, or a one-shot page (`set-key --web`). The key is written 0600, never
echoed, and never passes through the agent."""
import getpass
import hmac
import ipaddress
import os
import secrets
import shutil
import socketserver
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

from delegate.app import env, setup


def validate_key(key):
    req = urllib.request.Request("https://api.anthropic.com/v1/models", headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return None, f"could not reach the API ({e})"


def store_key(key, force=False):
    """Write the key file (mode 0600), enable the credit lane, check the key. -> (ok, message). Never echoes the key."""
    path = env.config_dir() / "claude-credit.key"
    if path.exists() and not force:
        return False, f"{path} exists; use --force to replace it"
    if not key:
        return False, "empty key"
    if path.exists():
        path.unlink()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    env.set_profile_lane("CLAUDE_CREDIT", "enabled")
    ok, why = validate_key(key)
    warn = "" if key.startswith("sk-ant-") else " Warning: the key does not start with sk-ant-."
    return True, f"saved to {path} (not printed). Key check: " + ("accepted" if ok else ("rejected, " + why if ok is False else why)) + "." + warn


def set_key(from_stdin=False, force=False):
    if from_stdin:
        key = sys.stdin.read().strip()
    elif sys.stdin.isatty():
        key = getpass.getpass("Anthropic API key (input is hidden): ").strip()
    else:
        print("route.py: no terminal. Run this in your own terminal, or use `route.py set-key --web`.", file=sys.stderr)
        return 2
    ok, msg = store_key(key, force)
    print(("" if ok else "route.py: ") + msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def allowed_host(host):
    """Loopback, private or tailnet (100.64.0.0/10) addresses only: the page is plain HTTP, so never a public interface."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_unspecified or ip.is_multicast:        # 0.0.0.0 counts as private in Python but listens on every interface
        return False
    return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


def tailscale_ip():
    ts = shutil.which("tailscale")
    if not ts:
        return None
    ok, out = setup.run_cmd([ts, "ip", "-4"], timeout=10)
    return out.split()[0] if ok and out else None


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>delegate: API key</title>
<body style="font:16px system-ui;max-width:32rem;margin:2rem auto;padding:0 1rem">%s</body>"""


def set_key_web(host="127.0.0.1", port=0, timeout=300, force=False, on_ready=None):
    """One-shot page to hand over the API key with no terminal. Plain HTTP: loopback/private/tailnet only, one-time token in the path,
    key in the POST body only, closes after one submit or `timeout` seconds. -> exit code."""
    path = env.config_dir() / "claude-credit.key"
    if host == "tailscale":
        host = tailscale_ip()
        if not host:
            print("route.py: tailscale not found or not connected; use --host <private IP>", file=sys.stderr)
            return 2
    if not allowed_host(host):
        print(f"route.py: --host {host} is not loopback, private or tailnet; refusing to serve a key form over plain HTTP there", file=sys.stderr)
        return 2
    if path.exists() and not force:
        print(f"route.py: {path} exists; use --force to replace it", file=sys.stderr)
        return 1
    token = secrets.token_urlsafe(16)
    result = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _ok(self):
            hostname = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return hmac.compare_digest(self.path, "/k/" + token) and hostname == host

        def do_GET(self):
            if not self._ok():
                return self._send(404, "not found")
            self._send(200, PAGE % ('<h2>Anthropic API key</h2><p>For the delegate credit lane. It is saved on this machine only and never printed.</p>'
                                    '<form method=post><input name=key type=password autofocus autocomplete=off style="width:100%;font-size:16px;padding:.5rem">'
                                    '<p><button style="font-size:16px;padding:.5rem 1rem">Save</button></p></form>'))

        def do_POST(self):
            if not self._ok():
                return self._send(404, "not found")
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            key = (parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("key") or [""])[0].strip()
            ok, msg = store_key(key, force)
            result["ok"] = ok
            self._send(200 if ok else 400, PAGE % ("<h2>%s</h2><p>You can close this tab.</p>" % ("Saved" if ok else "Not saved: " + msg.replace("<", "&lt;"))))
            if ok:
                result["done"] = True

    class Srv(HTTPServer):
        def server_bind(self):          # HTTPServer.server_bind does a reverse-DNS lookup that stalls for seconds on some hosts
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = host, self.socket.getsockname()[1]

    try:
        srv = Srv((host, port), H)
    except OSError as e:
        print(f"route.py: cannot listen on {host}: {e}", file=sys.stderr)
        return 1
    srv.timeout = 1.0
    url = f"http://{host}:{srv.server_address[1]}/k/{token}"
    (on_ready or (lambda u: print(f"Open this link on any device that can reach {host} and paste the key (valid {max(1, timeout // 60)} min, one use): {u}", flush=True)))(url)
    end = time.time() + timeout
    while not result.get("done") and time.time() < end:
        srv.handle_request()
    srv.server_close()
    if result.get("done"):
        print("key saved", flush=True)
        return 0
    print("route.py: timed out waiting for the key; nothing was saved", file=sys.stderr)
    return 1
