"""CreateTask / DestroyTask / QueryTask logic, shared by the ROS services and the UE HTTP API.

Only the cluster leader holds the task table. Every node exposes the public
entry points (create/destroy/query): the leader handles them itself, any
other node forwards them to the leader's leader/task/* services, which call
the *_local methods and never forward again (so requests cannot loop while
nodes disagree about the leader).

Task acceptance follows CONVENTIONS 5.3: create() succeeds only once the task
runs on an executor. If it is not running within create_wait_timeout_s, or the
leader changes meanwhile, the caller gets a failure and retries with the same
idempotency key, which returns the original task if it did start after all.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from ..core import constants as c
from ..core import idempotency
from ..core.task_spec import SpecError, TaskSpec, normalize_spec
from ..core.task_table import Cancel, StatusEvent, Submit, TaskRecord
from .task_store import TaskStore


@dataclass(frozen=True)
class GatewayConfig:
    task_types: tuple[str, ...] = ("PKCI", "PKCT")
    max_chain_depth: int = 5
    create_wait_timeout_s: float = 10.0


@dataclass(frozen=True)
class CreateReply:
    success: bool
    error_code: str = ""
    message: str = ""
    task_id: str = ""
    duplicate: bool = False
    assigned_node: str = ""
    dispatched_nodes: tuple[str, ...] = ()
    candidate_nodes: tuple[str, ...] = ()   # not tracked: the leader does not keep candidates


@dataclass(frozen=True)
class DestroyReply:
    success: bool
    error_code: str = ""
    message: str = ""
    final_status: str = ""


@dataclass(frozen=True)
class QueryReply:
    success: bool
    error_code: str = ""
    message: str = ""
    record: TaskRecord | None = None


class Forwarder(Protocol):
    """Calls the leader's leader/task/* services (ros/forwarder.py)."""

    def create(self, leader: str, spec: TaskSpec, idempotency_key: str) -> CreateReply: ...

    def destroy(self, leader: str, task_id: str, reason: str) -> DestroyReply: ...

    def query(self, leader: str, task_id: str) -> QueryReply: ...


