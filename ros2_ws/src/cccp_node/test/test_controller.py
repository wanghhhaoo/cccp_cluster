from _sim import Clock, SimWorld

from cccp_node.consensus.local import LocalConsensus
from cccp_node.control.controller import ControllerConfig, LeaderController
from cccp_node.core import constants as c
from cccp_node.core.scheduler import SchedulerConfig
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import Submit

CONFIG = ControllerConfig(scheduler=SchedulerConfig(leader_self_penalty=0.0))


def setup(names=("n1", "n2", "n3"), leader=True, config=CONFIG):
    clock = Clock()
    world = SimWorld(names, clock)
    consensus = LocalConsensus("n1", leader=leader)
    controller = LeaderController("n1", consensus, world.cluster, world.dispatcher, config, clock=clock)
    return clock, world, consensus, controller


def submit(consensus, key="k", **kw):
    kw.setdefault("task_type", "PKCI")
    return consensus.submit(Submit(key, TaskSpec(**kw)), 1.0).task_id


def test_waits_for_warmup_before_acting():
    clock, world, consensus, controller = setup()
    task_id = submit(consensus)
    world.run(controller, 4.5)                      # leader since the first step, at 1000.5
    assert world.dispatcher.starts == [] and consensus.get(task_id).status == c.PENDING
    world.run(controller, 1.5)                      # acts at 1005.5; Running arrives one step later
    assert consensus.get(task_id).status == c.RUNNING


def test_followers_do_nothing():
    clock, world, consensus, controller = setup(leader=False)
    submit(consensus)
    world.run(controller, 20.0)
    assert world.dispatcher.starts == [] and not controller.active


def test_schedules_starts_and_records_running():
    clock, world, consensus, controller = setup()
    world.nodes["n2"].view["cpu_usage"] = 5.0
    task_id = submit(consensus)
    world.run(controller, 7.0)
    record = consensus.get(task_id)
    assert (record.status, record.primary, record.epoch) == (c.RUNNING, "n2", 1)
    assert world.dispatcher.starts == [("n2", task_id, 1, 1, c.ROLE_PRIMARY)]


def test_collaborative_task_starts_primary_and_helpers():
    clock, world, consensus, controller = setup()
    task_id = submit(consensus, mode="fusion", helper_count=2)
    world.run(controller, 7.0)
    record = consensus.get(task_id)
    assert record.status == c.RUNNING and len(record.nodes) == 3
    roles = sorted(role for _, _, _, _, role in world.dispatcher.starts)
    assert roles == [c.ROLE_HELPER, c.ROLE_HELPER, c.ROLE_PRIMARY]


def test_primary_failure_moves_task_and_old_copy_is_stopped():
    clock, world, consensus, controller = setup()
    task_id = submit(consensus)
    world.run(controller, 7.0)
    first = consensus.get(task_id).primary
    world.nodes[first].alive = False
    world.run(controller, 8.0)                      # past the 5 s offline timeout
    record = consensus.get(task_id)
    assert record.status == c.RUNNING and record.epoch == 2 and record.primary != first
    world.nodes[first].alive = True                 # comes back still running epoch 1
    world.run(controller, 2.0)
    assert (first, task_id, 1, "epoch 1 superseded by 2") in world.dispatcher.stops
    assert world.nodes[first].guard.running() == {}
    running = [n for n in world.nodes.values() if task_id in n.guard.running()]
    assert len(running) == 1                        # never two copies after the cleanup


def test_transient_start_failure_requeues_and_avoids_the_node():
    clock, world, consensus, controller = setup()
    world.nodes["n1"].view["cpu_usage"] = 0.0       # best node...
    world.nodes["n1"].fail_start = c.BUSY           # ...but it refuses
    task_id = submit(consensus)
    world.run(controller, 8.0)
    record = consensus.get(task_id)
    assert record.status == c.RUNNING and record.primary != "n1" and record.epoch == 3


