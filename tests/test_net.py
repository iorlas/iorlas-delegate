"""Network facts (delegate-4n7, delegate-0sf, delegate-dlz): the probe, the words of a dropped connection, network never benching a pair,
roles{} in status --json --cheap, and the adapter registry."""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from test_route import route, CFG, T, lanes

LANES = Path(__file__).resolve().parent.parent / "skills" / "delegate" / "lanes"
from lanes import classify, cockpit, health, netprobe, profiles          # noqa: E402  (test_route put skills/delegate on sys.path)


@contextlib.contextmanager
def listener():
    """A local TCP server: the probe's 'vendor' that is up."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    try:
        yield "127.0.0.1:%d" % srv.getsockname()[1]
    finally:
        srv.close()


def closed_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    return "127.0.0.1:%d" % port


class Probe(unittest.TestCase):
    def test_up_when_a_host_connects(self):
        with listener() as host:
            r = netprobe.probe([host])
        self.assertTrue(r["up"])
        self.assertEqual(r["hosts"][host], "ok")

    def test_down_when_nothing_connects(self):
        r = netprobe.probe([closed_port()], connect=lambda addr, timeout: (_ for _ in ()).throw(OSError("refused")))
        self.assertEqual((r["up"], r["why"]), (False, "offline"))

    def test_a_dns_outage_is_not_up_even_when_an_ip_literal_answers(self):
        def resolve(name, port):
            raise socket.gaierror("no dns")
        r = netprobe.probe(["api.anthropic.com"], resolve=resolve, connect=lambda addr, timeout: None)
        self.assertEqual((r["up"], r["ip_up"], r["why"]), (False, True, "dns"))
        self.assertEqual(r["hosts"]["api.anthropic.com"], "dns")

    def test_a_resolver_that_hangs_cannot_hold_the_probe_past_three_seconds(self):
        gate = threading.Event()
        t0 = time.time()
        r = netprobe.probe(["api.anthropic.com", "chatgpt.com"], resolve=lambda n, p: gate.wait(30), connect=lambda addr, timeout: None)
        gate.set()
        self.assertLess(time.time() - t0, 3)
        self.assertFalse(r["up"])
        self.assertEqual(r["hosts"]["chatgpt.com"], "slow")

    def test_no_subprocess_and_no_vendor_call(self):
        src = (LANES / "netprobe.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"(?m)^\s*(import|from)\s+subprocess")
        self.assertNotIn("os.system", src)

    def test_the_cache_keeps_since_until_up_flips(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "net.json")
            mk = lambda up, at: {"up": up, "ip_up": up, "why": "ok" if up else "offline", "hosts": {"a": "ok" if up else "tcp"}, "at": at}
            netprobe.record(p, mk(True, 100.0))
            netprobe.record(p, mk(True, 200.0))
            self.assertEqual(netprobe.load(p)["since"], 100.0)
            netprobe.record(p, mk(False, 300.0))
            self.assertEqual(netprobe.load(p)["since"], 300.0)

    def test_view_is_unknown_when_stale_or_absent(self):
        c = {"up": True, "since": 1, "at": 1000.0, "hosts": {"a": "ok", "b": "tcp"}}
        self.assertEqual(netprobe.view(c, ["a"], 1010.0)["up"], True)
        self.assertEqual(netprobe.view(c, ["b"], 1010.0)["up"], False)
        self.assertEqual(netprobe.view(c, ["a", "b"], 1010.0)["up"], True)
        self.assertEqual(netprobe.view(c, ["zzz"], 1010.0)["up"], True)             # untested host: the overall answer
        self.assertIsNone(netprobe.view(c, ["a"], 1000.0 + netprobe.FRESH_S + 1)["up"])
        self.assertIsNone(netprobe.view(None, ["a"], 5.0)["up"])


class Words(unittest.TestCase):
    def test_what_a_dropped_connection_says(self):
        for text in ("request failed: getaddrinfo ENOTFOUND chatgpt.com", "Reconnecting... waiting for network (Connection failed: error sending request)",
                     "stream disconnected before completion: websocket closed by server before response.completed",
                     "stream disconnected before completion: idle timeout waiting for websocket", "error sending request for url (https://chatgpt.com/backend-api/ps/mcp)",
                     "API Error: Unable to connect to API (ECONNRESET)", "Connection reset by peer", "socket hang up", "dns error: failed to lookup address"):
            self.assertTrue(netprobe.looks_dropped(text), text)

    def test_what_is_not_a_drop(self):
        for text in ("stream disconnected before completion: Incomplete response returned, reason: max_output_tokens", "You have hit your usage limit",
                     "model not supported", "the test suite failed: 3 errors", ""):
            self.assertFalse(netprobe.looks_dropped(text), text)

    def test_waiting_for_network_is_progress(self):
        self.assertTrue(netprobe.waiting("Reconnecting... waiting for network (Connection failed: error sending request)"))
        self.assertFalse(netprobe.waiting("Reconnecting... 2/5 (request timed out)"))


class RouteNet(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        p = mock.patch.dict(os.environ, {"DELEGATE_CONFIG_DIR": self.d.name})
        p.start()
        self.addCleanup(p.stop)

    def run_main(self, argv, patch_spawns=True):
        out = io.StringIO()
        boom = mock.Mock(side_effect=AssertionError("a process was spawned"))
        with contextlib.ExitStack() as ex:
            if patch_spawns:
                ex.enter_context(mock.patch.object(subprocess, "run", boom))
                ex.enter_context(mock.patch.object(subprocess, "Popen", boom))
            ex.enter_context(mock.patch.object(route, "net_probe", route.REAL_NET_PROBE))
            ex.enter_context(contextlib.redirect_stdout(out))
            rc = route.main(argv)
        return rc, json.loads(out.getvalue())

    def test_route_net_answers_fast_with_no_process(self):
        with listener() as host:
            t0 = time.time()
            rc, r = self.run_main(["net", "--host", host])
        self.assertLess(time.time() - t0, 3)
        self.assertEqual((rc, r["up"], r["why"]), (0, True, "ok"))

    def test_route_net_down_is_still_exit_zero_with_the_object(self):
        rc, r = self.run_main(["net", "--host", closed_port()])
        self.assertEqual((rc, r["up"]), (0, False))

    def test_the_probe_is_cached_for_status_cheap(self):
        with listener() as host:
            self.run_main(["net", "--host", host])
        c = netprobe.load(str(route.net_cache_path()))
        self.assertTrue(c["up"])


class NotTheLanesFault(unittest.TestCase):
    def test_network_never_counts_against_a_pair(self):
        self.assertFalse(classify.benches_pair("network"))
        rows = [{"ts": 1000.0 + i, "run_id": f"r{i}", "task": f"t{i}", "lane": "OPENAI_SUBSCRIPTION", "model": "m", "status": "stop", "reason": "network",
                 **health.row_fields("network", "", "error sending request", None)} for i in range(12)]
        st = health.fold(rows)
        v = health.view(st, "OPENAI_SUBSCRIPTION", "m", 1100.0)
        self.assertEqual((v["state"], v["usable"], st["pairs"], st["lanes"]), ("up", True, {}, {}))

    def test_a_failure_that_names_no_cause_becomes_network_only_when_the_net_is_down(self):
        d = {"lane": "OPENAI_SUBSCRIPTION"}
        down = {"up": False, "ip_up": False, "why": "offline", "ms": 1, "hosts": {}, "at": 1.0}
        for reason in ("hung", "worker_failed", "vendor_blip"):
            o = classify.Outcome(reason, "", "ours")
            with mock.patch.object(route, "net_probe", lambda hosts=None: down):
                self.assertEqual(route.net_pass(o, d).reason, "network", reason)
            self.assertEqual(route.net_pass(o, d).reason, reason)          # test_route's probe says up
        for reason in ("rate_limited", "bad_request", "logged_out", "secrets", "timeout", "credit_exhausted"):
            o = classify.Outcome(reason, "", "ours")
            with mock.patch.object(route, "net_probe", lambda hosts=None: down):
                self.assertEqual(route.net_pass(o, d).reason, reason, reason)        # never second-guessed, never probed

    def test_dropped_words_win_without_a_probe(self):
        probe = mock.Mock(side_effect=AssertionError("probed"))
        with mock.patch.object(route, "net_probe", probe):
            o = route.net_pass(classify.Outcome("worker_failed", "stream disconnected before completion: websocket closed", "event"), {"lane": "X"})
        self.assertEqual(o.reason, "network")

    def test_a_hung_worker_on_a_dead_net_marks_no_lane_out(self):
        d = {"lane": "OPENAI_SUBSCRIPTION", "model": "m"}
        a = mock.Mock(schema=None, timeout=60)
        down = {"up": False, "ip_up": False, "why": "offline", "ms": 1, "hosts": {}, "at": 1.0}
        res = {"hung_words": "printed nothing within 120s", "hung_kind": "first_event", "ref": None}
        with mock.patch.object(route, "net_probe", lambda hosts=None: down), mock.patch.object(route, "mark_out") as mark:
            route.settle(res, classify.Outcome("hung", "", "ours"), d, T, "codex", a)
        mark.assert_not_called()
        self.assertEqual((res["status"], res["reason"], res["resumable"]), ("stop", "network", False))
        self.assertNotIn("hung_words", res)


class Roles(unittest.TestCase):
    def test_roles_say_usable_and_net(self):
        ls = lanes()
        cached = {"up": True, "since": T - 50, "at": T - 5, "hosts": {"chatgpt.com": "ok", "api.anthropic.com": "tcp"}}
        roles = cockpit.roles_view(CFG, ls, {}, T, route.decide, cached)
        self.assertEqual(set(roles), set(CFG["roles"]))
        self.assertTrue(roles["planner"]["usable"])
        self.assertEqual(roles["planner"]["lane"], "OPENAI_SUBSCRIPTION")
        self.assertIn("chatgpt.com", roles["planner"]["hosts"])
        self.assertTrue(roles["planner"]["net"]["up"])                      # one of its vendors answers
        self.assertEqual(roles["planner"]["net"]["age_s"], 5)

    def test_an_unusable_role_says_why_and_when(self):
        ls = lanes(openai={"out": True, "out_until": T + 600, "out_reason": "limit"}, sub={"out": True, "out_until": T + 900, "out_reason": "limit"})
        roles = cockpit.roles_view(CFG, ls, {}, T, route.decide, None)
        self.assertIs(roles["planner"]["usable"], False)
        self.assertEqual(roles["planner"]["until"], T + 600)
        self.assertIsNone(roles["planner"]["net"]["up"])

    def test_deciding_roles_leaves_the_lanes_as_it_found_them(self):
        ls = lanes()
        cockpit.roles_view(CFG, ls, {}, T, route.decide, None)
        self.assertTrue(all(l.held is None for l in ls.values()))

    def test_refresh_brings_net_and_holds_up_to_date_without_a_spawn(self):
        roles = {"worker": {"usable": True, "lane": "OPENAI_SUBSCRIPTION", "model": "m", "why": None, "until": None, "hosts": ["chatgpt.com"], "net": {"up": None}}}
        cached = {"up": False, "since": T, "at": T, "hosts": {"chatgpt.com": "tcp"}}
        r = cockpit.refresh(roles, [{"lane": "OPENAI_SUBSCRIPTION", "roles": ["worker"], "until": T + 100, "reason": "pace"}], cached, T + 1)
        self.assertEqual((r["worker"]["usable"], r["worker"]["net"]["up"]), (False, False))
        self.assertEqual(r["worker"]["until"], T + 100)


class Cheap(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        p = mock.patch.dict(os.environ, {"DELEGATE_CONFIG_DIR": self.d.name})
        p.start()
        self.addCleanup(p.stop)

    def cheap(self):
        boom = mock.Mock(side_effect=AssertionError("a process was spawned"))
        out = io.StringIO()
        with mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom), mock.patch.object(route, "run_tree", boom), \
                mock.patch.object(socket, "create_connection", boom), contextlib.redirect_stdout(out):
            rc = route.main(["status", "--json", "--cheap"])
        return rc, json.loads(out.getvalue())

    def test_a_full_status_writes_roles_and_cheap_replays_them_with_a_fresh_net(self):
        out = io.StringIO()
        with mock.patch.object(route, "probe_login", return_value=(True, "ok")), contextlib.redirect_stdout(out):
            route.main(["status", "--json"])
        full = json.loads(out.getvalue())
        self.assertEqual(set(full["roles"]), set(CFG["roles"]))
        for r in full["roles"].values():
            self.assertIn("usable", r)
            self.assertIn("net", r)
        netprobe.record(str(route.net_cache_path()), {"up": False, "ip_up": False, "why": "offline", "hosts": {"chatgpt.com": "tcp"}, "at": time.time()})
        rc, cheap = self.cheap()
        self.assertEqual(rc, 0)
        self.assertEqual(set(cheap["roles"]), set(CFG["roles"]))
        self.assertIn("chatgpt.com", cheap["roles"]["worker"]["hosts"])
        self.assertIs(cheap["roles"]["worker"]["net"]["up"], False)
        self.assertIs(cheap["network"]["up"], False)

    def test_cheap_without_any_snapshot_has_empty_roles(self):
        rc, d = self.cheap()
        self.assertEqual((rc, d["roles"]), (0, {}))
        self.assertIsNone(d["network"]["up"])


class Adapters(unittest.TestCase):
    def test_the_three_launchers_are_registered_and_route_dispatches_through_the_registry(self):
        self.assertEqual(set(profiles.REGISTRY), {"codex", "opencode", "claude_p"})
        self.assertEqual(set(route.LAUNCHERS), {"codex", "opencode", "claude_p"})
        self.assertIs(route.LAUNCHERS["claude_p"], route.launch_claude)
        from lanes import supervisor
        for a in profiles.REGISTRY.values():
            self.assertIn(a.cli, supervisor.LIMITS)
            self.assertTrue(callable(a.launch))
        self.assertEqual(profiles.REGISTRY["opencode"].resume, "untested")

    def test_a_sixth_runner_is_one_register_call(self):
        try:
            profiles.register("fake", "codex", "fake", lambda *a: {})
            self.assertIn("fake", profiles.launchers())
        finally:
            profiles.REGISTRY.pop("fake", None)

    def test_every_lane_in_routing_json_has_hosts_to_probe(self):
        for lane in CFG["lanes"]:
            self.assertTrue(profiles.hosts_of(lane), lane)


if __name__ == "__main__":
    unittest.main()
