"""The interface the rest of the node uses to read and change the replicated task table."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from ..core.task_table import ApplyResult, Command, StatusEvent, TaskRecord

EventListener = Callable[[tuple[StatusEvent, ...]], None]
LeaderListener = Callable[[bool, int], None]   # (is_leader, term)


@dataclass(frozen=True)
class Member:
    """One entry of cluster.yaml: a voting member of the Raft group."""

    name: str
    host: str
    port: int

    @property
    def address(self) -> str:
        return "%s:%d" % (self.host, self.port)


class Consensus(Protocol):
    def submit(self, command: Command, timeout: float) -> ApplyResult:
        """Replicate a command and return its result once applied locally.

        Never raises for cluster conditions. error_code NO_LEADER or BUSY means
        the command was definitely not applied; TIMEOUT means the outcome is
        unknown, so retry only with the same idempotency key (CONVENTIONS 7.2).
        Must not be called from a listener callback.
        """

    def records(self) -> Mapping[int, TaskRecord]:
        """Consistent copy of the local replica, keyed by task_id."""

    def get(self, task_id: int) -> TaskRecord | None: ...

    def find_by_key(self, idempotency_key: str) -> TaskRecord | None: ...

    def is_leader(self) -> bool: ...

    def leader_name(self) -> str:
        """Last known leader, '' if unknown."""

    def term(self) -> int: ...

    def is_ready(self) -> bool:
        """True once the local replica has caught up with the cluster after start."""

    def add_event_listener(self, listener: EventListener) -> None:
        """Called on every replica, in log order, with the events of each applied command."""

    def add_leader_listener(self, listener: LeaderListener) -> None: ...

    def close(self) -> None: ...
