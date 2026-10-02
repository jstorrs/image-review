import hashlib
import importlib.metadata
import importlib.util
import io
import json
import logging
import multiprocessing
import os
import re
import shutil
import signal
import stat
import unicodedata
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from itertools import groupby
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from zipfile import ZipFile

import matplotlib
import numpy as np
import pydicom
import skimage as ski
from PIL import Image
from pydicom.dataset import FileMetaDataset
from pydicom.errors import InvalidDicomError
from pydicom.uid import (
    UID,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    MediaStorageDirectoryStorage,
)
from scipy import ndimage as ndi
from tqdm import tqdm

from .access import MANIFEST_NAME, Access, Modes, modes
from .connection import package_version
from .export import ICON_SUFFIX, has_unsafe_char
from .review_db import format_tsv
from .status import ImageId
from .store import MANIFEST_HEADER, SKIPPED_HEADER, SKIPPED_NAME, SkipKind, SkippedRow

log = logging.getLogger(__name__)

EROSION_KERNEL_SIZE = 5
OUTLIER_PERCENTILE = 0.01
INTENSITY_MARGIN = 0.02
TAIL_FRACTION = 0.10  # output range given to each outlier tail (below bot, above top)
CLAHE_KERNEL_SIZE = 96  # CLAHE tile size in pixels
JPEG_QUALITY = 95
JPEG_SUBSAMPLING = 0  # 4:4:4, so small coloured text keeps its edges
ALPHA_BACKGROUND = 0.5  # mid-gray, so content carried only by alpha stays visible whatever its colour
SIDE_BY_SIDE_GAP = 4  # pixels between views placed side by side in one rendered image

SNIFF_BYTES = 132  # enough for the DICOM preamble and its `DICM` prefix
_DICOM_MAGIC_OFFSET = 128
_DICOM_MAGIC = b"DICM"
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")  # an archive, or an empty one
_APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"  # macOS `._name` metadata files
_RASTER_MAGIC = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",  # JPEG
    b"II*\x00",  # TIFF, little-endian
    b"MM\x00*",  # TIFF, big-endian
    b"II+\x00",  # BigTIFF, little-endian
    b"MM\x00+",  # BigTIFF, big-endian
    b"GIF87a",
    b"GIF89a",
    b"\x00\x00\x00\x0cjP  \r\n\x87\n",  # JPEG 2000 (JP2)
    b"\xff\x4f\xff\x51",  # JPEG 2000 codestream (J2K)
)
_BMP_DIB_HEADER_SIZES = {12, 40, 52, 56, 64, 108, 124}
_PNM_TYPES = {b"P1", b"P2", b"P3", b"P4", b"P5", b"P6"}
_OVERLAY_FIRST_GROUP = 0x6000
_OVERLAY_LAST_GROUP = 0x601E
_OVERLAY_DATA_ELEMENT = 0x3000
_OVERLAY_ORIGIN_ELEMENT = 0x0050
_DICOM_SUFFIXES = {".dcm", ".dicom", ".ima"}
_RASTER_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp",
    ".jp2", ".j2k", ".jpx", ".pbm", ".pgm", ".ppm", ".pnm", ".heic", ".heif", ".avif",
}  # fmt: skip
_BARE_DICOM_GROUPS = {b"\x02\x00", b"\x08\x00"}  # (0002,xxxx) file meta or (0008,xxxx), little-endian
_BARE_DICOM_MAX_FIRST_LENGTH = 256  # implicit VR: the first element is short (a group length, charset or UID)
_DICOM_VRS = {
    b"AE", b"AS", b"AT", b"CS", b"DA", b"DS", b"DT", b"FL", b"FD", b"IS", b"LO", b"LT", b"OB", b"OD", b"OF",
    b"OL", b"OV", b"OW", b"PN", b"SH", b"SL", b"SQ", b"SS", b"ST", b"SV", b"TM", b"UC", b"UI", b"UL", b"UN",
    b"UR", b"US", b"UT", b"UV",
}  # fmt: skip
_APPLEDOUBLE_REASON = "AppleDouble metadata (macOS resource fork)"
_ARCHIVE_MAGIC = (
    (b"\x1f\x8b", "gzip"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
    *((b"BZh%d" % level, "bzip2") for level in range(10)),  # `BZh` and an ASCII digit
)
_ARCHIVE_SUFFIXES = {
    ".gz": "gzip", ".tgz": "gzip", ".tar": "tar", ".7z": "7z", ".rar": "rar", ".bz2": "bzip2", ".xz": "xz", ".zst": "zstd",
}  # fmt: skip
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}
_ALPHA_MODES = {"RGBA", "RGBa", "LA", "La", "PA"}
_HIGH_BIT_GRAY_MODES = {"I", "F", "I;16", "I;16L", "I;16B", "I;16N"}
_PIXEL_DATA_KEYWORDS = ("PixelData", "FloatPixelData", "DoubleFloatPixelData")
PROVENANCE_NAME = "preprocess.json"
PENDING_NAME = ".pending"  # staging subdirectory holding JPGs until their image_ids are known not to collide
COLLISION_REASON = "image_id collides with another input (rename one of them)"
BAD_NAME_REASON = "name is not UTF-8 or holds a control character or line separator; rename it"
_LIBRARIES = ("pydicom", "numpy", "scikit-image", "Pillow", "matplotlib")  # distribution names
# Optional pixel-data codecs, recorded when importable: module name -> distribution name
_CODEC_DISTRIBUTIONS = {"gdcm": "python-gdcm", "pylibjpeg": "pylibjpeg", "openjpeg": "pylibjpeg-openjpeg"}
IN_FLIGHT_PER_JOB = 2  # inputs submitted to the pool and not yet written, per worker: bounds the bytes held in memory
WORKER_CHECK_SECONDS = 1.0  # how often the parent, waiting on a render, checks that no worker has died
# Spawned workers read these at numpy import: one BLAS/OpenMP thread each, not one per core per worker
_BLAS_THREAD_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
# Workers start fresh on every platform: no inherited threads, signal handlers or open ZIPs, as with macOS's default
_SPAWN = multiprocessing.get_context("spawn")

Kind = Literal["dicom", "raster"]
Content = Literal["dicom", "raster", "zip"]


@dataclass(frozen=True)
class Rejected:
    """Content that is not rendered: `ignored` (not an input) or `failed` (e.g. a `.zip` name with other content)."""

    kind: SkipKind
    reason: str


@dataclass(frozen=True)
class Candidate:
    """One discovered input, with its raw bytes (no decoding happens in discovery)."""

    image_id: ImageId
    kind: Kind
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class Rendered:
    """One output image: (H, W, 3) uint8 RGB."""

    image_id: ImageId
    rgb: np.ndarray


