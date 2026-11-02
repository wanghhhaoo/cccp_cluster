"""Cluster leader election over NodeStatus broadcasts (CONVENTIONS 5.1), no consensus protocol.

Every node publishes the leader it follows (leader_name, term) and whether it
claims leadership itself (is_leader); a claim doubles as the leader's lease,
renewed by every NodeStatus. Rules, applied by every node on its own view:

  * claims compare by (supporters, term, smaller name): supporters are the
    live nodes naming the claimant as their leader, the claimant included.
    A node that claimed alone while cut off (and so carries a higher term)
    therefore cannot unseat the leader the rest of the cluster follows, and
    after a partition heals the side with more nodes keeps its leader;
  * a leader that hears a term above its own (such a lone claim) raises its
    term past it and keeps leading, so executors keep accepting its commands;
  * no claim heard for leader_timeout_s: the smallest-named live node claims
    with term = highest known term + 1, the others wait for it;
  * an incumbent keeps the role when a smaller-named node (re)joins.

With lossy links two nodes can briefly both claim. Each steps down as soon as
it hears a better claim, executors reject commands from the lower term
(ExecutionGuard), and reconciliation stops duplicate executions; duplicates
are therefore short-lived but possible (CONVENTIONS 5.0).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .cluster_view import NodeView


@dataclass(frozen=True)
class ElectionConfig:
    # A dead leader first has to drop out of the online peers (node_offline_timeout_s,
    # 5 s), which already acts as its lease; this only adds a little hysteresis.
    leader_timeout_s: float = 1.0
    startup_grace_s: float = 5.0      # listen this long after start before claiming
    protocol_version: int = 2


@dataclass(frozen=True)
class Leadership:
    leader: str        # "" while unknown
    term: int
    is_self: bool


class Election:
    def __init__(self, node_name: str, config: ElectionConfig, now: float):
        self.node_name = node_name
        self._config = config
        self._started = now
        self._term = 0
        self._leader = ""
        self._claim_seen = None          # local time a foreign claim was last heard

    @property
    def state(self) -> Leadership:
        return Leadership(self._leader, self._term, self._leader == self.node_name)

    def update(self, peers: Mapping[str, NodeView], now: float) -> Leadership:
        """peers: online nodes other than this one (latest NodeStatus of each)."""
        eligible = {n: v for n, v in peers.items() if v.protocol_version == self._config.protocol_version}
        highest = max([self._term] + [v.term for v in eligible.values()])

        def supporters(claimant: str) -> int:
            followers = sum(1 for n, v in eligible.items() if n != claimant and v.leader_name == claimant)
            mine = 1 if self._leader == claimant else 0       # this node's own vote
            return 1 + followers + (mine if claimant != self.node_name else 0)

        claims = [(supporters(n), v.term, n) for n, v in eligible.items() if v.is_leader]
        best = max(claims, key=_rank, default=None)

        if self._leader == self.node_name:
            mine = (supporters(self.node_name), self._term, self.node_name)
            if best is not None and _rank(best) > _rank(mine):
                self._follow(best, now)                       # a better-supported claim: step down
            elif highest > self._term:
                self._term = highest + 1                      # stay ahead of lone claims
            return self.state

        if best is not None:
            self._follow(best, now)
            return self.state

        self._term = highest
        waited_since = self._claim_seen if self._claim_seen is not None else self._started
        timeout = self._config.leader_timeout_s if self._claim_seen is not None else self._config.startup_grace_s
        if now - waited_since >= timeout:
            if self.node_name == min([self.node_name, *eligible]):
                self._term = highest + 1
                self._leader = self.node_name
            else:
                self._leader = ""                             # the smallest live node will claim
        return self.state

    def _follow(self, claim, now: float) -> None:
        _, self._term, self._leader = claim
        self._claim_seen = now


def _rank(claim) -> tuple:
    """Sort key: more supporters, then higher term, then the smaller name."""
    supporters, term, name = claim
    return supporters, term, tuple(-ord(ch) for ch in name) + (0,)
