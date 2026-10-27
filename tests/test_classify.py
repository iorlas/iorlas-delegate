"""The outcome ladder (skills/delegate/lanes/classify.py) and contract.json, replayed on fixtures taken from real runs (tests/contract/fixtures)."""
import hermetic  # noqa: F401  (a home of its own for every test)
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_route import route, ROOT, T

classify = route.classify
FIXTURES = sorted((ROOT / "tests" / "contract" / "fixtures").glob("*.json"))
CONTRACT = json.loads((ROOT / "skills" / "delegate" / "contract.json").read_text(encoding="utf-8"))
LAUNCH_ONLY = {"duplicate_task", "busy", "no_lane", "caller", "crash", "schema_invalid", "lock", "setup", "held"}      # answered outside the ladder


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def replay(row, f):
    ev = row["evidence"]
    return classify.classify(ev["rc"], ev["vendor_err"], ev["stderr"], ev["timed_out"], ev["work_seen"], ev["structured"])


def all_rows():
    for path in FIXTURES:
        f = load(path)
        for row in f["rows"]:
            yield path.name, f, row, row.get("expected_reason", f.get("expected_reason"))


class Fixtures(unittest.TestCase):
    def test_every_fixture_row_replays_to_its_reason(self):
        n = 0
        for name, f, row, want in all_rows():
            if "evidence" not in row:
                continue
            o = replay(row, f)
            self.assertEqual(o.reason if o else None, want, f"{name} {row['id']}")
            n += 1
        self.assertGreater(n, 400)      # 53 + 215 + 53 + 114 + the rest

    def test_the_53_bad_requests_the_grok_batch_and_the_night_are_all_there(self):
        count = lambda name: len(load(ROOT / "tests" / "contract" / "fixtures" / name)["rows"])
        self.assertEqual(count("bad_request_22oct.json"), 53)
        self.assertEqual(count("grok_spending_limit_24oct.json"), 114)
        night = load(ROOT / "tests" / "contract" / "fixtures" / "night_22oct.json")["rows"]
        self.assertEqual(sum(1 for r in night if r["expected_reason"] == "bad_request"), 53)
        self.assertEqual(sum(1 for r in night if r["expected_reason"] is None), len(night) - 53)

    def test_every_declared_reason_has_a_fixture_row_and_every_fixture_reason_is_declared(self):
        used = {want for _, _, _, want in all_rows() if want}
        declared = set(CONTRACT["reasons"])
        self.assertEqual(declared - used, set(), "a reason without a fixture row")
        self.assertEqual(used - declared, set(), "a fixture reason contract.json does not declare")

    def test_a_row_that_is_not_from_a_real_run_says_so(self):
        for name, f, row, want in all_rows():
            if row.get("synthetic"):
                self.assertTrue(row.get("source"), name)

    def test_bad_request_never_benches_a_pair(self):
        self.assertFalse(classify.benches_pair("bad_request"))
        self.assertEqual(CONTRACT["reasons"]["bad_request"]["health"], "none")
        for name, f, row, want in all_rows():
            if want == "bad_request":
                self.assertFalse(classify.benches_pair(replay(row, f).reason), row["id"])
        for reason in ("network", "killed", "timeout", "secrets", "schema_invalid", "held", "busy", "caller"):
            self.assertFalse(classify.benches_pair(reason), reason)
        self.assertTrue(classify.benches_pair("rate_limited") and classify.benches_pair("credit_exhausted"))


def stop_tuple(r):
    s = r["stop"]
    return (s["reason"], s["scope"], s["permanence"], s["retry"]["kind"]) if s else None


def expected_tuple(f, row, reason):
    """The tuple a fixture row must answer with: the row's or file's own `expected_stop`, else the reason's row of contract.json; None = done."""
    es = row.get("expected_stop") or f.get("expected_stop")
    if es:
        return (es["reason"], es["scope"], es["permanence"], es["retry_kind"])
    if not reason:
        return None
    c = CONTRACT["reasons"][reason]
    return (reason, c["scope"], c["permanence"], c["retry"])


