"""Run: the single entry of `route.py run`. One call, end to end: decide the lane (decide.py), refuse without launching when the answer is a stop,
claim a lease (lease.py), launch the worker (launch.py) behind the canary, the network retry and the schema re-ask, record history, and print one
JSON answer. Every outcome, including a crash, is an answer; exit 0 whenever it was printed."""
import argparse
import contextlib
import json
import sys
import time
import uuid
from pathlib import Path

from delegate.adapters import agent_runs, git, procs
from delegate.app import answer, cockpit, decide, env, harnesses, launch, net, usage, watch
from delegate.app.lease import RunLease
from delegate.app.supervisor import RunHooks
from delegate.contracts import codec
from delegate.contracts import history as hist
from delegate.contracts.state import RefRecord
from delegate.domain.cost import accounting
from delegate.domain.health import ledger as health_ledger
from delegate.domain.holds.book import HoldBook
from delegate.domain.lanes import profiles
from delegate.domain.lanes.lane import Lane
from delegate.domain.policy.worker import GIT_RULES, SECRET_RULE

CANARY_BRIEF = "Reply with exactly: OK"
CANARY_TIMEOUT_S = 30
IDENTITY = ("lane", "model", "effort", "family", "flag", "rung", "fallback", "fallback_reason", "first_lane")


def lane_use(cfg, name, t):
    """(five_hour %, weekly %) of a lane now; None where unknown."""
    c = cfg["lanes"][name]
    lane = Lane(name, c["family"], c["kind"], c.get("shared_with_orchestrator", False))
    with contextlib.suppress(Exception):
        usage.READERS[c["reader"]](lane, env.settings_of(cfg), env.load_state(), t)
    return lane.five, lane.week


