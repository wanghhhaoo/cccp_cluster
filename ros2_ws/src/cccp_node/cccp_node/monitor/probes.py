"""CPU, memory and GPU sampling, ported from CCCP monitor_node.cpp.

Parsing is separated from I/O so it can be tested with captured outputs.
GPU handling:
  * discrete NVIDIA GPUs: nvidia-smi gives model, memory and utilization;
  * Jetson: model from device-tree/sysfs, utilization from the sysfs load
    file (0..1000). Its GPU shares system memory, so gpu_mem_total_mb is 0
    (CCCP reported system RAM there, which made the GPU look discrete).
Unavailable values are -1, as NodeStatus expects.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

NVIDIA_SMI_QUERY = ["nvidia-smi", "--query-gpu=name,memory.total,utilization.gpu,memory.used",
                    "--format=csv,noheader,nounits"]
JETSON_MODEL_PATHS = (
    "/sys/devices/gpu.0/model",
    "/sys/devices/17000000.gv11b/model",
    "/sys/devices/gpu.0/devfreq/17000000.gv11b/name",
    "/sys/firmware/devicetree/base/model",
)
# Orin paths need confirming on the target boards.
JETSON_LOAD_PATHS = (
    "/sys/devices/gpu.0/load",
    "/sys/devices/platform/gpu.0/load",
    "/sys/devices/platform/17000000.ga10b/load",
    "/sys/devices/platform/17000000.gv11b/load",
)


@dataclass(frozen=True)
class Hardware:
    cpu_model: str = ""
    cpu_cores: int = 0
    mem_total_mb: int = 0
    gpu_model: str = ""
    gpu_mem_total_mb: int = 0       # discrete GPU memory only
    gpu_kind: str = ""              # "nvidia", "jetson" or ""


@dataclass(frozen=True)
class Usage:
    cpu_usage: float = -1.0
    mem_usage: float = -1.0
    gpu_usage: float = -1.0
    gpu_mem_usage: float = -1.0


# ---------------------------------------------------------------- parsers

def parse_cpu_total(stat_text: str) -> tuple[int, int] | None:
    """(idle, total) jiffies of the aggregate 'cpu' line of /proc/stat."""
    for line in stat_text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu" and len(parts) >= 5:
            values = [int(v) for v in parts[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)   # idle + iowait
            return idle, sum(values)
    return None


def cpu_percent(previous: tuple[int, int] | None, current: tuple[int, int] | None) -> float:
    if previous is None or current is None:
        return -1.0
    total = current[1] - previous[1]
    idle = current[0] - previous[0]
    if total <= 0 or idle < 0:
        return -1.0
    return 100.0 * (total - idle) / total


def parse_meminfo(text: str) -> tuple[int, int]:
    """(total_mb, available_mb) from /proc/meminfo."""
    values = {}
    for line in text.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1]) // 1024
    return values.get("MemTotal", 0), values.get("MemAvailable", 0)


def parse_cpu_model(cpuinfo: str) -> str:
    fallback = ""
    for line in cpuinfo.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "model name" and value:
            return value
        if key == "Hardware" and value and not fallback:
            fallback = value
    return fallback


@dataclass(frozen=True)
class NvidiaSample:
    model: str
    mem_total_mb: int
    utilization: float
    mem_used_mb: int


def parse_nvidia_smi(output: str) -> NvidiaSample | None:
    """First GPU from `nvidia-smi --query-gpu=name,memory.total,utilization.gpu,memory.used`."""
    for line in output.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 4 or not fields[0]:
            continue
        try:
            return NvidiaSample(fields[0], int(float(fields[1])), float(fields[2]), int(float(fields[3])))
        except ValueError:
            return NvidiaSample(fields[0], 0, -1.0, 0)
    return None


def parse_tegrastats_gpu(line: str) -> float:
    """GPU utilization from a tegrastats line ('GR3D_FREQ 45%' or 'GR3D_FREQ 45%@[...]')."""
    match = re.search(r"GR3D_FREQ\s+(\d+)%", line)
    return float(match.group(1)) if match else -1.0


def strip_nul(text: str) -> str:
    return text.replace("\x00", "").strip()


# ------------------------------------------------------------------ probe

def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _run(command: list[str], timeout: float = 2.0) -> str | None:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


class SystemProbe:
    """Detects hardware once, then samples usage on demand (call about once per second)."""

    def __init__(self, read: Callable[[str], str | None] = _read,
                 run: Callable[[list[str]], str | None] = _run):
        self._read = read
        self._run = run
        self._previous_cpu = parse_cpu_total(read("/proc/stat") or "")
        self.hardware = self._detect()

    def _detect(self) -> Hardware:
        mem_total, _ = parse_meminfo(self._read("/proc/meminfo") or "")
        base = dict(cpu_model=parse_cpu_model(self._read("/proc/cpuinfo") or ""),
                    cpu_cores=os.cpu_count() or 0, mem_total_mb=mem_total)
        nvidia = parse_nvidia_smi(self._run(NVIDIA_SMI_QUERY) or "")
        if nvidia is not None:
            return Hardware(gpu_model=nvidia.model, gpu_mem_total_mb=nvidia.mem_total_mb, gpu_kind="nvidia", **base)
        for path in JETSON_MODEL_PATHS:
            model = strip_nul(self._read(path) or "")
            if model:
                return Hardware(gpu_model=model, gpu_kind="jetson", **base)
        return Hardware(**base)

    def sample(self) -> Usage:
        current = parse_cpu_total(self._read("/proc/stat") or "")
        cpu = cpu_percent(self._previous_cpu, current)
        self._previous_cpu = current
        total, available = parse_meminfo(self._read("/proc/meminfo") or "")
        mem = 100.0 * (total - available) / total if total > 0 else -1.0
        gpu, gpu_mem = -1.0, -1.0
        if self.hardware.gpu_kind == "nvidia":
            nvidia = parse_nvidia_smi(self._run(NVIDIA_SMI_QUERY) or "")
            if nvidia is not None:
                gpu = nvidia.utilization
                if nvidia.mem_total_mb > 0:
                    gpu_mem = 100.0 * nvidia.mem_used_mb / nvidia.mem_total_mb
        elif self.hardware.gpu_kind == "jetson":
            for path in JETSON_LOAD_PATHS:
                text = strip_nul(self._read(path) or "")
                if text.isdigit():
                    gpu = int(text) / 10.0
                    break
        return Usage(cpu, mem, gpu, gpu_mem)
