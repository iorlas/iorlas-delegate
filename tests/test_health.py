"""Lane health fold, breaker, canary and idle watchdog (R15: delegate-rdt, -a9x, -go0). Run: python -m unittest discover -s tests"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "delegate"))
from lanes import classify, health, supervisor      # noqa: E402

FIX = ROOT / "tests" / "contract" / "fixtures"
POL = health.policy()


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def row(ts, lane="L", model="m", outcome=None, task=None, say="", until=None, status=None, **kw):
    """A history row as route.run_once writes it."""
    r = {"ts": ts, "run_id": kw.pop("run_id", f"r{ts}"), "task": task or f"t{ts}", "lane": lane, "model": model,
         "status": status or ("done" if outcome is None else "failed"), "outcome": outcome, "say": say, "until": until}
    r.update(health.row_fields(outcome, "event", say, until) if outcome else {})
    r.update(kw)
    return r


def replay(rows, lane, model, pol=POL):
    """Offline replay of an incident: each row is a launch the engine made at row.ts. Ask the fold before it; a row the fold refuses was never
    launched. -> (admitted rows, canaries among them, the final state). Same rule as dev/replay_health.py in the design."""
    state, admitted, canaries = health.new_state(), 0, 0
    for r in rows:
        v = health.view(state, lane, model, r["ts"], pol)
        if not v["usable"]:
            continue
        admitted += 1
        canaries += v["canary"]
        health.step(state, r, pol)
    return admitted, canaries, state


def fixture_rows(name, lane, model, task_of=None):
    """Fixture rows -> history rows, the outcome from the ladder over the recorded evidence (lanes/classify.py)."""
    out = []
    for fr in sorted(fixture(name)["rows"], key=lambda r: r["history"]["ts"]):
        o = classify.classify(**fr["evidence"]) if "evidence" in fr else None
        h = fr["history"]
        t = h["ts"]
        out.append(row(t, lane, model, o.reason if o else None, task=(task_of(fr) if task_of else h.get("task")), say=o.detail if o else "", run_id=h.get("run_id") or fr["id"]))
    return out


def task_from_source(fr):
    return fr["id"].rsplit(":", 1)[0]


class Replay(unittest.TestCase):
    """The two incident histories are the regression fixtures for the thresholds."""

    def test_24_oct_grok_spending_limit_stops_after_one_real_launch(self):
        rows = fixture_rows("grok_spending_limit_24oct", "GROK_SUBSCRIPTION", "grok-4.7", task_from_source)
        self.assertEqual(len(rows), 114)
        admitted, canaries, state = replay(rows, "GROK_SUBSCRIPTION", "grok-4.7")
        self.assertLessEqual(admitted - canaries, 3)         # the real launches before the lane is out
        span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600
        self.assertLessEqual(canaries, int(span_h / (POL["credit_probe_s"] / 3600)) + 1)      # then one probe per 6 h, which a refused canary makes free
        self.assertLess(admitted, 10)
        self.assertEqual(health.view(state, "GROK_SUBSCRIPTION", "grok-4.7", rows[-1]["ts"] + 1)["state"], "exhausted")

    def test_24_oct_grok_server_errors_of_one_task_bench_nothing_and_network_is_never_counted(self):
        rows = fixture_rows("grok_other_24oct", "GROK_SUBSCRIPTION", "grok-4.7", task_from_source)
        self.assertEqual([r["outcome"] for r in rows], ["vendor_blip"] * 3 + ["network"])
        admitted, _, state = replay(rows, "GROK_SUBSCRIPTION", "grok-4.7")
        self.assertEqual(admitted, 4)
        self.assertEqual(health.view(state, "GROK_SUBSCRIPTION", "grok-4.7", rows[-1]["ts"] + 1)["state"], "up")

    def test_22_oct_bad_request_never_benches_a_pair(self):
        fx = fixture("bad_request_22oct")
        self.assertFalse(fx["benches_pair"])
        rows = [dict(fr["history"], outcome="bad_request", fingerprint=health.fingerprint("bad_request", "event", fr["evidence"]["vendor_err"])) for fr in fx["rows"]]
        self.assertEqual(len(rows), 53)
        admitted, _, state = replay(rows, "OPENAI_SUBSCRIPTION", "gpt-6.1-sol")
        self.assertEqual(admitted, 53)         # every one of the 53 was admitted: bad_request never refuses the next call
        self.assertFalse(state["pairs"] or state["lanes"])
        self.assertTrue(all(not classify.benches_pair(classify.classify(**fr["evidence"]).reason) for fr in fx["rows"]))

    def test_22_oct_night_whole_history_never_benches(self):
        rows = [dict(fr["history"]) for fr in sorted(fixture("night_22oct")["rows"], key=lambda r: r["history"]["ts"])]
        state = health.new_state()
        for r in rows:
            if r.get("lane") and r.get("model"):
                self.assertTrue(health.view(state, r["lane"], r["model"], r["ts"])["usable"], r)
            health.step(state, r, POL)
        self.assertFalse([k for k, p in state["pairs"].items() if p["rung"]])


class Breaker(unittest.TestCase):
    def test_three_identical_failures_from_two_tasks_bench_the_pair_15_minutes(self):
        s = health.fold([row(1000 + 60 * i, outcome="vendor_blip", say="Server error.", task="a" if i < 2 else "b") for i in range(3)])
        v = health.view(s, "L", "m", 1121)
        self.assertEqual((v["state"], v["usable"], v["kind"], v["source"], v["rung"]), ("broken", False, "vendor_blip", "breaker", 1))
        self.assertEqual(v["until"], 1120 + 15 * 60)
        self.assertEqual(v["evidence"], "Server error.")
        self.assertEqual(len(v["last_failures"]), 3)

    def test_one_task_failing_three_times_benches_nothing(self):
        s = health.fold([row(1000 + 60 * i, outcome="vendor_blip", say="Server error.", task="same") for i in range(5)])
        self.assertTrue(health.view(s, "L", "m", 1400)["usable"])

    def test_different_fingerprints_do_not_add_up(self):
        s = health.fold([row(1000, outcome="vendor_blip", say="boom A", task="a"), row(1060, outcome="vendor_blip", say="other text", task="b"),
                         row(1120, outcome="vendor_blip", say="third thing", task="c")])
        self.assertTrue(health.view(s, "L", "m", 1130)["usable"])

    def test_numbers_ids_and_times_in_the_text_do_not_make_failures_different(self):
        a = health.fingerprint("vendor_blip", "event", "Request 4821 failed at 2026-10-24T02:31:53Z id 3f2a9c1b77")
        b = health.fingerprint("vendor_blip", "event", "Request 77 failed at 2026-10-24T09:00:00Z id aa00bb11cc")
        self.assertEqual(a, b)

    def test_failures_outside_the_window_do_not_count(self):
        s = health.fold([row(1000, outcome="vendor_blip", say="x", task="a"), row(1000 + 11 * 60, outcome="vendor_blip", say="x", task="b"),
                         row(1000 + 22 * 60, outcome="vendor_blip", say="x", task="c")])
        self.assertTrue(health.view(s, "L", "m", 1000 + 23 * 60)["usable"])

    def test_a_done_resets_the_streak_and_the_ladder(self):
        s = health.fold([row(1000, outcome="vendor_blip", say="x", task="a"), row(1010, outcome="vendor_blip", say="x", task="b"), row(1020, outcome=None),
                         row(1030, outcome="vendor_blip", say="x", task="c")])
        self.assertTrue(health.view(s, "L", "m", 1040)["usable"])

    def test_ladder_is_15_30_60_then_stays_at_60_and_a_passing_canary_resets_it(self):
        s = health.new_state()
        for i in range(3):
            health.step(s, row(1000 + i, outcome="worker_failed", say="x", task=f"t{i}"), POL)
        fail_at, minutes = 1002, []
        for _ in range(4):
            v = health.view(s, "L", "m", fail_at + 1)
            self.assertEqual((v["state"], v["usable"]), ("broken", False))
            minutes.append(round((v["until"] - fail_at) / 60))
            fail_at = v["until"] + 1                                   # the bench ends: the next launch is the canary...
            self.assertEqual((health.view(s, "L", "m", fail_at)["usable"], health.view(s, "L", "m", fail_at)["canary"]), (True, True))
            health.step(s, row(fail_at, outcome="worker_failed", say="x", task="canary"), POL)       # ...and it fails: next rung at once
        self.assertEqual(minutes, [15, 30, 60, 60])
        t = health.view(s, "L", "m", fail_at + 1)["until"] + 1
        health.step(s, row(t, outcome=None), POL)                       # a canary that passes
        v = health.view(s, "L", "m", t + 1)
        self.assertEqual((v["state"], v["rung"], v["canary"]), ("up", 0, False))

    def test_hung_twice_in_30_minutes_benches_the_pair(self):
        s = health.fold([row(1000, outcome="hung"), row(1000 + 20 * 60, outcome="hung")])
        self.assertEqual(health.view(s, "L", "m", 1000 + 21 * 60)["kind"], "hung")
        s = health.fold([row(1000, outcome="hung"), row(1000 + 40 * 60, outcome="hung")])
        self.assertTrue(health.view(s, "L", "m", 1000 + 41 * 60)["usable"])

    def test_never_counted_reasons_leave_every_pair_up(self):
        never = ["bad_request", "network", "killed", "timeout", "secrets", "schema_invalid", "held", "busy", "caller", "lock", "setup", "crash"]
        rows = [row(1000 + i, outcome=never[i % len(never)], say="same", task=f"t{i}") for i in range(60)]
        s = health.fold(rows)
        self.assertTrue(health.view(s, "L", "m", 1100)["usable"])
        self.assertFalse(s["pairs"] or s["lanes"])

    def test_refusals_carry_no_run_id_and_are_not_facts(self):
        s = health.fold([dict(row(1000 + i, outcome="vendor_blip", say="x"), run_id=None) for i in range(9)])
        self.assertTrue(health.view(s, "L", "m", 1100)["usable"])

    def test_pairs_are_independent(self):
        s = health.fold([row(1000 + i, model="a", outcome="worker_failed", say="x", task=f"t{i}") for i in range(3)])
        self.assertFalse(health.view(s, "L", "a", 1010)["usable"])
        self.assertTrue(health.view(s, "L", "b", 1010)["usable"])

    def test_n_and_window_are_config_values(self):
        pol = health.policy({"breaker": {"n": 2}})
        s = health.fold([row(1000, outcome="worker_failed", say="x", task="a"), row(1001, outcome="worker_failed", say="x", task="b")], pol=pol)
        self.assertTrue(health.view(s, "L", "m", 1002, pol)["usable"])           # worker_failed also needs 3 distinct tasks
        s = health.fold([row(1000, outcome="vendor_blip", say="x", task="a"), row(1001, outcome="vendor_blip", say="x", task="b")], pol=pol)
        self.assertFalse(health.view(s, "L", "m", 1002, pol)["usable"])
        self.assertEqual(health.policy({"breaker": {"window_min": 3}})["window_min"], 3)

    def test_breaker_applies_to_grok_deepseek_and_zai_pairs(self):
        for lane, model in (("GROK_SUBSCRIPTION", "grok-4.7"), ("DEEPSEEK_FREE", "deepseek-v4.1-flash-free"), ("ZAI_CODING_SUBSCRIPTION", "glm-5.3")):
            with self.subTest(lane=lane):
                s = health.fold([row(1000 + i, lane, model, "vendor_blip", say="Server error.", task=f"t{i}") for i in range(3)])
                self.assertFalse(health.view(s, lane, model, 1010)["usable"])


class LaneFacts(unittest.TestCase):
    def test_vendor_time_ends_a_limit_and_then_one_canary(self):
        s = health.fold([row(1000, outcome="rate_limited", say="limit", until=5000.0)])
        v = health.view(s, "L", "any-model", 2000)
        self.assertEqual((v["state"], v["usable"], v["source"], v["until"]), ("exhausted", False, "vendor", 5000.0))
        v = health.view(s, "L", "any-model", 5001)
        self.assertEqual((v["state"], v["usable"], v["canary"]), ("up", True, True))

    def test_limit_without_a_time_is_a_guess_of_15_minutes(self):
        s = health.fold([row(1000, outcome="rate_limited", say="limit")])
        v = health.view(s, "L", "m", 1001)
        self.assertEqual((v["source"], v["until"]), ("guess", 1000 + 900))

    def test_credit_probe_after_6_hours_and_logged_out(self):
        s = health.fold([row(1000, lane="C", outcome="credit_exhausted", say="out of credits"), row(1000, lane="O", outcome="logged_out", say="not logged in")])
        self.assertEqual(health.view(s, "C", "m", 1000 + 6 * 3600 - 1)["state"], "exhausted")
        self.assertEqual(health.view(s, "O", "m", 1001)["state"], "logged_out")
        self.assertTrue(health.view(s, "C", "m", 1000 + 6 * 3600 + 1)["usable"])

    def test_a_done_on_the_lane_ends_its_limit_and_a_release_row_too(self):
        s = health.fold([row(1000, outcome="rate_limited", say="x", until=9e9), row(1100, model="other", outcome=None)])
        self.assertTrue(health.view(s, "L", "m", 1200)["usable"])
        s = health.fold([row(1000, outcome="rate_limited", say="x", until=9e9), {"ts": 1100, "release": True, "lane": "L", "model": None, "run_id": "manual"}])
        self.assertTrue(health.view(s, "L", "m", 1200)["usable"])

    def test_combine_picks_the_most_severe_state(self):
        up = health.view(health.new_state(), "L", "m", 1)
        self.assertEqual(health.combine(up), "up")
        self.assertEqual(health.combine(up, busy=True), "busy")
        self.assertEqual(health.combine(up, held={"id": "x"}, busy=True), "held")
        ex = health.view(health.fold([row(1, outcome="rate_limited", say="x", until=99.0)]), "L", "m", 2)
        self.assertEqual(health.combine(ex, busy=True), "exhausted")
        self.assertEqual(set(health.STATES), {"up", "busy", "exhausted", "logged_out", "held", "broken"})

    def test_usage_older_than_6_hours_is_unknown_not_a_gate(self):
        self.assertIsNone(health.stale_usage(1000, 1000 + 5 * 3600))
        self.assertEqual(health.stale_usage(1000, 1000 + 7 * 3600), 7.0)
        self.assertIsNone(health.stale_usage(None, 1e9))


class Fold(unittest.TestCase):
    def rows(self):
        out = []
        for i in range(40):
            out.append(row(1000 + 100 * i, outcome="vendor_blip" if i % 7 < 3 else ("rate_limited" if i % 11 == 0 else None), say="Server error.", task=f"t{i % 4}", until=3000.0 + 100 * i))
        return out

    def test_replay_is_deterministic_and_order_independent(self):
        rows = self.rows()
        a = health.fold(rows)
        b = health.fold(list(reversed(rows)))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_checkpoint_plus_tail_equals_full_fold(self):
        rows = self.rows()
        for cut in (5, 17, 33):
            head = health.fold(rows[:cut])
            cp = json.loads(json.dumps(head))               # a checkpoint went through a file
            resumed = health.fold(rows, cp)                 # rows at or before the checkpoint are skipped
            self.assertEqual(json.dumps(resumed, sort_keys=True), json.dumps(health.fold(rows), sort_keys=True))

    def test_rotation_skips_corrupt_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "history.jsonl"
            p.write_text(json.dumps(row(1000, outcome="worker_failed", say="x")) + "\n{not json\n\n[1,2]\n" + json.dumps({"no": "ts"}) + "\n"
                         + json.dumps(row(1001, outcome="worker_failed", say="x")) + "\n", encoding="utf-8")
            rows = health.read_rows(p)
            self.assertEqual([r["ts"] for r in rows], [1000, 1001])
            self.assertEqual(health.read_rows(Path(d) / "missing.jsonl"), [])
            self.assertEqual([r["ts"] for r in health.read_rows(p, since=1000)], [1001])

    def test_checkpoint_file_is_only_a_cache(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cp.json"
            s = health.fold(self.rows())
            health.save(p, s, POL)
            self.assertEqual(health.load(p, POL), s)
            self.assertIsNone(health.load(p, health.policy({"breaker": {"n": 9}})))        # another policy: refold
            p.write_text("{garbage", encoding="utf-8")
            self.assertIsNone(health.load(p, POL))

    def test_history_row_carries_v_fingerprint_say(self):
        f = health.row_fields("vendor_blip", "event", "Server error. " + "x" * 400, None)
        self.assertEqual((f["v"], f["outcome"], len(f["fingerprint"]), len(f["say"])), (1, "vendor_blip", 12, 240))
        self.assertEqual(health.row_fields(None, "", ""), {"v": 1})


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def run_watch(cli, events_at, until, tick=0.5, jump=None):
    """Drive a Watchdog on a fake clock like run_proc does (a poll every `tick` s). -> the second it fired, or None."""
    clk = Clock()
    wd = supervisor.Watchdog(clock=clk, **supervisor.limits(cli))
    events = sorted(events_at)
    while clk.t < until:
        clk.t = round(clk.t + tick, 3)
        while events and events[0] <= clk.t:
            events.pop(0)
            wd.touch()
        if jump and jump[0] <= clk.t < jump[0] + tick:
            clk.t += jump[1]
        if wd.poll():
            return clk.t, wd
    return None, wd


class Watchdog(unittest.TestCase):
    def test_answer_grace_is_30s_and_the_codex_silence_limit_is_unchanged(self):
        """delegate-itn: the grace after a final answer is 120 s, a stalled model request 900 s; a stream with no final message still waits the 3600 s silence limit."""
        self.assertEqual(supervisor.ANSWER_GRACE_S, 120)
        self.assertEqual(supervisor.STALL_S, 900)
        self.assertEqual(supervisor.LIMITS["codex"]["silence_s"], 3600)
        at, _ = run_watch("codex", [5], 6000)
        self.assertEqual(at, 3605.5)

    def test_limits(self):
        L = supervisor.LIMITS
        self.assertEqual({c: (L[c]["first_event_s"], L[c]["silence_s"]) for c in L}, {"claude": (120, 600), "codex": (120, 3600), "opencode": (120, 900)})
        self.assertGreaterEqual(L["codex"]["silence_s"], 3600)         # P7: healthy Codex reasoning gaps up to 2,298 s
        self.assertNotEqual(L["codex"]["silence_s"], 600)
        self.assertGreaterEqual(L["opencode"]["silence_s"], 900)

    def test_injected_1800s_codex_reasoning_gap_is_not_killed(self):
        at, _ = run_watch("codex", [10, 12, 10 + 1800 + 2, 10 + 1800 + 4], 10 + 1800 + 100)
        self.assertIsNone(at)

    def test_injected_codex_gap_of_2298s_is_not_killed_but_a_dead_stream_is(self):
        self.assertIsNone(run_watch("codex", [5, 5 + 2298], 5 + 2298 + 60)[0])
        at, wd = run_watch("codex", [5], 6000)
        self.assertEqual(wd.why, "silence")
        self.assertAlmostEqual(at, 5 + 3600, delta=1.0)

    def test_codex_3000s_gap_survives_and_is_logged_but_1800s_is_not(self):
        for gap, logged in ((1800, []), (3000, [3000.0])):
            at, wd = run_watch("codex", [5, 5 + gap], 5 + gap + 60)
            self.assertIsNone(at)
            rows = []
            wd.flush(rows.append, "OPENAI_SUBSCRIPTION", "gpt", True)
            self.assertEqual([r["gap_s"] for r in rows], logged)
            for r in rows:
                self.assertEqual((r["lane"], r["model"], r["finished"], r["run_id"]), ("OPENAI_SUBSCRIPTION", "gpt", True, None))

    def test_codex_3700s_dead_stream_is_killed_and_logged(self):
        at, wd = run_watch("codex", [5], 6000)       # run_proc turns this verdict into route.stop_tree (covered in test_route)
        self.assertEqual(wd.why, "silence")
        self.assertLess(at, 3700)
        rows = []
        wd.flush(rows.append, "L", "m", False)
        self.assertEqual([(r["finished"], r["gap_s"] > 1800) for r in rows], [(False, True)])
        wd.flush(lambda r: 1 / 0, "L", "m", False)      # a failing writer never fails the call

    def test_claude_silence_600_and_opencode_900(self):
        at, wd = run_watch("claude", [3], 2000)
        self.assertEqual((wd.why, round(at)), ("silence", 604))
        at, wd = run_watch("opencode", [3], 2000)
        self.assertEqual((wd.why, round(at)), ("silence", 904))

    def test_no_first_event_within_120s_is_hung(self):
        at, wd = run_watch("codex", [], 1000)
        self.assertEqual(wd.why, "first_event")
        self.assertAlmostEqual(at, 120, delta=1.0)
        self.assertIn("printed nothing within 120s", wd.describe())

    def test_any_line_is_activity(self):
        self.assertIsNone(run_watch("claude", list(range(5, 3000, 590)), 3000)[0])

    def test_sleep_gap_is_not_silence(self):
        # one poll sees the clock jump 2,000 s (the lid was shut): that time is not counted against a Claude worker with a 600 s limit
        at, wd = run_watch("claude", [5], 400, jump=(100, 2000))
        self.assertIsNone(at)

    def test_p95_hung_step_time_is_idle_limit_plus_at_most_one_tick(self):
        import random
        rnd, lat = random.Random(7), []
        for i in range(200):
            cli = ("claude", "codex", "opencode")[i % 3]
            lim = supervisor.LIMITS[cli]
            last_event = rnd.uniform(1, 300)                 # the worker dies after a random amount of healthy output
            at, wd = run_watch(cli, [last_event / 2, last_event], last_event + lim["silence_s"] + 300)
            self.assertIsNotNone(at, cli)
            lat.append(at - last_event - lim["silence_s"])
        lat.sort()
        self.assertLessEqual(lat[int(len(lat) * 0.95)], 180)             # the bar: idle limit + 3 min
        self.assertLessEqual(lat[-1], 1.0)                                # in practice: one poll tick

    def test_heartbeat_is_throttled(self):
        clk, beats = Clock(), []
        wd = supervisor.Watchdog(120, 600, clock=clk, beat=beats.append, wall=lambda: 1e9 + clk.t)
        for i in range(100):
            clk.t = i
            wd.touch()
        self.assertEqual(len(beats), 4)       # at 0, 30, 60, 90


if __name__ == "__main__":
    unittest.main()
