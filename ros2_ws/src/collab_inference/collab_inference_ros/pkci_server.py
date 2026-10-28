from pathlib import Path

import rclpy
from ament_index_python.packages import get_package_share_directory
from rclpy.node import Node

from collab_inference_interfaces.srv import Classify, FuseClassification

from .engines import PkciEngine


class PkciServer(Node):
    """ROS services over a PkciEngine. `engine` is also used in-process by the task executor."""

    def __init__(self):
        super().__init__("pkci_inference_server")
        share = Path(get_package_share_directory("collab_inference"))
        self.declare_parameter("device", "cuda:0")
        self.declare_parameter(
            "weights", str(share / "weights" / "pkci_mvpn_plong_s2.pt")
        )
        self.declare_parameter("classify_service", "algorithms/pkci/classify")
        self.declare_parameter("fuse_service", "algorithms/pkci/fuse")

        device = self.get_parameter("device").value
        weights = self.get_parameter("weights").value
        self.get_logger().info(
            "Loading PKCI classifier on %s from %s" % (device, weights)
        )
        self.engine = PkciEngine(weights=weights, device=device, logger=self.get_logger())

        self.create_service(
            Classify,
            self.get_parameter("classify_service").value,
            self.engine.classify,
        )
        self.create_service(
            FuseClassification,
            self.get_parameter("fuse_service").value,
            self.engine.fuse,
        )
        self.get_logger().info("PKCI services are ready")


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = PkciServer()
        rclpy.spin(node)
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
