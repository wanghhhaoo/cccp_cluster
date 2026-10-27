"""Replicated task table: the cluster's single source of truth for tasks.

TaskTable is a deterministic state machine. The consensus layer delivers the
same commands in the same order to every node, and each node feeds them to
apply(), so all replicas stay identical (CONVENTIONS 5.1). apply() therefore
must not read clocks, random sources, files, the environment or any other
local state (CONVENTIONS 5.2): ids are allocated from counters inside the
table, and anything else a command needs, such as a timestamp, travels in
the command itself. Records are immutable, so readers can keep them without
being able to modify the table behind apply()'s back.

Every state change advances a table-wide sequence number and yields a
StatusEvent, which the cluster leader publishes as a TaskStatus message.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from typing import Union

from . import constants as c
from .task_spec import TaskSpec

SNAPSHOT_VERSION = 1

_RANK = {c.PENDING: 0, c.SCHEDULED: 1, c.RUNNING: 2, c.FINISHED: 3, c.FAILED: 3, c.CANCELLED: 3}
_REPORTED = {c.REPORT_RUNNING: c.RUNNING, c.REPORT_FINISHED: c.FINISHED, c.REPORT_FAILED: c.FAILED}


@dataclass(frozen=True)
class TaskRecord:
    task_id: int
    idempotency_key: str
    spec: TaskSpec
    name: str
    status: str = c.PENDING
    epoch: int = 0
    primary: str = ""
    helpers: tuple[str, ...] = ()
    error_code: str = ""
    error_message: str = ""
    last_event: str = ""
    submitted_ms: int = 0  # caller wall clocks: for display only, never compare (CONVENTIONS 4.3)
    updated_ms: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in c.TERMINAL

    @property
    def nodes(self) -> tuple[str, ...]:
        return ((self.primary,) if self.primary else ()) + self.helpers

    @property
    def order(self) -> tuple[int, int]:
        """(epoch, status rank). Only ever increases (CONVENTIONS 5.5)."""
        return self.epoch, _RANK[self.status]


@dataclass(frozen=True)
class StatusEvent:
    seq: int
    task_id: int
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
    task_id: int = 0
    duplicate: bool = False
    events: tuple[StatusEvent, ...] = ()


# --------------------------------------------------------------------- commands
# OP is a plain class attribute (no annotation), so it is not a dataclass field.

@dataclass(frozen=True)
class Submit:
    """Gateway on any node. The gateway generates a key when the caller gave none."""

    OP = "submit"
    idempotency_key: str
    spec: TaskSpec
    now_ms: int = 0


@dataclass(frozen=True)
class Assign:
    """Leader: (re)schedule. Applies only while the task is still at expected_epoch;
    the epoch then advances by one, so two decisions made from the same snapshot
    cannot both succeed."""

    OP = "assign"
    task_id: int
    expected_epoch: int
    primary: str
    helpers: tuple[str, ...] = ()
    now_ms: int = 0


@dataclass(frozen=True)
class Requeue:
    """Leader: put a task back to Pending (e.g. its primary is gone and no node fits yet)."""

    OP = "requeue"
    task_id: int
    expected_epoch: int
    reason: str = ""
    now_ms: int = 0


@dataclass(frozen=True)
class ReportStatus:
    """Leader, relaying a TaskReport from an executing node."""

    OP = "report_status"
    node: str
    task_id: int
    epoch: int
    state: str
    error_code: str = ""
    message: str = ""
    now_ms: int = 0


@dataclass(frozen=True)
class MarkFailed:
    """Leader: failures the leader detects itself (dispatch failed, pending timeout)."""

    OP = "mark_failed"
    task_id: int
    expected_epoch: int
    error_code: str
    message: str = ""
    now_ms: int = 0


@dataclass(frozen=True)
class Cancel:
    """Gateway on any node (DestroyTask)."""

    OP = "cancel"
    task_id: int
    reason: str = ""
    now_ms: int = 0


@dataclass(frozen=True)
class Prune:
    """Leader: forget finished tasks. Non-terminal ids are skipped."""

    OP = "prune"
    task_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class Barrier:
    """Leader: no-op committed right after election.

    A new Raft leader learns which entries of earlier terms are committed only
    once an entry of its own term commits; until then its replica can lag and
    miss tasks. The controller waits for this command before acting.
    """

    OP = "barrier"
    term: int = 0


Command = Union[Submit, Assign, Requeue, ReportStatus, MarkFailed, Cancel, Prune, Barrier]
_COMMANDS = {cls.OP: cls for cls in (Submit, Assign, Requeue, ReportStatus, MarkFailed, Cancel, Prune, Barrier)}


def command_to_dict(command: Command) -> dict:
    """JSON-ready form used as the replicated log entry."""
    data = {"op": command.OP}
    for f in fields(command):
        value = getattr(command, f.name)
        if isinstance(value, TaskSpec):
            value = value.to_dict()
        elif isinstance(value, tuple):
            value = list(value)
        data[f.name] = value
    return data


def command_from_dict(data: dict) -> Command:
    values = dict(data)
    cls = _COMMANDS.get(values.pop("op", None))
    if cls is None:
        raise ValueError("unknown command op: %r" % data.get("op"))
    if "spec" in values:
        values["spec"] = TaskSpec.from_dict(values["spec"])
    for key, value in values.items():
        if isinstance(value, list):
            values[key] = tuple(value)
    return cls(**values)


def _error(error_code: str, message: str, task_id: int = 0) -> ApplyResult:
    return ApplyResult(False, error_code=error_code, message=message, task_id=task_id)


# ------------------------------------------------------------------------ table

class TaskTable:
    def __init__(self) -> None:
        self._tasks: dict[int, TaskRecord] = {}
        self._by_key: dict[str, int] = {}
        self._next_id = 1
        self._seq = 0

    # ------------------------------------------------------------------ reads
    @property
    def seq(self) -> int:
        """Sequence number of the latest status change."""
        return self._seq

    def get(self, task_id: int) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def find_by_key(self, idempotency_key: str) -> TaskRecord | None:
        task_id = self._by_key.get(idempotency_key)
        return None if task_id is None else self._tasks[task_id]

    def tasks(self) -> list[TaskRecord]:
        """All records in task_id order."""
        return list(self._tasks.values())

    def active_tasks(self) -> list[TaskRecord]:
        return [r for r in self._tasks.values() if not r.terminal]

    # ----------------------------------------------------------------- writes
    def apply(self, command: Command) -> ApplyResult:
        return getattr(self, "_apply_" + command.OP)(command)

    def _apply_submit(self, cmd: Submit) -> ApplyResult:
        if not cmd.idempotency_key:
            return _error(c.INVALID_INPUT, "idempotency_key is required")
        existing = self._by_key.get(cmd.idempotency_key)
        if existing is not None:
            return ApplyResult(True, message="duplicate of task %d" % existing, task_id=existing, duplicate=True)
        task_id = self._next_id
        self._next_id += 1
        spec = cmd.spec
        record = TaskRecord(
            task_id=task_id,
            idempotency_key=cmd.idempotency_key,
            spec=spec,
            name=spec.name or "%s-%d" % (spec.task_type.lower(), task_id),
            last_event="submitted",
            submitted_ms=cmd.now_ms,
            updated_ms=cmd.now_ms,
        )
        self._tasks[task_id] = record
        self._by_key[cmd.idempotency_key] = task_id
        return ApplyResult(True, message="submitted", task_id=task_id, events=(self._emit(record),))

    def _apply_assign(self, cmd: Assign) -> ApplyResult:
        record, error = self._changeable(cmd.task_id, cmd.expected_epoch)
        if error:
            return error
        helpers = cmd.helpers
        if not cmd.primary or not all(helpers) or len({cmd.primary, *helpers}) != 1 + len(helpers):
            return _error(c.INVALID_INPUT, "primary and helpers must be distinct node names", cmd.task_id)
        if len(helpers) > record.spec.helper_count:
            return _error(c.INVALID_INPUT, "task %d takes at most %d helpers"
                          % (cmd.task_id, record.spec.helper_count), cmd.task_id)
        message = "scheduled to %s" % cmd.primary
        if helpers:
            message += " with helpers %s" % ", ".join(helpers)
        return self._commit(record, message, cmd.now_ms, status=c.SCHEDULED, epoch=record.epoch + 1,
                            primary=cmd.primary, helpers=helpers, error_code="", error_message="")

    def _apply_requeue(self, cmd: Requeue) -> ApplyResult:
        record, error = self._changeable(cmd.task_id, cmd.expected_epoch)
        if error:
            return error
        return self._commit(record, cmd.reason or "requeued", cmd.now_ms, status=c.PENDING,
                            epoch=record.epoch + 1, primary="", helpers=())

    def _apply_report_status(self, cmd: ReportStatus) -> ApplyResult:
        record = self._tasks.get(cmd.task_id)
        if record is None:
            return _error(c.NOT_FOUND, "task %d does not exist" % cmd.task_id, cmd.task_id)
        target = _REPORTED.get(cmd.state)
        if target is None and cmd.state != c.REPORT_STOPPED:
            return _error(c.INVALID_INPUT, "unknown report state %r" % cmd.state, cmd.task_id)
        if cmd.epoch < record.epoch:
            return _error(c.STALE_EPOCH, "report for epoch %d, task %d is at epoch %d"
                          % (cmd.epoch, cmd.task_id, record.epoch), cmd.task_id)
        if cmd.epoch > record.epoch:
            return _error(c.INVALID_STATE, "report for future epoch %d of task %d"
                          % (cmd.epoch, cmd.task_id), cmd.task_id)
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
        record, error = self._changeable(cmd.task_id, cmd.expected_epoch)
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
                del self._by_key[record.idempotency_key]
                pruned += 1
        return ApplyResult(True, message="pruned %d" % pruned)

    def _apply_barrier(self, cmd: Barrier) -> ApplyResult:
        return ApplyResult(True, message="barrier for term %d" % cmd.term)

    # ---------------------------------------------------------------- helpers
    def _changeable(self, task_id: int, expected_epoch: int | None = None):
        """(record, None) if the task can still change, else (None, error result)."""
        record = self._tasks.get(task_id)
        if record is None:
            return None, _error(c.NOT_FOUND, "task %d does not exist" % task_id, task_id)
        if record.terminal:
            return None, _error(c.INVALID_STATE, "task %d is already %s" % (task_id, record.status), task_id)
        if expected_epoch is not None and expected_epoch != record.epoch:
            return None, _error(c.STALE_EPOCH, "task %d is at epoch %d, not %d"
                                % (task_id, record.epoch, expected_epoch), task_id)
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

    # --------------------------------------------------------------- snapshot
    def to_snapshot(self) -> dict:
        """JSON-ready copy of the whole table, for consensus log compaction."""
        tasks = []
        for record in self._tasks.values():
            data = asdict(record)
            data["spec"] = record.spec.to_dict()
            data["helpers"] = list(record.helpers)
            tasks.append(data)
        return {"version": SNAPSHOT_VERSION, "next_id": self._next_id, "seq": self._seq, "tasks": tasks}

    @classmethod
    def from_snapshot(cls, data: dict) -> TaskTable:
        if data.get("version") != SNAPSHOT_VERSION:
            raise ValueError("unsupported task table snapshot version %r" % data.get("version"))
        table = cls()
        table._next_id = data["next_id"]
        table._seq = data["seq"]
        for item in data["tasks"]:
            values = dict(item)
            values["spec"] = TaskSpec.from_dict(values["spec"])
            values["helpers"] = tuple(values["helpers"])
            record = TaskRecord(**values)
            table._tasks[record.task_id] = record
            table._by_key[record.idempotency_key] = record.task_id
        return table
