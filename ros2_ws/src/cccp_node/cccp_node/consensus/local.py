"""Single-process Consensus without replication: for tests and single-node development."""
from __future__ import annotations

import threading
from typing import Mapping

from ..core.task_table import ApplyResult, Command, TaskRecord, TaskTable, command_from_dict, command_to_dict
from .base import EventListener, LeaderListener


class LocalConsensus:
    def __init__(self, name: str = "local", leader: bool = True, term: int = 1):
        self.name = name
        self._table = TaskTable()
        self._lock = threading.RLock()
        self._leader = leader
        self._term = term
        self._event_listeners: list[EventListener] = []
        self._leader_listeners: list[LeaderListener] = []

    def submit(self, command: Command, timeout: float) -> ApplyResult:
        # Round-trip through the log format so tests exercise the same path as replication.
        with self._lock:
            result = self._table.apply(command_from_dict(command_to_dict(command)))
        if result.events:
            for listener in list(self._event_listeners):
                listener(result.events)
        return result

    def records(self) -> Mapping[int, TaskRecord]:
        with self._lock:
            return {r.task_id: r for r in self._table.tasks()}

    def get(self, task_id: int) -> TaskRecord | None:
        with self._lock:
            return self._table.get(task_id)

    def find_by_key(self, idempotency_key: str) -> TaskRecord | None:
        with self._lock:
            return self._table.find_by_key(idempotency_key)

    def is_leader(self) -> bool:
        return self._leader

    def leader_name(self) -> str:
        return self.name if self._leader else ""

    def term(self) -> int:
        return self._term

    def is_ready(self) -> bool:
        return True

    def set_leader(self, leader: bool, term: int | None = None) -> None:
        """Test hook: simulate an election result."""
        self._leader = leader
        if term is not None:
            self._term = term
        for listener in list(self._leader_listeners):
            listener(leader, self._term)

    def add_event_listener(self, listener: EventListener) -> None:
        self._event_listeners.append(listener)

    def add_leader_listener(self, listener: LeaderListener) -> None:
        self._leader_listeners.append(listener)

    def close(self) -> None:
        pass
