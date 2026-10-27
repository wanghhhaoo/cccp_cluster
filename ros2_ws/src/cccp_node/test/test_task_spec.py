import pytest

from cccp_node.core import constants as c
from cccp_node.core.task_spec import SpecError, TaskSpec, normalize_spec

TYPES = ("PKCI", "PKCT")


def norm(**kw):
    kw.setdefault("task_type", "pkci")
    return normalize_spec(TaskSpec(**kw), task_types=TYPES, max_chain_depth=5)


def rejected(**kw):
    with pytest.raises(SpecError) as info:
        norm(**kw)
    return info.value.error_code


def test_defaults_are_filled_and_values_normalized():
    spec = norm(task_type=" pkct ", mode="", origin="", name=" t ", target_nodes=(" a ", ""),
                params_json='{"bbox": [0, 0, 5, 5]}')
    assert (spec.task_type, spec.mode, spec.origin, spec.name, spec.target_nodes) == (
        "PKCT", c.MODE_LOCAL, c.ORIGIN_MANUAL, "t", ("a",))
    assert spec.participants == 1


@pytest.mark.parametrize("params", ["", "{}", '{"bbox": [1, 2, 0, 4]}', '{"bbox": [1, 2, 3]}',
                                    '{"bbox": [true, 2, 3, 4]}', '{"bbox": "1,2,3,4"}'])
def test_pkct_needs_a_positive_bbox(params):
    assert rejected(task_type="PKCT", params_json=params) == c.INVALID_INPUT


def test_pkct_bbox_accepted():
    assert norm(task_type="PKCT", params_json='{"bbox": [1, 2.5, 3, 4]}').task_type == "PKCT"


def test_unknown_enums_are_rejected():
    assert rejected(task_type="YOLO") == c.INVALID_INPUT
    assert rejected(mode="model_parallel") == c.INVALID_INPUT
    assert rejected(origin="cron") == c.INVALID_INPUT


def test_local_mode_takes_a_single_node():
    assert rejected(helper_count=1) == c.INVALID_INPUT
    assert rejected(target_nodes=("a", "b")) == c.INVALID_INPUT


def test_collaborative_modes_infer_helpers_from_targets():
    spec = norm(mode="fusion", target_nodes=("a", "b", "c"))
    assert spec.helper_count == 2 and spec.participants == 3
    assert norm(mode="redundant", helper_count=2, min_participants=2).participants == 2
    assert rejected(mode="redundant") == c.INVALID_INPUT
    assert rejected(mode="fusion", helper_count=2, target_nodes=("a", "b")) == c.INVALID_INPUT
    assert rejected(mode="fusion", helper_count=1, min_participants=3) == c.INVALID_INPUT
    assert rejected(mode="fusion", target_nodes=("a", "a")) == c.INVALID_INPUT


def test_chain_rules():
    assert norm(origin="chain", parent_task_id=7, chain_depth=5).parent_task_id == 7
    assert rejected(origin="chain", parent_task_id=7, chain_depth=6) == c.CHAIN_TOO_DEEP
    assert rejected(origin="chain", chain_depth=1) == c.INVALID_INPUT
    assert rejected(parent_task_id=7) == c.INVALID_INPUT


@pytest.mark.parametrize("field, value", [
    ("cpu_memory_required", -1.0), ("gpu_memory_required", float("nan")),
    ("latency_sensitivity", 1.5), ("params_json", "{bad"), ("params_json", "[1, 2]"),
])
def test_bad_values_are_rejected(field, value):
    assert rejected(**{field: value}) == c.INVALID_INPUT


def test_spec_round_trips_through_dict():
    spec = norm(mode="fusion", target_nodes=("a", "b"), params_json='{"bbox": [1, 2, 3, 4]}')
    assert TaskSpec.from_dict(spec.to_dict()) == spec
    with pytest.raises(ValueError):
        TaskSpec.from_dict({"task_type": "PKCI", "colour": "red"})
