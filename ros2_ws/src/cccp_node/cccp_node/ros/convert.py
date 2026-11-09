"""ROS message <-> core type conversion (protocol 3, DESIGN appendix C).

Functions only read and set attributes of the message objects they are given,
and nested messages are created through factories passed in by the caller, so
this module needs no ROS imports and is unit-tested with plain objects.

The wire is compact where the core is readable: nodes are 1-byte ids instead of
names (a Directory maps them; TaskList carries each node's name), task ids are
16-byte UUIDs, enums are uint8, link measurements are quantized to one byte,
and bounded strings are clipped to their bound.
"""
from __future__ import annotations

import uuid
from typing import Any, Callable, Iterable, Protocol

from ..control import protocol as p
from ..control.gateway import CreateReply, DestroyReply, QueryReply
from ..control.protocol import ExecCommand, ExecEvent, Reply, Request
from ..core import constants as c
from ..core import task_types
from ..core.cluster_view import LinkView, NodeView, TaskListView, TaskStateView
from ..core.generation import Generation
from ..core.link_estimator import (
    LinkReport, byte_to_echo_delay_ms, byte_to_ms, byte_to_rate, quantize,
)
from ..core.task_spec import TaskSpec
from ..core.task_table import StatusEvent, TaskRecord

SPEC_FIELDS = (
    "name", "task_type", "input", "input_node", "request_node", "mode", "helper_count", "min_participants",
    "use_gpu", "cpu_compute_required", "cpu_memory_required", "gpu_memory_required", "latency_sensitivity",
    "origin", "origin_source", "parent_task_id", "chain_depth", "drone_id", "level", "image_topic", "params_json",
    "priority", "preemption_policy",
)

# Wire limits (bounded arrays and strings in the .msg files).
MAX_TASKS = 8
MAX_TOMBSTONES = 16
MAX_PEERS = 15
NAME_LENGTH, NODE_NAME_LENGTH, REASON_LENGTH, MESSAGE_LENGTH = 64, 32, 64, 128

# uint8 enums. Index = value on the wire.
ALGORITHMS = task_types.ALGORITHMS                                   # bit i of algorithms_ready
ROLES = (c.ROLE_PRIMARY, c.ROLE_HELPER)                          # TaskState.ROLE_*, TaskBrief.ROLE_*
OPS = {p.START: 1, p.STOP: 2}                                    # Command.OP_*
STOP_KINDS = {c.STOP_CANCEL: 1, c.STOP_STALE: 2, c.STOP_DUPLICATE: 3, c.STOP_UNASSIGNED: 4}
EVENT_KINDS = {p.RUNNING: 1, p.REJECTED: 2, p.FAILED: 3, p.STOPPED: 4, p.FINISHED: 5}   # ExecEvent.KIND_*
EXEC_ERRORS = {"": 0, c.STALE_TERM: 1, c.STALE_EPOCH: 2, c.BUSY: 3, c.NOT_READY: 4, c.INVALID_INPUT: 5,
               c.UNSUPPORTED_VERSION: 6, c.INFERENCE_ERROR: 7, c.INSUFFICIENT_PEERS: 8, c.FUSION_ERROR: 9,
               c.CANCELLED_ERROR: 10}                            # ExecEvent.ERR_*
REQUEST_OPS = {p.CREATE: 1, p.DESTROY: 2, p.QUERY: 3}            # Request.OP_*
ORIGINS = {c.ORIGIN_MANUAL: 1, c.ORIGIN_CHAIN: 2, c.ORIGIN_SCHEDULE: 3, c.ORIGIN_EVENT: 4}
RESULTS = {"": 0, c.NO_LEADER: 1, c.NOT_LEADER: 2, c.TIMEOUT: 3, c.INVALID_INPUT: 4, c.CHAIN_TOO_DEEP: 5,
           c.NO_FEASIBLE_NODE: 6, c.DISPATCH_FAILED: 7, c.NOT_FOUND: 9, c.INVALID_STATE: 10}  # Reply.RES_*
RES_TASK_FAILED = 8          # the error is the task's own: Reply.exec_error says which (EXEC_ERRORS)
STATUSES = (c.PENDING, c.SCHEDULED, c.RUNNING, c.FINISHED, c.FAILED, c.CANCELLED)   # Reply.STATUS_*


