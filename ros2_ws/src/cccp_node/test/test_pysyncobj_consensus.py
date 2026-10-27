"""Three real PySyncObj replicas on localhost (one process, separate TCP ports)."""
import socket
import time

import pytest

pytest.importorskip("pysyncobj")

from cccp_node.core import constants as c  # noqa: E402
from cccp_node.core.task_spec import TaskSpec  # noqa: E402
from cccp_node.core.task_table import Assign, Submit  # noqa: E402
from cccp_node.consensus.base import Member  # noqa: E402
from cccp_node.consensus.pysyncobj_backend import (  # noqa: E402
    PySyncObjConsensus, RaftTiming, _ReplicatedTable, load_members,
)

FAST = RaftTiming(heartbeat_s=0.05, election_min_s=0.3, election_max_s=0.6, leader_fallback_s=1.0)


def free_ports(n):
    sockets = [socket.socket() for _ in range(n)]
    for s in sockets:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    return ports


def wait_for(predicate, timeout=10.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not met within %.1f s" % timeout)


@pytest.fixture
def cluster():
    members = [Member("n%d" % i, "127.0.0.1", port) for i, port in enumerate(free_ports(3))]
    nodes = {m.name: PySyncObjConsensus(m.name, members, FAST) for m in members}
    yield nodes
    for node in nodes.values():
        node.close()


def leaders(nodes):
    return [n for n in nodes.values() if n.is_leader()]


def submit(node, key="k"):
    return node.submit(Submit(key, TaskSpec(task_type="PKCI")), timeout=5.0)


def test_single_leader_is_elected_and_known_by_all(cluster):
    wait_for(lambda: len(leaders(cluster)) == 1 and all(n.leader_name() for n in cluster.values()))
    leader = leaders(cluster)[0]
    assert {n.leader_name() for n in cluster.values()} == {leader.name}
    assert len({n.term() for n in cluster.values()}) == 1


def test_follower_submissions_are_forwarded_and_replicated(cluster):
    wait_for(lambda: len(leaders(cluster)) == 1)
    follower = next(n for n in cluster.values() if not n.is_leader())
    result = submit(follower)
    assert result.ok and result.task_id == 1
    wait_for(lambda: all(n.get(1) is not None for n in cluster.values()))
    again = submit(next(n for n in cluster.values() if n is not follower))
    assert again.duplicate and again.task_id == 1


def test_every_replica_sees_the_same_events_in_order(cluster):
    seen = {name: [] for name in cluster}
    for name, node in cluster.items():
        node.add_event_listener(lambda events, name=name: seen[name].extend((e.seq, e.status) for e in events))
    wait_for(lambda: len(leaders(cluster)) == 1)
    leader = leaders(cluster)[0]
    task_id = submit(leader).task_id
    assert leader.submit(Assign(task_id, 0, "n1"), timeout=5.0).ok
    expected = [(1, c.PENDING), (2, c.SCHEDULED)]
    wait_for(lambda: all(events == expected for events in seen.values()))


def test_new_leader_takes_over_with_the_table(cluster):
    changes = []
    for node in cluster.values():
        node.add_leader_listener(lambda leader, term: changes.append((leader, term)))
    wait_for(lambda: len(leaders(cluster)) == 1)
    old = leaders(cluster)[0]
    assert submit(old).ok
    old_term = old.term()
    wait_for(lambda: all(n.get(1) is not None for n in cluster.values()))
    old.close()
    rest = {k: v for k, v in cluster.items() if v is not old}
    started = time.monotonic()
    new = wait_for(lambda: leaders(rest) and leaders(rest)[0])
    failover_s = time.monotonic() - started
    assert new.term() > old_term and new.get(1) is not None
    assert submit(new, "k2").task_id == 2
    assert any(leader for leader, _ in changes)
    print("failover with test timing: %.2f s" % failover_s)


def test_submit_without_quorum_reports_unknown_outcome(cluster):
    wait_for(lambda: len(leaders(cluster)) == 1)
    survivor = next(iter(cluster.values()))
    for node in cluster.values():
        if node is not survivor:
            node.close()
    wait_for(lambda: not survivor.is_leader(), timeout=5.0)
    result = submit(survivor)
    assert not result.ok and result.error_code in (c.TIMEOUT, c.NO_LEADER)


def test_snapshot_uses_table_snapshot_format():
    consumer = _ReplicatedTable()
    consumer._table.apply(Submit("k", TaskSpec(task_type="PKCI")))
    data = consumer._serialize()
    assert set(data) == {"table"} and data["table"]["next_id"] == 2
    other = _ReplicatedTable()
    other._deserialize(data)
    assert other.records()[1].idempotency_key == "k"


def test_load_members_and_unknown_self():
    members = load_members({"raft_port": 7480, "members": [{"name": "a", "ip": "10.0.0.1"},
                                                          {"name": "b", "ip": "10.0.0.2", "port": 9}]})
    assert [m.address for m in members] == ["10.0.0.1:7480", "10.0.0.2:9"]
    with pytest.raises(ValueError):
        PySyncObjConsensus("zz", members)
