"""Fetch camera frames from AirSim (Cosys-AirSim) over RPC and publish them.

The task executor subscribes to the same topic (<ns>/camera/image by default),
so the board reads simulation images directly from the AirSim RPC server with
no other ROS component in between. Frames are only requested while someone
subscribes, so an idle board puts no load on the simulator.
"""
import io
import threading
import time

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

try:
    import cosysairsim as airsim
except ImportError:  # upstream AirSim client exposes the same API
    import airsim


def decode_response(response, raw_encoding):
    """AirSim ImageResponse -> (encoding, height, width, step, bytes).

    Uncompressed Scene images arrive as raw 3-channel pixels (BGR on current
    AirSim/Cosys releases, configurable via raw_encoding); 4-channel buffers
    are treated as BGRA. Compressed responses are PNG and decoded to RGB.
    """
    width, height = int(response.width), int(response.height)
    data = bytes(response.image_data_uint8)
    if width <= 0 or height <= 0 or not data:
        raise ValueError("empty image (check vehicle_name / camera_name): %s"
                         % (getattr(response, "message", "") or "no data"))
    if response.compress:
        from PIL import Image as PILImage
        rgb = np.asarray(PILImage.open(io.BytesIO(data)).convert("RGB"))
        return "rgb8", rgb.shape[0], rgb.shape[1], rgb.shape[1] * 3, rgb.tobytes()
    channels = len(data) // (width * height)
    if channels == 3:
        return raw_encoding, height, width, width * 3, data[:width * height * 3]
    if channels == 4:
        return "bgra8", height, width, width * 4, data[:width * height * 4]
    raise ValueError("unexpected image buffer: %d bytes for %dx%d" % (len(data), width, height))


class AirSimCamera(Node):
    def __init__(self):
        super().__init__("airsim_camera")
        self.declare_parameter("airsim_host", "127.0.0.1")
        self.declare_parameter("airsim_port", 41451)
        self.declare_parameter("vehicle_name", "")
        self.declare_parameter("camera_name", "0")
        self.declare_parameter("image_type", "Scene")
        self.declare_parameter("compress", False)
        self.declare_parameter("raw_encoding", "bgr8")
        self.declare_parameter("fps", 10.0)
        self.declare_parameter("rpc_timeout", 10.0)
        self.declare_parameter("output_topic", "camera/image")
        self.declare_parameter("frame_id", "airsim_camera")
        self.declare_parameter("only_when_subscribed", True)

        get = lambda name: self.get_parameter(name).value  # noqa: E731
        self._host, self._port = get("airsim_host"), int(get("airsim_port"))
        self._vehicle, self._camera = get("vehicle_name"), str(get("camera_name"))
        self._image_type = getattr(airsim.ImageType, get("image_type"))
        self._compress = bool(get("compress"))
        self._raw_encoding = get("raw_encoding")
        self._period = 1.0 / max(float(get("fps")), 0.1)
        self._timeout = float(get("rpc_timeout"))
        self._frame_id = get("frame_id")
        self._lazy = bool(get("only_when_subscribed"))
        self._publisher = self.create_publisher(Image, get("output_topic"), qos_profile_sensor_data)
        self._client = None
        threading.Thread(target=self._loop, daemon=True).start()
        self.get_logger().info(
            "airsim camera: %s:%d vehicle=%r camera=%r -> %s @ %.1f fps"
            % (self._host, self._port, self._vehicle, self._camera,
               self._publisher.topic_name, 1.0 / self._period)
        )

    def _fetch(self):
        if self._client is None:
            self._client = airsim.VehicleClient(ip=self._host, port=self._port,
                                                timeout_value=self._timeout)
        request = airsim.ImageRequest(self._camera, self._image_type, False, self._compress)
        responses = self._client.simGetImages([request], vehicle_name=self._vehicle)
        if not responses:
            raise ValueError("simGetImages returned no image")
        return responses[0]

    def _loop(self):
        while rclpy.ok():
            began = time.monotonic()
            if self._lazy and self._publisher.get_subscription_count() == 0:
                time.sleep(0.2)
                continue
            try:
                response = self._fetch()
                encoding, height, width, step, data = decode_response(response, self._raw_encoding)
                msg = Image(height=height, width=width, encoding=encoding, step=step, data=data)
                msg.header.stamp = self.get_clock().now().to_msg()
                msg.header.frame_id = self._frame_id
                self._publisher.publish(msg)
            except Exception as exc:  # RPC errors, simulator restarts, bad names
                self._client = None
                self.get_logger().warning("airsim fetch failed: %s" % exc, throttle_duration_sec=5.0)
                time.sleep(1.0)
                continue
            time.sleep(max(0.0, self._period - (time.monotonic() - began)))


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = AirSimCamera()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
