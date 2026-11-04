"""route.py's main: parse, load the routing table, call the verb."""
import contextlib
import sys

from delegate.app import doctor, env, net, status
from delegate.cli import parser, verbs

TABLE = {"status": status.cmd_status, "next": verbs.cmd_next, "report": verbs.cmd_report, "capture-limits": verbs.cmd_capture,
         "run": verbs.cmd_run, "contract": verbs.cmd_contract, "doctor": doctor.cmd_doctor, "init": doctor.cmd_doctor, "config": doctor.cmd_config,
         "hold": verbs.cmd_hold, "release": verbs.cmd_release, "holds": verbs.cmd_holds}
def cmd_net_only(a: env.Args, cfg: dict | None = None):
    return net.cmd_net(a)


NO_CONFIG = {"guard": verbs.cmd_guard, "net": cmd_net_only}      # guard loads host policy itself; neither needs routing.json


def main(argv: list[str] | None = None):
    for stream in (sys.stdout, sys.stderr):      # the cockpit prints emoji: a console on another encoding must not crash on them
        with contextlib.suppress(Exception):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure and (stream.encoding or "").lower().replace("-", "") != "utf8":
                reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:] if argv is None else list(argv)
    parser.CallParser.answers, parser.CallParser.unattended = args[:1] == ["run"], "--unattended" in args
    exec_argv = None
    if args[:1] == ["guard"] and "--exec-allow" in args:
        index = args.index("--exec-allow")
        args, exec_argv = args[:index], args[index + 1:]
    a = parser.build().parse_args(args)
    if a.cmd == "guard":
        a.exec_allow = exec_argv
    if a.cmd in NO_CONFIG:
        return NO_CONFIG[a.cmd](a)
    return TABLE[a.cmd](a, env.load_routing())
