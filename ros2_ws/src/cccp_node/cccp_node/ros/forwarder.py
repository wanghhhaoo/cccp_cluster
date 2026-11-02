"""Gateway -> leader requests over protocol 2's leader/task/* services.

Stands in for the Request / Reply topics of protocol 3 (DESIGN 2.5) until the
interfaces change: a Request becomes an asynchronous call to the target leader's
service, and its response comes back to the gateway as the Reply. The leader
serves the call with Gateway.serve_*, which hands it to its own controller and
never forwards.
"""
from __future__ import annotations

from cccp_task_interfaces.srv import CreateTask, DestroyTask, QueryTask

from .. import names
from ..control import protocol as p
from ..control.protocol import Request
from . import convert
from .dispatcher import ServiceClients


class RosRequestTransport:
    """Implements control.gateway.RequestTransport; on_reply receives the leader's answers.

    send() runs on the caller's thread (a service callback or the UE HTTP server), which waits
    for the reply anyway, so it may wait for the leader's service to be discovered first."""

    def __init__(self, node, callback_group, on_reply, discovery_timeout_s: float = 3.0):
        self._clients = ServiceClients(node, callback_group)
        self._on_reply = on_reply
        self._discovery_timeout_s = discovery_timeout_s

    def send(self, request: Request) -> None:
        if request.op == p.CREATE:
            client = self._clients.get(CreateTask, request.target_leader, names.LEADER_CREATE_SERVICE)
            call = convert.fill_create_request(CreateTask.Request(), request.spec, request.idempotency_key)
        elif request.op == p.DESTROY:
            client = self._clients.get(DestroyTask, request.target_leader, names.LEADER_DESTROY_SERVICE)
            call = DestroyTask.Request(task_id=request.task_id, reason=request.reason)
        else:
            client = self._clients.get(QueryTask, request.target_leader, names.LEADER_QUERY_SERVICE)
            call = QueryTask.Request(task_id=request.task_id)
        if not client.wait_for_service(timeout_sec=self._discovery_timeout_s):
            return                   # as if the request were lost: the gateway times out
        future = client.call_async(call)
        future.add_done_callback(lambda f: self._answered(request, f))

    def _answered(self, request: Request, future) -> None:
        response = future.result()
        if response is not None:
            self._on_reply(convert.reply_from_response(request, response))
