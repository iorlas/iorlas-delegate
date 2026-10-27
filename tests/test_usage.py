"""Per-call usage and cost accounting (delegate-uk0, study 85)."""
import hermetic  # noqa: F401  (a home of its own for every test)
import json
import os
import tempfile
import unittest
from pathlib import Path
from test_route import route
from lanes import usage

# Two consecutive real Codex rollout token_count totals of one thread (2026-10-20, a resumed thread): the second is cumulative, not the second call's own.
CALL1 = {"input_tokens": 14742, "cached_input_tokens": 12288, "cache_write_input_tokens": 0, "output_tokens": 63, "reasoning_output_tokens": 0, "total_tokens": 14805}
CALL2 = {"input_tokens": 29610, "cached_input_tokens": 24576, "cache_write_input_tokens": 0, "output_tokens": 103, "reasoning_output_tokens": 0, "total_tokens": 29713}


def tc(u):
    return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": u}}}) + "\n"


def short(u):
    return {"in": u["input_tokens"], "cached": u["cached_input_tokens"], "out": u["output_tokens"]}


class CodexResume(unittest.TestCase):
    def test_the_second_calls_own_usage_is_cumulative_minus_the_first(self):
        own = usage.per_call(short(CALL2), short(CALL1))
        self.assertEqual(own, {"in": 14868, "cached": 12288, "out": 40})
        a = usage.codex(own)
        self.assertEqual((a["in_fresh"], a["cache_read"], a["cache_write"], a["out"]), (2580, 12288, None, 40))
        self.assertEqual((a["cost_usd"], a["cost_source"], a["accounting"]), (None, "unknown", "exact"))

    def test_a_first_call_or_a_smaller_total_stands_as_given(self):
        self.assertEqual(usage.per_call(short(CALL1), None), short(CALL1))
        self.assertEqual(usage.per_call(short(CALL1), short(CALL2)), short(CALL1))

    def test_the_rollout_file_supplies_the_earlier_total(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "sessions", "2026", "10", "20")
            f.mkdir(parents=True)
            (f / "rollout-2026-10-20T16-51-32-thr-1.jsonl").write_text(tc(CALL1))
            old = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = d
            try:
                before = route.codex_thread_total("thr-1")
            finally:
                os.environ.pop("CODEX_HOME") if old is None else os.environ.__setitem__("CODEX_HOME", old)
        self.assertEqual(before, short(CALL1))
        self.assertEqual(usage.per_call(short(CALL2), before)["in"], 14868)

    def test_history_keeps_the_last_cumulative_per_ref(self):
        rows = [{"ref": "a", "cumulative": short(CALL1)}, {"ref": "b", "cumulative": {"in": 1, "cached": 0, "out": 0}}, {"ref": "a", "cumulative": short(CALL2)}, {"ref": "a"}]
        self.assertEqual(usage.last_cumulative(rows, "a"), short(CALL2))
        self.assertIsNone(usage.last_cumulative(rows, "zzz"))


class Claude(unittest.TestCase):
    def test_subscription_cost_is_the_harness_figure(self):
        u = {"input_tokens": 3, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 20, "output_tokens": 9,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 20}}
        a = usage.claude(u, 0.0123, credit=False)
        self.assertEqual((a["in_fresh"], a["cache_read"], a["cache_write"], a["out"], a["cost_usd"], a["cost_source"]), (3, 50, 20, 9, 0.0123, "harness"))
        self.assertEqual((a["ttl_s"], a["ttl_source"], a["cache_write_by_ttl"]), (3600, "vendor_default", {"5m": 0, "1h": 20}))
        self.assertEqual(usage.claude(u, 0.01, credit=True)["ttl_s"], 300)

    def test_no_cost_reported_is_unknown_not_zero(self):
        a = usage.claude({"input_tokens": 1}, None, credit=False)
        self.assertEqual((a["cost_usd"], a["cost_source"], a["accounting"]), (None, "unknown", "unknown"))


class Opencode(unittest.TestCase):
    STEP = {"input": 10, "output": 3, "reasoning": 2, "cache": {"read": 40, "write": 1}}

    def run_steps(self, lane, cost):
        a = None
        for _ in range(2):
            a = usage.opencode_step(a, self.STEP, cost)
        return usage.opencode_close(a, lane)

    def test_grok_step_costs_are_summed(self):
        a = self.run_steps("GROK_SUBSCRIPTION", 0.25)
        self.assertEqual((a["in_fresh"], a["cache_read"], a["cache_write"], a["out"], a["cost_usd"], a["cost_source"], a["accounting"]),
                         (20, 80, 2, 10, 0.5, "harness", "estimated"))
        self.assertIsNone(a["ttl_s"])
        self.assertEqual(usage.fields(a, "ses_1", 1_000_000_000)["warm_until"], None)

    def test_zai_is_unknown_even_though_its_steps_say_zero(self):
        a = self.run_steps("ZAI_CODING_SUBSCRIPTION", 0)
        self.assertEqual((a["cost_usd"], a["cost_source"], a["accounting"]), (None, "unknown", "unknown"))

    def test_free_deepseek_is_free(self):
        a = self.run_steps("DEEPSEEK_FREE", 0)
        self.assertEqual((a["cost_usd"], a["cost_source"]), (0.0, "free"))


class Fields(unittest.TestCase):
    def test_warm_until_is_last_activity_plus_ttl_and_only_telemetry(self):
        f = usage.fields(usage.codex(short(CALL1)), "thr-1", 1_000_000_000)
        self.assertEqual((f["last_activity_at"], f["warm_until"], f["ttl_s"], f["ttl_source"]), ("2001-09-09T01:46:40Z", "2001-09-09T02:46:40Z", 3600, "observed"))
        self.assertEqual(usage.fields(None, None, 0)["warm_until"], None)
        self.assertNotIn("warm_until", Path(route.__file__).read_text().split("def decide", 1)[1].split("\ndef ", 1)[0])       # no routing policy reads it

    def test_merge_adds_counts_and_keeps_unknown_cost_unknown(self):
        a, b = usage.codex(short(CALL1)), usage.codex(short(CALL1))
        m = usage.merge(a, b)
        self.assertEqual((m["in_fresh"], m["cache_read"], m["cache_write"], m["cost_usd"]), (2 * 2454, 2 * 12288, None, None))


if __name__ == "__main__":
    unittest.main()
