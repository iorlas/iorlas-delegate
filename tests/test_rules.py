"""A1 (dg-rules): the architecture rules as one stdlib test (88-target-architecture sections 3 and 6).

What it enforces, over every .py under src/, skills/, dev/ and tests/ (src/<pkg>/_vendor/ is a leaf and is never walked):
  caps      400 SLOC per file, 80 per function, 800 per test file; SLOC = non-blank, non-comment, non-docstring lines by tokenize.
            Every cap violation that exists today is listed by file name in tests/rules_ratchet.json ("oversize" / "oversize_functions").
            A listed entry may be removed or lowered, never added. Run `python3 tests/test_rules.py --measure` to see the current table.
  layers    src/<pkg>/{contracts,domain,adapters,app,cli,hook} edges (LAYER_EDGES), app-slice edges (APP_EDGES), no cycle between slices,
            the dashboard package imports `<pkg>.contracts` only, the two plugins never import each other.
  placement `import re` only in adapters/ and domain/**/matchers.py; our file names only in contracts/paths.py;
            no import inside a function body; no mutable module global in domain/app/cli/hook/dash.
  vendor    dev/vendor.json matches the shelf checkout when one is present.
  pyrefly   the baseline's error count may only shrink (tests/rules_ratchet.json "pyrefly_baseline_errors").
"""
import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RATCHET = Path(__file__).with_name("rules_ratchet.json")
SCAN = ("src", "skills", "dev", "tests")
SKIP_DIRS = frozenset({".git", ".worktrees", ".slots", ".task", "__pycache__", "fixtures", "node_modules", "_vendor", ".venv"})
FILE_CAP, TEST_FILE_CAP, FUNC_CAP = 400, 800, 80

# -- per-repo constants (the only lines that differ between iorlas-task-loop and iorlas-delegate) ------------------------------------
MAIN_PKG = "delegate"
DASH_PKGS = frozenset()
FOREIGN_PKGS = frozenset({"taskloop", "taskloop_dash", "task_loop"})    # this plugin never imports these
FOREIGN_NAMES = ("task-loop", "task_loop", "taskloop")                  # ... nor names them in src/ text
PATH_LITERALS = ("state.json", "history.jsonl", "ledger.jsonl", "health-checkpoint.json", "net.json", "auth.json", "auth-cache.json",
                 "max-limits.json", "status-snapshot.json", "agent-runs/")

# -- section 3 tables ---------------------------------------------------------------------------------------------------------------
LAYER_EDGES = {
    "contracts": frozenset(),
    "domain": frozenset({"contracts"}),
    "adapters": frozenset({"contracts"}),
    "app": frozenset({"contracts", "domain", "adapters"}),
    "cli": frozenset({"app", "contracts"}),
    "hook": frozenset({"app", "contracts"}),
}
APP_EDGES = None   # delegate's app/ modules have no slice table; only a cycle between them fails
MUTABLE_LAYERS = frozenset({"domain", "app", "cli", "hook", "dash"})
MUTABLE_CALLS = frozenset({"list", "dict", "set", "defaultdict", "deque", "OrderedDict", "Counter", "bytearray"})


# -- measuring -----------------------------------------------------------------------------------------------------------------------
def py_files(root, scan=SCAN):
    for top in scan:
        base = Path(root) / top
        if base.is_dir():
            for path in sorted(base.rglob("*.py")):
                if not SKIP_DIRS & set(path.relative_to(root).parts):
                    yield path


def docstring_nodes(tree):
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.append(first)
    return found


def sloc_lines(text, tree):
    """The set of line numbers that hold code: not blank, not a comment, not part of a docstring."""
    skip = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER}
    lines = set()
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type not in skip:
            lines.update(range(tok.start[0], tok.end[0] + 1))
    for node in docstring_nodes(tree):
        lines -= set(range(node.lineno, node.end_lineno + 1))
    return lines


def walk_defs(node, prefix=""):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield prefix + child.name, child
            yield from walk_defs(child, prefix + child.name + ".")
        elif isinstance(child, ast.ClassDef):
            yield from walk_defs(child, prefix + child.name + ".")
        else:
            yield from walk_defs(child, prefix)


def measure(path):
    """(file SLOC, {qualified function name: SLOC}) for one .py file."""
    text = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text, str(path))
    lines = sloc_lines(text, tree)
    funcs = {}
    for qual, node in walk_defs(tree):
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        size = len(lines & set(range(start, node.end_lineno + 1)))
        funcs[qual] = max(size, funcs.get(qual, 0))
    return len(lines), funcs


