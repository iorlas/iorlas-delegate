"""The command line: `route.py VERB ...`. A bad flag in `run` is the caller's fault, answered as a bad_request stop (exit 0, no usage text)."""
import argparse
import sys
from typing import NoReturn, override

from delegate.app import answer

USAGE = """delegate router: deterministic lane and step selection for an LLM orchestrator.

Python 3.8+, standard library only. Windows, macOS, Linux, Docker.
The model supplies only what the script cannot read itself; the script decides.

  route.py status [--claude-usage JSON|@file|-] [--json] [--cheap]
  route.py next ROLE [--authored-by openai|claude|xai] [--premium [FAMILY]] [--allow-degraded]
                     [--claude-usage JSON|@file|-] [--cwd DIR] [--verbose]
  route.py run ROLE [--cwd DIR] [--brief FILE] [--schema FILE] [--resume REF] [--cap USD] [--timeout SEC] [--unattended] [--authored-by W] [--premium] [--allow-degraded] [--only-family F]...
                    [--claude-usage JSON] [--parent SESSION] [--task ID] [--title T]    brief on stdin; one JSON object on stdout, exit 0 (see docs/dev/integrations.md)
  route.py net [--lane L] [--host H ...]    is the machine online? stdlib sockets, no quota, answers in under 3 s: {up, why, hosts}
  route.py report --lane LANE --event out|ok [--until EPOCH|ISO]
  route.py capture-limits [--print]  (status line JSON on stdin)
  route.py doctor [--json]   (also `init`) a table of every lane: enabled, ready, and what to run to fix it
  route.py config set KEY VALUE | show
  route.py hold LANE --until EPOCH|ISO|+2h|+3d [--roles worker,light] [--reason TEXT] | release ID|LANE | holds   an owner hold stops a lane for some roles; --until is mandatory

Data: routing.json next to this file (lanes, models, roles, thresholds).
Per-user settings: <config dir>/config.json (same keys as "settings"); profile and state live in <config dir>:
  $DELEGATE_CONFIG_DIR, else Claude Code's plugin data dir (~/.claude/plugins/data/<plugin>-<marketplace>); a checkout must set the variable.
Workers never create branches or worktrees and never commit, merge, push or stash: the coordinator owns git.
"""


class CallParser(argparse.ArgumentParser):
    """A bad flag, a missing argument or a bad value in `run` is the caller's fault: answered as a bad_request stop (exit 0, no usage text, no
    vendor started), so the engine never has to read English to tell it from a crash. Other commands keep argparse's own exit 2."""
    answers = False
    unattended = False      # the caller is an engine: it gets every field of the answer

    @override
    def error(self, message: str) -> NoReturn:
        if not CallParser.answers:
            super().error(message)
        answer.emit(answer.result("bad_request", say=f"route.py: {message}", fix=f"update task-loop or delegate: {message}"), not CallParser.unattended)
        sys.exit(0)


def build():
    ap = CallParser(prog="route.py", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--claude-usage"); s.add_argument("--json", action="store_true"); s.add_argument("--cheap", action="store_true", help="no process spawns: last status snapshot plus cached logins, with age_s")
    n = sub.add_parser("next"); n.add_argument("role"); n.add_argument("--authored-by")
    n.add_argument("--premium", nargs="?", const="claude", metavar="FAMILY"); n.add_argument("--allow-degraded", action="store_true")
    n.add_argument("--claude-usage"); n.add_argument("--cwd"); n.add_argument("--verbose", action="store_true")
    r = sub.add_parser("report"); r.add_argument("--lane", required=True); r.add_argument("--event", choices=["out", "ok"], required=True)
    r.add_argument("--until")
    cl = sub.add_parser("capture-limits"); cl.add_argument("--print", action="store_true")
    dr = sub.add_parser("doctor", aliases=["init"]); dr.add_argument("--json", action="store_true")
    g = sub.add_parser("guard"); g.add_argument("--read-only", action="store_true")
    g.add_argument("--engine-only", action="append", default=[], help="a recipe only the engine runs; repeatable")
    nt = sub.add_parser("net", help="is the machine online? stdlib sockets, no quota"); nt.add_argument("--lane"); nt.add_argument("--host", action="append", help="a host or host:port; repeatable")
    sub.add_parser("contract").add_argument("--json", action="store_true")
    cf = sub.add_parser("config"); cf.add_argument("action", choices=["set", "show"]); cf.add_argument("key", nargs="?"); cf.add_argument("value", nargs="?")
    ru = sub.add_parser("run"); ru.add_argument("role"); ru.add_argument("--authored-by")
    ru.add_argument("--premium", nargs="?", const="claude", metavar="FAMILY"); ru.add_argument("--allow-degraded", action="store_true")
    ru.add_argument("--claude-usage"); ru.add_argument("--cwd"); ru.add_argument("--brief"); ru.add_argument("--timeout", type=int, help="seconds; none by default"); ru.add_argument("--idle", type=int, help="seconds without worker output before it is hung; default per CLI"); ru.add_argument("--unattended", action="store_true")
    ru.add_argument("--schema"); ru.add_argument("--resume"); ru.add_argument("--cap", type=float); ru.add_argument("--parent"); ru.add_argument("--task"); ru.add_argument("--title")
    ru.add_argument("--engine-only", action="append", default=[], help="a recipe only the engine runs; a write step's guard blocks it; repeatable")
    ru.add_argument("--only-family", action="append", default=[], metavar="FAMILY", help="use only this family's steps, no fallback outside; repeatable")
    ru.add_argument("--call-id", help="the caller's id for this call: echoed in the answer and written to history.jsonl, the key that joins the caller's record to ours")
    ho = sub.add_parser("hold", help="stop a lane for some roles until a time; --until is mandatory"); ho.add_argument("lane"); ho.add_argument("--until"); ho.add_argument("--roles")
    ho.add_argument("--reason", default=""); ho.add_argument("--set-by", default="owner")
    sub.add_parser("release", help="end a hold early (by id or lane)").add_argument("target")
    sub.add_parser("holds", help="every hold in force, with holder and expiry")
    return ap
