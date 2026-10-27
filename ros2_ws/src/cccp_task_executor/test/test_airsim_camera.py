import io
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

airsim_camera = pytest.importorskip("cccp_task_executor.airsim_camera")


def response(data, width, height, compress=False):
    return SimpleNamespace(image_data_uint8=data, width=width, height=height,
                           compress=compress, message="")


def test_raw_three_channel_keeps_configured_encoding():
    enc, h, w, step, data = airsim_camera.decode_response(response(bytes(range(24)), 4, 2), "bgr8")
    assert (enc, h, w, step, len(data)) == ("bgr8", 2, 4, 12, 24)


def test_raw_four_channel_is_bgra():
    enc, _, _, step, _ = airsim_camera.decode_response(response(bytes(32), 4, 2), "bgr8")
    assert enc == "bgra8" and step == 16


def test_compressed_png_decodes_to_rgb():
    buf = io.BytesIO()
    Image.fromarray(np.full((3, 5, 3), 200, dtype=np.uint8)).save(buf, format="PNG")
    enc, h, w, step, data = airsim_camera.decode_response(response(buf.getvalue(), 5, 3, True), "bgr8")
    assert (enc, h, w, step, len(data)) == ("rgb8", 3, 5, 15, 45)


def test_empty_image_is_an_error():
    with pytest.raises(ValueError, match="vehicle_name"):
        airsim_camera.decode_response(response(b"", 0, 0), "bgr8")
