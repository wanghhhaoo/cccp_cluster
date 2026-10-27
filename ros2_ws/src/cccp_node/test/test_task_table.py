import dataclasses
import random

import pytest

from cccp_node.core import constants as c
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import (
    Assign, Barrier, Cancel, MarkFailed, Prune, ReportStatus, Requeue, Submit, TaskTable,
    command_from_dict, command_to_dict,
)


def spec(**kw):
    return TaskSpec(task_type="PKCI", **kw)


def submitted(table, key="k1", **kw):
    return table.apply(Submit(key, spec(**kw))).task_id


def scheduled(table, primary="n1", helpers=(), **kw):
    task_id = submitted(table, helper_count=len(helpers), mode="redundant" if helpers else "local", **kw)
    assert table.apply(Assign(task_id, 0, primary, tuple(helpers))).ok
    return task_id


# --------------------------------------------------------------------- submit

def test_submit_allocates_sequential_ids_and_names():
    table = TaskTable()
    first = table.apply(Submit("a", spec(), now_ms=5))
    second = table.apply(Submit("b", spec(name="mine")))
    assert (first.task_id, second.task_id) == (1, 2)
    assert table.get(1).name == "pkci-1" and table.get(2).name == "mine"
    assert table.get(1).status == c.PENDING and table.get(1).epoch == 0
    assert [e.seq for e in first.events + second.events] == [1, 2]


def test_same_idempotency_key_returns_existing_task_without_event():
    table = TaskTable()
    first = table.apply(Submit("k", spec()))
    again = table.apply(Submit("k", spec(name="other")))
    assert again.ok and again.duplicate and again.task_id == first.task_id
    assert again.events == () and table.seq == 1 and len(table.tasks()) == 1


def test_submit_requires_idempotency_key():
    result = TaskTable().apply(Submit("", spec()))
    assert not result.ok and result.error_code == c.INVALID_INPUT


# --------------------------------------------------------------------- assign

def test_assign_advances_epoch_and_rejects_second_decision_from_same_snapshot():
    table = TaskTable()
    task_id = submitted(table)
    assert table.apply(Assign(task_id, 0, "n1")).ok
    record = table.get(task_id)
    assert (record.status, record.epoch, record.primary) == (c.SCHEDULED, 1, "n1")
    late = table.apply(Assign(task_id, 0, "n2"))   # decided before seeing epoch 1
    assert late.error_code == c.STALE_EPOCH and table.get(task_id).primary == "n1"


def test_reschedule_fences_reports_from_previous_epoch():
    table = TaskTable()
    task_id = scheduled(table, "n1")
    assert table.apply(Assign(task_id, 1, "n2")).ok
    stale = table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING))
    assert stale.error_code == c.STALE_EPOCH
    assert table.get(task_id).status == c.SCHEDULED and table.get(task_id).epoch == 2


@pytest.mark.parametrize("primary, helpers", [("", ()), ("n1", ("n1",)), ("n1", ("n2", "n2")), ("n1", ("",))])
def test_assign_rejects_malformed_placement(primary, helpers):
    table = TaskTable()
    task_id = submitted(table, mode="redundant", helper_count=2)
    result = table.apply(Assign(task_id, 0, primary, helpers))
    assert result.error_code == c.INVALID_INPUT and table.get(task_id).epoch == 0


def test_assign_cannot_exceed_helper_count():
    table = TaskTable()
    local = submitted(table, "a")
    assert table.apply(Assign(local, 0, "n1", ("n2",))).error_code == c.INVALID_INPUT
    pair = submitted(table, "b", mode="fusion", helper_count=1)
    assert table.apply(Assign(pair, 0, "n1", ("n2", "n3"))).error_code == c.INVALID_INPUT
    assert table.apply(Assign(pair, 0, "n1", ("n2",))).ok


def test_unknown_and_finished_tasks_cannot_be_assigned():
    table = TaskTable()
    assert table.apply(Assign(42, 0, "n1")).error_code == c.NOT_FOUND
    task_id = submitted(table)
    table.apply(Cancel(task_id))
    assert table.apply(Assign(task_id, 0, "n1")).error_code == c.INVALID_STATE


