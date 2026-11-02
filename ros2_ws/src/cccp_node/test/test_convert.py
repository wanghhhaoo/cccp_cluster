"""Message conversion, exercised with plain objects standing in for ROS messages."""
from types import SimpleNamespace as NS

from cccp_node import names
from cccp_node.control import protocol as p
from cccp_node.control.gateway import CreateReply, DestroyReply, QueryReply
from cccp_node.control.protocol import ExecCommand, Request
from cccp_node.core import constants as c
from cccp_node.core.cluster_view import NodeView, TaskStateView
from cccp_node.core.generation import Generation
from cccp_node.core.link_estimator import LinkReport
from cccp_node.core.naming import ros_namespace
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import StatusEvent, Submit, TaskTable
from cccp_node.ros import convert

SPEC = TaskSpec(task_type="PKCT", mode="redundant", helper_count=1, input="airsim://uav_5/0",
                params_json='{"bbox": [1, 2, 3, 4]}')


def blank():
    return NS()


def request_for(op, **fields):
    return Request("gw", 5, 9, "n1", 2, op, **fields)


def test_node_status_round_trip_keeps_full_task_state():
    task = TaskStateView("t-4", 2, c.ROLE_PRIMARY, "edge-01", ("edge-02",), "k4", "demo", SPEC.to_json(), 3)
    view = NodeView("edge-01", incarnation=5, seq=9, leader_name="edge-02", term=3, inference_ready=True,
                    algorithms_ready=("PKCI",), cpu_cores=8, cpu_usage=12.5, running_tasks=(task,))
    links = [LinkReport("edge-02", 12.0, 1.5, 0.1, echo_seq=77, echo_delay_ms=300)]
    msg = convert.fill_node_status(blank(), view, links, blank, blank)
    assert msg.tasks[0].helper_nodes == ["edge-02"] and msg.links[0].echo_seq == 77
    back = convert.node_view_from_msg(msg)
    assert back.running_tasks == (task,) and back.term == 3 and back.cpu_usage == 12.5
    assert TaskSpec.from_json(back.running_tasks[0].spec_json) == SPEC
    assert convert.echo_for(msg, "edge-02") == (77, 300) and convert.echo_for(msg, "edge-09") is None


def test_start_and_stop_commands_become_service_requests():
    command = ExecCommand(7, 3, ("a", "b"), p.START, "t-1", Generation(2, 6), primary="a", helpers=("b",),
                          idempotency_key="k", name="n", spec=SPEC)
    start = convert.fill_start_request(blank(), command, "b")
    assert (start.term, start.task_id, start.helper_nodes, start.idempotency_key) == (7, "t-1", ["b"], "k")
    assert (start.epoch, start.assign_term, start.role, start.primary_node) == (2, 6, c.ROLE_HELPER, "a")
    assert TaskSpec.from_json(start.spec_json) == SPEC
    stop = convert.fill_stop_request(blank(), ExecCommand(7, 4, ("a",), p.STOP, "t-1", Generation(2, 6),
                                                           stop_kind=c.STOP_CANCEL, reason="user"))
    assert (stop.epoch, stop.assign_term, stop.reason) == (2, 6, "cancel: user")


def test_create_request_round_trip_for_forwarding():
    request = convert.fill_create_request(blank(), SPEC, "key-1")
    assert request.idempotency_key == "key-1" and request.target_nodes == []
    assert convert.spec_from_request(request) == SPEC
    response = convert.fill_create_response(blank(), CreateReply(True, task_id="t-5", assigned_node="a",
                                                                 dispatched_nodes=("a", "b")))
    assert response.dispatched_nodes == ["a", "b"] and response.candidate_nodes == []
    reply = convert.reply_from_response(request_for(p.CREATE), response)
    assert reply.success and (reply.task_id, reply.primary, reply.helpers) == ("t-5", "a", ("b",))
    assert (reply.origin_node, reply.origin_incarnation, reply.request_id) == ("gw", 5, 9)


def test_query_and_destroy_round_trip_for_forwarding():
    table = TaskTable()
    table.apply(Submit("t-1", "k", SPEC))
    response = convert.fill_query_response(blank(), QueryReply(True, record=table.get("t-1")))
    assert response.success and response.status == c.PENDING and response.mode == "redundant"
    back = convert.reply_from_response(request_for(p.QUERY, task_id="t-1"), response).record
    assert (back.task_id, back.status, back.spec.task_type) == ("t-1", c.PENDING, "PKCT")
    missing = convert.fill_query_response(blank(), QueryReply(False, c.NOT_FOUND, "no such task"))
    assert not missing.success and missing.error_code == c.NOT_FOUND
    assert convert.reply_from_response(request_for(p.QUERY, task_id="x"), missing).error_code == c.NOT_FOUND
    destroyed = convert.fill_destroy_response(blank(), DestroyReply(True, final_status=c.CANCELLED))
    reply = convert.reply_from_response(request_for(p.DESTROY, task_id="t-1"), destroyed)
    assert reply.success and reply.status == c.CANCELLED


def test_status_report_and_result_conversion():
    status = convert.fill_task_status(blank(), StatusEvent(4, "t-1", "n", "PKCI", c.RUNNING, 2, "a", "m"), 7, "now")
    assert (status.seq, status.term, status.epoch, status.task_id) == (4, 7, 2, "t-1")
    event = convert.event_from_report(NS(node_name="a", task_id="t-1", epoch=2, assign_term=5, state=c.REPORT_FAILED,
                                         error_code=c.NOT_READY, message="", incarnation=9, seq=3))
    assert (event.task_id, event.generation, event.seq, event.kind, event.error_code) == (
        "t-1", (2, 5), 3, p.FAILED, c.NOT_READY)
    result = NS(task_id="t-1", epoch=2, assign_term=5, node_name="a", task_type="PKCT", drone_id=0, frame_id=5, success=True,
                valid=True, message="ok", latency_ms=12.3456, source_stamp=NS(sec=1, nanosec=500000000),
                payload=b"xyz", x=1.0, y=2.0, width=3.0, height=4.0, confidence=0.5)
    data = convert.result_to_dict(result)
    assert data["bbox"] == [1.0, 2.0, 3.0, 4.0] and data["source_stamp"] == 1.5 and data["task_id"] == "t-1"


def test_names_and_namespaces():
    assert ros_namespace("edge-01") == "/edge_01" and ros_namespace("3rd.board") == "/n3rd_board"
    assert names.on_node("edge-01", names.LEADER_CREATE_SERVICE) == "/edge_01/leader/task/create"
