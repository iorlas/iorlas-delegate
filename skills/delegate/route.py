#!/usr/bin/env python3
"""delegate router: deterministic lane and step selection for an LLM orchestrator.

Python 3.8+, standard library only. Windows, macOS, Linux, Docker.
The model supplies only what the script cannot read itself; the script decides.

  route.py status [--claude-usage JSON|@file|-] [--json] [--cheap]
  route.py next ROLE [--authored-by openai|claude|xai] [--failed 0,1] [--premium [FAMILY]] [--allow-degraded]
                     [--claude-usage JSON|@file|-] [--cwd DIR] [--verbose]
  route.py run ROLE [--cwd DIR] [--brief FILE] [--schema FILE] [--resume REF] [--cap USD] [--timeout SEC] [--unattended] [--authored-by W] [--failed I,J] [--premium] [--allow-degraded]
                    [--claude-usage JSON] [--parent SESSION] [--task ID] [--title T]    brief on stdin; one JSON object on stdout, exit 0 (see docs/dev/integrations.md)
  route.py net [--lane L] [--host H ...]    is the machine online? stdlib sockets, no quota, answers in under 3 s: {up, why, hosts}
  route.py report --lane LANE --event out|ok [--until EPOCH|ISO] [--message TEXT]
  route.py capture-limits [--print]  (status line JSON on stdin)
  route.py doctor [--lane L] [--next] [--fix ID] [--decline L] [--undecline L] [--ack ID] [--deep] [--json] [--quiet]   the one setup command: what each lane needs, and how to act on it
  route.py set-key [--stdin] [--force] | --web [--host IP|tailscale] [--port N] [--timeout S]   the key is handed over by the USER (terminal prompt or one-shot page); the agent never handles it
  route.py config set KEY VALUE | show
  route.py hold LANE --until EPOCH|ISO|+2h|+3d [--roles worker,hard] [--reason TEXT] | release ID|LANE | holds   an owner hold stops a lane for some roles; --until is mandatory

Data: routing.json next to this file (lanes, models, roles, thresholds).
Per-user settings: <config dir>/config.json (same keys as "settings"); profile, state, ledger and key live in <config dir>:
  $DELEGATE_CONFIG_DIR, else Claude Code's plugin data dir (~/.claude/plugins/data/<plugin>-<marketplace>); a checkout must set the variable.
Workers never create branches or worktrees and never commit, merge, push or stash: the coordinator owns git.
"""
import argparse
import contextlib
import copy
import datetime as dt
import json
import os
import random
import re
import shutil
import shlex
import signal
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
from lanes import classify, health, live as livemod, supervisor      # the one outcome ladder (delegate-e05); the health fold and breaker (rdt, a9x); the idle watchdog (go0)
from lanes import netprobe, profiles, usage as accounting                # network facts (4n7); the lane adapter registry (dlz)
from lanes import cockpit as role_view              # roles{} for status (0sf)
WEEK_MIN = 7 * 24 * 60


# ----------------------------------------------------------------------------- time, paths, config

def now():
    v = os.environ.get("DELEGATE_NOW")
    return float(v) if v else time.time()


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
    """Where settings, profile, state, ledger and the key live: DELEGATE_CONFIG_DIR, else Claude Code's data dir for the installed plugin.
    CLAUDE_PLUGIN_DATA is deliberately not read (it names whichever plugin launched the process). A checkout has no default: two places would split the state."""
    explicit = os.environ.get("DELEGATE_CONFIG_DIR")
    p = Path(explicit) if explicit else plugin_data_dir()
    if p is None:
        sys.exit("route.py: not running from an installed plugin; set DELEGATE_CONFIG_DIR to the folder for settings and state")
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
def locked(path, timeout=10.0):
    lock = str(path) + ".lock"
    end = time.time() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):      # Windows answers PermissionError while the previous holder's lock file is being deleted
            try:
                if time.time() - os.path.getmtime(lock) > 30:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.time() > end:
                raise TimeoutError(f"{path} is locked by another route.py for more than {timeout:.0f}s")
            time.sleep(0.05)
    try:
        yield
    finally:
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
    for i in range(50):         # Windows refuses to replace a file another process has open for reading
        try:
            os.replace(tmp, p)
            break
        except PermissionError:
            if i == 49:
                raise
            time.sleep(0.02)
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


UNATTENDED = False     # set by `run --unattended`: the caller is an engine, not a Claude session


def interactive():
    """A Claude session is behind this process (the Agent tool, get_usage and the user exist) unless the caller said --unattended."""
    return not UNATTENDED


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


def ledger_last_ts():
    try:
        with open(ledger_path(), encoding="utf-8") as f:
            return json.loads(f.read().splitlines()[-1])["ts"]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


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
        self.out_reason = None    # why it is out, for scripts: limit, logged_out, busy, error
        self.read_at = None       # when the usage reading was taken (Codex only)
        self.busy = False         # out only because its slots are taken (mark_busy): it frees up by itself in minutes
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
        self.windows = []         # every window the vendor exposes, for the cockpit: {name, used, resets_at, span, source, at}
        self.spend_blocked = False  # the vendor says a spend or credit limit stopped it (not a usage window)
        self.warn = []            # cockpit warnings: a reading the owner should double-check (shown as `!` lines)
        self.held = None          # the owner's hold record covering the role being decided (decide sets it); usable() then refuses
        self.benched = {}         # model -> health view of a pair the breaker benched (lanes/health.py); usable(model=) then refuses
        self.canary = {}          # model -> True when the pair's bench ended and its next launch is the one canary before full traffic
        self.hview = {}           # model -> health view of every known pair, for status --json

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
        kill_tree(p)
        p.wait()
        for f in (p.stdin, p.stdout):
            with contextlib.suppress(OSError):
                f.close()
    if not r or "result" not in r:
        return None, (r or {}).get("error", {}).get("message", "codex gave no limits (logged in? run `codex login`)")
    return r["result"], None


def span_name(mins):
    return "5-hour" if mins <= 360 else "daily" if mins <= 1500 else "weekly" if mins <= 14400 else "monthly"


def add_window(lane, used, resets_at, span, source, at, bucket=None):
    if used is not None or resets_at is not None:
        lane.windows.append({"name": (bucket + " " if bucket else "") + span_name(span), "used": used, "resets_at": resets_at, "span": span, "source": source, "at": at})


def codex_buckets(res):
    """-> [(bucket id, label, bucket dict)]: the top-level rateLimits plus every rateLimitsByLimitId entry (per-model limits), each once."""
    rl = res.get("rateLimits") or {}
    out = {rl.get("limitId") or "codex": rl}
    for k, b in (res.get("rateLimitsByLimitId") or {}).items():
        out.setdefault(k, b)
    return [(k, b.get("limitName") or k, b) for k, b in out.items() if isinstance(b, dict)]


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
    lane.read_at = cached["at"] if cached and res is cached["res"] else t
    rl = res.get("rateLimits") or {}
    ws = []          # (bucket label, window): every window of every bucket, by length not by slot (Pro Lite sends its only, weekly, window as primary)
    for bid, label, b in codex_buckets(res):
        for w in (b.get("primary"), b.get("secondary")):
            if w:
                ws.append((None if bid == (rl.get("limitId") or "codex") else label, w))
    for label, w in ws:
        span = w.get("windowDurationMins") or 300
        add_window(lane, window(w.get("usedPercent"), w.get("resetsAt"), span, t)[0], w.get("resetsAt"), span, "live read (codex app-server)", lane.read_at, label)
    lane.spend_blocked = bool(rl.get("spendControlReached")) or bool(re.search(r"credit|spend", str(rl.get("rateLimitReachedType") or ""), re.I))
    short = [w for _, w in ws if (w.get("windowDurationMins") or 300) <= 1440]
    long_ = [w for _, w in ws if (w.get("windowDurationMins") or 300) > 1440]
    binding = lambda group: max(group, key=lambda w: w.get("usedPercent") or 0, default={})      # the window closest to its limit binds
    pr, se = binding(short), binding(long_)
    lane.five_reset, lane.week_reset = pr.get("resetsAt"), se.get("resetsAt")
    lane.five, _, _ = window(pr.get("usedPercent"), lane.five_reset, pr.get("windowDurationMins") or 300, t)
    lane.week, wmins, lane.pace = window(se.get("usedPercent"), lane.week_reset, se.get("windowDurationMins") or WEEK_MIN, t)
    apply_burn(lane, S, wmins)
    if rl.get("rateLimitReachedType") or res.get("ordinaryUsageAllowed") is False or (lane.five or 0) >= 98 or (lane.week or 0) >= 98:
        lane.out, lane.out_reason = True, "limit"
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


def claude_logged_out():
    """True only when `claude auth status` answers loggedIn false; no CLI or an unreadable answer is not a logout (an Agent-tool host may have no CLI)."""
    c = cached_auth("CLAUDE_SUBSCRIPTION", now())
    if c:
        return c["ok"] is False
    exe = shutil.which("claude")
    if not exe:
        return False
    try:
        r = run_tree([exe, "auth", "status"], 15)
        v = json.loads(r.stdout).get("loggedIn")
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return False
    if isinstance(v, bool):
        write_auth("CLAUDE_SUBSCRIPTION", v, "claude auth status", now())
    return v is False


def read_claude_subscription(lane, S, st, t, ai_usage=None):
    stale = S["usage_stale_minutes"] * 60
    usage = None
    src = None      # (where the numbers came from, when they were taken)
    if ai_usage:
        usage = normalize_usage(ai_usage)
        if usage:
            update_state(lambda s: s.__setitem__("claude_usage", {"at": t, "data": usage}))
            src = ("manual mark (get_usage passed in)", t)
    if usage is None:
        saved = st.get("claude_usage")
        if saved and t - saved.get("at", 0) <= stale:
            usage = saved["data"]
            src = ("manual mark (get_usage passed in)", saved["at"])
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
                    src = ("live read (status line)", float(d.get("updated", 0)))
        except (OSError, ValueError, TypeError):
            pass
    if claude_logged_out():
        lane.out, lane.out_reason = True, "logged_out"
        lane.notes.append("claude is logged out: run `claude auth login`")
    weekly_spec = S.get("claude_weekly_reset", "")
    if usage:
        f5, wk = usage.get("five_hour") or {}, usage.get("weekly") or {}
        lane.five_reset, lane.week_reset = f5.get("resets_at"), wk.get("resets_at")
        lane.five, _, _ = window(f5.get("used"), lane.five_reset, 300, t)
        lane.week, wmins, lane.pace = window(wk.get("used"), lane.week_reset, WEEK_MIN, t)
        if f5:
            add_window(lane, lane.five, lane.five_reset, 300, src[0], src[1])
        if wk:
            add_window(lane, lane.week, lane.week_reset, WEEK_MIN, src[0], src[1])
        apply_burn(lane, S, wmins)
        if (lane.five or 0) >= 98 or (lane.week or 0) >= 98:
            lane.out, lane.out_reason = True, "limit"
            lane.out_until = (lane.week_reset if (lane.week or 0) >= 98 else lane.five_reset) or t + 3600
    elif weekly_spec:
        nxt = next_weekly(weekly_spec, t)
        if nxt:
            lane.week_reset = nxt
            add_window(lane, None, nxt, WEEK_MIN, "manual mark (claude_weekly_reset in config)", None)
            lane.notes.append(f"weekly reset {local(nxt)} (config); use unknown")


def read_claude_credit(lane, S, st, t):
    c = S["credit"]
    start, nxt = credit_cycle(c["reset_day"], t)
    lane.cap = float(c["monthly_cap"])
    lane.spend = ledger_spend(start)
    lane.reset_in_days = (nxt - t) / 86400.0
    lane.week_reset = nxt
    add_window(lane, 100.0 * lane.spend / lane.cap if lane.cap else None, nxt, 43200, "estimated from history (this tool's ledger)", ledger_last_ts())
    out = (st.get("out") or {}).get(lane.name)
    if out and out.get("until", 0) > t:
        lane.out, lane.out_until, lane.out_reason = True, out["until"], out.get("reason", "error")
        lane.notes.append(out.get("why", "reported out"))
    elif lane.spend >= lane.cap:
        lane.out, lane.out_until, lane.out_reason = True, nxt, "limit"
        lane.notes.append("own cycle cap reached")


READERS = {"codex_app_server": read_codex, "claude_subscription": read_claude_subscription, "claude_credit": read_claude_credit,
           "none": lambda lane, S, st, t: None}      # no usage reading (Grok): only refusals mark it out


def gather(cfg, st, t, ai_usage=None, check_config=True):
    S = cfg["settings"]
    lanes = {}
    conf = {}
    hstate = health_state(cfg, t)
    if check_config:
        import onboard
        conf = onboard.configured_lanes(sys.modules[__name__], cfg)
    for name, c in cfg["lanes"].items():
        if name == "DEEPSEEK_FREE" and S.get("deepseek_free_enabled") is not True:
            continue
        if name == "ZAI_CODING_SUBSCRIPTION" and S.get("zai_coding_enabled") is not True:
            continue
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
        if o and o.get("reason", "limit") == "limit" and lane.read_at and lane.read_at > o.get("at", 0) and not lane.out \
                and (lane.five or 0) < 95 and (lane.week or 0) < 95:       # a reading after the refusal shows room: the out was wrong or is over
            update_state(lambda s_: s_.get("out", {}).pop(name, None))
            o = None
        if o and o.get("until", 0) > t and not lane.out:      # an out reported by the model or by a launcher
            lane.out, lane.out_until, lane.out_reason = True, o["until"], o.get("reason", "error")
            lane.notes.append(o.get("why", "reported out"))
        if mode == "drift":
            lane.out, lane.out_until, lane.out_reason = True, None, "logged_out"
            lane.notes.insert(0, "enabled but not ready (login or key gone?)")
        apply_health(lane, cfg, hstate, t)
        lanes[name] = lane
    return lanes


def health_state(cfg, t):
    """-> the health fold (lanes/health.py) over history.jsonl, continued from a cached checkpoint. The checkpoint is a cache: no file, a corrupt
    one, or another breaker policy means a fold of the whole history."""
    pol = health.policy(cfg["settings"])
    cp = config_dir() / "health-checkpoint.json"
    base = health.load(cp, pol)
    rows = health.read_rows(history_path(), base["ts"] if base else 0.0)
    state = health.fold(rows, base, pol)
    if rows:
        health.save(cp, state, pol)
    return state


def apply_health(lane, cfg, hstate, t):
    """Put the breaker's view of each (lane, model) pair on the lane: `benched` (refused), `canary` (the next launch is the probe), `hview` (status)."""
    pol = health.policy(cfg["settings"])
    for model, m in cfg["models"].items():
        if m.get("family") != lane.family:
            continue
        v = health.view(hstate, lane.name, model, t, pol)
        lane.hview[model] = v
        if v["state"] == "broken":
            lane.benched[model] = v
        elif v["canary"]:
            lane.canary[model] = True


# ----------------------------------------------------------------------------- the decision functions (pure)

def limits_for(lane, S, relaxed):
    """-> (weekly %, five-hour %, name): where the router stops using the lane. The strict pass uses the gate (a shared lane keeps the orchestrator's reserve), the relaxed pass the ceiling."""
    c = S["relaxed_ceiling"]
    if lane.shared:
        r = S["orchestrator_reserve"]
        wk, fv = (c["orchestrator_weekly"], c["orchestrator_five_hour"]) if relaxed else (r["weekly"], r["five_hour"])
        return wk, fv, "hard ceiling" if relaxed else "orchestrator reserve"
    g = S["worker_lane_gate"]
    wk, fv = (c["weekly"], c["five_hour"]) if relaxed else (g["weekly"], g["five_hour"])
    return wk, fv, "hard ceiling" if relaxed else "lane gate"


def usable(lane, S, relaxed, model=None):
    """-> (bool, reason). With `model`, a pair the breaker benched is refused too."""
    if lane.held:
        return False, hold_text(lane.held)
    if model and model in lane.benched:
        b = lane.benched[model]
        return False, f"{model} benched ({b['kind']}, {b['source']}) until {local(b['until'])}"
    if lane.out:
        return False, f"out until {local(lane.out_until)}" if lane.out_until else "out"
    wk, fv, why = limits_for(lane, S, relaxed)
    if lane.week is not None and lane.week >= wk:
        return False, f"weekly {lane.week:.0f}% >= {wk} ({why})"
    if lane.five is not None and lane.five >= fv:
        return False, f"5h {lane.five:.0f}% >= {fv} ({why})"
    return True, "ok"


def back_at(lane, S):
    """When an unusable lane is expected back: its out-until, else the reset of the window it is over; None when usable or unknown."""
    ok, why = usable(lane, S, True)
    if ok:
        return None
    if lane.held:
        return lane.held.get("until")
    if lane.out:
        return lane.out_until
    return lane.week_reset if why.startswith("weekly") else lane.five_reset if why.startswith("5h") else None


def pick_lane(family, lanes, S, st, t, relaxed, trace, model=None):
    """Lane of `family` to use now for `model`, or None. Appends the reasoning to `trace`."""
    mine = [l for l in lanes.values() if l.family == family]
    ok = []
    for l in mine:
        good, why = usable(l, S, relaxed, model)
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


LATE_FLAGS = ("degraded", "same_family")


# ----------------------------------------------------------------------------- owner holds (delegate-dns)

LEGACY_HOLD = "flag:codex_plan_review_only"      # the config flag read as a hold with no expiry: the only hold that may lack `until`; the config migration removes it


def real_hold(lane, role, st, t):
    """-> the owner's hold record that stops `role` on `lane` at time t, else None. A record is {id, lane, roles, set_by, set_at, until, reason}."""
    for h in (st.get("holds") or {}).values():
        if h.get("lane") == lane and (not h.get("roles") or role in h["roles"]) and h.get("until", 0) > t:
            return h
    return None


def held(lane, role, mode, cfg, st, t):
    """-> the hold that stops `role` (step mode read_only|write) on `lane` now, else None: a hold record, else the legacy codex_plan_review_only flag."""
    h = real_hold(lane, role, st, t)
    if h or not cfg["settings"].get("codex_plan_review_only") or (cfg["lanes"].get(lane) or {}).get("family") != "openai":
        return h
    if role in ("planner", "reviewer") and mode == "read_only":
        return None
    return {"id": LEGACY_HOLD, "lane": lane, "roles": [], "set_by": "config", "set_at": None, "until": None,
            "reason": "codex_plan_review_only is set in config.json: Codex plans and reviews read-only"}


def hold_text(h):
    return f"held: {h.get('set_by') or 'owner'} " + (f"until {local(h['until'])}" if h.get("until") else "with no expiry") + (f" ({h['reason']})" if h.get("reason") else "")


def refusal_fields(h, say=None):
    """The answer fields for a refused call under a hold: the lane is out for this role until the hold ends, never `retry_after = now + 1`."""
    return {"outcome": "held", "retry_after": h.get("until"), "say": say or f"{h['lane']} is on hold, {hold_text(h)}"}


HOLD_STALE_BELOW = 5.0      # a hold whose lane now shows less than this % used (the weekly window, else the 5 h) has outlived its reason: one ask


def add_hold(lane, until, reason, roles=None, set_by="owner", t=None, auto_release_below=None, release_below=None):
    """Record an owner hold. `until` is mandatory and must be in the future: a hold with no end is how codex_plan_review_only stayed on for 12 hours.
    `auto_release_below` (percent used): the hold ends by itself, with no ask, once the lane shows less than that. `release_below` (default 5)
    is the level at which a hold still in force raises its one owner ask."""
    t = now() if t is None else t
    end = parse_until(until, t)
    if end is None:
        raise ValueError("a hold needs --until (an epoch, an ISO time, or +2h / +3d): a hold with no end is refused")
    if end <= t:
        raise ValueError(f"--until {until} is in the past")
    if lane not in load_routing()["lanes"]:
        raise ValueError(f"unknown lane {lane}; lanes: {', '.join(load_routing()['lanes'])}")
    rec = {"id": uuid.uuid4().hex[:8], "lane": lane, "roles": sorted(roles or []), "set_by": set_by, "set_at": t, "until": end, "reason": reason or ""}
    for k, v in (("auto_release_below", auto_release_below), ("release_below", release_below)):
        if v is not None:
            rec[k] = float(v)
    update_state(lambda s: s.setdefault("holds", {}).__setitem__(rec["id"], rec))
    return rec


def parse_until(v, t):
    m = re.fullmatch(r"\+(\d+)([mhd])", str(v or "").strip())
    if m:
        return t + int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    with contextlib.suppress(TypeError, ValueError):
        v = float(v)      # the command line hands over a string
    return to_epoch(v)