def file_cap(rel):
    return TEST_FILE_CAP if rel.startswith("tests/") else FILE_CAP


def measure_tree(root):
    """{'files': {rel: sloc over its cap}, 'functions': {'rel::qual': sloc over FUNC_CAP}}: every violation in the tree."""
    files, funcs = {}, {}
    for path in py_files(root):
        rel = path.relative_to(root).as_posix()
        size, per_func = measure(path)
        if size > file_cap(rel):
            files[rel] = size
        funcs.update({"%s::%s" % (rel, q): n for q, n in per_func.items() if n > FUNC_CAP})
    return {"files": files, "functions": funcs}


def load_ratchet():
    return json.loads(RATCHET.read_text(encoding="utf-8"))


def ratchet_problems(actual, known, label, ceiling=lambda key: True):
    """A violation absent from `known` fails; `known` entries that are fixed (or gone) must be removed; a ceiling may not be exceeded.
    `ceiling(key)` False means membership only (test files: the loops keep adding tests to the big ones)."""
    out = []
    for key, size in sorted(actual.items()):
        if key not in known:
            out.append('%s over cap: %s = %d. Split it; do not add it to the table.' % (label, key, size))
        elif ceiling(key) and size > known[key]:
            out.append("%s grew: %s = %d > recorded %d" % (label, key, size, known[key]))
    for key in sorted(set(known) - set(actual)):
        out.append('%s now within its cap (or gone): delete "%s" from tests/rules_ratchet.json' % (label, key))
    return out


def cap_problems(root=ROOT, known=None):
    known = load_ratchet() if known is None else known
    actual = measure_tree(root)
    not_test = lambda key: not key.startswith("tests/")  # noqa: E731
    return (ratchet_problems(actual["files"], known["oversize"], "file", not_test)
            + ratchet_problems(actual["functions"], known["oversize_functions"], "function", not_test))


# -- import graph over src/ ------------------------------------------------------------------------------------------------------------
def src_files(root):
    base = Path(root) / "src"
    if not base.is_dir():
        return
    for path in sorted(base.rglob("*.py")):
        parts = path.relative_to(base).parts
        if "_vendor" not in parts and "__pycache__" not in parts:
            yield path, parts


def imports_of(parts, tree):
    """Absolute dotted targets (as tuples) of every import in a module at `parts` (relative to src/)."""
    package = parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield tuple(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - (node.level - 1)] if node.level > 1 else package
                module = base + tuple((node.module or "").split(".") if node.module else ())
            else:
                module = tuple((node.module or "").split("."))
            yield module
            for alias in node.names:
                yield module + (alias.name,)


def layer_of(parts):
    pkg = parts[0]
    if pkg in DASH_PKGS:
        return "dash"
    return parts[1] if len(parts) > 2 else None


def slice_of(parts):
    return Path(parts[2]).stem if len(parts) > 3 or (len(parts) == 3 and parts[1] == "app") else None


def edge_problems(rel, parts, targets):
    out, here = set(), layer_of(parts)
    for target in set(targets):
        top = target[0]
        if top in FOREIGN_PKGS:
            out.add("%s: imports %s (the two plugins never import each other)" % (rel, top))
        elif top not in {parts[0], MAIN_PKG} | DASH_PKGS:
            continue
        elif parts[0] in DASH_PKGS:
            if top == MAIN_PKG and target[1:2] not in (("contracts",), ()):
                out.add("%s: dash imports %s (only %s.contracts)" % (rel, ".".join(target[:2]), MAIN_PKG))
            elif top in DASH_PKGS and top != parts[0]:
                out.add("%s: dash imports another dash package" % rel)
        elif top in DASH_PKGS:
            out.add("%s: %s imports the dashboard %s" % (rel, here, top))
        elif len(target) > 1 and target[1] != "_vendor" and here in LAYER_EDGES and target[1] != here:
            if target[1] not in LAYER_EDGES:
                out.add("%s: imports unknown layer %s" % (rel, ".".join(target[:2])))
            elif target[1] not in LAYER_EDGES[here]:
                out.add("%s: %s -> %s is not an allowed layer edge" % (rel, here, target[1]))
    return sorted(out)


def slice_edges(parts, targets):
    """{(from slice, to slice)} for app modules importing other app slices."""
    if layer_of(parts) != "app" or slice_of(parts) is None:
        return set()
    found = set()
    for target in targets:
        if target[0] == parts[0] and len(target) > 2 and target[1] == "app" and Path(target[2]).stem != slice_of(parts):
            found.add((slice_of(parts), target[2]))
    return found


