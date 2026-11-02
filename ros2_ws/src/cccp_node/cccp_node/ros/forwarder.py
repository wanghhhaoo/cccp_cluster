"""Forwards task API calls from any node to the cluster leader's leader/task/* services."""
from __future__ import annotations

from cccp_task_interfaces.srv import CreateTask, DestroyTask, QueryTask

from .. import names
from ..control.gateway import CreateReply, DestroyReply, QueryReply
from ..core import constants as c
from ..core.task_spec import TaskSpec
from . import convert
from .dispatcher import ServiceCaller


class RosForwarder(ServiceCaller):
    """Implements control.gateway.Forwarder. Called from reentrant service callbacks and the UE HTTP threads.

    create_timeout_s must exceed the leader's create wait, since the leader only
    answers once the task runs or the wait is over.
    """

    def __init__(self, node, callback_group, timeout_s: float = 3.0, create_timeout_s: float = 15.0):
        super().__init__(node, callback_group, timeout_s)
        self._create_timeout_s = create_timeout_s

    def create(self, leader: str, spec: TaskSpec, idempotency_key: str) -> CreateReply:
        client = self.client(CreateTask, leader, names.LEADER_CREATE_SERVICE)
        try:
            response = self.call(client, convert.fill_create_request(CreateTask.Request(), spec, idempotency_key),
                                 self._create_timeout_s)
        except Exception as exc:   # outcome unknown: retry with the same key (CONVENTIONS 7.2)
            return CreateReply(False, c.TIMEOUT, "leader %s: %s" % (leader, exc))
        return convert.create_reply_from_response(response)

    def destroy(self, leader: str, task_id: str, reason: str) -> DestroyReply:
        client = self.client(DestroyTask, leader, names.LEADER_DESTROY_SERVICE)
        try:
            response = self.call(client, DestroyTask.Request(task_id=task_id, reason=reason))
        except Exception as exc:
            return DestroyReply(False, c.TIMEOUT, "leader %s: %s" % (leader, exc))
        return convert.destroy_reply_from_response(response)

    def query(self, leader: str, task_id: str) -> QueryReply:
        client = self.client(QueryTask, leader, names.LEADER_QUERY_SERVICE)
        try:
            response = self.call(client, QueryTask.Request(task_id=task_id))
        except Exception as exc:
            return QueryReply(False, c.TIMEOUT, "leader %s: %s" % (leader, exc))
        return convert.query_reply_from_response(response, task_id)
