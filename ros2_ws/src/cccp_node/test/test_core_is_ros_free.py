"""cccp_node.core must stay importable without ROS or the consensus library (CONVENTIONS 6.2)."""
import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "cccp_node" / "core"
FORBIDDEN = ("rclpy", "rosidl", "builtin_interfaces", "sensor_msgs", "pysyncobj")


def imported_roots(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


def test_core_does_not_import_ros_or_consensus():
    offenders = []
    for path in sorted(CORE.glob("*.py")):
        for root in imported_roots(path):
            if root in FORBIDDEN or root.endswith("_interfaces"):
                offenders.append("%s imports %s" % (path.name, root))
    assert not offenders, offenders
