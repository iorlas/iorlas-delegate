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
  route.py report --lane LANE --event out|ok [--until EPOCH|ISO]
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
import collections
import contextlib
import copy
import dataclasses
import datetime as dt
from dataclasses import replace
import json
import os
import random
import re
import select
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
sys.path.insert(0, str(HERE.parents[1] / "src"))     # delegate.*: the typed records and the harness classes ship in the plugin dir, next to skills/
from lanes import classify                  # the one outcome ladder (delegate-e05)
from delegate.adapters import netprobe, procs
from lanes import profiles, usage as accounting                # network facts (4n7); the lane adapter registry (dlz)
from lanes import cockpit as role_view              # roles{} for status (0sf)
from delegate.adapters.harness import reset_parse
from delegate.adapters.harness.base import LaunchInputs
from delegate.adapters.harness.claude import ClaudeHarness
from delegate.adapters.harness.opencode import OpenCodeHarness, Profile
from delegate.adapters.harness import opencode_sandbox
from delegate.adapters.harness.codex import CodexHarness
from delegate.app import watch
from delegate.app.publish import LiveSink, end_status
from delegate.app.supervisor import LogWatcher, RunHooks, Supervisor
from delegate.app.fleet import AuthCache, FleetBuilder, drift_warnings, mark_busy, settle_holds
from delegate.contracts import codec, wire
from delegate.contracts import events as harness_events
from delegate.contracts import history as hist
from delegate.contracts.routing import CallArgs, Error, LaneOut, Routing, Run, Stop, split_retired
from delegate.contracts.state import Lease, OutState, RefRecord, UsageCache
from delegate.contracts.stores import CheckpointStore, ConfigStore, HistoryStore, LedgerStore, StateStore, locked, private
from delegate.domain.cost.cycle import credit_cycle
from delegate.domain.cost.ledger import CostLedger
from delegate.domain.health import ledger as health_ledger
from delegate.domain.holds.book import HoldBook, new_hold
from delegate.domain.lanes.lane import Lane, apply_burn, window
from delegate.domain.lanes.leases import LEASE_RENEW, LEASE_TTL, LeaseBook, suspect
from delegate.domain.policy import worker as policy
from delegate.domain.policy.matchers import heredoc_only_hit, secret_hit
from delegate.domain.policy.worker import APPLY_PATCH_HINT, EDIT_RULE, GIT_RULES, SECRET_RULE
from delegate.domain.routing.router import Router, callable_lanes
from delegate.domain.time.spec import WEEK_MIN, fmt_dur, fmt_in, local, next_weekly, parse_until, to_epoch, when


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
    with contextlib.suppress(OSError):
        os.chmod(p, 0o700)        # holds the API key file, state and ledger: private to the user
    return p


def deep_merge(a, b):
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_RETIRED_WARNED = set()


def load_routing():
    """routing.json with the owner's overrides (config.json, minus its `profile`) merged into `settings`, validated into contracts.routing.Routing.
    Retired settings keys are dropped with one stderr line each (stdout carries the answer); any other unknown key fails."""
    with open(HERE / "routing.json", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["settings"], _ = split_retired(deep_merge(cfg["settings"], config_store().overrides()))
    for k in retired_keys_present():
        if k not in _RETIRED_WARNED:
            _RETIRED_WARNED.add(k)
            print(f"route.py: {config_store().path}: '{k}' is retired and ignored; it can be deleted from config.json", file=sys.stderr)
    typed_routing(cfg)
    return cfg


def retired_keys_present():
    """The retired settings keys config.json (the owner's file) still carries."""
    return split_retired(config_store().overrides())[1]


def typed_routing(cfg):
    try:
        return codec.from_json(Routing, cfg)
    except codec.CodecError as e:
        sys.exit(f"route.py: routing.json / config.json is not a valid routing table: {e}. "
                 f"Fix: delete the key from {config_store().path} (or rename it to a known settings key; see `route.py config show`)")


# ----------------------------------------------------------------------------- the files under the config dir (each behind its store)

def states():
    return StateStore(config_dir() / "state.json")


def config_store():
    return ConfigStore(config_dir() / "config.json")


def history_path():
    return config_dir() / "history.jsonl"


def history_store():
    return HistoryStore(history_path())


def ledger_store():
    return LedgerStore(config_dir() / "ledger.jsonl")


def load_state():
    return states().load()


def update_state(fn):
    return states().update(fn)


def read_config():
    return config_store().read()


def update_config(fn):
    return config_store().update(fn)


def profile_lanes():
    return dict(config_store().profile().lanes)


def set_profile_lane(name, value):
    config_store().set_lane(name, value)


def acked():
    return set(config_store().profile().ack)


def set_ack(ident):
    config_store().set_ack(ident)


def history_add(row):
    history_store().add(row)


def ledger_add(cost, model, now_t):
    ledger_store().add(cost, model, now_t)


def ledger_spend(since_epoch):
    return CostLedger(ledger_store().rows()).spend(since_epoch)


def ledger_last_ts():
    return CostLedger(ledger_store().rows()).last_ts()


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


# ----------------------------------------------------------------------------- lane state

# ----------------------------------------------------------------------------- readers (the script gathers what it can)

APP_SERVER_S = 8          # seconds each codex app-server reply may take


def fetch_codex_limits():
    """-> (result_dict, error_text). Spawns `codex app-server` once; ~1-2 s."""
    exe = shutil.which("codex")
    if not exe:
        return None, "codex CLI not installed"
    try:
        p = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        return None, f"codex app-server failed to start: {e}"
    pending = bytearray()

    def send(m):
        p.stdin.write((json.dumps(m) + "\n").encode())
        p.stdin.flush()

    def read(i, seconds):
        """The reply with id `i`, else None at the deadline or EOF: the wait itself is bounded (select), not only the gaps between lines."""
        end = time.monotonic() + seconds
        fd = p.stdout.fileno()
        while True:
            while b"\n" in pending:
                line, _, rest = bytes(pending).partition(b"\n")
                pending[:] = rest
                with contextlib.suppress(ValueError):
                    m = json.loads(line)
                    if isinstance(m, dict) and m.get("id") == i:
                        return m
            left = end - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                return None
            chunk = os.read(fd, 65536)
            if not chunk:
                return None
            pending.extend(chunk)

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "delegate-route", "title": "delegate route", "version": "1"}}})
        if read(1, APP_SERVER_S) is None:
            return None, "codex app-server did not answer"
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        r = read(2, APP_SERVER_S)
    except BrokenPipeError:         # the server exited under us
        return None, "codex app-server closed its input"
    finally:
        procs.kill_tree(p)
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


def read_codex(lane, S, st, t, ai_usage=None):
    cached = st.codex_limits
    if cached and t - cached.at <= 60 and not os.environ.get("DELEGATE_NOCACHE"):
        res, err = cached.data, None
    else:
        res, err = fetch_codex_limits()
        if res is not None:
            update_state(lambda s_: setattr(s_, "codex_limits", UsageCache(at=t, data=res)))
    if res is None:
        lane.notes.append(err)
        return
    lane.read_at = cached.at if cached and res is cached.data else t
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


