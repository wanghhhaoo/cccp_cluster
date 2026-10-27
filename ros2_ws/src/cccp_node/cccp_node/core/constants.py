"""Enum values defined as constants in the ROS interfaces.

Pure-logic modules cannot import the generated interface packages, so the
values are mirrored here. test_constants.py reads the .msg/.srv files and
fails if the two ever drift apart.
"""

# TaskStatus.msg
PENDING = "Pending"
SCHEDULED = "Scheduled"
RUNNING = "Running"
FINISHED = "Finished"
FAILED = "Failed"
CANCELLED = "Cancelled"
TERMINAL = frozenset({FINISHED, FAILED, CANCELLED})

# TaskReport.msg (STATE_*)
REPORT_RUNNING = "Running"
REPORT_FINISHED = "Finished"
REPORT_FAILED = "Failed"
REPORT_STOPPED = "Stopped"

# CreateTask.srv
MODE_LOCAL = "local"
MODE_REDUNDANT = "redundant"
MODE_FUSION = "fusion"
MODES = frozenset({MODE_LOCAL, MODE_REDUNDANT, MODE_FUSION})

ORIGIN_MANUAL = "manual"
ORIGIN_SCHEDULE = "schedule"
ORIGIN_CHAIN = "chain"
ORIGIN_EVENT = "event"
ORIGINS = frozenset({ORIGIN_MANUAL, ORIGIN_SCHEDULE, ORIGIN_CHAIN, ORIGIN_EVENT})

# RunningTask.msg, StartTask.srv
ROLE_PRIMARY = "primary"
ROLE_HELPER = "helper"

# ErrorCodes.msg
INVALID_INPUT = "INVALID_INPUT"
NOT_READY = "NOT_READY"
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
BUSY = "BUSY"
TIMEOUT = "TIMEOUT"
CANCELLED_ERROR = "CANCELLED"
STALE_EPOCH = "STALE_EPOCH"
INFERENCE_ERROR = "INFERENCE_ERROR"
INSUFFICIENT_PEERS = "INSUFFICIENT_PEERS"
FUSION_ERROR = "FUSION_ERROR"
STALE_TERM = "STALE_TERM"
NO_LEADER = "NO_LEADER"
NO_FEASIBLE_NODE = "NO_FEASIBLE_NODE"
NOT_FOUND = "NOT_FOUND"
INVALID_STATE = "INVALID_STATE"
DISPATCH_FAILED = "DISPATCH_FAILED"
CHAIN_TOO_DEEP = "CHAIN_TOO_DEEP"
