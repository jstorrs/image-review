import csv
import io
import sys
from collections.abc import Iterator
from pathlib import Path
from zipfile import ZipFile

import matplotlib.pyplot as plt
import numpy as np
import pydicom
import skimage as ski
from tqdm import tqdm

EROSION_KERNEL_SIZE = 5
OUTLIER_PERCENTILE = 0.01
INTENSITY_MARGIN = 0.02
CLAHE_BINS = 96


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


def preprocess_dicom(dcm: pydicom.FileDataset) -> np.ndarray:
    img = ski.util.img_as_float32(dcm.pixel_array)
    match dcm.PhotometricInterpretation:
        case "MONOCHROME1":
            img = ski.util.invert(img)
        case "MONOCHROME2":
            pass
    bot, top = img.min(), img.max()
    margin_initial = OUTLIER_PERCENTILE * (top - bot)
    bot, top = bot + margin_initial, top - margin_initial
    filtered = img[(img > bot) & (img < top)]
    if filtered.size == 0:
        return compress_image(img)
    bot, top = np.quantile(filtered, [OUTLIER_PERCENTILE, 1 - OUTLIER_PERCENTILE])
    margin_final = INTENSITY_MARGIN * (top - bot)
    bot, top = bot + margin_final, top - margin_final
    if bot >= top:
        return compress_image(img)
    img = ski.exposure.rescale_intensity(img, (bot, top))
    img = np.clip(img, 0, 1)
    img = compress_image(img)
    img = ski.exposure.equalize_adapthist(img, CLAHE_BINS)
    return compress_image(img)


_IMAGE_EXTENSIONS = {".dcm", ".jpg", ".jpeg", ".png"}


def load_from_zip(path: Path) -> Iterator[tuple[str, np.ndarray]]:
    with ZipFile(path) as zf:
        files = [
            item.filename
            for item in zf.infolist()
            if Path(item.filename).suffix.lower() in _IMAGE_EXTENSIONS
        ]
        for file in tqdm(files, desc=path.name, position=1, leave=False):
            image_id = f"{path.as_posix()}::{file}"
            try:
                with zf.open(file, "r") as fp:
                    data = fp.read()
                if file.endswith(".dcm"):
                    dcm = pydicom.dcmread(io.BytesIO(data))
                    yield image_id, preprocess_dicom(dcm)
                else:
                    yield image_id, preprocess_non_dicom_bytes(data)
            except Exception as exc:
                tqdm.write(f"WARNING: skipping {image_id}: {exc}", file=sys.stderr)


def load_from_directory(path: Path) -> Iterator[tuple[str, np.ndarray | None]]:
    dcm_files = sorted(path.glob("**/*.dcm"))
    for f in tqdm(dcm_files, desc=path.name, position=1, leave=False):
        image_id = f.as_posix()
        try:
            dcm = pydicom.dcmread(f)
            yield image_id, preprocess_dicom(dcm)
        except Exception as exc:
            tqdm.write(f"WARNING: skipping {image_id}: {exc}", file=sys.stderr)
    non_dcm_files = []
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        non_dcm_files.extend(sorted(path.glob(f"**/{ext}")))
    for f in tqdm(non_dcm_files, desc=f"{path.name} (non-DICOM)", position=1, leave=False):
        image_id = f.as_posix()
        yield image_id, None


def load_single_file(path: Path) -> Iterator[tuple[str, np.ndarray | None]]:
    image_id = path.as_posix()
    try:
        if path.suffix == ".dcm":
            dcm = pydicom.dcmread(path)
            yield image_id, preprocess_dicom(dcm)
        else:
            yield image_id, None
    except Exception as exc:
        tqdm.write(f"WARNING: skipping {image_id}: {exc}", file=sys.stderr)


def load_sources(sources: list[Path]) -> Iterator[tuple[str, np.ndarray | None]]:
    for source in tqdm(sources, desc="Sources", position=0):
        if source.suffix == ".zip":
            yield from load_from_zip(source)
        elif source.is_dir():
            yield from load_from_directory(source)
        else:
            yield from load_single_file(source)


def apply_colormap(img: np.ndarray, colormap: str = "inferno") -> np.ndarray:
    cm = plt.get_cmap(colormap)
    return ski.util.img_as_ubyte(cm(img)[:, :, :3])


def _preprocess_non_dicom_array(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        img = ski.util.img_as_float32(img)
        img = ski.exposure.equalize_adapthist(img, CLAHE_BINS)
        img = ski.util.img_as_ubyte(img)
    return img


def preprocess_non_dicom(path: Path) -> np.ndarray:
    return _preprocess_non_dicom_array(ski.io.imread(path))


def preprocess_non_dicom_bytes(buf: bytes) -> np.ndarray:
    return _preprocess_non_dicom_array(ski.io.imread(io.BytesIO(buf)))


def _consume_batch(source_iter, batch_size):
    """Consume up to batch_size items from the source iterator.

    Returns list of (image_id, array_or_None, resolved_path_or_None).
    """
    entries = []
    for image_id, array in source_iter:
        resolved = Path(image_id) if array is None else None
        entries.append((image_id, array, resolved))
        if len(entries) >= batch_size:
            break
    return entries


def run_preprocess(
    sources: list[Path],
    output_dir: Path,
    batch_size: int = 300,
    colormap: str = "inferno",
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.tsv"

    manifest_rows = []
    batch_num = 0

    # Stream batches from the generator to avoid holding all arrays in memory
    source_iter = load_sources(sources)
    while True:
        entries = _consume_batch(source_iter, batch_size)
        if not entries:
            break
        batch_num += 1
        batch_id = f"batch_{batch_num:03d}"
        batch_dir = output_dir / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)

        for idx, (image_id, array, resolved) in enumerate(entries):
            img_filename = f"img_{idx + 1:05d}.jpg"
            img_path = batch_dir / img_filename

            if array is not None:
                rgb = apply_colormap(array, colormap)
                ski.io.imsave(str(img_path), rgb)
            else:
                img = preprocess_non_dicom(resolved)
                ski.io.imsave(str(img_path), img)

            manifest_rows.append((batch_id, img_path.relative_to(output_dir).as_posix(), image_id))

    # Write manifest
    with open(manifest_path, "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["batch", "preprocessed_path", "image_id"])
        writer.writerows(manifest_rows)

    print(f"Preprocessed {len(manifest_rows)} images in {batch_num} batches -> {output_dir}")


