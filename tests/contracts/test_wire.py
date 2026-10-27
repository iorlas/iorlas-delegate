"""contracts: the codec, the goldens (one answer per status, captured from route.py's output before the typed contract existed), the generated
answer_schema, the live-file records. Goldens round-trip byte for byte up to key order."""
import hermetic  # noqa: F401  (a home of its own for every test)
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from test_route import route                       # noqa: E402  (loads route.py and puts src/ on sys.path)
from delegate.contracts import codec, live, schema, wire   # noqa: E402

GOLDEN = sorted((Path(__file__).resolve().parent / "golden").glob("*.json"))
spec = importlib.util.spec_from_file_location("gen_contract", ROOT / "dev" / "gen_contract.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def canon(x):
    return json.dumps(x, sort_keys=True)


class Goldens(unittest.TestCase):
    def test_every_stop_reason_family_has_a_golden(self):
        names = {p.stem for p in GOLDEN}
        for want in ("done", "done_transcript", "done_warning", "agent_handoff", "stop_rate_limited", "stop_logged_out", "stop_network", "stop_timeout",
                     "stop_hung", "stop_killed", "stop_secrets", "stop_bad_request", "stop_schema_invalid", "stop_worker_failed",
                     "stop_no_lane_ask_user", "stop_busy_not_launched"):
            self.assertIn(want, names)

    def test_a_golden_round_trips_through_the_records_unchanged(self):
        for p in GOLDEN:
            with self.subTest(p.stem):
                gold = json.loads(p.read_text())
                self.assertEqual(canon(codec.to_json(codec.from_json(wire.RunResult, gold))), canon(gold))

    def test_a_golden_fits_the_generated_answer_schema(self):
        sch = schema.of(wire.RunResult)
        for p in GOLDEN:
            with self.subTest(p.stem):
                self.assertIsNone(route.schema_problem(json.loads(p.read_text()), sch))

    def test_route_builds_and_prints_the_golden(self):
        """result() + emit() rebuild each launched-and-stopped golden from its own facts and print it identically."""
        for p in GOLDEN:
            gold = json.loads(p.read_text())
            stop = gold["stop"]
            if gold.get("agent_model") or "stop_no_lane" in p.stem:       # built by decision(), not by result() alone
                continue
            kw = {k: v for k, v in gold.items() if k not in ("stop", "contract")}
            if stop:
                kw.update(retry_after=stop["retry"]["at"], fix=stop.get("fix"), resumable=stop.get("resumable"), partial=stop.get("partial"))
            with self.subTest(p.stem), mock.patch.object(route, "interactive", return_value=False), redirect_stdout(io.StringIO()) as out:
                route.emit(route.result(stop["reason"] if stop else None, **kw))
            self.assertEqual(canon(json.loads(out.getvalue())), canon(gold))

    def test_new_fields_are_absent_until_set_and_a_null_is_not_absent(self):
        gold = json.loads((GOLDEN[0].parent / "done.json").read_text())
        for k in ("exit_code", "first_error", "ask_kind"):
            self.assertNotIn(k, gold)
        gold["exit_code"] = None
        got = codec.to_json(codec.from_json(wire.RunResult, gold))
        self.assertIn("exit_code", got)
        self.assertIsNone(got["exit_code"])

    def test_the_worker_names_its_process(self):
        w = wire.WorkerEnd(pid=7, pgid=7, start="Oct 1")
        self.assertEqual(codec.from_json(wire.WorkerEnd, codec.to_json(w)), w)
        self.assertNotIn("pid", codec.to_json(wire.WorkerEnd()))


class Codec(unittest.TestCase):
    def test_an_int_stays_an_int_and_a_wrong_type_is_refused(self):
        gold = json.loads((GOLDEN[0].parent / "done.json").read_text())
        gold["cost"] = 0
        self.assertEqual(json.dumps(codec.to_json(codec.from_json(wire.RunResult, gold))["cost"]), "0")
        for key, bad in (("cost", "free"), ("text", 3), ("denied", {}), ("contract", 2), ("transcript_status", "maybe")):
            with self.subTest(key), self.assertRaises(codec.CodecError):
                codec.from_json(wire.RunResult, dict(gold, **{key: bad}))

    def test_an_unknown_key_and_a_missing_key_are_refused(self):
        gold = json.loads((GOLDEN[0].parent / "done.json").read_text())
        with self.assertRaises(codec.CodecError):
            codec.from_json(wire.RunResult, dict(gold, surprise=1))
        with self.assertRaises(codec.CodecError):
            codec.from_json(wire.RunResult, {k: v for k, v in gold.items() if k != "usage"})

    def test_a_stale_version_is_refused(self):
        with self.assertRaises(codec.StaleRecord):
            codec.from_json(live.WorkerStarted, {"v": 2, "pid": 1, "pgid": 1, "run_id": "r"})


class Generated(unittest.TestCase):
    def test_contract_json_answer_schema_is_what_the_records_generate(self):
        self.assertEqual(gen.PATH.read_text(encoding="utf-8"), gen.render(), "run python3 dev/gen_contract.py")

    def test_the_schema_requires_what_the_wire_always_carries(self):
        sch = schema.of(wire.RunResult)
        self.assertLessEqual({"contract", "stop", "usage", "tokens", "denied", "transcript_status"}, set(sch["required"]))
        self.assertNotIn("worker", sch["required"])
        self.assertFalse(sch["additionalProperties"])


class LiveRecords(unittest.TestCase):
    def test_every_kind_round_trips_with_its_k(self):
        recs = [live.LiveStart(t=1.0, harness="codex", pid=3, lane="L"), live.LiveCmdStart(t=1.0, id="a", text="ls", cwd="/w"),
                live.LiveCmdEnd(t=2.0, id="a", exit=None, secs=1.0, out="x"), live.LiveToolStart(t=1.0, id="b", text="edit f", prov=True),
                live.LiveToolEnd(t=2.0, id="b", secs=1.0), live.LiveMsg(t=1.0, text="m"), live.LiveThink(t=1.0, text="t"),
                live.LiveTokens(t=1.0, in_=5, cached=2, out=1), live.LiveStall(t=1.0, secs=61), live.LiveRetry(t=1.0, n=2, of=5, why="x"),
                live.LiveEnd(t=3.0, status="done", secs=2.0, cost_usd=0.5), live.LiveRaw(t=1.0, type="weird")]
        self.assertEqual({r.KIND for r in recs}, set(live.LIVE_EVENTS))
        for r in recs:
            with self.subTest(r.KIND):
                wire_form = codec.to_json(r)
                self.assertEqual(wire_form["k"], r.KIND)
                self.assertEqual(codec.from_tagged(live.LIVE_EVENTS, wire_form, "k"), r)
        self.assertEqual(codec.to_json(live.LiveTokens(t=1.0, in_=5))["in"], 5)

    def test_start_end_kinds_come_in_pairs(self):
        kinds = set(live.LIVE_EVENTS)
        for k in kinds:
            if k.endswith("_start"):
                self.assertIn(k[:-6] + "_end", kinds)


if __name__ == "__main__":
    unittest.main()
