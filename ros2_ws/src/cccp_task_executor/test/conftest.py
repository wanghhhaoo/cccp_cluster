"""Without ROS (development machines), run these tests on cccp_node's fake_ros, like the cluster tests.

With ROS installed (colcon test) nothing changes: the real rclpy and interface packages are used.
"""
import sys
from pathlib import Path

try:
    import rclpy  # noqa: F401
except ImportError:
    SRC = Path(__file__).resolve().parents[2]
    for path in (SRC / "cccp_node" / "test", SRC / "cccp_node", SRC / "cccp_task_executor"):
        sys.path.insert(0, str(path))
    import fake_ros
    fake_ros.install()
