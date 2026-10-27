"""QoS profiles (CONVENTIONS 3.3). Every publisher and subscriber takes its profile from here."""
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data


def _profile(depth: int, reliability: ReliabilityPolicy) -> QoSProfile:
    return QoSProfile(depth=depth, history=HistoryPolicy.KEEP_LAST, reliability=reliability,
                      durability=DurabilityPolicy.VOLATILE)


NODE_STATUS = _profile(1, ReliabilityPolicy.BEST_EFFORT)
EXECUTOR_STATUS = _profile(1, ReliabilityPolicy.BEST_EFFORT)
TASK_REPORT = _profile(100, ReliabilityPolicy.RELIABLE)
TASK_STATUS = _profile(100, ReliabilityPolicy.RELIABLE)
TASK_RESULT = _profile(10, ReliabilityPolicy.RELIABLE)
TASK_RESULT_IMAGE = _profile(5, ReliabilityPolicy.RELIABLE)
CAMERA = qos_profile_sensor_data
