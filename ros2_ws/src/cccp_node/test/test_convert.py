"""Message conversion, exercised with plain objects standing in for ROS messages."""
from types import SimpleNamespace as NS

from cccp_node import names
from cccp_node.control.controller import StartRequest
from cccp_node.control.gateway import CreateReply
from cccp_node.core import constants as c
from cccp_node.core.cluster_view import NodeView, RunningTaskView
from cccp_node.core.link_estimator import LinkReport
from cccp_node.core.naming import ros_namespace
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import StatusEvent, Submit, TaskTable
from cccp_node.ros import convert


def blank():
    return NS()


def test_node_status_round_trip():
    view = NodeView("edge-01", incarnation=5, seq=9, leader_name="edge-02", term=3, inference_ready=True,
                    algorithms_ready=("PKCI",), cpu_cores=8, cpu_usage=12.5, battery=-1.0,
                    running_tasks=(RunningTaskView(4, 2, c.ROLE_PRIMARY),))
    links = [LinkReport("edge-02", 12.0, 1.5, 0.1, echo_seq=77, echo_delay_ms=300)]
    msg = convert.fill_node_status(blank(), view, links, blank, blank)
    assert msg.links[0].echo_seq == 77 and msg.running_tasks[0].role == c.ROLE_PRIMARY
    back = convert.node_view_from_msg(msg)
    assert back.running_tasks == view.running_tasks and back.term == 3 and back.cpu_usage == 12.5
    assert back.link_to("edge-02").rtt_ms == 12.0
    assert convert.echo_for(msg, "edge-02") == (77, 300) and convert.echo_for(msg, "edge-09") is None


def test_create_request_to_spec_and_response():
    request = NS(name="t", task_type="PKCT", input="airsim://uav_5/0", input_node="", request_node="",
                 target_nodes=["a", "b"], mode="fusion", helper_count=1, min_participants=0, use_gpu=False,
                 cpu_compute_required=0.0, cpu_memory_required=0.0, gpu_memory_required=0.0,
                 latency_sensitivity=0.0, origin="", origin_source="", parent_task_id=0, chain_depth=0,
                 drone_id=3, level="", image_topic="", params_json="{}", idempotency_key="k")
    spec = convert.spec_from_request(request)
    assert spec.target_nodes == ("a", "b") and spec.drone_id == 3
    response = convert.fill_create_response(blank(), CreateReply(True, task_id=5, dispatched_nodes=("a", "b")))
    assert response.task_id == 5 and response.dispatched_nodes == ["a", "b"] and response.candidate_nodes == []


def test_query_and_status_and_start_fill():
    table = TaskTable()
    table.apply(Submit("k", TaskSpec(task_type="PKCI", mode="redundant", helper_count=1)))
    record = table.get(1)
    response = convert.fill_query_response(blank(), record, 1, c.NOT_FOUND)
    assert response.success and response.status == c.PENDING and response.mode == "redundant"
    missing = convert.fill_query_response(blank(), None, 9, c.NOT_FOUND)
    assert not missing.success and missing.error_code == c.NOT_FOUND
    status = convert.fill_task_status(blank(), StatusEvent(4, 1, "n", "PKCI", c.RUNNING, 2, "a", "m"), 7, "now")
    assert (status.seq, status.term, status.epoch, status.stamp) == (4, 7, 2, "now")
    start = convert.fill_start_request(blank(), StartRequest(7, 1, 2, c.ROLE_PRIMARY, "a", ("b",), record.spec))
    assert start.helper_nodes == ["b"] and start.term == 7 and start.mode == "redundant"


def test_report_and_result_conversion():
    report = convert.report_from_msg(NS(node_name="a", task_id=1, epoch=2, state=c.REPORT_RUNNING,
                                        error_code="", message="", incarnation=9, seq=3))
    assert (report.task_id, report.epoch, report.seq) == (1, 2, 3)
    result = NS(task_id=1, epoch=2, node_name="a", task_type="PKCT", drone_id=0, frame_id=5, success=True,
                valid=True, message="ok", latency_ms=12.3456, source_stamp=NS(sec=1, nanosec=500000000),
                payload=b"xyz", x=1.0, y=2.0, width=3.0, height=4.0, confidence=0.5)
    data = convert.result_to_dict(result)
    assert data["bbox"] == [1.0, 2.0, 3.0, 4.0] and data["source_stamp"] == 1.5 and data["payload_bytes"] == 3


def test_names_and_namespaces():
    assert ros_namespace("edge-01") == "/edge_01" and ros_namespace("3rd.board") == "/n3rd_board"
    assert ros_namespace("") == "/n"
    assert names.on_node("edge-01", names.EXEC_START_SERVICE) == "/edge_01/exec/task/start"
