"""Settler: the one place a classified outcome (adapters/harness/classify.py) is applied to the answer and, for a lane-scoped reason, to the lane's
state. All three launchers end here. `apply` is the only entry; one method per reason, found by name.

`reset` is the default end of a limit when the message names no time; `login` the hint stored
with a logged-out lane and the text of the answer with {why}.
"""
import time
from collections.abc import Callable
from dataclasses import dataclass

from delegate.adapters import netprobe
from delegate.adapters.harness import classify, reset_parse
from delegate.app import answer
from delegate.app.env import Args
from delegate.contracts.state import OutState
from delegate.contracts.stores import StateStore
from delegate.domain.lanes.profiles import LOGIN_CMD
from delegate.domain.policy.matchers import heredoc_only_hit
from delegate.domain.policy.worker import APPLY_PATCH_HINT
from delegate.domain.time.spec import local

NET_AMBIGUOUS = ("worker_failed", "vendor_blip", "hung")     # failures that name no cause of their own: the network may be it
LOGOUT_OUT = 900            # a lane whose worker found it logged out is skipped this long, then tried again (a login in between is not seen sooner)
HUNG_OUT_SECS = 1800        # a lane whose worker printed nothing at all rests this long
CREDIT_PROBE = 6 * 3600     # a lane out of credits with no stated end is looked at again after this long
GUESS_WINDOW = 6 * 3600


@dataclass
class Case:
    """One outcome being applied: the answer under construction and everything a reason's method may need."""
    res: dict
    o: classify.Outcome
    d: dict
    t: float
    who: str
    a: Args
    rc: int | None = None
    tail: str = ""
    reset: Callable[[], float | None] | None = None
    login: tuple[str, str] | None = None
    answered: bool = False       # the final answer was captured before the run failed

    @property
    def lane(self) -> str:
        return self.d["lane"]

    @property
    def why(self) -> str:
        return self.o.detail.strip()


