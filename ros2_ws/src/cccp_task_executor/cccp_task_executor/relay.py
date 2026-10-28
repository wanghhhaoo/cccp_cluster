"""Compressed copies of local image topics for other nodes (CONVENTIONS 4.4).

For every watched topic T on this node, <ns>/T/compressed carries JPEG frames.
The raw topic is only subscribed (and frames only encoded) while some other
node subscribes to the compressed one, so an unused relay costs nothing.
"""
from sensor_msgs.msg import CompressedImage, Image

from cccp_node.core.input_source import COMPRESSED_SUFFIX
from cccp_node.ros import qos

from .images import compress


class ImageRelay:
    def __init__(self, node, quality=80, check_period=0.5):
        self._node = node
        self._quality = quality
        self._topics = {}     # relative topic -> {"pub", "sub", "refs"}
        node.create_timer(check_period, self._update)

    def watch(self, topic):
        entry = self._topics.get(topic)
        if entry is None:
            pub = self._node.create_publisher(CompressedImage, topic + COMPRESSED_SUFFIX, qos.CAMERA)
            entry = self._topics[topic] = {"pub": pub, "sub": None, "refs": 0}
        entry["refs"] += 1

    def unwatch(self, topic):
        entry = self._topics.get(topic)
        if entry is None:
            return
        entry["refs"] -= 1
        if entry["refs"] <= 0:
            if entry["sub"] is not None:
                self._node.destroy_subscription(entry["sub"])
            self._node.destroy_publisher(entry["pub"])
            del self._topics[topic]

    def _update(self):
        for topic, entry in list(self._topics.items()):
            wanted = entry["pub"].get_subscription_count() > 0
            if wanted and entry["sub"] is None:
                entry["sub"] = self._node.create_subscription(
                    Image, topic, lambda msg, pub=entry["pub"]: self._forward(pub, msg), qos.CAMERA)
            elif not wanted and entry["sub"] is not None:
                self._node.destroy_subscription(entry["sub"])
                entry["sub"] = None

    def _forward(self, pub, msg):
        try:
            pub.publish(compress(msg, CompressedImage, self._quality))
        except Exception as exc:
            self._node.get_logger().warning("relay compression failed: %s" % exc, throttle_duration_sec=5.0)
