"""Executor-side term and generation checks for StartTask / StopTask (CONVENTIONS 5.4).

The executor asks the guard before acting on a command from the cluster
leader and tells it what it actually started or stopped. Commands from a
leader with a lower term than any seen before are rejected, which fences off
a deposed leader that has not noticed yet.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import constants as c
from .generation import NONE, Generation

START = "start"        # start a new session
RESTART = "restart"    # stop the running session, then start the newer generation
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
        self._running: dict[str, Generation] = {}

    def running(self) -> dict[str, Generation]:
        return dict(self._running)

    def _fence(self, term: int) -> Decision | None:
        if term < self.max_term:
            return Decision(REJECT, c.STALE_TERM, "term %d is older than %d" % (term, self.max_term))
        self.max_term = term
        return None

    def on_start(self, term: int, task_id: str, generation: Generation) -> Decision:
        rejected = self._fence(term)
        if rejected:
            return rejected
        generation = Generation(*generation)
        current = self._running.get(task_id)
        if current is None:
            return Decision(START)
        if generation == current:
            return Decision(NOOP, message="task %s %s already running" % (task_id, generation))
        if generation > current:
            return Decision(RESTART, message="replacing %s with %s" % (current, generation))
        return Decision(REJECT, c.STALE_EPOCH, "task %s is running %s, not %s" % (task_id, current, generation))

    def on_stop(self, term: int, task_id: str, generation: Generation) -> Decision:
        """(0, 0) stops any generation; stopping one older than the running one is a no-op."""
        rejected = self._fence(term)
        if rejected:
            return rejected
        generation = Generation(*generation)
        current = self._running.get(task_id)
        if current is None or (generation != NONE and generation < current):
            return Decision(NOOP, message="task %s %s is not running" % (task_id, generation))
        return Decision(STOP)

    def started(self, task_id: str, generation: Generation) -> None:
        self._running[task_id] = Generation(*generation)

    def stopped(self, task_id: str) -> None:
        self._running.pop(task_id, None)
