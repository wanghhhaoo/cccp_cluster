import threading
import time

from _sim import FakeElection, SimWorld

from cccp_node.control import protocol as p
from cccp_node.control.controller import ControllerConfig, LeaderController
from cccp_node.control.gateway import Gateway, GatewayConfig
from cccp_node.control.protocol import Reply
from cccp_node.control.task_store import TaskStore
from cccp_node.core import constants as c
from cccp_node.core.reconciler import ReconcileConfig
from cccp_node.core.task_spec import TaskSpec

FAST = GatewayConfig(request_timeout_s=0.2)


class Leader:
    """Stands in for a controller or the network: records requests, answers them on another thread."""

    def __init__(self, answer=None):
        self.requests = []
        self.answer = answer          # request -> Reply | None (None: never answers)
        self.gateway = None

    def __call__(self, request):
        self.requests.append(request)
        reply = self.answer(request) if self.answer else None
        if reply is not None:
            threading.Timer(0.01, self.gateway.on_reply, args=(reply,)).start()

    send = __call__                   # also usable as a RequestTransport


def answer(request, **fields):
    return Reply(request.origin_node, request.origin_incarnation, request.request_id, request.target_leader, 1,
                 **fields)


def gateway(name="n3", active=False, leader="n1", local=None, transport=None, config=FAST):
    gw = Gateway(name, lambda: active, lambda: leader, local or Leader(), transport, config, incarnation=5)
    for side in (local, transport):
        if side is not None:
            side.gateway = gw
    return gw


def test_invalid_spec_is_rejected_before_anything_is_sent():
    transport = Leader()
    reply = gateway(transport=transport).create(TaskSpec(task_type="YOLO"))
    assert not reply.success and reply.error_code == c.INVALID_INPUT and transport.requests == []


def test_the_ready_leader_asks_its_own_controller():
    local = Leader(lambda r: answer(r, task_id="t1", status=c.RUNNING, primary="n2", helpers=("n4",)))
    reply = gateway("n1", active=True, local=local).create(TaskSpec(task_type="pkci"), "manual:ue:1")
    [request] = local.requests
    assert (request.op, request.target_leader, request.idempotency_key, request.spec.task_type) == (
        p.CREATE, "n1", "manual:ue:1", "PKCI")
    assert reply.success and reply.assigned_node == "n2" and reply.dispatched_nodes == ("n2", "n4")


def test_other_nodes_send_a_request_naming_the_leader():
    transport = Leader(lambda r: answer(r, error_code=c.TIMEOUT, message="slow", task_id="t9", status=c.PENDING))
    reply = gateway("n3", transport=transport).create(TaskSpec(task_type="PKCI"))
    [request] = transport.requests
    assert (request.origin_node, request.origin_incarnation, request.target_leader) == ("n3", 5, "n1")
    assert request.idempotency_key.startswith("anon:")
    assert (reply.success, reply.error_code, reply.task_id) == (False, c.TIMEOUT, "t9")


def test_destroy_and_query_replies():
    transport = Leader(lambda r: answer(r, status=c.CANCELLED) if r.op == p.DESTROY
                       else answer(r, error_code=c.NOT_FOUND, message="no such task"))
    gw = gateway(transport=transport)
    assert gw.destroy("t1", "done").final_status == c.CANCELLED
    assert gw.query("t2").error_code == c.NOT_FOUND
    assert [(r.op, r.task_id, r.reason) for r in transport.requests] == [(p.DESTROY, "t1", "done"),
                                                                          (p.QUERY, "t2", "")]


def test_no_leader_or_warming_leader_asks_to_retry_without_sending():
    transport = Leader()
    assert gateway(leader="", transport=transport).create(TaskSpec(task_type="PKCI")).error_code == c.NO_LEADER
    warming = gateway("n1", leader="n1", transport=transport).query("t1")
    assert warming.error_code == c.NO_LEADER and "collecting" in warming.message
    assert transport.requests == []


def test_a_leader_that_stepped_down_is_answered_at_once():
    transport = Leader(lambda r: answer(r, error_code=c.NOT_LEADER, message="n1 is not the cluster leader"))
    started = time.monotonic()
    reply = gateway(transport=transport, config=GatewayConfig(request_timeout_s=5.0)).query("t1")
    assert reply.error_code == c.NOT_LEADER and time.monotonic() - started < 1.0


def test_no_reply_is_a_timeout_to_retry():
    reply = gateway(transport=Leader()).create(TaskSpec(task_type="PKCI"), "k")
    assert reply.error_code == c.TIMEOUT and "same idempotency key" in reply.message


def test_replies_for_other_gateways_or_an_earlier_incarnation_are_ignored():
    def wrong(request):
        return Reply("n4", request.origin_incarnation, request.request_id, "n1", 1)

    transport = Leader(wrong)
    assert gateway(transport=transport).query("t1").error_code == c.TIMEOUT
    transport.answer = lambda r: Reply(r.origin_node, 4, r.request_id, "n1", 1)
    assert gateway(transport=transport).query("t1").error_code == c.TIMEOUT


def test_forwarded_calls_go_to_the_local_controller_whatever_the_election_says():
    local, transport = Leader(lambda r: answer(r, error_code=c.NOT_LEADER)), Leader()
    gw = gateway("n3", leader="n1", local=local, transport=transport)
    assert gw.serve_create(TaskSpec(task_type="PKCI"), "k").error_code == c.NOT_LEADER
    assert gw.serve_destroy("t1").error_code == c.NOT_LEADER
    assert gw.serve_query("t1").error_code == c.NOT_LEADER
    assert [r.target_leader for r in local.requests] == ["n3"] * 3 and transport.requests == []


def test_end_to_end_create_and_destroy_through_the_controller():
    world = SimWorld(["n1", "n2"], clock=time.monotonic, offline_timeout_s=1.0)
    config = ControllerConfig(warmup_s=0.2, reconcile_period_s=0.02, reconcile=ReconcileConfig(dispatch_retry_s=0.2))
    gw = None
    controller = LeaderController("n1", TaskStore(), FakeElection("n1", 1), world.cluster, world.network, config,
                                  reply=lambda reply: gw.on_reply(reply))
    gw = Gateway("n1", lambda: controller.active, lambda: "n1", controller.on_request,
                 config=GatewayConfig(request_timeout_s=5.0))
    stop = threading.Event()

    def nodes_publish():
        while not stop.is_set():
            world.publish(controller)
            stop.wait(0.02)

    threads = [threading.Thread(target=nodes_publish), threading.Thread(target=controller.run, args=(stop, 0.01))]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while not controller.active and time.monotonic() < deadline:
            time.sleep(0.02)
        reply = gw.create(TaskSpec(task_type="PKCT", mode="redundant", helper_count=1,
                                   params_json='{"bbox": [1, 2, 3, 4]}'))
        assert reply.success, reply
        assert set(reply.dispatched_nodes) == {"n1", "n2"} and len(reply.task_id) == 36   # a UUID
        destroyed = gw.destroy(reply.task_id)
        assert destroyed.success and destroyed.final_status == c.CANCELLED
        assert not any(n.guard.running() for n in world.nodes.values())      # stopped before the reply
    finally:
        stop.set()
        for thread in threads:
            thread.join()
