from cccp_node.core import constants as c
from cccp_node.core import idempotency
from cccp_node.core.execution_guard import IGNORE, NOOP, REJECT, RESTART, START, STOP, ExecutionGuard


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_start_rules():
    guard = ExecutionGuard()
    assert guard.on_start(1, "t", (1, 1)).action == START
    guard.started("t", (1, 1))
    duplicate = guard.on_start(1, "t", (1, 1))
    assert duplicate.action == NOOP and duplicate.success          # resend after leader change
    assert guard.on_start(2, "t", (2, 1)).action == RESTART
    guard.started("t", (2, 1))
    stale = guard.on_start(2, "t", (1, 2))
    assert stale.action == REJECT and stale.error_code == c.STALE_EPOCH


def test_same_epoch_from_a_later_term_is_a_newer_session():
    guard = ExecutionGuard()
    guard.started("t", (2, 4))
    assert guard.on_start(5, "t", (2, 5)).action == RESTART
    guard.started("t", (2, 5))
    assert guard.on_start(5, "t", (2, 4)).error_code == c.STALE_EPOCH


def test_commands_for_other_nodes_are_ignored_before_any_check():
    guard = ExecutionGuard(me=3)
    ignored = guard.on_start(9, "t", (1, 9), targets=[1, 2])
    assert ignored.action == IGNORE and not ignored.success
    assert guard.max_term == 0                                     # not even the term is taken in
    assert guard.on_stop(9, "t", (1, 9), cancel=True, targets=[1]).action == IGNORE
    assert guard.tombstones() == ()
    assert guard.on_start(9, "t", (1, 9), targets=[1, 3]).action == START


def test_old_leader_is_fenced_by_term():
    guard = ExecutionGuard()
    guard.on_start(5, "a", (1, 5))
    rejected = guard.on_start(4, "b", (1, 4))
    assert rejected.error_code == c.STALE_TERM and not rejected.success
    assert guard.on_stop(4, "a", (1, 5)).error_code == c.STALE_TERM
    assert guard.max_term == 5


def test_admission_counts_what_actually_runs():
    guard = ExecutionGuard(max_tasks=2)
    guard.started("a", (1, 1))
    guard.started("b", (1, 1))
    busy = guard.on_start(1, "c", (1, 1))
    assert busy.action == REJECT and busy.error_code == c.BUSY
    assert guard.on_start(1, "a", (1, 1)).action == NOOP           # a resend is not a new task
    assert guard.on_start(1, "a", (2, 1)).action == RESTART        # nor is a newer session of one
    guard.stopped("b")
    assert guard.on_start(1, "c", (1, 1)).action == START


def test_stop_rules():
    guard = ExecutionGuard()
    assert guard.on_stop(1, "t", (1, 1)).action == NOOP            # not running: idempotent success
    guard.started("t", (3, 1))
    newer = guard.on_stop(1, "t", (2, 1))                          # the running session is newer
    assert newer.action == REJECT and newer.error_code == c.STALE_EPOCH
    assert guard.on_stop(1, "t", (3, 1)).action == STOP
    assert guard.on_stop(1, "t", (4, 1)).action == STOP            # an older session than the command's
    assert guard.on_stop(1, "t", (0, 0)).action == STOP            # (0, 0) = any generation
    guard.stopped("t")
    assert guard.running() == {}


def test_cancel_leaves_a_tombstone_whether_or_not_the_task_ran():
    guard = ExecutionGuard()
    guard.started("ran", (1, 1))
    assert guard.on_stop(1, "ran", (1, 1), cancel=True).action == STOP
    assert guard.on_stop(1, "never", (1, 1), cancel=True).action == NOOP
    assert guard.on_stop(1, "moved", (1, 1)).action == NOOP        # a plain stop leaves none
    assert guard.tombstones() == ("ran", "never")


def test_refused_cancel_leaves_no_tombstone():
    guard = ExecutionGuard()
    guard.started("t", (3, 2))
    assert guard.on_stop(2, "t", (2, 2), cancel=True).error_code == c.STALE_EPOCH
    assert guard.on_stop(1, "t", (3, 2), cancel=True).error_code == c.STALE_TERM
    assert guard.tombstones() == ()


def test_tombstones_expire_and_are_capped():
    clock = Clock()
    guard = ExecutionGuard(tombstone_ttl_s=600.0, max_tombstones=3, clock=clock)
    for i in range(4):
        clock.now = float(i)
        guard.on_stop(1, "t%d" % i, (1, 1), cancel=True)
    assert guard.tombstones() == ("t1", "t2", "t3")                # the oldest made room
    clock.now = 601.5
    assert guard.tombstones() == ("t2", "t3")
    clock.now = 602.0
    guard.on_stop(1, "t2", (1, 1), cancel=True)                    # cancelled again: kept longer
    clock.now = 604.0
    assert guard.tombstones() == ("t2",)


def test_idempotency_keys():
    assert idempotency.manual("ue", "u-1") == "manual:ue:u-1"
    assert idempotency.schedule("rule", 1700000000000) == "schedule:rule:1700000000000"
    assert idempotency.chain(4, "fuse") == "chain:4:fuse"
    assert idempotency.chain(4, "fuse", 12) == "chain:4:fuse:12"
    assert idempotency.event("edge-01", "radar", 3) == "event:edge-01:radar:3"
    assert idempotency.anonymous("x") == "anon:x"
