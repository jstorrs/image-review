import io

import numpy as np
import pygame as pg
import skimage as ski


def load_surface(buf: bytes) -> pg.Surface:
    img = ski.io.imread(io.BytesIO(buf))
    if img.ndim == 2:
        img = np.stack([img, img, img], axis=-1)
    elif img.ndim == 3 and img.shape[2] == 1:
        img = np.squeeze(img, axis=2)
        img = np.stack([img, img, img], axis=-1)
    elif img.ndim == 3 and img.shape[2] == 4:
        img = img[:, :, :3]
    return pg.surfarray.make_surface(img.transpose(1, 0, 2))