AUTH_TTL = 300          # a login check answers for this long: it spawns a CLI, cheap but not free
AUTH_TIMEOUT = 10
AUTH_FILE_TTL = 600     # what a non-cheap status, doctor, a login probe and a successful run left, as read by `next` and `status --cheap`


def auth_cache():
    return AuthCache(states(), not os.environ.get("DELEGATE_NOCACHE"))


def claude_logged_out():
    """True only when `claude auth status` answers loggedIn false; no CLI or an unreadable answer is not a logout (an Agent-tool host may have no CLI)."""
    c = auth_cache().fresh("CLAUDE_SUBSCRIPTION", now(), AUTH_FILE_TTL)
    if c:
        return c.ok is False
    exe = shutil.which("claude")
    if not exe:
        return False
    try:
        r = procs.run_capture([exe, "auth", "status"], 15)
        v = json.loads(r.stdout).get("loggedIn")
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return False
    if isinstance(v, bool):
        auth_cache().remember("CLAUDE_SUBSCRIPTION", v, "claude auth status", now())
    return v is False


def read_claude_subscription(lane, S, st, t, ai_usage=None):
    stale = S.usage_stale_minutes * 60
    usage = None
    src = None      # (where the numbers came from, when they were taken)
    if ai_usage:
        usage = normalize_usage(ai_usage)
        if usage:
            update_state(lambda s: setattr(s, "claude_usage", UsageCache(at=t, data=usage)))
            src = ("manual mark (get_usage passed in)", t)
    if usage is None:
        saved = st.claude_usage
        if saved and t - saved.at <= stale:
            usage = saved.data
            src = ("manual mark (get_usage passed in)", saved.at)
            lane.notes.append(f"usage from {fmt_dur(t - saved.at)} ago")
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
    weekly_spec = S.claude_weekly_reset
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


def read_claude_credit(lane, S, st, t, ai_usage=None):
    c = S.credit
    start, nxt = credit_cycle(c.reset_day, t)
    lane.cap = float(c.monthly_cap)
    lane.spend = ledger_spend(start)
    lane.reset_in_days = (nxt - t) / 86400.0
    lane.week_reset = nxt
    add_window(lane, 100.0 * lane.spend / lane.cap if lane.cap else None, nxt, 43200, "estimated from history (this tool's ledger)", ledger_last_ts())
    out = st.out.get(lane.name)
    if out and out.until > t:
        lane.out, lane.out_until, lane.out_reason = True, out.until, out.reason
        lane.notes.append(out.why)
    elif lane.spend >= lane.cap:
        lane.out, lane.out_until, lane.out_reason = True, nxt, "limit"
        lane.notes.append("own cycle cap reached")


READERS = {"codex_app_server": read_codex, "claude_subscription": read_claude_subscription, "claude_credit": read_claude_credit,
           "none": lambda lane, S, st, t, ai_usage=None: None}      # no usage reading (Grok): only refusals mark it out


# ----------------------------------------------------------------------------- owner holds (delegate-dns)

def cmd_hold(a, cfg):
    try:
        if a.lane not in cfg["lanes"]:
            raise ValueError(f"unknown lane {a.lane}; lanes: {', '.join(cfg['lanes'])}")
        rec = new_hold(uuid.uuid4().hex[:8], a.lane, a.until, a.reason, [x for x in (a.roles or "").split(",") if x], a.set_by, now())
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    update_state(lambda s: s.holds.__setitem__(rec.id, rec))
    print(json.dumps({"ok": True, "hold": codec.to_json(rec)}))
    return 0


def cmd_release(a, cfg):
    gone = []
    update_state(lambda s: gone.extend(HoldBook(s.holds).release(a.target)))
    print(json.dumps({"ok": bool(gone), "released": [codec.to_json(h) for h in gone]}))
    return 0 if gone else 1


def cmd_holds(a, cfg):
    print(json.dumps({"holds": HoldBook(load_state().holds).rows(now())}))
    return 0


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
    return [{k: getattr(r, k) for k in keys} for r in history_store().runs(n)]


# ----------------------------------------------------------------------------- the cockpit: every lane, one row each

EMOJI = {"ready": "✅", "resting": "⏳", "logged_out": "🔑", "spend": "💸", "slow": "🐢", "off": "⛔", "unknown": "❓"}
LEGEND = {"ready": "ready", "resting": "usage limit", "logged_out": "logged out", "spend": "spend/credit limit",
          "slow": "slow/hung", "off": "disabled", "unknown": "no read yet"}
LABELS = {"CLAUDE_SUBSCRIPTION": "Claude subscription", "CLAUDE_CREDIT": "Claude API credits", "OPENAI_SUBSCRIPTION": "ChatGPT / Codex",
          "GROK_SUBSCRIPTION": "SuperGrok / OpenCode", "DEEPSEEK_FREE": "DeepSeek / OpenCode Free", "ZAI_CODING_SUBSCRIPTION": "Z.ai GLM Coding Plan"}
LOGIN_CMD = {"OPENAI_SUBSCRIPTION": "codex login", "CLAUDE_SUBSCRIPTION": "claude auth login", "CLAUDE_CREDIT": "route.py set-key",
             "GROK_SUBSCRIPTION": "opencode auth login", "ZAI_CODING_SUBSCRIPTION": "opencode auth login, choose Z.AI Coding Plan",
             "DEEPSEEK_FREE": "route.py doctor --lane DEEPSEEK_FREE"}
SLOW_WINDOW = 2 * 3600  # a hang or timeout this recent, with no good run on the lane since, keeps it marked slow
SLOW_KINDS = ("hung", "timeout")




def probe_login(name):
    """-> (True | False | None, why): is the lane's login really there? A status command or a file, never a model call."""
    if name == "OPENAI_SUBSCRIPTION":
        return CodexHarness().login_probe(AUTH_TIMEOUT)
    if name == "CLAUDE_SUBSCRIPTION":
        return ClaudeHarness().login_probe(AUTH_TIMEOUT)
    if name == "CLAUDE_CREDIT":
        return (True, "API key stored") if find_key(quiet=True) else (False, "no API key stored")
    if name in OC_PROFILES:
        return OpenCodeHarness(OC_PROFILES[name]).login_probe(AUTH_TIMEOUT)
    return None, "no check for this lane"


def login_state(name, st, t):
    """-> (ok, why, checked_at): probe_login cached a few minutes in state, so a status call costs one process per lane at most."""
    c = auth_cache().fresh(name, t, AUTH_TTL, st)
    if c:
        return c.ok, c.why, c.at
    ok, why = probe_login(name)
    auth_cache().remember(name, ok, why, t)
    return ok, why, t


