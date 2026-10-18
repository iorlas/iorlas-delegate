#!/usr/bin/env python3
"""Where each delegate lane stands right now, and a suggested order. Python 3 stdlib only.

usage: limits.py [--json]

codex   live from `codex app-server` (account/rateLimits/read): 5-hour and weekly use, reset times.
max     from ~/.claude/state/max-limits.json, written by the status line (capture-limits.sh).
        Fallback when that is missing or stale: the weekly reset moment from MAX_WEEKLY_RESET
        in ~/.config/claude-credit-worker/config (e.g. MAX_WEEKLY_RESET="Sun 17:00"); use stays unknown.
credit  the API credit has no balance reading. Known: the reset day (CREDIT_RESET_DAY, default 1),
        this script's own spend ledger vs MONTHLY_CAP, and an "out" marker that run.sh writes on exit 75.

Per lane: use %, hours to reset, pace, verdict.
  pace  = percent of the week/cycle that has elapsed minus percent used. Positive: under-used, slack to spend.
          Negative: spending faster than the window allows.
  BURN  = the weekly window resets within BURN_MINUTES and at least BURN_MIN_LEFT percent is left.
  OUT   = a window is at 98 percent or more, the provider says so, or run.sh saw "credit balance too low".
  Order = BURN lanes first; then the base order (codex, credit, max); a lane whose pace is at or below
          -15 drops behind a lane whose pace is +25 or more.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time

BURN_MINUTES = 120
BURN_MIN_LEFT = 10
STALE_SECONDS = 45 * 60
CONF_DIR = os.path.expanduser("~/.config/claude-credit-worker")
STATE = os.path.expanduser("~/.claude/state/max-limits.json")
BASE_ORDER = ["codex", "credit", "max"]
WEEK_MIN = 7 * 24 * 60


def read_conf():
    conf = {}
    try:
        for line in open(os.path.join(CONF_DIR, "config")):
            m = re.match(r'\s*([A-Z_]+)=("([^"]*)"|\'([^\']*)\'|(\S*))', line)
            if m:
                conf[m.group(1)] = m.group(3) or m.group(4) or m.group(5) or ""
    except OSError:
        pass
    return conf


def fmt_left(epoch, now):
    s = int(epoch - now)
    if s <= 0:
        return "reset"
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return f"{d}d{h}h" if d else (f"{h}h{m:02d}m" if h else f"{m}m")


def local(epoch):
    return dt.datetime.fromtimestamp(epoch).strftime("%a %H:%M")


def window(label, used, resets_at, now, span_min):
    """Returns (text, used, minutes_to_reset, pace)."""
    if used is None or resets_at is None:
        return f"{label} ?", None, None, None
    if resets_at <= now:
        return f"{label} 0% (window reset)", 0.0, 0, None
    mins = (resets_at - now) / 60
    elapsed = max(0.0, min(100.0, 100 * (1 - mins / span_min)))
    pace = elapsed - float(used)
    return (f"{label} {float(used):.0f}% (resets {fmt_left(resets_at, now)}, {local(resets_at)})",
            float(used), mins, pace)


def verdict(five, week, reached=False):
    if reached:
        return "OUT", "provider reports the limit reached"
    u5 = five[0] if five else None
    uw = week[0] if week else None
    mw = week[1] if week else None
    if (u5 is not None and u5 >= 98) or (uw is not None and uw >= 98):
        return "OUT", "a window is full"
    if uw is None and u5 is None:
        return "UNKNOWN", "no data"
    if (uw is not None and mw is not None and 0 < mw <= BURN_MINUTES
            and 100 - uw >= BURN_MIN_LEFT and (u5 is None or u5 < 90)):
        return "BURN", f"weekly window resets in {int(mw)}m with {100 - uw:.0f}% left: spend it"
    return "OK", ""


def codex_status(now):
    def row(v, text, why="", pace=None):
        return {"lane": "codex", "verdict": v, "text": text, "why": why, "pace": pace}
    try:
        p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    except FileNotFoundError:
        return row("UNKNOWN", "codex CLI not installed")

    def send(m):
        p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()

    def read(i, deadline):
        while time.time() < deadline:
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
        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "delegate-limits", "title": "delegate limits", "version": "1"}}})
        if read(1, time.time() + 15) is None:
            return row("UNKNOWN", "app-server did not answer")
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        r = read(2, time.time() + 20)
    finally:
        p.kill()
    if not r or "result" not in r:
        err = (r or {}).get("error", {}).get("message", "no answer (logged in? run `codex login`)")
        return row("UNKNOWN", err)
    res = r["result"]
    rl = res.get("rateLimits") or {}
    pr, se = rl.get("primary") or {}, rl.get("secondary") or {}
    t5, u5, m5, _ = window("5h", pr.get("usedPercent"), pr.get("resetsAt"), now, pr.get("windowDurationMins") or 300)
    tw, uw, mw, pw = window("week", se.get("usedPercent"), se.get("resetsAt"), now, se.get("windowDurationMins") or WEEK_MIN)
    reached = bool(rl.get("rateLimitReachedType")) or res.get("ordinaryUsageAllowed") is False
    v, why = verdict((u5, m5) if u5 is not None else None, (uw, mw) if uw is not None else None, reached)
    cr = rl.get("credits") or {}
    extra = f"  credits {cr.get('balance')}" if cr.get("hasCredits") else ""
    return row(v, f"{t5}  {tw}{extra}", why, pw)


def next_weekly(spec, now):
    """spec like 'Sun 17:00' (local time) -> epoch of the next such moment, or None."""
    try:
        day, hm = spec.split()
        hh, mm = (int(x) for x in hm.split(":"))
        target = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(day[:3].lower())
    except (ValueError, AttributeError):
        return None
    t = dt.datetime.fromtimestamp(now)
    cand = t.replace(hour=hh, minute=mm, second=0, microsecond=0) + dt.timedelta(days=(target - t.weekday()) % 7)
    if cand.timestamp() <= now:
        cand += dt.timedelta(days=7)
    return cand.timestamp()


def max_status(now, conf):
    def row(v, text, why="", pace=None):
        return {"lane": "max", "verdict": v, "text": text, "why": why, "pace": pace}
    manual = next_weekly(conf.get("MAX_WEEKLY_RESET", ""), now)
    manual_txt = f"week resets {fmt_left(manual, now)}, {local(manual)} (from config; use unknown)" if manual else ""
    try:
        with open(STATE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return row("UNKNOWN", manual_txt or "no state file; the status line has not written it yet", "")
    age = now - float(d.get("updated", 0))
    rl = d.get("rate_limits") or {}
    f5, f7 = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    t5, u5, m5, _ = window("5h", f5.get("used_percentage"), f5.get("resets_at"), now, 300)
    tw, uw, mw, pw = window("week", f7.get("used_percentage"), f7.get("resets_at"), now, WEEK_MIN)
    v, why = verdict((u5, m5) if u5 is not None else None, (uw, mw) if uw is not None else None)
    if age > STALE_SECONDS and not (f7.get("resets_at") and f7["resets_at"] <= now):
        return row("STALE", manual_txt or f"{t5}  {tw}",
                   f"data is {int(age // 60)}m old; no interactive session is reporting")
    return row(v, f"{t5}  {tw}", why, pw)


def credit_status(now, conf):
    def row(v, text, why="", pace=None):
        return {"lane": "credit", "verdict": v, "text": text, "why": why, "pace": pace}
    day = int(conf.get("CREDIT_RESET_DAY", "1") or 1)
    cap = float(conf.get("MONTHLY_CAP", "150") or 150)
    t = dt.datetime.fromtimestamp(now)
    this = t.replace(day=min(day, 28), hour=0, minute=0, second=0, microsecond=0)
    if this.timestamp() > now:
        prev_m = (this.replace(day=1) - dt.timedelta(days=1))
        start = prev_m.replace(day=min(day, 28))
        nxt = this
    else:
        start = this
        nm = (this.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        nxt = nm.replace(day=min(day, 28))
    spent = 0.0
    try:
        for line in open(os.path.join(CONF_DIR, "spend.jsonl")):
            try:
                e = json.loads(line)
                ts = dt.datetime.fromisoformat(e["ts"]).timestamp()
                if ts >= start.timestamp():
                    spent += float(e["cost"])
            except (ValueError, KeyError):
                continue
    except OSError:
        pass
    span = (nxt.timestamp() - start.timestamp()) / 60
    mins = (nxt.timestamp() - now) / 60
    elapsed = 100 * (1 - mins / span)
    used_pct = 100 * spent / cap if cap else 0
    pace = elapsed - used_pct
    text = (f"cycle ${spent:.2f} of ${cap:.0f} cap by this script (resets {fmt_left(nxt.timestamp(), now)}, "
            f"{nxt.strftime('%b %d')}); balance unreadable")
    try:
        out_until = float(open(os.path.join(CONF_DIR, "out-until")).read().strip())
    except (OSError, ValueError):
        out_until = 0
    if out_until > now:
        return row("OUT", text, f"run.sh saw 'credit balance too low'; back at the {nxt.strftime('%b %d')} reset", pace)
    if spent >= cap:
        return row("OUT", text, "own monthly cap reached", pace)
    return row("UNKNOWN", text, "", pace)


def order(rows):
    by = {r["lane"]: r for r in rows}
    live = [l for l in BASE_ORDER if by[l]["verdict"] != "OUT"]
    burn = [l for l in live if by[l]["verdict"] == "BURN"]
    rest = [l for l in live if l not in burn]
    slack = [l for l in rest if (by[l]["pace"] or 0) >= 25]
    tight = [l for l in rest if by[l]["pace"] is not None and by[l]["pace"] <= -15]
    if slack:
        rest = [l for l in rest if l not in tight] + tight
    return burn + rest


def main():
    now = time.time()
    conf = read_conf()
    rows = [codex_status(now), credit_status(now, conf), max_status(now, conf)]
    seq = order(rows)
    out = [r["lane"] for r in rows if r["verdict"] == "OUT"]
    if "--json" in sys.argv:
        print(json.dumps({"lanes": rows, "order": seq, "out": out}))
        return
    for r in rows:
        pace = f" pace {r['pace']:+.0f}" if r.get("pace") is not None else ""
        why = f"  -> {r['why']}" if r["why"] else ""
        print(f"{r['lane']:<7} {r['text']}\n        {r['verdict']}{pace}{why}")
    print("order: " + " > ".join(seq) + (f"   (out: {', '.join(out)})" if out else ""))


if __name__ == "__main__":
    main()
