"""Executor-side checks for commands from the cluster leader (DESIGN 3.8, CONVENTIONS 5.4).

The executor asks the guard before acting on a command and tells it what it
actually started or stopped. In order:

- commands not addressed to this node are ignored;
- commands from a leader with a lower term than any seen before are rejected,
  which fences off a deposed leader that has not noticed yet;
- generations (epoch, assign_term) decide between a repeated start (no-op), a
  newer session (restart) and a stale command (STALE_EPOCH);
- a start that would push the node past max_tasks is refused (BUSY): the
  scheduler counts what the task table assigns, which can be wrong while two
  leaders coexist or right after an election, so the executor counts what
  actually runs;
- a stop for a destroyed task (cancel) leaves a tombstone, whether or not the
  task was running, so a new leader adopting the task's other copies records it
  as Cancelled instead of bringing it back (DESIGN 4.5).
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Collection, Hashable

from . import constants as c
from .generation import NONE, Generation

START = "start"        # start a new session
RESTART = "restart"    # stop the running session, then start the newer generation
STOP = "stop"
NOOP = "noop"          # already in the requested state: reply success
REJECT = "reject"
IGNORE = "ignore"      # addressed to other nodes: no reply at all


@dataclass(frozen=True)
class Decision:
    action: str
    error_code: str = ""
    message: str = ""

    @property
    def success(self) -> bool:
        return self.action not in (REJECT, IGNORE)


class ExecutionGuard:
    def __init__(self, me: Hashable = None, max_tasks: int = 4, tombstone_ttl_s: float = 600.0,
                 max_tombstones: int = 16, clock: Callable[[], float] = time.monotonic) -> None:
        """me: how commands address this node (None accepts every command)."""
        self.me = me
        self.max_term = 0
        self._max_tasks = max_tasks
        self._tombstone_ttl_s = tombstone_ttl_s
        self._max_tombstones = max_tombstones
        self._clock = clock
        self._running: dict[str, Generation] = {}
        self._tombstones: OrderedDict[str, float] = OrderedDict()   # task_id -> when, oldest first

    def running(self) -> dict[str, Generation]:
        return dict(self._running)

    def tombstones(self) -> tuple[str, ...]:
        """task_ids stopped because they were destroyed, oldest first; expired ones are dropped."""
        now = self._clock()
        while self._tombstones and now - next(iter(self._tombstones.values())) >= self._tombstone_ttl_s:
            self._tombstones.popitem(last=False)
        return tuple(self._tombstones)

    def _check(self, targets: Collection | None, term: int) -> Decision | None:
        if targets is not None and self.me is not None and self.me not in targets:
            return Decision(IGNORE)
        if term < self.max_term:
            return Decision(REJECT, c.STALE_TERM, "term %d is older than %d" % (term, self.max_term))
        self.max_term = term
        return None

    def on_start(self, term: int, task_id: str, generation: Generation, targets: Collection | None = None) -> Decision:
        refused = self._check(targets, term)
        if refused:
            return refused
        generation = Generation(*generation)
        current = self._running.get(task_id)
        if current is not None:
            if generation == current:
                return Decision(NOOP, message="task %s %s already running" % (task_id, generation))
            if generation < current:
                return Decision(REJECT, c.STALE_EPOCH, "task %s is running %s, not %s"
                                % (task_id, current, generation))
        others = sum(1 for t in self._running if t != task_id)     # an older session of it is replaced
        if others >= self._max_tasks:
            return Decision(REJECT, c.BUSY, "%d tasks running, at most %d" % (others, self._max_tasks))
        if current is not None:
            return Decision(RESTART, message="replacing %s with %s" % (current, generation))
        return Decision(START)

    def on_stop(self, term: int, task_id: str, generation: Generation, cancel: bool = False,
                targets: Collection | None = None) -> Decision:
        """Stops the running session unless it is newer than the command's generation;
        (0, 0) stops any generation. A cancel leaves a tombstone unless the command is refused."""
        refused = self._check(targets, term)
        if refused:
            return refused
        generation = Generation(*generation)
        current = self._running.get(task_id)
        if current is not None and generation != NONE and generation < current:
            return Decision(REJECT, c.STALE_EPOCH, "task %s is running %s, newer than %s"
                            % (task_id, current, generation))
        if cancel:
            self._bury(task_id)
        if current is None:
            return Decision(NOOP, message="task %s %s is not running" % (task_id, generation))
        return Decision(STOP)

    def started(self, task_id: str, generation: Generation) -> None:
        self._running[task_id] = Generation(*generation)

    def stopped(self, task_id: str) -> None:
        self._running.pop(task_id, None)

    def _bury(self, task_id: str) -> None:
        self._tombstones.pop(task_id, None)
        self._tombstones[task_id] = self._clock()
        while len(self._tombstones) > self._max_tombstones:
            self._tombstones.popitem(last=False)