def lane_model(cfg, name):
    """'model effort' the lane's family gets in the worker role (else the first role that uses the family, else its first model)."""
    fam = cfg["lanes"][name]["family"]
    for role in [cfg["roles"].get("worker")] + list(cfg["roles"].values()):
        for step in (role or {}).get("steps", []):
            if step.get("on") == fam:
                return f"{step['model']} {step['effort']}"
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
    text = " ".join([rec.why if rec else ""] + lane.notes)
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
        (gwk, gfv, gname), (cwk, cfv, _) = lane.limits(S, False), lane.limits(S, True)
        gate, ceil = (gwk, cwk) if w["span"] > 1440 else (gfv, cfv)
        if used >= gate:
            low.append({"window": w["name"], "used_pct": round(used, 1), "limit": ceil if used >= ceil else gate,
                        "over": "ceiling" if used >= ceil else "gate", "limit_name": "hard ceiling" if used >= ceil else gname})
    return rows, low


def lane_view(name, cfg, lane, st, t, history=(), running=0, login=(None, "", None), off_why=None):
    """One lane's cockpit row. Pure given its inputs: lane is what gather built (None = disabled or not configured), history the recent finished runs."""
    S, c = settings_of(cfg), cfg["lanes"][name]
    v = {"id": name, "label": c.get("label") or LABELS.get(name, name), "family": c["family"], "kind": c["kind"], "model": lane_model(cfg, name),
         "state": "off", "emoji": EMOJI["off"], "reason": off_why or "disabled or not configured", "detail": None,
         "unlock": {"locked": False, "known": False, "at": None, "in_s": None, "text": None, "why": "not in use"},
         "windows": [], "low_tokens": [], "warnings": list(lane.warn) if lane else [], "meter": c["reader"] != "none",
         "login": {"ok": login[0], "why": login[1], "checked_at": login[2]}, "running": running}
    if lane is None:
        return v
    v["windows"], v["low_tokens"] = window_rows(lane, S, t)
    rec = st.out.get(name)
    relogin = f"waits for you to log in (`{LOGIN_CMD[name]}`); re-checked every {AUTH_TTL // 60} min"
    until, why, locked = None, "not locked", False
    if lane.out:
        kind = classify_out(lane, rec)
        text = (rec.why if rec else "") or (lane.notes[0] if lane.notes else "")
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
    elif not lane.usable(S, True)[0]:               # over the hard ceiling with nothing recorded: resting until that window resets
        v["state"], locked = "resting", True
        until, note, why = lane.back_at(S), lane.usable(S, True)[1], "no reset time for the window it is over"
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
    S = settings_of(cfg)
    fams = {s["on"] for s in cfg["roles"]["worker"]["steps"] if s["on"] != "claude"}
    parts = []
    for name, c in cfg["lanes"].items():
        if c["family"] not in fams:
            continue
        l = lanes.get(name)
        parts.append(f"{name} " + ("not set up" if l is None else l.usable(S, True)[1]))
    return "; ".join(parts) or "no other lane exists"


def busy_jitter():
    return random.randint(60, 120)


def settings_of(cfg):
    return typed_routing(cfg).settings


def next_worker(cfg, lanes, st, t):
    """The lane `next worker` would pick now: decide() on a copy, nothing written."""
    routing = typed_routing(cfg)
    cur = mark_busy(lanes, st, routing.settings, t, busy_jitter)
    unattended = not interactive()
    d = Router(routing, HoldBook(st.holds), st.prev, t).route("worker", CallArgs(unattended=unattended), cur).as_dict()
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
    book = LeaseBook(st.leases)
    rows = []
    for name, c in cfg["lanes"].items():
        lane = lanes.get(name)
        login = login_state(name, st, t)
        off = off_reason(name, cfg, login) if lane is None else None
        rows.append(lane_view(name, cfg, lane, st, t, hist, book.counts(name, t)["live"], login, off))
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
            "needs_input": needs_input(lanes), "holds": HoldBook(st.holds).rows(t), "asks": [x.as_dict() for x in settle_holds(states(), lanes, st, t)[0]],
            "health": health_rows(cfg, lanes, st, t), **roles_payload(cfg, lanes, st, t)}


def net_cache_path():
    return config_dir() / "net.json"


def roles_payload(cfg, lanes, st, t):
    """roles{} (usable and net per role) and network{} for status --json; the probe is read from its cache, never run here."""
    cached = netprobe.load(str(net_cache_path()))
    routing = typed_routing(cfg)
    router = Router(routing, HoldBook(st.holds), st.prev, t)
    return {"roles": role_view.roles_view(cfg, lanes, router, st, t, cached), "network": network_view(cached, t)}


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
    rows, pol = [], settings_of(cfg).breaker
    for name, l in lanes.items():
        cnt = LeaseBook(st.leases).counts(name, t)
        rec = st.out.get(name)
        ats = [w.get("at") for w in l.windows if w.get("at")]
        stale = health_ledger.stale_usage(max(ats) if ats else l.read_at, t, pol)
        for model, v in l.hview.items():
            if v.state == "up" and l.out and not l.busy:         # the lane's own out record (state.json) is the other half of "exhausted"
                v = replace(v, state="logged_out" if l.out_reason == "logged_out" else "exhausted", until=l.out_until, source=rec.source if rec else None,
                            kind=l.out_reason, evidence=(rec.why if rec else "")[:240])
            state = health_ledger.combine(v, False, l.busy)
            v = v.as_dict()
            rows.append({"lane": name, "model": model, "state": state, "until": v["until"], "source": v["source"], "kind": v["kind"], "evidence": v["evidence"],
                         "canary": v["canary"], "rung": v["rung"], "leases": cnt, "last_failures": v["last_failures"],
                         "needs_login": state == "logged_out" and l.family == "claude",
                         "usage": {"known": l.usage_known and not stale, "text": f"unknown (stale {stale:g} h)" if stale else None}})
    return rows


def running_rows(st, t):
    return [{"run_id": k, "lane": v.lane, "role": v.role, "task": v.task, "started": v.start, "age_s": int(t - v.start),
             "pid": v.pid, "worker_pid": v.worker_pid, "suspect": suspect(v, t)}
            for k, v in sorted(LeaseBook(st.leases).live(t).items(), key=lambda kv: kv[1].start)]


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
    out.update(v=1, cheap=True, ts=t, age_s=int(t - at) if isinstance(at, (int, float)) else None, running=running_rows(st, t), holds=HoldBook(st.holds).rows(t),
               auth=auth_cache().rows(st, t))
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
    lanes = fleet_builder(cfg).build(st, t, parse_json_arg(a.claude_usage)).lanes
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


def orphan(v):
    """True when the lease's route.py process is gone (pid plus start time: an expired lease alone may be a laptop waking up) but its worker
    is still running: a SIGKILL on route.py leaves the worker, which is its own process group, editing the tree."""
    gone = procs.start_token(v.pid) != v.pid_start
    return gone and bool(v.worker_start) and procs.start_token(v.worker_pid) == v.worker_start


def reap(v):
    """Stop an orphaned worker's whole tree: SIGTERM, a short grace, SIGKILL (stop_tree). -> True when the worker is gone."""
    pg = int(v.worker_pid)
    procs.stop_tree(pg, grace=3, done=lambda: procs.start_token(pg) != v.worker_start)
    return procs.start_token(pg) != v.worker_start


