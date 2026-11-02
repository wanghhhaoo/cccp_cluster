from cccp_node.core import constants as c
from cccp_node.core.cluster_view import NodeView, TaskStateView
from cccp_node.core.reconciler import (
    AdoptTask, ConfirmRunning, Dispatch, FailTask, LeaderMemory, ReconcileConfig, Reschedule, ScheduleTask,
    StopOrphan, reconcile,
)
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import Adopt, Assign, Cancel, ReportStatus, Submit, TaskTable

CONFIG = ReconcileConfig()
SPEC = TaskSpec(task_type="PKCI")


def state(task_id, epoch, role=c.ROLE_PRIMARY, primary="n1", helpers=(), key=None, spec=SPEC):
    return TaskStateView(task_id, epoch, role, primary, tuple(helpers), key or "key-" + task_id, "", spec.to_json())


class World:
    """A task table plus node reports, advanced one step at a time."""

    def __init__(self):
        self.table = TaskTable()
        self.memory = LeaderMemory()
        self.running = {}          # node -> list of TaskStateView
        self.online = set()

    def submit(self, key="k", **kw):
        kw.setdefault("task_type", "PKCI")
        return self.table.apply(Submit("t-" + key, key, TaskSpec(**kw))).task_id

    def runs(self, node, task_id, epoch, role=c.ROLE_PRIMARY, **kw):
        record = self.table.get(task_id)
        kw.setdefault("primary", record.primary if record else node)
        kw.setdefault("key", record.idempotency_key if record else None)
        self.running.setdefault(node, []).append(state(task_id, epoch, role, **kw))

    def nodes(self):
        return {n: NodeView(n, running_tasks=tuple(self.running.get(n, ()))) for n in self.online}

    def step(self, now):
        records = {r.task_id: r for r in self.table.tasks()}
        self.memory.observe(records, now)
        return reconcile(records, self.nodes(), self.memory, now, CONFIG)


# ------------------------------------------------------------ table-driven

def test_pending_tasks_are_scheduled_then_failed_after_timeout():
    w = World()
    task_id = w.submit()
    assert w.step(0.0) == [ScheduleTask(task_id, 0)]
    assert w.step(7.9) == [ScheduleTask(task_id, 0)]
    [fail] = w.step(8.0)
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
    assert w.step(1.0) == []
    w.runs("n2", task_id, 1, c.ROLE_HELPER)
    assert w.step(5.0) == [Dispatch(task_id, 1, "n1", c.ROLE_PRIMARY)]
    w.runs("n1", task_id, 1)
    assert w.step(11.0) == [ConfirmRunning(task_id, 1, "n1")]      # its Running report never arrived


def test_scheduled_but_never_started_is_rescheduled():
    w = World()
    w.online = {"n1"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, 0, "n1"))
    w.step(0.0)
    assert w.step(15.0) == [Reschedule(task_id, 1, "not started on n1")]


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
    assert w.step(0.0) == [] and w.step(2.9) == []
    assert w.step(3.0) == [Reschedule(task_id, 1, "task lost on n1")]


def test_orphans_are_stopped_and_throttled():
    w = World()
    w.online = {"n1", "n2"}
    done = w.submit("a")
    moved = w.submit("b")
    w.table.apply(Assign(done, 0, "n1"))
    w.table.apply(Cancel(done))
    w.table.apply(Assign(moved, 0, "n1"))
    w.table.apply(Assign(moved, 1, "n2"))                         # rescheduled: epoch 2 on n2
    w.runs("n1", done, 1)
    w.runs("n1", moved, 1, primary="n1")
    w.runs("n2", moved, 2)
    stops = [a for a in w.step(0.0) if isinstance(a, StopOrphan)]
    assert [(s.node, s.task_id, s.epoch) for s in stops] == [("n1", done, 1), ("n1", moved, 1)]
    assert stops[1].reason == "epoch 1 superseded by 2"
    for s in stops:
        w.memory.note_stop(s.node, s.task_id, s.epoch, 0.0)
    assert not [a for a in w.step(1.0) if isinstance(a, StopOrphan)]
    assert len([a for a in w.step(5.0) if isinstance(a, StopOrphan)]) == 2


# --------------------------------------------------------------- adoption

