"""`route.py doctor` (and `init`): a plain lane table (lane, enabled, ready, fix) and the notes about retired config keys; and `config set|show`.
Nothing here installs, logs in or decides for the owner: the fix column says what to run."""
import json
import os

from delegate.app import env, onboard
from delegate.contracts.routing import split_retired

OPT_IN = {"DEEPSEEK_FREE": "deepseek_free_enabled", "ZAI_CODING_SUBSCRIPTION": "zai_coding_enabled"}


def lane_rows(cfg):
    """One row per lane: {lane, enabled, ready, fix}. enabled = the router may use it (not declined, its opt-in set); ready = the machine can run it."""
    ready, declined = onboard.configured_lanes(cfg), env.profile_lanes()
    rows = []
    for name in cfg["lanes"]:
        flag = OPT_IN.get(name)
        enabled = declined.get(name) != "declined" and (flag is None or cfg["settings"].get(flag) is True)
        rows.append({"lane": name, "enabled": enabled, "ready": ready[name], "fix": "" if ready[name] and enabled else onboard.fix_hint(name)})
    return rows


LEFTOVER_FILES = ("claude-credit.key", "ledger.jsonl")       # of the deleted CLAUDE_CREDIT lane
LEFTOVER_ENV = "CLAUDE_CREDIT_WORKER_KEY"


def leftovers():
    """Notes for what the deleted CLAUDE_CREDIT lane left behind. Nothing reads or deletes them."""
    out = [{"id": "unused_file", "file": str(env.config_dir() / n), "say": f"{env.config_dir() / n}: unused, safe to delete."}
           for n in LEFTOVER_FILES if (env.config_dir() / n).exists()]
    if os.environ.get(LEFTOVER_ENV):
        out.append({"id": "unused_env", "key": LEFTOVER_ENV, "say": f"env {LEFTOVER_ENV}: unused, safe to unset."})
    return out


def notes(cfg):
    out = [{"id": "retired_key", "key": k, "file": str(env.config_store().path), "say": f"{env.config_store().path}: '{k}' is retired and ignored; safe to delete from config.json."}
           for k in env.retired_keys_present()]
    with open(env.SKILL_DIR / "routing.json", encoding="utf-8") as f:
        out += [{"id": "retired_key", "key": k, "file": str(env.SKILL_DIR / "routing.json"), "say": f"{env.SKILL_DIR / 'routing.json'}: '{k}' is retired and ignored."}
                for k in split_retired(json.load(f).get("settings") or {})[1]]
    return out + leftovers()


def doctor(cfg):
    return {"v": 1, "harness": env.session_host(), "lanes": lane_rows(cfg), "notes": notes(cfg)}


def print_doctor(d):
    w = max(len(r["lane"]) for r in d["lanes"])
    print(f"{'lane':<{w}}  enabled  ready  fix")
    for r in d["lanes"]:
        print(f"{r['lane']:<{w}}  {'yes' if r['enabled'] else 'no':<7}  {'yes' if r['ready'] else 'no':<5}  {r['fix']}".rstrip())
    for n in d["notes"]:
        print(f"note [{n['id']}]: {n['say']}")


def cmd_doctor(a, cfg):
    d = doctor(cfg)
    if a.json:
        print(json.dumps(d, indent=1))
    else:
        print_doctor(d)
    return 0


def config_set(key, value):
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
    return env.update_config(put)


def config_show(cfg):
    p = env.config_dir() / "config.json"
    return {"file": str(p), "overrides": json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}, "effective_settings": cfg["settings"]}


def cmd_config(a, cfg):
    if a.action == "set":
        if not a.key or a.value is None:
            raise SystemExit("route.py: config set KEY VALUE")
        print(json.dumps(config_set(a.key, a.value), indent=1))
    else:
        print(json.dumps(config_show(cfg), indent=1))
    return 0