def fleet_builder(cfg):
    import onboard
    R = sys.modules[__name__]
    return FleetBuilder(typed_routing(cfg), states(), history_store(), CheckpointStore(config_dir() / "health-checkpoint.json"), config_store(),
                        READERS, lambda: onboard.configured_lanes(R, cfg))


def call_args(a, unattended):
    return CallArgs(authored_by=a.authored_by, failed=tuple(int(x) for x in a.failed.split(",") if x != "") if a.failed else (), premium=a.premium,
                    allow_degraded=a.allow_degraded, unattended=unattended, resume=getattr(a, "resume", None), cap=getattr(a, "cap", None))


def decision(a, cfg):
    """next and run share this: build the fleet, route, remember the lane choice. -> (d, lanes, warnings, t); d is the JSON answer of `next`."""
    t = now()
    st = load_state()
    routing = typed_routing(cfg)
    unattended = not interactive()
    fleet = fleet_builder(cfg).build(st, t, parse_json_arg(a.claude_usage))
    warnings = drift_warnings(states(), fleet.lanes, st, t)
    _, st = settle_holds(states(), fleet.lanes, st, t)
    publish_lanes(fleet.lanes, st, t)
    lanes = callable_lanes(mark_busy(fleet.lanes, st, routing.settings, t, busy_jitter), unattended)
    args = call_args(a, unattended)
    router = Router(routing, HoldBook(st.holds), st.prev, t)
    pick = router.route(a.role, args, lanes, st.refs.get(args.resume or ""))
    d = pick.as_dict()
    if isinstance(pick, Run):
        step = {"model": pick.model, "effort": pick.effort, "mode": pick.mode}
        if pick.cap is not None or pick.pass_ == "resume":
            step["cap"] = pick.cap
        d["how"] = how(lanes[pick.lane], step, cfg, a.cwd, unattended)
        if pick.pass_ != "resume":
            fam = lanes[pick.lane].family
            if args.cap:
                d["cap"] = d["how"]["cap"] = args.cap if d["how"]["kind"] == "claude_p" else d["how"].get("cap")

            def mark(s):
                new = router.dwell.note(fam, pick.lane, t)
                if new:
                    s.prev[fam] = new
            if sum(1 for lane in lanes.values() if lane.family == fam) > 1:
                update_state(mark)
    if fleet.offers:
        config_store().mark_offered(fleet.offers)
        d["notice"] = "not set up: " + ", ".join(fleet.offers) + " (doctor --lane NAME to set up, doctor --decline NAME to hide)"
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
        update_state(lambda s: s.out.pop(a.lane, None))
        history_add(hist.ReleaseRow(ts=now(), lane=a.lane))      # the fold forgets the lane's limits
        print(f"{a.lane}: marked available")
        return 0
    until = to_epoch(a.until)
    if until is None:
        st = load_state()
        lane = fleet_builder(cfg).build(st, t, check_config=False).lanes[a.lane]     # a lane the profile leaves out can still be reported out
        until = lane.out_until or (lane.week_reset if lane.week_reset and lane.week_reset > t and lane.kind == "credit" else None) or t + 3600
    mark_out(a.lane, until, "reported out", t, "limit", "manual" if a.until else "guess")
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
    cmd = " ".join(shlex.quote(x) for x in argv)
    return {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}]}}


def find_key(quiet=False):
    k = os.environ.get("CLAUDE_CREDIT_WORKER_KEY", "").strip()
    if k:
        return k
    p = config_dir() / "claude-credit.key"
    with contextlib.suppress(OSError):
        k = p.read_text(encoding="utf-8").strip()
        if k:
            if not quiet and (p.stat().st_mode & 0o077):
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
        outs = st.out
        agent_runs.lanes([(n, l.out_until if l.out else None, outs[n].why if l.out and n in outs else None,
                           [("5h", l.five, l.five_reset), ("week", l.week, l.week_reset)]) for n, l in lanes.items()], t)
    except Exception:
        pass


# ----------------------------------------------------------------------------- run: one step, end to end, one JSON answer



CREDENTIAL_RE = re.compile(r"(TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)|^(AWS_|GH_|GITHUB_|SSH_AUTH_SOCK$)", re.I)


def child_env(extra=None):
    """The worker gets the caller's environment minus what a Claude session exports (onboard.APP_ENV_RE), delegate's and credential-looking variables (tokens, keys, cloud and git credentials, the ssh agent), plus DELEGATE_DEPTH=1."""
    from onboard import app_free_env
    env = {k: v for k, v in app_free_env().items() if not k.startswith("DELEGATE_") and not CREDENTIAL_RE.search(k)}
    env["DELEGATE_DEPTH"] = "1"
    env.update(extra or {})
    return env


def find_transcript(ref, family):
    hx = {"openai": CodexHarness(), "claude": ClaudeHarness()}.get(family) or OpenCodeHarness(root=config_dir())        # opencode: exported after the run
    return hx.transcript(ref)


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


def codex_reset_now(t):
    """The reset of the Codex window a fresh reading shows full (>= 95%), else None."""
    res, _ = fetch_codex_limits()
    rl = (res or {}).get("rateLimits") or {}
    full = [w.get("resetsAt") for w in (rl.get("primary") or {}, rl.get("secondary") or {}) if (w.get("usedPercent") or 0) >= 95 and (w.get("resetsAt") or 0) > t]
    return min(full) if full else None


def mark_out(lane, until, why, t, reason="limit", source="guess", zone=None, guess_n=0):
    """Record a lane out. source: vendor (the vendor stated the end) | guess | manual (the owner said); zone: the zone the vendor's time was in;
    `set` = wall clock now (workers already running read it: a lane marked out after they started stops them)."""
    update_state(lambda s: s.out.__setitem__(lane, OutState(until=until, why=why, at=t, set=time.time(), reason=reason, source=source, zone=zone,
                                                           guess_n=guess_n)))


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
    if rc is not None:
        res["exit_code"] = rc
    res["detail"] = why[:240]           # run_once moves it into history.jsonl (`say`); it never reaches the wire
    if r == "secrets":
        res.update(say=f"stopped: the worker ran a command on a secret path: {why[:200]}" + (f"; {APPLY_PATCH_HINT}" if heredoc_only_hit(why) else ""))
    elif r == "timeout":
        res.update(say=f"{who} did not finish within {a.timeout}s and was killed")
    elif r == "hung":
        words, kind = res.pop("hung_words", "printed nothing"), res.pop("hung_kind", "first_event")
        if kind == "first_event":       # nothing came at all: the lane is the suspect, it rests; a worker that went quiet mid-run is the pair's (the breaker counts it)
            until = t + HUNG_OUT_SECS
            mark_out(lane, until, f"hung: {words}", t, "hung")
            res.update(retry_after=until, say=f"{who} {words} and was stopped; {lane} out until {local(until)}")
        else:
            until = now() + random.randint(60, 120)
            res.update(retry_after=until, say=f"{who} {words} and was stopped (its whole process tree); retry")
    elif r == "rate_limited":
        prev = load_state().out.get(lane)
        n = prev.guess_n + 1 if prev and prev.source == "guess" and t - prev.at < 6 * 3600 else 0
        until, source, zone = reset_parse.limit_until(why, t, d, reset, n)
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
        update_state(lambda s: s.out.pop(lane, None))
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


