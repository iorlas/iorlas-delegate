"""UNITYCODEX: per-call rules grant only a guard invocation, never a raw executable."""
import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401
from test_route import route

from delegate.adapters.harness.base import LaunchSpec
from delegate.app import codex_policy, env
from delegate.cli import parser
from delegate.contracts import stops, wire


def rules_at(home):
    """Parse just the generated prefix_rule syntax; no eval or real agent/login."""
    result = []
    for line in (home / "rules/default.rules").read_text().splitlines():
        call = ast.parse(line).body[0].value
        assert call.func.id == "prefix_rule"
        result.append({kw.arg: ast.literal_eval(kw.value) for kw in call.keywords})
    return result


class Overlay(unittest.TestCase):
    def setUp(self):
        self.root = env.config_dir()
        self.policy = {"binaries": {"python": sys.executable, "just": "/opt/just"},
                       "writer_exec_allow": [["python", "-m", "unittest"], ["python", "-m", "pytest"], ["just", "check"]]}
        (self.root / "host.json").write_text(json.dumps(self.policy))
        self.spec = LaunchSpec(argv=["codex", "exec", "--ignore-rules", "--ignore-user-config", "-c", "approval_policy=on-request",
                                     "-c", "sandbox_workspace_write.network_access=false"],
                               env={"PATH": "/bad/path"}, cwd=str(self.root), stdin="brief")

    def configure(self, rules):
        spec, overlay = codex_policy.configure_writer(self.spec, rules, self.root)
        if overlay:
            self.addCleanup(overlay.cleanup)
        return spec, overlay

    def test_rules_are_per_entry_guard_prefixes_and_auth_is_only_a_symlink(self):
        real = self.root / "real-codex"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(real)}):
            spec, overlay = self.configure([["python", "-m", "unittest"], ["just", "check"]])
        assert overlay is not None
        home = Path(spec.env["CODEX_HOME"])
        self.assertEqual(home, overlay.root)
        self.assertEqual(os.readlink(home / "auth.json"), str(real / "auth.json"))
        self.assertFalse((real / "auth.json").exists())  # no credential target was created or copied
        self.assertEqual({p.name for p in home.iterdir()}, {"auth.json", "rules", "exec-allow.json", "sessions", "archived_sessions"})
        rules = rules_at(home)
        self.assertEqual(len(rules), 2)
        for rule, tail in zip(rules, (["python", "-m", "unittest"], ["just", "check"]), strict=True):
            self.assertEqual(rule, {"pattern": [*overlay.prefix, *tail], "decision": "allow"})
        self.assertNotIn("--ignore-rules", spec.argv)
        self.assertIn("--ignore-user-config", spec.argv)
        self.assertIn("approval_policy=never", spec.argv)
        self.assertNotIn("approval_policy=on-request", spec.argv)
        self.assertIn("sandbox_workspace_write.network_access=false", spec.argv)
        self.assertIn("--exec-allow", spec.stdin)
        self.assertEqual(self.spec.env, {"PATH": "/bad/path"})

    def test_an_allowed_argv_runs_the_real_shim_without_a_prompt(self):
        spec, overlay = self.configure([["python", "-m", "unittest"]])
        assert overlay is not None
        command = [*overlay.prefix, "python", "-m", "unittest", "--help"]
        matching = [r for r in rules_at(overlay.root) if command[:len(r["pattern"])] == r["pattern"]]
        self.assertEqual([r["decision"] for r in matching], ["allow"])
        result = subprocess.run(command, env=os.environ | {"PATH": ""}, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads((self.root / "guard.jsonl").read_text())
        self.assertTrue(row["allowed"])
        self.assertEqual(row["resolved_argv"][0], sys.executable)
        self.assertIn("approval_policy=never", spec.argv)

    def test_another_argv_has_no_outside_sandbox_allow_and_call_scope_is_enforced(self):
        _, overlay = self.configure([["python", "-m", "unittest"]])
        assert overlay is not None
        rules = rules_at(overlay.root)
        for command in (["python", "-m", "unittest"], [*overlay.prefix, "python", "-m", "pytest"],
                        [*overlay.prefix, "python", "-c", "pass"]):
            self.assertFalse(any(command[:len(r["pattern"])] == r["pattern"] for r in rules))
        # Even an explicit direct call to the shim cannot borrow the host's broader allow.
        with mock.patch("sys.stderr", io.StringIO()), mock.patch("delegate.app.codex_policy.subprocess.run") as run:
            code = route.main([*overlay.prefix[2:], "python", "-m", "pytest"])
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_empty_list_keeps_the_exact_existing_launch_and_creates_nothing(self):
        before = set(self.root.iterdir())
        spec, overlay = self.configure([])
        self.assertIs(spec, self.spec)
        self.assertIsNone(overlay)
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertIn("--ignore-rules", spec.argv)

    def test_project_rule_layers_are_explicitly_untrusted(self):
        spec, _ = self.configure([["python", "-m", "unittest"]])
        config = tomllib.loads("\n".join(word for word in spec.argv if word.startswith("projects.")))
        cwd = Path(spec.cwd).resolve()
        for folder in (cwd, *cwd.parents):
            self.assertEqual(config["projects"][str(folder)]["trust_level"], "untrusted")

    def test_cleanup_removes_overlay_and_not_the_auth_target(self):
        _, overlay = self.configure([["python", "-m", "unittest"]])
        assert overlay is not None
        root = overlay.root
        overlay.cleanup()
        self.assertFalse(root.exists())
        overlay.cleanup()

    def test_session_data_survives_overlay_cleanup_without_being_copied(self):
        real = self.root / "real-codex"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(real)}):
            spec, overlay = self.configure([["python", "-m", "unittest"]])
        assert overlay is not None
        session = overlay.root / "sessions/rollout-test.jsonl"
        session.write_text("session data")
        self.assertEqual(os.readlink(overlay.root / "sessions"), str(real / "sessions"))
        self.assertIn("sqlite_home=" + json.dumps(str(real)), spec.argv)
        overlay.cleanup()
        self.assertEqual((real / "sessions/rollout-test.jsonl").read_text(), "session data")

    def test_invalid_unpinned_or_engine_only_lists_refuse_before_allocating_overlay(self):
        before = set(self.root.iterdir())
        for rules in ([["env", "python"]], [["just", "-f", "x", "check"]], [["python", "-c", "pass"]], "python", [3]):
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                self.configure(rules)
        with self.assertRaises(ValueError):
            codex_policy.configure_writer(self.spec, [["just", "check"]], self.root, ("check",))
        self.assertEqual(set(self.root.iterdir()), before)


