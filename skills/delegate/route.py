#!/usr/bin/env python3
"""delegate router: deterministic lane and step selection for an LLM orchestrator.

Python 3.8+, standard library only. Windows, macOS, Linux, Docker.
The model supplies only what the script cannot read itself; the script decides.

  route.py status [--claude-usage JSON|@file|-] [--json]
  route.py next ROLE [--writer openai|claude] [--failed 0,1] [--fable] [--allow-degraded]
                     [--claude-usage JSON|@file|-] [--text]
  route.py report --lane LANE --event out|ok [--until EPOCH|ISO] [--message TEXT]
  route.py run-claude --model ID --effort LEVEL --cap USD [--write] [--allow TOOL]... [--cwd DIR] [--bare] [PROMPT|-]
  route.py capture-limits            (status line JSON on stdin)

Data: routing.json next to this file (lanes, models, roles, thresholds).
Per-user settings: <config dir>/config.json (same keys as "settings"); state, ledger and key live in <config dir>:
  Windows %APPDATA%\\delegate, otherwise $XDG_CONFIG_HOME/delegate or ~/.config/delegate.

Exit codes of run-claude: 0 ok | 75 credit exhausted | 76 rate limited | 77 own cap reached | 2 key missing | 1 other.
"""
import argparse
import contextlib
import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEEK_MIN = 7 * 24 * 60


# ----------------------------------------------------------------------------- time, paths, config

def now():
    v = os.environ.get("DELEGATE_NOW")
    return float(v) if v else time.time()


def config_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    p = Path(base) / "delegate"
    p.mkdir(parents=True, exist_ok=True)
    return p


def deep_merge(a, b):
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_routing():
    with open(HERE / "routing.json", encoding="utf-8") as f:
        cfg = json.load(f)
    user = config_dir() / "config.json"
    if user.exists():
        with open(user, encoding="utf-8") as f:
            cfg["settings"] = deep_merge(cfg["settings"], json.load(f))
    return cfg


def fmt_dur(secs):
    s = int(secs)
    if s <= 0:
        return "now"
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return f"{d}d{h}h" if d else (f"{h}h{m:02d}m" if h else f"{m}m")


def local(epoch):
    return dt.datetime.fromtimestamp(epoch).strftime("%a %d %b %H:%M")


