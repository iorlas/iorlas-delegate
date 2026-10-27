"""A worker dies with its delegate call (delegate-own): stop the whole process tree, SIGTERM then SIGKILL, and say who is left.

`stop_tree(pid, grace, done)` finds the tree by parent links (never a pattern), signals every pid and every process group in it (the Codex CLI
is a node shim whose native child may sit in a group of its own and ignore SIGTERM), waits `grace` seconds after SIGTERM, then SIGKILLs and
re-collects until nothing below is left. -> contracts.WorkerEnd: stopped, term_sent, kill_sent, waited_s and `survivors` (pid + start token)
that outlived every signal. `kill_tree` is the same for a Popen and keeps the report in REPORTS, which route folds into the run's answer.
All process listing is osproc's; this file only signals.
"""
import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import contracts      # noqa: E402
import osproc         # noqa: E402

GRACE_S = float(os.environ.get("DELEGATE_STOP_GRACE") or 5)       # SIGTERM to SIGKILL
REPORTS = []        # one WorkerEnd per kill_tree call in this process


def _signal_all(groups, pids, sig):
    for g in groups:
        with contextlib.suppress(OSError):
            os.killpg(g, sig)
    for c in pids:
        with contextlib.suppress(OSError):
            os.kill(c, sig)


def stop_tree(pid, grace=0, done=None):
    """-> WorkerEnd. With `grace`, SIGTERM first and wait up to `grace` s for `done()` (default: nothing known is alive); then SIGKILL."""
    rep = contracts.WorkerEnd(contracts.END_OK, stopped=True)
    if os.name == "nt":
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        rep.kill_sent = True
        return rep
    me, mygroup, t0 = os.getpid(), os.getpgrp(), time.time()
    known, groups = {pid}, set()

    def collect():
        tab = osproc.table()
        known.update([pid] + osproc.descendants(pid, tab))      # a dead parent orphans its children: pids are kept from before the first signal
        groups.update(tab[c][1] for c in known if c in tab)
        groups.discard(mygroup)
        known.discard(me)

    def left():
        return [c for c in known if osproc.alive(c)]
    collect()
    if grace:
        _signal_all(groups, known, signal.SIGTERM)
        rep.term_sent = True
        end = time.time() + grace
        while time.time() < end and not (done() if done else not left()):
            time.sleep(0.1)
    for _ in range(5):
        collect()
        if not left():
            break
        _signal_all(groups, known, signal.SIGKILL)
        rep.kill_sent = True
        time.sleep(0.05)
    rep.waited_s = round(time.time() - t0, 2)
    rep.survivors = [contracts.Survivor(c, osproc.proc_start(c)) for c in left()]
    if rep.survivors:
        rep.end = contracts.END_SURVIVED
    return rep


def kill_tree(p, grace=None):
    """Stop the worker `p` (a Popen) and its whole tree: killing only the codex wrapper leaves the vendor binary running."""
    rep = contracts.WorkerEnd(contracts.END_ERROR, stopped=True)
    with contextlib.suppress(Exception):
        rep = stop_tree(p.pid, GRACE_S if grace is None else grace)
    with contextlib.suppress(Exception):
        p.kill()
    REPORTS.append(rep)
    return rep


def worker_end(outcome):
    """The run's WorkerEnd, from the stops this process made and the run's outcome reason (None = done)."""
    stops = list(REPORTS)
    survivors = [s for r in stops for s in r.survivors]
    end = (contracts.END_SURVIVED if survivors else contracts.END_TIMEOUT if outcome == "timeout" else contracts.END_ERROR if outcome
           else contracts.END_OK)
    return contracts.WorkerEnd(end, stopped=bool(stops), term_sent=any(r.term_sent for r in stops), kill_sent=any(r.kill_sent for r in stops),
                               waited_s=round(sum(r.waited_s for r in stops), 2), survivors=survivors)
