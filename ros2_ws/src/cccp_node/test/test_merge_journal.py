"""tools/merge_journal.py over journals written by control/journal.py."""
import importlib.util
import io
import json
import os

from cccp_node.control.journal import Journal

TOOL = os.path.join(os.path.dirname(__file__), *[os.pardir] * 4, "tools", "merge_journal.py")
spec = importlib.util.spec_from_file_location("merge_journal", TOOL)
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)

T0 = 1_800_000_000.0


class Board:
    """One node's journal with its own (possibly wrong) wall clock."""

    def __init__(self, name, skew=0.0):
        self.now, self.skew, self.lines = 0.0, skew, []
        self.follows = (1, "n1")
        self.journal = Journal(name, 100, context=lambda: self.follows, sink=self.lines.append,
                               wall=lambda: T0 + self.now + self.skew, clock=lambda: self.now)

    def at(self, now, event, follows=None, **fields):
        self.now = now
        if follows:
            self.follows = follows
        self.journal.record(event, **fields)

    def save(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(self.lines))


def failover(tmp_path, skew_n3=0.0):
    """n1 leads, dies at 10 s; n2 takes over at ~16 s and moves task abc to n3."""
    n1, n2, n3 = Board("n1"), Board("n2"), Board("n3", skew=skew_n3)
    for board in (n1, n2, n3):
        board.at(0.0 + 0.1 * int(board.journal.node[1]), "leader_changed", (1, "n1"), prev_leader="", prev_term=0)
    n1.at(5.0, "task_scheduled", task="abc-1", gen="(1,1)", primary="n1", helpers=[], reason="")
    n1.at(9.0, "snapshot", online=["n1", "n2", "n3"])
    n2.at(15.2, "node_offline", peer="n1", restarted=False, silent_s=5.2, missed=0)
    n3.at(15.4, "node_offline", peer="n1", restarted=False, silent_s=5.4, missed=0)
    n2.at(16.0, "leader_changed", (2, "n2"), prev_leader="n1", prev_term=1, leading=True)
    n3.at(16.9, "leader_changed", (2, "n2"), prev_leader="n1", prev_term=1, leading=False)
    n2.at(21.0, "task_reschedule", task="abc-1", gen="(1,1)", reason="primary n1 offline")
    n2.at(21.0, "task_scheduled", task="abc-1", gen="(2,2)", primary="n3", helpers=[],
          reason="primary n1 offline")
    n1.save(tmp_path / "edge-1" / "n1.jsonl")
    n2.save(tmp_path / "edge-2" / "n2.jsonl")
    n3.save(tmp_path / "edge-3" / "n3.jsonl")
    return tmp_path


def run(*argv):
    out, errors = io.StringIO(), io.StringIO()
    status = merge.main([str(a) for a in argv], out, errors)
    return status, out.getvalue().splitlines(), errors.getvalue()


def test_boards_merge_into_one_timeline(tmp_path):
    status, lines, errors = run(failover(tmp_path))
    assert status == 0 and "3 files" in errors and "clock check" not in errors
    events = [(line.split()[2], line.split()[4]) for line in lines]
    assert events == [
        ("n1", "leader_changed"), ("n2", "leader_changed"), ("n3", "leader_changed"),
        ("n1", "task_scheduled"),
        ("n2", "node_offline"), ("n3", "node_offline"),
        ("n2", "leader_changed"), ("n3", "leader_changed"),
        ("n2", "task_reschedule"), ("n2", "task_scheduled"),
    ]                                                # the snapshot is left out by default
    assert lines[-1].split()[3] == "t2/n2"
    assert 'reason="primary n1 offline"' in lines[-1] and "helpers=-" in lines[-1]


def test_a_board_with_its_clock_off_is_reported(tmp_path):
    status, lines, errors = run(failover(tmp_path, skew_n3=40.0))
    assert "n3 is +40.1 s" in errors and "n2 is" not in errors and "n1 is" not in errors
    offsets = merge.clock_offsets(merge.load(merge.journal_files([tmp_path])))
    assert (round(offsets["n3"][0], 1), offsets["n3"][1]) == (40.1, 1)   # only the change n1..n3 all saw


def test_with_two_boards_the_gap_is_reported_without_blame(tmp_path):
    failover(tmp_path, skew_n3=40.0)
    (tmp_path / "edge-1" / "n1.jsonl").unlink()
    status, _, errors = run(tmp_path)
    assert "n2 and n3 are" in errors and "cannot tell which" in errors and " is +" not in errors


def test_filters(tmp_path):
    failover(tmp_path)
    _, lines, _ = run(tmp_path, "--task", "abc")
    assert [line.split()[4] for line in lines] == ["task_scheduled", "task_reschedule", "task_scheduled"]
    _, lines, _ = run(tmp_path, "--node", "n3")
    assert {line.split()[4] for line in lines} == {"leader_changed", "node_offline", "task_scheduled"}
    assert all(" n3 " in line or "primary=n3" in line for line in lines)
    _, lines, _ = run(tmp_path, "--event", "snapshot")
    assert len(lines) == 1 and '"n1","n2","n3"' not in lines[0] and "online=n1,n2,n3" in lines[0]
    _, lines, _ = run(tmp_path, "--snapshots", "--json")
    assert len(lines) == 11 and json.loads(lines[3])["event"] == "task_scheduled"


def test_cut_lines_and_repeated_files_are_tolerated(tmp_path):
    failover(tmp_path)
    with open(tmp_path / "edge-1" / "n1.jsonl", "a") as stream:
        stream.write('{"t": 1800000030.0, "node": "n1", "ev')          # power cut mid-line
    copy = tmp_path / "again" / "n2.jsonl"
    copy.parent.mkdir()
    copy.write_text((tmp_path / "edge-2" / "n2.jsonl").read_text())
    status, lines, errors = run(tmp_path)
    assert status == 0 and "skipped 1 unreadable lines" in errors and len(lines) == 10


def test_no_files_is_an_error(tmp_path):
    status, _, errors = run(tmp_path)
    assert status == 1 and "no journal files" in errors
