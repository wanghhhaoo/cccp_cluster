"""Batch tasks (FASTLIVO, RSSMAP, HETEROMAP) with stand-in algorithm scripts, on fake_ros.

The real algorithms are covered by the opt-in test_batch_http.py. These tests run everywhere:
a shell script plays the algorithm, so they check the executor's job lifecycle (finish, stop
without blocking, kill after the grace period, a helper's finished session) and the artifact API.
"""
import json
import time
import uuid
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import fake_ros
from test_ros_integration import INSTALLED, FakeEngine, active_leader, cluster, record_where, wait_for  # noqa: F401

from cccp_node import names
from cccp_node.core import constants as c
from cccp_node.core.naming import ros_namespace
from cccp_node.core.task_spec import TaskSpec

if INSTALLED:
    from cccp_cluster_interfaces.msg import Command, ExecEvent

    from cccp_node.ros import convert, qos
    from cccp_node.ue_api import UeApi
    from cccp_task_executor import batch
    from cccp_task_executor.executor_node import TaskExecutor
    from cccp_task_executor.inference import LocalBackend

pytestmark = pytest.mark.skipif(not INSTALLED, reason="requires fake ROS control bus")

# Plays RSSMAP and HETEROMAP: writes --out if given; FAKE_BATCH=hang ignores SIGTERM
# (and creates <out>.trapped once it does).
SCRIPT = """#!/bin/bash
out=""
while [ $# -gt 0 ]; do [ "$1" = --out ] && out=$2; shift; done
if [ "$FAKE_BATCH" = hang ]; then trap '' TERM; touch "$out.trapped"; while true; do sleep 0.05; done; fi
if [ -n "$out" ]; then printf 'PK fake prediction' > "$out"; fi
"""


@pytest.fixture
def batch_env(tmp_path, monkeypatch):
    """Every executor in this process can run RSSMAP and HETEROMAP with the stand-in script."""
    script = tmp_path / "fake-algorithm"
    script.write_text(SCRIPT)
    script.chmod(0o755)
    data = tmp_path / "rss"
    for name in ("config/config.json", "weights/newdata.pt", "field/gaussians.npz", "field/voxel_grid.npz"):
        (data / name).parent.mkdir(parents=True, exist_ok=True)
        (data / name).write_text("")
    bev = tmp_path / "bev"
    (bev / "weights").mkdir(parents=True)
    for name in ["collaboration_best.pth"] + [f"id{i}_bev_best.pth" for i in range(5)]:
        (bev / "weights" / name).write_text("")
    store = tmp_path / "jobs"
    store.mkdir()
    for key, value in dict(RSSMAP_PYTHON=script, RSSMAP_DATA=data, HETEROMAP_PYTHON=script, HETEROMAP_ROOT=bev,
                           CCCP_JOB_STORE=store).items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv("FAKE_BATCH", raising=False)
    monkeypatch.setattr(batch, "STOP_GRACE_S", 1.0)
    return store


def http_get(url):
    try:
        with urlopen(url) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


@pytest.mark.parametrize("cluster", ["local_calls"], indirect=True)
def test_batch_task_finishes_and_its_artifacts_are_listed(batch_env, cluster):
    leader = wait_for(lambda: active_leader(cluster))
    gateway = leader.control.ue
    created = wait_for(lambda: (lambda r: r if r["success"] else None)(
        gateway.create({"task_type": c.RSSMAP, "params": {"tx": "node_7"}})))
    status = wait_for(lambda: (lambda s: s if s.get("status") == c.FINISHED else None)(
        gateway.query(created["task_id"])))
    assert status["result_url"] == "/api/ue/tasks/%s/artifacts/1-%d" % (
        created["task_id"], table_record(cluster, created["task_id"]).assign_term)
    api = UeApi(gateway, "127.0.0.1", 0, leader.control.get_logger())
    base = "http://127.0.0.1:%d" % api.port
    try:
        code, body = http_get(base + status["result_url"])
        assert code == 200 and body["files"] == [status["result_url"] + "/primary/rss.npz"]
        with urlopen(base + body["files"][0]) as response:
            assert response.read() == b"PK fake prediction"
        code, body = http_get(base + status["result_url"] + "/primary/missing.npz")
        assert code == 404 and body["success"] is False and body["error_code"] == c.NOT_FOUND
    finally:
        api.shutdown()