def hold_usage(lane):
    """-> percent used of the lane's weekly window (else its 5 h one), None when unknown."""
    return None if lane is None else lane.week if lane.week is not None else lane.five


def settle_holds(lanes, st, t):
    """Holds meet the lane's reading. A hold with `auto_release_below` whose lane shows less than that is released, silently. A hold still in force
    whose lane shows less than its `release_below` (default 5 %) is stale: it gets ONE ask, stamped on the record (`stale_at`) so no later call
    raises it again; nothing is printed or pushed, the ask is a field of `status --json` (`asks`). -> (asks, state after)."""
    drop, stamp = [], []
    for k, h in (st.get("holds") or {}).items():
        used = hold_usage(lanes.get(h.get("lane")))
        if used is None or h.get("until", 0) <= t:
            continue
        if h.get("auto_release_below") is not None and used < float(h["auto_release_below"]):
            drop.append(k)
        elif used < float(h.get("release_below", HOLD_STALE_BELOW)) and not h.get("stale_at"):
            stamp.append(k)
    if drop or stamp:
        def apply(s_):
            for k in drop:
                (s_.get("holds") or {}).pop(k, None)
            for k in stamp:
                h_ = (s_.get("holds") or {}).get(k)
                if h_ and not h_.get("stale_at"):
                    h_["stale_at"] = t
        st = update_state(apply)
    asks = []
    for k, h in (st.get("holds") or {}).items():
        used = hold_usage(lanes.get(h.get("lane")))
        if h.get("stale_at") and h.get("until", 0) > t and used is not None and used < float(h.get("release_below", HOLD_STALE_BELOW)):
            asks.append({"kind": "hold_stale", "id": h["id"], "lane": h["lane"], "raised_at": h["stale_at"], "used_pct": used,
                         "say": f"hold {h['id']} on {h['lane']} (until {local(h['until'])}{', ' + h['reason'] if h.get('reason') else ''}) is still in force but the lane shows {used:.0f}% used",
                         "answer": f"route.py release {h['id']} to end it, or leave it until it expires"})
    return asks, st


def hold_rows(cfg, st, t):
    """Every hold now in force, with who set it and when it ends: for status --json and `holds`."""
    rows = [dict(h, ends_in_s=int(h["until"] - t), stale=bool(h.get("stale_at"))) for h in (st.get("holds") or {}).values() if h.get("until", 0) > t]
    if cfg["settings"].get("codex_plan_review_only"):
        rows.append(dict(held("OPENAI_SUBSCRIPTION", "worker", "write", cfg, {}, t) or {}, ends_in_s=None))
    return sorted(rows, key=lambda h: (h.get("until") or 1e18))


def cmd_hold(a, cfg):
    try:
        rec = add_hold(a.lane, a.until, a.reason, [x for x in (a.roles or "").split(",") if x], a.set_by,
                       auto_release_below=a.auto_release_below, release_below=a.release_below)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps({"ok": True, "hold": rec}))
    return 0


def cmd_release(a, cfg):
    gone = []

    def drop(st):
        for k, h in list((st.get("holds") or {}).items()):
            if a.target in (k, h.get("lane")):
                gone.append(st["holds"].pop(k))
    update_state(drop)
    print(json.dumps({"ok": bool(gone), "released": gone}))
    return 0 if gone else 1


def cmd_holds(a, cfg):
    print(json.dumps({"holds": hold_rows(cfg, load_state(), now())}))
    return 0


def migrate_legacy_hold(t):
    """codex_plan_review_only in config.json becomes a hold record that ends in 7 days, and the flag is removed from the file."""
    if not read_config().get("codex_plan_review_only"):
        if "codex_plan_review_only" in read_config():
            update_config(lambda c: c.pop("codex_plan_review_only", None))
        return None
    rec = add_hold("OPENAI_SUBSCRIPTION", "+7d", "migrated from codex_plan_review_only", ["worker", "hard", "light"], "config", t)
    update_config(lambda c: c.pop("codex_plan_review_only", None))
    return rec


def codex_step_allowed(role_name, step, cfg):
    """A paused Codex writer may only plan/review, and always under read-only access."""
    if not cfg["settings"].get("codex_plan_review_only"):
        return True
    family = (cfg["models"].get(step.get("model")) or {}).get("family") or step.get("on")
    return family != "openai" or (role_name in ("planner", "reviewer") and step.get("mode", "read_only") == "read_only")


def loop_barred(step, role, cfg):
    """Why the loop never runs this step, else None: its model is explicit-call-only (models[m].loop false, Opus), or the role bars its family (reviewer: xai)."""
    m = cfg["models"].get(step.get("model")) or {}
    if m.get("loop") is False:
        return f"{step.get('model')} is never routed in the loop (explicit user call only)"
    fam = m.get("family") or step.get("on")
    if fam in (role.get("exclude_families") or []):
        return f"{fam} is excluded from this role"
    return None


def build_tiers(role, cfg, args, role_name=None):
    """-> list of tiers; each tier is a list of steps tried strict then relaxed before the next tier."""
    steps = list(role.get("steps") or [])
    if "by_author" in role:
        w = args.get("authored_by") or "unknown"
        if w not in role["by_author"]:
            return None
        steps = list(role["by_author"][w])
    if cfg["settings"].get("deepseek_free_enabled") is not True:
        steps = [s for s in steps if s.get("on") != "deepseek"]
    if cfg["settings"].get("zai_coding_enabled") is not True:
        steps = [s for s in steps if s.get("on") != "zai"]
    prem = args.get("premium")
    if prem:
        step = (cfg.get("premium") or {}).get(prem)
        if not step:
            return None
        steps = [dict(step)] + steps
    if role_name is None:
        role_name = next((name for name, value in cfg["roles"].items() if value == role), None)
    steps = [s for s in steps if codex_step_allowed(role_name, s, cfg) and not loop_barred(s, role, cfg)]
    if role.get("order") == "cost":         # cheapest configured model first inside a tier (stable: ties keep the listed order)
        steps.sort(key=lambda s: (cfg["models"].get(s.get("model")) or {}).get("cost", 1000))
    steps = [dict(s, idx=i) for i, s in enumerate(steps)]
    if role["advance"] == "on_failure":
        failed = args.get("failed") or []
        start = (max(failed) + 1) if failed else 0
        steps = steps[start:]
    # Order of tiers (each tried strict, then relaxed): a requested premium step, then every non-Claude step, then every Claude step;
    # within each, unflagged steps before LATE_FLAGS ones. Claude lanes come last for every role (bead delegate-m86).
    first = [s for s in steps if s.get("flag") == "user_requested_premium"]
    rest = [s for s in steps if s not in first]
    tiers = [first]
    for claude in (False, True):
        part = [s for s in rest if (s.get("on") == "claude") == claude]
        tiers += [[s for s in part if s.get("flag") not in LATE_FLAGS], [s for s in part if s.get("flag") in LATE_FLAGS]]
    tiers = [x for x in tiers if x]
    if args.get("allow_degraded") and role.get("degraded_needs_consent"):
        tiers.append([dict(s, flag="degraded_with_consent", idx=len(steps) + i) for i, s in enumerate(role["degraded_needs_consent"]) if codex_step_allowed(role_name, s, cfg) and not loop_barred(s, role, cfg)])
    return tiers


def family_state(fam, lanes, S):
    """Why no lane of `fam` is usable, in a few words: 'xai out until Sat 24 Oct 18:00', 'claude logged out', 'claude over its limit', 'claude not set up'."""
    mine = [l for l in lanes.values() if l.family == fam]
    if not mine:
        return f"{fam} not set up"
    l = mine[0]
    if l.out:
        if l.out_reason == "logged_out":
            return f"{fam} logged out"
        return f"{fam} out" + (f" until {local(l.out_until)}" if l.out_until else "")
    return f"{fam} over its limit"


def review_warning(flag, step, author, lanes, S, cfg):
    """One line for a same_family or degraded step; None for any other step."""
    if flag not in LATE_FLAGS:
        return None
    fam = step["on"]
    others = [f for f in dict.fromkeys(c["family"] for c in cfg["lanes"].values()) if f != fam]
    down_fams = [f for f in others if not any(usable(l, S, True)[0] for l in lanes.values() if l.family == f)]
    down = "; ".join(family_state(f, lanes, S) for f in down_fams)
    if flag == "same_family":
        spare = [f for f in others if f not in down_fams]
        return (f"reviewing {author or 'unknown'}'s work with {fam}: " + (f"{', '.join(spare)} is kept as the last resort" if spare else "no other family is up")
                + (f" ({down})" if down else ""))
    return f"degraded: {step['model']} {step['effort']} on {fam} because the better options are down" + (f" ({down})" if down else "")


def decide(role_name, cfg, lanes, st, t, args):
    """Pure: no I/O. Returns the decision dict."""
    S = cfg["settings"]
    role = cfg["roles"].get(role_name)
    if role is None:
        return {"action": "error", "error": f"unknown role {role_name}; roles: {', '.join(cfg['roles'])}"}
    tiers = build_tiers(role, cfg, args, role_name)
    if tiers is None:
        if args.get("premium") and not (cfg.get("premium") or {}).get(args["premium"]):
            return {"action": "error", "error": f"no premium model configured for family {args['premium']}; premium: {[k for k, v in (cfg.get('premium') or {}).items() if v and k != 'about']}"}
        return {"action": "error", "error": f"--authored-by must be {' or '.join(k for k in role['by_author'] if k != 'unknown')} (or omitted)"}
    trace = []
    for l in lanes.values():
        l.held = real_hold(l.name, role_name, st, t)
    steps_ = [x for tier in tiers for x in tier]
    first = next((x for x in steps_ if any(l.family == x["on"] for l in lanes.values())), steps_[0] if steps_ else None)      # the role's first choice among lanes that are set up
    mine = [l for l in lanes.values() if first and l.family == first["on"]]

    def free_ok(l):         # usable if its slots were free
        o, l.out = l.out, False
        try:
            return usable(l, S, True)[0]
        finally:
            l.out = o
    busy = [l for l in mine if l.busy and free_ok(l)]
    if busy and args.get("wait_busy") and not any(usable(l, S, True)[0] for l in mine):
        until = min(l.out_until for l in busy)      # the first choice is only waiting for a slot: a minute beats a round on the next rung
        return {"action": "stop", "reason": "busy", "lane": busy[0].name, "retry_after": until,
                "say": f"{busy[0].name} (the role's first choice) has no free slot: {busy[0].notes[0]}", "trace": trace}
    why_not_first = "busy" if busy else "out" if any(l.out for l in mine) else "ceiling"
    first_lane = next((l.name for l in mine if l.kind == "subscription"), mine[0].name if mine else None)
    for tier in tiers:
        for relaxed in (False, True):
            for step in tier:
                lane = pick_lane(step["on"], lanes, S, st, t, relaxed, trace, step["model"])
                if lane is None:
                    continue
                fb = step is not first
                warn = review_warning(step.get("flag"), step, args.get("authored_by"), lanes, S, cfg)
                return {"action": "run", "role": role_name, "pass": "relaxed" if relaxed else "strict",
                        "rung": step.get("idx"), "fallback": fb, "fallback_reason": why_not_first if fb else None, "first_lane": first_lane if fb else None,
                        "lane": lane.name, "model": step["model"], "effort": step["effort"], "mode": step.get("mode", "read_only"),
                        "cap": step.get("cap"), "flag": step.get("flag"),
                        "how": how(lane, step, cfg, args.get("cwd"), args.get("unattended", False)), "trace": trace,
                        **({"warning": warn} if warn else {})}
    resets = {l.name: local(l.out_until) for l in lanes.values() if l.out and l.out_until}
    resets.update({f"{l.name}/{m}": local(b["until"]) for l in lanes.values() for m, b in l.benched.items()})
    stop = {"action": "stop", "role": role_name, "say": role["stop"], "resets": resets, "trace": trace}
    barred = sorted({why for x in [*(role.get("steps") or []), *(sx for v in (role.get("by_author") or {}).values() for sx in v)] if (why := loop_barred(x, role, cfg))})
    if barred and not tiers:
        stop["reason"] = "loop_excluded"
        stop["say"] = f"{role['stop']} Nothing is left to route: " + "; ".join(barred) + "."
    consent = [s for s in role.get("degraded_needs_consent") or [] if not loop_barred(s, role, cfg)]
    if consent and not args.get("allow_degraded"):
        stop["ask_user"] = {"question": "No adequate model is available. Run a degraded option?",
                            "options": [f"{s['model']} {s['effort']}" for s in consent if not loop_barred(s, role, cfg)],
                            "then": f"route.py next {role_name} --allow-degraded"}
    return stop


# Every codex run and resume gets these, so neither the user's config nor a repo file widens or narrows them. The sandbox has no network and
# writes only the working directory; a command it blocks may ask to run outside it, and Codex's automatic reviewer (a model, no human)
# approves or refuses that one command. Codex permission profiles would also block reads (~/.ssh), but an approved escalation there
# stays inside the sandbox, so Unity cannot run (codex-cli 0.162.1).
CODEX_ACCESS = ["--ignore-user-config", "--ignore-rules", "-c", "sandbox_workspace_write.network_access=false",
                "-c", "sandbox_workspace_write.writable_roots=[]", "-c", "approval_policy=on-request", "-c", "approvals_reviewer=auto_review"]


def repo_dirs(cwd):
    """The folders from the repo root (or cwd outside a repo) down to cwd."""
    wd = Path(cwd).resolve()
    top = subprocess.run(["git", "-C", str(wd), "rev-parse", "--show-toplevel"], capture_output=True, text=True) if shutil.which("git") else None
    root = Path(top.stdout.strip()).resolve() if top is not None and top.returncode == 0 and top.stdout.strip() else wd
    out = [wd]
    while out[-1] != root and out[-1].parent != out[-1] and root in out[-1].parents:
        out.append(out[-1].parent)
    return out[::-1]


def repo_instructions(cwd):
    """The repo's instruction files, the same for both lanes (Codex's own rule): in each folder from the root down to cwd, AGENTS.md, else CLAUDE.md."""
    found = []
    for d in repo_dirs(cwd):
        f = next((d / n for n in ("AGENTS.md", "CLAUDE.md") if (d / n).is_file()), None)
        if f:
            found.append(f)
    return found


def codex_isolation(cwd):
    """Codex sees only what delegate sends plus the repo's instruction files: every skill it would list (its own system skills, the user's,
    the repo's .agents/skills) is turned off by path, and a repo with only CLAUDE.md is read as Claude would. Sub-agents cannot be turned off
    (codex 0.162.1: features.multi_agent=false and agents.max_depth=0 still let a brief that asks spawn one); NO_SUBAGENTS_RULE asks, and their
    tokens are counted as child threads."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    roots = [home / "skills", Path.home() / ".agents" / "skills"] + [d / x / "skills" for d in repo_dirs(cwd) for x in (".agents", ".codex")]
    paths = sorted({str(f.resolve()) for r in roots if r.is_dir() for f in r.rglob("SKILL.md")})
    return ["-c", "skills.config=[" + ",".join("{path=%s,enabled=false}" % json.dumps(x) for x in paths) + "]",
            "-c", 'project_doc_fallback_filenames=["CLAUDE.md"]']


def how(lane, step, cfg, cwd=None, unattended=False):
    """What a step runs on. Unattended callers have no Agent tool, so the subscription lane runs `claude -p` on the logged-in account."""
    m = cfg["models"][step["model"]]
    runner = cfg["lanes"][lane.name]["runner"]
    wd = os.path.abspath(cwd) if cwd else os.getcwd()
    write = step.get("mode") == "write"
    if runner == "agent_tool" and unattended:
        runner = "claude_p"
    if runner == "claude_p":
        return {"kind": "claude_p", "model": m["api"], "write": write, "cap": step.get("cap", 2), "cwd": wd, "read_only": not write}
    if runner == "agent_tool":
        return {"kind": "agent_tool", "model": m["agent"], "cwd": wd}
    if runner == "opencode":
        return {"kind": "opencode", "model": m["api"], "write": write, "cwd": wd}
    sandbox = "workspace-write" if write else "read-only"
    iso = codex_isolation(wd)
    cli = ["codex", "exec", "-C", wd, "-s", sandbox, "-m", m["api"], "-c", f"model_reasoning_effort={step['effort']}"] + CODEX_ACCESS + iso
    if git_state(wd) is None:
        cli.append("--skip-git-repo-check")      # codex refuses a directory that is not a git work tree
    return {"kind": "codex", "model": m["api"], "cli": cli, "cwd": wd, "sandbox": sandbox, "isolation": iso}


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


def finished_runs(n):
    """The last n finished runs, newest last: the dashboard's view of history.jsonl (the file itself stays private)."""
    keys = ("run_id", "ts", "role", "task", "lane", "model", "done", "outcome", "secs", "cost", "tokens", "denials")
    try:
        lines = history_path().read_text(encoding="utf-8").splitlines()[-n:]
        return [{k: json.loads(ln).get(k) for k in keys} for ln in lines if ln]
    except (OSError, ValueError):
        return []


def routing_snapshot(cfg, lanes, st, t):
    """Public read-only routing contract for dashboards; no vendor calls or worker launches."""
    current = copy.deepcopy(lanes)
    mark_busy(current, st, cfg["settings"], t)
    workers = {n: l for n, l in current.items() if l.kind != "credit"}
    inventory = []
    for name, c in cfg["lanes"].items():
        lane = current.get(name)
        available, why = usable(lane, cfg["settings"], True) if lane else (False, "disabled or not configured")
        label = c.get("label") or LABELS.get(name, name)
        if c["family"] == "openai" and cfg["settings"].get("codex_plan_review_only"):
            label += " (planning/review only)"
        inventory.append({"id": name, "label": label, "family": c["family"],
                          "billing": "free" if name == "DEEPSEEK_FREE" else c["kind"],
                          "enabled": lane is not None, "available": available, "reason": why,
                          "unattended": c["kind"] != "credit", "until": lane.out_until if lane else None,
                          "five": lane.five if lane else None, "week": lane.week if lane else None,
                          "models": [m for m, v in cfg["models"].items() if v["family"] == c["family"]]})

    def scenario(role_name, author=None):
        role = cfg["roles"][role_name]
        args = {"authored_by": author, "failed": [], "premium": None, "allow_degraded": False,
                "unattended": True, "cwd": None, "wait_busy": True}
        d = decide(role_name, cfg, workers, st, t, args)
        chain = []
        for tier in build_tiers(role, cfg, args, role_name) or []:
            for step in tier:
                chain.append({"model": step["model"], "family": step["on"], "effort": step["effort"],
                              "mode": step.get("mode", "read_only"), "flag": step.get("flag"),
                              "rung": step["idx"], "lanes": [n for n, c in cfg["lanes"].items()
                                                             if c["family"] == step["on"] and c["kind"] != "credit"]})
        keys = ("action", "lane", "model", "effort", "flag", "warning", "say", "reason", "rung", "pass", "fallback")
        return {"selected": {k: d[k] for k in keys if k in d}, "chain": chain}

    roles = []
    for name, role in cfg["roles"].items():
        item = dict(id=name, about=role.get("about", ""), advance=role.get("advance"), **scenario(name))
        if "by_author" in role:
            item["by_author"] = {who: scenario(name, who) for who in role["by_author"] if who != "unknown"}
        roles.append(item)
    return {"v": 1, "at": t, "caller": "unattended", "lanes": inventory, "roles": roles}


# ----------------------------------------------------------------------------- the cockpit: every lane, one row each

EMOJI = {"ready": "✅", "resting": "⏳", "logged_out": "🔑", "spend": "💸", "slow": "🐢", "off": "⛔", "unknown": "❓"}
LEGEND = {"ready": "ready", "resting": "usage limit", "logged_out": "logged out", "spend": "spend/credit limit",
          "slow": "slow/hung", "off": "disabled", "unknown": "no read yet"}
LABELS = {"CLAUDE_SUBSCRIPTION": "Claude subscription", "CLAUDE_CREDIT": "Claude API credits", "OPENAI_SUBSCRIPTION": "ChatGPT / Codex",
          "GROK_SUBSCRIPTION": "SuperGrok / OpenCode", "DEEPSEEK_FREE": "DeepSeek / OpenCode Free", "ZAI_CODING_SUBSCRIPTION": "Z.ai GLM Coding Plan"}
