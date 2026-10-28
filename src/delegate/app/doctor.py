"""`route.py doctor`, the one setup command: the report (checks, lanes, notes), `--next` (the first failing step), `--fix`, `--decline`, `--undecline`,
`--ack`; and `config set|show`. The agent never handles a secret: the API key is set by the user (keys.py)."""
import json

from delegate.contracts.routing import split_retired
from delegate.app import env, harnesses, keys, onboard, setup, usage


def doctor(cfg, deep=False, lane=None):
    checks = onboard.build_checks(cfg)
    lanes = onboard.configured_lanes(cfg)
    skip = {n for n, v in env.profile_lanes().items() if v == "declined"}
    ack = env.acked()
    notes = []
    if env.in_container() and "volumes" not in ack:
        notes.append({"id": "volumes", "say": "Container: whether ~/.codex and ~/.claude survive a rebuild is not checked. If your home folder is already a volume, run `route.py config ack volumes` and this goes away."})
    for k in env.retired_keys_present():
        notes.append({"id": "retired_key", "key": k, "file": str(env.config_store().path),
                      "say": f"{env.config_store().path}: '{k}' is retired and ignored; delete it from config.json."})
    with open(env.SKILL_DIR / "routing.json", encoding="utf-8") as f:
        for k in split_retired(json.load(f).get("settings") or {})[1]:
            notes.append({"id": "retired_key", "key": k, "file": str(env.SKILL_DIR / "routing.json"),
                          "say": f"{env.SKILL_DIR / 'routing.json'}: '{k}' is retired and ignored."})
    for c in checks:
        if c["id"] in ack and not c["ok"]:
            c["skip"] = True
            c["acked"] = True
    if deep:
        key = harnesses.find_key(quiet=True)
        if key:
            ok, why = keys.validate_key(key)
            checks.append({"id": "credit_key_valid", "lane": "CLAUDE_CREDIT", "ok": ok, "skip": "CLAUDE_CREDIT" in skip,
                           "who": "user", "say": "The stored API key was rejected: " + why})
    enabled = {n for n, v in env.profile_lanes().items() if v == "enabled"}
    shared = {n for n, c in cfg["lanes"].items() if c.get("shared_with_orchestrator")}
    if lane:
        checks = [c for c in checks if c["lane"] == lane]
    for c in checks:                # a lane nobody chose and the machine cannot run is on offer, not a problem
        if not c["ok"] and not c.get("skip") and c["lane"] not in enabled | shared and c["lane"] != lane:
            c["available"] = True
    blocking = [c for c in checks if not c["ok"] and not c.get("optional") and not c.get("skip") and not c.get("available")]
    return {"v": 1, "ready": not blocking, "lanes": lanes, "declined": sorted(skip), "harness": env.session_host(), "checks": checks, "notes": notes}


def print_doctor(d, quiet):
    bad = [c for c in d["checks"] if not c["ok"] and not c.get("optional") and not c.get("skip") and not c.get("available")]
    if quiet:
        print("READY" if d["ready"] else "SETUP NEEDED: " + ", ".join(c["id"] for c in bad) + " (run `doctor` for what to do about each)")
        return
    for c in d["checks"]:
        mark = "ok     " if c["ok"] else ("acked" if c.get("acked") else "skipped" if c.get("skip") else ("available" if c.get("available") else "optional" if c.get("optional") else "MISSING"))
        print(f"{mark:<9} {c['id']:<14} {c['lane']}")
        if not c["ok"] and not c.get("skip") and not c.get("available"):
            print(f"          -> {c['say']}")
    for n in d["notes"]:
        print(f"note [{n['id']}]: {n['say']}")
    if d["ready"]:
        avail = sorted({c["lane"] for c in d["checks"] if c.get("available")})
        print("READY" + (f". Not set up, your choice: {', '.join(avail)} (`doctor --lane NAME` to set one up, `doctor --decline NAME` to hide it)" if avail else ""))
    else:
        print("SETUP NEEDED. Each MISSING item above says what to do. You choose: do it (`doctor --fix ID`), `doctor --decline LANE` to drop its lane, or `doctor --ack ID` to say it needs nothing.")


# ----------------------------------------------------------------------------- doctor --next: first failing check -> one step

def next_step(d):
    """First failing REQUIRED check as a step; optional ones are listed, not pushed."""
    optional = [c["id"] for c in d["checks"] if not c["ok"] and not c.get("skip") and not c.get("available") and c.get("optional")]
    for c in d["checks"]:
        if not c["ok"] and not c.get("skip") and not c.get("optional") and not c.get("available"):
            step = {"step": c["id"], "lane": c["lane"], "who": c["who"], "say": c["say"]}
            for k in ("run", "url", "do"):
                if c.get(k):
                    step[k] = c[k]
            step["then"] = f"run `doctor --next` again; or `doctor --decline {c['lane']}` to drop this lane; or `doctor --ack {c['id']}` if it needs nothing"
            if optional:
                step["optional_steps_later"] = optional
            return step
    return {"optional_only": optional} if optional else None


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


def cmd_doctor(a, cfg):
    """The one setup command: report, then --fix / --decline / --undecline / --ack / --next act on what it reported."""
    if a.undecline:
        if env.profile_lanes().get(a.undecline) == "declined":
            env.set_profile_lane(a.undecline, None)
        print(json.dumps({"done": True, "say": f"{a.undecline} back in the list."}))
        return 0
    if a.ack:
        env.set_ack(a.ack)
        print(json.dumps({"acknowledged": sorted(env.acked())}))
        return 0
    if a.decline or a.fix:
        out = setup.act(cfg, do=a.fix, decline=a.decline, open_browser=a.open_browser)
        print(json.dumps(out, indent=1))
        return 1 if out.get("error") else 0
    d = doctor(cfg, deep=a.deep, lane=a.lane)
    for c in d.get("checks") or []:
        if isinstance(c, dict) and c.get("id") == "claude_login" and c.get("ok") is True:       # only a positive answer: doctor cannot tell "logged out" from "no CLI" or "unreadable"
            usage.auth_cache().remember("CLAUDE_SUBSCRIPTION", c.get("ok"), "doctor", env.now())
    if a.next:
        step = next_step(d)
        print(json.dumps(step if step and "optional_only" not in step else {"step": None, "say": "READY", "lanes": d["lanes"]}, indent=1))
        return 0
    if a.json:
        print(json.dumps(d, indent=1))
    else:
        print_doctor(d, a.quiet)
    return 0 if d["ready"] else 1


def cmd_config(a, cfg):
    if a.action == "set":
        if not a.key or a.value is None:
            raise SystemExit("route.py: config set KEY VALUE")
        print(json.dumps(config_set(a.key, a.value), indent=1))
    else:
        print(json.dumps(config_show(cfg), indent=1))
    return 0


def cmd_set_key(a, cfg):
    if a.web:
        return keys.set_key_web(host=a.host, port=a.port, timeout=a.timeout, force=a.force)
    return keys.set_key(from_stdin=a.stdin, force=a.force)
