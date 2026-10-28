"""The runners route.py can launch, as data: the runner kind `route.how()` names, the CLI it drives (the key of app/watch.LIMITS), the executable to
look for, and whether resume was tested. What a CLI does (argv, events, usage) lives in its harness class, adapters/harness/.

Lane facts that are data, not code, live here too (`LANE_HOSTS`: the API hosts a lane needs, for adapters/netprobe).
"""
from collections import namedtuple

Adapter = namedtuple("Adapter", "kind cli exe resume")
# resume: "tested" = spike P1 (62-spikes-resume.md) resumed a killed session on this CLI; "untested" = opencode, blocked by ENOTFOUND opencode.ai last time
REGISTRY = {"codex": Adapter("codex", "codex", "codex", "tested"), "opencode": Adapter("opencode", "opencode", "opencode", "untested"),
            "claude_p": Adapter("claude_p", "claude", "claude", "tested")}

LANE_HOSTS = {"CLAUDE_SUBSCRIPTION": ("api.anthropic.com",), "CLAUDE_CREDIT": ("api.anthropic.com",), "OPENAI_SUBSCRIPTION": ("chatgpt.com",),
              "GROK_SUBSCRIPTION": ("api.x.ai",), "DEEPSEEK_FREE": ("opencode.ai",), "ZAI_CODING_SUBSCRIPTION": ("api.z.ai",)}
FAMILY_HOSTS = {"claude": ("api.anthropic.com",), "openai": ("chatgpt.com",), "xai": ("api.x.ai",), "deepseek": ("opencode.ai",), "zai": ("api.z.ai",)}


def hosts_of(lane):
    return LANE_HOSTS.get(lane, ())


def hosts_for_family(family):
    return FAMILY_HOSTS.get(family, ())


def all_hosts():
    return tuple(dict.fromkeys(h for hs in LANE_HOSTS.values() for h in hs))