LOGIN_CMD = {"OPENAI_SUBSCRIPTION": "codex login", "CLAUDE_SUBSCRIPTION": "claude auth login", "CLAUDE_CREDIT": "route.py set-key",
             "GROK_SUBSCRIPTION": "opencode auth login", "ZAI_CODING_SUBSCRIPTION": "opencode auth login, choose Z.AI Coding Plan",
             "DEEPSEEK_FREE": "route.py doctor --lane DEEPSEEK_FREE"}
AUTH_TTL = 300          # a login check answers for this long: it spawns a CLI, cheap but not free
AUTH_TIMEOUT = 10
AUTH_FILE_TTL = 600     # auth-cache.json: written by a non-cheap status, doctor, a login probe and a successful run; read by next and status --cheap


def auth_cache_path():
    return config_dir() / "auth-cache.json"


def read_auth_cache():
    """-> {lane: {at, ok, why}}; empty when the file is missing or unreadable."""
    try:
        d = json.loads(auth_cache_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, SystemExit):
        return {}


def write_auth(name, ok, why, t):
    """Remember a login answer for AUTH_FILE_TTL. Never fails the caller."""
    if ok is None:
        return
    with contextlib.suppress(Exception):
        p = auth_cache_path()
        with locked(p):
            d = read_auth_cache()
            d[name] = {"at": t, "ok": bool(ok), "why": str(why)[:120]}
            tmp = p.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(d), encoding="utf-8")
            os.replace(tmp, p)
            private(p)


def cached_auth(name, t):
    """-> the fresh {at, ok, why} entry for a lane, or None (stale, missing, or DELEGATE_NOCACHE set)."""
    c = read_auth_cache().get(name)
    if isinstance(c, dict) and 0 <= t - c.get("at", 0) <= AUTH_FILE_TTL and "ok" in c and not os.environ.get("DELEGATE_NOCACHE"):
        return c
    return None
SLOW_WINDOW = 2 * 3600  # a hang or timeout this recent, with no good run on the lane since, keeps it marked slow
SLOW_KINDS = ("hung", "timeout")


def fmt_in(secs):
    """'2h 10m', '6d 21h', '45m'."""
    s = int(secs)
    if s < 60:
        return "<1m"
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return f"{d}d {h}h" if d else f"{h}h {m:02d}m" if h else f"{m}m"


def when(epoch, t):
    return f"{local(epoch)} (in {fmt_in(epoch - t)})"


def cli_login(exe_name, args, ok_if):
    """-> (True | False | None, why). Runs a vendor's own status command (no model call, no quota); None = could not tell."""
    exe = shutil.which(exe_name)
    if not exe:
        return None, f"{exe_name} CLI not installed"
    try:
        r = run_tree([exe] + args, AUTH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"{exe_name} login check failed: {type(e).__name__}"
    return ok_if(r), (r.stdout + r.stderr).strip()[:120]


def codex_login_ok(r):
    return r.returncode == 0 and "not logged" not in (r.stdout + r.stderr).lower()


def claude_login_ok(r):
    try:
        v = json.loads(r.stdout).get("loggedIn")
    except (ValueError, AttributeError):
        return None
    return v if isinstance(v, bool) else None


def opencode_auth_has(key, need_key=False):
    f = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "auth.json"
    try:
        v = json.loads(f.read_text(encoding="utf-8")).get(key)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return bool(v) and (not need_key or (isinstance(v, dict) and bool(v.get("key"))))


def probe_login(name):
    """-> (True | False | None, why): is the lane's login really there? A status command or a file, never a model call."""
    if name == "OPENAI_SUBSCRIPTION":
        return cli_login("codex", ["login", "status"], codex_login_ok)
    if name == "CLAUDE_SUBSCRIPTION":
        return cli_login("claude", ["auth", "status"], claude_login_ok)
    if name == "CLAUDE_CREDIT":
        return (True, "API key stored") if find_key(quiet=True) else (False, "no API key stored")
    if name in ("GROK_SUBSCRIPTION", "ZAI_CODING_SUBSCRIPTION", "DEEPSEEK_FREE"):
        if not opencode_exe():
            return None, "opencode CLI not installed"
        if name == "DEEPSEEK_FREE":
            return True, "free access needs no login"        # the zero-price check runs at launch
        key = "xai" if name == "GROK_SUBSCRIPTION" else "zai-coding-plan"
        ok = opencode_auth_has(key, need_key=name != "GROK_SUBSCRIPTION")
        return ok, f"{key} login in opencode's auth file" if ok else f"no {key} login in opencode's auth file"
    return None, "no check for this lane"


def login_state(name, st, t):
    """-> (ok, why, checked_at): probe_login cached a few minutes in state, so a status call costs one process per lane at most."""
    c = (st.get("auth") or {}).get(name)
    if c and t - c.get("at", 0) <= AUTH_TTL and not os.environ.get("DELEGATE_NOCACHE"):
        return c["ok"], c["why"], c["at"]
    ok, why = probe_login(name)
    if ok is not None:
        update_state(lambda s_: s_.setdefault("auth", {}).__setitem__(name, {"at": t, "ok": ok, "why": why}))
        write_auth(name, ok, why, t)
    return ok, why, t


def lane_model(cfg, name):
    """'model effort' the lane's family gets in the worker role (else the first role that uses the family, else its first model)."""
    fam = cfg["lanes"][name]["family"]
    for role in [cfg["roles"].get("worker")] + list(cfg["roles"].values()):
        for step in (role or {}).get("steps", []):
            if step.get("on") == fam:
                return f"{step['model']} {step['effort']}" + (" (planning/review only)" if fam == "openai" and cfg["settings"].get("codex_plan_review_only") else "")
    return next((m for m, v in cfg["models"].items() if v["family"] == fam), None)


def off_reason(name, cfg, login):
    flag = {"DEEPSEEK_FREE": "deepseek_free_enabled", "ZAI_CODING_SUBSCRIPTION": "zai_coding_enabled"}.get(name)
    if flag and cfg["settings"].get(flag) is not True:
        return f"disabled: `route.py config set {flag} true` turns it on"
    if profile_lanes().get(name) == "declined":
        return f"declined: `route.py doctor --undecline {name}` brings it back"
    if login[0] is False:
        return f"not configured: logged out, run `{LOGIN_CMD[name]}`"
    return f"not configured: `route.py doctor --lane {name}` sets it up" + (f" ({login[1]})" if login[0] is None and login[1] else "")


def classify_out(lane, rec):
    """Why a lane that is out is out -> logged_out | spend | slow | resting. rec is its state.json entry (older ones have no typed reason)."""
    r = lane.out_reason
    text = " ".join([(rec or {}).get("why", "")] + lane.notes)
    if r == "logged_out" or (r in (None, "error") and classify.LOGIN_RE.search(text)):
        return "logged_out"
    if lane.spend_blocked or classify.SPEND_RE.search(text) or (r == "limit" and lane.kind == "credit"):
        return "spend"
    if r == "hung":
        return "slow"
    return "resting"


def limit_words(text, lane):
    if re.search(r"free usage", text, re.I):
        return "free usage limit reached"
    for pat, word in ((r"week|7.?day", "weekly"), (r"5.?hour|five.?hour", "5-hour"), (r"daily|per day|\bday\b", "daily")):
        if re.search(pat, text, re.I):
            return f"{word} limit hit"
    top = max((w for w in lane.windows if w["used"] is not None), key=lambda w: w["used"], default=None)
    return f"{top['name']} limit hit" if top and top["used"] >= 95 else "usage limit hit"


def window_rows(lane, S, t):
    rows, low = [], []
    for w in lane.windows:
        used = w["used"]
        rows.append({"name": w["name"], "used_pct": None if used is None else round(used, 1), "resets_at": w["resets_at"],
                     "resets_in_s": None if not w["resets_at"] else max(0, int(w["resets_at"] - t)), "source": w["source"],
                     "age_s": None if w["at"] is None else max(0, int(t - w["at"]))})
        if used is None or lane.kind == "credit":
            continue
        (gwk, gfv, gname), (cwk, cfv, _) = limits_for(lane, S, False), limits_for(lane, S, True)
        gate, ceil = (gwk, cwk) if w["span"] > 1440 else (gfv, cfv)
        if used >= gate:
            low.append({"window": w["name"], "used_pct": round(used, 1), "limit": ceil if used >= ceil else gate,
                        "over": "ceiling" if used >= ceil else "gate", "limit_name": "hard ceiling" if used >= ceil else gname})
    return rows, low


def lane_view(name, cfg, lane, st, t, history=(), running=0, login=(None, "", None), off_why=None):
    """One lane's cockpit row. Pure given its inputs: lane is what gather built (None = disabled or not configured), history the recent finished runs."""
    S, c = cfg["settings"], cfg["lanes"][name]
    v = {"id": name, "label": c.get("label") or LABELS.get(name, name), "family": c["family"], "kind": c["kind"], "model": lane_model(cfg, name),
         "state": "off", "emoji": EMOJI["off"], "reason": off_why or "disabled or not configured", "detail": None,
         "unlock": {"locked": False, "known": False, "at": None, "in_s": None, "text": None, "why": "not in use"},
         "windows": [], "low_tokens": [], "warnings": list(lane.warn) if lane else [], "meter": c["reader"] != "none",
         "login": {"ok": login[0], "why": login[1], "checked_at": login[2]}, "running": running}
    if lane is None:
        return v
    v["windows"], v["low_tokens"] = window_rows(lane, S, t)
    rec = (st.get("out") or {}).get(name)
    relogin = f"waits for you to log in (`{LOGIN_CMD[name]}`); re-checked every {AUTH_TTL // 60} min"
    until, why, locked = None, "not locked", False
    if lane.out:
        kind = classify_out(lane, rec)
        text = (rec or {}).get("why") or (lane.notes[0] if lane.notes else "")
        v["detail"] = text[:160] or None
        until, locked = lane.out_until, True
        if kind == "logged_out" or login[0] is False:
            kind, until, why = "logged_out", None, relogin
            note = f"logged out: run `{LOGIN_CMD[name]}`"
        elif kind == "spend":
            note = "spending limit: " + (text[:70] or "credits used up")
            why = "a spending limit has no timer: it ends when you top up or the cycle resets"
        elif kind == "slow":
            note, why = "hung: no output inside the watchdog window, benched for a while", "the watchdog gave no end time"
        else:
            note, why = limit_words(text, lane), "the vendor gave no reset time"
        v["state"] = kind
    elif login[0] is False:
        v["state"], locked, why = "logged_out", True, relogin
        note = f"logged out: run `{LOGIN_CMD[name]}`"
    elif not usable(lane, S, True)[0]:               # over the hard ceiling with nothing recorded: resting until that window resets
        v["state"], locked = "resting", True
        until, note, why = back_at(lane, S), usable(lane, S, True)[1], "no reset time for the window it is over"
    else:
        last = next((h for h in reversed(history) if h.get("lane") == name), None)
        slow = last if last and last.get("outcome") in SLOW_KINDS and t - (last.get("ts") or 0) <= SLOW_WINDOW else None
        if slow:
            v["state"], note = "slow", f"hung {fmt_in(t - slow['ts'])} ago ({slow['outcome']}), no good run since"
            why = "not locked: it clears with its next good run"
        elif login[0] is None or (v["meter"] and not lane.usage_known):
            v["state"] = "unknown"
            note = "no read yet: " + (login[1] if login[0] is None and login[1] else (lane.notes[0] if lane.notes else "no usage reading"))
        else:
            v["state"] = "ready"
            note = "ready" + ("" if v["meter"] else ", vendor shows no usage (a limit appears only when hit)")
            if v["low_tokens"]:
                lt = v["low_tokens"][0]
                note = f"ready, low tokens: {lt['window']} {lt['used_pct']:.0f}% is over the {lt['limit_name']} {lt['limit']}%, last choice only"
    v["emoji"], v["reason"] = EMOJI[v["state"]], note
    if until and until > t and v["state"] != "logged_out":
        v["unlock"] = {"locked": True, "known": True, "at": until, "in_s": int(until - t), "text": when(until, t), "why": None}
    else:
        v["unlock"] = {"locked": locked, "known": False, "at": None, "in_s": None, "text": None, "why": why}
    return v


def claude_why(cfg, lanes):
    """Why no non-Claude worker lane was usable when a Claude lane got the pick: 'OPENAI_SUBSCRIPTION out until ..; ZAI_CODING_SUBSCRIPTION not set up'."""
    S = cfg["settings"]
    fams = {s["on"] for s in cfg["roles"]["worker"]["steps"] if s["on"] != "claude"}
    parts = []
    for name, c in cfg["lanes"].items():
        if c["family"] not in fams:
            continue
        l = lanes.get(name)
        parts.append(f"{name} " + ("not set up" if l is None else usable(l, S, True)[1]))
    return "; ".join(parts) or "no other lane exists"


def next_worker(cfg, lanes, st, t):
    """The lane `next worker` would pick now: decide() on a copy, nothing written."""
    cur = copy.deepcopy(lanes)
    mark_busy(cur, st, cfg["settings"], t)
    unattended = not interactive()
    if unattended:
        cur = {n: l for n, l in cur.items() if l.kind != "credit"}
    d = decide("worker", cfg, cur, st, t, {"authored_by": None, "failed": [], "premium": None, "allow_degraded": False,
                                           "unattended": unattended, "cwd": None, "wait_busy": unattended})
    if d["action"] == "run":
        r = {"action": "run", "lane": d["lane"], "model": d["model"], "effort": d["effort"], "pass": d["pass"], "fallback": d["fallback"], "why": None}
        if cfg["lanes"][d["lane"]]["family"] == "claude":
            r["why"] = claude_why(cfg, cur)
        return r
    return {"action": d["action"], "lane": d.get("lane"), "model": None, "effort": None, "say": d.get("say") or d.get("error")}


def cockpit(cfg, lanes, st, t):
    """The cockpit dict: one row per known lane, configured or not, plus the pick `next worker` would make."""
    import onboard
    R = sys.modules[__name__]
    hist = finished_runs(60)
    live = live_leases(st, t)
    rows = []
    for name, c in cfg["lanes"].items():
        lane = lanes.get(name)
        login = login_state(name, st, t)
        off = off_reason(name, cfg, login) if lane is None else None
        rows.append(lane_view(name, cfg, lane, st, t, hist, sum(1 for x in live.values() if x["lane"] == name), login, off))
    return {"v": 1, "at": t, "legend": {EMOJI[k]: LEGEND[k] for k in EMOJI}, "next_worker": next_worker(cfg, lanes, st, t), "lanes": rows}


def cockpit_text(ck):
    t, nw = ck["at"], ck["next_worker"]
    pick = f"{nw['lane']} {nw['model']} {nw['effort']}" if nw["action"] == "run" else f"none ({(nw.get('say') or nw['action'])[:60]})"
    width = max(len(r["id"]) for r in ck["lanes"])
    mw = max(len(r["model"] or "-") for r in ck["lanes"])
    if nw.get("why"):
        pick += f" (Claude because {nw['why']})"
    nw_ = max([6] + [len(w["name"]) for r in ck["lanes"] for w in r["windows"]])
    out = [f"delegate lanes, {local(t)}   next worker: {pick}"]
    for r in ck["lanes"]:
        out.append(f"{r['emoji']} {r['id']:<{width}}  {(r['model'] or '-'):<{mw}}  {r['reason']}")
        u = r["unlock"]
        if u["locked"]:
            out.append("     " + ("unlocks " + u["text"] if u["known"] else "unlock time unknown: " + u["why"]))
        for w in r["windows"]:
            used = "?" if w["used_pct"] is None else f"{w['used_pct']:.0f}%"
            reset = f", resets {when(w['resets_at'], t)}" if w["resets_at"] and w["resets_at"] > t else ""
            age = "" if w["age_s"] is None else f", {fmt_in(w['age_s'])} old" if w["age_s"] >= 60 else ", just now"
            out.append(f"     {w['name']:<{nw_}} {used:>4} used{reset}  [{w['source']}{age}]")
        out.extend("     ! " + x for x in r["warnings"])
    run = [r for r in ck["lanes"] if r["running"]]
    if run:
        out.append("running: " + ", ".join(f"{r['id']} x{r['running']}" for r in run))
    out.append("  ".join(f"{k} {v}" for k, v in ck["legend"].items()))
    return out


SNAPSHOT_TTL = 6 * 3600


def snapshot_path():
    return config_dir() / "status-snapshot.json"


def status_payload(cfg, lanes, ck, st, t):
    return {"v": 1, "ts": t,
            "lanes": {n: {"family": l.family, "kind": l.kind, "out": l.out, "out_until": l.out_until, "reason": l.out_reason if l.out else None, "five": l.five, "week": l.week,
                          "pace": l.pace, "burn": l.burn, "spend": l.spend, "cap": l.cap, "notes": l.notes} for n, l in lanes.items()},
            "cockpit": ck,
            "running": running_rows(st, t),
            "routing": routing_snapshot(cfg, lanes, st, t),
            "needs_input": needs_input(lanes), "holds": hold_rows(cfg, st, t), "asks": settle_holds(lanes, st, t)[0],
            "health": health_rows(cfg, lanes, st, t), **roles_payload(cfg, lanes, st, t)}


def net_cache_path():
    return config_dir() / "net.json"


def roles_payload(cfg, lanes, st, t):
    """roles{} (usable and net per role) and network{} for status --json; the probe is read from its cache, never run here."""
    cached = netprobe.load(str(net_cache_path()))
    return {"roles": role_view.roles_view(cfg, lanes, st, t, decide, cached), "network": network_view(cached, t)}


def network_view(cached, t):
    return dict(netprobe.view(cached, [], t), why=(cached or {}).get("why"))


def net_probe(hosts=None):
    """-> one probe (lanes/netprobe.py), stored in the net cache. Sockets only: no process, no vendor call, no quota."""
    res = netprobe.probe(hosts)
    netprobe.record(str(net_cache_path()), res)
    return res


def net_up(lane=None):
    """True when `lane`'s vendor hosts (else all of them) are reachable now. The one question the outcome ladder asks when a failure names no cause."""
    return bool(net_probe(list(profiles.hosts_of(lane)) or None)["up"])


def cmd_net(a):
    """route.py net: the probe, as one JSON object. Exit 0 whenever it printed."""
    hosts = list(a.host or []) or list(profiles.hosts_of(a.lane)) or list(profiles.all_hosts()) + list(netprobe.NEUTRAL)
    res = net_probe(hosts)
    cached = netprobe.load(str(net_cache_path())) or {}
    print(json.dumps({"v": 1, "up": res["up"], "ip_up": res["ip_up"], "why": res["why"], "ms": res["ms"], "hosts": res["hosts"], "at": res["at"], "since": cached.get("since")}))
    return 0


def health_rows(cfg, lanes, st, t):
    """One row per (lane, model): state (up|busy|exhausted|logged_out|held|broken), until, source, evidence, leases{live,suspect}, last_failures[3]."""
    rows, pol = [], health.policy(cfg["settings"])
    for name, l in lanes.items():
        cnt = lease_counts(st, t, name)
        rec = (st.get("out") or {}).get(name) or {}
        ats = [w.get("at") for w in l.windows if w.get("at")]
        stale = health.stale_usage(max(ats) if ats else l.read_at, t, pol)
        for model, v in l.hview.items():
            if v["state"] == "up" and l.out and not l.busy:         # the lane's own out record (state.json) is the other half of "exhausted"
                v = dict(v, state="logged_out" if l.out_reason == "logged_out" else "exhausted", until=l.out_until, source=rec.get("source"), kind=l.out_reason,
                         evidence=(rec.get("why") or "")[:240])
            state = health.combine(v, l.held, l.busy)
            rows.append({"lane": name, "model": model, "state": state, "until": v["until"], "source": v["source"], "kind": v["kind"], "evidence": v["evidence"],
                         "canary": v["canary"], "rung": v["rung"], "leases": cnt, "last_failures": v["last_failures"],
                         "needs_login": state == "logged_out" and l.family == "claude",
                         "usage": {"known": l.usage_known and not stale, "text": f"unknown (stale {stale:g} h)" if stale else None}})
    return rows


def running_rows(st, t):
    return [{"run_id": k, "lane": v["lane"], "role": v["role"], "task": v.get("task", ""), "started": v["start"], "age_s": int(t - v["start"]),
             "pid": v.get("pid"), "worker_pid": v.get("worker_pid"), "suspect": suspect(v, t)}
            for k, v in sorted(live_leases(st, t).items(), key=lambda kv: kv[1]["start"])]


def write_snapshot(payload):
    with contextlib.suppress(Exception):
        p = snapshot_path()
        tmp = p.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, p)
        private(p)


