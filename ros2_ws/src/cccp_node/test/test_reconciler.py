from cccp_node.core import constants as c
from cccp_node.core.cluster_view import NodeView, TaskListView, TaskStateView
from cccp_node.core.generation import Generation
from cccp_node.core.reconciler import (
    AdoptTask, CancelTask, ConfirmRunning, Dispatch, FailTask, LeaderMemory, ReconcileConfig, Reschedule, ScheduleTask,
    StopOrphan, reconcile,
)
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import Adopt, Assign, Cancel, MarkFailed, ReportStatus, Submit, TaskTable

CONFIG = ReconcileConfig()
SPEC = TaskSpec(task_type="PKCI")
T = 1                                    # the leader's term


def g(epoch, term=T):
    """Generation of epoch placed in term; a task never scheduled is at (0, 0)."""
    return Generation(epoch, term if epoch else 0)


def state(task_id, epoch, role=c.ROLE_PRIMARY, primary="n1", helpers=(), key=None, spec=SPEC, term=T):
    return TaskStateView(task_id, epoch, role, primary, tuple(helpers), key or "key-" + task_id, "", spec.to_json(),
                         term)


class World:
    """A task table plus node reports, advanced one step at a time."""

    def __init__(self):
        self.table = TaskTable()
        self.memory = LeaderMemory()
        self.running = {}          # node -> list of TaskStateView
        self.tombstones = {}       # node -> task ids
        self.online = set()
        self.retained = set()      # offline nodes whose last TaskList is still kept
        self.lagging = set()       # nodes whose TaskList has not caught up with their heartbeat
        self.stalled = set()       # nodes whose executor went quiet

    def submit(self, key="k", **kw):
        kw.setdefault("task_type", "PKCI")
        return self.table.apply(Submit("t-" + key, key, TaskSpec(**kw))).task_id

    def runs(self, node, task_id, epoch, role=c.ROLE_PRIMARY, **kw):
        record = self.table.get(task_id)
        kw.setdefault("primary", record.primary if record else node)
        kw.setdefault("key", record.idempotency_key if record else None)
        self.running.setdefault(node, []).append(state(task_id, epoch, role, **kw))

    def nodes(self):
        return {n: NodeView(n, running_tasks=tuple(self.running.get(n, ())), executor_alive=n not in self.stalled)
                for n in self.online}

    def task_lists(self):
        return {n: TaskListView(n, 1, 1, tuple(self.running.get(n, ())), tuple(self.tombstones.get(n, ())))
                for n in (self.online | self.retained) - self.lagging}

    def step(self, now, config=CONFIG):
        records = {r.task_id: r for r in self.table.tasks()}
        self.memory.observe(records, now)
        return reconcile(records, self.nodes(), self.task_lists(), self.memory, now, config)


# ------------------------------------------------------------ table-driven

def test_pending_tasks_are_scheduled_then_failed_after_timeout():
    w = World()
    task_id = w.submit()
    assert w.step(0.0) == [ScheduleTask(task_id, g(0))]
    assert w.step(7.9) == [ScheduleTask(task_id, g(0))]
    [fail] = w.step(8.0)
    assert isinstance(fail, FailTask) and fail.error_code == c.NO_FEASIBLE_NODE


def test_scheduled_task_is_dispatched_and_redispatched_until_started():
    w = World()
    w.online = {"n1", "n2"}
    task_id = w.submit(mode="redundant", helper_count=1)
    w.table.apply(Assign(task_id, g(0), T, "n1", ("n2",)))
    first = w.step(0.0)
    assert first == [Dispatch(task_id, g(1), "n1", c.ROLE_PRIMARY), Dispatch(task_id, g(1), "n2", c.ROLE_HELPER)]
    for action in first:
        w.memory.note_dispatch(action.task_id, action.generation, action.node, 0.0)
    assert w.step(1.0) == []
    w.runs("n2", task_id, 1, c.ROLE_HELPER)
    assert w.step(5.0) == [Dispatch(task_id, g(1), "n1", c.ROLE_PRIMARY)]
    w.runs("n1", task_id, 1)
    assert w.step(11.0) == [ConfirmRunning(task_id, g(1), "n1")]      # its Running report never arrived


def test_scheduled_but_never_started_is_rescheduled():
    w = World()
    w.online = {"n1"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, g(0), T, "n1"))
    w.step(0.0)
    assert w.step(15.0) == [Reschedule(task_id, g(1), "not started on n1")]


