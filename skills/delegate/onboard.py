"""Onboarding for the delegate router: doctor, set-key, config. Loaded lazily by route.py.

Rules:
  - `doctor` is the state. It derives readiness from the machine on every call; no progress is stored.
  - The agent never handles a secret. The API key is set by the user, in their own terminal (set-key).
  - Login is always device-auth: works on a laptop, over SSH and in a container. It is a user step.
  - Everything the user must read is produced here as `say`; skills only relay it.
"""
import contextlib
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def claude_settings_path():
    return Path.home() / ".claude" / "settings.json"


def route_path():
    return Path(__file__).resolve().parent / "route.py"


def shim_path(R):
    return R.config_dir() / "statusline.py"


# ----------------------------------------------------------------------------- readiness checks (no subprocess unless noted)

def check_codex_cli(R):
    return bool(shutil.which("codex"))


def check_codex_login(R):
    return (codex_home() / "auth.json").exists()


def check_codex_plugin(R):
    return any((Path.home() / ".claude" / "plugins" / "cache" / "openai-codex").glob("codex/*"))


def check_claude_cli(R):
    return bool(shutil.which("claude"))


def check_claude_login(R):
    exe = shutil.which("claude")
    if not exe:
        return False
    ok, out = run_cmd([exe, "auth", "status"], timeout=15)
    try:
        return bool(json.loads(out).get("loggedIn"))
    except ValueError:
        return False


def check_credit_key(R):
    return bool(R.find_key(quiet=True))


def check_usage_source(R):
    f = Path.home() / ".claude" / "state" / "max-limits.json"
    return f.exists()


