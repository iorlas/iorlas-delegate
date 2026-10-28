"""Readiness: what each lane needs on this machine (CLI, login, key). `doctor` prints them as a table; `configured_lanes` tells the fleet which
lanes the machine can run. No state is stored: every call derives it from the machine."""
import json
import os
import shutil
import sys
from pathlib import Path

from delegate.adapters import statusline
from delegate.adapters.harness.opencode import OpenCodeHarness
from delegate.app import env


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def check_codex_cli():
    return bool(shutil.which("codex"))


def check_codex_login():
    return (codex_home() / "auth.json").exists()


def check_usage_source():
    return statusline.path().exists()


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
    "always": lambda: True,
}


def lane_configured(lane_cfg):
    return READY_CHECKS[lane_cfg.get("ready", "always")]()


def configured_lanes(cfg):
    return {n: lane_configured(c) for n, c in cfg["lanes"].items()}


def fix_hint(name):
    """What to do about a lane that is not ready: one line, or '' when nothing is missing."""
    if name == "OPENAI_SUBSCRIPTION":
        return "run `codex login --device-auth`" if check_codex_cli() else "install the Codex CLI (`npm install -g @openai/codex`), then `codex login --device-auth`"
    if name == "GROK_SUBSCRIPTION":
        return "macOS: install OpenCode (https://opencode.ai), then `opencode auth login` and pick xAI"
    if name == "DEEPSEEK_FREE":
        return "macOS: install OpenCode, then `route.py config set deepseek_free_enabled true`"
    if name == "ZAI_CODING_SUBSCRIPTION":
        return "macOS: install OpenCode, `opencode auth login` (Z.AI Coding Plan), then `route.py config set zai_coding_enabled true`"
    return ""
