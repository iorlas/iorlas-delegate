"""HealthLedger: the fold, the breaker, the canary and the 24 Oct / 22 Oct incident replays (golden/health_replay.json, taken from the
pre-D2 fold: every row's admit/refuse decision must be the same). Run: python -m unittest discover -s tests"""
import hermetic  # noqa: F401  (a home of its own for every test)
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from delegate.contracts import codec                                                        # noqa: E402
from delegate.contracts.history import HealthState, RefusalRow, ReleaseRow, RunRow          # noqa: E402
from delegate.contracts.routing import BreakerPolicy                                        # noqa: E402
from delegate.contracts.stores import CheckpointStore, HistoryStore                         # noqa: E402
from delegate.domain.health.ledger import STATES, HealthLedger, combine, failure_fields, stale_usage   # noqa: E402
from delegate.domain.health.matchers import fingerprint                                     # noqa: E402
from delegate.cli import verbs

POL = BreakerPolicy()
LEDGER = HealthLedger(POL)
GOLDEN = json.loads((Path(__file__).resolve().parent / "golden" / "health_replay.json").read_text(encoding="utf-8"))


def row(ts, lane="L", model="m", outcome=None, task=None, say="", until=None, **kw):
    """A history row as verbs.cmd_run writes it."""
    return RunRow(ts=ts, run_id=kw.pop("run_id", f"r{ts}"), role="worker", lane=lane, model=model, effort="high", done=outcome is None,
                  task=task or f"t{ts}", **failure_fields(outcome, "event", say, until), **kw)


def fold(rows, state=None, pol=POL):
    return HealthLedger(pol).fold(rows, state)


def view(state, lane, model, t, pol=POL):
    return HealthLedger(pol).view(state, lane, model, t)


def golden_row(r):
    return RunRow(ts=r["ts"], run_id=r["run_id"], role=r.get("role") or "worker", lane=r["lane"], model=r["model"], effort="high",
                  done=r.get("done", False), task=r.get("task") or "", outcome=r.get("outcome"), fingerprint=r.get("fingerprint"),
                  say=r.get("say"), until=r.get("until"))


class Replay(unittest.TestCase):
    """The incident histories are the regression fixtures for the thresholds: each row is a launch the engine made at row.ts; the fold
    admits or refuses it. The admit/refuse/canary/state of every row equals the golden taken before the fold moved."""

    def replay(self, name):
        g = GOLDEN[name]
        state, got = HealthState(), []
        for r in g["rows"]:
            v = view(state, g["lane"], g["model"], r["ts"])
            got.append([r["ts"], v.usable, v.canary, v.state])
            if v.usable:
                LEDGER.step(state, golden_row(r))
        self.assertEqual(got, g["decisions"])
        return state, g

    def test_24_oct_grok_spending_limit_stops_after_one_real_launch(self):
        state, g = self.replay("grok_spending_limit_24oct")
        self.assertEqual(len(g["rows"]), 114)
        admitted = sum(1 for d in g["decisions"] if d[1])
        self.assertLess(admitted, 10)
        self.assertEqual(view(state, g["lane"], g["model"], g["rows"][-1]["ts"] + 1).state, g["final"])
        self.assertEqual(g["final"], "exhausted")

    def test_24_oct_grok_server_errors_of_one_task_bench_nothing_and_network_is_never_counted(self):
        state, g = self.replay("grok_other_24oct")
        self.assertEqual([r["outcome"] for r in g["rows"]], ["vendor_blip"] * 3 + ["network"])
        self.assertTrue(all(d[1] for d in g["decisions"]))
        self.assertEqual(view(state, g["lane"], g["model"], g["rows"][-1]["ts"] + 1).state, "up")

    def test_22_oct_bad_request_never_benches_a_pair(self):
        state, g = self.replay("bad_request_22oct")
        self.assertEqual(len(g["rows"]), 53)
        self.assertTrue(all(d[1] for d in g["decisions"]))         # every one of the 53 was admitted
        self.assertFalse(state.pairs or state.lanes)

    def test_22_oct_night_whole_history_never_benches(self):
        g = GOLDEN["night_22oct"]
        state, got = HealthState(), []
        for r in sorted(g["rows"], key=lambda r: r["ts"]):
            got.append([r["ts"], view(state, r["lane"], r["model"], r["ts"]).usable])
            LEDGER.step(state, golden_row(r))
        self.assertEqual(got, g["decisions"])
        self.assertEqual({k: p.rung for k, p in state.pairs.items()}, g["pairs"])
        self.assertEqual(sorted(state.lanes), g["lanes"])
        self.assertFalse([k for k, p in state.pairs.items() if p.rung])


