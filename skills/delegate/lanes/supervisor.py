"""Idle watchdog for a worker (delegate-go0): first-event and silence deadlines, on a clock that does not run while the laptop sleeps.

Pure but for the clock it is given. `Watchdog.touch()` is called for every line the worker prints (stdout or stderr: any stream delta is
activity); `poll()` is called about every half second and is truthy once the worker is hung. route.run_proc stops a hung worker through
route.stop_tree (the whole process tree).

Silence limits per CLI (spike P7, 62-spikes-local.md): Claude's longest healthy gap was 225 s, so 600; Codex reasons silently for up to
2,298 s with nothing on disk, so 2400 (P7b, whether the live --json stream shows reasoning deltas, was not run: this is its fallback);
opencode 900. A first event must arrive within 120 s (longest seen 35 s).
"""
import time

LIMITS = {"claude": {"first_event_s": 120, "silence_s": 600},
          "codex": {"first_event_s": 120, "silence_s": 2400},
          "opencode": {"first_event_s": 120, "silence_s": 900}}
CLI_OF_KIND = {"claude_p": "claude", "codex": "codex", "opencode": "opencode"}      # a runner kind (route.how) -> its CLI
SLEEP_GAP_S = 15       # poll() runs every 0.5 s: a gap this long between two polls is a suspended machine, not a quiet worker
BEAT_S = 30            # at most one heartbeat callback per this many seconds


def limits(cli):
    return dict(LIMITS[cli])


class Watchdog:
    """first_event_s: no line at all within this long after start -> hung("first_event"). silence_s: no line for this long after the first -> hung("silence").
    `beat(wall_time)` (optional) is called from touch(), at most every BEAT_S: route stamps the lease with it, so a quiet lease can be called suspect."""

    def __init__(self, first_event_s, silence_s, clock=time.monotonic, beat=None, wall=time.time):
        self.first_event_s, self.silence_s, self.clock, self.beat, self.wall = first_event_s, silence_s, clock, beat, wall
        now = clock()
        self.start = self.last = self.polled = now
        self.events, self.why, self.idle_s = 0, None, 0.0
        self._beat_at = None

    def touch(self):
        now = self.clock()
        self.events += 1
        self.last = now
        if self.beat and (self._beat_at is None or now - self._beat_at >= BEAT_S):
            self._beat_at = now
            self.beat(self.wall())

    def poll(self):
        """-> "first_event" | "silence" once hung (sticky), else None."""
        if self.why:
            return self.why
        now = self.clock()
        gap = now - self.polled
        self.polled = now
        if gap > SLEEP_GAP_S:        # the machine slept (a clock that counts sleep would otherwise kill a healthy worker): that time is not silence
            self.start += gap
            self.last += gap
        self.idle_s = now - self.last
        if not self.events and now - self.start > self.first_event_s:
            self.why = "first_event"
        elif self.events and self.idle_s > self.silence_s:
            self.why = "silence"
        return self.why

    def describe(self):
        """Words for the answer: which deadline fired."""
        if self.why == "first_event":
            return f"printed nothing within {self.first_event_s}s"
        return f"was silent for {self.silence_s}s"
