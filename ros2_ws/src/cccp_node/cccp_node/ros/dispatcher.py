"""Leader -> executor commands on /cluster/command (DESIGN 2.5).

Only the leader publishes commands, so the control node attaches a publisher when
this node is elected and detaches it when it steps down. A command sent while
none is attached is lost, like any lost command: reconciliation resends it.
"""
from __future__ import annotations

import threading

from cccp_cluster_interfaces.msg import Command

from ..control.protocol import ExecCommand
from . import convert


class RosCommandSink:
    """Implements control.controller.CommandSink. send() runs on the controller thread."""

    def __init__(self, directory: convert.Directory):
        self._directory = directory
        self._publisher = None
        self._lock = threading.Lock()

    def attach(self, publisher) -> None:
        with self._lock:
            self._publisher = publisher

    def detach(self):
        """Returns the publisher, for the node to destroy."""
        with self._lock:
            publisher, self._publisher = self._publisher, None
            return publisher

    def send(self, command: ExecCommand) -> None:
        msg = convert.fill_command(Command(), command, self._directory)
        with self._lock:
            if self._publisher is not None:
                self._publisher.publish(msg)
