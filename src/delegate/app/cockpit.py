"""The cockpit: one row per known lane (state, reason, unlock time, windows, model), the pick `next worker` would make, and the text of it.
`lane_view` is pure given its inputs."""
from delegate.adapters.harness import classify
from delegate.app import env, harnesses, usage
from delegate.app.decide import busy_jitter
from delegate.app.fleet import mark_busy
from delegate.contracts.routing import CallArgs
from delegate.domain.lanes.profiles import LOGIN_CMD
from delegate.domain.holds.book import HoldBook
from delegate.domain.lanes.leases import LeaseBook
from delegate.domain.routing.router import Router
from delegate.domain.time.spec import fmt_in, local, when

def needs_input(lanes):
    need = []
    cs = lanes.get("CLAUDE_SUBSCRIPTION")
    if cs and not cs.usage_known and env.session_host() == "desktop":
        need.append({"what": "claude_usage", "why": "Claude subscription use is unknown", "how": "in the Claude desktop app call the get_usage tool and pass its windows as --claude-usage JSON ({five_hour:{used,resets_at},weekly:{used,resets_at}})"})
    ox = lanes.get("OPENAI_SUBSCRIPTION")
    if ox and not ox.usage_known and not ox.out:
        need.append({"what": "codex_login", "why": ox.notes[0] if ox.notes else "codex limits unreadable", "how": "ask the user to run `codex login`, or report the lane out with `route.py report`"})
    return need


def finished_runs(n):
    """The last n finished runs, newest last: the dashboard's view of history.jsonl (the file itself stays private)."""
    keys = ("run_id", "ts", "role", "task", "lane", "model", "done", "outcome", "secs", "cost", "tokens", "denials")
    return [{k: getattr(r, k) for k in keys} for r in env.history_store().runs(n)]


EMOJI = {"ready": "✅", "resting": "⏳", "logged_out": "🔑", "spend": "💸", "slow": "🐢", "off": "⛔", "unknown": "❓"}
LEGEND = {"ready": "ready", "resting": "usage limit", "logged_out": "logged out", "spend": "spend/credit limit",
          "slow": "slow/hung", "off": "disabled", "unknown": "no read yet"}
LABELS = {"CLAUDE_SUBSCRIPTION": "Claude subscription", "CLAUDE_CREDIT": "Claude API credits", "OPENAI_SUBSCRIPTION": "ChatGPT / Codex",
          "GROK_SUBSCRIPTION": "SuperGrok / OpenCode", "DEEPSEEK_FREE": "DeepSeek / OpenCode Free", "ZAI_CODING_SUBSCRIPTION": "Z.ai GLM Coding Plan"}
SLOW_WINDOW = 2 * 3600  # a hang or timeout this recent, with no good run on the lane since, keeps it marked slow
SLOW_KINDS = ("hung", "timeout")


def login_state(name, st, t):
    """-> (ok, why, checked_at): probe_login cached a few minutes in state, so a status call costs one process per lane at most."""
    c = usage.auth_cache().fresh(name, t, usage.AUTH_TTL, st)
    if c:
        return c.ok, c.why, c.at
    ok, why = harnesses.probe_login(name)
    usage.auth_cache().remember(name, ok, why, t)
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
    if env.profile_lanes().get(name) == "declined":
        return f"declined: `route.py doctor --undecline {name}` brings it back"
    if login[0] is False:
        return f"not configured: logged out, run `{LOGIN_CMD[name]}`"
    return f"not configured: `route.py doctor --lane {name}` sets it up" + (f" ({login[1]})" if login[0] is None and login[1] else "")


def classify_out(lane, rec):
    """Why a lane that is out is out -> logged_out | spend | slow | resting. rec is its state.json entry (older ones have no typed reason)."""
    r = lane.out_reason
    text = " ".join([rec.why if rec else ""] + lane.notes)
    if r == "logged_out" or (r in (None, "error") and classify.says_login(text)):
        return "logged_out"
    if lane.spend_blocked or classify.says_spend(text) or (r == "limit" and lane.kind == "credit"):
        return "spend"
    if r == "hung":
        return "slow"
    return "resting"


def limit_words(text, lane):
    top = max((w for w in lane.windows if w["used"] is not None), key=lambda w: w["used"], default=None)
    return classify.limit_words(text, top["name"] if top and top["used"] >= 95 else None)


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
    S, c = env.settings_of(cfg), cfg["lanes"][name]
    v = {"id": name, "label": c.get("label") or LABELS.get(name, name), "family": c["family"], "kind": c["kind"], "model": lane_model(cfg, name),
         "state": "off", "emoji": EMOJI["off"], "reason": off_why or "disabled or not configured", "detail": None,
         "unlock": {"locked": False, "known": False, "at": None, "in_s": None, "text": None, "why": "not in use"},
         "windows": [], "low_tokens": [], "warnings": list(lane.warn) if lane else [], "meter": c["reader"] != "none",
         "login": {"ok": login[0], "why": login[1], "checked_at": login[2]}, "running": running}
    if lane is None:
        return v
    v["windows"], v["low_tokens"] = window_rows(lane, S, t)
    rec = st.out.get(name)
    relogin = f"waits for you to log in (`{LOGIN_CMD[name]}`); re-checked every {usage.AUTH_TTL // 60} min"
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
    S = env.settings_of(cfg)
    fams = {s["on"] for s in cfg["roles"]["worker"]["steps"] if s["on"] != "claude"}
    parts = []
    for name, c in cfg["lanes"].items():
        if c["family"] not in fams:
            continue
        l = lanes.get(name)
        parts.append(f"{name} " + ("not set up" if l is None else l.usable(S, True)[1]))
    return "; ".join(parts) or "no other lane exists"


def next_worker(cfg, lanes, st, t):
    """The lane `next worker` would pick now: decide() on a copy, nothing written."""
    routing = env.typed_routing(cfg)
    cur = mark_busy(lanes, st, routing.settings, t, busy_jitter)
    d = Router(routing, HoldBook(st.holds), st.prev, t).route("worker", CallArgs(), cur).as_dict()
    if d["action"] == "run":
        r = {"action": "run", "lane": d["lane"], "model": d["model"], "effort": d["effort"], "pass": d["pass"], "fallback": d["fallback"], "why": None}
        if cfg["lanes"][d["lane"]]["family"] == "claude":
            r["why"] = claude_why(cfg, cur)
        return r
    return {"action": d["action"], "lane": d.get("lane"), "model": None, "effort": None, "say": d.get("say") or d.get("error")}


def cockpit(cfg, lanes, st, t):
    """The cockpit dict: one row per known lane, configured or not, plus the pick `next worker` would make."""
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
