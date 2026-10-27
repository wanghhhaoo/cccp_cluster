"""Consensus over PySyncObj (Raft on its own TCP port, CONVENTIONS 3.1).

Findings from reading PySyncObj 0.3.17 that this module relies on:
  * commands submitted on a follower are forwarded to the leader;
  * a synchronous call returns the value our replicated method produced on
    the *local* replica, once the entry is committed and applied here;
  * there is no PreVote; leaderFallbackTimeout makes a leader that cannot
    reach a majority step down, which bounds how long a deposed leader keeps
    acting (term fencing on executors covers the remaining window);
  * snapshots serialize attributes created after the base __init__, so the
    lock and listeners are created before it and the table after it.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Mapping, Sequence

from pysyncobj import FAIL_REASON, SyncObj, SyncObjConf, SyncObjConsumer, replicated
from pysyncobj.syncobj import SyncObjException

from ..core import constants as c
from ..core.task_table import ApplyResult, Command, TaskRecord, TaskTable, command_from_dict, command_to_dict
from .base import EventListener, LeaderListener, Member

log = logging.getLogger(__name__)

# The command was certainly not appended to the log.
_NOT_APPLIED = {
    FAIL_REASON.QUEUE_FULL: c.BUSY,
    FAIL_REASON.MISSING_LEADER: c.NO_LEADER,
    FAIL_REASON.NOT_LEADER: c.NO_LEADER,
    FAIL_REASON.REQUEST_DENIED: c.NO_LEADER,
}


@dataclass(frozen=True)
class RaftTiming:
    """CONVENTIONS 5.7. Tests use shorter values."""

    heartbeat_s: float = 0.3
    election_min_s: float = 1.5
    election_max_s: float = 3.0
    leader_fallback_s: float = 6.0

    def conf(self, **extra) -> SyncObjConf:
        return SyncObjConf(
            appendEntriesPeriod=self.heartbeat_s,
            raftMinTimeout=self.election_min_s,
            raftMaxTimeout=self.election_max_s,
            connectionTimeout=max(3.5, self.election_max_s),
            leaderFallbackTimeout=self.leader_fallback_s,
            **extra,
        )


class _ReplicatedTable(SyncObjConsumer):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._listeners: list[EventListener] = []
        super().__init__()
        self._table = TaskTable()       # the only serialized attribute

    @replicated
    def apply_command(self, data: dict) -> ApplyResult:
        with self._lock:
            try:
                result = self._table.apply(command_from_dict(data))
            except Exception as exc:   # deterministic: every replica fails the same way
                log.exception("rejecting malformed command %r", data)
                result = ApplyResult(False, c.INVALID_INPUT, "malformed command: %s" % exc)
        if result.events:
            for listener in list(self._listeners):
                try:
                    listener(result.events)
                except Exception:
                    log.exception("task event listener failed")
        return result

    def _serialize(self):
        with self._lock:
            return {"table": self._table.to_snapshot()}

    def _deserialize(self, data) -> None:
        with self._lock:
            self._table = TaskTable.from_snapshot(data["table"])

    def records(self) -> dict[int, TaskRecord]:
        with self._lock:
            return {r.task_id: r for r in self._table.tasks()}

    def get(self, task_id: int) -> TaskRecord | None:
        with self._lock:
            return self._table.get(task_id)

    def find_by_key(self, key: str) -> TaskRecord | None:
        with self._lock:
            return self._table.find_by_key(key)


class PySyncObjConsensus:
    def __init__(self, self_name: str, members: Sequence[Member], timing: RaftTiming = RaftTiming(),
                 data_dir: str | None = None):
        by_name = {m.name: m for m in members}
        if self_name not in by_name:
            raise ValueError("node %r is not in the cluster member list" % self_name)
        self.name = self_name
        self._names = {m.address: m.name for m in members}
        self._leader_listeners: list[LeaderListener] = []
        self._table = _ReplicatedTable()
        extra = {"onStateChanged": self._on_state_changed}
        if data_dir:
            # Raft needs its log and vote to survive restarts to stay safe.
            os.makedirs(data_dir, exist_ok=True)
            extra.update(journalFile=os.path.join(data_dir, "raft.journal"),
                         fullDumpFile=os.path.join(data_dir, "raft.dump"))
        me = by_name[self_name]
        self._obj = SyncObj(
            me.address,
            [m.address for m in members if m.name != self_name],
            conf=timing.conf(bindAddress="0.0.0.0:%d" % me.port, **extra),
            consumers=[self._table],
        )

    # ----------------------------------------------------------------- writes
    def submit(self, command: Command, timeout: float) -> ApplyResult:
        try:
            return self._table.apply_command(command_to_dict(command), sync=True, timeout=timeout)
        except SyncObjException as exc:
            reason = exc.args[0] if exc.args else None
            if reason in _NOT_APPLIED:
                return ApplyResult(False, _NOT_APPLIED[reason], "not applied: %s" % _reason_name(reason))
            return ApplyResult(False, c.TIMEOUT, "outcome unknown: %s" % _reason_name(reason))

    # ------------------------------------------------------------------ reads
    def records(self) -> Mapping[int, TaskRecord]:
        return self._table.records()

    def get(self, task_id: int) -> TaskRecord | None:
        return self._table.get(task_id)

    def find_by_key(self, idempotency_key: str) -> TaskRecord | None:
        return self._table.find_by_key(idempotency_key)

    def is_leader(self) -> bool:
        return self._obj._isLeader()

    def leader_name(self) -> str:
        leader = self._obj._getLeader()
        return self._names.get(getattr(leader, "address", None), "") if leader is not None else ""

    def term(self) -> int:
        return self._obj.raftCurrentTerm

    def is_ready(self) -> bool:
        return self._obj.isReady()

    # -------------------------------------------------------------- listeners
    def add_event_listener(self, listener: EventListener) -> None:
        self._table._listeners.append(listener)

    def add_leader_listener(self, listener: LeaderListener) -> None:
        self._leader_listeners.append(listener)

    def _on_state_changed(self, old_state, new_state) -> None:
        leader, term = self.is_leader(), self.term()
        for listener in list(self._leader_listeners):
            try:
                listener(leader, term)
            except Exception:
                log.exception("leader listener failed")

    def close(self) -> None:
        self._obj.destroy()


def _reason_name(reason) -> str:
    for name in dir(FAIL_REASON):
        if not name.startswith("_") and getattr(FAIL_REASON, name) == reason:
            return name
    return str(reason)


def load_members(config: Mapping) -> list[Member]:
    """Members from a parsed cluster.yaml ({'raft_port': 7480, 'members': [{name, ip[, port]}]})."""
    default_port = int(config.get("raft_port", 7480))
    return [Member(m["name"], m["ip"], int(m.get("port", default_port))) for m in config["members"]]
