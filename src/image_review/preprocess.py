import csv
import io
import os
import re
import shutil
import stat
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Literal
from zipfile import ZipFile

import imageio.v3 as iio
import matplotlib.pyplot as plt
import numpy as np
import pydicom
import skimage as ski
from PIL import Image
from pydicom.errors import InvalidDicomError
from pydicom.uid import (
    UID,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    MediaStorageDirectoryStorage,
)
from tqdm import tqdm

from .access import Access, Modes, modes

EROSION_KERNEL_SIZE = 5
OUTLIER_PERCENTILE = 0.01
INTENSITY_MARGIN = 0.02
TAIL_FRACTION = 0.10  # output range given to each outlier tail (below bot, above top)
CLAHE_BINS = 96
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
)
_ARCHIVE_SUFFIXES = {
    ".gz": "gzip", ".tgz": "gzip", ".tar": "tar", ".7z": "7z", ".rar": "rar", ".bz2": "bzip2", ".xz": "xz", ".zst": "zstd",
}  # fmt: skip
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}
_ALPHA_MODES = {"RGBA", "RGBa", "LA", "La", "PA"}
_HIGH_BIT_GRAY_MODES = {"I", "F", "I;16", "I;16L", "I;16B", "I;16N"}
_PIXEL_DATA_KEYWORDS = ("PixelData", "FloatPixelData", "DoubleFloatPixelData")

Kind = Literal["dicom", "raster"]
Content = Literal["dicom", "raster", "zip"]
SkipKind = Literal["failed", "ignored"]


@dataclass(frozen=True)
class Rejected:
    """Content that is not rendered: `ignored` (not an input) or `failed` (e.g. a `.zip` name with other content)."""

    kind: SkipKind
    reason: str


@dataclass(frozen=True)
class Candidate:
    """One discovered input; `read` returns its raw bytes (no decoding happens in discovery).

    For ZIP entries `read` reads from the open archive, so it is only valid until
    the discovery generator advances past the source; call it before the next item.
    """

    image_id: str
    kind: Kind
    read: Callable[[], bytes]


@dataclass(frozen=True)
class Skipped:
    """An input (or a whole source) that produced no image, and why."""

    image_id: str
    kind: SkipKind
    reason: str


@dataclass(frozen=True)
class Rendered:
    """One output image: (H, W, 3) uint8 RGB."""

    image_id: str
    rgb: np.ndarray


@dataclass(frozen=True)
class PreprocessResult:
    found: int
    written: int
    batches: int
    skipped: list[Skipped]
    skipped_path: Path


class WorkDirExists(ValueError):
    """The work directory cannot be created: it is in use, or an earlier run left its staging dir."""


class NotAnImage(Exception):
    """A DICOM object that is not an image by design (a DICOMDIR index); recorded as ignored."""


class Unsupported(Exception):
    """A decodable input this tool does not render; the message starts with `unsupported:`."""

    def __init__(self, what: str) -> None:
        super().__init__(f"unsupported: {what}")


# ---------------------------------------------------------------- rendering (pure)


def compress_image(image):
    same_vert = image == np.roll(image, 1, axis=0)
    same_horiz = image == np.roll(image, 1, axis=1)
    both = same_vert & same_horiz
    if both.ndim == 3:
        both = np.all(both, axis=2)
    uniform = ski.morphology.erosion(
        both,
        np.ones((EROSION_KERNEL_SIZE, EROSION_KERNEL_SIZE), dtype=bool),
    )
    image = np.delete(image, np.all(uniform, axis=1), axis=0)
    image = np.delete(image, np.all(uniform, axis=0), axis=1)
    return image


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


def preprocess_dicom(dcm: pydicom.FileDataset) -> np.ndarray:
    if not _has_pixel_data(dcm):
        sop_class = _sop_class(dcm)
        if sop_class == MediaStorageDirectoryStorage:
            raise NotAnImage("DICOMDIR index")
        raise Unsupported(f"no pixel data ({sop_class.name})" if sop_class else "no pixel data")
    photometric = dcm.get("PhotometricInterpretation")
    if photometric not in ("MONOCHROME1", "MONOCHROME2"):
        raise Unsupported(f"photometric interpretation {photometric}")
    pixels = dcm.pixel_array
    if pixels.ndim == 3:
        raise Unsupported(f"multi-frame DICOM ({pixels.shape[0]} frames)")
    if pixels.ndim != 2:
        raise Unsupported(f"pixel array with shape {pixels.shape}")
    img = pixels.astype(np.float32)
    if photometric == "MONOCHROME1":
        img = -img
    if img.min() >= img.max():
        return _crop(np.zeros_like(img))
    img = _compress_tails(img)
    img = _crop(img)
    img = ski.exposure.equalize_adapthist(img, CLAHE_BINS)
    return _crop(img)


