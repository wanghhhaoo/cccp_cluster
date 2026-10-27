from pathlib import Path
from threading import RLock

import rclpy
from ament_index_python.packages import get_package_share_directory
from rclpy.node import Node

from collab_infer.pkct import (
    LEVELS,
    CollabNode,
    PkctTracker,
    decode_identity,
    encode_identity,
    load_calibration,
)
from collab_inference_interfaces.srv import (
    ElectPrimary,
    InitTracker,
    ResetTracker,
    Track,
)

from .image import ImageDecoder


class PkctServer(Node):
    def __init__(self):
        super().__init__("pkct_inference_server")
        share = Path(get_package_share_directory("collab_inference"))
        self.declare_parameter("device", "cuda:0")
        self.declare_parameter("weights", str(share / "weights" / "pkct_b0bgt.pt"))
        self.declare_parameter(
            "calibration", str(share / "weights" / "pkct_b0bgt.calib.json")
        )
        self.declare_parameter("max_trackers", 4)
        self.declare_parameter("init_service", "algorithms/pkct/init")
        self.declare_parameter("track_service", "algorithms/pkct/track")
        self.declare_parameter("elect_service", "algorithms/pkct/elect")
        self.declare_parameter("reset_service", "algorithms/pkct/reset")

        self._device = self.get_parameter("device").value
        self._weights = self.get_parameter("weights").value
        calibration_path = self.get_parameter("calibration").value
        self._calibration = load_calibration(calibration_path)
        self._max_trackers = int(self.get_parameter("max_trackers").value)
        self._images = ImageDecoder()
        self._trackers = {}
        self._elections = {}
        self._lock = RLock()

        self.create_service(
            InitTracker, self.get_parameter("init_service").value, self._init_tracker
        )
        self.create_service(
            Track, self.get_parameter("track_service").value, self._track
        )
        self.create_service(
            ElectPrimary, self.get_parameter("elect_service").value, self._elect
        )
        self.create_service(
            ResetTracker, self.get_parameter("reset_service").value, self._reset
        )
        self.get_logger().info(
            "PKCT services are ready on %s (max_trackers=%d)"
            % (self._device, self._max_trackers)
        )

    @staticmethod
    def _tracker_id(value):
        value = value.strip()
        if not value:
            raise ValueError("tracker_id must not be empty")
        return value

    def _init_tracker(self, request, response):
        try:
            tracker_id = self._tracker_id(request.tracker_id)
            with self._lock:
                if (
                    tracker_id not in self._trackers
                    and len(self._trackers) >= self._max_trackers
                ):
                    raise RuntimeError("maximum tracker count reached")
                frame = self._images.rgb8(request.first_frame)
                tracker = PkctTracker(weights=self._weights, device=self._device)
                tracker.init(
                    frame, (request.x, request.y, request.width, request.height)
                )
                self._trackers[tracker_id] = tracker
                self._elections[tracker_id] = CollabNode(
                    calibration=self._calibration
                )
            response.success = True
            response.message = "tracker initialized"
        except Exception as exc:
            response.success = False
            response.message = str(exc)
            self.get_logger().error("PKCT init failed: %s" % exc)
        return response

    def _track(self, request, response):
        try:
            tracker_id = self._tracker_id(request.tracker_id)
            level = request.level or "full"
            if level not in LEVELS:
                raise ValueError("unsupported bandwidth level: %s" % level)
            with self._lock:
                tracker = self._trackers.get(tracker_id)
                if tracker is None:
                    raise KeyError("unknown tracker_id: %s" % tracker_id)
                frame = self._images.rgb8(request.frame)
                result = tracker.track(frame)
                payload = encode_identity(
                    result.embedding,
                    result.confidence,
                    drone_id=int(request.drone_id),
                    frame_id=int(request.frame_id),
                    level=level,
                )
            response.success = True
            response.message = "ok"
            response.x = float(result.box_xywh[0])
            response.y = float(result.box_xywh[1])
            response.width = float(result.box_xywh[2])
            response.height = float(result.box_xywh[3])
            response.confidence = result.confidence
            response.latency_ms = result.latency_ms
            response.identity = list(payload)
        except Exception as exc:
            response.success = False
            response.message = str(exc)
            self.get_logger().error("PKCT track failed: %s" % exc)
        return response

    def _elect(self, request, response):
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
                        raise ValueError(
                            "identity frame_id %d does not match %d"
                            % (identity.frame_id, request.frame_id)
                        )
                    if (
                        encoded.frame_id != identity.frame_id
                        or encoded.drone_id != identity.drone_id
                    ):
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
            self.get_logger().error("PKCT election failed: %s" % exc)
        return response

    def _reset(self, request, response):
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


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = PkctServer()
        rclpy.spin(node)
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
