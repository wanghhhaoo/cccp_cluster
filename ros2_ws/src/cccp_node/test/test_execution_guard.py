from cccp_node.core import constants as c
from cccp_node.core import idempotency
from cccp_node.core.execution_guard import NOOP, REJECT, RESTART, START, STOP, ExecutionGuard


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


def test_old_leader_is_fenced_by_term():
    guard = ExecutionGuard()
    guard.on_start(5, "a", (1, 5))
    rejected = guard.on_start(4, "b", (1, 4))
    assert rejected.error_code == c.STALE_TERM and not rejected.success
    assert guard.on_stop(4, "a", (1, 5)).error_code == c.STALE_TERM
    assert guard.max_term == 5


def test_stop_rules():
    guard = ExecutionGuard()
    assert guard.on_stop(1, "t", (1, 1)).action == NOOP            # not running: idempotent success
    guard.started("t", (3, 1))
    assert guard.on_stop(1, "t", (2, 1)).action == NOOP            # older than the running one
    assert guard.on_stop(1, "t", (3, 1)).action == STOP
    assert guard.on_stop(1, "t", (0, 0)).action == STOP            # (0, 0) = any generation
    guard.stopped("t")
    assert guard.running() == {}


def test_idempotency_keys():
    assert idempotency.manual("ue", "u-1") == "manual:ue:u-1"
    assert idempotency.schedule("rule", 1700000000000) == "schedule:rule:1700000000000"
    assert idempotency.chain(4, "fuse") == "chain:4:fuse"
    assert idempotency.chain(4, "fuse", 12) == "chain:4:fuse:12"
    assert idempotency.event("edge-01", "radar", 3) == "event:edge-01:radar:3"
    assert idempotency.anonymous("x") == "anon:x"