def test_primary_offline_triggers_reschedule():
    w = World()
    w.online = {"n2"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, g(0), T, "n1"))
    assert w.step(0.0) == [Reschedule(task_id, g(1), "primary n1 offline")]


def test_running_task_missing_gets_grace_then_reschedule():
    w = World()
    w.online = {"n1"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, g(0), T, "n1"))
    w.table.apply(ReportStatus("n1", task_id, g(1), c.REPORT_RUNNING))
    assert w.step(0.0) == [] and w.step(2.9) == []
    assert w.step(3.0) == [Reschedule(task_id, g(1), "task lost on n1")]


def test_orphans_are_stopped_and_throttled():
    w = World()
    w.online = {"n1", "n2"}
    done = w.submit("a")
    moved = w.submit("b")
    w.table.apply(Assign(done, g(0), T, "n1"))
    w.table.apply(Cancel(done))
    w.table.apply(Assign(moved, g(0), T, "n1"))
    w.table.apply(Assign(moved, g(1), T, "n2"))                         # rescheduled: epoch 2 on n2
    w.runs("n1", done, 1)
    w.runs("n1", moved, 1, primary="n1")
    w.runs("n2", moved, 2)
    stops = [a for a in w.step(0.0) if isinstance(a, StopOrphan)]
    assert [(s.node, s.task_id, s.generation) for s in stops] == [("n1", done, g(1)), ("n1", moved, g(1))]
    assert stops[1].reason == "(1,1) superseded by (2,1)"
    for s in stops:
        w.memory.note_stop(s.node, s.task_id, s.generation, 0.0)
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
    assert (adopt.task_id, adopt.generation, adopt.primary, adopt.helpers) == ("t-x", g(3), "n1", ("n2",))
    assert (adopt.idempotency_key, adopt.reported_by) == ("kx", "n1")
    w.table.apply(Adopt(adopt.task_id, adopt.idempotency_key, adopt.name, adopt.spec, adopt.generation, adopt.primary,
                        adopt.helpers))
    assert w.step(1.0) == []                                      # now known and healthy


def test_task_whose_primary_died_is_adopted_from_a_helper_then_moved():
    w = World()
    w.online = {"n2"}
    w.runs("n2", "t-x", 1, role=c.ROLE_HELPER, primary="n1", key="kx")
    [adopt] = w.step(0.0)
    assert (adopt.primary, adopt.helpers) == ("n1", ("n2",))
    w.table.apply(Adopt(adopt.task_id, adopt.idempotency_key, adopt.name, adopt.spec, adopt.generation, adopt.primary,
                        adopt.helpers))
    assert w.step(1.0) == [Reschedule("t-x", g(1), "primary n1 offline")]


def test_copies_from_two_leaders_converge_on_one():
    w = World()
    w.online = {"n1", "n2", "n3"}
    w.runs("n3", "t-x", 2, primary="n3", key="kx")                # placed by leader A
    w.runs("n2", "t-x", 2, primary="n2", key="kx")                # placed again in the same generation
    w.runs("n1", "t-x", 1, primary="n1", key="kx")                # an older placement
    [adopt] = w.step(0.0)
    assert (adopt.generation, adopt.primary, adopt.reported_by) == (g(2), "n2", "n2")
    w.table.apply(Adopt(adopt.task_id, adopt.idempotency_key, adopt.name, adopt.spec, adopt.generation, adopt.primary))
    stops = sorted((a.node, a.reason) for a in w.step(1.0) if isinstance(a, StopOrphan))
    assert stops == [("n1", "(1,1) superseded by (2,1)"), ("n3", "not assigned to n3")]


def test_copies_rescheduled_by_two_leaders_are_told_apart_by_assign_term():
    """Leaders of terms 4 and 5 both moved the task to epoch 2: term 5's copy is the newer one."""
    w = World()
    w.online = {"n1", "n2"}
    w.runs("n1", "t-x", 2, primary="n1", key="kx", term=5)
    w.runs("n2", "t-x", 2, primary="n2", key="kx", term=4)
    [adopt] = w.step(0.0)
    assert (adopt.generation, adopt.primary) == ((2, 5), "n1")
    w.table.apply(Adopt(adopt.task_id, adopt.idempotency_key, adopt.name, adopt.spec, adopt.generation, adopt.primary))
    stops = [(a.node, a.reason) for a in w.step(1.0) if isinstance(a, StopOrphan)]
    assert stops == [("n2", "(2,4) superseded by (2,5)")]


