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
from delegate.adapters import procs
from delegate.adapters.harness.base import LaunchSpec
from delegate.adapters.harness.codex import CodexHarness
from delegate.app.publish import LiveSink
from delegate.app.supervisor import RunHooks, Supervisor
from delegate.contracts.state import Lease

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
        if procs.start_token(pid) != start:
            return True
        time.sleep(0.2)
    return False


class RealTrees(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.pidfile = os.path.join(self.d.name, "child.pid")
        self.addCleanup(self.d.cleanup)

    def test_supervisor_timeout_leaves_no_process(self):
        seen = {}
        t0 = time.time()

        def watch():          # capture start times while both are alive
            child = wait_for(self.pidfile)
            seen.update(child=child, child_start=procs.start_token(child), shim_start=procs.start_token(seen["shim"]))
        import threading
        th = threading.Thread(target=watch)
        th.start()
        sup = Supervisor(CodexHarness(), "codex", RunHooks(started=lambda w: seen.update(shim=w.pid)), LiveSink("codex", ""))
        out = sup.run(LaunchSpec(argv=[sys.executable, "-c", SHIM, self.pidfile], env=dict(os.environ), cwd=self.d.name, stdin=""), 3)
        th.join()
        self.assertTrue(out.timed_out)
        self.assertLess(time.time() - t0, 15)
        self.assertTrue(seen["child_start"] and seen["shim_start"])
        self.assertTrue(gone(seen["child"], seen["child_start"]), "the shim's native child survived the timeout")
        self.assertTrue(gone(seen["shim"], seen["shim_start"]))

    def test_reap_leaves_no_process(self):
        p = subprocess.Popen([sys.executable, "-c", SHIM, self.pidfile], start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        child = wait_for(self.pidfile)
        v = Lease(lane="L", pid=1, start=0.0, expires=0.0, worker_pid=p.pid, worker_start=procs.start_token(p.pid))
        cstart = procs.start_token(child)
        self.assertTrue(v.worker_start and cstart)
        t0 = time.time()
        self.assertTrue(route.reap(v))
        self.assertTrue(gone(child, cstart), "the shim's native child survived reap")
        self.assertLess(time.time() - t0, 15)

    def test_run_tree_timeout_stops_the_tree(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            procs.run_capture([sys.executable, "-c", SHIM, self.pidfile], 3)
        child = wait_for(self.pidfile)
        end = time.time() + 15
        while procs.alive(child) and time.time() < end:
            time.sleep(0.2)
        self.assertFalse(procs.alive(child), "the shim's native child survived the run_tree timeout")

    def test_stop_tree_grace_sends_term_first(self):
        code = "import signal,sys,time; signal.signal(signal.SIGTERM, lambda *a: (open(sys.argv[1],'w').write('term'), sys.exit(0))); open(sys.argv[1]+'.up','w'); time.sleep(300)"
        flag = os.path.join(self.d.name, "flag")
        p = subprocess.Popen([sys.executable, "-c", code, flag], start_new_session=True)
        wait_for_file = time.time() + 10
        while not os.path.exists(flag + ".up") and time.time() < wait_for_file:
            time.sleep(0.05)
        procs.stop_tree(p.pid, grace=3, done=lambda: p.poll() is not None)
        p.wait(5)
        self.assertEqual(Path(flag).read_text(), "term")


class KillPaths(unittest.TestCase):
    def fake_server(self, body):
        """A `codex` whose app-server runs `body` (python); returns the which() patch."""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        exe = Path(d.name) / "codex"
        exe.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n")
        exe.chmod(0o755)
        return mock.patch.object(route.shutil, "which", return_value=str(exe))

    def test_a_silent_app_server_is_cut_at_the_deadline_and_killed_through_the_tree_kill(self):
        with self.fake_server("time.sleep(300)"), mock.patch.object(route, "APP_SERVER_S", 1), \
                mock.patch.object(procs, "kill_tree", wraps=procs.kill_tree) as kt:
            t0 = time.monotonic()
            res, err = route.fetch_codex_limits()
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual((res, err), (None, "codex app-server did not answer"))
        kt.assert_called_once()

    def test_a_partial_line_does_not_hang_and_answers_are_read_across_chunks(self):
        body = ("sys.stdin.readline(); print('{\"id\":1,\"result\":{}}', flush=True); sys.stdin.readline(); sys.stdin.readline()\n"
                "sys.stdout.write('{\"id\":2,\"res'); sys.stdout.flush(); time.sleep(0.3); print('ult\":{\"ok\":1}}', flush=True); time.sleep(300)")
        with self.fake_server(body):
            res, err = route.fetch_codex_limits()
        self.assertEqual((res, err), ({"ok": 1}, None))

    def test_no_pattern_kills_and_no_bare_kill_in_router(self):
        src = "".join((ROOT / "skills" / "delegate" / f).read_text(encoding="utf-8") for f in ("route.py", "../../src/delegate/adapters/procs.py", "../../src/delegate/app/supervisor.py"))
        self.assertIsNone(re.search(r"pkill|killall|pgrep", src))
        # the only .kill() calls are the tree kill's own last-resort fallback
        self.assertEqual(len(re.findall(r"\bp\.kill\(\)", src)), 1)
        self.assertEqual(len(re.findall(r"\.terminate\(\)", src)), 0)


if __name__ == "__main__":
    unittest.main()
