import csv
import io
import os
import re
import shutil
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

import imageio.v3 as iio
import matplotlib.pyplot as plt
import numpy as np
import pydicom
import skimage as ski
from PIL import Image
from tqdm import tqdm

EROSION_KERNEL_SIZE = 5
OUTLIER_PERCENTILE = 0.01
INTENSITY_MARGIN = 0.02
CLAHE_BINS = 96
ALPHA_BACKGROUND = 0.5  # mid-gray, so content carried only by alpha stays visible whatever its colour
SIDE_BY_SIDE_GAP = 4  # pixels between views placed side by side in one rendered image

_DICOM_EXTENSIONS = {".dcm"}
_RASTER_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_IMAGE_EXTENSIONS = _DICOM_EXTENSIONS | _RASTER_EXTENSIONS
_ALPHA_MODES = {"RGBA", "RGBa", "LA", "La", "PA"}
_HIGH_BIT_GRAY_MODES = {"I", "F", "I;16", "I;16L", "I;16B", "I;16N"}
_PIXEL_DATA_KEYWORDS = ("PixelData", "FloatPixelData", "DoubleFloatPixelData")

Kind = Literal["dicom", "raster"]
SkipKind = Literal["failed"]


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


def preprocess_dicom(dcm: pydicom.FileDataset) -> np.ndarray:
    if not any(keyword in dcm for keyword in _PIXEL_DATA_KEYWORDS):
        raise Unsupported("no pixel data")
    photometric = dcm.get("PhotometricInterpretation")
    if photometric not in ("MONOCHROME1", "MONOCHROME2"):
        raise Unsupported(f"photometric interpretation {photometric}")
    pixels = dcm.pixel_array
    if pixels.ndim == 3:
        raise Unsupported(f"multi-frame DICOM ({pixels.shape[0]} frames)")
    if pixels.ndim != 2:
        raise Unsupported(f"pixel array with shape {pixels.shape}")
    img = ski.util.img_as_float32(pixels)
    if photometric == "MONOCHROME1":
        img = ski.util.invert(img)
    bot, top = img.min(), img.max()
    margin_initial = OUTLIER_PERCENTILE * (top - bot)
    bot, top = bot + margin_initial, top - margin_initial
    filtered = img[(img > bot) & (img < top)]
    if filtered.size == 0:
        return _crop(img)
    bot, top = np.quantile(filtered, [OUTLIER_PERCENTILE, 1 - OUTLIER_PERCENTILE])
    margin_final = INTENSITY_MARGIN * (top - bot)
    bot, top = bot + margin_final, top - margin_final
    if bot >= top:
        return _crop(img)
    img = ski.exposure.rescale_intensity(img, (bot, top))
    img = np.clip(img, 0, 1)
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


def render(kind: Kind, image_id: str, data: bytes, colormap: str) -> list[Rendered]:
    match kind:
        case "dicom":
            dcm = pydicom.dcmread(io.BytesIO(data))
            rgb = apply_colormap(preprocess_dicom(dcm), colormap)
        case "raster":
            rgb = preprocess_raster(decode_raster(data))
    return [Rendered(image_id, rgb)]


# ---------------------------------------------------------------- discovery (IO)


def _kind_for(name: str) -> Kind:
    return "dicom" if Path(name).suffix.lower() in _DICOM_EXTENSIONS else "raster"


def _clean_reason(reason: str) -> str:
    return re.sub(r"[\t\r\n]+", " ", reason).strip()


def _failed(image_id: str, exc: Exception) -> Skipped:
    reason = str(exc) if isinstance(exc, Unsupported) else f"{type(exc).__name__}: {exc}"
    return Skipped(image_id, "failed", _clean_reason(reason))


def discover_zip(path: Path) -> Iterator[Candidate | Skipped]:
    try:
        zf = ZipFile(path)
    except Exception as exc:  # noqa: BLE001 - any failure to open the archive is recorded as a skip
        yield _failed(path.as_posix(), exc)
        return
    with zf:
        entries = [
            info
            for info in zf.infolist()
            if not info.is_dir() and Path(info.filename).suffix.lower() in _IMAGE_EXTENSIONS
        ]
        # Repeated names are distinct entries: read each by its ZipInfo and give
        # later ones `#2`, `#3`, ... (no image name can end in `#N`, so no clash).
        seen: Counter[str] = Counter()
        for info in tqdm(entries, desc=path.name, position=1, leave=False):
            seen[info.filename] += 1
            nth = seen[info.filename]
            image_id = f"{path.as_posix()}::{info.filename}" + (f"#{nth}" if nth > 1 else "")
            yield Candidate(image_id, _kind_for(info.filename), partial(zf.read, info))


