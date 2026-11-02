"""CreateTask / DestroyTask / QueryTask entry points, shared by the ROS services and the UE HTTP API.

Every node offers them; only the cluster leader holds the task table. The
gateway validates and normalizes a create, then sends a Request naming the one
leader that should handle it and waits for the matching Reply (DESIGN 4.1):

- this node is the ready leader: the Request goes to the local controller;
- another node leads: the Request goes out through the transport. That leader
  handles it itself or answers NOT_LEADER at once; it never forwards, so
  requests cannot travel between nodes that disagree about the leader;
- no leader known, or this node leads but is still warming up: NO_LEADER.

Nothing comes back within request_timeout_s (the leader died, or the reply was
lost): TIMEOUT. All of these are worth retrying, a create with the same
idempotency key, which then returns the original task if it did start.
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
from ..core.task_table import TaskRecord
from . import protocol as p
from .protocol import Reply, Request


@dataclass(frozen=True)
class GatewayConfig:
    task_types: tuple[str, ...] = ("PKCI", "PKCT")
    max_chain_depth: int = 5
    request_timeout_s: float = 15.0      # the leader's create/destroy wait (10 s) plus a margin


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


class RequestTransport(Protocol):
    def send(self, request: Request) -> None:
        """Deliver to request.target_leader; its Reply comes back through Gateway.on_reply."""


class Gateway:
    def __init__(
        self,
        node_name: str,
        is_active: Callable[[], bool],
        leader_name: Callable[[], str],
        local: Callable[[Request], None],
        transport: RequestTransport | None = None,
        config: GatewayConfig = GatewayConfig(),
        term: Callable[[], int] = lambda: 0,
        incarnation: int | None = None,
        new_uuid: Callable[[], str] = lambda: str(uuid.uuid4()),
    ):
        """local: hands a Request to this node's controller (LeaderController.on_request)."""
        self.node_name = node_name
        self.incarnation = incarnation if incarnation is not None else int(time.time() * 1000)
        self._is_active = is_active
        self._leader_name = leader_name
        self._local = local
        self._transport = transport
        self._config = config
        self._term = term
        self._new_uuid = new_uuid
        self._lock = threading.Lock()
        self._request_id = 0
        self._waiting: dict[int, list] = {}      # request_id -> [threading.Event, Reply | None]

    # ------------------------------------------------------- public entry points
    def create(self, spec: TaskSpec, idempotency_key: str = "") -> CreateReply:
        try:
            spec = normalize_spec(spec, task_types=self._config.task_types,
                                  max_chain_depth=self._config.max_chain_depth)
        except SpecError as exc:
            return CreateReply(False, exc.error_code, str(exc))
        key = idempotency_key.strip() or idempotency.anonymous(self._new_uuid())
        return _create_reply(self._call(p.CREATE, idempotency_key=key, spec=spec))

    def destroy(self, task_id: str, reason: str = "") -> DestroyReply:
        return _destroy_reply(self._call(p.DESTROY, task_id=task_id, reason=reason))

    def query(self, task_id: str) -> QueryReply:
        return _query_reply(self._call(p.QUERY, task_id=task_id))

    def on_reply(self, reply: Reply) -> None:
        """Thread-safe; called for every Reply seen, including those meant for other gateways."""
        if (reply.origin_node, reply.origin_incarnation) != (self.node_name, self.incarnation):
            return
        with self._lock:
            waiting = self._waiting.get(reply.request_id)
            if waiting is None:
                return                  # timed out already, or a repeat
            waiting[1] = reply
        waiting[0].set()

    # ------------------------------------------------------------- internals
    def _call(self, op: str, **fields) -> Reply:
        leader, problem = self._route()
        if problem:
            return self._failure(c.NO_LEADER, problem)
        with self._lock:
            self._request_id += 1
            request = Request(self.node_name, self.incarnation, self._request_id, leader, self._term(), op, **fields)
            waiting = self._waiting[request.request_id] = [threading.Event(), None]
        try:
            if leader == self.node_name:
                self._local(request)
            else:
                self._transport.send(request)
            waiting[0].wait(self._config.request_timeout_s)
        finally:
            with self._lock:
                self._waiting.pop(request.request_id, None)
        if waiting[1] is None:
            return self._failure(c.TIMEOUT, "no reply from leader %s within %.0f s; retry%s" % (
                leader, self._config.request_timeout_s, " with the same idempotency key" if op == p.CREATE else ""))
        return waiting[1]

    def _route(self) -> tuple[str, str]:
        """(leader to ask, "") or ("", why there is none to ask)."""
        if self._is_active():
            return self.node_name, ""
        leader = self._leader_name()
        if leader == self.node_name:
            return "", "leader %s is still collecting the cluster state; retry shortly" % leader
        if not leader:
            return "", "no cluster leader at the moment; retry shortly"
        if self._transport is None:
            return "", "cannot reach leader %s from here" % leader
        return leader, ""

    def _failure(self, error_code: str, message: str) -> Reply:
        return Reply(self.node_name, self.incarnation, 0, "", 0, error_code, message)


def _create_reply(reply: Reply) -> CreateReply:
    common = dict(task_id=reply.task_id, duplicate=reply.duplicate, assigned_node=reply.primary)
    if reply.success:
        return CreateReply(True, message=reply.message, dispatched_nodes=(reply.primary, *reply.helpers), **common)
    return CreateReply(False, reply.error_code, reply.message, **common)


def _destroy_reply(reply: Reply) -> DestroyReply:
    return DestroyReply(reply.success, reply.error_code, reply.message, reply.status)


def _query_reply(reply: Reply) -> QueryReply:
    return QueryReply(reply.success, reply.error_code, reply.message, reply.record)
