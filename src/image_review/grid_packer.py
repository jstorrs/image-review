import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pygame as pg

from .layout import PlacedRect, fit_size, is_rotated, jpeg_size, plan_grids
from .status import Key, Rotation
from .store import ReviewStore
from .util import load_surface

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GridSpec:
    surface: pg.Surface
    keys: tuple[Key, ...]
    min_scale: float  # the smallest fit_size / source size ratio among the images drawn


def _composite_bin(
    placed: Sequence[PlacedRect],
    keys: Sequence[Key],
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
    drawn: list[Key] = []
    min_scale = 1.0
    failed: list[int] = []
    for rect in placed:
        rect_id, x, y, w, h = rect
        key = keys[rect_id]
        target = fit_size(*sizes[rect_id], grid_w, grid_h, rotated)
        try:
            surface = load_surface(blobs[key])
            if surface.get_size() != sizes[rect_id]:
                raise ValueError(f"decoded size {surface.get_size()} differs from header size {sizes[rect_id]}")
            if surface.get_size() != target:
                surface = pg.transform.smoothscale(surface, target)
            if is_rotated(rect, sizes[rect_id], grid_w, grid_h, rotated):
                surface = pg.transform.rotate(surface, -90)
            if surface.get_size() != (w, h):
                raise ValueError(f"surface size {surface.get_size()} differs from its packed rectangle {(w, h)}")
        except Exception as exc:  # noqa: BLE001 - any decode failure makes the image unloadable, not fatal
            log.warning("cannot load %s: %s", key, exc)
            failed.append(rect_id)
        else:
            canvas.blit(surface, (x, y))
            drawn.append(key)
            min_scale = min(min_scale, target[0] / sizes[rect_id][0], target[1] / sizes[rect_id][1])
        on_done()
    return GridSpec(surface=canvas, keys=tuple(drawn), min_scale=min_scale), failed


def pack_into_grids(
    keys: Sequence[Key],
    store: ReviewStore,
    grid_w: int,
    grid_h: int,
    *,
    rotation: Rotation = "auto",
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[GridSpec], list[Key]]:
    """Pack the images named by `keys` into grid canvases sized for the current screen.

    Image bytes are fetched via the store. Images are packed at their fit_size, read from the image
    header, and decoded one bin at a time while that bin is composited. `rotation` says whether images
    may be rotated 90 degrees: always, never, or (auto) only if that needs fewer grids. Returns the
    GridSpecs, each holding a composited pygame surface, and the keys left out of every grid (missing,
    unreadable header, failed decode, or left unpacked), in input order. A grid holds only keys whose
    pixels it shows. on_progress(i, n) is called as each of the n images is handled, ending with (n, n).
    """
    n = len(keys)
    done = 0

    def advance() -> None:
        nonlocal done
        done += 1
        if on_progress is not None:
            on_progress(done, n)

    blobs = store.image_bytes_many(list(keys))

    # Sizes from the headers only: no image is decoded yet
    sizes: dict[int, tuple[int, int]] = {}
    left_out: set[int] = set()
    for idx, key in enumerate(keys):
        if key not in blobs:  # absent: the store already warned
            left_out.add(idx)
            advance()
            continue
        try:
            w, h = jpeg_size(blobs[key])
        except Exception as exc:  # noqa: BLE001 - an unreadable header makes the image unloadable, not fatal
            log.warning("cannot load %s: %s", key, exc)
            left_out.add(idx)
            advance()
            continue
        sizes[idx] = (w, h)

    # Bin-pack at the fit sizes; no pixels are read
    plan = plan_grids(sizes, grid_w, grid_h, rotation)

    # Composite one bin at a time, so at most one bin's decoded images are alive
    grids = []
    for placed in plan.bins:
        grid, failed = _composite_bin(placed, keys, blobs, sizes, grid_w, grid_h, plan.rotated, advance)
        left_out.update(failed)
        if grid.keys:
            grids.append(grid)

    # fit_size makes every image fit a bin, so the packer should leave none out; any it does is
    # left to the caller (shown as a single image, loaded on display) rather than dropped
    for idx in plan.unpacked:
        log.warning("%s was not packed into a grid", keys[idx])
        left_out.add(idx)
        advance()

    log.debug(
        "packed %d images into %d grids of %dx%d (rotation %s, rotated: %s); %d left out",
        n,
        len(grids),
        grid_w,
        grid_h,
        rotation,
        plan.rotated,
        len(left_out),
    )
    return grids, [keys[idx] for idx in sorted(left_out)]
