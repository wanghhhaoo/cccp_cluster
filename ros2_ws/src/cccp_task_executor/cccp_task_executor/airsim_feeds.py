"""On-demand AirSim cameras for tasks whose input is airsim://<vehicle>/<camera> (CONVENTIONS 4.4).

A feed fetches one camera over RPC and publishes it on this node's
input/airsim/<vehicle>/cam_<camera> topic while at least one task uses it;
it stops when the last task releases it. Feeds are also offered through the
relay, so in relay mode helpers can read the primary's compressed stream
instead of querying the simulator themselves.
"""
import threading
import time

from sensor_msgs.msg import Image

from cccp_node.core.input_source import airsim_topic
from cccp_node.ros import qos

from .airsim_common import decode_response


class _Feed:
    def __init__(self, node, client, vehicle, camera, topic, period, raw_encoding):
        self._node, self._client = node, client
        self._vehicle, self._camera = vehicle, camera
        self._period, self._raw_encoding = period, raw_encoding
        self.publisher = node.create_publisher(Image, topic, qos.CAMERA)
        self.refs = 0
        self.frames = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="airsim-%s-%s" % (vehicle, camera))
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                response = self._client.get(self._camera, self._vehicle)
                encoding, height, width, step, data = decode_response(response, self._raw_encoding)
                msg = Image(height=height, width=width, encoding=encoding, step=step, data=data)
                msg.header.stamp = self._node.get_clock().now().to_msg()
                msg.header.frame_id = "airsim/%s/%s" % (self._vehicle, self._camera)
                self.publisher.publish(msg)
                self.frames += 1
            except Exception as exc:   # RPC errors, simulator restarts, wrong names
                self._node.get_logger().warning("airsim %s/%s fetch failed: %s" % (self._vehicle, self._camera, exc),
                                                throttle_duration_sec=5.0)
                self._stop.wait(1.0)
                continue
            self._stop.wait(max(0.0, self._period - (time.monotonic() - began)))

    def stop(self):
        self._stop.set()


class AirSimFeeds:
    def __init__(self, node, client_factory, fps=10.0, raw_encoding="bgr8", relay=None):
        """client_factory() -> object with get(camera, vehicle) returning an AirSim ImageResponse."""
        self._node = node
        self._client_factory = client_factory
        self._period = 1.0 / max(float(fps), 0.1)
        self._raw_encoding = raw_encoding
        self._relay = relay
        self._feeds = {}
        self._lock = threading.Lock()

    def acquire(self, vehicle, camera):
        """Start (or share) the feed; returns its relative topic."""
        topic = airsim_topic(vehicle, camera)
        with self._lock:
            feed = self._feeds.get((vehicle, camera))
            if feed is None:
                feed = _Feed(self._node, self._client_factory(), vehicle, camera, topic, self._period,
                             self._raw_encoding)
                self._feeds[(vehicle, camera)] = feed
                if self._relay is not None:
                    self._relay.watch(topic)
            feed.refs += 1
        return topic

    def release(self, vehicle, camera):
        with self._lock:
            feed = self._feeds.get((vehicle, camera))
            if feed is None:
                return
            feed.refs -= 1
            if feed.refs <= 0:
                feed.stop()
                self._node.destroy_publisher(feed.publisher)
                del self._feeds[(vehicle, camera)]
                if self._relay is not None:
                    self._relay.unwatch(airsim_topic(vehicle, camera))

    def active(self):
        with self._lock:
            return {key: feed.frames for key, feed in self._feeds.items()}

    def shutdown(self):
        with self._lock:
            for feed in self._feeds.values():
                feed.stop()
            self._feeds.clear()
