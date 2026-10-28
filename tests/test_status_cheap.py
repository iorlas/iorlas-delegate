"""status --cheap: zero process spawns, last snapshot plus cached logins with age_s; next reads the login cache instead of spawning."""
import hermetic  # noqa: F401  (a home of its own for every test)
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from delegate.adapters import procs
import routing_support as support
from test_route import route, T
from delegate.app import env
from delegate.app import usage
from delegate.app import status
from delegate.app import harnesses
import shutil


def boom(*a, **k):
    raise AssertionError("a process was spawned: %r" % (a[:1],))


class Cheap(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        p = mock.patch.dict(os.environ, {"DELEGATE_CONFIG_DIR": self.d.name})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("DELEGATE_NOCACHE", None)

    def cheap(self):
        out = io.StringIO()
        with mock.patch.object(subprocess, "run", side_effect=boom), mock.patch.object(subprocess, "Popen", side_effect=boom), \
                mock.patch.object(procs, "run_capture", side_effect=boom), contextlib.redirect_stdout(out):
            rc = route.main(["status", "--json", "--cheap"])
        return rc, json.loads(out.getvalue())

    def test_no_snapshot_yet(self):
        rc, d = self.cheap()
        self.assertEqual(rc, 0)
        self.assertIn("age_s", d)
        self.assertIsNone(d["age_s"])
        self.assertTrue(d["cheap"])

    def test_snapshot_and_cached_logins(self):
        now = env.now()
        status.write_snapshot({"v": 1, "ts": now - 90, "lanes": {"X": {}}, "cockpit": {"v": 1}, "running": [], "recent": [], "needs_input": []})
        usage.auth_cache().remember("CLAUDE_SUBSCRIPTION", False, "not logged in", now - 30)
        rc, d = self.cheap()
        self.assertEqual(rc, 0)
        self.assertTrue(89 <= d["age_s"] <= 100)
        self.assertEqual(d["lanes"], {"X": {}})
        self.assertIs(d["auth"]["CLAUDE_SUBSCRIPTION"]["ok"], False)
        self.assertTrue(29 <= d["auth"]["CLAUDE_SUBSCRIPTION"]["age_s"] <= 40)

    def test_running_comes_from_fresh_leases_without_ps(self):
        now = env.now()
        env.update_state(lambda s: s.leases.__setitem__("r1", support.lease(lane="L", role="worker", start=now - 5, expires=now + 60)))
        rc, d = self.cheap()
        self.assertEqual([r["run_id"] for r in d["running"]], ["r1"])

    def test_full_status_writes_the_snapshot_and_the_cache(self):
        out = io.StringIO()
        with mock.patch.object(harnesses, "probe_login", return_value=(True, "ok")), contextlib.redirect_stdout(out):
            route.main(["status", "--json"])
        self.assertTrue(env.snapshot_path().exists())
        rc, d = self.cheap()
        self.assertIsInstance(d["age_s"], int)
        self.assertIn("cockpit", d)


class AuthCache(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        p = mock.patch.dict(os.environ, {"DELEGATE_CONFIG_DIR": self.d.name})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("DELEGATE_NOCACHE", None)

    def next_worker(self):
        calls = []

        def fake(cmd, timeout, **kw):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"loggedIn": True}), "")
        with mock.patch.object(procs, "run_capture", side_effect=fake), mock.patch.object(usage, "fetch_codex_limits", return_value=(None, "no codex")), \
                mock.patch.object(shutil, "which", side_effect=lambda n, *a, **k: "/bin/" + n if n == "claude" else None), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            route.main(["next", "worker"])
        return [c for c in calls if c[1:3] == ["auth", "status"]]

    def test_fresh_cache_means_no_auth_status_call(self):
        usage.auth_cache().remember("CLAUDE_SUBSCRIPTION", True, "x", env.now() - 60)
        self.assertEqual(self.next_worker(), [])

    def test_stale_or_missing_cache_asks_once_and_fills_the_cache(self):
        self.assertEqual(len(self.next_worker()), 1)             # missing
        self.assertEqual(self.next_worker(), [])                 # now cached
        usage.auth_cache().remember("CLAUDE_SUBSCRIPTION", True, "x", env.now() - usage.AUTH_FILE_TTL - 5)
        self.assertEqual(len(self.next_worker()), 1)             # stale

    def test_cached_logout_is_honoured(self):
        usage.auth_cache().remember("CLAUDE_SUBSCRIPTION", False, "x", env.now())
        with mock.patch.object(procs, "run_capture", side_effect=AssertionError("spawned")):
            self.assertTrue(usage.claude_logged_out())

    def test_ttl_is_ten_minutes(self):
        self.assertEqual(usage.AUTH_FILE_TTL, 600)


if __name__ == "__main__":
    unittest.main()
