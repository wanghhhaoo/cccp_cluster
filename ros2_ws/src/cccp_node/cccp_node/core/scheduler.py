"""Two-stage node selection: hard filters, then weighted scoring.

Ported from the CCCP Java scheduler (SchedulerServiceImpl + LeastResource,
BalancedResource and Network scorers), keeping its formulas and constants,
with these fixes:
  * the network score uses measured links between each candidate and a
    reference node (the input data's node, else the leader) instead of
    hard-coded values; bandwidth is not measured, so its term is dropped;
  * GPU free ratio and GPU allocation use reported GPU memory usage instead
    of the CPU ratio and a fixed 30 %;
  * the balanced score adds the requested GPU memory, not GPU compute,
    to the GPU memory dimension;
  * tasks already assigned to a node count against it (capacity filter and
    a per-task penalty), because CPU usage only reflects a new task seconds
    later and every decision in between would otherwise pick the same node.

Units follow the Java code: cpu_compute_required is in cores, memory
requirements are in GB.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Collection, Mapping

from . import constants as c
from .cluster_view import LinkView, NodeView
from .task_spec import TaskSpec

# Defaults the Java scheduler used when a node did not report a value.
DEFAULT_CORES = 4
DEFAULT_MEM_TOTAL_MB = 8192
DEFAULT_CPU_RATIO = 0.1
DEFAULT_MEM_RATIO = 0.2
DEFAULT_GPU_MEM_RATIO = 0.3
CPU_RESERVE = 0.1          # share of cores and memory treated as always in use
MEM_RESERVE = 0.2

# Network reference values for an aerial network (NetworkScorer).
IDEAL_LATENCY_MS = 50.0
IDEAL_JITTER_MS = 10.0
IDEAL_LOSS_RATE = 0.01
UNMEASURED_LINK = LinkView("", rtt_ms=30.0, jitter_ms=5.0, loss_rate=0.005)


@dataclass(frozen=True)
class SchedulerConfig:
    protocol_version: int = 1
    weights: Mapping[str, tuple[float, float, float]] = field(default_factory=dict)  # task_type -> (lrs, brs, net)
    default_weights: tuple[float, float, float] = (0.3, 0.3, 0.4)   # Java "INFERENCE" weights
    default_latency_sensitivity: float = 0.5
    leader_self_penalty: float = 10.0
    per_task_penalty: float = 5.0
    max_tasks_per_node: int = 4
    min_battery: float = 0.2

    def weights_for(self, task_type: str) -> tuple[float, float, float]:
        return self.weights.get(task_type, self.default_weights)


@dataclass(frozen=True)
class ScoredNode:
    name: str
    score: float
    lrs: float
    brs: float
    net: float


@dataclass(frozen=True)
class Placement:
    primary: str
    helpers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScheduleResult:
    placement: Placement | None
    candidates: tuple[ScoredNode, ...] = ()
    rejected: Mapping[str, str] = field(default_factory=dict)
    error_code: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.placement is not None


# ------------------------------------------------------------------ helpers

def _ratio(usage_percent: float, default: float) -> float:
    return usage_percent / 100.0 if usage_percent >= 0 else default


def _cores(node: NodeView) -> int:
    return node.cpu_cores or DEFAULT_CORES


def _mem_gb(node: NodeView) -> float:
    return (node.mem_total_mb or DEFAULT_MEM_TOTAL_MB) / 1024.0


def _discrete_gpu(node: NodeView) -> bool:
    """Discrete GPUs report their own memory; Jetson-style GPUs share system memory."""
    return node.gpu_mem_total_mb > 0


def _system_memory_request(spec: TaskSpec, node: NodeView) -> float:
    gpu_in_system = spec.use_gpu and not _discrete_gpu(node)
    return spec.cpu_memory_required + (spec.gpu_memory_required if gpu_in_system else 0.0)


def _clamp(score: float) -> float:
    return max(0.0, min(100.0, score))


# ------------------------------------------------------------------ filters

def hard_reason(spec: TaskSpec, node: NodeView, config: SchedulerConfig) -> str | None:
    """Checks that apply even to explicitly requested nodes."""
    if node.protocol_version != config.protocol_version:
        return "protocol_mismatch"
    if not node.inference_ready:
        return "not_ready"
    if spec.task_type not in node.algorithms_ready:
        return "algorithm_unavailable"
    return None


def resource_reason(spec: TaskSpec, node: NodeView, assigned: int, config: SchedulerConfig) -> str | None:
    if assigned >= config.max_tasks_per_node:
        return "busy"
    if 0 <= node.battery < config.min_battery:
        return "battery_low"
    if spec.use_gpu and not node.gpu_model:
        return "gpu_unavailable"
    cores = _cores(node)
    free_cores = round(cores * (1.0 - _ratio(node.cpu_usage, DEFAULT_CPU_RATIO)))
    if spec.cpu_compute_required > 0 and free_cores < spec.cpu_compute_required:
        return "cpu_insufficient"
    free_mem_mb = (node.mem_total_mb or DEFAULT_MEM_TOTAL_MB) * (1.0 - _ratio(node.mem_usage, DEFAULT_MEM_RATIO))
    request_mb = _system_memory_request(spec, node) * 1024.0
    if request_mb > 0 and free_mem_mb < request_mb:
        return "mem_insufficient"
    if spec.use_gpu and _discrete_gpu(node) and spec.gpu_memory_required > 0:
        free_gpu_mb = node.gpu_mem_total_mb * (1.0 - _ratio(node.gpu_mem_usage, DEFAULT_GPU_MEM_RATIO))
        if free_gpu_mb < spec.gpu_memory_required * 1024.0:
            return "gpu_mem_insufficient"
    return None


# ------------------------------------------------------------------- scores

def least_resource_score(spec: TaskSpec, node: NodeView) -> float:
    """Prefer nodes with the most capacity left after placing the task (0..100)."""
    cores, mem = _cores(node), _mem_gb(node)
    cpu_used = cores * _ratio(node.cpu_usage, DEFAULT_CPU_RATIO) + cores * CPU_RESERVE
    mem_used = mem * _ratio(node.mem_usage, DEFAULT_MEM_RATIO) + mem * MEM_RESERVE
    cpu_free = (cores - cpu_used - spec.cpu_compute_required) / cores
    mem_free = (mem - mem_used - _system_memory_request(spec, node)) / mem
    parts = [cpu_free, mem_free]
    if spec.use_gpu:
        if _discrete_gpu(node) and node.gpu_mem_usage >= 0:
            gpu_mem = node.gpu_mem_total_mb / 1024.0
            parts.append((gpu_mem * (1.0 - node.gpu_mem_usage / 100.0) - spec.gpu_memory_required) / gpu_mem)
        elif node.gpu_usage >= 0:
            parts.append(1.0 - node.gpu_usage / 100.0)
        else:
            parts.append(cpu_free)
    return _clamp(sum(parts) / len(parts) * 100.0)


def _balance(allocated: list[float], capacity: list[float]) -> float:
    fractions = [min(a / cap, 1.0) for a, cap in zip(allocated, capacity) if cap > 0]
    if not fractions:
        return 0.0
    if len(fractions) == 2:
        std = abs(fractions[0] - fractions[1]) / 2.0
    else:
        mean = sum(fractions) / len(fractions)
        std = math.sqrt(sum((f - mean) ** 2 for f in fractions) / len(fractions))
    return _clamp((1.0 - std) * 100.0)


def balanced_resource_score(spec: TaskSpec, node: NodeView) -> float:
    """Prefer placements that keep CPU, GPU memory and memory usage even (0..100)."""
    cores, mem = _cores(node), _mem_gb(node)
    capacity = [float(cores), mem]
    allocated = [cores * _ratio(node.cpu_usage, DEFAULT_CPU_RATIO) + cores * CPU_RESERVE,
                 mem * _ratio(node.mem_usage, DEFAULT_MEM_RATIO) + mem * MEM_RESERVE]
    requested = [allocated[0] + spec.cpu_compute_required, allocated[1] + _system_memory_request(spec, node)]
    if spec.use_gpu and _discrete_gpu(node):
        gpu_mem = node.gpu_mem_total_mb / 1024.0
        gpu_used = gpu_mem * _ratio(node.gpu_mem_usage, DEFAULT_GPU_MEM_RATIO)
        capacity.append(gpu_mem)
        allocated.append(gpu_used)
        requested.append(gpu_used + spec.gpu_memory_required)
    without, with_task = _balance(allocated, capacity), _balance(requested, capacity)
    return _clamp(50.0 + (50.0 + with_task - without) / 2.0)


def link_between(nodes: Mapping[str, NodeView], a: str, b: str) -> LinkView:
    """Best available measurement of the a-b link: a's view, else b's, else defaults."""
    if a == b:
        return LinkView(a, rtt_ms=0.0, jitter_ms=0.0, loss_rate=0.0)
    for owner, peer in ((a, b), (b, a)):
        node = nodes.get(owner)
        link = node.link_to(peer) if node else None
        if link is not None and link.measured:
            return link
    return UNMEASURED_LINK


def network_score(spec: TaskSpec, link: LinkView, config: SchedulerConfig) -> float:
    """Start from 100 and subtract latency, jitter and loss penalties (0..100).

    The RTT stands in for latency. All three penalties scale with the task's
    latency sensitivity (the Java code had separate jitter and reliability
    sensitivities, which CreateTask does not carry).
    """
    sensitivity = spec.latency_sensitivity or config.default_latency_sensitivity
    score = 100.0
    rtt = max(link.rtt_ms, 0.0)
    jitter = max(link.jitter_ms, 0.0)
    loss = max(link.loss_rate, 0.0)
    if rtt > IDEAL_LATENCY_MS:
        score -= min((rtt - IDEAL_LATENCY_MS) / 50.0 * 10.0, 40.0) * sensitivity
    if jitter > IDEAL_JITTER_MS:
        score -= min((jitter - IDEAL_JITTER_MS) / 10.0 * 5.0, 30.0) * sensitivity
    if loss > IDEAL_LOSS_RATE:
        score -= min((loss - IDEAL_LOSS_RATE) * 100.0 * 5.0, 50.0) * sensitivity
    return _clamp(score)


# ----------------------------------------------------------------- schedule

def schedule(
    spec: TaskSpec,
    nodes: Mapping[str, NodeView],
    *,
    reference: str,
    leader: str,
    assigned: Mapping[str, int],
    config: SchedulerConfig,
    avoid: Collection[str] = (),
) -> ScheduleResult:
    """Choose the task primary and helpers among the online nodes.

    nodes: online nodes only. reference: node whose links define the network
    score (spec.input_node if set, else the leader). assigned: active tasks
    per node according to the task table. avoid: nodes that recently failed
    to start this task.

    target_nodes fixes the whole placement. request_node fixes only the
    primary; helpers, if the mode needs any, are still chosen by score.
    """
    if spec.target_nodes:
        return _explicit(spec, spec.target_nodes, nodes, config)
    pinned = spec.request_node
    if pinned:
        pinned_check = _explicit(spec, (pinned,), nodes, config)
        if not pinned_check.ok or not spec.helper_count:
            return pinned_check

    rejected: dict[str, str] = {}
    candidates = []
    w_lrs, w_brs, w_net = config.weights_for(spec.task_type)
    for name in sorted(nodes):
        if name == pinned:
            continue
        node = nodes[name]
        load = assigned.get(name, 0)
        reason = ("recently_failed" if name in avoid else None) or hard_reason(spec, node, config) \
            or resource_reason(spec, node, load, config)
        if reason:
            rejected[name] = reason
            continue
        lrs = least_resource_score(spec, node)
        brs = balanced_resource_score(spec, node)
        net = network_score(spec, link_between(nodes, reference, name), config)
        score = w_lrs * lrs + w_brs * brs + w_net * net - config.per_task_penalty * load
        if name == leader:
            score -= config.leader_self_penalty
        candidates.append(ScoredNode(name, score, lrs, brs, net))
    candidates.sort(key=lambda s: (-s.score, s.name))

    needed = spec.participants - (1 if pinned else 0)
    if len(candidates) < needed:
        return ScheduleResult(None, tuple(candidates), rejected, c.NO_FEASIBLE_NODE,
                              "need %d more node(s), %d feasible; %s"
                              % (needed, len(candidates), _describe(rejected)))
    ranked = [s.name for s in candidates]
    primary = pinned or ranked.pop(0)
    return ScheduleResult(Placement(primary, tuple(ranked[:spec.helper_count])), tuple(candidates), rejected)


def _explicit(spec: TaskSpec, names: tuple[str, ...], nodes: Mapping[str, NodeView],
              config: SchedulerConfig) -> ScheduleResult:
    rejected = {}
    for name in names:
        node = nodes.get(name)
        reason = "offline" if node is None else hard_reason(spec, node, config)
        if reason:
            rejected[name] = reason
    if rejected:
        return ScheduleResult(None, (), rejected, c.NO_FEASIBLE_NODE,
                              "requested node(s) unavailable; %s" % _describe(rejected))
    return ScheduleResult(Placement(names[0], tuple(names[1:])))


def _describe(rejected: Mapping[str, str]) -> str:
    return ", ".join("%s: %s" % item for item in sorted(rejected.items())) or "no nodes online"
