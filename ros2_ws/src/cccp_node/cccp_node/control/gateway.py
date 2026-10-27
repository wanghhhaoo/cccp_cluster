"""CreateTask / DestroyTask / QueryTask logic, shared by the ROS services and the UE HTTP API.

Runs on every node. Writes go through consensus (forwarded to the leader);
reads come from the local replica. create() returns once the task is running
or has failed, or after create_wait_timeout_s with the task still on its way.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from ..consensus.base import Consensus
from ..core import constants as c
from ..core import idempotency
from ..core.task_spec import SpecError, TaskSpec, normalize_spec
from ..core.task_table import Cancel, StatusEvent, Submit, TaskRecord


@dataclass(frozen=True)
class GatewayConfig:
    task_types: tuple[str, ...] = ("PKCI", "PKCT")
    max_chain_depth: int = 5
    submit_timeout_s: float = 5.0
    create_wait_timeout_s: float = 10.0


@dataclass(frozen=True)
class CreateReply:
    success: bool
    error_code: str = ""
    message: str = ""
    task_id: int = 0
    duplicate: bool = False
    assigned_node: str = ""
    dispatched_nodes: tuple[str, ...] = ()
    candidate_nodes: tuple[str, ...] = ()   # not tracked yet: the leader does not persist candidates


@dataclass(frozen=True)
class DestroyReply:
    success: bool
    error_code: str = ""
    message: str = ""
    final_status: str = ""


class Gateway:
    def __init__(
        self,
        consensus: Consensus,
        config: GatewayConfig = GatewayConfig(),
        new_uuid: Callable[[], str] = lambda: uuid.uuid4().hex,
        wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        clock: Callable[[], float] = time.monotonic,
    ):
        self._consensus = consensus
        self._config = config
        self._new_uuid = new_uuid
        self._wall_ms = wall_ms
        self._clock = clock
        self._changed = threading.Condition()
        consensus.add_event_listener(self._on_events)

    def _on_events(self, events: tuple[StatusEvent, ...]) -> None:
        with self._changed:
            self._changed.notify_all()

    # --------------------------------------------------------------- create
    def create(self, spec: TaskSpec, idempotency_key: str = "") -> CreateReply:
        try:
            spec = normalize_spec(spec, task_types=self._config.task_types,
                                  max_chain_depth=self._config.max_chain_depth)
        except SpecError as exc:
            return CreateReply(False, exc.error_code, str(exc))
        key = idempotency_key.strip() or idempotency.anonymous(self._new_uuid())
        result = self._consensus.submit(Submit(key, spec, self._wall_ms()), self._config.submit_timeout_s)
        if not result.ok:
            return CreateReply(False, result.error_code, result.message)
        record = self._wait(result.task_id, lambda r: r.status == c.RUNNING or r.terminal,
                            self._config.create_wait_timeout_s)
        return self._create_reply(record, result.task_id, result.duplicate)

    def _create_reply(self, record: TaskRecord | None, task_id: int, duplicate: bool) -> CreateReply:
        if record is None:      # pruned meanwhile, or the local replica lags behind the commit
            return CreateReply(True, message="task %d submitted" % task_id, task_id=task_id, duplicate=duplicate)
        common = dict(task_id=task_id, duplicate=duplicate, assigned_node=record.primary)
        if record.status in (c.FAILED, c.CANCELLED):
            return CreateReply(False, record.error_code or c.DISPATCH_FAILED,
                               record.error_message or record.last_event, **common)
        if record.status in (c.RUNNING, c.FINISHED):
            return CreateReply(True, message="task %d %s on %s" % (task_id, record.status.lower(), record.primary),
                               dispatched_nodes=record.nodes, **common)
        return CreateReply(True, message="task %d %s, not running yet: %s"
                           % (task_id, record.status.lower(), record.last_event), **common)

    def _wait(self, task_id: int, done: Callable[[TaskRecord], bool], timeout: float) -> TaskRecord | None:
        deadline = self._clock() + timeout
        with self._changed:
            while True:
                record = self._consensus.get(task_id)
                remaining = deadline - self._clock()
                if (record is not None and done(record)) or remaining <= 0:
                    return record
                self._changed.wait(min(remaining, 0.5))

    # -------------------------------------------------------------- destroy
    def destroy(self, task_id: int, reason: str = "") -> DestroyReply:
        result = self._consensus.submit(Cancel(task_id, reason, self._wall_ms()), self._config.submit_timeout_s)
        if result.ok:
            return DestroyReply(True, message="task %d cancelled" % task_id, final_status=c.CANCELLED)
        record = self._consensus.get(task_id)
        return DestroyReply(False, result.error_code, result.message, record.status if record else "")

    # ---------------------------------------------------------------- query
    def query(self, task_id: int) -> TaskRecord | None:
        return self._consensus.get(task_id)
