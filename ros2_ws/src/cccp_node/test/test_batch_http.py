"""Opt-in real algorithm smoke through the HTTP API and fake ROS control bus."""
import json
import os
import time
from urllib.request import Request, urlopen

import pytest

from test_ros_integration import INSTALLED, cluster, active_leader, wait_for
from cccp_node.ue_api import UeApi


@pytest.mark.skipif(not INSTALLED, reason="requires fake ROS control bus")
@pytest.mark.parametrize("cluster", ["local_calls"], indirect=True)
@pytest.mark.parametrize("kind", ["RSSMAP", "FASTLIVO"])
def test_batch_http(cluster, kind):
    if not os.getenv("CCCP_REAL_" + kind):
        pytest.skip("requires installed algorithm and sample")
    node = wait_for(lambda: active_leader(cluster)).control
    api = UeApi(node.ue, "127.0.0.1", 0, node.get_logger())
    base = f"http://127.0.0.1:{api.port}"
    try:
        body = {"task_type": kind, "request_node": "edge-01"}
        if kind == "RSSMAP":
            body["params"] = {"tx": "node_7", "score": True}
        req = Request(base + "/api/ue/tasks", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
        with urlopen(req) as response:
            created = json.load(response)
        assert created["success"], created
        url = base + "/api/ue/tasks/" + created["task_id"]
        deadline = time.monotonic() + 1100
        while time.monotonic() < deadline:
            with urlopen(url) as response:
                status = json.load(response)
            if status["status"] in ("Finished", "Failed", "Cancelled"):
                break
            time.sleep(1)
        assert status["status"] == "Finished", status
        with urlopen(base + status["result_url"]) as response:
            files = json.load(response)["files"]
        assert files
        with urlopen(base + files[0]) as response:
            assert int(response.headers["Content-Length"]) > 100
            assert response.read(2) in (b"PK", b"# ")
    finally:
        api.shutdown()

@pytest.mark.skipif(not INSTALLED, reason="requires fake ROS control bus")
@pytest.mark.parametrize("cluster", ["local_calls"], indirect=True)
def test_unknown_transmitter(cluster):
    if not os.getenv("CCCP_REAL_RSSMAP"):
        pytest.skip("requires propagation model and sample")
    gateway = wait_for(lambda: active_leader(cluster)).control.ue
    result = gateway.create({"task_type": "RSSMAP", "params": {"tx": "missing-transmitter"}})
    assert result["success"], result
    def failed():
        status = gateway.query(result["task_id"])
        return status if status.get("status") == "Failed" else None
    status = wait_for(failed, timeout=90)
    assert "missing-transmitter" in status["error_message"]
    assert not status["result_url"]