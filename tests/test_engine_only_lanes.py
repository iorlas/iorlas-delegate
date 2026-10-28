"""delegate-81g: `--engine-only RECIPE` is enforced by the harness of every lane that can write, or the lane is not routed a write step.
Claude: the PreToolUse hook. OpenCode: the bash deny rules of its permission config. Codex: no enforcement point, refused at route time."""
import copy
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import hermetic  # noqa: F401  (a home of its own for every test)
import routing_support as support
from test_review_order import CFG, T, lanes

from delegate.adapters.harness import opencode_sandbox as box
from delegate.adapters.harness.base import LaunchInputs
from delegate.adapters.harness.opencode import OpenCodeHarness
from delegate.app import launch
from delegate.contracts.routing import CallArgs
from delegate.contracts.state import RefRecord
from delegate.contracts.wire import RECIPE_RUNNERS

ROOT = Path(__file__).resolve().parent.parent
DENIED_BY_OPENCODE = (
    "just slow", "just slow --verbose", "just -f x/justfile slow", "just --justfile x/justfile slow", "FOO=1 just slow",
    "cd client && just slow", 'sh -c "just slow"', "bash -lc 'just slow'", "(just slow)", "make slow", "uv run slow", "pnpm slow",
    "ls && just slow | tee log")
PASSES = ("just test-fast", "just slower", "just fmt-check tests/slow.py", "python3 -m unittest discover -s tests", "git diff --stat",
          "ls slow")
TRAILS = ("", " *", '"*', "'*", ";*", "&*", "|*", ")*")


def hook_command(settings: dict) -> str:
    return settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def opencode_verdict(rules: dict[str, str], command: str) -> str:
    """OpenCode's wildcard match, restated here: `*` is any characters, the whole command must match, the last matching rule wins."""
    verdict = "ask"
    for pattern, action in rules.items():
        if re.fullmatch(".*".join(re.escape(x) for x in pattern.split("*")), command, re.S):
            verdict = action
    return verdict


class OpenCodeRulesTest(unittest.TestCase):
    def bash(self, write: bool = True, engine_only: tuple[str, ...] = ("slow",)):
        return box.permissions(write, engine_only).get("bash", {})

    def test_the_rules_for_one_runner_are_exactly_these(self):
        rules = self.bash()
        want = {lead + t: "deny" for lead in ("*just slow", "*just * slow") for t in TRAILS}
        self.assertEqual({k: v for k, v in rules.items() if k.startswith("*just ")}, want)
        self.assertEqual(len(rules), 1 + 16 * len(RECIPE_RUNNERS))
        self.assertEqual(next(iter(rules.items())), ("*", "allow"))        # the allow comes first: the deny rules after it win

    def test_every_way_to_run_the_recipe_is_denied_and_ordinary_commands_are_not(self):
        rules = self.bash()
        for c in DENIED_BY_OPENCODE:
            self.assertEqual(opencode_verdict(rules, c), "deny", c)
        for c in PASSES:
            self.assertEqual(opencode_verdict(rules, c), "allow", c)

    def test_read_only_steps_and_calls_without_the_list_get_no_bash_rules(self):
        self.assertEqual(self.bash(write=False), {})
        self.assertEqual(self.bash(engine_only=()), {})

    def test_the_launch_carries_the_rules_in_the_config_the_harness_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            hx = OpenCodeHarness(root=Path(tmp))
            spec = hx.launch_spec(LaunchInputs(exe="opencode", cwd=tmp, model="m", effort="high", prompt="p", env={}, session="abcdefghij",
                                               write=True, engine_only=("slow", "e2e")))
            hx.cleanup()
        perm = json.loads(spec.env["OPENCODE_CONFIG_CONTENT"])["permission"]
        self.assertEqual(perm, box.permissions(True, ("slow", "e2e")))
        self.assertEqual(opencode_verdict(perm["bash"], "just e2e"), "deny")

    def test_the_denial_is_the_typed_policy_record(self):
        hx = OpenCodeHarness()
        refusal = "The user has specified a rule which prevents you from using this specific tool call"
        state = {"status": "error", "input": {"command": "just slow"}, "error": refusal}
        hx.parse_line(json.dumps({"type": "tool_use", "sessionID": "s", "part": {"callID": "c1", "tool": "bash", "state": state}}))
        want = [{"kind": "policy", "tool": "bash", "path": None, "input": {"command": "just slow"}}]
        self.assertEqual(launch.denied_items(hx, False, ("slow",)), want)