def to_epoch(v):
    """epoch seconds, epoch ms, or ISO string -> epoch seconds; None if unparsable."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def next_weekly(spec, t):
    """'Sun 17:00' (local time) -> epoch of the next such moment, or None."""
    try:
        day, hm = spec.split()
        hh, mm = (int(x) for x in hm.split(":"))
        target = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(day[:3].lower())
    except (ValueError, AttributeError):
        return None
    base = dt.datetime.fromtimestamp(t)
    cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0) + dt.timedelta(days=(target - base.weekday()) % 7)
    if cand.timestamp() <= t:
        cand += dt.timedelta(days=7)
    return cand.timestamp()


def credit_cycle(day, t):
    """(start_epoch, next_reset_epoch) of the credit cycle containing t; reset on `day` of the month, local midnight."""
    day = max(1, min(int(day or 1), 28))
    base = dt.datetime.fromtimestamp(t)
    this = base.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if this.timestamp() > t:
        prev = (this.replace(day=1) - dt.timedelta(days=1)).replace(day=day)
        return prev.timestamp(), this.timestamp()
    nxt = ((this.replace(day=1) + dt.timedelta(days=32)).replace(day=1)).replace(day=day)
    return this.timestamp(), nxt.timestamp()


# ----------------------------------------------------------------------------- state (atomic, locked)

@contextlib.contextmanager
def locked(path, timeout=5.0):
    lock = str(path) + ".lock"
    end = time.time() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 30:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.time() > end:
                break
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.remove(lock)


def state_path():
    return config_dir() / "state.json"


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    p = state_path()
    tmp = p.with_suffix(f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, p)


def update_state(fn):
    with locked(state_path()):
        st = load_state()
        fn(st)
        save_state(st)
        return st


def ledger_path():
    return config_dir() / "ledger.jsonl"


def ledger_spend(since_epoch):
    total = 0.0
    try:
        with open(ledger_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if float(e["ts"]) >= since_epoch:
                        total += float(e["cost"])
                except (ValueError, KeyError):
                    continue
    except OSError:
        pass
    return total


def ledger_add(cost, model, now_t):
    with locked(ledger_path()):
        with open(ledger_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_t, "model": model, "cost": cost}) + "\n")


# ----------------------------------------------------------------------------- lane state

class Lane:
    """What the router knows about one lane right now. Plain data, so tests can build it directly."""

    def __init__(self, name, family, kind, shared=False):
        self.name, self.family, self.kind, self.shared = name, family, kind, shared
        self.out = False
        self.out_until = None
        self.five = None          # percent of the 5-hour window used, None = unknown
        self.week = None          # percent of the weekly window used
        self.week_reset = None    # epoch
        self.five_reset = None
        self.pace = None          # weekly pace in points: elapsed share minus used share
        self.burn = False         # weekly window about to reset with allowance left
        self.reset_in_days = None  # credit lanes
        self.spend = 0.0
        self.cap = None
        self.notes = []

    @property
    def usage_known(self):
        return self.five is not None or self.week is not None

    def brief(self, t):
        bits = []
        if self.five is not None:
            bits.append(f"5h {self.five:.0f}%" + (f" (resets {fmt_dur(self.five_reset - t)})" if self.five_reset and self.five_reset > t else ""))
        if self.week is not None:
            bits.append(f"week {self.week:.0f}%" + (f" (resets {fmt_dur(self.week_reset - t)}, {local(self.week_reset)})" if self.week_reset and self.week_reset > t else "") + (f" pace {self.pace:+.0f}" if self.pace is not None else ""))
        if self.kind == "credit":
            bits.append(f"cycle ${self.spend:.2f} of ${self.cap:.0f} cap by this tool" + (f", resets in {self.reset_in_days:.1f}d" if self.reset_in_days is not None else ""))
        if self.out:
            bits.append("OUT" + (f" until {local(self.out_until)}" if self.out_until else ""))
        elif self.burn:
            bits.append("BURN (about to be lost: spend it)")
        if not self.usage_known and self.kind == "subscription":
            bits.append("usage unknown")
        return "; ".join(bits + self.notes)


def window(used, resets_at, span_min, t):
    """-> (used_percent, minutes_to_reset, pace). A window whose reset passed counts as 0% used."""
    if used is None:
        return None, None, None
    if resets_at is not None and resets_at <= t:
        return 0.0, 0.0, None
    if resets_at is None:
        return float(used), None, None
    mins = (resets_at - t) / 60.0
    elapsed = max(0.0, min(100.0, 100.0 * (1 - mins / span_min)))
    return float(used), mins, elapsed - float(used)


def apply_burn(lane, S, week_mins):
    b = S["burn"]
    lane.burn = (lane.week is not None and week_mins is not None and 0 < week_mins <= b["minutes"]
                 and 100 - lane.week >= b["min_left_percent"] and (lane.five is None or lane.five < 60))


# ----------------------------------------------------------------------------- readers (the script gathers what it can)

def read_codex(lane, S, st, t):
    exe = shutil.which("codex")
    if not exe:
        lane.notes.append("codex CLI not installed")
        return
    try:
        p = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    except OSError as e:
        lane.notes.append(f"codex app-server failed to start: {e}")
        return

    def send(m):
        p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()

    def read(i, seconds):
        end = time.time() + seconds
        while time.time() < end:
            line = p.stdout.readline()
            if not line:
                return None
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == i:
                return m
        return None

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "delegate-route", "title": "delegate route", "version": "1"}}})
        if read(1, 15) is None:
            lane.notes.append("codex app-server did not answer")
            return
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        r = read(2, 20)
    finally:
        p.kill()
        p.wait()
        for f in (p.stdin, p.stdout):
            with contextlib.suppress(OSError):
                f.close()
    if not r or "result" not in r:
        lane.notes.append((r or {}).get("error", {}).get("message", "codex gave no limits (logged in? run `codex login`)"))
        return
    res = r["result"]
    rl = res.get("rateLimits") or {}
    pr, se = rl.get("primary") or {}, rl.get("secondary") or {}
    lane.five_reset, lane.week_reset = pr.get("resetsAt"), se.get("resetsAt")
    lane.five, _, _ = window(pr.get("usedPercent"), lane.five_reset, pr.get("windowDurationMins") or 300, t)
    lane.week, wmins, lane.pace = window(se.get("usedPercent"), lane.week_reset, se.get("windowDurationMins") or WEEK_MIN, t)
    apply_burn(lane, S, wmins)
    if rl.get("rateLimitReachedType") or res.get("ordinaryUsageAllowed") is False or (lane.five or 0) >= 98 or (lane.week or 0) >= 98:
        lane.out = True
        resets = [x for x, u in ((lane.five_reset, lane.five), (lane.week_reset, lane.week)) if x and u is not None and u >= 95]
        lane.out_until = max(resets) if resets else t + 3600


def normalize_usage(raw, t):
    """Accept the Claude subscription's windows in the shapes tools produce -> {five_hour, weekly, fable} each {used, resets_at}."""
    if not isinstance(raw, dict):
        return None

    def pick(d, names):
        for n in names:
            if n in d:
                return d[n]
        return None

    def one(w):
        if not isinstance(w, dict):
            return None
        used = pick(w, ["used", "used_percent", "used_percentage", "percent_used", "utilization", "percent", "usedPercent"])
        reset = to_epoch(pick(w, ["resets_at", "resetsAt", "reset_at", "reset", "resets"]))
        return {"used": float(used), "resets_at": reset} if used is not None else None

    out = {"five_hour": one(pick(raw, ["five_hour", "fiveHour", "session", "5h"])),
           "weekly": one(pick(raw, ["weekly", "seven_day", "sevenDay", "week", "weekly_all", "all_models"])),
           "fable": one(pick(raw, ["fable", "weekly_fable", "fable_weekly"]))}
    return out if any(out.values()) else None


