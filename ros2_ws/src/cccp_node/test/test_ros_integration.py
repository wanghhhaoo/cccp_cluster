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
    from cccp_task_interfaces.msg import TaskResult  # noqa: E402
    from collab_inference_interfaces.srv import Classify, InitTracker, ResetTracker, Track  # noqa: E402
    from sensor_msgs.msg import Image  # noqa: E402

    from cccp_node.ros.control_node import ControlNode  # noqa: E402
    from cccp_task_executor.executor_node import TaskExecutor  # noqa: E402
    from cccp_task_executor.inference import LocalBackend  # noqa: E402

NAMES = ("edge-01", "edge-02", "edge-03")


class FakeEngine:
    """Answers like collab_inference's PkciEngine + PkctEngine, instantly and without models."""

    @staticmethod
    def _ok(request, response):
        response.success = True
        return response

    init_tracker = reset = _ok

    def classify(self, request, response):
        response.success, response.label, response.class_name = True, 3, "car"
        response.score, response.confidence, response.latency_ms = 0.9, 0.9, 1.5
        response.log_probs, response.increment = [0.0] * 44, b"inc"
        return response

    def track(self, request, response):
        response.success, response.x, response.y, response.width, response.height = True, 1.0, 2.0, 3.0, 4.0
        response.confidence, response.identity = 0.8, b"id"
        return response


class FakeAlgorithms(fake_ros.Node):
    """The FakeEngine exposed as <ns>/algorithms/* services, like PkciServer/PkctServer."""

    def __init__(self, engine):
        super().__init__("fake_algorithms")
        self.create_service(Classify, "algorithms/pkci/classify", engine.classify)
        self.create_service(InitTracker, "algorithms/pkct/init", engine.init_tracker)
        self.create_service(Track, "algorithms/pkct/track", engine.track)
        self.create_service(ResetTracker, "algorithms/pkct/reset", engine.reset)


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


class Cluster(dict):
    """name -> node parts, plus the fake AirSim server they share."""

    def __init__(self, airsim):
        super().__init__()
        self.airsim = airsim


class FakeAirSim:
    """Stands in for the AirSim RPC server; counts frames fetched by each node."""

    def __init__(self):
        self.fetches = {}
        self._lock = threading.Lock()

    def client(self, node_name):
        airsim = self

        class Client:
            def get(self, camera, vehicle):
                with airsim._lock:
                    key = (node_name, vehicle, camera)
                    airsim.fetches[key] = airsim.fetches.get(key, 0) + 1
                return NS(image_data_uint8=bytes(4 * 4 * 3), width=4, height=4, compress=False, message="")
        return Client

    def fetchers(self):
        with self._lock:
            return sorted({node for node, _, _ in self.fetches})


@pytest.fixture(params=["ros_services", "local_calls"])
def cluster(tmp_path, monkeypatch, request):
    """Executors reach the algorithms through ROS services or in-process calls (data_plane default).

    @pytest.mark.airsim_mode("relay") selects how airsim:// inputs are fetched (default "each").
    """
    marker = request.node.get_closest_marker("airsim_mode")
    airsim_mode = marker.args[0] if marker else "each"
    airsim = FakeAirSim()
    monkeypatch.setattr("cccp_node.ros.control_node.SystemProbe", FixedProbe)
    config = tmp_path / "cluster.yaml"
    members = [{"name": n, "ip": "127.0.0.1", "port": p} for n, p in zip(NAMES, free_ports(3))]
    config.write_text(yaml.safe_dump({"protocol_version": 1, "members": members}))
    fake_ros.BUS.reset()
    nodes = Cluster(airsim)
    for name in NAMES:
        fake_ros.BUS.namespace = ros_namespace(name)
        fake_ros.BUS.params = dict(
            node_name=name, cluster_config=str(config), data_dir="", status_period=0.2, offline_timeout=1.0,
            executor_timeout=1.0, dispatch_timeout=1.0, service_wait_timeout=0.5, ue_http_port=0,
            raft_heartbeat=0.05, raft_election_min=0.3, raft_election_max=0.6, raft_leader_fallback=1.0,
            airsim_fetch_mode=airsim_mode, airsim_fps=20.0,
        )
        engine = FakeEngine()
        if request.param == "local_calls":
            backend = LocalBackend(engine, engine)
            algorithms = fake_ros.Node("no_algorithm_services")
        else:
            backend, algorithms = None, FakeAlgorithms(engine)
        executor = TaskExecutor(backend=backend, airsim_client_factory=airsim.client(name))
        nodes[name] = NS(name=name, control=ControlNode(), executor=executor, algorithms=algorithms, dead=False)
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


# ------------------------------------------------------------- input sources

class Results:
    """Collects (node, task_id, epoch) of every TaskResult published in the cluster."""

    def __init__(self):
        self._seen = set()
        self._lock = threading.Lock()
        self._node = fake_ros.Node("observer", namespace="/observer")
        for name in NAMES:
            self._node.create_subscription(TaskResult, "%s/task/result" % ros_namespace(name), self._on, None)

    def _on(self, msg):
        with self._lock:
            self._seen.add((msg.node_name, msg.task_id, msg.epoch))

    def nodes(self, task_id, epoch=None):
        with self._lock:
            return {n for n, t, e in self._seen if t == task_id and (epoch is None or e == epoch)}


