"""Message conversion, exercised with plain objects standing in for ROS messages."""
import uuid
from types import SimpleNamespace as NS

import pytest

from cccp_node import names
from cccp_node.control import protocol as p
from cccp_node.control.gateway import CreateReply, DestroyReply, QueryReply
from cccp_node.control.protocol import ExecCommand, Reply, Request
from cccp_node.core import constants as c
from cccp_node.core.cluster_view import ClusterView, NodeView, TaskListView, TaskStateView
from cccp_node.core.generation import Generation
from cccp_node.core.link_estimator import LinkReport
from cccp_node.core.naming import default_node_id, ros_namespace
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import StatusEvent, Submit, TaskTable
from cccp_node.ros import convert

SPEC = TaskSpec(task_type="PKCT", mode="redundant", helper_count=1, input="airsim://uav_5/0",
                params_json='{"bbox": [1, 2, 3, 4]}', priority=100)
T1, T2 = str(uuid.uuid4()), str(uuid.uuid4())


def blank():
    return NS()


def directory():
    view = ClusterView(5.0)
    for i in (1, 2, 3):
        view.register("edge-0%d" % i, i)
    return view


def test_heartbeat_round_trip_with_and_without_the_slow_part():
    d = directory()
    task = TaskStateView(T1, 2, c.ROLE_HELPER, assign_term=3)
    view = NodeView("edge-01", incarnation=5, seq=9, leader_name="edge-02", term=3, inference_ready=True,
                    algorithms_ready=("PKCT",), node_id=1, visible_peers=2, executor_alive=False, tasks_version=4,
                    cpu_cores=8, cpu_usage=12.4, mem_usage=-1.0, battery=0.5, running_tasks=(task,))
    links = [LinkReport("edge-02", 12.0, 1.5, 0.1, echo_seq=0x10077, echo_delay_ms=300),
             LinkReport("stranger", 5.0, 0.0, 0.0, echo_seq=1, echo_delay_ms=0)]   # no id known: left out
    msg = convert.fill_node_status(blank(), view, d, links, blank, blank, blank)
    assert (msg.leader_id, msg.algorithms_ready, msg.tasks[0].role, msg.load[0].battery) == (2, 2, 1, 50)
    assert [(l.peer, l.echo_seq, l.rtt_4ms) for l in msg.links] == [(2, 0x77, 3)]
    back = convert.node_view_from_msg(msg, "edge-01", d)
    assert back.running_tasks == (task,) and back.leader_name == "edge-02" and back.algorithms_ready == ("PKCT",)
    assert (back.cpu_usage, back.mem_usage, back.battery, back.links[0].rtt_ms) == (12.0, -1.0, 0.5, 12.0)
    assert (back.slow, back.executor_alive, back.tasks_version, back.visible_peers) == (True, False, 4, 2)
    assert convert.echo_for(msg, 2) == (0x77, 300.0) and convert.echo_for(msg, 3) is None
    lean = convert.fill_node_status(blank(), view, d, None, blank, blank, blank)
    assert lean.load == [] and lean.links == [] and not convert.node_view_from_msg(lean, "edge-01", d).slow


def test_task_list_round_trip_names_nodes_by_id():
    d = directory()
    state = TaskStateView(T1, 2, c.ROLE_PRIMARY, "edge-01", ("edge-02", "edge-03"), "k", "n", SPEC.to_json(), 3)
    task_list = TaskListView("edge-01", 7, 4, (state,), (T2,), node_id=1)
    msg = convert.fill_task_list(blank(), task_list, d, blank, blank)
    assert (msg.tasks[0].primary_node, msg.tasks[0].helper_nodes, len(msg.tasks[0].task_id)) == (1, [2, 3], 16)
    assert convert.task_list_from_msg(msg, d) == task_list


def test_unknown_node_ids_get_a_placeholder_that_matches_no_node():
    assert convert.node_name(directory(), 9) == "#9"


def test_start_and_stop_commands():
    d = directory()
    start = convert.fill_command(blank(), ExecCommand(7, 3, ("edge-01", "edge-02"), p.START, T1, Generation(2, 6),
                                                      primary="edge-01", helpers=("edge-02",), idempotency_key="k",
                                                      name="n", spec=SPEC, leader="edge-03"), d)
    assert (start.leader_id, start.targets, start.op, start.primary_node, start.primary_name) == (
        3, [1, 2], 1, 1, "edge-01")
    assert (start.epoch, start.assign_term, start.helper_nodes) == (2, 6, [2])
    assert TaskSpec.from_json(start.spec_json) == SPEC
    stop = convert.fill_command(blank(), ExecCommand(7, 4, ("edge-02",), p.STOP, T1, Generation(2, 6),
                                                     stop_kind=c.STOP_CANCEL, reason="x" * 100, leader="edge-03"), d)
    assert (stop.op, convert.stop_kind(stop), len(stop.reason)) == (2, c.STOP_CANCEL, convert.REASON_LENGTH)


def test_exec_event_round_trip():
    msg = convert.fill_exec_event(blank(), 2, 9, 5, 7, 3, T1, Generation(2, 6), p.REJECTED, c.BUSY, "full")
    event = convert.exec_event_from_msg(msg, directory())
    assert (event.node, event.seq, event.term, event.request_id, event.task_id) == ("edge-02", 5, 7, 3, T1)
    assert (event.generation, event.kind, event.error_code) == ((2, 6), p.REJECTED, c.BUSY)
    msg.kind = 99
    assert convert.exec_event_from_msg(msg, directory()) is None


