"""Orchestrator preparation costs only; chars / 4 estimates, not tokenizer counts.

The initialized Claude-only fixture measures `run worker`'s Agent handoff, the
current SKILL.md hot path. No worker runs and no worker tokens are counted.
"""
import contextlib
import io
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401
import routing_support as support

from delegate.app import env
from delegate.cli.main import main
from delegate.domain.lanes.lane import Lane

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "delegate"
BUDGET = Path(__file__).with_name("token_budget.json")

# Fixed initialized system: one healthy subscription, no setup notices or usage
# probes. Keep actual routing, brief_prefix and JSON serialization under test.
ROUTING = {
    "v": 1,
    "settings": {
        "orchestrator_reserve": {"weekly": 90, "five_hour": 85},
        "worker_lane_gate": {"weekly": 95, "five_hour": 90},
        "relaxed_ceiling": {"five_hour": 97, "weekly": 98,
                            "orchestrator_five_hour": 95, "orchestrator_weekly": 95},
        "strain": {"five_hour": 80, "weekly_pace": -15},
        "recover": {"five_hour": 50, "weekly_pace": -5},
        "burn": {"minutes": 120, "min_left_percent": 10},
        "max_parallel": 4, "min_dwell_minutes": 30, "usage_stale_minutes": 30,
    },
    "models": {"sonnet-5.5": {"family": "claude", "api": "claude-sonnet-5-5", "agent": "sonnet"}},
    "lanes": {"CLAUDE_SUBSCRIPTION": {
        "family": "claude", "kind": "subscription", "reader": "claude_subscription",
        "runner": "agent_tool", "shared_with_orchestrator": True,
    }},
    "roles": {"worker": {
        "about": "implement", "advance": "", "stop": "",
        "steps": [{"on": "claude", "model": "sonnet-5.5", "effort": "medium", "mode": "write"}],
    }},
    "premium": {},
}


def routing_output():
    lane = Lane("CLAUDE_SUBSCRIPTION", "claude", "subscription", True, five=10, week=5, pace=0)
    out = io.StringIO()
    with support.fleet_patches({lane.name: lane}), \
            mock.patch.object(env, "load_routing", return_value=ROUTING), \
            mock.patch.object(env, "now", return_value=1_000_000_000.0), \
            contextlib.redirect_stdout(out):
        rc = main(["run", "worker", "--cwd", "/workspace/project"])
    return rc, out.getvalue()


class TokenBudget(unittest.TestCase):
    def setUp(self):
        self.budgets = json.loads(BUDGET.read_text(encoding="utf-8"))

    def assert_budget(self, key, text):
        recorded = self.budgets[key]
        for unit, size in (("chars", len(text)), ("tokens_estimate", len(text) / 4)):
            with self.subTest(item=key, unit=unit):
                self.assertTrue(recorded[unit]["note"].strip(), f"{key}.{unit} needs a reason")
                self.assertLessEqual(
                    size, recorded[unit]["max"],
                    f"{key}: {size} {unit} exceeds {recorded[unit]['max']}; "
                    "shrink the text or raise the number in tests/token_budget.json with a reason",
                )

    def test_skill_description_and_body(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        _, frontmatter, body = text.split("---\n", 2)
        description = re.search(r"^description: (.+)$", frontmatter, re.M)
        assert description is not None, "update the description measurement if frontmatter changes"
        self.assert_budget("description", description[1])
        self.assert_budget("body", body)

    def test_each_mode(self):
        modes = {str(path.relative_to(SKILL)): path for path in (SKILL / "modes").rglob("*.md")}
        recorded = {key for key in self.budgets if key.startswith("modes/")}
        self.assertEqual(modes.keys(), recorded, "record today's sizes for each mode in tests/token_budget.json")
        for key, path in modes.items():
            self.assert_budget(key, path.read_text(encoding="utf-8"))

    def test_typical_routing_output(self):
        rc, text = routing_output()
        self.assertEqual(rc, 0)
        result = json.loads(text)
        self.assertIsNone(result["stop"])
        self.assertEqual(result["agent_model"], "sonnet")
        self.assertTrue(result["brief_prefix"])
        self.assertNotIn("notice", result)
        self.assertNotIn("needs_input", result)
        self.assert_budget("run_worker", text)
