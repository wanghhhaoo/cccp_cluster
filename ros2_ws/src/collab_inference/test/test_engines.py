"""PkciEngine / PkctEngine request handling with stand-in models (no torch, no ROS)."""
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np

from collab_infer.pkci import LocalResult, encode_increment
from collab_infer.pkct import encode_identity
from collab_inference_ros.engines import PkciEngine, PkctEngine

CALIBRATION = Path(__file__).resolve().parents[1] / "weights" / "pkct_b0bgt.calib.json"
TrackResult = namedtuple("TrackResult", "box_xywh confidence latency_ms embedding")


def passthrough(image):
    return image


class FakeClassifier:
    def infer(self, image):
        logits = np.linspace(-2.0, 2.0, 44, dtype=np.float32)
        return LocalResult(logits, logits - 3.0, 43, 0.9, 0.8, 1.5)


class FakeTracker:
    def init(self, frame, box):
        self.box = box

    def track(self, frame):
        embedding = np.ones(128, dtype=np.float32) / np.sqrt(128)
        return TrackResult(self.box, 0.7, 2.0, embedding)


def pkci():
    return PkciEngine(None, "cpu", decode=passthrough, classifier=FakeClassifier())


def pkct(max_trackers=2):
    return PkctEngine(None, "cpu", str(CALIBRATION), max_trackers, decode=passthrough,
                      tracker_factory=FakeTracker)


def test_classify_fills_response_and_increment():
    response = pkci().classify(NS(level="", image="img", drone_id=2, frame_id=5), NS())
    assert response.success and response.label == 43 and isinstance(response.score, float)
    assert len(response.log_probs) == 44 and len(response.increment) > 0


def test_classify_reports_bad_level_without_raising():
    response = pkci().classify(NS(level="ultra", image="img", drone_id=0, frame_id=0), NS())
    assert not response.success and "ultra" in response.message


def test_fuse_combines_own_and_peer_views():
    engine = pkci()
    result = FakeClassifier().infer(None)
    peer = encode_increment(result, drone_id=3, frame_id=9, level="full")
    request = NS(frame_id=9, own_log_probs=list(result.log_probs),
                 peers=[NS(data=peer, drone_id=3, frame_id=9), NS(data=peer, drone_id=3, frame_id=9)])
    response = engine.fuse(request, NS())
    assert response.success and response.views == 2 and response.used_drone_ids == [3]
    mismatched = NS(frame_id=10, own_log_probs=list(result.log_probs), peers=[NS(data=peer, drone_id=3, frame_id=9)])
    assert not engine.fuse(mismatched, NS()).success


def test_tracker_lifecycle_and_capacity():
    engine = pkct(max_trackers=1)
    init = engine.init_tracker(NS(tracker_id="task-1-1", first_frame="f", x=1.0, y=2.0, width=3.0, height=4.0), NS())
    assert init.success
    full = engine.init_tracker(NS(tracker_id="task-2-1", first_frame="f", x=0.0, y=0.0, width=1.0, height=1.0), NS())
    assert not full.success and "maximum" in full.message
    track = engine.track(NS(tracker_id="task-1-1", level="", frame="f", drone_id=1, frame_id=1), NS())
    assert track.success and (track.x, track.width) == (1.0, 3.0) and len(track.identity) > 0
    unknown = engine.track(NS(tracker_id="nope", level="", frame="f", drone_id=1, frame_id=1), NS())
    assert not unknown.success
    assert engine.reset(NS(tracker_id="task-1-1"), NS()).success
    assert not engine.reset(NS(tracker_id="task-1-1"), NS()).success


def test_election_uses_identities_of_one_frame():
    engine = pkct()
    engine.init_tracker(NS(tracker_id="t", first_frame="f", x=0.0, y=0.0, width=1.0, height=1.0), NS())
    embedding = np.ones(128, dtype=np.float32) / np.sqrt(128)
    identities = [NS(data=encode_identity(embedding, c, drone_id=d, frame_id=4), drone_id=d, frame_id=4)
                  for d, c in ((1, 0.9), (2, 0.4))]
    response = engine.elect(NS(tracker_id="t", frame_id=4, identities=identities), NS())
    assert response.success and sorted(response.used_drone_ids) == [1, 2]
    assert response.primary_drone_id in (-1, 1, 2)
