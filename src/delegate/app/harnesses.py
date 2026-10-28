"""Which harness serves which lane: the OpenCode profiles per lane, the login probe per lane, the transcript finder per family."""
from delegate.adapters.harness.claude import ClaudeHarness
from delegate.adapters.harness.codex import CodexHarness
from delegate.adapters.harness.opencode import OpenCodeHarness, Profile
from delegate.app import env

AUTH_TIMEOUT = 10
OC_PROFILES = {"GROK_SUBSCRIPTION": Profile(auth_key="xai"),
               "ZAI_CODING_SUBSCRIPTION": Profile(providers=("zai-coding-plan",), price="unknown", auth_key="zai-coding-plan", need_key=True),
               "DEEPSEEK_FREE": Profile(providers=("opencode",), own_data=True, price="free")}
ZAI_MODELS = ("zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash")


def probe_login(name: str):
    """-> (True | False | None, why): is the lane's login really there? A status command or a file, never a model call."""
    if name == "OPENAI_SUBSCRIPTION":
        return CodexHarness().login_probe(AUTH_TIMEOUT)
    if name == "CLAUDE_SUBSCRIPTION":
        return ClaudeHarness().login_probe(AUTH_TIMEOUT)
    if name in OC_PROFILES:
        return OpenCodeHarness(OC_PROFILES[name]).login_probe(AUTH_TIMEOUT)
    return None, "no check for this lane"


def find_transcript(ref: str | None, family: str):
    hx = {"openai": CodexHarness(), "claude": ClaudeHarness()}.get(family) or OpenCodeHarness(root=env.config_dir())        # opencode: exported after the run
    return hx.transcript(ref)


