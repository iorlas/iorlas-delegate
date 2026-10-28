"""Every test runs in its own home: HOME, XDG, CODEX_HOME and DELEGATE_CONFIG_DIR point into a fresh temp dir, and opening a file under the
real user's agent or config folders (~/.config, ~/.codex, ~/.claude, ~/.opencode, ~/.local/share) fails the test (an audit hook).
Imported first by every test module: `import hermetic  # noqa`. A test that needs the real thing is not a unit test.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))      # delegate.*: next to skills/ in the plugin tree

REAL_HOME = os.path.realpath(os.path.expanduser("~"))
FORBIDDEN = tuple(os.path.join(REAL_HOME, d) + os.sep for d in (".config", ".codex", ".claude", ".opencode", os.path.join(".local", "share")))
FORBIDDEN_EXACT = tuple(os.path.join(REAL_HOME, d) for d in (".config", ".codex", ".claude", ".opencode"))
ROOT = tempfile.mkdtemp(prefix="delegate-test-")
_ENV = ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "CODEX_HOME", "DELEGATE_CONFIG_DIR", "TMPDIR")


def _audit(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        p = os.fsdecode(args[0])
        if p.startswith(("/", REAL_HOME)) and (p.startswith(FORBIDDEN) or p in FORBIDDEN_EXACT):
            raise PermissionError("hermetic: a test opened the real %s" % p)


def home(base):
    """Point the process environment at a fresh home under `base`; returns the home path."""
    h = Path(base)
    for sub in (".config", ".local/share", "cfg", "tmp"):
        (h / sub).mkdir(parents=True, exist_ok=True)
    real = Path(REAL_HOME) / ".gitconfig"
    if real.is_file() and not (h / ".gitconfig").exists():      # tests commit with the identity it holds; HOME no longer finds it
        shutil.copyfile(real, h / ".gitconfig")
    for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):      # unset: every path derives from HOME, which tests re-point
        os.environ.pop(k, None)
    os.environ.update(HOME=str(h), USERPROFILE=str(h), CODEX_HOME=str(h / ".codex"), DELEGATE_CONFIG_DIR=str(h / "cfg"), TMPDIR=str(h / "tmp"))
    return h


if not getattr(sys, "_delegate_hermetic", False):
    sys._delegate_hermetic = True
    sys.addaudithook(_audit)
    from delegate.adapters.harness import opencode_sandbox
    opencode_sandbox._probed[:] = [None]        # no test starts a real sandbox-exec; tests of the preflight clear it
    home(ROOT)
    _run = unittest.TestCase.run

    def run(self, result=None):
        saved = {k: os.environ.get(k) for k in _ENV}
        d = tempfile.mkdtemp(prefix="t-", dir=ROOT)
        home(d)
        try:
            return _run(self, result)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            shutil.rmtree(d, ignore_errors=True)
    unittest.TestCase.run = run
