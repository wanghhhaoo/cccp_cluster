from cccp_node.core import constants as c
from cccp_node.core.cluster_view import NodeView, RunningTaskView
from cccp_node.core.reconciler import (
    ConfirmRunning, Dispatch, FailTask, LeaderMemory, ReconcileConfig, Reschedule, ScheduleTask, StopOrphan,
    reconcile,
)
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import Assign, Cancel, ReportStatus, Submit, TaskTable

CONFIG = ReconcileConfig()


class World:
    """A task table plus node reports, advanced one step at a time."""

    def __init__(self):
        self.table = TaskTable()
        self.memory = LeaderMemory()
        self.running = {}          # node -> list of (task_id, epoch, role)
        self.online = set()

    def submit(self, key="k", **kw):
        kw.setdefault("task_type", "PKCI")
        return self.table.apply(Submit(key, TaskSpec(**kw))).task_id

    def nodes(self):
        return {n: NodeView(n, running_tasks=tuple(RunningTaskView(*t) for t in self.running.get(n, ())))
                for n in self.online}

    def step(self, now):
        records = {r.task_id: r for r in self.table.tasks()}
        self.memory.observe(records, now)
        return reconcile(records, self.nodes(), self.memory, now, CONFIG)


def test_pending_tasks_are_scheduled_then_failed_after_timeout():
    w = World()
    task_id = w.submit()
    assert w.step(0.0) == [ScheduleTask(task_id, 0)]
    assert w.step(29.0) == [ScheduleTask(task_id, 0)]
    [fail] = w.step(30.0)
    assert isinstance(fail, FailTask) and fail.error_code == c.NO_FEASIBLE_NODE


def test_scheduled_task_is_dispatched_and_redispatched_until_started():
    w = World()
    w.online = {"n1", "n2"}
    task_id = w.submit(mode="redundant", helper_count=1)
    w.table.apply(Assign(task_id, 0, "n1", ("n2",)))
    first = w.step(0.0)
    assert first == [Dispatch(task_id, 1, "n1", c.ROLE_PRIMARY), Dispatch(task_id, 1, "n2", c.ROLE_HELPER)]
    for action in first:
        w.memory.note_dispatch(action.task_id, action.epoch, action.node, 0.0)
    assert w.step(1.0) == []                                   # retry not due yet
    w.running["n2"] = [(task_id, 1, c.ROLE_HELPER)]
    assert w.step(5.0) == [Dispatch(task_id, 1, "n1", c.ROLE_PRIMARY)]
    w.running["n1"] = [(task_id, 1, c.ROLE_PRIMARY)]
    assert w.step(11.0) == [ConfirmRunning(task_id, 1, "n1")]     # its Running report never arrived


def test_scheduled_but_never_started_is_rescheduled():
    w = World()
    w.online = {"n1"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, 0, "n1"))
    w.step(0.0)
    [action] = w.step(15.0)
    assert action == Reschedule(task_id, 1, "not started on n1")


def test_primary_offline_triggers_reschedule():
    w = World()
    w.online = {"n2"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, 0, "n1"))
    assert w.step(0.0) == [Reschedule(task_id, 1, "primary n1 offline")]


def test_running_task_missing_gets_grace_then_reschedule():
    w = World()
    w.online = {"n1"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, 0, "n1"))
    w.table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING))
    assert w.step(0.0) == []                                   # NodeStatus may lag the report
    assert w.step(2.9) == []
    assert w.step(3.0) == [Reschedule(task_id, 1, "task lost on n1")]


def test_healthy_running_task_needs_nothing():
    w = World()
    w.online = {"n1"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, 0, "n1"))
    w.table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING))
    w.running["n1"] = [(task_id, 1, c.ROLE_PRIMARY)]
    assert w.step(0.0) == [] and w.step(100.0) == []


def test_orphans_are_stopped_and_throttled():
    w = World()
    w.online = {"n1", "n2"}
    done = w.submit("a")
    moved = w.submit("b")
    w.table.apply(Assign(done, 0, "n1"))
    w.table.apply(Cancel(done))
    w.table.apply(Assign(moved, 0, "n1"))
    w.table.apply(Assign(moved, 1, "n2"))                       # rescheduled: epoch 2 on n2
    w.running["n1"] = [(done, 1, c.ROLE_PRIMARY), (moved, 1, c.ROLE_PRIMARY), (99, 1, c.ROLE_PRIMARY)]
    w.running["n2"] = [(moved, 2, c.ROLE_PRIMARY)]
    stops = [a for a in w.step(0.0) if isinstance(a, StopOrphan)]
    assert [(s.node, s.task_id, s.epoch) for s in stops] == [("n1", done, 1), ("n1", moved, 1), ("n1", 99, 1)]
    assert stops[1].reason == "epoch 1 superseded by 2"
    for s in stops:
        w.memory.note_stop(s.node, s.task_id, s.epoch, 0.0)
    assert not [a for a in w.step(1.0) if isinstance(a, StopOrphan)]
    assert len([a for a in w.step(5.0) if isinstance(a, StopOrphan)]) == 3


def test_node_running_a_task_it_was_not_assigned_is_stopped():
    w = World()
    w.online = {"n1", "n3"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, 0, "n1"))
    w.running = {"n1": [(task_id, 1, c.ROLE_PRIMARY)], "n3": [(task_id, 1, c.ROLE_PRIMARY)]}
    stops = [a for a in w.step(0.0) if isinstance(a, StopOrphan)]
    assert stops == [StopOrphan("n3", task_id, 1, "not assigned to n3")]


def test_memory_timers_restart_when_state_changes_and_prune():
    w = World()
    w.online = {"n1"}
    task_id = w.submit()
    w.step(0.0)
    w.table.apply(Assign(task_id, 0, "n1"))
    w.memory.note_dispatch(task_id, 1, "n1", 10.0)
    w.step(10.0)
    record = w.table.get(task_id)
    assert w.memory.since(record, 99.0) == 10.0                 # entered Scheduled at 10
    w.table.apply(Assign(task_id, 1, "n1"))
    w.memory.prune({r.task_id: r for r in w.table.tasks()})
    assert w.memory.dispatched == {}
