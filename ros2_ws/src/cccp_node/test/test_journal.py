"""Event journal: entry format, in-memory window, rotating files and the writer thread."""
import json
import os
import pytest

from cccp_node.control.journal import Journal, JournalWriter, RotatingFile, recorder, snapshot_fields
from cccp_node.core.cluster_view import LinkView, NodeView, TaskStateView


def test_entries_carry_clocks_identity_and_what_the_node_follows():
    lines = []
    state = {"term": 3, "leader": "n2"}
    journal = Journal("n1", 1234, context=lambda: (state["term"], state["leader"]), sink=lines.append,
                      wall=lambda: 1700000000.12345, clock=lambda: 42.5)
    journal.record("node_offline", peer="n3", missed=2)
    state.update(term=4, leader="n1")
    journal.record("leader_changed", prev_leader="n2")
    first, second = (json.loads(line) for line in lines)
    assert first == {"t": 1700000000.123, "node": "n1", "event": "node_offline", "peer": "n3", "missed": 2,
                     "term": 3, "leader": "n2", "seq": 1, "inc": 1234, "mono": 42.5}
    assert (second["term"], second["leader"], second["seq"]) == (4, "n1", 2)
    assert all(line.endswith("\n") and line.count("\n") == 1 for line in lines)


def test_reserved_field_names_are_refused():
    with pytest.raises(AssertionError):
        Journal("n1", 1).record("node_online", node="n2")


def test_memory_window_is_bounded_and_read_by_seq():
    journal = Journal("n1", 1, capacity=3)
    for i in range(5):
        journal.record("snapshot" if i % 2 else "e%d" % i)
    entries, last = journal.since(0)
    assert last == 5 and [e["seq"] for e in entries] == [3, 4, 5]
    assert [e["seq"] for e in journal.since(3)[0]] == [4, 5]
    assert [e["event"] for e in journal.since(0, include_snapshots=False)[0]] == ["e2", "e4"]
    assert [e["seq"] for e in journal.since(0, limit=1)[0]] == [3]


def test_recorder_without_a_journal_does_nothing():
    recorder(None)("anything", x=1)
    journal = Journal("n1", 1)
    recorder(journal)("x")
    assert journal.since(0)[1] == 1


def test_rotating_file_keeps_the_newest_files(tmp_path):
    out = RotatingFile(str(tmp_path), "n1", max_bytes=10, keep=2)
    for i in range(5):
        out.write(b"line-%d...\n" % i)          # 11 bytes: each line fills a file
    out.close()
    names = sorted(os.listdir(tmp_path))
    assert names == ["n1.1.jsonl", "n1.2.jsonl", "n1.jsonl"]
    read = lambda name: (tmp_path / name).read_bytes()
    assert (read("n1.2.jsonl"), read("n1.1.jsonl"), read("n1.jsonl")) == (b"line-2...\n", b"line-3...\n",
                                                                           b"line-4...\n")


def test_rotating_file_appends_after_a_restart(tmp_path):
    first = RotatingFile(str(tmp_path), "n1", max_bytes=100, keep=1)
    first.write(b"a\n")
    first.close()
    second = RotatingFile(str(tmp_path), "n1", max_bytes=100, keep=1)
    second.write(b"b\n")
    second.close()
    assert (tmp_path / "n1.jsonl").read_bytes() == b"a\nb\n"


def test_writer_puts_every_line_on_disk_by_close(tmp_path):
    writer = JournalWriter(str(tmp_path / "journal"), "n1")
    journal = Journal("n1", 1, sink=writer.put)
    for i in range(50):
        journal.record("e", i=i)
    writer.close()
    lines = (tmp_path / "journal" / "n1.jsonl").read_text().splitlines()
    assert [json.loads(line)["i"] for line in lines] == list(range(50))
    assert writer.dropped == writer.write_errors == 0


def test_an_unwritable_directory_costs_the_file_not_the_node(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    writer = JournalWriter(str(blocker / "journal"), "n1")
    journal = Journal("n1", 1, sink=writer.put)
    journal.record("e")
    writer.close()
    assert writer.write_errors >= 1
    assert journal.since(0)[1] == 1


def test_snapshot_lists_nodes_links_and_tasks():
    online = {
        "n1": NodeView("n1", term=2, is_leader=True, inference_ready=True,
                       running_tasks=(TaskStateView("t1", 1, "primary"), TaskStateView("t2", 1, "primary"))),
        "n2": NodeView("n2", term=2, executor_alive=False),
    }
    everyone = dict(online, n3=NodeView("n3"))
    links = [LinkView("n2", rtt_ms=12.345, loss_rate=0.04321)]
    fields = snapshot_fields(online, everyone, links, ["Running", "Pending", "Running"])
    assert fields == {
        "online": ["n1", "n2"], "offline": ["n3"],
        "nodes": {"n1": {"term": 2, "leading": True, "tasks": 2, "executor": True, "ready": True},
                  "n2": {"term": 2, "leading": False, "tasks": 0, "executor": False, "ready": False}},
        "links": {"n2": {"rtt_ms": 12.3, "loss": 0.043}},
        "tasks": {"Pending": 1, "Running": 2},
    }
    assert "tasks" not in snapshot_fields(online, everyone)
    assert len(json.dumps(snapshot_fields(online, everyone, links))) < 400
