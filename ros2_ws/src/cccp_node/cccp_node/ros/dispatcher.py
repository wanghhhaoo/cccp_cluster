"""Blocking ROS service calls to other nodes: StartTask / StopTask from the cluster leader to executors."""
from __future__ import annotations

import threading

from cccp_task_interfaces.srv import StartTask, StopTask

from .. import names
from ..control.controller import DispatchResult, StartRequest
from ..core import constants as c
from ..core.generation import Generation
from . import convert


class ServiceCaller:
    """Per-node service clients plus a blocking call with a timeout.

    Use from a thread that is not an executor callback of the same group (the
    controller thread, or a reentrant service callback). The node must spin in
    a MultiThreadedExecutor so responses are processed while the caller waits.
    """

    def __init__(self, node, callback_group, timeout_s: float):
        self._node = node
        self._group = callback_group
        self.timeout_s = timeout_s
        self._clients = {}
        self._lock = threading.Lock()

    def client(self, srv_type, node_name: str, relative: str):
        key = (relative, node_name)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = self._node.create_client(srv_type, names.on_node(node_name, relative),
                                                  callback_group=self._group)
                self._clients[key] = client
            return client

    def call(self, client, request, timeout_s: float | None = None):
        timeout_s = self.timeout_s if timeout_s is None else timeout_s
        if not client.wait_for_service(timeout_sec=min(timeout_s, self.timeout_s)):
            raise RuntimeError("%s is not available" % client.srv_name)
        done = threading.Event()
        future = client.call_async(request)
        future.add_done_callback(lambda _: done.set())
        if not done.wait(timeout_s):
            if hasattr(client, "remove_pending_request"):
                client.remove_pending_request(future)
            raise RuntimeError("%s timed out" % client.srv_name)
        return future.result()


class RosDispatcher(ServiceCaller):
    """Implements control.controller.Dispatcher. Blocking calls: use from the controller thread only."""

    def __init__(self, node, callback_group, timeout_s: float = 5.0):
        super().__init__(node, callback_group, timeout_s)

    def start(self, node: str, request: StartRequest) -> DispatchResult:
        client = self.client(StartTask, node, names.EXEC_START_SERVICE)
        try:
            reply = self.call(client, convert.fill_start_request(StartTask.Request(), request))
        except Exception as exc:
            return DispatchResult(False, c.TIMEOUT, str(exc))
        return DispatchResult(reply.success, reply.error_code, reply.message)

    def stop(self, node: str, term: int, task_id: str, generation: Generation, reason: str) -> DispatchResult:
        client = self.client(StopTask, node, names.EXEC_STOP_SERVICE)
        request = StopTask.Request(term=term, task_id=task_id, epoch=generation.epoch,
                                   assign_term=generation.assign_term, reason=reason)
        try:
            reply = self.call(client, request)
        except Exception as exc:
            return DispatchResult(False, c.TIMEOUT, str(exc))
        return DispatchResult(reply.success, reply.error_code, reply.message)
