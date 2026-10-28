"""Real PKCI/PKCT weights through the engines on CPU (skipped without torch).

Not an accuracy test: it checks that the shipped weights load and that the
engines produce well-formed responses and payloads on a camera-sized frame.
"""
import statistics
import time
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("torchvision")

from collab_inference_ros.engines import PkciEngine, PkctEngine  # noqa: E402

WEIGHTS = Path(__file__).resolve().parents[1] / "weights"
FRAME_SHAPE = (480, 640, 3)


def frame(seed):
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 255, FRAME_SHAPE, dtype=np.uint8)
    image[200:280, 300:380] = 255                       # a bright square to track
    return image


def timed(fn, *args):
    start = time.perf_counter()
    result = fn(*args)
    return result, (time.perf_counter() - start) * 1000.0


def test_pkci_real_weights():
    engine = PkciEngine(str(WEIGHTS / "pkci_mvpn_plong_s2.pt"), "cpu", decode=lambda image: image)
    times = []
    for i in range(6):
        response, ms = timed(engine.classify, NS(level="full", image=frame(i), drone_id=1, frame_id=i), NS())
        assert response.success, response.message
        times.append(ms)
    assert 0 <= response.label < 44 and len(response.log_probs) == 44 and 0 < len(response.increment) < 1024
    print("PKCI cpu 640x480: median %.1f ms (first call %.1f ms)" % (statistics.median(times[1:]), times[0]))


def test_pkct_real_weights():
    engine = PkctEngine(str(WEIGHTS / "pkct_b0bgt.pt"), "cpu", str(WEIGHTS / "pkct_b0bgt.calib.json"),
                        decode=lambda image: image)
    init = engine.init_tracker(NS(tracker_id="t", first_frame=frame(0), x=300.0, y=200.0, width=80.0,
                                  height=80.0), NS())
    assert init.success, init.message
    times = []
    for i in range(1, 7):
        response, ms = timed(engine.track, NS(tracker_id="t", level="full", frame=frame(i), drone_id=1,
                                              frame_id=i), NS())
        assert response.success, response.message
        times.append(ms)
    assert response.width > 0 and response.height > 0 and 0 < len(response.identity) < 1024
    elect = engine.elect(NS(tracker_id="t", frame_id=6,
                            identities=[NS(data=bytes(response.identity), drone_id=1, frame_id=6)]), NS())
    assert elect.success, elect.message
    print("PKCT cpu 640x480: median %.1f ms (first call %.1f ms)" % (statistics.median(times[1:]), times[0]))
