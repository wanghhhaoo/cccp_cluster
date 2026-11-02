import dataclasses
import random

import pytest

from cccp_node.core import constants as c
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import (
    Adopt, Assign, Cancel, MarkFailed, Prune, ReportStatus, Requeue, Submit, TaskTable,
)


def spec(**kw):
    return TaskSpec(task_type="PKCI", **kw)


def submitted(table, key="k1", task_id=None, **kw):
    return table.apply(Submit(task_id or "t-" + key, key, spec(**kw))).task_id


def scheduled(table, primary="n1", helpers=(), key="k1", **kw):
    task_id = submitted(table, key, helper_count=len(helpers), mode="redundant" if helpers else "local", **kw)
    assert table.apply(Assign(task_id, 0, primary, tuple(helpers))).ok
    return task_id


def adopt(task_id="t-a", key="ka", epoch=1, primary="n1", helpers=(), **kw):
    return Adopt(task_id, key, "", spec(**kw), epoch, primary, tuple(helpers), "n1")


# --------------------------------------------------------------------- submit

def test_submit_uses_given_id_and_names_tasks():
    table = TaskTable()
    first = table.apply(Submit("3f2a9c4e-0000", "a", spec(), now_ms=5))
    second = table.apply(Submit("b", "b", spec(name="mine")))
    assert (first.task_id, second.task_id) == ("3f2a9c4e-0000", "b")
    assert table.get("3f2a9c4e-0000").name == "pkci-3f2a9c4e" and table.get("b").name == "mine"
    assert table.get("b").status == c.PENDING and table.get("b").epoch == 0
    assert [e.seq for e in first.events + second.events] == [1, 2]


def test_same_idempotency_key_returns_existing_task_without_event():
    table = TaskTable()
    first = table.apply(Submit("t1", "k", spec()))
    again = table.apply(Submit("t2", "k", spec(name="other")))
    assert again.ok and again.duplicate and again.task_id == first.task_id
    assert again.events == () and table.seq == 1 and len(table.tasks()) == 1


def test_submit_requires_id_and_key_and_rejects_reused_id():
    table = TaskTable()
    assert table.apply(Submit("", "k", spec())).error_code == c.INVALID_INPUT
    assert table.apply(Submit("t", "", spec())).error_code == c.INVALID_INPUT
    table.apply(Submit("t", "k1", spec()))
    assert table.apply(Submit("t", "k2", spec())).error_code == c.INVALID_INPUT


# --------------------------------------------------------------------- assign

def test_assign_advances_epoch_and_rejects_second_decision_from_same_view():
    table = TaskTable()
    task_id = submitted(table)
    assert table.apply(Assign(task_id, 0, "n1")).ok
    record = table.get(task_id)
    assert (record.status, record.epoch, record.primary) == (c.SCHEDULED, 1, "n1")
    late = table.apply(Assign(task_id, 0, "n2"))
    assert late.error_code == c.STALE_EPOCH and table.get(task_id).primary == "n1"


def test_reschedule_fences_reports_from_previous_epoch():
    table = TaskTable()
    task_id = scheduled(table, "n1")
    assert table.apply(Assign(task_id, 1, "n2")).ok
    assert table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING)).error_code == c.STALE_EPOCH
    assert table.get(task_id).status == c.SCHEDULED and table.get(task_id).epoch == 2


@pytest.mark.parametrize("primary, helpers", [("", ()), ("n1", ("n1",)), ("n1", ("n2", "n2")), ("n1", ("",))])
def test_assign_rejects_malformed_placement(primary, helpers):
    table = TaskTable()
    task_id = submitted(table, mode="redundant", helper_count=2)
    assert table.apply(Assign(task_id, 0, primary, helpers)).error_code == c.INVALID_INPUT


def test_assign_cannot_exceed_helper_count_and_needs_a_live_task():
    table = TaskTable()
    local = submitted(table, "a")
    assert table.apply(Assign(local, 0, "n1", ("n2",))).error_code == c.INVALID_INPUT
    assert table.apply(Assign("missing", 0, "n1")).error_code == c.NOT_FOUND
    table.apply(Cancel(local))
    assert table.apply(Assign(local, 0, "n1")).error_code == c.INVALID_STATE


# -------------------------------------------------------------------- reports

def test_primary_reports_drive_the_lifecycle():
    table = TaskTable()
    task_id = scheduled(table)
    assert table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING)).events[0].status == c.RUNNING
    assert table.apply(ReportStatus("n1", task_id, 1, c.REPORT_FINISHED)).events[0].status == c.FINISHED


def test_duplicate_out_of_order_helper_and_stopped_reports_change_nothing():
    table = TaskTable()
    task_id = scheduled(table, "n1", ("n2",))
    table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING))
    assert table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING)).events == ()
    assert table.apply(ReportStatus("n2", task_id, 1, c.REPORT_FAILED)).events == ()
    assert table.apply(ReportStatus("n1", task_id, 1, c.REPORT_STOPPED)).events == ()
    table.apply(ReportStatus("n1", task_id, 1, c.REPORT_FAILED, c.INFERENCE_ERROR, "boom"))
    assert table.apply(ReportStatus("n1", task_id, 1, c.REPORT_RUNNING)).events == ()
    assert table.get(task_id).status == c.FAILED and table.get(task_id).error_code == c.INFERENCE_ERROR


