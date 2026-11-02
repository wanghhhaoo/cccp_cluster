import uuid

from _sim import Clock, FakeElection, SimWorld

from cccp_node.control import protocol as p
from cccp_node.control.controller import ControllerConfig, LeaderController
from cccp_node.control.protocol import Request
from cccp_node.control.task_store import TaskStore
from cccp_node.core import constants as c
from cccp_node.core.scheduler import SchedulerConfig
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import Cancel, Submit

CONFIG = ControllerConfig(scheduler=SchedulerConfig(leader_self_penalty=0.0))


def setup(names=("n1", "n2", "n3"), leader="n1", config=CONFIG):
    clock = Clock()
    world = SimWorld(names, clock)
    store = TaskStore()
    election = FakeElection(leader, 1, me="n1")
    controller = LeaderController("n1", store, election, world.cluster, world.network, config, clock=clock)
    return clock, world, store, election, controller


def warm(world, controller):
    world.run(controller, 5.5)
    assert controller.active


def submit(store, key="k", **kw):
    kw.setdefault("task_type", "PKCI")
    return store.apply(Submit(str(uuid.uuid4()), key, TaskSpec(**kw))).task_id


def running_copies(world, task_id):
    return [n.name for n in world.nodes.values() if task_id in n.guard.running()]


def test_new_term_starts_empty_and_acts_after_warmup():
    clock, world, store, election, controller = setup()
    stale = submit(store, "stale")                   # left over from an earlier term
    world.run(controller, 4.5)
    assert not controller.active and store.get(stale) is None
    world.run(controller, 1.0)
    assert controller.active and controller.term == 1


def test_followers_do_nothing():
    clock, world, store, election, controller = setup(leader="n2")
    world.run(controller, 20.0)
    assert world.network.starts == [] and not controller.active


def test_schedules_starts_and_records_running():
    clock, world, store, election, controller = setup()
    world.nodes["n2"].view["cpu_usage"] = 5.0
    warm(world, controller)
    task_id = submit(store)
    world.run(controller, 1.5)
    record = store.get(task_id)
    assert (record.status, record.primary, record.epoch) == (c.RUNNING, "n2", 1)
    assert world.network.starts == [("n2", task_id, (1, 1), 1, c.ROLE_PRIMARY)]


def test_collaborative_task_starts_primary_and_helpers():
    clock, world, store, election, controller = setup()
    warm(world, controller)
    task_id = submit(store, mode="fusion", helper_count=2)
    world.run(controller, 1.5)
    assert store.get(task_id).status == c.RUNNING and len(store.get(task_id).nodes) == 3
    assert sorted(role for *_, role in world.network.starts) == [c.ROLE_HELPER, c.ROLE_HELPER, c.ROLE_PRIMARY]


def test_primary_failure_moves_task_and_old_copy_is_stopped():
    clock, world, store, election, controller = setup()
    warm(world, controller)
    task_id = submit(store)
    world.run(controller, 1.5)
    first = store.get(task_id).primary
    world.nodes[first].alive = False
    world.run(controller, 8.0)
    record = store.get(task_id)
    assert record.status == c.RUNNING and record.epoch == 2 and record.primary != first
    world.nodes[first].alive = True                  # comes back still running epoch 1
    world.run(controller, 2.0)
    assert (first, task_id, (1, 1), "(1,1) superseded by (2,1)") in world.network.stops
    assert running_copies(world, task_id) == [record.primary]


def test_transient_start_failure_requeues_and_avoids_the_node():
    clock, world, store, election, controller = setup()
    world.nodes["n1"].view["cpu_usage"] = 0.0
    world.nodes["n1"].fail_start = c.BUSY
    warm(world, controller)
    task_id = submit(store)
    world.run(controller, 2.5)
    record = store.get(task_id)
    assert record.status == c.RUNNING and record.primary != "n1" and record.epoch == 3


def test_permanent_start_failure_fails_the_task():
    clock, world, store, election, controller = setup()
    for node in world.nodes.values():
        node.fail_start = c.INVALID_INPUT
    warm(world, controller)
    task_id = submit(store)
    world.run(controller, 1.5)
    assert store.get(task_id).error_code == c.DISPATCH_FAILED


