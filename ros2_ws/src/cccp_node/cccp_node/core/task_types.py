"""Task types (algorithms): what the control plane needs to know about each one.

Each algorithm is described by one TaskType entry in REGISTRY. The gateway validates
requests against that entry, and the leader's controller and the task table read their
policies from it. Apart from this module, nothing in core/ or control/ branches on a
task type name (test_task_types.py checks this). How an algorithm actually runs belongs
to the data plane (cccp_task_executor).

REGISTRY order is the wire order of the algorithms_ready bit mask (NodeStatus.msg,
ros/convert.py). New types are appended at the end. A position is never reordered or
reused. The mask is a uint8, so it holds at most 8 types.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from . import constants as c

STREAM = "stream"   # processes an input stream frame by frame, runs until it is stopped
BATCH = "batch"     # one process per task on node-configured input, ends on its own
KINDS = frozenset({STREAM, BATCH})

ParamCheck = Callable[[dict], None]   # raises ValueError(message) when params_json is not acceptable


def any_params(params: dict) -> None:
    """Pass params_json through unchecked; the algorithm interprets it."""


@dataclass(frozen=True)
class TaskType:
    name: str                          # task_type on the wire; also names the capability bit
    kind: str                          # STREAM or BATCH. Batch jobs take no input or image_topic
    modes: frozenset = c.MODES         # accepted CreateTask modes
    participants: int = 0              # > 0: exactly this many ordered target_nodes, the task primary first
    helpers_required: bool = False     # a failed helper fails the whole task instead of being replaced
    check_params: ParamCheck = any_params
    artifacts: tuple[str, ...] = ()    # file suffixes left in CCCP_JOB_STORE; non-empty: Finished has a result_url


def _check_pkct(params: dict) -> None:
    bbox = params.get("bbox")
    if not (isinstance(bbox, list) and len(bbox) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bbox)
            and bbox[2] > 0 and bbox[3] > 0):
        raise ValueError('PKCT requires params_json {"bbox": [x, y, width, height]} with positive size')


def _only(name: str, **types: type) -> ParamCheck:
    """A check accepting just the given optional keys, each of the given JSON type."""
    def check(params: dict) -> None:
        if set(params) - set(types) or any(
                key in params and not isinstance(params[key], kind) for key, kind in types.items()):
            raise ValueError("invalid parameters for " + name)
    return check


LOCAL = frozenset({c.MODE_LOCAL})

REGISTRY: tuple[TaskType, ...] = (
    TaskType(c.PKCI, STREAM),
    TaskType(c.PKCT, STREAM, check_params=_check_pkct),
    # Lidar mapping from a recorded bag, one node.
    TaskType(c.FASTLIVO, BATCH, LOCAL, check_params=_only(c.FASTLIVO), artifacts=(".pcd",)),
    # Collaborative BEV map: a fusion node and five helpers id_0..id_4, each needed for every frame.
    TaskType(c.HETEROMAP, BATCH, frozenset({c.MODE_FUSION}), participants=6, helpers_required=True,
             check_params=_only(c.HETEROMAP), artifacts=(".npz",)),
    # Radio propagation map, one node.
    TaskType(c.RSSMAP, BATCH, LOCAL, check_params=_only(c.RSSMAP, tx=str, score=bool), artifacts=(".npz",)),
)

BY_NAME = {t.name: t for t in REGISTRY}
ALGORITHMS = tuple(t.name for t in REGISTRY)                 # bit i of algorithms_ready
BATCH_TYPES = tuple(t.name for t in REGISTRY if t.kind == BATCH)
ARTIFACT_SUFFIXES = tuple(sorted({s for t in REGISTRY for s in t.artifacts}))
# Batch roles are "primary" and id_0..id_<n-1> for the helpers (cccp_task_executor/batch.py).
ARTIFACT_ROLES = ("primary",) + tuple("id_%d" % i for i in range(max(t.participants for t in REGISTRY) - 1))


def find(name: str) -> Optional[TaskType]:
    """The registered type of that name, or None (a newer node's type, or a typo)."""
    return BY_NAME.get(name)
