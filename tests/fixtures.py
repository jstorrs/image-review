import csv
import hashlib
import http.client
import io
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import click.testing
import numpy as np
import pydicom
import skimage as ski
from PIL import Image
from pydicom.dataset import FileMetaDataset
from pydicom.uid import SecondaryCaptureImageStorage, generate_uid

from image_review import layout as layout_module
from image_review.cli import cli
from image_review.status import MarkMode, Verdict
from image_review.store import LocalStore, ReviewStore

# (batch, preprocessed_path, image_id); image_ids deliberately differ from keys
ROWS = [
    ("batch_001", "batch_001/a.jpg", "/src/patient_smith/a.dcm"),
    ("batch_001", "batch_001/b.jpg", "/src/patient_jones/b.dcm"),
    ("batch_002", "batch_002/c.jpg", "/src/patient_lee/c.dcm"),
    ("batch_002", "batch_002/d.jpg", "/src/patient_kim/d.dcm"),
]


# Every variable the CLI reads from the environment; cleared so a developer's exports cannot leak into a test
CLEAN_ENV: dict[str, str | None] = {
    "IMAGE_REVIEW_REMOTE": None,
    "IMAGE_REVIEW_VIA": None,
    "IMAGE_REVIEW_DIRECT": None,
    "IMAGE_REVIEW_SOCKET_PATH": None,
    "IMAGE_REVIEW_TOKEN": None,
    "IMAGE_REVIEW_ACCESS": None,
    "IMAGE_REVIEW_REVIEWER": None,
}


def invoke_cli(*args: str, env: dict[str, str | None] | None = None, **kwargs) -> click.testing.Result:
    """Run the CLI in-process with IMAGE_REVIEW_* cleared, plus `env`; `kwargs` go to CliRunner.invoke."""
    return click.testing.CliRunner().invoke(cli, list(args), env={**CLEAN_ENV, **(env or {})}, **kwargs)


def make_work_dir(root: Path, hashed: bool = False) -> None:
    """The ROWS work dir: solid grey JPGs and a legacy 3-column manifest, or (`hashed`) one with hash columns."""
    for _, key, _ in ROWS:
        path = root / key
        path.parent.mkdir(exist_ok=True)
        ski.io.imsave(path, np.full((12, 20, 3), 128, dtype=np.uint8), check_contrast=False)
    write_manifest(root, hashed)


def write_manifest(root: Path, hashed: bool = False) -> None:
    """Write ROWS as manifest.tsv. `hashed`: the current 5-column format, with each JPG's SHA-256 as it is on disk
    now (and a stand-in source hash); otherwise the legacy 3-column format."""
    with open(root / "manifest.tsv", "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        if not hashed:
            writer.writerow(["batch", "preprocessed_path", "image_id"])
            writer.writerows(ROWS)
            return
        writer.writerow(["batch", "preprocessed_path", "image_id", "source_sha256", "jpeg_sha256"])
        for batch, key, image_id in ROWS:
            source = hashlib.sha256(image_id.encode()).hexdigest()
            writer.writerow([batch, key, image_id, source, hashlib.sha256((root / key).read_bytes()).hexdigest()])


def write_dicom(
    path: Path, pixels: np.ndarray, photometric: str = "MONOCHROME2", preamble: bool = True, **attrs
) -> None:
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


def _serve_in_thread(server, store):
    """Run server.serve_forever in a thread; returns an idempotent stop() that shuts down, closes and releases the store."""
    import threading

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()

    stopped = False

    def stop() -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        server.shutdown()
        thread.join()
        server.server_close()
        store.close()

    return stop


def start_server(work_dir: Path, port: int = 0):
    """Serve work_dir on 127.0.0.1 in a thread; returns (server, target, stop). The store holds the work dir lock until stop(), which is idempotent."""
    from image_review.server import make_server
    from image_review.store import LocalStore

    store = LocalStore(work_dir)
    server, target = make_server(store, "127.0.0.1", port)
    return server, target, _serve_in_thread(server, store)


def start_unix_server(work_dir: Path, path: Path):
    """Serve work_dir on the Unix socket `path` in a thread; returns (server, token, stop). As start_server."""
    from image_review.server import make_unix_server
    from image_review.store import LocalStore

    store = LocalStore(work_dir)
    server, token = make_unix_server(store, path)
    return server, token, _serve_in_thread(server, store)


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over the Unix socket `path`; `host` only sets the Host header."""

    def __init__(self, path: Path, host: str = "localhost:8080", timeout: float = 10):
        super().__init__(host, timeout=timeout)
        self.path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self.path))
        self.sock = sock


def mark(
    store: ReviewStore,
    keys: list[str],
    status: Verdict,
    pass_number: int = 1,
    *,
    reviewer: str = "tester",
    mode: MarkMode = "single",
):
    """store.mark with the usual reviewer and mode, for tests where neither is the point."""
    return store.mark(keys, status, pass_number, reviewer=reviewer, mode=mode)


HAS_AF_UNIX = hasattr(socket, "AF_UNIX")


def socket_dir(testcase: unittest.TestCase) -> Path:
    """A fresh directory under /tmp: macOS $TMPDIR is too long for sun_path."""
    path = Path(tempfile.mkdtemp(dir="/tmp"))
    testcase.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


def wait_for_file(testcase: unittest.TestCase, proc: subprocess.Popen, directory: Path, pattern: str) -> None:
    """Wait for `proc` to create a file matching `pattern` in `directory`; on timeout or exit, fail with its output."""
    deadline = time.monotonic() + 60  # generous: slow name lookups on CI runners
    while not list(directory.glob(pattern)):
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            out, err = proc.communicate()
            testcase.fail(f"no {pattern} in {directory} (exit {proc.returncode})\nstdout:\n{out}\nstderr:\n{err}")
        time.sleep(0.05)


def temp_dir(testcase: unittest.TestCase) -> Path:
    """A fresh temporary directory, removed when `testcase` finishes."""
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    return Path(tmp.name)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.work_dir = temp_dir(self)
        self.make_work_dir()
        self.store = LocalStore(self.work_dir)
        self.addCleanup(self.store.close)

    def make_work_dir(self) -> None:
        make_work_dir(self.work_dir)


def _jpeg_bytes(mode: str) -> bytes:
    """A 257x131 gradient-plus-noise JPG made like preprocess: Pillow, q95, 4:4:4."""
    rng = np.random.default_rng(0)
    y, x = np.mgrid[0:131, 0:257]
    base = np.stack([x * 255 // 256, y * 255 // 130, (x + y) % 256], axis=-1)
    pixels = np.clip(base + rng.integers(-20, 20, base.shape), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(pixels).convert(mode).save(buf, "JPEG", quality=95, subsampling=0)
    return buf.getvalue()


def dropping_packer(rect_id: int):
    """Patch layout.newPacker so its packer leaves out the rect `rect_id` (the key's index in `keys`)."""
    real = layout_module.newPacker

    def factory(*args, **kwargs):
        packer = real(*args, **kwargs)
        rect_list = packer.rect_list
        packer.rect_list = lambda: [r for r in rect_list() if r[5] != rect_id]
        return packer

    return mock.patch.object(layout_module, "newPacker", factory)