def read_claude_subscription(lane, S, st, t, ai_usage=None):
    stale = S["usage_stale_minutes"] * 60
    usage = None
    if ai_usage:
        usage = normalize_usage(ai_usage, t)
        if usage:
            update_state(lambda s: s.__setitem__("claude_usage", {"at": t, "data": usage}))
    if usage is None:
        saved = st.get("claude_usage")
        if saved and t - saved.get("at", 0) <= stale:
            usage = saved["data"]
            lane.notes.append(f"usage from {fmt_dur(t - saved['at'])} ago")
    if usage is None:
        f = Path.home() / ".claude" / "state" / "max-limits.json"
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            if t - float(d.get("updated", 0)) <= stale:
                rl = d.get("rate_limits") or {}
                usage = normalize_usage({"five_hour": {"used": (rl.get("five_hour") or {}).get("used_percentage"), "resets_at": (rl.get("five_hour") or {}).get("resets_at")},
                                         "weekly": {"used": (rl.get("seven_day") or {}).get("used_percentage"), "resets_at": (rl.get("seven_day") or {}).get("resets_at")}}, t)
                if usage:
                    lane.notes.append("usage from the status-line file")
        except (OSError, ValueError, TypeError):
            pass
    weekly_spec = S.get("claude_weekly_reset", "")
    if usage:
        f5, wk = usage.get("five_hour") or {}, usage.get("weekly") or {}
        lane.five_reset, lane.week_reset = f5.get("resets_at"), wk.get("resets_at")
        lane.five, _, _ = window(f5.get("used"), lane.five_reset, 300, t)
        lane.week, wmins, lane.pace = window(wk.get("used"), lane.week_reset, WEEK_MIN, t)
        apply_burn(lane, S, wmins)
        if (lane.five or 0) >= 98 or (lane.week or 0) >= 98:
            lane.out = True
            lane.out_until = (lane.week_reset if (lane.week or 0) >= 98 else lane.five_reset) or t + 3600
    elif weekly_spec:
        nxt = next_weekly(weekly_spec, t)
        if nxt:
            lane.week_reset = nxt
            lane.notes.append(f"weekly reset {local(nxt)} (config); use unknown")


