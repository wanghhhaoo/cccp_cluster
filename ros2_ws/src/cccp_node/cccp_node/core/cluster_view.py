"""What this node knows about every other node (DESIGN 3.2, 3.6).

Two messages feed it. NodeView mirrors one heartbeat (NodeStatus, 1 Hz,
best effort): liveness, election, readiness, a summary of the running tasks,
and every few beats the slow part (resources, links). TaskListView mirrors one
TaskList (reliable, sent when it changes): the full TaskState of every task a
node runs, and its tombstones. A new leader adopts tasks from TaskLists.

ClusterView keeps the latest of both per node and decides liveness by *local*
monotonic time of arrival, never by the sender's timestamps (CONVENTIONS 4.3).
Offline nodes are kept for offline_retain_s, so a new leader can still adopt
what an unreachable node (often the old leader itself) was running.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace

from . import constants as c
from .generation import Generation


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
class TaskStateView:
    """One task an executor reports running (TaskState.msg): enough to rebuild the leader's table."""

    task_id: str
    epoch: int
    role: str
    primary: str = ""
    helpers: tuple[str, ...] = ()
    idempotency_key: str = ""
    name: str = ""
    spec_json: str = ""
    assign_term: int = 0

    @property
    def generation(self) -> Generation:
        return Generation(self.epoch, self.assign_term)


@dataclass(frozen=True)
class NodeView:
    """One heartbeat. running_tasks are summaries (task_id, generation, role); the full
    TaskState of each is in the node's TaskList. Resources and links form the slow part,
    sent every few beats: slow=False means this beat did not carry it."""

    name: str
    protocol_version: int = c.PROTOCOL_VERSION
    incarnation: int = 0
    seq: int = 0
    leader_name: str = ""
    term: int = 0
    is_leader: bool = False
    inference_ready: bool = False
    algorithms_ready: tuple[str, ...] = ()
    node_id: int = 0
    visible_peers: int = 0           # peers this node sees online (election candidate order)
    executor_alive: bool = True      # False: the local executor went quiet; running_tasks are the last known
    tasks_version: int = 0           # matches TaskListView.tasks_version
    slow: bool = True
    cpu_cores: int = 0
    mem_total_mb: int = 0
    gpu_mem_total_mb: int = 0
    cpu_usage: float = -1.0
    mem_usage: float = -1.0
    gpu_usage: float = -1.0
    gpu_mem_usage: float = -1.0
    battery: float = -1.0
    running_tasks: tuple[TaskStateView, ...] = ()
    links: tuple[LinkView, ...] = ()

    def link_to(self, peer: str) -> LinkView | None:
        for link in self.links:
            if link.peer == peer:
                return link
        return None

    def runs(self, task_id: str, generation: Generation) -> bool:
        return any(t.task_id == task_id and t.generation == generation for t in self.running_tasks)

    def lists(self, task_id: str) -> bool:
        """Runs some generation of the task."""
        return any(t.task_id == task_id for t in self.running_tasks)


# The slow part of a heartbeat: carried over from the previous beat when absent.
SLOW_FIELDS = ("cpu_cores", "mem_total_mb", "gpu_mem_total_mb", "cpu_usage", "mem_usage", "gpu_usage",
               "gpu_mem_usage", "battery", "links")


@dataclass(frozen=True)
class TaskListView:
    """One TaskList: every task a node runs, in full, and the tasks it stopped because
    they were destroyed (tombstones, task ids)."""

    name: str
    incarnation: int
    tasks_version: int
    tasks: tuple[TaskStateView, ...] = ()
    tombstones: tuple[str, ...] = ()
    node_id: int = 0


@dataclass(frozen=True)
class NodeChange:
    """A node came online or went offline in this node's view (ClusterView.changes)."""
    name: str
    online: bool
    silent_s: float     # going offline: since its last heartbeat; coming back: how long it was unheard
    missed: int = 0     # heartbeats lost while it was online (gaps in seq of one incarnation)
    restarted: bool = False   # came back with a new incarnation


