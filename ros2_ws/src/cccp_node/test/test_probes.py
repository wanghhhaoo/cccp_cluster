import pytest

from cccp_node.monitor.probes import (
    NVIDIA_SMI_QUERY, SystemProbe, cpu_percent, parse_cpu_model, parse_cpu_total, parse_meminfo,
    parse_nvidia_smi, parse_tegrastats_gpu,
)

STAT_1 = "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 50 0 50 350 50 0 0 0 0 0\n"
STAT_2 = "cpu  200 0 200 1300 300 0 0 0 0 0\ncpu0 1 1 1 1 1\n"
MEMINFO = "MemTotal:        8192000 kB\nMemFree:  100 kB\nMemAvailable:    2048000 kB\n"
CPUINFO_X86 = "processor\t: 0\nmodel name\t: Intel(R) Xeon(R)\n"
CPUINFO_ARM = "processor\t: 0\nHardware\t: NVIDIA Orin\n"


def test_cpu_and_memory_parsing():
    assert parse_cpu_total(STAT_1) == (800, 1000)
    # delta: total 1000, idle 800 -> 20 % busy
    assert cpu_percent(parse_cpu_total(STAT_1), parse_cpu_total(STAT_2)) == pytest.approx(20.0)
    assert cpu_percent(None, (1, 2)) == -1.0 and cpu_percent((5, 10), (5, 10)) == -1.0
    assert parse_meminfo(MEMINFO) == (8000, 2000)
    assert parse_cpu_model(CPUINFO_X86) == "Intel(R) Xeon(R)"
    assert parse_cpu_model(CPUINFO_ARM) == "NVIDIA Orin"


def test_gpu_tool_parsing():
    sample = parse_nvidia_smi("NVIDIA GeForce RTX 4090, 24564, 37, 1200\n")
    assert (sample.model, sample.mem_total_mb, sample.utilization, sample.mem_used_mb) == (
        "NVIDIA GeForce RTX 4090", 24564, 37.0, 1200)
    assert parse_nvidia_smi("") is None
    assert parse_nvidia_smi("Tesla, [N/A], [N/A], [N/A]").utilization == -1.0
    line = "RAM 2316/7620MB (lfb 2x4MB) CPU [1%@729,0%@729] EMC_FREQ 0% GR3D_FREQ 45%@[305] VIC_FREQ 0%"
    assert parse_tegrastats_gpu(line) == 45.0
    assert parse_tegrastats_gpu("GR3D_FREQ 7%") == 7.0 and parse_tegrastats_gpu("RAM 1/2MB") == -1.0


def fake_fs(files):
    state = {"stat": iter(files.pop("/proc/stat"))}

    def read(path):
        if path == "/proc/stat":
            return next(state["stat"])
        return files.get(path)
    return read


def test_probe_with_discrete_gpu():
    read = fake_fs({"/proc/stat": [STAT_1, STAT_2], "/proc/meminfo": MEMINFO, "/proc/cpuinfo": CPUINFO_X86})
    probe = SystemProbe(read=read, run=lambda cmd: "RTX, 8000, 50, 2000\n" if cmd == NVIDIA_SMI_QUERY else None)
    hw = probe.hardware
    assert (hw.gpu_kind, hw.gpu_model, hw.gpu_mem_total_mb, hw.mem_total_mb) == ("nvidia", "RTX", 8000, 8000)
    usage = probe.sample()
    assert usage.cpu_usage == pytest.approx(20.0) and usage.mem_usage == pytest.approx(75.0)
    assert usage.gpu_usage == 50.0 and usage.gpu_mem_usage == pytest.approx(25.0)


def test_probe_on_jetson_reports_shared_memory():
    files = {"/proc/stat": [STAT_1, STAT_2], "/proc/meminfo": MEMINFO, "/proc/cpuinfo": CPUINFO_ARM,
             "/sys/firmware/devicetree/base/model": "NVIDIA Jetson AGX Orin\x00",
             "/sys/devices/platform/gpu.0/load": "455\n"}
    probe = SystemProbe(read=fake_fs(files), run=lambda cmd: None)
    hw = probe.hardware
    assert (hw.gpu_kind, hw.gpu_model, hw.gpu_mem_total_mb) == ("jetson", "NVIDIA Jetson AGX Orin", 0)
    usage = probe.sample()
    assert usage.gpu_usage == pytest.approx(45.5) and usage.gpu_mem_usage == -1.0


def test_probe_without_gpu_or_proc():
    probe = SystemProbe(read=lambda path: None, run=lambda cmd: None)
    assert probe.hardware.gpu_kind == "" and probe.sample().cpu_usage == -1.0
