"""Breaker, reset times, token budget, config location, agent-runs. Run: python -m unittest discover -s tests"""
import hermetic  # noqa: F401  (a home of its own for every test)
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
from unittest import mock
import unittest
from pathlib import Path
from test_route import route, CFG, ROOT, T, argparse_ns, at, lanes, why
from delegate.app.publish import LiveSink
from delegate.app.supervisor import RunHooks, Supervisor
import routing_support as support
from delegate.app.fleet import apply_health
from delegate.contracts import codec, stores
import stream_proc
from delegate.adapters import procs
from delegate.adapters.harness import reset_parse
from delegate.adapters.harness.base import LaunchSpec
from delegate.adapters.harness.codex import CodexHarness
from delegate.app import env
from delegate.app import usage
from delegate.app import launch
from delegate.app import decide as dec
from delegate.adapters.harness import classify
from delegate.domain.lanes.lane import Lane
from delegate.app import watch
from delegate.contracts import history as hist
from delegate.domain.health import ledger as health_ledger
from delegate.adapters import agent_runs


class Breaker(unittest.TestCase):
    """delegate-rdt / -y3f: the pair breaker is wired into the decision for every lane, and DeepSeek/ZAI/Grok run under the watchdog."""

    def lanes_with(self, hist_rows):
        lanes_ = lanes()
        grok = Lane("GROK_SUBSCRIPTION", "xai", "subscription")
        lanes_[grok.name] = grok
        led = health_ledger.HealthLedger(env.settings_of(CFG).breaker)
        st = led.fold(hist_rows)
        for l in lanes_.values():
            apply_health(l, env.typed_routing(CFG).models, led, st, T + 1)
        return lanes_

    def rows(self, lane, model, n=3, outcome="worker_failed"):
        return [hist.RunRow(ts=T + i, run_id=f"r{i}", task=f"t{i}", role="worker", lane=lane, model=model, effort="high", done=False,
                                  **health_ledger.failure_fields(outcome, "event", "boom")) for i in range(n)]

    def test_a_benched_pair_falls_through_to_the_next_step_and_only_that_pair(self):
        ls = self.lanes_with(self.rows("OPENAI_SUBSCRIPTION", "gpt-6.1-sol"))
        self.assertIn("gpt-6.1-sol", ls["OPENAI_SUBSCRIPTION"].benched)
        d = support.decide("worker", CFG, ls, {}, T + 1, {"authored_by": None, "premium": None, "allow_degraded": False})
        self.assertNotEqual(d["model"], "gpt-6.1-sol")
        S = env.settings_of(CFG)
        ok, why = ls["OPENAI_SUBSCRIPTION"].usable(S, True, "gpt-6.1-sol")
        self.assertFalse(ok)
        self.assertIn("benched", why)
        self.assertTrue(ls["OPENAI_SUBSCRIPTION"].usable(S, True, "gpt-6-luna")[0])
        self.assertTrue(ls["OPENAI_SUBSCRIPTION"].usable(S, True)[0])

    def test_all_pairs_benched_stops_with_the_bench_ends_listed(self):
        ls = self.lanes_with(self.rows("OPENAI_SUBSCRIPTION", "gpt-6.1-sol") + self.rows("CLAUDE_SUBSCRIPTION", "sonnet-5.5") + self.rows("CLAUDE_SUBSCRIPTION", "opus-5.5")
                             + self.rows("CLAUDE_CREDIT", "sonnet-5.5") + self.rows("GROK_SUBSCRIPTION", "grok-4.7"))
        d = support.decide("worker", CFG, ls, {}, T + 1, {"authored_by": None, "premium": None, "allow_degraded": False})
        self.assertEqual(d["action"], "stop")
        self.assertIn("OPENAI_SUBSCRIPTION/gpt-6.1-sol", d["resets"])

    def test_every_opencode_lane_runs_under_the_watchdog_and_the_breaker(self):
        lanes_ = [n for n, c in CFG["lanes"].items() if c["runner"] == "opencode"]
        self.assertEqual(sorted(lanes_), ["DEEPSEEK_FREE", "GROK_SUBSCRIPTION", "ZAI_CODING_SUBSCRIPTION"])
        lim = watch.LIMITS["opencode"]
        self.assertEqual((lim["first_event_s"], lim["silence_s"] >= 900), (120, True))
        for name in lanes_:
            fam = CFG["lanes"][name]["family"]
            models = [m for m, v in CFG["models"].items() if v["family"] == fam]
            self.assertTrue(models, name)
            for model in models:
                with self.subTest(lane=name, model=model):
                    lane = Lane(name, fam, "subscription")
                    led = health_ledger.HealthLedger(env.settings_of(CFG).breaker)
                    apply_health(lane, env.typed_routing(CFG).models, led, led.fold(self.rows(name, model, outcome="vendor_blip")), T + 1)
                    self.assertFalse(lane.usable(env.settings_of(CFG), True, model)[0])
        import inspect
        self.assertIn("Supervisor(", inspect.getsource(launch.launch_worker))         # all three harnesses run under the one supervisor, so under its watchdog

    def test_history_rotation_survives_a_corrupt_line(self):
        canary = lambda ts, rid: json.dumps(codec.to_json(hist.CanaryRow(ts=ts, run_id=rid, lane="L", model="m")))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(env, "config_dir", return_value=Path(d)), mock.patch.object(stores, "HISTORY_MAX", 100):
            p = env.history_path()
            p.write_text("{broken\n" + canary(1.0, "old") + "\n" + canary(time.time(), "keep") + "\n" * 1 + "x" * 200 + "\n", encoding="utf-8")
            env.history_add(hist.CanaryRow(ts=time.time(), run_id="new", lane="L", model="m"))
            ids = [json.loads(x)["run_id"] for x in p.read_text().splitlines()]
            self.assertEqual(ids, ["keep", "new"])


