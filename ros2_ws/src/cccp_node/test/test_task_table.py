import dataclasses
import random

import pytest

from cccp_node.core import constants as c
from cccp_node.core.task_spec import TaskSpec
from cccp_node.core.task_table import (
    Adopt, Assign, Cancel, MarkFailed, Prune, ReportStatus, Requeue, Submit, TaskRecord, TaskTable,
)


TERM = 3          # the term of the leader applying commands in these tests
G0, G1, G2 = (0, 0), (1, TERM), (2, TERM)


def spec(**kw):
    return TaskSpec(task_type="PKCI", **kw)


def submitted(table, key="k1", task_id=None, **kw):
    return table.apply(Submit(task_id or "t-" + key, key, spec(**kw))).task_id


def scheduled(table, primary="n1", helpers=(), key="k1", **kw):
    task_id = submitted(table, key, helper_count=len(helpers), mode="redundant" if helpers else "local", **kw)
    assert table.apply(Assign(task_id, G0, TERM, primary, tuple(helpers))).ok
    return task_id


def adopt(task_id="t-a", key="ka", generation=(1, 2), primary="n1", helpers=(), cancelled=False, **kw):
    return Adopt(task_id, key, "", spec(**kw), generation, primary, tuple(helpers), "n1", cancelled)


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

def test_assign_advances_generation_and_rejects_second_decision_from_same_view():
    table = TaskTable()
    task_id = submitted(table)
    assert table.apply(Assign(task_id, G0, TERM, "n1")).ok
    record = table.get(task_id)
    assert (record.status, record.generation, record.primary) == (c.SCHEDULED, G1, "n1")
    late = table.apply(Assign(task_id, G0, TERM, "n2"))
    assert late.error_code == c.STALE_EPOCH and table.get(task_id).primary == "n1"
    assert table.apply(Assign(task_id, (1, TERM - 1), TERM, "n2")).error_code == c.STALE_EPOCH   # term counts too


def test_reschedule_fences_reports_from_previous_generation():
    table = TaskTable()
    task_id = scheduled(table, "n1")
    assert table.apply(Assign(task_id, G1, TERM, "n2")).ok
    assert table.apply(ReportStatus("n1", task_id, G1, c.REPORT_RUNNING)).error_code == c.STALE_EPOCH
    assert table.get(task_id).status == c.SCHEDULED and table.get(task_id).generation == G2


def test_report_from_the_same_epoch_of_an_older_term_is_stale():
    """A copy another leader placed at (2, TERM - 1) must not drive the task now at (2, TERM)."""
    table = TaskTable()
    table.apply(adopt(generation=(1, TERM - 1)))
    assert table.apply(Assign("t-a", (1, TERM - 1), TERM, "n2")).ok          # now at (2, TERM) on n2
    stale = table.apply(ReportStatus("n2", "t-a", (2, TERM - 1), c.REPORT_FAILED, c.INFERENCE_ERROR))
    assert stale.error_code == c.STALE_EPOCH and table.get("t-a").status == c.SCHEDULED


def test_two_leaders_rescheduling_the_same_task_produce_different_generations():
    """0.2 gave both copies epoch 2; with assign_term the later leader's copy is the newer one."""
    tables = {term: TaskTable() for term in (4, 5)}
    for term, table in tables.items():
        table.apply(adopt("t", "k", (1, 3), "n1"))                 # both adopted the same running task
        assert table.apply(Assign("t", (1, 3), term, "n%d" % term)).ok
    assert [t.get("t").generation for t in tables.values()] == [(2, 4), (2, 5)]
    merged = tables[4]                                             # term 5's copy is adopted over term 4's
    assert merged.apply(adopt("t", "k", (2, 5), "n5")).events[0].node == "n5"
    assert tables[5].apply(adopt("t", "k", (2, 4), "n4")).events == ()       # and never the other way round


@pytest.mark.parametrize("primary, helpers", [("", ()), ("n1", ("n1",)), ("n1", ("n2", "n2")), ("n1", ("",))])
def test_assign_rejects_malformed_placement(primary, helpers):
    table = TaskTable()
    task_id = submitted(table, mode="redundant", helper_count=2)
    assert table.apply(Assign(task_id, G0, TERM, primary, helpers)).error_code == c.INVALID_INPUT


