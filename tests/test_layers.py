"""src/delegate layers (88 section 3), the cheap part: contracts import nothing of ours but contracts; adapters never import app; no `re` in
contracts or app; no import inside a function; no module past 400 lines of code. No rules gate exists in this repo yet."""
import hermetic  # noqa: F401  (a home of its own for every test)
import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "delegate"
FORBIDDEN = {"contracts": ("adapters", "app", "domain", "cli"), "adapters": ("app", "domain", "cli"), "domain": ("adapters", "app", "cli"), "app": ("cli",)}
NO_RE = ("contracts", "app", "domain")      # domain: only in a matchers.py
NO_WORLD = {"os", "subprocess", "socket", "time", "random", "shutil", "pathlib", "sys", "uuid"}      # domain computes; a clock is passed in
MAX_SLOC = 400


def modules():
    for p in sorted(SRC.rglob("*.py")):
        yield p, p.relative_to(SRC).parts[0], ast.parse(p.read_text())


def imported(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            yield from (a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            yield n.module


class Layers(unittest.TestCase):
    def test_a_layer_imports_only_what_the_layers_table_allows(self):
        for p, layer, tree in modules():
            for name in imported(tree):
                parts = name.split(".")
                if parts[0] == "delegate" and len(parts) > 1:
                    self.assertNotIn(parts[1], FORBIDDEN.get(layer, ()), f"{p.name} ({layer}) imports {name}")

    def test_no_re_in_contracts_or_app(self):
        for p, layer, tree in modules():
            if layer in NO_RE and p.name != "matchers.py":
                self.assertNotIn("re", set(imported(tree)), p.name)

    def test_re_in_the_domain_only_in_matchers(self):
        for p, layer, tree in modules():
            if layer == "domain" and p.name != "matchers.py":
                self.assertNotIn("re", set(imported(tree)), p.name)

    def test_domain_touches_no_world(self):
        for p, layer, tree in modules():
            if layer == "domain":
                self.assertEqual({n.split(".")[0] for n in imported(tree)} & NO_WORLD, set(), p.name)
                calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
                self.assertNotIn("open", calls, p.name)

    def test_no_import_inside_a_function(self):
        for p, _, tree in modules():
            for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
                self.assertEqual([n for n in ast.walk(fn) if isinstance(n, (ast.Import, ast.ImportFrom))], [], f"{p.name}:{fn.name}")

    def test_a_file_stays_under_the_cap(self):
        for p, _, _ in modules():
            sloc = sum(1 for line in p.read_text().splitlines() if line.strip() and not line.strip().startswith("#"))
            self.assertLessEqual(sloc, MAX_SLOC, p.name)


if __name__ == "__main__":
    unittest.main()
