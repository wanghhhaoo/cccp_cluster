"""Streaming algorithms (PKCI, PKCT) in one executor on fake_ros, commands sent by hand.

Checks what the executor does per frame: the result fields and frame numbering of each algorithm,
PKCT's tracker session (init on the first frame, isolated per generation, released on stop), how
failures end a task, readiness, and parameter checks. test_ros_integration.py covers the cluster.
"""
import json
import threading
import time
import uuid

import pytest

import fake_ros
from test_ros_integration import INSTALLED, wait_for

from cccp_node import names
from cccp_node.core import constants as c
from cccp_node.core.naming import ros_namespace
from cccp_node.core.task_spec import TaskSpec

if INSTALLED:
    from cccp_cluster_interfaces.msg import Command, ExecEvent, ExecutorStatus
    from cccp_task_interfaces.msg import TaskResult
    from collab_inference_interfaces.srv import Classify, InitTracker, ResetTracker, Track
    from sensor_msgs.msg import Image

    from cccp_node.ros import convert, qos
    from cccp_task_executor.executor_node import TaskExecutor
    from cccp_task_executor.algorithms import local_handlers
    from cccp_task_executor.inference import LocalBackend

pytestmark = pytest.mark.skipif(not INSTALLED, reason="real ROS present: run the colcon tests instead")

NODE = "edge-02"
BBOX = [1, 2, 3, 4]


class Engine:
    """A PKCI + PKCT engine that records its calls; fail_* make the next calls answer success=False."""

    def __init__(self):
        self.calls = []
        self.fail_classify = 0
        self.fail_init = False
        self._lock = threading.Lock()

    def _record(self, name, request):
        with self._lock:
            self.calls.append((name, request))

    def classify(self, request, response):
        self._record("classify", request)
        if self.fail_classify:
            self.fail_classify -= 1
            response.success, response.message = False, "classifier error"
            return response
        response.success, response.label, response.class_name = True, 3, "car"
        response.score, response.confidence, response.latency_ms = 0.9, 0.7, 1.5
        response.log_probs, response.increment = [0.5, -0.5], b"inc"
        return response

    def init_tracker(self, request, response):
        self._record("init", request)
        response.success = not self.fail_init
        response.message = "bad first frame" if self.fail_init else ""
        return response

    def track(self, request, response):
        self._record("track", request)
        response.success, response.x, response.y, response.width, response.height = True, 5.0, 6.0, 7.0, 8.0
        response.confidence, response.latency_ms, response.identity = 0.8, 2.5, b"id"
        return response

    def reset(self, request, response):
        self._record("reset", request)
        response.success = True
        return response

    def named(self, name):
        with self._lock:
            return [request for call, request in self.calls if call == name]


def local_backend(pkci=None, pkct=None):
    """The data_plane default: engines called in this process."""
    engines = {name: engine for name, engine in (("PKCI", pkci), ("PKCT", pkct)) if engine is not None}
    return LocalBackend(local_handlers(**engines))


class Probe:
    def __init__(self):
        self.node = fake_ros.Node("probe")
        self.events, self.results, self.statuses = [], [], []
        self.node.create_subscription(ExecEvent, names.EXEC_EVENT_TOPIC, self.events.append, qos.EXEC_EVENT)
        self.node.create_subscription(TaskResult, names.TASK_RESULT_TOPIC, self.results.append, qos.TASK_RESULT)
        self.node.create_subscription(ExecutorStatus, names.EXEC_STATUS_TOPIC, self.statuses.append,
                                      qos.EXECUTOR_STATUS)
        self.camera = fake_ros.Publisher(self.node, Image, "%s/camera/image" % ros_namespace(NODE))

    def frame(self, stamp=0):
        image = Image(height=2, width=2, step=6, encoding="rgb8", data=[0] * 12)
        image.header.stamp.sec = stamp
        self.camera.publish(image)

    def last_event(self):
        event = self.events[-1]
        return event.kind, event.error_code


def make_executor(backend, **params):
    fake_ros.BUS.reset()
    fake_ros.BUS.namespace = ros_namespace(NODE)
    fake_ros.BUS.params = dict(node_name=NODE, status_period=60.0, input_timeout=0.0, service_wait_timeout=0.1,
                               **params)
    return TaskExecutor(backend=backend)


