"""`route.py doctor` (and `init`): a plain lane table (lane, enabled, ready, fix) and the notes about retired config keys; and `config set|show`.
Nothing here installs, logs in or decides for the owner: the fix column says what to run."""
import json
import os
from typing import Any

from delegate.app import env, onboard
from delegate.contracts import codec
from delegate.contracts.routing import split_retired
from delegate.contracts.state import Profile


def lane_rows(cfg: dict):
    """One row per lane: {lane, enabled, ready, fix}. enabled = the router may use it (not declined; an opt-in lane only when the profile enables it); ready = the machine can run it."""
    ready, chosen = onboard.configured_lanes(cfg), env.profile_lanes()
    rows: list[dict[str, Any]] = []
    for name in cfg["lanes"]:
        enabled = chosen.get(name) != "declined" and (not cfg["lanes"][name].get("opt_in") or chosen.get(name) == "enabled")
        rows.append({"lane": name, "enabled": enabled, "ready": ready[name], "fix": "" if ready[name] and enabled else onboard.fix_hint(name)})
    return rows


LEFTOVER_FILES = ("claude-credit.key", "ledger.jsonl")       # of the deleted CLAUDE_CREDIT lane
LEFTOVER_ENV = "CLAUDE_CREDIT_WORKER_KEY"


def leftovers():
    """Notes for what the deleted CLAUDE_CREDIT lane left behind. Nothing reads or deletes them."""
    out: list[dict[str, str]] = [{"id": "unused_file", "file": str(env.config_dir() / n), "say": f"{env.config_dir() / n}: unused, safe to delete."}
           for n in LEFTOVER_FILES if (env.config_dir() / n).exists()]
    if os.environ.get(LEFTOVER_ENV):
        out.append({"id": "unused_env", "key": LEFTOVER_ENV, "say": f"env {LEFTOVER_ENV}: unused, safe to unset."})
    return out


def notes(cfg: dict):
    out: list[dict[str, str]] = [{"id": "retired_key", "key": k, "file": str(env.config_store().path), "say": f"{env.config_store().path}: '{k}' is retired and ignored; safe to delete from config.json."}
           for k in env.retired_keys_present()]
    with open(env.SKILL_DIR / "routing.json", encoding="utf-8") as f:
        out += [{"id": "retired_key", "key": k, "file": str(env.SKILL_DIR / "routing.json"), "say": f"{env.SKILL_DIR / 'routing.json'}: '{k}' is retired and ignored."}
                for k in split_retired(json.load(f).get("settings") or {})[1]]
    return out + leftovers() + smoke_problems(cfg)


PROFILE_VALUES = ("enabled", "declined")
SMOKE_IDS = ("bad_profile", "enabled_not_ready")


def smoke_problems(cfg: dict):
    """The post-update smoke check: the profile parses and names known lanes with known values, and every lane the owner enabled can run here
    (its CLI resolves, its login is there). Each problem is a note (ids in SMOKE_IDS) with the exact fix; `doctor` exits 1 on any."""
    out: list[dict[str, str]] = []
    raw = env.config_store().read().get("profile") or {}
    try:
        codec.from_json(Profile, raw)
        lanes = raw.get("lanes") or {}
    except codec.CodecError as e:
        return [{"id": "bad_profile", "say": f"{env.config_store().path}: profile does not parse ({e}). Fix: correct or delete the \"profile\" key."}]
    for name, v in lanes.items():
        if name not in cfg["lanes"] or v not in PROFILE_VALUES:
            out.append({"id": "bad_profile", "lane": name, "say": f"profile.lanes.{name} = {v!r}: expected a lane of {sorted(cfg['lanes'])} "
                        f"set to enabled or declined. Fix: `route.py config set profile.lanes.<LANE> enabled|declined`."})
    ready = onboard.configured_lanes(cfg)
    out += [{"id": "enabled_not_ready", "lane": n, "say": f"{n} is enabled but not ready: {onboard.fix_hint(n)}"}
            for n, v in lanes.items() if v == "enabled" and n in ready and not ready[n]]
    return out


def doctor(cfg: dict):
    return {"v": 1, "harness": env.session_host(), "lanes": lane_rows(cfg), "notes": notes(cfg)}


def print_doctor(d: dict):
    w = max(len(r["lane"]) for r in d["lanes"])
    print(f"{'lane':<{w}}  enabled  ready  fix")
    for r in d["lanes"]:
        print(f"{r['lane']:<{w}}  {'yes' if r['enabled'] else 'no':<7}  {'yes' if r['ready'] else 'no':<5}  {r['fix']}".rstrip())
    for n in d["notes"]:
        print(f"note [{n['id']}]: {n['say']}")


def cmd_doctor(a: env.Args, cfg: dict):
    d = doctor(cfg)
    if a.json:
        print(json.dumps(d, indent=1))
    else:
        print_doctor(d)
    return 1 if any(n["id"] in SMOKE_IDS for n in d["notes"]) else 0


def config_set(key: str, value: str):
    try:
        val = json.loads(value)
    except ValueError:
        val = value

    parts = key.split(".")
    if parts[:2] == ["profile", "lanes"] and (len(parts) != 3 or val not in PROFILE_VALUES):
        raise SystemExit(f"route.py: config set profile.lanes.<LANE> enabled|declined (got {key} {value})")

    def put(cur: dict):
        node = cur
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = val
    return env.update_config(put)


def config_show(cfg: dict):
    p = env.config_dir() / "config.json"
    return {"file": str(p), "overrides": json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}, "effective_settings": cfg["settings"]}


def cmd_config(a: env.Args, cfg: dict):
    if a.action == "set":
        if not a.key or a.value is None:
            raise SystemExit("route.py: config set KEY VALUE")
        print(json.dumps(config_set(a.key, a.value), indent=1))
    else:
        print(json.dumps(config_show(cfg), indent=1))
    return 0