HUNG_OUT_SECS = 1800       # a lane whose worker printed nothing at all rests this long
IDLE_S = None              # run --idle: seconds of silence before the worker is hung (None = the CLI's limit in app/watch.py)
Prep = collections.namedtuple("Prep", "hx spec extra")       # the harness, its launch, and what the ladder needs from before the launch
OC_PROFILES = {"GROK_SUBSCRIPTION": Profile(auth_key="xai"),
               "ZAI_CODING_SUBSCRIPTION": Profile(providers=("zai-coding-plan",), price="unknown", auth_key="zai-coding-plan", need_key=True),
               "DEEPSEEK_FREE": Profile(providers=("opencode",), own_data=True, price="free")}
ZAI_MODELS = ("zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash")


def opencode_exe():
    return OpenCodeHarness.find_exe()


def lane_gone_check(lane, since):
    """-> a function: the words of a limit, credit or login mark another process set on `lane` after `since` (wall clock), else None. The worker
    still running there is burning an exhausted lane."""
    def gone():
        o = load_state().out.get(lane)
        if o and o.set >= since and o.until > now() and o.reason in ("limit", "credit", "logged_out"):
            return o.why or "lane marked out"
        return None
    return gone


def secret_in(e):
    """The command a Cmd or CmdEnd event names a secret path in, else None (delegate polices the stream of the CLIs that have no hook)."""
    return e.text if secret_hit(e.text) else None


def denied_items(hx, read_only):
    """The `denied` items of a run, from the denials its harness reported. A git write the coordinator forbids, or a write on a read-only step, is
    delegate's policy, not the owner's call, and Claude's own record of it is dropped."""
    out = []
    for x in hx.evidence.denials:
        cmd = (x.input or {}).get("command")
        if hx.kind == "claude" and policy.is_policy_denial(x.tool, x.input, read_only):
            continue
        kind = x.kind if x.kind != "policy" else "secret" if hx.kind == "opencode" and x.tool == "Bash" and secret_hit(cmd) else "policy"
        out.append(policy.denial_item(policy.denial(kind, x.tool, x.input)))
    return out


def prepare_codex(d, brief, a, rid, t, cfg):
    h = d["how"]
    hold = HoldBook(load_state().holds).covering(d.get("lane") or "OPENAI_SUBSCRIPTION", d.get("role", getattr(a, "role", None)), t)
    if hold:
        return HoldBook.refusal(hold, "no worker was launched")
    exe = shutil.which("codex")
    if not exe:
        return {"outcome": "setup", "say": "codex CLI not found on PATH (route.py doctor --lane OPENAI_SUBSCRIPTION)"}
    hx = CodexHarness(a.resume)
    spec = hx.launch_spec(LaunchInputs(
        exe=exe, cwd=h["cwd"], model=h["model"], effort=d["effort"], sandbox=h["sandbox"], out_file=str(config_dir() / f"codex-last-{rid[:8]}.txt"), env=child_env(),
        cli=h["cli"], access=CODEX_ACCESS, isolation=h["isolation"], schema=a.schema, resume=a.resume,
        prompt=" ".join((SECRET_RULE, GIT_RULES, EDIT_RULE, NO_SUBAGENTS_RULE, ESCALATE_RULE, DENIED_RULE)) + "\n\n" + brief))
    before = (codex_thread_total(a.resume) or history_store().last_cumulative(a.resume)) if a.resume else None
    return Prep(hx, spec, {"before": before, "started": time.time()})       # wall clock, not now(): it is compared with file times


def prepare_claude(d, brief, a, rid, t, cfg):
    """claude -p on the credit key (CLAUDE_CREDIT) or the logged-in subscription (CLAUDE_SUBSCRIPTION, unattended)."""
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
    hx = ClaudeHarness(credit)
    spec = hx.launch_spec(LaunchInputs(
        exe=exe, cwd=h["cwd"], model=h["model"], effort=d["effort"], prompt=brief, env=child_env(extra), schema=a.schema, resume=a.resume, session=rid,
        system=PREAMBLE + repo_instruction_text(h["cwd"]), settings=json.dumps(guard_settings(h.get("read_only"))), write=bool(h["write"]),
        budget=cap if credit else None))
    return Prep(hx, spec, {"credit": credit, "credit_until": nxt})


def prepare_opencode(d, brief, a, rid, t, cfg):
    """OpenCode headless runner for subscription and isolated free-only lanes."""
    h, lane = d["how"], d["lane"]
    exe = opencode_exe()
    if not exe or not OpenCodeHarness.supported():
        return {"outcome": "setup", "say": f"{lane} needs macOS and the opencode CLI (route.py doctor --lane {lane})"}
    if lane == "ZAI_CODING_SUBSCRIPTION" and h["model"] not in ZAI_MODELS:       # pin subscription routing; never substitute the pay-as-you-go `zai` provider
        return {"outcome": "setup", "say": "Z.ai Coding Plan model must use the subscription provider."}
    rules = " ".join((SECRET_RULE, GIT_RULES, EDIT_RULE, NO_SUBAGENTS_RULE, opencode_sandbox.RULE, DENIED_RULE))
    if not any((x / "AGENTS.md").is_file() for x in repo_dirs(h["cwd"])):
        rules += "\n\n" + repo_instruction_text(h["cwd"])         # opencode reads AGENTS.md itself; CLAUDE.md only through what is turned off
    if a.schema:
        rules += "\n\nYour final message must be only a JSON object matching this JSON Schema:\n" + Path(a.schema).read_text(encoding="utf-8")
    hx = OpenCodeHarness(OC_PROFILES.get(lane, Profile()), config_dir())
    spec = hx.launch_spec(LaunchInputs(exe=exe, cwd=h["cwd"], model=h["model"], effort=d["effort"], prompt=rules + "\n\n" + brief, env=child_env(),
                                       resume=a.resume, session=rid, write=bool(h["write"])))
    if lane == "DEEPSEEK_FREE" and not hx.free_model_ok(h["model"], spec.env):        # fail closed if the exact free model or its zero price is gone
        hx.cleanup()
        until = t + 900
        mark_out(lane, until, "exact free model unavailable or zero catalog price unverified", t, "limit", "guess")
        return {"outcome": "rate_limited", "retry_after": until,
                "say": "Free DeepSeek is unavailable or its zero price could not be verified; rerun to use the next configured lane."}
    return Prep(hx, spec, {})


PREPARE = {"codex": prepare_codex, "claude_p": prepare_claude, "opencode": prepare_opencode}
REF_FIELD = {"codex": "thread", "claude_p": "session", "opencode": "session"}        # the agent-runs field the vendor's session id goes in
AGENT_KIND = {"codex": "codex", "claude_p": "claude-p", "opencode": "opencode"}