def cmd_status_cheap(a, cfg):
    """Zero process spawns and no network: the last full status (status-snapshot.json) with `running` read fresh from the leases and the
    cached logins from auth-cache.json. `age_s` = seconds since the snapshot was taken (null when no status has run yet)."""
    t = now()
    st = load_state()
    snap = {}
    with contextlib.suppress(OSError, ValueError):
        d = json.loads(snapshot_path().read_text(encoding="utf-8"))
        snap = d if isinstance(d, dict) else {}
    at = snap.get("ts")
    out = {k: v for k, v in snap.items() if k not in ("ts", "running")}
    out.update(v=1, cheap=True, ts=t, age_s=int(t - at) if isinstance(at, (int, float)) else None, running=running_rows(st, t), holds=hold_rows(cfg, st, t),
               auth={n: {"ok": c.get("ok"), "why": c.get("why"), "age_s": int(t - c.get("at", 0))} for n, c in read_auth_cache().items() if isinstance(c, dict)})
    out.setdefault("lanes", {})
    cached = netprobe.load(str(net_cache_path()))       # a file, not a socket: cheap opens no connection
    out.update(roles=role_view.refresh(snap.get("roles"), out["holds"], cached, t), network=network_view(cached, t))
    print(json.dumps(out) if a.json else "\n".join(f"{n}: {c['ok']} ({c['age_s']}s old)" for n, c in out["auth"].items()) + f"\nrunning: {len(out['running'])}  snapshot age: {out['age_s']}s")
    return 0


def cmd_status(a, cfg):
    if a.cheap:
        return cmd_status_cheap(a, cfg)
    t = now()
    st = load_state()
    lanes = gather(cfg, st, t, parse_json_arg(a.claude_usage))
    publish_lanes(lanes, st, t)
    ck = cockpit(cfg, lanes, st, t)
    payload = status_payload(cfg, lanes, ck, st, t)
    write_snapshot(payload)
    if a.json:
        print(json.dumps(payload))
        return 0
    print("\n".join(cockpit_text(ck)))
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


LEASE_TTL, LEASE_RENEW = 120, 30      # a running run holds a lease; renewed while it lives, it expires by itself if route.py is killed


def live_leases(st, t):
    return {k: v for k, v in (st.get("leases") or {}).items() if v.get("expires", 0) > t}


def suspect(v, t):
    """A lease whose worker printed nothing for longer than its CLI's silence limit: it is probably hung, and does not count against the lane's cap
    (the 22 false `busy` of 25 Oct). Its worker is stopped by its own watchdog; this only stops it blocking the lane meanwhile."""
    s_ = v.get("silence_s")
    return bool(s_) and t - max(v.get("last_event") or 0, v.get("start") or 0) > s_


def lease_counts(st, t, lane):
    """-> {live, suspect}: the running leases on `lane` (suspect ones are also in live)."""
    mine = [v for v in live_leases(st, t).values() if v.get("lane") == lane]
    return {"live": len(mine), "suspect": sum(1 for v in mine if suspect(v, t))}


def proc_start(pid):
    """The process's start time as ps prints it, or None when it is gone (POSIX only): with the pid it names one process, not a reused pid."""
    if os.name == "nt" or not pid:
        return None
    with contextlib.suppress(Exception):
        r = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5)
        stat, _, start = r.stdout.strip().partition(" ")
        return None if not stat or stat.startswith("Z") else start.strip() or None      # a zombie has exited
    return None


def orphan(v):
    """True when the lease's route.py process is gone (pid plus start time: an expired lease alone may be a laptop waking up) but its worker
    is still running: a SIGKILL on route.py leaves the worker, which is its own process group, editing the tree."""
    gone = proc_start(v.get("pid")) != v.get("pid_start")
    return gone and bool(v.get("worker_start")) and proc_start(v.get("worker_pid")) == v.get("worker_start")


def reap(v):
    """Stop an orphaned worker's whole tree: SIGTERM, a short grace, SIGKILL (stop_tree). -> True when the worker is gone."""
    pg = int(v["worker_pid"])
    stop_tree(pg, grace=3, done=lambda: proc_start(pg) != v.get("worker_start"))
    return proc_start(pg) != v.get("worker_start")


def slots(lane, S):
    """How many runs may be in flight on a lane: max_parallel (null = unlimited), one when the lane is strained."""
    n = S.get("max_parallel")
    if n is None:
        return None
    if lane.five is not None and lane.five >= S["strain"]["five_hour"]:
        return 1
    return int(n)


def mark_busy(lanes, st, S, t):
    """A lane with all its slots taken is skipped like a refused lane; the jitter keeps retrying engines from arriving together."""
    live = live_leases(st, t)
    for l in lanes.values():
        cap, running = slots(l, S), sum(1 for v in live.values() if v["lane"] == l.name and not suspect(v, t))
        if cap is not None and running >= cap and not l.out:
            l.out, l.out_until, l.busy, l.out_reason = True, t + random.randint(60, 120), True, "busy"
            l.notes.insert(0, f"busy: {running} running, {cap} allowed")


def decision(a, cfg):
    """next and run share this: gather, decide, remember the lane choice. -> (d, lanes, warnings, t)"""
    t = now()
    st = load_state()
    unattended = not interactive()
    del OFFERS[:]
    lanes = gather(cfg, st, t, parse_json_arg(a.claude_usage))
    new_offers = list(OFFERS)
    warnings = drift_warnings(lanes, st, t)
    _, st = settle_holds(lanes, st, t)         # a hold whose auto_release_below is met ends here
    publish_lanes(lanes, st, t)
    S = cfg["settings"]
    mark_busy(lanes, st, S, t)
    if unattended:        # no one to pay the API credit knowingly: credit lanes are never picked unattended
        lanes = {n: l for n, l in lanes.items() if l.kind != "credit"}
    ref = getattr(a, "resume", None)
    if ref:             # continue on the lane that made the ref; never switch lanes silently
        rec = (st.get("refs") or {}).get(ref)
        lane = lanes.get(rec["lane"]) if rec else None
        if not rec:
            return {"action": "error", "error": f"unknown --resume ref {ref}"}, lanes, warnings, t
        hold = held(rec["lane"], a.role, rec["mode"], cfg, st, t)
        if hold:
            return {"action": "stop", "lane": rec["lane"], "retry_after": hold.get("until"), "outcome": "held",
                    "say": ("Codex writing is paused by user policy." if hold["id"] == LEGACY_HOLD else f"{rec['lane']} is on hold, {hold_text(hold)}.")
                    + " Continue with a fresh writer on another lane; existing file changes remain in the working directory."}, lanes, warnings, t
        if lane is None or not usable(lane, S, True, rec["model"])[0]:
            why = usable(lane, S, True, rec["model"])[1] if lane else "not usable by this caller"
            if lane is not None and lane.busy:       # its slots are taken: wait for one, the thread stays on its lane
                return {"action": "stop", "reason": "busy", "lane": rec["lane"], "retry_after": lane.out_until,
                        "say": f"{rec['lane']} has no free slot to continue {ref}: {lane.notes[0]}"}, lanes, warnings, t
            return {"action": "stop", "lane": rec["lane"], "retry_after": back_at(lane, S) if lane else None,
                    "outcome": "held" if lane and lane.held else lane.benched[rec["model"]]["kind"] if lane and rec["model"] in lane.benched
                    else {"logged_out": "logged_out", "hung": "hung", "credit": "credit_exhausted"}.get(lane.out_reason, "rate_limited") if lane else "rate_limited",
                    "say": f"{rec['lane']} cannot continue {ref} now: {why}"}, lanes, warnings, t
        step = {"model": rec["model"], "effort": rec["effort"], "mode": rec["mode"], "cap": a.cap or rec.get("cap")}
        return {"action": "run", "role": a.role, "pass": "resume", "rung": rec.get("rung"), "fallback": rec.get("fallback"),
                "fallback_reason": rec.get("fallback_reason"), "first_lane": rec.get("first_lane"), "lane": lane.name, "model": rec["model"], "effort": rec["effort"],
                "mode": rec["mode"], "cap": step["cap"], "how": how(lane, step, cfg, a.cwd, unattended)}, lanes, warnings, t
    args = {"authored_by": a.authored_by, "failed": [int(x) for x in a.failed.split(",") if x != ""] if a.failed else [],
            "premium": a.premium, "allow_degraded": a.allow_degraded and not unattended, "cwd": a.cwd, "unattended": unattended, "wait_busy": unattended}
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
    if unattended and d.get("action") == "stop":
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
    if d.get("warning"):
        print("warning: " + d["warning"], file=sys.stderr)
    print(json.dumps(d, indent=1))
    return 0 if d["action"] != "error" else 1


def cmd_report(a, cfg):
    t = now()
    if a.lane not in cfg["lanes"]:
        sys.exit(f"route.py: unknown lane {a.lane}; lanes: {', '.join(cfg['lanes'])}")
    if a.event == "ok":
        update_state(lambda s: (s.setdefault("out", {}).pop(a.lane, None)))
        history_add({"ts": now(), "run_id": "manual", "release": True, "lane": a.lane, "model": None, "role": "report"})      # the fold forgets the lane's limits
        print(f"{a.lane}: marked available")
        return 0
    until = to_epoch(a.until)
    if until is None:
        st = load_state()
        lanes = gather(cfg, st, t, check_config=False)     # a lane the profile leaves out can still be reported out
        lane = lanes[a.lane]
        until = lane.out_until or (lane.week_reset if lane.week_reset and lane.week_reset > t and lane.kind == "credit" else None) or t + 3600
    mark_out(a.lane, until, a.message or "reported out", t, "limit", "manual" if a.until else "guess")
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

# Arguments that keep a path OUT of a search or listing (rg --glob '!**/.env*', grep --exclude, find -not -path, git ':!x'): a worker
# avoiding a secret must not trip the guard.
EXCLUDED_RE = re.compile(r"""(?:--i?glob|-g)[=\s]+(?:(['"])!.*?\1|!\S+)|--exclude(?:-dir)?[=\s]+(?:(['"]).*?\2|\S+)"""
                         r"""|(?:-not|!)\s+-i?(?:path|name|wholename|regex)\s+(?:(['"]).*?\3|\S+)|(['"]):(?:!|\(exclude\))[^'"]*\4|(?<!\S):(?:!|\(exclude\))\S+""")


SHELLS = {"sh", "bash", "zsh", "dash"}
EXCL_OPTS = {"-g", "--glob", "--iglob", "--exclude", "--exclude-dir", "--exclude-from"}
EXCL_PATH_OPTS = {"-path", "-ipath", "-name", "-iname", "-wholename", "-iwholename", "-regex", "-iregex"}


def unwrap(cmd, depth=3):
    """The script inside `sh|bash|zsh -c/-lc "<script>"` (nested up to `depth` times), else `cmd` unchanged."""
    for _ in range(depth):
        try:
            t = shlex.split(cmd)
        except ValueError:
            return cmd
        i = next((k for k, x in enumerate(t[1:], 1) if re.fullmatch(r"-[a-z]*c[a-z]*", x)), 0)
        if not (t and os.path.basename(t[0]) in SHELLS and i and all(x.startswith("-") for x in t[1:i]) and i + 1 < len(t)):
            return cmd
        cmd = t[i + 1]
    return cmd


def without_excluded(text):
    """`text` as shell words with the arguments that keep a path OUT of a search removed (quote splicing collapsed); None if unparsable."""
    try:
        t = shlex.split(unwrap(text))
    except ValueError:
        return None
    out, prev, prev2 = [], "", ""
    for x in t:
        glob_excl = prev in EXCL_OPTS and (x.startswith("!") or prev.startswith("--exclude"))
        path_excl = prev in EXCL_PATH_OPTS and prev2 in ("-not", "!")
        inline = re.match(r"--exclude(?:-dir|-from)?=|(?:--i?glob=|-g)!", x)
        if not (glob_excl or path_excl or inline or x in EXCL_OPTS or re.match(r":(?:!|\(exclude\))", x) or x in ("-not", "!") or x in EXCL_PATH_OPTS and prev in ("-not", "!")):
            out.append(x)
        prev2, prev = prev, x
    return " ".join(out)


def secret_hit(text):
    """The secret path `text` names, ignoring paths it only excludes; None when there is none."""
    text = text or ""
    stripped = without_excluded(text)
    m = classify.SECRET_RE.search(stripped if stripped is not None else EXCLUDED_RE.sub(" ", text))
    return m.group() if m else None


def denial(kind, tool, inp):
    """One `denied` item. kind: `policy` (a reviewer or classifier refused the call: the owner decides), `secret` (the call named a secret path),
    `sandbox_limit` (the sandbox blocked a call that needs a system resource, like ps: noise, never a failure). path: the secret matched, else
    the call's own path argument."""
    inp = inp if isinstance(inp, dict) else {}
    path = secret_hit(inp.get("command")) if kind == "secret" else inp.get("file_path") or inp.get("filePath") or inp.get("path")
    return {"kind": kind, "tool": tool, "path": path, "input": inp}


HEREDOC_RE = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n.*?(?:\n[ \t]*\2\b|\Z)", re.S)
EDIT_RULE = "Edit files with the patch/edit tool (apply_patch), never with shell heredocs or `python -c` file rewrites."
APPLY_PATCH_HINT = "rewrite this edit with apply_patch (the secret name is only inside a heredoc body; heredoc and `python -c` rewrites are blocked)"


def heredoc_only_hit(cmd):
    """True when `cmd` names a secret path only inside a heredoc body (code being written), not in the command itself."""
    cmd = unwrap(cmd or "")
    return bool(secret_hit(cmd) and not secret_hit(HEREDOC_RE.sub(lambda m: m.group().split("\n", 1)[0] + "\n", cmd)))


SECRET_RULE = "Never touch credential files (~/.ssh, .env, ~/.aws, tokens, keychains): it is blocked. If the task needs one, stop and say so."


# A read-only Claude step (reviewer, planner, light) gets Bash for reading only: every piece of the command line must start with one of
# these, with no output redirection or command substitution. Codex read steps get the same from their read-only sandbox, enforced by the OS;
# this is a filter on the command text, not a boundary.
READ_CMDS = {"cat", "head", "tail", "wc", "ls", "rg", "grep", "egrep", "fgrep", "find", "sed", "sort", "uniq", "cut", "tr", "diff", "nl",
             "file", "stat", "pwd", "echo", "jq", "tree", "du", "basename", "dirname", "realpath", "which", "true", "test", "cd", "git"}
READ_GIT = {"diff", "log", "show", "status", "blame", "ls-files", "ls-tree", "rev-parse", "grep", "merge-base", "cat-file", "describe",
            "shortlog", "rev-list", "branch"}
READ_ONLY_HELP = ("this step is read-only: Bash allows only reading commands (git diff/log/show/status/blame/grep/ls-files, rg, grep, cat, "
                  "head, tail, ls, find without -exec/-delete, sed without -i, wc, sort, diff, jq), no '>' and no $(...).")


def pieces(cmd):
    """The command lines in `cmd` (split on | || && ; and newlines), each as words; None when one cannot be parsed."""
    out = []
    for seg in re.split(r"\|\||&&|;|\||\n", re.sub(r"\d?>&\d|&?\d?>\s*/dev/null", " ", unwrap(cmd))):
        try:
            t = shlex.split(seg)
        except ValueError:
            return None
        if t:
            out.append(t)
    return out


def git_args(t):
    """Words after `git` and its -C/--no-pager/--git-dir options; the first is the subcommand."""
    args = t[1:]
    while args and args[0] in ("-C", "--no-pager") or args and args[0].startswith("--git-dir"):
        args = args[2:] if args[0] == "-C" else args[1:]
    return args


GIT_LISTING = ("--list", "-l", "-a", "-r", "--show-current", "-v", "-vv", "--contains", "--merged", "--no-merged", "--points-at", "--format", "--sort")


def git_write(cmd):
    """The first git command in `cmd` that changes refs, the index's commits, worktrees or remotes (git is the coordinator's), else None.
    Listing forms pass: stash list/show, branch and tag listings, worktree list."""
    for t in pieces(cmd) or []:
        if os.path.basename(t[0]) != "git":
            continue
        args = git_args(t)
        sub, rest = (args[0] if args else ""), args[1:]
        if sub in ("commit", "push", "checkout", "switch", "merge", "rebase", "reset", "cherry-pick", "revert", "am", "pull") \
                or sub == "stash" and not (rest and rest[0] in ("list", "show")) \
                or sub == "worktree" and not (rest and rest[0] == "list") \
                or sub in ("branch", "tag") and (rest and not rest[0].startswith("-") or any(x.startswith("-") and not x.startswith(GIT_LISTING) for x in rest)):
            return " ".join(t[:3])
    return None


def read_only_problem(cmd):
    """None when every piece of `cmd` only reads, else what is not allowed."""
    cmd = unwrap(cmd)
    if re.search(r"`|\$\(|[<>]\(|>", re.sub(r"\d?>&\d|&?\d?>\s*/dev/null", " ", cmd)):
        return "output redirection or command substitution"
    ps = pieces(cmd)
    if ps is None:
        return "unparsable command"
    for t in ps:
        name = os.path.basename(t[0])
        if name not in READ_CMDS:
            return f"`{name}` is not a reading command"
        if name == "find" and set(t) & {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}:
            return "find may not run or delete"
        if name == "sed" and any(x.startswith(("-i", "--in-place")) for x in t):
            return "sed -i writes"
        if name == "rg" and any(x.startswith("--pre") for x in t):
            return "rg --pre runs a program"
        if name == "git":
            args = git_args(t)
            sub = args[0] if args else ""
            if sub not in READ_GIT | {"stash", "tag", "worktree"}:         # also catches `git -c key=value ...`, which can run a program
                return f"git {sub} is not a reading command"
            if git_write(" ".join(shlex.quote(x) for x in t)):
                return f"git {sub} may only list"
            if any(x.startswith("--output") for x in args):
                return "--output writes a file"
    return None


def cmd_guard(a, cfg=None):
    """Claude PreToolUse hook: exit 2 (block, reason on stderr) when a tool call names a secret path, or, with --read-only, when a Bash
    command does more than read. Only the fields that name what is run or opened are checked, not file contents being written (a .gitignore
    listing .env is fine)."""
    with contextlib.suppress(ValueError, AttributeError):
        j = json.load(sys.stdin)
        ti = j.get("tool_input") or {}
        hit = secret_hit(" ".join(str(ti.get(k) or "") for k in ("command", "file_path", "path", "pattern", "glob")))
        if hit:
            hint = f" {APPLY_PATCH_HINT}." if heredoc_only_hit(str(ti.get("command") or "")) else ""
            print(f"blocked by delegate: {hit} is a secret path.{hint} {SECRET_RULE}", file=sys.stderr)
            return 2
        cmd = str(ti.get("command") or "") if j.get("tool_name") == "Bash" else ""
        gw = git_write(cmd)
        if gw:
            print(f"blocked by delegate: `{gw}` changes git, which is the coordinator's. {GIT_RULES}", file=sys.stderr)
            return 2
        why = getattr(a, "read_only", False) and cmd and read_only_problem(cmd)
        if why:
            print(f"blocked by delegate: {why}; {READ_ONLY_HELP}", file=sys.stderr)
            return 2
    return 0


GIT_RULES = ("Git is the coordinator's. Stay in the current directory. Do not create or switch branches or worktrees; "
             "do not commit, amend, merge, rebase, reset, stash, tag or push. Read-only git is fine. Leave changes uncommitted.")



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

DENIED_RULE = "If a command is denied, that is a decision for the owner: do not retry or reword it; report the exact command in your final message and stop."
NO_SUBAGENTS_RULE = "Do not spawn sub-agents; do the work yourself."
ESCALATE_RULE = ("You run unattended. If a command fails because of the sandbox (network, files outside this directory, a system resource), "
                 "request escalated permissions for that exact command once, with a one-line justification.")
PREAMBLE = (SECRET_RULE + " You are a delegated worker. Do exactly the task in the brief. Do not delegate further and do not use the delegate skill. "
            + GIT_RULES + " " + EDIT_RULE + " " + DENIED_RULE + " Finish with a short report: what you changed or found, and the exit code and last lines of any check you ran.")


def guard_settings(read_only):
    """--settings for claude -p: delegate's PreToolUse hook (route.py guard), read-only for steps that may not write."""
    argv = [sys.executable, str(Path(__file__).resolve()), "guard"] + (["--read-only"] if read_only else [])
    cmd = subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(shlex.quote(x) for x in argv)
    return {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}]}}


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