class ClaudeHookTest(unittest.TestCase):
    def hook(self, command: str, engine_only=("slow",), read_only=False):
        argv = hook_command(launch.guard_settings(read_only, engine_only))
        call = {"tool_name": "Bash", "tool_input": {"command": command}}
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(argv, shell=True, input=json.dumps(call), capture_output=True, text=True, check=False,
                               env={"PATH": "/usr/bin:/bin", "DELEGATE_CONFIG_DIR": tmp, "HOME": tmp})
        return r.returncode, r.stderr

    def test_the_generated_hook_denies_the_recipe_with_the_rule(self):
        for c in ("just slow", "cd client && just slow", "FOO=1 just slow", "just -f x slow"):
            code, why = self.hook(c)
            self.assertEqual(code, 2, c)
            self.assertIn("`just slow` is a check only the engine runs", why)
            self.assertIn("engine reruns the check", why)

    def test_the_generated_hook_lets_other_commands_through_and_a_read_step_ignores_the_list(self):
        self.assertEqual(self.hook("just test-fast")[0], 0)
        self.assertEqual(self.hook("just slow", ())[0], 0)
        self.assertNotIn("slow", hook_command(launch.guard_settings(True, ("slow",))))


class RoutingTest(unittest.TestCase):
    def decide(self, *up, role="worker", **kw):
        return support.decide(role, copy.deepcopy(CFG), lanes(*up), {}, T, dict({"authored_by": None, "unattended": True}, **kw))

    def test_a_write_step_with_the_list_skips_codex_and_says_why(self):
        free = self.decide("openai", "xai", "claude")
        self.assertEqual(free["lane"], "OPENAI_SUBSCRIPTION")
        d = self.decide("openai", "xai", "claude", engine_only=["slow"])
        self.assertEqual((d["action"], d["lane"]), ("run", "GROK_SUBSCRIPTION"))
        self.assertIn("OPENAI_SUBSCRIPTION: rejected, its harness cannot deny the engine-only recipes slow to a write step", d["trace"])

    def test_with_only_codex_up_the_call_is_refused_with_a_typed_reason(self):
        d = self.decide("openai", engine_only=["slow", "e2e"])
        self.assertEqual((d["action"], d["reason"]), ("stop", "engine_only_unenforced"))
        self.assertIn("--engine-only slow, e2e cannot be denied to a write step on OPENAI_SUBSCRIPTION", d["say"])
        self.assertTrue(any(x.startswith("OPENAI_SUBSCRIPTION: rejected") for x in d["trace"]), d["trace"])

    def test_claude_runs_as_claude_p_when_unattended_and_is_refused_as_an_agent_tool_when_not(self):
        self.assertEqual(self.decide("claude", engine_only=["slow"])["lane"], "CLAUDE_SUBSCRIPTION")
        d = self.decide("claude", engine_only=["slow"], unattended=False)
        self.assertEqual((d["action"], d["reason"]), ("stop", "engine_only_unenforced"))

    def test_read_only_steps_ignore_the_list(self):
        self.assertEqual(self.decide("openai", role="reviewer", engine_only=["slow"])["lane"], "OPENAI_SUBSCRIPTION")

    def test_a_resume_of_a_codex_writer_with_the_list_is_refused(self):
        rec = RefRecord(lane="OPENAI_SUBSCRIPTION", model="gpt-6.1-sol", effort="high", mode="write")
        d = support.router(copy.deepcopy(CFG), T).route("worker", CallArgs(resume="r1", engine_only=("slow",)), lanes("openai"), rec)
        self.assertEqual((d.as_dict()["action"], d.as_dict()["reason"]), ("stop", "engine_only_unenforced"))


if __name__ == "__main__":
    unittest.main()
