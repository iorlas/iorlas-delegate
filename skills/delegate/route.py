#!/usr/bin/env python3
"""delegate router: deterministic lane and step selection for an LLM orchestrator.

Python 3.8+, standard library only. Windows, macOS, Linux, Docker.
The model supplies only what the script cannot read itself; the script decides.

  route.py status [--claude-usage JSON|@file|-] [--json]
  route.py next ROLE [--writer openai|claude] [--failed 0,1] [--premium [FAMILY]] [--allow-degraded]
                     [--claude-usage JSON|@file|-] [--cwd DIR] [--verbose]
  route.py run ROLE [--cwd DIR] [--brief FILE] [--schema FILE] [--resume REF] [--cap USD] [--timeout SEC] [--writer W] [--failed I,J] [--premium] [--allow-degraded]
                    [--claude-usage JSON] [--parent SESSION] [--task ID] [--title T]    brief on stdin; one JSON object on stdout, exit 0 (see docs/dev/integrations.md)
  route.py report --lane LANE --event out|ok [--until EPOCH|ISO] [--message TEXT]
  route.py capture-limits [--print]  (status line JSON on stdin)
  route.py doctor [--lane L] [--next] [--fix ID] [--decline L] [--undecline L] [--ack ID] [--deep] [--json] [--quiet]   the one setup command: what each lane needs, and how to act on it
  route.py set-key [--stdin] [--force] | --web [--host IP|tailscale] [--port N] [--timeout S]   the key is handed over by the USER (terminal prompt or one-shot page); the agent never handles it
  route.py config set KEY VALUE | show

Data: routing.json next to this file (lanes, models, roles, thresholds).
Per-user settings: <config dir>/config.json (same keys as "settings"); state, ledger and key live in <config dir>:
  $DELEGATE_CONFIG_DIR, else Claude Code's plugin data dir (~/.claude/plugins/data/<plugin>-<marketplace>), else (a checkout) ~/.config/delegate
  (%APPDATA%\\delegate on Windows). Files from the old location are moved in once.

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
import threading
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


def checkout_dir():
    """Where a checkout (not an installed plugin) keeps its files."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "delegate"


def plugin_data_dir():
    """Claude Code's persistent data dir for this plugin: <plugins>/data/<plugin>-<marketplace>, derived from where this copy is installed
    (<plugins>/cache/<marketplace>/<plugin>/<version>/skills/delegate). None when running from a checkout."""
    parts = HERE.parts
    for i in range(1, len(parts) - 3):
        if parts[i] == "cache" and parts[i - 1] == "plugins":
            ident = re.sub(r"[^A-Za-z0-9_-]", "-", f"{parts[i + 2]}@{parts[i + 1]}")
            return Path(*parts[:i]) / "data" / ident
    return None


def config_dir():
    """Where settings, profile, state, ledger and the key live. Order: DELEGATE_CONFIG_DIR, the plugin's data dir (installed plugin),
    else ~/.config/delegate (checkout). CLAUDE_PLUGIN_DATA is deliberately not read: it names whichever plugin launched the process."""
    explicit = os.environ.get("DELEGATE_CONFIG_DIR")
    p = Path(explicit) if explicit else (plugin_data_dir() or checkout_dir())
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


def interactive():
    """A Claude session (Desktop or CLI) is behind this process: the Agent tool, get_usage and the user exist. Anything else is headless."""
    return harness() in ("desktop", "cli")


OFFERS = []     # lanes seen for the first time and not usable; reported once, then forgotten


def offered():
    return set((read_config().get("profile") or {}).get("offered") or [])


def mark_offered(names):
    def put(c):
        lst = c.setdefault("profile", {}).setdefault("offered", [])
        lst.extend(n for n in names if n not in lst)
    update_config(put)


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
    if name not in offered() and name not in OFFERS:
        OFFERS.append(name)
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


