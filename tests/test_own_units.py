"""delegate-own, unit side: the OS module on a fake /proc, the typed contract, the answer's `worker` field. No process is started."""
import hermetic          # noqa: F401
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_route import route, ROOT, CFG

from delegate.adapters import procs    # noqa: E402  (route put skills/delegate and src/ on sys.path)
from delegate.contracts import wire    # noqa: E402


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
        p = mock.patch.object(procs, "PROC", self.d)
        p.start()
        self.addCleanup(p.stop)

    def test_table_skips_zombies_and_reads_comm_with_spaces_and_parens(self):
        self.assertEqual(procs.table(), {10: (1, 10), 11: (10, 11), 12: (11, 11), 20: (1, 20)})

    def test_descendants_follow_parent_links_across_groups(self):
        self.assertEqual(sorted(procs.descendants(10)), [11, 12])
        self.assertEqual(procs.descendants(20), [])

    def test_start_token_names_one_process_and_a_zombie_is_gone(self):
        self.assertEqual((procs.start_token(11), procs.start_token(13), procs.start_token(99)), ("501", None, None))
        self.assertTrue(procs.alive(12) and not procs.alive(13))

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
