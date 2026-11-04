"""Real processes sharing the router's files; only readiness and usage are synthetic."""
import concurrent.futures
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import hermetic  # noqa: F401
from stream_proc import stop_tree

from delegate.contracts.state import Dwell
from delegate.contracts.stores import StateStore

T = 1_800_000_000
LANES = ("OPENAI_SUBSCRIPTION", "GROK_SUBSCRIPTION", "DEEPSEEK_FREE", "ZAI_CODING_SUBSCRIPTION")
N = len(LANES)
ROUNDS = 20

# A fresh interpreter calls the real launcher/CLI and the real file writers. Pipes
# rendezvous before contention, and (for next) after routing but before its write.
CHILD = r'''
import contextlib, datetime, io, json, runpy, sys, time, uuid
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "src"))
from delegate.app import env, onboard, usage
from delegate.adapters import agent_runs

mode, lane, rounds = sys.argv[1], sys.argv[2], int(sys.argv[3])
main = runpy.run_path("skills/delegate/route.py")["main"]
def call(args):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert main(args) == 0
    return out.getvalue()
def gate():
    print("ready", file=sys.__stdout__, flush=True)
    assert sys.stdin.readline() == "go\n"
if mode == "route":
    load = env.load_routing
    def routing():
        cfg = load()
        cfg["lanes"]["CLAUDE_SECONDARY"] = dict(cfg["lanes"]["CLAUDE_SUBSCRIPTION"])
        return cfg
    env.load_routing = routing
    onboard.configured_lanes = lambda cfg: dict.fromkeys(cfg["lanes"], True)
    usage.READERS.update({k: lambda *a: None for k in usage.READERS})
    update = env.update_state
    waiting = True
    def paused(fn):
        global waiting
        if fn.__name__ == "mark" and waiting:
            waiting = False
            gate()
        return update(fn)
    env.update_state = paused
gate()
ids = []
for i in range(rounds):
    if mode == "route":
        until = datetime.datetime.fromtimestamp(env.now() + 3600, datetime.timezone.utc).isoformat()
        call(["report", "--lane", lane, "--event", "out", "--until", until])
        d = json.loads(call(["next", "worker"]))
        assert (d["action"], d["lane"]) == ("run", "CLAUDE_SUBSCRIPTION"), d
    elif mode == "config":
        call(["config", "set", "profile.lanes." + lane, "enabled"])
        def increment(cur):
            n = cur["min_dwell_minutes"]
            time.sleep(0.003)  # widen the read/write window while holding the existing lock
            cur["min_dwell_minutes"] = n + 1
        env.update_config(increment)
    else:
        rid = str(uuid.uuid4())
        ids.append(rid)
        agent_runs.run(rid, kind="claude-p", lane=lane, model="test", cwd=str(Path.cwd()),
                       parent_session=lane, started=i, status="running")
        agent_runs.lanes([(lane, i + 1, lane, [("5h", i, i + 1)])], i)
        agent_runs.run(rid, status="completed", ended=i + 1, exit=0)
print(json.dumps(ids), flush=True)
'''


