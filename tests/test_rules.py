"""The size caps (88 section 4): a file has at most 400 lines of code (800 for a test file), a function at most 80. KNOWN_OVERSIZE names what is over
today and may only shrink: a file or function that is not in the table and is over fails, and an entry that is no longer over fails too."""
import hermetic  # noqa: F401  (a home of its own for every test)
import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dev"))
import vendor  # noqa: E402
import delegate  # noqa: E402,F401  (puts the vendored packages on sys.path)
EXEMPT_DIRS = ("skills/delegate/_vendor",)      # the one place the caps skip: the vendored shelf copy, byte-identical to its tag (dev/vendor.py)
FILE_CAP, TEST_FILE_CAP, FUNCTION_CAP = 400, 800, 80
KNOWN_OVERSIZE: dict[str, int] = {}          # "path" or "path:function" -> the size it may not grow past; delete the line when it is fixed


def code_files():
    for base in ("src", "skills", "dev"):
        yield from (p for p in sorted((ROOT / base).rglob("*.py")) if not str(p.relative_to(ROOT)).startswith(EXEMPT_DIRS))
    yield from sorted((ROOT / "tests").rglob("*.py"))


def sloc(path):
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#"))


def oversize():
    found = {}
    for p in code_files():
        rel = str(p.relative_to(ROOT))
        cap = TEST_FILE_CAP if rel.startswith("tests/") else FILE_CAP
        if sloc(p) > cap:
            found[rel] = sloc(p)
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.end_lineno - n.lineno + 1 > FUNCTION_CAP and not rel.startswith("tests/"):
                found[f"{rel}:{n.name}"] = n.end_lineno - n.lineno + 1
    return found


class Caps(unittest.TestCase):
    def test_nothing_is_over_a_cap_unless_it_is_known_and_has_not_grown(self):
        for key, size in oversize().items():
            self.assertIn(key, KNOWN_OVERSIZE, f"{key} is {size}: over its cap")
            self.assertLessEqual(size, KNOWN_OVERSIZE[key], f"{key} grew")

    def test_the_table_only_shrinks(self):
        found = oversize()
        for key in KNOWN_OVERSIZE:
            self.assertIn(key, found, f"{key} is under its cap now: delete its line")


class VendorGuard(unittest.TestCase):
    def test_vendor_is_unedited(self):
        self.assertEqual(vendor.problems(None), [], "vendored files edited; change the shelf and run dev/vendor.py sync")

    def test_manifest_lists_every_vendored_package(self):
        self.assertEqual([e["name"] for e in vendor.load()], sorted(p.name for p in vendor.VENDOR_DIR.iterdir() if p.is_dir() and p.name != "__pycache__"))

    def test_every_vendored_package_imports_by_its_absolute_name(self):
        import importlib
        for e in vendor.load():
            mod = importlib.import_module(e["name"])
            self.assertEqual(Path(mod.__file__).resolve().parent, (vendor.VENDOR_DIR / e["name"]).resolve())

    def test_an_edit_or_an_added_file_changes_the_hash(self):
        for e in vendor.load():
            files = vendor.dir_files(vendor.VENDOR_DIR / e["name"])
            self.assertNotEqual(vendor.tree_hash(dict(files, **{"extra.py": b""})), e["tree_hash"])
            first = sorted(files)[0]
            self.assertNotEqual(vendor.tree_hash(dict(files, **{first: files[first] + b"#"})), e["tree_hash"])

    def test_only_the_vendor_dir_is_exempt_and_it_is_the_one_on_the_path(self):
        self.assertEqual(EXEMPT_DIRS, ("skills/delegate/_vendor",))
        self.assertEqual(Path(delegate.VENDOR_DIR), ROOT / EXEMPT_DIRS[0])

    def test_vendor_drift(self):
        shelf = vendor.shelf_home()
        if not shelf:
            self.skipTest("no shelf checkout (SHELF_HOME)")
        r = subprocess.run([sys.executable, str(ROOT / "dev" / "vendor.py"), "check", "--shelf", str(shelf)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
