"""`status`: the payload (lanes, cockpit, running, holds, asks, health, roles, network), the snapshot it leaves for `status --cheap`, and the network
probe cache. `--cheap` spawns nothing: the last snapshot plus the cached logins, with the leases read fresh."""
import contextlib
import json
import os
from dataclasses import replace

from delegate.adapters import netprobe
from delegate.app import cockpit, decide, env, net, roles, usage
from delegate.app.fleet import settle_holds
from delegate.contracts.stores import private
from delegate.domain.health import ledger as health_ledger
from delegate.domain.holds.book import HoldBook
from delegate.domain.lanes.leases import LeaseBook, suspect
from delegate.domain.routing.router import Router

def status_payload(cfg, lanes, ck, st, t):
    return {"v": 1, "ts": t,
            "lanes": {n: {"family": l.family, "kind": l.kind, "out": l.out, "out_until": l.out_until, "reason": l.out_reason if l.out else None, "five": l.five, "week": l.week,
                          "pace": l.pace, "burn": l.burn, "spend": l.spend, "cap": l.cap, "notes": l.notes} for n, l in lanes.items()},
            "cockpit": ck,
            "running": running_rows(st, t),
            "needs_input": cockpit.needs_input(lanes), "holds": HoldBook(st.holds).rows(t), "asks": [x.as_dict() for x in settle_holds(env.states(), lanes, st, t)[0]],
            "health": health_rows(cfg, lanes, st, t), **roles_payload(cfg, lanes, st, t)}


def roles_payload(cfg, lanes, st, t):
    """roles{} (usable and net per role) and network{} for status --json; the probe is read from its cache, never run here."""
    cached = netprobe.load(str(env.net_cache_path()))
    routing = env.typed_routing(cfg)
    router = Router(routing, HoldBook(st.holds), st.prev, t)
    return {"roles": roles.roles_view(cfg, lanes, router, st, t, cached), "network": net.network_view(cached, t)}


def health_rows(cfg, lanes, st, t):
    """One row per (lane, model): state (up|busy|exhausted|logged_out|held|broken), until, source, evidence, leases{live,suspect}, last_failures[3]."""
    rows, pol = [], env.settings_of(cfg).breaker
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
        p = env.snapshot_path()
        tmp = p.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, p)
        private(p)


def cmd_status_cheap(a, cfg):
    """Zero process spawns and no network: the last full status (status-snapshot.json) with `running` read fresh from the leases and the
    cached logins from auth-cache.json. `age_s` = seconds since the snapshot was taken (null when no status has run yet)."""
    t = env.now()
    st = env.load_state()
    snap = {}
    with contextlib.suppress(OSError, ValueError):
        d = json.loads(env.snapshot_path().read_text(encoding="utf-8"))
        snap = d if isinstance(d, dict) else {}
    at = snap.get("ts")
    out = {k: v for k, v in snap.items() if k not in ("ts", "running")}
    out.update(v=1, cheap=True, ts=t, age_s=int(t - at) if isinstance(at, (int, float)) else None, running=running_rows(st, t), holds=HoldBook(st.holds).rows(t),
               auth=usage.auth_cache().rows(st, t))
    out.setdefault("lanes", {})
    cached = netprobe.load(str(env.net_cache_path()))       # a file, not a socket: cheap opens no connection
    out.update(roles=roles.refresh(snap.get("roles"), out["holds"], cached, t), network=net.network_view(cached, t))
    print(json.dumps(out) if a.json else "\n".join(f"{n}: {c['ok']} ({c['age_s']}s old)" for n, c in out["auth"].items()) + f"\nrunning: {len(out['running'])}  snapshot age: {out['age_s']}s")
    return 0


def cmd_status(a, cfg):
    if a.cheap:
        return cmd_status_cheap(a, cfg)
    t = env.now()
    st = env.load_state()
    lanes = decide.fleet_builder(cfg).build(st, t, decide.parse_json_arg(a.claude_usage)).lanes
    decide.publish_lanes(lanes, st, t)
    ck = cockpit.cockpit(cfg, lanes, st, t)
    payload = status_payload(cfg, lanes, ck, st, t)
    write_snapshot(payload)
    if a.json:
        print(json.dumps(payload))
        return 0
    print("\n".join(cockpit.cockpit_text(ck)))
    for n in cockpit.needs_input(lanes):
        print(f"needs input: {n['what']}: {n['how']}")
    return 0