def test_no_feasible_node_fails_quickly():
    clock, world, store, election, controller = setup()
    warm(world, controller)
    for node in world.nodes.values():
        node.view["inference_ready"] = False
    world.publish()
    task_id = submit(store)
    world.run(controller, 6.0)
    assert store.get(task_id).status == c.PENDING
    world.run(controller, 3.0)                       # past the 8 s pending timeout
    assert store.get(task_id).error_code == c.NO_FEASIBLE_NODE


def stall(world, store, task_id):
    """The task's primary got no input frames: it reports NOT_READY and drops the task."""
    record = store.get(task_id)
    sim = world.nodes[record.primary]
    sim.report(task_id, record.generation, p.FAILED, c.NOT_READY)
    sim.guard.stopped(task_id)
    return record.primary


def test_input_stall_moves_the_task_to_another_node():
    clock, world, store, election, controller = setup()
    warm(world, controller)
    task_id = submit(store)
    world.run(controller, 1.5)
    stalled = stall(world, store, task_id)
    world.run(controller, 1.5)
    record = store.get(task_id)
    assert (record.status, record.epoch) == (c.RUNNING, 3) and record.primary != stalled   # requeue + assign


def test_input_stalling_on_every_node_fails_the_task():
    clock, world, store, election, controller = setup()
    warm(world, controller)
    task_id = submit(store)
    world.run(controller, 1.5)
    primaries = []
    for _ in range(CONFIG.max_input_stalls):
        primaries.append(stall(world, store, task_id))
        world.run(controller, 1.5)
    record = store.get(task_id)
    assert (record.status, record.error_code) == (c.FAILED, c.NOT_READY)
    assert len(set(primaries)) == CONFIG.max_input_stalls          # each retry avoided the stalled nodes


def test_input_stall_of_a_pinned_task_fails_it():
    clock, world, store, election, controller = setup()
    warm(world, controller)
    task_id = submit(store, request_node="n2")
    world.run(controller, 1.5)
    stall(world, store, task_id)
    world.run(controller, 1.5)
    assert (store.get(task_id).status, store.get(task_id).error_code) == (c.FAILED, c.NOT_READY)


def test_new_leader_rebuilds_its_table_from_executor_reports():
    clock, world, store, election, old = setup()
    warm(world, old)
    ids = [submit(store, "k%d" % i, mode="redundant", helper_count=1) for i in range(3)]
    world.run(old, 1.5)
    starts_before = len(world.network.starts)
    election.set("n2", 2)                            # n2 wins the next election...
    new_store = TaskStore()                          # ...with its own, empty table
    new = LeaderController("n2", new_store, FakeElection("n2", 2, me="n2"), world.cluster, world.network,
                           CONFIG, clock=clock)
    world.run(new, 7.0)
    assert new.active and world.network.stops == [] and len(world.network.starts) == starts_before
    for task_id in ids:
        old_record, rebuilt = store.get(task_id), new_store.get(task_id)
        assert (rebuilt.status, rebuilt.epoch, rebuilt.primary, rebuilt.helpers) == (
            c.RUNNING, old_record.epoch, old_record.primary, old_record.helpers)
        assert rebuilt.idempotency_key == old_record.idempotency_key and rebuilt.spec == old_record.spec
    assert new_store.apply(Submit("t-retry", "k1", TaskSpec(task_type="PKCI"))).duplicate   # retries dedupe


def test_two_leaders_accepting_the_same_request_converge_to_one_copy():
    clock, world, store_a, election, leader_a = setup()
    store_b = TaskStore()
    leader_b = LeaderController("n2", store_b, FakeElection("n2", 2, me="n2"), world.cluster, world.network,
                                CONFIG, clock=clock)
    for _ in range(11):                              # both believe they lead (a split)
        clock.advance(0.5)
        world.publish(leader_a)
        leader_a.step()
        leader_b.step()
    a = submit(store_a, "same-request")
    b = submit(store_b, "same-request")              # the client retried on the other side
    for _ in range(6):                               # still split: both keep acting
        clock.advance(0.5)
        world.publish(leader_a)
        leader_a.step()
        leader_b.step()
    # B (higher term) stops A's copy as a duplicate; A's attempt to stop B's copy is fenced by the
    # executors, which have seen term 2. The duplicate is gone while the split still lasts.
    assert [t for t in (a, b) if running_copies(world, t)] == [b]
    assert any(reason == "duplicate of task %s" % b for *_, reason in world.network.stops)
    election.set("n2", 2)                            # the split heals
    world.run(leader_b, 4.0)
    assert [t for t in (a, b) if running_copies(world, t)] == [b]


