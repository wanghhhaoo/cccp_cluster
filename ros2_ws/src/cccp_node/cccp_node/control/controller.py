"""Cluster leader control loop: scheduling, dispatch and reconciliation.

Runs on every node and does nothing unless the election says this node leads.
Each time it becomes leader for a new term it starts from an empty task table
and waits warmup_s, so ClusterView holds the NodeStatus of every live node;
the first reconciliation then adopts every running task from those reports
(CONVENTIONS 5.3). Until then the gateway refuses new tasks.

The controller owns no ROS objects: StartTask/StopTask go through the
Dispatcher protocol, TaskReports arrive through report(), and the clock is
injected, so the whole loop runs under plain pytest.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from ..core import constants as c
from ..core.cluster_view import ClusterView
from ..core.election import Leadership
from ..core.reconciler import (
    AdoptTask, ConfirmRunning, Dispatch, FailTask, LeaderMemory, ReconcileConfig, Reschedule, ScheduleTask,
    StopOrphan, reconcile,
)
from ..core.scheduler import SchedulerConfig, schedule
from ..core.task_spec import TaskSpec
from ..core.task_table import Adopt, Assign, MarkFailed, Prune, ReportStatus, Requeue, TaskRecord
from .task_store import TaskStore

log = logging.getLogger(__name__)

# Errors after which starting the task anywhere else will not help either.
PERMANENT_START_ERRORS = frozenset({c.INVALID_INPUT, c.UNSUPPORTED_VERSION})


@dataclass(frozen=True)
class StartRequest:
    term: int
    task_id: str
    epoch: int
    role: str
    primary_node: str
    helper_nodes: tuple[str, ...]
    idempotency_key: str
    name: str
    spec: TaskSpec


@dataclass(frozen=True)
class DispatchResult:
    success: bool
    error_code: str = ""
    message: str = ""


class LeadershipSource(Protocol):
    @property
    def state(self) -> Leadership: ...


class Dispatcher(Protocol):
    def start(self, node: str, request: StartRequest) -> DispatchResult: ...

    def stop(self, node: str, term: int, task_id: str, epoch: int, reason: str) -> DispatchResult: ...


@dataclass(frozen=True)
class TaskReportView:
    """A /cluster/task_report message."""

    node_name: str
    task_id: str
    epoch: int
    state: str
    error_code: str = ""
    message: str = ""
    incarnation: int = 0
    seq: int = 0


@dataclass(frozen=True)
class ControllerConfig:
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    reconcile: ReconcileConfig = field(default_factory=ReconcileConfig)
    reconcile_period_s: float = 1.0
    warmup_s: float = 5.0                 # = node_offline_timeout_s (CONVENTIONS 5.6)
    avoid_failed_node_s: float = 60.0     # do not retry a node that failed to start the task
    max_input_stalls: int = 3             # primaries whose input stopped before the task is failed
    prune_terminal_after_s: float = 3600.0


class LeaderController:
    def __init__(
        self,
        node_name: str,
        store: TaskStore,
        election: LeadershipSource,
        cluster: ClusterView,
        dispatcher: Dispatcher,
        config: ControllerConfig = ControllerConfig(),
        clock: Callable[[], float] = time.monotonic,
        wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.node_name = node_name
        self._store = store
        self._election = election
        self._cluster = cluster
        self._dispatcher = dispatcher
        self._config = config
        self._clock = clock
        self._wall_ms = wall_ms
        self._reports: queue.Queue[TaskReportView] = queue.Queue()
        self._last_report_seq: dict[tuple[str, int], int] = {}
        self._ready_term = 0
        self._ready_at = 0.0
        self._last_reconcile = float("-inf")
        self._memory = LeaderMemory()
        self._failures: dict[str, dict[str, float]] = {}   # task_id -> node -> when start failed
        self._stalls: Counter = Counter()                   # task_id -> primaries whose input stopped

    # ------------------------------------------------------------- inputs
    def report(self, report: TaskReportView) -> None:
        """Thread-safe; called from the TaskReport subscription. Applied by the loop."""
        self._reports.put(report)

    # ---------------------------------------------------------------- loop
    @property
    def active(self) -> bool:
        """True while this node is the leader past its warm-up: it then owns the task table."""
        state = self._election.state
        return (state.is_self and self._ready_term == state.term
                and self._clock() - self._ready_at >= self._config.warmup_s)

    @property
    def term(self) -> int:
        return self._ready_term

    def run(self, stop: threading.Event, period_s: float = 0.1) -> None:
        while not stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("leader controller step failed")
            stop.wait(period_s)

    def step(self) -> None:
        """Drain reports; reconcile once per reconcile_period_s. Called often (~10 Hz)."""
        if not self._ensure_leadership():
            self._drain_reports(apply=False)
            return
        self._drain_reports(apply=True)
        now = self._clock()
        if now - self._ready_at < self._config.warmup_s:
            return
        if now - self._last_reconcile >= self._config.reconcile_period_s:
            self._last_reconcile = now
            self._reconcile(now)

    def _ensure_leadership(self) -> bool:
        state = self._election.state
        if not state.is_self:
            if self._ready_term:
                log.info("no longer leader (term %d), now following %s term %d",
                         self._ready_term, state.leader or "nobody", state.term)
                self._ready_term = 0
            return False
        if self._ready_term == state.term:
            return True
        self._store.reset()          # rebuilt from executor reports after the warm-up
        self._ready_term, self._ready_at = state.term, self._clock()
        self._memory = LeaderMemory()
        self._failures.clear()
        self._stalls.clear()
        log.info("leader for term %d; adopting running tasks after %.1f s warm-up",
                 state.term, self._config.warmup_s)
        return True

    def _drain_reports(self, apply: bool) -> None:
        while True:
            try:
                report = self._reports.get_nowait()
            except queue.Empty:
                return
            key = (report.node_name, report.incarnation)
            if report.seq and report.seq <= self._last_report_seq.get(key, 0):
                continue
            self._last_report_seq[key] = report.seq
            if apply and not self._requeue_stalled(report):
                result = self._submit(ReportStatus(report.node_name, report.task_id, report.epoch, report.state,
                                                   report.error_code, report.message, self._wall_ms()))
                if not result.ok and result.error_code != c.STALE_EPOCH:
                    log.warning("task=%s epoch=%d node=%s report %s not applied: %s", report.task_id,
                                report.epoch, report.node_name, report.state, result.message)

    def _requeue_stalled(self, report: TaskReportView) -> bool:
        """The primary got no input frames for a while (NOT_READY, CONVENTIONS 5.5): the input may
        still be reachable from another node, so move the task there, avoiding this node. Fails the
        task (by not handling the report) after max_input_stalls primaries, or when it is pinned."""
        if report.state != c.REPORT_FAILED or report.error_code != c.NOT_READY:
            return False
        record = self._store.get(report.task_id)
        if (record is None or record.terminal or record.epoch != report.epoch
                or record.primary != report.node_name or record.spec.request_node):
            return False
        self._stalls[record.task_id] += 1
        if self._stalls[record.task_id] >= self._config.max_input_stalls:
            return False
        self._failures.setdefault(record.task_id, {})[report.node_name] = self._clock()
        self._submit(Requeue(record.task_id, record.epoch, "%s (%d/%d)" % (
            report.message or "no input on %s" % report.node_name, self._stalls[record.task_id],
            self._config.max_input_stalls), self._wall_ms()))
        return True

    # ------------------------------------------------------------ reconcile
    def _reconcile(self, now: float) -> None:
        records = self._store.records()
        online = self._cluster.online(now)
        self._memory.observe(records, now)
        self._memory.prune(records)
        for action in reconcile(records, online, self._memory, now, self._config.reconcile):
            record = records.get(getattr(action, "task_id", ""))
            if isinstance(action, AdoptTask):
                result = self._submit(Adopt(action.task_id, action.idempotency_key, action.name, action.spec,
                                            action.epoch, action.primary, action.helpers, action.reported_by,
                                            self._wall_ms()))
                if result.ok:
                    log.info("task=%s epoch=%d adopted from %s (primary %s)", action.task_id, action.epoch,
                             action.reported_by, action.primary)
            elif isinstance(action, ScheduleTask):
                self._place(record, online, now)
            elif isinstance(action, Reschedule):
                log.info("task=%s epoch=%d reschedule: %s", action.task_id, action.expected_epoch, action.reason)
                self._place(record, online, now, reason=action.reason)
            elif isinstance(action, Dispatch):
                self._start(record, action.node, now)
            elif isinstance(action, ConfirmRunning):
                self._submit(ReportStatus(action.node, action.task_id, action.epoch, c.REPORT_RUNNING,
                                          message="running on %s (seen in NodeStatus)" % action.node,
                                          now_ms=self._wall_ms()))
            elif isinstance(action, StopOrphan):
                self._stop(action, now)
            elif isinstance(action, FailTask):
                self._submit(MarkFailed(action.task_id, action.expected_epoch, action.error_code,
                                        action.message, self._wall_ms()))
        self._prune(records, now)

    def _place(self, record: TaskRecord, online, now: float, reason: str = "") -> None:
        spec = record.spec
        result = schedule(
            spec, online,
            reference=spec.input_node or self.node_name,
            leader=self.node_name,
            assigned=self._assigned_counts(exclude=record.task_id),
            config=self._config.scheduler,
            avoid=self._recent_failures(record.task_id, now),
        )
        if not result.ok:
            if reason:   # the current placement is unusable: go back to Pending and keep trying
                self._submit(Requeue(record.task_id, record.epoch, "%s; %s" % (reason, result.message),
                                     self._wall_ms()))
            else:
                log.debug("task=%s still pending: %s", record.task_id, result.message)
            return
        placement = result.placement
        assigned = self._submit(Assign(record.task_id, record.epoch, placement.primary, placement.helpers,
                                       self._wall_ms()))
        if not assigned.ok:
            return
        updated = self._store.get(record.task_id)
        log.info("task=%s epoch=%d term=%d scheduled primary=%s helpers=%s", updated.task_id, updated.epoch,
                 self._ready_term, updated.primary, ",".join(updated.helpers) or "-")
        for node in updated.nodes:      # commit first, then dispatch (CONVENTIONS 5.3)
            if not self._start(updated, node, now):
                break

    def _start(self, record: TaskRecord, node: str, now: float) -> bool:
        role = c.ROLE_PRIMARY if node == record.primary else c.ROLE_HELPER
        self._memory.note_dispatch(record.task_id, record.epoch, node, now)
        request = StartRequest(self._ready_term, record.task_id, record.epoch, role, record.primary,
                               record.helpers, record.idempotency_key, record.name, record.spec)
        result = self._dispatcher.start(node, request)
        if result.success:
            return True
        log.warning("task=%s epoch=%d node=%s start failed: %s %s", record.task_id, record.epoch, node,
                    result.error_code, result.message)
        if result.error_code == c.STALE_TERM:
            return False                # a newer leader exists; it will take over
        self._failures.setdefault(record.task_id, {})[node] = now
        message = "start on %s failed: %s" % (node, result.message or result.error_code)
        if result.error_code in PERMANENT_START_ERRORS:
            self._submit(MarkFailed(record.task_id, record.epoch, c.DISPATCH_FAILED, message, self._wall_ms()))
        else:
            self._submit(Requeue(record.task_id, record.epoch, message, self._wall_ms()))
        return False

    def _stop(self, action: StopOrphan, now: float) -> None:
        self._memory.note_stop(action.node, action.task_id, action.epoch, now)
        result = self._dispatcher.stop(action.node, self._ready_term, action.task_id, action.epoch, action.reason)
        if not result.success:
            log.warning("task=%s epoch=%d node=%s stop failed: %s", action.task_id, action.epoch,
                        action.node, result.message)

    def _prune(self, records: Mapping[str, TaskRecord], now: float) -> None:
        old = tuple(sorted(
            task_id for task_id, record in records.items()
            if record.terminal and now - self._memory.since(record, now) >= self._config.prune_terminal_after_s))
        if old:
            self._submit(Prune(old))
        for task_id in [t for t in self._failures if t not in records or records[t].terminal]:
            del self._failures[task_id]
        for task_id in [t for t in self._stalls if t not in records or records[t].terminal]:
            del self._stalls[task_id]

    # -------------------------------------------------------------- helpers
    def _assigned_counts(self, exclude: str) -> Counter:
        counts: Counter = Counter()
        for record in self._store.records().values():
            if not record.terminal and record.task_id != exclude:
                counts.update(record.nodes)
        return counts

    def _recent_failures(self, task_id: str, now: float) -> set[str]:
        failed = self._failures.get(task_id, {})
        return {node for node, when in failed.items() if now - when < self._config.avoid_failed_node_s}

    def _submit(self, command):
        result = self._store.apply(command)
        if not result.ok and result.error_code not in (c.STALE_EPOCH, c.INVALID_STATE):
            log.warning("%s not applied: %s %s", type(command).__name__, result.error_code, result.message)
        return result
