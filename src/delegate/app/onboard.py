"""Readiness: what each lane needs on this machine (CLI, login, key, status line), as checks in setup order. `doctor` turns them into the report;
`configured_lanes` tells the fleet which lanes the machine can run. No state is stored: every call derives it from the machine."""
import contextlib
import json
import os
import shutil
import sys
from pathlib import Path

from delegate.adapters import statusline
from delegate.adapters.harness.claude import ClaudeHarness
from delegate.adapters.harness.opencode import OpenCodeHarness
from delegate.app import env, harnesses

LOGIN_CHECK_S = 15


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def claude_settings_path():
    return Path.home() / ".claude" / "settings.json"


def route_path():
    return env.SKILL_DIR / "route.py"


def shim_path():
    return env.config_dir() / "statusline.py"


def check_codex_cli():
    return bool(shutil.which("codex"))


def check_codex_login():
    return (codex_home() / "auth.json").exists()


def check_codex_plugin():
    return any((Path.home() / ".claude" / "plugins" / "cache" / "openai-codex").glob("codex/*"))


def check_claude_cli():
    return bool(shutil.which("claude"))


def check_claude_login():
    return ClaudeHarness().login_probe(LOGIN_CHECK_S)[0] is True


def check_credit_key():
    return bool(harnesses.find_key(quiet=True))


def check_usage_source():
    return statusline.path().exists()


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
    for word in cmd.split():
        if word.strip("\"'").endswith((".sh", ".py")):
            path = Path(os.path.expandvars(os.path.expanduser(word.strip("\"'").replace("$HOME", "~"))))
            with contextlib.suppress(OSError):
                if "capture-limits" in path.read_text(encoding="utf-8", errors="replace"):
                    return "wired"
    return "custom"


def check_opencode_xai():
    """macOS (commands run under sandbox-exec), the opencode CLI, and an xAI login in its auth file (the key's presence only)."""
    if sys.platform != "darwin" or not OpenCodeHarness.find_exe():
        return False
    f = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "auth.json"
    try:
        return "xai" in json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False


def check_opencode_deepseek_free():
    """Explicit opt-in; free OpenCode access needs no provider credential."""
    return (sys.platform == "darwin" and bool(OpenCodeHarness.find_exe())
            and env.load_routing()["settings"].get("deepseek_free_enabled") is True)