def test_unknown_running_tasks_are_adopted_not_stopped():
    w = World()
    w.online = {"n1", "n2"}
    w.runs("n1", "t-x", 3, primary="n1", helpers=("n2",), key="kx")
    w.runs("n2", "t-x", 3, role=c.ROLE_HELPER, primary="n1", key="kx")
    [adopt] = w.step(0.0)
    assert isinstance(adopt, AdoptTask)
    assert (adopt.task_id, adopt.epoch, adopt.primary, adopt.helpers, adopt.idempotency_key, adopt.reported_by) == (
        "t-x", 3, "n1", ("n2",), "kx", "n1")
    w.table.apply(Adopt(adopt.task_id, adopt.idempotency_key, adopt.name, adopt.spec, adopt.epoch, adopt.primary,
                        adopt.helpers))
    assert w.step(1.0) == []                                      # now known and healthy


def test_task_whose_primary_died_is_adopted_from_a_helper_then_moved():
    w = World()
    w.online = {"n2"}
    w.runs("n2", "t-x", 1, role=c.ROLE_HELPER, primary="n1", key="kx")
    [adopt] = w.step(0.0)
    assert (adopt.primary, adopt.helpers) == ("n1", ("n2",))
    w.table.apply(Adopt(adopt.task_id, adopt.idempotency_key, adopt.name, adopt.spec, adopt.epoch, adopt.primary,
                        adopt.helpers))
    assert w.step(1.0) == [Reschedule("t-x", 1, "primary n1 offline")]


def test_copies_from_two_leaders_converge_on_one():
    w = World()
    w.online = {"n1", "n2", "n3"}
    w.runs("n3", "t-x", 2, primary="n3", key="kx")                # placed by leader A
    w.runs("n2", "t-x", 2, primary="n2", key="kx")                # placed by leader B in the same epoch
    w.runs("n1", "t-x", 1, primary="n1", key="kx")                # an older placement
    [adopt] = w.step(0.0)
    assert (adopt.epoch, adopt.primary, adopt.reported_by) == (2, "n2", "n2")
    w.table.apply(Adopt(adopt.task_id, adopt.idempotency_key, adopt.name, adopt.spec, adopt.epoch, adopt.primary))
    stops = sorted((a.node, a.reason) for a in w.step(1.0) if isinstance(a, StopOrphan))
    assert stops == [("n1", "epoch 1 superseded by 2"), ("n3", "not assigned to n3")]


def test_newer_epoch_elsewhere_replaces_the_leaders_placement():
    w = World()
    w.online = {"n1", "n2"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, 0, "n1"))
    w.runs("n2", task_id, 4, primary="n2")
    assert [a for a in w.step(0.0) if isinstance(a, AdoptTask)][0].epoch == 4


def test_second_task_with_a_known_key_is_stopped_as_duplicate():
    w = World()
    w.online = {"n1"}
    known = w.submit("k")
    w.table.apply(Assign(known, 0, "n1"))
    w.runs("n1", known, 1)
    w.runs("n1", "t-other", 1, key="k")                          # same request accepted twice
    stops = [a for a in w.step(0.0) if isinstance(a, StopOrphan)]
    assert [(s.task_id, s.reason) for s in stops] == [("t-other", "duplicate of task %s" % known)]


def test_unreadable_spec_is_stopped():
    w = World()
    w.online = {"n1"}
    w.running["n1"] = [TaskStateView("t-x", 1, c.ROLE_PRIMARY, "n1", (), "kx", "", "{not json")]
    [stop] = w.step(0.0)
    assert isinstance(stop, StopOrphan) and stop.reason.startswith("unreadable spec")


def test_memory_timers_restart_when_state_changes_and_prune():
    w = World()
    w.online = {"n1"}
    task_id = w.submit()
    w.step(0.0)
    w.table.apply(Assign(task_id, 0, "n1"))
    w.memory.note_dispatch(task_id, 1, "n1", 10.0)
    w.step(10.0)
    assert w.memory.since(w.table.get(task_id), 99.0) == 10.0
    w.table.apply(Assign(task_id, 1, "n1"))
    w.memory.prune({r.task_id: r for r in w.table.tasks()})
    assert w.memory.dispatched == {}
