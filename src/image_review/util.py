import io

import pygame as pg
from PIL import Image


def load_surface(buf: bytes) -> pg.Surface:
    """Decode JPG bytes to a 24-bit RGB surface.

    Raises on undecodable or truncated input: Pillow is strict, whereas SDL_image
    would fill missing rows with grey and let a partial image be marked CLEAN.
    """
    with Image.open(io.BytesIO(buf)) as im:
        im.load()
        rgb = im.convert("RGB")
    return pg.image.frombytes(rgb.tobytes(), rgb.size, "RGB")
