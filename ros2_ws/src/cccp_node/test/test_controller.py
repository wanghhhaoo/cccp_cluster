import uuid

from _sim import Clock, FakeElection, SimWorld

from cccp_node.control.controller import ControllerConfig, LeaderController
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
    controller = LeaderController("n1", store, election, world.cluster, world.dispatcher, config, clock=clock)
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
    assert world.dispatcher.starts == [] and not controller.active


def test_schedules_starts_and_records_running():
    clock, world, store, election, controller = setup()
    world.nodes["n2"].view["cpu_usage"] = 5.0
    warm(world, controller)
    task_id = submit(store)
    world.run(controller, 1.5)
    record = store.get(task_id)
    assert (record.status, record.primary, record.epoch) == (c.RUNNING, "n2", 1)
    assert world.dispatcher.starts == [("n2", task_id, (1, 1), 1, c.ROLE_PRIMARY)]


def test_collaborative_task_starts_primary_and_helpers():
    clock, world, store, election, controller = setup()
    warm(world, controller)
    task_id = submit(store, mode="fusion", helper_count=2)
    world.run(controller, 1.5)
    assert store.get(task_id).status == c.RUNNING and len(store.get(task_id).nodes) == 3
    assert sorted(role for *_, role in world.dispatcher.starts) == [c.ROLE_HELPER, c.ROLE_HELPER, c.ROLE_PRIMARY]


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
    assert (first, task_id, (1, 1), "(1,1) superseded by (2,1)") in world.dispatcher.stops
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
    sim.report(task_id, record.generation, c.REPORT_FAILED, c.NOT_READY)
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
    starts_before = len(world.dispatcher.starts)
    election.set("n2", 2)                            # n2 wins the next election...
    new_store = TaskStore()                          # ...with its own, empty table
    new = LeaderController("n2", new_store, FakeElection("n2", 2, me="n2"), world.cluster, world.dispatcher,
                           CONFIG, clock=clock)
    world.run(new, 7.0)
    assert new.active and world.dispatcher.stops == [] and len(world.dispatcher.starts) == starts_before
    for task_id in ids:
        old_record, rebuilt = store.get(task_id), new_store.get(task_id)
        assert (rebuilt.status, rebuilt.epoch, rebuilt.primary, rebuilt.helpers) == (
            c.RUNNING, old_record.epoch, old_record.primary, old_record.helpers)
        assert rebuilt.idempotency_key == old_record.idempotency_key and rebuilt.spec == old_record.spec
    assert new_store.apply(Submit("t-retry", "k1", TaskSpec(task_type="PKCI"))).duplicate   # retries dedupe


def test_two_leaders_accepting_the_same_request_converge_to_one_copy():
    clock, world, store_a, election, leader_a = setup()
    store_b = TaskStore()
    leader_b = LeaderController("n2", store_b, FakeElection("n2", 2, me="n2"), world.cluster, world.dispatcher,
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
    assert any(reason == "duplicate of task %s" % b for *_, reason in world.dispatcher.stops)
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
    sim.report(failed, store.get(failed).generation, c.REPORT_FAILED, c.INFERENCE_ERROR)
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
        node.reports.clear()                         # the Running report is lost
    world.run(controller, 3.0)
    assert store.get(task_id).status == c.RUNNING and len(world.dispatcher.starts) == 1


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
