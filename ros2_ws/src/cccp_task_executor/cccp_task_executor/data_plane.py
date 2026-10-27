"""Data-plane process (CONVENTIONS 6.1): task executor plus the PKCI and PKCT services.

Running them in one process cuts DDS participants per board; inference stays
out of the control process so it cannot starve Raft heartbeats.
Parameters given with --ros-args apply to all three nodes (e.g. device:=cuda:0).
"""
import os

import rclpy
from rclpy.executors import MultiThreadedExecutor

from .executor_node import TaskExecutor


def _enabled(name):
    return os.environ.get(name, "true").lower() == "true"


def main(args=None):
    rclpy.init(args=args)
    nodes = []
    try:
        if _enabled("ENABLE_PKCI"):
            from collab_inference_ros.pkci_server import PkciServer
            nodes.append(PkciServer())
        if _enabled("ENABLE_PKCT"):
            from collab_inference_ros.pkct_server import PkctServer
            nodes.append(PkctServer())
        nodes.append(TaskExecutor())
        executor = MultiThreadedExecutor()
        for node in nodes:
            executor.add_node(node)
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        for node in nodes:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
