"""Leader reconciliation (DESIGN 4.9, CONVENTIONS 5.6): compare the task table with what
nodes report and decide what to do about it.

What runs comes from the nodes themselves (CONVENTIONS 5.3), in two forms: the
summary in every heartbeat (NodeView.running_tasks), and the full TaskState in
each node's TaskList. Placement checks and stops use the heartbeats of online
nodes. Adoption needs full specs, so it reads TaskLists: those of online nodes
and of offline nodes still retained in the cluster view, so the tasks of a
node that went away (typically the old leader) are taken over and then moved.

A running task the table does not know, or knows at an older generation, is
adopted rather than stopped. That is how a new leader rebuilds its table, and
how tasks placed by a second leader during a split are merged back. Among
several copies of one task the newest generation wins; within it the copy on
its primary; then the smallest node name. Losing copies are stopped in later
rounds. A tombstone for the task on any node means some leader destroyed it:
it is adopted as Cancelled, or cancelled if the table still has it running
(another leader destroyed it while both led), and its remaining copies are
stopped.

reconcile() is a pure function of the table, the nodes' reports, the leader's
local memory of when it saw and did things, and the current monotonic time.
The controller runs it once per second, executes the returned actions, and
records what it sent in LeaderMemory. LeaderMemory is local to the current
leader and starts empty after every election: timers then restart, which
only delays decisions and never repeats a non-idempotent one.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Mapping, Union

from . import constants as c
from .cluster_view import NodeView, TaskListView, TaskStateView
from .generation import Generation
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
    executor_stall_grace_s: float = 10.0   # primary's executor silent this long: move its tasks
    mass_offline_window_s: float = 10.0    # mass-offline protection: more than mass_offline_ratio of the
    mass_offline_ratio: float = 1 / 3      # nodes online at the window's start went offline within it
    mass_offline_hold_s: float = 30.0      # longest protection: real mass failures must still be handled


# ---------------------------------------------------------------- actions

@dataclass(frozen=True)
class ScheduleTask:
    task_id: str
    expected: Generation


@dataclass(frozen=True)
class Reschedule:
    task_id: str
    expected: Generation
    reason: str


@dataclass(frozen=True)
class Dispatch:
    task_id: str
    generation: Generation
    node: str
    role: str


@dataclass(frozen=True)
class ConfirmRunning:
    """The primary runs the task but the table still says Scheduled: its Running
    report was lost (typically during a leader change). Record it on its behalf."""

    task_id: str
    generation: Generation
    node: str


@dataclass(frozen=True)
class AdoptTask:
    """A running task the table does not know (or knows at an older generation): take it over.
    cancelled: some node holds a tombstone for it."""

    task_id: str
    generation: Generation
    primary: str
    helpers: tuple[str, ...]
    idempotency_key: str
    name: str
    spec: TaskSpec
    reported_by: str
    cancelled: bool = False


@dataclass(frozen=True)
class CancelTask:
    """The table has the task alive, but a node holds a tombstone for it: another leader
    destroyed it (DESIGN 4.5). Cancelled tasks do not come back."""

    task_id: str
    reason: str


@dataclass(frozen=True)
class StopOrphan:
    """kind is one of constants.STOP_*; reason is for people."""

    node: str
    task_id: str
    generation: Generation
    kind: str
    reason: str


@dataclass(frozen=True)
class FailTask:
    task_id: str
    expected: Generation
    error_code: str
    message: str


Action = Union[ScheduleTask, Reschedule, Dispatch, ConfirmRunning, AdoptTask, CancelTask, StopOrphan, FailTask]


@dataclass
class LeaderMemory:
    """What the current leader has seen and sent, by local monotonic time."""

    observed: dict = field(default_factory=dict)     # task_id -> ((generation, status), since)
    dispatched: dict = field(default_factory=dict)   # (task_id, generation, node) -> last StartTask sent
    stopped: dict = field(default_factory=dict)      # (node, task_id, generation) -> last StopTask sent
    stalled: dict = field(default_factory=dict)      # node -> first seen with executor_alive=False
    online: set = field(default_factory=set)         # nodes online at the previous round
    went_offline: dict = field(default_factory=dict)  # node -> when it dropped out of the online set
    online_counts: deque = field(default_factory=deque)   # (time, online count), the last window's worth
    protecting_since: float | None = None            # mass-offline protection active since
    protected: tuple = (frozenset(), 0)              # (nodes that dropped, online count before)
    protection_spent: bool = False                   # held for mass_offline_hold_s; off until it clears

    def observe(self, records: Mapping[str, TaskRecord], now: float) -> None:
        """Note when each task entered its current (generation, status); forget removed tasks."""
        for task_id in [t for t in self.observed if t not in records]:
            del self.observed[task_id]
        for task_id, record in records.items():
            key = (record.generation, record.status)
            seen = self.observed.get(task_id)
            if seen is None or seen[0] != key:
                self.observed[task_id] = (key, now)

    def since(self, record: TaskRecord, now: float) -> float:
        seen = self.observed.get(record.task_id)
        return seen[1] if seen and seen[0] == (record.generation, record.status) else now

    def note_dispatch(self, task_id: str, generation: Generation, node: str, now: float) -> None:
        self.dispatched[(task_id, generation, node)] = now

    def note_stop(self, node: str, task_id: str, generation: Generation, now: float) -> None:
        self.stopped[(node, task_id, generation)] = now

    def prune(self, records: Mapping[str, TaskRecord]) -> None:
        """Drop send timestamps that refer to generations the table has moved past."""
        def live(task_id: str, generation: Generation) -> bool:
            record = records.get(task_id)
            return record is not None and not record.terminal and record.generation == generation

        self.dispatched = {k: v for k, v in self.dispatched.items() if live(k[0], k[1])}
        self.stopped = {k: v for k, v in self.stopped.items() if k[1] in records}

    def observe_nodes(self, online: Mapping[str, NodeView], now: float, config: ReconcileConfig) -> None:
        """Track stalled executors and nodes going offline; update mass-offline protection.

        Protection starts when more than mass_offline_ratio of the nodes online at the start of
        the last mass_offline_window_s went offline within it. It ends when enough of those come
        back, or after mass_offline_hold_s; it cannot restart before the drops leave the window."""
        for name in [n for n in self.stalled if n not in online or online[n].executor_alive]:
            del self.stalled[name]
        for name, view in online.items():
            if not view.executor_alive:
                self.stalled.setdefault(name, now)

        for name in self.online - set(online):
            self.went_offline[name] = now
        self.went_offline = {n: t for n, t in self.went_offline.items()
                             if n not in online and now - t <= config.mass_offline_window_s}
        self.online = set(online)
        self.online_counts.append((now, len(online)))
        while len(self.online_counts) > 1 and now - self.online_counts[1][0] >= config.mass_offline_window_s:
            self.online_counts.popleft()
        baseline = self.online_counts[0][1]           # online at (about) the start of the window

        if self.protecting_since is None:
            if len(self.went_offline) <= config.mass_offline_ratio * baseline:
                self.protection_spent = False
            elif not self.protection_spent:
                self.protecting_since = now
                self.protected = (frozenset(self.went_offline), baseline)
            return
        dropped, baseline = self.protected
        if len(dropped - self.online) <= config.mass_offline_ratio * baseline:
            self.protecting_since = None              # they are back
        elif now - self.protecting_since >= config.mass_offline_hold_s:
            self.protecting_since, self.protection_spent = None, True

    @property
    def protecting(self) -> bool:
        return self.protecting_since is not None


def _due(last: float | None, now: float, period: float) -> bool:
    return last is None or now - last >= period


def reconcile(
    records: Mapping[str, TaskRecord],
    online: Mapping[str, NodeView],
    task_lists: Mapping[str, TaskListView],
    memory: LeaderMemory,
    now: float,
    config: ReconcileConfig,
) -> list[Action]:
    """online: heartbeats of online nodes. task_lists: the up-to-date TaskLists of online nodes
    and of retained offline ones (a node whose TaskList lags its heartbeat is left out).
    Pending tasks come first, in the table's order (pending_tasks: by priority)."""
    memory.observe_nodes(online, now, config)
    buried = {t: name for name in sorted(task_lists, reverse=True) for t in task_lists[name].tombstones}
    adoptions, rejected = _adoptions(records, task_lists, buried)
    actions: list[Action] = list(adoptions.values())
    cancelled = {task_id for task_id, record in records.items()
                 if task_id in buried and not record.terminal and task_id not in adoptions}
    actions.extend(CancelTask(task_id, "destroyed elsewhere: tombstone on %s" % buried[task_id])
                   for task_id in sorted(cancelled))
    pending = [r for r in records.values()
               if r.status == c.PENDING and r.task_id not in adoptions and r.task_id not in cancelled]
    for record in sorted(pending, key=lambda r: -r.spec.priority):
        since = memory.since(record, now)
        if now - since >= config.pending_timeout_s:
            actions.append(FailTask(record.task_id, record.generation, c.NO_FEASIBLE_NODE,
                                    "no feasible node for %.0f s" % (now - since)))
        else:
            actions.append(ScheduleTask(record.task_id, record.generation))
    for task_id in sorted(records):
        record = records[task_id]
        if record.terminal or record.status == c.PENDING or task_id in adoptions or task_id in cancelled:
            continue
        actions.extend(_check_placement(record, online, memory, now, config))
    actions.extend(_orphans(records, online, memory, now, config, adoptions, rejected))
    return actions