def test_deposed_leader_is_fenced_by_executors():
    clock, world, store, election, controller = setup()
    for node in world.nodes.values():
        node.guard.on_start(5, "other", (1, 5))      # executors already saw term 5
    warm(world, controller)                          # this controller only has term 1
    task_id = submit(store)
    world.run(controller, 3.0)
    assert all(not n.guard.running().get(task_id) for n in world.nodes.values())


def test_cancelled_and_failed_tasks_end_on_their_nodes():
    clock, world, store, election, controller = setup()
    warm(world, controller)
    cancelled = submit(store, "a")
    failed = submit(store, "b")
    world.run(controller, 1.5)
    store.apply(Cancel(cancelled, "user"))
    sim = world.nodes[store.get(failed).primary]
    sim.report(failed, store.get(failed).generation, p.FAILED, c.INFERENCE_ERROR)
    sim.guard.stopped(failed)
    world.run(controller, 2.0)
    assert running_copies(world, cancelled) == [] and store.get(failed).status == c.FAILED


def test_lost_running_report_is_recovered_from_node_status():
    clock, world, store, election, controller = setup()
    warm(world, controller)
    task_id = submit(store)
    clock.advance(0.5)
    controller.step()                                # dispatched now
    for node in world.nodes.values():
        node.events.clear()                          # the Running event is lost
    world.run(controller, 3.0)
    assert store.get(task_id).status == c.RUNNING and len(world.network.starts) == 1


def test_terminal_tasks_are_pruned_eventually():
    config = ControllerConfig(scheduler=SchedulerConfig(leader_self_penalty=0.0), prune_terminal_after_s=30.0)
    clock, world, store, election, controller = setup(config=config)
    warm(world, controller)
    task_id = submit(store)
    world.run(controller, 1.5)
    store.apply(Cancel(task_id))
    world.run(controller, 25.0)
    assert store.get(task_id) is not None
    world.run(controller, 10.0)
    assert store.get(task_id) is None


# ------------------------------------------------------------- requests (0.3)

class Replies(list):
    def __call__(self, reply):
        self.append(reply)


def request(op, request_id=1, target="n1", **fields):
    return Request("gw", 7, request_id, target, 1, op, **fields)


def create(request_id=1, key="k", target="n1", **spec):
    spec.setdefault("task_type", "PKCI")
    return request(p.CREATE, request_id, target, idempotency_key=key, spec=TaskSpec(**spec))


def leader(names=("n1", "n2", "n3"), config=CONFIG):
    clock = Clock()
    world = SimWorld(names, clock)
    store = TaskStore()
    replies = Replies()
    controller = LeaderController("n1", store, FakeElection("n1", 1, me="n1"), world.cluster, world.network,
                                  config, clock=clock, reply=replies)
    return clock, world, store, controller, replies


def test_created_task_is_dispatched_in_the_next_step_and_answered_once_running():
    clock, world, store, controller, replies = leader()
    warm(world, controller)
    controller.on_request(create())
    clock.advance(0.1)
    controller.step()                                 # no reconciliation is due in this step
    assert len(world.network.commands) == 1 and replies == []
    world.run(controller, 0.5, dt=0.1)                # the Running event arrives with the next publish
    [reply] = replies
    assert reply.success and reply.status == c.RUNNING and (reply.origin_node, reply.request_id) == ("gw", 1)
    assert reply.task_id == store.records().popitem()[0] and reply.primary in world.nodes


def test_one_start_command_addresses_every_participant():
    clock, world, store, controller, replies = leader()
    warm(world, controller)
    controller.on_request(create(mode="fusion", helper_count=2))
    world.run(controller, 1.0, dt=0.1)
    [command] = world.network.commands
    record = store.records().popitem()[1]
    assert command.op == p.START and set(command.targets) == {"n1", "n2", "n3"} == set(record.nodes)
    assert replies[0].success and set(replies[0].helpers) == set(record.helpers)


