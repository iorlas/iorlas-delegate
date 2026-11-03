"""Task-local retries and rejected answer evidence, with two fake ready lanes."""
from dataclasses import replace
from unittest import mock

import routing_support as support
from run_support import RunBase
from test_route import CFG, lanes, why

from delegate.adapters import git
from delegate.app import env, launch
from delegate.contracts import history as hist
from delegate.contracts.routing import CallArgs, Step
from delegate.domain.holds.book import HoldBook
from delegate.domain.routing.router import Router


class RunReroute(RunBase):
    def schema(self):
        path = self.base / "schema.json"
        path.write_text('{"type":"object","required":["a"]}')
        return str(path)

    def failure(self, outcome="timeout", task="T", role="worker", ts=None):
        env.history_add(hist.RunRow(ts=env.now() - 10 if ts is None else ts, run_id="prior", task=task, role=role,
                                   lane="OPENAI_SUBSCRIPTION", model="gpt-6.1-sol", effort="high", done=False, outcome=outcome))

    def fake(self, d, *args):
        return {"text": "prose é" * 4000, "data": None, "ref": d["lane"], "tokens": {"in": 10, "cached": 2, "out": 3}, "cost": 0.25}

    def rows(self):
        return [r for r in env.history_store().rows() if isinstance(r, hist.RunRow)]

    def test_schema_rejection_keeps_utf8_prefix_and_problem(self):
        with support.fleet_patches(lanes()), mock.patch.object(launch, "launch_worker", side_effect=self.fake):
            _, result = self.run_it("--schema", self.schema(), "--only-family", "openai")
        row = self.rows()[-1]
        expected = ("prose é" * 4000).encode()[:20 * 1024].decode("utf-8", errors="ignore")
        self.assertEqual(row.raw_answer, expected)
        self.assertIn("not JSON", row.schema_problem or "")
        self.assertIn(row.schema_problem, row.say or "")
        self.assertEqual(why(result), "schema_invalid")

    def test_fresh_task_avoids_recent_timeout_and_schema_invalid(self):
        for outcome in ("timeout", "schema_invalid"):
            with self.subTest(outcome=outcome):
                self.failure(outcome)
                with support.fleet_patches(lanes()):
                    _, result = self.run_it("--task", "T")
                self.assertEqual(result["lane"], "CLAUDE_SUBSCRIPTION")

    def test_avoidance_is_task_role_and_time_bounded(self):
        for override in ({"task": "other"}, {"role": "reviewer"}, {"ts": env.now() - 86400}):
            self.failure(**override)
        with support.fleet_patches(lanes()):
            _, result = self.run_it("--task", "T")
        self.assertEqual(result["lane"], "OPENAI_SUBSCRIPTION")

    def test_no_task_and_resume_keep_the_original_choice(self):
        self.failure()
        with support.fleet_patches(lanes()):
            _, result = self.run_it("--task", "another")
            _, resumed = self.run_it("--task", "T", "--resume", result["ref"])
            _, plain = self.run_it()
        self.assertEqual((resumed["lane"], plain["lane"]), ("OPENAI_SUBSCRIPTION",) * 2)

    def test_pinned_family_reuses_failed_pair_with_warning(self):
        self.failure()
        with support.fleet_patches(lanes()):
            _, result = self.run_it("--task", "T", "--only-family", "openai")
        self.assertEqual(result["lane"], "OPENAI_SUBSCRIPTION")
        self.assertIn("no other", result["warning"])
        self.assertIn("openai", result["warning"])

    def test_busy_alternative_explains_reuse(self):
        self.failure()
        ls = lanes()
        ls["CLAUDE_SUBSCRIPTION"] = ls["CLAUDE_SUBSCRIPTION"].with_busy(env.now() + 90, 4, 4)
        with support.fleet_patches(ls):
            _, result = self.run_it("--task", "T")
        self.assertEqual(result["lane"], "OPENAI_SUBSCRIPTION")
        self.assertIn("no other", result["warning"])
        self.assertIn("rejected, out", result["warning"])

    def test_same_lane_can_use_a_different_model_within_pinned_family(self):
        routing = env.typed_routing(CFG)
        steps = tuple(Step(model=m, effort="high", on="openai") for m in ("gpt-6.1-sol", "gpt-6-luna"))
        role = replace(routing.roles["worker"], steps=steps)
        routing = replace(routing, roles=dict(routing.roles, worker=role))
        pick = Router(routing, HoldBook({}), {}, env.now()).route("worker", CallArgs(only_family=("openai",)), lanes(),
                                                               avoid=frozenset({("OPENAI_SUBSCRIPTION", "gpt-6.1-sol")}))
        self.assertEqual((pick.as_dict()["lane"], pick.as_dict()["model"]), ("OPENAI_SUBSCRIPTION", "gpt-6-luna"))

    def test_second_lane_can_succeed_without_a_third_attempt(self):
        def worker(d, *args):
            res = self.fake(d, *args)
            if d["lane"] == "CLAUDE_SUBSCRIPTION":
                res.update(data={"a": 1}, text='{"a":1}')
            return res
        with support.fleet_patches(lanes()), mock.patch.object(launch, "launch_worker", side_effect=worker) as launched:
            _, result = self.run_it("--schema", self.schema())
        self.assertIsNone(result["stop"])
        self.assertEqual(launched.call_count, 3)
        self.assertEqual([r.done for r in self.rows()], [False, True])

    def test_schema_reask_failure_launches_one_fresh_different_lane(self):
        with support.fleet_patches(lanes()), mock.patch.object(launch, "launch_worker", side_effect=self.fake) as worker:
            _, result = self.run_it("--task", "T", "--schema", self.schema())
        rows = self.rows()
        self.assertEqual([r.lane for r in rows], ["OPENAI_SUBSCRIPTION", "CLAUDE_SUBSCRIPTION"])
        self.assertEqual([r.outcome for r in rows], ["schema_invalid"] * 2)
        self.assertEqual([(r.tokens or {})["in"] for r in rows], [20, 20])
        self.assertEqual(sum(r.cost for r in rows), 1.0)
        self.assertEqual(worker.call_count, 4)
        self.assertIsNone(worker.call_args_list[2].args[2].resume)
        self.assertEqual(why(result), "schema_invalid")

    def test_reroute_reports_first_attempt_head_change(self):
        state = {"head": "111111111", "branch": "main"}

        def worker(d, *args):
            res = self.fake(d, *args)
            if d["lane"] == "OPENAI_SUBSCRIPTION":
                state["head"] = "222222222"
            else:
                res.update(data={"a": 1}, text='{"a":1}')
            return res

        with support.fleet_patches(lanes()), mock.patch.object(git, "state", side_effect=lambda cwd: dict(state)), \
                mock.patch.object(launch, "launch_worker", side_effect=worker) as launched:
            _, result = self.run_it("--schema", self.schema())
        self.assertIsNone(result["stop"])
        self.assertEqual(launched.call_count, 3)
        self.assertEqual([r.done for r in self.rows()], [False, True])
        self.assertEqual(result["git_violation"], ["HEAD 111111111 -> 222222222"])

    def test_schema_single_lane_says_why_it_did_not_reroute(self):
        with support.fleet_patches(lanes()), mock.patch.object(launch, "launch_worker", side_effect=self.fake) as worker:
            _, result = self.run_it("--schema", self.schema(), "--only-family", "openai")
        self.assertEqual(worker.call_count, 2)
        self.assertIn("no other", result["say"])
        self.assertIn("openai", result["say"])
        self.assertEqual(len(self.rows()), 1)
