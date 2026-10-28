"""route.py's main: parse, load the routing table, call the verb."""
import contextlib
import sys

from delegate.app import doctor, env, net, status
from delegate.cli import parser, verbs

TABLE = {"status": status.cmd_status, "next": verbs.cmd_next, "report": verbs.cmd_report, "capture-limits": verbs.cmd_capture,
         "run": verbs.cmd_run, "contract": verbs.cmd_contract, "doctor": doctor.cmd_doctor, "init": doctor.cmd_doctor, "set-key": doctor.cmd_set_key, "config": doctor.cmd_config,
         "hold": verbs.cmd_hold, "release": verbs.cmd_release, "holds": verbs.cmd_holds}
NO_CONFIG = {"guard": verbs.cmd_guard, "net": lambda a, cfg=None: net.cmd_net(a)}      # a hook (no config, no state) and the probe


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):      # the cockpit prints emoji: a console on another encoding must not crash on them
        with contextlib.suppress(Exception):
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:] if argv is None else list(argv)
    parser.CallParser.answers, parser.CallParser.unattended = args[:1] == ["run"], "--unattended" in args
    a = parser.build().parse_args(argv)
    if a.cmd in NO_CONFIG:
        return NO_CONFIG[a.cmd](a)
    return TABLE[a.cmd](a, env.load_routing())
