"""Replace the supervisor's process run: the CLI's stdout lines go through the real parse, routing and summary, no process is spawned."""
import json
from unittest import mock

from delegate.app.supervisor import Supervisor


def replay(lines, rc=0, err="", timed_out=False):
    """-> a patcher: Supervisor.run feeds `lines` (dicts or strings) as the worker's stdout and ends with `rc`; the spec it was given is kept in `.specs`."""
    specs = []

    def run(self, spec, timeout):
        specs.append(spec)
        text = [json.dumps(x) if isinstance(x, dict) else x for x in lines]
        killed = False
        for line in text:
            if self._on_out(line + "\n"):
                killed = True
                break
        self.out.timed_out = timed_out
        return self.finish(rc, "".join(x + "\n" for x in text), err, killed)
    patcher = mock.patch.object(Supervisor, "run", run)
    patcher.specs = specs
    return patcher
