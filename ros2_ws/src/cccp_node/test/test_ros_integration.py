"""Three control nodes and three executors wired through fake_ros, with real PySyncObj on localhost.

Exercises the ROS layer (control_node, dispatcher, convert, executor_node)
end to end on machines without ROS: creating a task through the CreateTask
handler, per-frame results reaching another node's UE backend, and the
leader dying while the task runs. Skipped when the real rclpy is installed.
"""
import socket
import sys
import threading
import time
from types import SimpleNamespace as NS

import pytest
import yaml

import fake_ros

INSTALLED = fake_ros.install()
pytestmark = pytest.mark.skipif(not INSTALLED, reason="real ROS present: run the colcon tests instead")
pytest.importorskip("pysyncobj")
sys.path.insert(0, str(fake_ros.SRC / "cccp_task_executor"))

from cccp_node.core import constants as c  # noqa: E402
from cccp_node.core.naming import ros_namespace  # noqa: E402

if INSTALLED:
    from cccp_task_interfaces.srv import CreateTask, DestroyTask  # noqa: E402
    from collab_inference_interfaces.srv import Classify, InitTracker, ResetTracker, Track  # noqa: E402
    from sensor_msgs.msg import Image  # noqa: E402

    from cccp_node.ros.control_node import ControlNode  # noqa: E402
    from cccp_task_executor.executor_node import TaskExecutor  # noqa: E402

NAMES = ("edge-01", "edge-02", "edge-03")


class FakeAlgorithms(fake_ros.Node):
    """PKCI/PKCT services answering instantly, standing in for collab_inference."""

    def __init__(self):
        super().__init__("fake_algorithms")
        self.create_service(Classify, "algorithms/pkci/classify", self._classify)
        self.create_service(InitTracker, "algorithms/pkct/init", lambda req, res: self._ok(res))
        self.create_service(Track, "algorithms/pkct/track", self._track)
        self.create_service(ResetTracker, "algorithms/pkct/reset", lambda req, res: self._ok(res))

    @staticmethod
    def _ok(response):
        response.success = True
        return response

    def _classify(self, request, response):
        response.success, response.label, response.class_name = True, 3, "car"
        response.score, response.confidence, response.latency_ms = 0.9, 0.9, 1.5
        response.log_probs, response.increment = [0.0] * 44, b"inc"
        return response

    def _track(self, request, response):
        response.success, response.x, response.y, response.width, response.height = True, 1.0, 2.0, 3.0, 4.0
        response.confidence, response.identity = 0.8, b"id"
        return response


def free_ports(n):
    sockets = [socket.socket() for _ in range(n)]
    for s in sockets:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    return ports


