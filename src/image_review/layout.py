"""Grid layout: image sizes from JPEG headers and their bin-packing into grids.

Pure functions over plain data (stdlib and rectpack only), so both the pygame viewer and `serve` compute the same
layouts; this module must not import pygame or PIL.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import NamedTuple

from rectpack import newPacker

from .status import Rotation


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


# SOFn markers carry the frame size; C4 (DHT), C8 (JPG, reserved) and CC (DAC) share the range but are not frames
_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Markers that stand alone, with no length field: RST0-RST7 and TEM
_STANDALONE_MARKERS = frozenset({*range(0xD0, 0xD8), 0x01})
_SOS, _EOI = 0xDA, 0xD9


def jpeg_size(buf: bytes) -> tuple[int, int]:
    """(width, height) of a JPEG from its frame header; no pixels are decoded. Raises ValueError if unreadable.

    The walk goes on past the frame header to a complete start-of-scan header, so a file cut short before its scan
    is rejected here, as PIL.Image.open rejects it, rather than failing later at decode.
    """
    if buf[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG: no SOI marker")
    pos = 2
    size: tuple[int, int] | None = None
    while True:
        # A marker is 0xFF followed by a non-0xFF code; extra 0xFF bytes before it are fill
        if pos >= len(buf) or buf[pos] != 0xFF:
            raise ValueError(f"no JPEG marker at offset {pos}")
        while pos < len(buf) and buf[pos] == 0xFF:
            pos += 1
        if pos >= len(buf):
            raise ValueError("JPEG ends before its scan")
        marker = buf[pos]
        pos += 1
        if marker in _STANDALONE_MARKERS:
            continue
        if marker == _EOI:
            raise ValueError("JPEG ends (EOI) before its scan")
        # Every other segment starts with a big-endian length that counts itself but not the marker
        if pos + 2 > len(buf):
            raise ValueError("JPEG ends inside a segment length")
        length = int.from_bytes(buf[pos : pos + 2], "big")
        if length < 2:
            raise ValueError(f"JPEG segment length {length} at offset {pos}")
        if pos + length > len(buf):
            raise ValueError(f"JPEG ends inside the segment at offset {pos}")
        if marker == _SOS:
            if size is None:
                raise ValueError("JPEG has no frame header before its scan")
            return size
        if marker in _SOF_MARKERS:
            # Frame header after the length: sample precision (1 byte), height (2), width (2), component count (1)
            if length < 8:
                raise ValueError(f"JPEG frame header length {length}")
            height = int.from_bytes(buf[pos + 3 : pos + 5], "big")
            width = int.from_bytes(buf[pos + 5 : pos + 7], "big")
            if not width or not height:
                raise ValueError(f"JPEG frame size {width}x{height}")
            size = (width, height)
        pos += length


@dataclass(frozen=True)
class GridPlan:
    rotated: bool  # whether rectpack was allowed to rotate rects (and images are fit with rotation allowed)
    bins: tuple[tuple[PlacedRect, ...], ...]  # in bin-index order
    unpacked: frozenset[int]  # rect ids given but not placed in any bin


def _pack(sizes: Mapping[int, tuple[int, int]], grid_w: int, grid_h: int, rotate: bool) -> dict[int, list[PlacedRect]]:
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


def plan_grids(sizes: Mapping[int, tuple[int, int]], grid_w: int, grid_h: int, rotation: Rotation) -> GridPlan:
    """Pack images of the given (width, height) sizes, keyed by rect id, into grid_w x grid_h bins.

    The insertion order of `sizes` is the order rects are added to rectpack, which affects the layout. `rotation`
    says whether images may be rotated 90 degrees: always, never, or (auto) only if that needs fewer bins. Packing
    reads no pixels, so "auto" packs both ways and keeps the rotated packing only if it needs strictly fewer bins.
    """
    rotated = rotation == "always"
    bins = _pack(sizes, grid_w, grid_h, rotated)
    if rotation == "auto":
        with_rotation = _pack(sizes, grid_w, grid_h, True)
        if len(with_rotation) < len(bins):
            bins, rotated = with_rotation, True
    placed = {rect.rect_id for rects in bins.values() for rect in rects}
    return GridPlan(
        rotated=rotated,
        bins=tuple(tuple(bins[bin_idx]) for bin_idx in sorted(bins)),
        unpacked=frozenset(sizes.keys() - placed),
    )


def is_rotated(rect: PlacedRect, source_size: tuple[int, int], grid_w: int, grid_h: int, plan_rotated: bool) -> bool:
    """Whether rectpack rotated `rect`: its packed size differs from the image's fit size."""
    return (rect.w, rect.h) != fit_size(*source_size, grid_w, grid_h, plan_rotated)