def table_record(nodes, task_id):
    return wait_for(record_where(nodes, task_id, lambda r: True))


@pytest.mark.parametrize("cluster", ["local_calls"], indirect=True)
def test_destroying_a_batch_task_does_not_block_and_kills_a_job_ignoring_sigterm(batch_env, cluster, monkeypatch):
    monkeypatch.setenv("FAKE_BATCH", "hang")
    leader = wait_for(lambda: active_leader(cluster))
    gateway = leader.control.ue
    created = wait_for(lambda: (lambda r: r if r["success"] else None)(gateway.create({"task_type": c.RSSMAP})))
    task_id = created["task_id"]
    record = wait_for(record_where(cluster, task_id, lambda r: r.status == c.RUNNING))
    executor = cluster[record.primary].executor
    job = executor._tasks[task_id].job
    wait_for(lambda: (job.output / "rss.npz.trapped").exists())

    started = time.monotonic()
    destroyed = gateway.destroy(task_id, "test")
    assert destroyed["success"], destroyed
    assert time.monotonic() - started < batch.STOP_GRACE_S      # the executor did not wait for the job
    assert job.process.poll() is None and job in executor._stopping
    wait_for(lambda: job.process.poll() is not None, timeout=10.0)   # SIGKILL after the grace period
    assert time.monotonic() - started >= batch.STOP_GRACE_S
    wait_for(lambda: job not in executor._stopping)


# ------------------------------------------------------- one executor, commands sent by hand

@pytest.fixture
def executor(batch_env):
    fake_ros.BUS.reset()
    fake_ros.BUS.namespace = ros_namespace("edge-02")
    fake_ros.BUS.params = dict(node_name="edge-02", status_period=60.0, input_timeout=0.0)
    node = TaskExecutor(backend=LocalBackend(FakeEngine(), FakeEngine()))
    node.events = []
    probe = fake_ros.Node("probe")
    probe.create_subscription(ExecEvent, names.EXEC_EVENT_TOPIC, node.events.append, qos.EXEC_EVENT)
    yield node
    node.destroy_node()


HELPERS = [3, 2, 4, 5, 6]          # edge-02 is the second helper: role id_1


def command(op, task_id, **extra):
    spec = TaskSpec(task_type=c.HETEROMAP, mode=c.MODE_FUSION,
                    target_nodes=("edge-01",) + tuple("edge-%02d" % n for n in HELPERS))
    return Command(op=op, term=1, targets=[2], task_id=convert.uuid_bytes(task_id), epoch=1, assign_term=1,
                   primary_node=1, primary_name="edge-01", helper_nodes=HELPERS, idempotency_key="bev",
                   name="bev", spec_json=spec.to_json(), **extra)


def last_event(node):
    event = node.events[-1]
    return event.kind, event.error_code


def test_finished_helper_keeps_its_session_until_the_leader_stops_it(executor, batch_env):
    task_id = str(uuid.uuid4())
    executor._on_command(command(Command.OP_START, task_id))
    assert last_event(executor) == (ExecEvent.KIND_RUNNING, ExecEvent.ERR_NONE)
    job = executor._tasks[task_id].job
    assert job.output == batch_env / task_id / "1-1" / "id_1"           # helper position -> role
    wait_for(lambda: (executor._poll_jobs(), job.exited)[1])

    # The helper's part is done but the primary may still be fusing: it must keep reporting
    # the session, or reconciliation would take it for "not started" and send START again.
    assert task_id in executor._tasks and task_id in executor._guard.running()
    assert last_event(executor) == (ExecEvent.KIND_RUNNING, ExecEvent.ERR_NONE)
    executor._on_command(command(Command.OP_START, task_id))           # a resent START is a no-op
    assert last_event(executor) == (ExecEvent.KIND_RUNNING, ExecEvent.ERR_NONE)
    assert executor._tasks[task_id].job is job

    executor._on_command(command(Command.OP_STOP, task_id, stop_reason=Command.STOP_STALE, reason="finished"))
    assert last_event(executor) == (ExecEvent.KIND_STOPPED, ExecEvent.ERR_NONE)
    assert task_id not in executor._tasks and not executor._stopping