@pytest.fixture
def engine():
    return Engine()


@pytest.fixture
def run(engine):
    """(executor, probe) with both engines loaded in-process."""
    executor = make_executor(local_backend(engine, engine), max_consecutive_failures=3)
    probe = Probe()
    yield executor, probe
    executor.destroy_node()


def start(executor, task_type, params=None, task_id=None, epoch=1, assign_term=1):
    task_id = task_id or str(uuid.uuid4())
    spec = TaskSpec(task_type=task_type, params_json="" if params is None else json.dumps(params))
    executor._on_command(Command(
        op=Command.OP_START, term=1, targets=[2], task_id=convert.uuid_bytes(task_id), epoch=epoch,
        assign_term=assign_term, primary_node=2, primary_name=NODE, idempotency_key="k-" + task_id, name="t",
        spec_json=spec.to_json()))
    return task_id


def stop(executor, task_id, epoch=1, assign_term=1):
    executor._on_command(Command(op=Command.OP_STOP, term=1, targets=[2], task_id=convert.uuid_bytes(task_id),
                                 epoch=epoch, assign_term=assign_term, stop_reason=Command.STOP_STALE,
                                 reason="test"))


def frames_until(probe, count, timeout=5.0):
    """Publishes frames (the executor skips those arriving while busy) until count results are in."""
    deadline = time.monotonic() + timeout
    while len(probe.results) < count:
        assert time.monotonic() < deadline, "only %d results" % len(probe.results)
        probe.frame()
        time.sleep(0.02)
    return probe.results


# ------------------------------------------------------------------ PKCI

def test_pkci_publishes_each_classification(run, engine):
    executor, probe = run
    task_id = start(executor, "PKCI")
    assert probe.last_event() == (ExecEvent.KIND_RUNNING, ExecEvent.ERR_NONE)
    first, second = frames_until(probe, 2)[:2]
    assert (first.task_id, first.epoch, first.assign_term, first.node_name, first.task_type) == (
        task_id, 1, 1, NODE, "PKCI")
    assert (first.success, first.valid, first.message, first.latency_ms) == (True, True, "ok", 1.5)
    assert (first.label, first.class_name, first.score) == (3, "car", pytest.approx(0.9))
    assert first.confidence == pytest.approx(0.7) and list(first.log_probs) == [0.5, -0.5]
    assert bytes(first.payload) == b"inc"
    assert (first.frame_id, second.frame_id) == (1, 2)
    request = engine.named("classify")[0]
    assert (request.frame_id, request.level, request.drone_id) == (1, "full", 0)


def test_pkci_fails_after_consecutive_failures_only(run, engine):
    executor, probe = run
    engine.fail_classify = 2                      # below max_consecutive_failures (3): recovered
    task_id = start(executor, "PKCI")
    frames_until(probe, 1)
    assert task_id in executor._tasks
    engine.fail_classify = 3
    deadline = time.monotonic() + 5.0
    while task_id in executor._tasks:
        assert time.monotonic() < deadline
        probe.frame()
        time.sleep(0.02)
    assert probe.last_event() == (ExecEvent.KIND_FAILED, ExecEvent.ERR_INFERENCE_ERROR)
    assert probe.events[-1].message == "classifier error"


# ------------------------------------------------------------------ PKCT

def test_pkct_initializes_on_the_first_frame_then_tracks(run, engine):
    executor, probe = run
    task_id = start(executor, "PKCT", {"bbox": BBOX}, epoch=2, assign_term=5)
    init, track = frames_until(probe, 2)[:2]
    assert (init.message, init.frame_id, init.confidence) == ("tracker initialized", 0, 1.0)
    assert [init.x, init.y, init.width, init.height] == [1.0, 2.0, 3.0, 4.0]
    assert (track.message, track.frame_id, track.latency_ms) == ("ok", 1, 2.5)
    assert [track.x, track.y, track.width, track.height, track.confidence] == [5.0, 6.0, 7.0, 8.0, pytest.approx(0.8)]
    assert bytes(track.payload) == b"id" and track.task_type == "PKCT"
    [request] = engine.named("init")
    tracker = "task-%s-2-5" % task_id                # one tracker session per task and generation
    assert (request.tracker_id, request.x, request.y, request.width, request.height) == (tracker, 1, 2, 3, 4)
    assert {r.tracker_id for r in engine.named("track")} == {tracker}

    stop(executor, task_id, epoch=2, assign_term=5)
    assert probe.last_event() == (ExecEvent.KIND_STOPPED, ExecEvent.ERR_NONE)
    wait_for(lambda: [r.tracker_id for r in engine.named("reset")] == [tracker], timeout=5.0)


