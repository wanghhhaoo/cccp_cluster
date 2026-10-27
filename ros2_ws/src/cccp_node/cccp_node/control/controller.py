"""Cluster leader control loop: scheduling, dispatch and reconciliation.

Runs on every node and does nothing unless the node is the Raft leader. After
each election it first commits a Barrier, so the local replica holds every
task committed under earlier leaders, then waits warmup_s so ClusterView has
heard from all live nodes. Acting earlier would treat running tasks as
unknown orphans and stop them.

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

from ..consensus.base import Consensus
from ..core import constants as c
from ..core.cluster_view import ClusterView
from ..core.reconciler import (
    ConfirmRunning, Dispatch, FailTask, LeaderMemory, ReconcileConfig, Reschedule, ScheduleTask, StopOrphan,
    reconcile,
)
from ..core.scheduler import SchedulerConfig, schedule
from ..core.task_spec import TaskSpec
from ..core.task_table import Assign, Barrier, MarkFailed, Prune, ReportStatus, Requeue, TaskRecord

log = logging.getLogger(__name__)

# Errors after which starting the task anywhere else will not help either.
PERMANENT_START_ERRORS = frozenset({c.INVALID_INPUT, c.UNSUPPORTED_VERSION})


@dataclass(frozen=True)
class StartRequest:
    term: int
    task_id: int
    epoch: int
    role: str
    primary_node: str
    helper_nodes: tuple[str, ...]
    spec: TaskSpec


@dataclass(frozen=True)
class DispatchResult:
    success: bool
    error_code: str = ""
    message: str = ""


class Dispatcher(Protocol):
    def start(self, node: str, request: StartRequest) -> DispatchResult: ...

    def stop(self, node: str, term: int, task_id: int, epoch: int, reason: str) -> DispatchResult: ...


@dataclass(frozen=True)
class TaskReportView:
    """A /cluster/task_report message."""

    node_name: str
    task_id: int
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
    submit_timeout_s: float = 5.0
    avoid_failed_node_s: float = 60.0     # do not retry a node that failed to start the task
    prune_terminal_after_s: float = 3600.0


class LeaderController:
    def __init__(
        self,
        node_name: str,
        consensus: Consensus,
        cluster: ClusterView,
        dispatcher: Dispatcher,
        config: ControllerConfig = ControllerConfig(),
        clock: Callable[[], float] = time.monotonic,
        wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.node_name = node_name
        self._consensus = consensus
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
        self._failures: dict[int, dict[str, float]] = {}   # task_id -> node -> when start failed

    # ------------------------------------------------------------- inputs
    def report(self, report: TaskReportView) -> None:
        """Thread-safe; called from the TaskReport subscription. Applied by the loop."""
        self._reports.put(report)

    # ---------------------------------------------------------------- loop
    @property
    def active(self) -> bool:
        """True while this node is a ready leader past its warm-up."""
        return (self._ready_term != 0 and self._ready_term == self._consensus.term()
                and self._consensus.is_leader() and self._clock() - self._ready_at >= self._config.warmup_s)

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
        if not self._consensus.is_leader():
            if self._ready_term:
                log.info("no longer leader (term %d)", self._ready_term)
                self._ready_term = 0
            return False
        term = self._consensus.term()
        if self._ready_term == term:
            return True
        barrier = self._consensus.submit(Barrier(term), self._config.submit_timeout_s)
        if not barrier.ok:
            log.warning("leader barrier for term %d failed: %s", term, barrier.message)
            return False
        self._ready_term, self._ready_at = term, self._clock()
        self._memory = LeaderMemory()
        self._failures.clear()
        log.info("leader for term %d with %d tasks; acting after %.1f s warm-up",
                 term, len(self._consensus.records()), self._config.warmup_s)
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
            if apply:
                result = self._submit(ReportStatus(report.node_name, report.task_id, report.epoch, report.state,
                                                   report.error_code, report.message, self._wall_ms()))
                if not result.ok and result.error_code != c.STALE_EPOCH:
                    log.warning("task=%d epoch=%d node=%s report %s not applied: %s", report.task_id,
                                report.epoch, report.node_name, report.state, result.message)

    # ------------------------------------------------------------ reconcile
    def _reconcile(self, now: float) -> None:
        records = self._consensus.records()
        online = self._cluster.online(now)
        self._memory.observe(records, now)
        self._memory.prune(records)
        for action in reconcile(records, online, self._memory, now, self._config.reconcile):
            record = records.get(getattr(action, "task_id", 0))
            if isinstance(action, ScheduleTask):
                self._place(record, online, now)
            elif isinstance(action, Reschedule):
                log.info("task=%d epoch=%d reschedule: %s", action.task_id, action.expected_epoch, action.reason)
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
                log.debug("task=%d still pending: %s", record.task_id, result.message)
            return
        placement = result.placement
        assigned = self._submit(Assign(record.task_id, record.epoch, placement.primary, placement.helpers,
                                       self._wall_ms()))
        if not assigned.ok:
            return
        updated = self._consensus.get(record.task_id)
        log.info("task=%d epoch=%d term=%d scheduled primary=%s helpers=%s", updated.task_id, updated.epoch,
                 self._ready_term, updated.primary, ",".join(updated.helpers) or "-")
        for node in updated.nodes:      # commit first, then dispatch (CONVENTIONS 5.3)
            if not self._start(updated, node, now):
                break

    def _start(self, record: TaskRecord, node: str, now: float) -> bool:
        role = c.ROLE_PRIMARY if node == record.primary else c.ROLE_HELPER
        self._memory.note_dispatch(record.task_id, record.epoch, node, now)
        request = StartRequest(self._ready_term, record.task_id, record.epoch, role, record.primary,
                               record.helpers if role == c.ROLE_PRIMARY else (), record.spec)
        result = self._dispatcher.start(node, request)
        if result.success:
            return True
        log.warning("task=%d epoch=%d node=%s start failed: %s %s", record.task_id, record.epoch, node,
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
            log.warning("task=%d epoch=%d node=%s stop failed: %s", action.task_id, action.epoch,
                        action.node, result.message)

    def _prune(self, records: Mapping[int, TaskRecord], now: float) -> None:
        old = tuple(sorted(
            task_id for task_id, record in records.items()
            if record.terminal and now - self._memory.since(record, now) >= self._config.prune_terminal_after_s))
        if old:
            self._submit(Prune(old))
        for task_id in [t for t in self._failures if t not in records or records[t].terminal]:
            del self._failures[task_id]

    # -------------------------------------------------------------- helpers
    def _assigned_counts(self, exclude: int) -> Counter:
        counts: Counter = Counter()
        for record in self._consensus.records().values():
            if not record.terminal and record.task_id != exclude:
                counts.update(record.nodes)
        return counts

    def _recent_failures(self, task_id: int, now: float) -> set[str]:
        failed = self._failures.get(task_id, {})
        return {node for node, when in failed.items() if now - when < self._config.avoid_failed_node_s}

    def _submit(self, command):
        result = self._consensus.submit(command, self._config.submit_timeout_s)
        if not result.ok and result.error_code not in (c.STALE_EPOCH, c.INVALID_STATE):
            log.warning("%s not applied: %s %s", type(command).__name__, result.error_code, result.message)
        return result