@dataclass(frozen=True)
class Encoded:
    """One output image as JPEG bytes, with the SHA-256 of the source file (or ZIP entry) it came from."""

    image_id: ImageId
    jpeg: bytes
    source_sha256: str


@dataclass(frozen=True)
class Staged:
    """An encoded image saved under a provisional name until it is known not to collide."""

    image_id: ImageId
    path: Path
    source_sha256: str
    jpeg_sha256: str


@dataclass(frozen=True)
class PreprocessResult:
    found: int
    written: int
    batches: int
    skipped: list[SkippedRow]
    skipped_path: Path


class WorkerCrashed(RuntimeError):
    """A worker process died (out of memory, or a crash in a decoder); the run is abandoned."""


class WorkDirExists(ValueError):
    """The work directory cannot be created: it is in use, or an earlier run left its staging dir."""


class NotAnImage(Exception):
    """A DICOM object that is not an image by design (a DICOMDIR index); recorded as ignored."""


class Unsupported(Exception):
    """A decodable input this tool does not render; the message starts with `unsupported:`."""

    def __init__(self, what: str) -> None:
        super().__init__(f"unsupported: {what}")


class DecodeError(Exception):
    """Compressed pixel data that no installed codec could decode; the message starts with `cannot decode`."""


# ---------------------------------------------------------------- rendering (pure)


def compress_image(image: np.ndarray) -> np.ndarray:
    same_vert = image == np.roll(image, 1, axis=0)
    same_horiz = image == np.roll(image, 1, axis=1)
    both = same_vert & same_horiz
    if both.ndim == 3:
        both = np.all(both, axis=2)
    # A square erosion is separable, and a row is dropped only if its whole eroded row is True. Every
    # window cell lies in the row (reflection only repeats cells) and every cell is in its own window, so
    # that equals eroding the per-row all() with a 1-D window; likewise for columns.
    rows = ndi.minimum_filter1d(both.all(axis=1), EROSION_KERNEL_SIZE, mode="reflect")
    cols = ndi.minimum_filter1d(both.all(axis=0), EROSION_KERNEL_SIZE, mode="reflect")
    return image[~rows][:, ~cols]


def _crop(image: np.ndarray) -> np.ndarray:
    """compress_image, but keep the uncropped image when cropping would leave nothing."""
    cropped = compress_image(image)
    return cropped if cropped.size else image


def _sop_class(dcm: pydicom.FileDataset) -> UID | None:
    """The SOP Class UID, if the dataset (or its file meta) has a single one."""
    meta = getattr(dcm, "file_meta", None)
    value = dcm.get("SOPClassUID") or (meta.get("MediaStorageSOPClassUID") if meta is not None else None)
    return UID(value) if isinstance(value, str) and value else None  # not a MultiValue or other junk


def _has_pixel_data(dcm: pydicom.FileDataset) -> bool:
    return any(keyword in dcm for keyword in _PIXEL_DATA_KEYWORDS)


def _compress_tails(img: np.ndarray) -> np.ndarray:
    """Map float `img` to [0, 1], squeezing the outlier tails instead of clipping them.

    The robust core range [bot, top] fills [TAIL_FRACTION, 1 - TAIL_FRACTION]; values
    below bot and above top are interpolated into the remaining tails, so extreme
    values (e.g. burned-in text at the maximum) stay distinct from the core.
    Falls back to a plain min-max rescale when no robust core can be found.
    """
    lo, hi = float(img.min()), float(img.max())
    minmax = (img - lo) / (hi - lo)
    margin_initial = OUTLIER_PERCENTILE * (hi - lo)
    inner = img[(img > lo + margin_initial) & (img < hi - margin_initial)]
    if inner.size == 0:
        return minmax
    bot, top = np.quantile(inner, [OUTLIER_PERCENTILE, 1 - OUTLIER_PERCENTILE])
    margin_final = INTENSITY_MARGIN * (top - bot)
    bot, top = bot + margin_final, top - margin_final
    if not lo < bot < top < hi:
        return minmax
    return np.interp(img, [lo, bot, top, hi], [0, TAIL_FRACTION, 1 - TAIL_FRACTION, 1]).astype(np.float32)


def _to_uint8(pixels: np.ndarray, bits: int) -> np.ndarray:
    """Scale unsigned integer samples of `bits` significant bits to uint8."""
    if bits <= 8:
        return ski.util.img_as_ubyte(pixels.astype(np.uint8))
    scaled = pixels.astype(np.float32) * (255 / (2**bits - 1))
    return np.clip(np.rint(scaled), 0, 255).astype(np.uint8)


def _overlay_mask(dcm: pydicom.FileDataset, shape: tuple[int, int]) -> np.ndarray:
    """Union of the overlay planes (groups 0x6000-0x601E with OverlayData) as a bool mask of `shape`.

    Each plane is placed at its OverlayOrigin (1-based row, column; default 1, 1)
    and clipped to the image. All False when there are no overlays. A plane that
    cannot be decoded raises ValueError naming its group.
    """
    mask = np.zeros(shape, dtype=bool)
    for group in range(_OVERLAY_FIRST_GROUP, _OVERLAY_LAST_GROUP + 1, 2):
        if (group, _OVERLAY_DATA_ELEMENT) not in dcm:
            continue
        try:
            plane = dcm.overlay_array(group)
            if plane.ndim != 2:
                raise ValueError(f"overlay array with shape {plane.shape}")
            # Dataset.get() with a tuple tag returns the DataElement, which is subscriptable but not typed as iterable.
            origin = cast(Sequence[int], dcm.get((group, _OVERLAY_ORIGIN_ELEMENT), [1, 1]))
            row0, col0 = (int(v) - 1 for v in origin)
        except Exception as exc:
            raise ValueError(f"overlay 0x{group:04X} cannot be decoded: {exc}") from exc
        top, left = max(row0, 0), max(col0, 0)
        bottom, right = min(row0 + plane.shape[0], shape[0]), min(col0 + plane.shape[1], shape[1])
        if top < bottom and left < right:
            mask[top:bottom, left:right] |= plane[top - row0 : bottom - row0, left - col0 : right - col0].astype(bool)
    return mask


def _gray_dicom(pixels: np.ndarray, photometric: str, overlay: np.ndarray) -> np.ndarray:
    """Float [0, 1] image from MONOCHROME1/2 pixels: tail compression, overlay, crop, CLAHE, crop.

    Overlay pixels are set to 1.0 after intensity mapping, so they come out at the top of the range.
    """
    img = pixels.astype(np.float32)
    if photometric == "MONOCHROME1":
        img = -img
    constant = img.min() >= img.max()
    img = np.zeros_like(img) if constant else _compress_tails(img)
    img[overlay] = 1.0
    img = _crop(img)
    return img if constant else _crop(ski.exposure.equalize_adapthist(img, kernel_size=CLAHE_KERNEL_SIZE))


