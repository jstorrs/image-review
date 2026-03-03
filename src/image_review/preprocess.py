import csv
import io
from collections.abc import Iterator
from pathlib import Path
from zipfile import ZipFile

import matplotlib.pyplot as plt
import numpy as np
import pydicom
import skimage as ski
from tqdm import tqdm


def compress_image(image):
    i = image == np.roll(image, 1, axis=0)
    j = image == np.roll(image, 1, axis=1)
    k = ski.morphology.erosion(i & j, np.ones((5, 5), dtype=bool))
    image = np.delete(image, np.all(k, axis=1), axis=0)
    image = np.delete(image, np.all(k, axis=0), axis=1)
    return image


def preprocess_dicom(dcm: pydicom.FileDataset) -> np.ndarray:
    img = ski.util.img_as_float32(dcm.pixel_array)
    match dcm.PhotometricInterpretation:
        case "MONOCHROME1":
            img = ski.util.invert(img)
        case "MONOCHROME2":
            pass
    bot, top = img.min(), img.max()
    d = 0.01 * (top - bot) / 100
    bot, top = bot + d, top - d
    bot, top = np.quantile(img[(img > bot) & (img < top)], [0.01, 0.99])
    d = 0.02 * (top - bot)
    bot, top = bot + d, top - d
    img = ski.exposure.rescale_intensity(img, (bot, top))
    img = np.clip(img, 0, 1)
    img = ski.exposure.equalize_adapthist(img, 96)
    return compress_image(img)


def load_from_zip(path: Path) -> Iterator[tuple[str, np.ndarray]]:
    with ZipFile(path) as zf:
        files = [
            item.filename for item in zf.infolist() if item.filename.endswith(".dcm")
        ]
        for file in tqdm(files, desc=path.name, position=1, leave=False):
            with zf.open(file, "r") as fp:
                dcm = pydicom.dcmread(io.BytesIO(fp.read()))
                image_id = f"{path.name}:{Path(file).name}"
                yield image_id, preprocess_dicom(dcm)


def load_from_directory(path: Path) -> Iterator[tuple[str, np.ndarray | None]]:
    dcm_files = sorted(path.glob("**/*.dcm"))
    for f in tqdm(dcm_files, desc=path.name, position=1, leave=False):
        dcm = pydicom.dcmread(f)
        image_id = f"{path.name}/{f.relative_to(path)}"
        yield image_id, preprocess_dicom(dcm)
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        for f in sorted(path.glob(f"**/{ext}")):
            image_id = f"{path.name}/{f.relative_to(path)}"
            yield image_id, None


def load_single_file(path: Path) -> Iterator[tuple[str, np.ndarray | None]]:
    if path.suffix == ".dcm":
        dcm = pydicom.dcmread(path)
        yield path.name, preprocess_dicom(dcm)
    else:
        yield path.name, None


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


def preprocess_non_dicom(path: Path) -> np.ndarray:
    img = ski.io.imread(path)
    if img.ndim == 2:
        img = ski.util.img_as_float32(img)
        img = ski.exposure.equalize_adapthist(img, 96)
        img = ski.util.img_as_ubyte(img)
    return img


def _consume_batch(source_iter, batch_size, sources_dirs):
    """Consume up to batch_size items from the source iterator.

    Returns list of (image_id, source_path, array_or_None, resolved_path_or_None).
    """
    entries = []
    for image_id, array in source_iter:
        if array is None:
            resolved = _resolve_source_path(sources_dirs, image_id)
            source_path = str(resolved)
        else:
            resolved = None
            source_path = image_id.split(":")[0] if ":" in image_id else image_id.split("/")[0]
        entries.append((image_id, source_path, array, resolved))
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

    # Partition sources once to avoid repeated is_dir() calls
    sources_dirs = [(s, s.is_dir()) for s in sources]

    manifest_rows = []
    batch_num = 0

    # Stream batches from the generator to avoid holding all arrays in memory
    source_iter = load_sources(sources)
    while True:
        entries = _consume_batch(source_iter, batch_size, sources_dirs)
        if not entries:
            break
        batch_num += 1
        batch_id = f"batch_{batch_num:03d}"
        batch_dir = output_dir / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)

        for idx, (image_id, source_path, array, resolved) in enumerate(entries):
            img_filename = f"img_{idx + 1:05d}.jpg"
            img_path = batch_dir / img_filename

            if array is not None:
                rgb = apply_colormap(array, colormap)
                ski.io.imsave(str(img_path), rgb)
            else:
                img = preprocess_non_dicom(resolved)
                ski.io.imsave(str(img_path), img)

            manifest_rows.append((image_id, batch_id, source_path, str(img_path.relative_to(output_dir))))

    # Write manifest
    with open(manifest_path, "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["image_id", "batch", "source_path", "preprocessed_path"])
        writer.writerows(manifest_rows)

    print(f"Preprocessed {len(manifest_rows)} images in {batch_num} batches -> {output_dir}")


def _resolve_source_path(sources_dirs: list[tuple[Path, bool]], image_id: str) -> Path:
    for source, is_dir in sources_dirs:
        if is_dir:
            parts = image_id.split("/", 1)
            if len(parts) == 2 and parts[0] == source.name:
                candidate = source / parts[1]
                if candidate.exists():
                    return candidate
        elif source.name == image_id:
            return source
    return Path(image_id)