def descendants(pid):
    """Every live process below `pid` (POSIX), by parent links."""
    with contextlib.suppress(Exception):
        r = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5)
        kids = {}
        for ln in r.stdout.splitlines():
            c, _, par = ln.strip().partition(" ")
            kids.setdefault(int(par), []).append(int(c))
        out, todo = [], [pid]
        while todo:
            for c in kids.get(todo.pop(), []):
                out.append(c)
                todo.append(c)
        return out
    return []


def pid_alive(pid):
    """True while the pid is a live, non-zombie process (POSIX; Windows answers False: taskkill /T does the whole job there)."""
    if os.name == "nt":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    st = ""
    with contextlib.suppress(Exception):
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
    return not st.startswith("Z")


def stop_tree(pid, grace=0, done=None):
    """Stop `pid` and everything below it, however deep and whatever session or group each descendant sits in (the Codex CLI is a node
    shim whose native child may sit in a group of its own and ignore SIGTERM; Claude's background shells likewise). Only this tree, found
    by parent links: never a pattern. Windows: `taskkill /T /F /PID`. POSIX: with `grace`, SIGTERM first and wait up to `grace` seconds
    for `done()`; then SIGKILL every pid and group collected, re-collecting until nothing below is left (a dead parent orphans its
    children, so the pids are remembered from before the first signal). -> True when nothing is left."""
    if os.name == "nt":
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return True
    me, mygroup = os.getpid(), os.getpgrp()
    known = {pid}
    groups = set()

    def collect():
        for c in [pid] + descendants(pid):
            known.add(c)
        for c in list(known):
            with contextlib.suppress(OSError):
                groups.add(os.getpgid(c))
        groups.discard(mygroup)
        known.discard(me)

    def sweep(sig):
        for g in groups:
            with contextlib.suppress(OSError):
                os.killpg(g, sig)
        for c in known:
            with contextlib.suppress(OSError):
                os.kill(c, sig)
    collect()
    if grace:
        sweep(signal.SIGTERM)
        end = time.time() + grace
        while time.time() < end and not (done() if done else not any(pid_alive(c) for c in known)):
            time.sleep(0.2)
    for _ in range(5):
        collect()
        sweep(signal.SIGKILL)
        time.sleep(0.05)
        if not any(pid_alive(c) for c in known):
            return True
    return not any(pid_alive(c) for c in known)


def kill_tree(p):
    """Kill the worker `p` (a Popen) and its whole tree: killing only the codex wrapper leaves the vendor binary running."""
    with contextlib.suppress(Exception):
        stop_tree(p.pid)
    with contextlib.suppress(Exception):
        p.kill()


def run_tree(cmd, timeout, **kw):
    """subprocess.run with captured text output whose timeout stops the whole tree, not just the direct child. -> CompletedProcess; TimeoutExpired on timeout."""
    sess = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **sess, **kw)
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:
        kill_tree(p)
        with contextlib.suppress(Exception):
            p.communicate(timeout=5)
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


CREDENTIAL_RE = re.compile(r"(TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)|^(AWS_|GH_|GITHUB_|SSH_AUTH_SOCK$)", re.I)


def child_env(extra=None):
    """The worker gets the caller's environment minus what a Claude session exports (onboard.APP_ENV_RE), delegate's and credential-looking variables (tokens, keys, cloud and git credentials, the ssh agent), plus DELEGATE_DEPTH=1."""
    from onboard import app_free_env
    env = {k: v for k, v in app_free_env().items() if not k.startswith("DELEGATE_") and not CREDENTIAL_RE.search(k)}
    env["DELEGATE_DEPTH"] = "1"
    env.update(extra or {})
    return env


WORKER_TICK = []           # the idle watchdog's touch(): run_proc calls it for every line the worker prints (launchers set it before run_proc)
REF_SEEN = []              # callbacks run with the vendor session id the moment the vendor reveals it (run_once registers one per run)
WORKER_STARTED = []        # callbacks run with the worker's pid right after it starts (run_once registers one per run)


def note_ref(ref):
    """The vendor named its session: tell run_once now (it stores the ref and prints it), not when the call ends, so a killed route.py still leaves the ref."""
    for cb in REF_SEEN:
        with contextlib.suppress(Exception):
            cb(ref)


def live_for(cli, d, a):
    """The live event file writer for this launch (a no-op unless DELEGATE_LIVE_FILE is set); its start event goes out once the process has a pid."""
    lv = livemod.Live(cli)
    lv.start(lane=d.get("lane"), model=d.get("model"), effort=d.get("effort"), role=d.get("role") or getattr(a, "role", None))
    WORKER_STARTED.append(lv.started)
    return lv


def live_end(lv, turn, dog, timed_out, rc, cost=None):
    with contextlib.suppress(ValueError):
        WORKER_STARTED.remove(lv.started)
    lv.end(livemod.end_status(timed_out, dog["why"], turn.stop, rc), cost)


def run_proc(cmd, text, cwd, env, timeout, on_line=None, poll=None):
    """-> (returncode, stdout, stderr, timed_out). Own process group; the timeout kills the group; stdin carries `text` then closes; UTF-8, errors replaced.
    `on_line(line)` returning True stops the worker now; a number stops it that many seconds later unless it exits first.
    `poll()` runs about every half second; a truthy answer stops the worker (its own process tree only). `tick()` runs for every line the
    worker prints on stdout or stderr (any stream delta is activity): the callback in WORKER_TICK, which the launcher sets just before the call."""
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}
    tick = WORKER_TICK[0] if WORKER_TICK else None
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env, **kw)
    for cb in WORKER_STARTED:
        with contextlib.suppress(Exception):
            cb(p.pid)
    out, err, stop_at = [], [], []

    def pump(stream, sink, cb):
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace")
            sink.append(line)
            if tick:
                with contextlib.suppress(Exception):
                    tick()
            ask = cb(line) if cb else None
            if ask is True:        # the callback asks to stop the worker now
                kill_tree(p)
            elif ask and not stop_at:
                stop_at.append(time.time() + ask)

    def feed():
        with contextlib.suppress(OSError):
            p.stdin.write(text.encode("utf-8"))
            p.stdin.close()
    threads = [threading.Thread(target=pump, args=(p.stdout, out, on_line), daemon=True),
               threading.Thread(target=pump, args=(p.stderr, err, None), daemon=True), threading.Thread(target=feed, daemon=True)]
    for th in threads:
        th.start()
    timed_out, ppid, end = False, os.getppid(), time.time() + timeout if timeout else None

    def bye(signum, frame):
        raise SystemExit(128 + signum)
    old = {g: signal.signal(g, bye) for g in (getattr(signal, n, None) for n in ("SIGTERM", "SIGHUP")) if g is not None} if os.name != "nt" else {}
    try:
        while True:
            try:
                p.wait(timeout=(0.5 if poll else 2) if end is None else max(0.01, min(0.5 if poll else 2, end - time.time())))
                break
            except subprocess.TimeoutExpired:
                if end is not None and time.time() >= end:
                    timed_out = True
                    kill_tree(p)
                    p.wait()
                    break
                if (stop_at and time.time() >= stop_at[0]) or (poll and poll()):
                    kill_tree(p)
                    p.wait()
                    break
                if UNATTENDED and os.name != "nt" and os.getppid() != ppid:     # SIGKILL on the engine cannot be caught: the parent changing is the only sign
                    raise SystemExit(1)
    except BaseException:       # SIGTERM, SIGHUP, Ctrl-C, parent gone: the worker is in its own process group, so stop it here or it keeps burning the plan
        kill_tree(p)
        p.wait()
        raise
    finally:
        for g, h_ in old.items():
            signal.signal(g, h_)
    for th in threads:
        th.join(5)
    return p.returncode, "".join(out), "".join(err), timed_out


def find_transcript(ref, family):
    if not ref:
        return None
    import glob
    if family not in ("claude", "openai"):
        f = opencode_transcript(ref)        # exported after the run
        return str(f) if f.is_file() else None
    pattern = (str(Path.home() / ".claude" / "projects" / "*" / (ref + ".jsonl")) if family == "claude"
               else str(Path.home() / ".codex" / "sessions" / "**" / ("*" + ref + ".jsonl")))
    hits = glob.glob(pattern, recursive=True)
    return hits[0] if hits else None


def schema_problem(data, schema, at="$"):
    """None when `data` fits the JSON schema subset delegate checks (type, required, properties, items, enum), else what is wrong."""
    if not isinstance(schema, dict):
        return None
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None), "number": (int, float), "integer": int}
    want = schema.get("type")
    want = want if isinstance(want, list) else [want] if want else []
    if want and not any(isinstance(data, kinds.get(w, object)) and not (w in ("number", "integer") and isinstance(data, bool)) for w in want):
        return f"{at} is not {'/'.join(want)}"
    if "enum" in schema and data not in schema["enum"]:
        return f"{at} is not one of {schema['enum']}"
    if isinstance(data, dict):
        for k in schema.get("required") or []:
            if k not in data:
                return f"{at}.{k} is missing"
        for k, sub in (schema.get("properties") or {}).items():
            if k in data:
                why = schema_problem(data[k], sub, f"{at}.{k}")
                if why:
                    return why
    if isinstance(data, list) and isinstance(schema.get("items"), dict):
        for i, x in enumerate(data):
            why = schema_problem(x, schema["items"], f"{at}[{i}]")
            if why:
                return why
    return None


def parse_data(text, schema):
    if not schema or not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


LOGOUT_OUT = 900     # a lane whose worker found it logged out is skipped this long, then tried again (a login in between is not seen sooner)


RESET_LADDER_MIN = (15, 30, 60)       # a limit whose end nobody states: refusal time + this, one rung further for each consecutive guess
RESET_AT_RE = re.compile(r"(?:reset(?:s)?|resume(?:s)?|available|renews?|until|try again|retry)\s*(?:at|on|after|:)?\s*"
                         r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d(?::\d\d)?)(?:\.\d+)?\s*(Z\b|UTC\b|GMT\b|[+-]\d\d:?\d\d\b)?", re.I)
