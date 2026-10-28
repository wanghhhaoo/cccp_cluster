"""PKCI/PKCT request handling without ROS, shared by the ROS services and in-process callers.

Each handler takes a service Request and fills the matching Response, so the
ROS servers bind them directly as service callbacks and the task executor in
the same process calls them as plain functions (no image serialization).
Handlers never raise: failures are reported through success/message.
"""
import logging
from threading import RLock

import numpy as np

from collab_infer.pkci import NUM_CLASSES, LEVELS as PKCI_LEVELS, PkciClassifier, class_names, decode_increment, \
    encode_increment, fuse
from collab_infer.pkct import LEVELS as PKCT_LEVELS, CollabNode, PkctTracker, decode_identity, encode_identity, \
    load_calibration


def _default_decoder():
    from .image import ImageDecoder
    return ImageDecoder().rgb8


class PkciEngine:
    def __init__(self, weights, device, decode=None, logger=None, classifier=None):
        self._classifier = classifier or PkciClassifier(weights=weights, device=device)
        self._class_names = class_names()
        self._decode = decode or _default_decoder()
        self._log = logger or logging.getLogger("pkci")

    def classify(self, request, response):
        try:
            level = request.level or "full"
            if level not in PKCI_LEVELS:
                raise ValueError("unsupported bandwidth level: %s" % level)
            image = self._decode(request.image)
            result = self._classifier.infer(image)
            payload = encode_increment(result, drone_id=int(request.drone_id), frame_id=int(request.frame_id),
                                       level=level)
            response.success = True
            response.message = "ok"
            response.label = result.label
            response.class_name = self._class_names[result.label]
            response.score = float(result.score)
            response.confidence = float(result.confidence)
            response.latency_ms = float(result.latency_ms)
            response.log_probs = [float(value) for value in result.log_probs]
            response.increment = list(payload)
        except Exception as exc:  # keep serving after bad frames/model errors
            response.success = False
            response.message = str(exc)
            self._log.error("PKCI classify failed: %s" % exc)
        return response

    def fuse(self, request, response):
        try:
            own = np.asarray(request.own_log_probs, dtype=np.float32)
            if own.shape != (NUM_CLASSES,) or not np.isfinite(own).all():
                raise ValueError("own_log_probs must contain 44 finite values")
            posteriors = [own]
            used = []
            seen = set()
            for peer in request.peers:
                decoded = decode_increment(bytes(peer.data))
                if decoded.frame_id != request.frame_id:
                    raise ValueError("peer frame_id %d does not match %d" % (decoded.frame_id, request.frame_id))
                if peer.frame_id != decoded.frame_id or peer.drone_id != decoded.drone_id:
                    raise ValueError("peer metadata does not match encoded payload")
                if decoded.drone_id in seen:
                    continue
                seen.add(decoded.drone_id)
                used.append(decoded.drone_id)
                posteriors.append(decoded.log_probs)
            result = fuse(posteriors)
            response.success = True
            response.message = "ok"
            response.label = result.label
            response.class_name = self._class_names[result.label]
            response.score = float(result.score)
            response.views = result.views
            response.used_drone_ids = used
            response.log_probs = [float(value) for value in result.log_probs]
        except Exception as exc:
            response.success = False
            response.message = str(exc)
            self._log.error("PKCI fusion failed: %s" % exc)
        return response


class PkctEngine:
    """Stateful trackers keyed by tracker_id; every model call is serialized by one lock, as before."""

    def __init__(self, weights, device, calibration_path, max_trackers=4, decode=None, logger=None,
                 tracker_factory=None, calibration=None):
        self._weights = weights
        self._device = device
        self._calibration = calibration if calibration is not None else load_calibration(calibration_path)
        self.max_trackers = int(max_trackers)
        self._decode = decode or _default_decoder()
        self._log = logger or logging.getLogger("pkct")
        self._new_tracker = tracker_factory or (lambda: PkctTracker(weights=self._weights, device=self._device))
        self._trackers = {}
        self._elections = {}
        self._lock = RLock()

    @staticmethod
    def _tracker_id(value):
        value = value.strip()
        if not value:
            raise ValueError("tracker_id must not be empty")
        return value

    def init_tracker(self, request, response):
        try:
            tracker_id = self._tracker_id(request.tracker_id)
            with self._lock:
                if tracker_id not in self._trackers and len(self._trackers) >= self.max_trackers:
                    raise RuntimeError("maximum tracker count reached")
                frame = self._decode(request.first_frame)
                tracker = self._new_tracker()
                tracker.init(frame, (request.x, request.y, request.width, request.height))
                self._trackers[tracker_id] = tracker
                self._elections[tracker_id] = CollabNode(calibration=self._calibration)
            response.success = True
            response.message = "tracker initialized"
        except Exception as exc:
            response.success = False
            response.message = str(exc)
            self._log.error("PKCT init failed: %s" % exc)
        return response

    def track(self, request, response):
        try:
            tracker_id = self._tracker_id(request.tracker_id)
            level = request.level or "full"
            if level not in PKCT_LEVELS:
                raise ValueError("unsupported bandwidth level: %s" % level)
            with self._lock:
                tracker = self._trackers.get(tracker_id)
                if tracker is None:
                    raise KeyError("unknown tracker_id: %s" % tracker_id)
                frame = self._decode(request.frame)
                result = tracker.track(frame)
                payload = encode_identity(result.embedding, result.confidence, drone_id=int(request.drone_id),
                                          frame_id=int(request.frame_id), level=level)
            response.success = True
            response.message = "ok"
            response.x = float(result.box_xywh[0])
            response.y = float(result.box_xywh[1])
            response.width = float(result.box_xywh[2])
            response.height = float(result.box_xywh[3])
            response.confidence = float(result.confidence)
            response.latency_ms = float(result.latency_ms)
            response.identity = list(payload)
        except Exception as exc:
            response.success = False
            response.message = str(exc)
            self._log.error("PKCT track failed: %s" % exc)
        return response

    def elect(self, request, response):
        try:
            tracker_id = self._tracker_id(request.tracker_id)
            with self._lock:
                election = self._elections.get(tracker_id)
                if election is None:
                    raise KeyError("unknown tracker_id: %s" % tracker_id)
                identities = []
                used = []
                seen = set()
                for encoded in request.identities:
                    identity = decode_identity(bytes(encoded.data))
                    if identity.frame_id != request.frame_id:
                        raise ValueError("identity frame_id %d does not match %d"
                                         % (identity.frame_id, request.frame_id))
                    if encoded.frame_id != identity.frame_id or encoded.drone_id != identity.drone_id:
                        raise ValueError("identity metadata does not match payload")
                    if identity.drone_id in seen:
                        continue
                    seen.add(identity.drone_id)
                    used.append(identity.drone_id)
                    identities.append(identity)
                primary = election.step(identities)
            response.success = True
            response.message = "ok"
            response.primary_drone_id = -1 if primary is None else int(primary)
            response.used_drone_ids = used
        except Exception as exc:
            response.success = False
            response.message = str(exc)
            response.primary_drone_id = -1
            self._log.error("PKCT election failed: %s" % exc)
        return response

    def reset(self, request, response):
        try:
            tracker_id = self._tracker_id(request.tracker_id)
            with self._lock:
                existed = self._trackers.pop(tracker_id, None) is not None
                self._elections.pop(tracker_id, None)
            response.success = existed
            response.message = "tracker released" if existed else "tracker not found"
        except Exception as exc:
            response.success = False
            response.message = str(exc)
        return response