# -------------------------------------------------------------------- reports

def test_primary_reports_drive_the_lifecycle():
    table = TaskTable()
    task_id = scheduled(table)
    assert table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING)).events[0].status == c.RUNNING
    done = table.apply(ReportStatus("n1", task_id, 1, c.REPORT_FINISHED))
    assert done.events[0].status == c.FINISHED and table.get(task_id).terminal


def test_duplicate_and_out_of_order_reports_change_nothing():
    table = TaskTable()
    task_id = scheduled(table)
    table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING))
    seq = table.seq
    assert table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING)).events == ()
    table.apply(ReportStatus("n1", task_id, 1, c.REPORT_FAILED, c.INFERENCE_ERROR, "boom"))
    late = table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING))
    assert late.ok and late.events == ()
    record = table.get(task_id)
    assert record.status == c.FAILED and record.error_code == c.INFERENCE_ERROR
    assert table.seq == seq + 1


def test_finished_before_running_is_accepted():
    table = TaskTable()
    task_id = scheduled(table)
    table.apply(ReportStatus("n1", task_id, 1, c.REPORT_FINISHED))
    table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING))
    assert table.get(task_id).status == c.FINISHED


def test_helper_and_stopped_reports_do_not_change_state():
    table = TaskTable()
    task_id = scheduled(table, "n1", ("n2",))
    assert table.apply(ReportStatus("n2", task_id, 1, c.REPORT_FAILED)).events == ()
    assert table.apply(ReportStatus("n1", task_id, 1, c.REPORT_STOPPED)).events == ()
    assert table.apply(ReportStatus("n9", task_id, 1, c.REPORT_RUNNING)).events == ()
    assert table.get(task_id).status == c.SCHEDULED


def test_report_validation():
    table = TaskTable()
    task_id = scheduled(table)
    assert table.apply(ReportStatus("n1", 99, 1, c.REPORT_RUNNING)).error_code == c.NOT_FOUND
    assert table.apply(ReportStatus("n1", task_id, 2, c.REPORT_RUNNING)).error_code == c.INVALID_STATE
    assert table.apply(ReportStatus("n1", task_id, 1, "Exploded")).error_code == c.INVALID_INPUT


# ------------------------------------------------------- requeue / fail / cancel

def test_requeue_returns_to_pending_with_new_epoch():
    table = TaskTable()
    task_id = scheduled(table)
    table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING))
    assert table.apply(Requeue(task_id, 1, "n1 offline")).ok
    record = table.get(task_id)
    assert (record.status, record.epoch, record.primary, record.last_event) == (c.PENDING, 2, "", "n1 offline")
    assert table.apply(Assign(task_id, 2, "n3")).ok and table.get(task_id).epoch == 3


def test_mark_failed_uses_epoch_check_and_needs_a_code():
    table = TaskTable()
    task_id = scheduled(table)
    assert table.apply(MarkFailed(task_id, 0, c.DISPATCH_FAILED)).error_code == c.STALE_EPOCH
    assert table.apply(MarkFailed(task_id, 1, "")).error_code == c.INVALID_INPUT
    assert table.apply(MarkFailed(task_id, 1, c.DISPATCH_FAILED, "n1 unreachable")).ok
    record = table.get(task_id)
    assert (record.status, record.error_code, record.error_message) == (c.FAILED, c.DISPATCH_FAILED, "n1 unreachable")


def test_cancel_once_then_invalid_state():
    table = TaskTable()
    task_id = scheduled(table)
    assert table.apply(Cancel(task_id, "user")).events[0].status == c.CANCELLED
    assert table.apply(Cancel(task_id)).error_code == c.INVALID_STATE
    assert table.apply(Cancel(77)).error_code == c.NOT_FOUND