def cycle_in(edges):
    graph = {}
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
    state = {}

    def visit(n, path):
        if state.get(n) == 1:
            return path[path.index(n):] + [n]
        if state.get(n) == 2:
            return None
        state[n] = 1
        for m in sorted(graph.get(n, ())):
            found = visit(m, path + [n])
            if found:
                return found
        state[n] = 2
        return None
    for n in sorted(graph):
        found = visit(n, [])
        if found:
            return found
    return None


def layer_problems(root):
    out, all_slice_edges = [], set()
    for path, parts in src_files(root):
        rel = "src/" + "/".join(parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        targets = list(imports_of(parts, tree))
        out += edge_problems(rel, parts, targets)
        for a, b in slice_edges(parts, targets):
            all_slice_edges.add((a, b))
            allowed = None if APP_EDGES is None else APP_EDGES.get(a, frozenset())
            if allowed is not None and allowed != frozenset({"*"}) and b not in allowed:
                out.append("%s: app slice %s -> %s is not an allowed edge" % (rel, a, b))
    cycle = cycle_in(all_slice_edges)
    return out + (["app slices form a cycle: " + " -> ".join(cycle)] if cycle else [])


# -- placement rules over src/ ---------------------------------------------------------------------------------------------------------
def imports_re(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "re" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").split(".")[0] == "re":
            return True
    return False


def lazy_imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    yield inner.lineno


def is_final(annotation):
    return any(isinstance(n, ast.Name) and n.id == "Final" or isinstance(n, ast.Attribute) and n.attr == "Final" for n in ast.walk(annotation))


def mutable_value(value):
    if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    return isinstance(value, ast.Call) and getattr(value.func, "id", getattr(value.func, "attr", "")) in MUTABLE_CALLS


def module_statements(tree):
    for node in tree.body:
        if isinstance(node, (ast.If, ast.Try)):
            yield from node.body
        else:
            yield node


def mutable_globals(tree):
    declared = {n for node in ast.walk(tree) if isinstance(node, ast.Global) for n in node.names}
    found = []
    for node in module_statements(tree):
        if isinstance(node, ast.Assign):
            names, value, annotation = [t.id for t in node.targets if isinstance(t, ast.Name)], node.value, None
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            names, value, annotation = [node.target.id], node.value, node.annotation
        else:
            continue
        for name in names:
            if name.startswith("__") and name.endswith("__"):
                continue
            if annotation is not None and is_final(annotation):
                continue
            if mutable_value(value) or (isinstance(value, ast.Constant) and value.value is None and name in declared):
                found.append((name, node.lineno))
    return found


def file_literals(tree):
    skip = {id(n.value) for n in docstring_nodes(tree)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.value, node.lineno


def placement_problems(root):
    out = []
    for path, parts in src_files(root):
        rel, layer = "src/" + "/".join(parts), layer_of(parts)
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, str(path))
        if imports_re(tree) and not (layer == "adapters" or (layer == "domain" and parts[-1] == "matchers.py")):
            out.append("%s: import re is allowed only in adapters/ and domain/**/matchers.py" % rel)
        out += ["%s:%d: import inside a function body (a needed lazy import is a cycle)" % (rel, n) for n in lazy_imports(tree)]
        if layer in MUTABLE_LAYERS:
            out += ["%s:%d: mutable module global %s" % (rel, n, name) for name, n in mutable_globals(tree)]
        if parts[1:] != ("contracts", "paths.py"):
            out += ["%s:%d: file name %r belongs in contracts/paths.py" % (rel, n, lit) for lit, n in file_literals(tree)
                    if any(p in lit for p in PATH_LITERALS)]
        out += ["%s: names %r (the two plugins never name each other)" % (rel, n) for n in FOREIGN_NAMES if n in text]
    return out


def src_problems(root=ROOT):
    return layer_problems(root) + placement_problems(root)


# -- vendor drift --------------------------------------------------------------------------------------------------------------------
def shelf_home():
    for cand in (os.environ.get("SHELF_HOME"), str(Path.home() / "Workspaces" / "shelf")):
        if cand and (Path(cand) / ".git").exists():
            return Path(cand)
    return None


def git_out(repo, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True, check=True).stdout


def vendor_problems(root, shelf):
    """dev/vendor.json: {"packages": {name: {"tag": T}}}. Each vendored tree must equal the shelf's packages/<name>/src/<name> at T."""
    manifest = json.loads((Path(root) / "dev" / "vendor.json").read_text(encoding="utf-8"))
    out = []
    for name, spec in sorted(manifest.get("packages", {}).items()):
        src_dir = "packages/%s/src/%s" % (name, name)
        want = {p: git_out(shelf, "show", "%s:%s" % (spec["tag"], p)) for p in git_out(shelf, "ls-tree", "-r", "--name-only", spec["tag"], src_dir).decode().split()}
        for base in sorted((Path(root) / "src").glob("*/_vendor/" + name)):
            have = {src_dir + "/" + p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file() and p.suffix != ".pyc" and "__pycache__" not in p.parts}
            out += ["%s drifted from shelf %s: %s" % (base.relative_to(root), spec["tag"], p) for p in sorted(set(want) | set(have)) if want.get(p) != have.get(p)]
    return out


# -- tests: the real tree --------------------------------------------------------------------------------------------------------------
class RealTree(unittest.TestCase):
    def test_caps_hold_except_the_known_oversize_table(self):
        self.assertEqual(cap_problems(), [])

    def test_the_ratchet_file_names_only_existing_keys_in_sorted_form(self):
        known = load_ratchet()
        for table in ("oversize", "oversize_functions"):
            self.assertEqual(list(known[table]), sorted(known[table]), table)
            for key, size in known[table].items():
                self.assertTrue((ROOT / key.split("::")[0]).is_file(), key)
                self.assertGreater(size, FUNC_CAP if "::" in key else FILE_CAP, key)

    def test_src_obeys_layers_regex_lazy_imports_globals_and_file_names(self):
        self.assertEqual(src_problems(), [])

    def test_vendored_packages_match_the_shelf_checkout(self):
        if not (ROOT / "dev" / "vendor.json").is_file():
            self.skipTest("no dev/vendor.json yet")
        shelf = shelf_home()
        if shelf is None:
            self.skipTest("no shelf checkout ($SHELF_HOME or ~/Workspaces/shelf)")
        self.assertEqual(vendor_problems(ROOT, shelf), [])

    def test_the_pyrefly_baseline_error_count_may_only_shrink(self):
        path = ROOT / "pyrefly-baseline.json"
        recorded = load_ratchet()["pyrefly_baseline_errors"]
        data = json.loads(path.read_text(encoding="utf-8"))
        count = len(data["errors"])
        self.assertLessEqual(count, recorded, "the baseline grew: fix the new errors instead")
        self.assertEqual(count, recorded, "the baseline shrank: lower pyrefly_baseline_errors in tests/rules_ratchet.json to %d" % count)


# -- tests: planted trees --------------------------------------------------------------------------------------------------------------
class Planted(unittest.TestCase):
    def tree(self, files):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for rel, text in files.items():
            p = Path(d.name) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return Path(d.name)

    def problems(self, files):
        return src_problems(self.tree(files))

    def only(self, files, needle):
        found = self.problems(files)
        self.assertEqual(len(found), 1, found)
        self.assertIn(needle, found[0])

    def test_sloc_skips_blank_comment_and_docstring_lines(self):
        text = '"""doc\nmore"""\n\n# c\nx = 1\n\ndef f():\n    """d"""\n    return (1,\n            2)\n'
        self.assertEqual(sorted(sloc_lines(text, ast.parse(text))), [5, 7, 9, 10])

    def test_function_sloc_counts_nested_lines_and_decorators(self):
        root = self.tree({"skills/a.py": "import x\n\n@x.d\ndef f():\n    def g():\n        return 1\n    return g()\n"})
        size, funcs = measure(root / "skills" / "a.py")
        self.assertEqual((size, funcs["f"], funcs["f.g"]), (6, 5, 2))

    def test_a_file_over_its_cap_fails_unless_listed_and_a_fixed_one_must_leave_the_table(self):
        body = "x = 1\n" * (FILE_CAP + 1)
        root = self.tree({"skills/big.py": body, "tests/test_big.py": body})
        empty = {"oversize": {}, "oversize_functions": {}}
        self.assertEqual(len(cap_problems(root, empty)), 1)
        listed = {"oversize": {"skills/big.py": FILE_CAP + 1}, "oversize_functions": {}}
        self.assertEqual(cap_problems(root, listed), [])
        grown = {"oversize": {"skills/big.py": FILE_CAP}, "oversize_functions": {}}
        self.assertIn("grew", cap_problems(root, grown)[0])
        stale = {"oversize": {"skills/big.py": 500, "skills/gone.py": 500}, "oversize_functions": {}}
        self.assertIn("skills/gone.py", cap_problems(root, stale)[0])

    def test_a_test_file_may_be_over_400_up_to_800(self):
        root = self.tree({"tests/test_mid.py": "x = 1\n" * 700})
        self.assertEqual(cap_problems(root, {"oversize": {}, "oversize_functions": {}}), [])

    def test_a_function_over_80_fails_unless_listed(self):
        root = self.tree({"dev/f.py": "def f():\n" + "    x = 1\n" * 81})
        self.assertEqual(len(cap_problems(root, {"oversize": {}, "oversize_functions": {}})), 1)
        self.assertEqual(cap_problems(root, {"oversize": {}, "oversize_functions": {"dev/f.py::f": 82}}), [])

    def test_layer_edges(self):
        self.assertEqual(self.problems({"src/delegate/app/run/x.py": "from delegate.domain.plan import p\nfrom ...contracts import c\n"}), [])
        self.only({"src/delegate/domain/x.py": "from delegate.adapters import git\n"}, "domain -> adapters")
        self.only({"src/delegate/adapters/x.py": "import delegate.app\n"}, "adapters -> app")
        self.only({"src/delegate/cli/x.py": "from ..domain import y\n"}, "cli -> domain")
        self.only({"src/delegate/contracts/x.py": "from delegate import domain\n"}, "contracts -> domain")

    def test_vendor_is_a_leaf_any_layer_may_import(self):
        files = {"src/delegate/contracts/x.py": "from delegate._vendor.atomic_io import w\n", "src/delegate/_vendor/atomic_io/__init__.py": "import delegate.app\n"}
        self.assertEqual(self.problems(files), [])

    def test_plugins_never_import_each_other(self):
        found = self.problems({"src/delegate/app/x.py": "import taskloop.cli\n"})
        self.assertTrue(any("never import each other" in f for f in found), found)
        self.only({"src/delegate/app/y.py": "NAME = 'iorlas-task-loop'\n"}, "never name each other")

    def test_a_cycle_between_app_modules_fails(self):
        cyc = self.problems({"src/delegate/app/run.py": "from delegate.app import settle\n", "src/delegate/app/settle.py": "from delegate.app import run\n"})
        self.assertTrue(any("cycle" in c for c in cyc), cyc)
        self.assertEqual(self.problems({"src/delegate/app/run.py": "from delegate.app import settle\n", "src/delegate/app/settle.py": ""}), [])

    def test_regex_placement(self):
        self.assertEqual(self.problems({"src/delegate/adapters/a.py": "import re\n", "src/delegate/domain/plan/matchers.py": "from re import compile\n"}), [])
        self.only({"src/delegate/app/a.py": "import re\n"}, "import re")
        self.only({"src/delegate/domain/plan/rules.py": "import re as rx\n"}, "import re")
        self.only({"src/delegate/contracts/a.py": "from re import sub\n"}, "import re")

    def test_lazy_imports(self):
        self.only({"src/delegate/app/a.py": "def f():\n    import os\n    return os\n"}, "a.py:2: import inside a function")
        self.assertEqual(self.problems({"src/delegate/app/a.py": "import os\n\ndef f():\n    return os\n"}), [])

    def test_mutable_module_globals(self):
        self.only({"src/delegate/domain/a.py": "CACHE = {}\n"}, "mutable module global CACHE")
        self.only({"src/delegate/app/a.py": "from collections import defaultdict\nSEEN = defaultdict(list)\n"}, "SEEN")
        self.only({"src/delegate/cli/a.py": "_conn = None\n\ndef f():\n    global _conn\n    _conn = 1\n"}, "_conn")
        ok = "from typing import Final\n__all__ = ['A']\nA: Final = ('a',)\nB = (1, 2)\nC: Final[list] = []\nD = None\n"
        self.assertEqual(self.problems({"src/delegate/domain/a.py": ok}), [])
        self.assertEqual(self.problems({"src/delegate/adapters/a.py": "CACHE = {}\n"}), [])

    def test_file_names_only_in_contracts_paths(self):
        lit = PATH_LITERALS[0]
        self.assertEqual(self.problems({"src/delegate/contracts/paths.py": "A = %r\n" % lit}), [])
        self.only({"src/delegate/app/a.py": "p = root / %r\n" % lit}, "contracts/paths.py")
        self.assertEqual(self.problems({"src/delegate/app/a.py": '"""Writes %s."""\n' % lit}), [])


class LayerTable(unittest.TestCase):
    def test_the_layer_table_is_acyclic(self):
        self.assertIsNone(cycle_in({(a, b) for a, bs in LAYER_EDGES.items() for b in bs}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--measure"]:
        print(json.dumps(measure_tree(ROOT), indent=1, sort_keys=True))
    else:
        unittest.main()