def read_claude_credit(lane, S, st, t):
    c = S["credit"]
    start, nxt = credit_cycle(c["reset_day"], t)
    lane.cap = float(c["monthly_cap"])
    lane.spend = ledger_spend(start)
    lane.reset_in_days = (nxt - t) / 86400.0
    lane.week_reset = nxt
    out = (st.get("out") or {}).get(lane.name)
    if out and out.get("until", 0) > t:
        lane.out, lane.out_until = True, out["until"]
        lane.notes.append(out.get("why", "reported out"))
    elif lane.spend >= lane.cap:
        lane.out, lane.out_until = True, nxt
        lane.notes.append("own cycle cap reached")


READERS = {"codex_app_server": read_codex, "claude_subscription": read_claude_subscription, "claude_credit": read_claude_credit}


def gather(cfg, st, t, ai_usage=None, only_known=False):
    S = cfg["settings"]
    lanes = {}
    for name, c in cfg["lanes"].items():
        lane = Lane(name, c["family"], c["kind"], c.get("shared_with_orchestrator", False))
        reader = READERS[c["reader"]]
        if c["reader"] == "claude_subscription":
            reader(lane, S, st, t, ai_usage)
        else:
            reader(lane, S, st, t)
        o = (st.get("out") or {}).get(name)
        if o and o.get("until", 0) > t and not lane.out:      # an out reported by the model or by run-claude
            lane.out, lane.out_until = True, o["until"]
            lane.notes.append(o.get("why", "reported out"))
        lanes[name] = lane
    return lanes


# ----------------------------------------------------------------------------- the decision functions (pure)

def usable(lane, S, relaxed):
    """-> (bool, reason)."""
    if lane.out:
        return False, f"out until {local(lane.out_until)}" if lane.out_until else "out"
    if lane.shared:
        r = S["orchestrator_reserve"]
        c = S["relaxed_ceiling"]
        wk, fv = (c["orchestrator_weekly"], c["orchestrator_five_hour"]) if relaxed else (r["weekly"], r["five_hour"])
        why = "orchestrator reserve" if not relaxed else "hard ceiling"
    else:
        g = S["worker_lane_gate"]
        c = S["relaxed_ceiling"]
        wk, fv = (c["weekly"], c["five_hour"]) if relaxed else (g["weekly"], g["five_hour"])
        why = "lane gate" if not relaxed else "hard ceiling"
    if lane.week is not None and lane.week >= wk:
        return False, f"weekly {lane.week:.0f}% >= {wk} ({why})"
    if lane.five is not None and lane.five >= fv:
        return False, f"5h {lane.five:.0f}% >= {fv} ({why})"
    return True, "ok"


