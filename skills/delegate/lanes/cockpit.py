"""roles{} for status --json (delegate-0sf, A2.7): per role, may a worker start now (`usable`) and can its vendors be reached (`net`).

Pure: the caller passes `decide` (route.decide), the lanes, and the cached network probe. No process, no socket. A full `status` computes the
rows; `status --cheap` replays them from the snapshot and refreshes the parts that move without a spawn: `net` (the probe cache), live holds.
"""
from . import netprobe, profiles


def families(node):
    """-> every runner family a role's chain names (`"on": family` anywhere inside the role, in order, once)."""
    found = []

    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get("on"), str):
                found.append(x["on"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(node)
    return list(dict.fromkeys(found))


def role_hosts(role):
    return list(dict.fromkeys(h for f in families(role) for h in profiles.hosts_for_family(f)))


def roles_view(cfg, lanes, st, t, decide, cached_net=None):
    """-> {role: {usable, lane, model, why, until, hosts, net}}. `usable` is what `next ROLE` would answer with no flags, in the unattended view
    (credit lanes are never picked unattended). `until` = when a stopped role is expected back (the soonest lane return), else None."""
    live = {n: l for n, l in lanes.items() if l.kind != "credit"}
    held_before = {n: l.held for n, l in lanes.items()}
    out = {}
    try:
        for name, role in cfg["roles"].items():
            hosts = role_hosts(role)
            row = {"usable": None, "lane": None, "model": None, "why": None, "until": None, "hosts": hosts, "net": netprobe.view(cached_net, hosts, t)}
            try:
                d = decide(name, cfg, live, st, t, {"authored_by": None, "failed": [], "premium": None, "allow_degraded": False, "unattended": True})
            except Exception as e:      # one odd role must not take the status down
                row["why"] = f"{type(e).__name__}: {e}"
                out[name] = row
                continue
            if d["action"] == "run":
                row.update(usable=True, lane=d["lane"], model=d["model"])
            else:
                backs = [l.out_until for l in live.values() if l.out and l.out_until and l.out_until > t]
                backs += [b["until"] for l in live.values() for b in l.benched.values() if b.get("until") and b["until"] > t]
                backs += [l.held["until"] for l in live.values() if l.held and l.held.get("until") and l.held["until"] > t]
                row.update(usable=False, why=d.get("say") or d.get("error"), until=min(backs) if backs else None)
            out[name] = row
    finally:
        for n, l in lanes.items():
            l.held = held_before[n]
    return out


def refresh(roles, holds, cached_net, t):
    """-> `roles` (from a snapshot) with what moves without a spawn brought up to date: `net` from the probe cache; `usable` False while a live hold
    names the role's lane; `due` True when a stopped role's `until` has passed (run a full status to learn whether it is back)."""
    out = {}
    for name, r in (roles or {}).items():
        r = dict(r)
        r["net"] = netprobe.view(cached_net, r.get("hosts") or [], t)
        for h in holds or []:
            if r.get("usable") and h.get("lane") == r.get("lane") and (not h.get("roles") or name in h["roles"]) and (h.get("until") or t + 1) > t:
                r.update(usable=False, why=f"on hold: {h.get('reason') or h.get('id')}", until=h.get("until"))
        if r.get("usable") is False and r.get("until") and r["until"] <= t:
            r["due"] = True
        out[name] = r
    return out