def normalize_usage(raw):
    """get_usage's result (or its `plan`, or its `windows` list) -> {five_hour, weekly, fable}, each {used, resets_at}; None if no window matches."""
    if isinstance(raw, dict) and isinstance(raw.get("plan"), dict):
        raw = raw["plan"]
    if isinstance(raw, dict):
        raw = raw.get("windows")
    out = {}
    for w in raw if isinstance(raw, list) else []:
        if not isinstance(w, dict) or w.get("percentUsed") is None:
            continue
        label = str(w.get("label", "")).lower()
        key = "fable" if "fable" in label else "five_hour" if "5" in label and "hour" in label else "weekly" if "week" in label and "all" in label else None
        if key:
            out[key] = {"used": float(w["percentUsed"]), "resets_at": to_epoch(w.get("resetsAt"))}
    return out or None


def read_claude_subscription(lane, S, st, t, ai_usage=None):
    stale = S["usage_stale_minutes"] * 60
    usage = None
    if ai_usage:
        usage = normalize_usage(ai_usage)
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
                usage = {k: {"used": float(w["used_percentage"]), "resets_at": w.get("resets_at")}
                         for k, w in (("five_hour", rl.get("five_hour") or {}), ("weekly", rl.get("seven_day") or {})) if w.get("used_percentage") is not None} or None
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
        if o and o.get("until", 0) > t and not lane.out:      # an out reported by the model or by a launcher
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
                        "how": how(lane, step, cfg, args.get("cwd"), args.get("headless", False)), "trace": trace}
    resets = {l.name: local(l.out_until) for l in lanes.values() if l.out and l.out_until}
    stop = {"action": "stop", "role": role_name, "say": role["stop"], "resets": resets, "trace": trace}
    consent = role.get("degraded_needs_consent")
    if consent and not args.get("allow_degraded"):
        stop["ask_user"] = {"question": "No adequate model is available. Run a degraded option?",
                            "options": [f"{s['model']} {s['effort']}" for s in consent],
                            "then": f"route.py next {role_name} --allow-degraded"}
    return stop


def how(lane, step, cfg, cwd=None, headless=False):
    """What a step runs on. Headless callers have no Agent tool, so the subscription lane runs `claude -p` on the logged-in account."""
    m = cfg["models"][step["model"]]
    runner = cfg["lanes"][lane.name]["runner"]
    wd = os.path.abspath(cwd) if cwd else os.getcwd()
    write = step.get("mode") == "write"
    if runner == "agent_tool" and headless:
        runner = "claude_p"
    if runner == "claude_p":
        return {"kind": "claude_p", "model": m["api"], "write": write, "cap": step.get("cap", 2), "cwd": wd}
    if runner == "agent_tool":
        return {"kind": "agent_tool", "model": m["agent"], "cwd": wd}
    cli = ["codex", "exec", "-C", wd, "-s", "workspace-write" if write else "read-only", "-m", m["api"], "-c", f"model_reasoning_effort={step['effort']}"]
    if git_state(wd) is None:
        cli.append("--skip-git-repo-check")      # codex refuses a directory that is not a git work tree
    return {"kind": "codex", "model": m["api"], "cli": cli, "cwd": wd}


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
    out = [f"{n} is enabled but not ready, so it is skipped (route.py doctor; or route.py doctor --decline {n})" for n in bad if t - warned.get(n, 0) > 86400]
    if out:
        update_state(lambda s_: s_.setdefault("warned_at", {}).update({n: t for n in bad}))
    return out


