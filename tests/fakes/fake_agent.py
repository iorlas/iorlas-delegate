"""A fake agent CLI: prints a scripted Claude stream-json and does what the scenario says. No model, no network.

usage: fake_agent.py SCENARIO [PIDFILE]   (stdin is read and dropped, like a CLI that takes its brief there)
  normal    two events, a result, exit 0
  stall     one tool_use event, then silence for 300 s (SIGTERM ends it)
  hang      one event, then ignores SIGTERM and sleeps (only SIGKILL ends it)
  spawner   one event, then starts a child in a session of its own that ignores SIGTERM (its pid goes to PIDFILE) and sleeps: the node-shim shape
  slow      an event every 0.3 s for 3 s, then the result (never idle, never done quickly)
"""
import json
import signal
import subprocess
import sys
import time

CHILD = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(300)"


def say(**kw):
    print(json.dumps(kw), flush=True)


TOOL = dict(type="assistant", message={"id": "m1", "stop_reason": "tool_use", "usage": {"input_tokens": 3, "output_tokens": 1},
                                       "content": [{"type": "tool_use", "id": "tu1", "name": "Bash"}]})
RESULT = dict(type="result", result="fake answer", session_id="fake-1", is_error=False, num_turns=1, total_cost_usd=0.01,
              usage={"input_tokens": 3, "output_tokens": 2})


def main():
    scenario = sys.argv[1] if len(sys.argv) > 1 else "normal"
    sys.stdin.read()
    if scenario == "normal":
        say(**TOOL)
        say(**RESULT)
        return 0
    if scenario == "slow":
        for _ in range(10):
            say(**TOOL)
            time.sleep(0.3)
        say(**RESULT)
        return 0
    if scenario == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)      # before the first event: the event tells the test the setup is done
    if scenario == "spawner":
        c = subprocess.Popen([sys.executable, "-c", CHILD], start_new_session=True, stdout=subprocess.PIPE, text=True)
        c.stdout.readline()                                # the child ignores SIGTERM now; a SIGTERM earlier would kill it
        open(sys.argv[2], "w").write(str(c.pid))
    say(**TOOL)
    time.sleep(300)
    return 0


if __name__ == "__main__":
    sys.exit(main())