class Admission(unittest.TestCase):
    def test_new_field_round_trips_and_is_advertised(self):
        req = wire.RunRequest(role="worker", brief_file="b", cwd="/w", writer_exec_allow=[["dotnet", "test"]])
        args = req.to_argv("route.py", contract=stops.CONTRACT)
        ns = parser.build().parse_args(args[2:])
        self.assertEqual(ns.writer_exec_allow, [["dotnet", "test"]])
        self.assertEqual(stops.CONTRACT["contract"], 4)
        self.assertIn("writer_exec_allow", stops.CONTRACT["capabilities"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            route.main(["contract", "--json"])
        self.assertIn("writer_exec_allow", json.loads(out.getvalue())["capabilities"])

    def test_old_or_missing_capability_is_refused_before_serialization(self):
        req = wire.RunRequest(role="worker", brief_file="b", cwd="/w", writer_exec_allow=[["dotnet", "test"]])
        for contract in (None, {"contract": 3, "capabilities": ["writer_exec_allow"]},
                         {"contract": 4, "capabilities": []}):
            with self.subTest(contract=contract), self.assertRaises(ValueError):
                req.to_argv("route.py", contract=contract)

    def test_receiver_without_the_capability_refuses_before_routing(self):
        out = io.StringIO()
        with mock.patch.dict(stops.CONTRACT, {"capabilities": []}), \
                mock.patch("delegate.app.decide.decision") as decide, contextlib.redirect_stdout(out):
            route.main(["run", "worker", "--unattended", "--writer-exec-allow", '[["dotnet","test"]]'])
        decide.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["stop"]["reason"], "bad_request")


if __name__ == "__main__":
    unittest.main()
