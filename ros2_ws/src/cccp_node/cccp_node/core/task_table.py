"""The cluster leader's task table: what should exist and who should run it (CONVENTIONS 5.2).

The table lives only in the current leader's memory. What executors actually
run is reported in every node's TaskList; after an election the new leader starts
with an empty table and rebuilds it from those reports (Adopt). Tasks that had
not reached any executor when the old leader died are lost: the caller sees
its request fail and retries with the same idempotency key (CONVENTIONS 5.3).

Each record carries its execution generation (epoch, assign_term). Commands
that change it (Assign, Requeue) or act on it (MarkFailed) name the generation
they expect, so two decisions made from the same view cannot both succeed.

All changes go through apply(command) and the records are immutable, so
readers can keep them without being able to change the table. Every status
change advances the table's sequence number and yields a StatusEvent, which
the leader publishes as TaskStatus (term, seq).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Union

from . import constants as c
from .generation import Generation
from .task_spec import TaskSpec

_RANK = {c.PENDING: 0, c.SCHEDULED: 1, c.RUNNING: 2, c.FINISHED: 3, c.FAILED: 3, c.CANCELLED: 3}
_REPORTED = {c.REPORT_RUNNING: c.RUNNING, c.REPORT_FINISHED: c.FINISHED, c.REPORT_FAILED: c.FAILED}


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    idempotency_key: str
    spec: TaskSpec
    name: str
    status: str = c.PENDING
    epoch: int = 0
    assign_term: int = 0
    primary: str = ""
    helpers: tuple[str, ...] = ()
    error_code: str = ""
    error_message: str = ""
    last_event: str = ""
    submitted_ms: int = 0  # wall clocks: for display only, never compare (CONVENTIONS 4.3)
    updated_ms: int = 0

    @property
    def result_url(self):
        if self.status == c.FINISHED and self.spec.task_type in c.BATCH_TYPES:
            return f"/api/ue/tasks/{self.task_id}/artifacts/{self.epoch}-{self.assign_term}"
        return ""

    @property
    def terminal(self) -> bool:
        return self.status in c.TERMINAL

    @property
    def nodes(self) -> tuple[str, ...]:
        return ((self.primary,) if self.primary else ()) + self.helpers

    @property
    def generation(self) -> Generation:
        return Generation(self.epoch, self.assign_term)

    @property
    def order(self) -> tuple[int, int, int]:
        """(epoch, assign_term, status rank). Only ever increases (CONVENTIONS 5.5)."""
        return self.epoch, self.assign_term, _RANK[self.status]


@dataclass(frozen=True)
class StatusEvent:
    seq: int
    task_id: str
    name: str
    task_type: str
    status: str
    epoch: int
    node: str
    message: str


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    error_code: str = ""
    message: str = ""
    task_id: str = ""
    duplicate: bool = False
    events: tuple[StatusEvent, ...] = ()


# --------------------------------------------------------------------- commands
# OP is a plain class attribute (no annotation), so it is not a dataclass field.

@dataclass(frozen=True)
class Submit:
    """Gateway on the leader. task_id is a fresh UUID; idempotency_key is never empty."""

    OP = "submit"
    task_id: str
    idempotency_key: str
    spec: TaskSpec
    now_ms: int = 0


@dataclass(frozen=True)
class Adopt:
    """Leader: take over a task that an executor reports running but this table does not
    know, or knows at an older generation (after an election, or a task another leader
    placed while two leaders coexisted). The generation is kept as reported: adopting
    is claiming, not reassigning, and a new assign_term would restart the session.
    cancelled: some node holds a tombstone for the task, so it was destroyed and must
    not come back to life (DESIGN 4.5)."""

    OP = "adopt"
    task_id: str
    idempotency_key: str
    name: str
    spec: TaskSpec
    generation: Generation
    primary: str
    helpers: tuple[str, ...] = ()
    reported_by: str = ""
    cancelled: bool = False
    now_ms: int = 0


@dataclass(frozen=True)
class Assign:
    """Leader of term: (re)schedule. Applies only while the task is still at the
    expected generation, which then moves to (epoch + 1, term)."""

    OP = "assign"
    task_id: str
    expected: Generation
    term: int
    primary: str
    helpers: tuple[str, ...] = ()
    now_ms: int = 0


@dataclass(frozen=True)
class Requeue:
    """Leader of term: put a task back to Pending (its primary is gone and no node fits
    yet, or a participant turned the start down). Also moves to (epoch + 1, term)."""

    OP = "requeue"
    task_id: str
    expected: Generation
    term: int
    reason: str = ""
    now_ms: int = 0


@dataclass(frozen=True)
class ReportStatus:
    """Leader, relaying an ExecEvent from an executing node."""

    OP = "report_status"
    node: str
    task_id: str
    generation: Generation
    state: str
    error_code: str = ""
    message: str = ""
    now_ms: int = 0


@dataclass(frozen=True)
class MarkFailed:
    """Leader: failures the leader detects itself (dispatch failed, no feasible node)."""

    OP = "mark_failed"
    task_id: str
    expected: Generation
    error_code: str
    message: str = ""
    now_ms: int = 0


@dataclass(frozen=True)
class Cancel:
    """Gateway on the leader (DestroyTask)."""

    OP = "cancel"
    task_id: str
    reason: str = ""
    now_ms: int = 0


@dataclass(frozen=True)
class Prune:
    """Leader: forget finished tasks. Non-terminal ids are skipped."""

    OP = "prune"
    task_ids: tuple[str, ...] = ()


Command = Union[Submit, Adopt, Assign, Requeue, ReportStatus, MarkFailed, Cancel, Prune]


def _error(error_code: str, message: str, task_id: str = "") -> ApplyResult:
    return ApplyResult(False, error_code=error_code, message=message, task_id=task_id)


# ------------------------------------------------------------------------ table

class TaskTable:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._by_key: dict[str, str] = {}
        self._seq = 0

    # ------------------------------------------------------------------ reads
    @property
    def seq(self) -> int:
        """Sequence number of the latest status change."""
        return self._seq

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def find_by_key(self, idempotency_key: str) -> TaskRecord | None:
        task_id = self._by_key.get(idempotency_key)
        return None if task_id is None else self._tasks[task_id]

    def tasks(self) -> list[TaskRecord]:
        """All records in insertion order."""
        return list(self._tasks.values())

    def active_tasks(self) -> list[TaskRecord]:
        return [r for r in self._tasks.values() if not r.terminal]

    def pending_tasks(self) -> list[TaskRecord]:
        """Pending records in the order to schedule them: higher priority first, then
        submission order (DESIGN 8.1). Insertion order stands in for submission time,
        since the wall clocks in the records must not be compared."""
        pending = [r for r in self._tasks.values() if r.status == c.PENDING]
        return sorted(pending, key=lambda r: -r.spec.priority)     # stable: keeps insertion order

    # ----------------------------------------------------------------- writes
    def apply(self, command: Command) -> ApplyResult:
        return getattr(self, "_apply_" + command.OP)(command)

    def _apply_submit(self, cmd: Submit) -> ApplyResult:
        if not cmd.idempotency_key or not cmd.task_id:
            return _error(c.INVALID_INPUT, "task_id and idempotency_key are required")
        existing = self._by_key.get(cmd.idempotency_key)
        if existing is not None:
            return ApplyResult(True, message="duplicate of task %s" % existing, task_id=existing, duplicate=True)
        if cmd.task_id in self._tasks:
            return _error(c.INVALID_INPUT, "task_id %s already exists" % cmd.task_id, cmd.task_id)
        spec = cmd.spec
        record = TaskRecord(
            task_id=cmd.task_id,
            idempotency_key=cmd.idempotency_key,
            spec=spec,
            name=spec.name or "%s-%s" % (spec.task_type.lower(), cmd.task_id[:8]),
            last_event="submitted",
            submitted_ms=cmd.now_ms,
            updated_ms=cmd.now_ms,
        )
        self._insert(record)
        return ApplyResult(True, message="submitted", task_id=cmd.task_id, events=(self._emit(record),))

    def _apply_adopt(self, cmd: Adopt) -> ApplyResult:
        known = self._tasks.get(cmd.task_id)
        if known is not None and (known.terminal or cmd.generation <= known.generation):
            return ApplyResult(True, message="no change", task_id=cmd.task_id)
        key = known.idempotency_key if known is not None else cmd.idempotency_key   # fixed at creation
        owner = self._by_key.get(key)
        if owner is not None and owner != cmd.task_id:
            return _error(c.INVALID_STATE, "idempotency key already used by task %s" % owner, cmd.task_id)
        generation = Generation(*cmd.generation)
        message = "adopted %s on %s (reported by %s)" % (generation, cmd.primary, cmd.reported_by or "?")
        if cmd.cancelled:
            message += ", destroyed before"
        record = TaskRecord(
            task_id=cmd.task_id,
            idempotency_key=key,
            spec=cmd.spec,
            name=cmd.name or "%s-%s" % (cmd.spec.task_type.lower(), cmd.task_id[:8]),
            status=c.CANCELLED if cmd.cancelled else c.RUNNING,
            epoch=generation.epoch,
            assign_term=generation.assign_term,
            primary=cmd.primary,
            helpers=tuple(cmd.helpers),
            error_code=c.CANCELLED_ERROR if cmd.cancelled else "",
            error_message="destroyed before the leader changed" if cmd.cancelled else "",
            last_event=message,
            submitted_ms=known.submitted_ms if known else cmd.now_ms,
            updated_ms=cmd.now_ms,
        )
        self._insert(record)
        return ApplyResult(True, message=message, task_id=cmd.task_id, events=(self._emit(record),))

    def _apply_assign(self, cmd: Assign) -> ApplyResult:
        record, error = self._changeable(cmd.task_id, cmd.expected)
        if error:
            return error
        helpers = cmd.helpers
        if not cmd.primary or not all(helpers) or len({cmd.primary, *helpers}) != 1 + len(helpers):
            return _error(c.INVALID_INPUT, "primary and helpers must be distinct node names", cmd.task_id)
        if len(helpers) > record.spec.helper_count:
            return _error(c.INVALID_INPUT, "task %s takes at most %d helpers"
                          % (cmd.task_id, record.spec.helper_count), cmd.task_id)
        message = "scheduled to %s" % cmd.primary
        if helpers:
            message += " with helpers %s" % ", ".join(helpers)
        epoch, assign_term = record.generation.next(cmd.term)
        return self._commit(record, message, cmd.now_ms, status=c.SCHEDULED, epoch=epoch, assign_term=assign_term,
                            primary=cmd.primary, helpers=helpers, error_code="", error_message="")

    def _apply_requeue(self, cmd: Requeue) -> ApplyResult:
        record, error = self._changeable(cmd.task_id, cmd.expected)
        if error:
            return error
        epoch, assign_term = record.generation.next(cmd.term)
        return self._commit(record, cmd.reason or "requeued", cmd.now_ms, status=c.PENDING,
                            epoch=epoch, assign_term=assign_term, primary="", helpers=())

    def _apply_report_status(self, cmd: ReportStatus) -> ApplyResult:
        record = self._tasks.get(cmd.task_id)
        if record is None:
            return _error(c.NOT_FOUND, "task %s does not exist" % cmd.task_id, cmd.task_id)
        target = _REPORTED.get(cmd.state)
        if target is None and cmd.state != c.REPORT_STOPPED:
            return _error(c.INVALID_INPUT, "unknown report state %r" % cmd.state, cmd.task_id)
        if cmd.generation < record.generation:
            return _error(c.STALE_EPOCH, "report for %s, task %s is at %s"
                          % (Generation(*cmd.generation), cmd.task_id, record.generation), cmd.task_id)
        if cmd.generation > record.generation:
            return _error(c.INVALID_STATE, "report for future generation %s of task %s"
                          % (Generation(*cmd.generation), cmd.task_id), cmd.task_id)
        # Only the task primary drives the task state; helper reports, Stopped
        # acknowledgements and late reports for finished tasks change nothing.
        if record.terminal or target is None or cmd.node != record.primary:
            return ApplyResult(True, message="ignored", task_id=cmd.task_id)
        changes = {"status": target}
        if target == c.FAILED:
            changes.update(error_code=cmd.error_code or c.INFERENCE_ERROR, error_message=cmd.message)
        message = cmd.message or "%s on %s" % (cmd.state.lower(), cmd.node)
        return self._commit(record, message, cmd.now_ms, **changes)

    def _apply_mark_failed(self, cmd: MarkFailed) -> ApplyResult:
        record, error = self._changeable(cmd.task_id, cmd.expected)
        if error:
            return error
        if not cmd.error_code:
            return _error(c.INVALID_INPUT, "error_code is required", cmd.task_id)
        return self._commit(record, cmd.message or cmd.error_code, cmd.now_ms, status=c.FAILED,
                            error_code=cmd.error_code, error_message=cmd.message)

    def _apply_cancel(self, cmd: Cancel) -> ApplyResult:
        record, error = self._changeable(cmd.task_id)
        if error:
            return error
        return self._commit(record, cmd.reason or "cancelled", cmd.now_ms, status=c.CANCELLED,
                            error_code=c.CANCELLED_ERROR, error_message=cmd.reason)

    def _apply_prune(self, cmd: Prune) -> ApplyResult:
        pruned = 0
        for task_id in cmd.task_ids:
            record = self._tasks.get(task_id)
            if record is not None and record.terminal:
                del self._tasks[task_id]
                if self._by_key.get(record.idempotency_key) == task_id:
                    del self._by_key[record.idempotency_key]
                pruned += 1
        return ApplyResult(True, message="pruned %d" % pruned)

    # ---------------------------------------------------------------- helpers
    def _insert(self, record: TaskRecord) -> None:
        self._tasks[record.task_id] = record
        self._by_key[record.idempotency_key] = record.task_id

    def _changeable(self, task_id: str, expected: Generation | None = None):
        """(record, None) if the task can still change, else (None, error result)."""
        record = self._tasks.get(task_id)
        if record is None:
            return None, _error(c.NOT_FOUND, "task %s does not exist" % task_id, task_id)
        if record.terminal:
            return None, _error(c.INVALID_STATE, "task %s is already %s" % (task_id, record.status), task_id)
        if expected is not None and tuple(expected) != record.generation:
            return None, _error(c.STALE_EPOCH, "task %s is at %s, not %s"
                                % (task_id, record.generation, Generation(*expected)), task_id)
        return record, None

    def _commit(self, record: TaskRecord, message: str, now_ms: int, **changes) -> ApplyResult:
        """Store an updated copy of record if it moves the task forward."""
        updated = replace(record, last_event=message, updated_ms=now_ms, **changes)
        if updated.order <= record.order:
            return ApplyResult(True, message="no change", task_id=record.task_id)
        self._tasks[record.task_id] = updated
        return ApplyResult(True, message=message, task_id=record.task_id, events=(self._emit(updated),))

    def _emit(self, record: TaskRecord) -> StatusEvent:
        self._seq += 1
        return StatusEvent(self._seq, record.task_id, record.name, record.spec.task_type,
                           record.status, record.epoch, record.primary, record.last_event)
