"""The network probe: one run of adapters/netprobe (sockets only, no process, no vendor call, no quota), its cache file, and the view of the cache that
`status` shows. `net_up` is the one question the outcome ladder asks when a failure names no cause."""
import json

from delegate.adapters import netprobe
from delegate.app import env
from delegate.domain.lanes import profiles


def network_view(cached, t):
    return dict(netprobe.view(cached, [], t), why=(cached or {}).get("why"))


def net_probe(hosts=None):
    """-> one probe (lanes/netprobe.py), stored in the net cache. Sockets only: no process, no vendor call, no quota."""
    res = netprobe.probe(hosts)
    netprobe.record(str(env.net_cache_path()), res)
    return res


def net_up(lane=None):
    """True when `lane`'s vendor hosts (else all of them) are reachable now. The one question the outcome ladder asks when a failure names no cause."""
    return bool(net_probe(list(profiles.hosts_of(lane)) or None)["up"])


def cmd_net(a):
    """route.py net: the probe, as one JSON object. Exit 0 whenever it printed."""
    hosts = list(a.host or []) or list(profiles.hosts_of(a.lane)) or list(profiles.all_hosts()) + list(netprobe.NEUTRAL)
    res = net_probe(hosts)
    cached = netprobe.load(str(env.net_cache_path())) or {}
    print(json.dumps({"v": 1, "up": res["up"], "ip_up": res["ip_up"], "why": res["why"], "ms": res["ms"], "hosts": res["hosts"], "at": res["at"], "since": cached.get("since")}))
    return 0
