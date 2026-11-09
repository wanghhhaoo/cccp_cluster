"""Cluster leader control loop: requests, scheduling, dispatch and reconciliation (DESIGN 4.1, 4.5, 4.9).

Runs on every node. Requests addressed to this node are answered whatever its
role (NOT_LEADER when it does not lead, NO_LEADER while it warms up); the rest
happens only while the election says this node leads. Each time it is elected
it starts from an empty task table and waits warmup_s, so the cluster view
holds every live node's heartbeat and TaskList; the first reconciliation then
adopts every running task (CONVENTIONS 5.3). A leader that only raises its
term past a lone claim was never out of office: it keeps its table and just
commands under the new term.

Nothing here waits for another node. Commands go out through a CommandSink and
their outcome comes back later as ExecEvents (on_event). Requests come in
through on_request and are answered through the reply callback once their
outcome is known: a create once the task runs, a destroy once its participants
stopped. Waiting requests are a table checked every step, not threads. A new
task is scheduled in the next step (~0.1 s), not at the next reconciliation.

The controller owns no ROS objects and takes an injected clock, so the whole
loop runs under plain pytest.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Protocol

from ..core import constants as c
from ..core import task_types
from ..core.cluster_view import ClusterView
from ..core.election import Leadership
from ..core.generation import Generation
from ..core.reconciler import (
    AdoptTask, CancelTask, ConfirmRunning, Dispatch, FailTask, LeaderMemory, ReconcileConfig, Reschedule,
    ScheduleTask, StopOrphan, reconcile,
)
from ..core.scheduler import SchedulerConfig, schedule
from ..core.task_table import (
    Adopt, Assign, Cancel, MarkFailed, Prune, ReportStatus, Requeue, Submit, TaskRecord,
)
from . import protocol as p
from .journal import Journal, recorder
from .protocol import ExecCommand, ExecEvent, Reply, Request
from .task_store import TaskStore

log = logging.getLogger(__name__)

# Refusals after which starting the task anywhere else will not help either.
PERMANENT_START_ERRORS = frozenset({c.INVALID_INPUT, c.UNSUPPORTED_VERSION})
_EVENT_STATES = {p.RUNNING: c.REPORT_RUNNING, p.FAILED: c.REPORT_FAILED, p.FINISHED: c.REPORT_FINISHED}


class LeadershipSource(Protocol):
    @property
    def state(self) -> Leadership: ...


class CommandSink(Protocol):
    def send(self, command: ExecCommand) -> None:
        """Publish; never blocks on the executors. Outcomes arrive as ExecEvents."""


@dataclass(frozen=True)
class ControllerConfig:
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    reconcile: ReconcileConfig = field(default_factory=ReconcileConfig)
    reconcile_period_s: float = 1.0
    warmup_s: float = 5.0                 # = node_offline_timeout_s (CONVENTIONS 5.6)
    avoid_failed_node_s: float = 60.0     # do not retry a node that refused to start the task
    max_input_stalls: int = 3             # primaries whose input stopped before the task is failed
    create_wait_timeout_s: float = 10.0   # a create is answered once the task runs, or after this
    destroy_wait_timeout_s: float = 10.0  # a destroy is answered once its participants stopped, or after this
    prune_terminal_after_s: float = 3600.0


@dataclass
class _Waiting:
    request: Request
    task_id: str
    deadline: float
    duplicate: bool = False


class LeaderController:
    def __init__(
        self,
        node_name: str,
        store: TaskStore,
        election: LeadershipSource,
        cluster: ClusterView,
        commands: CommandSink,
        config: ControllerConfig = ControllerConfig(),
        clock: Callable[[], float] = time.monotonic,
        wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        reply: Callable[[Reply], None] = lambda reply: None,
        new_uuid: Callable[[], str] = lambda: str(uuid.uuid4()),
        journal: Journal | None = None,
    ):
        self.node_name = node_name
        self._store = store
        self._election = election
        self._cluster = cluster
        self._commands = commands
        self._config = config
        self._clock = clock
        self._wall_ms = wall_ms
        self._reply = reply
        self._new_uuid = new_uuid
        self._record = recorder(journal)
        self._events: queue.Queue[ExecEvent] = queue.Queue()
        self._requests: queue.Queue[Request] = queue.Queue()
        self._last_event_seq: dict[tuple[str, int], int] = {}
        self._ready_term = 0
        self._claimed_term = 0               # Leadership.claimed_term of the current leadership
        self._ready_at = 0.0
        self._last_reconcile = float("-inf")
        self._command_id = 0
        self._memory = LeaderMemory()
        self._failures: dict[str, dict[str, float]] = {}   # task_id -> node -> when it refused the task
        self._stalls: Counter = Counter()                   # task_id -> primaries whose input stopped
        self._waiting: list[_Waiting] = []
        self._stopped: dict[str, set[str]] = {}             # cancelled task_id -> nodes that confirmed
        self._active_term = 0                               # term whose warm-up end was journaled
        store.add_event_listener(self._journal_table_events)

    # ------------------------------------------------------------- inputs
    def on_event(self, event: ExecEvent) -> None:
        """Thread-safe; called for every ExecEvent. Applied by the loop."""
        self._events.put(event)

    def on_request(self, request: Request) -> None:
        """Thread-safe; called for every Request. Answered by the loop through the reply callback."""
        self._requests.put(request)

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
        """One control period (~10 Hz): events, requests, new tasks; reconcile once per period."""
        leading = self._ensure_leadership()
        self._drain_events(apply=leading)
        ready = self.active
        now = self._clock()
        if ready and self._active_term != self._ready_term:
            self._active_term = self._ready_term
            self._record("leader_active", warmup_s=round(now - self._ready_at, 2))
        self._drain_requests(ready)
        if not ready:
            return
        pending = self._store.pending()
        if pending:
            online = self._cluster.online(now)
            for record in pending:
                self._place(record, online, now)
        if now - self._last_reconcile >= self._config.reconcile_period_s:
            self._last_reconcile = now
            self._reconcile(now)
        self._answer_waiting(now)

    def _ensure_leadership(self) -> bool:
        state = self._election.state
        if not state.is_self:
            if self._ready_term:
                log.info("no longer leader (term %d), now following %s term %d",
                         self._ready_term, state.leader or "nobody", state.term)
                self._record("leader_stop", led_term=self._ready_term)
                self._ready_term = self._claimed_term = 0
                self._flush_waiting(c.NOT_LEADER, "%s is no longer the cluster leader" % self.node_name)
            return False
        if self._ready_term == state.term:
            return True
        if self._ready_term and state.claimed_term and state.claimed_term == self._claimed_term:
            log.info("still leader, term %d -> %d (raised past a higher claim); keeping the task table",
                     self._ready_term, state.term)
            self._record("term_raised", from_term=self._ready_term)
            self._ready_term, self._command_id = state.term, 0
            return True
        self._flush_waiting(c.NO_LEADER, "the leader changed; retry with the same idempotency key")
        self._store.reset()          # rebuilt from executor reports after the warm-up
        self._ready_term, self._claimed_term, self._ready_at = state.term, state.claimed_term, self._clock()
        self._command_id = 0
        self._memory = LeaderMemory()
        self._failures.clear()
        self._stalls.clear()
        self._stopped.clear()
        log.info("leader for term %d; adopting running tasks after %.1f s warm-up",
                 state.term, self._config.warmup_s)
        self._record("leader_start", warmup_s=self._config.warmup_s)
        return True

    # ------------------------------------------------------------ events
    def _drain_events(self, apply: bool) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return
            key = (event.node, event.incarnation)
            if event.seq and event.seq <= self._last_event_seq.get(key, 0):
                continue
            self._last_event_seq[key] = event.seq
            if apply:
                self._apply_event(event)

    def _apply_event(self, event: ExecEvent) -> None:
        """DESIGN 3.5: only the primary's events drive the task; others are receipts."""
        record = self._store.get(event.task_id)
        if record is None or event.generation < record.generation:
            return
        if event.generation > record.generation:
            log.warning("task=%s gen=%s node=%s %s event for a generation the table has not reached (%s)",
                        event.task_id, event.generation, event.node, event.kind, record.generation)
            return
        if event.kind == p.STOPPED:
            if record.status == c.CANCELLED:
                self._stopped.setdefault(record.task_id, set()).add(event.node)
            return
        if event.kind == p.REJECTED:
            self._rejected(record, event)
            return
        # A type that needs every helper (HETEROMAP: each frame needs all five) would only wait
        # for a failed one until its timeout, so fail the task now.
        entry = task_types.find(record.spec.task_type)
        if (not record.terminal and entry is not None and entry.helpers_required and
                event.node in record.helpers and event.kind == p.FAILED):
            self._submit(MarkFailed(record.task_id, event.generation, event.error_code or c.INFERENCE_ERROR,
                                    event.message, self._wall_ms()))
            return
        if record.terminal or event.node != record.primary:
            return
        if event.kind == p.FAILED and event.error_code == c.NOT_READY and self._requeue_stalled(record, event):
            return
        state = _EVENT_STATES.get(event.kind)
        if state is None:
            return
        result = self._submit(ReportStatus(event.node, event.task_id, event.generation, state, event.error_code,
                                           event.message, self._wall_ms()))
        if not result.ok and result.error_code != c.STALE_EPOCH:
            log.warning("task=%s gen=%s node=%s %s not applied: %s", event.task_id, event.generation,
                        event.node, event.kind, result.message)

    def _rejected(self, record: TaskRecord, event: ExecEvent) -> None:
        """DESIGN 3.3: a primary or a helper refused a command of the current generation."""
        if event.error_code == c.STALE_TERM:
            log.info("node %s has seen a term above %d; a newer leader takes over", event.node, self._ready_term)
            return
        if event.error_code == c.STALE_EPOCH or record.terminal:
            return
        log.warning("task=%s gen=%s node=%s refused: %s %s", record.task_id, record.generation, event.node,
                    event.error_code, event.message)
        self._record("task_refused", task=record.task_id, gen=str(record.generation), by=event.node,
                     error_code=event.error_code, message=event.message)
        self._failures.setdefault(record.task_id, {})[event.node] = self._clock()
        message = "start on %s refused: %s" % (event.node, event.message or event.error_code)
        if event.error_code in PERMANENT_START_ERRORS:
            self._submit(MarkFailed(record.task_id, record.generation, c.DISPATCH_FAILED, message, self._wall_ms()))
        else:
            self._submit(Requeue(record.task_id, record.generation, self._ready_term, message, self._wall_ms()))

    def _requeue_stalled(self, record: TaskRecord, event: ExecEvent) -> bool:
        """The primary got no input frames for a while (NOT_READY, CONVENTIONS 5.5): the input may
        still be reachable from another node, so move the task there, avoiding this node. Fails the
        task (by not handling the event) after max_input_stalls primaries, or when it is pinned."""
        if record.spec.request_node:
            return False
        self._stalls[record.task_id] += 1
        if self._stalls[record.task_id] >= self._config.max_input_stalls:
            return False
        self._failures.setdefault(record.task_id, {})[event.node] = self._clock()
        self._record("task_input_stalled", task=record.task_id, gen=str(record.generation), primary=event.node,
                     stalls=self._stalls[record.task_id], max_stalls=self._config.max_input_stalls)
        self._submit(Requeue(record.task_id, record.generation, self._ready_term, "%s (%d/%d)" % (
            event.message or "no input on %s" % event.node, self._stalls[record.task_id],
            self._config.max_input_stalls), self._wall_ms()))
        return True

    # ----------------------------------------------------------- requests
    def _drain_requests(self, ready: bool) -> None:
        while True:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                return
            if request.target_leader != self.node_name:
                continue                 # meant for another leader: requests are handled at most once
            if not self._election.state.is_self:
                self._answer(request, c.NOT_LEADER, "%s is not the cluster leader" % self.node_name)
            elif not ready:
                self._answer(request, c.NO_LEADER, "leader %s is still collecting the cluster state; retry "
                             "shortly" % self.node_name)
            elif request.op == p.CREATE:
                self._create(request)
            elif request.op == p.DESTROY:
                self._destroy(request)
            elif request.op == p.QUERY:
                record = self._store.get(request.task_id)
                if record is None:
                    self._answer(request, c.NOT_FOUND, "task %s does not exist" % request.task_id)
                else:
                    self._answer(request, record=record, message="ok", full=True)
            else:
                self._answer(request, c.INVALID_INPUT, "unknown request %r" % request.op)

    def _create(self, request: Request) -> None:
        if request.spec is None or not request.idempotency_key:
            self._answer(request, c.INVALID_INPUT, "spec and idempotency_key are required")
            return
        result = self._submit(Submit(self._new_uuid(), request.idempotency_key, request.spec, self._wall_ms()))
        if not result.ok:
            self._answer(request, result.error_code, result.message)
            return
        self._waiting.append(_Waiting(request, result.task_id, self._clock() + self._config.create_wait_timeout_s,
                                      result.duplicate))

    def _destroy(self, request: Request) -> None:
        """DESIGN 4.5: cancel, stop every participant now, answer once they confirmed."""
        record = self._store.get(request.task_id)
        if record is None:
            self._answer(request, c.NOT_FOUND, "task %s does not exist" % request.task_id)
            return
        if record.terminal and record.status != c.CANCELLED:
            self._answer(request, c.INVALID_STATE, "task %s already %s" % (record.task_id, record.status.lower()),
                         record=record)
            return
        if record.status != c.CANCELLED:
            self._record("task_cancelled", task=record.task_id, by=request.origin_node, reason=request.reason)
            self._submit(Cancel(record.task_id, request.reason, self._wall_ms()))
            self._stopped[record.task_id] = set()
            record = self._store.get(record.task_id)
        if record.nodes:   # a retried destroy stops again: the earlier stop may have been lost
            self._stop(record.nodes, record.task_id, record.generation, c.STOP_CANCEL,
                       request.reason or "destroyed", self._clock())
        self._waiting.append(_Waiting(request, record.task_id, self._clock() + self._config.destroy_wait_timeout_s))

    def _answer_waiting(self, now: float) -> None:
        still = []
        for waiting in self._waiting:
            record = self._store.get(waiting.task_id)
            if record is None:
                self._answer(waiting.request, c.NOT_FOUND, "task %s no longer exists" % waiting.task_id)
            elif waiting.request.op == p.CREATE:
                if not self._answer_create(waiting, record, now):
                    still.append(waiting)
            elif self._stopped_everywhere(record, now):
                self._answer(waiting.request, record=record, message="task %s stopped" % record.task_id)
            elif now >= waiting.deadline:
                self._answer(waiting.request, c.TIMEOUT, "task %s cancelled but not yet stopped everywhere; retry "
                             "the destroy" % record.task_id, record=record)
            else:
                still.append(waiting)
        self._waiting = still

    def _answer_create(self, waiting: _Waiting, record: TaskRecord, now: float) -> bool:
        common = dict(record=record, duplicate=waiting.duplicate)
        if record.status in (c.RUNNING, c.FINISHED):
            self._answer(waiting.request, message="task %s %s on %s" % (
                record.task_id, record.status.lower(), record.primary), **common)
        elif record.status in (c.FAILED, c.CANCELLED):
            self._answer(waiting.request, record.error_code or c.DISPATCH_FAILED,
                         record.error_message or record.last_event, **common)
        elif now >= waiting.deadline:
            self._answer(waiting.request, c.TIMEOUT, "task %s not running yet (%s: %s); retry with the same "
                         "idempotency key" % (record.task_id, record.status.lower(), record.last_event), **common)
        else:
            return False
        return True

    def _stopped_everywhere(self, record: TaskRecord, now: float) -> bool:
        """Every participant of the current generation that is online confirmed the stop, by an
        event or by no longer listing the task. Offline ones cannot answer and are not waited for;
        reconciliation stops them when they come back."""
        online = self._cluster.online(now)
        confirmed = self._stopped.get(record.task_id, set())
        return all(node not in online or node in confirmed or not online[node].lists(record.task_id)
                   for node in record.nodes)

    def _flush_waiting(self, error_code: str, message: str) -> None:
        for waiting in self._waiting:
            self._answer(waiting.request, error_code, message, task_id=waiting.task_id)
        self._waiting = []

    def _answer(self, request: Request, error_code: str = "", message: str = "", record: TaskRecord | None = None,
                duplicate: bool = False, full: bool = False, task_id: str = "") -> None:
        snapshot = {}
        if record is not None:
            snapshot = dict(task_id=record.task_id, status=record.status, generation=record.generation,
                            primary=record.primary, helpers=record.helpers, record=record if full else None)
        elif task_id:
            snapshot = dict(task_id=task_id)
        self._reply(Reply(request.origin_node, request.origin_incarnation, request.request_id, self.node_name,
                          self._election.state.term, error_code, message, duplicate=duplicate, **snapshot))

    # ------------------------------------------------------------ reconcile
    def _reconcile(self, now: float) -> None:
        records = self._store.records()
        online = self._cluster.online(now)
        listed = {name: self._cluster.task_list(name) for name in (*online, *self._cluster.retained(now))}
        task_lists = {name: task_list for name, task_list in listed.items() if task_list is not None}
        self._memory.observe(records, now)
        self._memory.prune(records)
        protecting = self._memory.protecting
        actions = reconcile(records, online, task_lists, self._memory, now, self._config.reconcile)
        if self._memory.protecting != protecting:
            if self._memory.protecting:
                log.warning("mass offline: %d nodes dropped out at once; not moving their tasks for now",
                            len(self._memory.protected[0]))
                self._record("mass_offline", nodes=sorted(self._memory.protected[0]))
            else:
                ended = "held too long" if self._memory.protection_spent else "nodes are back"
                log.warning("mass offline protection ended (%s)", ended)
                self._record("mass_offline_end", reason=ended)
        dispatches: dict[tuple[str, Generation], list[str]] = {}
        for action in actions:
            record = records.get(getattr(action, "task_id", ""))
            if isinstance(action, AdoptTask):
                result = self._submit(Adopt(action.task_id, action.idempotency_key, action.name, action.spec,
                                            action.generation, action.primary, action.helpers, action.reported_by,
                                            action.cancelled, self._wall_ms()))
                if result.ok:
                    log.info("task=%s gen=%s adopted from %s (primary %s)%s", action.task_id, action.generation,
                             action.reported_by, action.primary, " as cancelled" if action.cancelled else "")
                    self._record("task_adopted", task=action.task_id, gen=str(action.generation),
                                 primary=action.primary, helpers=list(action.helpers), reported_by=action.reported_by,
                                 cancelled=action.cancelled)
            elif isinstance(action, CancelTask):
                log.warning("task=%s cancelled: %s", action.task_id, action.reason)
                self._record("task_cancelled", task=action.task_id, by="reconcile", reason=action.reason)
                self._submit(Cancel(action.task_id, action.reason, self._wall_ms()))
            elif isinstance(action, ScheduleTask):
                self._place(record, online, now)     # usually done already by step(); Assign is fenced
            elif isinstance(action, Reschedule):
                log.info("task=%s gen=%s reschedule: %s", action.task_id, action.expected, action.reason)
                self._record("task_reschedule", task=action.task_id, gen=str(action.expected), reason=action.reason)
                self._place(record, online, now, reason=action.reason)
            elif isinstance(action, Dispatch):
                dispatches.setdefault((action.task_id, action.generation), []).append(action.node)
            elif isinstance(action, ConfirmRunning):
                self._submit(ReportStatus(action.node, action.task_id, action.generation, c.REPORT_RUNNING,
                                          message="running on %s (seen in its heartbeat)" % action.node,
                                          now_ms=self._wall_ms()))
            elif isinstance(action, StopOrphan):
                self._stop((action.node,), action.task_id, action.generation, action.kind, action.reason, now)
            elif isinstance(action, FailTask):
                self._submit(MarkFailed(action.task_id, action.expected, action.error_code,
                                        action.message, self._wall_ms()))
        for (task_id, generation), nodes in dispatches.items():
            record = self._store.get(task_id)
            if record is not None and record.generation == generation:
                self._start(record, nodes, now)          # one command for every node still missing it
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
                self._submit(Requeue(record.task_id, record.generation, self._ready_term,
                                     "%s; %s" % (reason, result.message), self._wall_ms()))
            else:
                log.debug("task=%s still pending: %s", record.task_id, result.message)
            return
        placement = result.placement
        assigned = self._submit(Assign(record.task_id, record.generation, self._ready_term, placement.primary,
                                       placement.helpers, self._wall_ms()))
        if not assigned.ok:
            return
        updated = self._store.get(record.task_id)
        log.info("task=%s gen=%s scheduled primary=%s helpers=%s", updated.task_id, updated.generation,
                 updated.primary, ",".join(updated.helpers) or "-")
        self._record("task_scheduled", task=updated.task_id, gen=str(updated.generation), primary=updated.primary,
                     helpers=list(updated.helpers), reason=reason)
        self._start(updated, updated.nodes, now)      # table first, then the command (CONVENTIONS 5.3)

    def _start(self, record: TaskRecord, nodes: Iterable[str], now: float) -> None:
        nodes = tuple(nodes)
        for node in nodes:
            self._memory.note_dispatch(record.task_id, record.generation, node, now)
        self._commands.send(ExecCommand(
            self._ready_term, self._next_command_id(), nodes, p.START, record.task_id, record.generation,
            primary=record.primary, helpers=record.helpers, idempotency_key=record.idempotency_key,
            name=record.name, spec=record.spec, leader=self.node_name))

    def _stop(self, nodes: Iterable[str], task_id: str, generation: Generation, kind: str, reason: str,
              now: float) -> None:
        nodes = tuple(nodes)
        for node in nodes:
            self._memory.note_stop(node, task_id, generation, now)
        log.info("task=%s gen=%s stop on %s: %s (%s)", task_id, generation, ",".join(nodes), kind, reason)
        self._record("task_stop", task=task_id, gen=str(generation), nodes=list(nodes), kind=kind, reason=reason)
        self._commands.send(ExecCommand(self._ready_term, self._next_command_id(), nodes, p.STOP, task_id,
                                        generation, stop_kind=kind, reason=reason, leader=self.node_name))

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
        for task_id in [t for t in self._stopped if t not in records or t in old]:
            del self._stopped[task_id]

    # -------------------------------------------------------------- helpers
    def _journal_table_events(self, events) -> None:
        """Every status change of the task table (only the leader changes it)."""
        for event in events:
            self._record("task_status", task=event.task_id, status=event.status, epoch=event.epoch,
                         primary=event.node, message=event.message)

    def _next_command_id(self) -> int:
        self._command_id += 1
        return self._command_id

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
