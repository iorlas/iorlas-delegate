"""Network facts (delegate-4n7, A2.5): is the machine online, and does this text say a connection dropped.

Pure but for sockets and the cache file it is given. stdlib only (Windows too), no subprocess, no vendor call, so a probe costs no quota.
`probe()` resolves each host and opens a TCP connection to it, all hosts at once, and gives up on the whole thing after `budget_s`
(socket.getaddrinfo ignores socket timeouts, so the join deadline is what bounds the call). `up` means a vendor name resolved AND connected: a
neutral IP literal that answers while names do not is reported as `ip_up`, because that is a DNS outage and the vendor is still unreachable.
"""
import json
import os
import re
import socket
import threading
import time

BUDGET_S = 2.5          # whole probe; route.py net answers in under 3 s
CONNECT_S = 1.5         # one TCP connect
FRESH_S = 120           # a cached probe older than this is "unknown"
NEUTRAL = ("one.one.one.one:443",)      # a name every resolver knows: the fallback when no vendor host is given
IP_LITERAL = "1.1.1.1:443"              # no DNS: separates "offline" from "DNS is down"

# What a vendor or the OS says when the connection itself broke. `max_output_tokens` after "stream disconnected" is the model's cap, not the network.
DROP_RE = re.compile(r"ENOTFOUND|ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|EPIPE|getaddrinfo|could not resolve host|network is unreachable"
                     r"|cannot connect to api|unable to connect|error sending request|connection (reset|closed|refused|timed out|failed)|socket hang up"
                     r"|dns error|network error|waiting for network|(?:TLS|SSL) handshake|stream disconnected(?!.{0,200}max_output_tokens)", re.I | re.S)
WAITING_RE = re.compile(r"^\s*Reconnecting\.\.\.\s*waiting for network", re.I)      # codex waits for the network by itself; the engine is the one waiter


def looks_dropped(text):
    """-> True when `text` (a vendor error event or stderr, never the model's answer) says the connection broke."""
    return bool(DROP_RE.search(text or ""))


def waiting(text):
    """-> True for codex's own "Reconnecting... waiting for network" line (no n/of): progress, not a failure."""
    return bool(WAITING_RE.match(text or ""))


def split_host(h, default_port=443):
    h = h.strip()
    if h.count(":") == 1 and h.rsplit(":", 1)[1].isdigit():
        name, port = h.rsplit(":", 1)
        return name, int(port)
    return h, default_port


def literal(name):
    return bool(re.fullmatch(r"\d+\.\d+\.\d+\.\d+", name))


def _try(host, out, connect_s, resolve, connect):
    name, port = split_host(host)
    try:
        addr = name if literal(name) else resolve(name, port)
    except (OSError, UnicodeError):
        out[host] = "dns"
        return
    try:
        connect((addr, port), connect_s)
        out[host] = "ok"
    except OSError:
        out[host] = "tcp"


def _resolve(name, port):
    return socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)[0][4][0]


def _connect(addr, timeout):
    socket.create_connection(addr, timeout).close()


def probe(hosts=None, budget_s=BUDGET_S, connect_s=CONNECT_S, resolve=_resolve, connect=_connect, clock=time.time):
    """-> {up, ip_up, why, ms, hosts: {host: ok|dns|tcp|slow}, at}. `hosts` are vendor hosts ("name" or "name:port"); the neutral ones are added when none is
    given. `why`: ok | dns (names do not resolve, the IP literal connects) | vendor (names resolve, the vendor host refuses, the IP literal connects) | offline."""
    t0 = time.monotonic()
    names = list(dict.fromkeys(hosts or NEUTRAL))
    allh = names + [IP_LITERAL]
    out = {}
    threads = [threading.Thread(target=_try, args=(h, out, connect_s, resolve, connect), daemon=True) for h in allh]
    for th in threads:
        th.start()
    for th in threads:
        th.join(max(0.0, budget_s - (time.monotonic() - t0)))
    res = {h: out.get(h, "slow") for h in allh}
    up = any(res[h] == "ok" for h in names)
    ip_up = res[IP_LITERAL] == "ok"
    return {"up": up, "ip_up": ip_up, "why": "ok" if up else "dns" if ip_up and any(res[h] == "dns" for h in names) else "vendor" if ip_up else "offline", "ms": int((time.monotonic() - t0) * 1000), "hosts": res, "at": clock()}


# ---- the cache: written by `route.py net` and by every probe a run makes, read by status --cheap (which opens no socket) --------------

def record(path, res, clock=time.time):
    """Store a probe in `path` (atomic replace). `since` moves only when up flips, so outages are countable. -> the stored dict. Never raises."""
    prev = load(path) or {}
    since = prev.get("since") if prev.get("up") == res["up"] and prev.get("since") else res["at"]
    row = {"v": 1, "up": res["up"], "ip_up": res["ip_up"], "why": res["why"], "hosts": res["hosts"], "at": res["at"], "since": since}
    try:
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(row, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return row


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("at"), (int, float)) else None
    except (OSError, ValueError):
        return None


def view(cached, hosts, t, fresh_s=FRESH_S):
    """-> {up: True|False|None, since, age_s} for a set of vendor `hosts` from a cached probe. None = no probe, or one older than fresh_s.
    A host the probe did not test says nothing; when none of them was tested the answer is the probe's overall `up`."""
    if not cached or t - cached["at"] > fresh_s:
        return {"up": None, "since": cached and cached.get("since"), "age_s": int(t - cached["at"]) if cached else None}
    seen = [cached["hosts"][h] for h in hosts if h in (cached.get("hosts") or {})]
    up = any(x == "ok" for x in seen) if seen else bool(cached["up"])
    return {"up": up, "since": cached.get("since"), "age_s": int(t - cached["at"])}
