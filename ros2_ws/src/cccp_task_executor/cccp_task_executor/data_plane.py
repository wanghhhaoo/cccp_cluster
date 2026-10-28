"""Data-plane process (CONVENTIONS 6.1): task executor plus the PKCI and PKCT services.

Running them in one process cuts DDS participants per board; inference stays
out of the control process so it cannot starve Raft heartbeats.

The executor calls the algorithm engines of this process directly
(INFERENCE_BACKEND=local, the default), so frames are not serialized per call.
The engines are still exposed as <ns>/algorithms/* ROS services for other
processes and nodes; INFERENCE_BACKEND=ros makes the executor use those too.
Parameters given with --ros-args apply to all three nodes (e.g. device:=cuda:0).
"""
import os

import rclpy
from rclpy.executors import MultiThreadedExecutor

from .executor_node import TaskExecutor
from .inference import LocalBackend


def _enabled(name):
    return os.environ.get(name, "true").lower() == "true"


def main(args=None):
    rclpy.init(args=args)
    nodes = []
    backend = None
    try:
        pkci = pkct = None
        if _enabled("ENABLE_PKCI"):
            from collab_inference_ros.pkci_server import PkciServer
            pkci = PkciServer()
            nodes.append(pkci)
        if _enabled("ENABLE_PKCT"):
            from collab_inference_ros.pkct_server import PkctServer
            pkct = PkctServer()
            nodes.append(pkct)
        if os.environ.get("INFERENCE_BACKEND", "local") == "local":
            backend = LocalBackend(pkci.engine if pkci else None, pkct.engine if pkct else None)
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
