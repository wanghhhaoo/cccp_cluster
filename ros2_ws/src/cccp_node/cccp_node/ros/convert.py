"""ROS message <-> core type conversion.

Functions only read and set attributes of the message objects they are given,
and nested messages are created through factories passed in by the caller, so
this module needs no ROS imports and is unit-tested with plain objects.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from ..control import protocol as p
from ..control.gateway import CreateReply, DestroyReply, QueryReply
from ..control.protocol import ExecCommand, ExecEvent, Reply, Request
from ..core import constants as c
from ..core.cluster_view import LinkView, NodeView, TaskStateView
from ..core.generation import Generation
from ..core.link_estimator import LinkReport
from ..core.task_spec import TaskSpec
from ..core.task_table import StatusEvent, TaskRecord

SPEC_FIELDS = (
    "name", "task_type", "input", "input_node", "request_node", "mode", "helper_count", "min_participants",
    "use_gpu", "cpu_compute_required", "cpu_memory_required", "gpu_memory_required", "latency_sensitivity",
    "origin", "origin_source", "parent_task_id", "chain_depth", "drone_id", "level", "image_topic", "params_json",
)


# --------------------------------------------------------------- NodeStatus

def node_view_from_msg(msg: Any) -> NodeView:
    return NodeView(
        name=msg.node_name,
        protocol_version=msg.protocol_version,
        incarnation=msg.incarnation,
        seq=msg.seq,
        leader_name=msg.leader_name,
        term=msg.term,
        is_leader=msg.is_leader,
        inference_ready=msg.inference_ready,
        algorithms_ready=tuple(msg.algorithms_ready),
        cpu_model=msg.cpu_model,
        cpu_cores=msg.cpu_cores,
        mem_total_mb=msg.mem_total_mb,
        gpu_model=msg.gpu_model,
        gpu_mem_total_mb=msg.gpu_mem_total_mb,
        cpu_usage=msg.cpu_usage,
        mem_usage=msg.mem_usage,
        gpu_usage=msg.gpu_usage,
        gpu_mem_usage=msg.gpu_mem_usage,
        battery=msg.battery,
        running_tasks=tuple(task_state_from_msg(t) for t in msg.tasks),
        links=tuple(LinkView(l.peer_name, l.rtt_ms, l.jitter_ms, l.loss_rate) for l in msg.links),
    )


def task_state_from_msg(msg: Any) -> TaskStateView:
    return TaskStateView(msg.task_id, msg.epoch, msg.role, msg.primary_node, tuple(msg.helper_nodes),
                         msg.idempotency_key, msg.name, msg.spec_json, msg.assign_term)


def fill_task_state(msg: Any, state: TaskStateView) -> Any:
    return _set(msg, task_id=state.task_id, epoch=state.epoch, assign_term=state.assign_term, role=state.role,
                primary_node=state.primary,
                helper_nodes=list(state.helpers), idempotency_key=state.idempotency_key, name=state.name,
                spec_json=state.spec_json)


def echo_for(msg: Any, me: str) -> tuple[int, int] | None:
    """(echo_seq, echo_delay_ms) a peer's NodeStatus carries about node `me`."""
    for link in msg.links:
        if link.peer_name == me and link.echo_seq:
            return link.echo_seq, link.echo_delay_ms
    return None


def fill_node_status(msg: Any, view: NodeView, links: Iterable[LinkReport],
                     link_factory: Callable[[], Any], task_factory: Callable[[], Any]) -> Any:
    """Copy this node's own view and link reports into a NodeStatus message."""
    for name in ("protocol_version", "incarnation", "seq", "leader_name", "term", "is_leader",
                 "inference_ready", "cpu_model", "cpu_cores", "mem_total_mb", "gpu_model",
                 "gpu_mem_total_mb", "cpu_usage", "mem_usage", "gpu_usage", "gpu_mem_usage", "battery"):
        setattr(msg, name, getattr(view, name))
    msg.node_name = view.name
    msg.algorithms_ready = list(view.algorithms_ready)
    msg.tasks = [fill_task_state(task_factory(), t) for t in view.running_tasks]
    msg.links = [_set(link_factory(), peer_name=r.peer_name, rtt_ms=float(r.rtt_ms), jitter_ms=float(r.jitter_ms),
                      loss_rate=float(r.loss_rate), echo_seq=r.echo_seq, echo_delay_ms=r.echo_delay_ms)
                 for r in links]
    return msg


def _set(obj: Any, **values) -> Any:
    for key, value in values.items():
        setattr(obj, key, value)
    return obj


# ------------------------------------------------------------- task API

def spec_from_request(request: Any) -> TaskSpec:
    """CreateTask.Request (or any object with its fields) -> TaskSpec."""
    values = {name: getattr(request, name) for name in SPEC_FIELDS}
    return TaskSpec(target_nodes=tuple(request.target_nodes), **values)


