from pathlib import Path

import rclpy
from ament_index_python.packages import get_package_share_directory
from rclpy.node import Node

from collab_inference_interfaces.srv import (
    ElectPrimary,
    InitTracker,
    ResetTracker,
    Track,
)

from .engines import PkctEngine


class PkctServer(Node):
    """ROS services over a PkctEngine. `engine` is also used in-process by the task executor."""

    def __init__(self):
        super().__init__("pkct_inference_server")
        share = Path(get_package_share_directory("collab_inference"))
        self.declare_parameter("device", "cuda:0")
        self.declare_parameter("weights", str(share / "weights" / "pkct_b0bgt.pt"))
        self.declare_parameter(
            "calibration", str(share / "weights" / "pkct_b0bgt.calib.json")
        )
        self.declare_parameter("max_trackers", 4)
        self.declare_parameter("init_service", "algorithms/pkct/init")
        self.declare_parameter("track_service", "algorithms/pkct/track")
        self.declare_parameter("elect_service", "algorithms/pkct/elect")
        self.declare_parameter("reset_service", "algorithms/pkct/reset")

        device = self.get_parameter("device").value
        self.engine = PkctEngine(
            weights=self.get_parameter("weights").value,
            device=device,
            calibration_path=self.get_parameter("calibration").value,
            max_trackers=int(self.get_parameter("max_trackers").value),
            logger=self.get_logger(),
        )

        self.create_service(
            InitTracker, self.get_parameter("init_service").value, self.engine.init_tracker
        )
        self.create_service(
            Track, self.get_parameter("track_service").value, self.engine.track
        )
        self.create_service(
            ElectPrimary, self.get_parameter("elect_service").value, self.engine.elect
        )
        self.create_service(
            ResetTracker, self.get_parameter("reset_service").value, self.engine.reset
        )
        self.get_logger().info(
            "PKCT services are ready on %s (max_trackers=%d)"
            % (device, self.engine.max_trackers)
        )


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = PkctServer()
        rclpy.spin(node)
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
