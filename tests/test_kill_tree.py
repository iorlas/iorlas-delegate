"""Tree kill: a worker stopped by timeout, reap or the app-server path leaves no descendant, whatever session it sits in."""
import hermetic  # noqa: F401  (a home of its own for every test)
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_route import route, ROOT

POSIX = os.name != "nt"

# A fake of the Codex CLI: a node-shim analogue that starts a native child in a session of its own; the child ignores SIGTERM.
CHILD = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
SHIM = ("import subprocess,sys,time; "
        "c = subprocess.Popen([sys.executable, '-c', %r], start_new_session=True); "
        "open(sys.argv[1], 'w').write(str(c.pid)); time.sleep(300)") % CHILD


def wait_for(path, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        with contextlib.suppress(OSError, ValueError):
            return int(Path(path).read_text())
        time.sleep(0.05)
    raise AssertionError("the shim never started its child")


def gone(pid, start, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if route.proc_start(pid) != start:
            return True
        time.sleep(0.2)
    return False


@unittest.skipUnless(POSIX, "real process trees are POSIX; the Windows branch is asserted with a mock below")
class RealTrees(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.pidfile = os.path.join(self.d.name, "child.pid")
        self.addCleanup(self.d.cleanup)

    def test_run_proc_timeout_leaves_no_process(self):
        seen = {}
        route.WORKER_STARTED.append(lambda pid: seen.update(shim=pid))
        self.addCleanup(route.WORKER_STARTED.clear)
        t0 = time.time()

        def watch():          # capture start times while both are alive
            child = wait_for(self.pidfile)
            seen.update(child=child, child_start=route.proc_start(child), shim_start=route.proc_start(seen["shim"]))
        import threading
        th = threading.Thread(target=watch)
        th.start()
        rc, out, err, timed_out = route.run_proc([sys.executable, "-c", SHIM, self.pidfile], "", self.d.name, dict(os.environ), 3)
        th.join()
        self.assertTrue(timed_out)
        self.assertLess(time.time() - t0, 15)
        self.assertTrue(seen["child_start"] and seen["shim_start"])
        self.assertTrue(gone(seen["child"], seen["child_start"]), "the shim's native child survived the timeout")
        self.assertTrue(gone(seen["shim"], seen["shim_start"]))

    def test_reap_leaves_no_process(self):
        p = subprocess.Popen([sys.executable, "-c", SHIM, self.pidfile], start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        child = wait_for(self.pidfile)
        v = {"worker_pid": p.pid, "worker_start": route.proc_start(p.pid)}
        cstart = route.proc_start(child)
        self.assertTrue(v["worker_start"] and cstart)
        t0 = time.time()
        self.assertTrue(route.reap(v))
        self.assertTrue(gone(child, cstart), "the shim's native child survived reap")
        self.assertLess(time.time() - t0, 15)

    def test_run_tree_timeout_stops_the_tree(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            route.run_tree([sys.executable, "-c", SHIM, self.pidfile], 3)
        child = wait_for(self.pidfile)
        end = time.time() + 15
        while route.pid_alive(child) and time.time() < end:
            time.sleep(0.2)
        self.assertFalse(route.pid_alive(child), "the shim's native child survived the run_tree timeout")

    def test_stop_tree_grace_sends_term_first(self):
        code = "import signal,sys,time; signal.signal(signal.SIGTERM, lambda *a: (open(sys.argv[1],'w').write('term'), sys.exit(0))); open(sys.argv[1]+'.up','w'); time.sleep(300)"
        flag = os.path.join(self.d.name, "flag")
        p = subprocess.Popen([sys.executable, "-c", code, flag], start_new_session=True)
        wait_for_file = time.time() + 10
        while not os.path.exists(flag + ".up") and time.time() < wait_for_file:
            time.sleep(0.05)
        route.stop_tree(p.pid, grace=3, done=lambda: p.poll() is not None)
        p.wait(5)
        self.assertEqual(Path(flag).read_text(), "term")


class KillPaths(unittest.TestCase):
    def test_windows_branch_uses_taskkill_tree(self):
        p = mock.Mock(pid=4242)
        with mock.patch.object(route.os, "name", "nt"), mock.patch.object(route.subprocess, "run") as run:
            route.kill_tree(p)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/T", "/F", "/PID", "4242"])
        p.kill.assert_called()

    def test_windows_reap_goes_through_taskkill(self):
        with mock.patch.object(route.os, "name", "nt"), mock.patch.object(route.subprocess, "run") as run, \
                mock.patch.object(route, "proc_start", return_value=None):
            route.reap({"worker_pid": 77, "worker_start": "x"})
        self.assertEqual(run.call_args.args[0], ["taskkill", "/T", "/F", "/PID", "77"])

    def test_app_server_kill_goes_through_the_tree_kill(self):
        p = mock.Mock()
        p.stdout.readline.return_value = ""          # the server never answers
        with mock.patch.object(route.shutil, "which", return_value="codex"), mock.patch.object(route.subprocess, "Popen", return_value=p), \
                mock.patch.object(route, "kill_tree") as kt:
            res, err = route.fetch_codex_limits()
        self.assertIsNone(res)
        kt.assert_called_once_with(p)
        p.kill.assert_not_called()

    def test_no_pattern_kills_and_no_bare_kill_in_router(self):
        src = "".join((ROOT / "skills" / "delegate" / f).read_text(encoding="utf-8") for f in ("route.py", "lanes/reaper.py"))
        self.assertIsNone(re.search(r"pkill|killall|pgrep", src))
        # the only .kill() calls are the tree kill's own last-resort fallback
        self.assertEqual(len(re.findall(r"\bp\.kill\(\)", src)), 1)
        self.assertEqual(len(re.findall(r"\.terminate\(\)", src)), 0)


if __name__ == "__main__":
    unittest.main()
