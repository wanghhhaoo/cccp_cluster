"""Data-plane process (CONVENTIONS 6.1): the task executor plus the engines of the stream algorithms.

Running them in one process cuts DDS participants per board; inference stays
out of the control process so it cannot starve the heartbeats (DESIGN 2.4).

Every stream algorithm (algorithms/) is loaded unless ENABLE_<NAME>=false, e.g.
ENABLE_PKCT=false. Its engine node (StreamAlgorithm.serve) exposes the engine as
<ns>/algorithms/* ROS services for other processes and nodes. The executor calls
the engines of this process directly (INFERENCE_BACKEND=local, the default), so
frames are not serialized per call; INFERENCE_BACKEND=ros makes it use the
services too. Batch algorithms need no engine here: each task runs as its own
process (batch.py). Parameters given with --ros-args apply to all nodes
(e.g. device:=cuda:0).
"""
import os

import rclpy
from rclpy.executors import MultiThreadedExecutor

from . import algorithms
from .executor_node import TaskExecutor
from .inference import LocalBackend


def _enabled(name):
    return os.environ.get(name, "true").lower() == "true"


def main(args=None):
    rclpy.init(args=args)
    nodes = []
    backend = None
    try:
        handlers = {}
        for cls in algorithms.STREAM:
            if _enabled("ENABLE_" + cls.name):
                server = cls.serve()
                nodes.append(server)
                handlers.update(cls.handlers(server.engine))
        if os.environ.get("INFERENCE_BACKEND", "local") == "local":
            backend = LocalBackend(handlers)
        nodes.append(TaskExecutor(backend=backend))
        executor = MultiThreadedExecutor()
        for node in nodes:
            executor.add_node(node)
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        if backend is not None:
            backend.shutdown()
        for node in nodes:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
