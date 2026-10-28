"""The lane cockpit: one row per known lane, an emoji for each state, a plain reason, the unlock time, usage windows with their source."""
import hermetic  # noqa: F401  (a home of its own for every test)
import copy
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import routing_support as support
from test_route import route, CFG, lanes, T
from delegate.app import onboard
from delegate.app import env
from delegate.app import usage
from delegate.app import cockpit
from delegate.app import harnesses
from delegate.domain.time import spec
from delegate.contracts import history as hist
from delegate.domain.lanes.lane import Lane
from delegate.contracts.stores import locked
from delegate.app import status
from delegate.adapters.harness.opencode import OpenCodeHarness

HOUR = 3600


def cfg_all():
    cfg = copy.deepcopy(CFG)
    cfg["settings"].update(deepseek_free_enabled=True, zai_coding_enabled=True)
    return cfg


def lane(name, **over):
    c = CFG["lanes"][name]
    l = Lane(name, c["family"], c["kind"], c.get("shared_with_orchestrator", False))
    for k, v in over.items():
        setattr(l, k, v)
    return l


def view(name, l, st=None, history=(), login=(True, "ok", T), cfg=None, off=None):
    return cockpit.lane_view(name, cfg or cfg_all(), l, support.doc_of(st or {}), T, history, 0, login, off)


