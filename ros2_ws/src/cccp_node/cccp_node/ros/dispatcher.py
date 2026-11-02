"""Leader -> executor commands over protocol 2's StartTask / StopTask services.

Stands in for the Command / ExecEvent topics of protocol 3 (DESIGN 2.5) until the
interfaces change: each command becomes one asynchronous service call per target,
and each response comes back as an ExecEvent. Nothing blocks; a lost call simply
produces no event, and reconciliation resends from heartbeats as it would for a
lost Command.
"""
from __future__ import annotations

import threading

from cccp_task_interfaces.srv import StartTask, StopTask

from .. import names
from ..control import protocol as p
from ..control.protocol import ExecCommand, ExecEvent
from . import convert


class ServiceClients:
    """Per-node service clients, created once and reused."""

    def __init__(self, node, callback_group):
        self._node = node
        self._group = callback_group
        self._clients = {}
        self._lock = threading.Lock()

    def get(self, srv_type, node_name: str, relative: str):
        key = (relative, node_name)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = self._node.create_client(srv_type, names.on_node(node_name, relative),
                                                  callback_group=self._group)
                self._clients[key] = client
            return client


class RosCommandSink:
    """Implements control.controller.CommandSink; on_event receives the executors' answers."""

    def __init__(self, node, callback_group, on_event):
        self._clients = ServiceClients(node, callback_group)
        self._on_event = on_event

    def send(self, command: ExecCommand) -> None:
        for target in command.targets:
            if command.op == p.START:
                client = self._clients.get(StartTask, target, names.EXEC_START_SERVICE)
                request = convert.fill_start_request(StartTask.Request(), command, target)
                done = p.RUNNING
            else:
                client = self._clients.get(StopTask, target, names.EXEC_STOP_SERVICE)
                request = convert.fill_stop_request(StopTask.Request(), command)
                done = p.STOPPED
            if not client.service_is_ready():
                continue             # as if the command were lost: reconciliation resends
            future = client.call_async(request)
            future.add_done_callback(lambda f, node=target, kind=done: self._answered(command, node, kind, f))

    def _answered(self, command: ExecCommand, node: str, kind: str, future) -> None:
        response = future.result()
        if response is None:
            return
        self._on_event(ExecEvent(node, 0, 0, command.term, command.request_id, command.task_id, command.generation,
                                 kind if response.success else p.REJECTED, response.error_code, response.message))
