"""An install from before 0.48 (CLAUDE_CREDIT lane, codex_login_pid) must upgrade with no hand edits. Run: python -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import hermetic  # noqa: F401  (a home of its own for every test)
import run_support as T
from test_route import route

from delegate.app import doctor, env
from delegate.contracts.stores import StateStore

FIX = Path(__file__).parent / "fixtures"
ROUTE_PY = Path(route.__file__)


class Upgrade(unittest.TestCase):
    FAKE_CODEX, FAKE_CLAUDE = T.RunBase.FAKE_CODEX, T.RunBase.FAKE_CLAUDE
    setUp, tearDown, fresh_usage = T.RunBase.setUp, T.RunBase.tearDown, T.RunBase.fresh_usage

    def load_state(self, name="state.json"):
        path = Path(self.tmp.name, name)
        shutil.copy(FIX / "state_pre_0_48.json", path)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            doc = StateStore(path).load()
        return doc, err.getvalue().splitlines(), path

    def test_state_drops_retired_keys_and_lane_entries_keeps_live_ones(self):
        before = (FIX / "state_pre_0_48.json").read_bytes()
        doc, warned, path = self.load_state()
        self.assertEqual(sorted(doc.out), ["OPENAI_SUBSCRIPTION"])
        self.assertEqual(doc.out["OPENAI_SUBSCRIPTION"].source, "vendor")
        self.assertEqual(list(doc.holds), ["h1"])
        self.assertEqual(doc.holds["h1"].reason, "keep for me")
        self.assertEqual(doc.leases["r-live"].pid, 4242)
        self.assertEqual(list(doc.auth), ["OPENAI_SUBSCRIPTION"])
        self.assertEqual(len(warned), 2, warned)
        self.assertTrue(any("'codex_login_pid'" in w for w in warned))
        self.assertTrue(any("'out.CLAUDE_CREDIT'" in w for w in warned))
        self.assertTrue(all(str(path) in w for w in warned))
        self.assertEqual(path.read_bytes(), before)            # a load alone rewrites nothing

    def test_state_retired_lane_in_holds_leases_and_refs_goes_too(self):
        path = Path(self.tmp.name, "state.json")
        d = json.loads((FIX / "state_pre_0_48.json").read_text())
        d["holds"]["h2"] = {**d["holds"]["h1"], "id": "h2", "lane": "CLAUDE_CREDIT"}
        d["leases"]["r-old"] = {**d["leases"]["r-live"], "lane": "CLAUDE_CREDIT"}
        d["refs"]["abc"] = {"lane": "CLAUDE_CREDIT", "model": "m", "effort": "e", "mode": "x"}
        path.write_text(json.dumps(d))
        with contextlib.redirect_stderr(io.StringIO()):
            doc = StateStore(path).load()
        self.assertEqual((list(doc.holds), list(doc.leases), list(doc.refs)), (["h1"], ["r-live"], []))

    def test_state_unknown_key_that_is_not_retired_still_resets(self):
        path = Path(self.tmp.name, "state.json")
        d = json.loads((FIX / "state_pre_0_48.json").read_text())
        d["mystery"] = 1
        path.write_text(json.dumps(d))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            doc = StateStore(path).load()
        self.assertEqual((doc.out, doc.holds), ({}, {}))
        self.assertIn("empty state", err.getvalue())

    def test_config_with_retired_keys_warns_naming_file_safe_to_delete_and_is_untouched(self):
        cfg = Path(self.tmp.name, "config.json")
        shutil.copy(FIX / "config_pre_0_45.json", cfg)
        before = cfg.read_bytes()
        err = io.StringIO()
        env._RETIRED_WARNED.clear()
        with contextlib.redirect_stderr(err):
            env.load_routing()
        w = [ln for ln in err.getvalue().splitlines() if "'credit'" in ln]
        self.assertEqual(len(w), 1)
        self.assertIn(str(cfg), w[0])
        self.assertIn("safe to delete", w[0])
        self.assertEqual(cfg.read_bytes(), before)
        self.assertEqual(env.profile_lanes()["CLAUDE_CREDIT"], "declined")      # a retired lane in the profile is just ignored

    def test_leftovers_are_listed_by_doctor_and_never_deleted(self):
        cfg = env.load_routing()
        d = Path(self.tmp.name)
        self.assertEqual([n for n in doctor.notes(cfg) if n["id"].startswith("unused")], [])
        (d / "claude-credit.key").write_text("sk-secret")
        (d / "ledger.jsonl").write_text("{}\n")
        os.environ["CLAUDE_CREDIT_WORKER_KEY"] = "sk-secret"
        self.addCleanup(os.environ.pop, "CLAUDE_CREDIT_WORKER_KEY", None)
        notes = [n for n in doctor.notes(cfg) if n["id"].startswith("unused")]
        self.assertEqual(sorted(n["id"] for n in notes), ["unused_env", "unused_file", "unused_file"])
        self.assertTrue(all("unused, safe to" in n["say"] for n in notes))
        self.assertTrue((d / "claude-credit.key").exists() and (d / "ledger.jsonl").exists())
        p = subprocess.run([sys.executable, str(ROUTE_PY), "doctor", "--json"], env=os.environ, capture_output=True, text=True, timeout=60)
        self.assertEqual(len([n for n in json.loads(p.stdout)["notes"] if n["id"].startswith("unused")]), 3)
        p = subprocess.run([sys.executable, str(ROUTE_PY), "doctor"], env=os.environ, capture_output=True, text=True, timeout=60)
        self.assertEqual(len([ln for ln in p.stdout.splitlines() if "unused, safe to" in ln]), 3)
        self.assertEqual((d / "claude-credit.key").read_text(), "sk-secret")

    @unittest.skipIf(os.name == "nt", "fake codex/claude are shell scripts")
    def test_run_ignores_leftovers_and_keeps_live_state(self):
        d = Path(self.tmp.name)
        shutil.copy(FIX / "state_pre_0_48.json", d / "state.json")
        (d / "claude-credit.key").write_text("sk-secret")
        (d / "ledger.jsonl").write_text("{}\n")
        os.environ.update(FAKE_MODE="ok", CLAUDE_CREDIT_WORKER_KEY="sk-secret")
        self.addCleanup(os.environ.pop, "CLAUDE_CREDIT_WORKER_KEY", None)
        p = subprocess.run([sys.executable, str(ROUTE_PY), "run", "worker", "--cwd", self.repo, "--unattended", "--timeout", "30"],
                           input="do the thing\nthe brief", env=os.environ, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("CLAUDE_CREDIT", p.stderr.replace("'out.CLAUDE_CREDIT'", ""))
        self.assertNotIn("sk-secret", p.stdout + p.stderr)
        st = json.loads((d / "state.json").read_text())
        self.assertNotIn("codex_login_pid", st)
        self.assertIn("h1", st["holds"])                         # the live lane's hold survived the run's own save
        self.assertTrue((d / "claude-credit.key").exists() and (d / "ledger.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
