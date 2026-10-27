from cccp_node.core import constants as c
from cccp_node.core import idempotency
from cccp_node.core.execution_guard import NOOP, REJECT, RESTART, START, STOP, ExecutionGuard


def test_start_rules():
    guard = ExecutionGuard()
    assert guard.on_start(1, 7, 1).action == START
    guard.started(7, 1)
    duplicate = guard.on_start(1, 7, 1)
    assert duplicate.action == NOOP and duplicate.success          # resend after leader change
    assert guard.on_start(2, 7, 2).action == RESTART
    guard.started(7, 2)
    stale = guard.on_start(2, 7, 1)
    assert stale.action == REJECT and stale.error_code == c.STALE_EPOCH


def test_old_leader_is_fenced_by_term():
    guard = ExecutionGuard()
    guard.on_start(5, 1, 1)
    rejected = guard.on_start(4, 2, 1)
    assert rejected.error_code == c.STALE_TERM and not rejected.success
    assert guard.on_stop(4, 1, 1).error_code == c.STALE_TERM
    assert guard.max_term == 5


def test_stop_rules():
    guard = ExecutionGuard()
    assert guard.on_stop(1, 7, 1).action == NOOP                   # not running: idempotent success
    guard.started(7, 3)
    assert guard.on_stop(1, 7, 2).action == NOOP                   # older epoch than the running one
    assert guard.on_stop(1, 7, 3).action == STOP
    assert guard.on_stop(1, 7, 0).action == STOP                   # 0 = any epoch
    guard.stopped(7)
    assert guard.running() == {}


def test_idempotency_keys():
    assert idempotency.manual("ue", "u-1") == "manual:ue:u-1"
    assert idempotency.schedule("rule", 1700000000000) == "schedule:rule:1700000000000"
    assert idempotency.chain(4, "fuse") == "chain:4:fuse"
    assert idempotency.chain(4, "fuse", 12) == "chain:4:fuse:12"
    assert idempotency.event("edge-01", "radar", 3) == "event:edge-01:radar:3"
    assert idempotency.anonymous("x") == "anon:x"
