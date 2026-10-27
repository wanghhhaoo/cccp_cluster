"""Topic and service names (CONVENTIONS 2.2). Relative names resolve inside the node namespace."""
from __future__ import annotations

from .core.naming import ros_namespace

NODE_STATUS_TOPIC = "/cluster/node_status"
TASK_REPORT_TOPIC = "/cluster/task_report"
TASK_STATUS_TOPIC = "/cluster/task_status"

EXEC_STATUS_TOPIC = "exec/status"
EXEC_START_SERVICE = "exec/task/start"
EXEC_STOP_SERVICE = "exec/task/stop"

API_CREATE_SERVICE = "api/task/create"
API_DESTROY_SERVICE = "api/task/destroy"
API_QUERY_SERVICE = "api/task/query"

TASK_RESULT_TOPIC = "task/result"
TASK_RESULT_IMAGE_TOPIC = "task/result_image"


def on_node(node_name: str, relative: str) -> str:
    """Absolute name of a relative endpoint on another node: ('edge-01', 'exec/task/start')."""
    return "%s/%s" % (ros_namespace(node_name), relative)
