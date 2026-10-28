"""Task specification carried by CreateTask, and its validation."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields, replace
from typing import Iterable

from . import constants as c
from .input_source import ROS, parse_input


class SpecError(ValueError):
    """A submitted spec was rejected; error_code is an ErrorCodes value."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class TaskSpec:
    """The CreateTask request fields that describe a task (all but idempotency_key)."""

    task_type: str
    name: str = ""
    input: str = ""
    input_node: str = ""
    request_node: str = ""
    target_nodes: tuple[str, ...] = ()
    mode: str = c.MODE_LOCAL
    helper_count: int = 0
    min_participants: int = 0
    use_gpu: bool = False
    cpu_compute_required: float = 0.0
    cpu_memory_required: float = 0.0
    gpu_memory_required: float = 0.0
    latency_sensitivity: float = 0.0
    origin: str = c.ORIGIN_MANUAL
    origin_source: str = ""
    parent_task_id: int = 0
    chain_depth: int = 0
    drone_id: int = 0
    level: str = ""
    image_topic: str = ""
    params_json: str = ""

    @property
    def participants(self) -> int:
        """Minimum number of valid participants, the task primary included."""
        return self.min_participants or 1 + self.helper_count

    def to_dict(self) -> dict:
        data = asdict(self)
        data["target_nodes"] = list(self.target_nodes)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> TaskSpec:
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError("unknown TaskSpec fields: %s" % ", ".join(sorted(unknown)))
        values = dict(data)
        values["target_nodes"] = tuple(values.get("target_nodes", ()))
        return cls(**values)


def _check_pkct(params: dict) -> None:
    bbox = params.get("bbox")
    if not (isinstance(bbox, list) and len(bbox) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bbox)
            and bbox[2] > 0 and bbox[3] > 0):
        raise SpecError(c.INVALID_INPUT, 'PKCT requires params_json {"bbox": [x, y, width, height]} '
                                         "with positive size")


# Algorithm-specific params_json checks, as the CCCP bridge did before dispatch.
PARAM_CHECKS = {"PKCT": _check_pkct}


def normalize_spec(spec: TaskSpec, *, task_types: Iterable[str], max_chain_depth: int) -> TaskSpec:
    """Validate a CreateTask spec and fill in defaults; raises SpecError.

    Runs in the gateway before submission, so the replicated task table only
    ever stores well-formed specs.
    """
    task_types = frozenset(task_types)
    task_type = spec.task_type.strip().upper()
    if task_type not in task_types:
        raise SpecError(c.INVALID_INPUT, "task_type must be one of %s" % ", ".join(sorted(task_types)))
    mode = spec.mode.strip().lower() or c.MODE_LOCAL
    if mode not in c.MODES:
        raise SpecError(c.INVALID_INPUT, "mode must be one of %s" % ", ".join(sorted(c.MODES)))
    origin = spec.origin.strip().lower() or c.ORIGIN_MANUAL
    if origin not in c.ORIGINS:
        raise SpecError(c.INVALID_INPUT, "origin must be one of %s" % ", ".join(sorted(c.ORIGINS)))

    targets = tuple(n.strip() for n in spec.target_nodes if n.strip())
    if len(set(targets)) != len(targets):
        raise SpecError(c.INVALID_INPUT, "target_nodes contains duplicates")
    helper_count = spec.helper_count
    if mode == c.MODE_LOCAL:
        if helper_count:
            raise SpecError(c.INVALID_INPUT, "local mode takes no helpers")
        if len(targets) > 1:
            raise SpecError(c.INVALID_INPUT, "several target_nodes need mode redundant or fusion")
    else:
        if not helper_count and len(targets) > 1:
            helper_count = len(targets) - 1
        if helper_count < 1:
            raise SpecError(c.INVALID_INPUT, "%s mode needs helper_count >= 1" % mode)
        if targets and len(targets) != 1 + helper_count:
            raise SpecError(c.INVALID_INPUT, "target_nodes must list the primary and every helper")
    if spec.min_participants > 1 + helper_count:
        raise SpecError(c.INVALID_INPUT, "min_participants exceeds 1 + helper_count")

    if origin == c.ORIGIN_CHAIN:
        if spec.parent_task_id <= 0 or spec.chain_depth < 1:
            raise SpecError(c.INVALID_INPUT, "chain tasks need parent_task_id and chain_depth >= 1")
        if spec.chain_depth > max_chain_depth:
            raise SpecError(c.CHAIN_TOO_DEEP, "chain depth %d exceeds %d" % (spec.chain_depth, max_chain_depth))
    elif spec.parent_task_id or spec.chain_depth:
        raise SpecError(c.INVALID_INPUT, "parent_task_id and chain_depth are only for chain tasks")

    for name in ("cpu_compute_required", "cpu_memory_required", "gpu_memory_required"):
        value = getattr(spec, name)
        if not math.isfinite(value) or value < 0:
            raise SpecError(c.INVALID_INPUT, "%s must be a non-negative number" % name)
    if not 0.0 <= spec.latency_sensitivity <= 1.0:
        raise SpecError(c.INVALID_INPUT, "latency_sensitivity must be within [0, 1]")
    if spec.params_json.strip():
        try:
            params = json.loads(spec.params_json)
        except ValueError as exc:
            raise SpecError(c.INVALID_INPUT, "params_json is not valid JSON: %s" % exc) from exc
        if not isinstance(params, dict):
            raise SpecError(c.INVALID_INPUT, "params_json must be a JSON object")
    else:
        params = {}
    check = PARAM_CHECKS.get(task_type)
    if check is not None:
        check(params)

    try:
        source = parse_input(spec.input)
    except ValueError as exc:
        raise SpecError(c.INVALID_INPUT, str(exc)) from exc
    input_node = spec.input_node.strip()
    if source.kind == ROS:
        if input_node and input_node != source.owner:
            raise SpecError(c.INVALID_INPUT, "input_node %r contradicts input %r" % (input_node, spec.input))
        input_node = source.owner     # the data lives there: network scores are measured from it

    return replace(
        spec,
        input=source.uri,
        input_node=input_node,
        task_type=task_type,
        name=spec.name.strip(),
        request_node=spec.request_node.strip(),
        target_nodes=targets,
        mode=mode,
        helper_count=helper_count,
        origin=origin,
    )