class Breaker(unittest.TestCase):
    def test_three_identical_failures_from_two_tasks_bench_the_pair_15_minutes(self):
        s = fold([row(1000 + 60 * i, outcome="vendor_blip", say="Server error.", task="a" if i < 2 else "b") for i in range(3)])
        v = view(s, "L", "m", 1121)
        self.assertEqual((v.state, v.usable, v.kind, v.source, v.rung), ("broken", False, "vendor_blip", "breaker", 1))
        self.assertEqual(v.until, 1120 + 15 * 60)
        self.assertEqual(v.evidence, "Server error.")
        self.assertEqual(len(v.last_failures), 3)

    def test_one_task_failing_three_times_benches_nothing(self):
        s = fold([row(1000 + 60 * i, outcome="vendor_blip", say="Server error.", task="same") for i in range(5)])
        self.assertTrue(view(s, "L", "m", 1400).usable)

    def test_different_fingerprints_do_not_add_up(self):
        s = fold([row(1000, outcome="vendor_blip", say="boom A", task="a"), row(1060, outcome="vendor_blip", say="other text", task="b"),
                  row(1120, outcome="vendor_blip", say="third thing", task="c")])
        self.assertTrue(view(s, "L", "m", 1130).usable)

    def test_numbers_ids_and_times_in_the_text_do_not_make_failures_different(self):
        a = fingerprint("vendor_blip", "event", "Request 4821 failed at 2026-10-24T02:31:53Z id 3f2a9c1b77")
        b = fingerprint("vendor_blip", "event", "Request 77 failed at 2026-10-24T09:00:00Z id aa00bb11cc")
        self.assertEqual(a, b)

    def test_failures_outside_the_window_do_not_count(self):
        s = fold([row(1000, outcome="vendor_blip", say="x", task="a"), row(1000 + 11 * 60, outcome="vendor_blip", say="x", task="b"),
                  row(1000 + 22 * 60, outcome="vendor_blip", say="x", task="c")])
        self.assertTrue(view(s, "L", "m", 1000 + 23 * 60).usable)

    def test_a_done_resets_the_streak_and_the_ladder(self):
        s = fold([row(1000, outcome="vendor_blip", say="x", task="a"), row(1010, outcome="vendor_blip", say="x", task="b"), row(1020, outcome=None),
                  row(1030, outcome="vendor_blip", say="x", task="c")])
        self.assertTrue(view(s, "L", "m", 1040).usable)

    def test_ladder_is_15_30_60_then_stays_at_60_and_a_passing_canary_resets_it(self):
        s = HealthState()
        for i in range(3):
            LEDGER.step(s, row(1000 + i, outcome="worker_failed", say="x", task=f"t{i}"))
        fail_at, minutes = 1002, []
        for _ in range(4):
            v = view(s, "L", "m", fail_at + 1)
            self.assertEqual((v.state, v.usable), ("broken", False))
            minutes.append(round((v.until - fail_at) / 60))
            fail_at = v.until + 1                                   # the bench ends: the next launch is the canary...
            self.assertEqual((view(s, "L", "m", fail_at).usable, view(s, "L", "m", fail_at).canary), (True, True))
            LEDGER.step(s, row(fail_at, outcome="worker_failed", say="x", task="canary"))       # ...and it fails: next rung at once
        self.assertEqual(minutes, [15, 30, 60, 60])
        t = view(s, "L", "m", fail_at + 1).until + 1
        LEDGER.step(s, row(t, outcome=None))                       # a canary that passes
        v = view(s, "L", "m", t + 1)
        self.assertEqual((v.state, v.rung, v.canary), ("up", 0, False))

    def test_hung_twice_in_30_minutes_benches_the_pair(self):
        s = fold([row(1000, outcome="hung"), row(1000 + 20 * 60, outcome="hung")])
        self.assertEqual(view(s, "L", "m", 1000 + 21 * 60).kind, "hung")
        s = fold([row(1000, outcome="hung"), row(1000 + 40 * 60, outcome="hung")])
        self.assertTrue(view(s, "L", "m", 1000 + 41 * 60).usable)

    def test_never_counted_reasons_leave_every_pair_up(self):
        never = ["bad_request", "network", "killed", "timeout", "secrets", "schema_invalid", "held", "busy", "caller", "lock", "setup", "crash"]
        s = fold([row(1000 + i, outcome=never[i % len(never)], say="same", task=f"t{i}") for i in range(60)])
        self.assertTrue(view(s, "L", "m", 1100).usable)
        self.assertFalse(s.pairs or s.lanes)

    def test_refusals_are_not_facts(self):
        s = fold([RefusalRow(ts=1000 + i, outcome="vendor_blip", role="worker", lane="L", model="m", say="x") for i in range(9)])
        self.assertTrue(view(s, "L", "m", 1100).usable)

    def test_pairs_are_independent(self):
        s = fold([row(1000 + i, model="a", outcome="worker_failed", say="x", task=f"t{i}") for i in range(3)])
        self.assertFalse(view(s, "L", "a", 1010).usable)
        self.assertTrue(view(s, "L", "b", 1010).usable)

    def test_n_and_window_are_config_values(self):
        pol = BreakerPolicy(n=2)
        s = fold([row(1000, outcome="worker_failed", say="x", task="a"), row(1001, outcome="worker_failed", say="x", task="b")], pol=pol)
        self.assertTrue(view(s, "L", "m", 1002, pol).usable)           # worker_failed also needs 3 distinct tasks
        s = fold([row(1000, outcome="vendor_blip", say="x", task="a"), row(1001, outcome="vendor_blip", say="x", task="b")], pol=pol)
        self.assertFalse(view(s, "L", "m", 1002, pol).usable)

    def test_breaker_applies_to_grok_deepseek_and_zai_pairs(self):
        for lane, model in (("GROK_SUBSCRIPTION", "grok-4.7"), ("DEEPSEEK_FREE", "deepseek-v4.1-flash-free"), ("ZAI_CODING_SUBSCRIPTION", "glm-5.3")):
            with self.subTest(lane=lane):
                s = fold([row(1000 + i, lane, model, "vendor_blip", say="Server error.", task=f"t{i}") for i in range(3)])
                self.assertFalse(view(s, lane, model, 1010).usable)


