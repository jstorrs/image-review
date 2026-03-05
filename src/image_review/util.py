import pygame as pg
import skimage as ski


def load_surface(path: str) -> pg.Surface:
    img = ski.io.imread(path)
    return pg.surfarray.make_surface(img.transpose(1, 0, 2))