RETRY_AFTER_RE = re.compile(r"retry[- ]after\W{0,3}\s*(\d+(?:\.\d+)?)\b(?!\s*[-:/]\d)|retry[- ]after\W{0,3}\s*([A-Z][a-z]{2}, \d\d [A-Z][a-z]{2} \d{4} [\d:]{8} GMT)", re.I)
IN_RE = re.compile(r"\bin\s+(?:about\s+|around\s+)?(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b(?:\s*(?:and\s*)?(\d+)\s*(minutes?|mins?|m)\b)?", re.I)
TRY_AT_RE = re.compile(r"try again at (\d{1,2}):(\d{2})\s*([AP]M)?", re.I)
UNIT_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_reset(msg, t):
    """-> (epoch, zone) when a vendor message states when a limit ends, else (None, None). Forms, in order: `Retry-After: 120` or an HTTP date;
    `will reset at 2026-10-25 14:49:00` / ISO after reset|until|try again (a stated offset or Z/UTC is kept; none = this machine's zone, since the
    vendor shows its user local time); `try again at 5:50 AM` (local, the next such time); `in 5 hours` / `in 2h 30m`. A time already past, or
    more than 40 days off, is not a reset time."""
    msg = msg or ""
    ok = lambda at: at if at and t < at <= t + 40 * 86400 else None
    m = RETRY_AFTER_RE.search(msg)
    if m:
        at = ok(t + float(m.group(1))) if m.group(1) else None
        if m.group(2):
            with contextlib.suppress(TypeError, ValueError):
                at = ok(dt.datetime.strptime(m.group(2), "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=dt.timezone.utc).timestamp())
        if at:
            return at, None
    m = RESET_AT_RE.search(msg)
    if m:
        day, clock, zone = m.group(1), m.group(2), (m.group(3) or "")
        z = zone.upper().replace("GMT", "UTC")
        with contextlib.suppress(ValueError):
            naive = dt.datetime.fromisoformat(f"{day}T{clock if clock.count(':') == 2 else clock + ':00'}")
            if not z:
                at = ok(naive.timestamp())          # naive = this machine's local zone
                if at:
                    return at, "local " + (time.tzname[time.localtime(at).tm_isdst > 0] or "")
            else:
                off = dt.timezone.utc if z in ("Z", "UTC") else dt.timezone(dt.timedelta(hours=int(z[:3]), minutes=int(z[0] + z[-2:])))
                at = ok(naive.replace(tzinfo=off).timestamp())
                if at:
                    return at, "UTC" if z in ("Z", "UTC") else z
    m = TRY_AT_RE.search(msg)
    if m:
        h, mi = int(m.group(1)) % 12 if m.group(3) else int(m.group(1)), int(m.group(2))
        h += 12 if (m.group(3) or "").upper() == "PM" else 0
        at = dt.datetime.fromtimestamp(t).replace(hour=h % 24, minute=mi, second=0, microsecond=0)
        if at.timestamp() <= t:
            at += dt.timedelta(days=1)
        return at.timestamp(), "local " + (time.tzname[time.localtime(at.timestamp()).tm_isdst > 0] or "")
    m = IN_RE.search(msg)
    if m:
        secs = float(m.group(1)) * UNIT_S[m.group(2)[0].lower()] + (int(m.group(3)) * 60 if m.group(3) else 0)
        at = ok(t + secs)
        if at:
            return at, None
    return None, None


def limit_until(msg, t, d, reset=None, guess_n=0):
    """-> (until, source, zone) for a limit refusal. source: `vendor` = the message (or the vendor's own usage reading, `reset()`) states the end;
    `guess` = the lane's soonest window reset, else the refusal time + 15/30/60 min by how many guesses in a row (guess_n)."""
    at, zone = parse_reset(msg, t)
    if at:
        return at, "vendor", zone
    at = reset() if reset else None
    if at and at > t:
        return at, "vendor", None
    at = d.get("limit_reset")
    if at and at > t:
        return at, "guess", None
    return t + RESET_LADDER_MIN[min(guess_n, len(RESET_LADDER_MIN) - 1)] * 60, "guess", None


def next_utc_midnight(t):
    d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    return (d.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)).timestamp()


def codex_reset_now(t):
    """The reset of the Codex window a fresh reading shows full (>= 95%), else None."""
    res, _ = fetch_codex_limits()
    rl = (res or {}).get("rateLimits") or {}
    full = [w.get("resetsAt") for w in (rl.get("primary") or {}, rl.get("secondary") or {}) if (w.get("usedPercent") or 0) >= 95 and (w.get("resetsAt") or 0) > t]
    return min(full) if full else None


def mark_out(lane, until, why, t, reason="limit", source="guess", zone=None, guess_n=0):
    """Record a lane out. source: vendor (the vendor stated the end) | guess | manual (the owner said); zone: the zone the vendor's time was in;
    `set` = wall clock now (workers already running read it: a lane marked out after they started stops them)."""
    update_state(lambda s: s.setdefault("out", {}).__setitem__(lane, {"until": until, "why": why, "at": t, "set": time.time(), "reason": reason,
                                                                    "source": source, "zone": zone, "guess_n": guess_n}))


def codex_child_tokens(thread, since):
    """-> (tokens, threads) the child threads of a codex thread spent since `since`: the automatic reviewer runs as one, in its own
    rollout file under $CODEX_HOME/sessions/Y/M/D (local date), and `codex exec --json` never reports it. Each file's last token_count holds its total."""
    tot, n = {"in": 0, "cached": 0, "out": 0}, 0
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    for day in sorted({time.strftime("%Y/%m/%d", time.localtime(x)) for x in (since, time.time())}):
        for f in (root / day).glob("rollout-*.jsonl"):
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
                if f.stat().st_mtime < since:
                    continue
                with f.open(encoding="utf-8", errors="replace") as fh:
                    if (json.loads(fh.readline()).get("payload") or {}).get("parent_thread_id") != thread:
                        continue
                    last = None
                    for line in fh:
                        if '"token_count"' in line:
                            last = line
                if last:
                    u = json.loads(last)["payload"]["info"]["total_token_usage"]
                    tot = {"in": tot["in"] + (u.get("input_tokens") or 0), "cached": tot["cached"] + (u.get("cached_input_tokens") or 0),
                           "out": tot["out"] + (u.get("output_tokens") or 0)}
                    n += 1
    return tot, n


def codex_thread_total(thread):
    """-> {in, cached, out} a codex thread has spent so far, or None: its rollout file (any day) ends with a token_count holding the
    thread's total. `turn.completed` on `exec resume` reports that running total too, so a resumed run subtracts what came before."""
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    for f in root.glob(f"*/*/*/rollout-*{thread}.jsonl"):
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
            last = None
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"token_count"' in line:
                        last = line
            if last:
                u = json.loads(last)["payload"]["info"]["total_token_usage"]
                return {"in": u.get("input_tokens") or 0, "cached": u.get("cached_input_tokens") or 0, "out": u.get("output_tokens") or 0}
    return None


CREDIT_PROBE = 6 * 3600      # a lane out of credits with no stated end is looked at again after this long


NET_AMBIGUOUS = ("worker_failed", "vendor_blip", "hung")     # failures that name no cause of their own: the network may be it


def net_pass(o, d):
    """-> `o`, or the network outcome when it was the network (A2.5). classify.py trusts network words only before the worker did anything, and a
    silent hang is only a network drop if the net is down: so a failure with no cause of its own is checked against the words of a dropped
    connection and then against one probe (<= 3 s, sockets only). Limits, logins, bad requests, secrets and timeouts are never second-guessed."""
    if o.reason in NET_AMBIGUOUS and (netprobe.looks_dropped(o.detail) or not net_up(d["lane"])):
        return classify.Outcome("network", o.detail, o.source)
    return o


def settle(res, o, d, t, who, a, rc=None, tail="", reset=None, credit_until=None, login=None):
    """Apply a classified outcome (lanes/classify.py) to the answer and, for a lane-scoped reason, to the lane's state. The one place all three
    launchers end in. `reset` -> default end of a limit when the message names no time; `credit_until` -> the end of a credit cycle, when known;
    `login` -> (hint stored with the lane, text of the answer with {why})."""
    o = net_pass(o, d)
    r, why, lane = o.reason, o.detail.strip(), d["lane"]
    soon = lambda: now() + random.randint(60, 120)
    answered = res.pop("answered", False)
    if r != "hung":
        res.pop("hung_words", None), res.pop("hung_kind", None)
    res["outcome"] = r
    res["detail"] = why[:240]           # run_once moves it into history.jsonl (`say`); it never reaches the wire
    if r == "secrets":
        res.update(say=f"stopped: the worker ran a command on a secret path: {why[:200]}" + (f"; {APPLY_PATCH_HINT}" if heredoc_only_hit(why) else ""))
    elif r == "timeout":
        res.update(say=f"{who} did not finish within {a.timeout}s and was killed")
    elif r == "hung":
        words, kind = res.pop("hung_words", "printed nothing"), res.pop("hung_kind", "first_event")
        if kind == "first_event":       # nothing came at all: the lane is the suspect, it rests; a worker that went quiet mid-run is the pair's (the breaker counts it)
            until = t + OC_HUNG_OUT_SECS
            mark_out(lane, until, f"hung: {words}", t, "hung")
            res.update(retry_after=until, say=f"{who} {words} and was stopped; {lane} out until {local(until)}")
        else:
            until = now() + random.randint(60, 120)
            res.update(retry_after=until, say=f"{who} {words} and was stopped (its whole process tree); retry")
    elif r == "rate_limited":
        prev = (load_state().get("out") or {}).get(lane) or {}
        n = prev.get("guess_n", -1) + 1 if prev.get("source") == "guess" and t - prev.get("at", 0) < 6 * 3600 else 0
        until, source, zone = limit_until(why, t, d, reset, n)
        mark_out(lane, until, ("limit (opencode log): " if o.source == "ours" else "limit refusal: ") + why[:120].replace("\n", " "), t, "limit", source, zone, n if source == "guess" else 0)
        res.update(retry_after=until, say=f"{lane} hit a limit" + (f" ({why[:120]}); worker stopped, lane" if o.source == "ours" else ";") + f" out until {local(until)}")
    elif r == "credit_exhausted":
        until = credit_until or t + CREDIT_PROBE
        mark_out(lane, until, "out of credits: " + why[:120].replace("\n", " "), t, "credit", "vendor" if credit_until else "guess")
        res.update(retry_after=credit_until, say=f"{lane} is out of credits ({why[:120]}); out until {local(until)}")
    elif r == "logged_out":
        hint, say = login or (f"logged out: run `{LOGIN_CMD.get(lane, 'route.py doctor')}`", f"{who} is not logged in: {{why}}")
        mark_out(lane, t + LOGOUT_OUT, hint, t, "logged_out", "guess")
        res.update(fix=f"run `{LOGIN_CMD.get(lane, 'route.py doctor')}`", say=say.format(why=why[-200:].strip()))
    elif r == "bad_request":
        res.update(fix="the vendor rejected the request itself (schema, flags or arguments): fix the caller, not the lane", say=f"{who} rejected the request: {why[:300]}")
    elif r == "network" and answered:         # the final answer was captured before the connection dropped (ae6v): the work is done, say so
        update_state(lambda s: s.setdefault("out", {}).pop(lane, None))
        res.update(outcome=None, data=parse_data(res.get("text") or "", a.schema), warnings=["net_after_answer"], detail="")
    elif r == "network":      # not a lane fault: nothing is marked out, the pair is not counted, and the caller decides when to resume (`resumable`, `ref`)
        res.update(retry_after=soon(), resumable=bool(res.get("ref") and res.get("started")), partial=bool(res.get("partial")),
                   say=f"{who} could not reach its service: {why[-200:].strip()}")
    elif r == "killed":
        res.update(resumable=bool(res.get("ref") and res.get("started")), partial=bool(res.get("partial")),
                   say=f"{who} was ended by a signal it did not get from route.py ({why}); retry")
    elif r == "vendor_blip":
        res.update(retry_after=soon(), say=f"{who} failed before any work: {why[:200]}")
    else:
        res.update(say=f"{who}: {why[:300]}" if o.source == "event" else f"{who} exited {rc} without an error: {(why or tail)[-300:].strip()}")
    return res


def codex_context_k(thread):
    """-> thousands of input tokens of the thread's last model request (its context size), from the end of its rollout file, else None."""
    if not thread:
        return None
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    for f in root.glob(f"*/*/*/rollout-*{thread}.jsonl"):
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
            with f.open("rb") as fh:
                fh.seek(max(0, f.stat().st_size - 131072))
                tail = fh.read().decode("utf-8", "replace").splitlines()
            for line in reversed(tail):
                if '"token_count"' in line:
                    info = json.loads(line)["payload"]["info"]
                    return round(info["last_token_usage"]["input_tokens"] / 1000, 1)
    return None


def launch_codex(d, brief, a, rid, t, meta):
    """-> dict of result fields."""
    h = d["how"]
    hold = held(d.get("lane") or "OPENAI_SUBSCRIPTION", d.get("role", getattr(a, "role", None)), "read_only" if h.get("sandbox") == "read-only" else "write", load_routing(), load_state(), t)
    if hold:
        return refusal_fields(hold, "Codex is restricted to read-only planning and review; no worker was launched." if hold["id"] == LEGACY_HOLD else "no worker was launched")
    exe = shutil.which("codex")
    if not exe:
        return {"outcome": "setup", "say": "codex CLI not found on PATH (route.py doctor --lane OPENAI_SUBSCRIPTION)"}
    out_file = config_dir() / f"codex-last-{rid[:8]}.txt"
    if a.resume:
        cmd = [exe, "exec", "resume", "-m", h["model"], "-c", f"model_reasoning_effort={d['effort']}", "-c", f'sandbox_mode="{h["sandbox"]}"'] + CODEX_ACCESS + h["isolation"]
        cmd += [x for x in h["cli"] if x == "--skip-git-repo-check"] + ["--json", "-o", str(out_file)]
        cmd += (["--output-schema", a.schema] if a.schema else []) + [a.resume, "-"]
    else:
        cmd = [exe] + h["cli"][1:] + ["--json", "-o", str(out_file)] + (["--output-schema", a.schema] if a.schema else []) + ["-"]
    seen = {"ref": a.resume, "last": "", "tokens": None, "denied": [], "error": "", "transient": "", "work": False, "turns": 0, "net_at": None, "net": False, }
    turn = supervisor.Turn("codex")
    live = live_for("codex", d, a)
    if a.resume:
        note_ref(a.resume)

    def on_line(line):
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        live.feed(ev)
        kind, it = ev.get("type"), ev.get("item") or {}
        if kind == "turn.completed" or (kind == "item.completed" and it.get("type") == "agent_message"):
            turn.finished()         # the answer may be in: poll() stops the tree if nothing else comes within the grace (delegate-itn)
        else:
            turn.more()             # any later event: the turn goes on
        if it.get("type") in ("command_execution", "mcp_tool_call", "file_change") and kind in ("item.started", "item.completed"):      # a running tool keeps the long silence limit
            turn.tool(it.get("id"), kind == "item.started")
        if ev.get("type") == "thread.started" and ev.get("thread_id"):
            seen["ref"] = ev["thread_id"]
            note_ref(seen["ref"])
            if agent_runs:
                agent_runs.run(rid, thread=seen["ref"])
        if ev.get("type", "").startswith(("item.", "turn.")) and ev.get("type") != "turn.failed":
            seen["net_at"] = None           # the stream moves again: the wait for the network is over
        if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            u = ev["usage"]
            seen["tokens"] = {"in": u.get("input_tokens"), "cached": u.get("cached_input_tokens"), "out": u.get("output_tokens")}
            seen["cumulative"] = dict(seen["tokens"])       # the thread's running total on a resume: kept in the history row for the next call
            seen["turns"] += 1
        if ev.get("type") in ("turn.failed", "error"):      # codex's own reason; stderr then holds only transport noise
            msg = str((ev.get("error") or {}).get("message") if ev.get("type") == "turn.failed" else ev.get("message") or "")
            wait = netprobe.waiting(msg)
            seen["transient" if classify.transient(msg) or wait else "error"] = msg        # "Reconnecting... 2/5" and "...waiting for network" are progress, not a failure
            if wait:
                seen["net_at"] = time.monotonic()
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") in ("command_execution", "file_change", "agent_message", "mcp_tool_call"):
            seen["work"] = True
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            seen["last"] = item.get("text", "")
        if ev.get("type") == "item.completed" and item.get("type") == "command_execution" and item.get("status") == "declined":
            seen["denied"].append(denial("policy", "Bash", {"command": item.get("command")}))     # the reviewer refused an escalation
        if ev.get("type") == "item.started" and item.get("type") == "command_execution" and secret_hit(item.get("command")):
            seen["denied"].append(denial("secret", "Bash", {"command": item.get("command")}))
            seen["secret"] = item.get("command")
            return True             # codex has no hook under exec: stop the worker before it can use what it read
    if agent_runs:
        agent_runs.run(rid, kind="codex", lane=d["lane"], model=d["model"], cwd=h["cwd"], started=t, status="running", **meta)
    started = time.time()       # wall clock, not now(): it is compared with file times
    before = (codex_thread_total(a.resume) or history_cumulative(a.resume)) if a.resume else None
    poll0, tick, dog = worker_watch("codex", d["lane"])
    WORKER_TICK[:] = [tick]
    base_poll = stream_poll(poll0, turn, dog, live)

    def poll():
        """Codex waits for the network by itself, for as long as it takes. Past NET_GRACE_S of that, one probe: still offline -> stop it now and hand the
        wait to the caller (the thread resumes), instead of holding the slot for the whole silence limit."""
        if base_poll():
            return True
        if seen["net_at"] is not None and time.monotonic() - seen["net_at"] >= NET_GRACE_S:
            seen["net_at"] = None
            if not net_up(d["lane"]):
                seen["net"] = True
                return True
        return False
    rc, out, err, timed_out = run_proc(cmd, " ".join((SECRET_RULE, GIT_RULES, EDIT_RULE, NO_SUBAGENTS_RULE, ESCALATE_RULE, DENIED_RULE)) + "\n\n" + brief, h["cwd"], child_env(), a.timeout, on_line, poll)
    live_end(live, turn, dog, timed_out, rc)
    log_gaps(dog, d)
    text = ""
    with contextlib.suppress(OSError):
        text = out_file.read_text(encoding="utf-8", errors="replace").strip()
        out_file.unlink()
    if seen["ref"] and stalled_once_more("codex", turn, dog, a):
        return launch_codex(d, RESUME_NOTE, argparse.Namespace(**dict(vars(a), resume=seen["ref"], stalled_once=True)), rid, t, meta)       # once; off by default
    final = bool(text) or bool(seen["last"] and (seen["turns"] or turn.stop == "answer"))        # the turn finished: codex writes -o only at its end
    text = text or seen["last"]
    seen["tokens"] = accounting.per_call(seen["tokens"], before)      # a running total: keep this run's part
    if seen["ref"]:             # the reviewer's tokens are the run's too
        extra, n = codex_child_tokens(seen["ref"], started - 1)
        if n:
            base = seen["tokens"] or {"in": 0, "cached": 0, "out": 0}
            seen["tokens"] = {k: (base.get(k) or 0) + extra[k] for k in extra}
    res = {"text": text, "ref": seen["ref"], "stderr": err, "tokens": seen["tokens"], "denied": seen["denied"], "turns": seen["turns"] or None,
           "context_k": codex_context_k(seen["ref"]), "started": bool(seen["ref"]), "partial": seen["work"], "answered": final,
           "acct": accounting.codex(seen["tokens"]), "cumulative": seen.get("cumulative")}
    o = classify.classify(rc, seen["error"] or (seen["transient"] if rc != 0 else ""), err, timed_out, seen["work"], dict(watch_fields(res, dog), secret=seen.get("secret")))
    if turn.stop == "answer" and o and o.reason == "killed" and final and not seen["error"] and not timed_out:       # we stopped it after its answer: the answer stands
        o = None
        res["warnings"] = ["answer_done_process_alive"]
    if dog["why"] or turn.stop or timed_out:        # the raw tail of the stream, so the call record shows what the CLI last said before it was stopped
        res["stream_tail"] = supervisor.tail(out)
    if seen["net"] and (o is None or o.reason == "killed"):       # we stopped it because the network is down
        o = classify.Outcome("network", seen["transient"], "event")
    if o:       # codex's error or stderr, never the model's answer
        settle(res, o, d, t, "codex", a, rc, err or text, reset=lambda: codex_reset_now(t),
               login=("logged out: run `codex login`", f"codex is not logged in: {{why}} (route.py doctor --lane {d['lane']})"))
    else:
        update_state(lambda s: s.setdefault("out", {}).pop(d["lane"], None))
        res.update(data=parse_data(text, a.schema))
    return res


OC_SECRETS = (".ssh", ".aws", ".gnupg", ".azure", ".config/gh", ".docker", ".netrc", ".git-credentials", ".codex", ".grok", ".claude", ".claude.json",
              ".local/share/opencode", "Library/Keychains")
OC_RULE = ("You run unattended. Every command runs in a sandbox: no network, no credential files, no writes outside this directory. If a command "
           "fails because of it, do not work around it or change code to avoid it: say which command and why in your final message, and go on without it.")


def opencode_transcript(ref):
    return config_dir() / "opencode-sessions" / (ref + ".json")


OC_FINAL_REASONS = ("stop", "end_turn", "end-turn")       # step_finish part.reason of a step that ends the turn (not "tool-calls")
OC_HUNG_OUT_SECS = 1800       # a lane whose worker printed nothing at all rests this long


def opencode_log_dir():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "log"


def opencode_log_poll():
    """-> (poll, state). poll() is truthy once opencode's own log gained a LIMIT_RE line since launch (it retries a limit forever without exiting);
    state["why"] is then "limit" and state["msg"] the log line."""
    logs = opencode_log_dir()
    state = {"why": None, "msg": ""}
    offs = {}
    with contextlib.suppress(OSError):
        offs = {f: f.stat().st_size for f in logs.glob("*.log")}

    def poll():
        with contextlib.suppress(OSError):
            for f in logs.glob("*.log"):
                size, off = f.stat().st_size, offs.get(f, 0)
                if size > off:
                    with open(f, "rb") as fh:
                        fh.seek(off)
                        new = fh.read(size - off).decode("utf-8", "replace")
                    offs[f] = size
                    m = next((ln for ln in new.splitlines() if classify.LIMIT_RE.search(ln)), None)
                    if m:
                        state.update(why="limit", msg=m.strip())
                        return True
        return False
    return poll, state


WORKER_BEAT = []        # callbacks run with the wall time at a worker's heartbeat (run_once registers one: it stamps the lease)
LANE_GONE_EVERY = 10    # seconds between looks at whether the lane was marked out by someone else


def lane_gone_poll(lane, since):
    """-> (poll, state). poll() is truthy when another process marked `lane` out for a limit, credit or login reason after `since` (wall clock):
    the worker still running there is burning an exhausted lane and is stopped within LANE_GONE_EVERY seconds."""
    state, last = {"why": None, "msg": ""}, [None]

    def poll():
        if last[0] is not None and time.monotonic() - last[0] < LANE_GONE_EVERY:
            return False
        last[0] = time.monotonic()
        o = (load_state().get("out") or {}).get(lane) or {}
        if o.get("set", 0) >= since and o.get("until", 0) > now() and o.get("reason", "limit") in ("limit", "credit", "logged_out"):
            state.update(why="limit", msg=o.get("why") or "lane marked out")
            return True
        return False
    return poll, state


NET_GRACE_S = 15        # how long a worker may say "waiting for network" before one probe decides whether to stop it


def worker_watch(cli, lane, extra=None):
    """-> (poll, tick, state) for run_proc: stop the worker when its lane was marked out under it, when `extra` fires (opencode's log), or when it is
    hung (no first line within first_event_s, silence longer than the CLI's silence_s: lanes/supervisor.py). `tick` is for every line printed.
    state["why"] ends as "limit" | "hung"; "kind" says which deadline; "words" is the sentence for the answer; "msg" the limit line."""
    lim = supervisor.limits(cli)
    wd = supervisor.Watchdog(lim["first_event_s"], lim["silence_s"], beat=WORKER_BEAT[0] if WORKER_BEAT else None)
    gone, gstate = lane_gone_poll(lane, time.time())
    state = {"why": None, "msg": "", "kind": None, "words": "", "wd": wd}
    polls = [(gone, gstate)] + ([extra] if extra else [])

    def poll():
        for p_, st_ in polls:
            if p_():
                state.update(why=st_["why"], msg=st_["msg"])
                return True
        if wd.poll():
            state.update(why="hung", kind=wd.why, words=wd.describe())
            return True
        return False
    return poll, wd.touch, state


def stream_poll(poll0, turn, dog, live=None):
    """poll for run_proc: `poll0` (limit, silence), then the shared Turn rules: answer-then-grace and stall (lanes/supervisor.py, delegate-np7). The stall is
    reported as a hung worker of kind "stall"; the answer as turn.stop == "answer" (the launcher keeps the answer)."""
    def poll():
        if poll0():
            return True
        if live:
            live.poll(dog["wd"].idle_s)
        v = turn.verdict(dog["wd"])
        if v == "stall":
            dog.update(why="hung", kind="stall", words=turn.stall_words())
        return bool(v)
    return poll


def stalled_once_more(cli, turn, dog, a):
    """True when this run died on a stalled model stream (stall or silence deadline) and the one resume is allowed: setting `<cli>_resume_after_stall`, off by default
    (resuming a turn that never closed is untested for all three CLIs)."""
    stalled = turn.stop == "stall" or (dog["why"] == "hung" and dog["kind"] == "silence")
    return stalled and not getattr(a, "stalled_once", False) and bool(load_routing()["settings"].get(f"{cli}_resume_after_stall"))


RESUME_NOTE = "Your previous request stalled and was stopped. Continue and give your final answer."


def log_gaps(w, d):
    """After a worker run: one history row per inter-event gap over supervisor.LONG_GAP_S (role "gap", no run_id: the health fold ignores it). Never raises."""
    with contextlib.suppress(Exception):
        w["wd"].flush(history_add, d["lane"], d["model"], w["why"] is None)


def watch_fields(res, w):
    """What a hung worker's launcher tells settle (the sentence and the deadline) and the structured facts for classify."""
    if w["why"] == "hung":
        res["hung_words"], res["hung_kind"] = w["words"], w["kind"]
    return {"watchdog": w["why"], "msg": w["msg"]}


def opencode_exe():
    return shutil.which("opencode") or next((str(p) for p in [Path.home() / ".opencode" / "bin" / "opencode"] if p.is_file()), None)


def network_refusal(outp):
    """True when a command's output shows it was refused the network (the shell's sandbox blocks it): that is not an owner decision."""
    return any(x in outp for x in ("tcp connect error", "Network is unreachable", "Failed to connect", "error sending request"))


def opencode_shell(cwd, write, path):
    """The $SHELL OpenCode runs commands with: each one under sandbox-exec (no network, no secret paths or .env files, writes only in cwd and temp).
    OpenCode itself stays outside: it needs the network for its model, and macOS cannot nest a sandbox inside another."""
    H = Path.home()
    q = lambda x: json.dumps(os.path.realpath(str(x)))
    secrets = " ".join(f"(subpath {q(H / x)})" for x in OC_SECRETS)
    scratch = Path(cwd) / ".task"        # the task-loop engine's git-ignored scratch folder: a reviewer's test run writes its temp files there
    writable = [cwd if write else scratch] + ["/private/tmp", "/private/var/folders", H / "Library" / "Caches", H / ".cache"]
    keep = " ".join(f"(require-not (subpath {q(x)}))" for x in writable)
    prof = ('(version 1)(allow default)(deny network-outbound (remote ip))'
            f'(deny file-read* file-write* {secrets} (regex #"/\\.env$") (regex #"/\\.env\\.[^/]*$"))'
            '(allow file-read* (regex #"/\\.env\\.(example|sample|template|dist)$"))'
            f'(deny file-write* (require-all {keep} (require-not (regex #"^/dev/"))))' + ("" if write else f"(deny file-write* (require-all (subpath {q(cwd)}) (require-not (subpath {q(scratch)}))))"))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    head = f"export XDG_CONFIG_HOME={shlex.quote(xdg)}" if xdg else "unset XDG_CONFIG_HOME"      # commands get the user's config root back
    path.write_text(f"#!/bin/sh\n{head}\nexport SHELL=/bin/zsh\nexport UV_OFFLINE=1\nexec /usr/bin/sandbox-exec -p {shlex.quote(prof)} /bin/zsh \"$@\"\n", encoding="utf-8")
    path.chmod(0o700)


def opencode_readable_outside():
    """Folders outside cwd a read-only lane may read: the task-loop runs folder (the engine points reviewers at its files) and temp dirs."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    runs = (Path(xdg) if xdg else Path.home() / ".config") / "task-loop" / "runs"
    return [str(runs), "/private/tmp", "/private/var/folders", "/tmp", "/var/folders"]


def opencode_permissions(write):
    reads = {"*": "allow", ".env": "deny", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow", "*.env.sample": "allow", "*.env.template": "allow"}
    for x in OC_SECRETS:
        reads[str(Path.home() / x)] = reads[str(Path.home() / x) + "/*"] = "deny"
    return {"*": "allow", "read": reads, "webfetch": "deny", "websearch": "deny", "external_directory": "deny" if write else {"*": "deny", **{x + "/**": "allow" for x in opencode_readable_outside()}}, "task": "deny", **({} if write else {"edit": "deny"})}


def opencode_free_model(exe, model, env):
    """Fail closed if the exact free model disappears or its catalog costs change."""
    if model != "opencode/deepseek-v4.1-flash-free":
        return False
    try:
        x = subprocess.run([exe, "models", "opencode", "--verbose"], env=env,
                           capture_output=True, text=True, timeout=30)
        if x.returncode:
            return False
        marker = model + "\n"
        if marker not in x.stdout:
            return False
        obj, _ = json.JSONDecoder().raw_decode(x.stdout.split(marker, 1)[1].lstrip())
        cost = obj.get("cost") or {}
        cache = cost.get("cache") or {}
        return (obj.get("providerID") == "opencode" and obj.get("id") == model.split("/", 1)[1]
                and obj.get("status") == "active"
                and all(type(v) in (int, float) and v == 0 for v in
                        (cost.get("input"), cost.get("output"), cache.get("read"), cache.get("write"))))
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return False


def launch_opencode(d, brief, a, rid, t, meta):
    """OpenCode headless runner for subscription and isolated free-only lanes."""
    h = d["how"]
    exe = opencode_exe()
    if not exe or sys.platform != "darwin":
        return {"outcome": "setup", "say": f"{d['lane']} needs macOS and the opencode CLI (route.py doctor --lane {d['lane']})"}
    home = config_dir() / "opencode-home"       # an empty config root: the user's ~/.config/opencode (MCP servers, providers, permissions) stays out
    home.mkdir(parents=True, exist_ok=True)
    shell = config_dir() / f"opencode-shell-{rid[:8]}.sh"
    opencode_shell(h["cwd"], h["write"], shell)
    env = child_env({"SHELL": str(shell), "XDG_CONFIG_HOME": str(home), "OPENCODE_CONFIG_CONTENT": json.dumps({"permission": opencode_permissions(h["write"])}),
                     "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
                     "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_SHARE": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1"})
    if d["lane"] == "DEEPSEEK_FREE":
        # No saved provider logins or API keys: this lane cannot consume a paid balance.
        for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_AUTH_JSON"):
            env.pop(key, None)
        env["XDG_DATA_HOME"] = str(config_dir() / "opencode-free-data")
        env["XDG_CACHE_HOME"] = str(config_dir() / "opencode-free-cache")
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"permission": opencode_permissions(h["write"]), "enabled_providers": ["opencode"]})
        if not opencode_free_model(exe, h["model"], env):
            with contextlib.suppress(OSError):
                shell.unlink()
            until = t + 900
            mark_out(d["lane"], until, "exact free model unavailable or zero catalog price unverified", t, "limit", "guess")
            return {"outcome": "rate_limited", "retry_after": until,
                    "say": "Free DeepSeek is unavailable or its zero price could not be verified; rerun to use the next configured lane."}
    if d["lane"] == "ZAI_CODING_SUBSCRIPTION":
        # Pin subscription routing; never substitute the pay-as-you-go `zai` provider.
        if h["model"] not in ("zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"):
            with contextlib.suppress(OSError):
                shell.unlink()
            return {"outcome": "setup", "say": "Z.ai Coding Plan model must use the subscription provider."}
        for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_AUTH_JSON", "ZHIPU_API_KEY", "ZAI_API_KEY"):
            env.pop(key, None)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"permission": opencode_permissions(h["write"]),
            "enabled_providers": ["zai-coding-plan"]})
    cmd = [exe, "run", "--pure", "--format", "json", "-m", h["model"], "--variant", d["effort"], "--dir", h["cwd"]] + (["--session", a.resume] if a.resume else [])
    rules = " ".join((SECRET_RULE, GIT_RULES, EDIT_RULE, NO_SUBAGENTS_RULE, OC_RULE, DENIED_RULE))
    if not any((x / "AGENTS.md").is_file() for x in repo_dirs(h["cwd"])):
        rules += "\n\n" + repo_instruction_text(h["cwd"])         # opencode reads AGENTS.md itself; CLAUDE.md only through what is turned off
    if a.schema:
        rules += "\n\nYour final message must be only a JSON object matching this JSON Schema:\n" + Path(a.schema).read_text(encoding="utf-8")
    seen = {"ref": a.resume, "last": "", "tokens": None, "denied": [], "error": "", "work": False, "turns": 0, "ctx": None, "acct": None}
    if a.resume:
        note_ref(a.resume)

    poll, tick, dog = worker_watch("opencode", d["lane"], opencode_log_poll())
    WORKER_TICK[:] = [tick]
    turn = supervisor.Turn("opencode")
    live = live_for("opencode", d, a)
    poll = stream_poll(poll, turn, dog, live)

    def on_line(line):
        try:
            ev = json.loads(line)
        except ValueError:
            return None
        if not isinstance(ev, dict):
            return None
        live.feed(ev)
        if ev.get("sessionID") and not seen["ref"]:
            seen["ref"] = ev["sessionID"]
            note_ref(seen["ref"])
            if agent_runs:
                agent_runs.run(rid, session=seen["ref"])
        p = ev.get("part") or {}
        if ev.get("type") == "step_finish" and p.get("reason") in OC_FINAL_REASONS:
            turn.finished()         # the last step of the turn (a "tool-calls" step is followed by another); a later event resets the grace
        else:
            turn.more()
        if ev.get("type") == "tool_use" and (p.get("callID") or p.get("id")) and (p.get("state") or {}).get("status") in ("pending", "running", "completed", "error"):
            turn.tool(p.get("callID") or p.get("id"), (p.get("state") or {}).get("status") in ("pending", "running"))      # opencode prints a tool call when it ends: usually no open call is seen
        if ev.get("type") == "step_finish" and isinstance(p.get("tokens"), dict):
            u, c = p["tokens"], p["tokens"].get("cache") or {}
            tk = seen["tokens"] or {"in": 0, "cached": 0, "out": 0}
            seen["tokens"] = {"in": tk["in"] + (u.get("input") or 0) + (c.get("read") or 0) + (c.get("write") or 0), "cached": tk["cached"] + (c.get("read") or 0),
                              "out": tk["out"] + (u.get("output") or 0) + (u.get("reasoning") or 0)}
            seen["acct"] = accounting.opencode_step(seen["acct"], u, p.get("cost"))
            seen["turns"] += 1
            seen["ctx"] = round(((u.get("input") or 0) + (c.get("read") or 0) + (c.get("write") or 0)) / 1000, 1)
        if ev.get("type") == "text":
            seen["last"] = p.get("text", "")
            seen["work"] = True
        if ev.get("type") == "error":
            e = ev.get("error") or {}
            seen["error"] = str((e.get("data") or {}).get("message") or e.get("name") or e)
        if ev.get("type") == "tool_use":
            seen["work"] = True
            st = p.get("state") or {}
            inp, outp = st.get("input") or {}, str(st.get("output") or st.get("error") or "")
            if p.get("tool") == "bash" and secret_hit(inp.get("command")):      # reported after it ran: the sandbox already refused the read
                seen["denied"].append(denial("secret", "bash", inp))
                seen["secret"] = inp.get("command")
                return True
            if "prevents you from using this specific tool call" in outp:
                seen["denied"].append(denial("policy", p.get("tool"), inp))
            elif "Operation not permitted" in outp and not network_refusal(outp):
                seen["denied"].append(denial("sandbox_limit", p.get("tool"), inp))
        return None
    if agent_runs:
        agent_runs.run(rid, kind="opencode", lane=d["lane"], model=d["model"], cwd=h["cwd"], started=t, status="running", **meta)
    try:
        rc, out, err, timed_out = run_proc(cmd, rules + "\n\n" + brief, h["cwd"], env, a.timeout, on_line, poll)
        live_end(live, turn, dog, timed_out, rc)
        log_gaps(dog, d)
    finally:
        with contextlib.suppress(OSError):
            shell.unlink()
    if seen["ref"] and stalled_once_more("opencode", turn, dog, a):
        return launch_opencode(d, RESUME_NOTE, argparse.Namespace(**dict(vars(a), resume=seen["ref"], stalled_once=True)), rid, t, meta)       # once; off by default
    if seen["ref"]:     # opencode keeps sessions in its database: export one to a file, so `transcript` is a path like the other lanes'
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            x = run_tree([exe, "export", seen["ref"]], 60, env=env)
            if x.returncode == 0 and x.stdout.strip():
                f = opencode_transcript(seen["ref"])
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(x.stdout, encoding="utf-8")
                private(f)
    text = seen["last"].strip()
    res = {"text": text, "ref": seen["ref"], "stderr": err, "tokens": seen["tokens"], "denied": seen["denied"], "turns": seen["turns"] or None, "context_k": seen["ctx"],
           "started": bool(seen["ref"]), "partial": seen["work"], "acct": accounting.opencode_close(seen["acct"], d["lane"])}       # no `answered`: opencode has no resume-tested finished-answer signal (62-spikes-resume.md)
    res["cost"] = (res["acct"] or {}).get("cost_usd") or 0         # opencode's per-step cost where it states one (Grok); zai and free steps carry none
    why = seen["error"] or (err if rc != 0 else "")
    o = classify.classify(rc, why, "", timed_out, seen["work"], dict(watch_fields(res, dog), secret=seen.get("secret")))
    if turn.stop == "answer" and o and o.reason == "killed" and text and not seen["error"] and not timed_out:       # we stopped it after its last step: the answer stands
        o = None
        res["warnings"] = ["answer_done_process_alive"]
    if dog["why"] or turn.stop or timed_out:        # the raw tail of the stream, so the call record shows what the CLI last said before it was stopped
        res["stream_tail"] = supervisor.tail(out)
    if o:       # opencode's error event, stderr or log, never the model's answer
        hint = "free OpenCode access refused; no paid credentials will be used" if d["lane"] == "DEEPSEEK_FREE" else "run `opencode auth login` (xAI, SuperGrok Subscription)"
        if d["lane"] == "ZAI_CODING_SUBSCRIPTION":
            hint = "run `opencode auth login` and choose Z.AI Coding Plan; enter your Coding Plan key in your own terminal"
        settle(res, o, d, t, "opencode", a, rc, err or text, login=(hint, f"{d['lane']}: {hint}: {{why}}"))
    else:
        update_state(lambda s_: s_.setdefault("out", {}).pop(d["lane"], None))
        res.update(data=parse_data(text, a.schema))
    return res


def repo_instruction_text(cwd, cap=32768):
    """--restricted keeps Claude from loading the repo's CLAUDE.md; Codex reads AGENTS.md. Both lanes get the same files (repo_instructions),
    capped like Codex's project_doc_max_bytes."""
    out, left = "", cap
    for f in repo_instructions(cwd):
        with contextlib.suppress(OSError):
            body = f.read_text(encoding="utf-8", errors="replace")[:left]
            left -= len(body)
            out += f"\n\nRepository instructions ({f.name} in {f.parent}):\n{body}"
        if left <= 0:
            break
    return out


def launch_claude(d, brief, a, rid, t, cfg, meta):
    """claude -p on the credit key (CLAUDE_CREDIT) or the logged-in subscription (CLAUDE_SUBSCRIPTION, unattended). -> dict of result fields."""
    h = d["how"]
    credit = cfg["lanes"][d["lane"]]["kind"] == "credit"
    exe = shutil.which("claude")
    if not exe:
        return {"outcome": "setup", "say": "claude CLI not found on PATH"}
    extra, cap, nxt = {}, float(h.get("cap") or 2), None
    if credit:
        key = find_key()
        if not key:
            return {"outcome": "setup", "say": "no API key for the credit lane: route.py doctor --lane CLAUDE_CREDIT (never paste the key in chat)"}
        c = cfg["settings"]["credit"]
        start, nxt = credit_cycle(c["reset_day"], t)
        if ledger_spend(start) + cap > float(c["monthly_cap"]):
            return {"outcome": "rate_limited", "retry_after": nxt, "say": f"cycle cap: ${ledger_spend(start):.2f} spent + ${cap:.2f} > ${c['monthly_cap']}"}
        extra["ANTHROPIC_API_KEY"] = key
    write = bool(h["write"])
    tools = ["Read", "Glob", "Grep"] + (["Edit", "Write"] if write else []) + ["Bash"]       # read-only steps: Bash limited by the guard
    cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--model", h["model"], "--effort", d["effort"]]
    cmd += ["--max-budget-usd", str(cap)] if credit else []
    cmd += ["--resume", a.resume] if a.resume else ["--session-id", rid]
    cmd += ["--append-system-prompt", PREAMBLE + repo_instruction_text(h["cwd"]), "--restricted", "--tools", ",".join(tools), "--strict-mcp-config",
            "--permission-mode", "auto", "--permission-prompts", "none", "--settings", json.dumps(guard_settings(h.get("read_only")))]
    if a.schema:
        cmd += ["--json-schema", Path(a.schema).read_text(encoding="utf-8")]
    ref = a.resume or rid
    note_ref(ref)               # claude takes the session id from us (--session-id): known before the first event
    if agent_runs:
        agent_runs.run(rid, kind="claude-p", session=ref, lane=d["lane"], model=d["model"], cwd=h["cwd"], started=t, status="running", **meta)
    final, ctx, begun, said, streamed = [], [], [], [""], {}
    turn = supervisor.Turn("claude")
    live = live_for("claude", d, a)

    def on_line(line):      # the first result is the answer: claude -p then waits for background tasks and takes another turn on their exit
        with contextlib.suppress(ValueError):
            e = json.loads(line)
            if not isinstance(e, dict):
                return None
            live.feed(e)
            kind, msg = e.get("type"), e.get("message") if isinstance(e.get("message"), dict) else {}
            blocks = [b for b in msg.get("content") or [] if isinstance(b, dict)] if isinstance(msg.get("content"), list) else []
            if kind in ("assistant", "user", "result"):
                begun[:] = [True]           # the session exists on disk from its first message
            if kind == "result":
                if not final:
                    final.append(e)
                turn.finished()
            elif kind == "assistant":
                for b in blocks:
                    if b.get("type") == "tool_use":
                        turn.tool(b.get("id"), True)        # a running tool keeps the long silence limit
                    elif b.get("type") == "text" and b.get("text"):
                        said[0] = b["text"]
                if msg.get("stop_reason") == "end_turn" and not any(b.get("type") == "tool_use" for b in blocks):
                    turn.finished()         # the final assistant message; a later event resets the grace
                else:
                    turn.more()
                if isinstance(msg.get("usage"), dict):
                    u_ = msg["usage"]       # one model request: its whole input is the context size
                    streamed[msg.get("id") or len(streamed)] = u_
                    ctx[:] = [round(((u_.get("input_tokens") or 0) + (u_.get("cache_read_input_tokens") or 0) + (u_.get("cache_creation_input_tokens") or 0)) / 1000, 1)]
            else:
                for b in blocks:
                    if b.get("type") == "tool_result":
                        turn.tool(b.get("tool_use_id"), False)
                turn.more()
        return None
    poll, tick, dog = worker_watch("claude", d["lane"])
    WORKER_TICK[:] = [tick]
    rc, out, err, timed_out = run_proc(cmd, brief, h["cwd"], child_env(extra), a.timeout, on_line, stream_poll(poll, turn, dog, live))
    live_end(live, turn, dog, timed_out, rc, float((final[0] if final else {}).get("total_cost_usd") or 0) or None)
    log_gaps(dog, d)
    if stalled_once_more("claude", turn, dog, a) and begun:
        return launch_claude(d, RESUME_NOTE, argparse.Namespace(**dict(vars(a), resume=ref, stalled_once=True)), rid, t, cfg, meta)       # once; off by default
    j = final[0] if final else {}
    by_message = not final and turn.stop == "answer" and bool(said[0])         # the final assistant message came, the result event never did
    if (final and not j.get("is_error")) or by_message:
        rc = 0          # stopped by us after answering
    text = j.get("result") if isinstance(j.get("result"), str) else said[0] if by_message else out.strip()[-2000:]
    cost = float(j.get("total_cost_usd") or 0)          # the harness's own figure on every lane, subscription included (study 85)
    if credit and cost > 0:
        ledger_add(cost, d["model"], t)
    u = j.get("usage") if isinstance(j.get("usage"), dict) else accounting.claude_stream(streamed.values())      # no result event: what the stream's requests reported
    res = {"text": text, "ref": j.get("session_id") or ref, "cost": cost, "stderr": err, "tokens": claude_tokens(u),
           "acct": accounting.claude(u, j.get("total_cost_usd"), credit),
           "denied": [denial("secret" if x.get("tool_name") == "Bash" and secret_hit((x.get("tool_input") or {}).get("command")) else "policy", x.get("tool_name"), x.get("tool_input"))
                      for x in j.get("permission_denials") or [] if isinstance(x, dict) and not policy_denial(x, h.get("read_only"))]}
    res.update(turns=j.get("num_turns"), context_k=ctx[0] if ctx else None, started=bool(begun), partial=bool(begun))
    if dog["why"] or turn.stop or timed_out:        # the raw tail of the stream, so the call record shows what the CLI last said before it was stopped
        res["stream_tail"] = supervisor.tail(out)
    failed = rc != 0 or bool(j.get("is_error"))
    o = classify.classify(rc or (1 if failed else 0), classify.claude_vendor_error(j), err, timed_out, (j.get("num_turns") or 0) > 1,
                          dict(watch_fields(res, dog), http_status=j.get("api_error_status"))) if failed or timed_out else None
    if o:       # the CLI's own error and stderr, never the model's answer (a worker may write "rate limit")
        settle(res, o, d, t, "claude", a, rc, text, credit_until=nxt, login=("logged out: run `claude auth login`", "claude is not logged in: {why} (claude auth login)"))
    else:
        update_state(lambda s: s.setdefault("out", {}).pop(d["lane"], None))
        data = j.get("structured_output") if isinstance(j.get("structured_output"), (dict, list)) else parse_data(text, a.schema)
        res.update(data=data)
        if by_message:
            res["warnings"] = ["answer_done_process_alive"]
        if final and netprobe.looks_dropped(err):       # the answer was in before the connection broke (ae6v): done, with a note
            res["warnings"] = ["net_after_answer"]
    return res


def policy_denial(x, read_only=False):
    """A git write the coordinator forbids, or a write on a read-only step, is delegate's policy, not a decision for the owner (a secret path stays one)."""
    cmd = ((x.get("tool_input") or {}).get("command") or "").strip()
    return x.get("tool_name") == "Bash" and not secret_hit(cmd) and bool(git_write(cmd) or read_only and read_only_problem(cmd))


def claude_tokens(u):
    """Claude's input_tokens leaves out cache reads and cache writes; `in` counts all input, as codex's does."""
    if not u:
        return None
    cached = u.get("cache_read_input_tokens") or 0
    return {"in": (u.get("input_tokens") or 0) + cached + (u.get("cache_creation_input_tokens") or 0),
            "cached": cached, "out": u.get("output_tokens")}


profiles.register("codex", "codex", "codex", lambda d, b, a, rid, t, cfg, meta: launch_codex(d, b, a, rid, t, meta))
profiles.register("opencode", "opencode", "opencode", lambda d, b, a, rid, t, cfg, meta: launch_opencode(d, b, a, rid, t, meta), resume="untested")
profiles.register("claude_p", "claude", "claude", launch_claude)
LAUNCHERS = profiles.launchers()      # by how()'s `kind`; an unknown kind is a setup error, not a silent `claude -p`


def transcript_info(path, family, start=0):
    """-> {path, kind, from, to, bytes}: the transcript file and the byte window this call wrote (a resumed thread appends to one file), or None."""
    if not path:
        return None
    try:
        size = Path(path).stat().st_size
    except OSError:
        return None
    kind = {"claude": "claude", "openai": "codex"}.get(family, "opencode_export")
    frm = start if kind != "opencode_export" and 0 <= start <= size else 0          # an opencode export is rewritten whole each run
    return {"path": path, "kind": kind, "from": frm, "to": size, "bytes": size - frm}


def file_size(path):
    try:
        return Path(path).stat().st_size if path else 0
    except OSError:
        return 0


def result(outcome=None, **kw):
    """The contract 3 answer. `outcome` is the contract.json reason behind a non-done answer (lanes/classify.py); None = done, or the agent-tool
    handoff (`agent_model` set). `stop` is null exactly when outcome is None; its facts (scope, permanence, retry) come from the reason's row, and
    `retry_after` (a kw, never an answer field) becomes `stop.retry.at`."""
    retry_after, fix = kw.pop("retry_after", None), kw.pop("fix", None)
    resumable, partial = kw.pop("resumable", None), kw.pop("partial", None)
    stop = None
    if outcome:
        stop = classify.stop_facts(outcome, None if outcome == "logged_out" else retry_after, fix)      # logged_out ends when the owner logs in, not at a time
        stop["ref"] = kw.get("ref")        # ref on every outcome that has one
        if outcome in ("network", "killed"):       # resumable: the vendor session can be continued; partial: work was done before the stop
            stop.update(resumable=bool(resumable), partial=bool(partial))
    r = {"contract": classify.CONTRACT["contract"], "data": None, "text": "", "ref": None, "lane": None, "model": None, "effort": None, "family": None,
         "flag": None, "cost": 0, "tokens": None, "denied": [], "secs": 0, "run_id": None, "transcript": None, "say": "", "git_violation": [],
         "call_id": None, "transcript_info": None, "transcript_status": None, "stop": stop,
         "usage": {"tokens": None, "cost": 0, "secs": 0, "turns": None, "context_k": None, "answer_bytes": 0, **accounting.fields(None, None, 0)}}
    r.update(kw)
    return r


def emit(r):
    """One object on stdout. A model reads it when a Claude session is behind us, so empty fields are dropped there; scripts get every field."""
    if interactive():
        r = {k: v for k, v in r.items() if k in ("contract", "stop") or v not in (None, "", [], 0) and k not in ("context", "usage", "transcript_info", "transcript_status")}     # these are for scripts
    print(json.dumps(r))
    return 0        # exit 0 whenever the object was printed; non-zero only if route.py itself crashed


HISTORY_MAX, HISTORY_DAYS = 1_000_000, 14


def history_path():
    return config_dir() / "history.jsonl"


def history_add(rec):
    """One line per finished run, kept for calibration. Never holds the brief; never fails a run."""
    with contextlib.suppress(Exception):
        with locked(history_path()):
            p = history_path()
            if p.exists() and p.stat().st_size > HISTORY_MAX:       # rotate: keep the last HISTORY_DAYS days
                cut = time.time() - HISTORY_DAYS * 86400
                keep_ = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln and (health.row_ts(ln) or 0) >= cut]       # a corrupt line is dropped, never fatal
                p.write_text("\n".join(keep_) + ("\n" if keep_ else ""), encoding="utf-8")
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            private(p)


