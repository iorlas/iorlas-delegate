"""Mac bug (0.49.0): uv and mise failed under the opencode sandbox ("failed to create directory ... Operation not permitted"):
the cache dirs did not exist and cannot be created from inside the sandbox, and mise state went to ~/.local/state."""
import json
import re
import tempfile
import unittest
from pathlib import Path

import hermetic  # noqa: F401  (a home of its own for every test)

from delegate.adapters.harness.base import LaunchInputs
from delegate.adapters.harness.opencode import OpenCodeHarness, Profile

DIR_VARS = re.compile(r"^(UV_CACHE_DIR|XDG_\w+_HOME|MISE_\w*_DIR)$")


class CacheDirs(unittest.TestCase):
    def test_every_dir_env_var_exists_before_any_process_and_is_writable_in_the_profile_and_none_is_under_local(self):
        for profile in (Profile(), Profile(own_data=True)):
            with self.subTest(own_data=profile.own_data), tempfile.TemporaryDirectory() as d:
                root = Path(d) / ".claude" / "plugins" / "data" / "x"        # under a SECRETS folder, like the plugin's real config dir
                root.mkdir(parents=True)
                h = OpenCodeHarness(profile, root)
                spec = h.launch_spec(LaunchInputs(exe="/bin/oc", cwd="/w", model="m", effort="high", prompt="p", env={},
                                                  session="rid12345", write=True))
                dirs = {k: v for k, v in spec.env.items() if DIR_VARS.match(k)}
                for need in ("UV_CACHE_DIR", "MISE_CACHE_DIR", "MISE_STATE_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
                    self.assertIn(need, dirs)
                assert h.shell is not None
                script = h.shell.read_text()
                secrets_end = script.index(".env$")
                for k, v in dirs.items():
                    real = str(Path(v).resolve())
                    self.assertTrue(Path(v).is_dir(), k)
                    self.assertNotIn("/.local/", v + "/", k)
                    self.assertIn(f"(require-not (subpath {json.dumps(real)}))", script, k)
                    if k in ("UV_CACHE_DIR", "MISE_CACHE_DIR", "MISE_STATE_DIR", "XDG_STATE_HOME"):
                        allow = f'(allow file-read* file-write* (subpath "{real}"))'      # after the secrets rule that covers its parent
                        self.assertGreater(script.index(allow), secrets_end, k)
