"""The verbs of route.py that are not a module of their own: holds, report, capture-limits, guard, contract, next, run. Each takes the parsed
arguments and the routing table and returns the exit code."""
import contextlib
import json
import sys
import uuid

from delegate.adapters import statusline
from delegate.app import cockpit, decide, env, launch, run
from delegate.contracts import codec, stops
from delegate.contracts import history as hist
from delegate.domain.holds.book import HoldBook, new_hold
from delegate.domain.policy import worker as policy
from delegate.domain.time.spec import local, to_epoch


def cmd_hold(a, cfg):
    try:
        if a.lane not in cfg["lanes"]:
            raise ValueError(f"unknown lane {a.lane}; lanes: {', '.join(cfg['lanes'])}")
        rec = new_hold(uuid.uuid4().hex[:8], a.lane, a.until, a.reason, [x for x in (a.roles or "").split(",") if x], a.set_by, env.now())
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    env.update_state(lambda s: s.holds.__setitem__(rec.id, rec))
    print(json.dumps({"ok": True, "hold": codec.to_json(rec)}))
    return 0


def cmd_release(a, cfg):
    gone = []
    env.update_state(lambda s: gone.extend(HoldBook(s.holds).release(a.target)))
    print(json.dumps({"ok": bool(gone), "released": [codec.to_json(h) for h in gone]}))
    return 0 if gone else 1


def cmd_holds(a, cfg):
    print(json.dumps({"holds": HoldBook(env.load_state().holds).rows(env.now())}))
    return 0


def cmd_report(a, cfg):
    t = env.now()
    if a.lane not in cfg["lanes"]:
        sys.exit(f"route.py: unknown lane {a.lane}; lanes: {', '.join(cfg['lanes'])}")
    if a.event == "ok":
        env.update_state(lambda s: s.out.pop(a.lane, None))
        env.history_add(hist.ReleaseRow(ts=env.now(), lane=a.lane))      # the fold forgets the lane's limits
        print(f"{a.lane}: marked available")
        return 0
    until = to_epoch(a.until)
    if until is None:
        st = env.load_state()
        lane = decide.fleet_builder(cfg).build(st, t, check_config=False).lanes[a.lane]     # a lane the profile leaves out can still be reported out
        until = lane.out_until or t + 3600
    launch.settler().mark_out(a.lane, until, "reported out", t, "limit", "manual" if a.until else "guess")
    print(f"{a.lane}: out until {local(until)}")
    return 0


def cmd_capture(a, cfg):
    try:
        d = json.loads(sys.stdin.read())
    except ValueError:
        return 0
    rl = d.get("rate_limits") if isinstance(d, dict) else None
    if not rl:
        return 0
    if getattr(a, "print", False):
        f5, f7 = (rl.get("five_hour") or {}).get("used_percentage"), (rl.get("seven_day") or {}).get("used_percentage")
        print("5h %s%% wk %s%%" % ("?" if f5 is None else round(f5), "?" if f7 is None else round(f7)))
    statusline.write(rl, env.now())
    return 0


def cmd_guard(a, cfg=None):
    """Claude PreToolUse hook: exit 2 (block, reason on stderr) when a tool call names a secret path, changes git, or, with --read-only, does
    more than read. The verdict is domain/policy's; this is the hook's stdin and exit code."""
    with contextlib.suppress(ValueError, AttributeError):
        j = json.load(sys.stdin)
        v = policy.judge(j.get("tool_name"), j.get("tool_input") or {}, bool(getattr(a, "read_only", False)))
        if v.blocked:
            print(v.reason, file=sys.stderr)
            return 2
    return 0


def cmd_contract(a, cfg):
    """The handshake: the contract number, every reason with its facts, and the flags `run` accepts."""
    print(json.dumps({"contract": stops.CONTRACT["contract"], "reasons": stops.CONTRACT["reasons"], "run_args": stops.CONTRACT["run_args"]}))
    return 0


def cmd_next(a, cfg):
    d, lanes, warnings, t = decide.decision(a, cfg)
    d["needs_input"] = cockpit.needs_input(lanes)
    if warnings:
        d["warnings"] = warnings
    if not a.verbose:       # the hot path stays small: reasoning and the lane table only on request
        d.pop("trace", None)
    else:
        d["lanes"] = {n: ln.brief(t) for n, ln in lanes.items()}
    if d.get("warning"):
        print("warning: " + d["warning"], file=sys.stderr)
    print(json.dumps(d, indent=1))
    return 0 if d["action"] != "error" else 1


def cmd_run(a, cfg):
    return run.Run(a, cfg, unattended=getattr(a, "unattended", False), idle_s=getattr(a, "idle", None)).go()
