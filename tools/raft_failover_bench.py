"""Measure leader failover of PySyncObjConsensus with production Raft timing (12 replicas on localhost).

Usage: python3 tools/raft_failover_bench.py ros2_ws/src/cccp_node
Prints election time and the time until the new leader has caught up (Barrier committed).
Localhost numbers are a lower bound; repeat on the real ad-hoc network.
"""
import socket
import statistics
import sys
import time

sys.path.insert(0, sys.argv[1])

from cccp_node.consensus.base import Member  # noqa: E402
from cccp_node.consensus.pysyncobj_backend import PySyncObjConsensus, RaftTiming  # noqa: E402
from cccp_node.core.task_spec import TaskSpec  # noqa: E402
from cccp_node.core.task_table import Barrier, Submit  # noqa: E402


def free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def leader_of(nodes):
    found = [n for n in nodes if n.is_leader()]
    return found[0] if len(found) == 1 else None


def wait(pred, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.01)
    raise SystemExit("timeout")


members = [Member("n%02d" % i, "127.0.0.1", p) for i, p in enumerate(free_ports(12))]
nodes = [PySyncObjConsensus(m.name, members, RaftTiming()) for m in members]
t0 = time.monotonic()
leader = wait(lambda: leader_of(nodes))
print("initial election: %.2f s (leader %s)" % (time.monotonic() - t0, leader.name))
t0 = time.monotonic()
assert leader.submit(Submit("k0", TaskSpec(task_type="PKCI")), timeout=10).ok
print("submit commit latency: %.0f ms" % ((time.monotonic() - t0) * 1000))

samples, ready = [], []
for trial in range(5):
    leader.close()
    nodes.remove(leader)
    t0 = time.monotonic()
    leader = wait(lambda: leader_of(nodes))
    samples.append(time.monotonic() - t0)
    seen_at_election = leader.get(1) is not None
    assert leader.submit(Barrier(leader.term()), timeout=10).ok
    ready.append(time.monotonic() - t0)
    assert leader.get(1) is not None
    print("trial %d: leader %s elected after %.2f s (task visible: %s), caught up after %.2f s, term %d, %d nodes"
          % (trial, leader.name, samples[-1], seen_at_election, ready[-1], leader.term(), len(nodes)))
print("election median %.2f s max %.2f s; ready median %.2f s max %.2f s"
      % (statistics.median(samples), max(samples), statistics.median(ready), max(ready)))
for n in nodes:
    n.close()
