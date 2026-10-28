import pytest

from cccp_node.core import constants as c
from cccp_node.core.cluster_view import NodeView
from cccp_node.core.input_source import (
    AIRSIM, FETCH_EACH, FETCH_RELAY, LOCAL, ROS, InputPlan, airsim_topic, parse_input, plan_input,
)
from cccp_node.core.scheduler import SchedulerConfig, schedule
from cccp_node.core.task_spec import SpecError, TaskSpec, normalize_spec


# ------------------------------------------------------------------ parsing

def test_parse_addresses():
    assert parse_input("").kind == LOCAL and parse_input("local").kind == LOCAL
    sim = parse_input(" airsim://uav_5/0 ")
    assert (sim.kind, sim.vehicle, sim.camera, sim.shared, sim.uri) == (AIRSIM, "uav_5", "0", True, "airsim://uav_5/0")
    assert parse_input("airsim:///front").vehicle == ""            # AirSim's default vehicle
    ros = parse_input("ros://edge-03/lidar/points")
    assert (ros.kind, ros.owner, ros.topic, ros.shared) == (ROS, "edge-03", "lidar/points", False)


@pytest.mark.parametrize("bad", ["airsim", "airsim://uav_5", "ros://edge-03", "ros://edge-03/9bad", "file:///x",
                                 "ros://edge-03/a//b"])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_input(bad)


def test_airsim_topics_are_valid_ros_names():
    assert airsim_topic("uav_5", "0") == "input/airsim/uav_5/cam_0"
    assert airsim_topic("", "front-left") == "input/airsim/default/cam_front_left"
    assert airsim_topic("5th", "0") == "input/airsim/n5th/cam_0"


# ------------------------------------------------------------------ planning

def plan(uri, node="edge-01", **kw):
    return plan_input(parse_input(uri), node_name=node, default_topic="camera/image", **kw)


def test_local_input_keeps_previous_behaviour():
    assert plan("") == InputPlan("camera/image")
    assert plan("", image_topic="cam2/image") == InputPlan("cam2/image")


def test_ros_input_is_read_locally_on_its_node_and_streamed_elsewhere():
    assert plan("ros://edge-01/camera/image") == InputPlan("camera/image")
    remote = plan("ros://edge-03/camera/image")
    assert remote == InputPlan("/edge_03/camera/image/compressed", compressed=True)


def test_airsim_each_mode_fetches_on_every_participant():
    for role in ("primary", "helper"):
        assert plan("airsim://uav_5/0", role=role, primary_node="edge-02", airsim_mode=FETCH_EACH) == InputPlan(
            "input/airsim/uav_5/cam_0", fetch=("uav_5", "0"))


def test_airsim_relay_mode_only_primary_fetches():
    primary = plan("airsim://uav_5/0", role="primary", primary_node="edge-01", airsim_mode=FETCH_RELAY)
    assert primary.fetch == ("uav_5", "0")
    helper = plan("airsim://uav_5/0", role="helper", primary_node="edge-02", airsim_mode=FETCH_RELAY)
    assert helper == InputPlan("/edge_02/input/airsim/uav_5/cam_0/compressed", compressed=True)


# ---------------------------------------------------------- spec and placement

def norm(**kw):
    return normalize_spec(TaskSpec(task_type="PKCI", **kw), task_types=("PKCI",), max_chain_depth=5)


def test_spec_validates_input_and_derives_the_reference_node():
    assert norm(input="ros://edge-03/camera/image").input_node == "edge-03"
    assert norm(input=" airsim://uav_5/0").input == "airsim://uav_5/0"
    with pytest.raises(SpecError) as bad:
        norm(input="camera")
    assert bad.value.error_code == c.INVALID_INPUT
    with pytest.raises(SpecError):
        norm(input="ros://edge-03/camera/image", input_node="edge-01")


def nodes(*names):
    return {n: NodeView(n, inference_ready=True, algorithms_ready=("PKCI",), cpu_cores=8, cpu_usage=10.0,
                        mem_total_mb=8192, mem_usage=10.0) for n in names}


def place(spec, online, leader="edge-01"):
    return schedule(spec, online, reference=spec.input_node or leader, leader=leader, assigned={},
                    config=SchedulerConfig())


def test_node_bound_input_prefers_its_node_even_if_it_is_the_leader():
    spec = norm(input="ros://edge-01/camera/image")
    assert place(spec, nodes("edge-01", "edge-02", "edge-03")).placement.primary == "edge-01"


def test_node_bound_input_needs_its_node_online():
    result = place(norm(input="ros://edge-09/camera/image"), nodes("edge-01", "edge-02"))
    assert not result.ok and result.error_code == c.NO_FEASIBLE_NODE and result.rejected == {"edge-09": "offline"}


def test_shared_input_has_no_placement_constraint():
    result = place(norm(input="airsim://uav_5/0"), nodes("edge-01", "edge-02"))
    assert result.ok and result.placement.primary == "edge-02"      # leader penalty still applies