def decision(a, cfg):
    """next and run share this: gather, decide, remember the lane choice. -> (d, lanes, warnings, t)"""
    t = now()
    st = load_state()
    headless = not interactive()
    del OFFERS[:]
    lanes = gather(cfg, st, t, parse_json_arg(a.claude_usage))
    new_offers = list(OFFERS)
    warnings = drift_warnings(lanes, st, t)
    publish_lanes(lanes, st, t)
    if headless:        # no one to pay the API credit knowingly: credit lanes are never picked headless
        lanes = {n: l for n, l in lanes.items() if l.kind != "credit"}
    S = cfg["settings"]
    ref = getattr(a, "resume", None)
    if ref:             # continue on the lane that made the ref; never switch lanes silently
        rec = (st.get("refs") or {}).get(ref)
        lane = lanes.get(rec["lane"]) if rec else None
        if not rec:
            return {"action": "error", "error": f"unknown --resume ref {ref}"}, lanes, warnings, t
        if lane is None or not usable(lane, S, True)[0]:
            return {"action": "lane_out", "lane": rec["lane"], "retry_after": lane.out_until if lane else None,
                    "say": f"{rec['lane']} cannot continue {ref} now"}, lanes, warnings, t
        step = {"model": rec["model"], "effort": rec["effort"], "mode": rec["mode"], "cap": a.cap or rec.get("cap")}
        return {"action": "run", "role": a.role, "step": None, "pass": "resume", "lane": lane.name, "model": rec["model"], "effort": rec["effort"],
                "mode": rec["mode"], "cap": step["cap"], "how": how(lane, step, cfg, a.cwd, headless)}, lanes, warnings, t
    args = {"writer": a.writer, "failed": [int(x) for x in a.failed.split(",") if x != ""] if a.failed else [],
            "premium": a.premium, "allow_degraded": a.allow_degraded and not headless, "cwd": a.cwd, "headless": headless}
    d = decide(a.role, cfg, lanes, st, t, args)
    if d["action"] == "run":
        fam = cfg["lanes"][d["lane"]]["family"]
        if getattr(a, "cap", None):
            d["cap"] = d["how"]["cap"] = a.cap if d["how"]["kind"] == "claude_p" else d["how"].get("cap")

        def mark(s):
            prev = s.setdefault("prev", {}).get(fam)
            if not prev or prev.get("lane") != d["lane"]:
                s["prev"][fam] = {"lane": d["lane"], "since": t}
        if sum(1 for l in lanes.values() if l.family == fam) > 1:
            update_state(mark)
    if headless and d.get("action") == "stop":
        d.pop("ask_user", None)
    if new_offers:
        mark_offered(new_offers)
        d["notice"] = "not set up: " + ", ".join(new_offers) + " (doctor --lane NAME to set up, doctor --decline NAME to hide)"
    return d, lanes, warnings, t


def cmd_next(a, cfg):
    d, lanes, warnings, t = decision(a, cfg)
    d["needs_input"] = needs_input(lanes)
    if warnings:
        d["warnings"] = warnings
    if not a.verbose:       # the hot path stays small: reasoning and the lane table only on request
        d.pop("trace", None)
    else:
        d["lanes"] = {n: l.brief(t) for n, l in lanes.items()}
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
    """HEAD and current branch of cwd, or None when cwd is not a git work tree. Other refs and worktrees change while parallel slots work and land, so they are not compared."""
    def git(*args):
        r = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    if shutil.which("git") is None or git("rev-parse", "--is-inside-work-tree") is None:
        return None
    return {"head": (git("rev-parse", "HEAD") or "").strip(), "branch": (git("symbolic-ref", "-q", "--short", "HEAD") or "").strip()}


def git_changes(before, after):
    """-> list of human-readable differences; empty when the worker left HEAD and branch alone."""
    if before is None or after is None:
        return []
    out = []
    if before["branch"] != after["branch"]:
        out.append(f"branch {before['branch'] or '(detached)'} -> {after['branch'] or '(detached)'}")
    if before["head"] != after["head"]:
        out.append(f"HEAD {before['head'][:9]} -> {after['head'][:9]}")
    return out


# ----------------------------------------------------------------------------- shared by the claude -p launcher

PREAMBLE = ("You are a delegated worker. Do exactly the task in the brief. Do not delegate further and do not use the delegate skill. "
            + GIT_RULES + " Finish with a short report: what you changed or found, and the exit code and last lines of any check you ran.")


def find_key(quiet=False):
    k = os.environ.get("CLAUDE_CREDIT_WORKER_KEY", "").strip()
    if k:
        return k
    p = config_dir() / "claude-credit.key"
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


# ----------------------------------------------------------------------------- run: one step, end to end, one JSON answer

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