def test_prune_forgets_only_terminal_tasks_and_their_keys():
    table = TaskTable()
    done = submitted(table, "done")
    live = submitted(table, "live")
    table.apply(Cancel(done))
    assert table.apply(Prune((done, live, 123))).message == "pruned 1"
    assert table.get(done) is None and table.get(live) is not None
    resubmitted = table.apply(Submit("done", spec()))
    assert not resubmitted.duplicate and resubmitted.task_id == 3   # ids are never reused


# ----------------------------------------------------------------- invariants

def test_records_are_immutable():
    table = TaskTable()
    record = table.get(submitted(table))
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.status = c.RUNNING


def test_commands_round_trip_through_dicts():
    commands = [
        Submit("k", spec(target_nodes=("a", "b"), mode="fusion", helper_count=1), now_ms=1),
        Assign(1, 0, "a", ("b",)), Requeue(1, 1, "x"), ReportStatus("a", 1, 1, c.REPORT_RUNNING),
        MarkFailed(1, 1, c.DISPATCH_FAILED), Cancel(1, "r"), Prune((1, 2)), Barrier(3),
    ]
    for command in commands:
        assert command_from_dict(command_to_dict(command)) == command
    table = TaskTable()
    assert table.apply(Barrier(3)).ok and table.seq == 0 and table.tasks() == []
    with pytest.raises(ValueError):
        command_from_dict({"op": "drop_table"})


def random_commands(rng, table, steps):
    """Mostly sensible commands mixed with stale, duplicate and bogus ones."""
    nodes = ["n1", "n2", "n3"]
    for _ in range(steps):
        task_id = rng.choice([r.task_id for r in table.tasks()] + [999])
        record = table.get(task_id)
        epoch = (record.epoch if record else 0) + rng.choice([0, 0, 0, -1, 1])
        node = (record.primary if record and record.primary and rng.random() < 0.7 else rng.choice(nodes))
        kind = rng.randrange(7)
        if kind == 0:
            yield Submit("k%d" % rng.randrange(30), spec(mode="redundant", helper_count=1))
        elif kind == 1:
            yield Assign(task_id, epoch, rng.choice(nodes), tuple(rng.sample(nodes, rng.randrange(2))))
        elif kind == 2:
            yield Requeue(task_id, epoch, "requeue")
        elif kind == 3:
            yield ReportStatus(node, task_id, epoch, rng.choice(
                [c.REPORT_RUNNING, c.REPORT_RUNNING, c.REPORT_FINISHED, c.REPORT_FAILED, c.REPORT_STOPPED]))
        elif kind == 4:
            yield MarkFailed(task_id, epoch, c.DISPATCH_FAILED)
        elif kind == 5:
            yield Cancel(task_id)
        else:
            yield Prune((task_id,))


def test_random_histories_keep_invariants_and_replicas_identical():
    rng = random.Random(20261027)
    table, replica = TaskTable(), TaskTable()
    previous = {}
    for command in random_commands(rng, table, 5000):
        result = table.apply(command)
        assert replica.apply(command_from_dict(command_to_dict(command))) == result
        for i, event in enumerate(result.events):
            assert event.seq == table.seq - len(result.events) + i + 1
        for task_id, record in list(previous.items()):
            current = table.get(task_id)
            if current is None:                          # pruned: must have been finished
                assert record.terminal
                del previous[task_id]
                continue
            assert current.order >= record.order         # never moves backwards
            if record.terminal:
                assert current == record                 # terminal states are final
        previous = {r.task_id: r for r in table.tasks()}
    assert replica.to_snapshot() == table.to_snapshot()
    assert any(r.status == c.FINISHED for r in table.tasks()) or table.seq > 100


def test_snapshot_restores_an_identical_table():
    rng = random.Random(3)
    table = TaskTable()
    for command in random_commands(rng, table, 800):
        table.apply(command)
    restored = TaskTable.from_snapshot(table.to_snapshot())
    assert restored.to_snapshot() == table.to_snapshot()
    for command in random_commands(random.Random(4), table, 300):
        assert restored.apply(command) == table.apply(command)
    with pytest.raises(ValueError):
        TaskTable.from_snapshot({"version": 0})