def _decode_pixels(dcm: pydicom.FileDataset) -> np.ndarray:
    """`dcm.pixel_array`; a compressed file that cannot be decoded is a `DecodeError` naming its transfer syntax."""
    try:
        return dcm.pixel_array
    except Exception as exc:
        syntax = dcm.file_meta.get("TransferSyntaxUID")
        if syntax is None or not syntax.is_compressed:
            raise
        cause = (str(exc).strip().splitlines() or [""])[0]
        raise DecodeError(f"cannot decode {syntax.name}: {type(exc).__name__}: {cause}") from exc


def _colour_dicom(dcm: pydicom.FileDataset, photometric: str) -> np.ndarray:
    """(H, W, 3) uint8 from RGB, YBR_* (pydicom's pixel_array yields RGB) or PALETTE COLOR pixels."""
    pixels = _decode_pixels(dcm)
    bits = int(dcm.BitsStored)
    if photometric == "PALETTE COLOR":
        pixels = pydicom.pixels.apply_color_lut(pixels, dcm)
        bits = int(dcm.RedPaletteColorLookupTableDescriptor[2])
    if pixels.ndim != 3 or pixels.shape[2] != 3:
        raise Unsupported(f"pixel array with shape {pixels.shape}")
    return _to_uint8(pixels, bits)


def preprocess_dicom(dcm: pydicom.FileDataset, colormap: str = "inferno") -> np.ndarray:
    """Render a single-frame DICOM to (H, W, 3) uint8: grayscale through `colormap`, colour as is.

    Overlay planes are drawn at maximum brightness.
    """
    if not _has_pixel_data(dcm):
        sop_class = _sop_class(dcm)
        if sop_class == MediaStorageDirectoryStorage:
            raise NotAnImage("DICOMDIR index")
        raise Unsupported(f"no pixel data ({sop_class.name})" if sop_class else "no pixel data")
    frames = int(dcm.get("NumberOfFrames") or 1)
    if frames > 1:
        raise Unsupported(f"multi-frame DICOM ({frames} frames)")
    photometric = dcm.get("PhotometricInterpretation")
    match photometric:
        case "MONOCHROME1" | "MONOCHROME2":
            pixels = _decode_pixels(dcm)
            if pixels.ndim != 2:
                raise Unsupported(f"pixel array with shape {pixels.shape}")
            return apply_colormap(_gray_dicom(pixels, photometric, _overlay_mask(dcm, pixels.shape)), colormap)
        case (
            "RGB"
            | "PALETTE COLOR"
            | "YBR_FULL"
            | "YBR_FULL_422"
            | "YBR_PARTIAL_420"
            | "YBR_PARTIAL_422"
            | "YBR_ICT"
            | "YBR_RCT"
        ):
            rgb = _colour_dicom(dcm, photometric)
            rgb[_overlay_mask(dcm, rgb.shape[:2])] = 255
            return _crop(rgb)
        case _:
            raise Unsupported(f"photometric interpretation {photometric}")


def apply_colormap(img: np.ndarray, colormap: str = "inferno") -> np.ndarray:
    cm = matplotlib.colormaps[colormap]
    return ski.util.img_as_ubyte(cm(img)[:, :, :3])


def _side_by_side(views: list[np.ndarray], fill: float) -> np.ndarray:
    """Place float views left to right, top-aligned, padded and separated by `fill`.

    If any view is RGB, grayscale views are promoted to RGB.
    """
    if any(v.ndim == 3 for v in views):
        views = [v if v.ndim == 3 else np.stack([v] * 3, axis=2) for v in views]
    height = max(v.shape[0] for v in views)
    channels = views[0].shape[2:]
    parts = []
    for i, view in enumerate(views):
        if i:
            parts.append(np.full((height, SIDE_BY_SIDE_GAP, *channels), fill, dtype=np.float32))
        padded = np.full((height, view.shape[1], *channels), fill, dtype=np.float32)
        padded[: view.shape[0]] = view
        parts.append(padded)
    return np.concatenate(parts, axis=1)