def check_opencode_zai_coding():
    """Subscription-specific saved credential only; never an API-credit provider or env key."""
    if (sys.platform != "darwin" or not OpenCodeHarness.find_exe()
            or env.load_routing()["settings"].get("zai_coding_enabled") is not True):
        return False
    f = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "auth.json"
    try:
        auth = json.loads(f.read_text(encoding="utf-8")).get("zai-coding-plan", {})
        return isinstance(auth, dict) and auth.get("type") == "api" and bool(auth.get("key"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


READY_CHECKS = {
    "opencode_zai_coding": check_opencode_zai_coding,
    "opencode_xai": check_opencode_xai,
    "opencode_deepseek_free": check_opencode_deepseek_free,
    "codex": lambda: check_codex_cli() and check_codex_login(),
    "claude_credit": lambda: check_claude_cli() and check_credit_key(),
    "always": lambda: True,
}


def lane_configured(lane_cfg):
    return READY_CHECKS[lane_cfg.get("ready", "always")]()


def configured_lanes(cfg):
    return {n: lane_configured(c) for n, c in cfg["lanes"].items()}


# ----------------------------------------------------------------------------- the check list (order = setup order)

def build_checks(cfg):
    S = cfg["settings"]
    skip = {n for n, v in env.profile_lanes().items() if v == "declined"}
    py = str(sys.executable)
    rp = str(route_path())
    oa_skip = "OPENAI_SUBSCRIPTION" in skip
    cr_skip = "CLAUDE_CREDIT" in skip
    checks = [
        {"id": "codex_cli", "lane": "OPENAI_SUBSCRIPTION", "ok": check_codex_cli(), "skip": oa_skip, "who": "agent",
         "say": "Install the Codex CLI.", "run": "npm install -g @openai/codex", "do": "codex_cli"},
        {"id": "codex_login", "lane": "OPENAI_SUBSCRIPTION", "ok": check_codex_login(), "skip": oa_skip, "who": "user",
         "say": "Sign in to ChatGPT for Codex. The agent starts the login and shows you a link and a code; open the link on any device and enter the code.",
         "do": "codex_login"},
        {"id": "codex_plugin", "lane": "OPENAI_SUBSCRIPTION", "ok": check_codex_plugin(), "skip": oa_skip, "who": "agent", "optional": True,
         "say": "Install the official Codex plugin for Claude Code (adds /codex:review and friends).",
         "run": "claude plugin marketplace add openai/codex-plugin-cc && claude plugin install codex@openai-codex --scope user", "do": "codex_plugin"},
        {"id": "claude_cli", "lane": "CLAUDE_CREDIT", "ok": check_claude_cli(), "skip": cr_skip, "who": "user",
         "say": "The `claude` CLI is not on PATH, so the credit lane cannot run workers. Install Claude Code."},
        {"id": "credit_key", "lane": "CLAUDE_CREDIT", "ok": check_credit_key(), "skip": cr_skip, "who": "user",
         "say": ("Create an API key for the credit lane (claude.ai > Settings > Billing > API credits: link a Console organization; "
                 "Console > Settings > API keys: key in a workspace with a spend limit). Never paste it into chat. Give it to the tool one of these ways: "
                 f"(1) set env CLAUDE_CREDIT_WORKER_KEY on this host; (2) in your own terminal run: {py} \"{rp}\" set-key; "
                 f"(3) no terminal: tell the agent to run `{py} \"{rp}\" set-key --web` (add `--host tailscale` to open the link from another device) and open the link it shows; "
                 f"(4) skip the lane: `{py} \"{rp}\" doctor --decline CLAUDE_CREDIT`."),
         "url": "https://platform.claude.com"},
        {"id": "opencode_xai", "lane": "GROK_SUBSCRIPTION", "ok": check_opencode_xai(), "skip": "GROK_SUBSCRIPTION" in skip, "who": "user", "optional": True,
         "say": "Optional Grok lane (macOS): install OpenCode (https://opencode.ai) and sign in with SuperGrok: `opencode auth login`, pick xAI, then SuperGrok Subscription."},
        {"id": "opencode_deepseek_free", "lane": "DEEPSEEK_FREE", "ok": check_opencode_deepseek_free(), "skip": "DEEPSEEK_FREE" in skip, "who": "user", "optional": True,
         "say": "Optional free-only DeepSeek lane (macOS): install OpenCode, then `route.py config set deepseek_free_enabled true`. No key required. Only the exact free model is used; catalog price must be zero. Disable with `route.py config set deepseek_free_enabled false`."},
        {"id": "opencode_zai_coding", "lane": "ZAI_CODING_SUBSCRIPTION", "ok": check_opencode_zai_coding(), "skip": "ZAI_CODING_SUBSCRIPTION" in skip, "who": "user", "optional": True,
         "say": "Z.ai Coding Plan: run `opencode auth login`, choose Z.AI Coding Plan and enter your subscription key in your own terminal. Enable with `route.py config set zai_coding_enabled true`. Never paste a key in chat. Ordinary Z.AI API credit is a separate provider."},
        {"id": "claude_login", "lane": "CLAUDE_SUBSCRIPTION", "ok": check_claude_login(), "who": "user", "optional": True,
         "say": "Unattended callers (task-loop, cron) run `claude -p` on your Claude login, and it is not signed in. Run `claude auth login`."},
        {"id": "weekly_reset", "lane": "CLAUDE_SUBSCRIPTION", "ok": bool(S.get("claude_weekly_reset")), "who": "user", "optional": True,
         "say": f"When does your Claude weekly limit reset (day and local time, e.g. Sun 17:00)? Then run: {py} \"{rp}\" config set claude_weekly_reset \"Sun 17:00\"",
         "do": None},
        {"id": "usage_source", "lane": "CLAUDE_SUBSCRIPTION", "ok": check_usage_source() or statusline_state() == "wired", "who": "agent", "optional": True,
         "say": ("Claude Code gives plan limits only to the status line, which does not run in headless sessions (-p, Claude RC, containers). "
                 "Wiring it lets interactive sessions on this machine record them. Without it, subscription use is unknown here and Claude steps prefer the credit lane."),
         "do": "statusline"},
    ]
    return checks
