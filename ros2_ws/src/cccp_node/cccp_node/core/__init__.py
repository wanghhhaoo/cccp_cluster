"""Pure decision and state logic of the cluster node.

Nothing in this package may import rclpy, interface packages or the consensus
library (CONVENTIONS 6.2): it must run under plain pytest and translate
module by module to C++ later. test_core_is_ros_free.py enforces this.
"""
