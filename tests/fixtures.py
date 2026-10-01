import csv
from pathlib import Path

import numpy as np
import pydicom
import skimage as ski
from pydicom.dataset import FileMetaDataset
from pydicom.uid import SecondaryCaptureImageStorage, generate_uid

# (batch, preprocessed_path, image_id); image_ids deliberately differ from keys
ROWS = [
    ("batch_001", "batch_001/a.jpg", "/src/patient_smith/a.dcm"),
    ("batch_001", "batch_001/b.jpg", "/src/patient_jones/b.dcm"),
    ("batch_002", "batch_002/c.jpg", "/src/patient_lee/c.dcm"),
    ("batch_002", "batch_002/d.jpg", "/src/patient_kim/d.dcm"),
]


def make_work_dir(root: Path) -> None:
    for _, key, _ in ROWS:
        path = root / key
        path.parent.mkdir(exist_ok=True)
        ski.io.imsave(path, np.full((12, 20, 3), 128, dtype=np.uint8), check_contrast=False)
    with open(root / "manifest.tsv", "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["batch", "preprocessed_path", "image_id"])
        writer.writerows(ROWS)


def write_dicom(path: Path, pixels: np.ndarray, photometric: str = "MONOCHROME2", preamble: bool = True, **attrs) -> None:
    """Write a synthetic DICOM file (with preamble and file meta) holding `pixels`.

    `pixels` is (rows, cols) or (rows, cols, 3) for one frame, or (frames, rows, cols[, 3]).
    With `preamble=False` only the dataset is written (implicit VR little endian,
    no preamble, `DICM` or file meta), as some older systems do.
    Extra keyword arguments are set as DICOM attributes.
    """
    ds = pydicom.Dataset()
    ds.file_meta = FileMetaDataset()
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.set_pixel_data(pixels, photometric, pixels.dtype.itemsize * 8)
    for name, value in attrs.items():
        setattr(ds, name, value)
    if preamble:
        ds.save_as(path, enforce_file_format=True)
    else:
        del ds.file_meta
        ds.save_as(path, implicit_vr=True, little_endian=True)


def add_overlay(ds: pydicom.Dataset, mask: np.ndarray, origin: tuple[int, int] = (1, 1), group: int = 0x6000) -> None:
    """Add a bitmap overlay plane for the boolean `mask` at OverlayOrigin `origin` (1-based row, column)."""
    rows, cols = mask.shape
    packed = np.packbits(mask.ravel().astype(np.uint8), bitorder="little").tobytes()
    ds.add_new((group, 0x0010), "US", rows)
    ds.add_new((group, 0x0011), "US", cols)
    ds.add_new((group, 0x0040), "CS", "G")
    ds.add_new((group, 0x0050), "SS", list(origin))
    ds.add_new((group, 0x0100), "US", 1)
    ds.add_new((group, 0x0102), "US", 0)
    ds.add_new((group, 0x3000), "OW", packed + b"\x00" * (len(packed) % 2))


def start_server(work_dir: Path, port: int = 0):
    """Serve work_dir on 127.0.0.1 in a thread; returns (server, target, stop)."""
    import threading

    from image_review.server import make_server
    from image_review.store import LocalStore

    server, target = make_server(LocalStore(work_dir), "127.0.0.1", port)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        thread.join()
        server.server_close()

    return server, target, stop
