"""What this node knows about every other node, built from /cluster/node_status.

NodeView mirrors one NodeStatus message. ClusterView keeps the latest view per
node and decides liveness by *local* monotonic time of arrival, never by the
sender's timestamps (CONVENTIONS 4.3).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class LinkView:
    peer: str
    rtt_ms: float = -1.0
    jitter_ms: float = -1.0
    loss_rate: float = -1.0

    @property
    def measured(self) -> bool:
        return self.rtt_ms >= 0


@dataclass(frozen=True)
class RunningTaskView:
    task_id: int
    epoch: int
    role: str


@dataclass(frozen=True)
class NodeView:
    name: str
    protocol_version: int = 1
    incarnation: int = 0
    seq: int = 0
    leader_name: str = ""
    term: int = 0
    is_leader: bool = False
    inference_ready: bool = False
    algorithms_ready: tuple[str, ...] = ()
    cpu_model: str = ""
    cpu_cores: int = 0
    mem_total_mb: int = 0
    gpu_model: str = ""
    gpu_mem_total_mb: int = 0
    cpu_usage: float = -1.0
    mem_usage: float = -1.0
    gpu_usage: float = -1.0
    gpu_mem_usage: float = -1.0
    battery: float = -1.0
    running_tasks: tuple[RunningTaskView, ...] = ()
    links: tuple[LinkView, ...] = ()

    def link_to(self, peer: str) -> LinkView | None:
        for link in self.links:
            if link.peer == peer:
                return link
        return None

    def runs(self, task_id: int, epoch: int) -> bool:
        return any(t.task_id == task_id and t.epoch == epoch for t in self.running_tasks)


class ClusterView:
    """Latest NodeView of each node plus the local time it arrived.

    Thread-safe: ROS callbacks update it while the leader controller reads it.
    """

    def __init__(self, offline_timeout_s: float):
        self._offline_timeout_s = offline_timeout_s
        self._lock = threading.Lock()
        self._views: dict[str, NodeView] = {}
        self._seen_at: dict[str, float] = {}

    def update(self, view: NodeView, now: float) -> bool:
        """Record a NodeStatus received at local monotonic time now.

        Returns False for messages older than what is already known: an older
        incarnation (a delayed message from before a restart) or a lower seq.
        """
        with self._lock:
            known = self._views.get(view.name)
            if known is not None:
                if view.incarnation < known.incarnation:
                    return False
                if view.incarnation == known.incarnation and view.seq <= known.seq:
                    return False
            self._views[view.name] = view
            self._seen_at[view.name] = now
            return True

    def get(self, name: str) -> NodeView | None:
        with self._lock:
            return self._views.get(name)

    def is_online(self, name: str, now: float) -> bool:
        with self._lock:
            return self._online(name, now)

    def _online(self, name: str, now: float) -> bool:
        seen = self._seen_at.get(name)
        return seen is not None and now - seen < self._offline_timeout_s

    def online(self, now: float) -> dict[str, NodeView]:
        with self._lock:
            return {name: view for name, view in self._views.items() if self._online(name, now)}

    def all(self) -> dict[str, NodeView]:
        with self._lock:
            return dict(self._views)

    def age(self, name: str, now: float) -> float | None:
        with self._lock:
            seen = self._seen_at.get(name)
            return None if seen is None else now - seen
