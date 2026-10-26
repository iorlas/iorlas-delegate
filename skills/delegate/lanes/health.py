"""Lane health as a replayable fold over history.jsonl (delegate-rdt, delegate-a9x).

Pure: no clock, no I/O except `read_rows` (reads a file) and `load`/`save` (a checkpoint cache). `fold(rows, state)` turns finished-run rows
into a state; `view(state, lane, model, t)` answers "may this (lane, model) pair launch at time t, and if not why". The same rows replayed
in time order give the same answer, so a past incident is a regression fixture (tests/test_health.py).

Two kinds of fact, both read from the run's outcome (lanes/classify.py, contract.json):
  lane facts   rate_limited, credit_exhausted, logged_out: the whole lane is out until a time (the vendor's, else a probe time).
  pair facts   vendor_blip, worker_failed, hung: counted per fingerprint per (lane, model) inside a window; enough of them bench the pair on a
               15/30/60 min ladder. A launch after a bench ends is the canary: it passes (a `done`) and the ladder resets, or it fails and the
               next rung starts at once.
Never counted: bad_request (the caller's fault, 61 5.1), network, killed, timeout, secrets, schema_invalid, refusals (no run_id).
"""
import json
import re

from . import classify

V = 1
DEFAULTS = {"n": 3, "window_min": 10, "ladder_min": (15, 30, 60), "credit_probe_s": 6 * 3600, "logout_s": 900, "guess_s": 15 * 60,
            "stale_usage_s": 6 * 3600}
# per-reason breaker rows: n / window_min fall back to the defaults; `tasks` = distinct tasks the failures must come from (one bead failing
# three times benches nothing: 60-design-A2 2.5)
BREAKER = {"vendor_blip": {"tasks": 2}, "worker_failed": {"tasks": 3}, "hung": {"n": 2, "window_min": 30, "tasks": 1}}
LANE_KINDS = ("rate_limited", "credit_exhausted", "logged_out")
STATES = ("up", "busy", "exhausted", "logged_out", "held", "broken")


def policy(settings=None):
    """-> DEFAULTS overridden by settings["breaker"] (config.json `breaker`: n, window_min, ladder_min, ...)."""
    pol = dict(DEFAULTS)
    pol.update({k: v for k, v in ((settings or {}).get("breaker") or {}).items() if k in DEFAULTS})
    pol["ladder_min"] = tuple(pol["ladder_min"])
    return pol


def norm(text):
    """Vendor text with what varies per run (times, ids, numbers, paths) replaced, so two runs of one failure compare equal."""
    s = (text or "").lower()
    s = re.sub(r"\d{4}-\d\d-\d\d[t ]\d\d:\d\d(:\d\d)?(\.\d+)?z?", "#", s)
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{8,}\b|\bses_\w+", "#", s)
    s = re.sub(r"(?:/[\w.@~-]+){2,}", "<p>", s)
    s = re.sub(r"\d+", "#", s)
    return re.sub(r"\s+", " ", s).strip()[:200]


def fingerprint(outcome, source, text):
    """12 chars naming one failure: same outcome from the same channel with the same normalised text."""
    import hashlib
    return hashlib.sha1(f"{outcome}|{source}|{norm(text)}".encode("utf-8")).hexdigest()[:12]


def row_fields(outcome, source, say, until=None):
    """-> the health fields a history row carries (v, outcome, fingerprint, say, until); {} for a done run (outcome None)."""
    if not outcome:
        return {"v": V}
    return {"v": V, "outcome": outcome, "fingerprint": fingerprint(outcome, source, say), "say": (say or "")[:240], "until": until}


def read_rows(path, since=0.0):
    """-> the history rows (dicts) with ts > since, in file order. A corrupt line is skipped, never fatal."""
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("ts"), (int, float)) and r["ts"] > since:
                    rows.append(r)
    except OSError:
        pass
    return rows


def row_ts(line):
    """-> the ts of one history line, or None when the line is corrupt or has none."""
    try:
        r = json.loads(line)
    except ValueError:
        return None
    return r["ts"] if isinstance(r, dict) and isinstance(r.get("ts"), (int, float)) else None


def _outcome(row):
    """-> "done", "release", an outcome name that feeds health, or None (not a fact: a refusal, a caller fault, network, ...)."""
    if row.get("release"):
        return "release"
    if row.get("run_id") is None:
        return None
    if row.get("status") == "done":
        return "done"
    o = row.get("outcome") or (row.get("reason") if row.get("reason") in classify.CONTRACT["reasons"] else None)
    return o if o and classify.benches_pair(o) and (o in LANE_KINDS or o in BREAKER) else None


def new_state():
    return {"v": V, "ts": 0.0, "pairs": {}, "lanes": {}}


def _pair(state, lane, model):
    return state["pairs"].setdefault(f"{lane}|{model}", {"streak": [], "rung": 0, "until": 0.0, "kind": None, "fp": None, "evidence": "", "source": None,
                                                         "fails": [], "last_done": 0.0})


def _fail(p, row, outcome):
    p["fails"] = (p["fails"] + [{"ts": row["ts"], "outcome": outcome, "say": (row.get("say") or "")[:120]}])[-3:]


