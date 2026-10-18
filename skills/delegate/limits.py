#!/usr/bin/env python3
"""Where each delegate lane stands right now. Python 3 stdlib only.

usage: limits.py [--json]

codex   asked live from `codex app-server` (account/rateLimits/read): 5-hour and weekly use, reset times.
max     read from ~/.claude/state/max-limits.json, written by the status line
        (see capture-limits.sh). Only an interactive session keeps it fresh.
credit  not readable by script. claude-credit-worker run.sh exits 75 when it is empty.

Verdict per lane: OK | BURN | OUT | STALE | UNKNOWN
  BURN = the weekly window resets within BURN_MINUTES and at least BURN_MIN_LEFT percent is left:
         that allowance is lost at the reset, so spend it first.
  OUT  = a window is at 98 percent or more, or the provider says the limit is reached.
"""
import json
import os
import subprocess
import sys
import time

BURN_MINUTES = 120
BURN_MIN_LEFT = 10
STALE_SECONDS = 45 * 60
STATE = os.path.expanduser("~/.claude/state/max-limits.json")


def fmt_left(epoch, now):
    s = int(epoch - now)
    if s <= 0:
        return "reset"
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def window(label, used, resets_at, now):
    """used is a 0-100 number or None; returns (text, used_effective, minutes_to_reset)."""
    if used is None or resets_at is None:
        return f"{label} ?", None, None
    if resets_at <= now:
        return f"{label} 0% (window reset)", 0.0, 0
    mins = (resets_at - now) / 60
    return f"{label} {used:.0f}% (resets {fmt_left(resets_at, now)})", float(used), mins


def verdict(five, week, reached=False):
    """five/week are (used, mins_to_reset) tuples or None."""
    if reached:
        return "OUT", "provider reports the limit reached"
    used5 = five[0] if five else None
    usedw = week[0] if week else None
    minsw = week[1] if week else None
    if (used5 is not None and used5 >= 98) or (usedw is not None and usedw >= 98):
        return "OUT", "a window is full"
    if usedw is None and used5 is None:
        return "UNKNOWN", "no data"
    if (usedw is not None and minsw is not None and minsw <= BURN_MINUTES
            and 100 - usedw >= BURN_MIN_LEFT and (used5 is None or used5 < 90)):
        return "BURN", f"weekly window resets in {int(minsw)}m with {100 - usedw:.0f}% left: spend it"
    return "OK", ""


def codex_status(now):
    try:
        p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    except FileNotFoundError:
        return {"lane": "codex", "verdict": "UNKNOWN", "text": "codex CLI not installed", "why": ""}

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
            return {"lane": "codex", "verdict": "UNKNOWN", "text": "app-server did not answer", "why": ""}
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        r = read(2, time.time() + 20)
    finally:
        p.kill()
    if not r or "result" not in r:
        err = (r or {}).get("error", {}).get("message", "no answer (logged in? run `codex login`)")
        return {"lane": "codex", "verdict": "UNKNOWN", "text": err, "why": ""}
    res = r["result"]
    rl = res.get("rateLimits") or {}
    pr, se = rl.get("primary") or {}, rl.get("secondary") or {}
    t5, u5, m5 = window("5h", pr.get("usedPercent"), pr.get("resetsAt"), now)
    tw, uw, mw = window("week", se.get("usedPercent"), se.get("resetsAt"), now)
    reached = bool(rl.get("rateLimitReachedType")) or res.get("ordinaryUsageAllowed") is False
    v, why = verdict((u5, m5) if u5 is not None else None, (uw, mw) if uw is not None else None, reached)
    cr = rl.get("credits") or {}
    extra = f"  credits {cr.get('balance')}" if cr.get("hasCredits") else ""
    return {"lane": "codex", "verdict": v, "text": f"{t5}  {tw}{extra}", "why": why}


def max_status(now):
    try:
        with open(STATE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"lane": "max", "verdict": "UNKNOWN",
                "text": "no state file; the status line has not written it yet", "why": ""}
    age = now - float(d.get("updated", 0))
    rl = d.get("rate_limits") or {}
    f5, f7 = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    t5, u5, m5 = window("5h", f5.get("used_percentage"), f5.get("resets_at"), now)
    tw, uw, mw = window("week", f7.get("used_percentage"), f7.get("resets_at"), now)
    v, why = verdict((u5, m5) if u5 is not None else None, (uw, mw) if uw is not None else None)
    stale = age > STALE_SECONDS
    if stale and v in ("OK", "BURN"):
        # a past reset time still proves a reset; anything else may have moved
        v, why = "STALE", f"data is {int(age // 60)}m old; no interactive session is reporting"
    return {"lane": "max", "verdict": v, "text": f"{t5}  {tw}", "why": why}


def main():
    now = time.time()
    rows = [codex_status(now), max_status(now),
            {"lane": "credit", "verdict": "UNKNOWN",
             "text": "not readable; run.sh exits 75 when empty", "why": ""}]
    burn = [r["lane"] for r in rows if r["verdict"] == "BURN"]
    out = [r["lane"] for r in rows if r["verdict"] == "OUT"]
    if "--json" in sys.argv:
        print(json.dumps({"lanes": rows, "burn_first": burn, "out": out}))
        return
    for r in rows:
        why = f"  -> {r['why']}" if r["why"] else ""
        print(f"{r['lane']:<7} {r['text']:<58} {r['verdict']}{why}")
    if burn:
        print("suggest: spend " + ", ".join(burn) + " first (use-it-or-lose-it), then the usual order")
    if out:
        print("out: " + ", ".join(out))


if __name__ == "__main__":
    main()