def test_pkct_init_failure_fails_the_task_at_once(run, engine):
    executor, probe = run
    engine.fail_init = True
    task_id = start(executor, "PKCT", {"bbox": BBOX})
    probe.frame()
    wait_for(lambda: task_id not in executor._tasks, timeout=5.0)
    assert probe.last_event() == (ExecEvent.KIND_FAILED, ExecEvent.ERR_INFERENCE_ERROR)
    assert probe.events[-1].message == "tracker init failed: bad first frame"
    assert probe.results == []


@pytest.mark.parametrize("params", [None, {}, {"bbox": [0, 0, -1, 5]}, {"bbox": [1, 2, 3]}])
def test_pkct_without_a_valid_bbox_is_refused(run, params):
    executor, probe = run
    task_id = start(executor, "PKCT", params)
    assert probe.last_event() == (ExecEvent.KIND_REJECTED, ExecEvent.ERR_INVALID_INPUT)
    assert task_id not in executor._tasks


# ------------------------------------------------------------------ readiness

def test_only_loaded_algorithms_are_ready_and_others_are_refused_as_not_ready(engine):
    executor = make_executor(local_backend(pkci=engine))
    probe = Probe()
    try:
        executor._publish_status()
        assert convert.algorithms_from_mask(probe.statuses[-1].algorithms_ready) == ("PKCI",)
        assert probe.statuses[-1].inference_ready
        start(executor, "PKCT", {"bbox": BBOX})
        assert probe.last_event() == (ExecEvent.KIND_REJECTED, ExecEvent.ERR_NOT_READY)
        start(executor, "PKCI")
        assert probe.last_event() == (ExecEvent.KIND_RUNNING, ExecEvent.ERR_NONE)
    finally:
        executor.destroy_node()


def test_with_ros_services_an_algorithm_is_ready_once_its_services_are(engine):
    executor = make_executor(None)                    # default backend: <ns>/algorithms/* services
    probe = Probe()
    try:
        executor._publish_status()
        assert convert.algorithms_from_mask(probe.statuses[-1].algorithms_ready) == ()
        start(executor, "PKCI")
        assert probe.last_event() == (ExecEvent.KIND_REJECTED, ExecEvent.ERR_NOT_READY)
        assert "algorithms/pkci/classify" in probe.events[-1].message
        server = fake_ros.Node("pkct_server")
        server.create_service(InitTracker, "algorithms/pkct/init", engine.init_tracker)
        server.create_service(Track, "algorithms/pkct/track", engine.track)
        executor._publish_status()
        assert convert.algorithms_from_mask(probe.statuses[-1].algorithms_ready) == ("PKCT",)
        server.create_service(Classify, "algorithms/pkci/classify", engine.classify)
        server.create_service(ResetTracker, "algorithms/pkct/reset", engine.reset)
        executor._publish_status()
        assert convert.algorithms_from_mask(probe.statuses[-1].algorithms_ready) == ("PKCI", "PKCT")
        task_id = start(executor, "PKCT", {"bbox": BBOX})
        frames_until(probe, 2)
        stop(executor, task_id)
        wait_for(lambda: engine.named("reset"), timeout=5.0)
    finally:
        executor.destroy_node()


def test_unknown_task_type_is_refused_as_invalid(run):
    executor, probe = run
    start(executor, "YOLO")
    assert probe.last_event() == (ExecEvent.KIND_REJECTED, ExecEvent.ERR_INVALID_INPUT)