class LaneFacts(unittest.TestCase):
    def test_vendor_time_ends_a_limit_and_then_one_canary(self):
        s = fold([row(1000, outcome="rate_limited", say="limit", until=5000.0)])
        v = view(s, "L", "any-model", 2000)
        self.assertEqual((v.state, v.usable, v.source, v.until), ("exhausted", False, "vendor", 5000.0))
        v = view(s, "L", "any-model", 5001)
        self.assertEqual((v.state, v.usable, v.canary), ("up", True, True))

    def test_limit_without_a_time_is_a_guess_of_15_minutes(self):
        v = view(fold([row(1000, outcome="rate_limited", say="limit")]), "L", "m", 1001)
        self.assertEqual((v.source, v.until), ("guess", 1000 + 900))

    def test_credit_probe_after_6_hours_and_logged_out(self):
        s = fold([row(1000, lane="C", outcome="credit_exhausted", say="out of credits"), row(1000, lane="O", outcome="logged_out", say="not logged in")])
        self.assertEqual(view(s, "C", "m", 1000 + 6 * 3600 - 1).state, "exhausted")
        self.assertEqual(view(s, "O", "m", 1001).state, "logged_out")
        self.assertTrue(view(s, "C", "m", 1000 + 6 * 3600 + 1).usable)

    def test_a_done_on_the_lane_ends_its_limit_and_a_release_row_too(self):
        s = fold([row(1000, outcome="rate_limited", say="x", until=9e9), row(1100, model="other", outcome=None)])
        self.assertTrue(view(s, "L", "m", 1200).usable)
        s = fold([row(1000, outcome="rate_limited", say="x", until=9e9), ReleaseRow(ts=1100, lane="L")])
        self.assertTrue(view(s, "L", "m", 1200).usable)

    def test_a_lane_limit_and_a_bench_that_both_ended_still_make_the_next_launch_a_canary(self):
        s = fold([row(1000, outcome="rate_limited", say="x", until=1500.0)] + [row(1100 + i, outcome="worker_failed", say="y", task=f"t{i}") for i in range(3)])
        v = view(s, "L", "m", 1300)
        self.assertEqual((v.state, v.usable, v.canary), ("exhausted", False, False))
        s2 = fold([row(1000, outcome="rate_limited", say="x", until=1500.0), row(1400, outcome="worker_failed", say="y", task="a")])
        self.assertTrue(view(s2, "L", "m", 1600).canary)

    def test_combine_picks_the_most_severe_state(self):
        up = view(HealthState(), "L", "m", 1)
        self.assertEqual(combine(up), "up")
        self.assertEqual(combine(up, busy=True), "busy")
        self.assertEqual(combine(up, held=True, busy=True), "held")
        ex = view(fold([row(1, outcome="rate_limited", say="x", until=99.0)]), "L", "m", 2)
        self.assertEqual(combine(ex, busy=True), "exhausted")
        self.assertEqual(set(STATES), {"up", "busy", "exhausted", "logged_out", "held", "broken"})

    def test_usage_older_than_6_hours_is_unknown_not_a_gate(self):
        self.assertIsNone(stale_usage(1000, 1000 + 5 * 3600, POL))
        self.assertEqual(stale_usage(1000, 1000 + 7 * 3600, POL), 7.0)
        self.assertIsNone(stale_usage(None, 1e9, POL))


