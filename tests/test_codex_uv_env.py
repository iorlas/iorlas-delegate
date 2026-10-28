"""delegate-e8z: a codex worker's uv never touches ~/.cache/uv (outside the sandbox) and stays offline while the sandbox has no network."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import hermetic  # noqa: F401  (a home of its own for every test)

from delegate.app import decide


class CodexUvEnv(unittest.TestCase):
    def test_a_writing_step_caches_inside_its_working_folder_and_uv_is_offline(self):
        e = decide.codex_env({"PATH": "/bin"}, "/w/repo", "workspace-write")
        self.assertEqual((e["UV_CACHE_DIR"], e["UV_OFFLINE"], e["PATH"]), ("/w/repo/.task/uv-cache", "1", "/bin"))
        self.assertTrue(Path(e["UV_CACHE_DIR"]).is_relative_to("/w/repo"))      # codex's workspace-write roots: the working folder
        self.assertIn("sandbox_workspace_write.writable_roots=[]", decide.CODEX_ACCESS)

    def test_a_reading_step_uses_no_cache_at_all(self):
        e = decide.codex_env({}, "/w/repo", "read-only")
        self.assertEqual((e.get("UV_NO_CACHE"), e.get("UV_OFFLINE"), "UV_CACHE_DIR" in e), ("1", "1", False))

    def test_offline_only_when_the_access_flags_turn_the_network_off(self):
        e = decide.codex_env({}, "/w", "workspace-write", ["-c", "sandbox_workspace_write.network_access=true"])
        self.assertNotIn("UV_OFFLINE", e)

    @unittest.skipUnless(shutil.which("uv"), "uv is not installed")
    def test_uv_run_succeeds_with_a_cold_cache_offline(self):
        with tempfile.TemporaryDirectory() as d:
            e = decide.codex_env({k: v for k, v in os.environ.items() if not k.startswith("UV_")}, d, "workspace-write")
            r = subprocess.run(["uv", "run", "python", "-c", "pass"], cwd=d, env=e, capture_output=True, text=True, timeout=120,
                               check=False)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((Path(d) / ".task" / "uv-cache").is_dir())


if __name__ == "__main__":
    unittest.main()
