from dataclasses import dataclass, field
from pathlib import Path

import pygame as pg
from PIL import Image
from rectpack import newPacker

from .util import load_surface


@dataclass
class GridSpec:
    surface: pg.Surface
    image_ids: list[str] = field(default_factory=list)
    batch: str = ""


def pack_into_grids(
    items: list[dict],
    work_dir: Path,
    grid_w: int,
    grid_h: int,
) -> list[GridSpec]:
    """Pack review items into grid canvases sized for the current screen.

    Each item is a manifest dict with keys: image_id, batch, preprocessed_path.
    Returns a list of GridSpec, each holding a composited pygame surface.
    """
    # Read image dimensions (header-only, no pixel decode)
    sizes = []
    for item in items:
        path = work_dir / item["preprocessed_path"]
        with Image.open(path) as im:
            w, h = im.size
        sizes.append((w, h))

    # Bin-pack
    packer = newPacker()
    packer.add_bin(grid_w, grid_h, float("inf"))
    for idx, (w, h) in enumerate(sizes):
        packer.add_rect(w, h, idx)
    packer.pack()

    # Identify which items were packed into which bins
    packed = set()
    bins: dict[int, list[tuple[int, int, int, int, int]]] = {}
    for bin_idx, x, y, w, h, rect_id in packer.rect_list():
        bins.setdefault(bin_idx, []).append((rect_id, x, y, w, h))
        packed.add(rect_id)

    grids = []

    # Composite each bin into a surface
    for bin_idx in sorted(bins):
        surface = pg.Surface((grid_w, grid_h))
        surface.fill((0, 0, 0))
        image_ids = []
        batch = ""
        for rect_id, x, y, w, h in bins[bin_idx]:
            item = items[rect_id]
            image_ids.append(item["image_id"])
            if not batch:
                batch = item["batch"]
            img_surface = load_surface(str(work_dir / item["preprocessed_path"]))
            orig_w, orig_h = img_surface.get_size()
            if (w, h) == (orig_w, orig_h):
                surface.blit(img_surface, (x, y))
            else:
                rotated = pg.transform.rotate(img_surface, -90)
                surface.blit(rotated, (x, y))
        grids.append(GridSpec(surface=surface, image_ids=image_ids, batch=batch))

    # Overflow: images too large to fit any bin become single-image grids
    for idx in range(len(items)):
        if idx not in packed:
            item = items[idx]
            img_surface = load_surface(str(work_dir / item["preprocessed_path"]))
            grids.append(GridSpec(
                surface=img_surface,
                image_ids=[item["image_id"]],
                batch=item["batch"],
            ))

    return grids
