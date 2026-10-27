"""delegate-own, the contract set: REAL processes and signals (a fake agent CLI, no model). The worker is dead when the delegate call ends, and a
survivor is named. Run with `python dev/suite.py --contract`; the unit shards never start these."""
import hermetic          # noqa: F401
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from test_route import route, ROOT

sys.path.insert(0, str(ROOT / "skills" / "delegate"))
import contracts, osproc               # noqa: E402
from lanes import reaper, supervisor   # noqa: E402

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake_agent.py")
POSIX = os.name != "nt"


def wait_pid(path, secs=10):
    end = time.time() + secs
    while time.time() < end:
        try:
            return int(Path(path).read_text())
        except (OSError, ValueError):
            time.sleep(0.05)
    raise AssertionError("the fake never wrote its child's pid")


def dead(pid, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if not osproc.alive(pid):
            return True
        time.sleep(0.1)
    return False


@unittest.skipUnless(POSIX, "process groups and signals are POSIX")
class RealProcesses(unittest.TestCase):
    def setUp(self):
        del reaper.REPORTS[:]
        self.pidfile = os.path.join(os.environ["TMPDIR"], "child.pid")
        self.procs = []
        self.addCleanup(self.reap_leftovers)

    def reap_leftovers(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                f and f.close()

    def start(self, scenario, **kw):
        p = subprocess.Popen([sys.executable, FAKE, scenario, self.pidfile], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             start_new_session=True, **kw)
        self.procs.append(p)
        p.stdin.write("brief")
        p.stdin.close()
        p.stdout.readline()          # its first event: the script is past its signal setup
        return p

    def test_process_group_stop_takes_the_sessions_child_too(self):
        p = self.start("spawner")
        child = wait_pid(self.pidfile)
        rep = reaper.kill_tree(p, grace=1)
        p.wait()
        self.assertTrue(dead(child), "the child in its own session survived")
        self.assertEqual((rep.end, rep.survivors, rep.term_sent, rep.kill_sent), (contracts.END_OK, [], True, True))

    def test_sigkill_escalation_when_sigterm_is_ignored(self):
        p = self.start("hang")
        t0 = time.time()
        rep = reaper.kill_tree(p, grace=1)
        self.assertEqual(p.wait(), -signal.SIGKILL)
        self.assertTrue(rep.term_sent and rep.kill_sent)
        self.assertGreaterEqual(time.time() - t0, 1)         # it waited the grace before SIGKILL
        self.assertLess(time.time() - t0, 10)

    def test_a_worker_that_obeys_sigterm_is_never_sigkilled(self):
        p = self.start("stall")
        rep = reaper.kill_tree(p, grace=5)
        self.assertEqual(p.wait(), -signal.SIGTERM)
        self.assertEqual((rep.term_sent, rep.kill_sent, rep.survivors), (True, False, []))

    def test_worker_survived_names_the_pids_and_ends_the_run_so(self):
        p = self.start("stall")
        pid = p.pid
        with mock.patch.object(osproc, "alive", side_effect=lambda x: x == pid):      # a pid the kernel will not let go
            rep = reaper.kill_tree(p, grace=0)
        self.assertEqual(rep.end, contracts.END_SURVIVED)
        self.assertEqual([s.pid for s in rep.survivors], [pid])
        end = reaper.worker_end(None)
        self.assertEqual((end.end, [s.pid for s in end.survivors]), (contracts.END_SURVIVED, [pid]))
        self.assertEqual(contracts.WorkerEnd.parse(end.wire()), end)

    def run_fake(self, scenario, timeout=0, poll=None):
        route.WORKER_STARTED[:] = [lambda pid: self.__dict__.setdefault("pid", pid)]
        self.addCleanup(route.WORKER_STARTED.clear)
        return route.run_proc([sys.executable, FAKE, scenario, self.pidfile], "brief", os.environ["TMPDIR"], dict(os.environ), timeout, poll=poll)

    def test_normal_run_is_ok_and_nothing_was_stopped(self):
        rc, out, err, timed_out = self.run_fake("normal")
        self.assertEqual((rc, timed_out), (0, False))
        self.assertEqual(json.loads(out.splitlines()[-1])["result"], "fake answer")
        end = reaper.worker_end(None)
        self.assertEqual((end.end, end.stopped), (contracts.END_OK, False))

    def test_call_timeout_stops_a_worker_that_ignores_sigterm(self):
        t0 = time.time()
        rc, out, err, timed_out = self.run_fake("spawner", timeout=2)
        self.assertTrue(timed_out)
        self.assertLess(time.time() - t0, 20)
        end = reaper.worker_end("timeout")
        self.assertEqual((end.end, end.stopped, end.survivors), (contracts.END_TIMEOUT, True, []))
        self.assertTrue(dead(wait_pid(self.pidfile)))
        self.assertTrue(dead(self.pid))

    def test_idle_worker_is_stopped_by_the_watchdog_not_the_wall_clock(self):
        dog = supervisor.Watchdog(first_event_s=5, silence_s=1)
        route.WORKER_TICK[:] = [dog.touch]
        self.addCleanup(route.WORKER_TICK.clear)
        t0 = time.time()
        rc, out, err, timed_out = self.run_fake("stall", timeout=60, poll=dog.poll)
        self.assertEqual((timed_out, dog.why), (False, "silence"))
        self.assertLess(time.time() - t0, 15)
        self.assertTrue(dead(self.pid))

    def test_a_streaming_worker_is_never_idle(self):
        dog = supervisor.Watchdog(first_event_s=5, silence_s=1)
        route.WORKER_TICK[:] = [dog.touch]
        self.addCleanup(route.WORKER_TICK.clear)
        rc, out, err, timed_out = self.run_fake("slow", timeout=60, poll=dog.poll)
        self.assertEqual((rc, dog.why), (0, None))

    def test_sigterm_to_the_delegate_stops_the_worker_tree_first(self):
        code = ("import sys; sys.path.insert(0, %r); sys.argv = ['x']; import route; "
                "route.run_proc([sys.executable, %r, 'spawner', %r], 'b', '.', dict(__import__('os').environ), 0)") % (
                    str(ROOT / "skills" / "delegate"), FAKE, self.pidfile)
        h = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(h)
        child = wait_pid(self.pidfile)
        time.sleep(0.3)
        h.send_signal(signal.SIGTERM)
        self.assertEqual(h.wait(timeout=30), 128 + signal.SIGTERM)
        self.assertTrue(dead(child), "SIGTERM to the delegate left the worker's child running")
