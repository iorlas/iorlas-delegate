"""Run the delegate suite sharded: `python dev/suite.py [-j N] [--contract]` (also `--list`, `--check`).

Unit tests (everything but tests/contract_procs/) are split over N processes by test class, heaviest first (weights below are measured seconds per
test of a module), each process with its own HOME and config dir (tests/hermetic.py makes one per test on top). `--contract` runs the marked contract
set instead: real processes and signals (tests/test_own_contract.py, tests/test_kill_tree.py), one process, serial, because they race the clock.
Prints one `Ran N tests in Ws` like unittest; exit 0 only when every shard passed.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
CONTRACT_MODULES = ("test_own_contract", "test_kill_tree")      # real processes and signals: never in a unit shard
WEIGHT = {"test_route.RunCommand": 4.0, "test_route.WatchdogProc": 3.0, "test_route": 1.0, "test_stream_supervisor": 6.0, "test_live_events": 1.0, "test_status_cheap": 3.0}
DEFAULT_WEIGHT = 0.5
BIG, CHUNK = 20, 8


def collect():
    """{unit: [test id]}: a class is a unit; one with more than BIG tests is cut in chunks."""
    sys.path.insert(0, str(TESTS))
    by_class = {}

    def walk(s):
        for t in s:
            walk(t) if isinstance(t, unittest.TestSuite) else by_class.setdefault("%s.%s" % (type(t).__module__, type(t).__name__), []).append(t.id())
    walk(unittest.defaultTestLoader.discover(str(TESTS), top_level_dir=str(TESTS)))
    units = {}
    for cls, ids in by_class.items():
        ids.sort()
        k = -(-len(ids) // CHUNK) if len(ids) > BIG else 1
        for j in range(k):
            units[cls if k == 1 else "%s#%d" % (cls, j)] = ids[j::k]
    return units


def split(count, units):
    shards, load = [[] for _ in range(count)], [0.0] * count
    cls = lambda u: u.split("#")[0]
    w = {u: len(ids) * WEIGHT.get(cls(u), WEIGHT.get(u.split(".")[0], DEFAULT_WEIGHT)) for u, ids in units.items()}
    for u in sorted(units, key=lambda u: (-w[u], u)):
        i = load.index(min(load))
        shards[i].extend(units[u])
        load[i] += w[u]
    return [s for s in shards if s]


def run_ids(ids):
    """Child mode: run these ids in this process."""
    sys.path.insert(0, str(TESTS))
    res = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromNames(ids))
    return 0 if res.wasSuccessful() else 1


def launch(ids, tmp, n):
    f = Path(tmp) / ("ids-%d.txt" % n)
    f.write_text("\n".join(ids), encoding="utf-8")
    env = dict(os.environ)
    env.pop("DELEGATE_CONFIG_DIR", None)
    p = subprocess.run([sys.executable, str(Path(__file__)), "--ids", str(f)], capture_output=True, text=True, env=env, cwd=str(ROOT))
    return p.returncode, p.stdout + p.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    ap.add_argument("--contract", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--ids")
    a = ap.parse_args()
    if a.ids:
        return run_ids(Path(a.ids).read_text(encoding="utf-8").split())
    units = collect()
    is_contract = lambda u: u.split(".")[0] in CONTRACT_MODULES
    chosen = {u: i for u, i in units.items() if is_contract(u) == a.contract}
    shards = split(1 if a.contract else a.j, chosen)
    if a.check:
        every = sorted(t for ids in units.values() for t in ids)
        placed = sorted(t for s in split(a.j, {u: i for u, i in units.items() if not is_contract(u)}) for t in s) + \
            sorted(t for u, i in units.items() if is_contract(u) for t in i)
        assert sorted(placed) == every and len(set(placed)) == len(placed), "a test is in no shard or in two"
        print("ok: %d tests, %d unit shards + %d contract" % (len(every), len(split(a.j, {u: i for u, i in units.items() if not is_contract(u)})),
                                                               sum(len(i) for u, i in units.items() if is_contract(u))))
        return 0
    if a.list:
        print("\n".join(t for s in shards for t in s))
        return 0
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(len(shards) or 1) as ex:
        outs = list(ex.map(lambda x: launch(x[1], tmp, x[0]), enumerate(shards)))
    bad = [o for rc, o in outs if rc]
    for rc, o in outs:
        if rc:
            print(o[-3000:])
    total = sum(len(s) for s in shards)
    print("Ran %d tests in %.1fs (%d shards)\n%s" % (total, time.monotonic() - t0, len(shards), "FAILED (%d shards)" % len(bad) if bad else "OK"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