def test_start_of_a_generation_that_already_ran_here_is_refused_as_retryable(executor):
    """After an executor restart its sessions are gone but their output folders are not."""
    task_id = str(uuid.uuid4())
    executor._on_command(command(Command.OP_START, task_id))
    executor._on_command(command(Command.OP_STOP, task_id, stop_reason=Command.STOP_STALE, reason="test"))
    executor._on_command(command(Command.OP_START, task_id))
    assert last_event(executor) == (ExecEvent.KIND_REJECTED, ExecEvent.ERR_NOT_READY)
    assert task_id not in executor._tasks and task_id not in executor._guard.running()


# ------------------------------------------------------- FASTLIVO's container, with a fake docker

FAKE_DOCKER = """#!/bin/bash
echo "$*" >> "$DOCKER_LOG"
if [ "$1" = run ]; then [ "$FAKE_BATCH" = hang ] && trap '' TERM; while true; do sleep 0.05; done; fi
"""


@pytest.fixture
def docker_log(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", "%s:%s" % (bin_dir, __import__("os").environ["PATH"]))
    monkeypatch.setenv("DOCKER_LOG", str(tmp_path / "docker.log"))
    return tmp_path / "docker.log"


def fastlivo_task():
    from types import SimpleNamespace
    return SimpleNamespace(task_id=str(uuid.uuid4()), epoch=2, assign_term=3, role=c.ROLE_PRIMARY, helper_ids=(),
                           task_type=c.FASTLIVO, params={})


def docker_calls(log):
    return log.read_text().splitlines() if log.exists() else []


def test_stopped_fastlivo_script_removes_its_named_container(batch_env, docker_log):
    task = fastlivo_task()
    job = batch.start(task, 1)
    name = "cccp-fastlivo-%s-2-3" % task.task_id
    assert job.container == name
    wait_for(lambda: any(call.startswith("run ") for call in docker_calls(docker_log)))
    assert "--name %s" % name in docker_calls(docker_log)[0]
    assert batch.stop(job)
    wait_for(lambda: job.process.poll() is not None)          # the script's TERM trap: exit, docker rm
    assert "rm -f %s" % name in docker_calls(docker_log)
    assert batch.reap([job]) == []


def test_killed_fastlivo_job_has_its_container_removed_by_name(batch_env, docker_log, monkeypatch):
    """A container belongs to the Docker daemon: a SIGKILL of the process group leaves it running."""
    monkeypatch.setenv("FAKE_BATCH", "hang")
    script = batch_env.parent / "fake-algorithm"                 # ignores SIGTERM, like a stuck script
    process = __import__("subprocess").Popen([str(script), "--out", str(batch_env / "out")], start_new_session=True)
    job = batch.Job(process, batch_env, container="cccp-fastlivo-x")
    wait_for(lambda: (batch_env / "out.trapped").exists())
    assert batch.stop(job, now=0.0)
    assert batch.reap([job], now=0.5) == [job] and process.poll() is None
    assert batch.reap([job], now=1.0) == [job]                     # SIGKILL sent, docker rm started
    wait_for(lambda: batch.reap([job]) == [])
    assert process.returncode == -9 and docker_calls(docker_log) == ["rm -f cccp-fastlivo-x"]