class Emoji(unittest.TestCase):
    def test_ready_with_a_live_window(self):
        l = lane("OPENAI_SUBSCRIPTION", week=3.0, week_reset=T + 6 * 86400)
        usage.add_window(l, 3.0, T + 6 * 86400, 10080, "live read (codex app-server)", T - 120)
        v = view("OPENAI_SUBSCRIPTION", l)
        self.assertEqual((v["state"], v["emoji"], v["reason"]), ("ready", "✅", "ready"))
        w = v["windows"][0]
        self.assertEqual((w["name"], w["used_pct"], w["source"], w["age_s"], w["resets_in_s"]), ("weekly", 3.0, "live read (codex app-server)", 120, 6 * 86400))
        self.assertFalse(v["unlock"]["locked"])

    def test_resting_on_a_limit_names_the_unlock_in_local_time_and_duration(self):
        l = lane("OPENAI_SUBSCRIPTION", out=True, out_until=T + 2 * HOUR + 10 * 60, out_reason="limit", week=99.0)
        v = view("OPENAI_SUBSCRIPTION", l, {"out": {"OPENAI_SUBSCRIPTION": {"until": l.out_until, "why": "weekly limit", "at": T}}})
        self.assertEqual((v["state"], v["emoji"], v["reason"]), ("resting", "⏳", "weekly limit hit"))
        self.assertTrue(v["unlock"]["known"])
        self.assertEqual(v["unlock"]["in_s"], 2 * HOUR + 600)
        self.assertEqual(v["unlock"]["text"], f"{spec.local(l.out_until)} (in 2h 10m)")

    def test_an_old_state_entry_without_a_typed_reason_is_still_resting_until_its_time(self):
        rec = {"until": T + 5 * HOUR, "why": "free usage limit still reached at 18:45; opencode hangs 47 min instead of exiting", "at": T - HOUR}
        l = lane("DEEPSEEK_FREE", out=True, out_until=rec["until"], out_reason="error", notes=[rec["why"]])
        v = view("DEEPSEEK_FREE", l, {"out": {"DEEPSEEK_FREE": rec}})
        self.assertEqual((v["state"], v["reason"]), ("resting", "free usage limit reached"))
        self.assertEqual(v["unlock"]["at"], rec["until"])

    def test_limit_without_a_reset_time_says_unknown_and_why(self):
        l = lane("OPENAI_SUBSCRIPTION", out=True, out_until=None, out_reason="limit")
        v = view("OPENAI_SUBSCRIPTION", l)
        self.assertEqual(v["state"], "resting")
        self.assertEqual((v["unlock"]["locked"], v["unlock"]["known"], v["unlock"]["why"]), (True, False, "the vendor gave no reset time"))

    def test_logged_out_by_the_real_check_even_with_no_limit_recorded(self):
        v = view("OPENAI_SUBSCRIPTION", lane("OPENAI_SUBSCRIPTION", week=1.0), login=(False, "Not logged in", T))
        self.assertEqual((v["state"], v["emoji"], v["reason"]), ("logged_out", "🔑", "logged out: run `codex login`"))
        self.assertFalse(v["unlock"]["known"])
        self.assertIn("log in", v["unlock"]["why"])

    def test_logged_out_from_a_drifted_lane(self):
        l = lane("GROK_SUBSCRIPTION", out=True, out_reason="logged_out", notes=["enabled but not ready (login or key gone?)"])
        v = view("GROK_SUBSCRIPTION", l, login=(None, "", None))
        self.assertEqual((v["state"], v["emoji"]), ("logged_out", "🔑"))

    def test_spending_limit_old_entry_without_a_reason(self):
        rec = {"until": T + 3 * HOUR, "why": "spending-limit: out of credits (personal-team-blocked); top up then report --event ok", "at": T - 60}
        l = lane("GROK_SUBSCRIPTION", out=True, out_until=rec["until"], out_reason="error", notes=[rec["why"]])
        v = view("GROK_SUBSCRIPTION", l, {"out": {"GROK_SUBSCRIPTION": {"until": rec["until"], "why": rec["why"], "at": rec["at"]}}})
        self.assertEqual((v["state"], v["emoji"]), ("spend", "💸"))
        self.assertTrue(v["unlock"]["known"])

    def test_credit_lane_over_its_cap_is_a_spend_limit_resting_until_the_cycle_resets(self):
        l = lane("CLAUDE_CREDIT", out=True, out_until=T + 86400, out_reason="limit", cap=150.0, spend=150.0, notes=["own cycle cap reached"])
        v = view("CLAUDE_CREDIT", l)
        self.assertEqual((v["state"], v["emoji"]), ("spend", "💸"))
        self.assertEqual(v["unlock"]["in_s"], 86400)

    def test_codex_spend_control_is_a_spend_limit_without_a_timer(self):
        l = lane("OPENAI_SUBSCRIPTION", out=True, out_until=None, out_reason="limit", spend_blocked=True)
        v = view("OPENAI_SUBSCRIPTION", l)
        self.assertEqual(v["state"], "spend")
        self.assertIn("no timer", v["unlock"]["why"])

    def test_hung_and_benched_is_slow_with_its_end_time(self):
        l = lane("GROK_SUBSCRIPTION", out=True, out_until=T + 1800, out_reason="hung")
        v = view("GROK_SUBSCRIPTION", l, {"out": {"GROK_SUBSCRIPTION": {"until": T + 1800, "why": "hung: no output within 120s", "at": T, "reason": "hung"}}})
        self.assertEqual((v["state"], v["emoji"]), ("slow", "🐢"))
        self.assertEqual(v["unlock"]["in_s"], 1800)

    def test_a_recent_hang_with_no_good_run_since_is_slow_but_not_locked(self):
        hist = [{"lane": "GROK_SUBSCRIPTION", "done": True, "outcome": None, "ts": T - 5 * HOUR},
                {"lane": "GROK_SUBSCRIPTION", "outcome": "hung", "ts": T - 600}]
        v = view("GROK_SUBSCRIPTION", lane("GROK_SUBSCRIPTION"), history=hist)
        self.assertEqual((v["state"], v["emoji"], v["unlock"]["locked"]), ("slow", "🐢", False))
        hist.append({"lane": "GROK_SUBSCRIPTION", "done": True, "outcome": None, "ts": T - 60})
        self.assertEqual(view("GROK_SUBSCRIPTION", lane("GROK_SUBSCRIPTION"), history=hist)["state"], "ready")

    def test_disabled_and_not_configured(self):
        cfg = copy.deepcopy(CFG)
        for name, text in (("DEEPSEEK_FREE", "disabled: `route.py config set deepseek_free_enabled true` turns it on"),):
            v = view(name, None, cfg=cfg, off=cockpit.off_reason(name, cfg, (True, "", T)))
            self.assertEqual((v["state"], v["emoji"], v["reason"]), ("off", "⛔", text))
            self.assertEqual(v["model"], "deepseek-v4.1-flash-free high")        # still shows what it would use
        with patch.object(env, "profile_lanes", return_value={}):
            self.assertEqual(cockpit.off_reason("OPENAI_SUBSCRIPTION", cfg_all(), (None, "", None)), "not configured: `route.py doctor` says what it needs")
            self.assertEqual(cockpit.off_reason("OPENAI_SUBSCRIPTION", cfg_all(), (False, "", None)), "not configured: logged out, run `codex login`")
        with patch.object(env, "profile_lanes", return_value={"GROK_SUBSCRIPTION": "declined"}):
            self.assertIn("declined", cockpit.off_reason("GROK_SUBSCRIPTION", cfg_all(), (True, "", None)))

    def test_unknown_when_a_metered_lane_has_no_reading(self):
        v = view("CLAUDE_SUBSCRIPTION", lane("CLAUDE_SUBSCRIPTION", shared=True))
        self.assertEqual((v["state"], v["emoji"]), ("unknown", "❓"))
        self.assertEqual(view("CLAUDE_SUBSCRIPTION", lane("CLAUDE_SUBSCRIPTION", week=1.0, shared=True), login=(None, "claude CLI not installed", None))["state"], "unknown")

    def test_a_vendor_without_a_meter_is_ready_not_unknown(self):
        v = view("GROK_SUBSCRIPTION", lane("GROK_SUBSCRIPTION"))
        self.assertEqual(v["state"], "ready")
        self.assertFalse(v["meter"])

    def test_low_tokens_over_the_gate_but_under_the_ceiling_stays_ready_with_a_warning(self):
        l = lane("OPENAI_SUBSCRIPTION", week=96.0)
        usage.add_window(l, 96.0, T + 86400, 10080, "live read", T)
        v = view("OPENAI_SUBSCRIPTION", l)
        self.assertEqual(v["state"], "ready")
        self.assertEqual(v["low_tokens"][0]["over"], "gate")
        self.assertIn("low tokens: weekly 96% is over the lane gate 95%", v["reason"])

    def test_over_the_hard_ceiling_rests_until_that_window_resets(self):
        l = lane("OPENAI_SUBSCRIPTION", week=99.0, week_reset=T + 3 * HOUR)
        usage.add_window(l, 99.0, T + 3 * HOUR, 10080, "live read", T)
        v = view("OPENAI_SUBSCRIPTION", l)
        self.assertEqual((v["state"], v["low_tokens"][0]["over"]), ("resting", "ceiling"))
        self.assertEqual(v["unlock"]["in_s"], 3 * HOUR)

    def test_every_state_has_its_own_emoji(self):
        self.assertEqual(len(set(cockpit.EMOJI.values())), 7)
        self.assertEqual(set(cockpit.EMOJI), {"ready", "resting", "logged_out", "spend", "slow", "off", "unknown"})