def test_create_that_does_not_run_in_time_times_out_with_its_task_id():
    clock, world, store, controller, replies = leader(config=ControllerConfig(
        scheduler=SchedulerConfig(leader_self_penalty=0.0), create_wait_timeout_s=3.0))
    warm(world, controller)
    for node in world.nodes.values():
        node.view["inference_ready"] = False
    world.publish()
    controller.on_request(create())
    world.run(controller, 2.5)
    assert replies == []
    world.run(controller, 1.0)
    assert replies[0].error_code == c.TIMEOUT and replies[0].task_id and replies[0].status == c.PENDING


def test_retried_create_returns_the_same_task_as_a_duplicate():
    clock, world, store, controller, replies = leader()
    warm(world, controller)
    controller.on_request(create(1))
    world.run(controller, 1.0)
    controller.on_request(create(2))
    world.run(controller, 0.5)
    assert [r.request_id for r in replies] == [1, 2] and replies[0].task_id == replies[1].task_id
    assert (replies[0].duplicate, replies[1].duplicate) == (False, True) and len(store.records()) == 1


def test_requests_are_answered_by_their_target_only_and_never_handled_elsewhere():
    clock, world, store, controller, replies = leader()
    controller.on_request(create(1))                  # warming up
    world.run(controller, 0.5)
    assert replies[-1].error_code == c.NO_LEADER
    warm(world, controller)
    controller.on_request(create(2, target="n2"))     # someone else's request
    world.run(controller, 1.0)
    assert [r.request_id for r in replies] == [1] and store.records() == {}
    follower = LeaderController("n2", TaskStore(), FakeElection("n1", 1, me="n2"), world.cluster, world.network,
                                CONFIG, clock=clock, reply=replies)
    follower.on_request(create(3, target="n2"))       # sent to n2 when it led, or believed to
    follower.step()
    assert replies[-1].error_code == c.NOT_LEADER and replies[-1].request_id == 3


def test_query():
    clock, world, store, controller, replies = leader()
    warm(world, controller)
    controller.on_request(create())
    world.run(controller, 1.0)
    task_id = replies[0].task_id
    controller.on_request(request(p.QUERY, 2, task_id=task_id))
    controller.on_request(request(p.QUERY, 3, task_id="nope"))
    world.run(controller, 0.5)
    assert replies[1].record.task_id == task_id and replies[1].record.spec.task_type == "PKCI"
    assert replies[2].error_code == c.NOT_FOUND


def created(world, controller, replies, **spec):
    controller.on_request(create(**spec))
    world.run(controller, 1.0)
    assert replies[-1].success
    return replies[-1].task_id


def test_destroy_stops_every_participant_at_once_and_answers_when_they_confirmed():
    clock, world, store, controller, replies = leader()
    warm(world, controller)
    task_id = created(world, controller, replies, mode="redundant", helper_count=1)
    nodes = store.get(task_id).nodes
    controller.on_request(request(p.DESTROY, 2, task_id=task_id, reason="user"))
    clock.advance(0.1)
    controller.step()                                 # no reconciliation is due: the stop goes out now
    stop = world.network.commands[-1]
    assert (stop.op, stop.stop_kind, set(stop.targets)) == (p.STOP, c.STOP_CANCEL, set(nodes))
    assert running_copies(world, task_id) == [] and replies[-1].request_id == 1
    world.run(controller, 0.5, dt=0.1)                # the Stopped events arrive
    assert replies[-1].request_id == 2 and replies[-1].success and replies[-1].status == c.CANCELLED
    assert all(task_id in world.nodes[n].guard.tombstones() for n in nodes)


def test_destroy_times_out_while_an_online_participant_has_not_stopped_and_succeeds_on_retry():
    clock, world, store, controller, replies = leader()
    warm(world, controller)
    task_id = created(world, controller, replies, mode="redundant", helper_count=1)
    helper = store.get(task_id).helpers[0]
    world.nodes[helper].deaf = True                   # the stop does not reach it
    controller.on_request(request(p.DESTROY, 2, task_id=task_id))
    world.run(controller, 9.5)
    assert replies[-1].request_id == 1
    world.run(controller, 1.0)
    assert replies[-1].error_code == c.TIMEOUT and replies[-1].status == c.CANCELLED
    world.nodes[helper].deaf = False
    controller.on_request(request(p.DESTROY, 3, task_id=task_id))   # retry: stops again, now confirmed
    world.run(controller, 1.0)
    assert replies[-1].request_id == 3 and replies[-1].success and running_copies(world, task_id) == []


