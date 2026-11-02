"""Thread-safe holder of the leader's in-memory TaskTable.

The ROS callbacks, the gateway and the leader controller all touch the table;
every access goes through this lock. reset() starts an empty table when this
node becomes leader for a new term: the table is then rebuilt from executor
reports (CONVENTIONS 5.3).
"""
from __future__ import annotations

import threading
from typing import Callable, Mapping

from ..core.task_table import ApplyResult, Command, StatusEvent, TaskRecord, TaskTable

EventListener = Callable[[tuple[StatusEvent, ...]], None]


class TaskStore:
    def __init__(self) -> None:
        self._table = TaskTable()
        self._lock = threading.RLock()
        self._listeners: list[EventListener] = []

    def apply(self, command: Command) -> ApplyResult:
        with self._lock:
            result = self._table.apply(command)
        if result.events:
            for listener in list(self._listeners):
                listener(result.events)
        return result

    def reset(self) -> None:
        with self._lock:
            self._table = TaskTable()

    def records(self) -> Mapping[str, TaskRecord]:
        with self._lock:
            return {r.task_id: r for r in self._table.tasks()}

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._table.get(task_id)

    def find_by_key(self, idempotency_key: str) -> TaskRecord | None:
        with self._lock:
            return self._table.find_by_key(idempotency_key)

    def add_event_listener(self, listener: EventListener) -> None:
        """Called after each change with its events, outside the lock."""
        self._listeners.append(listener)
