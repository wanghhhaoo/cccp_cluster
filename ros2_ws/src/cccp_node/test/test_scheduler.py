import pytest

from cccp_node.core import constants as c
from cccp_node.core.cluster_view import LinkView, NodeView
from cccp_node.core.scheduler import (
    SchedulerConfig, balanced_resource_score, hard_reason, least_resource_score, link_between,
    network_score, resource_reason, schedule,
)
from cccp_node.core.task_spec import TaskSpec

CONFIG = SchedulerConfig()


def node(name="n1", **kw):
    base = dict(inference_ready=True, algorithms_ready=("PKCI", "PKCT"), cpu_cores=8, cpu_usage=50.0,
                mem_total_mb=8192, mem_usage=50.0)
    base.update(kw)
    return NodeView(name, **base)


def task(**kw):
    kw.setdefault("cpu_compute_required", 1.0)
    kw.setdefault("cpu_memory_required", 1.0)
    return TaskSpec(task_type="PKCI", **kw)


def run(spec, nodes, leader="", assigned=None, avoid=(), reference=None):
    by_name = {n.name: n for n in nodes}
    return schedule(spec, by_name, reference=reference or leader or "n1", leader=leader,
                    assigned=assigned or {}, config=CONFIG, avoid=avoid)


# ------------------------------------------------- parity with the Java formulas

def test_scores_match_java_formulas():
    # Hand-computed from LeastResourceScorer / BalancedResourceScorer:
    # cpu used 8*0.5+0.8=4.8 -> free (8-5.8)/8=0.275; mem used 4+1.6=5.6 -> free (8-6.6)/8=0.175
    assert least_resource_score(task(), node()) == pytest.approx(22.5)
    # fractions (0.6, 0.7) and (0.725, 0.825): equal spread -> 50 + 50/2
    assert balanced_resource_score(task(), node()) == pytest.approx(75.0)


def test_network_penalties_match_java_formulas():
    link = LinkView("x", rtt_ms=150.0, jitter_ms=30.0, loss_rate=0.05)
    # latency 20 + jitter 10 + loss 20 at full sensitivity
    assert network_score(task(latency_sensitivity=1.0), link, CONFIG) == pytest.approx(50.0)
    assert network_score(task(latency_sensitivity=0.5), link, CONFIG) == pytest.approx(75.0)
    assert network_score(task(), link, CONFIG) == pytest.approx(75.0)          # default sensitivity 0.5
    assert network_score(task(latency_sensitivity=1.0), LinkView("x", 1000.0, 1000.0, 1.0), CONFIG) == 0.0


def test_gpu_scores_use_reported_gpu_memory():
    spec = task(use_gpu=True, gpu_memory_required=2.0)
    idle = node(gpu_model="RTX", gpu_mem_total_mb=8192, gpu_mem_usage=0.0)
    full = node(gpu_model="RTX", gpu_mem_total_mb=8192, gpu_mem_usage=75.0)
    assert least_resource_score(spec, idle) > least_resource_score(spec, full)


# ---------------------------------------------------------------------- filters

@pytest.mark.parametrize("override, reason", [
    (dict(protocol_version=2), "protocol_mismatch"),
    (dict(inference_ready=False), "not_ready"),
    (dict(algorithms_ready=("PKCT",)), "algorithm_unavailable"),
])
def test_hard_filters(override, reason):
    assert hard_reason(task(), node(**override), CONFIG) == reason


@pytest.mark.parametrize("spec, override, assigned, reason", [
    (task(), {}, 4, "busy"),
    (task(), dict(battery=0.1), 0, "battery_low"),
    (task(use_gpu=True), {}, 0, "gpu_unavailable"),
    (task(cpu_compute_required=5.0), {}, 0, "cpu_insufficient"),
    (task(cpu_memory_required=4.5), {}, 0, "mem_insufficient"),
    (task(use_gpu=True, gpu_memory_required=3.0),
     dict(gpu_model="RTX", gpu_mem_total_mb=4096, gpu_mem_usage=50.0), 0, "gpu_mem_insufficient"),
    # Jetson: no separate GPU memory, so GPU memory comes out of system memory
    (task(use_gpu=True, cpu_memory_required=2.0, gpu_memory_required=2.5), dict(gpu_model="Orin"), 0,
     "mem_insufficient"),
])
def test_resource_filters(spec, override, assigned, reason):
    assert resource_reason(spec, node(**override), assigned, CONFIG) == reason