def _adoptions(records, task_lists, buried) -> tuple[dict, dict]:
    """Row 1-2. Copies newer than the table: {task_id: AdoptTask}, {task_id: (stop kind, reason)}."""
    copies: dict[str, list[tuple[str, TaskStateView]]] = {}
    for name in sorted(task_lists):
        for state in task_lists[name].tasks:
            record = records.get(state.task_id)
            if record is None or (not record.terminal and state.generation > record.generation):
                copies.setdefault(state.task_id, []).append((name, state))
    owners = {r.idempotency_key: r.task_id for r in records.values()}
    adoptions, rejected = {}, {}
    for task_id, found in sorted(copies.items()):
        generation = max(state.generation for _, state in found)
        newest = [(node, state) for node, state in found if state.generation == generation]
        node, winner = min(newest, key=lambda copy: (copy[1].role != c.ROLE_PRIMARY or copy[0] != copy[1].primary,
                                                     copy[0]))
        owner = owners.get(winner.idempotency_key)
        if owner is not None and owner != task_id:
            rejected[task_id] = (c.STOP_DUPLICATE, "duplicate of task %s" % owner)
            continue
        try:
            spec = TaskSpec.from_json(winner.spec_json)
        except ValueError as exc:
            rejected[task_id] = (c.STOP_STALE, "unreadable spec: %s" % exc)
            continue
        primary = winner.primary or node
        helpers = {h for _, state in newest for h in state.helpers}
        helpers |= {n for n, state in newest if state.role == c.ROLE_HELPER}
        adoptions[task_id] = AdoptTask(task_id, generation, primary, tuple(sorted(helpers - {primary})),
                                       winner.idempotency_key, winner.name, spec, node, task_id in buried)
    return adoptions, rejected