def test_newer_epoch_elsewhere_replaces_the_leaders_placement():
    w = World()
    w.online = {"n1", "n2"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, g(0), T, "n1"))
    w.runs("n2", task_id, 4, primary="n2")
    assert [a for a in w.step(0.0) if isinstance(a, AdoptTask)][0].generation == g(4)


def test_second_task_with_a_known_key_is_stopped_as_duplicate():
    w = World()
    w.online = {"n1"}
    known = w.submit("k")
    w.table.apply(Assign(known, g(0), T, "n1"))
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
    w.table.apply(Assign(task_id, g(0), T, "n1"))
    w.memory.note_dispatch(task_id, g(1), "n1", 10.0)
    w.step(10.0)
    assert w.memory.since(w.table.get(task_id), 99.0) == 10.0
    w.table.apply(Assign(task_id, g(1), T, "n1"))
    w.memory.prune({r.task_id: r for r in w.table.tasks()})
    assert w.memory.dispatched == {}


# ------------------------------------------------------------- 0.3 rules

def adopt_all(w, actions):
    for a in actions:
        if isinstance(a, AdoptTask):
            w.table.apply(Adopt(a.task_id, a.idempotency_key, a.name, a.spec, a.generation, a.primary, a.helpers,
                                a.reported_by, a.cancelled))


def stops(actions):
    return sorted((a.node, a.task_id, a.kind) for a in actions if isinstance(a, StopOrphan))


def test_pending_tasks_are_scheduled_by_priority():
    w = World()
    low, high, mid = w.submit("a"), w.submit("b", priority=200), w.submit("c", priority=100)
    assert [a.task_id for a in w.step(0.0)] == [high, mid, low]


def test_tasks_of_an_offline_node_are_adopted_from_its_retained_task_list_then_moved():
    """Row 1: the old leader ran the task itself and is gone; other nodes kept its TaskList."""
    w = World()
    w.online, w.retained = {"n2"}, {"n1"}
    w.runs("n1", "t-x", 2, primary="n1", key="kx", term=4)
    [adopt] = w.step(0.0)
    assert (adopt.task_id, adopt.generation, adopt.reported_by, adopt.cancelled) == ("t-x", (2, 4), "n1", False)
    adopt_all(w, [adopt])
    assert w.step(1.0) == [Reschedule("t-x", (2, 4), "primary n1 offline")]


def test_tombstone_anywhere_adopts_the_task_as_cancelled_and_stops_its_copies():
    """Destroyed just before the leader died: n1 stopped and buried it, n2 still runs it."""
    w = World()
    w.online = {"n1", "n2"}
    w.runs("n2", "t-x", 1, role=c.ROLE_HELPER, primary="n1", helpers=("n2",), key="kx")
    w.tombstones["n1"] = ["t-x"]
    [adopt] = w.step(0.0)
    assert adopt.cancelled
    adopt_all(w, [adopt])
    assert w.table.get("t-x").status == c.CANCELLED
    assert stops(w.step(1.0)) == [("n2", "t-x", c.STOP_CANCEL)]


def test_unknown_task_waits_for_its_nodes_task_list():
    w = World()
    w.online, w.lagging = {"n1"}, {"n1"}
    w.runs("n1", "t-x", 1, key="kx")
    assert w.step(0.0) == []                                      # neither adopted nor stopped
    w.lagging.clear()
    assert [type(a) for a in w.step(1.0)] == [AdoptTask]


def test_stop_kinds():
    w = World()
    w.online = {"n1", "n2", "n3"}
    cancelled, failed, moved, known = w.submit("a"), w.submit("b"), w.submit("c"), w.submit("d")
    for task_id in (cancelled, failed, moved, known):
        w.table.apply(Assign(task_id, g(0), T, "n1"))
    w.table.apply(Cancel(cancelled))
    w.table.apply(MarkFailed(failed, g(1), c.DISPATCH_FAILED))
    w.table.apply(Assign(moved, g(1), T, "n2"))
    for task_id in (cancelled, failed, moved, known):
        w.runs("n1", task_id, 1)
    w.runs("n3", known, 1)                                        # n3 has no part in it
    w.runs("n3", "t-dup", 1, key="d")                             # same key as `known`
    assert stops(w.step(0.0)) == [
        ("n1", cancelled, c.STOP_CANCEL), ("n1", failed, c.STOP_STALE), ("n1", moved, c.STOP_STALE),
        ("n3", known, c.STOP_UNASSIGNED), ("n3", "t-dup", c.STOP_DUPLICATE)]


