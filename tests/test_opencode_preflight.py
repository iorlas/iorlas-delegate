"""delegate-avz: opencode sandbox covers the launcher XDG dirs; a nested sandbox is caught before any token; no sandbox-exec, no lane."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401  (a home of its own for every test)
from test_route import CFG

from delegate.adapters.harness import opencode_sandbox as box
from delegate.adapters.harness.base import LaunchInputs
from delegate.adapters.harness.opencode import OpenCodeHarness, Profile
from delegate.app import cockpit, env, launch, onboard


class XdgWritable(unittest.TestCase):
    def test_every_xdg_var_the_launcher_sets_is_writable_in_the_profile(self):
        for profile in (Profile(), Profile(own_data=True)):
            with self.subTest(own_data=profile.own_data), tempfile.TemporaryDirectory() as d:
                h = OpenCodeHarness(profile, Path(d))
                spec = h.launch_spec(LaunchInputs(exe="/bin/oc", cwd="/w", model="m", effort="high", prompt="p", env={}, session="rid12345",
                                                  write=True))
                xdg = {k: v for k, v in spec.env.items() if k.startswith("XDG_")}
                self.assertTrue(xdg)
                self.assertEqual(len(xdg), 4 if profile.own_data else 2)
                assert h.shell is not None
                script = h.shell.read_text()
                for k, v in xdg.items():
                    self.assertIn(f"(require-not (subpath {json.dumps(str(Path(v).resolve()))}))", script, k)
                    self.assertTrue(Path(v).is_dir(), k)        # commands cannot create it: its parent is not writable


class Preflight(unittest.TestCase):
    def setUp(self):
        saved = list(box._probed)
        box._probed.clear()
        self.addCleanup(lambda: (box._probed.clear(), box._probed.extend(saved)))

    def test_exit_71_names_the_nested_sandbox_once_per_process(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 71, "", "sandbox_apply: Operation not permitted"))
        self.assertIn("exited 71", box.preflight(run) or "")
        self.assertIn("exited 71", box.preflight(run) or "")
        run.assert_called_once()

    def test_a_working_sandbox_passes_and_a_missing_one_fails(self):
        self.assertIsNone(box.preflight(mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))))
        box._probed.clear()
        self.assertIn("could not run", box.preflight(mock.Mock(side_effect=FileNotFoundError("sandbox-exec"))) or "")

    def test_a_nested_sandbox_stops_the_lane_before_any_launch_and_marks_it_out(self):
        marks = []
        settler = mock.Mock(mark_out=lambda *a: marks.append(a))
        d = {"lane": "DEEPSEEK_FREE", "model": "m", "effort": "high", "how": {"cwd": "/w", "model": "m", "write": True}}
        with mock.patch.object(OpenCodeHarness, "find_exe", return_value="/bin/oc"), \
                mock.patch.object(OpenCodeHarness, "supported", return_value=True), \
                mock.patch.object(box, "preflight", return_value="sandbox-exec exited 71: x"), \
                mock.patch.object(launch, "settler", return_value=settler), mock.patch.object(OpenCodeHarness, "launch_spec") as spec:
            res = launch.prepare_opencode(d, "brief", mock.Mock(schema=None, resume=None), "rid", 1000.0, CFG)
        assert isinstance(res, dict)
        self.assertEqual(res["outcome"], "setup")
        self.assertIn("nested_sandbox", str(res["say"]))
        spec.assert_not_called()                                  # no worker, so no model tokens
        self.assertEqual((marks[0][0], marks[0][1]), ("DEEPSEEK_FREE", 4600.0))


class NoSandbox(unittest.TestCase):
    def test_off_darwin_the_opencode_lanes_are_unavailable_and_others_are_not(self):
        cfg = {"settings": {"deepseek_free_enabled": True, "zai_coding_enabled": True}}
        with mock.patch.object(OpenCodeHarness, "supported", return_value=False), mock.patch.object(env, "profile_lanes", return_value={}):
            for lane in ("GROK_SUBSCRIPTION", "DEEPSEEK_FREE", "ZAI_CODING_SUBSCRIPTION"):
                self.assertEqual(cockpit.off_reason(lane, cfg, (None, "", None)), cockpit.NO_SANDBOX)
            self.assertIn("not configured", cockpit.off_reason("OPENAI_SUBSCRIPTION", cfg, (None, "", None)))
            self.assertIn("no sandbox", onboard.fix_hint("GROK_SUBSCRIPTION"))
            self.assertIn("deepseek_free_enabled", onboard.fix_hint("DEEPSEEK_FREE"))
            self.assertIn("codex", onboard.fix_hint("OPENAI_SUBSCRIPTION"))


if __name__ == "__main__":
    unittest.main()
