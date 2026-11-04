"""The real delegate launch uses and removes the overlay; workers are local fakes, never a login or model call."""
import json
import os
import sys
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401
from run_support import RunBase

from delegate.app import env, launch


class WiredOverlay(RunBase):
    def setUp(self):
        super().setUp()
        (env.config_dir() / "host.json").write_text(json.dumps({"binaries": {"python": sys.executable},
                                                              "writer_exec_allow": [["python", "-m", "unittest"]]}))
        self.flag = ["--writer-exec-allow", '[["python","-m","unittest"]]']

    def test_writer_receives_overlay_and_it_is_removed_after_the_call(self):
        code, result = self.run_it(*self.flag)
        self.assertEqual((code, result["stop"]), (0, None))
        environment = dict(line.split("=", 1) for line in Path(os.environ["FAKE_ENV"]).read_text().splitlines() if "=" in line)
        overlay = Path(environment["CODEX_HOME"])
        self.assertNotEqual(environment["CODEX_HOME"], os.environ["CODEX_HOME"])
        self.assertFalse(overlay.exists())
        self.assertNotIn("--ignore-rules", self.args_seen())
        self.assertIn("approval_policy=never", self.args_seen())
        self.assertIn("--exec-allow", Path(os.environ["FAKE_ARGS"] + ".brief").read_text())
        self.assertNotIn(launch.ESCALATE_RULE, Path(os.environ["FAKE_ARGS"] + ".brief").read_text())

    def test_empty_list_preserves_original_environment_flags_and_prompt(self):
        code, result = self.run_it("--writer-exec-allow", "[]")
        self.assertEqual((code, result["stop"]), (0, None))
        self.assertIn("--ignore-rules", self.args_seen())
        self.assertIn("approval_policy=on-request", self.args_seen())
        self.assertNotIn("--exec-allow", Path(os.environ["FAKE_ARGS"] + ".brief").read_text())
        self.assertIn(launch.ESCALATE_RULE, Path(os.environ["FAKE_ARGS"] + ".brief").read_text())
        self.assertFalse(list(env.config_dir().glob("codex-exec-*")))

    def test_cleanup_on_launch_failure(self):
        roots = []

        def fail(supervisor, spec, timeout):
            root = Path(spec.env["CODEX_HOME"])
            self.assertTrue((root / "rules/default.rules").is_file())
            roots.append(root)
            raise OSError("fake launch error")

        with mock.patch.object(launch.Supervisor, "run", fail):
            _, result = self.run_it(*self.flag)
        self.assertEqual(result["stop"]["reason"], "crash")
        self.assertTrue(roots)
        self.assertTrue(all(not root.exists() for root in roots))

    def test_read_step_and_non_codex_kind_refuse_the_grant(self):
        for role, flags in (("reviewer", []), ("worker", ["--only-family", "claude"])):
            with self.subTest(role=role, flags=flags):
                _, result = self.run_it(*self.flag, *flags, role=role)
                self.assertEqual(result["stop"]["reason"], "bad_request")
                self.assertFalse(list(env.config_dir().glob("codex-exec-*")))

    def test_resume_gets_its_own_overlay_and_rules(self):
        self.run_it()
        _, result = self.run_it(*self.flag, "--resume", "thread-123")
        self.assertIsNone(result["stop"])
        self.assertIn("exec resume", self.args_seen())
        self.assertIn("approval_policy=never", self.args_seen())
        self.assertFalse(list(env.config_dir().glob("codex-exec-*")))
