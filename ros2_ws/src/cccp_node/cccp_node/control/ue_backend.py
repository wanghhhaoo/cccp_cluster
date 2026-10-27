"""What the UE HTTP API needs from a node, built on Gateway and ClusterView (no ROS).

Per-frame results of a task are only received once somebody asks about it:
results travel over the wireless network, so each node subscribes to the
result topics of the nodes running tasks it was asked about, through the
`watch` callback provided by the ROS layer.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from ..core import constants as c
from ..core.cluster_view import ClusterView
from ..core.task_table import StatusEvent
from ..ue_api import EventLog, encode_jpeg, spec_from_body
from .gateway import Gateway

MAX_CACHED_TASKS = 100


class UeBackend:
    def __init__(self, node_name: str, gateway: Gateway, cluster: ClusterView,
                 watch: Callable[[str, bool], None] = lambda node, images: None,
                 leader_name: Callable[[], str] = lambda: "", clock: Callable[[], float] = time.monotonic):
        self.node_name = node_name
        self.events = EventLog()
        self._gateway = gateway
        self._cluster = cluster
        self._watch = watch
        self._leader_name = leader_name
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: OrderedDict[int, dict] = OrderedDict()   # task_id -> {"result": dict, "image": msg}
        self._last_status_seq = 0

    # --------------------------------------------------------------- inputs
    def on_status(self, event: StatusEvent) -> None:
        """A /cluster/task_status message, deduplicated by its table seq."""
        with self._lock:
            if event.seq <= self._last_status_seq:
                return
            self._last_status_seq = event.seq
        self.events.add("status", {"task_id": event.task_id, "name": event.name, "task_type": event.task_type,
                                   "status": event.status, "node": event.node, "epoch": event.epoch,
                                   "message": event.message})
        if event.status in c.TERMINAL:
            with self._lock:
                self._latest.pop(event.task_id, None)

    def on_result(self, data: dict) -> None:
        with self._lock:
            self._entry(data["task_id"])["result"] = data
        self.events.add("result", data)

    def on_result_image(self, task_id: int, image: Any) -> None:
        with self._lock:
            self._entry(task_id)["image"] = image

    def _entry(self, task_id: int) -> dict:
        entry = self._latest.get(task_id)
        if entry is None:
            entry = self._latest[task_id] = {"result": None, "image": None}
            while len(self._latest) > MAX_CACHED_TASKS:
                self._latest.popitem(last=False)
        return entry

    # ------------------------------------------------------------ requests
    def health(self) -> dict:
        return {"node": self.node_name, "leader": self._leader_name()}

    def create(self, body: Any) -> dict:
        spec, key = spec_from_body(body)
        reply = self._gateway.create(spec, key)
        for node in reply.dispatched_nodes or ((reply.assigned_node,) if reply.assigned_node else ()):
            self._watch(node, False)
        return {"success": reply.success, "error_code": reply.error_code, "message": reply.message,
                "task_id": reply.task_id, "duplicate": reply.duplicate, "assigned_node": reply.assigned_node,
                "dispatched_nodes": list(reply.dispatched_nodes), "candidate_nodes": list(reply.candidate_nodes)}

    def destroy(self, task_id: int, reason: str) -> dict:
        reply = self._gateway.destroy(task_id, reason or "destroyed by UE")
        if reply.success:
            with self._lock:
                self._latest.pop(task_id, None)
        return {"success": reply.success, "error_code": reply.error_code, "message": reply.message,
                "final_status": reply.final_status}

    def query(self, task_id: int) -> dict:
        record = self._gateway.query(task_id)
        if record is None:
            return {"success": False, "error_code": c.NOT_FOUND, "message": "task %d does not exist" % task_id,
                    "task_id": task_id}
        if record.primary:
            self._watch(record.primary, False)
        return {"success": True, "message": "ok", "task_id": task_id, "name": record.name,
                "task_type": record.spec.task_type, "mode": record.spec.mode, "status": record.status,
                "node": record.primary, "target_nodes": list(record.nodes), "epoch": record.epoch,
                "error_message": record.error_message, "result_url": "", "last_event": record.last_event}

    def list_nodes(self) -> tuple[bool, list, str]:
        now = self._clock()
        nodes = []
        for name, view in sorted(self._cluster.all().items()):
            nodes.append({
                "name": name, "status": "Online" if self._cluster.is_online(name, now) else "Offline",
                "leader": view.is_leader, "term": view.term, "inference_ready": view.inference_ready,
                "algorithms": list(view.algorithms_ready), "cpu_model": view.cpu_model, "cpu_cores": view.cpu_cores,
                "cpu_usage": view.cpu_usage, "mem_total_mb": view.mem_total_mb, "mem_usage": view.mem_usage,
                "gpu_model": view.gpu_model, "gpu_usage": view.gpu_usage, "battery": view.battery,
                "running_tasks": [t.task_id for t in view.running_tasks],
                "links": [{"peer": l.peer, "rtt_ms": l.rtt_ms, "jitter_ms": l.jitter_ms, "loss_rate": l.loss_rate}
                          for l in view.links],
            })
        return True, nodes, "ok"

    def latest_result(self, task_id: int) -> dict | None:
        with self._lock:
            entry = self._latest.get(task_id)
            return entry["result"] if entry else None

    def latest_image_jpeg(self, task_id: int) -> bytes | None:
        record = self._gateway.query(task_id)
        if record is not None and record.primary:
            self._watch(record.primary, True)
        with self._lock:
            entry = self._latest.get(task_id)
            image = entry["image"] if entry else None
        return encode_jpeg(image) if image is not None else None