class Fmt(unittest.TestCase):
    def test_fmt_in(self):
        self.assertEqual([spec.fmt_in(s) for s in (20, 45 * 60, 2 * HOUR + 600, 6 * 86400 + 21 * HOUR)], ["<1m", "45m", "2h 10m", "6d 21h"])


class Readers(unittest.TestCase):
    def test_the_credit_reader_estimates_from_the_ledger_and_the_claude_reader_marks_its_source(self):
        S = CFG["settings"]
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"DELEGATE_CONFIG_DIR": d}):
            env.ledger_add(30.0, "m", T - 3600)
            l = lane("CLAUDE_CREDIT")
            with patch.object(env, "now", return_value=T):
                usage.read_claude_credit(l, env.settings_of(CFG), support.doc_of({}), T)
            w = l.windows[0]
            self.assertEqual((w["name"], round(w["used"]), w["source"], w["at"]), ("monthly", 20, "estimated from history (this tool's ledger)", T - 3600))
            c = lane("CLAUDE_SUBSCRIPTION", shared=True)
            with patch.object(usage, "claude_logged_out", return_value=False), patch.object(env, "update_state"):
                usage.read_claude_subscription(c, env.settings_of(CFG), support.doc_of({}), T, {"plan": {"windows": [{"label": "5 hour", "percentUsed": 12, "resetsAt": T + HOUR}, {"label": "All weekly", "percentUsed": 40, "resetsAt": T + 86400}]}})
            self.assertEqual([(w["name"], w["used"], w["source"]) for w in c.windows], [("5-hour", 12.0, "manual mark (get_usage passed in)"), ("weekly", 40.0, "manual mark (get_usage passed in)")])