def test_assign_cannot_exceed_helper_count_and_needs_a_live_task():
    table = TaskTable()
    local = submitted(table, "a")
    assert table.apply(Assign(local, G0, TERM, "n1", ("n2",))).error_code == c.INVALID_INPUT
    assert table.apply(Assign("missing", G0, TERM, "n1")).error_code == c.NOT_FOUND
    table.apply(Cancel(local))
    assert table.apply(Assign(local, G0, TERM, "n1")).error_code == c.INVALID_STATE


# -------------------------------------------------------------------- reports

def test_primary_reports_drive_the_lifecycle():
    table = TaskTable()
    task_id = scheduled(table)
    assert table.apply(ReportStatus("n1", task_id, G1, c.REPORT_RUNNING)).events[0].status == c.RUNNING
    assert table.apply(ReportStatus("n1", task_id, G1, c.REPORT_FINISHED)).events[0].status == c.FINISHED


def test_duplicate_out_of_order_helper_and_stopped_reports_change_nothing():
    table = TaskTable()
    task_id = scheduled(table, "n1", ("n2",))
    table.apply(ReportStatus("n1", task_id, G1, c.REPORT_RUNNING))
    assert table.apply(ReportStatus("n1", task_id, G1, c.REPORT_RUNNING)).events == ()
    assert table.apply(ReportStatus("n2", task_id, G1, c.REPORT_FAILED)).events == ()
    assert table.apply(ReportStatus("n1", task_id, G1, c.REPORT_STOPPED)).events == ()
    table.apply(ReportStatus("n1", task_id, G1, c.REPORT_FAILED, c.INFERENCE_ERROR, "boom"))
    assert table.apply(ReportStatus("n1", task_id, G1, c.REPORT_RUNNING)).events == ()
    assert table.get(task_id).status == c.FAILED and table.get(task_id).error_code == c.INFERENCE_ERROR


def test_report_validation():
    table = TaskTable()
    task_id = scheduled(table)
    assert table.apply(ReportStatus("n1", "nope", G1, c.REPORT_RUNNING)).error_code == c.NOT_FOUND
    assert table.apply(ReportStatus("n1", task_id, G2, c.REPORT_RUNNING)).error_code == c.INVALID_STATE
    assert table.apply(ReportStatus("n1", task_id, (1, TERM + 1), c.REPORT_RUNNING)).error_code == c.INVALID_STATE
    assert table.apply(ReportStatus("n1", task_id, G1, "Exploded")).error_code == c.INVALID_INPUT


# ---------------------------------------------------------------------- adopt

def test_adopt_rebuilds_an_unknown_running_task():
    table = TaskTable()
    result = table.apply(adopt(helpers=("n2",), mode="redundant", helper_count=1))
    record = table.get("t-a")
    assert result.ok and result.events[0].status == c.RUNNING
    assert (record.status, record.generation, record.primary, record.helpers) == (c.RUNNING, (1, 2), "n1", ("n2",))
    assert table.find_by_key("ka").task_id == "t-a"
    again = table.apply(Submit("t-new", "ka", spec()))
    assert again.duplicate and again.task_id == "t-a"       # retries with the same key find it


def test_adopt_only_moves_forward():
    table = TaskTable()
    task_id = scheduled(table, "n1", key="ka", task_id="t-a")
    assert table.apply(adopt(task_id, generation=G1, primary="n9")).events == ()     # same generation: keep ours
    assert table.apply(adopt(task_id, generation=(1, 2), primary="n9")).events == ()  # older assign_term
    assert table.apply(adopt(task_id, generation=(1, 9), primary="n9")).ok
    assert (table.get(task_id).generation, table.get(task_id).primary) == ((1, 9), "n9")  # placed elsewhere
    table.apply(Cancel(task_id))
    assert table.apply(adopt(task_id, generation=(7, 9))).events == ()                # finished tasks stay finished


def test_adopt_keeps_the_reported_assign_term():
    """Adopting is claiming: a new assign_term would make executors restart the session."""
    table = TaskTable()
    table.apply(adopt(generation=(4, 1)))
    assert table.get("t-a").generation == (4, 1)
    assert table.apply(Assign("t-a", (4, 1), TERM, "n2")).ok and table.get("t-a").generation == (5, TERM)


def test_adopt_with_a_tombstone_records_the_task_as_cancelled():
    table = TaskTable()
    result = table.apply(adopt(cancelled=True))
    record = table.get("t-a")
    assert (record.status, record.error_code, record.generation) == (c.CANCELLED, c.CANCELLED_ERROR, (1, 2))
    assert result.events[0].status == c.CANCELLED
    assert table.apply(Submit("t-retry", "ka", spec())).task_id == "t-a"     # a retried create finds it


