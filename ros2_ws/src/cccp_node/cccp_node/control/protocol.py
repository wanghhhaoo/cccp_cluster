"""The control plane's messages between nodes, free of ROS (DESIGN 2.5, appendix C.5-C.8).

Every cross-node exchange is a message on a topic, never a blocking call:

- ExecCommand, leader -> executors: start or stop a task. One command may
  address several nodes (targets); executors ignore commands for others.
- ExecEvent, executor -> leader: what happened (running, rejected, failed,
  stopped). Doubles as the receipt for a command; losing it only delays
  things, since heartbeats show the same state a moment later.
- Request, gateway -> leader: create, destroy or query a task. It names the
  one leader that may handle it (target_leader), so a request is handled at
  most once even while two nodes believe they lead.
- Reply, leader -> gateway: the outcome, matched to its request by
  (origin_node, origin_incarnation, request_id).

ros/convert.py maps these onto the ROS messages; node ids replace names there.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.generation import Generation
from ..core.task_spec import TaskSpec
from ..core.task_table import TaskRecord

# ExecCommand.op
START = "start"
STOP = "stop"

# ExecEvent.kind
RUNNING = "running"       # running; also the receipt for a start, sent again for a repeated start
REJECTED = "rejected"     # refused a command: see error_code
FAILED = "failed"         # failed while running
STOPPED = "stopped"       # stopped; also the receipt for a stop, sent even if it was not running
FINISHED = "finished"     # reserved for tasks that end by themselves

# Request.op
CREATE = "create"
DESTROY = "destroy"
QUERY = "query"


@dataclass(frozen=True)
class ExecCommand:
    term: int
    request_id: int                  # increasing within (leader, term)
    targets: tuple[str, ...]
    op: str
    task_id: str
    generation: Generation
    stop_kind: str = ""              # constants.STOP_*, for op STOP
    reason: str = ""                 # for logs
    primary: str = ""                # the rest only for op START
    helpers: tuple[str, ...] = ()
    idempotency_key: str = ""
    name: str = ""
    spec: TaskSpec | None = None
    leader: str = ""


@dataclass(frozen=True)
class ExecEvent:
    node: str
    incarnation: int                 # of the executor process
    seq: int                         # per incarnation; 0: not numbered, never treated as a repeat
    term: int                        # a receipt carries its command's term and request_id;
    request_id: int                  # a spontaneous event has request_id 0
    task_id: str
    generation: Generation
    kind: str
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True)
class Request:
    origin_node: str
    origin_incarnation: int          # a restarted gateway starts request_id over
    request_id: int
    target_leader: str
    term: int                        # the term the gateway believes in, for logs
    op: str
    idempotency_key: str = ""        # CREATE
    spec: TaskSpec | None = None     # CREATE, already validated and normalized by the gateway
    task_id: str = ""                # DESTROY, QUERY
    reason: str = ""                 # DESTROY


@dataclass(frozen=True)
class Reply:
    origin_node: str
    origin_incarnation: int
    request_id: int
    leader: str
    term: int
    error_code: str = ""             # "" on success
    message: str = ""
    task_id: str = ""                # set once the task exists, even when error_code is set
    duplicate: bool = False
    status: str = ""                 # snapshot of the task
    generation: Generation = Generation()
    primary: str = ""
    helpers: tuple[str, ...] = ()
    record: TaskRecord | None = None  # QUERY only: the whole record

    @property
    def success(self) -> bool:
        return not self.error_code

    def answers(self, request: Request) -> bool:
        return (self.origin_node, self.origin_incarnation, self.request_id) == (
            request.origin_node, request.origin_incarnation, request.request_id)
