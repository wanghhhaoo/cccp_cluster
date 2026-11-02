"""Gateway -> leader requests on /cluster/request (DESIGN 2.5, 4.1).

Every gateway can publish a Request; only the leader it names subscribes and
answers on /cluster/reply, which every gateway reads. A destroy or query of
something that is not a task id cannot exist anywhere (leaders only make UUIDs)
and is answered NOT_FOUND here, without a round trip.
"""
from __future__ import annotations

from cccp_cluster_interfaces.msg import Request as RequestMsg

from ..control.protocol import Reply, Request
from ..core import constants as c
from . import convert


class RosRequestTransport:
    """Implements control.gateway.RequestTransport; on_reply is Gateway.on_reply."""

    def __init__(self, publisher, directory: convert.Directory, on_reply):
        self._publisher = publisher
        self._directory = directory
        self._on_reply = on_reply

    def send(self, request: Request) -> None:
        try:
            msg = convert.fill_request(RequestMsg(), request, self._directory)
        except ValueError:
            self._on_reply(Reply(request.origin_node, request.origin_incarnation, request.request_id,
                                 request.target_leader, request.term, c.NOT_FOUND,
                                 "task %s does not exist" % request.task_id))
            return
        self._publisher.publish(msg)