class LoginCheck(unittest.TestCase):
    def fake(self, d, name, body):
        p = Path(d) / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    @unittest.skipIf(sys.platform == "win32", "shell fakes")
    def test_codex_login_status_decides_and_the_answer_is_cached(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ["PATH"], "DELEGATE_CONFIG_DIR": d}):
            os.environ.pop("DELEGATE_NOCACHE", None)
            count = Path(d) / "count"
            self.fake(d, "codex", 'echo x >> "%s"; [ "$1 $2" = "login status" ] && { echo "Not logged in"; exit 1; }; exit 2' % count)
            st = {}
            with patch.object(env, "update_state", side_effect=lambda fn: fn(st)):
                a = cockpit.login_state("OPENAI_SUBSCRIPTION", st, T)
                b = cockpit.login_state("OPENAI_SUBSCRIPTION", st, T + 200)
                c = cockpit.login_state("OPENAI_SUBSCRIPTION", st, T + 400)
            self.assertEqual((a[0], b[0], c[0]), (False, False, False))
            self.assertEqual(len(count.read_text().split()), 2)          # once, then again after the 5-minute cache ran out
            self.fake(d, "codex", 'echo "Logged in using ChatGPT"')
            self.assertTrue(harnesses.probe_login("OPENAI_SUBSCRIPTION")[0])

    def test_opencode_logins_are_read_from_its_auth_file(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"XDG_DATA_HOME": d}), patch.object(OpenCodeHarness, "find_exe", return_value="opencode"):
            (Path(d) / "opencode").mkdir()
            auth = Path(d) / "opencode" / "auth.json"
            auth.write_text(json.dumps({"xai": {"type": "oauth"}}))
            self.assertTrue(harnesses.probe_login("GROK_SUBSCRIPTION")[0])
            self.assertFalse(harnesses.probe_login("ZAI_CODING_SUBSCRIPTION")[0])
            auth.write_text(json.dumps({"zai-coding-plan": {"type": "api", "key": "k"}}))
            self.assertTrue(harnesses.probe_login("ZAI_CODING_SUBSCRIPTION")[0])
            self.assertFalse(harnesses.probe_login("GROK_SUBSCRIPTION")[0])


class Command(unittest.TestCase):
    def run_status(self, *argv, state=None, ready=None):
        cfg = cfg_all()
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"DELEGATE_CONFIG_DIR": d, "CLAUDE_CODE_ENTRYPOINT": "cli"}):
            (Path(d) / "state.json").write_text(json.dumps({"v": 1, **(state or {})}))
            ready = ready or {n: True for n in cfg["lanes"]}
            noread = {k: (lambda lane_, S, st_, t, *a: None) for k in usage.READERS}
            logins = {"OPENAI_SUBSCRIPTION": (False, "Not logged in"), "GROK_SUBSCRIPTION": (True, "ok")}
            out, sys.stdout = sys.stdout, io.StringIO()
            try:
                with patch.object(env, "load_routing", return_value=cfg), patch.dict(usage.READERS, noread), \
                        patch.object(onboard, "configured_lanes", return_value=ready), patch.object(harnesses, "probe_login", side_effect=lambda n: logins.get(n, (True, "ok"))), \
                        patch.object(env, "now", return_value=T):
                    route.main(["status", *argv])
                return sys.stdout.getvalue()
            finally:
                sys.stdout = out

    def test_text_is_short_and_names_every_known_lane_even_a_logged_out_one(self):
        text = self.run_status(ready={n: n != "ZAI_CODING_SUBSCRIPTION" for n in CFG["lanes"]})
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 25, text)
        for n in CFG["lanes"]:
            self.assertIn(n, text)
        self.assertIn("🔑 OPENAI_SUBSCRIPTION", text)                                  # the real check says logged out although the file is there
        self.assertIn("logged out: run `codex login`", text)
        self.assertIn("unlock time unknown: waits for you to log in", text)
        self.assertIn("⛔ ZAI_CODING_SUBSCRIPTION", text)
        self.assertIn("next worker:", lines[0])

    def test_json_has_the_same_lanes_and_keeps_the_dashboard_keys(self):
        state = {"out": {"DEEPSEEK_FREE": {"until": T + 3 * HOUR, "why": "free usage limit still reached", "at": T - 60}}}
        text = self.run_status(state=state)
        data = json.loads(self.run_status("--json", state=state))
        ck = data["cockpit"]
        ids = [r["id"] for r in ck["lanes"]]
        self.assertEqual(ids, list(CFG["lanes"]))
        for n in ids:
            self.assertIn(n, text)
        row = next(r for r in ck["lanes"] if r["id"] == "DEEPSEEK_FREE")
        self.assertEqual((row["state"], row["unlock"]["in_s"]), ("resting", 3 * HOUR))
        self.assertIn("unlocks " + row["unlock"]["text"], text)
        for k in ("lanes", "running", "needs_input"):
            self.assertIn(k, data)
        self.assertIn(ck["next_worker"]["action"], ("run", "stop"))

    def test_stdout_gets_a_safe_encoding_for_emoji(self):
        class Narrow(io.StringIO):
            encoding = "cp1252"
            reconfigured = None

            def reconfigure(self, **kw):
                self.reconfigured = kw
        s = Narrow()
        with patch.object(sys, "stdout", s), patch.object(status, "cmd_status", return_value=0):
            route.main(["status"])
        self.assertEqual(s.reconfigured, {"encoding": "utf-8", "errors": "replace"})


if __name__ == "__main__":
    unittest.main()
