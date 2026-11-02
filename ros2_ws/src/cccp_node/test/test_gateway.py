import threading
import time

from _sim import FakeElection, SimWorld

from cccp_node.control.controller import ControllerConfig, LeaderController
from cccp_node.control.gateway import CreateReply, DestroyReply, Gateway, GatewayConfig, QueryReply
from cccp_node.control.task_store import TaskStore
from cccp_node.core import constants as c
from cccp_node.core.reconciler import ReconcileConfig
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import Assign, ReportStatus

FAST_WAIT = GatewayConfig(create_wait_timeout_s=0.05)


def leader_gateway(store=None, config=FAST_WAIT, active=True, **kw):
    store = store or TaskStore()
    ids = iter("t%d" % i for i in range(1, 100))
    return Gateway("n1", store, lambda: active, lambda: "n1", config=config, new_uuid=lambda: next(ids), **kw), store


class RecordingForwarder:
    def __init__(self):
        self.calls = []

    def create(self, leader, spec, key):
        self.calls.append(("create", leader, spec.task_type, key))
        return CreateReply(True, task_id="remote")

    def destroy(self, leader, task_id, reason):
        self.calls.append(("destroy", leader, task_id))
        return DestroyReply(True, final_status=c.CANCELLED)

    def query(self, leader, task_id):
        self.calls.append(("query", leader, task_id))
        return QueryReply(False, c.NOT_FOUND, "no")


def test_invalid_spec_is_rejected_before_anything_else():
    gateway, store = leader_gateway()
    reply = gateway.create(TaskSpec(task_type="YOLO"))
    assert not reply.success and reply.error_code == c.INVALID_INPUT and reply.task_id == ""
    assert store.records() == {}


def test_task_that_does_not_start_in_time_is_a_failure_to_retry():
    gateway, store = leader_gateway()
    reply = gateway.create(TaskSpec(task_type="pkci"), "manual:ue:1")
    assert not reply.success and reply.error_code == c.TIMEOUT and reply.task_id == "t1"
    assert "same idempotency key" in reply.message
    again = gateway.create(TaskSpec(task_type="PKCI"), "manual:ue:1")
    assert again.duplicate and again.task_id == "t1"
    assert store.get("t1").spec.task_type == "PKCI"


def test_running_task_is_a_success():
    store = TaskStore()

    def start_immediately(events):
        for event in events:
            if event.status == c.PENDING:
                store.apply(Assign(event.task_id, 0, "n2"))
                store.apply(ReportStatus("n2", event.task_id, 1, c.REPORT_RUNNING))

    store.add_event_listener(start_immediately)
    gateway, _ = leader_gateway(store)
    reply = gateway.create(TaskSpec(task_type="PKCI"))
    assert reply.success and reply.assigned_node == "n2" and reply.dispatched_nodes == ("n2",)


def test_destroy_and_query_on_the_leader():
    gateway, store = leader_gateway()
    task_id = gateway.create(TaskSpec(task_type="PKCI")).task_id
    assert gateway.query(task_id).record.status == c.PENDING
    assert gateway.destroy(task_id, "done").final_status == c.CANCELLED
    again = gateway.destroy(task_id)
    assert again.error_code == c.INVALID_STATE and again.final_status == c.CANCELLED
    assert gateway.destroy("nope").error_code == c.NOT_FOUND
    assert gateway.query("nope").error_code == c.NOT_FOUND


def test_other_nodes_forward_to_the_leader():
    forwarder = RecordingForwarder()
    gateway = Gateway("n3", TaskStore(), lambda: False, lambda: "n1", forwarder)
    assert gateway.create(TaskSpec(task_type="pkci"), "k").task_id == "remote"
    gateway.destroy("t9")
    gateway.query("t9")
    assert forwarder.calls == [("create", "n1", "PKCI", "k"), ("destroy", "n1", "t9"), ("query", "n1", "t9")]


def test_no_leader_or_warming_leader_asks_to_retry():
    nobody = Gateway("n3", TaskStore(), lambda: False, lambda: "", RecordingForwarder())
    assert nobody.create(TaskSpec(task_type="PKCI")).error_code == c.NO_LEADER
    warming = Gateway("n1", TaskStore(), lambda: False, lambda: "n1", RecordingForwarder())
    reply = warming.query("t1")
    assert reply.error_code == c.NO_LEADER and "collecting" in reply.message


def test_leader_only_handlers_never_forward():
    forwarder = RecordingForwarder()
    follower = Gateway("n3", TaskStore(), lambda: False, lambda: "n1", forwarder)
    assert follower.create_local(TaskSpec(task_type="PKCI"), "k").error_code == c.NO_LEADER
    assert follower.destroy_local("t1").error_code == c.NO_LEADER
    assert follower.query_local("t1").error_code == c.NO_LEADER
    assert forwarder.calls == []


def test_end_to_end_create_returns_once_running():
    world = SimWorld(["n1", "n2"], clock=time.monotonic, offline_timeout_s=1.0)
    store = TaskStore()
    config = ControllerConfig(warmup_s=0.2, reconcile_period_s=0.02, reconcile=ReconcileConfig(dispatch_retry_s=0.2))
    controller = LeaderController("n1", store, FakeElection("n1", 1), world.cluster, world.dispatcher, config)
    stop = threading.Event()

    def nodes_publish():
        while not stop.is_set():
            world.publish(controller)
            stop.wait(0.02)

    threads = [threading.Thread(target=nodes_publish), threading.Thread(target=controller.run, args=(stop, 0.01))]
    for thread in threads:
        thread.start()
    try:
        gateway = Gateway("n1", store, lambda: controller.active, lambda: "n1",
                          config=GatewayConfig(create_wait_timeout_s=5.0))
        deadline = time.monotonic() + 5.0
        while not controller.active and time.monotonic() < deadline:
            time.sleep(0.02)
        reply = gateway.create(TaskSpec(task_type="PKCT", mode="redundant", helper_count=1,
                                        params_json='{"bbox": [1, 2, 3, 4]}'))
        assert reply.success, reply
        assert set(reply.dispatched_nodes) == {"n1", "n2"} and len(reply.task_id) == 36   # a UUID
        assert gateway.destroy(reply.task_id).success
        while any(n.guard.running() for n in world.nodes.values()) and time.monotonic() < deadline + 5:
            time.sleep(0.02)
        assert not any(n.guard.running() for n in world.nodes.values())
    finally:
        stop.set()
        for thread in threads:
            thread.join()