class Contention(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.cfg = self.base / "cfg"
        self.cfg.mkdir()
        self.config = self.cfg / "config.json"
        self.config.write_text(json.dumps({"min_dwell_minutes": 30, "profile": {"lanes": dict.fromkeys(LANES, "declined")}}))
        self.children: list[subprocess.Popen[bytes]] = []
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=N)
        self.addCleanup(self.pool.shutdown, wait=True)
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for p in self.children:
            if p.poll() is None:
                stop_tree(p, grace=0.1)
            p.wait(timeout=10)
            for pipe in (p.stdin, p.stdout, p.stderr):
                if pipe is not None:
                    pipe.close()

    def start(self, mode):
        child_env = dict(os.environ, HOME=str(self.base), USERPROFILE=str(self.base), PATH="",
                         XDG_CONFIG_HOME=str(self.base / "xdg"), XDG_DATA_HOME=str(self.base / "data"),
                         CODEX_HOME=str(self.base / "codex"), DELEGATE_CONFIG_DIR=str(self.cfg), DELEGATE_NOW=str(T))
        for lane in LANES:
            p = subprocess.Popen([sys.executable, "-c", CHILD, mode, lane, str(ROUNDS)], env=child_env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
            self.children.append(p)
        self.ready()

    def ready(self):
        reads = []
        for p in self.children:
            assert p.stdout is not None
            reads.append(self.pool.submit(p.stdout.readline))
        for p, read in zip(self.children, reads, strict=True):
            line = read.result(timeout=20)
            assert p.stderr is not None
            self.assertEqual(line, b"ready\n", line if line else p.stderr.read().decode())

    def release(self):
        for p in self.children:
            assert p.stdin is not None
            p.stdin.write(b"go\n")
            p.stdin.flush()

    def finish(self):
        results = []
        for p in self.children:
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err)
            self.assertEqual(err, b"")
            results.extend(json.loads(out))
        return results

    @contextlib.contextmanager
    def observing(self, paths):
        """Read without a lock, as dashboards do; every observed replacement must be whole JSON."""
        done = threading.Event()
        errors, seen = [], set()

        def observe():
            while not done.is_set():
                for path in paths():
                    try:
                        json.loads(path.read_text())
                        seen.add(path)
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        errors.append((str(path), str(e)))
                done.wait(0.001)

        reader = threading.Thread(target=observe)
        reader.start()
        try:
            yield seen
        finally:
            done.set()
            reader.join(timeout=5)
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])

    def test_parallel_report_and_next_preserve_out_marks_and_fresh_dwell(self):
        state = self.cfg / "state.json"
        with self.observing(lambda: [state]) as seen:
            self.start("route")
            self.release()
            self.ready()  # all four routed using a snapshot without prev.claude
            store = StateStore(state)
            store.update(lambda s: s.prev.__setitem__("claude", Dwell(lane="CLAUDE_SUBSCRIPTION", since=T - 1)))
            self.release()
            self.finish()
        self.assertIn(state, seen)
        doc = json.loads(state.read_text())
        self.assertEqual(set(doc["out"]), set(LANES))
        for mark in doc["out"].values():
            self.assertEqual((mark["until"], mark["at"], mark["source"]), (T + 3600, T, "manual"))
        self.assertEqual(doc["prev"], {"claude": {"lane": "CLAUDE_SUBSCRIPTION", "since": T - 1}})

    def test_parallel_config_cli_and_update_config_lose_no_updates(self):
        with self.observing(lambda: [self.config]) as seen:
            self.start("config")
            self.release()
            self.finish()
        self.assertIn(self.config, seen)
        doc = json.loads(self.config.read_text())
        self.assertEqual(doc["min_dwell_minutes"], 30 + N * ROUNDS)
        self.assertEqual(doc["profile"]["lanes"], dict.fromkeys(LANES, "enabled"))

    def test_parallel_agent_runs_keep_unique_runs_and_whole_lane_snapshots(self):
        root = self.base / "xdg" / "agent-runs"
        lane_path = root / "lanes" / "delegate.json"
        with self.observing(lambda: [lane_path, *root.glob("runs/*.json")]) as seen:
            self.start("records")
            self.release()
            ids = self.finish()
        self.assertIn(lane_path, seen)
        self.assertTrue(any(p.parent.name == "runs" for p in seen))
        self.assertEqual(len(ids), N * ROUNDS)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual({p.stem for p in root.glob("runs/*.json")}, set(ids))
        for path in root.glob("runs/*.json"):
            rec = json.loads(path.read_text())
            self.assertEqual((rec["v"], rec["producer"], rec["id"], rec["status"], rec["exit"]),
                             (1, "delegate", path.stem, "completed", 0))
            self.assertEqual(rec["lane"], rec["parent_session"])
            self.assertEqual(rec["ended"], rec["started"] + 1)
        rec = json.loads(lane_path.read_text())
        self.assertEqual((rec["v"], rec["producer"]), (1, "delegate"))
        self.assertEqual(len(rec["lanes"]), 1)
        lane = rec["lanes"][0]
        self.assertIn(lane["name"], LANES)
        self.assertEqual(lane, {"name": lane["name"], "out_until": rec["at"] + 1, "why": lane["name"],
                                "usage": [{"window": "5h", "used_pct": rec["at"], "resets_at": rec["at"] + 1}]})