def apply_colormap(img: np.ndarray, colormap: str = "inferno") -> np.ndarray:
    cm = plt.get_cmap(colormap)
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
        img = ski.exposure.equalize_adapthist(img, CLAHE_BINS)
        img = ski.util.img_as_ubyte(img)
        return np.stack([img] * 3, axis=2)
    if img.ndim == 3 and img.shape[2] == 3:
        return ski.util.img_as_ubyte(img)
    raise Unsupported(f"image with shape {img.shape}")


def read_dicom(data: bytes) -> pydicom.FileDataset:
    """Read DICOM with or without the preamble, `DICM` and file meta.

    `force` lets pydicom read a bare dataset; a file with the preamble reads the
    same either way. `force` parses anything, so a bare dataset is
    `InvalidDicomError` unless it has a SOP Class UID or pixel data (an
    ACR-NEMA file may have only the latter). A bare dataset is uncompressed by
    definition, so its transfer syntax is the encoding pydicom detected.
    """
    dcm = pydicom.dcmread(io.BytesIO(data), force=True)
    has_preamble = data[_DICOM_MAGIC_OFFSET : _DICOM_MAGIC_OFFSET + len(_DICOM_MAGIC)] == _DICOM_MAGIC
    if not has_preamble and _sop_class(dcm) is None and not _has_pixel_data(dcm):
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


def render(kind: Kind, image_id: str, data: bytes, colormap: str) -> list[Rendered]:
    match kind:
        case "dicom":
            dcm = read_dicom(data)
            rgb = apply_colormap(preprocess_dicom(dcm), colormap)
        case "raster":
            rgb = preprocess_raster(decode_raster(data))
    return [Rendered(image_id, rgb)]


# ---------------------------------------------------------------- discovery (IO)


