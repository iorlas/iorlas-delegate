"""The size caps (88 section 4): a file has at most 400 lines of code (800 for a test file), a function at most 80. KNOWN_OVERSIZE names what is over
today and may only shrink: a file or function that is not in the table and is over fails, and an entry that is no longer over fails too."""
import hermetic  # noqa: F401  (a home of its own for every test)
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE_CAP, TEST_FILE_CAP, FUNCTION_CAP = 400, 800, 80
KNOWN_OVERSIZE: dict[str, int] = {}          # "path" or "path:function" -> the size it may not grow past; delete the line when it is fixed


def code_files():
    for base in ("src", "skills", "dev"):
        yield from sorted((ROOT / base).rglob("*.py"))
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


if __name__ == "__main__":
    unittest.main()
