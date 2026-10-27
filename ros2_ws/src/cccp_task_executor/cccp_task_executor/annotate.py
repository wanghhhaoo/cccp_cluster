"""Draw algorithm results onto the input frame (the composited result image)."""
import numpy as np
from PIL import Image, ImageDraw, ImageFont

_COLOR = (255, 48, 48)
_TEXT = (255, 255, 255)


def _text_size(draw, font, text):
    # Pillow 9.0 (Ubuntu 22.04 / JetPack) cannot textbbox() the default bitmap
    # font; Pillow >= 10 removed textsize(). Support both.
    try:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        return right - left, bottom - top
    except (ValueError, AttributeError):
        return draw.textsize(text, font=font)


def _label(draw, font, xy, text):
    left, top = xy
    width, height = _text_size(draw, font, text)
    draw.rectangle((left - 2, top - 2, left + width + 2, top + height + 2), fill=_COLOR)
    draw.text((left, top), text, fill=_TEXT, font=font)


def annotate(rgb, result):
    """rgb: HxWx3 uint8; result: TaskResult message. Returns a new HxWx3 uint8 array."""
    image = Image.fromarray(np.ascontiguousarray(rgb))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    if result.task_type == "PKCT":
        x0, y0 = result.x, result.y
        x1, y1 = x0 + result.width, y0 + result.height
        draw.rectangle((x0, y0, x1, y1), outline=_COLOR, width=3)
        _label(draw, font, (x0 + 2, max(0.0, y0 - 14)), "conf %.2f" % result.confidence)
    else:
        _label(draw, font, (6, 6), "%s %.2f" % (result.class_name, result.score))
    return np.asarray(image)