class Directory(Protocol):
    """Node id <-> name (ClusterView)."""

    def name_of(self, node_id: int) -> str | None: ...

    def id_of(self, name: str) -> int: ...


def _decode(table: dict, value: int, default: str = "") -> str:
    for key, code in table.items():
        if code == value:
            return key
    return default


def _set(obj: Any, **values) -> Any:
    for key, value in values.items():
        setattr(obj, key, value)
    return obj


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def uuid_bytes(task_id: str) -> list[int]:
    """Raises ValueError for anything but a UUID (the only task ids leaders make)."""
    return list(uuid.UUID(task_id).bytes)


def uuid_str(raw: Any) -> str:
    return str(uuid.UUID(bytes=bytes(bytearray(raw))))


def node_name(directory: Directory, node_id: int) -> str:
    """A node whose TaskList has not arrived yet gets a placeholder that matches no live node."""
    return directory.name_of(node_id) or "#%d" % node_id


def node_ids(directory: Directory, names: Iterable[str]) -> list[int]:
    return [directory.id_of(name) for name in names]


def algorithms_mask(algorithms: Iterable[str]) -> int:
    return sum(1 << i for i, name in enumerate(ALGORITHMS) if name in algorithms)


def algorithms_from_mask(mask: int) -> tuple[str, ...]:
    return tuple(name for i, name in enumerate(ALGORITHMS) if mask & (1 << i))


def _percent(value: float) -> int:
    return -1 if value < 0 else max(0, min(100, int(round(value))))


# --------------------------------------------------------------- heartbeat

def fill_node_status(msg: Any, view: NodeView, directory: Directory, links: Iterable[LinkReport] | None,
                     brief_factory: Callable[[], Any], load_factory: Callable[[], Any],
                     link_factory: Callable[[], Any]) -> Any:
    """This node's heartbeat. links=None leaves out the slow part (resources and links)."""
    _set(msg, protocol_version=view.protocol_version, node_id=view.node_id, incarnation=view.incarnation,
         seq=view.seq, leader_id=directory.id_of(view.leader_name) if view.leader_name else 0, term=view.term,
         is_leader=view.is_leader, visible_peers=min(255, view.visible_peers), executor_alive=view.executor_alive,
         inference_ready=view.inference_ready, algorithms_ready=algorithms_mask(view.algorithms_ready),
         tasks_version=view.tasks_version)
    msg.tasks = [_set(brief_factory(), task_id=uuid_bytes(t.task_id), epoch=t.epoch, assign_term=t.assign_term,
                      role=ROLES.index(t.role)) for t in view.running_tasks[:MAX_TASKS]]
    if links is None:
        msg.load, msg.links = [], []
        return msg
    msg.load = [_set(load_factory(), cpu_cores=view.cpu_cores, mem_total_mb=view.mem_total_mb,
                     gpu_mem_total_mb=view.gpu_mem_total_mb, cpu_usage=_percent(view.cpu_usage),
                     mem_usage=_percent(view.mem_usage), gpu_usage=_percent(view.gpu_usage),
                     gpu_mem_usage=_percent(view.gpu_mem_usage),
                     battery=_percent(view.battery * 100 if view.battery >= 0 else -1))]
    entries = []
    for report in links:
        peer = directory.id_of(report.peer_name)
        if peer:
            entries.append(_set(link_factory(), peer=peer, **quantize(report)))
    msg.links = entries[:MAX_PEERS]
    return msg


