"""Lane adapter seam (delegate-dlz, interface only): one registry of the runners route.py can launch, and the data a lane profile adds to them.

An `Adapter` is what the router needs to know about one vendor CLI: the runner kind `route.how()` names, the CLI it drives (the key of
lanes/supervisor.LIMITS), the executable to look for, and `launch`, the function that runs one worker and returns the answer fields. route.py
registers its three launchers at import; `launchers()` is the dict `route.LAUNCHERS` is built from, so adding a runner is one `register` call. The
build / events / render_policy / ready split of the bead is NOT done here: the launchers still own their command lines and event parsing.

Lane facts that are data, not code, live here too (`LANE_HOSTS`: the API hosts a lane needs, for adapters/netprobe).
"""
from collections import namedtuple

Adapter = namedtuple("Adapter", "kind cli exe launch resume")
# resume: "tested" = spike P1 (62-spikes-resume.md) resumed a killed session on this CLI; "untested" = opencode, blocked by ENOTFOUND opencode.ai last time
REGISTRY = {}

LANE_HOSTS = {"CLAUDE_SUBSCRIPTION": ("api.anthropic.com",), "CLAUDE_CREDIT": ("api.anthropic.com",), "OPENAI_SUBSCRIPTION": ("chatgpt.com",),
              "GROK_SUBSCRIPTION": ("api.x.ai",), "DEEPSEEK_FREE": ("opencode.ai",), "ZAI_CODING_SUBSCRIPTION": ("api.z.ai",)}
FAMILY_HOSTS = {"claude": ("api.anthropic.com",), "openai": ("chatgpt.com",), "xai": ("api.x.ai",), "deepseek": ("opencode.ai",), "zai": ("api.z.ai",)}


def register(kind, cli, exe, launch, resume="tested"):
    """Add (or replace) the adapter of runner kind `kind`. -> the Adapter."""
    REGISTRY[kind] = Adapter(kind, cli, exe, launch, resume)
    return REGISTRY[kind]


def launchers():
    """-> {kind: launch}, the table route.run_once dispatches on. An unknown kind is absent (a setup error, not a silent default)."""
    return {k: a.launch for k, a in REGISTRY.items()}


def hosts_of(lane):
    return LANE_HOSTS.get(lane, ())


def hosts_for_family(family):
    return FAMILY_HOSTS.get(family, ())


def all_hosts():
    return tuple(dict.fromkeys(h for hs in LANE_HOSTS.values() for h in hs))
