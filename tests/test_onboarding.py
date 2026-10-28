"""Onboarding, onboarding wizard and its child environment. Run: python -m unittest discover -s tests"""
import hermetic  # noqa: F401  (a home of its own for every test)
import os
import sys
import tempfile
from unittest import mock
import unittest
from pathlib import Path
from test_route import route, T, argparse_ns
from delegate.adapters.harness import opencode_sandbox as box


class Onboarding(unittest.TestCase):
    """Run against an empty fake home so nothing depends on the machine's real setup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA", "DELEGATE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_CREDIT_WORKER_KEY")}
        for k in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA", "DELEGATE_CONFIG_DIR"):
            os.environ[k] = self.tmp.name
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_CREDIT_WORKER_KEY", None)
        import onboard
        self.onboard = onboard
        self.which = onboard.shutil.which
        onboard.shutil.which = lambda name: None
        route.shutil.which = lambda name: None
        self.cfg = route.load_routing()

    def tearDown(self):
        self.onboard.shutil.which = self.which
        route.shutil.which = self.which
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_empty_machine_is_ready_with_lanes_on_offer_and_asking_for_one_gives_its_first_step(self):
        d = self.onboard.doctor(route, self.cfg)
        self.assertTrue(d["ready"])                                   # sub-agent-only is a complete setup
        self.assertTrue(d["lanes"]["CLAUDE_SUBSCRIPTION"])
        self.assertFalse(d["lanes"]["OPENAI_SUBSCRIPTION"])
        self.assertTrue(all(c.get("available") for c in d["checks"] if not c["ok"] and not c.get("optional")))
        want = self.onboard.doctor(route, self.cfg, lane="OPENAI_SUBSCRIPTION")
        self.assertFalse(want["ready"])
        step = self.onboard.next_step(want)
        self.assertEqual((step["step"], step["who"], step["do"]), ("codex_cli", "agent", "codex_cli"))

    def test_deep_doctor_validates_the_key_with_one_live_request(self):
        with mock.patch.object(route, "find_key", return_value="sk-x"), mock.patch.object(self.onboard, "validate_key", return_value=(False, "401")) as vk:
            d = self.onboard.doctor(route, self.cfg, deep=True)
        vk.assert_called_once_with("sk-x")
        self.assertIn("401", next(c for c in d["checks"] if c["id"] == "credit_key_valid")["say"])

    def test_decline_both_lanes_makes_it_ready(self):
        self.onboard.act(route, self.cfg, decline="OPENAI_SUBSCRIPTION")
        self.onboard.act(route, self.cfg, decline="CLAUDE_CREDIT")
        self.assertTrue(self.onboard.doctor(route, self.cfg)["ready"])
        self.assertNotIn("step", self.onboard.next_step(self.onboard.doctor(route, self.cfg)))     # nothing required is left

    def _gather(self, st=None):
        real = dict(route.READERS)
        route.READERS.update({k: (lambda lane, S, st_, t, *a: None) for k in real})
        try:
            return route.fleet_builder(self.cfg).build(route.load_state(), T).lanes
        finally:
            route.READERS.update(real)

    def _make_codex_ready(self):
        self.onboard.shutil.which = lambda name: "/x/codex" if name == "codex" else None
        home = Path(os.environ["HOME"]) / ".codex"
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text("{}")

    def test_fresh_machine_is_sub_agent_only_with_no_nag(self):
        ls = self._gather()
        self.assertEqual(list(ls), ["CLAUDE_SUBSCRIPTION"])
        self.assertEqual(route.profile_lanes(), {})
        self.assertEqual(route.drift_warnings(route.states(), ls, route.load_state(), T), [])

    def test_ready_lane_is_enabled_once_and_remembered(self):
        self._make_codex_ready()
        self.assertIn("OPENAI_SUBSCRIPTION", self._gather())
        self.assertEqual(route.profile_lanes()["OPENAI_SUBSCRIPTION"], "enabled")

    def test_declined_lane_stays_out_even_when_ready(self):
        self._make_codex_ready()
        route.set_profile_lane("OPENAI_SUBSCRIPTION", "declined")
        self.assertNotIn("OPENAI_SUBSCRIPTION", self._gather())

    def test_enabled_but_no_longer_ready_is_out_and_warns_once_a_day(self):
        route.set_profile_lane("OPENAI_SUBSCRIPTION", "enabled")          # nothing on this fake machine
        ls = self._gather()
        self.assertTrue(ls["OPENAI_SUBSCRIPTION"].out)
        self.assertEqual(len(route.drift_warnings(route.states(), ls, route.load_state(), T)), 1)
        self.assertEqual(route.drift_warnings(route.states(), ls, route.load_state(), T + 60), [])

    def test_usage_is_requested_only_on_desktop(self):
        ls = self._gather()
        saved = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        try:
            os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
            self.assertEqual([n["what"] for n in route.needs_input(ls)], ["claude_usage"])
            os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
            self.assertEqual(route.needs_input(ls), [])
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT")
            self.assertEqual(route.needs_input(ls), [])
        finally:
            if saved is not None:
                os.environ["CLAUDE_CODE_ENTRYPOINT"] = saved

    def test_set_key_web_takes_the_key_once_and_stays_private(self):
        import threading
        import urllib.error
        import urllib.parse
        import urllib.request
        self.onboard.validate_key = lambda k: (True, "HTTP 200")
        box = {}
        ready = threading.Event()

        def on_ready(url):
            box["url"] = url
            ready.set()
        th = threading.Thread(target=lambda: box.__setitem__("code", self.onboard.set_key_web(route, timeout=20, on_ready=on_ready)))
        th.start()
        self.assertTrue(ready.wait(15))
        url = box["url"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:                     # wrong token
            urllib.request.urlopen(url[:-3] + "xxx")
        self.assertEqual(ctx.exception.code, 404)
        self.assertIn(b"password", urllib.request.urlopen(url).read())              # the form
        data = urllib.parse.urlencode({"key": "sk-ant-web-key"}).encode()
        self.assertIn(b"Saved", urllib.request.urlopen(url, data=data).read())
        th.join(5)
        self.assertEqual(box["code"], 0)
        path = route.config_dir() / "claude-credit.key"
        self.assertEqual(path.read_text().strip(), "sk-ant-web-key")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(route.profile_lanes()["CLAUDE_CREDIT"], "enabled")

    def test_set_key_web_refuses_public_and_wildcard_hosts(self):
        for h in ("0.0.0.0", "8.8.8.8"):
            self.assertEqual(self.onboard.set_key_web(route, host=h, timeout=1), 2)
        self.assertTrue(self.onboard.allowed_host("100.101.102.103"))      # tailnet
        self.assertTrue(self.onboard.allowed_host("192.168.1.5"))


    def test_every_missing_item_says_what_to_do_and_ack_clears_it(self):
        import io
        d = self.onboard.doctor(route, self.cfg, lane="OPENAI_SUBSCRIPTION")
        self.assertFalse(d["ready"])
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            self.onboard.print_doctor(d, quiet=False)
        finally:
            sys.stdout = real
        text = buf.getvalue()
        self.assertIn("MISSING   codex_cli", text)
        self.assertIn("-> Install the Codex CLI.", text)
        self.assertIn("doctor --decline LANE", text)
        route.set_ack("codex_cli")
        route.set_ack("codex_login")
        d2 = self.onboard.doctor(route, self.cfg, lane="OPENAI_SUBSCRIPTION")
        self.assertTrue(next(c for c in d2["checks"] if c["id"] == "codex_cli")["acked"])

    def test_volumes_note_is_ackable(self):
        import unittest.mock as m
        with m.patch.object(self.onboard.os.path, "exists", lambda p: p == "/.dockerenv"):
            self.assertEqual([n["id"] for n in self.onboard.doctor(route, self.cfg)["notes"]], ["volumes"])
            route.set_ack("volumes")
            self.assertEqual(self.onboard.doctor(route, self.cfg)["notes"], [])

    def test_unset_lanes_are_offered_once_and_never_block(self):
        a = argparse_ns("worker")
        saved = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"                    # a Claude session: the Agent tool is available
        try:
            d1 = route.decision(a, self.cfg)[0]
            self.assertEqual(d1["action"], "run")
            self.assertIn("OPENAI_SUBSCRIPTION", d1["notice"])
            self.assertNotIn("notice", route.decision(a, self.cfg)[0])      # said once
        finally:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None) if saved is None else os.environ.__setitem__("CLAUDE_CODE_ENTRYPOINT", saved)

    def test_set_key_enables_a_declined_credit_lane(self):
        import io
        self.onboard.validate_key = lambda k: (True, "HTTP 200")
        route.set_profile_lane("CLAUDE_CREDIT", "declined")
        real = sys.stdin
        try:
            sys.stdin = io.StringIO("sk-ant-test-key\n")
            self.onboard.set_key(route, from_stdin=True)
        finally:
            sys.stdin = real
        self.assertEqual(route.profile_lanes()["CLAUDE_CREDIT"], "enabled")

    def test_config_set_and_show(self):
        self.onboard.config_set(route, "credit.reset_day", "15")
        self.onboard.config_set(route, "claude_weekly_reset", "Sun 17:00")
        cfg = route.load_routing()
        self.assertEqual(cfg["settings"]["credit"]["reset_day"], 15)
        self.assertEqual(cfg["settings"]["claude_weekly_reset"], "Sun 17:00")
        self.assertEqual(cfg["settings"]["credit"]["monthly_cap"], 150)     # untouched defaults survive the merge

    def test_set_key_stdin_is_private_and_not_overwritten(self):
        import io
        self.onboard.validate_key = lambda k: (True, "HTTP 200")
        real = sys.stdin
        try:
            sys.stdin = io.StringIO("sk-ant-test-key\n")
            self.assertEqual(self.onboard.set_key(route, from_stdin=True), 0)
            path = route.config_dir() / "claude-credit.key"
            self.assertEqual(path.read_text().strip(), "sk-ant-test-key")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o077, 0)
            sys.stdin = io.StringIO("sk-ant-other\n")
            self.assertEqual(self.onboard.set_key(route, from_stdin=True), 1)
            self.assertEqual(path.read_text().strip(), "sk-ant-test-key")
        finally:
            sys.stdin = real
        self.assertEqual(route.find_key(quiet=True), "sk-ant-test-key")


class OnboardChildEnv(unittest.TestCase):
    def test_child_clis_run_without_the_claude_app_variables(self):
        import onboard
        from unittest import mock
        seen = {}
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "http://x", "CLAUDE_CODE_OAUTH_SCOPES": "s", "KEEP_ME": "1"}), \
                mock.patch.object(onboard.subprocess, "run", side_effect=lambda argv, **kw: seen.update(kw["env"]) or mock.Mock(returncode=0, stdout="", stderr="")):
            onboard.run_cmd(["claude", "auth", "status"])
        self.assertNotIn("ANTHROPIC_BASE_URL", seen)
        self.assertNotIn("CLAUDE_CODE_OAUTH_SCOPES", seen)
        self.assertEqual(seen["KEEP_ME"], "1")


if __name__ == "__main__":
    unittest.main()