def history_cumulative(ref):
    """The thread total the newest history row for `ref` recorded: where the rollout file is gone, a resume still knows what came before."""
    with contextlib.suppress(Exception):
        rows = [json.loads(ln) for ln in history_path().read_text(encoding="utf-8", errors="replace").splitlines()[-2000:] if ref in ln]
        return accounting.last_cumulative(rows, ref)
    return None


def lane_use(cfg, name, t):
    """(five_hour %, weekly %) of a lane now; None where unknown."""
    c = cfg["lanes"][name]
    lane = Lane(name, c["family"], c["kind"], c.get("shared_with_orchestrator", False))
    with contextlib.suppress(Exception):
        READERS[c["reader"]](lane, cfg["settings"], load_state(), t)
    return lane.five, lane.week


def cmd_contract(a, cfg):
    """The handshake: the contract number, every reason with its facts, and the flags `run` accepts."""
    print(json.dumps({"contract": classify.CONTRACT["contract"], "reasons": classify.CONTRACT["reasons"], "run_args": classify.CONTRACT["run_args"]}))
    return 0


def refuse(a, outcome, say, retry_after, extra, **who):
    """An answer given without launching a worker; history keeps it too, so refusals are countable."""
    call_id = getattr(a, "call_id", None)
    history_add({"ts": now(), "run_id": None, "call_id": call_id, **health.row_fields(outcome, "", say, retry_after), "role": a.role, "task": getattr(a, "task", None) or "",
                 "lane": who.get("lane"), "model": who.get("model"), "resume": bool(getattr(a, "resume", None))})
    return emit(result(outcome, say=say, retry_after=retry_after, call_id=call_id, transcript_status="not_launched", **who, **extra))