def _is_raster(head: bytes) -> bool:
    return (
        head.startswith(_RASTER_MAGIC)
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
        or (head.startswith(b"BM") and len(head) >= 18 and int.from_bytes(head[14:18], "little") in _BMP_DIB_HEADER_SIZES)
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


def classify(name: str, head: bytes) -> Content | Rejected:
    """Classify an input by its first `SNIFF_BYTES` bytes; `name` is its path (or ZIP entry name).

    The name only matters when the content is not recognized: macOS metadata is
    ignored, a DICOM or image suffix leaves the decision to the decoder (so a
    damaged file fails rather than being dropped), and a `.zip` or other archive
    name fails. Compressed archives (gzip, bzip2, xz, zstd, 7z, rar, tar) fail as unsupported.
    """
    if head[_DICOM_MAGIC_OFFSET : _DICOM_MAGIC_OFFSET + len(_DICOM_MAGIC)] == _DICOM_MAGIC:
        return "dicom"
    if head.startswith(_ZIP_MAGIC):
        return "zip"
    for magic, archive in _ARCHIVE_MAGIC:
        if head.startswith(magic):
            return Rejected("failed", f"unsupported: {archive} archive")
    if head.startswith(b"BZh") and head[3:4].isdigit():
        return Rejected("failed", "unsupported: bzip2 archive")
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
        return Rejected("failed", f"unsupported: {_ARCHIVE_SUFFIXES[suffix]} archive")
    if suffix == ".zip":
        return Rejected("failed", "unrecognized content for a .zip file")
    return Rejected("ignored", "not an image (unrecognized content)")


def _clean_reason(reason: str) -> str:
    """One line, and reproducible: control characters become spaces, object reprs become `<data>`."""
    reason = re.sub(r"<[^<>]* object at 0x[0-9a-fA-F]+>", "<data>", reason)
    return re.sub(r"[\x00-\x1f\x7f]+", " ", reason).strip()


def _failed(image_id: str, exc: Exception) -> Skipped:
    reason = str(exc) if isinstance(exc, Unsupported) else f"{type(exc).__name__}: {exc}"
    return Skipped(image_id, "failed", _clean_reason(reason))


def _discover_zip(path: Path) -> Iterator[Candidate | Skipped]:
    try:
        zf = ZipFile(path)
    except Exception as exc:  # noqa: BLE001 - any failure to open the archive is recorded as a skip
        yield _failed(path.as_posix(), exc)
        return
    with zf:
        # Repeated names are distinct entries: read each by its ZipInfo and give
        # later ones `#2`, `#3`, ... (no entry name can end in `#N`, so no clash).
        files = [info for info in zf.infolist() if not info.is_dir()]
        if not files:
            yield Skipped(path.as_posix(), "ignored", "zip contains no files")
        seen: Counter[str] = Counter()
        for info in files:
            seen[info.filename] += 1
            nth = seen[info.filename]
            image_id = f"{path.as_posix()}::{info.filename}" + (f"#{nth}" if nth > 1 else "")
            try:
                with zf.open(info) as f:
                    head = f.read(SNIFF_BYTES)
            except Exception as exc:  # noqa: BLE001 - an unreadable entry (encrypted, bad compression) is recorded as failed
                yield _failed(image_id, exc)
                continue
            match classify(info.filename, head):
                case "zip":
                    yield Skipped(image_id, "failed", "unsupported: nested zip")
                case Rejected(kind, reason):
                    yield Skipped(image_id, kind, reason)
                case "dicom" | "raster" as kind:
                    yield Candidate(image_id, kind, partial(zf.read, info))


def _discover_file(path: Path, named: bool = False) -> Iterator[Candidate | Skipped]:
    """Classify one file by content (reading only its first bytes); a ZIP yields its entries.

    A file `named` as a source was asked for explicitly, so a file that would be
    ignored fails instead (macOS metadata stays ignored).
    """
    image_id = path.as_posix()
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
            yield Skipped(image_id, "failed", reason)
        case Rejected(kind, reason):
            yield Skipped(image_id, kind, reason)
        case "dicom" | "raster" as kind:
            yield Candidate(image_id, kind, path.read_bytes)


def _symlinked_directory(link: Path, target: Path, source_dirs: tuple[Path, ...]) -> Skipped:
    """The row for a symlinked directory, which is never entered (it could lead out of the source).

    `target` is the resolved link. An enclosing directory is the link's own
    directory or any ancestor of it (so the source root and its ancestors).
    """
    link_dir = link.parent.resolve()
    if target == link_dir or target in link_dir.parents:
        return Skipped(link.as_posix(), "ignored", "symlink to an enclosing directory")
    for source in source_dirs:
        if target == source or source in target.parents:
            return Skipped(link.as_posix(), "ignored", _clean_reason(f"symlinked directory already included via SOURCE {source}"))
    return Skipped(
        link.as_posix(),
        "failed",
        _clean_reason(f"symlinked directory not followed; pass its target {target} as a SOURCE"),
    )


def _discover_directory(root: Path, exclude: frozenset[Path], source_dirs: tuple[Path, ...]) -> Iterator[Candidate | Skipped]:
    """Walk `root` in sorted order without entering symlinked directories (see `_symlinked_directory`) or `exclude`.

    An unreadable directory (including `root`) becomes a failed row. Symlinked
    files are read; a dangling symlink is a failed row.
    """
    errors: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=errors.append):
        yield from (_failed(Path(e.filename).as_posix(), e) for e in errors)
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
    yield from (_failed(Path(e.filename).as_posix(), e) for e in errors)


def discover(sources: list[Path], exclude: frozenset[Path] = frozenset()) -> Iterator[Candidate | Skipped]:
    """Yield every input under `sources`, classified by content; nothing under `exclude` (resolved paths)."""
    source_dirs = tuple(s.resolve() for s in sources if s.is_dir())
    for source in tqdm(sources, desc="Sources", position=0):
        items = _discover_directory(source, exclude, source_dirs) if source.is_dir() else _discover_file(source, named=True)
        yield from tqdm(items, desc=source.name, position=1, leave=False, unit="input")


# ---------------------------------------------------------------- processing and writing (IO)


def encode_jpeg(rgb: np.ndarray) -> bytes:
    """Encode in memory with the same encoder and settings as `ski.io.imsave(path.jpg)`."""
    return iio.imwrite("<bytes>", rgb, extension=".jpg")


def _process(candidate: Candidate, colormap: str) -> list[tuple[str, bytes]] | Skipped:
    """Read, render and JPEG-encode one input; any failure here is that input's failure."""
    try:
        rendered = render(candidate.kind, candidate.image_id, candidate.read(), colormap)
        encoded = [(r.image_id, encode_jpeg(r.rgb)) for r in rendered]
    except NotAnImage as exc:
        return Skipped(candidate.image_id, "ignored", _clean_reason(str(exc)))
    except Exception as exc:  # noqa: BLE001 - one bad input must not abort the run; it is recorded in skipped.tsv
        return _failed(candidate.image_id, exc)
    if not encoded:
        return Skipped(candidate.image_id, "failed", "rendered no images")
    return encoded