class ClusterView:
    """Latest NodeView and TaskListView of each node plus the local time the node was last heard.

    Thread-safe: ROS callbacks update it while the leader controller reads it.
    """

    def __init__(self, offline_timeout_s: float, offline_retain_s: float = 300.0):
        self._offline_timeout_s = offline_timeout_s
        self._offline_retain_s = offline_retain_s
        self._lock = threading.Lock()
        self._views: dict[str, NodeView] = {}
        self._seen_at: dict[str, float] = {}
        self._task_lists: dict[str, TaskListView] = {}
        self._listed_at: dict[str, float] = {}
        self._names: dict[int, str] = {}
        self._ids: dict[str, int] = {}
        self._reported: dict[str, float] = {}     # online as last reported by changes(): name -> last heard
        self._missed: dict[str, int] = {}         # heartbeats lost since changes() last reported the node
        self._restarted: set[str] = set()
        self._reported_off: dict[str, float] = {}  # last heard, for nodes changes() reported offline

    def update(self, view: NodeView, now: float) -> bool:
        """Record a heartbeat received at local monotonic time now.

        Returns False for messages older than what is already known: an older
        incarnation (a delayed message from before a restart) or a lower seq.
        """
        with self._lock:
            self._expire(now)
            known = self._views.get(view.name)
            if known is not None:
                if view.incarnation < known.incarnation:
                    return False
                if view.incarnation == known.incarnation and view.seq <= known.seq:
                    return False
                if view.incarnation == known.incarnation:
                    self._missed[view.name] = self._missed.get(view.name, 0) + view.seq - known.seq - 1
                else:
                    self._restarted.add(view.name)
                if not view.slow:
                    view = replace(view, slow=known.slow, **{f: getattr(known, f) for f in SLOW_FIELDS})
            self._views[view.name] = view
            self._seen_at[view.name] = now
            if view.node_id:
                self._register(view.name, view.node_id)
            return True

    def update_task_list(self, task_list: TaskListView, now: float) -> bool:
        """Record a TaskList. TaskLists are reliable and replayed to late joiners, so
        repeats and lists from before a restart are expected and ignored (False)."""
        with self._lock:
            self._expire(now)
            known = self._views.get(task_list.name)
            if known is not None and task_list.incarnation < known.incarnation:
                return False
            cached = self._task_lists.get(task_list.name)
            if cached is not None and (task_list.incarnation, task_list.tasks_version) <= (
                    cached.incarnation, cached.tasks_version):
                return False
            self._task_lists[task_list.name] = task_list
            self._listed_at[task_list.name] = now
            if task_list.node_id:
                self._register(task_list.name, task_list.node_id)
            return True

    def changes(self, now: float) -> list[NodeChange]:
        """Nodes that came online or went offline since the previous call, for the log: one-way
        heartbeat loss shows up as one node's view losing a node the others still see."""
        with self._lock:
            result = []
            for name in sorted(self._reported):
                if not self._online(name, now):
                    seen = self._seen_at.get(name)
                    result.append(NodeChange(name, False, now - seen if seen is not None else 0.0,
                                             self._missed.pop(name, 0)))
                    del self._reported[name]
            for name in sorted(n for n in self._views if n not in self._reported and self._online(n, now)):
                silent = self._seen_at[name] - self._reported_off.get(name, self._seen_at[name])
                result.append(NodeChange(name, True, silent, self._missed.pop(name, 0), name in self._restarted))
                self._restarted.discard(name)
                self._reported[name] = self._seen_at[name]
            for change in result:
                if not change.online:
                    self._reported_off[change.name] = self._seen_at.get(change.name, now)
            return result

    def task_list(self, name: str) -> TaskListView | None:
        """The node's TaskList, if it is as new as its latest heartbeat says. None while the
        TaskList for a newer tasks_version is still on its way: skip the node for now. That
        includes a node back with a new incarnation: what it ran before is gone."""
        with self._lock:
            listed = self._task_lists.get(name)
            if listed is None:
                return None
            beat = self._views.get(name)
            if beat is not None and (beat.incarnation, beat.tasks_version) > (listed.incarnation,
                                                                              listed.tasks_version):
                return None
            return listed

    def register(self, name: str, node_id: int) -> None:
        """Note a node's id (this node's own, from its configuration)."""
        with self._lock:
            self._register(name, node_id)

    def _register(self, name: str, node_id: int) -> None:
        self._names[node_id] = name
        self._ids[name] = node_id

    def name_of(self, node_id: int) -> str | None:
        """Messages name nodes by id; TaskLists say which name each id has."""
        with self._lock:
            return self._names.get(node_id)

    def id_of(self, name: str) -> int:
        """0 ("none") for a node whose id is not known yet."""
        with self._lock:
            return self._ids.get(name, 0)

    def visible_peers(self, me: str, now: float) -> int:
        with self._lock:
            return sum(1 for name in self._views if name != me and self._online(name, now))

    def retained(self, now: float) -> dict[str, NodeView]:
        """Offline nodes still within offline_retain_s: their last heartbeat."""
        with self._lock:
            self._expire(now)
            return {name: view for name, view in self._views.items() if not self._online(name, now)}

    def _expire(self, now: float) -> None:
        for name in [n for n, seen in self._seen_at.items() if now - seen >= self._offline_retain_s]:
            del self._views[name], self._seen_at[name]
            self._task_lists.pop(name, None)
            self._listed_at.pop(name, None)
        for name in [n for n, at in self._listed_at.items()       # TaskLists of nodes never heard beating
                     if n not in self._seen_at and now - at >= self._offline_retain_s]:
            del self._task_lists[name], self._listed_at[name]

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
