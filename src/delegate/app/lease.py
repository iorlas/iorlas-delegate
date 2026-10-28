"""The lease a run holds on its lane: claimed inside the state lock (the authoritative slot count), renewed by a heartbeat thread, stamped with the
worker's pid and the vendor ref as they appear, released when the run ends. Orphans of a SIGKILLed route.py are reaped at the claim."""
import contextlib
import os
import threading
from dataclasses import replace

from delegate.adapters import procs
from delegate.app import env
from delegate.contracts import history as hist
from delegate.contracts.state import Lease
from delegate.domain.lanes.leases import LEASE_RENEW, LEASE_TTL, LeaseBook, suspect


def orphan(v):
    """True when the lease's route.py process is gone (pid plus start time: an expired lease alone may be a laptop waking up) but its worker
    is still running: a SIGKILL on route.py leaves the worker, which is its own process group, editing the tree."""
    gone = procs.start_token(v.pid) != v.pid_start
    return gone and bool(v.worker_start) and procs.start_token(v.worker_pid) == v.worker_start


def reap(v):
    """Stop an orphaned worker's whole tree: SIGTERM, a short grace, SIGKILL (stop_tree). -> True when the worker is gone."""
    pg = int(v.worker_pid)
    procs.stop_tree(pg, grace=3, done=lambda: procs.start_token(pg) != v.worker_start)
    return procs.start_token(pg) != v.worker_start


class RunLease:
    def __init__(self, rid, role, task, lane, model, slots, canary_due, silence_s):
        self.rid, self.role, self.task, self.lane, self.model = rid, role, task, lane, model
        self.slots, self.canary_due, self.silence_s = slots, canary_due, silence_s
        self.peers, self.ended_peers = [], []
        self.beat = threading.Event()

    def claim(self):
        """-> None when the lease is held, else (reason, say): duplicate_task | busy."""
        taken = {}
        me_start = procs.start_token(os.getpid())

        def claim(st):
            live = self._live_after_reaping(st)
            st.leases = live
            if self.task and any(v.task == self.task for v in live.values()):
                taken["no"] = ("duplicate_task", f"task {self.task} already has a running worker")
            elif self.canary_due and any(v.canary == self.model and v.lane == self.lane for v in live.values()):
                taken["no"] = ("busy", f"{self.lane}/{self.model} has its canary running; wait for it")
            elif self.slots is not None and sum(1 for v in live.values() if v.lane == self.lane and not suspect(v, env.now())) >= self.slots:
                taken["no"] = ("busy", f"{self.lane} has all {self.slots} slots taken")
            else:
                self.peers = sorted(live)
                live[self.rid] = Lease(lane=self.lane, role=self.role, pid=os.getpid(), pid_start=me_start, start=env.now(), expires=env.now() + LEASE_TTL, task=self.task or "",
                                       silence_s=self.silence_s, canary=self.model if self.canary_due else None)
        env.update_state(claim)
        return taken.get("no")

    @staticmethod
    def _live_after_reaping(st):
        live = LeaseBook(st.leases).live(env.now())
        for k, v in list(st.leases.items()):
            if orphan(v):
                gone = reap(v)
                env.history_add(hist.ReapRow(ts=env.now(), run_id=k, role=v.role, task=v.task, lane=v.lane, reaped=gone, worker_pid=v.worker_pid))
                if gone:
                    live.pop(k, None)
                else:           # still running: its lease stays, so the same task or a full lane waits
                    live[k] = replace(v, expires=env.now() + LEASE_TTL)
            elif k not in live and v.pid_start and procs.start_token(v.pid) == v.pid_start:
                live[k] = v     # expired only because route.py could not renew it (the laptop slept): it is still running
        return live

    def _edit(self, fn):
        def apply(st):
            if self.rid in st.leases:
                fn(st.leases[self.rid])
        env.update_state(apply)

    def keep_alive(self):
        def renew():
            while not self.beat.wait(LEASE_RENEW):
                with contextlib.suppress(Exception):
                    self._edit(lambda v: setattr(v, "expires", env.now() + LEASE_TTL))
        threading.Thread(target=renew, daemon=True).start()

    def worker_started(self, pid, start):
        self._edit(lambda v: (setattr(v, "worker_pid", pid), setattr(v, "worker_start", start)))

    def stamp_event(self, wall):
        self._edit(lambda v: setattr(v, "last_event", wall))

    def stamp_ref(self, ref):
        self._edit(lambda v: setattr(v, "ref", ref))

    def release(self):
        """Stop the heartbeat and drop the lease; the peers that were running meanwhile are kept for the history row."""
        self.beat.set()

        def release(st):
            self.ended_peers = [k for k in LeaseBook(st.leases).live(env.now()) if k != self.rid]
            st.leases.pop(self.rid, None)
        with contextlib.suppress(Exception):
            env.update_state(release)

    @property
    def overlap(self):
        return sorted(set(self.peers) | set(self.ended_peers))