def pick_lane(family, lanes, S, st, t, relaxed, trace):
    """Lane of `family` to use now, or None. Appends the reasoning to `trace`."""
    mine = [l for l in lanes.values() if l.family == family]
    ok = []
    for l in mine:
        good, why = usable(l, S, relaxed)
        trace.append(f"{l.name}: {'usable' if good else 'rejected, ' + why}")
        if good:
            ok.append(l)
    if not ok:
        return None
    sub = next((l for l in ok if l.kind == "subscription"), None)
    credit = next((l for l in ok if l.kind == "credit"), None)
    if not (sub and credit):
        return ok[0]
    if not relaxed and not sub.usage_known:
        trace.append(f"{sub.name}: usage unknown, so {credit.name} first")
        return credit
    c = S["credit"]
    # use-it-or-lose-it
    if sub.burn:
        trace.append(f"{sub.name}: BURN")
        return sub
    if credit.reset_in_days is not None and credit.reset_in_days <= c["use_before_reset_days"] and credit.spend < c["under_cap_fraction"] * credit.cap:
        trace.append(f"{credit.name}: resets in {credit.reset_in_days:.1f}d with the allowance unspent")
        return credit
    # strained / recovered, with a minimum dwell so the choice does not flap
    prev = (st.get("prev") or {}).get(family)
    sticky = prev and prev.get("lane") in (sub.name, credit.name) and t - prev.get("since", 0) < S["min_dwell_minutes"] * 60
    if sticky:
        trace.append(f"dwell: keep {prev['lane']} for {S['min_dwell_minutes']} min")
        return sub if prev["lane"] == sub.name else credit
    if (sub.five is not None and sub.five >= S["strain"]["five_hour"]) or (sub.pace is not None and sub.pace <= S["strain"]["weekly_pace"]):
        trace.append(f"{sub.name}: strained")
        return credit
    if (sub.five is None or sub.five < S["recover"]["five_hour"]) and (sub.pace is None or sub.pace > S["recover"]["weekly_pace"]):
        return sub
    return sub if (not prev or prev.get("lane") == sub.name) else credit


def build_tiers(role, cfg, args):
    """-> list of tiers; each tier is a list of steps tried strict then relaxed before the next tier."""
    steps = list(role.get("steps") or [])
    if "by_writer" in role:
        w = args.get("writer")
        if w not in role["by_writer"]:
            return None
        steps = list(role["by_writer"][w])
    if args.get("fable"):
        steps = [dict(cfg["fable_step"])] + steps
    steps = [dict(s, idx=i) for i, s in enumerate(steps)]
    if role["advance"] == "on_failure":
        failed = args.get("failed") or []
        start = (max(failed) + 1) if failed else 0
        steps = steps[start:]
    main = [s for s in steps if s.get("flag") != "degraded"]
    flagged = [s for s in steps if s.get("flag") == "degraded"]
    tiers = [main]
    if flagged:
        tiers.append(flagged)
    if args.get("allow_degraded") and role.get("degraded_needs_consent"):
        tiers.append([dict(s, flag="degraded_with_consent") for s in role["degraded_needs_consent"]])
    return tiers


def decide(role_name, cfg, lanes, st, t, args):
    """Pure: no I/O. Returns the decision dict."""
    S = cfg["settings"]
    role = cfg["roles"].get(role_name)
    if role is None:
        return {"action": "error", "error": f"unknown role {role_name}; roles: {', '.join(cfg['roles'])}"}
    tiers = build_tiers(role, cfg, args)
    if tiers is None:
        return {"action": "error", "error": f"role {role_name} needs --writer ({'|'.join(role['by_writer'])})"}
    trace = []
    for tier in tiers:
        for relaxed in (False, True):
            for step in tier:
                lane = pick_lane(step["on"], lanes, S, st, t, relaxed, trace)
                if lane is None:
                    continue
                return {"action": "run", "role": role_name, "step": step.get("idx"), "pass": "relaxed" if relaxed else "strict",
                        "lane": lane.name, "model": step["model"], "effort": step["effort"], "mode": step.get("mode", "read_only"),
                        "cap": step.get("cap"), "use": step.get("use"), "flag": step.get("flag"),
                        "how": how(lane, step, cfg), "trace": trace}
    resets = {l.name: local(l.out_until) for l in lanes.values() if l.out and l.out_until}
    stop = {"action": "stop", "role": role_name, "say": role["stop"], "resets": resets, "trace": trace}
    consent = role.get("degraded_needs_consent")
    if consent and not args.get("allow_degraded"):
        stop["ask_user"] = {"question": "No adequate model is available. Run a degraded option?",
                            "options": [f"{s['model']} {s['effort']}" for s in consent],
                            "then": f"route.py next {role_name} --allow-degraded"}
    return stop