class Run:
    def __init__(self, a, cfg, unattended=False, idle_s=None):
        self.a, self.cfg, self.unattended, self.idle_s = a, cfg, unattended, idle_s
        self.extra = {}         # fields every answer of this call carries: warnings, notice, needs_input
        self.settler = launch.settler()

    # -- the answer ----------------------------------------------------------------------------------------------------------------------
    def emit(self, outcome, **kw):
        return answer.emit(answer.result(outcome, **kw), not self.unattended)

    def refuse(self, outcome, say, retry_after, **who):
        """An answer given without launching a worker; history keeps it too, so refusals are countable."""
        a, call_id = self.a, getattr(self.a, "call_id", None)
        env.history_add(hist.RefusalRow(ts=env.now(), call_id=call_id, **health_ledger.failure_fields(outcome, "", say, retry_after), role=a.role, task=getattr(a, "task", None) or "",
                                        lane=who.get("lane"), model=who.get("model"), resume=bool(getattr(a, "resume", None))))
        return self.emit(outcome, say=say, retry_after=retry_after, call_id=call_id, transcript_status="not_launched", **who, **self.extra)

    # -- the call --------------------------------------------------------------------------------------------------------------------------
    def go(self):
        try:
            return self._go()
        except TimeoutError as e:       # state lock held too long: transient, so stop with a retry time (error means permanent)
            return self.refuse("lock", str(e), env.now() + decide.busy_jitter())

    def _go(self):
        d, lanes, warnings, t = decide.decision(self.a, self.cfg, self.unattended)
        if warnings:
            self.extra["warnings"] = warnings
        for key in ("notice", "warning"):
            if d.get(key):
                self.extra[key] = d[key]
        if d.get("warning"):
            print("warning: " + d["warning"], file=sys.stderr)
        if d["action"] in ("error", "stop"):
            return self._stopped(d, lanes, t)
        h = d["how"]
        who = {k: d.get(k) for k in IDENTITY}
        who["family"] = self.cfg["lanes"][d["lane"]]["family"]
        if not self.unattended:
            ni = [n["what"] for n in cockpit.needs_input(lanes)]
            if ni:
                self.extra["needs_input"] = ni
        call_id = getattr(self.a, "call_id", None)
        if h["kind"] not in profiles.REGISTRY and h["kind"] != "agent_tool":
            return self.refuse("setup", f"no launcher for runner kind {h['kind']!r}: route.py has codex, opencode and claude_p", None, **who)
        if h["kind"] == "agent_tool":
            return self.emit(None, **who, call_id=call_id, transcript_status="unsupported", agent_model=h["model"], brief_prefix=SECRET_RULE + " " + GIT_RULES, cwd=h["cwd"], **self.extra)
        brief = Path(self.a.brief).read_text(encoding="utf-8", errors="replace") if self.a.brief else sys.stdin.read()
        if not brief.strip():
            return self.emit("caller", **who, call_id=call_id, say="empty brief: pass it on stdin or with --brief FILE", **self.extra)
        return self._run(d, lanes, t, who, brief)

    def _stopped(self, d, lanes, t):
        a = self.a
        if d["action"] == "error":
            return self.refuse("caller", d["error"], None)
        if d.get("outcome"):        # --resume on a lane that is out, benched or held
            return self.refuse(d["outcome"], d["say"], d.get("retry_after"), lane=d["lane"])
        holds, S = HoldBook(env.load_state().holds), env.settings_of(self.cfg)
        retry = min([x for x in [ln.back_at(S, holds.covering(ln.name, a.role, t)) for ln in lanes.values()] + [b.until for ln in lanes.values() for b in ln.benched.values()] if x] or [None])
        if not self.unattended and d.get("ask_user"):
            self.extra.update(ask_user=d["ask_user"], ask_kind="decision")
        if d.get("reason") == "busy":
            return self.refuse("busy", d["say"], d["retry_after"], lane=d.get("lane"))
        return self.refuse("no_lane", d["say"], retry)

    def _run(self, d, lanes, t, who, brief):
        a, h = self.a, d["how"]
        rid = str(uuid.uuid4())
        lane = lanes[d["lane"]]
        # A limit refusal mid-run lasts until the time its message names, else the soonest window reset (else an hour).
        resets = [(lane.week or 0, lane.week_reset), (lane.five or 0, lane.five_reset)]
        d["limit_reset"] = min((r for _, r in resets if r and r > t), default=None)     # a guess: the soonest reset (too early costs one refused call, too late days)
        cap = lane.slots(env.settings_of(self.cfg))
        canary_due = bool(lane.canary.get(d["model"])) and h["kind"] in profiles.REGISTRY
        silence_s = self.idle_s or watch.LIMITS[watch.CLI_OF_KIND[h["kind"]]]["silence_s"]
        lease = RunLease(rid, a.role, a.task, d["lane"], d["model"], cap, canary_due, silence_s)
        no = lease.claim()
        if no:
            return self.refuse(no[0], no[1], env.now() + decide.busy_jitter(), **who)
        lease.keep_alive()
        hooks, launched = self._hooks(rid, lease, d)
        meta = {"parent_session": a.parent or "", "title": a.title or brief.strip().splitlines()[0][:120]}
        if a.task:
            meta["task"] = a.task
        fam, cwd = who["family"], h["cwd"]
        git_before, t0 = git.state(cwd), time.time()
        t_from = file_size(harnesses.find_transcript(a.resume, fam)) if a.resume else 0
        try:
            res = self._launch(d, brief, rid, t, meta, hooks, canary_due)
        except Exception as e:      # a launcher bug is an error object, not a stack trace
            res = {"outcome": "crash", "say": f"{type(e).__name__}: {e}"}
        except SystemExit as e:     # SIGTERM / SIGHUP to this process: the supervisor stopped the worker's tree first; the caller still gets a typed answer
            if not isinstance(e.code, int) or e.code <= 128:
                raise
            res = {"outcome": "killed", "say": f"route.py got signal {e.code - 128}; its worker was stopped first"}
        finally:
            lease.release()
        return self._finish(res, d, lane, who, rid, lease, hooks, launched, cap, (t0, t_from, git_before))

    def _hooks(self, rid, lease, d):
        launched = {}
        hooks = RunHooks(idle_s=self.idle_s, unattended=self.unattended)
        ref_rec = RefRecord(lane=d["lane"], model=d["model"], effort=d["effort"], mode=d["mode"], cap=d.get("cap"),
                            rung=d.get("rung"), fallback=d.get("fallback"), fallback_reason=d.get("fallback_reason"), first_lane=d.get("first_lane"))

        def worker_started(w):
            """The worker runs in its own process group (pgid = pid), so a SIGKILL on route.py leaves it running: say where it is, before any
            work, on stderr and in the lease (status --json `running`), so the caller can kill the group itself."""
            print(f"delegate: worker pid={w.pid} pgid={w.pgid} run_id={rid}", file=sys.stderr, flush=True)
            launched.update(pid=w.pid, pgid=w.pgid, start=w.start)
            lease.worker_started(w.pid, w.start)

        def keep_ref(ref):
            """Remember the ref for `--resume` whatever the outcome."""
            def keep(s):
                s.refs[ref] = ref_rec
                for k in list(s.refs)[:-200]:
                    s.refs.pop(k)
            env.update_state(keep)
        self.keep_ref = keep_ref

        def ref_seen(ref):
            """The vendor revealed its session id: store it, stamp the lease, and print it on stderr (the engine reads `delegate: ref=`) before any work is lost."""
            keep_ref(ref)
            print(f"delegate: ref={ref} run_id={rid}", file=sys.stderr, flush=True)
            lease.stamp_ref(ref)
        hooks.started, hooks.beat, hooks.ref = worker_started, lease.stamp_event, ref_seen
        return hooks, launched

    # -- the launch ladder: canary, network retry, schema re-ask ------------------------------------------------------------------------------
    def _launch(self, d, brief, rid, t, meta, hooks, canary_due):
        a = self.a
        launch_one = lambda b, a_: launch.launch_worker(d, b, a_, rid, t, self.cfg, meta, hooks)
        res = self._canary(launch_one, d, a) if canary_due else None      # the one probe before full traffic; None = it passed
        res = res or launch_one(brief, a)
        if res.get("outcome") == "network" and not res.pop("started", False) and net.net_up(d["lane"]):
            res = launch_one(brief, a)      # dropped before the vendor had a session and the net is back: nothing to resume, nothing lost, so run it again, once
        if res.get("outcome") is None and a.schema:
            res = self._schema(res, launch_one)
        res.pop("started", None)        # the launcher's own bookkeeping, whichever launch (first, re-ask) produced `res`
        res.pop("answered", None)
        return res

    def _canary(self, launch_one, d, a):
        """The pair's bench ended (or its lane's limit did): one 30 s probe before full traffic. -> None when it answered (a `done` row in history ends
        the ladder), else the failed result, which is the answer to the caller: the run's own history row then starts the next rung."""
        res = launch_one(CANARY_BRIEF, argparse.Namespace(**dict(vars(a), resume=None, schema=None, timeout=CANARY_TIMEOUT_S)))
        if res.get("outcome") is None:
            env.history_add(hist.CanaryRow(ts=env.now(), run_id=str(uuid.uuid4()), lane=d["lane"], model=d["model"]))
            return None
        if res["outcome"] == "timeout":      # a probe that did not answer in time is hung: `timeout` is never counted, `hung` is
            res.update(outcome="hung", retry_after=env.now() + 60, say=f"canary: {d['lane']}/{d['model']} did not answer within {CANARY_TIMEOUT_S}s")
        else:
            res["say"] = "canary failed: " + (res.get("say") or "")
        return res

    def _schema(self, res, launch_one):
        """delegate enforces the schema: one re-ask in the same thread, then schema_invalid."""
        a = self.a
        schema = json.loads(Path(a.schema).read_text(encoding="utf-8"))
        why = "the answer is not JSON" if res.get("data") is None else answer.schema_problem(res["data"], schema)
        if why and res.get("ref"):
            again = launch_one(f"Your final answer does not match the required JSON schema: {why}. Reply again with only the JSON object.",
                               argparse.Namespace(**dict(vars(a), resume=res["ref"])))
            tok = [x for x in (res.get("tokens"), again.get("tokens")) if x]
            again["tokens"] = {k: sum(x.get(k) or 0 for x in tok) for k in ("in", "cached", "out")} if tok else None
            again["acct"] = accounting.merge(res.get("acct"), again.get("acct"))
            again["cost"] = (res.get("cost") or 0) + (again.get("cost") or 0)
            again["cumulative"] = again.get("cumulative") or res.get("cumulative")
            again["denied"] = (res.get("denied") or []) + (again.get("denied") or [])
            res = again
            why = None if res.get("outcome") else "the answer is not JSON" if res.get("data") is None else answer.schema_problem(res["data"], schema)
        if why and res.get("outcome") is None:
            res.update(outcome="schema_invalid", say=f"the answer does not match the schema: {why}")
        return res

    # -- the end: history, usage, the answer ----------------------------------------------------------------------------------------------------
    def _finish(self, res, d, lane, who, rid, lease, hooks, launched, cap, started):
        a, cfg, extra = self.a, self.cfg, self.extra
        t0, t_from, git_before = started
        fam, cwd = who["family"], d["how"]["cwd"]
        err = res.pop("stderr", "")
        if err:
            print(err[-2000:], file=sys.stderr)
        done = res.get("outcome") is None
        if done and d["lane"] in ("CLAUDE_SUBSCRIPTION", "OPENAI_SUBSCRIPTION"):
            usage.auth_cache().remember(d["lane"], True, "a worker run succeeded", env.now())
        agent_runs.run(rid, ended=env.now(), status="completed" if done else "failed", transcript=harnesses.find_transcript(res.get("ref"), fam))
        if res.get("ref"):          # every outcome: a stop, a failure and a timeout can all be resumed
            with contextlib.suppress(Exception):
                self.keep_ref(res["ref"])
        secs = round(time.time() - t0, 1)
        five_after, week_after = lane_use(cfg, d["lane"], env.now())
        transcript = harnesses.find_transcript(res.get("ref"), fam)
        detail = res.pop("detail", "")
        if detail:
            res["first_error"] = detail
        env.history_add(hist.RunRow(
            ts=env.now(), run_id=rid, call_id=getattr(a, "call_id", None), ref=res.get("ref"), **health_ledger.failure_fields(res.get("outcome"), "", detail or res.get("say"), res.get("retry_after")),
            role=a.role, task=a.task or "", lane=d["lane"], model=d["model"], effort=d["effort"], done=done, warnings=res.get("warnings"), stream_tail=res.pop("stream_tail", None),
            denials=sum(1 for x in res.get("denied") or [] if x.get("kind") != "sandbox_limit"), secs=secs, cost=res.get("cost", 0), tokens=res.get("tokens"),
            cumulative=res.get("cumulative"), five_before=lane.five, week_before=lane.week, five_after=five_after, week_after=week_after,
            overlap=lease.overlap, slots=cap, transcript=transcript))
        info = transcript_info(transcript, fam, t_from)
        ac = res.pop("acct", None)
        res.pop("cumulative", None)
        usage_rec = {"tokens": dict(res["tokens"], **accounting.token_fields(ac)) if res.get("tokens") else None, "cost": res.get("cost", 0), "secs": secs,
                     "turns": res.pop("turns", None), "context_k": res.pop("context_k", None), "answer_bytes": len((res.get("text") or "").encode("utf-8")),
                     **accounting.fields(ac, res.get("ref"), time.time())}
        if res.get("warnings"):      # the launcher's own (net_after_answer) join the router's
            extra["warnings"] = list(extra.get("warnings") or []) + res.pop("warnings")
        res.update(**who, run_id=rid, secs=secs, git_violation=git.changes(git_before, git.state(cwd)),
                   context={"instructions": [str(f) for f in decide.repo_instructions(cwd)], "env_keys": sorted(env.child_env().keys())},
                   transcript=transcript, call_id=getattr(a, "call_id", None), usage=usage_rec, transcript_info=info,
                   transcript_status="ok" if info else "ref_unresolvable" if res.get("ref") else "died_before_session_id", **extra)
        wk = procs.worker_end(hooks.stops, res.get("outcome"))
        if launched:
            wk.pid, wk.pgid, wk.start = launched["pid"], launched["pgid"], launched["start"]
        if wk.survivors and res.get("outcome") is None:      # a done answer must not hide a worker still running
            res.update(outcome="worker_survived", say="worker pids outlived SIGTERM and SIGKILL: " + ", ".join(str(x.pid) for x in wk.survivors))
        return self.emit(res.pop("outcome", None), worker=codec.to_json(wk), **res)


def transcript_info(path, family, start=0):
    """-> {path, kind, from, to, bytes}: the transcript file and the byte window this call wrote (a resumed thread appends to one file), or None."""
    if not path:
        return None
    try:
        size = Path(path).stat().st_size
    except OSError:
        return None
    kind = {"claude": "claude", "openai": "codex"}.get(family, "opencode_export")
    frm = start if kind != "opencode_export" and 0 <= start <= size else 0          # an opencode export is rewritten whole each run
    return {"path": path, "kind": kind, "from": frm, "to": size, "bytes": size - frm}


def file_size(path):
    try:
        return Path(path).stat().st_size if path else 0
    except OSError:
        return 0