def child_env(extra=None):
    """The worker gets the caller's environment minus Claude, Anthropic and delegate variables, plus DELEGATE_DEPTH=1."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "ANTHROPIC_", "DELEGATE_"))}
    env["DELEGATE_DEPTH"] = "1"
    env.update(extra or {})
    return env


def run_proc(cmd, text, cwd, env, timeout, on_line=None):
    """-> (returncode, stdout, stderr, timed_out). Own process group; the timeout kills the group; stdin carries `text` then closes; UTF-8, errors replaced."""
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env, **kw)
    out, err = [], []

    def pump(stream, sink, cb):
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace")
            sink.append(line)
            if cb:
                cb(line)

    def feed():
        with contextlib.suppress(OSError):
            p.stdin.write(text.encode("utf-8"))
            p.stdin.close()
    threads = [threading.Thread(target=pump, args=(p.stdout, out, on_line), daemon=True),
               threading.Thread(target=pump, args=(p.stderr, err, None), daemon=True), threading.Thread(target=feed, daemon=True)]
    for th in threads:
        th.start()
    timed_out = False
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(p)
        p.wait()
    for th in threads:
        th.join(5)
    return p.returncode, "".join(out), "".join(err), timed_out


def find_transcript(ref, family):
    if not ref:
        return None
    import glob
    pattern = (str(Path.home() / ".claude" / "projects" / "*" / (ref + ".jsonl")) if family == "claude"
               else str(Path.home() / ".codex" / "sessions" / "**" / ("*" + ref + ".jsonl")))
    hits = glob.glob(pattern, recursive=True)
    return hits[0] if hits else None


def parse_data(text, schema):
    if not schema or not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def mark_out(lane, until, why, t):
    update_state(lambda s: s.setdefault("out", {}).__setitem__(lane, {"until": until, "why": why, "at": t}))


def launch_codex(d, brief, a, rid, t, meta):
    """-> dict of result fields."""
    h = d["how"]
    exe = shutil.which("codex")
    if not exe:
        return {"status": "error", "say": "codex CLI not found on PATH (route.py doctor --lane OPENAI_SUBSCRIPTION)"}
    out_file = config_dir() / f"codex-last-{rid[:8]}.txt"
    if a.resume:
        sandbox = "workspace-write" if d["mode"] == "write" else "read-only"
        cmd = [exe, "exec", "resume", "-m", h["model"], "-c", f"model_reasoning_effort={d['effort']}", "-c", f'sandbox_mode="{sandbox}"']
        cmd += [x for x in h["cli"] if x == "--skip-git-repo-check"] + ["--json", "-o", str(out_file)]
        cmd += (["--output-schema", a.schema] if a.schema else []) + [a.resume, "-"]
    else:
        cmd = [exe] + h["cli"][1:] + ["--json", "-o", str(out_file)] + (["--output-schema", a.schema] if a.schema else []) + ["-"]
    seen = {"ref": a.resume, "last": ""}

    def on_line(line):
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        if ev.get("type") == "thread.started" and ev.get("thread_id"):
            seen["ref"] = ev["thread_id"]
            if agent_runs:
                agent_runs.run(rid, thread=seen["ref"])
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            seen["last"] = item.get("text", "")
    if agent_runs:
        agent_runs.run(rid, kind="codex", lane=d["lane"], model=d["model"], cwd=h["cwd"], started=t, status="running", **meta)
    rc, out, err, timed_out = run_proc(cmd, GIT_RULES + "\n\n" + brief, h["cwd"], child_env(), a.timeout, on_line)
    text = ""
    with contextlib.suppress(OSError):
        text = out_file.read_text(encoding="utf-8", errors="replace").strip()
        out_file.unlink()
    text = text or seen["last"]
    res = {"text": text, "ref": seen["ref"], "stderr": err}
    if timed_out:
        res.update(status="timeout", say=f"codex did not finish within {a.timeout}s and was killed")
    elif rc != 0 and LIMIT_RE.search(text + err):
        until = t + 3600
        mark_out(d["lane"], until, "limit refusal: " + (err or text)[:120].replace("\n", " "), t)
        res.update(status="lane_out", retry_after=until, say=f"{d['lane']} hit a limit; out until {local(until)}")
    elif rc != 0:
        res.update(status="failed", say=(err or text)[-300:].strip())
    else:
        update_state(lambda s: s.setdefault("out", {}).pop(d["lane"], None))
        res.update(status="done", data=parse_data(text, a.schema))
    return res


def launch_claude(d, brief, a, rid, t, cfg, meta):
    """claude -p on the credit key (CLAUDE_CREDIT) or the logged-in subscription (CLAUDE_SUBSCRIPTION, headless). -> dict of result fields."""
    h = d["how"]
    credit = cfg["lanes"][d["lane"]]["kind"] == "credit"
    exe = shutil.which("claude")
    if not exe:
        return {"status": "error", "say": "claude CLI not found on PATH"}
    extra, cap, nxt = {}, float(h.get("cap") or 2), None
    if credit:
        key = find_key()
        if not key:
            return {"status": "error", "say": "no API key for the credit lane: route.py doctor --lane CLAUDE_CREDIT (never paste the key in chat)"}
        c = cfg["settings"]["credit"]
        start, nxt = credit_cycle(c["reset_day"], t)
        if ledger_spend(start) + cap > float(c["monthly_cap"]):
            return {"status": "lane_out", "retry_after": nxt, "say": f"cycle cap: ${ledger_spend(start):.2f} spent + ${cap:.2f} > ${c['monthly_cap']}"}
        extra["ANTHROPIC_API_KEY"] = key
    write = bool(h["write"])
    tools = ["Read", "Glob", "Grep"] + (["Edit", "Write", "Bash"] if write else [])
    cmd = [exe, "-p", "--output-format", "json", "--model", h["model"], "--effort", d["effort"]]
    cmd += ["--max-budget-usd", str(cap)] if credit else []
    cmd += ["--resume", a.resume] if a.resume else ["--session-id", rid]
    cmd += ["--append-system-prompt", PREAMBLE, "--permission-mode", "acceptEdits" if write else "default",
            "--allowedTools", *tools, "--disallowedTools", *GIT_WRITE_DENY]
    if a.schema:
        cmd += ["--json-schema", Path(a.schema).read_text(encoding="utf-8")]
    ref = a.resume or rid
    if agent_runs:
        agent_runs.run(rid, kind="claude-p", session=ref, lane=d["lane"], model=d["model"], cwd=h["cwd"], started=t, status="running", **meta)
    rc, out, err, timed_out = run_proc(cmd, brief, h["cwd"], child_env(extra), a.timeout)
    try:
        j = json.loads(out)
    except ValueError:
        j = {}
    if not isinstance(j, dict):
        j = {}
    text = j.get("result") if isinstance(j.get("result"), str) else out.strip()
    cost = float(j.get("total_cost_usd") or 0) if credit else 0.0
    if credit and cost > 0:
        ledger_add(cost, d["model"], t)
    res = {"text": text, "ref": j.get("session_id") or ref, "cost": cost, "stderr": err}
    if timed_out:
        res.update(status="timeout", say=f"claude did not finish within {a.timeout}s and was killed")
    elif rc != 0 or j.get("is_error"):
        blob = text + err
        if credit and re.search(r"credit balance (is )?too low", blob, re.I):
            mark_out(d["lane"], nxt, "credit balance too low", t)
            res.update(status="lane_out", retry_after=nxt, say=f"credit exhausted; out until {local(nxt)}")
        elif LIMIT_RE.search(blob):
            until = t + 3600
            mark_out(d["lane"], until, "limit refusal: " + blob[:120].replace("\n", " "), t)
            res.update(status="lane_out", retry_after=until, say=f"{d['lane']} hit a limit; out until {local(until)}")
        else:
            res.update(status="failed", say=blob[-300:].strip())
    else:
        update_state(lambda s: s.setdefault("out", {}).pop(d["lane"], None))
        data = j.get("structured_output") if isinstance(j.get("structured_output"), (dict, list)) else parse_data(text, a.schema)
        res.update(status="done", data=data)
    return res


def result(status, **kw):
    r = {"v": 1, "status": status, "data": None, "text": "", "ref": None, "lane": None, "model": None, "effort": None, "family": None,
         "cost": 0, "secs": 0, "run_id": None, "transcript": None, "retry_after": None, "say": "", "git_violation": []}
    r.update(kw)
    return r


def emit(r):
    """One object on stdout. A model reads it when a Claude session is behind us, so empty fields are dropped there; scripts get every field."""
    if interactive():
        r = {k: v for k, v in r.items() if k in ("v", "status") or v not in (None, "", [], 0)}
    print(json.dumps(r))
    return 0        # exit 0 whenever the object was printed; non-zero only if route.py itself crashed


def cmd_run(a, cfg):
    headless = not interactive()
    d, lanes, warnings, t = decision(a, cfg)
    extra = {}
    if warnings:
        extra["warnings"] = warnings
    if d.get("notice"):
        extra["notice"] = d["notice"]
    if d["action"] == "error":
        return emit(result("error", say=d["error"], **extra))
    if d["action"] == "lane_out":
        return emit(result("lane_out", lane=d["lane"], retry_after=d.get("retry_after"), say=d["say"], **extra))
    if d["action"] == "stop":
        retry = min([l.out_until for l in lanes.values() if l.out and l.out_until] or [None], key=lambda x: (x is None, x))
        if not headless and d.get("ask_user"):
            extra["ask_user"] = d["ask_user"]
        return emit(result("stop", say=d["say"], retry_after=retry, **extra))
    h = d["how"]
    fam = cfg["lanes"][d["lane"]]["family"]
    who = dict(lane=d["lane"], model=d["model"], effort=d["effort"], family=fam)
    if not headless:
        ni = [n["what"] for n in needs_input(lanes)]
        if ni:
            extra["needs_input"] = ni
    if h["kind"] == "agent_tool":
        return emit(result("agent", **who, agent_model=h["model"], brief_prefix=GIT_RULES, cwd=h["cwd"], **extra))
    brief = Path(a.brief).read_text(encoding="utf-8", errors="replace") if a.brief else sys.stdin.read()
    if not brief.strip():
        return emit(result("error", **who, say="empty brief: pass it on stdin or with --brief FILE", **extra))
    cwd = h["cwd"]
    rid = str(uuid.uuid4())
    meta = {"parent_session": a.parent or "", "title": a.title or brief.strip().splitlines()[0][:120]}
    if a.task:
        meta["task"] = a.task
    git_before = git_state(cwd)
    t0 = time.time()
    try:
        res = launch_codex(d, brief, a, rid, t, meta) if h["kind"] == "codex" else launch_claude(d, brief, a, rid, t, cfg, meta)
    except Exception as e:      # a launcher bug is an error object, not a stack trace
        res = {"status": "error", "say": f"{type(e).__name__}: {e}"}
    err = res.pop("stderr", "")
    if err:
        print(err[-2000:], file=sys.stderr)
    if agent_runs:
        agent_runs.run(rid, ended=now(), status="completed" if res["status"] == "done" else "failed", transcript=find_transcript(res.get("ref"), fam))
    if res.get("ref") and res["status"] == "done":
        rec = {"lane": d["lane"], "model": d["model"], "effort": d["effort"], "mode": d["mode"], "cap": d.get("cap")}

        def keep(s):
            refs = s.setdefault("refs", {})
            refs[res["ref"]] = rec
            for k in list(refs)[:-200]:
                refs.pop(k)
        update_state(keep)
    res.update(**who, run_id=rid, secs=round(time.time() - t0, 1), git_violation=git_changes(git_before, git_state(cwd)),
               transcript=find_transcript(res.get("ref"), fam), **extra)
    return emit(result(res.pop("status"), **res))


# ----------------------------------------------------------------------------- onboarding commands (code in onboard.py)

def cmd_doctor(a, cfg):
    """The one setup command: report, then --fix / --decline / --undecline / --ack / --next act on what it reported."""
    import onboard
    R = sys.modules[__name__]
    if a.undecline:
        if profile_lanes().get(a.undecline) == "declined":
            set_profile_lane(a.undecline, None)
        print(json.dumps({"done": True, "say": f"{a.undecline} back in the list."}))
        return 0
    if a.ack or a.unack:
        set_ack(a.ack or a.unack, bool(a.ack))
        print(json.dumps({"acknowledged": sorted(acked())}))
        return 0
    if a.decline or a.fix:
        out = onboard.act(R, cfg, do=a.fix, decline=a.decline, open_browser=a.open_browser)
        print(json.dumps(out, indent=1))
        return 1 if out.get("error") else 0
    d = onboard.doctor(R, cfg, deep=a.deep, lane=a.lane)
    if a.next:
        step = onboard.next_step(d)
        print(json.dumps(step if step and "optional_only" not in step else {"step": None, "say": "READY", "lanes": d["lanes"]}, indent=1))
        return 0
    if a.json:
        print(json.dumps(d, indent=1))
    else:
        onboard.print_doctor(d, a.quiet)
    return 0 if d["ready"] else 1


def cmd_set_key(a, cfg):
    import onboard
    R = sys.modules[__name__]
    if a.web:
        return onboard.set_key_web(R, host=a.host, port=a.port, timeout=a.timeout, force=a.force)
    return onboard.set_key(R, from_stdin=a.stdin, force=a.force)


def cmd_config(a, cfg):
    import onboard
    R = sys.modules[__name__]
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
    n.add_argument("--claude-usage"); n.add_argument("--cwd"); n.add_argument("--verbose", action="store_true")
    r = sub.add_parser("report"); r.add_argument("--lane", required=True); r.add_argument("--event", choices=["out", "ok"], required=True)
    r.add_argument("--until"); r.add_argument("--message")
    cl = sub.add_parser("capture-limits"); cl.add_argument("--print", action="store_true")
    dr = sub.add_parser("doctor"); dr.add_argument("--quiet", action="store_true"); dr.add_argument("--deep", action="store_true"); dr.add_argument("--json", action="store_true")
    dr.add_argument("--lane"); dr.add_argument("--next", action="store_true"); dr.add_argument("--fix"); dr.add_argument("--decline"); dr.add_argument("--undecline")
    dr.add_argument("--ack"); dr.add_argument("--unack"); dr.add_argument("--open-browser", action="store_true")
    sk = sub.add_parser("set-key"); sk.add_argument("--stdin", action="store_true"); sk.add_argument("--force", action="store_true")
    sk.add_argument("--web", action="store_true"); sk.add_argument("--host", default="127.0.0.1"); sk.add_argument("--port", type=int, default=0); sk.add_argument("--timeout", type=int, default=300)
    cf = sub.add_parser("config"); cf.add_argument("action", choices=["set", "show"]); cf.add_argument("key", nargs="?"); cf.add_argument("value", nargs="?")
    ru = sub.add_parser("run"); ru.add_argument("role"); ru.add_argument("--writer"); ru.add_argument("--failed", default="")
    ru.add_argument("--premium", nargs="?", const="claude", metavar="FAMILY"); ru.add_argument("--allow-degraded", action="store_true")
    ru.add_argument("--claude-usage"); ru.add_argument("--cwd"); ru.add_argument("--brief"); ru.add_argument("--timeout", type=int, default=1800)
    ru.add_argument("--schema"); ru.add_argument("--resume"); ru.add_argument("--cap", type=float); ru.add_argument("--parent"); ru.add_argument("--task"); ru.add_argument("--title")
    a = ap.parse_args(argv)
    cfg = load_routing()
    return {"status": cmd_status, "next": cmd_next, "report": cmd_report, "capture-limits": cmd_capture,
            "run": cmd_run, "doctor": cmd_doctor, "set-key": cmd_set_key, "config": cmd_config}[a.cmd](a, cfg)


if __name__ == "__main__":
    sys.exit(main())
