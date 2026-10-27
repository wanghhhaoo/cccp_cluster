from pathlib import Path

import numpy as np
import rclpy
from ament_index_python.packages import get_package_share_directory
from rclpy.node import Node

from collab_infer.pkci import (
    NUM_CLASSES,
    LEVELS,
    PkciClassifier,
    class_names,
    decode_increment,
    encode_increment,
    fuse,
)
from collab_inference_interfaces.srv import Classify, FuseClassification

from .image import ImageDecoder


class PkciServer(Node):
    def __init__(self):
        super().__init__("pkci_inference_server")
        share = Path(get_package_share_directory("collab_inference"))
        self.declare_parameter("device", "cuda:0")
        self.declare_parameter(
            "weights", str(share / "weights" / "pkci_mvpn_plong_s2.pt")
        )
        self.declare_parameter("classify_service", "algorithms/pkci/classify")
        self.declare_parameter("fuse_service", "algorithms/pkci/fuse")

        device = self.get_parameter("device").value
        weights = self.get_parameter("weights").value
        self.get_logger().info(
            "Loading PKCI classifier on %s from %s" % (device, weights)
        )
        self._classifier = PkciClassifier(weights=weights, device=device)
        self._class_names = class_names()
        self._images = ImageDecoder()

        self.create_service(
            Classify,
            self.get_parameter("classify_service").value,
            self._classify,
        )
        self.create_service(
            FuseClassification,
            self.get_parameter("fuse_service").value,
            self._fuse,
        )
        self.get_logger().info("PKCI services are ready")

    def _classify(self, request, response):
        try:
            level = request.level or "full"
            if level not in LEVELS:
                raise ValueError("unsupported bandwidth level: %s" % level)
            image = self._images.rgb8(request.image)
            result = self._classifier.infer(image)
            payload = encode_increment(
                result,
                drone_id=int(request.drone_id),
                frame_id=int(request.frame_id),
                level=level,
            )
            response.success = True
            response.message = "ok"
            response.label = result.label
            response.class_name = self._class_names[result.label]
            response.score = result.score
            response.confidence = result.confidence
            response.latency_ms = result.latency_ms
            response.log_probs = [float(value) for value in result.log_probs]
            response.increment = list(payload)
        except Exception as exc:  # keep service alive after bad frames/model errors
            response.success = False
            response.message = str(exc)
            self.get_logger().error("PKCI classify failed: %s" % exc)
        return response

    def _fuse(self, request, response):
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
                    raise ValueError(
                        "peer frame_id %d does not match %d"
                        % (decoded.frame_id, request.frame_id)
                    )
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
            response.score = result.score
            response.views = result.views
            response.used_drone_ids = used
            response.log_probs = [float(value) for value in result.log_probs]
        except Exception as exc:
            response.success = False
            response.message = str(exc)
            self.get_logger().error("PKCI fusion failed: %s" % exc)
        return response


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = PkciServer()
        rclpy.spin(node)
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
