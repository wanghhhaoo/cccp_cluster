"""Per-frame latency of the PKCI/PKCT engines with the shipped weights.

Usage: python3 tools/inference_bench.py ros2_ws/src/collab_inference <device> [threads] [frames]
  device: cpu or cuda:0. threads limits torch CPU threads (default: torch's choice).
Synthetic 640x480 frames: measures speed only, not accuracy. Run it on the target boards.
"""
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np

root = Path(sys.argv[1]).resolve()
device = sys.argv[2]
threads = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] != "0" else 0
frames = int(sys.argv[4]) if len(sys.argv) > 4 else 30
sys.path.insert(0, str(root))

import torch  # noqa: E402

from collab_inference_ros.engines import PkciEngine, PkctEngine  # noqa: E402

if threads:
    torch.set_num_threads(threads)
weights = root / "weights"
rng = np.random.default_rng(0)
images = [rng.integers(0, 255, (480, 640, 3), dtype=np.uint8) for _ in range(8)]


def bench(name, call):
    call(0)                                   # warm-up: model load / CUDA init
    times = []
    for i in range(1, frames + 1):
        start = time.perf_counter()
        response = call(i)
        times.append((time.perf_counter() - start) * 1000.0)
        assert response.success, response.message
    times.sort()
    print("%s %s threads=%s: P50 %.1f ms  P95 %.1f ms  (%d frames)" % (
        name, device, threads or torch.get_num_threads(), statistics.median(times),
        times[int(len(times) * 0.95) - 1], frames))


pkci = PkciEngine(str(weights / "pkci_mvpn_plong_s2.pt"), device, decode=lambda image: image)
bench("PKCI", lambda i: pkci.classify(NS(level="full", image=images[i % 8], drone_id=1, frame_id=i), NS()))

pkct = PkctEngine(str(weights / "pkct_b0bgt.pt"), device, str(weights / "pkct_b0bgt.calib.json"),
                  decode=lambda image: image)
init = pkct.init_tracker(NS(tracker_id="t", first_frame=images[0], x=300.0, y=200.0, width=80.0, height=80.0),
                         NS())
assert init.success, init.message
bench("PKCT", lambda i: pkct.track(NS(tracker_id="t", level="full", frame=images[i % 8], drone_id=1,
                                      frame_id=i), NS()))