def node_view_from_msg(msg: Any, name: str, directory: Directory) -> NodeView:
    """A peer's heartbeat; name: the sender's (from its TaskList). slow=False when it carries no slow part."""
    slow = {}
    if msg.load:
        load = msg.load[0]
        slow = dict(cpu_cores=load.cpu_cores, mem_total_mb=load.mem_total_mb, gpu_mem_total_mb=load.gpu_mem_total_mb,
                    cpu_usage=float(load.cpu_usage), mem_usage=float(load.mem_usage),
                    gpu_usage=float(load.gpu_usage), gpu_mem_usage=float(load.gpu_mem_usage),
                    battery=load.battery / 100.0 if load.battery >= 0 else -1.0,
                    links=tuple(LinkView(node_name(directory, link.peer), byte_to_ms(link.rtt_4ms),
                                         byte_to_ms(link.jitter_4ms), byte_to_rate(link.loss_pct))
                                for link in msg.links))
    return NodeView(
        name=name, protocol_version=msg.protocol_version, incarnation=msg.incarnation, seq=msg.seq,
        leader_name=node_name(directory, msg.leader_id) if msg.leader_id else "", term=msg.term,
        is_leader=msg.is_leader, inference_ready=msg.inference_ready,
        algorithms_ready=algorithms_from_mask(msg.algorithms_ready), node_id=msg.node_id,
        visible_peers=msg.visible_peers, executor_alive=msg.executor_alive, tasks_version=msg.tasks_version,
        slow=bool(msg.load),
        running_tasks=tuple(TaskStateView(uuid_str(t.task_id), t.epoch, ROLES[t.role], assign_term=t.assign_term)
                            for t in msg.tasks),
        **slow)


def echo_for(msg: Any, my_id: int) -> tuple[int, float] | None:
    """(echo_seq low 16 bits, echo delay in ms or -1) a peer's heartbeat carries about this node."""
    for link in msg.links:
        if link.peer == my_id and link.echo_seq:
            return link.echo_seq, byte_to_echo_delay_ms(link.echo_delay_4ms)
    return None


# ------------------------------------------------------- TaskState, TaskList

def fill_task_state(msg: Any, state: TaskStateView, directory: Directory) -> Any:
    return _set(msg, task_id=uuid_bytes(state.task_id), epoch=state.epoch, assign_term=state.assign_term,
                role=ROLES.index(state.role), primary_node=directory.id_of(state.primary),
                helper_nodes=node_ids(directory, state.helpers)[:MAX_PEERS], idempotency_key=state.idempotency_key,
                name=clip(state.name, NAME_LENGTH), spec_json=state.spec_json)


def task_state_from_msg(msg: Any, directory: Directory) -> TaskStateView:
    return TaskStateView(uuid_str(msg.task_id), msg.epoch, ROLES[msg.role], node_name(directory, msg.primary_node),
                         tuple(node_name(directory, h) for h in msg.helper_nodes), msg.idempotency_key, msg.name,
                         msg.spec_json, msg.assign_term)


def fill_task_list(msg: Any, task_list: TaskListView, directory: Directory, state_factory: Callable[[], Any],
                   tombstone_factory: Callable[[], Any]) -> Any:
    return _set(msg, node_id=task_list.node_id, node_name=clip(task_list.name, NODE_NAME_LENGTH),
                incarnation=task_list.incarnation, tasks_version=task_list.tasks_version,
                tasks=[fill_task_state(state_factory(), t, directory) for t in task_list.tasks[:MAX_TASKS]],
                tombstones=[_set(tombstone_factory(), task_id=uuid_bytes(t))
                            for t in task_list.tombstones[-MAX_TOMBSTONES:]])


def task_list_from_msg(msg: Any, directory: Directory) -> TaskListView:
    return TaskListView(msg.node_name, msg.incarnation, msg.tasks_version,
                        tuple(task_state_from_msg(t, directory) for t in msg.tasks),
                        tuple(uuid_str(t.task_id) for t in msg.tombstones), msg.node_id)


# ---------------------------------------------------------- Command, ExecEvent

def fill_command(msg: Any, command: ExecCommand, directory: Directory) -> Any:
    _set(msg, leader_id=directory.id_of(command.leader), term=command.term, request_id=command.request_id,
         targets=node_ids(directory, command.targets)[:MAX_PEERS], op=OPS[command.op],
         stop_reason=STOP_KINDS.get(command.stop_kind, 0), reason=clip(command.reason, REASON_LENGTH),
         task_id=uuid_bytes(command.task_id), epoch=command.generation.epoch,
         assign_term=command.generation.assign_term)
    if command.op == p.START:
        _set(msg, primary_node=directory.id_of(command.primary),
             primary_name=clip(command.primary, NODE_NAME_LENGTH),
             helper_nodes=node_ids(directory, command.helpers)[:MAX_PEERS], idempotency_key=command.idempotency_key,
             name=clip(command.name, NAME_LENGTH), spec_json=command.spec.to_json())
    return msg


