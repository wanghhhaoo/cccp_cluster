"""Topic and service names (CONVENTIONS 2.2). Relative names resolve inside the node namespace."""
from __future__ import annotations

from .core.naming import ros_namespace

# Control plane between nodes: the only cross-node traffic besides data streams (DESIGN 2.5).
NODE_STATUS_TOPIC = "/cluster/node_status"     # heartbeat, every node <-> every node
TASK_LIST_TOPIC = "/cluster/task_list"         # full task state, every node -> every node, on change
COMMAND_TOPIC = "/cluster/command"             # leader -> executors
EXEC_EVENT_TOPIC = "/cluster/exec_event"       # executors -> leader
REQUEST_TOPIC = "/cluster/request"             # gateways -> leader
REPLY_TOPIC = "/cluster/reply"                 # leader -> gateways

# Access layer.
TASK_STATUS_TOPIC = "/cluster/task_status"
API_CREATE_SERVICE = "api/task/create"
API_DESTROY_SERVICE = "api/task/destroy"
API_QUERY_SERVICE = "api/task/query"

# Within one node: executor -> control process.
EXEC_STATUS_TOPIC = "exec/status"

TASK_RESULT_TOPIC = "task/result"
TASK_RESULT_IMAGE_TOPIC = "task/result_image"


def on_node(node_name: str, relative: str) -> str:
    """Absolute name of a relative endpoint on another node: ('edge-01', 'task/result')."""
    return "%s/%s" % (ros_namespace(node_name), relative)
