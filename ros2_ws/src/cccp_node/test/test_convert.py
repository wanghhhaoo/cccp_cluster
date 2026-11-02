"""Message conversion, exercised with plain objects standing in for ROS messages."""
from types import SimpleNamespace as NS

from cccp_node import names
from cccp_node.control.controller import StartRequest
from cccp_node.control.gateway import CreateReply, DestroyReply, QueryReply
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


def test_start_request_carries_the_whole_spec():
    start = convert.fill_start_request(blank(), StartRequest(7, "t-1", Generation(2, 6), c.ROLE_HELPER, "a", ("b",),
                                                             "k", "n", SPEC))
    assert (start.term, start.task_id, start.helper_nodes, start.idempotency_key) == (7, "t-1", ["b"], "k")
    assert (start.epoch, start.assign_term) == (2, 6)
    assert TaskSpec.from_json(start.spec_json) == SPEC


def test_create_request_round_trip_for_forwarding():
    request = convert.fill_create_request(blank(), SPEC, "key-1")
    assert request.idempotency_key == "key-1" and request.target_nodes == []
    assert convert.spec_from_request(request) == SPEC
    response = convert.fill_create_response(blank(), CreateReply(True, task_id="t-5", dispatched_nodes=("a", "b")))
    assert response.dispatched_nodes == ["a", "b"] and response.candidate_nodes == []
    assert convert.create_reply_from_response(response) == CreateReply(True, task_id="t-5",
                                                                       dispatched_nodes=("a", "b"))


def test_query_and_destroy_round_trip_for_forwarding():
    table = TaskTable()
    table.apply(Submit("t-1", "k", SPEC))
    response = convert.fill_query_response(blank(), QueryReply(True, record=table.get("t-1")))
    assert response.success and response.status == c.PENDING and response.mode == "redundant"
    back = convert.query_reply_from_response(response, "t-1")
    assert (back.record.task_id, back.record.status, back.record.spec.task_type) == ("t-1", c.PENDING, "PKCT")
    missing = convert.fill_query_response(blank(), QueryReply(False, c.NOT_FOUND, "no such task"))
    assert not missing.success and missing.error_code == c.NOT_FOUND
    assert convert.query_reply_from_response(missing, "x").error_code == c.NOT_FOUND
    destroyed = convert.fill_destroy_response(blank(), DestroyReply(True, final_status=c.CANCELLED))
    assert convert.destroy_reply_from_response(destroyed) == DestroyReply(True, final_status=c.CANCELLED)


def test_status_report_and_result_conversion():
    status = convert.fill_task_status(blank(), StatusEvent(4, "t-1", "n", "PKCI", c.RUNNING, 2, "a", "m"), 7, "now")
    assert (status.seq, status.term, status.epoch, status.task_id) == (4, 7, 2, "t-1")
    report = convert.report_from_msg(NS(node_name="a", task_id="t-1", epoch=2, assign_term=5, state=c.REPORT_RUNNING,
                                        error_code="", message="", incarnation=9, seq=3))
    assert (report.task_id, report.generation, report.seq) == ("t-1", (2, 5), 3)
    result = NS(task_id="t-1", epoch=2, assign_term=5, node_name="a", task_type="PKCT", drone_id=0, frame_id=5, success=True,
                valid=True, message="ok", latency_ms=12.3456, source_stamp=NS(sec=1, nanosec=500000000),
                payload=b"xyz", x=1.0, y=2.0, width=3.0, height=4.0, confidence=0.5)
    data = convert.result_to_dict(result)
    assert data["bbox"] == [1.0, 2.0, 3.0, 4.0] and data["source_stamp"] == 1.5 and data["task_id"] == "t-1"


def test_names_and_namespaces():
    assert ros_namespace("edge-01") == "/edge_01" and ros_namespace("3rd.board") == "/n3rd_board"
    assert names.on_node("edge-01", names.LEADER_CREATE_SERVICE) == "/edge_01/leader/task/create"
