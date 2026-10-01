import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple

import pygame as pg
from rectpack import newPacker

from .store import ManifestRow, ReviewStore
from .util import load_surface


@dataclass
class GridSpec:
    surface: pg.Surface
    keys: list[str] = field(default_factory=list)


class PlacedRect(NamedTuple):
    rect_id: int
    x: int
    y: int
    w: int
    h: int


def _fit_to_bin(surface: pg.Surface, grid_w: int, grid_h: int, allow_rotation: bool) -> pg.Surface:
    """Smoothscale an image that cannot fit the bin, keeping its aspect ratio.

    An image that fits upright, or rotated when rotation is allowed, is returned
    unchanged. Otherwise it is shrunk by the larger of the two orientations' scales.
    """
    w, h = surface.get_size()
    bounds = [(grid_w, grid_h), (grid_h, grid_w)] if allow_rotation else [(grid_w, grid_h)]
    bound_w, bound_h = max(bounds, key=lambda b: min(b[0] / w, b[1] / h))  # orientation needing the least shrink
    scale = min(bound_w / w, bound_h / h)
    if scale >= 1:
        return surface
    new_size = (max(1, min(bound_w, int(w * scale))), max(1, min(bound_h, int(h * scale))))
    return pg.transform.smoothscale(surface, new_size)


def pack_into_grids(
    items: list[ManifestRow],
    store: ReviewStore,
    grid_w: int,
    grid_h: int,
    *,
    allow_rotation: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[GridSpec], list[str]]:
    """Pack review items into grid canvases sized for the current screen.

    Each item is a ManifestRow; image bytes are fetched via the store.
    Returns the GridSpecs, each holding a composited pygame surface, and the
    unloadable keys (missing or undecodable), in input order. No grid holds an
    unloadable key. Images larger than the bin are shrunk to fit before packing.
    on_progress(i, n) is called after each of the n images is handled.
    """
    # Load all surfaces upfront — avoids fetching each image twice
    blobs = store.image_bytes_many([item.key for item in items])
    loaded: dict[int, pg.Surface] = {}
    unloadable: list[str] = []
    for idx, item in enumerate(items):
        if item.key not in blobs:  # absent: the store already warned
            unloadable.append(item.key)
        else:
            try:
                loaded[idx] = _fit_to_bin(load_surface(blobs[item.key]), grid_w, grid_h, allow_rotation)
            except Exception as exc:  # noqa: BLE001 - any decode failure makes the image unloadable, not fatal
                print(f"WARNING: cannot load {item.key}: {exc}", file=sys.stderr)
                unloadable.append(item.key)
        if on_progress is not None:
            on_progress(idx + 1, len(items))

    # Bin-pack
    packer = newPacker(rotation=allow_rotation)
    packer.add_bin(grid_w, grid_h, float("inf"))
    for idx, surface in loaded.items():
        packer.add_rect(*surface.get_size(), idx)
    packer.pack()

    # Identify which items were packed into which bins
    packed = set()
    bins: dict[int, list[PlacedRect]] = {}
    for bin_idx, x, y, w, h, rect_id in packer.rect_list():
        bins.setdefault(bin_idx, []).append(PlacedRect(rect_id, x, y, w, h))
        packed.add(rect_id)

    grids = []

    # Composite each bin into a surface
    for bin_idx in sorted(bins):
        canvas = pg.Surface((grid_w, grid_h))
        canvas.fill((0, 0, 0))
        keys = []
        for rect_id, x, y, w, h in bins[bin_idx]:
            keys.append(items[rect_id].key)
            img_surface = loaded[rect_id]
            orig_w, orig_h = img_surface.get_size()
            if (w, h) == (orig_w, orig_h):
                canvas.blit(img_surface, (x, y))
            else:
                rotated = pg.transform.rotate(img_surface, -90)
                canvas.blit(rotated, (x, y))
        grids.append(GridSpec(surface=canvas, keys=keys))

    # Overflow: images the packer left out (none are larger than the bin) become single-image grids
    for idx, surface in loaded.items():
        if idx not in packed:
            grids.append(GridSpec(surface=surface, keys=[items[idx].key]))

    return grids, unloadable