def _check_placement(record, online, memory, now, config) -> list[Action]:
    """Rows 4-8."""
    since = memory.since(record, now)
    primary = online.get(record.primary)
    if primary is None:
        if memory.protecting:
            return []          # it may be this leader that lost its links, not the nodes their power
        return [Reschedule(record.task_id, record.generation, "primary %s offline" % record.primary)]
    stalled = memory.stalled.get(record.primary)
    if stalled is not None and now - stalled >= config.executor_stall_grace_s:
        return [Reschedule(record.task_id, record.generation,
                           "executor on %s silent for %.0f s" % (record.primary, now - stalled))]
    actions: list[Action] = []
    if not primary.runs(record.task_id, record.generation):
        waited = now - since
        if record.status == c.RUNNING and waited >= config.missing_grace_s:
            return [Reschedule(record.task_id, record.generation, "task lost on %s" % record.primary)]
        if record.status == c.SCHEDULED and waited >= config.start_timeout_s:
            return [Reschedule(record.task_id, record.generation, "not started on %s" % record.primary)]
    elif record.status == c.SCHEDULED:
        actions.append(ConfirmRunning(record.task_id, record.generation, record.primary))
    for node in record.nodes:
        view = online.get(node)
        if view is None or view.runs(record.task_id, record.generation):
            continue   # an offline helper is the primary's problem (min_participants)
        if record.status == c.RUNNING and node == record.primary:
            continue   # within the grace period: NodeStatus may simply lag behind
        if _due(memory.dispatched.get((record.task_id, record.generation, node)), now, config.dispatch_retry_s):
            role = c.ROLE_PRIMARY if node == record.primary else c.ROLE_HELPER
            actions.append(Dispatch(record.task_id, record.generation, node, role))
    return actions


def _orphans(records, online, memory, now, config, adoptions, rejected) -> list[Action]:
    """Rows 2, 9, 10: copies on online nodes that should not run. A task the table does not
    know and did not adopt is left alone: its node's TaskList has not caught up yet."""
    actions: list[Action] = []
    for name in sorted(online):
        for running in online[name].running_tasks:
            if running.task_id in adoptions:
                continue          # adopted this round; losing copies are stopped next round
            record = records.get(running.task_id)
            if running.task_id in rejected:
                kind, reason = rejected[running.task_id]
            elif record is None:
                continue
            elif record.status == c.CANCELLED:
                kind, reason = c.STOP_CANCEL, "task cancelled"
            elif record.terminal:
                kind, reason = c.STOP_STALE, "task %s" % record.status.lower()
            elif running.generation < record.generation:
                kind, reason = c.STOP_STALE, "%s superseded by %s" % (running.generation, record.generation)
            elif name not in record.nodes:
                kind, reason = c.STOP_UNASSIGNED, "not assigned to %s" % name
            else:
                continue
            if _due(memory.stopped.get((name, running.task_id, running.generation)), now, config.stop_retry_s):
                actions.append(StopOrphan(name, running.task_id, running.generation, kind, reason))
    return actions