def how(lane, step, cfg):
    """Command shape for the step. The orchestrator executes it; the router never runs a model."""
    m = cfg["models"][step["model"]]
    runner = cfg["lanes"][lane.name]["runner"]
    write = step.get("mode") == "write"
    if runner == "claude_credit_worker":
        argv = [sys.executable, str(HERE / "route.py"), "run-claude", "--model", step["model"], "--effort", step["effort"], "--cap", str(step.get("cap", 2))]
        if write:
            argv.append("--write")
        return {"kind": "command", "argv": argv, "prompt": "stdin or last argument", "background": "use Bash run_in_background for long tasks"}
    if runner == "agent_tool":
        return {"kind": "agent_tool", "model": m["agent"], "note": "set model on the Agent call; effort cannot be set per call, so state the effort in the brief"}
    if runner == "codex":
        return {"kind": "codex", "skill": "codex-worker", "use": step.get("use", "task"), "model": m["api"], "effort": step["effort"], "mode": step.get("mode", "read_only"),
                "cli": ["codex", "exec", "-s", "workspace-write" if write else "read-only", "-m", m["api"], "-c", f"model_reasoning_effort={step['effort']}"]}
    return {"kind": "unknown", "runner": runner}


# ----------------------------------------------------------------------------- commands

def parse_json_arg(v):
    if not v:
        return None
    if v == "-":
        v = sys.stdin.read()
    elif v.startswith("@"):
        v = Path(v[1:]).read_text(encoding="utf-8")
    try:
        return json.loads(v)
    except ValueError as e:
        sys.exit(f"route.py: --claude-usage is not valid JSON: {e}")


def needs_input(lanes):
    need = []
    cs = lanes.get("CLAUDE_SUBSCRIPTION")
    if cs and not cs.usage_known:
        need.append({"what": "claude_usage", "why": "Claude subscription use is unknown", "how": "in the Claude desktop app call the get_usage tool and pass its windows as --claude-usage JSON ({five_hour:{used,resets_at},weekly:{used,resets_at}})"})
    ox = lanes.get("OPENAI_SUBSCRIPTION")
    if ox and not ox.usage_known and not ox.out:
        need.append({"what": "codex_login", "why": ox.notes[0] if ox.notes else "codex limits unreadable", "how": "ask the user to run `codex login`, or report the lane out with `route.py report`"})
    return need


def cmd_status(a, cfg):
    t = now()
    st = load_state()
    lanes = gather(cfg, st, t, parse_json_arg(a.claude_usage))
    if a.json:
        print(json.dumps({"lanes": {n: {"family": l.family, "kind": l.kind, "out": l.out, "out_until": l.out_until, "five": l.five, "week": l.week,
                                        "pace": l.pace, "burn": l.burn, "spend": l.spend, "cap": l.cap, "notes": l.notes} for n, l in lanes.items()},
                          "needs_input": needs_input(lanes)}, indent=1))
        return 0
    for n, l in lanes.items():
        print(f"{n:<20} {l.brief(t)}")
    for n in needs_input(lanes):
        print(f"needs input: {n['what']}: {n['how']}")
    return 0


def cmd_next(a, cfg):
    t = now()
    st = load_state()
    lanes = gather(cfg, st, t, parse_json_arg(a.claude_usage))
    args = {"writer": a.writer, "failed": [int(x) for x in a.failed.split(",") if x != ""] if a.failed else [],
            "fable": a.fable, "allow_degraded": a.allow_degraded}
    d = decide(a.role, cfg, lanes, st, t, args)
    if d["action"] == "run":
        fam = cfg["lanes"][d["lane"]]["family"]

        def mark(s):
            prev = s.setdefault("prev", {}).get(fam)
            if not prev or prev.get("lane") != d["lane"]:
                s["prev"][fam] = {"lane": d["lane"], "since": t}
        if sum(1 for l in lanes.values() if l.family == fam) > 1:
            update_state(mark)
    d["needs_input"] = needs_input(lanes)
    d["lanes"] = {n: l.brief(t) for n, l in lanes.items()}
    if a.text:
        if d["action"] == "run":
            print(f"RUN {d['role']} step {d['step']} on {d['lane']}: {d['model']} {d['effort']} ({d['pass']} pass)")
            print("how:", json.dumps(d["how"]))
        else:
            print("STOP:", d.get("say") or d.get("error"))
            if d.get("ask_user"):
                print("ask the user:", d["ask_user"]["question"], d["ask_user"]["options"])
            if d.get("resets"):
                print("resets:", d["resets"])
        for line in d.get("trace", []):
            print("  -", line)
        return 0
    print(json.dumps(d, indent=1))
    return 0 if d["action"] != "error" else 1