class Gateway:
    def __init__(
        self,
        node_name: str,
        store: TaskStore,
        is_active: Callable[[], bool],
        leader_name: Callable[[], str],
        forwarder: Forwarder | None = None,
        config: GatewayConfig = GatewayConfig(),
        new_uuid: Callable[[], str] = lambda: str(uuid.uuid4()),
        wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        clock: Callable[[], float] = time.monotonic,
    ):
        self.node_name = node_name
        self._store = store
        self._is_active = is_active
        self._leader_name = leader_name
        self._forwarder = forwarder
        self._config = config
        self._new_uuid = new_uuid
        self._wall_ms = wall_ms
        self._clock = clock
        self._changed = threading.Condition()
        store.add_event_listener(self._on_events)

    def _on_events(self, events: tuple[StatusEvent, ...]) -> None:
        with self._changed:
            self._changed.notify_all()

    def _route(self) -> tuple[str, str]:
        """('local', '') on the ready leader, ('forward', leader) elsewhere, ('', reason) if impossible."""
        if self._is_active():
            return "local", ""
        leader = self._leader_name()
        if leader == self.node_name:
            return "", "leader %s is still collecting the cluster state; retry shortly" % leader
        if not leader:
            return "", "no cluster leader at the moment; retry shortly"
        if self._forwarder is None:
            return "", "cannot reach leader %s from here" % leader
        return "forward", leader

    def _normalize(self, spec: TaskSpec) -> TaskSpec:
        return normalize_spec(spec, task_types=self._config.task_types, max_chain_depth=self._config.max_chain_depth)

    # ------------------------------------------------------- public entry points
    def create(self, spec: TaskSpec, idempotency_key: str = "") -> CreateReply:
        try:
            spec = self._normalize(spec)
        except SpecError as exc:
            return CreateReply(False, exc.error_code, str(exc))
        key = idempotency_key.strip() or idempotency.anonymous(self._new_uuid())
        route, target = self._route()
        if route == "local":
            return self.create_local(spec, key)
        if route == "forward":
            return self._forwarder.create(target, spec, key)
        return CreateReply(False, c.NO_LEADER, target)

    def destroy(self, task_id: str, reason: str = "") -> DestroyReply:
        route, target = self._route()
        if route == "local":
            return self.destroy_local(task_id, reason)
        if route == "forward":
            return self._forwarder.destroy(target, task_id, reason)
        return DestroyReply(False, c.NO_LEADER, target)

    def query(self, task_id: str) -> QueryReply:
        route, target = self._route()
        if route == "local":
            return self.query_local(task_id)
        if route == "forward":
            return self._forwarder.query(target, task_id)
        return QueryReply(False, c.NO_LEADER, target)

    # -------------------------------------------------- leader-only handlers
    def create_local(self, spec: TaskSpec, idempotency_key: str) -> CreateReply:
        if not self._is_active():
            return CreateReply(False, c.NO_LEADER, "%s is not the ready cluster leader" % self.node_name)
        try:
            spec = self._normalize(spec)
        except SpecError as exc:
            return CreateReply(False, exc.error_code, str(exc))
        if not idempotency_key:
            return CreateReply(False, c.INVALID_INPUT, "idempotency_key is required")
        result = self._store.apply(Submit(self._new_uuid(), idempotency_key, spec, self._wall_ms()))
        if not result.ok:
            return CreateReply(False, result.error_code, result.message)
        record = self._wait(result.task_id, lambda r: r.status == c.RUNNING or r.terminal,
                            self._config.create_wait_timeout_s)
        return self._create_reply(record, result.task_id, result.duplicate)

    def _create_reply(self, record: TaskRecord | None, task_id: str, duplicate: bool) -> CreateReply:
        if record is None:      # the table was reset: this node lost the leadership meanwhile
            return CreateReply(False, c.NO_LEADER, "leader changed while starting task %s; retry with the same "
                               "idempotency key" % task_id, task_id=task_id, duplicate=duplicate)
        common = dict(task_id=task_id, duplicate=duplicate, assigned_node=record.primary)
        if record.status in (c.FAILED, c.CANCELLED):
            return CreateReply(False, record.error_code or c.DISPATCH_FAILED,
                               record.error_message or record.last_event, **common)
        if record.status in (c.RUNNING, c.FINISHED):
            return CreateReply(True, message="task %s %s on %s" % (task_id, record.status.lower(), record.primary),
                               dispatched_nodes=record.nodes, **common)
        return CreateReply(False, c.TIMEOUT, "task %s not running yet (%s: %s); retry with the same idempotency key"
                           % (task_id, record.status.lower(), record.last_event), **common)

    def _wait(self, task_id: str, done: Callable[[TaskRecord], bool], timeout: float) -> TaskRecord | None:
        deadline = self._clock() + timeout
        with self._changed:
            while True:
                record = self._store.get(task_id)
                remaining = deadline - self._clock()
                if record is None or done(record) or remaining <= 0:
                    return record
                self._changed.wait(min(remaining, 0.5))

    def destroy_local(self, task_id: str, reason: str = "") -> DestroyReply:
        if not self._is_active():
            return DestroyReply(False, c.NO_LEADER, "%s is not the ready cluster leader" % self.node_name)
        result = self._store.apply(Cancel(task_id, reason, self._wall_ms()))
        if result.ok:
            return DestroyReply(True, message="task %s cancelled" % task_id, final_status=c.CANCELLED)
        record = self._store.get(task_id)
        return DestroyReply(False, result.error_code, result.message, record.status if record else "")

    def query_local(self, task_id: str) -> QueryReply:
        if not self._is_active():
            return QueryReply(False, c.NO_LEADER, "%s is not the ready cluster leader" % self.node_name)
        record = self._store.get(task_id)
        if record is None:
            return QueryReply(False, c.NOT_FOUND, "task %s does not exist" % task_id)
        return QueryReply(True, message="ok", record=record)