def test_permanent_start_failure_fails_the_task():
    clock, world, consensus, controller = setup()
    for node in world.nodes.values():
        node.fail_start = c.INVALID_INPUT
    task_id = submit(consensus)
    world.run(controller, 7.0)
    record = consensus.get(task_id)
    assert record.status == c.FAILED and record.error_code == c.DISPATCH_FAILED


def test_no_feasible_node_fails_after_pending_timeout():
    clock, world, consensus, controller = setup()
    for node in world.nodes.values():
        node.view["inference_ready"] = False
    task_id = submit(consensus)
    world.run(controller, 20.0)
    assert consensus.get(task_id).status == c.PENDING
    world.run(controller, 20.0)
    assert consensus.get(task_id).error_code == c.NO_FEASIBLE_NODE


def test_new_leader_adopts_running_tasks_without_restarting_them():
    clock, world, consensus, old = setup()
    task_ids = [submit(consensus, "k%d" % i) for i in range(4)]
    world.run(old, 7.0)
    assert all(consensus.get(t).status == c.RUNNING for t in task_ids)
    starts_before = len(world.dispatcher.starts)
    consensus.set_leader(False)
    consensus.set_leader(True, term=2)              # another node won the election
    new = LeaderController("n2", consensus, world.cluster, world.dispatcher, CONFIG, clock=clock)
    world.run(new, 20.0)
    assert world.dispatcher.stops == []
    assert len(world.dispatcher.starts) == starts_before
    assert all(consensus.get(t).epoch == 1 for t in task_ids)


def test_lost_running_report_is_recovered_from_node_status():
    clock, world, consensus, controller = setup()
    task_id = submit(consensus)
    world.run(controller, 6.0)                      # dispatched in the last step
    for node in world.nodes.values():
        node.reports.clear()                        # the Running report is lost with the old leader
    world.run(controller, 3.0)
    record = consensus.get(task_id)
    assert record.status == c.RUNNING and record.epoch == 1
    assert len(world.dispatcher.starts) == 1        # recovered without restarting the task


def test_deposed_leader_is_fenced_by_executors():
    clock, world, consensus, controller = setup()
    for node in world.nodes.values():
        node.guard.on_start(5, 999, 1)              # executors already saw term 5
    task_id = submit(consensus)
    world.run(controller, 7.0)                      # this controller only has term 1
    assert all(n.guard.running() == {} for n in world.nodes.values())
    assert consensus.get(task_id).status != c.RUNNING


def test_cancelled_task_is_stopped_on_its_nodes():
    clock, world, consensus, controller = setup()
    task_id = submit(consensus)
    world.run(controller, 7.0)
    node = consensus.get(task_id).primary
    from cccp_node.core.task_table import Cancel
    consensus.submit(Cancel(task_id, "user"), 1.0)
    world.run(controller, 2.0)
    assert world.nodes[node].guard.running() == {}


def test_failed_report_from_primary_ends_the_task():
    clock, world, consensus, controller = setup()
    task_id = submit(consensus)
    world.run(controller, 7.0)
    record = consensus.get(task_id)
    sim = world.nodes[record.primary]
    sim.report(task_id, 1, c.REPORT_FAILED, c.INFERENCE_ERROR)
    sim.guard.stopped(task_id)
    world.run(controller, 1.0)
    assert consensus.get(task_id).status == c.FAILED


def test_terminal_tasks_are_pruned_eventually():
    config = ControllerConfig(scheduler=SchedulerConfig(leader_self_penalty=0.0), prune_terminal_after_s=30.0)
    clock, world, consensus, controller = setup(config=config)
    task_id = submit(consensus)
    world.run(controller, 7.0)
    from cccp_node.core.task_table import Cancel
    consensus.submit(Cancel(task_id), 1.0)
    world.run(controller, 25.0)
    assert consensus.get(task_id) is not None
    world.run(controller, 10.0)
    assert consensus.get(task_id) is None