def cmd_report(a, cfg):
    t = now()
    if a.lane not in cfg["lanes"]:
        sys.exit(f"route.py: unknown lane {a.lane}; lanes: {', '.join(cfg['lanes'])}")
    if a.event == "ok":
        update_state(lambda s: (s.setdefault("out", {}).pop(a.lane, None)))
        print(f"{a.lane}: marked available")
        return 0
    until = to_epoch(a.until)
    if until is None:
        st = load_state()
        lanes = gather(cfg, st, t)
        lane = lanes[a.lane]
        until = lane.out_until or (lane.week_reset if lane.week_reset and lane.week_reset > t and lane.kind == "credit" else None) or t + 3600
    update_state(lambda s: s.setdefault("out", {}).__setitem__(a.lane, {"until": until, "why": a.message or "reported out", "at": t}))
    print(f"{a.lane}: out until {local(until)}")
    return 0


def cmd_capture(a, cfg):
    raw = sys.stdin.read()
    try:
        d = json.loads(raw)
    except ValueError:
        return 0
    rl = d.get("rate_limits")
    if not rl:
        return 0
    p = Path.home() / ".claude" / "state"
    p.mkdir(parents=True, exist_ok=True)
    tmp = p / f"max-limits.json.{os.getpid()}"
    tmp.write_text(json.dumps({"updated": now(), "rate_limits": rl}), encoding="utf-8")
    os.replace(tmp, p / "max-limits.json")
    return 0


# ----------------------------------------------------------------------------- run-claude (the CLAUDE_CREDIT runner)

PREAMBLE = ("You are a delegated worker. Do exactly the task in the brief. Do not delegate further and do not use the delegate skills. "
            "Do not commit or push. Finish with a short report: what you changed or found, and the exit code and last lines of any check you ran.")


