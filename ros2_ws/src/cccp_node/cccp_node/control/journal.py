"""Event journal of a node: one JSON line per control-plane event (CONVENTIONS 7.4, DESIGN 2.9).

The log is for reading one node live; the journal is for reconstructing what the
cluster did afterwards: every node writes its own to local disk, and
tools/merge_journal.py merges the boards' files into one timeline. Each entry
carries the wall clock (to merge boards; only as good as their clock sync), the
monotonic clock, the node, its incarnation, the term and leader it follows, the
event name and the event's own fields. A snapshot of the cluster view every few
seconds bounds what a missed or unrecorded change can hide.

record() never touches the disk: lines go through a bounded queue to a writer
thread (an fsync on an SD card can take tens of milliseconds, and events are
recorded from the heartbeat's callback group and the controller loop). The last
`capacity` entries also stay in memory for GET /api/ue/journal.

No ROS here (CONVENTIONS 3.1).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import Counter, deque
from typing import Callable, Iterable, Mapping, Optional

log = logging.getLogger(__name__)

# Keys every entry has; event fields must not reuse them.
RESERVED = frozenset(("seq", "t", "mono", "node", "inc", "event", "term", "leader"))
SNAPSHOT = "snapshot"


class RotatingFile:
    """Appends to <directory>/<stem>.jsonl. Past max_bytes it becomes <stem>.1.jsonl, older files
    shift up (<stem>.2.jsonl, ...) and the oldest beyond `keep` is removed."""

    def __init__(self, directory: str, stem: str, max_bytes: int, keep: int):
        self.directory, self.stem = directory, stem
        self.max_bytes, self.keep = max(1, max_bytes), max(0, keep)
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, stem + ".jsonl")
        self._file = open(self.path, "ab")
        self._size = self._file.tell()

    def _old(self, index: int) -> str:
        return os.path.join(self.directory, "%s.%d.jsonl" % (self.stem, index))

    def write(self, data: bytes) -> None:
        if self._size and self._size + len(data) > self.max_bytes:
            self._rotate()
        self._file.write(data)
        self._size += len(data)

    def _rotate(self) -> None:
        self._file.close()
        if self.keep == 0:
            os.remove(self.path)
        else:
            if os.path.exists(self._old(self.keep)):
                os.remove(self._old(self.keep))
            for index in range(self.keep - 1, 0, -1):
                if os.path.exists(self._old(index)):
                    os.replace(self._old(index), self._old(index + 1))
            os.replace(self.path, self._old(1))
        self._file = open(self.path, "ab")
        self._size = 0

    def flush(self, sync: bool) -> None:
        self._file.flush()
        if sync:
            os.fsync(self._file.fileno())

    def close(self) -> None:
        self._file.close()


class JournalWriter:
    """Writes queued lines to a RotatingFile from its own thread; flushes after every batch and
    fsyncs at most once per sync_period_s (a board can lose power at any moment: a drone's battery
    is pulled, not shut down). A full queue (the disk stalled) drops lines and counts them."""

    def __init__(self, directory: str, stem: str, max_bytes: int = 20 << 20, keep: int = 9,
                 queue_size: int = 10000, sync_period_s: float = 1.0):
        self._args = (directory, stem, max_bytes, keep)
        self._file: Optional[RotatingFile] = None
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._sync_period = sync_period_s
        self._last_sync = 0.0
        self._last_error = float("-inf")
        self.dropped = 0
        self.write_errors = 0
        self._open()                     # a bad directory shows at startup, not at the first event
        self._thread = threading.Thread(target=self._run, name="journal-writer", daemon=True)
        self._thread.start()

    @property
    def path(self) -> str:
        return os.path.join(self._args[0], self._args[1] + ".jsonl")

    def put(self, line: str) -> None:
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            self.dropped += 1

    def close(self, timeout: float = 2.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def _open(self) -> bool:
        if self._file is None:
            try:
                self._file = RotatingFile(*self._args)
            except OSError as exc:
                self._error("cannot open the journal in %s: %s" % (self._args[0], exc))
        return self._file is not None

    def _error(self, message: str) -> None:
        self.write_errors += 1
        now = time.monotonic()
        if now - self._last_error >= 60.0:          # a full disk would otherwise log every event
            self._last_error = now
            log.warning("journal: %s (%d errors so far)", message, self.write_errors)

    def _run(self) -> None:
        while True:
            batch = [self._queue.get()]
            while len(batch) < 500:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            done = None in batch
            lines = [line for line in batch if line is not None]
            if lines and self._open():
                try:
                    for line in lines:
                        self._file.write(line.encode("utf-8"))
                    now = time.monotonic()
                    sync = done or now - self._last_sync >= self._sync_period
                    self._file.flush(sync)
                    if sync:
                        self._last_sync = now
                except OSError as exc:
                    self._error("write to %s failed: %s" % (self._args[0], exc))
                    try:
                        self._file.close()
                    except OSError:
                        pass
                    self._file = None                # reopened with the next batch
            if done:
                if self._file is not None:
                    self._file.close()
                return


class Journal:
    """record(event, **fields) is thread-safe and cheap: it formats one line, keeps it in memory
    and hands it to the sink (JournalWriter.put, or nothing)."""

    def __init__(self, node: str, incarnation: int,
                 context: Callable[[], tuple[int, str]] = lambda: (0, ""),
                 sink: Optional[Callable[[str], None]] = None,
                 wall: Callable[[], float] = time.time, clock: Callable[[], float] = time.monotonic,
                 capacity: int = 2000):
        self.node, self.incarnation = node, incarnation
        self._context = context          # (term, leader) this node follows
        self._sink = sink
        self._wall, self._clock = wall, clock
        self._entries: deque = deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()

    def record(self, event: str, **fields) -> dict:
        clash = RESERVED.intersection(fields)
        assert not clash, "journal field names %s are reserved" % sorted(clash)
        term, leader = self._context()
        with self._lock:
            self._seq += 1
            entry = {"t": round(self._wall(), 3), "node": self.node, "event": event, **fields,
                     "term": term, "leader": leader, "seq": self._seq, "inc": self.incarnation,
                     "mono": round(self._clock(), 3)}
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            self._entries.append(entry)
            if self._sink is not None:
                self._sink(line)
        return entry

    def since(self, after: int = 0, limit: int = 500, include_snapshots: bool = True) -> tuple[list, int]:
        """Entries with seq > after, oldest first, and the newest seq."""
        with self._lock:
            found = [e for e in self._entries if e["seq"] > after
                     and (include_snapshots or e["event"] != SNAPSHOT)]
            return found[:max(0, limit)], self._seq


def recorder(journal: Optional[Journal]) -> Callable[..., None]:
    """journal.record, or a no-op for components built without a journal (tests)."""
    if journal is None:
        return lambda event, **fields: None
    return journal.record


def snapshot_fields(online: Mapping, everyone: Mapping, links: Iterable = (),
                    task_statuses: Optional[Iterable[str]] = None) -> dict:
    """What a periodic snapshot records: who is online and what each node says in its heartbeat,
    this node's own link measurements (LinkView; -1 is unknown), and the task table by status while this node leads.
    About 0.6 KB with 12 nodes; every 10 s that is ~5 MB a day."""
    fields = {
        "online": sorted(online),
        "offline": sorted(name for name in everyone if name not in online),
        "nodes": {name: {"term": view.term, "leading": view.is_leader, "tasks": len(view.running_tasks),
                         "executor": view.executor_alive, "ready": view.inference_ready}
                  for name, view in sorted(online.items())},
        "links": {link.peer: {"rtt_ms": round(link.rtt_ms, 1), "loss": round(link.loss_rate, 3)}
                  for link in links},
    }
    if task_statuses is not None:
        fields["tasks"] = dict(sorted(Counter(task_statuses).items()))
    return fields
