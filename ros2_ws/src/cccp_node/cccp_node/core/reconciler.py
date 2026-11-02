"""Leader reconciliation (CONVENTIONS 5.6): compare the task table with what
nodes report in NodeStatus.tasks and decide what to do about it.

Executors are the source of truth for what runs (CONVENTIONS 5.3): a running
task the table does not know, or knows at an older epoch, is adopted rather
than stopped. That is how a new leader rebuilds its table, and how tasks
placed by a second leader during a split are merged back. Among several
copies of one task the newest epoch wins; within it the copy on its primary;
then the smallest node name. Losing copies are stopped in later rounds.

reconcile() is a pure function of the table, the online nodes, the leader's
local memory of when it saw and did things, and the current monotonic time.
The controller runs it once per second, executes the returned actions, and
records what it sent in LeaderMemory. LeaderMemory is local to the current
leader and starts empty after every election: timers then restart, which
only delays decisions and never repeats a non-idempotent one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Union

from . import constants as c
from .cluster_view import NodeView, TaskStateView
from .task_spec import TaskSpec
from .task_table import TaskRecord


@dataclass(frozen=True)
class ReconcileConfig:
    dispatch_retry_s: float = 5.0     # resend StartTask to a node that has not started the task
    start_timeout_s: float = 15.0     # Scheduled this long without the primary running it: reschedule
    missing_grace_s: float = 3.0      # Running but absent from the primary's running_tasks this long: lost
    pending_timeout_s: float = 8.0    # Pending this long without a feasible node: fail (shorter than
                                      # the gateway's create wait, so callers get a definite answer)
    stop_retry_s: float = 5.0         # resend StopTask for an orphan still running


# ---------------------------------------------------------------- actions

@dataclass(frozen=True)
class ScheduleTask:
    task_id: str
    expected_epoch: int


@dataclass(frozen=True)
class Reschedule:
    task_id: str
    expected_epoch: int
    reason: str


@dataclass(frozen=True)
class Dispatch:
    task_id: str
    epoch: int
    node: str
    role: str


@dataclass(frozen=True)
class ConfirmRunning:
    """The primary runs the task but the table still says Scheduled: its Running
    report was lost (typically during a leader change). Record it on its behalf."""

    task_id: str
    epoch: int
    node: str


@dataclass(frozen=True)
class AdoptTask:
    """A running task the table does not know (or knows at an older epoch): take it over."""

    task_id: str
    epoch: int
    primary: str
    helpers: tuple[str, ...]
    idempotency_key: str
    name: str
    spec: TaskSpec
    reported_by: str


@dataclass(frozen=True)
class StopOrphan:
    node: str
    task_id: str
    epoch: int
    reason: str


@dataclass(frozen=True)
class FailTask:
    task_id: str
    expected_epoch: int
    error_code: str
    message: str


Action = Union[ScheduleTask, Reschedule, Dispatch, ConfirmRunning, AdoptTask, StopOrphan, FailTask]


@dataclass
class LeaderMemory:
    """What the current leader has seen and sent, by local monotonic time."""

    observed: dict = field(default_factory=dict)     # task_id -> ((epoch, status), since)
    dispatched: dict = field(default_factory=dict)   # (task_id, epoch, node) -> last StartTask sent
    stopped: dict = field(default_factory=dict)      # (node, task_id, epoch) -> last StopTask sent

    def observe(self, records: Mapping[str, TaskRecord], now: float) -> None:
        """Note when each task entered its current (epoch, status); forget removed tasks."""
        for task_id in [t for t in self.observed if t not in records]:
            del self.observed[task_id]
        for task_id, record in records.items():
            key = (record.epoch, record.status)
            seen = self.observed.get(task_id)
            if seen is None or seen[0] != key:
                self.observed[task_id] = (key, now)

    def since(self, record: TaskRecord, now: float) -> float:
        seen = self.observed.get(record.task_id)
        return seen[1] if seen and seen[0] == (record.epoch, record.status) else now

    def note_dispatch(self, task_id: str, epoch: int, node: str, now: float) -> None:
        self.dispatched[(task_id, epoch, node)] = now

    def note_stop(self, node: str, task_id: str, epoch: int, now: float) -> None:
        self.stopped[(node, task_id, epoch)] = now

    def prune(self, records: Mapping[str, TaskRecord]) -> None:
        """Drop send timestamps that refer to epochs the table has moved past."""
        def live(task_id: str, epoch: int) -> bool:
            record = records.get(task_id)
            return record is not None and not record.terminal and record.epoch == epoch

        self.dispatched = {k: v for k, v in self.dispatched.items() if live(k[0], k[1])}
        self.stopped = {k: v for k, v in self.stopped.items() if k[1] in records}


def _due(last: float | None, now: float, period: float) -> bool:
    return last is None or now - last >= period


def reconcile(
    records: Mapping[str, TaskRecord],
    online: Mapping[str, NodeView],
    memory: LeaderMemory,
    now: float,
    config: ReconcileConfig,
) -> list[Action]:
    adoptions, rejected = _adoptions(records, online)
    actions: list[Action] = list(adoptions.values())
    for task_id in sorted(records):
        record = records[task_id]
        if record.terminal or task_id in adoptions:
            continue
        since = memory.since(record, now)
        if record.status == c.PENDING:
            if now - since >= config.pending_timeout_s:
                actions.append(FailTask(task_id, record.epoch, c.NO_FEASIBLE_NODE,
                                        "no feasible node for %.0f s" % (now - since)))
            else:
                actions.append(ScheduleTask(task_id, record.epoch))
            continue
        actions.extend(_check_placement(record, online, memory, now, since, config))
    actions.extend(_orphans(records, online, memory, now, config, adoptions, rejected))
    return actions


def _adoptions(records, online) -> tuple[dict, dict]:
    """Running copies newer than the table: {task_id: AdoptTask}, {task_id: reason not to adopt}."""
    copies: dict[str, list[tuple[str, TaskStateView]]] = {}
    for name in sorted(online):
        for state in online[name].running_tasks:
            record = records.get(state.task_id)
            if record is None or (not record.terminal and state.epoch > record.epoch):
                copies.setdefault(state.task_id, []).append((name, state))
    owners = {r.idempotency_key: r.task_id for r in records.values()}
    adoptions, rejected = {}, {}
    for task_id, found in sorted(copies.items()):
        epoch = max(state.epoch for _, state in found)
        newest = [(node, state) for node, state in found if state.epoch == epoch]
        node, winner = min(newest, key=lambda copy: (copy[1].role != c.ROLE_PRIMARY or copy[0] != copy[1].primary,
                                                     copy[0]))
        owner = owners.get(winner.idempotency_key)
        if owner is not None and owner != task_id:
            rejected[task_id] = "duplicate of task %s" % owner
            continue
        try:
            spec = TaskSpec.from_json(winner.spec_json)
        except ValueError as exc:
            rejected[task_id] = "unreadable spec: %s" % exc
            continue
        primary = winner.primary or node
        helpers = {h for _, state in newest for h in state.helpers}
        helpers |= {n for n, state in newest if state.role == c.ROLE_HELPER}
        adoptions[task_id] = AdoptTask(task_id, epoch, primary, tuple(sorted(helpers - {primary})),
                                       winner.idempotency_key, winner.name, spec, node)
    return adoptions, rejected


def _check_placement(record, online, memory, now, since, config) -> list[Action]:
    primary = online.get(record.primary)
    if primary is None:
        return [Reschedule(record.task_id, record.epoch, "primary %s offline" % record.primary)]
    actions: list[Action] = []
    if not primary.runs(record.task_id, record.epoch):
        waited = now - since
        if record.status == c.RUNNING and waited >= config.missing_grace_s:
            return [Reschedule(record.task_id, record.epoch, "task lost on %s" % record.primary)]
        if record.status == c.SCHEDULED and waited >= config.start_timeout_s:
            return [Reschedule(record.task_id, record.epoch, "not started on %s" % record.primary)]
    elif record.status == c.SCHEDULED:
        actions.append(ConfirmRunning(record.task_id, record.epoch, record.primary))
    for node in record.nodes:
        view = online.get(node)
        if view is None or view.runs(record.task_id, record.epoch):
            continue   # an offline helper is the primary's problem (min_participants)
        if record.status == c.RUNNING and node == record.primary:
            continue   # within the grace period: NodeStatus may simply lag behind
        if _due(memory.dispatched.get((record.task_id, record.epoch, node)), now, config.dispatch_retry_s):
            role = c.ROLE_PRIMARY if node == record.primary else c.ROLE_HELPER
            actions.append(Dispatch(record.task_id, record.epoch, node, role))
    return actions


def _orphans(records, online, memory, now, config, adoptions, rejected) -> list[Action]:
    actions: list[Action] = []
    for name in sorted(online):
        for running in online[name].running_tasks:
            if running.task_id in adoptions:
                continue          # adopted this round; losing copies are stopped next round
            record = records.get(running.task_id)
            if running.task_id in rejected:
                reason = rejected[running.task_id]
            elif record is None:
                reason = "unknown task"
            elif record.terminal:
                reason = "task %s" % record.status.lower()
            elif running.epoch < record.epoch:
                reason = "epoch %d superseded by %d" % (running.epoch, record.epoch)
            elif name not in record.nodes:
                reason = "not assigned to %s" % name
            else:
                continue
            if _due(memory.stopped.get((name, running.task_id, running.epoch)), now, config.stop_retry_s):
                actions.append(StopOrphan(name, running.task_id, running.epoch, reason))
    return actions
