import io
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

import pygame as pg
from PIL import Image
from rectpack import newPacker

from .status import Key, Rotation
from .store import ManifestRow, ReviewStore
from .util import load_surface

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GridSpec:
    surface: pg.Surface
    keys: tuple[Key, ...]
    min_scale: float  # the smallest fit_size / source size ratio among the images drawn


class PlacedRect(NamedTuple):
    rect_id: int
    x: int
    y: int
    w: int
    h: int


def fit_size(w: int, h: int, grid_w: int, grid_h: int, allow_rotation: bool) -> tuple[int, int]:
    """The size a w x h image is packed at: unchanged if it fits the bin upright, or rotated when
    rotation is allowed; otherwise shrunk, keeping its aspect ratio, by the larger of the two
    orientations' scales."""
    bounds = [(grid_w, grid_h), (grid_h, grid_w)] if allow_rotation else [(grid_w, grid_h)]
    bound_w, bound_h = max(bounds, key=lambda b: min(b[0] / w, b[1] / h))  # orientation needing the least shrink
    scale = min(bound_w / w, bound_h / h)
    if scale >= 1:
        return w, h
    return max(1, min(bound_w, int(w * scale))), max(1, min(bound_h, int(h * scale)))


def _header_size(buf: bytes) -> tuple[int, int]:
    """Image dimensions from the header alone; no pixels are decoded. Raises on an unreadable header."""
    with Image.open(io.BytesIO(buf)) as im:
        return im.size


def _pack(sizes: dict[int, tuple[int, int]], grid_w: int, grid_h: int, rotate: bool) -> dict[int, list[PlacedRect]]:
    """Bin-pack the images at their fit sizes, by bin index; rectpack rotates a rect only if `rotate`."""
    packer = newPacker(rotation=rotate)
    packer.add_bin(grid_w, grid_h, float("inf"))
    for idx, (w, h) in sizes.items():
        packer.add_rect(*fit_size(w, h, grid_w, grid_h, rotate), idx)
    packer.pack()
    bins: dict[int, list[PlacedRect]] = {}
    for bin_idx, x, y, w, h, rect_id in packer.rect_list():
        bins.setdefault(bin_idx, []).append(PlacedRect(rect_id, x, y, w, h))
    return bins


def _composite_bin(
    placed: list[PlacedRect],
    items: list[ManifestRow],
    blobs: dict[Key, bytes],
    sizes: dict[int, tuple[int, int]],
    grid_w: int,
    grid_h: int,
    rotated: bool,
    on_done: Callable[[], None],
) -> tuple[GridSpec, list[int]]:
    """Decode and blit one bin's images; their surfaces are released when this returns.

    Returns the grid, holding only the keys whose pixels were drawn, and the rect ids that
    failed to decode (their rectangles stay black)."""
    canvas = pg.Surface((grid_w, grid_h))
    canvas.fill((0, 0, 0))
    keys: list[Key] = []
    min_scale = 1.0
    failed: list[int] = []
    for rect_id, x, y, w, h in placed:
        key = items[rect_id].key
        target = fit_size(*sizes[rect_id], grid_w, grid_h, rotated)
        try:
            surface = load_surface(blobs[key])
            if surface.get_size() != sizes[rect_id]:
                raise ValueError(f"decoded size {surface.get_size()} differs from header size {sizes[rect_id]}")
            if surface.get_size() != target:
                surface = pg.transform.smoothscale(surface, target)
            if (w, h) != target:  # rectpack rotated it
                surface = pg.transform.rotate(surface, -90)
            if surface.get_size() != (w, h):
                raise ValueError(f"surface size {surface.get_size()} differs from its packed rectangle {(w, h)}")
        except Exception as exc:  # noqa: BLE001 - any decode failure makes the image unloadable, not fatal
            log.warning("cannot load %s: %s", key, exc)
            failed.append(rect_id)
        else:
            canvas.blit(surface, (x, y))
            keys.append(key)
            min_scale = min(min_scale, target[0] / sizes[rect_id][0], target[1] / sizes[rect_id][1])
        on_done()
    return GridSpec(surface=canvas, keys=tuple(keys), min_scale=min_scale), failed


def pack_into_grids(
    items: list[ManifestRow],
    store: ReviewStore,
    grid_w: int,
    grid_h: int,
    *,
    rotation: Rotation = "auto",
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[GridSpec], list[Key]]:
    """Pack review items into grid canvases sized for the current screen.

    Each item is a ManifestRow; image bytes are fetched via the store. Images are
    packed at their fit_size, read from the image header, and decoded one bin at a
    time while that bin is composited. `rotation` says whether images may be rotated 90 degrees:
    always, never, or (auto) only if that needs fewer grids. Returns the GridSpecs, each holding a
    composited pygame surface, and the keys left out of every grid (missing,
    unreadable header, failed decode, or left unpacked), in input order. A grid holds only keys
    whose pixels it shows. on_progress(i, n) is called as each of the n images is
    handled, ending with (n, n).
    """
    n = len(items)
    done = 0

    def advance() -> None:
        nonlocal done
        done += 1
        if on_progress is not None:
            on_progress(done, n)

    blobs = store.image_bytes_many([item.key for item in items])

    # Sizes from the headers only: no image is decoded yet
    sizes: dict[int, tuple[int, int]] = {}
    left_out: set[int] = set()
    for idx, item in enumerate(items):
        if item.key not in blobs:  # absent: the store already warned
            left_out.add(idx)
            advance()
            continue
        try:
            w, h = _header_size(blobs[item.key])
        except Exception as exc:  # noqa: BLE001 - an unreadable header makes the image unloadable, not fatal
            log.warning("cannot load %s: %s", item.key, exc)
            left_out.add(idx)
            advance()
            continue
        sizes[idx] = (w, h)

    # Bin-pack at the fit sizes. Packing reads no pixels, so "auto" packs both ways and keeps the
    # rotated packing only if it needs strictly fewer grids
    rotated = rotation == "always"
    bins = _pack(sizes, grid_w, grid_h, rotated)
    if rotation == "auto":
        with_rotation = _pack(sizes, grid_w, grid_h, True)
        if len(with_rotation) < len(bins):
            bins, rotated = with_rotation, True

    # Composite one bin at a time, so at most one bin's decoded images are alive
    grids = []
    for bin_idx in sorted(bins):
        grid, failed = _composite_bin(bins[bin_idx], items, blobs, sizes, grid_w, grid_h, rotated, advance)
        left_out.update(failed)
        if grid.keys:
            grids.append(grid)

    # fit_size makes every image fit a bin, so the packer should leave none out; any it does is
    # left to the caller (shown as a single image, loaded on display) rather than dropped
    placed = {rect.rect_id for rects in bins.values() for rect in rects}
    for idx in sizes.keys() - placed:
        log.warning("%s was not packed into a grid", items[idx].key)
        left_out.add(idx)
        advance()

    log.debug(
        "packed %d images into %d grids of %dx%d (rotation %s, rotated: %s); %d left out",
        n,
        len(grids),
        grid_w,
        grid_h,
        rotation,
        rotated,
        len(left_out),
    )
    return grids, [items[idx].key for idx in sorted(left_out)]
