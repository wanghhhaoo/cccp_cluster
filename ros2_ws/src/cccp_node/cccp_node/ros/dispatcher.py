"""StartTask / StopTask calls from the cluster leader to executors on any node."""
from __future__ import annotations

import threading

from cccp_task_interfaces.srv import StartTask, StopTask

from .. import names
from ..control.controller import DispatchResult, StartRequest
from ..core import constants as c
from . import convert


class RosDispatcher:
    """Implements control.controller.Dispatcher. Blocking calls: use from the controller thread only.

    The node must spin in a MultiThreadedExecutor and the callback group must be
    reentrant, so service responses are processed while this thread waits.
    """

    def __init__(self, node, callback_group, timeout_s: float = 5.0):
        self._node = node
        self._group = callback_group
        self._timeout_s = timeout_s
        self._clients = {}
        self._lock = threading.Lock()

    def _client(self, srv_type, node_name: str, relative: str):
        key = (relative, node_name)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = self._node.create_client(srv_type, names.on_node(node_name, relative),
                                                  callback_group=self._group)
                self._clients[key] = client
            return client

    def _call(self, client, request):
        if not client.wait_for_service(timeout_sec=self._timeout_s):
            raise RuntimeError("%s is not available" % client.srv_name)
        done = threading.Event()
        future = client.call_async(request)
        future.add_done_callback(lambda _: done.set())
        if not done.wait(self._timeout_s):
            if hasattr(client, "remove_pending_request"):
                client.remove_pending_request(future)
            raise RuntimeError("%s timed out" % client.srv_name)
        return future.result()

    def start(self, node: str, request: StartRequest) -> DispatchResult:
        client = self._client(StartTask, node, names.EXEC_START_SERVICE)
        try:
            reply = self._call(client, convert.fill_start_request(StartTask.Request(), request))
        except Exception as exc:
            return DispatchResult(False, c.TIMEOUT, str(exc))
        return DispatchResult(reply.success, reply.error_code, reply.message)

    def stop(self, node: str, term: int, task_id: int, epoch: int, reason: str) -> DispatchResult:
        client = self._client(StopTask, node, names.EXEC_STOP_SERVICE)
        request = StopTask.Request(term=term, task_id=task_id, epoch=epoch, reason=reason)
        try:
            reply = self._call(client, request)
        except Exception as exc:
            return DispatchResult(False, c.TIMEOUT, str(exc))
        return DispatchResult(reply.success, reply.error_code, reply.message)