def test_destroy_does_not_wait_for_offline_participants():
    clock, world, store, controller, replies = leader()
    warm(world, controller)
    task_id = created(world, controller, replies, mode="redundant", helper_count=1)
    world.nodes[store.get(task_id).helpers[0]].alive = False
    world.run(controller, 6.0)                        # now offline
    controller.on_request(request(p.DESTROY, 2, task_id=task_id))
    world.run(controller, 1.0)
    assert replies[-1].request_id == 2 and replies[-1].success


def test_destroy_of_missing_or_finished_tasks():
    clock, world, store, controller, replies = leader()
    warm(world, controller)
    task_id = created(world, controller, replies)
    sim = world.nodes[store.get(task_id).primary]
    sim.report(task_id, store.get(task_id).generation, p.FAILED, c.INFERENCE_ERROR)
    world.run(controller, 0.5)
    controller.on_request(request(p.DESTROY, 2, task_id=task_id))
    controller.on_request(request(p.DESTROY, 3, task_id="nope"))
    world.run(controller, 0.5)
    assert [r.error_code for r in replies[1:]] == [c.INVALID_STATE, c.NOT_FOUND]


def test_destroyed_task_does_not_come_back_after_a_leader_change():
    """DESIGN 4.5: the primary stopped and left a tombstone, the helper missed the stop, then the
    leader died. The new leader adopts the task as Cancelled and stops the helper's copy."""
    clock, world, store, old, replies = leader()
    warm(world, old)
    task_id = created(world, old, replies, mode="redundant", helper_count=1)
    helper = store.get(task_id).helpers[0]
    world.nodes[helper].deaf = True
    old.on_request(request(p.DESTROY, 2, task_id=task_id))
    world.run(old, 0.5)
    assert running_copies(world, task_id) == [helper]
    world.nodes[helper].deaf = False
    new_store = TaskStore()
    new = LeaderController("n2", new_store, FakeElection("n2", 2, me="n2"), world.cluster, world.network, CONFIG,
                           clock=clock)
    world.run(new, 7.0)
    assert new_store.get(task_id).status == c.CANCELLED and running_copies(world, task_id) == []


def test_stepping_down_answers_waiting_requests_at_once():
    clock, world, store, controller, replies = leader(config=ControllerConfig(
        scheduler=SchedulerConfig(leader_self_penalty=0.0), create_wait_timeout_s=30.0))
    warm(world, controller)
    for node in world.nodes.values():
        node.view["inference_ready"] = False
    world.publish()
    controller.on_request(create())
    world.run(controller, 1.0)
    controller._election.set("n2", 2)
    world.run(controller, 0.5)
    assert replies[-1].error_code == c.NOT_LEADER and replies[-1].task_id


def test_a_helper_refusing_the_start_moves_the_whole_task():
    clock, world, store, controller, replies = leader()
    world.nodes["n1"].view["cpu_usage"] = 90.0       # keep n1 last in line
    warm(world, controller)
    world.nodes["n3"].fail_start = c.BUSY
    controller.on_request(create(mode="redundant", helper_count=1))
    world.run(controller, 2.0, dt=0.1)
    record = store.records().popitem()[1]
    assert record.status == c.RUNNING and record.epoch >= 3 and "n3" not in record.nodes


def test_destroy_is_confirmed_by_stopped_events_before_heartbeats_catch_up():
    clock, world, store, controller, replies = leader()
    warm(world, controller)
    task_id = created(world, controller, replies, mode="redundant", helper_count=1)
    controller.on_request(request(p.DESTROY, 2, task_id=task_id))
    clock.advance(0.1)
    controller.step()                                 # stops sent; heartbeats still list the task
    for node in world.nodes.values():
        for event in node.events:
            controller.on_event(event)
        node.events.clear()
    clock.advance(0.1)
    controller.step()
    assert replies[-1].request_id == 2 and replies[-1].success


def test_destroy_is_confirmed_by_heartbeats_when_stopped_events_are_lost():
    clock, world, store, controller, replies = leader()
    warm(world, controller)
    task_id = created(world, controller, replies, mode="redundant", helper_count=1)
    controller.on_request(request(p.DESTROY, 2, task_id=task_id))
    clock.advance(0.1)
    controller.step()
    for node in world.nodes.values():
        node.events.clear()                           # the Stopped events are lost
    world.run(controller, 0.5, dt=0.1)
    assert replies[-1].request_id == 2 and replies[-1].success
