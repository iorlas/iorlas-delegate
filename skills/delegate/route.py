#!/usr/bin/env python3
"""delegate router: deterministic lane and step selection for an LLM orchestrator.

Python 3.8+, standard library only. Windows, macOS, Linux, Docker.
The model supplies only what the script cannot read itself; the script decides.

  route.py status [--claude-usage JSON|@file|-] [--json]
  route.py next ROLE [--writer openai|claude] [--failed 0,1] [--premium [FAMILY]] [--allow-degraded]
                     [--claude-usage JSON|@file|-] [--text]
  route.py run ROLE [same flags as next] [--brief FILE] [--timeout SEC]   brief on stdin; runs a Codex or credit step end to end (guard, stdin, timeout, limit refusal)
  route.py report --lane LANE --event out|ok [--until EPOCH|ISO] [--message TEXT]
  route.py run-claude --model ID --effort LEVEL --cap USD [--write] [--allow TOOL]... [--cwd DIR] [--bare] [PROMPT|-]
  route.py capture-limits [--print]  (status line JSON on stdin)
  route.py guard before|after [--cwd DIR]          snapshot/compare git state around a worker (exit 78 if a worker changed branches, commits, worktrees or stashes)
  route.py doctor [--quiet] [--deep] [--json]      readiness of every lane (no state kept; --deep also validates the API key)
  route.py init [--do STEP] [--decline LANE] [--undecline LANE] [--open-browser]   next onboarding step as JSON
  route.py set-key [--stdin] [--force] | --web [--host IP|tailscale] [--port N] [--timeout S]   the key is handed over by the USER (terminal prompt or one-shot page); the agent never handles it
  route.py config set KEY VALUE | show | ack ID | unack ID     (ack: the user decided a doctor finding needs no action)

Data: routing.json next to this file (lanes, models, roles, thresholds).
Per-user settings: <config dir>/config.json (same keys as "settings"); state, ledger and key live in <config dir>:
  Windows %APPDATA%\\delegate, otherwise $XDG_CONFIG_HOME/delegate or ~/.config/delegate.

Exit codes of run-claude: 0 ok | 75 credit exhausted | 76 rate limited | 77 own cap reached | 78 worker changed git state | 2 key missing | 1 other.
Workers never create branches or worktrees and never commit, merge, push or stash: the coordinator owns git.
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
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import agent_runs  # publishes runs and lane limits for dashboards (agent-runs v1); optional
except ImportError:  # pragma: no cover
    agent_runs = None

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
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
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(p, 0o700)        # holds the API key file, state and ledger: private to the user
    return p


def private(path):
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


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
    private(p)


def update_state(fn):
    with locked(state_path()):
        st = load_state()
        fn(st)
        save_state(st)
        return st


def config_path():
    return config_dir() / "config.json"


def read_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def update_config(fn):
    """Locked read-modify-write of config.json: callers never save a stale snapshot."""
    p = config_path()
    with locked(p):
        cur = read_config()
        fn(cur)
        tmp = p.with_suffix(f".tmp{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=2)
            f.write("\n")
        os.replace(tmp, p)
        private(p)
        return cur


def profile_lanes():
    """{lane: "enabled"|"declined"} written by init, set-key and first use. A missing lane = not decided."""
    return dict((read_config().get("profile") or {}).get("lanes") or {})


def set_profile_lane(name, value):
    """value None removes the entry (back to auto-detect)."""
    def put(c):
        lanes = c.setdefault("profile", {}).setdefault("lanes", {})
        if value is None:
            lanes.pop(name, None)
        else:
            lanes[name] = value
    update_config(put)


def acked():
    return set((read_config().get("profile") or {}).get("ack") or [])


def set_ack(ident, on):
    def put(c):
        lst = c.setdefault("profile", {}).setdefault("ack", [])
        if on and ident not in lst:
            lst.append(ident)
        if not on and ident in lst:
            lst.remove(ident)
    update_config(put)


def migrate_profile():
    """state.json skip_lanes (older versions) -> profile declined. Runs once; cheap when there is nothing to move."""
    st = load_state()
    old = st.get("skip_lanes")
    if not old:
        return
    for n in old:
        if n not in profile_lanes():
            set_profile_lane(n, "declined")
    update_state(lambda s: s.pop("skip_lanes", None))


def harness():
    """desktop | container | cli | headless | unknown, from the environment. The model never decides this."""
    ep = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    if "desktop" in ep:
        return "desktop"
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return "container"
    if ep == "cli":
        return "cli"
    return "unknown" if ep else "headless"


def lane_mode(name, cfg_lane, ready):
    """-> "on" | "off" | "drift". Shared lanes are always on. Otherwise the profile decides;
    a lane never decided is turned on once if the machine has it ready, else left out silently."""
    if cfg_lane.get("shared_with_orchestrator"):
        return "on"
    v = profile_lanes().get(name)
    if v == "declined":
        return "off"
    if v == "enabled":
        return "on" if ready else "drift"
    if ready:
        set_profile_lane(name, "enabled")
        return "on"
    return "off"


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
        private(ledger_path())


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

def fetch_codex_limits():
    """-> (result_dict, error_text). Spawns `codex app-server` once; ~1-2 s."""
    exe = shutil.which("codex")
    if not exe:
        return None, "codex CLI not installed"
    try:
        p = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    except OSError as e:
        return None, f"codex app-server failed to start: {e}"

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
        if read(1, 8) is None:
            return None, "codex app-server did not answer"
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        r = read(2, 8)
    finally:
        p.kill()
        p.wait()
        for f in (p.stdin, p.stdout):
            with contextlib.suppress(OSError):
                f.close()
    if not r or "result" not in r:
        return None, (r or {}).get("error", {}).get("message", "codex gave no limits (logged in? run `codex login`)")
    return r["result"], None


def read_codex(lane, S, st, t):
    cached = st.get("codex_limits")
    if cached and t - cached.get("at", 0) <= 60 and not os.environ.get("DELEGATE_NOCACHE"):
        res, err = cached["res"], None
    else:
        res, err = fetch_codex_limits()
        if res is not None:
            update_state(lambda s_: s_.__setitem__("codex_limits", {"at": t, "res": res}))
    if res is None:
        lane.notes.append(err)
        return
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
    if isinstance(raw, dict) and isinstance(raw.get("plan"), dict):      # the whole get_usage tool result
        raw = raw["plan"]
    if isinstance(raw, dict) and isinstance(raw.get("windows"), list):   # {"windows":[{"label","percentUsed","resetsAt"}]}
        raw = raw["windows"]
    if isinstance(raw, list):
        found = {}
        for w in raw:
            label = str((w or {}).get("label", "")).lower()
            if "fable" in label:
                key = "fable"
            elif "5" in label and "hour" in label:
                key = "five_hour"
            elif "week" in label and "all" in label:
                key = "weekly"
            else:
                continue
            found[key] = w
        raw = found
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
        used = pick(w, ["used", "used_percent", "used_percentage", "percent_used", "utilization", "percent", "usedPercent", "percentUsed"])
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


def gather(cfg, st, t, ai_usage=None, check_config=True):
    S = cfg["settings"]
    lanes = {}
    conf = {}
    if check_config:
        import onboard
        conf = onboard.configured_lanes(sys.modules[__name__], cfg)
    for name, c in cfg["lanes"].items():
        mode = lane_mode(name, c, conf.get(name, True)) if check_config else "on"
        if mode == "off":
            continue
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
        if mode == "drift":
            lane.out, lane.out_until = True, None
            lane.notes.insert(0, "enabled but not ready (login or key gone?)")
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
    prem = args.get("premium")
    if prem:
        step = (cfg.get("premium") or {}).get(prem)
        if not step:
            return None
        steps = [dict(step)] + steps
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
        if args.get("premium") and not (cfg.get("premium") or {}).get(args["premium"]):
            return {"action": "error", "error": f"no premium model configured for family {args['premium']}; premium: {[k for k, v in (cfg.get('premium') or {}).items() if v and k != 'about']}"}
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
                        "how": how(lane, step, cfg, args.get("cwd")), "trace": trace}
    resets = {l.name: local(l.out_until) for l in lanes.values() if l.out and l.out_until}
    stop = {"action": "stop", "role": role_name, "say": role["stop"], "resets": resets, "trace": trace}
    consent = role.get("degraded_needs_consent")
    if consent and not args.get("allow_degraded"):
        stop["ask_user"] = {"question": "No adequate model is available. Run a degraded option?",
                            "options": [f"{s['model']} {s['effort']}" for s in consent],
                            "then": f"route.py next {role_name} --allow-degraded"}
    return stop


def how(lane, step, cfg, cwd=None):
    """Command shape for the step. The orchestrator executes it; the router never runs a model."""
    m = cfg["models"][step["model"]]
    runner = cfg["lanes"][lane.name]["runner"]
    write = step.get("mode") == "write"
    wd = os.path.abspath(cwd) if cwd else os.getcwd()
    common = {"cwd": wd, "brief_prefix": GIT_RULES,
              "guard": {"before": [sys.executable, str(HERE / "route.py"), "guard", "before", "--cwd", wd],
                        "after": [sys.executable, str(HERE / "route.py"), "guard", "after", "--cwd", wd]}}
    res = _how(lane, step, cfg, runner, m, write, wd)
    res.update(common)
    if runner == "claude_credit_worker":
        res.pop("guard")          # run-claude guards git itself
    return res


def _how(lane, step, cfg, runner, m, write, wd):
    if runner == "claude_credit_worker":
        argv = [sys.executable, str(HERE / "route.py"), "run-claude", "--model", step["model"], "--effort", step["effort"], "--cap", str(step.get("cap", 2)), "--cwd", wd]
        if write:
            argv.append("--write")
        return {"kind": "command", "argv": argv, "prompt": "stdin or last argument", "background": "use Bash run_in_background for long tasks"}
    if runner == "agent_tool":
        return {"kind": "agent_tool", "model": m["agent"], "note": "set model on the Agent call; effort cannot be set per call, so state the effort in the brief"}
    if runner == "codex":
        cli = ["codex", "exec", "-C", wd, "-s", "workspace-write" if write else "read-only", "-m", m["api"], "-c", f"model_reasoning_effort={step['effort']}"]
        if git_state(wd) is None:
            cli.append("--skip-git-repo-check")      # codex refuses a directory that is not a git work tree
        return {"kind": "codex", "doc": "modes/codex.md", "use": step.get("use", "task"), "model": m["api"], "effort": step["effort"], "mode": step.get("mode", "read_only"),
                "cli": cli, "stdin": "close_unless_brief", "timeout_kills_process_group": True}
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
    if cs and not cs.usage_known and harness() == "desktop":
        need.append({"what": "claude_usage", "why": "Claude subscription use is unknown", "how": "in the Claude desktop app call the get_usage tool and pass its windows as --claude-usage JSON ({five_hour:{used,resets_at},weekly:{used,resets_at}})"})
    ox = lanes.get("OPENAI_SUBSCRIPTION")
    if ox and not ox.usage_known and not ox.out:
        need.append({"what": "codex_login", "why": ox.notes[0] if ox.notes else "codex limits unreadable", "how": "ask the user to run `codex login`, or report the lane out with `route.py report`"})
    return need


def cmd_status(a, cfg):
    t = now()
    st = load_state()
    lanes = gather(cfg, st, t, parse_json_arg(a.claude_usage))
    publish_lanes(lanes, st, t)
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


def drift_warnings(lanes, st, t):
    """A lane the profile enabled but the machine no longer supports: say so once a day."""
    warned = st.get("warned_at") or {}
    bad = [n for n, l in lanes.items() if l.notes and l.notes[0].startswith("enabled but not ready")]
    out = [f"{n} is enabled but not ready, so it is skipped (route.py doctor; or route.py init --decline {n})" for n in bad if t - warned.get(n, 0) > 86400]
    if out:
        update_state(lambda s_: s_.setdefault("warned_at", {}).update({n: t for n in bad}))
    return out


def decision(a, cfg):
    """next and run share this: gather, decide, remember the lane choice. -> (d, lanes, warnings, t)"""
    t = now()
    st = load_state()
    lanes = gather(cfg, st, t, parse_json_arg(a.claude_usage))
    warnings = drift_warnings(lanes, st, t)
    publish_lanes(lanes, st, t)
    args = {"writer": a.writer, "failed": [int(x) for x in a.failed.split(",") if x != ""] if a.failed else [],
            "premium": a.premium, "allow_degraded": a.allow_degraded, "cwd": a.cwd}
    d = decide(a.role, cfg, lanes, st, t, args)
    if d["action"] == "run":
        fam = cfg["lanes"][d["lane"]]["family"]

        def mark(s):
            prev = s.setdefault("prev", {}).get(fam)
            if not prev or prev.get("lane") != d["lane"]:
                s["prev"][fam] = {"lane": d["lane"], "since": t}
        if sum(1 for l in lanes.values() if l.family == fam) > 1:
            update_state(mark)
    return d, lanes, warnings, t


def cmd_next(a, cfg):
    d, lanes, warnings, t = decision(a, cfg)
    d["needs_input"] = needs_input(lanes)
    if warnings:
        d["warnings"] = warnings
    if not a.verbose:       # the hot path stays small: reasoning, lane table and guard commands only on request
        d.pop("trace", None)
        (d.get("how") or {}).pop("guard", None)
    else:
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
        lanes = gather(cfg, st, t, check_config=False)     # a lane the profile leaves out can still be reported out
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
    if getattr(a, "print", False):
        f5, f7 = (rl.get("five_hour") or {}).get("used_percentage"), (rl.get("seven_day") or {}).get("used_percentage")
        print("5h %s%% wk %s%%" % ("?" if f5 is None else round(f5), "?" if f7 is None else round(f7)))
    p = Path.home() / ".claude" / "state"
    p.mkdir(parents=True, exist_ok=True)
    tmp = p / f"max-limits.json.{os.getpid()}"
    tmp.write_text(json.dumps({"updated": now(), "rate_limits": rl}), encoding="utf-8")
    os.replace(tmp, p / "max-limits.json")
    return 0


# ----------------------------------------------------------------------------- git ownership: the coordinator owns git, workers do not

GIT_RULES = ("Git is the coordinator's. Stay in the current directory. Do not create or switch branches or worktrees; "
             "do not commit, amend, merge, rebase, reset, stash, tag or push. Read-only git is fine. Leave changes uncommitted.")

GIT_WRITE_DENY = ["Bash(git commit:*)", "Bash(git push:*)", "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git branch:*)",
                  "Bash(git worktree:*)", "Bash(git merge:*)", "Bash(git rebase:*)", "Bash(git reset:*)", "Bash(git stash:*)",
                  "Bash(git cherry-pick:*)", "Bash(git tag:*)", "Bash(git revert:*)", "Bash(git am:*)", "Bash(git pull:*)"]


def git_state(cwd):
    """Snapshot of what a worker must not change, or None when cwd is not a git work tree."""
    def git(*args):
        r = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    if shutil.which("git") is None or git("rev-parse", "--is-inside-work-tree") is None:
        return None
    refs = {}
    for line in (git("for-each-ref", "--format=%(refname) %(objectname)") or "").splitlines():
        name, _, sha = line.partition(" ")
        refs[name] = sha
    return {"head": (git("rev-parse", "HEAD") or "").strip(), "branch": (git("symbolic-ref", "-q", "--short", "HEAD") or "").strip(),
            "refs": refs, "worktrees": sorted(l for l in (git("worktree", "list", "--porcelain") or "").splitlines() if l.startswith("worktree "))}


def git_changes(before, after):
    """-> list of human-readable differences; empty when the worker left git alone."""
    if before is None or after is None:
        return []
    out = []
    if before["branch"] != after["branch"]:
        out.append(f"branch {before['branch'] or '(detached)'} -> {after['branch'] or '(detached)'}")
    if before["head"] != after["head"]:
        out.append(f"HEAD {before['head'][:9]} -> {after['head'][:9]}")
    for name in sorted(set(before["refs"]) | set(after["refs"])):
        a, b = before["refs"].get(name), after["refs"].get(name)
        if a != b:
            out.append(f"ref {name}: " + ("created" if a is None else "deleted" if b is None else "moved"))
    if before["worktrees"] != after["worktrees"]:
        out.append("worktrees changed")
    return out


def cmd_guard(a, cfg):
    cwd = os.path.abspath(a.cwd or os.getcwd())
    key = "git_guard:" + cwd
    if a.when == "before":
        snap = git_state(cwd)
        update_state(lambda s: s.setdefault("guard", {}).__setitem__(cwd, snap))
        print(json.dumps({"guard": "armed" if snap else "not a git work tree", "cwd": cwd}))
        return 0
    before = (load_state().get("guard") or {}).get(cwd)
    diffs = git_changes(before, git_state(cwd))
    update_state(lambda s: s.setdefault("guard", {}).pop(cwd, None))
    if diffs:
        print("GIT-VIOLATION: the worker changed git state: " + "; ".join(diffs) +
              ". The file changes are still on disk; inspect with git reflog and decide. Workers must not create branches or worktrees, commit, merge or stash.", file=sys.stderr)
        return 78
    print(json.dumps({"guard": "clean", "cwd": cwd}))
    return 0


# ----------------------------------------------------------------------------- run-claude (the CLAUDE_CREDIT runner)

PREAMBLE = ("You are a delegated worker. Do exactly the task in the brief. Do not delegate further and do not use the delegate skill. "
            + GIT_RULES + " Finish with a short report: what you changed or found, and the exit code and last lines of any check you ran.")


def find_key(quiet=False):
    k = os.environ.get("CLAUDE_CREDIT_WORKER_KEY", "").strip()
    if k:
        return k
    for p in (config_dir() / "claude-credit.key", Path.home() / ".config" / "claude-credit-worker" / "key"):
        with contextlib.suppress(OSError):
            k = p.read_text(encoding="utf-8").strip()
            if k:
                if not quiet and os.name != "nt" and (p.stat().st_mode & 0o077):
                    print(f"route.py: warning: {p} is readable by others; run: chmod 600 {p}", file=sys.stderr)
                return k
    if sys.platform == "darwin" and shutil.which("security"):
        r = subprocess.run(["security", "find-generic-password", "-s", "anthropic-worker-key", "-w"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return ""


def publish_lanes(lanes, st, t):
    """Lane limits as agent-runs v1 (agent_runs.py), for dashboards. Never fails the caller."""
    if agent_runs is None:
        return
    try:
        outs = st.get("out") or {}
        agent_runs.lanes([(n, l.out_until if l.out else None, (outs.get(n) or {}).get("why") if l.out else None,
                           [("5h", l.five, l.five_reset), ("week", l.week, l.week_reset)]) for n, l in lanes.items()], t)
    except Exception:
        pass


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
        print("route.py: no API key for the credit lane. Run `route.py init` and relay its step to the user; do not ask for the key in chat.", file=sys.stderr)
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
    run_id = str(uuid.uuid4())  # the worker's session id: its transcript is ~/.claude/projects/*/<run_id>.jsonl
    write = bool(a.write)
    tools = ["Read", "Glob", "Grep"] + (["Edit", "Write", "Bash"] if write else []) + list(a.allow or [])
    cmd = [exe] + (["--bare"] if a.bare else []) + [
        "-p", "--output-format", "json", "--model", model["api"], "--effort", a.effort, "--max-budget-usd", str(cap),
        "--session-id", run_id, "--append-system-prompt", PREAMBLE,
        "--permission-mode", "acceptEdits" if write else "default",
        "--allowedTools", *tools, "--disallowedTools", *GIT_WRITE_DENY]
    env = dict(os.environ)
    for k in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(k, None)
    env["ANTHROPIC_API_KEY"] = key
    env["DELEGATE_DEPTH"] = "1"
    workdir = os.path.abspath(a.cwd) if a.cwd else os.getcwd()
    git_before = git_state(workdir)
    if agent_runs:
        agent_runs.run(run_id, kind="claude-p", session=run_id, lane="CLAUDE_CREDIT", model=a.model, cwd=workdir,
                       parent_session=os.environ.get("CLAUDE_CODE_SESSION_ID", ""), started=t, status="running",
                       title=prompt.strip().splitlines()[0][:120])
    print(f"session={run_id}", file=sys.stderr, flush=True)
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=workdir, env=env)
    if agent_runs:
        agent_runs.run(run_id, ended=now(), status="failed" if r.returncode else "completed", exit=r.returncode)
    violations = git_changes(git_before, git_state(workdir))
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
    if violations:
        print("GIT-VIOLATION: the worker changed git state: " + "; ".join(violations) +
              ". The file changes are still on disk; inspect with git reflog and decide.", file=sys.stderr)
        print(f"cost=${cost:.4f} turns={j.get('num_turns', 0)}", file=sys.stderr)
        return 78
    print(f"cost=${cost:.4f} turns={j.get('num_turns', 0)} cycle=${ledger_spend(start):.2f}/${float(c['monthly_cap']):.0f}", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------------- run: execute one step (Codex or credit) end to end

LIMIT_RE = re.compile(r"usage limit|rate.?limit|quota|too many requests|\b429\b|try again (later|in)|credit balance", re.I)


def kill_tree(p):
    """Kill the worker and everything it started. Killing only the codex wrapper leaves the vendor binary running."""
    with contextlib.suppress(Exception):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else:
            import signal
            os.killpg(p.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        p.kill()


def run_codex_step(d, brief, timeout, t):
    """-> (exit_code, text). stdin carries the brief and is then closed, so codex never waits on it."""
    h = d["how"]
    cwd = h["cwd"]
    exe = shutil.which("codex")
    if not exe:
        return 2, "codex CLI not found on PATH (route.py init)"
    out_file = config_dir() / f"codex-last-{uuid.uuid4().hex[:8]}.txt"
    cmd = [exe] + h["cli"][1:] + ["-o", str(out_file), "-"]
    run_id = str(uuid.uuid4())
    if agent_runs:
        agent_runs.run(run_id, kind="codex", lane=d["lane"], model=d["model"], cwd=cwd, parent_session=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
                       started=t, status="running", title=brief.strip().splitlines()[0][:120] if brief.strip() else "codex")
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd, **kw)
    try:
        log, _ = p.communicate(input=brief, timeout=timeout)
        code = p.returncode
    except subprocess.TimeoutExpired:
        kill_tree(p)
        log, _ = p.communicate()
        code = 124
    text = ""
    with contextlib.suppress(OSError):
        text = out_file.read_text(encoding="utf-8").strip()
        out_file.unlink()
    if agent_runs:
        agent_runs.run(run_id, ended=now(), status="failed" if code else "completed", exit=code)
    if code == 124:
        return 124, f"codex did not finish within {timeout}s and was killed (process group). Last output:\n" + (log or "")[-600:]
    if code != 0:
        return code, (text or (log or "")[-1500:])
    return 0, text or (log or "")[-1500:]


def cmd_run(a, cfg):
    d, lanes, warnings, t = decision(a, cfg)
    if d["action"] != "run":
        print(json.dumps({k: d[k] for k in ("action", "say", "resets", "ask_user", "error") if k in d}))
        return 0 if d["action"] == "stop" else 1
    head = {"lane": d["lane"], "model": d["model"], "effort": d["effort"], "pass": d["pass"]}
    if warnings:
        head["warnings"] = warnings
    ni = [n["what"] for n in needs_input(lanes)]
    if ni:
        head["needs_input"] = ni
    h = d["how"]
    if h["kind"] == "agent_tool":
        print(json.dumps({"action": "agent", **head, "agent_model": h["model"], "brief_prefix": GIT_RULES, "cwd": h["cwd"]}))
        return 0
    brief = sys.stdin.read() if not a.brief else Path(a.brief).read_text(encoding="utf-8")
    if not brief.strip():
        print(json.dumps({"action": "error", "error": "empty brief: pass it on stdin or with --brief FILE"}))
        return 1
    cwd = h["cwd"]
    if h["kind"] == "codex":
        full = GIT_RULES + "\n\n" + brief
        git_before = git_state(cwd)
        code, text = run_codex_step(d, full, a.timeout, t)
        violations = git_changes(git_before, git_state(cwd))
        if code not in (0, 124) and LIMIT_RE.search(text):
            until = t + 3600
            update_state(lambda s: s.setdefault("out", {}).__setitem__(d["lane"], {"until": until, "why": "limit refusal: " + text[:120].replace("\n", " "), "at": t}))
            print(text, file=sys.stderr)
            print(json.dumps({"action": "limit", **head, "exit": 76, "say": f"{d['lane']} marked out until {local(until)}; call run again to advance"}), file=sys.stderr)
            return 76
        if code == 0:
            update_state(lambda s: s.setdefault("out", {}).pop(d["lane"], None))
        print(text)
        status = {"action": "done" if code == 0 else "failed", **head, "exit": code}
        if violations:
            status.update(git_violation=violations, exit=78)
            print("GIT-VIOLATION: the worker changed git state: " + "; ".join(violations) + ". The file changes are still on disk; inspect with git reflog and decide.", file=sys.stderr)
            code = 78
        print(json.dumps(status), file=sys.stderr)
        return code
    if h["kind"] == "command":          # CLAUDE_CREDIT: run-claude does its own guard, ledger and out marking
        ns = argparse.Namespace(model=d["model"], effort=d["effort"], cap=str(d.get("cap") or 2), write=d["mode"] == "write", allow=None, cwd=cwd, bare=False, prompt=[brief])
        code = cmd_run_claude(ns, cfg)
        print(json.dumps({"action": "done" if code == 0 else "failed", **head, "exit": code}), file=sys.stderr)
        return code
    print(json.dumps({"action": "error", "error": f"cannot run kind {h['kind']}"}))
    return 1


# ----------------------------------------------------------------------------- onboarding commands (code in onboard.py)

def cmd_doctor(a, cfg):
    import onboard
    d = onboard.doctor(sys.modules[__name__], cfg, deep=a.deep)
    if a.json:
        print(json.dumps(d, indent=1))
    else:
        onboard.print_doctor(d, a.quiet)
    return 0 if d["ready"] else 1


def cmd_init(a, cfg):
    import onboard
    R = sys.modules[__name__]
    if a.undecline:
        if profile_lanes().get(a.undecline) == "declined":
            set_profile_lane(a.undecline, None)
        print(json.dumps({"done": True, "say": f"{a.undecline} back in the setup list."}))
        return 0
    out = onboard.init(R, cfg, do=a.do, decline=a.decline, open_browser=a.open_browser)
    print(json.dumps(out, indent=1))
    return 1 if out.get("error") else 0


def cmd_set_key(a, cfg):
    import onboard
    R = sys.modules[__name__]
    if a.web:
        return onboard.set_key_web(R, host=a.host, port=a.port, timeout=a.timeout, force=a.force)
    return onboard.set_key(R, from_stdin=a.stdin, force=a.force)


def cmd_config(a, cfg):
    import onboard
    R = sys.modules[__name__]
    if a.action in ("ack", "unack"):
        if not a.key:
            sys.exit(f"route.py: config {a.action} ID  (an id from `doctor`)")
        set_ack(a.key, a.action == "ack")
        print(json.dumps({"acknowledged": sorted(acked())}))
        return 0
    if a.action == "set":
        if not a.key or a.value is None:
            sys.exit("route.py: config set KEY VALUE")
        print(json.dumps(onboard.config_set(R, a.key, a.value), indent=1))
    else:
        print(json.dumps(onboard.config_show(R, cfg), indent=1))
    return 0


# ----------------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(prog="route.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--claude-usage"); s.add_argument("--json", action="store_true")
    n = sub.add_parser("next"); n.add_argument("role"); n.add_argument("--writer"); n.add_argument("--failed", default="")
    n.add_argument("--premium", nargs="?", const="claude", metavar="FAMILY"); n.add_argument("--allow-degraded", action="store_true")
    n.add_argument("--claude-usage"); n.add_argument("--text", action="store_true"); n.add_argument("--cwd"); n.add_argument("--verbose", action="store_true")
    r = sub.add_parser("report"); r.add_argument("--lane", required=True); r.add_argument("--event", choices=["out", "ok"], required=True)
    r.add_argument("--until"); r.add_argument("--message")
    cl = sub.add_parser("capture-limits"); cl.add_argument("--print", action="store_true")
    dr = sub.add_parser("doctor"); dr.add_argument("--quiet", action="store_true"); dr.add_argument("--deep", action="store_true"); dr.add_argument("--json", action="store_true")
    ini = sub.add_parser("init"); ini.add_argument("--do"); ini.add_argument("--decline"); ini.add_argument("--undecline"); ini.add_argument("--open-browser", action="store_true")
    gd = sub.add_parser("guard"); gd.add_argument("when", choices=["before", "after"]); gd.add_argument("--cwd")
    sk = sub.add_parser("set-key"); sk.add_argument("--stdin", action="store_true"); sk.add_argument("--force", action="store_true")
    sk.add_argument("--web", action="store_true"); sk.add_argument("--host", default="127.0.0.1"); sk.add_argument("--port", type=int, default=0); sk.add_argument("--timeout", type=int, default=300)
    cf = sub.add_parser("config"); cf.add_argument("action", choices=["set", "show", "ack", "unack"]); cf.add_argument("key", nargs="?"); cf.add_argument("value", nargs="?")
    ru = sub.add_parser("run"); ru.add_argument("role"); ru.add_argument("--writer"); ru.add_argument("--failed", default="")
    ru.add_argument("--premium", nargs="?", const="claude", metavar="FAMILY"); ru.add_argument("--allow-degraded", action="store_true")
    ru.add_argument("--claude-usage"); ru.add_argument("--cwd"); ru.add_argument("--brief"); ru.add_argument("--timeout", type=int, default=1800)
    c = sub.add_parser("run-claude"); c.add_argument("--model", required=True); c.add_argument("--effort", required=True)
    c.add_argument("--cap", required=True); c.add_argument("--write", action="store_true"); c.add_argument("--allow", action="append")
    c.add_argument("--cwd"); c.add_argument("--bare", action="store_true"); c.add_argument("prompt", nargs="*")
    a = ap.parse_args(argv)
    migrate_profile()
    cfg = load_routing()
    return {"status": cmd_status, "next": cmd_next, "report": cmd_report, "capture-limits": cmd_capture, "run-claude": cmd_run_claude,
            "run": cmd_run, "doctor": cmd_doctor, "guard": cmd_guard, "init": cmd_init, "set-key": cmd_set_key, "config": cmd_config}[a.cmd](a, cfg)


if __name__ == "__main__":
    sys.exit(main())