class Settler:
    def __init__(self, states: StateStore, net_up: Callable[[str], bool], clock: Callable[[], float], jitter: Callable[[], int]) -> None:
        self.states, self.net_up, self.clock, self.jitter = states, net_up, clock, jitter
        self.by_reason = {"secrets": self._secrets, "timeout": self._timeout, "hung": self._hung, "rate_limited": self._rate_limited, "credit_exhausted": self._credit_exhausted,
                          "logged_out": self._logged_out, "bad_request": self._bad_request, "network": self._network, "killed": self._killed, "vendor_blip": self._vendor_blip}

    def mark_out(self, lane: str, until: float, why: str, t: float, reason: str = "limit", source: str = "guess", zone: str | None = None, guess_n: int = 0) -> None:
        """Record a lane out. source: vendor (the vendor stated the end) | guess | manual (the owner said); zone: the zone the vendor's time was in;
        `set` = wall clock now (workers already running read it: a lane marked out after they started stops them)."""
        self.states.update(lambda s: s.out.__setitem__(lane, OutState(until=until, why=why, at=t, set=time.time(), reason=reason, source=source, zone=zone, guess_n=guess_n)))

    def net_pass(self, o: classify.Outcome, d: dict) -> classify.Outcome:
        """-> `o`, or the network outcome when it was the network. classify trusts network words only before the worker did anything, and a silent
        hang is only a network drop if the net is down: so a failure with no cause of its own is checked against the words of a dropped connection
        and then against one probe (<= 3 s, sockets only). Limits, logins, bad requests, secrets and timeouts are never second-guessed."""
        if o.reason in NET_AMBIGUOUS and (netprobe.looks_dropped(o.detail) or not self.net_up(d["lane"])):
            return classify.Outcome("network", o.detail, o.source)
        return o

    def apply(self, res: dict, o: classify.Outcome, d: dict, t: float, who: str, a: Args, rc: int | None = None, tail: str = "",
              reset: Callable[[], float | None] | None = None, login: tuple[str, str] | None = None) -> dict:
        c = Case(res, self.net_pass(o, d), d, t, who, a, rc, tail, reset, login, res.pop("answered", False))
        if c.o.reason != "hung":
            res.pop("hung_words", None), res.pop("hung_kind", None)
        res["outcome"] = c.o.reason
        if rc is not None:
            res["exit_code"] = rc
        res["detail"] = c.why[:240]           # Run moves it into history.jsonl (`say`); it never reaches the wire
        self.by_reason.get(c.o.reason, self._other)(c)
        return res

    def _soon(self) -> float:
        return self.clock() + self.jitter()

    def _secrets(self, c: Case) -> None:
        c.res.update(say=f"stopped: the worker ran a command on a secret path: {c.why[:200]}" + (f"; {APPLY_PATCH_HINT}" if heredoc_only_hit(c.why) else ""))

    def _timeout(self, c: Case) -> None:
        c.res.update(say=f"{c.who} did not finish within {c.a.timeout}s and was killed")

    def _hung(self, c: Case) -> None:
        words, kind = c.res.pop("hung_words", "printed nothing"), c.res.pop("hung_kind", "first_event")
        if kind == "first_event":       # nothing came at all: the lane is the suspect, it rests; a worker that went quiet mid-run is the pair's (the breaker counts it)
            until = c.t + HUNG_OUT_SECS
            self.mark_out(c.lane, until, f"hung: {words}", c.t, "hung")
            c.res.update(retry_after=until, say=f"{c.who} {words} and was stopped; {c.lane} out until {local(until)}")
        else:
            c.res.update(retry_after=self._soon(), say=f"{c.who} {words} and was stopped (its whole process tree); retry")

    def _rate_limited(self, c: Case) -> None:
        prev = self.states.load().out.get(c.lane)
        n = prev.guess_n + 1 if prev and prev.source == "guess" and c.t - prev.at < GUESS_WINDOW else 0
        until, source, zone = reset_parse.limit_until(c.why, c.t, c.d, c.reset, n)
        ours = c.o.source == "ours"
        self.mark_out(c.lane, until, ("limit (opencode log): " if ours else "limit refusal: ") + c.why[:120].replace("\n", " "), c.t, "limit", source, zone, n if source == "guess" else 0)
        c.res.update(retry_after=until, say=f"{c.lane} hit a limit" + (f" ({c.why[:120]}); worker stopped, lane" if ours else ";") + f" out until {local(until)}")

    def _credit_exhausted(self, c: Case) -> None:
        until = c.t + CREDIT_PROBE
        self.mark_out(c.lane, until, "out of credits: " + c.why[:120].replace("\n", " "), c.t, "credit", "guess")
        c.res.update(retry_after=None, say=f"{c.lane} is out of credits ({c.why[:120]}); out until {local(until)}")

    def _logged_out(self, c: Case) -> None:
        cmd = LOGIN_CMD.get(c.lane, "route.py doctor")
        hint, say = c.login or (f"logged out: run `{cmd}`", f"{c.who} is not logged in: {{why}}")
        self.mark_out(c.lane, c.t + LOGOUT_OUT, hint, c.t, "logged_out", "guess")
        c.res.update(fix=f"run `{cmd}`", say=say.format(why=c.why[-200:].strip()))

    def _bad_request(self, c: Case) -> None:
        c.res.update(fix="the vendor rejected the request itself (schema, flags or arguments): fix the caller, not the lane", say=f"{c.who} rejected the request: {c.why[:300]}")

    def _network(self, c: Case) -> None:
        if c.answered:         # the final answer was captured before the connection dropped: the work is done, say so
            self.states.update(lambda s: s.out.pop(c.lane, None))
            c.res.update(outcome=None, data=answer.parse_data(c.res.get("text") or "", c.a.schema), warnings=["net_after_answer"], detail="")
        else:                # not a lane fault: nothing is marked out, the pair is not counted, and the caller decides when to resume (`resumable`, `ref`)
            c.res.update(retry_after=self._soon(), resumable=bool(c.res.get("ref") and c.res.get("started")), partial=bool(c.res.get("partial")),
                         say=f"{c.who} could not reach its service: {c.why[-200:].strip()}")

    def _killed(self, c: Case) -> None:
        c.res.update(resumable=bool(c.res.get("ref") and c.res.get("started")), partial=bool(c.res.get("partial")),
                     say=f"{c.who} was ended by a signal it did not get from route.py ({c.why}); retry")

    def _vendor_blip(self, c: Case) -> None:
        c.res.update(retry_after=self._soon(), say=f"{c.who} failed before any work: {c.why[:200]}")

    def _other(self, c: Case) -> None:
        c.res.update(say=f"{c.who}: {c.why[:300]}" if c.o.source == "event" else f"{c.who} exited {c.rc} without an error: {(c.why or c.tail)[-300:].strip()}")
