"""ROS message <-> core type conversion.

Functions only read and set attributes of the message objects they are given,
and nested messages are created through factories passed in by the caller, so
this module needs no ROS imports and is unit-tested with plain objects.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from ..control.controller import StartRequest, TaskReportView
from ..control.gateway import CreateReply, DestroyReply, QueryReply
from ..core.cluster_view import LinkView, NodeView, TaskStateView
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
                         msg.idempotency_key, msg.name, msg.spec_json)


def fill_task_state(msg: Any, state: TaskStateView) -> Any:
    return _set(msg, task_id=state.task_id, epoch=state.epoch, role=state.role, primary_node=state.primary,
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


def create_reply_from_response(response: Any) -> CreateReply:
    return CreateReply(response.success, response.error_code, response.message, response.task_id,
                       response.duplicate, response.assigned_node, tuple(response.dispatched_nodes),
                       tuple(response.candidate_nodes))


def destroy_reply_from_response(response: Any) -> DestroyReply:
    return DestroyReply(response.success, response.error_code, response.message, response.final_status)


def query_reply_from_response(response: Any, task_id: str) -> QueryReply:
    """QueryTask.Response -> QueryReply with the fields QueryTask carries (spec details are not included)."""
    if not response.success:
        return QueryReply(False, response.error_code, response.message)
    nodes = list(response.target_nodes)
    spec = TaskSpec(task_type=response.task_type, mode=response.mode or "local",
                    origin=response.origin or "manual", parent_task_id=response.parent_task_id)
    record = TaskRecord(task_id=task_id, idempotency_key="", spec=spec, name=response.name, status=response.status,
                        epoch=response.epoch, primary=response.node,
                        helpers=tuple(n for n in nodes if n != response.node),
                        error_message=response.error_message, last_event=response.last_event)
    return QueryReply(True, message=response.message, record=record)


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


def report_from_msg(msg: Any) -> TaskReportView:
    return TaskReportView(msg.node_name, msg.task_id, msg.epoch, msg.state, msg.error_code, msg.message,
                          msg.incarnation, msg.seq)


def fill_start_request(request: Any, start: StartRequest) -> Any:
    return _set(request, term=start.term, task_id=start.task_id, epoch=start.epoch, role=start.role,
                primary_node=start.primary_node, helper_nodes=list(start.helper_nodes),
                idempotency_key=start.idempotency_key, name=start.name, spec_json=start.spec.to_json())


def result_to_dict(msg: Any) -> dict:
    """TaskResult -> JSON-friendly dict for UE (payload summarised, not copied). From CCCP ue_api."""
    data = {
        "task_id": msg.task_id, "epoch": msg.epoch, "node": msg.node_name, "task_type": msg.task_type,
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