def discover_directory(path: Path) -> Iterator[Candidate | Skipped]:
    try:
        os.listdir(path)  # surface an unreadable directory; glob silently yields nothing
        files: list[tuple[Path, Kind]] = [(f, "dicom") for f in sorted(path.glob("**/*.dcm"))]
        for ext in sorted(_RASTER_EXTENSIONS):
            files.extend((f, "raster") for f in sorted(path.glob(f"**/*{ext}")))
    except OSError as exc:
        yield _failed(path.as_posix(), exc)
        return
    for f, kind in tqdm(files, desc=path.name, position=1, leave=False):
        yield Candidate(f.as_posix(), kind, f.read_bytes)


def discover(sources: list[Path]) -> Iterator[Candidate | Skipped]:
    for source in tqdm(sources, desc="Sources", position=0):
        if source.suffix == ".zip":
            yield from discover_zip(source)
        elif source.is_dir():
            yield from discover_directory(source)
        else:
            yield Candidate(source.as_posix(), _kind_for(source.name), source.read_bytes)


# ---------------------------------------------------------------- processing and writing (IO)


def encode_jpeg(rgb: np.ndarray) -> bytes:
    """Encode in memory with the same encoder and settings as `ski.io.imsave(path.jpg)`."""
    return iio.imwrite("<bytes>", rgb, extension=".jpg")


def _process(candidate: Candidate, colormap: str) -> list[tuple[str, bytes]] | Skipped:
    """Read, render and JPEG-encode one input; any failure here is that input's failure."""
    try:
        rendered = render(candidate.kind, candidate.image_id, candidate.read(), colormap)
        encoded = [(r.image_id, encode_jpeg(r.rgb)) for r in rendered]
    except Exception as exc:  # noqa: BLE001 - one bad input must not abort the run; it is recorded in skipped.tsv
        return _failed(candidate.image_id, exc)
    if not encoded:
        return Skipped(candidate.image_id, "failed", "rendered no images")
    return encoded


def _write_tsv(path: Path, header: list[str], rows: list[tuple[str, ...]]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(header)
        writer.writerows(rows)


STAGING_MODE = 0o700  # the work dir keeps the staging dir's mode through the final rename


def staging_path(output_dir: Path) -> Path:
    return output_dir.parent / f".{output_dir.name}.partial"


def _claim_staging(output_dir: Path) -> Path:
    """Refuse an in-use work dir, then create the private staging dir next to it."""
    if output_dir.is_symlink() or (output_dir.exists() and not (output_dir.is_dir() and not any(output_dir.iterdir()))):
        raise WorkDirExists(
            f"work directory {output_dir} already exists; choose a new --work-dir or remove the old one"
        )
    staging = staging_path(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        staging.mkdir(mode=STAGING_MODE)
    except FileExistsError:
        raise WorkDirExists(
            f"a previous preprocess into {output_dir} did not finish; remove {staging}"
        ) from None
    return staging


def run_preprocess(
    sources: list[Path],
    output_dir: Path,
    batch_size: int = 300,
    colormap: str = "inferno",
) -> PreprocessResult:
    """Render every discovered input into a private staging dir, then rename it to `output_dir`.

    Every input ends up in exactly one of manifest.tsv or skipped.tsv. Write
    errors (OSError on the work directory) propagate and abort the run. The
    work dir appears only on success; an existing non-empty one is refused
    (`WorkDirExists`), so verdicts can never attach to a replaced image.
    """
    staging = _claim_staging(output_dir)
    try:
        result = _render_into(sources, staging, batch_size, colormap)
        if output_dir.exists():
            output_dir.rmdir()  # an empty directory the user made
        os.rename(staging, output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return replace(result, skipped_path=output_dir / result.skipped_path.name)


def _render_into(sources: list[Path], output_dir: Path, batch_size: int, colormap: str) -> PreprocessResult:
    manifest_rows: list[tuple[str, str, str]] = []
    skipped: list[Skipped] = []
    found = 0

    for item in discover(sources):
        found += 1
        outcome = _process(item, colormap) if isinstance(item, Candidate) else item
        if isinstance(outcome, Skipped):
            tqdm.write(f"WARNING: skipping {outcome.image_id}: {outcome.reason}", file=sys.stderr)
            skipped.append(outcome)
            continue
        for image_id, jpeg in outcome:
            n = len(manifest_rows)
            batch_id = f"batch_{n // batch_size + 1:03d}"
            batch_dir = output_dir / batch_id
            if n % batch_size == 0:
                batch_dir.mkdir(parents=True, exist_ok=True)
            img_path = batch_dir / f"img_{n % batch_size + 1:05d}.jpg"
            img_path.write_bytes(jpeg)
            manifest_rows.append((batch_id, img_path.relative_to(output_dir).as_posix(), image_id))

    _write_tsv(output_dir / "manifest.tsv", ["batch", "preprocessed_path", "image_id"], manifest_rows)
    skipped_path = output_dir / "skipped.tsv"
    _write_tsv(skipped_path, ["image_id", "kind", "reason"], [(s.image_id, s.kind, s.reason) for s in skipped])

    written = len(manifest_rows)
    batches = -(-written // batch_size)
    return PreprocessResult(found, written, batches, skipped, skipped_path)