class Fold(unittest.TestCase):
    def rows(self):
        return [row(1000 + 100 * i, outcome="vendor_blip" if i % 7 < 3 else ("rate_limited" if i % 11 == 0 else None), say="Server error.", task=f"t{i % 4}",
                    until=3000.0 + 100 * i) for i in range(40)]

    def dump(self, s):
        return json.dumps(codec.to_json(s), sort_keys=True)

    def test_replay_is_deterministic_and_order_independent(self):
        rows = self.rows()
        self.assertEqual(self.dump(fold(rows)), self.dump(fold(list(reversed(rows)))))

    def test_checkpoint_plus_tail_equals_full_fold(self):
        rows = self.rows()
        for cut in (5, 17, 33):
            cp = codec.from_json(HealthState, json.loads(self.dump(fold(rows[:cut]))))          # a checkpoint went through a file
            self.assertEqual(self.dump(fold(rows, cp)), self.dump(fold(rows)))          # rows at or before the checkpoint are skipped

    def test_history_skips_corrupt_and_stale_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "history.jsonl"
            h = HistoryStore(p)
            h.add(row(1000, outcome="worker_failed", say="x"))
            with open(p, "a", encoding="utf-8") as f:
                f.write("{not json\n\n[1,2]\n" + json.dumps({"ts": 5, "run_id": "old", "done": True}) + "\n")      # the old shape has no kind: dropped
            h.add(row(1001, outcome="worker_failed", say="x"))
            self.assertEqual([r.ts for r in h.rows()], [1000, 1001])
            self.assertEqual(HistoryStore(Path(d) / "missing.jsonl").rows(), [])
            self.assertEqual([r.ts for r in h.rows(since=1000)], [1001])

    def test_checkpoint_file_is_only_a_cache(self):
        with tempfile.TemporaryDirectory() as d:
            store = CheckpointStore(Path(d) / "cp.json")
            s = fold(self.rows())
            store.save(s, POL)
            self.assertEqual(store.load(POL), s)
            self.assertIsNone(store.load(BreakerPolicy(n=9)))        # another policy: refold
            (Path(d) / "cp.json").write_text("{garbage", encoding="utf-8")
            self.assertIsNone(store.load(POL))

    def test_a_run_row_carries_v_fingerprint_say(self):
        f = failure_fields("vendor_blip", "event", "Server error. " + "x" * 400, None)
        self.assertEqual((f["outcome"], len(f["fingerprint"]), len(f["say"])), ("vendor_blip", 12, 240))
        self.assertEqual(failure_fields(None, "", ""), {})
        self.assertEqual(codec.to_json(row(1, outcome="hung", say="x"))["v"], 1)
        self.assertEqual(replace(POL, n=2).n, 2)


if __name__ == "__main__":
    unittest.main()