def wait_for(predicate, timeout=15.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not met within %.0f s" % timeout)


def kill(node):
    node.control.shutdown()
    for part in (node.control, node.executor, node.algorithms):
        part.destroy_node()
    node.dead = True


class FixedProbe:
    """Constant resources: the real probe runs nvidia-smi, which stalls the shared fake timer thread."""

    def __init__(self):
        from cccp_node.monitor.probes import Hardware
        self.hardware = Hardware(cpu_model="test", cpu_cores=8, mem_total_mb=8192)

    def sample(self):
        from cccp_node.monitor.probes import Usage
        return Usage(cpu_usage=20.0, mem_usage=20.0)


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    monkeypatch.setattr("cccp_node.ros.control_node.SystemProbe", FixedProbe)
    config = tmp_path / "cluster.yaml"
    members = [{"name": n, "ip": "127.0.0.1", "port": p} for n, p in zip(NAMES, free_ports(3))]
    config.write_text(yaml.safe_dump({"protocol_version": 1, "members": members}))
    fake_ros.BUS.reset()
    nodes = {}
    for name in NAMES:
        fake_ros.BUS.namespace = ros_namespace(name)
        fake_ros.BUS.params = dict(
            node_name=name, cluster_config=str(config), data_dir="", status_period=0.2, offline_timeout=1.0,
            executor_timeout=1.0, dispatch_timeout=1.0, service_wait_timeout=0.5, ue_http_port=0,
            raft_heartbeat=0.05, raft_election_min=0.3, raft_election_max=0.6, raft_leader_fallback=1.0,
        )
        nodes[name] = NS(name=name, control=ControlNode(), executor=TaskExecutor(), algorithms=FakeAlgorithms(),
                         dead=False)
    stop = threading.Event()
    timers = threading.Thread(target=fake_ros.BUS.run_timers, args=(stop,), daemon=True)
    timers.start()
    yield nodes
    stop.set()
    timers.join()
    for node in nodes.values():
        if not node.dead:
            kill(node)


def live(nodes):
    return [n for n in nodes.values() if not n.dead]


def active_leader(nodes):
    leaders = [n for n in live(nodes) if n.control.controller.active]
    return leaders[0] if len(leaders) == 1 else None


def runners(nodes, task_id):
    return [n.name for n in live(nodes) if task_id in n.executor._tasks]


def publish_frame(node):
    camera = fake_ros.Publisher(node.executor, Image, "%s/camera/image" % ros_namespace(node.name))
    camera.publish(Image(height=2, width=2, step=6, encoding="rgb8", data=[0] * 12))


def test_task_lifecycle_and_leader_failover(cluster):
    leader = wait_for(lambda: active_leader(cluster))
    creator = next(n for n in live(cluster) if n is not leader)

    request = CreateTask.Request(task_type="PKCI", idempotency_key="it-1", name="demo")
    reply = creator.control._on_create(request, CreateTask.Response())
    assert reply.success, reply.message
    assert reply.task_id == 1 and reply.assigned_node in NAMES
    assert runners(cluster, 1) == [reply.assigned_node]
    again = creator.control._on_create(request, CreateTask.Response())
    assert again.duplicate and again.task_id == 1

    # per-frame results reach the UE backend of the node that was asked
    creator.control.ue.query(1)
    wait_for(lambda: (reply.assigned_node, False) in creator.control._watched)
    primary = cluster[reply.assigned_node]
    wait_for(lambda: (publish_frame(primary), creator.control.ue.latest_result(1))[1])
    result = creator.control.ue.latest_result(1)
    assert result["class_name"] == "car" and result["epoch"] == 1 and result["node"] == primary.name
    # TaskStatus is asynchronous: the leader publishes it from a timer after applying the change
    def statuses():
        return [e["status"] for e in creator.control.ue.events.since(0, 0)[0] if e["kind"] == "status"]
    wait_for(lambda: len(statuses()) >= 3)
    assert statuses()[:3] == [c.PENDING, c.SCHEDULED, c.RUNNING]

    # the leader dies; the task must keep exactly one runner
    old_primary = reply.assigned_node
    kill(leader)
    new_leader = wait_for(lambda: active_leader(cluster))
    assert new_leader is not leader
    record = wait_for(lambda: (lambda r: r if r and r.status == c.RUNNING and runners(cluster, 1) == [r.primary]
                               else None)(new_leader.control.consensus.get(1)), timeout=20.0)
    print("killed leader %s, primary was %s, now %s at epoch %d"
          % (leader.name, old_primary, record.primary, record.epoch))
    if old_primary == leader.name:
        assert record.epoch == 2 and record.primary != old_primary
    else:
        assert record.epoch == 1 and record.primary == old_primary
    time.sleep(1.5)                                     # several reconcile rounds later: still one runner
    assert runners(cluster, 1) == [new_leader.control.consensus.get(1).primary]

    # destroy through the surviving gateway stops the executor
    destroy = creator.control if not creator.dead else new_leader.control
    response = destroy._on_destroy(DestroyTask.Request(task_id=1, reason="done"), DestroyTask.Response())
    assert response.success and response.final_status == c.CANCELLED
    wait_for(lambda: runners(cluster, 1) == [])


PKCT_BBOX = '{"bbox": [1, 2, 3, 4]}'


def test_unpinned_task_moves_when_its_primary_dies(cluster):
    leader = wait_for(lambda: active_leader(cluster))
    gateway = next(n for n in live(cluster) if n is not leader)
    reply = gateway.control._on_create(CreateTask.Request(task_type="PKCT", params_json=PKCT_BBOX),
                                       CreateTask.Response())
    assert reply.success
    primary = cluster[reply.assigned_node]
    assert primary.executor._tasks[reply.task_id].tracker_id == "task-%d-1" % reply.task_id
    kill(primary)
    reader = (gateway if not gateway.dead else leader).control.consensus
    record = wait_for(lambda: (lambda r: r if r and r.epoch == 2 and r.status == c.RUNNING else None)(
        reader.get(reply.task_id)), timeout=20.0)
    print("killed primary %s; task moved to %s" % (primary.name, record.primary))
    assert record.primary != primary.name and runners(cluster, reply.task_id) == [record.primary]
    assert cluster[record.primary].executor._tasks[reply.task_id].tracker_id == "task-%d-2" % reply.task_id


def test_pinned_task_waits_when_its_node_dies_even_if_it_was_the_leader(cluster):
    """request_node is a hard pin: the task is not silently moved to another node's camera."""
    leader = wait_for(lambda: active_leader(cluster))
    gateway = next(n for n in live(cluster) if n is not leader)
    request = CreateTask.Request(task_type="PKCT", params_json=PKCT_BBOX, request_node=leader.name)
    reply = gateway.control._on_create(request, CreateTask.Response())
    assert reply.success and reply.assigned_node == leader.name
    kill(leader)
    wait_for(lambda: active_leader(cluster))
    record = wait_for(lambda: (lambda r: r if r and r.epoch == 2 else None)(
        gateway.control.consensus.get(reply.task_id)), timeout=20.0)
    assert record.status == c.PENDING and record.primary == "" and "offline" in record.last_event
    assert runners(cluster, reply.task_id) == []


def test_fake_messages_are_strict():
    from cccp_cluster_interfaces.msg import NodeStatus
    msg = NodeStatus()
    with pytest.raises(AttributeError):
        msg.no_such_field = 1
    with pytest.raises(AssertionError):
        msg.cpu_usage = 1                               # float field: int rejected, like rosidl
    msg.cpu_usage = 1.0
    assert NodeStatus.PROTOCOL_VERSION == 1