def test_adopt_refuses_a_second_task_with_the_same_key():
    table = TaskTable()
    submitted(table, key="k", task_id="t-1")
    result = table.apply(adopt("t-2", key="k"))
    assert not result.ok and result.error_code == c.INVALID_STATE and table.get("t-2") is None


# ------------------------------------------------------- requeue / fail / cancel

def test_requeue_mark_failed_and_cancel():
    table = TaskTable()
    task_id = scheduled(table)
    assert table.apply(Requeue(task_id, G1, TERM + 1, "n1 offline")).ok
    record = table.get(task_id)
    assert (record.status, record.generation, record.primary) == (c.PENDING, (2, TERM + 1), "")
    assert table.apply(MarkFailed(task_id, G1, c.DISPATCH_FAILED)).error_code == c.STALE_EPOCH
    assert table.apply(MarkFailed(task_id, (2, TERM + 1), "")).error_code == c.INVALID_INPUT
    other = submitted(table, "k2")
    assert table.apply(Cancel(other, "user")).events[0].status == c.CANCELLED
    assert table.apply(Cancel(other)).error_code == c.INVALID_STATE
    assert table.apply(Cancel("nope")).error_code == c.NOT_FOUND


def test_pending_tasks_come_by_priority_then_submission_order():
    table = TaskTable()
    for key, priority in (("a", 0), ("b", 100), ("c", 0), ("d", 200), ("e", 100)):
        submitted(table, key, priority=priority)
    table.apply(Assign("t-d", G0, TERM, "n1"))                     # no longer pending
    table.apply(Requeue("t-d", G1, TERM))                          # back in line, at its original place
    assert [r.task_id for r in table.pending_tasks()] == ["t-d", "t-b", "t-e", "t-a", "t-c"]


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
        assign_term = (record.assign_term if record else 0) + rng.choice([0, 0, 0, -1, 1])
        generation = (epoch, assign_term)
        term = rng.randrange(1, 4)                       # leaders of different terms interleave
        node = record.primary if record and record.primary and rng.random() < 0.7 else rng.choice(nodes)
        kind = rng.randrange(8)
        if kind == 0:
            key = "k%d" % rng.randrange(30)
            yield Submit("t-%s-%d" % (key, rng.randrange(3)), key, spec(mode="redundant", helper_count=1))
        elif kind == 1:
            yield Assign(task_id, generation, term, rng.choice(nodes), tuple(rng.sample(nodes, rng.randrange(2))))
        elif kind == 2:
            yield Requeue(task_id, generation, term, "requeue")
        elif kind == 3:
            yield ReportStatus(node, task_id, generation, rng.choice(
                [c.REPORT_RUNNING, c.REPORT_RUNNING, c.REPORT_FINISHED, c.REPORT_FAILED, c.REPORT_STOPPED]))
        elif kind == 4:
            yield MarkFailed(task_id, generation, c.DISPATCH_FAILED)
        elif kind == 5:
            yield Cancel(task_id)
        elif kind == 6:
            yield Adopt(task_id, "k%d" % rng.randrange(30), "", spec(mode="redundant", helper_count=1),
                        (max(epoch, 1), max(assign_term, 1)), node, cancelled=rng.random() < 0.1)
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
            assert current.generation >= record.generation
            if record.terminal:
                assert current == record
        keys = [r.idempotency_key for r in table.tasks()]
        assert len(keys) == len(set(keys))                # one task per idempotency key
        previous = {r.task_id: r for r in table.tasks()}


# --------------------------------------------------------------------- result_url

@pytest.mark.parametrize("task_type, status, has_url", [
    ("RSSMAP", c.FINISHED, True), ("FASTLIVO", c.FINISHED, True), ("HETEROMAP", c.FINISHED, True),
    ("RSSMAP", c.RUNNING, False), ("RSSMAP", c.FAILED, False), ("PKCI", c.FINISHED, False),
])
def test_only_finished_batch_jobs_have_a_result_url(task_type, status, has_url):
    record = TaskRecord("t-1", "k", TaskSpec(task_type=task_type), "t", status=status, epoch=2, assign_term=3)
    assert record.result_url == ("/api/ue/tasks/t-1/artifacts/2-3" if has_url else "")
