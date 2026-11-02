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


def default_node_id(node_name: str) -> int:
    """The node id a deployment gets unless NODE_ID says otherwise: the number the name ends with
    ('edge-07' -> 7, DESIGN 3.8). Raises ValueError when there is none in 1..254."""
    match = re.search(r"(\d+)$", node_name.strip())
    node_id = int(match.group(1)) if match else 0
    if not 1 <= node_id <= 254:
        raise ValueError("cannot derive a node id (1..254) from %r; set node_id" % node_name)
    return node_id