def test_request_round_trip():
    d = directory()
    create = Request("edge-02", 5, 9, "edge-01", 3, p.CREATE, idempotency_key="k", spec=SPEC)
    msg = convert.fill_request(NS(task_id=[0] * 16), create, d)
    assert (msg.origin_node, msg.target_leader, msg.op, msg.origin) == (2, 1, 1, 1)
    assert convert.request_from_msg(msg, d) == create
    destroy = Request("edge-02", 5, 10, "edge-01", 3, p.DESTROY, task_id=T1, reason="done")
    assert convert.request_from_msg(convert.fill_request(NS(spec_json=""), destroy, d), d) == destroy
    with pytest.raises(ValueError):
        convert.fill_request(blank(), Request("edge-02", 5, 11, "edge-01", 3, p.QUERY, task_id="nope"), d)


def reply_msg():
    return NS(task_id=[0] * 16, spec_json="", name="", error_message="", last_event="")


def test_reply_round_trip_maps_errors_to_results():
    d = directory()
    ok = Reply("edge-02", 5, 9, "edge-01", 3, task_id=T1, duplicate=True, status=c.RUNNING,
               generation=Generation(1, 3), primary="edge-02", helpers=("edge-03",))
    msg = convert.fill_reply(reply_msg(), ok, d)
    assert (msg.result, msg.status, msg.primary_node, msg.helper_nodes) == (0, 2, 2, [3])
    assert convert.reply_from_msg(msg, d) == ok
    failed = convert.fill_reply(reply_msg(), Reply("edge-02", 5, 9, "edge-01", 3, c.INFERENCE_ERROR, "boom",
                                                   task_id=T1, status=c.FAILED), d)
    assert (failed.result, failed.exec_error) == (convert.RES_TASK_FAILED, 7)
    assert convert.reply_from_msg(failed, d).error_code == c.INFERENCE_ERROR
    for code in (c.NOT_LEADER, c.TIMEOUT, c.NOT_FOUND, c.NO_FEASIBLE_NODE, c.CANCELLED_ERROR):
        reply = convert.reply_from_msg(convert.fill_reply(reply_msg(), Reply("edge-02", 5, 9, "edge-01", 3, code), d),
                                       d)
        assert reply.error_code == code and reply.task_id == ""


def test_query_reply_carries_the_record():
    d = directory()
    table = TaskTable()
    table.apply(Submit(T1, "k", SPEC))
    record = table.get(T1)
    msg = convert.fill_reply(reply_msg(), Reply("edge-02", 5, 9, "edge-01", 3, task_id=T1, status=record.status,
                                                record=record), d)
    back = convert.reply_from_msg(msg, d).record
    assert (back.task_id, back.status, back.spec, back.name) == (T1, c.PENDING, SPEC, record.name)


def test_access_layer_conversion():
    request = NS(target_nodes=["a"], **{name: getattr(SPEC, name) for name in convert.SPEC_FIELDS})
    assert convert.spec_from_request(request) == SPEC.__class__(**{**SPEC.to_dict(), "target_nodes": ("a",)})
    response = convert.fill_create_response(blank(), CreateReply(True, task_id=T1, dispatched_nodes=("a", "b")))
    assert response.dispatched_nodes == ["a", "b"] and response.candidate_nodes == []
    table = TaskTable()
    table.apply(Submit(T1, "k", SPEC))
    response = convert.fill_query_response(blank(), QueryReply(True, record=table.get(T1)))
    assert response.success and response.status == c.PENDING and response.mode == "redundant"
    missing = convert.fill_query_response(blank(), QueryReply(False, c.NOT_FOUND, "no such task"))
    assert not missing.success and missing.error_code == c.NOT_FOUND
    destroyed = convert.fill_destroy_response(blank(), DestroyReply(True, final_status=c.CANCELLED))
    assert destroyed.success and destroyed.final_status == c.CANCELLED


def test_status_and_result_conversion():
    status = convert.fill_task_status(blank(), StatusEvent(4, "t-1", "n", "PKCI", c.RUNNING, 2, "a", "m"), 7, "now")
    assert (status.seq, status.term, status.epoch, status.task_id) == (4, 7, 2, "t-1")
    result = NS(task_id="t-1", epoch=2, assign_term=5, node_name="a", task_type="PKCT", drone_id=0, frame_id=5,
                success=True, valid=True, message="ok", latency_ms=12.3456, source_stamp=NS(sec=1, nanosec=500000000),
                payload=b"xyz", x=1.0, y=2.0, width=3.0, height=4.0, confidence=0.5)
    data = convert.result_to_dict(result)
    assert data["bbox"] == [1.0, 2.0, 3.0, 4.0] and data["source_stamp"] == 1.5 and data["assign_term"] == 5


def test_names_namespaces_and_node_ids():
    assert ros_namespace("edge-01") == "/edge_01" and ros_namespace("3rd.board") == "/n3rd_board"
    assert names.on_node("edge-01", names.TASK_RESULT_TOPIC) == "/edge_01/task/result"
    assert default_node_id("edge-07") == 7 and default_node_id("uav254") == 254
    for bad in ("leader", "edge-0", "edge-255"):
        with pytest.raises(ValueError):
            default_node_id(bad)