def _with_alpha(color: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Flatten float `color` with float `alpha` (both in [0, 1]).

    Opaque alpha is just dropped. Otherwise the result shows the composite over
    mid-gray (left) beside the raw channels with alpha ignored (right), so
    content carried only by alpha and content hidden under transparent pixels
    are both visible.
    """
    if alpha.min() >= 1:
        return color
    a = alpha if color.ndim == 2 else alpha[:, :, None]
    composited = color * a + ALPHA_BACKGROUND * (1 - a)
    return _side_by_side([composited, color], fill=ALPHA_BACKGROUND)


def _high_bit_gray(im: Image.Image) -> np.ndarray:
    """Scale an I/F/I;16 image to float32 [0, 1], keeping its full range."""
    arr = np.asarray(im)
    if im.mode.startswith("I;16"):
        return ski.util.img_as_float32(arr.astype(np.uint16))
    arr = arr.astype(np.float32)
    lo, hi = arr.min(), arr.max()
    return (arr - lo) / (hi - lo) if hi > lo else np.zeros_like(arr)


def _decode_frame(im: Image.Image) -> np.ndarray:
    """Decode the current frame to float32 [0, 1]: (H, W) grayscale or (H, W, 3) RGB."""
    transparency = im.info.get("transparency")
    if im.mode in _HIGH_BIT_GRAY_MODES:
        gray = _high_bit_gray(im)
        if transparency is None:
            return gray
        alpha = (np.asarray(im) != transparency).astype(np.float32)  # tRNS key on native values
        return _with_alpha(gray, alpha)
    if im.mode in _ALPHA_MODES or transparency is not None:
        is_gray = im.mode in ("1", "L", "LA", "La")
        arr = ski.util.img_as_float32(np.asarray(im.convert("LA" if is_gray else "RGBA")))
        color = arr[:, :, 0] if is_gray else arr[:, :, :3]
        return _with_alpha(color, arr[:, :, -1])
    if im.mode in ("1", "L"):
        return ski.util.img_as_float32(np.asarray(im))
    return ski.util.img_as_float32(np.asarray(im.convert("RGB")))


def decode_raster(data: bytes) -> np.ndarray:
    """Decode with PIL, branching on mode, to float32 [0, 1] (H, W) grayscale or (H, W, 3) RGB.

    Grayscale modes (including 16-bit) stay grayscale; everything else (CMYK,
    YCbCr, P, ...) becomes RGB; see `_with_alpha` for transparency. An MPO
    (JPEG with extra MPF images: HDR gain maps, previews) renders all its frames
    side by side; any other multi-frame image is unsupported.
    """
    with Image.open(io.BytesIO(data)) as im:
        frames = getattr(im, "n_frames", 1)
        if frames > 1 and im.format != "MPO":
            raise Unsupported(f"multi-frame image ({frames} frames)")
        views = []
        for index in range(frames):
            im.seek(index)
            im.load()
            views.append(_decode_frame(im))
    return views[0] if len(views) == 1 else _side_by_side(views, fill=0.0)


def preprocess_raster(img: np.ndarray) -> np.ndarray:
    """Normalize a decoded raster to (H, W, 3) uint8, applying CLAHE to grayscale."""
    if img.ndim == 2:
        img = ski.util.img_as_float32(img)
        img = ski.exposure.equalize_adapthist(img, kernel_size=CLAHE_KERNEL_SIZE)
        img = ski.util.img_as_ubyte(img)
        return np.stack([img] * 3, axis=2)
    if img.ndim == 3 and img.shape[2] == 3:
        return ski.util.img_as_ubyte(img)
    raise Unsupported(f"image with shape {img.shape}")


def _has_preamble(head: bytes) -> bool:
    """Whether the 128-byte preamble is followed by `DICM`; false for input too short to hold it."""
    return head[_DICOM_MAGIC_OFFSET : _DICOM_MAGIC_OFFSET + len(_DICOM_MAGIC)] == _DICOM_MAGIC


def read_dicom(data: bytes) -> pydicom.FileDataset:
    """Read DICOM with or without the preamble, `DICM` and file meta.

    `force` lets pydicom read a bare dataset; a file with the preamble reads the
    same either way. `force` parses anything, so a bare dataset is
    `InvalidDicomError` unless it has a SOP Class UID or pixel data (an
    ACR-NEMA file may have only the latter). A bare dataset is uncompressed by
    definition, so its transfer syntax is the encoding pydicom detected.
    """
    dcm = pydicom.dcmread(io.BytesIO(data), force=True)
    if not _has_preamble(data) and _sop_class(dcm) is None and not _has_pixel_data(dcm):
        first = min([*dcm.file_meta.keys(), *dcm.keys()], default=None)
        if first is not None and first.group in (0x0002, 0x0008):
            raise InvalidDicomError("not DICOM (no preamble, no SOP Class UID and no pixel data)")
        raise InvalidDicomError("not DICOM (no preamble and no recognizable DICOM elements)")
    if "TransferSyntaxUID" not in dcm.file_meta:
        implicit, little = dcm.original_encoding
        if implicit:
            dcm.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
        else:
            dcm.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian if little else ExplicitVRBigEndian
    return dcm


def _render_icon(dcm: pydicom.FileDataset, image_id: ImageId, colormap: str) -> Rendered | SkippedRow | None:
    """The embedded IconImageSequence thumbnail (item 0) as its own row, or None when there is none."""
    icons = dcm.get("IconImageSequence")
    if not icons:
        return None
    icon_id = ImageId(f"{image_id}{ICON_SUFFIX}")
    try:
        icon = icons[0]
        # icon pixels are always uncompressed, in the byte order of the enclosing dataset
        big_endian = dcm.file_meta.get("TransferSyntaxUID") == ExplicitVRBigEndian
        icon.file_meta = FileMetaDataset()
        icon.file_meta.TransferSyntaxUID = ExplicitVRBigEndian if big_endian else ExplicitVRLittleEndian
        return Rendered(icon_id, preprocess_dicom(icon, colormap))
    except Exception as exc:  # noqa: BLE001 - a bad icon must not lose the main image; it is recorded as its own failed row
        return _failed(icon_id, exc)


def render(
    kind: Kind, image_id: ImageId, data: bytes, colormap: str
) -> tuple[Rendered, *tuple[Rendered | SkippedRow, ...]]:
    """Render one input: its image, plus (DICOM) an `{image_id}#icon` row for an embedded icon image.

    A failure of the main image raises; a failure of the icon is returned as a `SkippedRow` row.
    """
    match kind:
        case "dicom":
            dcm = read_dicom(data)
            main = Rendered(image_id, preprocess_dicom(dcm, colormap))
            icon = _render_icon(dcm, image_id, colormap)
            return (main,) if icon is None else (main, icon)
        case "raster":
            return (Rendered(image_id, preprocess_raster(decode_raster(data))),)


# ---------------------------------------------------------------- discovery (IO)


def _is_raster(head: bytes) -> bool:
    return (
        head.startswith(_RASTER_MAGIC)
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
        or (
            head.startswith(b"BM")
            and len(head) >= 18
            and int.from_bytes(head[14:18], "little") in _BMP_DIB_HEADER_SIZES
        )
        or (head[:2] in _PNM_TYPES and head[2:3].isspace())
        or (head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS)  # HEIF/AVIF: Pillow decides
    )


def _is_bare_dicom(head: bytes) -> bool:
    """A dataset without preamble: a little-endian (0002|0008,xxxx) first element with a VR or a short even length.

    The element number may be odd: (0008,0005) Specific Character Set often comes first.
    """
    if len(head) < 8 or head[:2] not in _BARE_DICOM_GROUPS:
        return False
    implicit_length = int.from_bytes(head[4:8], "little")
    return head[4:6] in _DICOM_VRS or (implicit_length <= _BARE_DICOM_MAX_FIRST_LENGTH and implicit_length % 2 == 0)


def _unsupported_archive(name: str) -> Rejected:
    return Rejected("failed", f"unsupported: {name} archive")


def classify(name: str, head: bytes) -> Content | Rejected:
    """Classify an input by its first `SNIFF_BYTES` bytes; `name` is its path (or ZIP entry name).

    The name only matters when the content is not recognized: macOS metadata is
    ignored, a DICOM or image suffix leaves the decision to the decoder (so a
    damaged file fails rather than being dropped), and a `.zip` or other archive
    name fails. Compressed archives (gzip, bzip2, xz, zstd, 7z, rar, tar) fail as unsupported.
    """
    if _has_preamble(head):
        return "dicom"
    if head.startswith(_ZIP_MAGIC):
        return "zip"
    for magic, archive in _ARCHIVE_MAGIC:
        if head.startswith(magic):
            return _unsupported_archive(archive)
    if _is_raster(head):
        return "raster"
    path = PurePosixPath(name)
    if head.startswith(_APPLEDOUBLE_MAGIC) or path.name.startswith("._") or "__MACOSX" in path.parts:
        return Rejected("ignored", _APPLEDOUBLE_REASON)
    if _is_bare_dicom(head):
        return "dicom"
    suffix = path.suffix.lower()
    if suffix in _DICOM_SUFFIXES:
        return "dicom"
    if suffix in _RASTER_SUFFIXES:
        return "raster"
    if suffix in _ARCHIVE_SUFFIXES:  # tar's `ustar` is at offset 257, beyond the sniffed bytes
        return _unsupported_archive(_ARCHIVE_SUFFIXES[suffix])
    if suffix == ".zip":
        return Rejected("failed", "unrecognized content for a .zip file")
    return Rejected("ignored", "not an image (unrecognized content)")


def _bad_char(c: str) -> bool:
    """A character skipped.tsv cannot hold: a surrogate (from a name that is not UTF-8) or one export refuses."""
    return unicodedata.category(c) == "Cs" or has_unsafe_char(c)


def _escape_name(text: str) -> str:
    """`text` with each bad character spelled out: a surrogate from a non-UTF-8 byte as that byte (`\\xff`), an
    ASCII control as `\\xNN`, any other (C1, U+2028/U+2029, another surrogate) as `\\uNNNN`, so no two differ only
    in spelling a byte that is on disk and one that is not."""

    def escape(c: str) -> str:
        code = ord(c)
        if 0xDC80 <= code <= 0xDCFF:  # os.fsdecode's surrogateescape for the byte code - 0xDC00
            return f"\\x{code - 0xDC00:02x}"
        if not _bad_char(c):
            return c
        return f"\\x{code:02x}" if code < 0x80 else f"\\u{code:04x}"

    return "".join(map(escape, text))


def _checked_name(item: Candidate | SkippedRow) -> Candidate | SkippedRow:
    """`item`, or a failed row under its escaped id if that id could not be written to skipped.tsv or exported."""
    if any(map(_bad_char, item.image_id)):
        return SkippedRow(ImageId(_escape_name(item.image_id)), "failed", BAD_NAME_REASON)
    return item


def _clean_reason(reason: str) -> str:
    """One line, and reproducible: runs of bad characters (see `_bad_char`) become a space, object reprs `<data>`."""
    reason = re.sub(r"<[^<>]* object at 0x[0-9a-fA-F]+>", "<data>", reason)
    return "".join(" " if bad else "".join(run) for bad, run in groupby(reason, _bad_char)).strip()


def _failed(image_id: ImageId, exc: Exception) -> SkippedRow:
    reason = str(exc) if isinstance(exc, Unsupported | DecodeError) else f"{type(exc).__name__}: {exc}"
    return SkippedRow(image_id, "failed", _clean_reason(reason))


def _candidate(image_id: ImageId, kind: Kind, read: Callable[[], bytes]) -> Candidate | SkippedRow:
    """The input with the bytes `read` returns, or a failed row if they cannot be read."""
    try:
        return Candidate(image_id, kind, read())
    except Exception as exc:  # noqa: BLE001 - an unreadable input is recorded in skipped.tsv
        return _failed(image_id, exc)


def _discover_zip(path: Path) -> Iterator[Candidate | SkippedRow]:
    zip_id = ImageId(path.as_posix())
    try:
        zf = ZipFile(path)
    except Exception as exc:  # noqa: BLE001 - any failure to open the archive is recorded as a skip
        yield _failed(zip_id, exc)
        return
    with zf:
        # Repeated names are distinct entries: read each by its ZipInfo and give
        # later ones `#2`, `#3`, ... An entry literally named `a.png#2` can still
        # clash with the second `a.png`; `colliding_ids` catches that after rendering.
        files = [info for info in zf.infolist() if not info.is_dir()]
        if not files:
            yield SkippedRow(zip_id, "ignored", "zip contains no files")
        seen: Counter[str] = Counter()
        for info in files:
            seen[info.filename] += 1
            nth = seen[info.filename]
            image_id = ImageId(f"{zip_id}::{info.filename}" + (f"#{nth}" if nth > 1 else ""))
            try:
                with zf.open(info) as f:
                    head = f.read(SNIFF_BYTES)
            except Exception as exc:  # noqa: BLE001 - an unreadable entry (encrypted, bad compression) is recorded as failed
                yield _failed(image_id, exc)
                continue
            match classify(info.filename, head):
                case "zip":
                    yield SkippedRow(image_id, "failed", "unsupported: nested zip")
                case Rejected(kind, reason):
                    yield SkippedRow(image_id, kind, reason)
                case "dicom" | "raster" as kind:
                    yield _candidate(image_id, kind, partial(zf.read, info))


def _discover_file(path: Path, named: bool = False) -> Iterator[Candidate | SkippedRow]:
    """Classify one file by content (reading only its first bytes); a ZIP yields its entries.

    A file `named` as a source was asked for explicitly, so a file that would be
    ignored fails instead (macOS metadata stays ignored).
    """
    image_id = ImageId(path.as_posix())
    try:
        if stat.S_ISREG(path.stat().st_mode):  # follows symlinks; a FIFO would block the read
            with open(path, "rb") as f:
                outcome = classify(path.as_posix(), f.read(SNIFF_BYTES))
        else:
            outcome = Rejected("ignored", "not a regular file")
    except OSError as exc:
        yield _failed(image_id, exc)
        return
    match outcome:
        case "zip":
            yield from _discover_zip(path)
        case Rejected("ignored", reason) if named and reason != _APPLEDOUBLE_REASON:
            yield SkippedRow(image_id, "failed", reason)
        case Rejected(kind, reason):
            yield SkippedRow(image_id, kind, reason)
        case "dicom" | "raster" as kind:
            yield _candidate(image_id, kind, path.read_bytes)


def _symlinked_directory(link: Path, target: Path, source_dirs: tuple[Path, ...]) -> SkippedRow:
    """The row for a symlinked directory, which is never entered (it could lead out of the source).

    `target` is the resolved link. An enclosing directory is the link's own
    directory or any ancestor of it (so the source root and its ancestors).
    """
    link_id = ImageId(link.as_posix())
    link_dir = link.parent.resolve()
    if target == link_dir or target in link_dir.parents:
        return SkippedRow(link_id, "ignored", "symlink to an enclosing directory")
    for source in source_dirs:
        if target == source or source in target.parents:
            return SkippedRow(
                link_id,
                "ignored",
                _clean_reason(f"symlinked directory already included via SOURCE {_escape_name(source.as_posix())}"),
            )
    return SkippedRow(
        link_id,
        "failed",
        _clean_reason(
            f"symlinked directory not followed; pass its target {_escape_name(target.as_posix())} as a SOURCE"
        ),
    )


def _discover_directory(
    root: Path, exclude: frozenset[Path], source_dirs: tuple[Path, ...]
) -> Iterator[Candidate | SkippedRow]:
    """Walk `root` in sorted order without entering symlinked directories (see `_symlinked_directory`) or `exclude`.

    An unreadable directory (including `root`) becomes a failed row. Symlinked
    files are read; a dangling symlink is a failed row.
    """
    errors: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=errors.append):
        yield from (_failed(ImageId(Path(e.filename).as_posix()), e) for e in errors)
        errors.clear()
        here = Path(dirpath)
        entered = []
        for name in sorted(dirnames):
            sub = here / name
            target = sub.resolve()
            if target in exclude or any(e in target.parents for e in exclude):
                continue
            if sub.is_symlink():
                yield _symlinked_directory(sub, target, source_dirs)
            else:
                entered.append(name)
        dirnames[:] = entered
        for name in sorted(filenames):
            yield from _discover_file(here / name)
    yield from (_failed(ImageId(Path(e.filename).as_posix()), e) for e in errors)


def discover(sources: list[Path], exclude: frozenset[Path] = frozenset()) -> Iterator[Candidate | SkippedRow]:
    """Yield every input under `sources`, classified by content; nothing under `exclude` (resolved paths)."""
    source_dirs = tuple(s.resolve() for s in sources if s.is_dir())
    for source in tqdm(sources, desc="Sources", position=0):
        items = (
            _discover_directory(source, exclude, source_dirs) if source.is_dir() else _discover_file(source, named=True)
        )
        # Every image_id is minted in discovery, and later ones (`#icon`, collision rows) derive from these.
        for item in tqdm(items, desc=source.name, position=1, leave=False, unit="input"):
            yield _checked_name(item)


# ---------------------------------------------------------------- processing and writing (IO)


def encode_jpeg(rgb: np.ndarray) -> bytes:
    """Encode to JPEG bytes in memory: quality 95, 4:4:4 chroma so small text stays crisp."""
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=JPEG_QUALITY, subsampling=JPEG_SUBSAMPLING)
    return buf.getvalue()


def render_and_encode(kind: Kind, image_id: ImageId, data: bytes, colormap: str) -> list[Encoded | SkippedRow]:
    """Hash, render and JPEG-encode one input's bytes into its output rows: encoded images and skipped parts.

    Pure, and the unit of work sent to pool workers (so it and its arguments pickle). Every image rendered
    from the input (main and icon) carries the SHA-256 of `data`. A failure of the input itself is a single
    `SkippedRow`; an embedded icon that fails is a `SkippedRow` beside the main image.
    """
    try:
        source_sha256 = hashlib.sha256(data).hexdigest()
        rendered = render(kind, image_id, data, colormap)
        encoded = [
            r if isinstance(r, SkippedRow) else Encoded(r.image_id, encode_jpeg(r.rgb), source_sha256) for r in rendered
        ]
    except NotAnImage as exc:
        return [SkippedRow(image_id, "ignored", _clean_reason(str(exc)))]
    except Exception as exc:  # noqa: BLE001 - one bad input must not abort the run; it is recorded in skipped.tsv
        return [_failed(image_id, exc)]
    return encoded


def _process(candidate: Candidate, colormap: str) -> list[Encoded | SkippedRow]:
    """`render_and_encode` one input, in this process."""
    return render_and_encode(candidate.kind, candidate.image_id, candidate.data, colormap)


Outcome = tuple[ImageId, list[Encoded | SkippedRow]]  # (input image_id, its output rows)


def _outcomes_serial(items: Iterable[Candidate | SkippedRow], colormap: str) -> Iterator[Outcome]:
    for item in items:
        yield item.image_id, (_process(item, colormap) if isinstance(item, Candidate) else [item])


def _outcomes_pooled(
    items: Iterable[Candidate | SkippedRow], colormap: str, pool: Executor, limit: int
) -> Iterator[Outcome]:
    """`_outcomes_serial`, rendered by `pool`: yielded in input order, with at most `limit` inputs submitted and
    not yet yielded. Workers hash.

    A dead worker breaks the pool and every input in flight with it, so the run fails (`WorkerCrashed`) rather
    than retrying in this process, where the same input could take the whole run down without a message.
    """
    queue: deque[tuple[ImageId, Future[list[Encoded | SkippedRow]] | list[Encoded | SkippedRow]]] = deque()
    in_flight = 0

    def start(item: Candidate | SkippedRow) -> Future[list[Encoded | SkippedRow]] | list[Encoded | SkippedRow]:
        if isinstance(item, SkippedRow):
            return [item]
        with _sigint_blocked():  # a worker spawned here starts with SIGINT blocked, until it ignores it
            # the pool keeps the bytes until the result is back; `limit` bounds them
            return pool.submit(render_and_encode, item.kind, item.image_id, item.data, colormap)

    def finish() -> Outcome:
        nonlocal in_flight
        image_id, rows = queue[0]
        if isinstance(rows, Future):
            rows = _result(rows, pool)
            in_flight -= 1
        queue.popleft()
        return image_id, rows

    try:
        for item in items:
            queue.append((item.image_id, start(item)))
            in_flight += isinstance(queue[-1][1], Future)
            while queue and (in_flight >= limit or not isinstance(queue[0][1], Future)):
                yield finish()
        while queue:
            yield finish()
    except BrokenProcessPool as exc:
        first = queue[0][0] if queue else "unknown"
        raise WorkerCrashed(
            f"a preprocess worker died (out of memory, or a crash in a decoder); {in_flight} input(s) were in "
            f"flight, starting with {first}; no work directory was made. "
            "Re-run with --jobs 1 to find the input, or with more memory."
        ) from exc


def _workers(pool: Executor) -> list[multiprocessing.process.BaseProcess]:
    """The pool's worker processes; none for another executor or a pool already shut down.

    `_processes` is private (Python 3.14 adds `terminate_workers`), so it is read defensively: an AttributeError
    here must never replace the exception being handled.
    """
    processes = getattr(pool, "_processes", None)
    return list(processes.values()) if isinstance(processes, dict) else []


def _result(future: Future[list[Encoded | SkippedRow]], pool: Executor) -> list[Encoded | SkippedRow]:
    """`future.result()`, checking every `WORKER_CHECK_SECONDS` that no worker has exited.

    A worker killed (e.g. by the OOM killer) while sending a result leaves the pool's manager thread waiting
    for the rest of the message forever, so the future would never complete. Without `max_tasks_per_child`
    workers exit only at shutdown, so any exit before then is a death.
    """
    while True:
        try:
            return future.result(timeout=WORKER_CHECK_SECONDS)
        except TimeoutError:
            dead = [w for w in _workers(pool) if w.exitcode is not None]
            if dead:
                raise BrokenProcessPool(f"worker {dead[0].pid} exited with code {dead[0].exitcode}") from None


@contextmanager
def _sigint_blocked() -> Iterator[None]:
    """Block SIGINT in this thread; a process spawned meanwhile inherits the mask through exec."""
    if not hasattr(signal, "pthread_sigmask"):  # Windows
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _ignore_sigint() -> None:
    """Pool worker initializer: Ctrl-C reaches the whole process group, but only the parent should act on it.

    The worker started with SIGINT blocked (`_sigint_blocked`); a Ctrl-C pending since then is discarded.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})


