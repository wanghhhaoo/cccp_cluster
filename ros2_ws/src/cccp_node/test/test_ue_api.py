"""UE HTTP API against a real HTTP server on localhost, backed by LocalConsensus."""
import json
import logging
import urllib.error
import urllib.request
from types import SimpleNamespace as NS

import pytest

from cccp_node.consensus.local import LocalConsensus
from cccp_node.control.gateway import Gateway, GatewayConfig
from cccp_node.control.ue_backend import UeBackend
from cccp_node.core import constants as c
from cccp_node.core.cluster_view import ClusterView, NodeView
from cccp_node.core.task_table import Assign, ReportStatus, StatusEvent
from cccp_node.ue_api import EventLog, UeApi, spec_from_body


@pytest.fixture
def server():
    consensus = LocalConsensus("n1")
    cluster = ClusterView(5.0)
    cluster.update(NodeView("n1", seq=1, is_leader=True, inference_ready=True), now=0.0)
    watched = []
    backend = UeBackend("n1", Gateway(consensus, GatewayConfig(create_wait_timeout_s=0.05)), cluster,
                        watch=lambda node, images: watched.append((node, images)),
                        leader_name=lambda: "n1", clock=lambda: 1.0)
    api = UeApi(backend, "127.0.0.1", 0, logging.getLogger("ue"))
    yield NS(base="http://127.0.0.1:%d/api/ue" % api.port, consensus=consensus, backend=backend, watched=watched)
    api.shutdown()


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def call_json(method, url, body=None):
    status, raw = call(method, url, body)
    return status, json.loads(raw)


def test_health_and_nodes(server):
    status, body = call_json("GET", server.base + "/health")
    assert status == 200 and body["leader"] == "n1"
    status, body = call_json("GET", server.base + "/nodes")
    assert status == 200 and body["nodes"][0]["name"] == "n1" and body["nodes"][0]["status"] == "Online"


def test_create_query_destroy(server):
    status, body = call_json("POST", server.base + "/tasks",
                             {"task_type": "PKCT", "params": {"bbox": [1, 2, 3, 4]}, "idempotency_key": "ue:1"})
    assert status == 200 and body["task_id"] == 1
    status, again = call_json("POST", server.base + "/tasks",
                              {"task_type": "PKCT", "params": {"bbox": [1, 2, 3, 4]}, "idempotency_key": "ue:1"})
    assert again["task_id"] == 1 and again["duplicate"]
    server.consensus.submit(Assign(1, 0, "n2"), 1.0)
    status, body = call_json("GET", server.base + "/tasks/1")
    assert status == 200 and body["status"] == c.SCHEDULED and body["node"] == "n2"
    assert ("n2", False) in server.watched
    status, body = call_json("DELETE", server.base + "/tasks/1")
    assert status == 200 and body["final_status"] == c.CANCELLED
    status, body = call_json("POST", server.base + "/tasks/1/destroy", {"reason": "again"})
    assert status == 409 and body["error_code"] == c.INVALID_STATE
    status, body = call_json("DELETE", server.base + "/tasks/42")
    assert status == 404


def test_create_errors_map_to_status_codes(server):
    status, body = call_json("POST", server.base + "/tasks", {"task_type": "PKCT"})          # bbox missing
    assert status == 400 and body["task_id"] == 0 and body["error_code"] == c.INVALID_INPUT
    status, body = call_json("POST", server.base + "/tasks", {"task_type": "PKCI", "drone_id": 999})
    assert status == 400
    status, raw = call("POST", server.base + "/tasks", None)
    assert status == 400
    status, body = call_json("GET", server.base + "/tasks/77")
    assert status == 404 and body["error_code"] == c.NOT_FOUND
    status, body = call_json("GET", server.base + "/nope")
    assert status == 404


def test_failed_task_returns_502(server):
    def fail_on_submit(events):
        for event in events:
            if event.status == c.PENDING:
                server.consensus.submit(Assign(event.task_id, 0, "n1"), 1.0)
                server.consensus.submit(ReportStatus("n1", event.task_id, 1, c.REPORT_FAILED,
                                                     c.INFERENCE_ERROR, "model crashed"), 1.0)
    server.consensus.add_event_listener(fail_on_submit)
    status, body = call_json("POST", server.base + "/tasks", {"task_type": "PKCI"})
    assert status == 502 and body["task_id"] == 1 and body["error_code"] == c.INFERENCE_ERROR


def test_events_long_poll_with_status_and_results(server):
    backend = server.backend
    backend.on_status(StatusEvent(1, 7, "t", "PKCI", c.RUNNING, 1, "n2", "running"))
    backend.on_status(StatusEvent(1, 7, "t", "PKCI", c.RUNNING, 1, "n2", "dup"))     # same seq: ignored
    backend.on_result({"task_id": 7, "frame_id": 1})
    status, body = call_json("GET", server.base + "/events?after=0&wait=0")
    assert [e["kind"] for e in body["events"]] == ["status", "result"] and body["last_seq"] == 2
    status, body = call_json("GET", server.base + "/events?after=2&wait=0.1")
    assert body["events"] == []
    assert backend.latest_result(7) == {"task_id": 7, "frame_id": 1}


def test_result_image_is_jpeg(server):
    status, _ = call("GET", server.base + "/tasks/5/result_image")
    assert status == 404
    image = NS(width=2, height=2, step=6, encoding="bgr8", data=bytes(range(12)))
    server.backend.on_result_image(5, image)
    status, raw = call("GET", server.base + "/tasks/5/result_image")
    assert status == 200 and raw[:2] == b"\xff\xd8"


def test_body_parsing():
    spec, key = spec_from_body({"task_type": "PKCI", "target_nodes": ["a", 2], "params_json": "{}",
                                "mode": "fusion", "helper_count": 1})
    assert spec.target_nodes == ("a", "2") and spec.helper_count == 1 and key == ""
    with pytest.raises(ValueError):
        spec_from_body([])
    with pytest.raises(ValueError):
        spec_from_body({"target_nodes": "a"})


def test_event_log_bounds():
    log = EventLog(capacity=3)
    for i in range(5):
        log.add("status", {"i": i})
    events, last = log.since(0, 0)
    assert last == 5 and [e["i"] for e in events] == [2, 3, 4]
