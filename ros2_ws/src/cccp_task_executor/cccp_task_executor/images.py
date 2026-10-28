"""sensor_msgs Image / CompressedImage conversion with numpy and PIL (no cv_bridge needed)."""
import io

import numpy as np

_CHANNELS = {"rgb8": 3, "bgr8": 3, "rgba8": 4, "bgra8": 4, "mono8": 1}


def to_rgb(image_msg):
    """sensor_msgs/Image -> HxWx3 uint8 RGB array."""
    channels = _CHANNELS.get(image_msg.encoding)
    if channels is None:
        raise ValueError("unsupported encoding %s" % image_msg.encoding)
    rows = np.frombuffer(bytes(image_msg.data), dtype=np.uint8).reshape(image_msg.height, image_msg.step)
    pixels = rows[:, :image_msg.width * channels].reshape(image_msg.height, image_msg.width, channels)
    if image_msg.encoding == "mono8":
        return np.repeat(pixels, 3, axis=2)
    if image_msg.encoding.startswith("bgr"):
        pixels = pixels[:, :, 2::-1]
    return np.ascontiguousarray(pixels[:, :, :3])


def encode_jpeg(image_msg, quality=80):
    from PIL import Image as PILImage
    out = io.BytesIO()
    PILImage.fromarray(to_rgb(image_msg)).save(out, format="JPEG", quality=quality)
    return out.getvalue()


def compress(image_msg, compressed_cls, quality=80):
    """Image -> CompressedImage (JPEG), keeping the header (stamp, frame_id)."""
    msg = compressed_cls()
    msg.header = image_msg.header
    msg.format = "jpeg"
    msg.data = encode_jpeg(image_msg, quality)
    return msg


def decompress(compressed_msg, image_cls):
    """CompressedImage -> rgb8 Image, keeping the header."""
    from PIL import Image as PILImage
    rgb = np.asarray(PILImage.open(io.BytesIO(bytes(compressed_msg.data))).convert("RGB"))
    msg = image_cls()
    msg.header = compressed_msg.header
    msg.height, msg.width = int(rgb.shape[0]), int(rgb.shape[1])
    msg.encoding = "rgb8"
    msg.step = int(rgb.shape[1] * 3)
    msg.data = rgb.tobytes()
    return msg
