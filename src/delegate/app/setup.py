"""The fixers behind `doctor --fix ID`: install the Codex CLI and plugin, start the Codex device login, wire the status line. Each returns
{done, say}; the report (doctor.py) says which check asks for which fixer."""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser

from delegate.adapters.harness.codex import CodexHarness
from delegate.app import env, onboard


def run_cmd(argv, timeout=300):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env.app_free_env())
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


def do_codex_login(open_browser=False):
    codex = shutil.which("codex")
    if not codex:
        return {"done": False, "say": "Install the Codex CLI first (`doctor --fix codex_cli` does it)."}
    log = env.config_dir() / "codex-login.log"
    st = env.load_state()
    pid = st.codex_login_pid
    alive = False
    if pid:
        with contextlib.suppress(OSError):
            os.kill(pid, 0)
            alive = True
    if not alive:
        with open(log, "w", encoding="utf-8") as lf:
            p = subprocess.Popen([codex, "login", "--device-auth"], stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        env.update_state(lambda s: setattr(s, "codex_login_pid", p.pid))
    url = code = None
    end = time.time() + 20
    while time.time() < end and not (url and code):
        time.sleep(0.5)
        with contextlib.suppress(OSError):
            url, code = CodexHarness.device_login_prompt(log.read_text(encoding="utf-8", errors="replace"))
    if not url:
        return {"done": False, "say": "Codex did not print a login link. Run `codex login --device-auth` in your own terminal."}
    if open_browser and (sys.platform == "darwin" or os.environ.get("DISPLAY")):
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    return {"done": False, "waiting_for_user": True,
            "say": f"Open {url} on any device and enter the code {code or '(shown on that page)'}. The login keeps waiting in the background; run `doctor --next` again when you are done."}


def write_shim():
    p = onboard.shim_path()
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


def do_statusline():
    shim = write_shim()
    state = onboard.statusline_state()
    cmd = f'{sys.executable} "{shim}"'
    if state == "unset":
        path = onboard.claude_settings_path()
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


def act(cfg, do=None, decline=None, open_browser=False):
    """Run one fixer (`doctor --fix ID`) or drop a lane (`doctor --decline LANE`)."""
    if decline:
        if decline not in cfg["lanes"]:
            return {"error": f"unknown lane {decline}"}
        env.set_profile_lane(decline, "declined")
        return {"done": True, "say": f"{decline} opted out. `doctor --undecline {decline}` brings it back."}
    fn = FIXERS.get(do)
    if not fn:
        return {"error": f"no automatic fixer for {do}; the step's `say` tells the user what to do"}
    return fn(open_browser) if do == "codex_login" else fn()