def fill_create_request(request: Any, spec: TaskSpec, idempotency_key: str) -> Any:
    """TaskSpec -> CreateTask.Request, for forwarding to the leader."""
    _set(request, idempotency_key=idempotency_key, target_nodes=list(spec.target_nodes))
    return _set(request, **{name: getattr(spec, name) for name in SPEC_FIELDS})


def reply_from_response(request: Request, response: Any) -> Reply:
    """A leader/task/* service response -> the Reply to the request it answers (protocol 2 only)."""
    common = dict(origin_node=request.origin_node, origin_incarnation=request.origin_incarnation,
                  request_id=request.request_id, leader=request.target_leader, term=request.term,
                  error_code="" if response.success else response.error_code or c.TIMEOUT, message=response.message)
    if request.op == p.CREATE:
        nodes = tuple(response.dispatched_nodes)
        return Reply(task_id=response.task_id, duplicate=response.duplicate, primary=response.assigned_node,
                     helpers=tuple(n for n in nodes if n != response.assigned_node),
                     status=c.RUNNING if response.success else "", **common)
    if request.op == p.DESTROY:
        return Reply(task_id=request.task_id, status=response.final_status, **common)
    if not response.success:
        return Reply(**common)
    nodes = list(response.target_nodes)       # QueryTask carries only part of the record
    spec = TaskSpec(task_type=response.task_type, mode=response.mode or "local",
                    origin=response.origin or "manual", parent_task_id=response.parent_task_id)
    record = TaskRecord(task_id=request.task_id, idempotency_key="", spec=spec, name=response.name,
                        status=response.status, epoch=response.epoch, primary=response.node,
                        helpers=tuple(n for n in nodes if n != response.node),
                        error_message=response.error_message, last_event=response.last_event)
    return Reply(task_id=request.task_id, status=record.status, primary=record.primary, helpers=record.helpers,
                 record=record, **common)


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
        error_message=record.error_message, result_url="", last_event=record.last_event,
    )


def fill_task_status(msg: Any, event: StatusEvent, term: int, stamp: Any) -> Any:
    return _set(msg, task_id=event.task_id, name=event.name, task_type=event.task_type, status=event.status,
                node=event.node, epoch=event.epoch, term=term, seq=event.seq, message=event.message, stamp=stamp)


_REPORT_KINDS = {c.REPORT_RUNNING: p.RUNNING, c.REPORT_FAILED: p.FAILED, c.REPORT_STOPPED: p.STOPPED,
                 c.REPORT_FINISHED: p.FINISHED}


def event_from_report(msg: Any) -> ExecEvent | None:
    """TaskReport (protocol 2) -> ExecEvent; None for an unknown state."""
    kind = _REPORT_KINDS.get(msg.state)
    if kind is None:
        return None
    return ExecEvent(msg.node_name, msg.incarnation, msg.seq, 0, 0, msg.task_id,
                     Generation(msg.epoch, msg.assign_term), kind, msg.error_code, msg.message)


def fill_start_request(request: Any, command: ExecCommand, target: str) -> Any:
    """One target's StartTask for a START command (protocol 2)."""
    return _set(request, term=command.term, task_id=command.task_id, epoch=command.generation.epoch,
                assign_term=command.generation.assign_term,
                role=c.ROLE_PRIMARY if target == command.primary else c.ROLE_HELPER,
                primary_node=command.primary, helper_nodes=list(command.helpers),
                idempotency_key=command.idempotency_key, name=command.name, spec_json=command.spec.to_json())


def fill_stop_request(request: Any, command: ExecCommand) -> Any:
    """StopTask for a STOP command (protocol 2): the kind travels in the reason text."""
    return _set(request, term=command.term, task_id=command.task_id, epoch=command.generation.epoch,
                assign_term=command.generation.assign_term, reason="%s: %s" % (command.stop_kind, command.reason))


def result_to_dict(msg: Any) -> dict:
    """TaskResult -> JSON-friendly dict for UE (payload summarised, not copied). From CCCP ue_api."""
    data = {
        "task_id": msg.task_id, "epoch": msg.epoch, "assign_term": msg.assign_term, "node": msg.node_name, "task_type": msg.task_type,
        "drone_id": msg.drone_id, "frame_id": msg.frame_id, "success": msg.success, "valid": msg.valid,
        "message": msg.message, "latency_ms": round(float(msg.latency_ms), 3),
        "source_stamp": msg.source_stamp.sec + msg.source_stamp.nanosec * 1e-9,
        "payload_bytes": len(msg.payload),
    }
    if msg.task_type == "PKCT":
        data["bbox"] = [float(msg.x), float(msg.y), float(msg.width), float(msg.height)]
        data["confidence"] = float(msg.confidence)
    else:
        data.update(label=msg.label, class_name=msg.class_name, score=float(msg.score))
    return data