class Stop(unittest.TestCase):
    """settle() + result() turn a classified row into the contract 3 answer; a request/machine fault must leave the lane's state byte for byte as it was."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(route, "config_dir", return_value=Path(self.tmp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def answer(self, o, lane, credit_until=None):
        res = route.settle({}, o, {"lane": lane}, T, "cli", SimpleNamespace(timeout=30), 1, "tail", reset=lambda: T + 3600, credit_until=credit_until)
        return route.result(res.pop("outcome"), **res)

    def test_every_fixture_row_answers_with_its_expected_stop_tuple_and_fits_the_schema(self):
        n = 0
        for path in FIXTURES:
            f = load(path)
            for row in f["rows"]:
                want_reason = row.get("expected_reason", f.get("expected_reason"))
                if "evidence" not in row or not want_reason:
                    continue
                before = route.load_state().get("out")
                lane = f.get("lane", "OPENAI_SUBSCRIPTION")
                o = replay(row, f)
                r = self.answer(o, lane)
                if route.net_pass(o, {"lane": lane}).reason == "network" and o.reason != "network":      # a failure that words a dropped connection is the network's (R17)
                    self.assertEqual(r["stop"]["reason"], "network", f"{path.name} {row['id']}")
                    continue
                self.assertEqual(stop_tuple(r), expected_tuple(f, row, want_reason), f"{path.name} {row['id']}")
                self.assertIsNone(route.schema_problem(r, CONTRACT["answer_schema"]), f"{path.name} {row['id']}")
                if r["stop"]["scope"] in ("bead", "caller", "machine"):
                    self.assertEqual(route.load_state().get("out"), before, f"{path.name} {row['id']}: a {r['stop']['scope']}-scoped fault touched the lane's state")
                route.update_state(lambda s: s.pop("out", None))
                n += 1
        self.assertGreater(n, 250)

    def test_golden_the_53_bad_requests_map_to_caller_needs_owner_none(self):
        f = load(ROOT / "tests" / "contract" / "fixtures" / "bad_request_22oct.json")
        self.assertEqual(len(f["rows"]), 53)
        for row in f["rows"]:
            self.assertEqual(stop_tuple(self.answer(replay(row, f), "OPENAI_SUBSCRIPTION")), ("bad_request", "caller", "needs_owner", "none"), row["id"])
        self.assertIsNone(route.load_state().get("out"))            # not one of them marks the lane

    def test_golden_the_grok_batch_is_a_lane_out_with_no_time_and_marks_the_lane_once_for_all(self):
        f = load(ROOT / "tests" / "contract" / "fixtures" / "grok_spending_limit_24oct.json")
        self.assertEqual(len(f["rows"]), 114)
        for row in f["rows"]:
            r = self.answer(replay(row, f), "GROK_SUBSCRIPTION")
            self.assertEqual(stop_tuple(r), ("credit_exhausted", "lane", "needs_owner", "none"), row["id"])
            self.assertIsNone(r["stop"]["retry"]["at"])
        self.assertIn("GROK_SUBSCRIPTION", route.load_state()["out"])

    def test_golden_the_22_oct_night_has_53_bad_requests_and_every_other_row_is_done(self):
        f = load(ROOT / "tests" / "contract" / "fixtures" / "night_22oct.json")
        self.assertEqual(len(f["rows"]), 268)
        tuples = [stop_tuple(self.answer(o, "OPENAI_SUBSCRIPTION")) if (o := replay(row, f)) else None for row in f["rows"]]
        self.assertEqual(tuples.count(("bad_request", "caller", "needs_owner", "none")), 53)
        self.assertEqual(tuples.count(None), 268 - 53)
        self.assertIsNone(route.load_state().get("out"))

    def test_credit_exhausted_marks_the_lane_with_no_retry_time(self):
        row = load(ROOT / "tests" / "contract" / "fixtures" / "grok_spending_limit_24oct.json")["rows"][0]
        o = replay(row, None)
        r = self.answer(o, "GROK_SUBSCRIPTION")
        out = route.load_state()["out"]["GROK_SUBSCRIPTION"]
        self.assertGreater(out["until"], T)                  # hidden from the picker (gather needs a time) and probed again later
        self.assertEqual((out["reason"], r["stop"]["scope"], r["stop"]["retry"]), ("credit", "lane", {"kind": "none", "at": None}))

    def test_a_credit_cycle_that_names_its_end_is_a_predicted_retry(self):
        r = self.answer(classify.Outcome("credit_exhausted", "credit balance is too low", "event"), "CLAUDE_CREDIT", credit_until=T + 86400)
        self.assertEqual((r["stop"]["permanence"], r["stop"]["retry"]), ("transient", {"kind": "predicted", "at": T + 86400}))

    def test_answers_that_wait_for_a_time_say_when(self):
        r = self.answer(classify.Outcome("rate_limited", "Usage limit reached for 5 hour", "event"), "ZAI_CODING_SUBSCRIPTION")
        self.assertEqual((r["stop"]["permanence"], r["stop"]["retry"]), ("transient", {"kind": "predicted", "at": T + 3600}))
        r = self.answer(classify.Outcome("logged_out", "Not logged in", "stderr"), "OPENAI_SUBSCRIPTION")
        self.assertEqual((r["stop"]["permanence"], r["stop"]["retry"]["kind"]), ("needs_owner", "none"))
        self.assertIn("codex login", r["stop"]["fix"])

    def test_killed_and_network_carry_resumable_and_partial_and_never_a_lane_mark(self):
        for reason in ("killed", "network"):
            res = {"ref": "thread-1", "started": True, "partial": True}
            route.settle(res, classify.Outcome(reason, "signal 15" if reason == "killed" else "ENOTFOUND", "exit"), {"lane": "OPENAI_SUBSCRIPTION"}, T, "cli", SimpleNamespace(timeout=30), 1, "")
            r = route.result(res.pop("outcome"), **res)
            self.assertEqual((r["stop"]["reason"], r["stop"]["resumable"], r["stop"]["partial"], r["stop"]["ref"]), (reason, True, True, "thread-1"))
        self.assertIsNone(route.load_state().get("out"))

    def test_an_unknown_reason_still_gets_a_row_of_closed_facts(self):
        s = classify.stop_facts("some_new_reason")
        self.assertEqual((s["scope"], s["permanence"], s["retry"]["kind"]), ("machine", "needs_owner", "none"))
        self.assertIsNone(route.schema_problem(dict(route.result("no_lane"), stop=dict(s, ref=None)), CONTRACT["answer_schema"]))

    def test_the_schema_refuses_an_answer_with_a_missing_or_wrong_field(self):
        good = route.result("no_lane")
        self.assertIsNone(route.schema_problem(good, CONTRACT["answer_schema"]))
        done = route.result()
        self.assertIsNone(route.schema_problem(done, CONTRACT["answer_schema"]))
        self.assertIsNone(done["stop"])
        for bad in (dict(good, stop=dict(good["stop"], scope="planet")), {k: v for k, v in good.items() if k != "usage"}, {k: v for k, v in good.items() if k != "stop"},
                    dict(good, contract=2), dict(good, denied=[{"kind": "whim", "tool": "Bash"}])):
            self.assertIsNotNone(route.schema_problem(bad, CONTRACT["answer_schema"]))

    def test_a_bad_flag_or_argument_of_run_is_a_bad_request_answer_and_other_commands_keep_exit_2(self):
        import io
        cases = [["run", "worker", "--nope"], ["run"], ["run", "worker", "--timeout", "soon"]]
        for argv in cases:
            with patch("sys.stdout", io.StringIO()) as out, patch.object(route, "interactive", lambda: False), self.assertRaises(SystemExit) as ex:
                route.main(argv)
            self.assertEqual(ex.exception.code, 0, argv)
            r = json.loads(out.getvalue())
            self.assertEqual(stop_tuple(r), ("bad_request", "caller", "needs_owner", "none"), argv)
            self.assertTrue(r["stop"]["fix"].startswith("update task-loop or delegate: "), argv)
            self.assertIsNone(route.schema_problem(r, CONTRACT["answer_schema"]), argv)
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as ex:
            route.main(["status", "--nope"])
        self.assertEqual(ex.exception.code, 2)

    def test_sandbox_limit_is_listed_and_never_counted(self):
        d = route.denial("sandbox_limit", "bash", {"command": "ps aux"})
        self.assertEqual((d["kind"], d["path"]), ("sandbox_limit", None))
        self.assertEqual(route.denial("secret", "bash", {"command": "cat ~/.ssh/id_rsa"})["path"], ".ssh")
        self.assertEqual(route.denial("policy", "edit", {"filePath": "x.py"})["path"], "x.py")
        self.assertEqual(sum(1 for x in [d, route.denial("policy", "bash", {})] if x.get("kind") != "sandbox_limit"), 1)

    def test_contract_3_has_no_wire_left(self):
        gone = ("v", "status", "reason", "next", "retry_after")
        for r in (route.result(), route.result("no_lane"), route.result("rate_limited", retry_after=T)):
            self.assertEqual(r["contract"], 3)
            for k in gone:
                self.assertNotIn(k, r)
        for k in ("wire_reasons", "wire_default"):
            self.assertNotIn(k, CONTRACT)
        for name, row in CONTRACT["reasons"].items():
            self.assertNotIn("wire", row, name)
        self.assertEqual(set(CONTRACT["answer_schema"]["properties"]) & set(gone), set())
        self.assertEqual(CONTRACT["denied_kinds"], ["policy", "sandbox_limit", "secret"])

    def test_the_old_wire_is_gone_from_the_source(self):
        for path in [*(ROOT / "skills" / "delegate").rglob("*.py"), ROOT / "skills" / "delegate" / "contract.json"]:
            text = path.read_text(encoding="utf-8")
            for word in ("to_wire_v2", "vendor_prework", "lane_out", "wire_reasons", "wire_default"):
                self.assertNotIn(word, text, f"{path.name}: {word}")

    def test_the_declared_facts_are_closed_lists(self):
        for reason, row in CONTRACT["reasons"].items():
            self.assertIn(row["scope"], CONTRACT["scopes"])
            self.assertIn(row["permanence"], CONTRACT["permanences"])
            self.assertIn(row["retry"], CONTRACT["retry_kinds"])
            self.assertIn(row["health"], CONTRACT["health_effects"])
        self.assertEqual(CONTRACT["contract"], 3)


class Docs(unittest.TestCase):
    """The words the orchestrator reads match the code (delegate-fvk): flags, reasons, no dead compat."""
    SRC = (ROOT / "skills" / "delegate" / "route.py").read_text(encoding="utf-8")
    FOREIGN = {"codex.md": {"--write", "--background", "--skip-git-repo-check"}}      # flags of other tools, named in prose

    def test_run_args_are_the_flags_of_run(self):
        import re
        self.assertEqual(set(re.findall(r'ru\.add_argument\("(--[a-z-]+)"', self.SRC)), set(CONTRACT["run_args"]))

    def test_docs_name_only_flags_argparse_knows(self):
        import re
        known = set(re.findall(r'add_argument\("(--[a-z-]+)"', self.SRC))
        docs = [ROOT / "skills" / "delegate" / "SKILL.md", *sorted((ROOT / "skills" / "delegate" / "modes").glob("*.md"))]
        for path in docs:
            for flag in set(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", path.read_text(encoding="utf-8"))):
                self.assertTrue(flag in known or flag in self.FOREIGN.get(path.name, ()), f"{path.name}: {flag} is not a route.py flag")

    def test_no_dead_compat_is_left(self):
        import re
        gate = re.compile(r"recent_runs|LANE_CONTEXT|\"features\"|no_sandbox|no.wait")
        for base in ("skills", "tests", "docs"):
            for path in (ROOT / base).rglob("*"):
                if path.is_file() and path.suffix in (".py", ".md", ".json") and "fixtures" not in path.parts and path.name != "test_classify.py":
                    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                        self.assertIsNone(gate.search(line), f"{path}:{n}")
        self.assertIsNone(re.search(r'\b(ru|n)\.add_argument\("--host"', self.SRC))      # only `set-key` and `net` take a host

    def test_the_prose_of_integrations_md_has_no_changelog(self):
        import re
        text = (ROOT / "docs" / "dev" / "integrations.md").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?i)\b(since|before) 0\.\d+", text))

    def test_the_extra_capacity_notes_are_not_in_the_plugin(self):
        self.assertFalse((ROOT / "extra-coding-capacity-2026-10.md").exists())


class Ladder(unittest.TestCase):
    def test_order_first_match_wins(self):
        c = classify.classify
        self.assertEqual(c(1, "x", "", True, False, {"secret": "cat .env"}).reason, "secrets")
        self.assertEqual(c(1, "", "", True, False, {"watchdog": "limit", "msg": "Usage limit"}).reason, "rate_limited")
        self.assertEqual(c(1, "", "", False, False, {"watchdog": "limit", "msg": "personal-team-blocked:spending-limit"}).reason, "credit_exhausted")
        self.assertEqual(c(-15, "", "", False, False, {"watchdog": "hung"}).reason, "hung")
        self.assertEqual(c(-15, "", "", False, False, {"watchdog": "hung", "net_up": False}).reason, "network")
        self.assertEqual(c(-15, "", "", True, False).reason, "timeout")
        self.assertEqual(c(1, "credit balance is too low", "", False, True).reason, "credit_exhausted")      # before the limit pattern that also matches it
        self.assertEqual(c(1, "", "401 Unauthorized", False, False).reason, "logged_out")
        self.assertEqual(c(1, 'HTTP {"status":404}', "", False, False).reason, "bad_request")
        self.assertEqual(c(1, 'HTTP {"status":429}', "", False, False).reason, "rate_limited")
        for text in ("Invalid schema for response_format 'x'", "error: unknown argument '--foo'", "codex: error: unrecognized arguments: --bar"):      # the engine's INFRA_TEXT, now the vendor's own bad_request
            self.assertEqual(c(2, "", text, False, False).reason, "bad_request", text)
        self.assertEqual(c(1, "getaddrinfo ENOTFOUND x", "", False, False).reason, "network")
        self.assertEqual(c(1, "getaddrinfo ENOTFOUND x", "", False, True).reason, "worker_failed")      # it had started: not a network start-up failure
        self.assertEqual(c(-9, "", "", False, True).reason, "killed")
        self.assertEqual(c(1, "boom", "", False, False).reason, "vendor_blip")
        self.assertEqual(c(1, "boom", "", False, True).reason, "worker_failed")
        self.assertEqual(c(2, "", "noise", False, False).reason, "worker_failed")
        self.assertIsNone(c(0, "", "login noise: not logged in", False, True))      # stderr of a good run is not read

    def test_http_status_from_the_cli_is_read_before_any_text(self):
        c = classify.classify
        self.assertEqual(c(1, "", "", False, False, {"http_status": 429}).reason, "rate_limited")
        self.assertEqual(c(1, "x", "", False, False, {"http_status": 401}).reason, "logged_out")
        self.assertEqual(c(1, "x", "", False, False, {"http_status": 400}).reason, "bad_request")

    def test_claude_error_text_is_the_clis_not_the_models(self):
        self.assertEqual(classify.claude_vendor_error({"is_error": True, "result": "API Error: 429 rate limit"}), "API Error: 429 rate limit")
        self.assertEqual(classify.claude_vendor_error({"is_error": True, "result": "x", "api_error_status": 529}), "x")
        self.assertEqual(classify.claude_vendor_error({"is_error": True, "result": "I added rate limit handling; quota tests fail with 429"}), "")
        self.assertEqual(classify.claude_vendor_error({"is_error": False, "result": "API Error: shown by the task itself"}), "")

    def test_one_patterns_block_one_copy_of_each_regex(self):
        src = (ROOT / "skills" / "delegate" / "route.py").read_text(encoding="utf-8")
        for name in ("LIMIT_RE", "LOGIN_RE", "NETWORK_RE", "SECRET_RE", "SPEND_RE"):
            self.assertNotIn(f"\n{name} = re.compile", src)
            self.assertIn(f"\n{name} = re.compile", (ROOT / "skills" / "delegate" / "lanes" / "classify.py").read_text(encoding="utf-8"))
        self.assertEqual(set(classify.PATTERNS), {"limit", "credit", "spend", "login", "network", "codex_status", "bad_request", "claude_error", "secret"})


class Launchers(unittest.TestCase):
    """The three real launch_* functions end in the same ladder; the CLI is replaced by run_proc feeding the events of a fixture row."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for p in (patch.object(route, "config_dir", return_value=Path(self.tmp.name)), patch.object(route, "agent_runs", None),
                  patch.object(route.shutil, "which", return_value="cli"), patch.object(route, "opencode_exe", return_value="opencode"),
                  patch.object(route.sys, "platform", "darwin"), patch.object(route, "opencode_shell"), patch.object(route, "child_env", return_value={}),
                  patch.object(route, "codex_thread_total", return_value=None), patch.object(route, "codex_child_tokens", return_value=({"in": 0, "cached": 0, "out": 0}, 0))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.a = SimpleNamespace(resume=None, schema=None, timeout=30, role="worker")

    def run_cli(self, fn, lines, rc, err="", lane="OPENAI_SUBSCRIPTION", kind="codex", **how):
        def fake(cmd, text, cwd, env, timeout, on_line=None, poll=None):
            for line in lines:
                if on_line:
                    on_line(json.dumps(line))
            return rc, "", err, False
        d = dict(lane=lane, model="m", effort="high", role="worker", how=dict(model="m", cwd=self.tmp.name, write=False, sandbox="read-only", cli=["codex", "exec"], isolation=[], **how))
        with patch.object(route, "run_proc", fake):
            res = fn(d, "brief", self.a, "rid12345", T, {}) if fn is not route.launch_claude else fn(d, "brief", self.a, "rid12345", T, route.load_routing(), {})
        res.pop("stderr", None)
        return res

    def opencode(self, lines, rc=1, **kw):
        return self.run_cli(route.launch_opencode, lines, rc, lane=kw.pop("lane", "GROK_SUBSCRIPTION"), kind="opencode", **kw)

    def test_a_grok_row_through_the_opencode_launcher_marks_the_lane_out_without_a_time(self):
        row = load(ROOT / "tests" / "contract" / "fixtures" / "grok_spending_limit_24oct.json")["rows"][0]
        res = self.opencode([{"type": "error", "error": {"name": "APIError", "data": {"message": row["evidence"]["vendor_err"]}}}])
        self.assertEqual((res["outcome"], res.get("retry_after")), ("credit_exhausted", None))
        self.assertIn("GROK_SUBSCRIPTION", route.load_state()["out"])

    def test_a_bad_request_row_through_the_codex_launcher_leaves_the_lane_alone(self):
        for row in load(ROOT / "tests" / "contract" / "fixtures" / "bad_request_22oct.json")["rows"][:3]:
            res = self.run_cli(route.launch_codex, [{"type": "turn.failed", "error": {"message": row["evidence"]["vendor_err"]}}], 1)
            self.assertEqual(res["outcome"], "bad_request")
        self.assertEqual(route.load_state().get("out"), None)

    def test_a_sigterm_with_no_error_is_killed_and_retried_not_failed(self):
        res = self.opencode([{"type": "step_finish", "part": {"tokens": {"input": 5}}}], rc=-15)
        self.assertEqual(res["outcome"], "killed")
        self.assertNotIn("retry_after", res)
        self.assertEqual(route.load_state().get("out"), None)

    def test_codex_stream_retries_do_not_fail_a_run_that_finished(self):
        res = self.run_cli(route.launch_codex, [{"type": "error", "message": "Reconnecting... 2/5 (idle timeout waiting for websocket)"},
                                                {"type": "item.completed", "item": {"type": "agent_message", "text": "all done"}},
                                                {"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 1}}], 0)
        self.assertEqual((res.get("outcome"), res["text"], res["turns"]), (None, "all done", 1))
        res = self.run_cli(route.launch_codex, [{"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "status": "completed"}},
                                                {"type": "error", "message": "Reconnecting... 5/5 (websocket closed by server)"}], 1)
        self.assertEqual(res["outcome"], "worker_failed")
        self.assertIn("Reconnecting... 5/5", res["say"])

    def test_a_workers_own_words_about_a_rate_limit_never_take_a_lane_out(self):
        words = "The batch is queued behind a rate limit on the Unity side; quota 429 seen in the log."
        res = self.run_cli(route.launch_codex, [{"type": "item.completed", "item": {"type": "agent_message", "text": words}}], 1)
        self.assertEqual(res["outcome"], "worker_failed")
        res = self.run_cli(route.launch_claude, [{"type": "result", "is_error": True, "result": words, "num_turns": 6, "session_id": "s1"}], 1, lane="CLAUDE_SUBSCRIPTION", kind="claude_p",
                           cap=1, read_only=False)
        self.assertEqual(res["outcome"], "worker_failed")
        res = self.opencode([{"type": "text", "part": {"text": words}}], rc=1)
        self.assertEqual(res["outcome"], "worker_failed")
        self.assertEqual(route.load_state().get("out"), None)

    def test_the_clis_own_error_still_does(self):
        res = self.run_cli(route.launch_claude, [{"type": "result", "is_error": True, "result": "API Error: 429 rate limit exceeded", "num_turns": 1}], 1,
                           lane="CLAUDE_SUBSCRIPTION", kind="claude_p", cap=1, read_only=False)
        self.assertEqual(res["outcome"], "rate_limited")
        self.assertIn("CLAUDE_SUBSCRIPTION", route.load_state()["out"])

    def test_additive_usage_facts_come_from_the_events(self):
        res = self.opencode([{"type": "step_finish", "part": {"tokens": {"input": 1000, "output": 5, "cache": {"read": 3000, "write": 0}}}},
                             {"type": "step_finish", "part": {"tokens": {"input": 2000, "output": 5, "cache": {"read": 4000, "write": 0}}}},
                             {"type": "text", "part": {"text": "hi"}}], rc=0)
        self.assertEqual((res.get("outcome"), res["turns"], res["context_k"]), (None, 2, 6.0))

    def test_a_runner_kind_that_is_not_a_launcher_is_a_setup_error(self):
        kinds = {c["runner"] for c in route.load_routing()["lanes"].values()}
        self.assertEqual(set(route.LAUNCHERS), {"codex", "opencode", "claude_p"})
        self.assertLessEqual(kinds, {"codex", "opencode", "claude_p", "agent_tool"})
        d = {"action": "run", "role": "worker", "lane": "OPENAI_SUBSCRIPTION", "model": "m", "effort": "high", "mode": "write", "how": {"kind": "codexx", "cwd": self.tmp.name}}
        a = argparse.Namespace(role="worker", authored_by=None, failed="", premium=None, allow_degraded=False, claude_usage=None, cwd=None, resume=None, cap=None,
                               brief=None, timeout=5, unattended=True, schema=None, parent=None, task=None, title=None, call_id="c#1")
        import io
        with patch.object(route, "decision", return_value=(d, {}, [], T)), patch.object(route, "interactive", return_value=False), patch("sys.stdout", io.StringIO()) as out:
            route.run_once(a, route.load_routing())
        r = json.loads(out.getvalue())
        self.assertEqual((r["stop"]["reason"], r["call_id"]), ("setup", "c#1"))
        self.assertIn("codexx", r["say"])


if __name__ == "__main__":
    unittest.main()
