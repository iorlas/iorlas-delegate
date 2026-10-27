"""delegate-own, unit side: the OS module on a fake /proc, the typed contract, the answer's `worker` field. No process is started."""
import hermetic          # noqa: F401
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_route import route, ROOT, CFG

import contracts, osproc               # noqa: E402  (route put skills/delegate on sys.path)
from lanes import reaper               # noqa: E402


def fake_proc(root, rows):
    """rows: {pid: (comm, state, ppid, pgid, start)} written as /proc/<pid>/stat does (comm may hold spaces and parens)."""
    (Path(root) / "self").mkdir(parents=True, exist_ok=True)
    (Path(root) / "self" / "stat").write_text("1 (x) S 0 1 1\n")
    for pid, (comm, st, ppid, pgid, start) in rows.items():
        d = Path(root) / str(pid)
        d.mkdir()
        tail = [st, ppid, pgid, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, start, 0]
        (d / "stat").write_text("%d (%s) %s\n" % (pid, comm, " ".join(map(str, tail))))


class OsProc(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(dir=os.environ["TMPDIR"])
        fake_proc(self.d, {10: ("node", "S", 1, 10, 500), 11: ("codex (native) x", "S", 10, 11, 501), 12: ("sh", "S", 11, 11, 502),
                           13: ("zomb", "Z", 10, 10, 503), 20: ("other", "S", 1, 20, 600)})
        for p in (mock.patch.object(osproc, "PROC", self.d), mock.patch.object(osproc, "POSIX", True)):
            p.start()
            self.addCleanup(p.stop)

    def test_table_skips_zombies_and_reads_comm_with_spaces_and_parens(self):
        self.assertEqual(osproc.table(), {10: (1, 10), 11: (10, 11), 12: (11, 11), 20: (1, 20)})

    def test_descendants_follow_parent_links_across_groups(self):
        self.assertEqual(sorted(osproc.descendants(10)), [11, 12])
        self.assertEqual(osproc.descendants(20), [])

    def test_start_token_names_one_process_and_a_zombie_is_gone(self):
        self.assertEqual((osproc.proc_start(11), osproc.proc_start(13), osproc.proc_start(99)), ("501", None, None))
        self.assertTrue(osproc.alive(12) and not osproc.alive(13))

    def test_no_other_module_lists_processes(self):
        for f in (ROOT / "skills" / "delegate").rglob("*.py"):
            if f.name == "osproc.py":
                continue
            src = f.read_text(encoding="utf-8")
            for bad in ('["ps"', '"lsof"', '"tasklist"', "pgrep", "pkill"):
                self.assertNotIn(bad, src, "%s lists processes itself (%s): ask osproc" % (f.name, bad))


class Contract(unittest.TestCase):
    def test_request_flags_are_flags_of_run(self):
        req = contracts.RunRequest("writer", "b.md", "/w", schema_file="s.json", resume="r1", authored_by="x", timeout_s=60, idle_s=30,
                                   parent="p", task="t", title="T", call_id="c1", live_file="/l.jsonl")
        flags = {a for a in req.argv("route.py") if a.startswith("--")}
        self.assertLessEqual(flags, set(json.loads((ROOT / "skills/delegate/contract.json").read_text())["run_args"]))
        self.assertIn("--idle", flags)
        self.assertEqual(req.env(), {"DELEGATE_LIVE_FILE": "/l.jsonl"})
        self.assertNotIn("--resume", contracts.RunRequest("w", "b", "/w").argv("route.py"))

    def test_answer_parses_with_and_without_a_worker(self):
        ok = contracts.RunResult.parse({"contract": 3, "stop": None, "text": "hi", "secs": 2, "cost": 0.5})
        self.assertEqual((ok.reason, ok.worker.end, ok.text), (None, "ok", "hi"))
        bad = contracts.RunResult.parse({"contract": 3, "stop": {"reason": "worker_survived"}, "worker": {
            "end": "worker_survived", "stopped": True, "kill_sent": True, "survivors": [{"pid": 7, "start": "9"}]}})
        self.assertEqual((bad.reason, bad.worker.survivors), ("worker_survived", [contracts.Survivor(7, "9")]))
        with self.assertRaises(ValueError):
            contracts.RunResult.parse({"contract": 2})

    def test_the_contracts_file_stands_alone(self):
        src = (ROOT / "skills/delegate/contracts.py").read_text()
        self.assertEqual([l for l in src.splitlines() if l.startswith(("import ", "from ")) and "dataclass" not in l and "typing" not in l], ["import sys"])


class WorkerField(unittest.TestCase):
    def setUp(self):
        del reaper.REPORTS[:]

    def test_end_by_outcome(self):
        self.assertEqual([reaper.worker_end(o).end for o in (None, "timeout", "hung", "worker_failed")], ["ok", "timeout", "error", "error"])

    def test_a_survivor_beats_every_outcome(self):
        reaper.REPORTS.append(contracts.WorkerEnd(contracts.END_SURVIVED, stopped=True, survivors=[contracts.Survivor(5)]))
        self.assertEqual([reaper.worker_end(o).end for o in (None, "timeout")], ["worker_survived"] * 2)

    def test_reason_worker_survived_is_declared_with_facts(self):
        r = json.loads((ROOT / "skills/delegate/contract.json").read_text())["reasons"]["worker_survived"]
        self.assertEqual((r["scope"], r["permanence"], r["retry"]), ("machine", "needs_owner", "none"))
