"""config.json `profile` (the owner's lane choices) never leaks into the routing settings."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401  (a home of its own for every test)

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "skills" / "delegate"))

from delegate.app import env
from delegate.contracts.stores import ConfigStore


class ProfileStaysOut(unittest.TestCase):
    def write(self, d, doc):
        (Path(d) / "config.json").write_text(json.dumps(doc), encoding="utf-8")

    def test_profile_is_not_an_override_and_not_a_setting(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"DELEGATE_CONFIG_DIR": d}):
            self.write(d, {"profile": {"lanes": {"X": "enabled"}, "ack": ["volumes"]}, "max_parallel": 3})
            store = ConfigStore(Path(d) / "config.json")
            self.assertNotIn("profile", store.overrides())
            self.assertEqual(store.overrides().get("max_parallel"), 3)
            cfg = env.load_routing()
            self.assertNotIn("profile", cfg["settings"])
            self.assertFalse(hasattr(env.typed_routing(cfg).settings, "profile"))
            self.assertEqual(store.profile().lanes, {"X": "enabled"})

    def test_choosing_a_lane_keeps_the_settings_overrides_and_writes_only_the_profile(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, {"max_parallel": 3})
            store = ConfigStore(Path(d) / "config.json")
            store.set_lane("DEEPSEEK_FREE", "declined")
            doc = json.loads((Path(d) / "config.json").read_text())
            self.assertEqual((doc["max_parallel"], doc["profile"]["lanes"]), (3, {"DEEPSEEK_FREE": "declined"}))
            self.assertNotIn("DEEPSEEK_FREE", json.dumps(store.overrides()))


if __name__ == "__main__":
    unittest.main()
