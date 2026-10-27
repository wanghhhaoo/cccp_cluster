"""Executor-side term and epoch checks for StartTask / StopTask (CONVENTIONS 5.4).

The executor asks the guard before acting on a command from the cluster
leader and tells it what it actually started or stopped. Commands from a
leader with a lower term than any seen before are rejected, which fences off
a deposed leader that has not noticed yet.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import constants as c

START = "start"        # start a new session
RESTART = "restart"    # stop the running session, then start the newer epoch
STOP = "stop"
NOOP = "noop"          # already in the requested state: reply success
REJECT = "reject"


@dataclass(frozen=True)
class Decision:
    action: str
    error_code: str = ""
    message: str = ""

    @property
    def success(self) -> bool:
        return self.action != REJECT


class ExecutionGuard:
    def __init__(self) -> None:
        self.max_term = 0
        self._running: dict[int, int] = {}   # task_id -> epoch

    def running(self) -> dict[int, int]:
        return dict(self._running)

    def _fence(self, term: int) -> Decision | None:
        if term < self.max_term:
            return Decision(REJECT, c.STALE_TERM, "term %d is older than %d" % (term, self.max_term))
        self.max_term = term
        return None

    def on_start(self, term: int, task_id: int, epoch: int) -> Decision:
        rejected = self._fence(term)
        if rejected:
            return rejected
        current = self._running.get(task_id)
        if current is None:
            return Decision(START)
        if epoch == current:
            return Decision(NOOP, message="task %d epoch %d already running" % (task_id, epoch))
        if epoch > current:
            return Decision(RESTART, message="replacing epoch %d with %d" % (current, epoch))
        return Decision(REJECT, c.STALE_EPOCH, "task %d is running epoch %d, not %d" % (task_id, current, epoch))

    def on_stop(self, term: int, task_id: int, epoch: int) -> Decision:
        """epoch 0 stops any epoch; stopping an epoch older than the running one is a no-op."""
        rejected = self._fence(term)
        if rejected:
            return rejected
        current = self._running.get(task_id)
        if current is None or (epoch and epoch < current):
            return Decision(NOOP, message="task %d epoch %d is not running" % (task_id, epoch))
        return Decision(STOP)

    def started(self, task_id: int, epoch: int) -> None:
        self._running[task_id] = epoch

    def stopped(self, task_id: int) -> None:
        self._running.pop(task_id, None)
