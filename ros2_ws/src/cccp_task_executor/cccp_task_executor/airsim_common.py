"""AirSim RPC helpers that do not need the airsim package at import time."""
import io

import numpy as np


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


class AirSimClient:
    """One RPC connection to the AirSim server; reconnects lazily after errors."""

    def __init__(self, host, port, timeout=10.0, image_type="Scene", compress=False):
        self._host, self._port, self._timeout = host, int(port), timeout
        self._image_type, self._compress = image_type, compress
        self._client = None

    def get(self, camera, vehicle):
        try:
            import cosysairsim as airsim
        except ImportError:  # upstream AirSim client exposes the same API
            import airsim
        try:
            if self._client is None:
                self._client = airsim.VehicleClient(ip=self._host, port=self._port, timeout_value=self._timeout)
            request = airsim.ImageRequest(camera, getattr(airsim.ImageType, self._image_type), False, self._compress)
            responses = self._client.simGetImages([request], vehicle_name=vehicle)
        except Exception:
            self._client = None
            raise
        if not responses:
            raise ValueError("simGetImages returned no image")
        return responses[0]