def test_report_validation():
    table = TaskTable()
    task_id = scheduled(table)
    assert table.apply(ReportStatus("n1", "nope", 1, c.REPORT_RUNNING)).error_code == c.NOT_FOUND
    assert table.apply(ReportStatus("n1", task_id, 2, c.REPORT_RUNNING)).error_code == c.INVALID_STATE
    assert table.apply(ReportStatus("n1", task_id, 1, "Exploded")).error_code == c.INVALID_INPUT


# ---------------------------------------------------------------------- adopt

def test_adopt_rebuilds_an_unknown_running_task():
    table = TaskTable()
    result = table.apply(adopt(helpers=("n2",), mode="redundant", helper_count=1))
    record = table.get("t-a")
    assert result.ok and result.events[0].status == c.RUNNING
    assert (record.status, record.epoch, record.primary, record.helpers) == (c.RUNNING, 1, "n1", ("n2",))
    assert table.find_by_key("ka").task_id == "t-a"
    again = table.apply(Submit("t-new", "ka", spec()))
    assert again.duplicate and again.task_id == "t-a"       # retries with the same key find it


def test_adopt_only_moves_forward():
    table = TaskTable()
    task_id = scheduled(table, "n1", key="ka", task_id="t-a")
    assert table.apply(adopt(task_id, epoch=1, primary="n9")).events == ()        # same epoch: keep ours
    assert table.apply(adopt(task_id, epoch=3, primary="n9")).ok
    assert (table.get(task_id).epoch, table.get(task_id).primary) == (3, "n9")  # a newer placement elsewhere
    table.apply(Cancel(task_id))
    assert table.apply(adopt(task_id, epoch=7)).events == ()                      # finished tasks stay finished


def test_adopt_refuses_a_second_task_with_the_same_key():
    table = TaskTable()
    submitted(table, key="k", task_id="t-1")
    result = table.apply(adopt("t-2", key="k"))
    assert not result.ok and result.error_code == c.INVALID_STATE and table.get("t-2") is None


# ------------------------------------------------------- requeue / fail / cancel

def test_requeue_mark_failed_and_cancel():
    table = TaskTable()
    task_id = scheduled(table)
    assert table.apply(Requeue(task_id, 1, "n1 offline")).ok
    record = table.get(task_id)
    assert (record.status, record.epoch, record.primary) == (c.PENDING, 2, "")
    assert table.apply(MarkFailed(task_id, 1, c.DISPATCH_FAILED)).error_code == c.STALE_EPOCH
    assert table.apply(MarkFailed(task_id, 2, "")).error_code == c.INVALID_INPUT
    other = submitted(table, "k2")
    assert table.apply(Cancel(other, "user")).events[0].status == c.CANCELLED
    assert table.apply(Cancel(other)).error_code == c.INVALID_STATE
    assert table.apply(Cancel("nope")).error_code == c.NOT_FOUND


def test_prune_forgets_only_terminal_tasks_and_their_keys():
    table = TaskTable()
    done = submitted(table, "done")
    live = submitted(table, "live")
    table.apply(Cancel(done))
    assert table.apply(Prune((done, live, "nope"))).message == "pruned 1"
    assert table.get(done) is None and table.get(live) is not None
    assert not table.apply(Submit("t-again", "done", spec())).duplicate


# ----------------------------------------------------------------- invariants

def test_records_are_immutable():
    table = TaskTable()
    record = table.get(submitted(table))
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.status = c.RUNNING


def random_commands(rng, table, steps):
    """Mostly sensible commands mixed with stale, duplicate and bogus ones."""
    nodes = ["n1", "n2", "n3"]
    for _ in range(steps):
        task_id = rng.choice([r.task_id for r in table.tasks()] + ["missing"])
        record = table.get(task_id)
        epoch = (record.epoch if record else 0) + rng.choice([0, 0, 0, -1, 1, 2])
        node = record.primary if record and record.primary and rng.random() < 0.7 else rng.choice(nodes)
        kind = rng.randrange(8)
        if kind == 0:
            key = "k%d" % rng.randrange(30)
            yield Submit("t-%s-%d" % (key, rng.randrange(3)), key, spec(mode="redundant", helper_count=1))
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
        elif kind == 6:
            yield Adopt(task_id, "k%d" % rng.randrange(30), "", spec(mode="redundant", helper_count=1),
                        max(epoch, 1), node)
        else:
            yield Prune((task_id,))


def test_random_histories_never_move_tasks_backwards():
    rng = random.Random(20261028)
    table = TaskTable()
    previous = {}
    for command in random_commands(rng, table, 5000):
        result = table.apply(command)
        for i, event in enumerate(result.events):
            assert event.seq == table.seq - len(result.events) + i + 1
        for task_id, record in list(previous.items()):
            current = table.get(task_id)
            if current is None:
                assert record.terminal                    # only finished tasks are pruned
                continue
            assert current.order >= record.order
            if record.terminal:
                assert current == record
        keys = [r.idempotency_key for r in table.tasks()]
        assert len(keys) == len(set(keys))                # one task per idempotency key
        previous = {r.task_id: r for r in table.tasks()}
