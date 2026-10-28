"""Onboarding, onboarding wizard and its child environment. Run: python -m unittest discover -s tests"""
import hermetic  # noqa: F401  (a home of its own for every test)
import os
import sys
import tempfile
import unittest
from pathlib import Path
from test_route import route, T, argparse_ns
from delegate.adapters.harness import opencode_sandbox as box
from delegate.app import env
from delegate.app import usage
from delegate.app import cockpit
from delegate.app import harnesses
from delegate.app import decide as dec
from delegate.app import fleet
import shutil
from delegate.app import doctor, keys


class Onboarding(unittest.TestCase):
    """Run against an empty fake home so nothing depends on the machine's real setup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA", "DELEGATE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_CREDIT_WORKER_KEY")}
        for k in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA", "DELEGATE_CONFIG_DIR"):
            os.environ[k] = self.tmp.name
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_CREDIT_WORKER_KEY", None)
        self.which = shutil.which
        shutil.which = lambda name: None
        shutil.which = lambda name: None
        self.cfg = env.load_routing()

    def tearDown(self):
        shutil.which = self.which
        shutil.which = self.which
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()




    def test_doctor_is_a_plain_lane_table_and_nothing_blocks(self):
        rows = {r["lane"]: r for r in doctor.doctor(self.cfg)["lanes"]}
        self.assertEqual(list(rows), list(self.cfg["lanes"]))
        self.assertEqual((rows["CLAUDE_SUBSCRIPTION"]["enabled"], rows["CLAUDE_SUBSCRIPTION"]["ready"], rows["CLAUDE_SUBSCRIPTION"]["fix"]), (True, True, ""))
        o = rows["OPENAI_SUBSCRIPTION"]
        self.assertEqual((o["enabled"], o["ready"]), (True, False))
        self.assertIn("npm install -g @openai/codex", o["fix"])
        self.assertFalse(rows["DEEPSEEK_FREE"]["enabled"])                            # an opt-in lane is off until its setting is true
        self.assertIn("deepseek_free_enabled", rows["DEEPSEEK_FREE"]["fix"])
        self._make_codex_ready()
        o = {r["lane"]: r for r in doctor.doctor(self.cfg)["lanes"]}["OPENAI_SUBSCRIPTION"]
        self.assertEqual((o["ready"], o["fix"]), (True, ""))
        env.set_profile_lane("OPENAI_SUBSCRIPTION", "declined")
        self.assertFalse({r["lane"]: r for r in doctor.doctor(self.cfg)["lanes"]}["OPENAI_SUBSCRIPTION"]["enabled"])

    def test_the_table_prints_one_line_per_lane(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(route.main(["doctor"]), 0)
            self.assertEqual(route.main(["init"]), 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2 * (len(self.cfg["lanes"]) + 1))
        self.assertTrue(lines[0].startswith("lane") and "enabled" in lines[0] and "ready" in lines[0] and "fix" in lines[0])

    def _gather(self, st=None):
        real = dict(usage.READERS)
        usage.READERS.update({k: (lambda lane, S, st_, t, *a: None) for k in real})
        try:
            return dec.fleet_builder(self.cfg).build(env.load_state(), T).lanes
        finally:
            usage.READERS.update(real)

    def _make_codex_ready(self):
        shutil.which = lambda name: "/x/codex" if name == "codex" else None
        home = Path(os.environ["HOME"]) / ".codex"
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text("{}")

    def test_fresh_machine_is_sub_agent_only_with_no_nag(self):
        ls = self._gather()
        self.assertEqual(list(ls), ["CLAUDE_SUBSCRIPTION"])
        self.assertEqual(env.profile_lanes(), {})
        self.assertEqual(fleet.drift_warnings(env.states(), ls, env.load_state(), T), [])

    def test_ready_lane_is_enabled_once_and_remembered(self):
        self._make_codex_ready()
        self.assertIn("OPENAI_SUBSCRIPTION", self._gather())
        self.assertEqual(env.profile_lanes()["OPENAI_SUBSCRIPTION"], "enabled")

    def test_declined_lane_stays_out_even_when_ready(self):
        self._make_codex_ready()
        env.set_profile_lane("OPENAI_SUBSCRIPTION", "declined")
        self.assertNotIn("OPENAI_SUBSCRIPTION", self._gather())

    def test_enabled_but_no_longer_ready_is_out_and_warns_once_a_day(self):
        env.set_profile_lane("OPENAI_SUBSCRIPTION", "enabled")          # nothing on this fake machine
        ls = self._gather()
        self.assertTrue(ls["OPENAI_SUBSCRIPTION"].out)
        self.assertEqual(len(fleet.drift_warnings(env.states(), ls, env.load_state(), T)), 1)
        self.assertEqual(fleet.drift_warnings(env.states(), ls, env.load_state(), T + 60), [])

    def test_usage_is_requested_only_on_desktop(self):
        ls = self._gather()
        saved = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        try:
            os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
            self.assertEqual([n["what"] for n in cockpit.needs_input(ls)], ["claude_usage"])
            os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
            self.assertEqual(cockpit.needs_input(ls), [])
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT")
            self.assertEqual(cockpit.needs_input(ls), [])
        finally:
            if saved is not None:
                os.environ["CLAUDE_CODE_ENTRYPOINT"] = saved

    def test_set_key_web_takes_the_key_once_and_stays_private(self):
        import threading
        import urllib.error
        import urllib.parse
        import urllib.request
        keys.validate_key = lambda k: (True, "HTTP 200")
        box = {}
        ready = threading.Event()

        def on_ready(url):
            box["url"] = url
            ready.set()
        th = threading.Thread(target=lambda: box.__setitem__("code", keys.set_key_web(timeout=20, on_ready=on_ready)))
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
        path = env.config_dir() / "claude-credit.key"
        self.assertEqual(path.read_text().strip(), "sk-ant-web-key")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(env.profile_lanes()["CLAUDE_CREDIT"], "enabled")

    def test_set_key_web_refuses_public_and_wildcard_hosts(self):
        for h in ("0.0.0.0", "8.8.8.8"):
            self.assertEqual(keys.set_key_web(host=h, timeout=1), 2)
        self.assertTrue(keys.allowed_host("100.101.102.103"))      # tailnet
        self.assertTrue(keys.allowed_host("192.168.1.5"))




    def test_unset_lanes_are_offered_once_and_never_block(self):
        a = argparse_ns("worker")
        saved = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"                    # a Claude session: the Agent tool is available
        try:
            d1 = dec.decision(a, self.cfg)[0]
            self.assertEqual(d1["action"], "run")
            self.assertIn("OPENAI_SUBSCRIPTION", d1["notice"])
            self.assertNotIn("notice", dec.decision(a, self.cfg)[0])      # said once
        finally:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None) if saved is None else os.environ.__setitem__("CLAUDE_CODE_ENTRYPOINT", saved)

    def test_set_key_enables_a_declined_credit_lane(self):
        import io
        keys.validate_key = lambda k: (True, "HTTP 200")
        env.set_profile_lane("CLAUDE_CREDIT", "declined")
        real = sys.stdin
        try:
            sys.stdin = io.StringIO("sk-ant-test-key\n")
            keys.set_key(from_stdin=True)
        finally:
            sys.stdin = real
        self.assertEqual(env.profile_lanes()["CLAUDE_CREDIT"], "enabled")

    def test_config_set_and_show(self):
        doctor.config_set("credit.reset_day", "15")
        doctor.config_set("claude_weekly_reset", "Sun 17:00")
        cfg = env.load_routing()
        self.assertEqual(cfg["settings"]["credit"]["reset_day"], 15)
        self.assertEqual(cfg["settings"]["claude_weekly_reset"], "Sun 17:00")
        self.assertEqual(cfg["settings"]["credit"]["monthly_cap"], 150)     # untouched defaults survive the merge

    def test_set_key_stdin_is_private_and_not_overwritten(self):
        import io
        keys.validate_key = lambda k: (True, "HTTP 200")
        real = sys.stdin
        try:
            sys.stdin = io.StringIO("sk-ant-test-key\n")
            self.assertEqual(keys.set_key(from_stdin=True), 0)
            path = env.config_dir() / "claude-credit.key"
            self.assertEqual(path.read_text().strip(), "sk-ant-test-key")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o077, 0)
            sys.stdin = io.StringIO("sk-ant-other\n")
            self.assertEqual(keys.set_key(from_stdin=True), 1)
            self.assertEqual(path.read_text().strip(), "sk-ant-test-key")
        finally:
            sys.stdin = real
        self.assertEqual(harnesses.find_key(quiet=True), "sk-ant-test-key")


if __name__ == "__main__":
    unittest.main()
