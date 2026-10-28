"""route.py status --json --cheap prints exactly the bytes in tests/fixtures/status_cheap.golden.json for the fixture config dir (state, snapshot, hold, logins).
Runs the real launcher in a subprocess from a copy of the plugin tree, as the installed copy runs it."""
import hermetic  # noqa: F401  (a home of its own for every test)
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"


class StatusGolden(unittest.TestCase):
    def test_cheap_status_is_byte_equal_from_a_copy_of_the_tree(self):
        with tempfile.TemporaryDirectory() as d:
            tree, cfg = Path(d) / "plugin", Path(d) / "cfg"
            shutil.copytree(FIX / "status_cheap", cfg)
            os.chmod(cfg, 0o700)
            for sub in ("skills", "src"):
                shutil.copytree(ROOT / sub, tree / sub, ignore=shutil.ignore_patterns("__pycache__"))
            env = dict(os.environ, DELEGATE_CONFIG_DIR=str(cfg), DELEGATE_NOW="1800000000")
            r = subprocess.run([sys.executable, str(tree / "skills" / "delegate" / "route.py"), "status", "--json", "--cheap"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, (FIX / "status_cheap.golden.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
