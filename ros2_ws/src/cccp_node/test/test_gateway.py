import threading
import time

from _sim import SimWorld

from cccp_node.consensus.local import LocalConsensus
from cccp_node.control.controller import ControllerConfig, LeaderController
from cccp_node.control.gateway import Gateway, GatewayConfig
from cccp_node.core import constants as c
from cccp_node.core.reconciler import ReconcileConfig
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import ApplyResult

FAST_WAIT = GatewayConfig(create_wait_timeout_s=0.05)


def test_invalid_spec_is_rejected_before_submission():
    consensus = LocalConsensus()
    reply = Gateway(consensus).create(TaskSpec(task_type="YOLO"))
    assert not reply.success and reply.error_code == c.INVALID_INPUT and reply.task_id == 0
    assert consensus.records() == {}


def test_create_without_runner_returns_pending_task_after_wait():
    gateway = Gateway(LocalConsensus(), FAST_WAIT, new_uuid=lambda: "u1")
    reply = gateway.create(TaskSpec(task_type="pkci"))
    assert reply.success and reply.task_id == 1 and "pending" in reply.message
    assert gateway.query(1).idempotency_key == "anon:u1"
    assert gateway.query(1).spec.task_type == "PKCI"


def test_same_key_returns_same_task():
    gateway = Gateway(LocalConsensus(), FAST_WAIT)
    first = gateway.create(TaskSpec(task_type="PKCI"), "manual:ue:1")
    again = gateway.create(TaskSpec(task_type="PKCI"), "manual:ue:1")
    assert again.task_id == first.task_id and again.duplicate and not first.duplicate


def test_destroy_semantics():
    gateway = Gateway(LocalConsensus(), FAST_WAIT)
    task_id = gateway.create(TaskSpec(task_type="PKCI")).task_id
    assert gateway.destroy(task_id, "done").final_status == c.CANCELLED
    again = gateway.destroy(task_id)
    assert not again.success and again.error_code == c.INVALID_STATE and again.final_status == c.CANCELLED
    assert gateway.destroy(99).error_code == c.NOT_FOUND


def test_consensus_errors_are_passed_through():
    consensus = LocalConsensus()
    consensus.submit = lambda command, timeout: ApplyResult(False, c.NO_LEADER, "no leader")
    reply = Gateway(consensus).create(TaskSpec(task_type="PKCI"))
    assert (reply.success, reply.error_code, reply.task_id) == (False, c.NO_LEADER, 0)


def test_end_to_end_create_returns_once_running():
    world = SimWorld(["n1", "n2"], clock=time.monotonic, offline_timeout_s=1.0)
    consensus = LocalConsensus("n1")
    config = ControllerConfig(warmup_s=0.0, reconcile_period_s=0.02,
                              reconcile=ReconcileConfig(dispatch_retry_s=0.2))
    controller = LeaderController("n1", consensus, world.cluster, world.dispatcher, config)
    stop = threading.Event()

    def nodes_publish():
        while not stop.is_set():
            world.publish(controller)
            stop.wait(0.02)

    threads = [threading.Thread(target=nodes_publish), threading.Thread(target=controller.run, args=(stop, 0.01))]
    for thread in threads:
        thread.start()
    try:
        gateway = Gateway(consensus, GatewayConfig(create_wait_timeout_s=5.0))
        reply = gateway.create(TaskSpec(task_type="PKCT", mode="redundant", helper_count=1,
                                        params_json='{"bbox": [1, 2, 3, 4]}'))
        assert reply.success, reply
        assert reply.assigned_node in ("n1", "n2") and set(reply.dispatched_nodes) == {"n1", "n2"}
        assert gateway.query(reply.task_id).status == c.RUNNING
        assert gateway.destroy(reply.task_id).success
        deadline = time.monotonic() + 5.0
        while any(n.guard.running() for n in world.nodes.values()) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not any(n.guard.running() for n in world.nodes.values())
    finally:
        stop.set()
        for thread in threads:
            thread.join()