def find_key():
    k = os.environ.get("CLAUDE_CREDIT_WORKER_KEY", "").strip()
    if k:
        return k
    for p in (config_dir() / "claude-credit.key", Path.home() / ".config" / "claude-credit-worker" / "key"):
        with contextlib.suppress(OSError):
            k = p.read_text(encoding="utf-8").strip()
            if k:
                return k
    if sys.platform == "darwin" and shutil.which("security"):
        r = subprocess.run(["security", "find-generic-password", "-s", "anthropic-worker-key", "-w"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return ""


def cmd_run_claude(a, cfg):
    t = now()
    S = cfg["settings"]
    model = cfg["models"].get(a.model)
    if not model or model["family"] != "claude":
        sys.exit(f"route.py: run-claude needs a claude model from routing.json, got {a.model}")
    prompt = sys.stdin.read() if (not a.prompt or a.prompt == ["-"]) else " ".join(a.prompt)
    if not prompt.strip():
        sys.exit("route.py: empty prompt")
    key = find_key()
    if not key:
        print("route.py: no API key. Store it once, in your own terminal:\n"
              f"  file (any OS): write it to {config_dir() / 'claude-credit.key'} (private to you)\n"
              "  macOS: security add-generic-password -U -a \"$USER\" -s anthropic-worker-key -w\n"
              "  or set CLAUDE_CREDIT_WORKER_KEY in this worker's own environment (not ANTHROPIC_*).", file=sys.stderr)
        return 2
    c = S["credit"]
    start, nxt = credit_cycle(c["reset_day"], t)
    spent = ledger_spend(start)
    cap = float(a.cap)
    if spent + cap > float(c["monthly_cap"]):
        print(f"route.py: cycle cap: ${spent:.2f} spent + ${cap:.2f} this run > ${c['monthly_cap']} "
              f"(raise monthly_cap in {config_dir() / 'config.json'}, or treat CLAUDE_CREDIT as out)", file=sys.stderr)
        return 77
    exe = shutil.which("claude")
    if not exe:
        print("route.py: claude CLI not found on PATH", file=sys.stderr)
        return 1
    write = bool(a.write)
    tools = ["Read", "Glob", "Grep"] + (["Edit", "Write", "Bash"] if write else []) + list(a.allow or [])
    cmd = [exe] + (["--bare"] if a.bare else []) + [
        "-p", "--output-format", "json", "--model", model["api"], "--effort", a.effort, "--max-budget-usd", str(cap),
        "--no-session-persistence", "--append-system-prompt", PREAMBLE,
        "--permission-mode", "acceptEdits" if write else "default",
        "--allowedTools", *tools, "--disallowedTools", "Bash(git push:*)", "Bash(git commit:*)"]
    env = dict(os.environ)
    for k in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(k, None)
    env["ANTHROPIC_API_KEY"] = key
    env["DELEGATE_DEPTH"] = "1"
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=a.cwd or None, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    try:
        j = json.loads(r.stdout)
    except ValueError:
        j = {}
    text = j.get("result") or out.strip()
    cost = float(j.get("total_cost_usd") or 0)
    if cost > 0:
        ledger_add(cost, a.model, t)
    failed = r.returncode != 0 or j.get("is_error")
    if failed:
        if re.search(r"credit balance (is )?too low", text, re.I):
            update_state(lambda s: s.setdefault("out", {}).__setitem__("CLAUDE_CREDIT", {"until": nxt, "why": "credit balance too low", "at": t}))
            print(text, file=sys.stderr)
            print(f"route.py: credit exhausted, CLAUDE_CREDIT marked out until {local(nxt)}", file=sys.stderr)
            return 75
        if re.search(r"rate.?limit|overloaded|\b429\b|\b529\b", text, re.I):
            print(text, file=sys.stderr)
            print("route.py: rate limited", file=sys.stderr)
            return 76
        print(text, file=sys.stderr)
        return 1
    update_state(lambda s: s.setdefault("out", {}).pop("CLAUDE_CREDIT", None))
    print(text)
    print(f"cost=${cost:.4f} turns={j.get('num_turns', 0)} cycle=${ledger_spend(start):.2f}/${float(c['monthly_cap']):.0f}", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(prog="route.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--claude-usage"); s.add_argument("--json", action="store_true")
    n = sub.add_parser("next"); n.add_argument("role"); n.add_argument("--writer"); n.add_argument("--failed", default="")
    n.add_argument("--fable", action="store_true"); n.add_argument("--allow-degraded", action="store_true")
    n.add_argument("--claude-usage"); n.add_argument("--text", action="store_true")
    r = sub.add_parser("report"); r.add_argument("--lane", required=True); r.add_argument("--event", choices=["out", "ok"], required=True)
    r.add_argument("--until"); r.add_argument("--message")
    sub.add_parser("capture-limits")
    c = sub.add_parser("run-claude"); c.add_argument("--model", required=True); c.add_argument("--effort", required=True)
    c.add_argument("--cap", required=True); c.add_argument("--write", action="store_true"); c.add_argument("--allow", action="append")
    c.add_argument("--cwd"); c.add_argument("--bare", action="store_true"); c.add_argument("prompt", nargs="*")
    a = ap.parse_args(argv)
    cfg = load_routing()
    return {"status": cmd_status, "next": cmd_next, "report": cmd_report, "capture-limits": cmd_capture, "run-claude": cmd_run_claude}[a.cmd](a, cfg)


if __name__ == "__main__":
    sys.exit(main())
