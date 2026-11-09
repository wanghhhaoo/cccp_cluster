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
    assert norm(origin="chain", parent_task_id="t7", chain_depth=5).parent_task_id == "t7"
    assert rejected(origin="chain", parent_task_id="t7", chain_depth=6) == c.CHAIN_TOO_DEEP
    assert rejected(origin="chain", chain_depth=1) == c.INVALID_INPUT
    assert rejected(parent_task_id="t7") == c.INVALID_INPUT


@pytest.mark.parametrize("field, value", [
    ("cpu_memory_required", -1.0), ("gpu_memory_required", float("nan")),
    ("latency_sensitivity", 1.5), ("params_json", "{bad"), ("params_json", "[1, 2]"),
    ("priority", 256), ("priority", -1), ("priority", True), ("preemption_policy", 1.5), ("name", "x" * 65),
])
def test_bad_values_are_rejected(field, value):
    assert rejected(**{field: value}) == c.INVALID_INPUT


def test_priority_is_passed_through_and_defaults_to_normal():
    assert (norm().priority, norm().preemption_policy) == (0, 0)
    spec = norm(priority=200, preemption_policy=0)
    assert spec.priority == 200 and TaskSpec.from_json(spec.to_json()) == spec
    assert TaskSpec.from_dict({"task_type": "PKCI"}).priority == 0     # a spec_json without the fields


def test_spec_round_trips_through_dict():
    spec = norm(mode="fusion", target_nodes=("a", "b"), params_json='{"bbox": [1, 2, 3, 4]}')
    assert TaskSpec.from_dict(spec.to_dict()) == spec
    with pytest.raises(ValueError):
        TaskSpec.from_dict({"task_type": "PKCI", "colour": "red"})


# ------------------------------------------------- per-type rules (batch jobs, DESIGN adjustment 13)

ALL_TYPES = ("PKCI", "PKCT", "FASTLIVO", "HETEROMAP", "RSSMAP")
SIX = tuple("edge-%02d" % i for i in range(1, 7))


def norm_any(**kw):
    return normalize_spec(TaskSpec(**kw), task_types=ALL_TYPES, max_chain_depth=5)


def rejected_any(**kw):
    with pytest.raises(SpecError) as info:
        norm_any(**kw)
    return info.value.error_code, str(info.value)


@pytest.mark.parametrize("task_type", ["FASTLIVO", "RSSMAP"])
def test_single_board_batch_jobs_run_local_only(task_type):
    assert norm_any(task_type=task_type).mode == c.MODE_LOCAL
    assert rejected_any(task_type=task_type, mode="redundant", helper_count=1) == (
        c.INVALID_INPUT, task_type + " requires local mode")


@pytest.mark.parametrize("task_type", ["FASTLIVO", "HETEROMAP", "RSSMAP"])
@pytest.mark.parametrize("field", ["input", "image_topic"])
def test_batch_jobs_take_no_input(task_type, field):
    kw = dict(mode="fusion", target_nodes=SIX) if task_type == "HETEROMAP" else {}
    value = "airsim://drone1/front" if field == "input" else "camera/image"
    assert rejected_any(task_type=task_type, **{field: value}, **kw) == (
        c.INVALID_INPUT, "batch jobs use node-configured input")


def test_heteromap_needs_fusion_and_six_ordered_targets():
    spec = norm_any(task_type="HETEROMAP", mode="fusion", target_nodes=SIX)
    assert (spec.target_nodes, spec.helper_count) == (SIX, 5)
    wrong_mode = (c.INVALID_INPUT, "HETEROMAP requires fusion mode")
    six = (c.INVALID_INPUT, "HETEROMAP needs 6 ordered target_nodes, the task primary first")
    assert rejected_any(task_type="HETEROMAP", mode="fusion", target_nodes=SIX[:5]) == six
    assert rejected_any(task_type="HETEROMAP", mode="fusion", helper_count=5) == six
    assert rejected_any(task_type="HETEROMAP", mode="redundant", target_nodes=SIX) == wrong_mode
    assert rejected_any(task_type="HETEROMAP") == wrong_mode


def test_rssmap_params():
    assert norm_any(task_type="RSSMAP", params_json='{"tx": "node_7", "score": true}').task_type == "RSSMAP"
    for params in ('{"tx": 7}', '{"score": "yes"}', '{"other": 1}'):
        assert rejected_any(task_type="RSSMAP", params_json=params) == (
            c.INVALID_INPUT, "invalid parameters for RSSMAP")


@pytest.mark.parametrize("task_type", ["FASTLIVO", "HETEROMAP"])
def test_other_batch_jobs_take_no_params(task_type):
    kw = dict(mode="fusion", target_nodes=SIX) if task_type == "HETEROMAP" else {}
    assert norm_any(task_type=task_type, params_json="{}", **kw).task_type == task_type
    assert rejected_any(task_type=task_type, params_json='{"tx": "a"}', **kw) == (
        c.INVALID_INPUT, "invalid parameters for " + task_type)


def test_pkci_passes_any_params_through():
    assert norm_any(task_type="PKCI", params_json='{"anything": [1, 2]}').params_json == '{"anything": [1, 2]}'


def test_configured_task_types_limit_what_the_gateway_accepts():
    with pytest.raises(SpecError):
        normalize_spec(TaskSpec(task_type="RSSMAP"), task_types=("PKCI",), max_chain_depth=5)