def step(state, row, pol):
    """Apply one history row to `state` (mutates). Rows must come in time order."""
    state["ts"] = max(state["ts"], row["ts"])
    o, lane, model, ts = _outcome(row), row.get("lane"), row.get("model"), row["ts"]
    if o is None or not lane:
        return
    if o == "release":       # `route.py report --event ok`: the owner says the lane is fine
        state["lanes"].pop(lane, None)
        for k in [k for k in state["pairs"] if k.startswith(lane + "|") and (not model or k == f"{lane}|{model}")]:
            state["pairs"].pop(k)
        return
    if o == "done":          # a run that worked proves the lane and resets the ladder of its pair
        state["lanes"].pop(lane, None)
        p = _pair(state, lane, model)
        p.update(streak=[], rung=0, until=0.0, kind=None, fp=None, evidence="", source=None, last_done=ts)
        return
    if o in LANE_KINDS:
        until = row.get("until")
        if o == "credit_exhausted":      # a spending limit has no timer: look again after the probe time
            until, src = ts + pol["credit_probe_s"], "probe"
        elif o == "logged_out":
            until, src = ts + pol["logout_s"], "probe"
        else:
            src = "vendor" if isinstance(until, (int, float)) and until > ts else "guess"
            until = until if src == "vendor" else ts + pol["guess_s"]
        state["lanes"][lane] = {"kind": o, "until": float(until), "source": src, "evidence": (row.get("say") or "")[:240], "ts": ts}
        return
    # a pair fact (breaker)
    p = _pair(state, lane, model)
    _fail(p, row, o)
    if ts < p["until"]:        # launched while benched (a call already in flight): it does not count again
        return
    rule = BREAKER[o]
    n, win = rule.get("n", pol["n"]), rule.get("window_min", pol["window_min"])
    fp = row.get("fingerprint") or fingerprint(o, "", row.get("say"))
    task = row.get("task") or row.get("run_id")
    probation = p["rung"] > 0 and ts >= p["until"]           # the canary failed: the next rung starts at once
    p["streak"] = [s for s in p["streak"] if ts - s[0] <= win * 60 and s[1] == o] + [[ts, o, fp, task]]
    same = [s for s in p["streak"] if s[2] == fp]
    if probation or (len(same) >= n and len({s[3] for s in same}) >= rule["tasks"]):
        rung = min(p["rung"] + 1, len(pol["ladder_min"]))
        p.update(rung=rung, until=ts + pol["ladder_min"][rung - 1] * 60, kind=o, fp=fp, evidence=(row.get("say") or "")[:240], source="breaker", streak=[])


def fold(rows, state=None, pol=None):
    """-> the state after `rows` (history rows) on top of `state` (a checkpoint, else empty). Rows at or before the checkpoint's ts are skipped,
    so fold(all) == fold(tail, fold(head)): the checkpoint is a cache, never truth."""
    pol = pol or policy()
    state = state if state is not None else new_state()
    for r in sorted((r for r in rows if r["ts"] > state["ts"]), key=lambda r: r["ts"]):
        step(state, r, pol)
    return state


def view(state, lane, model, t, pol=None):
    """-> {state, usable, canary, until, source, kind, evidence, rung, last_failures} of one pair at time t. `usable` is False while a bench or a
    lane-level out is running; once it ends the pair is `up` with canary True: the next launch is the one probe before full traffic."""
    pol = pol or policy()
    L, p = state["lanes"].get(lane), state["pairs"].get(f"{lane}|{model}")
    out = {"state": "up", "usable": True, "canary": False, "until": None, "source": None, "kind": None, "evidence": "", "rung": 0,
           "last_failures": list((p or {}).get("fails") or [])}
    if p:
        out["rung"] = p["rung"]
    if L:
        if t < L["until"]:
            out.update(state="logged_out" if L["kind"] == "logged_out" else "exhausted", usable=False, until=L["until"], source=L["source"], kind=L["kind"], evidence=L["evidence"])
            return out
        out["canary"] = True
        out.update(kind=L["kind"], evidence=L["evidence"])
    if p and p["rung"]:
        if t < p["until"]:
            out.update(state="broken", usable=False, until=p["until"], source=p["source"], kind=p["kind"], evidence=p["evidence"])
        else:
            out["canary"] = True
            out.update(kind=p["kind"], evidence=p["evidence"])
    return out


def combine(v, held=None, busy=False, needs_login=False):
    """-> the one state word of a pair: held > logged_out > exhausted > broken > busy > up. Holds and leases are not history facts, so the caller passes them."""
    if held:
        return "held"
    if v["state"] in ("logged_out", "exhausted", "broken"):
        return v["state"]
    return "busy" if busy else "up"


def stale_usage(read_at, t, pol=None):
    """-> hours old when a usage reading is older than the stale limit (it is then unknown, never a silent gate), else None."""
    pol = pol or policy()
    return round((t - read_at) / 3600, 1) if read_at and t - read_at > pol["stale_usage_s"] else None


def save(path, state, pol):
    import os
    try:
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"policy": dict(pol, ladder_min=list(pol["ladder_min"])), "state": state}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def load(path, pol):
    """-> the cached state, or None when missing, unreadable or built under another policy."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if d["policy"] == dict(pol, ladder_min=list(pol["ladder_min"])) and d["state"].get("v") == V:
            return d["state"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None