def _open_new(path: Path, perm: int, **kwargs):
    """Create `path` exclusively with `perm` as its creation mode.

    A POSIX default ACL on the parent makes Linux ignore the umask, so the mode
    is given explicitly (an ACL's other entry can only narrow it).
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, perm)
    return os.fdopen(fd, **kwargs)


def _write_tsv(path: Path, header: list[str], rows: list[tuple[str, ...]], perm: int) -> None:
    with _open_new(path, perm, mode="w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(header)
        writer.writerows(rows)


def staging_path(output_dir: Path) -> Path:
    return output_dir.parent / f".{output_dir.name}.partial"


def _claim_staging(output_dir: Path, dir_mode: int) -> Path:
    """Refuse an in-use work dir, then create the staging dir next to it with the policy's mode.

    The work dir keeps the staging dir's mode through the final rename.
    """
    if output_dir.is_symlink() or (output_dir.exists() and not (output_dir.is_dir() and not any(output_dir.iterdir()))):
        raise WorkDirExists(
            f"work directory {output_dir} already exists; choose a new --work-dir or remove the old one"
        )
    staging = staging_path(output_dir)
    try:
        staging.mkdir(mode=dir_mode)
    except FileExistsError:
        raise WorkDirExists(
            f"a previous preprocess into {output_dir} did not finish; remove {staging}"
        ) from None
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
) -> PreprocessResult:
    """Render every discovered input into a staging dir, then rename it to `output_dir`.

    Every directory and file follows the `access` policy (see `access.modes`),
    enforced by a umask held for the whole run and restored afterwards.

    Every input ends up in exactly one of manifest.tsv or skipped.tsv. Write
    errors (OSError on the work directory) propagate and abort the run. The
    work dir appears only on success; an existing non-empty one is refused
    (`WorkDirExists`), so verdicts can never attach to a replaced image.
    """
    policy = modes(access)
    output_dir.parent.mkdir(parents=True, exist_ok=True)  # before the umask: parents get normal permissions
    previous_umask = os.umask(policy.umask)
    try:
        staging = _claim_staging(output_dir, policy.dir_mode)
        exclude = frozenset({output_dir.resolve(), staging.resolve()})  # never ingest our own output
        try:
            result = _render_into(sources, exclude, staging, batch_size, colormap, policy)
            if output_dir.exists():
                output_dir.rmdir()  # an empty directory the user made
            os.rename(staging, output_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    finally:
        os.umask(previous_umask)
    return replace(result, skipped_path=output_dir / result.skipped_path.name)


def _render_into(
    sources: list[Path], exclude: frozenset[Path], output_dir: Path, batch_size: int, colormap: str, policy: Modes
) -> PreprocessResult:
    manifest_rows: list[tuple[str, str, str]] = []
    skipped: list[Skipped] = []
    found = 0

    for item in discover(sources, exclude):
        found += 1
        outcome = _process(item, colormap) if isinstance(item, Candidate) else item
        if isinstance(outcome, Skipped):
            if outcome.kind == "failed":
                tqdm.write(f"WARNING: skipping {outcome.image_id}: {outcome.reason}", file=sys.stderr)
            skipped.append(outcome)
            continue
        for image_id, jpeg in outcome:
            n = len(manifest_rows)
            batch_id = f"batch_{n // batch_size + 1:03d}"
            batch_dir = output_dir / batch_id
            if n % batch_size == 0:
                batch_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(batch_dir, policy.dir_mode)  # explicit: mkdir's mode is masked and drops setgid
            img_path = batch_dir / f"img_{n % batch_size + 1:05d}.jpg"
            with _open_new(img_path, policy.file_mode, mode="wb") as f:
                f.write(jpeg)
            manifest_rows.append((batch_id, img_path.relative_to(output_dir).as_posix(), image_id))

    _write_tsv(output_dir / "manifest.tsv", ["batch", "preprocessed_path", "image_id"], manifest_rows, policy.file_mode)
    skipped_path = output_dir / "skipped.tsv"
    _write_tsv(skipped_path, ["image_id", "kind", "reason"], [(s.image_id, s.kind, s.reason) for s in skipped], policy.file_mode)

    written = len(manifest_rows)
    batches = -(-written // batch_size)
    return PreprocessResult(found, written, batches, skipped, skipped_path)