def stop_kind(msg: Any) -> str:
    return _decode(STOP_KINDS, msg.stop_reason)


def fill_exec_event(msg: Any, node_id: int, incarnation: int, seq: int, term: int, request_id: int, task_id: str,
                    generation: Generation, kind: str, error_code: str = "", message: str = "") -> Any:
    return _set(msg, node_id=node_id, incarnation=incarnation, seq=seq, term=term, request_id=request_id,
                task_id=uuid_bytes(task_id), epoch=generation.epoch, assign_term=generation.assign_term,
                kind=EVENT_KINDS[kind], error_code=EXEC_ERRORS.get(error_code, 0),
                message=clip(message, MESSAGE_LENGTH))


def exec_event_from_msg(msg: Any, directory: Directory) -> ExecEvent | None:
    kind = _decode(EVENT_KINDS, msg.kind)
    if not kind:
        return None
    return ExecEvent(node_name(directory, msg.node_id), msg.incarnation, msg.seq, msg.term, msg.request_id,
                     uuid_str(msg.task_id), Generation(msg.epoch, msg.assign_term), kind,
                     _decode(EXEC_ERRORS, msg.error_code), msg.message)


# ------------------------------------------------------------ Request, Reply

def fill_request(msg: Any, request: Request, directory: Directory) -> Any:
    """Raises ValueError for a destroy or query of something that is not a task id."""
    _set(msg, origin_node=directory.id_of(request.origin_node), origin_incarnation=request.origin_incarnation,
         request_id=request.request_id, target_leader=directory.id_of(request.target_leader), term=request.term,
         op=REQUEST_OPS[request.op], idempotency_key=request.idempotency_key,
         reason=clip(request.reason, REASON_LENGTH))
    if request.spec is not None:
        _set(msg, origin=ORIGINS.get(request.spec.origin, 0), chain_depth=request.spec.chain_depth,
             name=clip(request.spec.name, NAME_LENGTH), spec_json=request.spec.to_json())
    if request.task_id:
        msg.task_id = uuid_bytes(request.task_id)
    return msg


def request_from_msg(msg: Any, directory: Directory) -> Request:
    op = _decode(REQUEST_OPS, msg.op)
    return Request(node_name(directory, msg.origin_node), msg.origin_incarnation, msg.request_id,
                   node_name(directory, msg.target_leader), msg.term, op, idempotency_key=msg.idempotency_key,
                   spec=TaskSpec.from_json(msg.spec_json) if op == p.CREATE else None,
                   task_id=uuid_str(msg.task_id) if op != p.CREATE else "", reason=msg.reason)


def fill_reply(msg: Any, reply: Reply, directory: Directory) -> Any:
    if reply.error_code in RESULTS:
        result, exec_error = RESULTS[reply.error_code], 0
    else:
        result, exec_error = RES_TASK_FAILED, EXEC_ERRORS.get(reply.error_code, 0)
    _set(msg, origin_node=directory.id_of(reply.origin_node), origin_incarnation=reply.origin_incarnation,
         request_id=reply.request_id, leader_id=directory.id_of(reply.leader), term=reply.term, result=result,
         exec_error=exec_error, message=clip(reply.message, MESSAGE_LENGTH), duplicate=reply.duplicate,
         status=STATUSES.index(reply.status) if reply.status else 0, epoch=reply.generation.epoch,
         assign_term=reply.generation.assign_term, primary_node=directory.id_of(reply.primary) if reply.primary else 0,
         helper_nodes=node_ids(directory, reply.helpers)[:MAX_PEERS])
    if reply.task_id:
        msg.task_id = uuid_bytes(reply.task_id)
    record = reply.record
    if record is not None:
        _set(msg, name=clip(record.name, NAME_LENGTH), spec_json=record.spec.to_json(),
             error_message=clip(record.error_message, MESSAGE_LENGTH),
             last_event=clip(record.last_event, MESSAGE_LENGTH))
    return msg


