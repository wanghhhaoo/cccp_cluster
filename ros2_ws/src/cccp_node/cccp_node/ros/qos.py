"""QoS profiles (CONVENTIONS 3.3, DESIGN 2.5). Every publisher and subscriber takes its profile from here,
so the two ends of a topic cannot drift apart and silently fail to match."""
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data


def _profile(depth: int, reliability: ReliabilityPolicy,
             durability: DurabilityPolicy = DurabilityPolicy.VOLATILE) -> QoSProfile:
    return QoSProfile(depth=depth, history=HistoryPolicy.KEEP_LAST, reliability=reliability, durability=durability)


NODE_STATUS = _profile(1, ReliabilityPolicy.BEST_EFFORT)
EXECUTOR_STATUS = _profile(1, ReliabilityPolicy.BEST_EFFORT)
# TaskList: both ends must be TRANSIENT_LOCAL, or late joiners get no history. The subscription
# needs depth for every publisher: ROS 2 messages have no key, so its history is shared by all
# 12 nodes' TaskLists, and a depth of 1 would let them overwrite each other on (re)connection.
TASK_LIST_PUB = _profile(1, ReliabilityPolicy.RELIABLE, DurabilityPolicy.TRANSIENT_LOCAL)
TASK_LIST_SUB = _profile(32, ReliabilityPolicy.RELIABLE, DurabilityPolicy.TRANSIENT_LOCAL)
COMMAND = _profile(32, ReliabilityPolicy.RELIABLE)
EXEC_EVENT = _profile(32, ReliabilityPolicy.RELIABLE)
REQUEST = _profile(32, ReliabilityPolicy.RELIABLE)
REPLY = _profile(32, ReliabilityPolicy.RELIABLE)
TASK_STATUS = _profile(100, ReliabilityPolicy.RELIABLE)
TASK_RESULT = _profile(10, ReliabilityPolicy.RELIABLE)
TASK_RESULT_IMAGE = _profile(5, ReliabilityPolicy.RELIABLE)
CAMERA = qos_profile_sensor_data
