"""A real pre-0.45 config.json (tests/fixtures/config_pre_0_45.json, retired keys and all) must still drive `route.py run`. Run: python -m unittest discover -s tests"""
import hermetic  # noqa: F401  (a home of its own for every test)
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import run_support as T
from test_route import route
from delegate.app import env
from delegate.contracts.routing import RETIRED_SETTINGS, split_retired

FIXTURE = Path(__file__).parent / "fixtures" / "config_pre_0_45.json"
ROUTE_PY = Path(route.__file__)


@unittest.skipIf(os.name == "nt", "fake codex/claude are shell scripts")
class LegacyConfig(unittest.TestCase):
    FAKE_CODEX, FAKE_CLAUDE = T.RunBase.FAKE_CODEX, T.RunBase.FAKE_CLAUDE
    setUp, tearDown, fresh_usage = T.RunBase.setUp, T.RunBase.tearDown, T.RunBase.fresh_usage

    def put_config(self, extra=None):
        cfg = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cfg.update(extra or {})
        Path(self.tmp.name, "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def run_py(self, *args):
        os.environ["FAKE_MODE"] = "ok"
        p = subprocess.run([sys.executable, str(ROUTE_PY), *args], input="do the thing\nthe brief", env=os.environ, capture_output=True, text=True, timeout=60)
        return p

    def run_worker(self):
        return self.run_py("run", "worker", "--cwd", self.repo, "--unattended", "--timeout", "30")

    def test_run_loads_the_pre_0_45_config_and_warns_once_per_retired_key(self):
        self.put_config()
        p = self.run_worker()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIsNone(json.loads(p.stdout)["stop"])                  # stdout is still exactly the answer
        warned = [ln for ln in p.stderr.splitlines() if "is retired" in ln]
        self.assertEqual(len(warned), 3)
        for key in ("codex_weekly_used_override", "codex_resume_after_stall", "credit"):
            self.assertEqual(len([w for w in warned if f"'{key}'" in w]), 1)
        self.assertTrue(all("config.json" in w and "safe to delete" in w for w in warned))
        self.assertEqual(json.loads(Path(self.tmp.name, "config.json").read_text())["codex_weekly_used_override"], None)   # the owner's file is never touched

    def test_overrides_of_the_old_config_still_apply(self):
        self.put_config()
        cfg = env.load_routing()
        self.assertEqual((cfg["settings"]["max_parallel"], cfg["settings"]["strain"]["five_hour"]), (3, 85))
        self.assertNotIn("codex_weekly_used_override", cfg["settings"])

    def test_an_unknown_key_still_fails_and_names_file_key_and_fix(self):
        self.put_config({"codex_wekly_cap": 5})
        p = self.run_worker()
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")
        for want in ("codex_wekly_cap", str(Path(self.tmp.name, "config.json")), "delete the key"):
            self.assertIn(want, p.stderr)

    def test_doctor_reports_retired_keys_as_notes_not_errors(self):
        self.put_config()
        p = self.run_py("doctor", "--json")
        d = json.loads(p.stdout)
        notes = [n for n in d["notes"] if n["id"] == "retired_key"]
        self.assertEqual(sorted(n["key"] for n in notes), ["codex_resume_after_stall", "codex_weekly_used_override", "credit"])
        self.assertTrue(all(n["file"].endswith("config.json") for n in notes))

    def test_an_old_unversioned_state_file_is_reset_with_one_warning(self):
        self.put_config()
        Path(self.tmp.name, "state.json").write_text(json.dumps({"out": {"OPENAI_SUBSCRIPTION": {"until": 1, "why": "old"}}}), encoding="utf-8")
        p = self.run_worker()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIsNone(json.loads(p.stdout)["stop"])
        self.assertEqual(len([ln for ln in p.stderr.splitlines() if "state.json" in ln and "empty state" in ln]), 1)
        self.assertEqual(json.loads(Path(self.tmp.name, "state.json").read_text())["v"], 1)       # rewritten typed: the warning does not repeat

    def test_split_retired(self):
        kept, gone = split_retired({"max_parallel": 2, RETIRED_SETTINGS[0]: None})
        self.assertEqual((kept, gone), ({"max_parallel": 2}, [RETIRED_SETTINGS[0]]))


if __name__ == "__main__":
    unittest.main()
