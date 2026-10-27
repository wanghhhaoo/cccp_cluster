"""Node name -> ROS 2 namespace (CONVENTIONS 2.1), same rule as CCCP naming.py.

deploy/start_node.sh applies the same rule in shell; keep them in sync.
"""
from __future__ import annotations

import re


def ros_namespace(node_name: str) -> str:
    """'edge-01' -> '/edge_01'; '3rd.board' -> '/n3rd_board'."""
    token = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", node_name.strip()))
    if not token or token[0].isdigit():
        token = "n" + token
    return "/" + token
