"""The routing table (`routing.json` + the owner's `config.json` overrides, validated at load) and the router's answer.

`Routing` is read, never written. The owner's `profile` (lane choices, acks) is not a setting and never reaches `Settings`. A router
`Decision` is `Run | Stop | LaneOut | Error`; `as_dict()` is the JSON that `route.py next` prints.
"""
from dataclasses import field
from typing import Any

from delegate.contracts.codec import record

V = 1

# Settings keys an older config.json or routing.json may still carry. Loading drops them with one warning each; any other unknown key fails.
RETIRED_SETTINGS = ("codex_weekly_used_override", "codex_resume_after_stall", "claude_resume_after_stall", "opencode_resume_after_stall",
                    "codex_plan_review_only", "host_lanes", "contained_network", "cache_roots", "credit")

def replaced_settings(lanes: dict) -> dict[str, str]:
    """The per-lane enable settings an old config.json may carry (`<lane>_enabled`, from the lane name) -> the lane. They moved to the lane
    profile: loading fails loudly with the exact fix, the old key is never honored."""
    return {f"{n.removesuffix('_SUBSCRIPTION').lower()}_enabled": n for n, spec in lanes.items() if spec.get("opt_in")}


def split_retired(settings: dict) -> tuple[dict, list[str]]:
    """(settings without the retired keys, the retired keys that were there, in file order)."""
    gone = [k for k in settings if k in RETIRED_SETTINGS]
    return {k: v for k, v in settings.items() if k not in RETIRED_SETTINGS}, gone


@record
class Gate:
    weekly: float
    five_hour: float


@record
class Ceiling:
    five_hour: float
    weekly: float
    orchestrator_five_hour: float
    orchestrator_weekly: float


@record
class Strain:
    five_hour: float
    weekly_pace: float


@record
class Burn:
    minutes: float
    min_left_percent: float


@record
class BreakerPolicy:
    n: int = 3
    window_min: float = 10
    ladder_min: tuple[float, ...] = (15, 30, 60)
    credit_probe_s: float = 6 * 3600
    logout_s: float = 900
    guess_s: float = 15 * 60
    stale_usage_s: float = 6 * 3600


@record
class Settings:
    orchestrator_reserve: Gate
    worker_lane_gate: Gate
    relaxed_ceiling: Ceiling
    strain: Strain
    recover: Strain
    burn: Burn
    max_parallel: int | None
    min_dwell_minutes: float
    usage_stale_minutes: float
    claude_weekly_reset: str = ""
    breaker: BreakerPolicy = field(default_factory=BreakerPolicy)


@record
class ModelSpec:
    family: str
    api: str
    agent: str | None = None
    cost: float = 1000
    loop: bool = True


@record
class LaneSpec:
    family: str
    kind: str
    reader: str
    runner: str
    label: str = ""
    ready: str = ""
    shared_with_orchestrator: bool = False
    opt_in: bool = False        # off until profile.lanes.<lane> is "enabled"; others turn on when ready


@record
class Step:
    model: str
    effort: str
    on: str
    mode: str = "read_only"
    cap: float | None = None
    flag: str | None = None
    idx: int | None = None


@record
class Role:
    about: str
    advance: str
    stop: str
    steps: tuple[Step, ...] = ()
    by_author: dict[str, tuple[Step, ...]] | None = None
    degraded_needs_consent: tuple[Step, ...] = ()
    exclude_families: tuple[str, ...] = ()
    order: str | None = None


@record
class Routing:
    V = V
    settings: Settings
    models: dict[str, ModelSpec]
    lanes: dict[str, LaneSpec]
    roles: dict[str, Role]
    premium: dict[str, Step | None]


@record
class CallArgs:
    """What the caller asked for, as the router sees it (no argv, no paths)."""

    authored_by: str | None = None
    premium: str | None = None
    allow_degraded: bool = False
    unattended: bool = False
    resume: str | None = None
    cap: float | None = None
    only_family: tuple[str, ...] = ()       # the families a call may use: the role's steps are filtered to them, no fallback outside
    engine_only: tuple[str, ...] = ()       # recipes only the engine runs: a write step needs a lane whose harness can deny them


@record
class AskUser:
    question: str
    options: tuple[str, ...]
    then: str


@record
class Run:
    role: str
    lane: str
    model: str
    effort: str
    mode: str
    pass_: str                     # strict | relaxed | resume
    rung: int | None = None
    fallback: bool | None = None
    fallback_reason: str | None = None
    first_lane: str | None = None
    cap: float | None = None
    flag: str | None = None
    warning: str | None = None
    trace: tuple[str, ...] | None = None       # None: a resume plan reasoned about nothing

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"action": "run", "role": self.role, "pass": self.pass_, "rung": self.rung, "fallback": self.fallback,
             "fallback_reason": self.fallback_reason, "first_lane": self.first_lane, "lane": self.lane, "model": self.model,
             "effort": self.effort, "mode": self.mode, "cap": self.cap}
        if self.pass_ != "resume":
            d["flag"] = self.flag
        if self.trace is not None:
            d["trace"] = list(self.trace)
        if self.warning:
            d["warning"] = self.warning
        return d


@record
class Stop:
    """No lane can take the call now (reason None), or the role's first choice only waits for a slot (reason busy)."""

    say: str
    role: str | None = None
    reason: str | None = None
    lane: str | None = None
    retry_after: float | None = None
    resets: dict[str, str] = field(default_factory=dict)
    ask_user: AskUser | None = None
    trace: tuple[str, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"action": "stop"}
        if self.reason == "busy":
            d |= {"reason": "busy", "lane": self.lane, "retry_after": self.retry_after, "say": self.say}
            extra: dict[str, Any] = {} if self.trace is None else {"trace": list(self.trace)}
            return d | extra
        d |= {"role": self.role, "say": self.say, "resets": dict(self.resets), "trace": list(self.trace or ())}
        if self.reason:
            d["reason"] = self.reason
        if self.ask_user:
            d["ask_user"] = {"question": self.ask_user.question, "options": list(self.ask_user.options), "then": self.ask_user.then}
        return d


@record
class LaneOut:
    """`--resume` on a lane that is held, out, benched or logged out: the thread stays on its lane, the call is refused with `outcome`."""

    lane: str
    outcome: str
    say: str
    retry_after: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"action": "stop", "lane": self.lane, "retry_after": self.retry_after, "outcome": self.outcome, "say": self.say}


@record
class Error:
    error: str

    def as_dict(self) -> dict[str, Any]:
        return {"action": "error", "error": self.error}


Decision = Run | Stop | LaneOut | Error