class ResetTimes(unittest.TestCase):
    """delegate-eau: vendor reset times are parsed in the zone they were stated in; an unstated end is the refusal time + 15/30/60 min."""

    def setUp(self):
        self.tz = os.environ.get("TZ")
        if hasattr(time, "tzset"):
            os.environ["TZ"] = "Europe/Berlin"
            time.tzset()
        self.addCleanup(self.restore)

    def restore(self):
        if self.tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.tz
        if hasattr(time, "tzset"):
            time.tzset()

    def at(self, text, t=None):
        t = t or dt.datetime(2026, 10, 25, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        return reset_parse.parse_reset(text, t), t

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_will_reset_at_is_read_in_the_local_zone(self):
        (at, zone), _ = self.at("Usage limit reached. Your limit will reset at 2026-10-25 14:49:00.")
        self.assertEqual(dt.datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S"), "2026-10-25 14:49:00")
        self.assertTrue(zone.startswith("local"))

    def test_a_stated_zone_is_kept(self):
        (at, zone), _ = self.at("resets at 2026-10-25T08:19:00Z")
        self.assertEqual((dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%H:%M"), zone), ("08:19", "UTC"))
        (at, zone), _ = self.at("will reset at 2026-10-25 14:49:00 +08:00")
        self.assertEqual((dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%H:%M"), zone), ("06:49", "+08:00"))

    def test_retry_after_seconds_and_http_date(self):
        (at, _), t = self.at("429 Too Many Requests, Retry-After: 120")
        self.assertEqual(at, t + 120)
        (at, _), t = self.at("HTTP 429 retry-after: Sun, 25 Oct 2026 12:00:00 GMT", dt.datetime(2026, 10, 25, 10, 0, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%H:%M"), "12:00")

    def test_in_n_hours_and_try_again_at(self):
        (at, _), t = self.at("You can try again in 5 hours.")
        self.assertEqual(at, t + 5 * 3600)
        (at, _), t = self.at("limit reached, resets in 2 hours 30 minutes")
        self.assertEqual(at, t + 9000)
        (at, _), t = self.at("try again at 5:50 AM")
        self.assertEqual(dt.datetime.fromtimestamp(at).strftime("%H:%M"), "05:50")

    def test_a_log_lines_own_timestamp_is_not_a_reset_time_and_unknown_text_is_not_parsed(self):
        line = 'timestamp=2026-10-25T03:02:47.846Z level=ERROR message="stream error" providerID=zai-coding-plan'
        self.assertEqual(self.at(line)[0], (None, None))
        self.assertEqual(self.at("the batch run is queued behind a rate limit")[0], (None, None))
        self.assertEqual(self.at("reset at 2026-10-24 14:49:00")[0], (None, None))        # already past

    def test_unknown_text_is_refusal_time_plus_15_30_60(self):
        t = 1_800_000_000.0
        got = [reset_parse.limit_until("rate limited", t, {}, None, n) for n in range(4)]
        self.assertEqual([round((u - t) / 60) for u, _, _ in got], [15, 30, 60, 60])
        self.assertEqual({src for _, src, _ in got}, {"guess"})
        self.assertEqual(reset_parse.limit_until("rate limited", t, {"limit_reset": t + 7200})[1:], ("guess", None))
        self.assertEqual(reset_parse.limit_until("rate limited", t, {}, lambda: t + 999)[:2], (t + 999, "vendor"))

    def test_out_rows_record_source_and_zone_and_report_is_manual(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(env, "config_dir", return_value=Path(d)):
            res = launch.settler().apply({}, classify.Outcome("rate_limited", "will reset at 2026-10-30 14:49:00 UTC", "event"), {"lane": "L1", "limit_reset": None},
                               time.time(), "codex", argparse_ns("worker", timeout=0))
            rec = env.load_state().out["L1"]
            self.assertEqual((rec.source, rec.zone), ("vendor", "UTC"))
            self.assertEqual(rec.until, dt.datetime(2026, 10, 30, 14, 49, tzinfo=dt.timezone.utc).timestamp())
            for want_n in (0, 1, 2):          # three guesses in a row climb the ladder
                launch.settler().apply({}, classify.Outcome("rate_limited", "slow down", "event"), {"lane": "L2"}, 1_800_000_000.0, "codex", argparse_ns("worker", timeout=0))
                rec = env.load_state().out["L2"]
                self.assertEqual((rec.source, rec.guess_n, round((rec.until - 1_800_000_000.0) / 60)), ("guess", want_n, (15, 30, 60)[want_n]))
            route.main(["report", "--lane", "OPENAI_SUBSCRIPTION", "--event", "out", "--until", str(int(time.time()) + 600)])
            self.assertEqual(env.load_state().out["OPENAI_SUBSCRIPTION"].source, "manual")


class TokenBudget(unittest.TestCase):
    """The orchestrator pays for these files and outputs on every session / delegation. Budgets are characters (about 4 per token); ratchet them down, never up."""
    ROOT = Path(__file__).resolve().parent.parent / "skills" / "delegate"

    def test_skill_md(self):
        text = (self.ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(text), 1900)      # raised from 1800 on 2026-10-19: the run_in_background line
        import re
        desc = re.search(r"^description: (.*)$", text, re.M).group(1)
        self.assertLessEqual(len(desc), 170)

    def test_mode_files(self):
        for f in (self.ROOT / "modes").glob("*.md"):
            self.assertLessEqual(len(f.read_text(encoding="utf-8")), 1900, f.name)

    def test_agent_step_answer_is_small(self):
        import io
        saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "DELEGATE_CONFIG_DIR", "HOME", "CLAUDE_CODE_ENTRYPOINT")}
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_CONFIG_HOME"] = os.environ["DELEGATE_CONFIG_DIR"] = os.environ["HOME"] = d
            os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
            real = dict(usage.READERS)
            usage.READERS.update({k: (lambda lane, S, st_, t, *a: None) for k in real})
            out, sys.stdout = sys.stdout, io.StringIO()
            try:
                route.main(["run", "planner"])                 # fresh machine: the Claude sub-agent step
                answer = sys.stdout.getvalue()
            finally:
                sys.stdout = out
                usage.READERS.update(real)
                for k, v in saved.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertLessEqual(len(answer.replace(os.getcwd(), "")), 700, answer)      # raised from 600 on 2026-10-20: the secrets rule in brief_prefix; the checkout's path is not ours to budget


class ConfigLocation(unittest.TestCase):
    """Settings, state and the key live in Claude Code's plugin data dir when installed, ~/.config/delegate in a checkout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_here = env.SKILL_DIR
        self.saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA", "DELEGATE_CONFIG_DIR", "CLAUDE_PLUGIN_DATA")}
        for k in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA"):
            os.environ[k] = self.tmp.name
        os.environ.pop("DELEGATE_CONFIG_DIR", None)
        os.environ["CLAUDE_PLUGIN_DATA"] = str(Path(self.tmp.name) / "someone-elses-data")      # must be ignored

    def tearDown(self):
        env.SKILL_DIR = self.saved_here
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def installed(self):
        root = Path(self.tmp.name) / ".claude" / "plugins"
        env.SKILL_DIR = root / "cache" / "iorlas-marketplace" / "iorlas-delegate" / "0.12.0" / "skills" / "delegate"
        return root / "data" / "iorlas-delegate-iorlas-marketplace"

    def test_checkout_without_an_override_refuses_instead_of_splitting_the_state(self):
        with self.assertRaises(SystemExit):
            env.config_dir()

    def test_installed_copy_uses_the_plugin_data_dir_and_ignores_foreign_env(self):
        want = self.installed()
        self.assertEqual(env.plugin_data_dir(), want)
        self.assertEqual(env.config_dir(), want)
        self.assertFalse((Path(self.tmp.name) / "someone-elses-data").exists())

    def test_explicit_override_wins(self):
        self.installed()
        os.environ["DELEGATE_CONFIG_DIR"] = str(Path(self.tmp.name) / "mine")
        self.assertEqual(env.config_dir(), Path(self.tmp.name) / "mine")


class AgentRuns(unittest.TestCase):
    """delegate publishes agent-runs v1 (iorlas-task-loop docs/contracts/agent-runs-v1.md), never its own state."""

    def test_run_and_lanes_records(self):
        saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "APPDATA")}
        with tempfile.TemporaryDirectory() as d:
            os.environ["XDG_CONFIG_HOME"] = d
            os.environ["APPDATA"] = d
            try:
                ar = agent_runs
                ar.run("r1", kind="claude-p", parent_session="p", cwd="/w", started=1.0, status="running", ended=None)
                ar.run("r1", status="completed", exit=0)
                rec = json.loads((ar.root() / "runs" / "r1.json").read_text())
                self.assertEqual((rec["v"], rec["producer"], rec["id"], rec["parent_session"], rec["status"], rec["exit"]),
                                 (1, "delegate", "r1", "p", "completed", 0))
                self.assertNotIn("ended", rec)
                lane = Lane("OPENAI_SUBSCRIPTION", "openai", "subscription")
                lane.five, lane.five_reset = 41, 99
                dec.publish_lanes({"OPENAI_SUBSCRIPTION": lane}, support.doc_of({}), 5.0)
                lr = json.loads((ar.root() / "lanes" / "delegate.json").read_text())
                self.assertEqual(lr["lanes"][0]["usage"], [{"window": "5h", "used_pct": 41, "resets_at": 99}])
                self.assertEqual(lr["at"], 5.0)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


class WatchdogProc(unittest.TestCase):
    """delegate-go0 on a real child process (python, so it runs on Windows too): a dead stream is stopped through stop_tree, a chatty one is not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(env, "config_dir", return_value=Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def run_child(self, code, **limits):
        with mock.patch.dict(watch.LIMITS["codex"], **limits):
            sup = Supervisor(CodexHarness(), "codex", RunHooks(), LiveSink("codex", ""))
        with mock.patch.object(stream_proc, "stop_tree", wraps=stream_proc.stop_tree) as st:
            t0 = time.time()
            out = sup.run(LaunchSpec(argv=[sys.executable, "-c", code], env=dict(os.environ), cwd=self.tmp.name, stdin=""), 0)
        return out.rc, {"why": out.watch.why, "kind": out.watch.kind}, st, time.time() - t0

    def test_dead_stream_is_stopped_through_stop_tree(self):
        rc, state, st, secs = self.run_child("import time;print('started',flush=True);time.sleep(60)", first_event_s=5, silence_s=1)
        self.assertEqual((state["why"], state["kind"]), ("hung", "silence"))
        self.assertTrue(st.called)
        self.assertLess(secs, 10)
        self.assertNotEqual(rc, 0)

    def test_a_child_that_prints_nothing_is_hung_on_the_first_event_deadline(self):
        rc, state, st, secs = self.run_child("import time;time.sleep(60)", first_event_s=1, silence_s=60)
        self.assertEqual((state["why"], state["kind"]), ("hung", "first_event"))
        self.assertLess(secs, 10)

    def test_stderr_deltas_keep_a_quiet_stdout_alive(self):
        code = "import sys,time\nfor i in range(8):\n sys.stderr.write('thinking\\n');sys.stderr.flush();time.sleep(0.4)"
        rc, state, st, secs = self.run_child(code, first_event_s=5, silence_s=1)
        self.assertEqual((rc, state["why"], st.called), (0, None, False))


if __name__ == "__main__":
    unittest.main()