def create(node, **fields):
    reply = node.control._on_create(CreateTask.Request(task_type="PKCI", **fields), CreateTask.Response())
    assert reply.success, reply.message
    return reply


def feeds_of(cluster):
    return {n.name: n.executor._feeds.active() for n in live(cluster)}


def test_node_bound_input_runs_on_its_node_and_streams_to_helpers(cluster):
    leader = wait_for(lambda: active_leader(cluster))
    results = Results()
    reply = create(leader, input="ros://edge-02/camera/image", mode="redundant", helper_count=1)
    assert reply.assigned_node == "edge-02"                    # data locality wins
    helper = next(n for n in reply.dispatched_nodes if n != "edge-02")
    task = cluster[helper].executor._tasks[reply.task_id]
    assert task.compressed and task.image_topic == "/edge_02/camera/image/compressed"
    owner = cluster["edge-02"]
    wait_for(lambda: (publish_frame(owner), {"edge-02", helper} <= results.nodes(reply.task_id))[1])
    assert cluster.airsim.fetchers() == []


def test_shared_input_each_participant_fetches_it(cluster):
    leader = wait_for(lambda: active_leader(cluster))
    results = Results()
    reply = create(leader, input="airsim://uav_5/0", mode="redundant", helper_count=1)
    wait_for(lambda: set(reply.dispatched_nodes) <= results.nodes(reply.task_id))
    assert cluster.airsim.fetchers() == sorted(reply.dispatched_nodes)
    assert leader.control._on_destroy(DestroyTask.Request(task_id=reply.task_id), DestroyTask.Response()).success
    wait_for(lambda: all(not feeds for feeds in feeds_of(cluster).values()))   # feeds stop with the task


@pytest.mark.airsim_mode("relay")
def test_shared_input_relay_mode_fetches_once(cluster):
    leader = wait_for(lambda: active_leader(cluster))
    results = Results()
    reply = create(leader, input="airsim://uav_5/0", mode="redundant", helper_count=1)
    wait_for(lambda: set(reply.dispatched_nodes) <= results.nodes(reply.task_id))
    assert cluster.airsim.fetchers() == [reply.assigned_node]


def test_moved_task_keeps_its_shared_input(cluster):
    leader = wait_for(lambda: active_leader(cluster))
    results = Results()
    reply = create(leader, input="airsim://uav_7/front")
    primary = cluster[reply.assigned_node]
    assert primary is not leader
    wait_for(lambda: primary.name in results.nodes(reply.task_id, epoch=1))
    kill(primary)
    record = wait_for(lambda: (lambda r: r if r and r.epoch == 2 and r.status == c.RUNNING else None)(
        leader.control.consensus.get(reply.task_id)), timeout=20.0)
    wait_for(lambda: record.primary in results.nodes(reply.task_id, epoch=2))
    assert ("uav_7", "front") in feeds_of(cluster)[record.primary]   # same camera on the new node


def test_node_bound_input_waits_while_its_node_is_down(cluster):
    leader = wait_for(lambda: active_leader(cluster))
    owner = next(n for n in live(cluster) if n is not leader)
    kill(owner)
    reply = create(leader, input="ros://%s/camera/image" % owner.name)
    assert reply.assigned_node == "" and "pending" in reply.message
    time.sleep(2.0)
    record = leader.control.consensus.get(reply.task_id)
    assert record.status == c.PENDING and runners(cluster, reply.task_id) == []


def test_image_conversions_keep_colour_order():
    import numpy as np
    from sensor_msgs.msg import CompressedImage
    from cccp_task_executor.images import compress, decompress, to_rgb
    red_bgr = Image(height=1, width=2, step=6, encoding="bgr8", data=[0, 0, 255] * 2)
    assert to_rgb(red_bgr)[0, 0].tolist() == [255, 0, 0]
    red_bgra = Image(height=1, width=1, step=4, encoding="bgra8", data=[0, 0, 255, 9])
    assert to_rgb(red_bgra)[0, 0].tolist() == [255, 0, 0]
    gray = Image(height=1, width=1, step=1, encoding="mono8", data=[7])
    assert to_rgb(gray)[0, 0].tolist() == [7, 7, 7]
    frame = Image(height=8, width=8, step=24, encoding="rgb8", data=[200, 30, 30] * 64)
    frame.header.frame_id = "cam"
    small = compress(frame, CompressedImage)
    assert small.format == "jpeg" and len(small.data) < 8 * 8 * 3 * 4
    back = decompress(small, Image)
    assert (back.encoding, back.width, back.header.frame_id) == ("rgb8", 8, "cam")
    assert np.abs(to_rgb(back).astype(int) - [200, 30, 30]).max() < 20      # lossy but same colour


def test_fake_messages_are_strict():
    from cccp_cluster_interfaces.msg import NodeStatus
    msg = NodeStatus()
    with pytest.raises(AttributeError):
        msg.no_such_field = 1
    with pytest.raises(AssertionError):
        msg.cpu_usage = 1                               # float field: int rejected, like rosidl
    msg.cpu_usage = 1.0
    assert NodeStatus.PROTOCOL_VERSION == 1
