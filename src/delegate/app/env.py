"""Where this process lives: the clock, the plugin tree, the config dir, and the stores under it (each file behind its store).

The functions are the seam tests patch; `load_routing` is the one place routing.json and the owner's config.json meet.
"""
import contextlib
import copy
import json
import os
import sys
import time
from pathlib import Path

from child_env import scrub_current

from delegate.contracts import codec
from delegate.contracts.routing import Routing, split_retired
from delegate.contracts.stores import ConfigStore, HistoryStore, StateStore

SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "delegate"        # routing.json, contract.json, SKILL.md: next to src/ in the plugin tree


def now() -> float:
    v = os.environ.get("DELEGATE_NOW")
    return float(v) if v else time.time()


def plugin_data_dir(here: Path | None = None) -> Path | None:
    """Claude Code's persistent data dir for this plugin: <plugins>/data/<plugin>-<marketplace>, derived from where this copy is installed
    (<plugins>/cache/<marketplace>/<plugin>/<version>/...). None when running from a checkout."""
    parts = (here or SKILL_DIR).parts
    for i in range(1, len(parts) - 3):
        if parts[i] == "cache" and parts[i - 1] == "plugins":
            ident = "".join(c if c.isascii() and (c.isalnum() or c in "_-") else "-" for c in f"{parts[i + 2]}@{parts[i + 1]}")
            return Path(*parts[:i]) / "data" / ident
    return None


def config_dir() -> Path:
    """Where settings, profile and state live: DELEGATE_CONFIG_DIR, else Claude Code's data dir for the installed plugin.
    CLAUDE_PLUGIN_DATA is deliberately not read (it names whichever plugin launched the process). A checkout has no default: two places would split the state."""
    explicit = os.environ.get("DELEGATE_CONFIG_DIR")
    p = Path(explicit) if explicit else plugin_data_dir()
    if p is None:
        sys.exit("route.py: not running from an installed plugin; set DELEGATE_CONFIG_DIR to the folder for settings and state")
    p.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(p, 0o700)        # holds state and history: private to the user
    return p


def deep_merge(a: dict, b: dict | None) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_RETIRED_WARNED: set[str] = set()


def load_routing() -> dict:
    """routing.json with the owner's overrides (config.json, minus its `profile`) merged into `settings`, validated into contracts.routing.Routing.
    Retired settings keys are dropped with one stderr line each (stdout carries the answer); any other unknown key fails."""
    with open(SKILL_DIR / "routing.json", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["settings"], _ = split_retired(deep_merge(cfg["settings"], config_store().overrides()))
    for k in retired_keys_present():
        if k not in _RETIRED_WARNED:
            _RETIRED_WARNED.add(k)
            print(f"route.py: {config_store().path}: '{k}' is retired and ignored; it is safe to delete from config.json (nothing rewrites the file)", file=sys.stderr)
    typed_routing(cfg)
    return cfg


def retired_keys_present() -> list:
    """The retired settings keys config.json (the owner's file) still carries."""
    return split_retired(config_store().overrides())[1]


def typed_routing(cfg: dict) -> Routing:
    try:
        return codec.from_json(Routing, cfg)
    except codec.CodecError as e:
        sys.exit(f"route.py: routing.json / config.json is not a valid routing table: {e}. "
                 f"Fix: delete the key from {config_store().path} (or rename it to a known settings key; see `route.py config show`)")


def settings_of(cfg: dict):
    return typed_routing(cfg).settings


def states() -> StateStore:
    return StateStore(config_dir() / "state.json")


def config_store() -> ConfigStore:
    return ConfigStore(config_dir() / "config.json")


def history_path() -> Path:
    return config_dir() / "history.jsonl"


def history_store() -> HistoryStore:
    return HistoryStore(history_path())


def snapshot_path() -> Path:
    return config_dir() / "status-snapshot.json"


def net_cache_path() -> Path:
    return config_dir() / "net.json"


def load_state():
    return states().load()


def update_state(fn):
    return states().update(fn)


def update_config(fn):
    return config_store().update(fn)


def profile_lanes() -> dict:
    return dict(config_store().profile().lanes)


def set_profile_lane(name, value) -> None:
    config_store().set_lane(name, value)




def history_add(row) -> None:
    history_store().add(row)


def in_container() -> bool:
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def session_host() -> str:
    """desktop | container | cli | headless | unknown, from the environment. The model never decides this."""
    ep = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    if "desktop" in ep:
        return "desktop"
    if in_container():
        return "container"
    if ep == "cli":
        return "cli"
    return "unknown" if ep else "headless"


# The child's environment is child_env.scrub (what a Claude session exports and credential-looking variables are removed); CLAUDE_CONFIG_DIR is kept
# so a worker on a custom Claude login finds it.
KEEP = ("CLAUDE_CONFIG_DIR",)


def child_env(extra: dict | None = None) -> dict:
    """The worker gets the caller's environment minus what a Claude session exports, delegate's and credential-looking variables, plus DELEGATE_DEPTH=1."""
    return scrub_current(keep=KEEP) | {"DELEGATE_DEPTH": "1"} | (extra or {})