@contextmanager
def _single_threaded_blas() -> Iterator[None]:
    """Set each of `_BLAS_THREAD_VARS` the user has not set to 1 while workers are spawned (they inherit the
    environment); restore the environment afterwards."""
    added = [name for name in _BLAS_THREAD_VARS if name not in os.environ]
    try:
        os.environ.update(dict.fromkeys(added, "1"))
        yield
    finally:
        for name in added:
            os.environ.pop(name, None)


def _abandon(pool: Executor) -> None:
    """Stop the pool's workers now, so `shutdown` cannot hang.

    A worker killed mid-send leaves the manager thread reading a cut-off result; it sees EOF only once every
    write end of the result pipe is closed. This process's end is closed first (EOF still waits for the
    workers' ends to close as they die), then the workers are killed and joined. Each step is attempted even
    if an earlier one fails, or a second Ctrl-C interrupts it.
    """
    with suppress(BaseException):
        writer = getattr(getattr(pool, "_result_queue", None), "_writer", None)
        if writer is not None:
            writer.close()
    workers: list[multiprocessing.process.BaseProcess] = []
    with suppress(BaseException):
        workers = _workers(pool)
    for worker in workers:
        with suppress(BaseException):
            worker.kill()
    for worker in workers:
        with suppress(BaseException):
            worker.join()