def test_stalled_executor_moves_its_tasks_only_after_the_grace_period():
    """Row 5. Its heartbeat keeps the last known tasks, so row 8 (task lost) does not fire."""
    w = World()
    w.online = {"n1", "n2"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, g(0), T, "n1"))
    w.table.apply(ReportStatus("n1", task_id, g(1), c.REPORT_RUNNING))
    w.runs("n1", task_id, 1)
    w.stalled = {"n1"}
    assert w.step(0.0) == [] and w.step(9.9) == []
    assert w.step(10.0) == [Reschedule(task_id, g(1), "executor on n1 silent for 10 s")]
    w.stalled.clear()                                            # recovered in time elsewhere: timer resets
    w.step(11.0)
    w.stalled = {"n1"}
    assert w.step(12.0) == []


def mass_offline_world(nodes=6, config=CONFIG):
    w = World()
    w.online = {"n%d" % i for i in range(1, nodes + 1)}
    ids = []
    for i in range(1, nodes + 1):
        task_id = w.submit("k%d" % i)
        w.table.apply(Assign(task_id, g(0), T, "n%d" % i))
        w.table.apply(ReportStatus("n%d" % i, task_id, g(1), c.REPORT_RUNNING))
        w.runs("n%d" % i, task_id, 1)
        ids.append(task_id)
    w.step(0.0, config)
    return w, ids


def reschedules(actions):
    return sorted(a.task_id for a in actions if isinstance(a, Reschedule))


def test_a_few_nodes_going_offline_are_handled_as_usual():
    w, ids = mass_offline_world()
    w.online -= {"n5", "n6"}                                      # 2 of 6: not more than a third
    assert reschedules(w.step(5.0)) == ids[4:] and not w.memory.protecting


def test_mass_offline_pauses_moving_tasks_until_the_nodes_return():
    w, ids = mass_offline_world()
    w.online -= {"n4", "n5", "n6"}                                # 3 of 6 within the window
    assert reschedules(w.step(5.0)) == [] and w.memory.protecting
    assert reschedules(w.step(20.0)) == [] and w.memory.protecting   # still out of the window: held
    w.online |= {"n4", "n5"}                                      # only n6 still missing
    assert reschedules(w.step(21.0)) == [ids[5]] and not w.memory.protecting


def test_mass_offline_protection_gives_up_after_the_hold_time():
    w, ids = mass_offline_world()
    w.online -= {"n4", "n5", "n6"}
    assert reschedules(w.step(5.0)) == []
    assert reschedules(w.step(34.9)) == []
    assert reschedules(w.step(35.0)) == ids[3:] and not w.memory.protecting
    assert not w.memory.protecting and w.memory.protection_spent


def test_mass_offline_counts_drops_spread_over_the_window():
    w, ids = mass_offline_world()
    w.online -= {"n6"}
    assert reschedules(w.step(3.0)) == [ids[5]]                   # the first loss looks ordinary
    w.online -= {"n4", "n5"}
    assert reschedules(w.step(8.0)) == [] and w.memory.protecting


def test_protection_held_too_long_does_not_restart_while_the_drops_are_recent():
    config = ReconcileConfig(mass_offline_window_s=10.0, mass_offline_hold_s=4.0)
    w, ids = mass_offline_world(config=config)
    w.online -= {"n4", "n5", "n6"}
    assert reschedules(w.step(5.0, config)) == []
    assert reschedules(w.step(9.0, config)) == ids[3:]            # gave up after 4 s
    assert reschedules(w.step(10.0, config)) == ids[3:]           # the drops are still recent: stays off


def test_a_task_destroyed_by_another_leader_is_cancelled_here_too():
    """While two leaders coexisted the other one destroyed the task: its primary stopped and left
    a tombstone. This leader, which kept its table, must not move the task elsewhere."""
    w = World()
    w.online = {"n1", "n2"}
    task_id = w.submit()
    w.table.apply(Assign(task_id, g(0), T, "n1"))
    w.table.apply(ReportStatus("n1", task_id, g(1), c.REPORT_RUNNING))
    w.tombstones["n1"] = [task_id]                    # stopped and buried; nothing runs it now
    assert w.step(10.0) == [CancelTask(task_id, "destroyed elsewhere: tombstone on n1")]
    w.table.apply(Cancel(task_id))
    w.runs("n2", task_id, 1, role=c.ROLE_HELPER)      # a helper still running it is stopped
    assert stops(w.step(11.0)) == [("n2", task_id, c.STOP_CANCEL)]