def reply_from_msg(msg: Any, directory: Directory) -> Reply:
    if msg.result == RES_TASK_FAILED:
        error_code = _decode(EXEC_ERRORS, msg.exec_error) or c.DISPATCH_FAILED
    else:
        error_code = _decode(RESULTS, msg.result, c.TIMEOUT)
    task_id = uuid_str(msg.task_id) if any(msg.task_id) else ""
    status = STATUSES[msg.status] if task_id else ""
    primary = node_name(directory, msg.primary_node) if msg.primary_node else ""
    helpers = tuple(node_name(directory, h) for h in msg.helper_nodes)
    generation = Generation(msg.epoch, msg.assign_term)
    record = None
    if msg.spec_json:            # a query's answer: the whole record
        record = TaskRecord(task_id=task_id, idempotency_key="", spec=TaskSpec.from_json(msg.spec_json),
                            name=msg.name, status=status, epoch=generation.epoch,
                            assign_term=generation.assign_term, primary=primary, helpers=helpers,
                            error_message=msg.error_message, last_event=msg.last_event)
    return Reply(node_name(directory, msg.origin_node), msg.origin_incarnation, msg.request_id,
                 node_name(directory, msg.leader_id), msg.term, error_code, msg.message, task_id, msg.duplicate,
                 status, generation, primary, helpers, record)


# ------------------------------------------------------------ access layer

def spec_from_request(request: Any) -> TaskSpec:
    """CreateTask.Request (or any object with its fields) -> TaskSpec."""
    values = {name: getattr(request, name) for name in SPEC_FIELDS}
    return TaskSpec(target_nodes=tuple(request.target_nodes), **values)


def fill_create_response(response: Any, reply: CreateReply) -> Any:
    return _set(response, success=reply.success, error_code=reply.error_code, message=reply.message,
                task_id=reply.task_id, duplicate=reply.duplicate, assigned_node=reply.assigned_node,
                dispatched_nodes=list(reply.dispatched_nodes), candidate_nodes=list(reply.candidate_nodes))


def fill_destroy_response(response: Any, reply: DestroyReply) -> Any:
    return _set(response, success=reply.success, error_code=reply.error_code, message=reply.message,
                final_status=reply.final_status)


def fill_query_response(response: Any, reply: QueryReply) -> Any:
    record = reply.record
    if record is None:
        return _set(response, success=False, error_code=reply.error_code, message=reply.message)
    return _set(
        response, success=True, error_code="", message="ok", name=record.name, task_type=record.spec.task_type,
        mode=record.spec.mode, status=record.status, node=record.primary, target_nodes=list(record.nodes),
        epoch=record.epoch, origin=record.spec.origin, parent_task_id=record.spec.parent_task_id,
        error_message=record.error_message, result_url=record.result_url, last_event=record.last_event,
    )


def fill_task_status(msg: Any, event: StatusEvent, term: int, stamp: Any) -> Any:
    return _set(msg, task_id=event.task_id, name=event.name, task_type=event.task_type, status=event.status,
                node=event.node, epoch=event.epoch, term=term, seq=event.seq, message=event.message, stamp=stamp)


def result_to_dict(msg: Any) -> dict:
    """TaskResult -> JSON-friendly dict for UE (payload summarised, not copied). From CCCP ue_api."""
    data = {
        "task_id": msg.task_id, "epoch": msg.epoch, "assign_term": msg.assign_term, "node": msg.node_name,
        "task_type": msg.task_type, "drone_id": msg.drone_id, "frame_id": msg.frame_id, "success": msg.success,
        "valid": msg.valid, "message": msg.message, "latency_ms": round(float(msg.latency_ms), 3),
        "source_stamp": msg.source_stamp.sec + msg.source_stamp.nanosec * 1e-9,
        "payload_bytes": len(msg.payload),
    }
    if msg.task_type == "PKCT":
        data["bbox"] = [float(msg.x), float(msg.y), float(msg.width), float(msg.height)]
        data["confidence"] = float(msg.confidence)
    else:
        data.update(label=msg.label, class_name=msg.class_name, score=float(msg.score))
    return data