@contextmanager
def _worker_pool(jobs: int) -> Iterator[Executor]:
    """A spawn-context process pool whose workers run single-threaded BLAS and ignore SIGINT.

    On any exception (Ctrl-C, SIGTERM through `interrupt_on`, a dead worker, a write error) the workers are
    killed, queued inputs are cancelled and the pool is joined, so nothing waits on a render and no worker
    outlives the run.
    """
    with _single_threaded_blas():
        pool = ProcessPoolExecutor(max_workers=jobs, mp_context=_SPAWN, initializer=_ignore_sigint)
        try:
            yield pool
        except BaseException:
            _abandon(pool)  # never raises: it must not replace the exception being raised
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)


def _write_new(path: Path, perm: int, data: bytes) -> None:
    """Create `path` exclusively with `perm` as its creation mode, and write `data` to it.

    A POSIX default ACL on the parent makes Linux ignore the umask, so the mode
    is given explicitly (an ACL's other entry can only narrow it).
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, perm)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _write_tsv(path: Path, header: list[str], rows: Sequence[tuple[str, ...]], perm: int) -> None:
    _write_new(path, perm, format_tsv([header, *rows]).encode("utf-8"))


def _library_versions() -> dict[str, str]:
    """Versions of the libraries that shape the output; the optional codecs only when importable."""
    versions = {name: importlib.metadata.version(name) for name in _LIBRARIES}
    for module, distribution in _CODEC_DISTRIBUTIONS.items():
        if importlib.util.find_spec(module) is None:
            continue
        try:
            versions[module] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[module] = "unknown"
    return versions


def provenance(
    *,
    sources: list[Path],
    batch_size: int,
    colormap: str,
    access: Access,
    jobs: int,
    result: PreprocessResult,
    created: datetime,
    tool_version: str,
    libraries: dict[str, str],
) -> dict[str, Any]:
    """The content of preprocess.json: how this work dir was made. Sources are absolute, resolved paths, escaped like
    a bad image_id (`_escape_name`)."""
    failed = sum(s.kind == "failed" for s in result.skipped)
    return {
        "tool_version": tool_version,
        "created": created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [_escape_name(s.resolve().as_posix()) for s in sources],
        "parameters": {
            "batch_size": batch_size,
            "colormap": colormap,
            "clahe_kernel_size": CLAHE_KERNEL_SIZE,
            "outlier_percentile": OUTLIER_PERCENTILE,
            "intensity_margin": INTENSITY_MARGIN,
            "tail_fraction": TAIL_FRACTION,
            "jpeg_quality": JPEG_QUALITY,
            "jpeg_subsampling": JPEG_SUBSAMPLING,
            "access": access,
            "jobs": jobs,  # how the run was made, not what it made: the output is the same for any value
        },
        "libraries": libraries,
        "counts": {
            "inputs": result.found,
            "written": result.written,
            "skipped_failed": failed,
            "skipped_ignored": len(result.skipped) - failed,
        },
    }


def staging_path(output_dir: Path) -> Path:
    return output_dir.parent / f".{output_dir.name}.partial"


def _claim_staging(output_dir: Path, dir_mode: int) -> Path:
    """Refuse an in-use work dir, then create the staging dir next to it with the policy's mode.

    The work dir keeps the staging dir's mode through the final rename.
    """
    if output_dir.is_symlink() or (output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir()))):
        raise WorkDirExists(
            f"work directory {output_dir} already exists; choose a new --work-dir or remove the old one"
        )
    staging = staging_path(output_dir)
    try:
        staging.mkdir(mode=dir_mode)
    except FileExistsError:
        raise WorkDirExists(f"a previous preprocess into {output_dir} did not finish; remove {staging}") from None
    try:
        os.chmod(staging, dir_mode)  # mkdir's mode is masked by the umask and drops setgid
    except BaseException:
        staging.rmdir()  # a leftover would make every later run refuse
        raise
    return staging


def run_preprocess(
    sources: list[Path],
    output_dir: Path,
    batch_size: int = 300,
    colormap: str = "inferno",
    access: Access = "private",
    jobs: int = 1,
) -> PreprocessResult:
    """Render every discovered input into a staging dir, then rename it to `output_dir`.

    Every directory and file follows the `access` policy (see `access.modes`),
    enforced by a umask held for the whole run and restored afterwards.

    Every input ends up in exactly one of manifest.tsv or skipped.tsv; preprocess.json
    records how the work dir was made (see `provenance`). Write
    errors (OSError on the work directory) propagate and abort the run. The
    work dir appears only on success; an existing non-empty one is refused
    (`WorkDirExists`), so verdicts can never attach to a replaced image.

    `jobs` > 1 renders in that many worker processes; the output is identical
    to `jobs` = 1, which renders in this process. A dead worker fails the run
    (`WorkerCrashed`).
    """
    policy = modes(access)
    output_dir.parent.mkdir(parents=True, exist_ok=True)  # before the umask: parents get normal permissions
    previous_umask = os.umask(policy.umask)
    try:
        staging = _claim_staging(output_dir, policy.dir_mode)
        exclude = frozenset({output_dir.resolve(), staging.resolve()})  # never ingest our own output
        try:
            found, manifest_rows, skipped = _render_into(
                sources=sources,
                exclude=exclude,
                staging=staging,
                batch_size=batch_size,
                colormap=colormap,
                policy=policy,
                jobs=jobs,
            )
            _write_tsv(staging / MANIFEST_NAME, MANIFEST_HEADER, manifest_rows, policy.file_mode)
            _write_tsv(
                staging / SKIPPED_NAME,
                SKIPPED_HEADER,
                [(s.image_id, s.kind, s.reason) for s in skipped],
                policy.file_mode,
            )
            written = len(manifest_rows)
            result = PreprocessResult(found, written, -(-written // batch_size), skipped, output_dir / SKIPPED_NAME)
            record = provenance(
                sources=sources,
                batch_size=batch_size,
                colormap=colormap,
                access=access,
                jobs=jobs,
                result=result,
                created=datetime.now(UTC),
                tool_version=package_version(),
                libraries=_library_versions(),
            )
            _write_new(
                staging / PROVENANCE_NAME, policy.file_mode, (json.dumps(record, indent=2) + "\n").encode("utf-8")
            )
            if output_dir.exists():
                output_dir.rmdir()  # an empty directory the user made
            os.rename(staging, output_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    finally:
        os.umask(previous_umask)
    return result


def colliding_ids(images: Iterable[tuple[ImageId, str, str]]) -> frozenset[ImageId]:
    """The image_ids, among `(image_id, source_sha256, jpeg_sha256)` triples, naming more than one distinct
    `(source_sha256, jpeg_sha256)` pair.

    Verdicts are stored per image_id, so such an id would let a verdict on one image
    cover another nobody saw; and export attests a single source_sha256 per id, so
    two different sources that render the same JPG collide too. The same source
    reached twice (through overlapping SOURCEs) has the same pair: not a collision.
    """
    pairs: dict[ImageId, set[tuple[str, str]]] = {}
    for image_id, source_sha256, jpeg_sha256 in images:
        pairs.setdefault(image_id, set()).add((source_sha256, jpeg_sha256))
    return frozenset(image_id for image_id, found in pairs.items() if len(found) > 1)


def _stage(
    *,
    sources: list[Path],
    exclude: frozenset[Path],
    pending: Path,
    colormap: str,
    policy: Modes,
    jobs: int,
) -> list[tuple[ImageId, list[Staged | SkippedRow]]]:
    """Discover and render every input, writing its JPGs into `pending`.

    Returns `(input image_id, its output rows)` in discovery order.
    Inputs are rendered by `jobs` worker processes, or in this one for 1; either way this process writes
    every file, in discovery order.
    """
    inputs: list[tuple[ImageId, list[Staged | SkippedRow]]] = []
    staged = 0

    items = discover(sources, exclude)
    with _worker_pool(jobs) if jobs > 1 else nullcontext() as pool:
        outcomes = (
            _outcomes_serial(items, colormap)
            if pool is None
            else _outcomes_pooled(items, colormap, pool, IN_FLIGHT_PER_JOB * jobs)
        )
        for input_id, outcome in outcomes:
            parts: list[Staged | SkippedRow] = []
            for part in outcome:
                if isinstance(part, SkippedRow):
                    if part.kind == "failed":
                        log.warning("skipping %s: %s", part.image_id, part.reason)
                    parts.append(part)
                    continue
                staged += 1
                path = pending / f"{staged:08d}.jpg"
                _write_new(path, policy.file_mode, part.jpeg)
                parts.append(Staged(part.image_id, path, part.source_sha256, hashlib.sha256(part.jpeg).hexdigest()))
            inputs.append((input_id, parts))
    return inputs


def _place(
    inputs: list[tuple[ImageId, list[Staged | SkippedRow]]],
    *,
    staging: Path,
    batch_size: int,
    policy: Modes,
) -> tuple[list[tuple[str, str, str, str, str]], list[SkippedRow]]:
    """Move the JPGs of inputs without a colliding image_id into batches under `staging`.

    An input with any colliding image (main or icon) becomes one `failed` row and its JPGs are deleted.
    Returns the manifest rows and the skipped rows, both in discovery order.
    """
    collisions = colliding_ids(
        (row.image_id, row.source_sha256, row.jpeg_sha256)
        for _, rows in inputs
        for row in rows
        if isinstance(row, Staged)
    )
    manifest_rows: list[tuple[str, str, str, str, str]] = []
    skipped: list[SkippedRow] = []
    for input_id, rows in inputs:
        if any(isinstance(row, Staged) and row.image_id in collisions for row in rows):
            for row in rows:
                if isinstance(row, Staged):
                    row.path.unlink()
            log.warning("skipping %s: %s", input_id, COLLISION_REASON)
            skipped.append(SkippedRow(input_id, "failed", COLLISION_REASON))
            continue
        for row in rows:
            if isinstance(row, SkippedRow):
                skipped.append(row)
                continue
            batch_index, slot = divmod(len(manifest_rows), batch_size)
            batch_id = f"batch_{batch_index + 1:03d}"
            batch_dir = staging / batch_id
            if slot == 0:
                batch_dir.mkdir()
                os.chmod(batch_dir, policy.dir_mode)  # explicit: mkdir's mode is masked and drops setgid
            img_path = batch_dir / f"img_{slot + 1:05d}.jpg"
            os.rename(row.path, img_path)
            key = img_path.relative_to(staging).as_posix()
            manifest_rows.append((batch_id, key, row.image_id, row.source_sha256, row.jpeg_sha256))
    return manifest_rows, skipped


def _render_into(
    *,
    sources: list[Path],
    exclude: frozenset[Path],
    staging: Path,
    batch_size: int,
    colormap: str,
    policy: Modes,
    jobs: int,
) -> tuple[int, list[tuple[str, str, str, str, str]], list[SkippedRow]]:
    """Render every input into `PENDING_NAME`, then move the JPGs of inputs without a colliding image_id into batches."""
    pending = staging / PENDING_NAME
    pending.mkdir()
    os.chmod(pending, policy.dir_mode)  # explicit: mkdir's mode is masked and drops setgid
    inputs = _stage(sources=sources, exclude=exclude, pending=pending, colormap=colormap, policy=policy, jobs=jobs)
    manifest_rows, skipped = _place(inputs, staging=staging, batch_size=batch_size, policy=policy)
    pending.rmdir()  # every staged JPG was moved or deleted
    return len(inputs), manifest_rows, skipped