def statusline_state():
    """unset | wired | custom. 'wired' = the status line (or a script it runs) calls capture-limits or our shim."""
    try:
        s = json.loads(claude_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unset"
    sl = s.get("statusLine")
    if not sl:
        return "unset"
    cmd = sl.get("command", "") if isinstance(sl, dict) else str(sl)
    if "capture-limits" in cmd or "statusline.py" in cmd:
        return "wired"
    for m in re.finditer(r"[\w~./$\\-]+\.(?:sh|py)", cmd):
        path = Path(os.path.expandvars(os.path.expanduser(m.group(0).replace("$HOME", "~"))))
        with contextlib.suppress(OSError):
            if "capture-limits" in path.read_text(encoding="utf-8", errors="replace"):
                return "wired"
    return "custom"


READY_CHECKS = {
    "codex": lambda R: check_codex_cli(R) and check_codex_login(R),
    "claude_credit": lambda R: check_claude_cli(R) and check_credit_key(R),
    "always": lambda R: True,
}


def lane_configured(R, lane_cfg):
    return READY_CHECKS[lane_cfg.get("ready", "always")](R)


def configured_lanes(R, cfg):
    return {n: lane_configured(R, c) for n, c in cfg["lanes"].items()}


# ----------------------------------------------------------------------------- the check list (order = setup order)

def build_checks(R, cfg, st):
    S = cfg["settings"]
    skip = {n for n, v in R.profile_lanes().items() if v == "declined"}
    py = str(sys.executable)
    rp = str(route_path())
    oa_skip = "OPENAI_SUBSCRIPTION" in skip
    cr_skip = "CLAUDE_CREDIT" in skip
    checks = [
        {"id": "codex_cli", "lane": "OPENAI_SUBSCRIPTION", "ok": check_codex_cli(R), "skip": oa_skip, "who": "agent",
         "say": "Install the Codex CLI.", "run": "npm install -g @openai/codex", "do": "codex_cli"},
        {"id": "codex_login", "lane": "OPENAI_SUBSCRIPTION", "ok": check_codex_login(R), "skip": oa_skip, "who": "user",
         "say": "Sign in to ChatGPT for Codex. The agent starts the login and shows you a link and a code; open the link on any device and enter the code.",
         "do": "codex_login"},
        {"id": "codex_plugin", "lane": "OPENAI_SUBSCRIPTION", "ok": check_codex_plugin(R), "skip": oa_skip, "who": "agent", "optional": True,
         "say": "Install the official Codex plugin for Claude Code (adds /codex:review and friends).",
         "run": "claude plugin marketplace add openai/codex-plugin-cc && claude plugin install codex@openai-codex --scope user", "do": "codex_plugin"},
        {"id": "claude_cli", "lane": "CLAUDE_CREDIT", "ok": check_claude_cli(R), "skip": cr_skip, "who": "user",
         "say": "The `claude` CLI is not on PATH, so the credit lane cannot run workers. Install Claude Code."},
        {"id": "credit_key", "lane": "CLAUDE_CREDIT", "ok": check_credit_key(R), "skip": cr_skip, "who": "user",
         "say": ("Create an API key for the credit lane (claude.ai > Settings > Billing > API credits: link a Console organization; "
                 "Console > Settings > API keys: key in a workspace with a spend limit). Never paste it into chat. Give it to the tool one of these ways: "
                 f"(1) set env CLAUDE_CREDIT_WORKER_KEY on this host; (2) in your own terminal run: {py} \"{rp}\" set-key; "
                 f"(3) no terminal: tell the agent to run `{py} \"{rp}\" set-key --web` (add `--host tailscale` to open the link from another device) and open the link it shows; "
                 f"(4) skip the lane: `{py} \"{rp}\" doctor --decline CLAUDE_CREDIT`."),
         "url": "https://platform.claude.com"},
        {"id": "claude_login", "lane": "CLAUDE_SUBSCRIPTION", "ok": check_claude_login(R), "who": "user", "optional": True,
         "say": "Headless callers (task-loop, cron) run `claude -p` on your Claude login, and it is not signed in. Run `claude auth login`."},
        {"id": "weekly_reset", "lane": "CLAUDE_SUBSCRIPTION", "ok": bool(S.get("claude_weekly_reset")), "who": "user", "optional": True,
         "say": f"When does your Claude weekly limit reset (day and local time, e.g. Sun 17:00)? Then run: {py} \"{rp}\" config set claude_weekly_reset \"Sun 17:00\"",
         "do": None},
        {"id": "usage_source", "lane": "CLAUDE_SUBSCRIPTION", "ok": check_usage_source(R) or statusline_state() == "wired", "who": "agent", "optional": True,
         "say": ("Claude Code gives plan limits only to the status line, which does not run in headless sessions (-p, Claude RC, containers). "
                 "Wiring it lets interactive sessions on this machine record them. Without it, subscription use is unknown here and Claude steps prefer the credit lane."),
         "do": "statusline"},
    ]
    return checks


def doctor(R, cfg, deep=False, lane=None):
    st = R.load_state()
    checks = build_checks(R, cfg, st)
    lanes = configured_lanes(R, cfg)
    skip = {n for n, v in R.profile_lanes().items() if v == "declined"}
    ack = R.acked()
    notes = []
    if (os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")) and "volumes" not in ack:
        notes.append({"id": "volumes", "say": "Container: whether ~/.codex and ~/.claude survive a rebuild is not checked. If your home folder is already a volume, run `route.py config ack volumes` and this goes away."})
    for c in checks:
        if c["id"] in ack and not c["ok"]:
            c["skip"] = True
            c["acked"] = True
    if deep:
        key = R.find_key(quiet=True)
        if key:
            checks.append({"id": "credit_key_valid", "lane": "CLAUDE_CREDIT", "ok": validate_key(key)[0], "skip": "CLAUDE_CREDIT" in skip,
                           "who": "user", "say": "The stored API key was rejected: " + validate_key(key)[1]})
    enabled = {n for n, v in R.profile_lanes().items() if v == "enabled"}
    shared = {n for n, c in cfg["lanes"].items() if c.get("shared_with_orchestrator")}
    if lane:
        checks = [c for c in checks if c["lane"] == lane]
    for c in checks:                # a lane nobody chose and the machine cannot run is on offer, not a problem
        if not c["ok"] and not c.get("skip") and c["lane"] not in enabled | shared and c["lane"] != lane:
            c["available"] = True
    blocking = [c for c in checks if not c["ok"] and not c.get("optional") and not c.get("skip") and not c.get("available")]
    return {"ready": not blocking, "lanes": lanes, "declined": sorted(skip), "harness": R.harness(), "checks": checks, "notes": notes}


def validate_key(key):
    req = urllib.request.Request("https://api.anthropic.com/v1/models", headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return None, f"could not reach the API ({e})"


def print_doctor(d, quiet):
    bad = [c for c in d["checks"] if not c["ok"] and not c.get("optional") and not c.get("skip") and not c.get("available")]
    if quiet:
        print("READY" if d["ready"] else "SETUP NEEDED: " + ", ".join(c["id"] for c in bad) + " (run `doctor` for what to do about each)")
        return
    for c in d["checks"]:
        mark = "ok     " if c["ok"] else ("acked" if c.get("acked") else "skipped" if c.get("skip") else ("available" if c.get("available") else "optional" if c.get("optional") else "MISSING"))
        print(f"{mark:<9} {c['id']:<14} {c['lane']}")
        if not c["ok"] and not c.get("skip") and not c.get("available"):
            print(f"          -> {c['say']}")
    for n in d["notes"]:
        print(f"note [{n['id']}]: {n['say']}")
    if d["ready"]:
        avail = sorted({c["lane"] for c in d["checks"] if c.get("available")})
        print("READY" + (f". Not set up, your choice: {', '.join(avail)} (`doctor --lane NAME` to set one up, `doctor --decline NAME` to hide it)" if avail else ""))
    else:
        print("SETUP NEEDED. Each MISSING item above says what to do. You choose: do it (`doctor --fix ID`), `doctor --decline LANE` to drop its lane, or `doctor --ack ID` to say it needs nothing.")


# ----------------------------------------------------------------------------- doctor --next: first failing check -> one step

def next_step(d):
    """First failing REQUIRED check as a step; optional ones are listed, not pushed."""
    optional = [c["id"] for c in d["checks"] if not c["ok"] and not c.get("skip") and not c.get("available") and c.get("optional")]
    for c in d["checks"]:
        if not c["ok"] and not c.get("skip") and not c.get("optional") and not c.get("available"):
            step = {"step": c["id"], "lane": c["lane"], "who": c["who"], "say": c["say"]}
            for k in ("run", "url", "do"):
                if c.get(k):
                    step[k] = c[k]
            step["then"] = f"run `doctor --next` again; or `doctor --decline {c['lane']}` to drop this lane; or `doctor --ack {c['id']}` if it needs nothing"
            if optional:
                step["optional_steps_later"] = optional
            return step
    return {"optional_only": optional} if optional else None


def run_cmd(argv, timeout=300):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-600:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def do_codex_cli(R):
    npm = shutil.which("npm")
    if not npm:
        return {"done": False, "say": "npm is not installed, so the Codex CLI cannot be installed automatically. Install Node.js 18.18+ then run `doctor --next` again."}
    ok, out = run_cmd([npm, "install", "-g", "@openai/codex"])
    return {"done": ok, "say": "Codex CLI installed." if ok else "npm install failed: " + out}


def do_codex_plugin(R):
    claude = shutil.which("claude")
    if not claude:
        return {"done": False, "say": "The claude CLI is not on PATH."}
    ok1, o1 = run_cmd([claude, "plugin", "marketplace", "add", "openai/codex-plugin-cc"])
    ok2, o2 = run_cmd([claude, "plugin", "install", "codex@openai-codex", "--scope", "user"])
    return {"done": ok1 and ok2, "say": "Codex plugin installed." if (ok1 and ok2) else "plugin install failed: " + (o2 or o1)}


def do_codex_login(R, open_browser=False):
    codex = shutil.which("codex")
    if not codex:
        return {"done": False, "say": "Install the Codex CLI first (`doctor --fix codex_cli` does it)."}
    log = R.config_dir() / "codex-login.log"
    st = R.load_state()
    pid = st.get("codex_login_pid")
    alive = False
    if pid and os.name != "nt":
        with contextlib.suppress(OSError):
            os.kill(pid, 0)
            alive = True
    if not alive:
        with open(log, "w", encoding="utf-8") as lf:
            kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000008 | 0x00000200}
            p = subprocess.Popen([codex, "login", "--device-auth"], stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
        R.update_state(lambda s: s.__setitem__("codex_login_pid", p.pid))
    url = code = None
    end = time.time() + 20
    while time.time() < end and not (url and code):
        time.sleep(0.5)
        with contextlib.suppress(OSError):
            text = ANSI.sub("", log.read_text(encoding="utf-8", errors="replace"))
            m = re.search(r"https://\S+", text)
            c = re.search(r"\b[A-Z0-9]{4,}-[A-Z0-9]{4,}\b", text)
            url, code = (m.group(0) if m else None), (c.group(0) if c else None)
    if not url:
        return {"done": False, "say": "Codex did not print a login link. Run `codex login --device-auth` in your own terminal."}
    if open_browser and (sys.platform in ("darwin", "win32") or os.environ.get("DISPLAY")):
        with contextlib.suppress(Exception):
            import webbrowser
            webbrowser.open(url)
    return {"done": False, "waiting_for_user": True,
            "say": f"Open {url} on any device and enter the code {code or '(shown on that page)'}. The login keeps waiting in the background; run `doctor --next` again when you are done."}


def write_shim(R):
    p = shim_path(R)
    p.write_text(
        '"""Stable status-line entry for the delegate router; finds the newest installed plugin copy."""\n'
        "import glob, os, subprocess, sys\n"
        "home = os.path.expanduser('~')\n"
        "cands = sorted(glob.glob(os.path.join(home, '.claude', 'plugins', 'cache', '*', 'iorlas-delegate', '*', 'skills', 'delegate', 'route.py')),\n"
        "               key=lambda x: [int(t) if t.isdigit() else 0 for t in x.split(os.sep)[-5].replace('.', ' ').split()])\n"
        "if cands:\n"
        "    sys.exit(subprocess.call([sys.executable, cands[-1], 'capture-limits', '--print'], stdin=sys.stdin))\n",
        encoding="utf-8")
    return p


def do_statusline(R):
    shim = write_shim(R)
    state = statusline_state()
    cmd = f'{sys.executable} "{shim}"'
    if state == "unset":
        path = claude_settings_path()
        try:
            s = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except ValueError:
            return {"done": False, "say": f"{path} is not valid JSON; fix it, then run `doctor --next` again."}
        s["statusLine"] = {"type": "command", "command": cmd}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
        return {"done": True, "say": "Status line wired: interactive sessions will record the Claude plan limits. Restart sessions to apply."}
    if state == "wired":
        return {"done": True, "say": "Status line already wired."}
    return {"done": False, "waiting_for_user": True,
            "say": f"You already have a custom status line. Add this line to its script (it reads the same JSON on stdin): printf '%s' \"$input\" | {cmd} >/dev/null"}


FIXERS = {"codex_cli": do_codex_cli, "codex_plugin": do_codex_plugin, "codex_login": do_codex_login, "statusline": do_statusline, "usage_source": do_statusline}


def act(R, cfg, do=None, decline=None, open_browser=False):
    """Run one fixer (`doctor --fix ID`) or drop a lane (`doctor --decline LANE`)."""
    if decline:
        if decline not in cfg["lanes"]:
            return {"error": f"unknown lane {decline}"}
        R.set_profile_lane(decline, "declined")
        return {"done": True, "say": f"{decline} opted out. `doctor --undecline {decline}` brings it back."}
    fn = FIXERS.get(do)
    if not fn:
        return {"error": f"no automatic fixer for {do}; the step's `say` tells the user what to do"}
    return fn(R, open_browser) if do == "codex_login" else fn(R)


# ----------------------------------------------------------------------------- set-key, config

def store_key(R, key, force=False):
    """Write the key file (mode 0600), enable the credit lane, check the key. -> (ok, message). Never echoes the key."""
    path = R.config_dir() / "claude-credit.key"
    if path.exists() and not force:
        return False, f"{path} exists; use --force to replace it"
    if not key:
        return False, "empty key"
    if path.exists():
        path.unlink()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    R.set_profile_lane("CLAUDE_CREDIT", "enabled")
    ok, why = validate_key(key)
    warn = "" if key.startswith("sk-ant-") else " Warning: the key does not start with sk-ant-."
    return True, f"saved to {path} (not printed). Key check: " + ("accepted" if ok else ("rejected, " + why if ok is False else why)) + "." + warn


def set_key(R, from_stdin=False, force=False):
    if from_stdin:
        key = sys.stdin.read().strip()
    elif sys.stdin.isatty():
        key = getpass.getpass("Anthropic API key (input is hidden): ").strip()
    else:
        print("route.py: no terminal. Run this in your own terminal, or use `route.py set-key --web`.", file=sys.stderr)
        return 2
    ok, msg = store_key(R, key, force)
    print(("" if ok else "route.py: ") + msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def allowed_host(host):
    """Loopback, private or tailnet (100.64.0.0/10) addresses only: the page is plain HTTP, so never a public interface."""
    import ipaddress
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
    ok, out = run_cmd([ts, "ip", "-4"], timeout=10)
    return out.split()[0] if ok and out else None


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>delegate: API key</title>
<body style="font:16px system-ui;max-width:32rem;margin:2rem auto;padding:0 1rem">%s</body>"""


def set_key_web(R, host="127.0.0.1", port=0, timeout=300, force=False, on_ready=None):
    """One-shot page to hand over the API key with no terminal. Plain HTTP: loopback/private/tailnet only, one-time token in the path,
    key in the POST body only, closes after one submit or `timeout` seconds. -> exit code."""
    import hmac
    import secrets
    from http.server import BaseHTTPRequestHandler, HTTPServer
    path = R.config_dir() / "claude-credit.key"
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
            from urllib.parse import parse_qs
            key = (parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("key") or [""])[0].strip()
            ok, msg = store_key(R, key, force)
            result["ok"] = ok
            self._send(200 if ok else 400, PAGE % ("<h2>%s</h2><p>You can close this tab.</p>" % ("Saved" if ok else "Not saved: " + msg.replace("<", "&lt;"))))
            if ok:
                result["done"] = True

    import socketserver

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


def config_set(R, key, value):
    try:
        val = json.loads(value)
    except ValueError:
        val = value

    def put(cur):
        node = cur
        parts = key.split(".")
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = val
    return R.update_config(put)


def config_show(R, cfg):
    p = R.config_dir() / "config.json"
    return {"file": str(p), "overrides": json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}, "effective_settings": cfg["settings"]}
