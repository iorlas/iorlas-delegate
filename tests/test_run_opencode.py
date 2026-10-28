"""`route.py run` on the OpenCode lanes: watchdog, sandboxed shell, Grok answers and cost."""
import hermetic  # noqa: F401  (a home of its own for every test)
import json
import os
import sys
import time
from unittest import mock
import unittest
from pathlib import Path
from test_route import why
from delegate.adapters.harness import opencode_sandbox as box
from delegate.app import env
from delegate.app import watch
from run_support import RunBase


class RunOpenCode(RunBase):
    @unittest.skipIf(sys.platform != "darwin", "the Grok lane runs commands under sandbox-exec: macOS only")
    def test_opencode_limit_in_its_log_stops_the_child_and_marks_the_lane_out_once(self):
        log = Path(self.tmp.name) / ".local" / "share" / "opencode" / "log"
        log.mkdir(parents=True, exist_ok=True)
        pid = self.watchdog_fake('echo "ERROR Free usage limit reached" >> "%s/opencode.log"; sleep 60' % log)
        t0 = time.time()
        _, r = self.run_it(timeout=0)
        self.assertLess(time.time() - t0, 20)
        self.assertEqual((why(r), r["stop"]["permanence"], r["lane"]), ("rate_limited", "transient", "GROK_SUBSCRIPTION"))
        out = env.load_state().out["GROK_SUBSCRIPTION"]
        self.assertIn("Free usage limit reached", out["why"])
        self.assertGreater(out["until"], time.time())
        self.assertLessEqual(out["until"], (time.time() // 86400 + 1) * 86400 + 1)
        self.assert_child_gone(pid)

    @unittest.skipIf(sys.platform != "darwin", "the Grok lane runs commands under sandbox-exec: macOS only")
    def test_opencode_with_no_first_event_is_stopped_and_the_lane_marked_hung(self):
        pid = self.watchdog_fake("sleep 60")
        with mock.patch.dict(watch.LIMITS["opencode"], first_event_s=1):
            t0 = time.time()
            _, r = self.run_it(timeout=0)
        self.assertLess(time.time() - t0, 20)
        self.assertEqual((why(r), r["stop"]["permanence"]), ("hung", "transient"))
        self.assertEqual(env.load_state().out["GROK_SUBSCRIPTION"].reason, "hung")
        self.assert_child_gone(pid)

    @unittest.skipIf(sys.platform != "darwin", "the Grok lane runs commands under sandbox-exec: macOS only")
    def test_opencode_normal_run_is_not_touched_by_the_watchdog(self):
        log = Path(self.tmp.name) / ".local" / "share" / "opencode" / "log"
        log.mkdir(parents=True, exist_ok=True)
        (log / "opencode.log").write_text("old: Free usage limit reached\n")        # before launch: ignored
        self.grok_ready()
        self.codex_out()
        with mock.patch.dict(watch.LIMITS["opencode"], first_event_s=20):
            _, r = self.run_it(timeout=0)
        self.assertEqual((r["stop"], r["text"]), (None, "grok did it"))
        self.assertNotIn("GROK_SUBSCRIPTION", env.load_state().out)

    @unittest.skipIf(sys.platform != "darwin", "the Grok lane runs commands under sandbox-exec: macOS only")
    def test_grok_is_the_worker_after_codex_in_a_sandboxed_shell_without_user_config(self):
        self.grok_ready()
        self.codex_out()
        _, r = self.run_it()
        self.assertEqual((r["stop"], r["lane"], r["family"], r["text"], r["ref"]), (None, "GROK_SUBSCRIPTION", "xai", "grok did it", "ses_1"))
        self.assertEqual(r["tokens"], {"in": 50, "cached": 40, "out": 5})
        self.assertTrue(r["transcript"].endswith("opencode-sessions/ses_1.json") and Path(r["transcript"]).is_file())
        args, env = self.args_seen().splitlines()[-1], Path(os.environ["FAKE_ENV"]).read_text()
        self.assertIn("run --pure --format json -m xai/grok-4.7 --variant medium", args)
        self.assertIn("OPENCODE_DISABLE_PROJECT_CONFIG=1", env)
        self.assertNotIn(f"XDG_CONFIG_HOME={self.tmp.name}\n", env)         # the user's ~/.config/opencode stays out
        perm = json.loads(next(l for l in env.splitlines() if l.startswith("OPENCODE_CONFIG_CONTENT="))[len("OPENCODE_CONFIG_CONTENT="):])["permission"]
        self.assertEqual((perm["webfetch"], perm["task"], perm["read"]["*.env"], "edit" in perm), ("deny", "deny", "deny", False))
        self.assertIn("Every command runs in a sandbox", Path(os.environ["FAKE_ARGS"] + ".brief").read_text())
        for who in ("openai", "xai"):                                       # Grok never reviews (delegate-qjw): with only Grok up the reviewer is Claude Sonnet
            _, r = self.run_it("--authored-by", who, role="reviewer")
            self.assertNotEqual(r["lane"], "GROK_SUBSCRIPTION", who)

    @unittest.skipIf(sys.platform != "darwin", "macOS only")
    def test_the_grok_shell_has_no_network_secrets_or_writes_outside_cwd(self):
        import subprocess as sp
        repo = Path(self.repo)
        (repo / ".env").write_text("FAKE=1")
        (repo / ".env.example").write_text("EX=1")
        sh = self.base / "sh"
        for write in (True, False):
            box.write_shell(self.repo, write, sh)
            run = lambda c: sp.run([str(sh), "-c", c], cwd=self.repo, capture_output=True, text=True)
            self.assertNotEqual(run("cat .env").returncode, 0)
            self.assertEqual(run("cat .env.example").stdout, "EX=1")
            self.assertEqual(run("touch made").returncode == 0, write)
            self.assertNotEqual(run("touch /Users/Shared/delegate-probe-x").returncode, 0)        # writable by everyone, outside the sandbox's list

    def test_opencode_shell_is_offline_for_uv_and_network_refusals_are_not_denials(self):
        sh = self.base / "sh-offline"
        box.write_shell(self.repo, True, sh)
        self.assertIn("export UV_OFFLINE=1\n", sh.read_text())
        self.assertTrue(box.network_refusal("error: tcp connect error\n  Operation not permitted (os error 1)"))
        self.assertFalse(box.network_refusal("cat: /Users/x/.ssh/id_rsa: Operation not permitted"))

    @unittest.skipIf(sys.platform != "darwin", "macOS only")
    def test_grok_limit_and_secret_answers(self):
        self.grok_ready()
        self.codex_out()
        _, r = self.run_it(mode="limit")
        self.assertEqual((why(r), r["lane"]), ("rate_limited", "GROK_SUBSCRIPTION"))
        self.assertEqual(time.strftime("%H:%M", time.localtime(env.load_state().out["GROK_SUBSCRIPTION"].until)), "05:50")
        env.update_state(lambda s: s.out.pop("GROK_SUBSCRIPTION"))
        _, r = self.run_it(mode="silent")
        self.assertEqual((why(r), r["stop"]["scope"]), ("worker_failed", "bead"))
        t0 = time.time()
        _, r = self.run_it(mode="secret")
        self.assertEqual((why(r), r["stop"]["permanence"]), ("secrets", "needs_owner"))
        self.assertLess(time.time() - t0, 20)

    @unittest.skipIf(sys.platform != "darwin", "macOS only")
    def test_grok_step_cost_is_passed_through_per_call(self):
        self.grok_ready()
        self.codex_out()
        _, r = self.run_it()
        u = r["usage"]
        self.assertEqual((r["lane"], r["cost"]), ("GROK_SUBSCRIPTION", 0.25))
        self.assertEqual((u["cost_usd"], u["cost_source"], u["accounting"]), (0.25, "harness", "estimated"))
        self.assertEqual({k: u["tokens"][k] for k in ("in_fresh", "cache_read", "cache_write", "out")}, {"in_fresh": 10, "cache_read": 40, "cache_write": 0, "out": 5})
        self.assertEqual((u["ttl_s"], u["warm_until"], u["ttl_source"], u["ref"]), (None, None, "unknown", "ses_1"))      # no known cache window