def cmd_run(a, cfg):
    try:
        return run_once(a, cfg)
    except TimeoutError as e:       # state lock held too long: transient, so stop with a retry time (error means permanent)
        return refuse(a, "lock", str(e), now() + random.randint(60, 120), {})


CANARY_BRIEF = "Reply with exactly: OK"
CANARY_TIMEOUT_S = 30


def canary(launch, d, rid, a):
    """The pair's bench ended (or its lane's limit did): one 30 s probe before full traffic. -> None when it answered (a `done` row in history ends
    the ladder), else the failed result, which is the answer to the caller: the run's own history row then starts the next rung."""
    res = launch(CANARY_BRIEF, argparse.Namespace(**dict(vars(a), resume=None, schema=None, timeout=CANARY_TIMEOUT_S)))
    if res.get("outcome") is None:
        history_add({"ts": now(), "run_id": str(uuid.uuid4()), "role": "canary", "lane": d["lane"], "model": d["model"], "done": True, "secs": None, "v": health.V})
        return None
    if res["outcome"] == "timeout":      # a probe that did not answer in time is hung: `timeout` is never counted, `hung` is
        res.update(outcome="hung", retry_after=now() + 60, say=f"canary: {d['lane']}/{d['model']} did not answer within {CANARY_TIMEOUT_S}s")
    else:
        res["say"] = "canary failed: " + (res.get("say") or "")
    return res


def run_once(a, cfg):
    unattended = not interactive()
    d, lanes, warnings, t = decision(a, cfg)
    extra = {}
    if warnings:
        extra["warnings"] = warnings
    if d.get("notice"):
        extra["notice"] = d["notice"]
    if d.get("warning"):
        extra["warning"] = d["warning"]
        print("warning: " + d["warning"], file=sys.stderr)
    if d["action"] == "error":
        return refuse(a, "caller", d["error"], None, extra)
    if d["action"] == "stop":
        if d.get("outcome"):        # --resume on a lane that is out, benched or held
            return refuse(a, d["outcome"], d["say"], d.get("retry_after"), extra, lane=d["lane"])
        retry = min([x for x in [back_at(l, cfg["settings"]) for l in lanes.values()] + [b["until"] for l in lanes.values() for b in l.benched.values()] if x] or [None])
        if not unattended and d.get("ask_user"):
            extra["ask_user"] = d["ask_user"]
        if d.get("reason") == "busy":
            return refuse(a, "busy", d["say"], d["retry_after"], extra, lane=d.get("lane"))
        return refuse(a, "no_lane", d["say"], retry, extra)
    h = d["how"]
    fam = cfg["lanes"][d["lane"]]["family"]
    who = dict(lane=d["lane"], model=d["model"], effort=d["effort"], family=fam, flag=d.get("flag"),
               rung=d.get("rung"), fallback=d.get("fallback"), fallback_reason=d.get("fallback_reason"), first_lane=d.get("first_lane"))
    if not unattended:
        ni = [n["what"] for n in needs_input(lanes)]
        if ni:
            extra["needs_input"] = ni
    runner = LAUNCHERS.get(h["kind"])
    if runner is None and h["kind"] != "agent_tool":
        return refuse(a, "setup", f"no launcher for runner kind {h['kind']!r}: route.py has codex, opencode and claude_p", None, extra, **who)
    if h["kind"] == "agent_tool":
        return emit(result(None, **who, call_id=getattr(a, "call_id", None), transcript_status="unsupported", agent_model=h["model"], brief_prefix=SECRET_RULE + " " + GIT_RULES, cwd=h["cwd"], **extra))
    brief = Path(a.brief).read_text(encoding="utf-8", errors="replace") if a.brief else sys.stdin.read()
    if not brief.strip():
        return emit(result("caller", **who, call_id=getattr(a, "call_id", None), say="empty brief: pass it on stdin or with --brief FILE", **extra))
    cwd = h["cwd"]
    rid = str(uuid.uuid4())
    S, lane = cfg["settings"], lanes[d["lane"]]
    # A limit refusal mid-run lasts until the time its message names, else the soonest window reset (else an hour).
    resets = [(lane.week or 0, lane.week_reset), (lane.five or 0, lane.five_reset)]
    d["limit_reset"] = min((r for _, r in resets if r and r > t), default=None)     # a guess: the soonest reset (too early costs one refused call, too late days)
    cap, taken = slots(lane, S), {}
    canary_due = bool(lane.canary.get(d["model"])) and h["kind"] in LAUNCHERS
    silence_s = supervisor.LIMITS[supervisor.CLI_OF_KIND[h["kind"]]]["silence_s"]

    def claim(st):      # the authoritative count: inside the lock, never from the snapshot decision() read
        live = live_leases(st, now())
        for k, v in (st.get("leases") or {}).items():
            if orphan(v):
                gone = reap(v)
                history_add({"ts": now(), "run_id": k, "role": v.get("role"), "task": v.get("task", ""), "lane": v.get("lane"), "reaped": gone, "worker_pid": v.get("worker_pid")})
                if gone:
                    live.pop(k, None)
                else:           # still running: its lease stays, so the same task or a full lane waits
                    live[k] = dict(v, expires=now() + LEASE_TTL)
            elif k not in live and v.get("pid_start") and proc_start(v.get("pid")) == v["pid_start"]:
                live[k] = v     # expired only because route.py could not renew it (the laptop slept): it is still running
        st["leases"] = live
        if a.task and any(v.get("task") == a.task for v in live.values()):
            taken["no"] = ("duplicate_task", f"task {a.task} already has a running worker")
        elif canary_due and any(v.get("canary") == d["model"] and v["lane"] == d["lane"] for v in live.values()):
            taken["no"] = ("busy", f"{d['lane']}/{d['model']} has its canary running; wait for it")
        elif cap is not None and sum(1 for v in live.values() if v["lane"] == d["lane"] and not suspect(v, now())) >= cap:
            taken["no"] = ("busy", f"{d['lane']} has all {cap} slots taken")
        else:
            taken["peers"] = sorted(live)
            live[rid] = {"lane": d["lane"], "role": a.role, "pid": os.getpid(), "pid_start": me_start, "start": now(), "expires": now() + LEASE_TTL, "task": a.task or "",
                         "silence_s": silence_s, **({"canary": d["model"]} if canary_due else {})}
    me_start = proc_start(os.getpid())
    update_state(claim)
    if "no" in taken:
        return refuse(a, taken["no"][0], taken["no"][1], now() + random.randint(60, 120), extra, **who)
    beat = threading.Event()

    def keep_lease():
        def renew(st):
            if rid in (st.get("leases") or {}):
                st["leases"][rid]["expires"] = now() + LEASE_TTL
        while not beat.wait(LEASE_RENEW):
            with contextlib.suppress(Exception):
                update_state(renew)
    threading.Thread(target=keep_lease, daemon=True).start()

    def worker_started(pid):
        """The worker runs in its own process group (pgid = pid), so a SIGKILL on route.py leaves it running: say where it is, before any
        work, on stderr and in the lease (status --json `running`), so the caller can kill the group itself."""
        print(f"delegate: worker pid={pid} pgid={pid} run_id={rid}", file=sys.stderr, flush=True)

        started_at = proc_start(pid)

        def note(st):
            if rid in (st.get("leases") or {}):
                st["leases"][rid].update(worker_pid=pid, worker_start=started_at)
        update_state(note)
    WORKER_STARTED[:] = [worker_started]

    def stamp_beat(wall):
        """The worker printed something: stamp the lease (at most every 30 s)."""
        def stamp(st):
            if rid in (st.get("leases") or {}):
                st["leases"][rid]["last_event"] = wall
        update_state(stamp)
    WORKER_BEAT[:] = [stamp_beat]
    ref_rec = {"lane": d["lane"], "model": d["model"], "effort": d["effort"], "mode": d["mode"], "cap": d.get("cap"),
               "rung": d.get("rung"), "fallback": d.get("fallback"), "fallback_reason": d.get("fallback_reason"), "first_lane": d.get("first_lane")}

    def keep_ref(ref):
        """Remember the ref for `--resume` whatever the outcome."""
        def keep(s):
            refs = s.setdefault("refs", {})
            refs[ref] = ref_rec
            for k in list(refs)[:-200]:
                refs.pop(k)
        update_state(keep)

    def ref_seen(ref):
        """The vendor revealed its session id: store it, stamp the lease, and print it on stderr (the engine reads `delegate: ref=`) before any work is lost."""
        keep_ref(ref)
        print(f"delegate: ref={ref} run_id={rid}", file=sys.stderr, flush=True)

        def stamp(st):
            if rid in (st.get("leases") or {}):
                st["leases"][rid]["ref"] = ref
        update_state(stamp)
    REF_SEEN[:] = [ref_seen]
    meta = {"parent_session": a.parent or "", "title": a.title or brief.strip().splitlines()[0][:120]}
    if a.task:
        meta["task"] = a.task
    git_before = git_state(cwd)
    t0, ends = time.time(), {}
    launch = lambda b, a_: runner(d, b, a_, rid, t, cfg, meta)
    t_from = file_size(find_transcript(a.resume, fam)) if a.resume else 0
    try:
        res = canary(launch, d, rid, a) if canary_due else None      # the one probe before full traffic; None = it passed
        res = res or launch(brief, a)
        if res.get("outcome") == "network" and not res.pop("started", False) and net_up(d["lane"]):
            res = launch(brief, a)      # dropped before the vendor had a session and the net is back: nothing to resume, nothing lost, so run it again, once
        res.pop("started", None)
        res.pop("answered", None)
        if res.get("outcome") is None and a.schema:     # delegate enforces the schema: one re-ask in the same thread, then schema_invalid
            schema = json.loads(Path(a.schema).read_text(encoding="utf-8"))
            why = "the answer is not JSON" if res.get("data") is None else schema_problem(res["data"], schema)
            if why and res.get("ref"):
                again = launch(f"Your final answer does not match the required JSON schema: {why}. Reply again with only the JSON object.",
                               argparse.Namespace(**dict(vars(a), resume=res["ref"])))
                tok = [x for x in (res.get("tokens"), again.get("tokens")) if x]
                again["tokens"] = {k: sum(x.get(k) or 0 for x in tok) for k in ("in", "cached", "out")} if tok else None
                again["acct"] = accounting.merge(res.get("acct"), again.get("acct"))
                again["cost"] = (res.get("cost") or 0) + (again.get("cost") or 0)
                again["cumulative"] = again.get("cumulative") or res.get("cumulative")
                again["denied"] = (res.get("denied") or []) + (again.get("denied") or [])
                res = again
                why = None if res.get("outcome") else "the answer is not JSON" if res.get("data") is None else schema_problem(res["data"], schema)
            if why and res.get("outcome") is None:
                res.update(outcome="schema_invalid", say=f"the answer does not match the schema: {why}")
    except Exception as e:      # a launcher bug is an error object, not a stack trace
        res = {"outcome": "crash", "say": f"{type(e).__name__}: {e}"}
    finally:
        beat.set()

        def release(st):
            ends["peers"] = [k for k in live_leases(st, now()) if k != rid]
            (st.get("leases") or {}).pop(rid, None)
        with contextlib.suppress(Exception):
            update_state(release)
    err = res.pop("stderr", "")
    if err:
        print(err[-2000:], file=sys.stderr)
    done = res.get("outcome") is None
    if done and d["lane"] in ("CLAUDE_SUBSCRIPTION", "OPENAI_SUBSCRIPTION"):
        write_auth(d["lane"], True, "a worker run succeeded", now())
    if agent_runs:
        agent_runs.run(rid, ended=now(), status="completed" if done else "failed", transcript=find_transcript(res.get("ref"), fam))
    if res.get("ref"):          # every outcome: a stop, a failure and a timeout can all be resumed
        with contextlib.suppress(Exception):
            keep_ref(res["ref"])
    secs = round(time.time() - t0, 1)
    five_after, week_after = lane_use(cfg, d["lane"], now())
    transcript = find_transcript(res.get("ref"), fam)
    detail = res.pop("detail", "")
    history_add({"ts": now(), "run_id": rid, "call_id": getattr(a, "call_id", None), "ref": res.get("ref"), **health.row_fields(res.get("outcome"), "", detail or res.get("say"), res.get("retry_after")),
                 "role": a.role, "task": a.task or "", "lane": d["lane"], "model": d["model"], "effort": d["effort"],
                 "done": done, "warnings": res.get("warnings"), "stream_tail": res.pop("stream_tail", None), "denials": sum(1 for x in res.get("denied") or [] if x.get("kind") != "sandbox_limit"), "secs": secs, "cost": res.get("cost", 0), "tokens": res.get("tokens"), "cumulative": res.get("cumulative"),
                 "five_before": lane.five, "week_before": lane.week, "five_after": five_after, "week_after": week_after,
                 "overlap": sorted(set(taken["peers"]) | set(ends.get("peers", []))), "slots": cap, "transcript": transcript})
    info = transcript_info(transcript, fam, t_from)
    ac = res.pop("acct", None)
    res.pop("cumulative", None)
    usage = {"tokens": dict(res["tokens"], **accounting.token_fields(ac)) if res.get("tokens") else None, "cost": res.get("cost", 0), "secs": secs,
             "turns": res.pop("turns", None), "context_k": res.pop("context_k", None), "answer_bytes": len((res.get("text") or "").encode("utf-8")),
             **accounting.fields(ac, res.get("ref"), time.time())}
    if res.get("warnings"):      # the launcher's own (net_after_answer) join the router's
        extra["warnings"] = list(extra.get("warnings") or []) + res.pop("warnings")
    res.update(**who, run_id=rid, secs=secs, git_violation=git_changes(git_before, git_state(cwd)),
               context={"instructions": [str(f) for f in repo_instructions(cwd)], "env_keys": sorted(child_env().keys())},
               transcript=transcript, call_id=getattr(a, "call_id", None), usage=usage, transcript_info=info,
               transcript_status="ok" if info else "ref_unresolvable" if res.get("ref") else "died_before_session_id", **extra)
    return emit(result(res.pop("outcome", None), **res))


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
    for c in d.get("checks") or []:
        if isinstance(c, dict) and c.get("id") == "claude_login" and c.get("ok") is True:       # only a positive answer: doctor cannot tell "logged out" from "no CLI" or "unreadable"
            write_auth("CLAUDE_SUBSCRIPTION", c.get("ok"), "doctor", now())
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

class CallParser(argparse.ArgumentParser):
    """A bad flag, a missing argument or a bad value in `run` is the caller's fault: answered as a bad_request stop (exit 0, no usage text, no
    vendor started), so the engine never has to read English to tell it from a crash. Other commands keep argparse's own exit 2."""
    answers = False

    def error(self, message):
        if not CallParser.answers:
            super().error(message)
        emit(result("bad_request", say=f"route.py: {message}", fix=f"update task-loop or delegate: {message}"))
        sys.exit(0)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):      # the cockpit prints emoji: a Windows console on cp1252 must not crash on them
        with contextlib.suppress(Exception):
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
    CallParser.answers = (sys.argv[1:] if argv is None else list(argv))[:1] == ["run"]
    ap = CallParser(prog="route.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--claude-usage"); s.add_argument("--json", action="store_true"); s.add_argument("--cheap", action="store_true", help="no process spawns: last status snapshot plus cached logins, with age_s")
    n = sub.add_parser("next"); n.add_argument("role"); n.add_argument("--authored-by"); n.add_argument("--failed", default="")
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
    sub.add_parser("guard").add_argument("--read-only", action="store_true")
    nt = sub.add_parser("net", help="is the machine online? stdlib sockets, no quota"); nt.add_argument("--lane"); nt.add_argument("--host", action="append", help="a host or host:port; repeatable")
    sub.add_parser("contract").add_argument("--json", action="store_true")
    cf = sub.add_parser("config"); cf.add_argument("action", choices=["set", "show"]); cf.add_argument("key", nargs="?"); cf.add_argument("value", nargs="?")
    ru = sub.add_parser("run"); ru.add_argument("role"); ru.add_argument("--authored-by"); ru.add_argument("--failed", default="")
    ru.add_argument("--premium", nargs="?", const="claude", metavar="FAMILY"); ru.add_argument("--allow-degraded", action="store_true")
    ru.add_argument("--claude-usage"); ru.add_argument("--cwd"); ru.add_argument("--brief"); ru.add_argument("--timeout", type=int, help="seconds; none by default"); ru.add_argument("--unattended", action="store_true")
    ru.add_argument("--schema"); ru.add_argument("--resume"); ru.add_argument("--cap", type=float); ru.add_argument("--parent"); ru.add_argument("--task"); ru.add_argument("--title")
    ru.add_argument("--call-id", help="the caller's id for this call: echoed in the answer and written to history.jsonl, the key that joins the caller's record to ours")
    ho = sub.add_parser("hold", help="stop a lane for some roles until a time; --until is mandatory"); ho.add_argument("lane"); ho.add_argument("--until"); ho.add_argument("--roles")
    ho.add_argument("--reason", default=""); ho.add_argument("--set-by", default="owner")
    ho.add_argument("--auto-release-below", type=float, metavar="PCT", help="end the hold by itself, without an ask, when the lane shows less than PCT used")
    ho.add_argument("--release-below", type=float, metavar="PCT", help="raise the one 'hold is stale' ask when the lane shows less than PCT used (default 5)")
    sub.add_parser("release", help="end a hold early (by id or lane)").add_argument("target")
    sub.add_parser("holds", help="every hold in force, with holder and expiry")
    a = ap.parse_args(argv)
    global UNATTENDED
    UNATTENDED = getattr(a, "unattended", False)
    if a.cmd == "guard":        # a hook: no config, no state
        return cmd_guard(a)
    if a.cmd == "net":
        return cmd_net(a)
    with contextlib.suppress(Exception):
        migrate_legacy_hold(now())      # a codex_plan_review_only flag left in config.json becomes a hold with an end
    cfg = load_routing()
    return {"status": cmd_status, "next": cmd_next, "report": cmd_report, "capture-limits": cmd_capture,
            "run": cmd_run, "contract": cmd_contract, "doctor": cmd_doctor, "set-key": cmd_set_key, "config": cmd_config,
            "hold": cmd_hold, "release": cmd_release, "holds": cmd_holds}[a.cmd](a, cfg)


if __name__ == "__main__":
    sys.exit(main())
