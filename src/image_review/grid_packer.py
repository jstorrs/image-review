import sys
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
    batch: str = ""


class PlacedRect(NamedTuple):
    rect_id: int
    x: int
    y: int
    w: int
    h: int


def pack_into_grids(
    items: list[ManifestRow],
    store: ReviewStore,
    grid_w: int,
    grid_h: int,
    *,
    allow_rotation: bool = True,
) -> list[GridSpec]:
    """Pack review items into grid canvases sized for the current screen.

    Each item is a ManifestRow; image bytes are fetched via the store.
    Returns a list of GridSpec, each holding a composited pygame surface.
    """
    # Load all surfaces upfront — avoids fetching each image twice
    blobs = store.image_bytes_many([item.key for item in items])
    surfaces = []
    skipped: set[int] = set()
    for idx, item in enumerate(items):
        surface = None
        if item.key in blobs:  # absent: the store already warned
            try:
                surface = load_surface(blobs[item.key])
            except Exception as exc:
                print(f"WARNING: cannot load {item.key}: {exc}", file=sys.stderr)
        surfaces.append(surface)
        if surface is None:
            skipped.add(idx)

    sizes = [s.get_size() if s is not None else (0, 0) for s in surfaces]

    # Bin-pack
    packer = newPacker(rotation=allow_rotation)
    packer.add_bin(grid_w, grid_h, float("inf"))
    for idx, (w, h) in enumerate(sizes):
        if idx not in skipped:
            packer.add_rect(w, h, idx)
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
        batch = ""
        for rect_id, x, y, w, h in bins[bin_idx]:
            item = items[rect_id]
            keys.append(item.key)
            if not batch:
                batch = item.batch
            img_surface = surfaces[rect_id]
            orig_w, orig_h = img_surface.get_size()
            if (w, h) == (orig_w, orig_h):
                canvas.blit(img_surface, (x, y))
            else:
                rotated = pg.transform.rotate(img_surface, -90)
                canvas.blit(rotated, (x, y))
        grids.append(GridSpec(surface=canvas, keys=keys, batch=batch))

    # Overflow: images too large to fit any bin become single-image grids
    for idx in range(len(items)):
        if idx in skipped:
            continue
        if idx not in packed:
            item = items[idx]
            grids.append(GridSpec(
                surface=surfaces[idx],
                keys=[item.key],
                batch=item.batch,
            ))

    return grids