def login_text(kind, lane):
    """(the hint stored with a lane that is logged out, the text of the answer with {why})."""
    if kind == "codex":
        return "logged out: run `codex login`", f"codex is not logged in: {{why}} (route.py doctor --lane {lane})"
    if kind == "claude_p":
        return "logged out: run `claude auth login`", "claude is not logged in: {why} (claude auth login)"
    hint = ("free OpenCode access refused; no paid credentials will be used" if lane == "DEEPSEEK_FREE" else "run `opencode auth login` and choose Z.AI Coding Plan; enter your Coding Plan key in your own terminal"
            if lane == "ZAI_CODING_SUBSCRIPTION" else "run `opencode auth login` (xAI, SuperGrok Subscription)")
    return hint, f"{lane}: {hint}: {{why}}"


def codex_usage(res, x):
    """Codex's usage is a thread's running total: keep this run's part, add the reviewer's tokens (its own child thread), and read the context size."""
    tokens = accounting.per_call(res["tokens"], x["before"])
    if res["ref"]:
        extra, n = codex_child_tokens(res["ref"], x["started"] - 1)
        if n:
            base = tokens or {"in": 0, "cached": 0, "out": 0}
            tokens = {k: (base.get(k) or 0) + extra[k] for k in extra}
    res.update(tokens=tokens, context_k=codex_context_k(res["ref"]), acct=accounting.codex(tokens))


def launch_worker(d, brief, a, rid, t, cfg, meta, hooks):
    """One worker, end to end: its harness builds the launch, the supervisor runs it, the one ladder settles it. -> dict of result fields."""
    h = d["how"]
    prep = PREPARE[h["kind"]](d, brief, a, rid, t, cfg)
    if isinstance(prep, dict):
        return prep
    hx, spec, x = prep
    field = REF_FIELD[h["kind"]]

    def on_ref(ref):
        if hooks.ref:
            hooks.ref(ref)
        if agent_runs:
            agent_runs.run(rid, **{field: ref})
    if hx.evidence.ref:
        on_ref(hx.evidence.ref)        # known before the first event: a resume, or claude's own --session-id
    if agent_runs:
        agent_runs.run(rid, kind=AGENT_KIND[h["kind"]], lane=d["lane"], model=d["model"], cwd=h["cwd"], started=t, status="running",
                       **({field: hx.evidence.ref} if hx.evidence.ref else {}), **meta)
    live = LiveSink(hx.kind, run_id=rid)
    live.start(lane=d.get("lane"), model=d.get("model"), effort=d.get("effort"), role=d.get("role") or getattr(a, "role", None))
    log = LogWatcher(hx.log_dir, classify.LIMIT_RE.search, hx.echoes_input) if hx.log_dir else None
    sup = Supervisor(hx, watch.CLI_OF_KIND[h["kind"]], dataclasses.replace(hooks, ref=on_ref), live, gone=lane_gone_check(d["lane"], time.time()), log=log,
                     policy=secret_in if hx.secret_event else None, net_up=lambda: net_up(d["lane"]))
    try:
        out = sup.run(spec, a.timeout)
    finally:
        hx.cleanup()
    hx.export(spec.env)
    with contextlib.suppress(Exception):        # one history row per inter-event gap over watch.LONG_GAP_S (role "gap"): the health fold ignores it
        sup.dog.flush(history_add, d["lane"], d["model"], out.watch.why is None)
    sm, w = out.summary, out.watch
    res = dict(sm.res, stderr=out.err, denied=denied_items(hx, h.get("read_only")))
    if h["kind"] == "codex":
        codex_usage(res, x)
    if x.get("credit") and sm.cost > 0:
        ledger_add(sm.cost, d["model"], t)
    if out.stream_tail:
        res["stream_tail"] = out.stream_tail
    if w.why == "hung":
        res["hung_words"], res["hung_kind"] = w.words, w.kind
    facts = dict(sm.facts, watchdog=w.why, msg=w.msg, secret=out.secret)
    o = classify.classify(sm.rc, sm.vendor, sm.stderr, out.timed_out, sm.work, facts) if sm.check else None
    if out.stop == "answer" and o and o.reason == "killed" and sm.stands and not out.timed_out:       # we stopped it after its answer: the answer stands
        o = None
        res["warnings"] = ["answer_done_process_alive"]
    if out.net and (o is None or o.reason == "killed"):       # we stopped it because the network is down
        o = classify.Outcome("network", hx.evidence.transient, "event")
    if o:       # the CLI's own error or stderr, never the model's answer
        settle(res, o, d, t, hx.kind, a, sm.rc, sm.tail, reset=(lambda: codex_reset_now(t)) if hx.kind == "codex" else None,
               credit_until=x.get("credit_until"), login=login_text(h["kind"], d["lane"]))
    else:
        update_state(lambda s: s.out.pop(d["lane"], None))
        res.update(data=sm.data if sm.data is not None else parse_data(sm.text, a.schema))
        if sm.warnings:
            res["warnings"] = sm.warnings
    return res


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
    """One object on stdout. A model reads it when a Claude session is behind us, so empty fields are dropped there; scripts get every field.
    The answer passes through RunResult first: a key or a type the contract does not know is a bug here, never an answer."""
    r = codec.to_json(codec.from_json(wire.RunResult, r))
    if interactive():
        r = {k: v for k, v in r.items() if k in ("contract", "stop") or v not in (None, "", [], 0) and k not in ("context", "usage", "transcript_info", "transcript_status")}     # these are for scripts
    print(json.dumps(r))
    return 0        # exit 0 whenever the object was printed; non-zero only if route.py itself crashed


def lane_use(cfg, name, t):
    """(five_hour %, weekly %) of a lane now; None where unknown."""
    c = cfg["lanes"][name]
    lane = Lane(name, c["family"], c["kind"], c.get("shared_with_orchestrator", False))
    with contextlib.suppress(Exception):
        READERS[c["reader"]](lane, settings_of(cfg), load_state(), t)
    return lane.five, lane.week


def cmd_contract(a, cfg):
    """The handshake: the contract number, every reason with its facts, and the flags `run` accepts."""
    print(json.dumps({"contract": classify.CONTRACT["contract"], "reasons": classify.CONTRACT["reasons"], "run_args": classify.CONTRACT["run_args"]}))
    return 0


