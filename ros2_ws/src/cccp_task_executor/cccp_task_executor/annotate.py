"""Drawing helpers for the composited result image (StreamAlgorithm.annotate)."""
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


def _canvas(rgb):
    image = Image.fromarray(np.ascontiguousarray(rgb))
    return image, ImageDraw.Draw(image), ImageFont.load_default()


def label(rgb, text):
    """A label in the top left corner. rgb: HxWx3 uint8; returns a new array."""
    image, draw, font = _canvas(rgb)
    _label(draw, font, (6, 6), text)
    return np.asarray(image)


def box(rgb, x, y, width, height, text):
    """A box with a label above it. rgb: HxWx3 uint8; returns a new array."""
    image, draw, font = _canvas(rgb)
    draw.rectangle((x, y, x + width, y + height), outline=_COLOR, width=3)
    _label(draw, font, (x + 2, max(0.0, y - 14)), text)
    return np.asarray(image)
