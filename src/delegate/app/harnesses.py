"""Which harness serves which lane: the OpenCode profiles per lane, the login probe per lane, the transcript finder per family, and the credit key."""
import contextlib
import os
import sys

from delegate.adapters.harness.claude import ClaudeHarness
from delegate.adapters.harness.codex import CodexHarness
from delegate.adapters.harness.opencode import OpenCodeHarness, Profile
from delegate.app import env

AUTH_TIMEOUT = 10
OC_PROFILES = {"GROK_SUBSCRIPTION": Profile(auth_key="xai"),
               "ZAI_CODING_SUBSCRIPTION": Profile(providers=("zai-coding-plan",), price="unknown", auth_key="zai-coding-plan", need_key=True),
               "DEEPSEEK_FREE": Profile(providers=("opencode",), own_data=True, price="free")}
ZAI_MODELS = ("zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash")


def find_key(quiet=False):
    """The credit lane's API key: the environment, else <config dir>/claude-credit.key (warned about when others can read it)."""
    k = os.environ.get("CLAUDE_CREDIT_WORKER_KEY", "").strip()
    if k:
        return k
    p = env.config_dir() / "claude-credit.key"
    with contextlib.suppress(OSError):
        k = p.read_text(encoding="utf-8").strip()
        if k:
            if not quiet and (p.stat().st_mode & 0o077):
                print(f"route.py: warning: {p} is readable by others; run: chmod 600 {p}", file=sys.stderr)
            return k
    return ""


def probe_login(name):
    """-> (True | False | None, why): is the lane's login really there? A status command or a file, never a model call."""
    if name == "OPENAI_SUBSCRIPTION":
        return CodexHarness().login_probe(AUTH_TIMEOUT)
    if name == "CLAUDE_SUBSCRIPTION":
        return ClaudeHarness().login_probe(AUTH_TIMEOUT)
    if name == "CLAUDE_CREDIT":
        return (True, "API key stored") if find_key(quiet=True) else (False, "no API key stored")
    if name in OC_PROFILES:
        return OpenCodeHarness(OC_PROFILES[name]).login_probe(AUTH_TIMEOUT)
    return None, "no check for this lane"


def find_transcript(ref, family):
    hx = {"openai": CodexHarness(), "claude": ClaudeHarness()}.get(family) or OpenCodeHarness(root=env.config_dir())        # opencode: exported after the run
    return hx.transcript(ref)


