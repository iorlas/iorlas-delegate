"""delegate-own, unit side: the process-lister guard, the typed contract, the answer's `worker` field. No process is started."""
import json
import unittest

import hermetic  # noqa: F401
from test_route import ROOT

from delegate.adapters import procs
from delegate.contracts import wire


class OsProc(unittest.TestCase):
    def test_no_module_runs_a_process_lister(self):
        for f in list((ROOT / "skills" / "delegate").rglob("*.py")) + list((ROOT / "src").rglob("*.py")):
            src = f.read_text(encoding="utf-8")
            for bad in ('["ps"', '"lsof"', '"tasklist"', "pgrep", "pkill"):
                self.assertNotIn(bad, src, "%s runs a process lister (%s): the worker's own record (adapters/procs.py) is the source" % (f.name, bad))


class Contract(unittest.TestCase):
    def test_request_flags_are_flags_of_run(self):
        req = wire.RunRequest(role="writer", brief_file="b.md", cwd="/w", schema_file="s.json", resume="r1", authored_by="x", timeout_s=60, idle_s=30,
                              parent="p", task="t", title="T", call_id="c1", live_file="/l.jsonl")
        flags = {a for a in req.to_argv("route.py") if a.startswith("--")}
        self.assertLessEqual(flags, set(json.loads((ROOT / "skills/delegate/contract.json").read_text())["run_args"]))
        self.assertIn("--idle", flags)
        self.assertEqual(req.env(), {"DELEGATE_LIVE_FILE": "/l.jsonl"})
        self.assertNotIn("--resume", wire.RunRequest(role="w", brief_file="b", cwd="/w").to_argv("route.py"))


class WorkerField(unittest.TestCase):
    def test_end_by_outcome(self):
        stop = [wire.WorkerEnd(end=wire.END_OK, stopped=True)]
        self.assertEqual([procs.worker_end(stop, o).end for o in (None, "timeout", "hung", "worker_failed")], ["ok", "timeout", "error", "error"])
        self.assertEqual(procs.worker_end([], None).stopped, False)

    def test_a_survivor_beats_every_outcome(self):
        stops = [wire.WorkerEnd(end=wire.END_SURVIVED, stopped=True, survivors=[wire.Survivor(pid=5)])]
        self.assertEqual([procs.worker_end(stops, o).end for o in (None, "timeout")], ["worker_survived"] * 2)

    def test_reason_worker_survived_is_declared_with_facts(self):
        r = json.loads((ROOT / "skills/delegate/contract.json").read_text())["reasons"]["worker_survived"]
        self.assertEqual((r["scope"], r["permanence"], r["retry"]), ("machine", "needs_owner", "none"))