def refuse(a, outcome, say, retry_after, extra, **who):
    """An answer given without launching a worker; history keeps it too, so refusals are countable."""
    call_id = getattr(a, "call_id", None)
    history_add(hist.RefusalRow(ts=now(), call_id=call_id, **health_ledger.failure_fields(outcome, "", say, retry_after), role=a.role, task=getattr(a, "task", None) or "",
                                lane=who.get("lane"), model=who.get("model"), resume=bool(getattr(a, "resume", None))))
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
        history_add(hist.CanaryRow(ts=now(), run_id=str(uuid.uuid4()), lane=d["lane"], model=d["model"]))
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
        holds, S = HoldBook(load_state().holds), settings_of(cfg)
        retry = min([x for x in [l.back_at(S, holds.covering(l.name, a.role, t)) for l in lanes.values()] + [b.until for l in lanes.values() for b in l.benched.values()] if x] or [None])
        if not unattended and d.get("ask_user"):
            extra.update(ask_user=d["ask_user"], ask_kind="decision")
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
    if h["kind"] not in profiles.REGISTRY and h["kind"] != "agent_tool":
        return refuse(a, "setup", f"no launcher for runner kind {h['kind']!r}: route.py has codex, opencode and claude_p", None, extra, **who)
    if h["kind"] == "agent_tool":
        return emit(result(None, **who, call_id=getattr(a, "call_id", None), transcript_status="unsupported", agent_model=h["model"], brief_prefix=SECRET_RULE + " " + GIT_RULES, cwd=h["cwd"], **extra))
    brief = Path(a.brief).read_text(encoding="utf-8", errors="replace") if a.brief else sys.stdin.read()
    if not brief.strip():
        return emit(result("caller", **who, call_id=getattr(a, "call_id", None), say="empty brief: pass it on stdin or with --brief FILE", **extra))
    cwd = h["cwd"]
    rid = str(uuid.uuid4())
    S, lane = settings_of(cfg), lanes[d["lane"]]
    # A limit refusal mid-run lasts until the time its message names, else the soonest window reset (else an hour).
    resets = [(lane.week or 0, lane.week_reset), (lane.five or 0, lane.five_reset)]
    d["limit_reset"] = min((r for _, r in resets if r and r > t), default=None)     # a guess: the soonest reset (too early costs one refused call, too late days)
    cap, taken = lane.slots(S), {}
    canary_due = bool(lane.canary.get(d["model"])) and h["kind"] in profiles.REGISTRY
    silence_s = IDLE_S or watch.LIMITS[watch.CLI_OF_KIND[h["kind"]]]["silence_s"]

    def claim(st):      # the authoritative count: inside the lock, never from the snapshot decision() read
        live = LeaseBook(st.leases).live(now())
        for k, v in list(st.leases.items()):
            if orphan(v):
                gone = reap(v)
                history_add(hist.ReapRow(ts=now(), run_id=k, role=v.role, task=v.task, lane=v.lane, reaped=gone, worker_pid=v.worker_pid))
                if gone:
                    live.pop(k, None)
                else:           # still running: its lease stays, so the same task or a full lane waits
                    live[k] = replace(v, expires=now() + LEASE_TTL)
            elif k not in live and v.pid_start and procs.start_token(v.pid) == v.pid_start:
                live[k] = v     # expired only because route.py could not renew it (the laptop slept): it is still running
        st.leases = live
        if a.task and any(v.task == a.task for v in live.values()):
            taken["no"] = ("duplicate_task", f"task {a.task} already has a running worker")
        elif canary_due and any(v.canary == d["model"] and v.lane == d["lane"] for v in live.values()):
            taken["no"] = ("busy", f"{d['lane']}/{d['model']} has its canary running; wait for it")
        elif cap is not None and sum(1 for v in live.values() if v.lane == d["lane"] and not suspect(v, now())) >= cap:
            taken["no"] = ("busy", f"{d['lane']} has all {cap} slots taken")
        else:
            taken["peers"] = sorted(live)
            live[rid] = Lease(lane=d["lane"], role=a.role, pid=os.getpid(), pid_start=me_start, start=now(), expires=now() + LEASE_TTL, task=a.task or "",
                              silence_s=silence_s, canary=d["model"] if canary_due else None)
    me_start = procs.start_token(os.getpid())
    launched = {}
    update_state(claim)
    if "no" in taken:
        return refuse(a, taken["no"][0], taken["no"][1], now() + random.randint(60, 120), extra, **who)
    beat = threading.Event()

    def keep_lease():
        def renew(st):
            if rid in st.leases:
                st.leases[rid].expires = now() + LEASE_TTL
        while not beat.wait(LEASE_RENEW):
            with contextlib.suppress(Exception):
                update_state(renew)
    threading.Thread(target=keep_lease, daemon=True).start()

    hooks = RunHooks(idle_s=IDLE_S, unattended=UNATTENDED)

    def worker_started(w):
        """The worker runs in its own process group (pgid = pid), so a SIGKILL on route.py leaves it running: say where it is, before any
        work, on stderr and in the lease (status --json `running`), so the caller can kill the group itself."""
        print(f"delegate: worker pid={w.pid} pgid={w.pgid} run_id={rid}", file=sys.stderr, flush=True)
        launched.update(pid=w.pid, pgid=w.pgid, start=w.start)

        def note(st):
            if rid in st.leases:
                st.leases[rid].worker_pid, st.leases[rid].worker_start = w.pid, w.start
        update_state(note)
    hooks.started = worker_started

    def stamp_beat(wall):
        """The worker printed something: stamp the lease (at most every 30 s)."""
        def stamp(st):
            if rid in st.leases:
                st.leases[rid].last_event = wall
        update_state(stamp)
    hooks.beat = stamp_beat
    ref_rec = RefRecord(lane=d["lane"], model=d["model"], effort=d["effort"], mode=d["mode"], cap=d.get("cap"),
                        rung=d.get("rung"), fallback=d.get("fallback"), fallback_reason=d.get("fallback_reason"), first_lane=d.get("first_lane"))

    def keep_ref(ref):
        """Remember the ref for `--resume` whatever the outcome."""
        def keep(s):
            s.refs[ref] = ref_rec
            for k in list(s.refs)[:-200]:
                s.refs.pop(k)
        update_state(keep)

    def ref_seen(ref):
        """The vendor revealed its session id: store it, stamp the lease, and print it on stderr (the engine reads `delegate: ref=`) before any work is lost."""
        keep_ref(ref)
        print(f"delegate: ref={ref} run_id={rid}", file=sys.stderr, flush=True)

        def stamp(st):
            if rid in st.leases:
                st.leases[rid].ref = ref
        update_state(stamp)
    hooks.ref = ref_seen
    meta = {"parent_session": a.parent or "", "title": a.title or brief.strip().splitlines()[0][:120]}
    if a.task:
        meta["task"] = a.task
    git_before = git_state(cwd)
    t0, ends = time.time(), {}
    launch = lambda b, a_: launch_worker(d, b, a_, rid, t, cfg, meta, hooks)
    t_from = file_size(find_transcript(a.resume, fam)) if a.resume else 0
    try:
        res = canary(launch, d, rid, a) if canary_due else None      # the one probe before full traffic; None = it passed
        res = res or launch(brief, a)
        if res.get("outcome") == "network" and not res.pop("started", False) and net_up(d["lane"]):
            res = launch(brief, a)      # dropped before the vendor had a session and the net is back: nothing to resume, nothing lost, so run it again, once
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
        res.pop("started", None)        # the launcher's own bookkeeping, whichever launch (first, re-ask) produced `res`
        res.pop("answered", None)
    except Exception as e:      # a launcher bug is an error object, not a stack trace
        res = {"outcome": "crash", "say": f"{type(e).__name__}: {e}"}
    except SystemExit as e:     # SIGTERM / SIGHUP to this process: run_proc stopped the worker's tree first; the caller still gets a typed answer
        if not isinstance(e.code, int) or e.code <= 128:
            raise
        res = {"outcome": "killed", "say": f"route.py got signal {e.code - 128}; its worker was stopped first"}
    finally:
        beat.set()

        def release(st):
            ends["peers"] = [k for k in LeaseBook(st.leases).live(now()) if k != rid]
            st.leases.pop(rid, None)
        with contextlib.suppress(Exception):
            update_state(release)
    err = res.pop("stderr", "")
    if err:
        print(err[-2000:], file=sys.stderr)
    done = res.get("outcome") is None
    if done and d["lane"] in ("CLAUDE_SUBSCRIPTION", "OPENAI_SUBSCRIPTION"):
        auth_cache().remember(d["lane"], True, "a worker run succeeded", now())
    if agent_runs:
        agent_runs.run(rid, ended=now(), status="completed" if done else "failed", transcript=find_transcript(res.get("ref"), fam))
    if res.get("ref"):          # every outcome: a stop, a failure and a timeout can all be resumed
        with contextlib.suppress(Exception):
            keep_ref(res["ref"])
    secs = round(time.time() - t0, 1)
    five_after, week_after = lane_use(cfg, d["lane"], now())
    transcript = find_transcript(res.get("ref"), fam)
    detail = res.pop("detail", "")
    if detail:
        res["first_error"] = detail
    history_add(hist.RunRow(
        ts=now(), run_id=rid, call_id=getattr(a, "call_id", None), ref=res.get("ref"), **health_ledger.failure_fields(res.get("outcome"), "", detail or res.get("say"), res.get("retry_after")),
        role=a.role, task=a.task or "", lane=d["lane"], model=d["model"], effort=d["effort"], done=done, warnings=res.get("warnings"), stream_tail=res.pop("stream_tail", None),
        denials=sum(1 for x in res.get("denied") or [] if x.get("kind") != "sandbox_limit"), secs=secs, cost=res.get("cost", 0), tokens=res.get("tokens"),
        cumulative=res.get("cumulative"), five_before=lane.five, week_before=lane.week, five_after=five_after, week_after=week_after,
        overlap=sorted(set(taken["peers"]) | set(ends.get("peers", []))), slots=cap, transcript=transcript))
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
    wk = procs.worker_end(hooks.stops, res.get("outcome"))
    if launched:
        wk.pid, wk.pgid, wk.start = launched["pid"], launched["pgid"], launched["start"]
    if wk.survivors and res.get("outcome") is None:      # a done answer must not hide a worker still running
        res.update(outcome="worker_survived", say="worker pids outlived SIGTERM and SIGKILL: " + ", ".join(str(x.pid) for x in wk.survivors))
    return emit(result(res.pop("outcome", None), worker=codec.to_json(wk), **res))


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
    if a.ack:
        set_ack(a.ack)
        print(json.dumps({"acknowledged": sorted(acked())}))
        return 0
    if a.decline or a.fix:
        out = onboard.act(R, cfg, do=a.fix, decline=a.decline, open_browser=a.open_browser)
        print(json.dumps(out, indent=1))
        return 1 if out.get("error") else 0
    d = onboard.doctor(R, cfg, deep=a.deep, lane=a.lane)
    for c in d.get("checks") or []:
        if isinstance(c, dict) and c.get("id") == "claude_login" and c.get("ok") is True:       # only a positive answer: doctor cannot tell "logged out" from "no CLI" or "unreadable"
            auth_cache().remember("CLAUDE_SUBSCRIPTION", c.get("ok"), "doctor", now())
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
    r.add_argument("--until")
    cl = sub.add_parser("capture-limits"); cl.add_argument("--print", action="store_true")
    dr = sub.add_parser("doctor"); dr.add_argument("--quiet", action="store_true"); dr.add_argument("--deep", action="store_true"); dr.add_argument("--json", action="store_true")
    dr.add_argument("--lane"); dr.add_argument("--next", action="store_true"); dr.add_argument("--fix"); dr.add_argument("--decline"); dr.add_argument("--undecline")
    dr.add_argument("--ack"); dr.add_argument("--open-browser", action="store_true")
    sk = sub.add_parser("set-key"); sk.add_argument("--stdin", action="store_true"); sk.add_argument("--force", action="store_true")
    sk.add_argument("--web", action="store_true"); sk.add_argument("--host", default="127.0.0.1"); sk.add_argument("--port", type=int, default=0); sk.add_argument("--timeout", type=int, default=300)
    sub.add_parser("guard").add_argument("--read-only", action="store_true")
    nt = sub.add_parser("net", help="is the machine online? stdlib sockets, no quota"); nt.add_argument("--lane"); nt.add_argument("--host", action="append", help="a host or host:port; repeatable")
    sub.add_parser("contract").add_argument("--json", action="store_true")
    cf = sub.add_parser("config"); cf.add_argument("action", choices=["set", "show"]); cf.add_argument("key", nargs="?"); cf.add_argument("value", nargs="?")
    ru = sub.add_parser("run"); ru.add_argument("role"); ru.add_argument("--authored-by"); ru.add_argument("--failed", default="")
    ru.add_argument("--premium", nargs="?", const="claude", metavar="FAMILY"); ru.add_argument("--allow-degraded", action="store_true")
    ru.add_argument("--claude-usage"); ru.add_argument("--cwd"); ru.add_argument("--brief"); ru.add_argument("--timeout", type=int, help="seconds; none by default"); ru.add_argument("--idle", type=int, help="seconds without worker output before it is hung; default per CLI"); ru.add_argument("--unattended", action="store_true")
    ru.add_argument("--schema"); ru.add_argument("--resume"); ru.add_argument("--cap", type=float); ru.add_argument("--parent"); ru.add_argument("--task"); ru.add_argument("--title")
    ru.add_argument("--call-id", help="the caller's id for this call: echoed in the answer and written to history.jsonl, the key that joins the caller's record to ours")
    ho = sub.add_parser("hold", help="stop a lane for some roles until a time; --until is mandatory"); ho.add_argument("lane"); ho.add_argument("--until"); ho.add_argument("--roles")
    ho.add_argument("--reason", default=""); ho.add_argument("--set-by", default="owner")
    sub.add_parser("release", help="end a hold early (by id or lane)").add_argument("target")
    sub.add_parser("holds", help="every hold in force, with holder and expiry")
    a = ap.parse_args(argv)
    global UNATTENDED, IDLE_S
    UNATTENDED = getattr(a, "unattended", False)
    IDLE_S = getattr(a, "idle", None)
    if a.cmd == "guard":        # a hook: no config, no state
        return cmd_guard(a)
    if a.cmd == "net":
        return cmd_net(a)
    cfg = load_routing()
    return {"status": cmd_status, "next": cmd_next, "report": cmd_report, "capture-limits": cmd_capture,
            "run": cmd_run, "contract": cmd_contract, "doctor": cmd_doctor, "set-key": cmd_set_key, "config": cmd_config,
            "hold": cmd_hold, "release": cmd_release, "holds": cmd_holds}[a.cmd](a, cfg)


if __name__ == "__main__":
    sys.exit(main())