def test_unreported_usage_falls_back_to_java_defaults():
    assert resource_reason(task(), node(cpu_usage=-1.0, mem_usage=-1.0, battery=-1.0), 0, CONFIG) is None


# --------------------------------------------------------------------- schedule

def test_picks_best_node_and_reports_rejections():
    result = run(task(), [node("n1", cpu_usage=80.0), node("n2", cpu_usage=10.0), node("n3", inference_ready=False)])
    assert result.ok and result.placement.primary == "n2"
    assert [s.name for s in result.candidates] == ["n2", "n1"]
    assert result.rejected == {"n3": "not_ready"}


def test_leader_and_load_penalties_spread_tasks():
    twins = [node("n1"), node("n2")]
    assert run(task(), twins).placement.primary == "n1"                       # tie broken by name
    assert run(task(), twins, leader="n1").placement.primary == "n2"
    assert run(task(), twins, assigned={"n1": 1}).placement.primary == "n2"
    assert run(task(), twins, avoid=("n1",)).rejected == {"n1": "recently_failed"}


def test_network_reference_prefers_nodes_close_to_the_input():
    far = node("n1", links=(LinkView("src", rtt_ms=400.0, jitter_ms=50.0, loss_rate=0.2),))
    near = node("n2", links=(LinkView("src", rtt_ms=10.0, jitter_ms=1.0, loss_rate=0.0),))
    spec = task(input_node="src", latency_sensitivity=1.0)
    assert run(spec, [far, near], reference="src").placement.primary == "n2"


def test_link_between_prefers_reference_measurement():
    a = node("a", links=(LinkView("b", rtt_ms=10.0),))
    b = node("b", links=(LinkView("a", rtt_ms=99.0),))
    nodes = {"a": a, "b": b}
    assert link_between(nodes, "a", "b").rtt_ms == 10.0
    assert link_between(nodes, "b", "a").rtt_ms == 99.0
    assert link_between(nodes, "a", "a").rtt_ms == 0.0
    assert link_between(nodes, "a", "zz").rtt_ms == 30.0                       # Java default


def test_collaborative_placement_and_degraded_helpers():
    nodes = [node("n1", cpu_usage=10.0), node("n2", cpu_usage=20.0), node("n3", cpu_usage=30.0)]
    full = run(task(mode="fusion", helper_count=2), nodes)
    assert full.placement.primary == "n1" and full.placement.helpers == ("n2", "n3")
    degraded = run(task(mode="redundant", helper_count=3, min_participants=2), nodes)
    assert degraded.placement.helpers == ("n2", "n3")
    short = run(task(mode="redundant", helper_count=3), nodes)
    assert not short.ok and short.error_code == c.NO_FEASIBLE_NODE


def test_explicit_targets_skip_scoring_but_not_hard_checks():
    nodes = [node("n1"), node("n2", cpu_usage=99.0)]
    result = run(task(mode="fusion", helper_count=1, target_nodes=("n2", "n1")), nodes)
    assert result.placement.primary == "n2" and result.placement.helpers == ("n1",)
    missing = run(task(target_nodes=("n9",)), nodes)
    assert missing.error_code == c.NO_FEASIBLE_NODE and missing.rejected == {"n9": "offline"}


def test_request_node_pins_primary_and_scores_helpers():
    nodes = [node("n1", cpu_usage=10.0), node("n2", cpu_usage=20.0), node("n3", cpu_usage=90.0)]
    result = run(task(mode="redundant", helper_count=1, request_node="n3"), nodes)
    assert result.placement.primary == "n3" and result.placement.helpers == ("n1",)
    alone = run(task(request_node="n3"), nodes)
    assert alone.placement.primary == "n3" and alone.placement.helpers == ()
    unready = run(task(request_node="n3"), [node("n3", inference_ready=False)])
    assert unready.rejected == {"n3": "not_ready"}


def test_no_nodes_online():
    result = run(task(), [])
    assert result.error_code == c.NO_FEASIBLE_NODE and "no nodes online" in result.message
