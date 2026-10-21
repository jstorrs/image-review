"""Preprocess: every input lands in exactly one of manifest.tsv or skipped.tsv; notes.tsv notes written ones."""

import contextlib
import csv
import faulthandler
import hashlib
import io
import json
import multiprocessing
import os
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
import zlib
from collections.abc import Sequence
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from unittest import mock

import imagecodecs
import numpy as np
import pydicom
import skimage as ski
import tifffile
from PIL import Image, JpegImagePlugin, PngImagePlugin
from pydicom.data import get_testdata_file, get_testdata_files
from pydicom.dataset import FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import (
    MPEG2MPML,
    UID,
    ExplicitVRLittleEndian,
    HTJ2KLossless,
    JPEG2000Lossless,
    JPEGBaseline8Bit,
    JPEGLSLossless,
    MediaStorageDirectoryStorage,
    RLELossless,
    SecondaryCaptureImageStorage,
    generate_uid,
)

from image_review import cli as cli_module
from image_review import preprocess as preprocess_module
from image_review.cli import default_jobs
from image_review.export import AMBIGUOUS_ICON, export_rows, unreviewed_content
from image_review.preprocess import (
    BAD_NAME_REASON,
    COLLISION_REASON,
    SIDE_BY_SIDE_GAP,
    Candidate,
    DecodeError,
    Note,
    Rejected,
    Unsupported,
    WorkDirExists,
    WorkerCrashed,
    _check_rle_frame,
    _escape_name,
    _failed,
    _j2k_frame_size,
    _jpeg_end,
    _jpeg_frame_size,
    _mpo_unrendered,
    _png_end,
    _rle_decoded_length,
    _to_uint8,
    _trailing_data_notes,
    _uncovered,
    _unrendered_detail,
    _WalkBudget,
    _window,
    classify,
    colliding_ids,
    compress_image,
    decode_raster,
    discover,
    encode_jpeg,
    render,
    render_and_encode,
    run_preprocess,
)
from image_review.review_db import Decision
from image_review.signals import TERMINATION_SIGNALS, interrupt_on
from image_review.status import ImageId
from image_review.store import LocalStore, SkippedRow, load_manifest, load_notes, load_skipped
from tests.fixtures import add_overlay, invoke_cli, mark, temp_dir, write_dicom

RNG = np.random.default_rng(0)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
APPLEDOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + bytes(100)
# SLURM_CPUS_PER_TASK=1 renders in-process, with no worker start-up per run
CLI_ENV = {"SLURM_CPUS_PER_TASK": "1"}
NO_NOTES = {
    "overlay-plane": 0,
    "overlay-unused-bits": 0,
    "icon-image": 0,
    "alpha": 0,
    "embedded-thumbnail": 0,
    "trailing-data": 0,
}


def _good_pixels() -> np.ndarray:
    gradient = np.linspace(0, 3000, 128 * 128).reshape(128, 128)
    return (gradient + RNG.integers(0, 200, (128, 128))).astype(np.uint16)


@contextlib.contextmanager
def quiet():
    """Silence progress bars, skip warnings and skimage low-contrast warnings."""
    with warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
        warnings.simplefilter("ignore")
        yield


def _png_bytes(arr: np.ndarray, mode: str | None = None) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr, mode=mode).save(buf, "PNG")
    return buf.getvalue()


def _text_mask(shape=(40, 60)) -> np.ndarray:
    """A bar of 'text' in an otherwise empty field."""
    mask = np.zeros(shape, dtype=bool)
    mask[15:25, 10:50] = True
    return mask


def _text_pixels() -> np.ndarray:
    """12-bit uint16 noise with a bright bar of 'text' (see `_text_mask`)."""
    pixels = np.random.default_rng(7).integers(500, 1500, (40, 60)).astype(np.uint16)
    pixels[_text_mask()] = 4000
    return pixels


def _with_unused_bit(pixels: np.ndarray, bit: int) -> np.ndarray:
    """`pixels` with `bit` set under the middle rows of the text mask, as a retired overlay in PixelData would be."""
    marked = pixels.copy()
    under = np.zeros(pixels.shape, dtype=bool)
    under[18:22, 12:48] = True
    marked[under] |= marked.dtype.type(1 << bit)
    return marked


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def make_mixed_source(root: Path) -> dict[str, Path]:
    """A directory of good, odd and broken inputs, plus a corrupt zip beside it."""
    src = root / "src"
    src.mkdir()
    paths = {
        "good": src / "good.dcm",
        "zero": src / "zero.dcm",
        "rgb": src / "rgb.dcm",
        "multi": src / "multi.dcm",
        "rgba": src / "rgba.png",
        "la": src / "la.png",
        "corrupt_jpg": src / "corrupt.jpg",
        "corrupt_zip": root / "corrupt.zip",
    }
    write_dicom(paths["good"], _good_pixels())
    write_dicom(paths["zero"], np.zeros((64, 64), dtype=np.uint16))
    write_dicom(paths["rgb"], RNG.integers(0, 255, (32, 32, 3), dtype=np.uint8), "RGB")
    write_dicom(paths["multi"], RNG.integers(0, 4000, (3, 32, 32), dtype=np.uint16))
    Image.fromarray(RNG.integers(0, 255, (40, 50, 4), dtype=np.uint8), mode="RGBA").save(paths["rgba"])
    Image.fromarray(RNG.integers(0, 255, (40, 50, 2), dtype=np.uint8), mode="LA").save(paths["la"])
    paths["corrupt_jpg"].write_bytes(b"\xff\xd8\xff truncated jpeg")
    paths["corrupt_zip"].write_bytes(b"PK\x03\x04 truncated garbage")
    return paths


def _exif_ifd1(entries: Sequence[tuple[int, int]], tail: bytes = b"") -> bytes:
    """EXIF bytes: a TIFF header, an IFD0 with one entry, and an IFD1 of LONG `entries` (tag, value), then `tail`."""
    ifd1_at = 8 + 2 + 12 + 4  # IFD0 (one entry) follows the 8-byte header; IFD1 follows IFD0
    body = struct.pack("<2sHI", b"II", 42, 8)
    body += struct.pack("<H", 1) + struct.pack("<HHIHH", 0x0112, 3, 1, 1, 0) + struct.pack("<I", ifd1_at)  # Orientation
    body += struct.pack("<H", len(entries))
    body += b"".join(struct.pack("<HHII", tag, 4, 1, value) for tag, value in entries)
    return b"Exif\x00\x00" + body + struct.pack("<I", 0) + tail


def _exif_with_thumbnail() -> bytes:
    """EXIF bytes whose IFD1 has 0x0201/0x0202 and holds a tiny JPEG as the thumbnail."""
    thumb = io.BytesIO()
    Image.new("L", (4, 4), 9).save(thumb, "JPEG")
    jpeg = thumb.getvalue()
    jpeg_at = 8 + 2 + 12 + 4 + 2 + 2 * 12 + 4
    return _exif_ifd1([(0x0201, jpeg_at), (0x0202, len(jpeg))], jpeg)


def _thumbnail_len(exif: bytes) -> int:
    return len(exif) - exif.index(b"\xff\xd8")


def _png_with_exif_after_pixels(png: bytes, exif: bytes) -> bytes:
    """`png` with an eXIf chunk holding `exif` (without its `Exif\\0\\0` prefix) inserted just before IEND."""
    payload = exif.removeprefix(b"Exif\x00\x00")
    chunk = struct.pack(">I", len(payload)) + b"eXIf" + payload + struct.pack(">I", zlib.crc32(b"eXIf" + payload))
    return png[:-12] + chunk + png[-12:]


def _mpo_bytes(frame1_exif: bytes | None = None) -> bytes:
    """A two-frame MPO, with an APP1 segment holding `frame1_exif` inserted right after the second frame's SOI."""
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (255, 255, 255)).save(
        buf, "MPO", save_all=True, append_images=[Image.new("L", (30, 20), 128)]
    )
    data = buf.getvalue()
    if frame1_exif is None:
        return data
    soi = data.rindex(b"\xff\xd8\xff")  # the second frame; the first holds no embedded JPEG
    segment = b"\xff\xe1" + struct.pack(">H", len(frame1_exif) + 2) + frame1_exif
    return data[: soi + 2] + segment + data[soi + 2 :]


_HDRGM_XMP = b'http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta><rdf:Description hdrgm:Version="1.0"/></x:xmpmeta>'


def _with_hdrgm_xmp(mpo: bytes) -> bytes:
    """`mpo` with an APP1 XMP segment declaring `hdrgm:Version` right after the first frame's SOI: an Ultra HDR file
    (a base image and its gain map), which Pillow 12 opens as a plain JPEG and Pillow 10.3 as an MPO."""
    segment = b"\xff\xe1" + struct.pack(">H", len(_HDRGM_XMP) + 2) + _HDRGM_XMP
    return mpo[:2] + segment + mpo[2:]


class MixedSourceTest(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self).resolve()
        self.paths = make_mixed_source(self.root)
        self.sources = [self.root / "src", self.paths["corrupt_zip"]]

    def run_quietly(self, work: Path, **kwargs):
        with quiet():
            return run_preprocess(self.sources, work, **kwargs)

    def test_every_input_accounted_for_exactly_once(self):
        work = self.root / "work"
        result = self.run_quietly(work)
        manifest = _read_tsv(work / "manifest.tsv")
        skipped = _read_tsv(work / "skipped.tsv")

        ids = [r["image_id"] for r in manifest] + [r["image_id"] for r in skipped]
        self.assertCountEqual(ids, [p.as_posix() for p in self.paths.values()])
        self.assertEqual(result.found, len(ids))
        self.assertEqual(result.written, len(manifest))
        self.assertEqual(len(result.skipped), len(skipped))

        rendered = {r["image_id"] for r in manifest}
        expected = {self.paths[k].as_posix() for k in ("good", "zero", "rgb", "rgba", "la")}
        self.assertEqual(rendered, expected)

        reasons = {r["image_id"]: r["reason"] for r in skipped}
        self.assertTrue(all(r["kind"] == "failed" for r in skipped))
        self.assertEqual(reasons[self.paths["multi"].as_posix()], "unsupported: multi-frame DICOM (3 frames)")
        self.assertIn(self.paths["corrupt_jpg"].as_posix(), reasons)
        self.assertTrue(reasons[self.paths["corrupt_zip"].as_posix()].startswith("BadZipFile:"))

    def test_output_jpgs_are_rgb_uint8(self):
        work = self.root / "work"
        self.run_quietly(work)
        for row in _read_tsv(work / "manifest.tsv"):
            img = ski.io.imread(work / row["preprocessed_path"])
            self.assertEqual(img.dtype, np.uint8)
            self.assertEqual(img.ndim, 3)
            self.assertEqual(img.shape[2], 3)

    def test_output_jpgs_are_444_high_quality(self):
        work = self.root / "work"
        self.run_quietly(work)
        for row in _read_tsv(work / "manifest.tsv"):
            with Image.open(work / row["preprocessed_path"]) as img:
                self.assertEqual(JpegImagePlugin.get_sampling(img), 0)
                self.assertLess(max(img.quantization[0]), 20)  # q75 tables reach well above this

    def test_batches_are_numbered_by_write_order(self):
        work = self.root / "work"
        result = self.run_quietly(work, batch_size=3)
        keys = [r["preprocessed_path"] for r in _read_tsv(work / "manifest.tsv")]
        self.assertEqual(
            keys,
            [
                "batch_001/img_00001.jpg",
                "batch_001/img_00002.jpg",
                "batch_001/img_00003.jpg",
                "batch_002/img_00001.jpg",
                "batch_002/img_00002.jpg",
            ],
        )
        self.assertEqual(result.batches, 2)
        self.assertEqual(sorted(p.name for p in work.glob("batch_*")), ["batch_001", "batch_002"])


class EmptySkippedTest(unittest.TestCase):
    def test_skipped_tsv_written_with_header_when_nothing_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dicom(root / "good.dcm", _good_pixels())
            with quiet():
                result = run_preprocess([root / "good.dcm"], root / "work")
            self.assertEqual(result.skipped, [])
            with open(root / "work" / "skipped.tsv", newline="", encoding="utf-8") as f:
                self.assertEqual(list(csv.reader(f, delimiter="\t")), [["image_id", "kind", "reason"]])
            self.assertEqual(len(_read_tsv(root / "work" / "manifest.tsv")), 1)


class ZipSourceTest(unittest.TestCase):
    def test_zip_entries_are_read_while_archive_is_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            write_dicom(root / "good.dcm", _good_pixels())
            archive = root / "scans.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.write(root / "good.dcm", "dir/good.dcm")
                zf.writestr("dir/broken.png", PNG_MAGIC + b"truncated")
                zf.writestr("dir/notes.txt", b"not an image")
            with quiet():
                result = run_preprocess([archive], root / "work")
            manifest = _read_tsv(root / "work" / "manifest.tsv")
            skipped = _read_tsv(root / "work" / "skipped.tsv")
            self.assertEqual([r["image_id"] for r in manifest], [f"{archive.as_posix()}::dir/good.dcm"])
            self.assertEqual(
                [(r["image_id"], r["kind"]) for r in skipped],
                [
                    (f"{archive.as_posix()}::dir/broken.png", "failed"),
                    (f"{archive.as_posix()}::dir/notes.txt", "ignored"),
                ],
            )
            self.assertEqual(result.found, 3)

    def test_non_ascii_entry_names_round_trip_under_an_ascii_locale(self):
        script = (
            "import sys, zipfile\n"
            "from pathlib import Path\n"
            "from image_review.preprocess import run_preprocess\n"
            "from image_review.store import load_manifest, load_skipped\n"
            "root = Path(sys.argv[1])\n"
            "with zipfile.ZipFile(root / 'scans.zip', 'w') as zf:\n"
            "    zf.writestr('\\u00e9\\u65e5.png', Path(sys.argv[2]).read_bytes())\n"
            "    zf.writestr('\\u00e8.txt', b'not an image')\n"
            "run_preprocess([root / 'scans.zip'], root / 'work')\n"
            "print(ascii([e.image_id for e in load_manifest(root / 'work')]))\n"
            "print(ascii([r.image_id for r in load_skipped(root / 'work')]))\n"
        )
        env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            png = root / "in.png"
            png.write_bytes(_png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)))
            proc = subprocess.run(
                [sys.executable, "-c", script, str(root), str(png)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            archive = (root / "scans.zip").as_posix()
            self.assertEqual(
                proc.stdout.splitlines(),
                [ascii([f"{archive}::\u00e9\u65e5.png"]), ascii([f"{archive}::\u00e8.txt"])],
            )
            self.assertIn("\u00e9\u65e5".encode(), (root / "work" / "manifest.tsv").read_bytes())

    def test_duplicate_entry_names_are_distinct_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive = root / "dup.zip"
            with warnings.catch_warnings(), zipfile.ZipFile(archive, "w") as zf:
                warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
                zf.writestr("a.png", _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)))
                zf.writestr("a.png", _png_bytes(np.full((40, 60, 3), 255, dtype=np.uint8)))
            with quiet():
                run_preprocess([archive], root / "work")
            manifest = _read_tsv(root / "work" / "manifest.tsv")
            base = f"{archive.as_posix()}::a.png"
            self.assertEqual([r["image_id"] for r in manifest], [base, f"{base}#2"])
            first, second = (ski.io.imread(root / "work" / r["preprocessed_path"]) for r in manifest)
            self.assertLess(first.mean(), 64)
            self.assertGreater(second.mean(), 192)


def _jpeg_bytes(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG")
    return buf.getvalue()


def _discovered(sources: list[Path], exclude: frozenset[Path] = frozenset()) -> list[tuple[str, str]]:
    """(image_id, kind or skip kind) for each discovered item."""
    with quiet():
        return [(item.image_id, item.kind) for item in discover(sources, exclude)]


def _dicom_with_icon(path: Path) -> None:
    """A DICOM with an 8x8 8-bit ramp icon."""
    icon = pydicom.Dataset()
    icon.Rows, icon.Columns, icon.SamplesPerPixel = 8, 8, 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.BitsAllocated, icon.BitsStored, icon.HighBit, icon.PixelRepresentation = 8, 8, 7, 0
    icon.PixelData = np.arange(64, dtype=np.uint8).tobytes()
    write_dicom(path, _good_pixels(), IconImageSequence=[icon])


def _dicom_with_overlays(path: Path, *groups: int, photometric: str = "MONOCHROME2") -> None:
    """A DICOM with an 8x8 overlay plane in each of `groups`."""
    write_dicom(path, _good_pixels(), photometric)
    ds = pydicom.dcmread(path)
    for group in groups:
        add_overlay(ds, np.ones((8, 8), dtype=bool), origin=(3, 3), group=group)
    ds.save_as(path, enforce_file_format=True)


def _png_text(key: str, value: str) -> PngImagePlugin.PngInfo:
    info = PngImagePlugin.PngInfo()
    info.add_text(key, value)
    return info


def _solid_png(value: int) -> bytes:
    return _png_bytes(np.full((40, 60, 3), value, dtype=np.uint8))


class CollisionTest(unittest.TestCase):
    """An image_id naming two different images would share one verdict: every input involved fails instead."""

    def setUp(self):
        self.root = temp_dir(self).resolve()
        self.src = self.root / "src"
        self.src.mkdir()
        self.work = self.root / "work"

    def run_quietly(self, *sources: Path):
        with quiet():
            return run_preprocess(list(sources or [self.src]), self.work)

    def assert_no_orphans(self, manifest: list[dict[str, str]]) -> None:
        """Colliding JPGs are deleted: the batch dirs hold exactly the manifest's JPGs, and no staging is left."""
        on_disk = {p.relative_to(self.work).as_posix() for p in self.work.glob("batch_*/*")}
        self.assertEqual(on_disk, {r["preprocessed_path"] for r in manifest})
        self.assertEqual(sorted(p.name for p in self.work.iterdir() if p.name.startswith(".")), [])

    def assert_collision(self, expected_ids: list[str], kept: list[str], inputs: int) -> None:
        manifest = _read_tsv(self.work / "manifest.tsv")
        skipped = _read_tsv(self.work / "skipped.tsv")
        self.assertEqual([r["image_id"] for r in manifest], kept)
        self.assertEqual(
            [(r["image_id"], r["kind"], r["reason"]) for r in skipped],
            [(i, "failed", COLLISION_REASON) for i in expected_ids],
        )
        self.assert_no_orphans(manifest)
        record = json.loads((self.work / "preprocess.json").read_text())
        self.assertEqual(
            record["counts"],
            {
                "inputs": inputs,
                "written": len(kept),
                "skipped_failed": len(expected_ids),
                "skipped_ignored": 0,
                "notes": NO_NOTES,
            },
        )

    def assert_cli_exit_codes(self) -> None:
        for extra, code in (((), 1), (("--allow-skipped",), 0)):
            with self.subTest(args=extra), quiet():
                work = self.root / f"cli{code}"
                args = ["preprocess", str(self.src), "--work-dir", str(work), *extra]
                result = invoke_cli(*args, env=CLI_ENV)
            self.assertEqual(result.exit_code, code, result.output)

    def test_zip_entry_named_like_a_numbered_duplicate(self):
        archive = self.src / "z.zip"
        with warnings.catch_warnings(), zipfile.ZipFile(archive, "w") as zf:
            warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
            zf.writestr("a.png", _solid_png(0))
            zf.writestr("a.png", _solid_png(255))  # becomes a.png#2
            zf.writestr("a.png#2", _solid_png(128))
        self.run_quietly()
        base = f"{archive.as_posix()}::a.png"
        self.assert_collision([f"{base}#2", f"{base}#2"], kept=[base], inputs=3)
        self.assert_cli_exit_codes()

    def test_file_named_like_a_zip_entry(self):
        archive = self.src / "z.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.png", _solid_png(0))
        (self.src / "z.zip::a.png").write_bytes(_solid_png(255))
        self.run_quietly()
        image_id = f"{archive.as_posix()}::a.png"
        self.assert_collision([image_id, image_id], kept=[], inputs=2)
        self.assert_cli_exit_codes()

    def test_file_named_like_a_dicom_icon_is_not_a_collision(self):
        _dicom_with_icon(self.src / "a.dcm")
        (self.src / "a.dcm#icon").write_bytes(_solid_png(255))
        result = self.run_quietly()
        dicom = (self.src / "a.dcm").as_posix()
        # the icon is never rendered, so no `#icon` id is minted to collide with the real file
        self.assertEqual([r["image_id"] for r in _read_tsv(self.work / "manifest.tsv")], [dicom, f"{dicom}#icon"])
        self.assertEqual(result.skipped, [])
        self.assertEqual(_read_tsv(self.work / "skipped.tsv"), [])
        # export cannot tell the file from an older version's icon: both rows are NOT_REVIEWED, even if CLEAN
        entries = load_manifest(self.work)
        decisions = {
            e.image_id: Decision(
                image_id=e.image_id,
                batch=e.batch,
                status="CLEAN",
                pass_number=1,
                timestamp="t",
                reviewer="r",
                mode="single",
                grid_size=1,
                tool_version="0.1",
            )
            for e in entries
        }
        notes = unreviewed_content(load_notes(self.work, required=True), frozenset())
        rows = export_rows(entries, decisions, load_skipped(self.work), notes)
        self.assertEqual(
            [(r.image_id, r.status, r.reason) for r in rows],
            [
                (dicom, "NOT_REVIEWED", f"unreviewed embedded content: icon-image; {AMBIGUOUS_ICON}"),
                (f"{dicom}#icon", "NOT_REVIEWED", AMBIGUOUS_ICON),
            ],
        )

    def test_dicom_with_icon_is_one_row(self):
        _dicom_with_icon(self.src / "a.dcm")
        result = self.run_quietly()
        dicom = (self.src / "a.dcm").as_posix()
        self.assertEqual([r["image_id"] for r in _read_tsv(self.work / "manifest.tsv")], [dicom])
        self.assertEqual(result.skipped, [])
        self.assertFalse((self.work / preprocess_module.PENDING_NAME).exists())

    def test_same_file_through_overlapping_sources_is_not_a_collision(self):
        (self.src / "a.png").write_bytes(_solid_png(0))
        result = self.run_quietly(self.src, self.src / "a.png")
        ids = [r["image_id"] for r in _read_tsv(self.work / "manifest.tsv")]
        self.assertEqual(ids, [(self.src / "a.png").as_posix()] * 2)
        self.assertEqual(result.skipped, [])

    def test_colliding_ids_are_those_naming_two_source_and_jpg_pairs(self):
        images = [
            ("a", "s1", "j1"), ("a", "s1", "j2"),  # different JPGs
            ("b", "s1", "j1"), ("b", "s2", "j1"),  # same JPG from different sources
            ("c", "s1", "j1"), ("c", "s1", "j1"),  # the same source reached twice
            ("d", "s3", "j3"),
        ]  # fmt: skip
        self.assertEqual(colliding_ids(images), frozenset({"a", "b"}))
        self.assertEqual(colliding_ids([]), frozenset())

    def test_different_files_rendering_identically_collide(self):
        pixels = RNG.integers(0, 255, (40, 60, 3), dtype=np.uint8)
        plain = _png_bytes(pixels)
        buf = io.BytesIO()
        Image.fromarray(pixels).save(buf, "PNG", compress_level=1, pnginfo=_png_text("note", "other bytes"))
        archive = self.src / "z.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.png", plain)
        (self.src / "z.zip::a.png").write_bytes(buf.getvalue())
        self.assertNotEqual(plain, buf.getvalue())
        rendered = [render("raster", "x", data, "gray").rgb for data in (plain, buf.getvalue())]
        self.assertTrue(np.array_equal(*rendered))
        self.run_quietly()
        image_id = f"{archive.as_posix()}::a.png"
        self.assert_collision([image_id, image_id], kept=[], inputs=2)

    def test_kept_images_are_renumbered_around_a_collision(self):
        (self.src / "a.png").write_bytes(_solid_png(10))
        archive = self.src / "b.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("x.png", _solid_png(20))
        (self.src / "b.zip::x.png").write_bytes(_solid_png(30))
        (self.src / "c.png").write_bytes(_solid_png(40))
        (self.src / "d.png").write_bytes(_solid_png(50))
        with quiet():
            result = run_preprocess([self.src], self.work, batch_size=1)
        manifest = _read_tsv(self.work / "manifest.tsv")
        self.assertEqual(
            [(r["preprocessed_path"], r["image_id"]) for r in manifest],
            [
                ("batch_001/img_00001.jpg", (self.src / "a.png").as_posix()),
                ("batch_002/img_00001.jpg", (self.src / "c.png").as_posix()),
                ("batch_003/img_00001.jpg", (self.src / "d.png").as_posix()),
            ],
        )
        self.assertEqual(result.batches, 3)
        for row in manifest:
            data = (self.work / row["preprocessed_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["jpeg_sha256"])
        self.assert_no_orphans(manifest)


class ProvenanceTest(unittest.TestCase):
    """preprocess.json records how the work dir was made; manifest.tsv records source and JPG hashes."""

    def setUp(self):
        self.root = temp_dir(self).resolve()
        self.src = self.root / "src"
        self.src.mkdir()
        _dicom_with_icon(self.src / "icon.dcm")
        write_dicom(self.src / "plain.dcm", _good_pixels())
        (self.src / "notes.txt").write_text("not an image")  # ignored
        (self.src / "broken.dcm").write_bytes(b"\xff\xd8\xff truncated jpeg")  # failed
        self.entry = _png_bytes(RNG.integers(0, 255, (20, 30, 3), dtype=np.uint8))
        self.archive = self.root / "scans.zip"
        with zipfile.ZipFile(self.archive, "w") as zf:
            zf.writestr("dir/scan.png", self.entry)
        self.work = self.root / "work"
        with quiet():
            self.result = run_preprocess([self.src, self.archive], self.work, batch_size=2, colormap="gray")
        self.manifest = {r["image_id"]: r for r in _read_tsv(self.work / "manifest.tsv")}

    def test_preprocess_json_records_parameters_and_counts(self):
        record = json.loads((self.work / "preprocess.json").read_text())
        self.assertEqual(record["tool_version"], preprocess_module.package_version())
        created = datetime.strptime(record["created"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self.assertLess(abs((datetime.now(UTC) - created).total_seconds()), 600)
        self.assertEqual(record["sources"], [self.src.as_posix(), self.archive.as_posix()])
        self.assertEqual(
            record["parameters"],
            {
                "batch_size": 2,
                "colormap": "gray",
                "clahe_kernel_size": preprocess_module.CLAHE_KERNEL_SIZE,
                "outlier_percentile": preprocess_module.OUTLIER_PERCENTILE,
                "intensity_margin": preprocess_module.INTENSITY_MARGIN,
                "range_trim": preprocess_module.RANGE_TRIM,
                "jpeg_quality": preprocess_module.JPEG_QUALITY,
                "jpeg_subsampling": preprocess_module.JPEG_SUBSAMPLING,
                "draw_overlays": False,
                "access": "private",
                "jobs": 1,
                "max_input_bytes": preprocess_module.MAX_INPUT_BYTES,
                "max_render_bytes": preprocess_module.MAX_RENDER_BYTES,
                "render_bytes_per_pixel": dict(preprocess_module.RENDER_BYTES_PER_PIXEL),
            },
        )
        self.assertLessEqual(
            {"pydicom", "numpy", "scikit-image", "Pillow", "matplotlib", "imagecodecs", "tifffile"},
            set(record["libraries"]),
        )
        self.assertEqual(record["libraries"]["pydicom"], pydicom.__version__)
        self.assertEqual(
            record["counts"],
            {
                "inputs": 5,
                "written": 3,
                "skipped_failed": 1,
                "skipped_ignored": 1,
                "notes": {**NO_NOTES, "icon-image": 1},
            },
        )
        self.assertEqual((self.result.found, self.result.written), (5, 3))
        self.assertEqual(stat.S_IMODE((self.work / "preprocess.json").stat().st_mode), 0o600)

    def test_preprocess_json_follows_group_policy(self):
        work = self.root / "shared"
        with quiet():
            run_preprocess([self.src], work, access="group")
        self.assertEqual(stat.S_IMODE((work / "preprocess.json").stat().st_mode), 0o660)
        self.assertEqual(json.loads((work / "preprocess.json").read_text())["parameters"]["access"], "group")

    def test_source_sha256_of_a_file_is_of_the_file(self):
        main = (self.src / "icon.dcm").as_posix()
        expected = hashlib.sha256((self.src / "icon.dcm").read_bytes()).hexdigest()
        self.assertEqual(self.manifest[main]["source_sha256"], expected)
        plain = (self.src / "plain.dcm").as_posix()
        self.assertEqual(
            self.manifest[plain]["source_sha256"], hashlib.sha256((self.src / "plain.dcm").read_bytes()).hexdigest()
        )

    def test_source_sha256_of_a_zip_entry_is_of_the_entry(self):
        row = self.manifest[f"{self.archive.as_posix()}::dir/scan.png"]
        self.assertEqual(row["source_sha256"], hashlib.sha256(self.entry).hexdigest())
        self.assertNotEqual(row["source_sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_jpeg_sha256_matches_the_file(self):
        self.assertEqual(len(self.manifest), 3)
        for row in self.manifest.values():
            data = (self.work / row["preprocessed_path"]).read_bytes()
            self.assertEqual(row["jpeg_sha256"], hashlib.sha256(data).hexdigest())

    def test_source_is_read_once(self):
        reads = []

        def counting(path: Path, size: int) -> bytes:
            reads.append(path)
            return real(path, size)

        real = preprocess_module._read_input
        with mock.patch.object(preprocess_module, "_read_input", counting), quiet():
            run_preprocess([self.src / "icon.dcm"], self.root / "once")
        self.assertEqual(reads, [self.src / "icon.dcm"])

    def test_manifest_loads_with_hashes(self):
        from image_review.store import load_manifest

        entries = {e.image_id: e for e in load_manifest(self.work)}
        for image_id, row in self.manifest.items():
            self.assertEqual(
                (entries[image_id].source_sha256, entries[image_id].jpeg_sha256),
                (row["source_sha256"], row["jpeg_sha256"]),
            )


class NotesTest(unittest.TestCase):
    """notes.tsv lists what written inputs hold that their review images do not show; it never fails an input."""

    def setUp(self):
        self.root = temp_dir(self).resolve()
        self.src = self.root / "src"
        self.src.mkdir()
        self.work = self.root / "work"

    def run_quietly(self, **kwargs):
        with quiet():
            return run_preprocess([self.src], self.work, **kwargs)

    def notes(self) -> list[list[str]]:
        with open(self.work / "notes.tsv", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f, delimiter="\t"))
        self.assertEqual(rows[0], ["image_id", "kind", "detail"])
        return rows[1:]

    def written(self) -> list[str]:
        return [r["image_id"] for r in _read_tsv(self.work / "manifest.tsv")]

    def dicom_with_icons(self, *items: pydicom.Dataset) -> str:
        path = self.src / "a.dcm"
        write_dicom(path, _good_pixels(), IconImageSequence=list(items))
        return path.as_posix()

    def test_transparent_raster_is_noted_with_its_pixel_counts(self):
        alpha = np.full((40, 60), 255, dtype=np.uint8)
        alpha[:10, :6] = 0
        alpha[10:20, :6] = 100
        rgba = np.dstack([np.zeros((40, 60, 3), dtype=np.uint8), alpha])
        (self.src / "a.png").write_bytes(_png_bytes(rgba, "RGBA"))
        (self.src / "b.png").write_bytes(_png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)))  # opaque: no note
        result = self.run_quietly()
        self.assertEqual(self.notes(), [[(self.src / "a.png").as_posix(), "alpha", "120 of 2400 pixels not opaque"]])
        self.assertEqual(result.notes, {**NO_NOTES, "alpha": 1})

    def test_embedded_thumbnail_is_noted_and_the_input_still_written(self):
        exif = _exif_with_thumbnail()
        thumb_len = len(exif) - exif.index(b"\xff\xd8")
        Image.new("RGB", (60, 40), (9, 9, 9)).save(self.src / "a.jpg", exif=exif)
        Image.new("RGB", (60, 40), (9, 9, 9)).save(self.src / "b.jpg")  # plain: no note
        result = self.run_quietly()
        self.assertEqual(
            self.notes(),
            [[(self.src / "a.jpg").as_posix(), "embedded-thumbnail", f"EXIF IFD1 thumbnail ({thumb_len} bytes)"]],
        )
        self.assertEqual(result.notes, {**NO_NOTES, "embedded-thumbnail": 1})
        self.assertEqual(len(self.written()), 2)

    def test_unreadable_exif_is_noted_and_the_input_still_written(self):
        Image.new("RGB", (60, 40), (9, 9, 9)).save(self.src / "a.jpg", exif=_exif_with_thumbnail()[:42])  # IFD1 cut off
        Image.new("RGB", (60, 40), (9, 9, 9)).save(
            self.src / "b.png", exif=b"Exif\x00\x00XXXXXXXXXXXX"
        )  # no TIFF header
        result = self.run_quietly()
        self.assertEqual(
            self.notes(),
            [
                [(self.src / "a.jpg").as_posix(), "embedded-thumbnail", "EXIF unreadable: corrupt"],
                [(self.src / "b.png").as_posix(), "embedded-thumbnail", "EXIF unreadable: SyntaxError"],
            ],
        )
        self.assertEqual(result.notes, {**NO_NOTES, "embedded-thumbnail": 2})
        self.assertEqual(len(self.written()), 2)

    def test_trailing_data_is_noted_and_the_input_still_written(self):
        first, second = (
            _jpeg_bytes(np.zeros((8, 8, 3), dtype=np.uint8)),
            _jpeg_bytes(np.ones((8, 8, 3), dtype=np.uint8)),
        )
        (self.src / "a.jpg").write_bytes(first + second)
        (self.src / "b.jpg").write_bytes(first)  # plain: no note
        result = self.run_quietly()
        self.assertEqual(
            self.notes(),
            [
                [
                    (self.src / "a.jpg").as_posix(),
                    "trailing-data",
                    f"{len(second)} bytes after the JPEG end; they begin with a JPEG signature",
                ]
            ],
        )
        self.assertEqual(result.notes, {**NO_NOTES, "trailing-data": 1})
        self.assertEqual(len(self.written()), 2)
        self.assertEqual(result.skipped, [])

    def test_export_denies_a_nonzero_trailing_tail_and_allows_an_all_zero_one(self):
        first = _jpeg_bytes(np.zeros((8, 8, 3), dtype=np.uint8))
        (self.src / "zero.jpg").write_bytes(first + bytes(16))
        (self.src / "tail.jpg").write_bytes(first + _jpeg_bytes(np.ones((8, 8, 3), dtype=np.uint8)))
        self.run_quietly()
        self.assertEqual(sorted(kind for _, kind, _ in self.notes()), ["trailing-data", "trailing-data"])
        with LocalStore(self.work) as store:
            mark(store, [r.key for r in store.manifest()], "CLEAN")
        report = self.root / "report.tsv"
        exported = invoke_cli("export", "--work-dir", str(self.work), "--report", str(report))
        self.assertEqual(exported.exit_code, 0, exported.output)
        self.assertEqual(
            [line.split("\t")[1] for line in exported.stdout.splitlines()[1:]], [(self.src / "zero.jpg").as_posix()]
        )
        self.assertEqual(
            [line.split("\t")[:2] + line.split("\t")[5:6] for line in report.read_text().splitlines()[1:]],
            [[(self.src / "tail.jpg").as_posix(), "NOT_REVIEWED", "unreviewed embedded content: trailing-data"]],
        )

    def test_overlay_plane_is_noted_and_counted(self):
        _dicom_with_overlays(self.src / "a.dcm", 0x6000)
        result = self.run_quietly()
        self.assertEqual(self.notes(), [[(self.src / "a.dcm").as_posix(), "overlay-plane", "group 0x6000"]])
        self.assertEqual(result.notes, {**NO_NOTES, "overlay-plane": 1})
        self.assertEqual(result.notes_path, self.work / "notes.tsv")
        record = json.loads((self.work / "preprocess.json").read_text())
        self.assertEqual(record["counts"]["notes"], {**NO_NOTES, "overlay-plane": 1})

    def test_undecodable_overlay_is_written_with_its_note_by_default_and_fails_with_the_flag(self):
        def corrupt(path: Path) -> None:
            ds = pydicom.dcmread(path)
            ds[0x6000, 0x3000].value = b"\x00\x00"
            ds.save_as(path, enforce_file_format=True)

        path = self.src / "a.dcm"
        _dicom_with_overlays(path, 0x6000)
        corrupt(path)
        result = self.run_quietly()
        self.assertEqual(result.written, 1)
        self.assertEqual(self.notes(), [[path.as_posix(), "overlay-plane", "group 0x6000"]])
        self.assertEqual(json.loads((self.work / "preprocess.json").read_text())["parameters"]["draw_overlays"], False)
        shutil.rmtree(self.work)
        result = self.run_quietly(draw_overlays=True)
        self.assertEqual(result.written, 0)
        [skipped] = result.skipped
        self.assertIn("overlay 0x6000 cannot be decoded", skipped.reason)
        self.assertEqual(json.loads((self.work / "preprocess.json").read_text())["parameters"]["draw_overlays"], True)

    def test_several_overlay_planes_are_one_note(self):
        _dicom_with_overlays(self.src / "a.dcm", 0x6000, 0x6002)
        self.run_quietly()
        self.assertEqual(self.notes(), [[(self.src / "a.dcm").as_posix(), "overlay-plane", "groups 0x6000, 0x6002"]])

    def test_icon_is_noted_without_decoding_its_pixels(self):
        _dicom_with_icon(self.src / "a.dcm")
        decoded = []
        real = pydicom.Dataset.pixel_array

        def counting(ds):
            decoded.append(ds)
            return real.fget(ds)

        with mock.patch.object(pydicom.Dataset, "pixel_array", property(counting)):
            result = self.run_quietly()
        self.assertEqual(len(decoded), 1)  # the input's own pixel data only
        self.assertEqual(
            self.notes(), [[(self.src / "a.dcm").as_posix(), "icon-image", "IconImageSequence: 8x8 MONOCHROME2"]]
        )
        self.assertEqual(result.notes, {**NO_NOTES, "icon-image": 1})

    def test_icon_lacking_its_size_is_noted_with_question_marks(self):
        icon = pydicom.Dataset()
        icon.SamplesPerPixel = 1
        image_id = self.dicom_with_icons(icon, icon)
        self.run_quietly()
        self.assertEqual(self.written(), [image_id])
        self.assertEqual(self.notes(), [[image_id, "icon-image", "IconImageSequence: ?x? ?, 2 items"]])

    def test_icon_detail_is_one_tsv_safe_line(self):
        icon = pydicom.Dataset()
        icon.Rows, icon.Columns = 4, 6
        with quiet():  # pydicom warns that the value is not a valid CS
            icon.PhotometricInterpretation = "MONO\tCHROME\n2"
            image_id = self.dicom_with_icons(icon)
        self.run_quietly()
        self.assertEqual(self.notes(), [[image_id, "icon-image", "IconImageSequence: 4x6 MONO CHROME 2"]])

    def test_unreadable_icon_is_noted_and_the_input_still_renders(self):
        _dicom_with_icon(self.src / "a.dcm")
        with mock.patch.object(pydicom.sequence.Sequence, "__getitem__", side_effect=ValueError("bad item")):
            result = self.run_quietly()
        image_id = (self.src / "a.dcm").as_posix()
        self.assertEqual(self.written(), [image_id])
        self.assertEqual(result.skipped, [])
        self.assertEqual(self.notes(), [[image_id, "icon-image", "IconImageSequence: unreadable (ValueError)"]])

    def test_icon_with_a_malformed_element_is_noted_and_the_input_still_renders(self):
        path = self.src / "a.dcm"
        _dicom_with_icon(path)
        data = bytearray(path.read_bytes())
        # Give the icon's Rows (US) a value length of 3, growing the sequence and item lengths to match
        sequence = data.find(b"\x88\x00\x00\x02SQ\x00\x00")
        item = sequence + 12
        self.assertEqual(data[item : item + 4], b"\xfe\xff\x00\xe0")
        rows = data.find(b"\x28\x00\x10\x00US\x02\x00", item)
        for offset in (sequence + 8, item + 4):
            data[offset : offset + 4] = struct.pack("<I", struct.unpack("<I", data[offset : offset + 4])[0] + 1)
        data[rows + 6 : rows + 8] = b"\x03\x00"
        data[rows + 10 : rows + 10] = b"\x00"
        path.write_bytes(bytes(data))
        result = self.run_quietly()
        self.assertEqual(self.written(), [path.as_posix()])
        self.assertEqual(result.skipped, [])
        self.assertEqual(
            self.notes(), [[path.as_posix(), "icon-image", "IconImageSequence: unreadable (BytesLengthException)"]]
        )

    def test_long_icon_values_are_truncated(self):
        icon = pydicom.Dataset()
        icon.Rows, icon.Columns = 4, 6
        cut = "M" * preprocess_module.NOTE_VALUE_MAX + "…"
        # 1000 characters stay a CS string; 200000 overflow explicit VR's 16-bit length, so pydicom stores UN bytes
        for length, expected in ((1000, cut), (200_000, "b'" + cut[2:])):
            with self.subTest(length=length):
                self.work = self.root / f"work{length}"
                with quiet():  # pydicom warns that the value is not a valid CS
                    icon.PhotometricInterpretation = "M" * length
                    image_id = self.dicom_with_icons(icon)
                self.run_quietly()
                self.assertEqual(self.notes(), [[image_id, "icon-image", f"IconImageSequence: 4x6 {expected}"]])

    def test_notes_follow_manifest_order_and_kind_order(self):
        _dicom_with_icon(self.src / "a.dcm")
        (self.src / "b.png").write_bytes(_solid_png(10))
        path = self.src / "c.dcm"
        _dicom_with_icon(path)
        ds = pydicom.dcmread(path)
        add_overlay(ds, np.ones((8, 8), dtype=bool))
        ds.save_as(path, enforce_file_format=True)
        self.run_quietly()
        a, c = (self.src / "a.dcm").as_posix(), path.as_posix()
        self.assertEqual(self.written(), [a, (self.src / "b.png").as_posix(), c])
        icon = "IconImageSequence: 8x8 MONOCHROME2"
        self.assertEqual(
            self.notes(),
            [[a, "icon-image", icon], [c, "overlay-plane", "group 0x6000"], [c, "icon-image", icon]],
        )

    def test_one_input_with_every_kind_lists_them_in_kind_order_from_one_decode(self):
        path = self.src / "a.dcm"
        _dicom_with_icon(path)
        ds = pydicom.dcmread(path)
        ds.PixelData = _with_unused_bit(ds.pixel_array, 15).tobytes()
        ds.BitsStored, ds.HighBit = 12, 11
        add_overlay(ds, np.ones((8, 8), dtype=bool))
        ds.save_as(path, enforce_file_format=True)
        decoded = []
        real = pydicom.Dataset.pixel_array

        def counting(ds):
            decoded.append(ds)
            return real.fget(ds)

        with mock.patch.object(pydicom.Dataset, "pixel_array", property(counting)):
            result = self.run_quietly()
        self.assertEqual(len(decoded), 1)  # the unused bits come from the decode that rendered the input
        self.assertEqual(
            self.notes(),
            [
                [path.as_posix(), "overlay-plane", "group 0x6000"],
                [path.as_posix(), "overlay-unused-bits", "bit 15"],
                [path.as_posix(), "icon-image", "IconImageSequence: 8x8 MONOCHROME2"],
            ],
        )
        self.assertEqual(list(result.notes), list(preprocess_module.NOTE_KINDS))
        self.assertEqual(result.notes, {**NO_NOTES, "overlay-plane": 1, "overlay-unused-bits": 1, "icon-image": 1})

    def test_empty_icon_sequence_is_not_noted(self):
        self.dicom_with_icons()
        self.run_quietly()
        self.assertEqual(self.notes(), [])

    def test_nothing_notable_writes_a_header_only_file_with_the_policy_mode(self):
        write_dicom(self.src / "a.dcm", _good_pixels())
        (self.src / "b.png").write_bytes(_solid_png(10))
        for access, mode in (("private", 0o600), ("group", 0o660)):
            with self.subTest(access=access):
                self.work = self.root / access
                result = self.run_quietly(access=access)
                self.assertEqual((self.work / "notes.tsv").read_text(encoding="utf-8"), "image_id\tkind\tdetail\n")
                self.assertEqual(stat.S_IMODE((self.work / "notes.tsv").stat().st_mode), mode)
                self.assertEqual(result.notes, NO_NOTES)
                record = json.loads((self.work / "preprocess.json").read_text())
                self.assertEqual(record["counts"]["notes"], NO_NOTES)

    def test_failed_and_colliding_inputs_have_no_notes(self):
        write_dicom(self.src / "multi.dcm", RNG.integers(0, 4000, (3, 32, 32), dtype=np.uint16))
        ds = pydicom.dcmread(self.src / "multi.dcm")
        add_overlay(ds, np.ones((8, 8), dtype=bool))
        ds.save_as(self.src / "multi.dcm", enforce_file_format=True)
        _dicom_with_overlays(self.src / "z.zip::a.dcm", 0x6000)
        _dicom_with_icon(self.root / "a.dcm")
        with zipfile.ZipFile(self.src / "z.zip", "w") as zf:
            zf.write(self.root / "a.dcm", "a.dcm")
        result = self.run_quietly()
        self.assertEqual(self.written(), [])
        self.assertEqual(len(result.skipped), 3)
        self.assertEqual(self.notes(), [])
        self.assertEqual(result.notes, NO_NOTES)


class UnusedBitsTest(unittest.TestCase):
    """Bits above BitsStored are masked before rendering, for every transfer syntax, and noted when they held content."""

    def dataset(self, pixels: np.ndarray, photometric: str = "MONOCHROME2", bits_stored: int = 12) -> pydicom.Dataset:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.dcm"
            write_dicom(path, pixels, photometric)
            ds = pydicom.dcmread(path)
        ds.BitsStored, ds.HighBit = bits_stored, bits_stored - 1
        return ds

    def rendered(self, ds: pydicom.Dataset) -> tuple[bytes, tuple[Note, ...]]:
        """The JPG and notes `ds` renders to, through its saved bytes."""
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        out = render("dicom", "id", buf.getvalue(), "inferno")
        return encode_jpeg(out.rgb), out.notes

    def test_native_unused_bits_are_noted_and_not_rendered(self):
        clean, notes = self.rendered(self.dataset(_text_pixels()))
        self.assertEqual(notes, ())
        jpg, notes = self.rendered(self.dataset(_with_unused_bit(_text_pixels(), 15)))
        self.assertEqual(notes, (Note("overlay-unused-bits", "bit 15"),))
        self.assertEqual(jpg, clean)
        jpg, notes = self.rendered(self.dataset(_with_unused_bit(_with_unused_bit(_text_pixels(), 15), 12)))
        self.assertEqual(notes, (Note("overlay-unused-bits", "bits 12, 15"),))
        self.assertEqual(jpg, clean)

    def test_rle_unused_bit_is_noted_and_not_rendered(self):
        clean, _ = self.rendered(self.dataset(_text_pixels()))
        ds = self.dataset(_with_unused_bit(_text_pixels(), 15))
        ds.compress(RLELossless, encoding_plugin="pydicom")  # from the dataset's bytes: no range check on the values
        jpg, notes = self.rendered(ds)
        self.assertEqual(notes, (Note("overlay-unused-bits", "bit 15"),))
        self.assertEqual(jpg, clean)

    def test_jpeg_2000_renders_as_the_masked_native_file(self):
        if not pydicom.pixels.get_encoder(JPEG2000Lossless).is_available:
            self.skipTest("no JPEG 2000 encoder")
        clean, _ = self.rendered(self.dataset(_text_pixels()))
        # Compressed at 16 bits, then marked 12: the codestream keeps bit 15, which pydicom does not clear for J2K
        ds = self.dataset(_with_unused_bit(_text_pixels(), 15), bits_stored=16)
        ds.compress(JPEG2000Lossless)
        ds.BitsStored, ds.HighBit = 12, 11
        jpg, notes = self.rendered(ds)
        self.assertEqual(notes, (Note("overlay-unused-bits", "bit 15"),))
        self.assertEqual(jpg, clean)

    def test_overlay_group_whose_bit_position_holds_content_is_named(self):
        ds = self.dataset(_with_unused_bit(_text_pixels(), 15))
        ds.add_new((0x6000, 0x0102), "US", 15)  # a retired overlay in PixelData: no OverlayData
        ds.add_new((0x6002, 0x0102), "US", 14)  # names a bit that holds nothing
        add_overlay(ds, np.ones((8, 8), dtype=bool), group=0x6004)
        ds[0x6004, 0x0102].value = 15  # has OverlayData: an overlay plane, not the bit's
        _, notes = self.rendered(ds)
        self.assertEqual(
            notes, (Note("overlay-plane", "group 0x6004"), Note("overlay-unused-bits", "bit 15 (overlay 0x6000)"))
        )

    def test_signed_sign_extension_is_not_content(self):
        pixels = _text_pixels().astype(np.int16) - 2048  # negatives, sign-extended to 16 bits, and a positive bar
        before, _ = self.rendered(self.dataset(pixels, bits_stored=16))
        jpg, notes = self.rendered(self.dataset(pixels))
        self.assertEqual(notes, ())
        self.assertEqual(jpg, before)
        zero_filled = (pixels.view(np.uint16) & 0x0FFF).view(np.int16)  # negatives with their unused bits cleared
        jpg, notes = self.rendered(self.dataset(zero_filled))
        self.assertEqual(notes, ())
        self.assertEqual(jpg, before)
        stray = _with_unused_bit(pixels, 13)  # set under the positive bar: not its sign
        stray[0, 0] &= ~np.int16(1 << 14)  # cleared in a negative value: not its sign either
        jpg, notes = self.rendered(self.dataset(stray))
        self.assertEqual(notes, (Note("overlay-unused-bits", "bits 13, 14"),))
        self.assertEqual(jpg, before)

    def test_palette_indices_are_masked_before_the_lookup(self):
        def palette(indices: np.ndarray) -> pydicom.Dataset:
            ds = self.dataset(indices, "PALETTE COLOR", bits_stored=8)
            ramp = np.arange(256, dtype=np.uint16) * 257
            for element, lut in ((0x1101, ramp), (0x1102, ramp[::-1]), (0x1103, ramp // 2)):  # red, green, blue
                ds.add_new((0x0028, element), "US", [256, 0, 16])
                ds.add_new((0x0028, element + 0x100), "OW", lut.astype("<u2").tobytes())
            return ds

        indices = np.random.default_rng(3).integers(0, 200, (40, 60)).astype(np.uint16)
        clean, notes = self.rendered(palette(indices))
        self.assertEqual(notes, ())
        jpg, notes = self.rendered(palette(_with_unused_bit(indices, 12)))  # unmasked, these indices are off the LUT
        self.assertEqual(notes, (Note("overlay-unused-bits", "bit 12"),))
        self.assertEqual(jpg, clean)

    def test_split_matches_a_reference_mask_for_every_integer_dtype(self):
        def reference(raw: np.ndarray, bits_stored: int) -> tuple[np.ndarray, tuple[int, ...]]:
            width = raw.dtype.itemsize * 8
            if bits_stored >= width:
                return raw, ()
            signed = raw.dtype.kind == "i"
            masked, differing = [], 0
            for value in raw.view(f"u{raw.dtype.itemsize}").ravel().tolist():
                low = value & ((1 << bits_stored) - 1)
                negative = signed and low >> (bits_stored - 1) & 1
                extended = low | ((1 << width) - (1 << bits_stored)) if negative else low  # as unsigned
                masked.append(extended)
                if value >> bits_stored not in (0, extended >> bits_stored):
                    differing |= value ^ extended
            out = np.array(masked, dtype=f"u{raw.dtype.itemsize}").view(raw.dtype).reshape(raw.shape)
            return out, tuple(bit for bit in range(bits_stored, width) if differing >> bit & 1)

        rng = np.random.default_rng(11)
        for dtype in (np.uint8, np.uint16, np.uint32, np.int8, np.int16, np.int32):
            width = np.dtype(dtype).itemsize * 8
            unsigned = f"u{np.dtype(dtype).itemsize}"
            for bits_stored in (1, width - 1, width):
                full = rng.integers(0, 1 << width, (6, 7), dtype=np.uint64).astype(unsigned).view(dtype)
                low = (full.view(unsigned) & ((1 << bits_stored) - 1)).view(dtype)  # unsigned or zero-filled
                extended, _ = reference(full, bits_stored)  # sign-extended (signed) or cleared (unsigned)
                for name, raw in (("full", full), ("zero-filled", low), ("sign-extended", extended)):
                    with self.subTest(dtype=np.dtype(dtype).name, bits_stored=bits_stored, raw=name):
                        masked, positions = preprocess_module._split_unused_bits(raw, bits_stored)
                        expected_masked, expected_positions = reference(raw, bits_stored)
                        np.testing.assert_array_equal(masked, expected_masked)
                        self.assertEqual(masked.dtype, raw.dtype)
                        self.assertEqual(positions, expected_positions)
                        if name != "full":
                            self.assertEqual(positions, ())

    def test_pixels_with_no_unused_bits_are_untouched(self):
        pixels = _with_unused_bit(_text_pixels(), 15)
        _, notes = self.rendered(self.dataset(pixels, bits_stored=16))
        self.assertEqual(notes, ())
        masked, _ = self.rendered(self.dataset(pixels))
        self.assertNotEqual(self.rendered(self.dataset(pixels, bits_stored=16))[0], masked)  # bit 15 is a value bit


class ClassifyTest(unittest.TestCase):
    def test_content_decides_and_name_only_breaks_ties(self):
        preamble_dicom = bytes(128) + b"DICM"
        bmp = b"BM" + bytes(12) + (40).to_bytes(4, "little")
        cases = [
            ("IM0001", preamble_dicom, "dicom"),
            ("notes.txt", preamble_dicom, "dicom"),
            ("A.ZIP", b"PK\x03\x04rest", "zip"),
            ("scan.dcm", b"PK\x03\x04rest", "zip"),
            ("empty.zip", b"PK\x05\x06" + bytes(18), "zip"),
            ("d.PNG", PNG_MAGIC, "raster"),
            ("e", b"\xff\xd8\xff\xe0", "raster"),
            ("t", b"II*\x00", "raster"),
            ("t", b"MM\x00*", "raster"),
            ("t", b"II+\x00", "raster"),
            ("t", b"MM\x00+", "raster"),
            ("b", bmp, "raster"),
            ("g", b"GIF89a", "raster"),
            ("w", b"RIFF\x10\x00\x00\x00WEBPVP8 ", "raster"),
            ("j", b"\x00\x00\x00\x0cjP  \r\n\x87\n", "raster"),
            ("j", b"\xff\x4f\xff\x51", "raster"),
            ("p", b"P5\n20 20\n255\n", "raster"),
            ("bare.DCM", b"\x20\x08\x00\x05", "dicom"),
            ("bare.dicom", b"junk", "dicom"),
            ("bare.ima", b"junk", "dicom"),
            ("IM0003", b"\x08\x00\x16\x00\x1a\x00\x00\x001.2.840", "dicom"),  # implicit VR
            ("I.001", b"\x08\x00\x05\x00CS\x0a\x00ISO_IR 100", "dicom"),  # explicit VR
            ("meta", b"\x02\x00\x00\x00UL\x04\x00", "dicom"),
            ("__MACOSX/._x.dcm", APPLEDOUBLE, Rejected("ignored", "AppleDouble metadata (macOS resource fork)")),
            ("x.png", APPLEDOUBLE, Rejected("ignored", "AppleDouble metadata (macOS resource fork)")),
            ("/src/._x.JPG", b"\x00\x00", Rejected("ignored", "AppleDouble metadata (macOS resource fork)")),
            ("__MACOSX/sub/x.dcm", b"\x00\x00", Rejected("ignored", "AppleDouble metadata (macOS resource fork)")),
            ("__MACOSX/sub/x.png", PNG_MAGIC, "raster"),
            ("notes.txt", b"hello", Rejected("ignored", "not an image (unrecognized content)")),
            ("empty", b"", Rejected("ignored", "not an image (unrecognized content)")),
            ("BMnotes", b"BM is not a bitmap header", Rejected("ignored", "not an image (unrecognized content)")),
            ("P5x", b"P5x", Rejected("ignored", "not an image (unrecognized content)")),
            ("odd", b"\x08\x00\x01\x00\x05\x00\x00\x00", Rejected("ignored", "not an image (unrecognized content)")),
            ("group", b"\x09\x00\x10\x00\x04\x00\x00\x00", Rejected("ignored", "not an image (unrecognized content)")),
            ("long", b"\x08\x00\x02\x00\x00\x10\x00\x00", Rejected("ignored", "not an image (unrecognized content)")),
            ("study.tar.gz", b"\x1f\x8b\x08\x00", Rejected("failed", "unsupported: gzip archive")),
            ("s", b"BZh91AY&SY", Rejected("failed", "unsupported: bzip2 archive")),
            ("BZhello", b"BZhello", Rejected("ignored", "not an image (unrecognized content)")),
            ("s", b"\xfd7zXZ\x00\x00", Rejected("failed", "unsupported: xz archive")),
            ("s", b"\x28\xb5\x2f\xfd", Rejected("failed", "unsupported: zstd archive")),
            ("s", b"7z\xbc\xaf\x27\x1c", Rejected("failed", "unsupported: 7z archive")),
            ("s", b"Rar!\x1a\x07\x00", Rejected("failed", "unsupported: rar archive")),
            ("series.TAR", b"series/" + bytes(20), Rejected("failed", "unsupported: tar archive")),
            ("s.tgz", b"junk", Rejected("failed", "unsupported: gzip archive")),
            ("photo", b"\x00\x00\x00\x18ftypheic", "raster"),
            ("photo", b"\x00\x00\x00\x1cftypavif", "raster"),
            ("movie", b"\x00\x00\x00\x18ftypisom", Rejected("ignored", "not an image (unrecognized content)")),
            ("photo.HEIC", b"junk", "raster"),
            ("photo.jpg", b"not a jpeg", "raster"),  # the decoder decides, and fails it
            ("/src/PHOTO.JPEG", b"", "raster"),
            ("x.webp", b"xx", "raster"),
            ("x.PPM", b"xx", "raster"),
            ("A.ZIP", b"<html>login</html>", Rejected("failed", "unrecognized content for a .zip file")),
        ]
        for name, head, expected in cases:
            with self.subTest(name=name, head=head[:8]):
                self.assertEqual(classify(name, head), expected)


class ContentDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self).resolve()

    def invoke(self, source: Path, work: Path, *args):
        with quiet():
            return invoke_cli("preprocess", str(source), "--work-dir", str(work), *args, env=CLI_ENV)

    def skipped_rows(self, work: Path) -> list[tuple[str, str, str]]:
        return [(r["image_id"], r["kind"], r["reason"]) for r in _read_tsv(work / "skipped.tsv")]

    def run_quietly(self, sources: list[Path]):
        with quiet():
            return run_preprocess(sources, self.root / "work")

    def test_source_directory_is_discovered_by_content(self):
        src = self.root / "src"
        (src / "sub").mkdir(parents=True)
        for name in ("a.dcm", "B.DCM", "IM0001", "c.dicom"):
            write_dicom(src / name, _good_pixels())
        (src / "d.PNG").write_bytes(_png_bytes(RNG.integers(0, 255, (40, 60, 3), dtype=np.uint8)))
        (src / "e.JPG").write_bytes(_jpeg_bytes(RNG.integers(0, 255, (40, 60, 3), dtype=np.uint8)))
        (src / "notes.txt").write_text("not an image")
        inner = src / "sub" / "inner.zip"
        with warnings.catch_warnings(), zipfile.ZipFile(inner, "w") as zf:
            warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
            zf.writestr("x.png", _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)))
            zf.writestr("x.png", _png_bytes(np.full((40, 60, 3), 255, dtype=np.uint8)))
            zf.writestr("__MACOSX/._x.dcm", APPLEDOUBLE)
        work = src / "review_work"  # the work dir and its staging dir are inside the source
        work.mkdir()  # an empty work dir is used, so the walk meets it

        result = self.invoke(src, work)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Found 10 inputs: wrote 8 images in 1 batches; 2 skipped (0 failed, 2 ignored;", result.output)
        manifest = [r["image_id"] for r in _read_tsv(work / "manifest.tsv")]
        z = f"{inner.as_posix()}::x.png"
        self.assertEqual(
            manifest,
            [(src / n).as_posix() for n in ("B.DCM", "IM0001", "a.dcm", "c.dicom", "d.PNG", "e.JPG")] + [z, f"{z}#2"],
        )
        self.assertEqual(
            self.skipped_rows(work),
            [
                ((src / "notes.txt").as_posix(), "ignored", "not an image (unrecognized content)"),
                (f"{inner.as_posix()}::__MACOSX/._x.dcm", "ignored", "AppleDouble metadata (macOS resource fork)"),
            ],
        )
        self.assertFalse((src / ".review_work.partial").exists())

    def test_excluded_directories_are_not_walked(self):
        src = self.root / "src"
        for d in ("review_work/batch_001", ".review_work.partial/batch_001", "keep"):
            (src / d).mkdir(parents=True)
            write_dicom(src / d / "img.dcm", _good_pixels())
        (src / "link_to_work").symlink_to(src / "review_work", target_is_directory=True)
        exclude = frozenset({src / "review_work", src / ".review_work.partial"})
        self.assertEqual(_discovered([src], exclude), [((src / "keep" / "img.dcm").as_posix(), "dicom")])

    def test_single_file_sources_are_classified_by_content(self):
        write_dicom(self.root / "X.DCM", _good_pixels())
        write_dicom(self.root / "IM0002", _good_pixels())
        write_dicom(self.root / "bare.ima", _good_pixels(), preamble=False)
        archive = self.root / "A.ZIP"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("scan", _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)))
        sources = [self.root / n for n in ("X.DCM", "IM0002", "bare.ima", "A.ZIP")]
        result = self.run_quietly(sources)
        self.assertEqual(result.skipped, [])
        self.assertEqual(
            [r["image_id"] for r in _read_tsv(self.root / "work" / "manifest.tsv")],
            [s.as_posix() for s in sources[:3]] + [f"{archive.as_posix()}::scan"],
        )

    def test_pillow_formats_render_whatever_their_name(self):
        rgb = RNG.integers(0, 255, (40, 60, 3), dtype=np.uint8)
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, "WEBP")
        (self.root / "x.webp").write_bytes(buf.getvalue())
        (self.root / "photo.jpg").write_bytes(buf.getvalue())  # a WebP saved with a .jpg name
        result = self.run_quietly([self.root / "x.webp", self.root / "photo.jpg"])
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.written, 2)

    def test_damaged_image_name_is_failed_and_exits_1(self):
        src = self.root / "src"
        src.mkdir()
        write_dicom(src / "a.dcm", _good_pixels())
        (src / "broken.JPG").write_bytes(b"\x00" * 50)  # e.g. a zero-filled download
        (src / "._a.dcm").write_bytes(b"\x00" * 50)
        work = self.root / "work"
        result = self.invoke(src, work)
        self.assertEqual(result.exit_code, 1, result.output)
        appledouble, broken = self.skipped_rows(work)
        self.assertEqual(
            appledouble, ((src / "._a.dcm").as_posix(), "ignored", "AppleDouble metadata (macOS resource fork)")
        )
        self.assertEqual(broken[:2], ((src / "broken.JPG").as_posix(), "failed"))
        self.assertTrue(broken[2].startswith("UnidentifiedImageError:"), broken[2])

    def test_skipped_tsv_is_reproducible(self):
        (self.root / "garbage.jpg").write_bytes(b"\x00" * 50)
        outputs = []
        for work in (self.root / "work1", self.root / "work2"):
            with quiet():
                run_preprocess([self.root / "garbage.jpg"], work)
            outputs.append((work / "skipped.tsv").read_bytes())
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn(b"cannot identify image file <data>", outputs[0])

    def test_zip_name_with_other_content_is_failed(self):
        src = self.root / "src"
        src.mkdir()
        for zip_path in (src / "study.zip", self.root / "download.ZIP"):
            zip_path.write_text("<html><body>Please log in</body></html>")
        for source, bad in ((src, src / "study.zip"), (self.root / "download.ZIP", self.root / "download.ZIP")):
            with self.subTest(source=source.name):
                work = self.root / f"work_{source.name}"
                result = self.invoke(source, work)
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertEqual(
                    self.skipped_rows(work), [(bad.as_posix(), "failed", "unrecognized content for a .zip file")]
                )

    def test_zip_without_files_is_one_ignored_row(self):
        empty, dirs_only = self.root / "empty.zip", self.root / "dirs.zip"
        with zipfile.ZipFile(empty, "w"):
            pass
        with zipfile.ZipFile(dirs_only, "w") as zf:
            zf.mkdir("a")
        result = self.run_quietly([empty, dirs_only])
        self.assertEqual(
            result.skipped,
            [SkippedRow(p.as_posix(), "ignored", "zip contains no files") for p in (empty, dirs_only)],
        )

    def test_unrecognized_file_named_as_source_is_failed(self):
        notes = self.root / "notes.txt"
        notes.write_text("not an image")
        result = self.invoke(notes, self.root / "work")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(
            self.skipped_rows(self.root / "work"), [(notes.as_posix(), "failed", "not an image (unrecognized content)")]
        )

    def test_archive_beside_dicoms_is_failed(self):
        src = self.root / "src"
        src.mkdir()
        write_dicom(src / "a.dcm", _good_pixels())
        (src / "series2.tar.gz").write_bytes(b"\x1f\x8b\x08\x00" + bytes(100))
        result = self.invoke(src, self.root / "work")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(
            self.skipped_rows(self.root / "work"),
            [((src / "series2.tar.gz").as_posix(), "failed", "unsupported: gzip archive")],
        )

    def test_fifo_is_ignored_in_a_directory_and_failed_when_named(self):
        src = self.root / "src"
        src.mkdir()
        fifo = src / "pipe"
        os.mkfifo(fifo)
        self.assertEqual(_discovered([src]), [(fifo.as_posix(), "ignored")])
        self.assertEqual(_discovered([fifo]), [(fifo.as_posix(), "failed")])

    def test_bare_dicom_is_recognized_without_a_dicom_suffix(self):
        names = ("IM0003", "I.001", "1.2.840.113619.2.55.3.123")
        for name in names:
            write_dicom(self.root / name, _good_pixels(), preamble=False)
        result = self.run_quietly([self.root / n for n in names])
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.written, 3)

    def test_dcm_name_with_other_content_fails_as_not_dicom(self):
        path = self.root / "x.dcm"
        path.write_text("<html><body>Please log in</body></html>")
        [skip] = self.run_quietly([path]).skipped
        self.assertEqual(skip.kind, "failed")
        self.assertFalse(skip.reason.startswith("unsupported:"), skip.reason)
        self.assertIn("not DICOM", skip.reason)

    def test_pydicom_no_meta_file_is_classified_as_dicom(self):
        path = get_testdata_file("no_meta.dcm", download=False)
        if path is None:
            self.skipTest("pydicom test file no_meta.dcm not available")
        # The bundled file is not renderable (it starts with a stray byte); it must still be read as DICOM.
        with quiet():
            [item] = list(discover([Path(path)]))
        self.assertIsInstance(item, Candidate)
        self.assertEqual(item.kind, "dicom")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read a mode-000 directory")
    def test_unreadable_subdirectory_is_a_failed_row(self):
        src = self.root / "src"
        locked = src / "locked"
        locked.mkdir(parents=True)
        write_dicom(src / "a.dcm", _good_pixels())
        write_dicom(locked / "b.dcm", _good_pixels())
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        work = self.root / "work"
        result = self.invoke(src, work)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("1 skipped (1 failed, 0 ignored;", result.output)
        [row] = _read_tsv(work / "skipped.tsv")
        self.assertEqual((row["image_id"], row["kind"]), (locked.as_posix(), "failed"))
        self.assertTrue(row["reason"].startswith("PermissionError:"), row["reason"])

    def test_symlinked_directory_outside_the_sources_is_failed(self):
        src = self.root / "src"
        elsewhere = self.root / "elsewhere"
        src.mkdir()
        elsewhere.mkdir()
        write_dicom(elsewhere / "a.dcm", _good_pixels())
        (src / "linked_dir").symlink_to(elsewhere, target_is_directory=True)
        (src / "linked.dcm").symlink_to(elsewhere / "a.dcm")  # symlinked files are read
        work = self.root / "work"
        result = self.invoke(src, work)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual([r["image_id"] for r in _read_tsv(work / "manifest.tsv")], [(src / "linked.dcm").as_posix()])
        self.assertEqual(
            self.skipped_rows(work),
            [
                (
                    (src / "linked_dir").as_posix(),
                    "failed",
                    f"symlinked directory not followed; pass its target {elsewhere} as a SOURCE",
                )
            ],
        )

    def test_link_to_an_enclosing_directory_ingests_nothing_from_siblings(self):
        studies = self.root / "studies"
        (studies / "A").mkdir(parents=True)
        (studies / "B").mkdir()
        write_dicom(studies / "A" / "a.dcm", _good_pixels())
        write_dicom(studies / "B" / "other_patient.dcm", _good_pixels())
        (studies / "A" / "up").symlink_to("..", target_is_directory=True)
        (studies / "A" / "root").symlink_to("/", target_is_directory=True)
        work = self.root / "work"
        result = self.invoke(studies / "A", work)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            [r["image_id"] for r in _read_tsv(work / "manifest.tsv")], [(studies / "A" / "a.dcm").as_posix()]
        )
        self.assertEqual(
            self.skipped_rows(work),
            [
                ((studies / "A" / "root").as_posix(), "ignored", "symlink to an enclosing directory"),
                ((studies / "A" / "up").as_posix(), "ignored", "symlink to an enclosing directory"),
            ],
        )

    def test_link_to_another_source_is_ignored_without_duplicates(self):
        s1, s2 = self.root / "S1", self.root / "S2"
        s1.mkdir()
        s2.mkdir()
        write_dicom(s1 / "a.dcm", _good_pixels())
        write_dicom(s2 / "b.dcm", _good_pixels())
        (s1 / "s2link").symlink_to(s2, target_is_directory=True)
        result = self.run_quietly([s1, s2])
        self.assertEqual(
            [r["image_id"] for r in _read_tsv(self.root / "work" / "manifest.tsv")],
            [(s1 / "a.dcm").as_posix(), (s2 / "b.dcm").as_posix()],
        )
        self.assertEqual(
            result.skipped,
            [
                SkippedRow(
                    (s1 / "s2link").as_posix(), "ignored", f"symlinked directory already included via SOURCE {s2}"
                )
            ],
        )

    def test_symlinked_directory_reason_keeps_a_trailing_space_in_the_source_path(self):
        s1, s2 = self.root / "S1", self.root / "S2 "
        s1.mkdir()
        s2.mkdir()
        (s1 / "s2link").symlink_to(s2, target_is_directory=True)
        self.run_quietly([s1, s2])
        self.assertEqual(
            self.skipped_rows(self.root / "work"),
            [((s1 / "s2link").as_posix(), "ignored", f"symlinked directory already included via SOURCE {s2}")],
        )

    def test_symlink_loop_terminates(self):
        src = self.root / "src"
        (src / "a").mkdir(parents=True)
        write_dicom(src / "a" / "x.dcm", _good_pixels())
        (src / "a" / "loop").symlink_to(src, target_is_directory=True)
        (src / "b").symlink_to(src / "a", target_is_directory=True)
        (src / "dangling").symlink_to(self.root / "missing", target_is_directory=True)
        self.assertEqual(
            _discovered([src]),
            [
                ((src / "b").as_posix(), "ignored"),  # inside the source being walked
                ((src / "dangling").as_posix(), "failed"),
                ((src / "a" / "loop").as_posix(), "ignored"),  # an enclosing directory
                ((src / "a" / "x.dcm").as_posix(), "dicom"),
            ],
        )

    def test_dicomdir_is_ignored(self):
        bundled = get_testdata_file("DICOMDIR", download=False)
        if bundled is None:
            ds = pydicom.Dataset()
            ds.file_meta = FileMetaDataset()
            ds.file_meta.MediaStorageSOPClassUID = MediaStorageDirectoryStorage
            ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
            ds.FileSetID = "TEST"
            path = self.root / "DICOMDIR"
            ds.save_as(path, implicit_vr=False, little_endian=True, enforce_file_format=True)
        else:
            path = Path(bundled)
        result = self.run_quietly([path])
        self.assertEqual(result.written, 0)
        self.assertEqual(result.skipped, [SkippedRow(path.as_posix(), "ignored", "DICOMDIR index")])

    def test_nested_zip_is_failed(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as zf:
            zf.writestr("a.png", _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)))
        archive = self.root / "outer.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("inner.zip", nested.getvalue())
        result = self.run_quietly([archive])
        self.assertEqual(
            result.skipped, [SkippedRow(f"{archive.as_posix()}::inner.zip", "failed", "unsupported: nested zip")]
        )

    def test_zip_entry_with_a_control_character_is_failed_and_exports(self):
        archive = self.root / "scans.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("good.png", _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)))
            zf.writestr("a\nb.png", _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)))
            zf.writestr("c\td.txt", b"not an image")  # would be ignored, but its name cannot be exported
        work = self.root / "work"
        result = self.invoke(archive, work)
        self.assertEqual(result.exit_code, 1, result.output)
        a = archive.as_posix()
        self.assertEqual([r["image_id"] for r in _read_tsv(work / "manifest.tsv")], [f"{a}::good.png"])
        self.assertEqual(
            self.skipped_rows(work),
            [(f"{a}::a\\x0ab.png", "failed", BAD_NAME_REASON), (f"{a}::c\\x09d.txt", "failed", BAD_NAME_REASON)],
        )
        report = self.root / "report.tsv"
        exported = invoke_cli("export", "--work-dir", str(work), "--report", str(report))
        self.assertEqual(exported.exit_code, 0, exported.output)
        self.assertNotIn("\\x0a", exported.stdout)  # never allowlisted
        self.assertIn(f"{a}::a\\x0ab.png\tNOT_REVIEWED\t", report.read_text())

    def test_file_whose_name_is_not_utf8_is_failed(self):
        src = self.root / "src"
        src.mkdir()
        write_dicom(src / "a.dcm", _good_pixels())
        bad = src / os.fsdecode(b"bad\xff.png")
        try:
            bad.write_bytes(_png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)))
        except (OSError, UnicodeEncodeError):
            self.skipTest("the file system refuses names that are not UTF-8")
        work = self.root / "work"
        result = self.invoke(src, work)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual([r["image_id"] for r in _read_tsv(work / "manifest.tsv")], [(src / "a.dcm").as_posix()])
        self.assertEqual(self.skipped_rows(work), [(f"{src.as_posix()}/bad\\xff.png", "failed", BAD_NAME_REASON)])

    def test_everything_under_a_badly_named_directory_is_failed(self):
        src = self.root / "src"
        bad = src / os.fsdecode(b"d\xff")
        try:
            bad.mkdir(parents=True)
        except (OSError, UnicodeEncodeError):
            self.skipTest("the file system refuses names that are not UTF-8")
        (bad / "a.png").write_bytes(_png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)))
        (bad / "notes.txt").write_text("not an image")  # would be ignored
        work = self.root / "work"
        result = self.invoke(src, work)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(_read_tsv(work / "manifest.tsv"), [])
        d = f"{src.as_posix()}/d\\xff"
        self.assertEqual(
            self.skipped_rows(work),
            [(f"{d}/a.png", "failed", BAD_NAME_REASON), (f"{d}/notes.txt", "failed", BAD_NAME_REASON)],
        )
        record = json.loads((work / "preprocess.json").read_text(encoding="utf-8"))
        self.assertEqual(record["sources"], [src.as_posix()])
        with quiet():
            run_preprocess([bad], self.root / "work2")
        record = json.loads((self.root / "work2" / "preprocess.json").read_text(encoding="utf-8"))
        self.assertEqual(record["sources"], [d])

    def test_symlinked_directory_reason_escapes_a_bad_target_name(self):
        src = self.root / "src"
        src.mkdir()
        elsewhere = self.root / os.fsdecode(b"e\xff")
        try:
            elsewhere.mkdir()
        except (OSError, UnicodeEncodeError):
            self.skipTest("the file system refuses names that are not UTF-8")
        (src / "linked_dir").symlink_to(elsewhere, target_is_directory=True)
        work = self.root / "work"
        self.invoke(src, work)
        [(_, kind, reason)] = self.skipped_rows(work)
        self.assertEqual(kind, "failed")
        self.assertIn(f"pass its target {self.root.as_posix()}/e\\xff as a SOURCE", reason)


def _png_header_only(width: int, height: int, color_type: int = 0, bit_depth: int = 8) -> bytes:
    """A PNG declaring `width` x `height` pixels with empty pixel data: it opens, but cannot be decoded."""
    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    return PNG_MAGIC + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(b"")) + _png_chunk(b"IEND", b"")


def _bad_exif_in_frame0(mpo: bytes) -> bytes:
    """`mpo` with an unreadable EXIF segment inserted before frame 0's MPF segment, so offsets stay valid."""
    bad = b"Exif\x00\x00XXXXXXXXXXXX"
    return mpo[:2] + b"\xff\xe1" + struct.pack(">H", len(bad) + 2) + bad + mpo[2:]


class SizeLimitTest(unittest.TestCase):
    """Inputs too large to render safely fail as `failed` rows before their bytes are read or pixels decoded."""

    def setUp(self):
        self.root = temp_dir(self).resolve()
        self.png = _png_bytes(RNG.integers(0, 255, (20, 30, 3), dtype=np.uint8))  # 600 pixels

    def run_failed(self, source: Path) -> list[tuple[str, str, str]]:
        work = Path(tempfile.mkdtemp(dir=self.root)) / "work"
        with quiet():
            run_preprocess([source], work)
        return [(r["image_id"], r["kind"], r["reason"]) for r in _read_tsv(work / "skipped.tsv")]

    def too_large(self, render_call, limit: int) -> bool:
        """Whether `render_call()` is refused as too large with `MAX_RENDER_BYTES` at `limit`; any other outcome, a
        success or a failure to decode, means the estimate passed."""
        with mock.patch.object(preprocess_module, "MAX_RENDER_BYTES", limit), quiet():
            try:
                render_call()
            except Unsupported as exc:
                return "image too large" in str(exc)
            except Exception:  # noqa: BLE001
                return False
            return False

    def assert_estimated_at(self, render_call, pixels: int, path: str):
        """The estimate is `pixels` times the factor of `path`: over a limit one byte under it, not over it at it."""
        estimate = pixels * preprocess_module.RENDER_BYTES_PER_PIXEL[path]
        self.assertTrue(self.too_large(render_call, estimate - 1), path)
        self.assertFalse(self.too_large(render_call, estimate), path)

    # ---- files and ZIP entries

    def test_oversized_file_fails_without_being_read(self):
        path = self.root / "big.png"
        path.write_bytes(self.png)
        opened = []
        real = open
        with (
            mock.patch.object(preprocess_module, "MAX_INPUT_BYTES", len(self.png) - 1),
            mock.patch("builtins.open", lambda f, *a, **k: opened.append(Path(f)) or real(f, *a, **k)),
        ):
            rows = self.run_failed(path)
        self.assertEqual(opened.count(path), 1)  # to sniff its first bytes
        reason = f"unsupported: input too large ({len(self.png)} bytes; limit {len(self.png) - 1})"
        self.assertEqual(rows, [(path.as_posix(), "failed", reason)])

    def test_file_at_the_byte_limit_renders(self):
        path = self.root / "ok.png"
        path.write_bytes(self.png)
        with mock.patch.object(preprocess_module, "MAX_INPUT_BYTES", len(self.png)):
            self.assertEqual(self.run_failed(path), [])

    def test_file_is_read_no_further_than_the_limit(self):
        path = self.root / "grown.png"
        path.write_bytes(self.png)
        with (
            mock.patch.object(preprocess_module, "MAX_INPUT_BYTES", len(self.png) - 1),
            self.assertRaisesRegex(Unsupported, r"input too large \(more than \d+ bytes; limit \d+\)"),
        ):
            preprocess_module._read_input(path, 1)  # as if stat had said it was small

    def test_file_grown_within_the_limit_is_read_whole(self):
        path = self.root / "grown.png"
        path.write_bytes(self.png)
        self.assertEqual(preprocess_module._read_input(path, 1), self.png)

    def test_encrypted_zip_entries_fail_as_encrypted(self):
        archive = self.root / "enc.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("aes.png", self.png, zipfile.ZIP_STORED)
            zf.writestr("crypto.png", self.png, zipfile.ZIP_DEFLATED)
        data = bytearray(archive.read_bytes())
        for name, method in ((b"aes.png", 99), (b"crypto.png", None)):
            entry = data.index(b"PK\x01\x02", 0)
            while data[entry + 46 : entry + 46 + len(name)] != name:
                entry = data.index(b"PK\x01\x02", entry + 1)
            struct.pack_into("<H", data, entry + 8, 1)  # general purpose flags: encrypted
            if method:
                struct.pack_into("<H", data, entry + 10, method)
        archive.write_bytes(data)
        reason = "unsupported: encrypted ZIP entry (decrypt the archive first)"
        self.assertEqual(
            self.run_failed(archive),
            [
                (f"{archive.as_posix()}::aes.png", "failed", reason),
                (f"{archive.as_posix()}::crypto.png", "failed", reason),
            ],
        )

    def test_oversized_zip_entry_is_neither_opened_nor_read(self):
        archive = self.root / "scans.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("big.png", self.png)
            zf.writestr("big.txt", b"x" * len(self.png))  # not an image, but over the limit all the same
        with (
            mock.patch.object(preprocess_module, "MAX_INPUT_BYTES", len(self.png) - 1),
            mock.patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("opened")) as opened,
            mock.patch.object(zipfile.ZipFile, "read", side_effect=AssertionError("read")) as read,
        ):
            rows = self.run_failed(archive)
        opened.assert_not_called()
        read.assert_not_called()
        reason = f"unsupported: input too large ({len(self.png)} bytes; limit {len(self.png) - 1})"
        self.assertEqual(
            rows,
            [
                (f"{archive.as_posix()}::big.png", "failed", reason),
                (f"{archive.as_posix()}::big.txt", "failed", reason),
            ],
        )

    def test_zip_entries_compressed_by_unbounded_methods_fail(self):
        archive = self.root / "methods.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("stored.png", self.png, zipfile.ZIP_STORED)
            zf.writestr("deflated.png", self.png, zipfile.ZIP_DEFLATED)
            zf.writestr("bzip2.png", self.png, zipfile.ZIP_BZIP2)
            zf.writestr("lzma.png", self.png, zipfile.ZIP_LZMA)
        rows = self.run_failed(archive)
        self.assertEqual(
            rows,
            [
                (f"{archive.as_posix()}::bzip2.png", "failed", "unsupported: ZIP compression method bzip2"),
                (f"{archive.as_posix()}::lzma.png", "failed", "unsupported: ZIP compression method lzma"),
            ],
        )

    def test_zip_entry_whose_header_understates_its_size_cannot_be_read_past_it(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("lie.png", self.png + bytes(100_000))
        data = bytearray(buf.getvalue())
        directory = data.rindex(b"PK\x01\x02")
        declared = 5000  # past the 4096 bytes that sniffing consumes, so that the read is reached
        self.assertGreater(len(self.png) + 100_000, declared)
        struct.pack_into("<I", data, directory + 24, declared)  # the declared uncompressed size
        archive = self.root / "lie.zip"
        archive.write_bytes(data)
        real = zipfile.ZipFile.read
        reads = []
        with mock.patch.object(zipfile.ZipFile, "read", lambda *a: reads.append(a) or real(*a)):
            reasons = [reason for _, _, reason in self.run_failed(archive)]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reasons, ["BadZipFile: Bad CRC-32 for file 'lie.png'"])

    # ---- DICOM

    def dicom(self, **attrs) -> Path:
        path = self.root / f"{len(list(self.root.iterdir()))}.dcm"
        write_dicom(path, np.zeros((20, 30), dtype=np.uint16), **attrs)
        return path

    def render_dicom(self, path: Path, **kwargs):
        return lambda: render("dicom", ImageId("x"), path.read_bytes(), "gray", **kwargs)

    def test_dicom_over_the_budget_fails_before_decoding(self):
        path = self.dicom(Rows=6000, Columns=7000)  # 42 million pixels of 16-bit gray, about 3.1 GiB
        with mock.patch.object(preprocess_module, "_decode_pixels", side_effect=AssertionError("decoded")):
            rows = self.run_failed(path)
        reason = "unsupported: image too large (7000 x 6000, about 3.2 GiB to render; limit 3.0 GiB)"
        self.assertEqual(rows, [(path.as_posix(), "failed", reason)])

    def test_dicom_estimate_follows_the_decode_path(self):
        with mock.patch.object(preprocess_module, "_decode_pixels", side_effect=AssertionError("decoded")):
            cases = {
                "dicom-gray": self.dicom(),
                "dicom-gray-wide": self.dicom(BitsAllocated=32),
                "dicom-colour": self.dicom(PhotometricInterpretation="YBR_FULL"),
                "dicom-colour-wide": self.dicom(PhotometricInterpretation="RGB", BitsAllocated=32),
                "dicom-palette": self.dicom(PhotometricInterpretation="PALETTE COLOR"),
            }
            for path, file in cases.items():
                with self.subTest(path):
                    self.assert_estimated_at(self.render_dicom(file), 600, path)

    def test_dicom_overlays_are_counted_only_when_drawn(self):
        path = self.root / "overlay.dcm"
        ds_pixels = np.zeros((20, 30), dtype=np.uint16)
        write_dicom(path, ds_pixels)
        dcm = pydicom.dcmread(path)
        add_overlay(dcm, np.zeros((20, 30), dtype=bool))
        dcm[0x6000, 0x0010].value, dcm[0x6000, 0x0011].value = 100, 100  # the plane is declared larger
        dcm.save_as(path, enforce_file_format=True)
        image = 600 * preprocess_module.RENDER_BYTES_PER_PIXEL["dicom-gray"]
        limit = image + 100 * 100 * preprocess_module.OVERLAY_BYTES_PER_PIXEL - 1
        self.assertFalse(self.too_large(self.render_dicom(path), limit))
        self.assertTrue(self.too_large(self.render_dicom(path, draw_overlays=True), limit))

    def test_unusable_overlay_dimensions_are_reported_by_the_overlay_decoder(self):
        for name, value in (("empty", None), ("multi-valued", [10, 10])):
            with self.subTest(name):
                path = self.root / f"{name}.dcm"
                write_dicom(path, np.zeros((20, 30), dtype=np.uint16))
                dcm = pydicom.dcmread(path)
                add_overlay(dcm, np.zeros((20, 30), dtype=bool))
                dcm[0x6000, 0x0010].value = value
                dcm.save_as(path, enforce_file_format=True)
                with quiet(), self.assertRaisesRegex(ValueError, "overlay 0x6000 cannot be decoded"):
                    render("dicom", ImageId("x"), path.read_bytes(), "gray", draw_overlays=True)

    def test_dicom_without_rows_or_columns_is_left_to_the_decoder(self):
        path = self.dicom()
        dcm = pydicom.dcmread(path)
        del dcm.Rows
        dcm.save_as(path, enforce_file_format=True)
        self.assertFalse(self.too_large(self.render_dicom(path), 0))

    def test_multi_frame_dicom_is_still_refused_for_its_frames(self):
        path = self.root / "cine.dcm"
        write_dicom(path, np.zeros((3, 20, 30), dtype=np.uint16))
        with mock.patch.object(preprocess_module, "MAX_RENDER_BYTES", 1):
            rows = self.run_failed(path)
        self.assertEqual(rows[0][2], "unsupported: multi-frame DICOM (3 frames)")

    def test_failure_is_the_same_for_any_jobs(self):
        path = self.dicom(Rows=40_000, Columns=40_000)  # workers use the real limit
        outputs = []
        for jobs in (1, 2):
            with quiet():
                run_preprocess([path], self.root / f"work{jobs}", jobs=jobs)
            outputs.append((self.root / f"work{jobs}" / "skipped.tsv").read_bytes())
        self.assertEqual(outputs[0], outputs[1])
        reason = "unsupported: image too large (40000 x 40000, about 119.3 GiB to render; limit 3.0 GiB)"
        self.assertIn(reason.encode(), outputs[0])

    # ---- rasters

    def test_raster_over_the_budget_fails_before_decoding(self):
        png = _png_header_only(10_000, 10_000)  # 100 million pixels of gray: about 4.1 GiB
        with (
            quiet(),
            mock.patch.object(preprocess_module, "_decode_frame", side_effect=AssertionError("decoded")),
            self.assertRaisesRegex(
                Unsupported,
                r"^unsupported: image too large \(10000 x 10000, about 4\.1 GiB to render; limit 3\.0 GiB\)$",
            ),
        ):
            decode_raster(png)

    def test_raster_estimate_follows_the_decode_path(self):
        def saved(image: Image.Image, **options) -> bytes:
            out = io.BytesIO()
            image.save(out, "PNG", **options)
            return out.getvalue()

        inputs = {
            "raster-gray8": [saved(Image.new("L", (30, 20), 9)), saved(Image.new("1", (30, 20)))],
            "raster-gray16": [saved(Image.fromarray(RNG.integers(0, 65535, (20, 30), dtype=np.uint16)))],
            "raster-rgb8": [self.png, saved(Image.new("P", (30, 20)))],
            # a palette with a transparent index is decoded as RGBA
            "raster-alpha": [saved(Image.new("RGBA", (30, 20))), saved(Image.new("P", (30, 20)), transparency=0)],
        }
        for path, datas in inputs.items():
            for data in datas:
                with self.subTest(path):
                    self.assert_estimated_at(partial(decode_raster, data), 600, path)

    def test_sixteen_bit_colour_is_checked_before_the_full_depth_decode(self):
        rgb = RNG.integers(0, 65535, (20, 30, 3), dtype=np.uint16)
        rgba = RNG.integers(0, 65535, (20, 30, 4), dtype=np.uint16)
        tiff = io.BytesIO()
        tifffile.imwrite(tiff, rgb, photometric="rgb")
        cases = {
            "raster-rgb16": [imagecodecs.png_encode(rgb), tiff.getvalue()],
            "raster-alpha": [imagecodecs.png_encode(rgba)],
        }
        with (
            mock.patch.object(preprocess_module, "_png_samples", side_effect=AssertionError("decoded")),
            mock.patch.object(preprocess_module, "_decode_tiff", side_effect=AssertionError("decoded")),
        ):
            for path, datas in cases.items():
                for data in datas:
                    with self.subTest(path):
                        under = 600 * preprocess_module.RENDER_BYTES_PER_PIXEL[path] - 1
                        self.assertTrue(self.too_large(partial(decode_raster, data), under))

    def test_mpo_frames_are_laid_out_on_one_canvas(self):
        mpo = _mpo_bytes()  # 60 x 40 RGB and 30 x 20 gray: a canvas of 94 x 40 with the gap
        canvas = (60 + SIDE_BY_SIDE_GAP + 30) * 40
        with mock.patch.object(preprocess_module, "_decode_frame", side_effect=AssertionError("decoded")):
            self.assertTrue(self.too_large(partial(decode_raster, mpo), canvas * 44 - 1))
        self.assertFalse(self.too_large(partial(decode_raster, mpo), canvas * 44))
        with (
            mock.patch.object(preprocess_module, "MAX_RENDER_BYTES", canvas * 44 - 1),
            self.assertRaisesRegex(Unsupported, r"image too large \(2 frames, 94 x 40 side by side, about "),
        ):
            decode_raster(mpo)

    def test_ultra_hdr_gain_map_counts_toward_the_canvas(self):
        uhdr = _with_hdrgm_xmp(_mpo_bytes())
        canvas = (60 + SIDE_BY_SIDE_GAP + 30) * 40
        with mock.patch.object(preprocess_module, "_decode_frame", side_effect=AssertionError("decoded")):
            self.assertTrue(self.too_large(partial(decode_raster, uhdr), canvas * 44 - 1))
        self.assertFalse(self.too_large(partial(decode_raster, uhdr), canvas * 44))

    def test_mpo_with_unreadable_exif_in_frame_zero_renders_as_before(self):
        mpo = _bad_exif_in_frame0(_mpo_bytes(_exif_with_thumbnail()))
        views, notes = decode_raster(mpo)
        self.assertEqual(
            notes,
            (
                Note("embedded-thumbnail", "EXIF unreadable: SyntaxError"),
                Note("embedded-thumbnail", "frame 1: EXIF IFD1 thumbnail (332 bytes)"),
            ),
        )
        self.assertEqual(views[0].shape, (40, 94, 3))

    def test_mpo_cut_inside_frame_zero_fails_as_before(self):
        mpo = _mpo_bytes()
        cut = mpo[: mpo.rindex(b"\xff\xd8\xff") - 30]
        with self.assertRaisesRegex(OSError, r"^image file is truncated \(0 bytes not processed\)$"):
            decode_raster(cut)

    def test_pillows_decompression_bomb_error_is_the_same_failure(self):
        png = _png_header_only(20_000, 20_000)  # 400 million pixels, over twice Pillow's limit
        with self.assertRaisesRegex(
            Unsupported, r"^unsupported: image too large \(over \d+ pixels, Pillow's decompression bomb limit\)$"
        ):
            decode_raster(png)


HTJ2K_ENCODE = getattr(imagecodecs, "htj2k_encode", None)  # not in the oldest supported imagecodecs


def _sof(height: int, width: int, components: int = 1, marker: int = 0xC0) -> bytes:
    """A JPEG frame header (SOFn, DHP or JPEG-LS SOF55) of 8-bit samples."""
    body = bytes([8]) + struct.pack(">HHB", height, width, components) + bytes([1, 0x11, 0]) * components
    return bytes([0xFF, marker]) + struct.pack(">H", 2 + len(body)) + body


def _jpeg_segment(marker: int, payload: bytes = b"") -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", 2 + len(payload)) + payload


def _jpeg_headers(*segments: bytes) -> bytes:
    """SOI, the given segments, then a scan header and EOI: enough for `_jpeg_frame_size`, not for a decoder."""
    return b"\xff\xd8" + b"".join(segments) + _jpeg_segment(0xDA, bytes(6)) + b"\x00\xff\xd9"


def _j2k_segment(marker: int, body: bytes) -> bytes:
    return struct.pack(">HH", marker, 2 + len(body)) + body


def _cod(levels: int = 5, xcb: int = 4, ycb: int = 4, precincts: bytes | None = None, layers: int = 1) -> bytes:
    """A JPEG 2000 COD marker: 2**(xcb + 2) x 2**(ycb + 2) code-blocks, and `precincts` (one PPy << 4 | PPx byte per
    resolution) if given."""
    style = bytes([0 if precincts is None else 1, 0]) + struct.pack(">HB", layers, 0)  # Scod, order, layers, MCT
    return _j2k_segment(0xFF52, style + bytes([levels, xcb, ycb, 0, 1]) + (precincts or b""))


def _coc(component: int, xcb: int = 4, ycb: int = 4, index_bytes: int = 1) -> bytes:
    body = component.to_bytes(index_bytes, "big") + bytes([0, 5, xcb, ycb, 0, 1])
    return _j2k_segment(0xFF53, body)


def _tile_part(*segments: bytes, psot: int | None = None) -> bytes:
    """A JPEG 2000 tile-part: SOT (its Psot the tile-part's length unless given), `segments`, SOD and 8 bytes."""
    header = b"".join(segments)
    length = 12 + len(header) + 2 + 8 if psot is None else psot
    return struct.pack(">HHHLBB", 0xFF90, 10, 0, length, 0, 1) + header + b"\xff\x93" + bytes(8)


def _siz(
    width: int, height: int, components: int = 1, x0: int = 0, y0: int = 0, cod: bytes | None = None, tail: bytes = b""
) -> bytes:
    """A JPEG 2000 codestream's SOC and SIZ marker (image and tile offsets `x0`, `y0`), a COD (`_cod()` unless given),
    `tail`, then EOC."""
    siz = struct.pack(">H8LH", 0, width, height, x0, y0, width, height, x0, y0, components)
    siz += bytes([7, 1, 1]) * components
    head = b"\xff\x4f\xff\x51" + struct.pack(">H", 2 + len(siz)) + siz
    return head + (_cod() if cod is None else cod) + tail + b"\xff\xd9"


def _jp2_box(kind: bytes, payload: bytes, extended: bool = False) -> bytes:
    if extended:
        return struct.pack(">L4sQ", 1, kind, 16 + len(payload)) + payload
    return struct.pack(">L4s", 8 + len(payload), kind) + payload


def _jp2(codestream: bytes, extended: bool = False) -> bytes:
    """`codestream` in a JP2 file: signature, file type and header boxes, then the `jp2c` box."""
    return (
        _jp2_box(b"jP  ", b"\r\n\x87\n")
        + _jp2_box(b"ftyp", b"jp2 \x00\x00\x00\x00jp2 ")
        + _jp2_box(b"jp2h", _jp2_box(b"ihdr", bytes(14)), extended)
        + _jp2_box(b"jp2c", codestream)
    )


def _rle(*segments: bytes) -> bytes:
    """An RLE Lossless frame: the 64-byte header, then `segments`."""
    offsets, pos = [], 64
    for segment in segments:
        offsets.append(pos)
        pos += len(segment)
    return struct.pack(f"<{1 + len(offsets)}L", len(segments), *offsets).ljust(64, b"\x00") + b"".join(segments)


def _compressed_dicom(
    syntax: UID, frames: Sequence[bytes], rows: int, columns: int, samples: int = 1, bits: int = 8, **attrs
) -> bytes:
    """A single-frame DICOM (NumberOfFrames absent) whose encapsulated PixelData holds `frames`, with a Basic Offset
    Table when there is more than one."""
    ds = pydicom.Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = syntax
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.Rows, ds.Columns, ds.SamplesPerPixel = rows, columns, samples
    ds.PhotometricInterpretation = "MONOCHROME2" if samples == 1 else "RGB"
    if samples == 3:
        ds.PlanarConfiguration = 0
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = bits, bits, bits - 1, 0
    for name, value in attrs.items():
        setattr(ds, name, value)
    ds.PixelData = encapsulate(list(frames), has_bot=len(frames) > 1)
    ds["PixelData"].VR = "OB"
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


class CompressedFrameTest(unittest.TestCase):
    """Compressed pixel data must decode to its declared Rows, Columns and SamplesPerPixel; that is read from the
    codestream's own headers before anything is decoded."""

    def setUp(self):
        self.gray = RNG.integers(0, 255, (20, 30), dtype=np.uint8)
        self.rgb = RNG.integers(0, 255, (20, 30, 3), dtype=np.uint8)

    def assert_unreadable(self, call, what: str):
        with self.assertRaises(Unsupported) as ctx:
            call()
        self.assertEqual(str(ctx.exception), f"unsupported: cannot read compressed pixel data header ({what})")

    def refused(self, data: bytes) -> str:
        """The reason `render` refuses DICOM `data` with, failing the test if a decoder is reached."""
        with (
            quiet(),
            mock.patch.object(preprocess_module, "_decode_pixels", side_effect=AssertionError("decoded")),
            self.assertRaises(Unsupported) as ctx,
        ):
            render("dicom", ImageId("x"), data, "inferno")
        return _failed(ImageId("x"), ctx.exception).reason

    # ---- JPEG and JPEG-LS

    def test_jpeg_size_is_read_from_real_codestreams(self):
        self.assertEqual(_jpeg_frame_size(imagecodecs.jpeg8_encode(self.gray)), (30, 20, 1))
        self.assertEqual(_jpeg_frame_size(imagecodecs.jpeg8_encode(self.rgb)), (30, 20, 3))
        self.assertEqual(_jpeg_frame_size(_jpeg_bytes(self.gray)), (30, 20, 1))  # Pillow's, with JFIF APP0
        self.assertEqual(_jpeg_frame_size(imagecodecs.jpegls_encode(self.gray)), (30, 20, 1))

    def test_every_frame_header_marker_is_read(self):
        for marker in (*range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), *range(0xCD, 0xD0), 0xDE, 0xF7):
            with self.subTest(marker=f"{marker:02X}"):
                self.assertEqual(_jpeg_frame_size(_jpeg_headers(_sof(20, 30, 3, marker))), (30, 20, 3))

    def test_tables_and_stray_bytes_before_the_frame_header_are_stepped_over(self):
        segments = (
            _jpeg_segment(0xE0, b"JFIF\x00" + bytes(9)),
            _jpeg_segment(0xC4, bytes(20)),  # DHT, JPG and DAC are not frame headers
            _jpeg_segment(0xCC, bytes(4)),
            _jpeg_segment(0xC8),
            b"junk\xff\xff\xff",  # skipped to the next marker, and fill bytes before it
            _jpeg_segment(0xDB, bytes(65)),
            _sof(20, 30),
        )
        self.assertEqual(_jpeg_frame_size(_jpeg_headers(*segments)), (30, 20, 1))

    def test_jpeg_height_set_by_a_dnl_marker_is_refused(self):
        dnl = b"\xff\xdc\x00\x04\x00\x14"
        data = b"\xff\xd8" + _sof(0, 30) + _jpeg_segment(0xDA, bytes(6)) + b"\x00" + dnl + b"\xff\xd9"
        self.assert_unreadable(lambda: _jpeg_frame_size(data), "JPEG frame header without a height (DNL or LSE)")
        lse = _jpeg_headers(_sof(0, 30, marker=0xF7), _jpeg_segment(0xF8, b"\x04\x02" + bytes(8)))  # JPEG-LS
        self.assert_unreadable(lambda: _jpeg_frame_size(lse), "JPEG frame header without a height (DNL or LSE)")

    def test_truncated_or_garbage_jpeg_is_unreadable(self):
        whole = _jpeg_headers(_jpeg_segment(0xE0, bytes(14)), _sof(20, 30))
        sof = whole.index(b"\xff\xc0")
        cases = {
            "no JPEG SOI marker": (b"", b"\xff", b"garbage", RNG.integers(0, 255, 4096, dtype=np.uint8).tobytes()),
            "truncated JPEG frame header": (whole[: sof + 9],),
            "no JPEG frame header": (
                whole[:sof],
                whole[: sof + 1],
                b"\xff\xd8\xff\xd9" + _sof(20, 30),  # after EOI
                b"\xff\xd8" + _jpeg_segment(0xDA, bytes(6)) + _sof(20, 30),  # after a scan
                b"\xff\xd8\xff\xd8" + _sof(20, 30),  # after a nested SOI
                b"\xff\xd8" + bytes(100),
            ),
        }
        for what, datas in cases.items():
            for data in datas:
                with self.subTest(what=what, data=data[:16]):
                    self.assert_unreadable(lambda data=data: _jpeg_frame_size(data), what)

    def test_marker_walk_is_bounded(self):
        data = _jpeg_headers(*[_jpeg_segment(0xFE)] * 50, _sof(20, 30))
        self.assertEqual(_jpeg_frame_size(data), (30, 20, 1))
        with mock.patch.object(preprocess_module, "JPEG_WALK_MAX_MARKERS", 50):
            self.assert_unreadable(lambda: _jpeg_frame_size(data), "too many JPEG markers")

    # ---- JPEG 2000 and HTJ2K

    def test_j2k_size_is_read_from_real_codestreams_and_jp2_files(self):
        for codecformat in ("J2K", "JP2"):
            with self.subTest(codecformat):
                encode = partial(imagecodecs.jpeg2k_encode, level=0, codecformat=codecformat)
                self.assertEqual(_j2k_frame_size(encode(self.gray)), (30, 20, 1))
                self.assertEqual(_j2k_frame_size(encode(self.rgb)), (30, 20, 3))
        if HTJ2K_ENCODE is not None:
            self.assertEqual(_j2k_frame_size(HTJ2K_ENCODE(self.rgb)), (30, 20, 3))

    def test_j2k_size_excludes_the_image_offset(self):
        self.assertEqual(_j2k_frame_size(_siz(130, 220, 3, x0=100, y0=200)), (30, 20, 3))

    def test_jp2_boxes_are_walked_to_the_codestream(self):
        self.assertEqual(_j2k_frame_size(_jp2(_siz(30, 20))), (30, 20, 1))
        self.assertEqual(_j2k_frame_size(_jp2(_siz(30, 20), extended=True)), (30, 20, 1))
        last = _jp2(b"")[:-8] + struct.pack(">L4s", 0, b"jp2c") + _siz(30, 20)  # a last box of length 0
        self.assertEqual(_j2k_frame_size(last), (30, 20, 1))

    def test_truncated_or_garbage_j2k_is_unreadable(self):
        signature = _jp2_box(b"jP  ", b"\r\n\x87\n")
        cases = {
            "no JPEG 2000 SOC and SIZ markers": (b"", b"garbage", _siz(30, 20)[2:], b"\xff\x4f\xff\x52" + bytes(40)),
            "truncated JPEG 2000 SIZ marker": (_siz(30, 20)[:41], _jp2(_siz(30, 20)[:30])),
            "empty JPEG 2000 image area": (_siz(30, 20, x0=30), _siz(30, 20, y0=21)),
            "no JP2 codestream box": (signature, signature + _jp2_box(b"jp2h", bytes(20)), _jp2(b"")[:-8]),
            "bad JP2 box length": (signature + struct.pack(">L4s", 4, b"free") + _jp2_box(b"jp2c", _siz(30, 20)),),
        }
        for what, datas in cases.items():
            for data in datas:
                with self.subTest(what=what, data=data[:16]):
                    self.assert_unreadable(lambda data=data: _j2k_frame_size(data), what)

    def test_j2k_code_blocks_of_256_pixels_or_more_pass(self):
        cods = (
            _cod(),  # 64 x 64, as coders write
            _cod(xcb=2, ycb=2),  # 16 x 16
            _cod(xcb=1, ycb=3),  # 8 x 32
            _cod(xcb=0, ycb=6),  # 4 x 256
            _cod(levels=1, precincts=b"\x44\x55"),  # capped at 16 x 16 by the precincts, in band coordinates
            _cod(levels=0, precincts=b"\x44"),
        )
        for cod in cods:
            with self.subTest(cod=cod.hex()):
                self.assertEqual(_j2k_frame_size(_siz(30, 20, cod=cod, tail=_tile_part(_cod()))), (30, 20, 1))

    def test_small_j2k_code_blocks_are_refused_wherever_they_are_set(self):
        tiny = _cod(xcb=0, ycb=0)
        cases = {
            "4 x 4": _siz(30, 20, cod=tiny),
            "8 x 16": _siz(30, 20, cod=_cod(xcb=1, ycb=2)),
            "1 x 1 precinct at the lowest resolution": _siz(30, 20, cod=_cod(1, 0, 0, b"\x00\x11")),
            "8 x 8 precinct above it": _siz(30, 20, cod=_cod(levels=1, precincts=b"\x44\x44")),
            "main-header COC": _siz(30, 20, 3, tail=_coc(2, 0, 0)),
            "two-byte COC index": _siz(30, 20, 300, tail=_coc(299, 0, 0, index_bytes=2)),
            "tile-part COD": _siz(30, 20, tail=_tile_part() + _tile_part(tiny)),
            "tile-part COC": _siz(30, 20, tail=_tile_part(_coc(0, 0, 0))),
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(Unsupported) as ctx:
                _j2k_frame_size(data)
            self.assertEqual(str(ctx.exception), "unsupported: JPEG 2000 code-blocks of fewer than 256 pixels")
        self.assertEqual(_j2k_frame_size(_siz(30, 20, 300, tail=_coc(299, index_bytes=2))), (30, 20, 300))

    def test_j2k_tile_parts_are_found_by_their_length(self):
        last = _siz(30, 20, tail=_tile_part() + _tile_part(psot=0))  # 0: the last, to the end
        self.assertEqual(_j2k_frame_size(last), (30, 20, 1))
        self.assertEqual(_j2k_frame_size(_siz(30, 20, tail=_tile_part(psot=1000))), (30, 20, 1))  # truncated
        three = _siz(30, 20, tail=_tile_part() * 3)
        self.assertEqual(_j2k_frame_size(three), (30, 20, 1))
        with mock.patch.object(preprocess_module, "_J2K_MAX_TILE_PARTS", 2):
            self.assert_unreadable(lambda: _j2k_frame_size(three), "too many JPEG 2000 tile-parts")

    def test_bad_j2k_coding_headers_are_unreadable(self):
        cases = {
            "no JPEG 2000 COD marker": (_siz(30, 20, cod=b""), _siz(30, 20, cod=b"", tail=_tile_part(_coc(0)))),
            "truncated JPEG 2000 COD or COC marker": (_siz(30, 20)[:-6], _siz(30, 20, cod=_cod(3, precincts=b"\x55"))),
            "bad JPEG 2000 marker segment": (
                _siz(30, 20, cod=b"\xff\x64\x00\x01" + _cod()),
                _siz(30, 20, tail=_tile_part(b"\xff\x64\x00\x00")),
            ),
            "unexpected JPEG 2000 marker": (
                _siz(30, 20, cod=b"\x12\x34\x00\x04" + _cod()),
                _siz(30, 20, tail=_tile_part(b"\x00\x00\x00\x04")),
                _siz(30, 20, tail=_j2k_segment(0xFF51, bytes(4))),  # a second SIZ
                _siz(30, 20, tail=_j2k_segment(0xFF61, bytes(4))),  # PPT: tile-part headers only
                _siz(30, 20, tail=_tile_part(_j2k_segment(0xFF60, bytes(4)))),  # PPM: the main header only
            ),
            "bad JPEG 2000 tile-part length": (
                _siz(30, 20, tail=_tile_part(psot=5)),
                _siz(30, 20, tail=_tile_part() + b"garbage!"),
            ),
        }
        for what, datas in cases.items():
            for data in datas:
                with self.subTest(what=what, data=data[-16:]):
                    self.assert_unreadable(lambda data=data: _j2k_frame_size(data), what)

    def test_j2k_marker_segments_allowed_in_headers_pass(self):
        main = (0xFF50, 0xFF55, 0xFF57, 0xFF59, 0xFF5C, 0xFF5D, 0xFF5E, 0xFF5F, 0xFF60, 0xFF63, 0xFF64)
        tile = (0xFF52, 0xFF53, 0xFF58, 0xFF5C, 0xFF5D, 0xFF5E, 0xFF5F, 0xFF61, 0xFF64)
        # COD and COC aside (checked elsewhere), bodies are read by their length, so a COD marker inside one is not one
        body = b"\x00\x00\xff\x52\x00\x0c"
        data = _siz(
            30,
            20,
            tail=b"".join(_j2k_segment(m, body) for m in main) + _tile_part(*(_j2k_segment(m, body) for m in tile[2:])),
        )
        self.assertEqual(_j2k_frame_size(data), (30, 20, 1))
        cap_com = _siz(30, 20, cod=_j2k_segment(0xFF50, bytes(6)) + _cod() + _j2k_segment(0xFF64, b"\x00\x01Kakadu"))
        self.assertEqual(_j2k_frame_size(cap_com), (30, 20, 1))

    def test_unknown_or_part_2_j2k_markers_are_refused(self):
        # OpenJPEG steps over an unknown marker by scanning for the next marker it knows, not by its declared length:
        # here it would find the COD of 1 x 1 code-blocks that a walk by length steps over
        hidden = _cod(1, 0, 0, b"\x00\x11")
        desync = _siz(30, 20, tail=b"\xff\x6e" + struct.pack(">H", 2 + len(hidden)) + hidden)
        mct = _siz(30, 20, tail=_j2k_segment(0xFF74, bytes(6 + 60_000)))  # Part 2 MCT, copied into every tile
        for name, data in (
            ("desync", desync),
            ("MCT", mct),
            ("tile-part MCC", _siz(30, 20, tail=_tile_part(_j2k_segment(0xFF75, bytes(4))))),
        ):
            with self.subTest(name):
                self.assert_unreadable(lambda data=data: _j2k_frame_size(data), "unexpected JPEG 2000 marker")

    def test_j2k_tlm_bytes_are_capped(self):
        tlm = _j2k_segment(0xFF55, b"\x00\x10" + bytes(60_000))  # Stlm 0x10, every Ttlm 0
        under = _siz(30, 20, tail=tlm * 17)  # 1,020,034 body bytes
        self.assertEqual(_j2k_frame_size(under), (30, 20, 1))
        with self.assertRaises(Unsupported) as ctx:
            _j2k_frame_size(_siz(30, 20, tail=tlm * 18))
        self.assertEqual(str(ctx.exception), "unsupported: JPEG 2000 TLM markers over 1 MiB")

    def test_j2k_quality_layers_are_capped(self):
        cap = preprocess_module._J2K_MAX_LAYERS
        self.assertEqual(_j2k_frame_size(_siz(30, 20, cod=_cod(layers=cap))), (30, 20, 1))
        for data in (_siz(30, 20, cod=_cod(layers=cap + 1)), _siz(30, 20, tail=_tile_part(_cod(layers=65535)))):
            with self.subTest(data=data[-24:]), self.assertRaises(Unsupported) as ctx:
                _j2k_frame_size(data)
            self.assertEqual(str(ctx.exception), f"unsupported: JPEG 2000 quality layers over {cap}")

    # ---- RLE

    def test_rle_decoded_length_matches_pydicom(self):
        decode = getattr(pydicom.pixels.decoders.rle, "_rle_decode_segment", None)
        if decode is None:
            self.skipTest("pydicom's RLE segment decoder moved")
        rng = np.random.default_rng(1)
        for size in (*range(8), 64, 1000):
            for _ in range(20):
                data = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
                no_ops = np.where(rng.random(size) < 0.5, 0x80, rng.integers(0, 256, size)).astype(np.uint8).tobytes()
                for case in (data, no_ops):
                    with self.subTest(data=case[:16]):
                        self.assertEqual(_rle_decoded_length(case, 0, size, 1 << 30), len(decode(case)))

    def test_rle_decoded_length_reads_only_the_segment_and_stops_past_the_limit(self):
        data = b"\x00a\x81b" + b"\x02xyz"  # 1 literal, then 128 repeated; then 3 literals
        self.assertEqual(_rle_decoded_length(data, 0, 4, 1000), 129)
        self.assertEqual(_rle_decoded_length(data, 4, 8, 1000), 3)
        self.assertEqual(_rle_decoded_length(data, 4, 100, 1000), 3)  # an end past the data is the data's end
        self.assertEqual(_rle_decoded_length(data, 8, 4, 1000), 0)  # an end before the start is empty, as in pydicom
        self.assertEqual(_rle_decoded_length(b"\x80" * 10, 0, 10, 1000), 0)  # no-ops
        self.assertEqual(_rle_decoded_length(b"\x81\x00" * 1000, 0, 2000, 300), 384)  # three runs, then stops

    def test_consistent_rle_passes(self):
        _check_rle_frame(_rle(b"\x81\x00\x7f" + bytes(128)), 256, 1)  # 128 repeated, then 128 literal
        _check_rle_frame(_rle(b"\x81\x00", b"\x81\x00\x00", b"\x81\x00"), 128, 3)  # a trailing pad byte adds nothing
        _check_rle_frame(_rle(b"\x81\x00"), 200, 1)  # short: pydicom fails it, having decoded no more than that

    def test_rle_segment_decoding_past_rows_x_columns_is_refused(self):
        bomb = _rle(b"\x81\x00" * 4_000_000)  # 512 million bytes
        start = time.monotonic()
        with self.assertRaises(Unsupported) as ctx:
            _check_rle_frame(bomb, 256, 1)
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(str(ctx.exception), "unsupported: RLE segment decodes to more than Rows x Columns (256) bytes")
        with self.assertRaisesRegex(Unsupported, r"\(128\) bytes$"):  # by one byte, in the last segment
            _check_rle_frame(_rle(b"\x81\x00", b"\x81\x00\x00\x00"), 128, 2)

    def test_rle_walk_is_bounded_by_the_decoded_bytes(self):
        # 15 segments that all span the same 4 MB of no-ops: each is one regex match, not millions of steps
        frame = struct.pack("<16L", 15, *[64] * 15) + b"\x80" * 4_000_000
        start = time.monotonic()
        _check_rle_frame(frame, 1 << 20, 15)
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(_rle_decoded_length(b"\x80\x80\x00a\x80\x81b\x80", 0, 8, 1000), 129)

    def test_rle_segment_count_must_match_samples_and_bytes(self):
        with self.assertRaises(Unsupported) as ctx:
            _check_rle_frame(_rle(b"\x81\x00", b"\x81\x00"), 128, 3)
        self.assertEqual(
            str(ctx.exception),
            "unsupported: RLE pixel data has 2 segments, not SamplesPerPixel x BitsAllocated / 8 (3)",
        )

    def test_truncated_or_garbage_rle_is_unreadable(self):
        self.assert_unreadable(lambda: _check_rle_frame(_rle(b"\x81\x00")[:63], 128, 1), "truncated RLE header")
        self.assert_unreadable(lambda: _check_rle_frame(b"\x10" + bytes(63), 128, 1), "bad RLE segment count")
        self.assert_unreadable(lambda: _check_rle_frame(b"\xff" * 64, 128, 1), "bad RLE segment count")

    # ---- rendering

    def test_inconsistent_files_fail_before_decoding(self):
        j2k = partial(imagecodecs.jpeg2k_encode, level=0)
        cases = {
            "compressed pixel data is 30 x 20, not Columns x Rows 16 x 16": (
                (JPEG2000Lossless, j2k(self.gray, codecformat="J2K"), 16, 16, 1),
                (JPEG2000Lossless, j2k(self.gray, codecformat="JP2"), 16, 16, 1),
                *([(HTJ2KLossless, HTJ2K_ENCODE(self.gray), 16, 16, 1)] if HTJ2K_ENCODE else []),
                (JPEGBaseline8Bit, imagecodecs.jpeg8_encode(self.gray), 16, 16, 1),
                (JPEGLSLossless, imagecodecs.jpegls_encode(self.gray), 16, 16, 1),
            ),
            "compressed pixel data is 30 x 20, not Columns x Rows 20 x 30": (
                (JPEG2000Lossless, j2k(self.gray, codecformat="J2K"), 30, 20, 1),
            ),
            "compressed pixel data has 3 components, not SamplesPerPixel 1": (
                (JPEG2000Lossless, j2k(self.rgb, codecformat="J2K"), 20, 30, 1),
                (JPEGBaseline8Bit, imagecodecs.jpeg8_encode(self.rgb), 20, 30, 1),
            ),
            "RLE segment decodes to more than Rows x Columns (256) bytes": (
                (RLELossless, _rle(b"\x81\x00" * 100_000), 16, 16, 1),
            ),
            "RLE pixel data has 1 segments, not SamplesPerPixel x BitsAllocated / 8 (3)": (
                (RLELossless, _rle(b"\x81\x00\x81\x00"), 16, 16, 3),
            ),
            "cannot read compressed pixel data header (JPEG frame header without a height (DNL or LSE))": (
                (JPEGBaseline8Bit, _jpeg_headers(_sof(0, 16)), 16, 16, 1),
            ),
            "cannot read compressed pixel data header (no JPEG 2000 SOC and SIZ markers)": (
                (JPEG2000Lossless, b"garbage!", 16, 16, 1),
            ),
            "JPEG 2000 code-blocks of fewer than 256 pixels": (
                # 1 x 1 code-blocks: OpenJPEG needed 9.3 GB for this 4096 x 4096 image of under 1 KB
                (JPEG2000Lossless, _siz(4096, 4096, cod=_cod(1, 0, 0, b"\x00\x11"), tail=_tile_part()), 4096, 4096, 1),
            ),
        }
        for reason, files in cases.items():
            for syntax, frame, rows, columns, samples in files:
                with self.subTest(reason=reason, syntax=syntax.name):
                    data = _compressed_dicom(syntax, [frame], rows, columns, samples)
                    self.assertEqual(self.refused(data), f"unsupported: {reason}")

    def test_a_frame_past_number_of_frames_is_refused(self):
        frame = imagecodecs.jpeg2k_encode(self.gray, level=0, codecformat="J2K")
        data = _compressed_dicom(JPEG2000Lossless, [frame, frame], 20, 30)  # pydicom would decode both
        self.assertEqual(self.refused(data), "unsupported: compressed pixel data holds more than one frame")

    def test_extended_offset_table_selects_the_frame_as_pydicom_does(self):
        small = imagecodecs.jpeg2k_encode(self.gray, level=0, codecformat="J2K")
        large = imagecodecs.jpeg2k_encode(np.zeros((40, 40), np.uint8), level=0, codecformat="J2K")
        small, large = (b + bytes(len(b) % 2) for b in (small, large))

        def with_table(offsets: list[int], lengths: list[int]) -> bytes:
            dcm = pydicom.dcmread(io.BytesIO(_compressed_dicom(JPEG2000Lossless, [small], 20, 30)))
            dcm.PixelData = encapsulate([small, large], has_bot=False)  # without the table, one frame of both
            dcm.add_new(0x7FE00001, "OV", struct.pack(f"<{len(offsets)}Q", *offsets))
            dcm.add_new(0x7FE00002, "OV", struct.pack(f"<{len(lengths)}Q", *lengths))
            buf = io.BytesIO()
            dcm.save_as(buf, enforce_file_format=True)
            return buf.getvalue()

        second = with_table([8 + len(small)], [len(large)])  # the frame is the second fragment
        self.assertEqual(
            self.refused(second), "unsupported: compressed pixel data is 40 x 40, not Columns x Rows 30 x 20"
        )
        both = with_table([0, 8 + len(small)], [len(small), len(large)])
        self.assertEqual(self.refused(both), "unsupported: compressed pixel data holds more than one frame")
        ignored = with_table([8 + len(small)], [len(large), 4])  # lengths that do not match: pydicom ignores it
        with quiet():
            self.assertEqual(render("dicom", ImageId("x"), ignored, "inferno").rgb.dtype, np.uint8)

    def test_transfer_syntax_uid_read_as_a_plain_string_is_checked(self):
        frame = imagecodecs.jpeg2k_encode(self.gray, level=0, codecformat="J2K")
        lossless = "1.2.840.10008.1.2.4.90"  # JPEG 2000 Lossless, as a plain str
        for value, rows in ((lossless, 20), (lossless, 16), ([lossless, "1.2.3"], 20)):
            with self.subTest(value=value, rows=rows):
                dcm = pydicom.dcmread(io.BytesIO(_compressed_dicom(JPEG2000Lossless, [frame], rows, 30)))
                dcm.file_meta[0x00020010] = pydicom.DataElement(0x00020010, "LO", value)  # read as str, not UID
                self.assertNotIsInstance(dcm.file_meta.TransferSyntaxUID, UID)
                if rows == 20 and isinstance(value, str):
                    with quiet():
                        self.assertEqual(preprocess_module.preprocess_dicom(dcm)[0].dtype, np.uint8)
                    continue
                reason = (
                    "unsupported: compressed pixel data is 30 x 20, not Columns x Rows 30 x 16"
                    if rows == 16
                    else "unsupported: transfer syntax UID is not a single value"
                )
                with self.assertRaises(Unsupported) as ctx:
                    preprocess_module.preprocess_dicom(dcm)
                self.assertEqual(str(ctx.exception), reason)

    def test_transfer_syntax_without_a_decoder_fails_as_undecodable(self):
        data = _compressed_dicom(MPEG2MPML, [bytes(16)], 16, 16)
        with quiet(), self.assertRaises(DecodeError) as ctx:
            render("dicom", ImageId("x"), data, "inferno")
        self.assertTrue(str(ctx.exception).startswith("cannot decode MPEG2 Main Profile / Main Level: "))
        with mock.patch.object(pydicom.pixels, "get_decoder"):  # as if pydicom had a decoder for it
            reason = self.refused(data)
        self.assertEqual(
            reason, "unsupported: cannot check compressed pixel data dimensions (MPEG2 Main Profile / Main Level)"
        )

    def test_unparseable_encapsulation_is_unreadable(self):
        data = _compressed_dicom(JPEG2000Lossless, [_siz(30, 20)], 20, 30)
        item = b"\xfe\xff\x00\xe0"
        fragment = data.index(item, data.index(item) + 4)  # after the empty Basic Offset Table
        reason = self.refused(data[:fragment] + b"\x00" * 4 + data[fragment + 4 :])  # not an item tag
        self.assertEqual(reason, "unsupported: cannot read compressed pixel data header (bad encapsulation)")

    def test_failures_are_skipped_rows(self):
        root = temp_dir(self)
        data = _compressed_dicom(RLELossless, [_rle(b"\x81\x00" * 1000)], 16, 16)
        (root / "bomb.dcm").write_bytes(data)
        with quiet():
            run_preprocess([root / "bomb.dcm"], root / "work")
        rows = [(r["kind"], r["reason"]) for r in _read_tsv(root / "work" / "skipped.tsv")]
        self.assertEqual(rows, [("failed", "unsupported: RLE segment decodes to more than Rows x Columns (256) bytes")])

    def test_consistent_compressed_files_render(self):
        pixels = (np.arange(20 * 30).reshape(20, 30) % 251).astype(np.uint8)
        j2k = imagecodecs.jpeg2k_encode(pixels, level=0, codecformat="J2K")
        rle = pydicom.pixels.encoders.RLELosslessEncoder.encode(
            pixels,
            encoding_plugin="pydicom",
            rows=20,
            columns=30,
            samples_per_pixel=1,
            bits_allocated=8,
            bits_stored=8,
            pixel_representation=0,
            photometric_interpretation="MONOCHROME2",
            number_of_frames=1,
        )
        files = {
            "j2k": (JPEG2000Lossless, j2k),
            "jp2": (JPEG2000Lossless, imagecodecs.jpeg2k_encode(pixels, level=0, codecformat="JP2")),
            **({"htj2k": (HTJ2KLossless, HTJ2K_ENCODE(pixels))} if HTJ2K_ENCODE else {}),
            "rle": (RLELossless, rle),
            "jpeg": (JPEGBaseline8Bit, imagecodecs.jpeg8_encode(pixels)),
        }
        expected = render("dicom", ImageId("x"), _compressed_dicom(JPEG2000Lossless, [j2k], 20, 30), "inferno").rgb
        for name, (syntax, frame) in files.items():
            with self.subTest(name), quiet():
                out = render("dicom", ImageId("x"), _compressed_dicom(syntax, [frame], 20, 30), "inferno")
                self.assertEqual(out.rgb.dtype, np.uint8)
                if name != "jpeg":  # lossless
                    np.testing.assert_array_equal(out.rgb, expected)


class EscapeNameTest(unittest.TestCase):
    def test_bad_characters_are_spelled_out_and_others_kept(self):
        self.assertEqual(_escape_name(os.fsdecode(b"/s/a\xff.png")), "/s/a\\xff.png")
        self.assertEqual(_escape_name("a\nb.png"), "a\\x0ab.png")
        self.assertEqual(_escape_name("a\x7f\x85\u2028\u2029b"), "a\\x7f\\u0085\\u2028\\u2029b")
        self.assertEqual(_escape_name("/s/\u00e9\u65e5 \\x.png"), "/s/\u00e9\u65e5 \\x.png")

    def test_a_non_utf8_byte_and_a_c1_character_escape_differently(self):
        raw_byte, c1 = os.fsdecode(b"a\x85b.png"), os.fsdecode(b"a\xc2\x85b.png")
        self.assertEqual((_escape_name(raw_byte), _escape_name(c1)), ("a\\x85b.png", "a\\u0085b.png"))


class RunErrorTest(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self).resolve()

    def test_jpeg_encoding_failure_skips_only_that_input(self):
        src = self.root / "src"
        src.mkdir()
        write_dicom(src / "good.dcm", _good_pixels())
        (src / "wide.png").write_bytes(_png_bytes(np.zeros((4, 66000, 3), dtype=np.uint8)))
        with quiet():
            result = run_preprocess([src], self.root / "work")
        self.assertEqual(result.written, 1)
        [skip] = result.skipped
        self.assertEqual(skip.image_id, (src / "wide.png").as_posix())
        self.assertTrue(skip.reason.startswith("OSError:"), skip.reason)
        self.assertEqual([p.name for p in (self.root / "work" / "batch_001").iterdir()], ["img_00001.jpg"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read a mode-000 directory")
    def test_unreadable_source_directory_is_one_failed_row(self):
        src = self.root / "locked"
        src.mkdir()
        write_dicom(src / "good.dcm", _good_pixels())
        src.chmod(0)
        self.addCleanup(src.chmod, 0o755)
        with quiet():
            result = run_preprocess([src], self.root / "work")
        self.assertEqual(result.found, 1)
        [skip] = result.skipped
        self.assertEqual(skip.image_id, src.as_posix())
        self.assertTrue(skip.reason.startswith("PermissionError:"), skip.reason)

    def test_work_dir_write_error_aborts_and_leaves_nothing(self):
        write_dicom(self.root / "good.dcm", _good_pixels())
        work = self.root / "work"
        with (
            quiet(),
            mock.patch("image_review.preprocess._write_tsv", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            run_preprocess([self.root / "good.dcm"], work)
        self.assertEqual(list(self.root.glob("*work*")), [])


class StagingTest(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self).resolve()
        self.src = self.root / "src"
        self.src.mkdir()
        write_dicom(self.src / "a.dcm", _good_pixels())
        write_dicom(self.src / "b.dcm", _good_pixels())
        self.work = self.root / "work"
        self.staging = self.root / ".work.partial"

    def run_quietly(self, sources=None):
        with quiet():
            return run_preprocess(sources or [self.src], self.work)

    def test_second_run_is_refused_and_changes_nothing(self):
        self.run_quietly()
        manifest = (self.work / "manifest.tsv").read_bytes()
        jpg = (self.work / "batch_001" / "img_00001.jpg").read_bytes()
        other = self.root / "other"
        other.mkdir()
        write_dicom(other / "c.dcm", _good_pixels() // 2)
        with self.assertRaises(WorkDirExists):
            self.run_quietly([other])
        self.assertEqual((self.work / "manifest.tsv").read_bytes(), manifest)
        self.assertEqual((self.work / "batch_001" / "img_00001.jpg").read_bytes(), jpg)
        self.assertFalse(self.staging.exists())

    def test_work_dir_that_is_a_file_is_refused(self):
        self.work.write_text("x")
        with self.assertRaises(WorkDirExists):
            self.run_quietly()
        self.assertEqual(self.work.read_text(), "x")

    def test_existing_empty_directory_is_used(self):
        self.work.mkdir()
        result = self.run_quietly()
        self.assertEqual(result.written, 2)
        self.assertTrue((self.work / "manifest.tsv").exists())
        self.assertFalse(self.staging.exists())

    def test_result_paths_refer_to_the_final_location(self):
        result = self.run_quietly()
        self.assertEqual(result.skipped_path, self.work / "skipped.tsv")
        self.assertTrue(result.skipped_path.exists())

    def test_interrupt_leaves_neither_work_dir_nor_staging(self):
        real = preprocess_module.render_and_encode
        calls = []

        def interrupt_on_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        with (
            quiet(),
            mock.patch("image_review.preprocess.render_and_encode", side_effect=interrupt_on_second),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_preprocess([self.src], self.work)
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.work.exists())
        self.assertFalse(self.staging.exists())

    def test_leftover_staging_dir_is_named_in_the_error(self):
        self.staging.mkdir()
        with self.assertRaises(WorkDirExists) as ctx:
            self.run_quietly()
        self.assertIn(str(self.staging), str(ctx.exception))
        self.assertFalse(self.work.exists())
        self.assertTrue(self.staging.exists())

    def test_work_dir_is_private(self):
        self.run_quietly()
        self.assertEqual(stat.S_IMODE(self.work.stat().st_mode), 0o700)


def _halves(img: np.ndarray, width: int) -> tuple[np.ndarray, np.ndarray]:
    """Split a side-by-side (left | gap | right) image into its two views."""
    return img[:, :width], img[:, width + SIDE_BY_SIDE_GAP :]


def _decode_joined(data: bytes) -> tuple[np.ndarray, tuple[Note, ...]]:
    """`decode_raster` with its views placed side by side, as `preprocess_raster` places them."""
    views, notes = decode_raster(data)
    return preprocess_module._side_by_side(list(views), fill=0.5) if len(views) > 1 else views[0], notes


def _decoded(data: bytes) -> np.ndarray:
    return _decode_joined(data)[0]


def _gradient_jpeg(**options) -> bytes:
    """A JPEG with real entropy-coded data (a gradient, not a flat image), saved with Pillow `options`."""
    buf = io.BytesIO()
    Image.linear_gradient("L").resize((200, 150)).convert("RGB").save(buf, "JPEG", **options)
    return buf.getvalue()


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def _mpo_with_frame1_at(mpo: bytes, offset: int) -> bytes:
    """`mpo` (from `_mpo_bytes`) with its second MPF entry's DataOffset set to `offset` (relative, as stored)."""
    with Image.open(io.BytesIO(mpo)) as im:
        entry = im.mpinfo[0xB002][1]
    old = struct.pack("<LL", entry["Size"], entry["DataOffset"])
    assert mpo.count(old) == 1
    return mpo.replace(old, struct.pack("<LL", entry["Size"], offset))


class ImageEndTest(unittest.TestCase):
    """`_jpeg_end`, `_png_end` and `_mpo_unrendered` find where an image's rendered bytes end."""

    def test_jpeg_end_is_the_file_end_for_whole_files(self):
        for name, options in (
            ("baseline", {}),
            ("progressive", {"progressive": True}),
            ("restart blocks", {"restart_marker_blocks": 1}),
            ("restart rows", {"restart_marker_rows": 1}),
        ):
            with self.subTest(name):
                data = _gradient_jpeg(**options)
                self.assertEqual(_jpeg_end(data), len(data))
                self.assertEqual(_jpeg_end(data + data), len(data))  # the first image's end
                self.assertEqual(_jpeg_end(b"xyz" + data, 3), 3 + len(data))
        self.assertIn(b"\xff\xd0", _gradient_jpeg(restart_marker_blocks=1))
        self.assertEqual(_gradient_jpeg(progressive=True).count(b"\xff\xda"), 10)  # several scans

    def test_jpeg_fill_bytes_stray_bytes_and_short_lengths_are_passed_over(self):
        data = _gradient_jpeg()
        sos = data.index(b"\xff\xda")
        for name, patched in (
            ("fill before SOS", data[:sos] + b"\xff\xff\xff" + data[sos:]),
            ("fill before EOI", data[:-2] + b"\xff\xff" + data[-2:]),
            ("stray before SOS", data[:sos] + b"\x00\x12" + data[sos:]),
            ("APP1 length 0", data[:2] + b"\xff\xe1\x00\x00" + data[2:]),
            ("APP0 length 1", data[:2] + b"\xff\xe0\x00\x01" + data[2:]),
            ("COM length 0", data[:2] + b"\xff\xfe\x00\x00" + data[2:]),
            ("COM length 1", data[:2] + b"\xff\xfe\x00\x01" + data[2:]),
        ):
            with self.subTest(name):
                self.assertEqual(_jpeg_end(patched), len(patched))
                self.assertEqual(_jpeg_end(patched + data), len(patched))

    def test_jpeg_end_is_none_when_truncated_or_malformed(self):
        data = _gradient_jpeg()
        sos = data.index(b"\xff\xda")
        for name, bad in (
            ("empty", b""),
            ("not a JPEG", b"not a jpeg at all"),
            ("SOI only", b"\xff\xd8"),
            ("no EOI", data[:-2]),
            ("cut in the header", data[:sos]),
            ("cut in a segment", data[:30]),
            ("cut in a length", data[:5]),
            ("length past the end", b"\xff\xd8\xff\xe0\xff\xff\xff\xd9"),
            ("nested SOI", data[:sos] + b"\xff\xd8" + data[sos:]),
            ("garbage after SOI", b"\xff\xd8" + bytes(range(256)) * 4),
        ):
            with self.subTest(name):
                self.assertIsNone(_jpeg_end(bad))
        self.assertIsNone(_jpeg_end(data, len(data) + 10))

    def test_png_end_is_where_pillow_stops_reading(self):
        png = _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8))
        with_chunk = _png_with_exif_after_pixels(png, _exif_ifd1([]))  # a chunk after the pixels
        huge = png[:8] + struct.pack(">I4s", 0xFFFFFFFF, b"tEXt") + png[8:]
        for name, data, end in (
            ("whole", png, len(png)),
            ("bytes after IEND", png + b"tail", len(png)),
            ("a chunk after the pixels", with_chunk + b"tail", len(with_chunk)),
            ("no IEND", png[:-12], len(png) - 12),
            ("IEND's CRC cut", png[:-1], len(png) - 1),
            ("an invalid chunk type", png[:-12] + b"Patient: DOE^JANE " * 3, len(png) - 12),
            ("a JPEG before IEND", png[:-12] + _gradient_jpeg() + png[-12:], len(png) - 12),
            ("a chunk past the end", huge, 8),
            ("a chunk header cut", png[:-12] + png[-12:-6], len(png) - 12),
            ("signature only", png[:8], 8),
            ("an IEND with data", png[:-12] + _png_chunk(b"IEND", b"secret"), len(png) - 4),
            ("an IEND past the end", png[:-12] + struct.pack(">I4s", 0xFFFFFFF0, b"IEND") + b"secret", len(png) - 4),
        ):
            with self.subTest(name):
                self.assertEqual(_png_end(data), end)

    def test_jpeg_end_spends_its_budget(self):
        data = _gradient_jpeg(progressive=True)
        budget = _WalkBudget(markers=10**6, search_bytes=10**9)
        self.assertEqual(_jpeg_end(data, 0, budget), len(data))
        markers, searched = 10**6 - budget.markers, 10**9 - budget.search_bytes
        self.assertGreater(markers, 10)  # one per segment, through every scan
        self.assertLess(searched, len(data))  # each byte searched at most once, segments skipped
        for name, budget in (
            ("too few markers", _WalkBudget(markers=markers - 1, search_bytes=10**9)),
            ("too few bytes", _WalkBudget(markers=10**6, search_bytes=searched - 1)),
        ):
            with self.subTest(name), self.assertRaises(preprocess_module._OverBudget):
                _jpeg_end(data, 0, budget)
        self.assertEqual(_jpeg_end(data, 0, _WalkBudget(markers=markers, search_bytes=searched)), len(data))

    def test_uncovered_runs(self):
        self.assertEqual(_uncovered(10, [(5, 7), (0, 3)]), ((3, 5), (7, 10)))
        self.assertEqual(_uncovered(10, [(0, 6), (2, 4), (6, 10)]), ())
        self.assertEqual(_uncovered(10, [(0, 4), (2, 8)]), ((8, 10),))

    def test_mpo_frames_cover_their_own_ranges_only(self):
        mpo = _mpo_bytes()
        first = _jpeg_end(mpo)
        assert first is not None
        self.assertEqual(_mpo_unrendered(mpo, first, budget=_WalkBudget.for_data(mpo), offsets=(0, first)), ())
        self.assertEqual(
            _mpo_unrendered(mpo, first, budget=_WalkBudget.for_data(mpo), offsets=(0, first, first, 0)), ()
        )  # repeats walked once
        for name, offsets in (
            ("no frames", ()),
            ("first only", (0,)),
            ("not at FF D8 FF", (0, first + 1)),
            ("past the end", (0, 10**9)),
        ):
            with self.subTest(name):
                self.assertEqual(
                    _mpo_unrendered(mpo, first, budget=_WalkBudget.for_data(mpo), offsets=offsets), ((first, len(mpo)),)
                )
        twice, second = mpo + b"gap" + mpo, len(mpo) + 3
        self.assertEqual(
            _mpo_unrendered(twice, first, budget=_WalkBudget.for_data(twice), offsets=(0, second)),
            ((first, second), (second + first, len(twice))),
        )  # the gap and the first file's second frame, then the second file's

    def test_unrendered_details(self):
        dicom_head = bytes(128) + b"DICM" + b"\x02\x00"
        for tail, detail in (
            (bytes(10), "10 bytes after the JPEG end; all zero bytes"),
            (_gradient_jpeg()[:20], "20 bytes after the JPEG end; they begin with a JPEG signature"),
            (PNG_MAGIC + b"x", "9 bytes after the JPEG end; they begin with a PNG signature"),
            (dicom_head, "134 bytes after the JPEG end; they begin with a DICOM preamble"),
            (b"GIF89a\x01\x00", "8 bytes after the JPEG end; they begin with another image signature"),
            (b"\tsecret\nname\x00", "13 bytes after the JPEG end"),  # never the bytes themselves
        ):
            with self.subTest(detail=detail):
                data = b"image" + tail
                self.assertEqual(_unrendered_detail(data, ((5, len(data)),), "after the JPEG end"), detail)
        data = b"ab\x00\x00cd\x00e"
        self.assertEqual(_unrendered_detail(data, ((2, 4), (6, 7)), "x"), "3 bytes x; all zero bytes")
        self.assertEqual(_unrendered_detail(data, ((2, 4), (7, 8)), "x"), "3 bytes x")


class TrailingDataTest(unittest.TestCase):
    """Bytes of a JPEG or PNG that are not rendered are noted, and change nothing else."""

    def assert_noted(self, data: bytes, tail: bytes, detail: str, before: tuple[Note, ...] = ()):
        self.assertEqual(decode_raster(data)[1], before)
        self.assert_renders_as(data + tail, data, detail, before)

    def assert_renders_as(self, data: bytes, plain: bytes, detail: str, before: tuple[Note, ...] = ()):
        """`data` renders as `plain` does, with the `trailing-data` note `detail` after the notes `before`."""
        views, notes = decode_raster(data)
        self.assertEqual(notes, (*before, Note("trailing-data", detail)))
        plain_views = decode_raster(plain)[0]
        self.assertEqual(len(views), len(plain_views))
        for view, plain_view in zip(views, plain_views, strict=True):
            np.testing.assert_array_equal(view, plain_view)
        np.testing.assert_array_equal(
            render("raster", "id", data, "inferno").rgb, render("raster", "id", plain, "inferno").rgb
        )

    def test_a_second_jpeg_appended_to_a_jpeg(self):
        second = _jpeg_bytes(np.zeros((8, 8, 3), dtype=np.uint8))
        self.assert_noted(
            _gradient_jpeg(), second, f"{len(second)} bytes after the JPEG end; they begin with a JPEG signature"
        )

    def test_a_segment_of_length_0_or_1_does_not_hide_an_appended_jpeg(self):
        data, second = _gradient_jpeg(), _jpeg_bytes(np.zeros((8, 8, 3), dtype=np.uint8))
        detail = f"{len(second)} bytes after the JPEG end; they begin with a JPEG signature"
        for segment in (b"\xff\xe1\x00\x00", b"\xff\xe0\x00\x01", b"\xff\xfe\x00\x00", b"\xff\xfe\x00\x01"):
            with self.subTest(segment=segment.hex()):
                patched = data[:2] + segment + data[2:]
                self.assert_renders_as(patched + second, patched, detail)

    def test_a_jpeg_whose_end_is_not_found_is_noted(self):
        data, second = _gradient_jpeg(), _jpeg_bytes(np.zeros((8, 8, 3), dtype=np.uint8))
        for name, bad in (
            ("text instead of EOI", data[:-2] + b"secret patient name"),
            ("a comment swallowing EOI", data[:-2] + b"\xff\xfe\xff\xff" + data[-2:] + second),
        ):
            with self.subTest(name):
                self.assertEqual(decode_raster(bad)[1], (Note("trailing-data", "JPEG end not found"),))
        for bad in (data[:-2] + second, data[:-300]):  # a nested SOI, a cut scan: Pillow fails these
            self.assertEqual(_trailing_data_notes(bad, "JPEG"), (Note("trailing-data", "JPEG end not found"),))

    def test_bytes_after_a_png(self):
        for name, png in (
            ("8-bit", _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8))),
            ("16-bit", imagecodecs.png_encode(np.full((40, 60, 3), 4000, dtype=np.uint16))),
        ):
            with self.subTest(name):
                self.assert_noted(png, b"secret", "6 bytes after the PNG end")
                self.assert_noted(png, PNG_MAGIC, "8 bytes after the PNG end; they begin with a PNG signature")

    def test_bytes_where_pillow_stops_reading_a_png(self):
        png, jpeg = _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)), _gradient_jpeg()
        text = b"Patient: DOE^JANE MRN 12345 " * 3
        for name, data, detail in (
            ("text instead of IEND", png[:-12] + text, f"{len(text)} bytes after the PNG end"),
            (
                "a JPEG before IEND",
                png[:-12] + jpeg + png[-12:],
                f"{len(jpeg) + 12} bytes after the PNG end; they begin with a JPEG signature",
            ),
            (
                "a JPEG instead of IEND",
                png[:-12] + jpeg,
                f"{len(jpeg)} bytes after the PNG end; they begin with a JPEG signature",
            ),
        ):
            with self.subTest(name):
                self.assert_renders_as(data, png, detail)
        for name, data in (("no IEND", png[:-12]), ("IEND's CRC cut", png[:-2])):
            with self.subTest(name):
                self.assertEqual(decode_raster(data)[1], ())

    def test_data_inside_iend_is_noted(self):
        jpeg = _gradient_jpeg()
        for name, png in (
            ("8-bit", _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8))),
            ("16-bit", imagecodecs.png_encode(np.full((40, 60, 3), 4000, dtype=np.uint16))),
        ):
            for iend, count in (
                (_png_chunk(b"IEND", jpeg), len(jpeg) + 4),  # its data and CRC
                (struct.pack(">I4s", 0xFFFFFFF0, b"IEND") + jpeg, len(jpeg)),  # a length past the end
            ):
                with self.subTest(name, count=count):
                    detail = f"{count} bytes after the PNG end; they begin with a JPEG signature"
                    self.assert_renders_as(png[:-12] + iend, png, detail)

    def test_zero_padding_is_noted(self):
        self.assert_noted(_gradient_jpeg(), bytes(4096), "4096 bytes after the JPEG end; all zero bytes")

    def test_progressive_jpeg_and_restart_markers_have_no_note(self):
        for options in ({"progressive": True}, {"restart_marker_blocks": 1}):
            with self.subTest(options=options):
                self.assertEqual(decode_raster(_gradient_jpeg(**options))[1], ())

    def test_mpo_frames_are_not_trailing_data(self):
        self.assertEqual(decode_raster(_mpo_bytes())[1], ())
        self.assert_noted(_mpo_bytes(), b"junk", "4 bytes after the MPO end")
        exif = _exif_with_thumbnail()
        thumbnail = Note("embedded-thumbnail", f"frame 1: EXIF IFD1 thumbnail ({_thumbnail_len(exif)} bytes)")
        self.assert_noted(_mpo_bytes(exif), bytes(3), "3 bytes after the MPO end; all zero bytes", (thumbnail,))

    def test_ultra_hdr_renders_both_frames_with_no_note(self):
        uhdr = _with_hdrgm_xmp(_mpo_bytes())
        views, notes = decode_raster(uhdr)
        self.assertEqual(notes, ())
        self.assertEqual(views[0].shape, (40, 60 + SIDE_BY_SIDE_GAP + 30, 3))
        plain = decode_raster(_mpo_bytes())[0]
        np.testing.assert_array_equal(views[0], plain[0])  # as an MPO without the XMP is
        self.assert_noted(uhdr, b"junk", "4 bytes after the MPO end")

    def test_ultra_hdr_with_malformed_mpf_renders_its_primary_image(self):
        mpo = _mpo_bytes()
        mpf = mpo.index(b"MPF\x00") + 4
        bad = _with_hdrgm_xmp(mpo[:mpf] + b"XX*\x00" + mpo[mpf + 4 :])  # an MPF byte order that is neither II nor MM
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            views, notes = decode_raster(bad)
        first = _jpeg_end(mpo)
        assert first is not None
        detail = f"{len(mpo) - first} bytes after the JPEG end; they begin with a JPEG signature"
        self.assertEqual(views[0].shape, (40, 60, 3))
        self.assertEqual(notes, (Note("trailing-data", detail),))

    def test_bytes_between_mpo_frames_are_noted(self):
        mpo, jpeg = _mpo_bytes(), _gradient_jpeg()
        first = _jpeg_end(mpo)
        assert first is not None
        with Image.open(io.BytesIO(mpo)) as im:
            offset = im.mpinfo[0xB002][1]["DataOffset"]
        inserted = _mpo_with_frame1_at(mpo, offset + len(jpeg))
        self.assert_renders_as(
            inserted[:first] + jpeg + inserted[first:],
            mpo,
            f"{len(jpeg)} bytes between or after the MPO's frames; they begin with a JPEG signature",
        )
        repointed = _mpo_with_frame1_at(mpo, offset + len(mpo) - first) + jpeg  # the second frame is left in a gap
        _, notes = decode_raster(repointed)
        detail = f"{len(mpo) - first} bytes between or after the MPO's frames; they begin with a JPEG signature"
        self.assertEqual(notes, (Note("trailing-data", detail),))

    def test_mpo_with_unreadable_frame_offsets_falls_back_to_the_first_eoi(self):
        mpo = _mpo_bytes()
        first = _jpeg_end(mpo)
        assert first is not None
        with Image.open(io.BytesIO(mpo)) as im:
            self.assertEqual(preprocess_module._mpf_offsets(im), (0, first))  # Pillow's own, absolute
            for offsets in (None, [None], "x"):
                with self.subTest(offsets=offsets):
                    im._MpoImageFile__mpoffsets = offsets
                    self.assertEqual(preprocess_module._mpf_offsets(im), ())
            del im._MpoImageFile__mpoffsets
            self.assertEqual(preprocess_module._mpf_offsets(im), ())
        self.assertEqual(
            _trailing_data_notes(mpo, "MPO"),
            (Note("trailing-data", f"{len(mpo) - first} bytes after the MPO end; they begin with a JPEG signature"),),
        )

    def test_a_check_over_budget_is_a_fixed_text_note_and_the_input_still_renders(self):
        data, mpo = _gradient_jpeg(), _mpo_bytes()
        note = (Note("trailing-data", "not checked: too many JPEG markers or frames"),)
        for name, patches in (
            ("markers", {"JPEG_WALK_MAX_MARKERS": 3}),
            ("bytes", {"JPEG_WALK_SEARCH_FACTOR": 0, "JPEG_WALK_SEARCH_SLACK": 10}),
        ):
            for plain in (data, mpo):
                with self.subTest(name, mpo=plain is mpo), mock.patch.multiple(preprocess_module, **patches):
                    views, notes = decode_raster(plain + b"junk")
                    self.assertEqual(notes, note)
                    for view, plain_view in zip(views, decode_raster(plain)[0], strict=True):
                        np.testing.assert_array_equal(view, plain_view)

    def test_other_formats_are_not_checked(self):
        buf = io.BytesIO()
        Image.new("RGB", (60, 40)).save(buf, "GIF")
        self.assertEqual(decode_raster(buf.getvalue() + b"secret")[1], ())
        self.assertEqual(_trailing_data_notes(b"anything", "TIFF"), ())

    def test_a_failure_is_a_fixed_text_note_and_the_input_still_renders(self):
        data = _gradient_jpeg() + b"junk"
        with mock.patch.object(preprocess_module, "_jpeg_end", side_effect=RuntimeError("tab\there")):
            views, notes = decode_raster(data)
        self.assertEqual(notes, (Note("trailing-data", "trailing data unreadable: RuntimeError"),))
        np.testing.assert_array_equal(views[0], decode_raster(_gradient_jpeg())[0][0])


class WindowTest(unittest.TestCase):
    def test_extreme_pixel_leaves_the_content_a_real_range(self):
        img = np.random.default_rng(0).integers(0, 4096, (400, 400)).astype(np.float32)
        img[0, 0] = 2**24
        out = _window(img)
        self.assertEqual(out[0, 0], 1)
        content = out.ravel()[1:]
        self.assertGreater(content.max() - content.min(), 0.99)  # not squeezed against 0 as min-max would
        self.assertGreater(len(np.unique(content)), 1000)

    def test_two_valued_sparse_image_stays_min_max(self):
        img = np.zeros((100, 100), dtype=np.float32)
        img[:2, :2] = 4000
        img[5, 5:8] = 2000
        np.testing.assert_array_equal(_window(img), img / 4000)

    def test_window_values_by_hand(self):
        img = np.arange(1, 201, dtype=np.float32)  # 1..200, one of each
        # The trim of 0.01% of 199 leaves 2..199; their 1% and 99% quantiles are 3.97 and 197.03, shrunk by 2% of
        # 193.06 (3.8612) to 7.8312 and 193.1688
        out = _window(img)
        expected = [0, (8 - 7.8312) / 185.3376, (101 - 7.8312) / 185.3376, 1]
        np.testing.assert_allclose(out[[0, 7, 100, 199]], expected, atol=1e-5)

    def test_window_clips_at_the_robust_range(self):
        img = (np.arange(10000).reshape(100, 100) % 1000).astype(np.float32)
        img[0, 0], img[0, 1] = 0, 1000
        img[1, :3] = [-3000, 3000, 2500]
        lo, hi = float(img.min()), float(img.max())
        margin = 0.0001 * (hi - lo)
        inner = img[(img > lo + margin) & (img < hi - margin)]
        bot, top = np.quantile(inner, [0.01, 0.99])
        shrink = 0.02 * (top - bot)
        bot, top = bot + shrink, top - shrink
        expected = np.clip((img - bot) / (top - bot), 0, 1).astype(np.float32)
        np.testing.assert_allclose(_window(img), expected, atol=1e-6)
        np.testing.assert_array_equal(_window(img)[1, :3], [0, 1, 1])  # the outliers clip

    def test_text_in_the_clipped_tail_is_not_separated(self):
        # Known limit of clipping: bright text on a bright border above the window both come out 1
        rng = np.random.default_rng(1)
        img = rng.integers(0, 1001, (200, 200)).astype(np.float32)
        band = np.zeros((200, 200), dtype=bool)
        band[:, :12] = band[:, -12:] = True
        img[band] = 3900
        text = np.zeros_like(band)
        text[20:24, 2:10] = True
        img[text] = 4095
        out = _window(img)
        self.assertTrue((out[text] == 1).all())
        self.assertTrue((out[band] == 1).all())


class DecodeRasterTest(unittest.TestCase):
    def test_cmyk_jpeg_keeps_black_text(self):
        cmyk = np.zeros((40, 60, 4), dtype=np.uint8)
        cmyk[_text_mask(), 3] = 255  # text only in K
        buf = io.BytesIO()
        Image.fromarray(cmyk, mode="CMYK").save(buf, "JPEG", quality=95)
        img = _decoded(buf.getvalue())
        self.assertEqual(img.shape, (40, 60, 3))
        self.assertLess(img[_text_mask()].mean(), 0.25)
        self.assertGreater(img[~_text_mask()].mean(), 0.75)

    def test_alpha_only_text_is_visible_in_the_alpha_view_whatever_the_colour(self):
        alpha = np.where(_text_mask(), 255, 0).astype(np.uint8)
        rgba = np.dstack([np.full((40, 60, 3), 128, dtype=np.uint8), alpha])
        la = np.dstack([np.full((40, 60), 128, dtype=np.uint8), alpha])
        for arr, mode in ((rgba, "RGBA"), (la, "LA")):
            with self.subTest(mode=mode):
                img, notes = _decode_joined(_png_bytes(arr, mode))
                self.assertEqual(
                    img.shape, (40, 120 + SIDE_BY_SIDE_GAP, 3) if mode == "RGBA" else (40, 120 + SIDE_BY_SIDE_GAP)
                )
                left, right = _halves(img, 60)
                self.assertTrue((left == np.float32(128 / 255)).all())  # the colour view shows nothing
                self.assertTrue((right[_text_mask()] == 1).all())
                self.assertTrue((right[~_text_mask()] == 0).all())
                self.assertEqual(notes, (Note("alpha", f"{int((~_text_mask()).sum())} of 2400 pixels not opaque"),))
                out = render("raster", "id", _png_bytes(arr, mode), "inferno").rgb  # final uint8, after CLAHE for LA
                out_right = out[:, 60 + SIDE_BY_SIDE_GAP :]
                contrast = int(out_right[_text_mask()].min()) - int(out_right[~_text_mask()].max())
                self.assertGreaterEqual(contrast, 200)

    def test_alpha_view_is_rendered_linearly(self):
        for level, want in ((0, 0), (255, 255), (128, 128)):
            with self.subTest(alpha=level):
                alpha = np.full((40, 60), level, dtype=np.uint8)
                alpha[0, 0] = 0 if level else 255  # keeps the image transparent, or not constant
                rgba = np.dstack([RNG.integers(0, 255, (40, 60, 3), dtype=np.uint8), alpha])
                out = render("raster", "id", _png_bytes(rgba, "RGBA"), "x").rgb
                self.assertEqual(out.shape, (40, 120 + SIDE_BY_SIDE_GAP, 3))
                alpha_view = _halves(out, 60)[1]
                np.testing.assert_array_equal(alpha_view[1:, 1:], want)
                np.testing.assert_array_equal(alpha_view[0, 0], 0 if level else 255)
                np.testing.assert_array_equal(alpha_view[..., 0], alpha_view[..., 2])

    def test_text_under_transparent_pixels_visible_in_colour_half(self):
        rgb = np.full((40, 60, 3), 255, dtype=np.uint8)
        rgb[_text_mask()] = 0
        rgba = np.dstack([rgb, np.zeros((40, 60), dtype=np.uint8)])  # fully transparent: an erased layer
        img, notes = _decode_joined(_png_bytes(rgba, "RGBA"))
        left, right = _halves(img, 60)
        self.assertTrue((left[_text_mask()] == 0).all())
        self.assertTrue((left[~_text_mask()] == 1).all())
        self.assertTrue((right == 0).all())  # transparent = black
        self.assertEqual(notes, (Note("alpha", "2400 of 2400 pixels not opaque"),))
        out = render("raster", "id", _png_bytes(rgba, "RGBA"), "inferno")
        self.assertEqual(out.rgb.shape, (40, 120 + SIDE_BY_SIDE_GAP, 3))
        self.assertEqual(out.notes, notes)

    def test_opaque_alpha_is_dropped(self):
        rgb = RNG.integers(0, 255, (40, 60, 3), dtype=np.uint8)
        rgba = np.dstack([rgb, np.full((40, 60), 255, dtype=np.uint8)])
        img, notes = _decode_joined(_png_bytes(rgba, "RGBA"))
        np.testing.assert_array_equal(ski.util.img_as_ubyte(img), rgb)
        self.assertEqual(notes, ())
        np.testing.assert_array_equal(
            render("raster", "id", _png_bytes(rgba, "RGBA"), "x").rgb, render("raster", "id", _png_bytes(rgb), "x").rgb
        )

    def test_16_bit_grayscale_png_stays_grayscale(self):
        gray = (np.arange(40 * 60).reshape(40, 60) * 20).astype(np.uint16)
        img = _decoded(_png_bytes(gray))
        self.assertEqual(img.ndim, 2)
        # Windowed like a DICOM's grayscale, not scaled by 65535
        np.testing.assert_array_equal(img, _window(gray.astype(np.float32)))
        out = render("raster", "id", _png_bytes(gray), "inferno")
        self.assertEqual(out.rgb.shape, (40, 60, 3))

    def test_16_bit_grayscale_with_trns_is_windowed(self):
        gray = np.full((40, 60), 30000, dtype=np.uint16)
        gray[_text_mask()] = 50000
        gray[:5, :5] = 0  # the transparent key
        buf = io.BytesIO()
        Image.fromarray(gray).save(buf, "PNG", transparency=0)
        img, notes = _decode_joined(buf.getvalue())
        self.assertEqual(img.ndim, 2)
        left, right = _halves(img, 60)
        np.testing.assert_array_equal(left, _window(gray.astype(np.float32)))  # not scaled by 65535
        self.assertEqual(left[0, 0], 0)  # the key's own value, in the colour view
        np.testing.assert_array_equal(right, (gray != 0).astype(np.float32))  # binary alpha
        self.assertEqual(notes, (Note("alpha", "25 of 2400 pixels not opaque"),))

    def test_high_bit_grayscale_with_an_extreme_pixel_shows_the_text(self):
        rng = np.random.default_rng(7)
        field = rng.integers(0, 1001, (400, 400))
        field[150:250, 100:300] = 4000  # text, within 0..4095
        cases = {
            "mode I TIFF": (lambda a: Image.fromarray(a.astype(np.int32), "I"), "TIFF", 2**24),
            "I;16 PNG": (lambda a: Image.fromarray(a.astype(np.uint16)), "PNG", 65535),
        }
        for name, (make, fmt, extreme) in cases.items():
            with self.subTest(name):
                means = []
                for outlier in (False, True):
                    pixels = field.copy()
                    if outlier:
                        pixels[0, 0] = extreme
                    buf = io.BytesIO()
                    make(pixels).save(buf, fmt)
                    out = render("raster", "id", buf.getvalue(), "x").rgb
                    means.append((out[150:250, 100:300].mean(), out[300:, :].mean(), (out[300:] == 0).mean()))
                (text, _, black), (text_out, background_out, black_out) = means
                self.assertGreaterEqual(text_out - background_out, 40)
                # The outlier is clipped; the window moves a little with it, but the text stays near white
                self.assertAlmostEqual(text_out, text, delta=20)
                self.assertLess(black_out, black + 0.1)  # the field keeps its range: it is not clipped to black

    def test_hot_pixel_over_a_bright_structure_shows_the_text(self):
        for share in (0.2, 0.6):
            with self.subTest(structure=share):
                rng = np.random.default_rng(5)
                gray = rng.integers(190, 211, (400, 400)).astype(np.uint16)
                gray[150:250, 100:300] = 600  # text
                start = round(400 * (1 - share))
                gray[:, start:] = rng.integers(900, 1001, (400, 400 - start))  # a structure over most of a side
                gray[0, 0] = 65535
                out = render("raster", "id", _png_bytes(gray), "x").rgb
                background = out[300:, : start - 1]
                self.assertGreaterEqual(out[150:250, 100:300].mean() - background.mean(), 40)

    def test_caption_on_black_with_an_extreme_pixel_shows_the_text(self):
        pixels = np.zeros((400, 400), dtype=np.int32)
        caption = np.zeros((400, 400), dtype=bool)
        caption[100:110, 100:300] = True  # 1.25% of the pixels: more than the window's 1%
        pixels[caption] = 4000
        pixels[0, 0] = 2**24
        buf = io.BytesIO()
        Image.fromarray(pixels, "I").save(buf, "TIFF")
        out = render("raster", "id", buf.getvalue(), "x").rgb
        self.assertGreaterEqual(out[caption].mean() - out[~caption].mean(), 200)

    def test_label_box_over_noise_leaves_the_glyphs_visible(self):
        rng = np.random.default_rng(7)
        gray = rng.integers(0, 10001, (400, 400)).astype(np.uint16)
        gray[:80, :75] = 60000  # a label box of 3.75% of the pixels
        glyphs = np.zeros((400, 400), dtype=bool)
        glyphs[10:70:6, 5:70] = True
        gray[glyphs] = 50000
        out = render("raster", "id", _png_bytes(gray), "x").rgb
        self.assertGreaterEqual(out[glyphs].mean() - out[300:, :].mean(), 40)

    def test_16_bit_gray_alpha_png_with_an_extreme_pixel_shows_the_text(self):
        gray = np.random.default_rng(7).integers(0, 1001, (400, 400)).astype(np.uint16)
        gray[150:250, 100:300] = 4000
        gray[0, 0] = 65535
        out = render("raster", "id", imagecodecs.png_encode(np.dstack([gray, np.full_like(gray, 65535)])), "x").rgb
        self.assertGreaterEqual(out[150:250, 100:300].mean() - out[300:, :].mean(), 40)

    def test_mpo_frames_render_side_by_side(self):
        buf = io.BytesIO()
        primary = Image.new("RGB", (60, 40), (255, 255, 255))
        gain_map = Image.new("L", (30, 20), 128)
        primary.save(buf, "MPO", save_all=True, append_images=[gain_map])
        img = _decoded(buf.getvalue())
        self.assertEqual(img.shape, (40, 60 + SIDE_BY_SIDE_GAP + 30, 3))
        left, right = _halves(img, 60)
        self.assertGreater(left.min(), 0.95)
        self.assertAlmostEqual(float(right[:20].mean()), 128 / 255, delta=0.02)
        self.assertTrue((right[20:] == 0).all())  # padded below the shorter frame
        out = render("raster", "id", buf.getvalue(), "inferno")
        self.assertEqual(out.rgb.shape, (40, 94, 3))

    def test_exif_thumbnail_in_jpeg_and_png(self):
        exif = _exif_with_thumbnail()
        for fmt in ("JPEG", "PNG"):
            with self.subTest(fmt=fmt):
                buf = io.BytesIO()
                Image.new("RGB", (60, 40), (9, 9, 9)).save(buf, fmt, exif=exif)
                _, notes = decode_raster(buf.getvalue())
                self.assertEqual(
                    notes, (Note("embedded-thumbnail", f"EXIF IFD1 thumbnail ({_thumbnail_len(exif)} bytes)"),)
                )

    def test_png_exif_chunk_after_the_pixels_is_found(self):
        exif = _exif_with_thumbnail()
        note = Note("embedded-thumbnail", f"EXIF IFD1 thumbnail ({_thumbnail_len(exif)} bytes)")
        plain = _png_bytes(np.full((40, 60, 3), 9, dtype=np.uint8))
        self.assertEqual(decode_raster(_png_with_exif_after_pixels(plain, exif))[1], (note,))
        rgb = np.zeros((40, 60, 3), dtype=np.uint16)
        rgb[0, 0] = [4000, 1, 2]
        views, notes = decode_raster(_png_with_exif_after_pixels(imagecodecs.png_encode(rgb), exif))
        self.assertEqual(notes, (note,))
        np.testing.assert_array_equal(views[0][0, 0], np.array([4000, 1, 2], dtype=np.float32) / 65535)  # full depth

    def test_png_with_a_broken_pixel_stream_still_fails(self):
        noise = np.random.default_rng(0).integers(0, 256, (200, 300, 3), dtype=np.uint8)
        png = _png_bytes(noise)
        out, pos, idats = [png[:8]], 8, 0
        while pos < len(png):
            (length,) = struct.unpack(">I", png[pos : pos + 4])
            kind, body = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
            if kind == b"IDAT":
                body, idats = body[: len(body) // 2], idats + 1  # half the stream, a valid CRC
                out.append(struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body)))
            else:
                out.append(png[pos : pos + 12 + length])
            pos += 12 + length
        self.assertEqual(idats, 3)
        row = render_and_encode(Candidate("id", "raster", b"".join(out)), "inferno", draw_overlays=False)
        self.assertIsInstance(row, SkippedRow)
        self.assertEqual(row.kind, "failed")

    def test_every_png_exif_chunk_is_checked(self):
        thumb = _exif_with_thumbnail()
        note = Note("embedded-thumbnail", f"EXIF IFD1 thumbnail ({_thumbnail_len(thumb)} bytes)")
        harmless = _exif_ifd1([])
        plain = _png_bytes(np.full((40, 60, 3), 9, dtype=np.uint8))
        for first, second in ((harmless, thumb), (thumb, harmless)):
            with self.subTest(thumbnail_first=first is thumb):
                data = _png_with_exif_after_pixels(_png_with_exif_after_pixels(plain, first), second)
                self.assertEqual(decode_raster(data)[1], (note,))

    def test_imagemagick_raw_profile_exif_is_read(self):
        exif = _exif_with_thumbnail()
        note = Note("embedded-thumbnail", f"EXIF IFD1 thumbnail ({_thumbnail_len(exif)} bytes)")
        for name, profile in (("with prefix", exif), ("without prefix", exif.removeprefix(b"Exif\x00\x00"))):
            with self.subTest(name):
                info = PngImagePlugin.PngInfo()
                info.add_text("Raw profile type exif", f"\nexif\n{len(profile):8d}\n{profile.hex()}\n")
                buf = io.BytesIO()
                Image.new("RGB", (60, 40)).save(buf, "PNG", pnginfo=info)
                self.assertEqual(decode_raster(buf.getvalue())[1], (note,))
        info = PngImagePlugin.PngInfo()
        info.add_text("Raw profile type exif", "\nexif\n       4\nzz\n")
        buf = io.BytesIO()
        Image.new("RGB", (60, 40)).save(buf, "PNG", pnginfo=info)
        self.assertEqual(decode_raster(buf.getvalue())[1], (Note("embedded-thumbnail", "EXIF unreadable: ValueError"),))

    def test_thumbnail_forms(self):
        for entries, detail in (
            ([(0x0201, 40)], "EXIF IFD1 thumbnail (? bytes)"),
            ([(0x0201, 40), (0x0202, 77)], "EXIF IFD1 thumbnail (77 bytes)"),
            ([(0x0111, 40), (0x0117, 300)], "EXIF IFD1 thumbnail, uncompressed (300 bytes)"),
            ([(0x0111, 40)], "EXIF IFD1 thumbnail, uncompressed (? bytes)"),
            ([(0x0202, 77)], None),  # a length alone is no thumbnail
        ):
            with self.subTest(entries=entries):
                buf = io.BytesIO()
                Image.new("RGB", (60, 40)).save(buf, "JPEG", exif=_exif_ifd1(entries))
                expected = () if detail is None else (Note("embedded-thumbnail", detail),)
                self.assertEqual(decode_raster(buf.getvalue())[1], expected)

    def test_plain_jpeg_and_mpo_have_no_embedded_thumbnail_note(self):
        buf = io.BytesIO()
        Image.new("RGB", (60, 40)).save(buf, "JPEG")
        self.assertEqual(decode_raster(buf.getvalue())[1], ())
        self.assertEqual(decode_raster(_mpo_bytes())[1], ())

    def test_mpo_later_frames_are_noted_by_frame(self):
        exif = _exif_with_thumbnail()
        _, notes = decode_raster(_mpo_bytes(exif))
        self.assertEqual(
            notes, (Note("embedded-thumbnail", f"frame 1: EXIF IFD1 thumbnail ({_thumbnail_len(exif)} bytes)"),)
        )

    def test_mpo_with_unparseable_exif_in_a_later_frame_still_renders(self):
        data = _mpo_bytes(b"Exif\x00\x00XXXXXXXXXXXX")
        plain = _decoded(_mpo_bytes())
        views, notes = decode_raster(data)
        np.testing.assert_array_equal(views[0], plain)
        self.assertEqual(notes, (Note("embedded-thumbnail", "frame 1: EXIF unreadable: SyntaxError"),))

    def test_malformed_exif_gives_an_unreadable_note(self):
        for fmt, exif, detail in (
            ("JPEG", _exif_with_thumbnail()[:42], "EXIF unreadable: corrupt"),
            ("PNG", b"Exif\x00\x00XXXXXXXXXXXX", "EXIF unreadable: SyntaxError"),
        ):
            with self.subTest(fmt=fmt):
                buf = io.BytesIO()
                Image.new("RGB", (60, 40)).save(buf, fmt, exif=exif)
                self.assertEqual(decode_raster(buf.getvalue())[1], (Note("embedded-thumbnail", detail),))

    def test_tiff_subifds_are_counted_including_16_bit_and_bigtiff(self):
        for dtype in (np.uint8, np.uint16):
            for bigtiff in (False, True):
                with self.subTest(dtype=dtype.__name__, bigtiff=bigtiff):
                    buf = io.BytesIO()
                    full = np.zeros((40, 60, 3), dtype)
                    with tifffile.TiffWriter(buf, bigtiff=bigtiff) as tif:
                        tif.write(full, photometric="rgb", subifds=2)
                        tif.write(full[::2, ::2], photometric="rgb", subfiletype=1)
                        tif.write(full[::4, ::4], photometric="rgb", subfiletype=1)
                    _, notes = decode_raster(buf.getvalue())
                    self.assertEqual(notes, (Note("embedded-thumbnail", "TIFF SubIFDs (2)"),))

    def test_unreadable_tiff_subifds_tag_is_noted(self):
        for bigtiff in (False, True):
            buf = io.BytesIO()
            full = np.zeros((40, 60, 3), np.uint8)
            with tifffile.TiffWriter(buf, bigtiff=bigtiff, byteorder="<") as tif:
                tif.write(full, photometric="rgb", subifds=2)
                tif.write(full[::2, ::2], photometric="rgb", subfiletype=1)
                tif.write(full[::4, ::4], photometric="rgb", subfiletype=1)
            data = buf.getvalue()
            ifd = struct.unpack_from("<Q" if bigtiff else "<I", data, 8 if bigtiff else 4)[0]
            count_size, entry_size = (8, 20) if bigtiff else (2, 12)
            (n,) = struct.unpack_from("<Q" if bigtiff else "<H", data, ifd)
            entries = [ifd + count_size + i * entry_size for i in range(n)]
            entry = next(e for e in entries if struct.unpack_from("<H", data, e)[0] == 330)
            value_format = "<QQ" if bigtiff else "<II"  # count, then offset (or inline value)
            for count, offset in ((2, len(data) + 100), (0x40000000, 8)):
                with self.subTest(bigtiff=bigtiff, count=count):
                    patched = bytearray(data)
                    struct.pack_into(value_format, patched, entry + 4, count, offset)
                    with quiet():
                        _, notes = decode_raster(bytes(patched))
                    self.assertEqual(notes, (Note("embedded-thumbnail", "TIFF SubIFDs unreadable: invalid value"),))

    def test_animated_png_is_unsupported(self):
        buf = io.BytesIO()
        frames = [Image.new("L", (20, 20), v) for v in (0, 255)]
        frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:])
        with self.assertRaisesRegex(Unsupported, r"^unsupported: multi-frame image \(2 frames\)$"):
            _decoded(buf.getvalue())

    def test_16_bit_colour_png_keeps_its_low_bits(self):
        rgb = np.zeros((40, 60, 3), dtype=np.uint16)
        rgb[_text_mask()] = 4000
        rgb[0, 0] = [4000, 1, 2]
        img = _decoded(imagecodecs.png_encode(rgb))
        self.assertEqual(img.dtype, np.float32)
        np.testing.assert_array_equal(img[0, 0], np.array([4000, 1, 2], dtype=np.float32) / 65535)
        self.assertEqual(len(set(img[0, 0].tolist())), 3)

    def test_16_bit_colour_and_alpha_pngs_scale_by_their_depth(self):
        text = _to_uint8(np.array([0, 4000]), 16)[1]  # 16 of 255 whatever else the image holds
        colour = np.where(_text_mask()[..., None], 4000, 0).astype(np.uint16).repeat(3, axis=2)
        opaque = np.full((40, 60, 1), 65535, dtype=np.uint16)
        partly = opaque.copy()
        partly[:, 30:] = 0
        cases = {
            "RGB": colour,
            "RGBA opaque": np.concatenate([colour, opaque], axis=2),
            "RGBA partly transparent": np.concatenate([colour, partly], axis=2),
        }
        for name, samples in cases.items():
            with self.subTest(name):
                out = render("raster", "id", imagecodecs.png_encode(samples), "inferno").rgb
                raw = _halves(out, 60)[0] if name == "RGBA partly transparent" else out
                self.assertEqual(raw.shape, (40, 60, 3))
                np.testing.assert_array_equal(raw[_text_mask()], text)
                np.testing.assert_array_equal(raw[~_text_mask()], 0)
        out = render("raster", "id", imagecodecs.png_encode(cases["RGBA partly transparent"]), "x")
        _, alpha_view = _halves(out.rgb, 60)
        np.testing.assert_array_equal(alpha_view[:, :30], 255)  # opaque: white
        np.testing.assert_array_equal(alpha_view[:, 30:], 0)  # transparent: black
        self.assertEqual(out.notes, (Note("alpha", "1200 of 2400 pixels not opaque"),))

    def test_16_bit_gray_alpha_png_stays_grayscale(self):
        gray = np.where(_text_mask(), 4000, 0).astype(np.uint16)
        for alpha in (65535, 0):
            with self.subTest(alpha=alpha):
                img, notes = _decode_joined(imagecodecs.png_encode(np.dstack([gray, np.full_like(gray, alpha)])))
                raw = img if alpha else _halves(img, 60)[0]
                self.assertEqual(raw.shape, (40, 60))  # still grayscale
                np.testing.assert_array_equal(raw, _window(gray.astype(np.float32)))  # two values: min-max
                np.testing.assert_array_equal(raw, (gray / 4000).astype(np.float32))
                if alpha:
                    self.assertEqual(notes, ())
                else:
                    np.testing.assert_array_equal(_halves(img, 60)[1], 0)
                    self.assertEqual(notes, (Note("alpha", "2400 of 2400 pixels not opaque"),))
        out = render("raster", "id", imagecodecs.png_encode(np.dstack([gray, np.full_like(gray, 65535)])), "x").rgb
        self.assertEqual(out.shape, (40, 60, 3))
        self.assertTrue((out[_text_mask()] == 255).all())
        self.assertTrue((out[~_text_mask()] == 0).all())

    def test_16_bit_rgb_png_colour_key_becomes_transparency(self):
        rgb = np.full((40, 60, 3), 4000, dtype=np.uint16)
        rgb[:, 30:] = 0  # the tRNS key

        def chunk(kind: bytes, body: bytes) -> bytes:
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

        rows = b"".join(b"\0" + row.astype(">u2").tobytes() for row in rgb)
        png = (
            PNG_MAGIC
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 60, 40, 16, 2, 0, 0, 0))
            + chunk(b"tRNS", struct.pack(">HHH", 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b"")
        )
        img, notes = _decode_joined(png)
        left, right = _halves(img, 60)
        np.testing.assert_array_equal(left[:, :30], np.float32(4000 / 65535))
        np.testing.assert_array_equal(right[:, :30], 1)
        np.testing.assert_array_equal(right[:, 30:], 0)
        self.assertEqual(notes, (Note("alpha", "1200 of 2400 pixels not opaque"),))

    def test_16_bit_rgb_tiff_scales_by_its_depth(self):
        text = _to_uint8(np.array([0, 4000]), 16)[1]
        rgb = np.where(_text_mask()[..., None], 4000, 0).astype(np.uint16).repeat(3, axis=2)
        rgb[0, 0] = [4000, 1, 2]
        cases = {
            "uncompressed": {},
            "LZW": {"compression": "lzw"},
            "deflate": {"compression": "zlib"},
            "planar": {"planarconfig": "separate"},
            "big-endian": {"byteorder": ">"},
            "big-endian tiled": {"byteorder": ">", "tile": (16, 16)},
            "big-endian planar": {"byteorder": ">", "planarconfig": "separate"},
        }
        for name, options in cases.items():
            with self.subTest(name):
                buf = io.BytesIO()
                samples = np.moveaxis(rgb, -1, 0) if "planarconfig" in options else rgb
                tifffile.imwrite(buf, samples, photometric="rgb", **options)
                img = _decoded(buf.getvalue())
                np.testing.assert_array_equal(img[0, 0], np.array([4000, 1, 2], dtype=np.float32) / 65535)
                out = render("raster", "id", buf.getvalue(), "inferno").rgb
                np.testing.assert_array_equal(out[_text_mask()], text)
                np.testing.assert_array_equal(out[~_text_mask()][1:], 0)  # but the low-bit pixel at [0, 0]

    def test_16_bit_rgba_tiff_alpha_is_transparency(self):
        rgb = np.where(_text_mask()[..., None], 4000, 0).astype(np.uint16).repeat(3, axis=2)
        alpha = np.full((40, 60, 1), 65535, dtype=np.uint16)
        alpha[:, 30:] = 0
        buf = io.BytesIO()
        tifffile.imwrite(buf, np.concatenate([rgb, alpha], axis=2), photometric="rgb", extrasamples=["unassalpha"])
        img, notes = _decode_joined(buf.getvalue())
        left, right = _halves(img, 60)
        np.testing.assert_array_equal(left, (rgb / 65535).astype(np.float32))
        np.testing.assert_array_equal(right[:, :30], 1)
        np.testing.assert_array_equal(right[:, 30:], 0)
        self.assertEqual(notes, (Note("alpha", "1200 of 2400 pixels not opaque"),))

    def test_16_bit_tiff_fourth_sample_without_extra_samples_is_alpha(self):
        alpha = np.where(_text_mask(), 65535, 0).astype(np.uint16)[..., None]  # content only in the 4th sample
        buf = io.BytesIO()
        tifffile.imwrite(buf, np.concatenate([np.zeros((40, 60, 3), np.uint16), alpha], axis=2), photometric="rgb")
        data = bytearray(buf.getvalue())
        tag = data.index(struct.pack("<HH", 338, 3))  # ExtraSamples, SHORT
        data[tag : tag + 2] = struct.pack("<H", 65000)  # renamed to an unknown private tag: no ExtraSamples
        with tifffile.TiffFile(io.BytesIO(bytes(data))) as tif:
            self.assertEqual(tif.pages[0].extrasamples, ())
        left, right = _halves(_decoded(bytes(data)), 60)
        np.testing.assert_array_equal(left, 0)
        np.testing.assert_array_equal(right[_text_mask()], 1)
        np.testing.assert_array_equal(right[~_text_mask()], 0)

    def test_16_bit_tiff_associated_alpha_is_unpremultiplied_as_pillow_does(self):
        straight = np.full((40, 60, 3), 40000, dtype=np.uint16)
        alpha = np.full((40, 60, 1), 65535, dtype=np.uint16)
        alpha[:, 20:40], alpha[:, 40:] = 16384, 0
        premultiplied = (straight.astype(np.uint32) * alpha // 65535).astype(np.uint16)
        buf = io.BytesIO()
        tifffile.imwrite(
            buf, np.concatenate([premultiplied, alpha], axis=2), photometric="rgb", extrasamples=["assocalpha"]
        )
        colour, _ = _halves(_decoded(buf.getvalue()), 60)
        with Image.open(io.BytesIO(buf.getvalue())) as im:
            im.load()
            pillow = np.asarray(im)[:, :, :3] / 255
        np.testing.assert_allclose(colour[:, :40], 40000 / 65535, atol=2 / 65535)  # straight colour, not dimmed
        np.testing.assert_array_equal(colour[:, 40:], 0)  # fully transparent: colour dropped, as Pillow does
        np.testing.assert_allclose(colour, pillow, atol=1.5 / 255)

    def test_16_bit_cmyk_tiff_matches_pillow_at_full_depth(self):
        ink = RNG.integers(0, 256, (40, 60, 4), dtype=np.uint16)
        ink[0, 0] = 255  # full ink: all 16 bits used, so the scale is that of an 8-bit image
        buf = io.BytesIO()
        tifffile.imwrite(buf, ink * 257, photometric="separated")
        with Image.open(io.BytesIO(buf.getvalue())) as im:
            self.assertEqual(im.mode, "CMYK")
            pillow = np.asarray(im.convert("RGB")).astype(int)
        out = render("raster", "id", buf.getvalue(), "inferno").rgb.astype(int)
        self.assertLessEqual(np.abs(out - pillow).max(), 1)

    def test_16_bit_cmyk_tiff_keeps_12_bit_black_text_dark_ink(self):
        ink = np.zeros((40, 60, 4), dtype=np.uint16)
        ink[_text_mask(), 3] = 4000  # a little ink: shown as such, not stretched
        buf = io.BytesIO()
        tifffile.imwrite(buf, ink, photometric="separated")
        img = _decoded(buf.getvalue())
        np.testing.assert_allclose(img[_text_mask()], 1 - 4000 / 65535, atol=1e-6)
        np.testing.assert_array_equal(img[~_text_mask()], 1)

    def test_png_with_a_chunk_before_ihdr_is_not_truncated(self):
        png = imagecodecs.png_encode(np.full((40, 60, 3), 4000, dtype=np.uint16))
        body = b"a\0b"
        text = struct.pack(">I", len(body)) + b"tEXt" + body + struct.pack(">I", zlib.crc32(b"tEXt" + body))
        misordered = png[:8] + text + png[8:]
        with Image.open(io.BytesIO(misordered)) as im:
            self.assertTrue(preprocess_module._pillow_truncates(im))  # routed to libpng, which refuses it
        row = render_and_encode(Candidate("id", "raster", misordered), "inferno", draw_overlays=False)
        self.assertIsInstance(row, SkippedRow)
        self.assertEqual(row.kind, "failed")
        # Older imagecodecs raise PngError; newer ones garble libpng's message into a UnicodeDecodeError
        self.assertIn(
            row.reason, {"cannot decode 16-bit PNG: PngError", "cannot decode 16-bit PNG: UnicodeDecodeError"}
        )

    def test_content_under_transparent_pixels_does_not_change_the_visible_ones(self):
        visible = np.where(_text_mask()[..., None], 4000, 0).astype(np.uint16).repeat(3, axis=2)
        alpha = np.full((40, 60, 1), 65535, dtype=np.uint16)
        alpha[:, 30:] = 0
        outs = []
        for hidden in (0, 65535):
            colour = visible.copy()
            colour[:, 30:] = hidden
            outs.append(
                render("raster", "id", imagecodecs.png_encode(np.concatenate([colour, alpha], axis=2)), "x").rgb
            )
            shown = outs[-1][:, :30]
            expected = np.where(_text_mask()[:, :30, None], round(4000 / 65535 * 255), 0)
            np.testing.assert_array_equal(shown, expected.repeat(3, axis=2))
        np.testing.assert_array_equal(outs[0][:, :30], outs[1][:, :30])

    def test_hidden_content_brighter_than_the_visible_scale_is_not_clipped(self):
        colour = np.full((40, 60, 3), 50000, dtype=np.uint16)
        colour[_text_mask()] = 60000
        alpha = np.zeros((40, 60, 1), dtype=np.uint16)  # everything hidden
        out = render("raster", "id", imagecodecs.png_encode(np.concatenate([colour, alpha], axis=2)), "x").rgb
        left = out[:, :60]
        self.assertNotEqual(int(left[_text_mask()].min()), int(left[~_text_mask()].max()))

    def test_gray_alpha_colour_view_is_rendered_as_an_opaque_image_would_be(self):
        for gray_level, text_level in ((128, 131), (128, 140)):
            with self.subTest(text_level=text_level):
                gray = np.full((40, 60), gray_level, dtype=np.uint8)
                gray[_text_mask()] = text_level
                alpha = np.full((40, 60), 255, dtype=np.uint8)
                alpha[0, 0] = 0  # one transparent pixel: the alpha view appears
                with_alpha = render("raster", "id", _png_bytes(np.dstack([gray, alpha]), "LA"), "x").rgb
                opaque = render("raster", "id", _png_bytes(gray), "x").rgb
                colour_view, alpha_view = _halves(with_alpha, 60)
                np.testing.assert_array_equal(colour_view, opaque)
                self.assertEqual(set(np.unique(alpha_view)), {0, 255})  # binary, whatever the colour view holds

    def test_multipage_16_bit_rgb_tiff_is_unsupported(self):
        buf = io.BytesIO()
        with tifffile.TiffWriter(buf) as tif:
            for _ in range(2):
                tif.write(np.zeros((20, 20, 3), dtype=np.uint16), photometric="rgb")
        with self.assertRaisesRegex(Unsupported, r"^unsupported: multi-frame image \(2 frames\)$"):
            _decoded(buf.getvalue())

    def test_inputs_pillow_decodes_in_full_stay_on_its_path(self):
        gray16 = (np.arange(40 * 60).reshape(40, 60) * 20).astype(np.uint16)
        rgb8 = RNG.integers(0, 255, (40, 60, 3), dtype=np.uint8)
        tiff8 = io.BytesIO()
        tifffile.imwrite(tiff8, rgb8, photometric="rgb")
        for name, data in {
            "8-bit RGB PNG": _png_bytes(rgb8),
            "8-bit RGBA PNG": imagecodecs.png_encode(np.dstack([rgb8, rgb8[:, :, 0]])),
            "16-bit grayscale PNG": _png_bytes(gray16),
            "8-bit RGB TIFF": tiff8.getvalue(),
        }.items():
            with self.subTest(name), Image.open(io.BytesIO(data)) as im:
                im.load()
                views, _ = decode_raster(data)
                expected, _ = preprocess_module._decode_frame(im)
                self.assertEqual(len(views), len(expected))
                for view, want in zip(views, expected, strict=True):
                    np.testing.assert_array_equal(view, want)

    def test_damaged_16_bit_colour_png_is_a_failed_row(self):
        png = bytearray(imagecodecs.png_encode(RNG.integers(0, 4096, (40, 60, 3)).astype(np.uint16)))
        idat = png.index(b"IDAT") + 4
        png[idat : idat + 8] = bytes(8)  # zlib header and the start of the stream, CRC left stale
        row = render_and_encode(Candidate("id", "raster", bytes(png)), "inferno", draw_overlays=False)
        self.assertIsInstance(row, SkippedRow)
        self.assertEqual(row.kind, "failed")
        self.assertEqual(row.reason, "cannot decode 16-bit PNG: PngError")


class RenderTest(unittest.TestCase):
    def dicom_bytes(self, pixels: np.ndarray, photometric: str = "MONOCHROME2") -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.dcm"
            write_dicom(path, pixels, photometric)
            return path.read_bytes()

    def with_elements(self, pixels: np.ndarray, photometric: str = "MONOCHROME2", edit=lambda ds: None) -> bytes:
        ds = pydicom.dcmread(io.BytesIO(self.dicom_bytes(pixels, photometric)))
        edit(ds)
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        return buf.getvalue()

    def wide_integer_dicom_bytes(self, pixels: np.ndarray) -> bytes:
        ds = pydicom.Dataset()
        ds.file_meta = FileMetaDataset()
        ds.SOPClassUID = SecondaryCaptureImageStorage
        ds.SOPInstanceUID = generate_uid()
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.Rows, ds.Columns = pixels.shape
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = ds.BitsStored = 32
        ds.HighBit = 31
        ds.PixelRepresentation = int(pixels.dtype.kind == "i")
        ds.PixelData = pixels.astype("<i4" if pixels.dtype.kind == "i" else "<u4").tobytes()
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        return buf.getvalue()

    def test_dicom_with_an_extreme_pixel_shows_the_text(self):
        rng = np.random.default_rng(3)
        pixels = rng.integers(0, 101, (400, 400)).astype(np.uint16)
        pixels[150:250, 100:300] = 400  # below 1% of the range once one pixel is at 65535 (655)
        plain = render("dicom", "id", self.dicom_bytes(pixels), "gray").rgb
        pixels[0, 0] = 65535
        out = render("dicom", "id", self.dicom_bytes(pixels), "gray").rgb
        self.assertGreaterEqual(out[150:250, 100:300].mean() - out[300:, :].mean(), 40)
        self.assertAlmostEqual(out[150:250, 100:300].mean(), plain[150:250, 100:300].mean(), delta=20)

    def test_gray_float32_accepts_exact_integer_values(self):
        for pixels in (
            np.array([[-32768, 32767]], dtype=np.int16),
            np.array([[0, 65535]], dtype=np.uint16),
            np.array([[1 << 40, (1 << 40) + (1 << 17)]], dtype=np.int64),
        ):
            with self.subTest(dtype=pixels.dtype):
                gray = preprocess_module._gray_float32(pixels)
                self.assertEqual(gray.dtype, np.float32)
                np.testing.assert_array_equal(gray.astype(pixels.dtype), pixels)

    def test_gray_float32_rejects_native_integer_bounds_and_float_overflow(self):
        values = (
            np.array([[np.iinfo(np.int64).max]], dtype=np.int64),
            np.array([[np.iinfo(np.uint64).max]], dtype=np.uint64),
            np.array([[float(np.finfo(np.float32).max) * 2.0]], dtype=np.float64),
        )
        for pixels in values:
            with self.subTest(dtype=pixels.dtype), warnings.catch_warnings():
                warnings.simplefilter("error", RuntimeWarning)
                with self.assertRaisesRegex(
                    Unsupported,
                    r"^unsupported: not implemented: grayscale values cannot be processed safely as float32$",
                ):
                    preprocess_module._gray_float32(pixels)

    def test_wide_integer_grayscale_precision_loss_fails_and_is_skipped(self):
        root = temp_dir(self).resolve()
        src = root / "src"
        src.mkdir()
        positive = np.full((40, 60), 1_000_000_000, dtype=np.uint32)
        positive[20:24, 10:50] += 1
        negative = positive.astype(np.int32) - 2_000_000_000
        (src / "uint32.dcm").write_bytes(self.wide_integer_dicom_bytes(positive))
        (src / "int32.dcm").write_bytes(self.wide_integer_dicom_bytes(negative))
        mode_i = root / "mode-i.tif"
        Image.fromarray(positive.astype(np.int32), "I").save(mode_i, "TIFF")
        good = src / "good.dcm"
        write_dicom(good, _good_pixels())

        with quiet():
            result = run_preprocess([src, mode_i], root / "work")

        expected_reason = "unsupported: not implemented: grayscale values cannot be processed safely as float32"
        skipped = {row["image_id"]: row for row in _read_tsv(root / "work" / "skipped.tsv")}
        self.assertEqual(
            set(skipped), {(src / "uint32.dcm").as_posix(), (src / "int32.dcm").as_posix(), mode_i.as_posix()}
        )
        self.assertTrue(all(row["kind"] == "failed" and row["reason"] == expected_reason for row in skipped.values()))
        manifest = _read_tsv(root / "work" / "manifest.tsv")
        self.assertEqual([row["image_id"] for row in manifest], [good.as_posix()])
        self.assertEqual(result.written, 1)
        self.assertEqual(sum(row.kind == "failed" for row in result.skipped), 3)

    def test_overlay_is_drawn_at_its_origin(self):
        pixels = RNG.integers(0, 1001, (200, 200)).astype(np.uint16)  # noise: nothing is cropped
        block = np.ones((5, 8), dtype=bool)
        data = self.with_elements(pixels, edit=lambda ds: add_overlay(ds, block, origin=(10, 20)))
        out = render("dicom", "id", data, "gray", draw_overlays=True)
        self.assertEqual(out.rgb.shape, (200, 200, 3))
        self.assertGreaterEqual(out.rgb[9:14, 19:27].min(), 250)  # 1-based (10, 20) is zero-based (9, 19)
        self.assertLess(out.rgb[8, 19:27].mean(), 250)
        self.assertLess(out.rgb[9:14, 18].mean(), 250)

    def test_overlay_is_clipped_to_the_image(self):
        pixels = RNG.integers(0, 1001, (50, 60)).astype(np.uint16)
        block = np.ones((20, 20), dtype=bool)
        data = self.with_elements(pixels, edit=lambda ds: add_overlay(ds, block, origin=(40, 50)))
        out = render("dicom", "id", data, "gray", draw_overlays=True)
        self.assertEqual(out.rgb.shape, (50, 60, 3))
        self.assertGreaterEqual(out.rgb[39:, 49:].min(), 250)

    def test_overlay_is_drawn_on_colour_images(self):
        pixels = RNG.integers(0, 200, (60, 80, 3)).astype(np.uint8)
        block = np.ones((4, 4), dtype=bool)
        data = self.with_elements(pixels, "RGB", lambda ds: add_overlay(ds, block, origin=(5, 6)))
        out = render("dicom", "id", data, "inferno", draw_overlays=True)
        self.assertTrue((out.rgb[4:8, 5:9] == 255).all())

    def test_undecodable_overlay_fails_the_file_naming_the_group(self):
        def edit(ds):
            add_overlay(ds, np.ones((8, 8), dtype=bool), group=0x6002)
            ds[0x6002, 0x3000].value = b"\x00\x00"  # far too short for 8x8 bits

        data = self.with_elements(_good_pixels(), edit=edit)
        with self.assertRaisesRegex(ValueError, r"overlay 0x6002 cannot be decoded"):
            render("dicom", "id", data, "gray", draw_overlays=True)

    def test_overlays_are_not_drawn_by_default(self):
        pixels = RNG.integers(0, 1001, (200, 200)).astype(np.uint16)
        block = np.ones((5, 8), dtype=bool)
        for photometric, shape in (("MONOCHROME2", (200, 200)), ("RGB", (60, 80, 3))):
            with self.subTest(photometric=photometric):
                pix = pixels if photometric == "MONOCHROME2" else RNG.integers(0, 200, shape).astype(np.uint8)
                bare = self.with_elements(pix, photometric)
                drawn = self.with_elements(pix, photometric, lambda ds: add_overlay(ds, block, origin=(10, 20)))
                plain, default = render("dicom", "id", bare, "gray"), render("dicom", "id", drawn, "gray")
                self.assertTrue(np.array_equal(plain.rgb, default.rgb))
                self.assertEqual(default.notes, (Note("overlay-plane", "group 0x6000"),))
                flagged = render("dicom", "id", drawn, "gray", draw_overlays=True)
                self.assertFalse(np.array_equal(plain.rgb, flagged.rgb))

    def test_undecodable_overlay_is_not_decoded_by_default(self):
        def edit(ds):
            add_overlay(ds, np.ones((8, 8), dtype=bool), group=0x6002)
            ds[0x6002, 0x3000].value = b"\x00\x00"

        data = self.with_elements(_good_pixels(), edit=edit)
        out = render("dicom", "id", data, "gray")
        self.assertEqual(out.notes, (Note("overlay-plane", "group 0x6002"),))

    def test_bundled_overlay_is_rendered_and_its_icon_is_not(self):
        path = get_testdata_file("examples_overlay.dcm", download=False)
        if path is None:
            self.skipTest("examples_overlay.dcm not bundled")
        data = Path(path).read_bytes()

        def strip(ds):
            for tag in [e.tag for e in ds if 0x6000 <= e.tag.group <= 0x601E]:
                del ds[tag]

        self.assertIn("IconImageSequence", pydicom.dcmread(path))
        bare = self.with_elements_from(data, strip)
        main = render("dicom", "/x/a.dcm", data, "gray", draw_overlays=True)
        plain = render("dicom", "/x/a.dcm", bare, "gray")
        self.assertEqual(main.image_id, "/x/a.dcm")
        self.assertFalse(np.array_equal(main.rgb, plain.rgb))
        mask = pydicom.dcmread(path).overlay_array(0x6000).astype(bool)
        self.assertEqual(main.rgb.shape[:2], mask.shape)  # nothing cropped, so the mask lines up
        self.assertGreaterEqual(np.percentile(main.rgb[..., 0][mask], 5), 250)

    def with_elements_from(self, data: bytes, edit) -> bytes:
        ds = pydicom.dcmread(io.BytesIO(data))
        edit(ds)
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        return buf.getvalue()

    def test_broken_icon_is_ignored(self):
        def edit(ds):
            item = pydicom.Dataset()
            item.Rows = 4  # no pixel data
            ds.IconImageSequence = [item]

        data = self.with_elements(_good_pixels(), edit=edit)
        main = render("dicom", "/x/a.dcm", data, "gray")
        self.assertIsInstance(main, preprocess_module.Rendered)
        self.assertEqual(main.image_id, "/x/a.dcm")

    def test_icon_pixels_are_never_decoded(self):
        path = temp_dir(self) / "a.dcm"
        _dicom_with_icon(path)
        with mock.patch.object(preprocess_module, "preprocess_dicom", wraps=preprocess_module.preprocess_dicom) as spy:
            render("dicom", "/x/a.dcm", path.read_bytes(), "gray")
        [((dcm, _), _)] = spy.call_args_list
        self.assertIn("IconImageSequence", dcm)  # the whole file, not its icon item

    def test_dicom_with_icon_is_one_manifest_row_and_a_broken_icon_no_skipped_row(self):
        path = get_testdata_file("examples_overlay.dcm", download=False)
        if path is None:
            self.skipTest("examples_overlay.dcm not bundled")

        def edit(ds):
            item = pydicom.Dataset()
            item.Rows = 4
            ds.IconImageSequence = [item]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "ok.dcm").write_bytes(Path(path).read_bytes())
            (root / "src" / "bad.dcm").write_bytes(self.with_elements(_good_pixels(), edit=edit))
            with quiet():
                result = run_preprocess([root / "src"], root / "work")
            with open(root / "work" / "manifest.tsv", newline="", encoding="utf-8") as f:
                ids = [row["image_id"] for row in csv.DictReader(f, delimiter="\t")]
        ok, bad = str(root / "src" / "ok.dcm"), str(root / "src" / "bad.dcm")
        self.assertEqual(sorted(ids), sorted([ok, bad]))
        self.assertEqual(result.skipped, [])
        self.assertEqual(result.found, 2)

    def test_all_zero_dicom_keeps_uncropped_image(self):
        with quiet():
            out = render("dicom", "id", self.dicom_bytes(np.zeros((40, 60), dtype=np.uint16)), "inferno")
        self.assertEqual(out.rgb.shape, (40, 60, 3))
        self.assertEqual(out.rgb.dtype, np.uint8)

    def test_monochrome1_is_inverted(self):
        pixels = _good_pixels()  # values grow from the top row to the bottom row
        mono2 = render("dicom", "id", self.dicom_bytes(pixels, "MONOCHROME2"), "gray")
        mono1 = render("dicom", "id", self.dicom_bytes(pixels, "MONOCHROME1"), "gray")
        self.assertGreater(mono2.rgb[-10:].mean(), mono2.rgb[:10].mean() + 100)
        self.assertLess(mono1.rgb[-10:].mean(), mono1.rgb[:10].mean() - 100)

    def luminance(self, rgb: np.ndarray) -> np.ndarray:
        return rgb @ np.array([0.299, 0.587, 0.114])

    def test_two_level_text_image_renders_text_bright(self):
        for dtype, low, high in ((np.uint16, 0, 4095), (np.int16, -1000, 3000)):
            with self.subTest(dtype=dtype.__name__):
                pixels = np.full((60, 80), low, dtype=dtype)
                pixels[20:30, 10:40:2] = high  # stripes, so cropping keeps the text
                out = render("dicom", "id", self.dicom_bytes(pixels), "gray")
                lum = self.luminance(out.rgb.astype(float))
                self.assertGreater(lum.max(), 200)
                self.assertLess(lum.min(), 50)

    def test_uniform_dicom_renders(self):
        out = render("dicom", "id", self.dicom_bytes(np.full((30, 40), 1234, dtype=np.uint16)), "gray")
        self.assertEqual(out.rgb.shape, (30, 40, 3))

    def float_dicom_bytes(self, pixels: np.ndarray) -> bytes:
        ds = pydicom.Dataset()
        ds.file_meta = FileMetaDataset()
        ds.SOPClassUID = SecondaryCaptureImageStorage
        ds.SOPInstanceUID = generate_uid()
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.Rows, ds.Columns = pixels.shape
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 32
        ds.FloatPixelData = pixels.astype("<f4").tobytes()
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        return buf.getvalue()

    def float_pixels(self, bad: float | None = None) -> np.ndarray:
        pixels = np.full((40, 60), 100.0, dtype=np.float32)
        pixels[_text_mask()] = 200.0
        if bad is not None:
            pixels[0, 0] = bad
        return pixels

    def test_finite_float_dicom_renders(self):
        out = render("dicom", "id", self.float_dicom_bytes(self.float_pixels()), "gray")
        self.assertEqual(out.rgb.ndim, 3)  # cropped, so the shape is not the input's
        self.assertGreater(len(np.unique(out.rgb)), 1)
        self.assertEqual(out.notes, ())  # float samples have no unused bits

    def test_non_finite_float_dicom_is_unsupported(self):
        for bad, counts in ((np.nan, "1 NaN, 0 infinite"), (np.inf, "0 NaN, 1 infinite")):
            with self.subTest(bad=bad):
                data = self.float_dicom_bytes(self.float_pixels(bad))
                with self.assertRaisesRegex(Unsupported, rf"^unsupported: non-finite pixel values \({counts}\)$"):
                    render("dicom", "id", data, "inferno")

    def test_float32_grayscale_span_overflow_is_unsupported(self):
        pixels = np.full((40, 60), 2.0e38, dtype=np.float32)
        pixels[20:24, 10:50] = -2.0e38
        with self.assertRaisesRegex(
            Unsupported,
            r"^unsupported: not implemented: grayscale values cannot be processed safely as float32$",
        ):
            render("dicom", "id", self.float_dicom_bytes(pixels), "gray")

    def test_non_finite_float_inputs_are_failed_rows(self):
        root = temp_dir(self).resolve()
        src = root / "src"
        src.mkdir()
        (src / "nan.dcm").write_bytes(self.float_dicom_bytes(self.float_pixels(np.nan)))
        (src / "inf.dcm").write_bytes(self.float_dicom_bytes(self.float_pixels(np.inf)))
        (src / "ok.dcm").write_bytes(self.float_dicom_bytes(self.float_pixels()))
        with quiet():
            run_preprocess([src], root / "work")
        skipped = {r["image_id"]: r for r in _read_tsv(root / "work" / "skipped.tsv")}
        self.assertEqual(
            {k: (r["kind"], r["reason"]) for k, r in skipped.items()},
            {
                (src / "nan.dcm").as_posix(): ("failed", "unsupported: non-finite pixel values (1 NaN, 0 infinite)"),
                (src / "inf.dcm").as_posix(): ("failed", "unsupported: non-finite pixel values (0 NaN, 1 infinite)"),
            },
        )
        self.assertEqual(
            [r["image_id"] for r in _read_tsv(root / "work" / "manifest.tsv")], [(src / "ok.dcm").as_posix()]
        )

    def float_rgb_dicom_bytes(self, rgb: np.ndarray) -> bytes:
        ds = pydicom.dcmread(io.BytesIO(self.float_dicom_bytes(rgb[:, :, 0])))
        ds.SamplesPerPixel = 3
        ds.PlanarConfiguration = 0
        ds.PhotometricInterpretation = "RGB"
        ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 32, 31, 0
        ds.FloatPixelData = rgb.astype("<f4").tobytes()
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        return buf.getvalue()

    def test_non_finite_float_rgb_dicom_is_unsupported(self):
        rgb = np.full((40, 60, 3), 100.0, dtype=np.float32)
        rgb[0, 0, 0] = np.nan
        with self.assertRaisesRegex(Unsupported, r"^unsupported: non-finite pixel values \(1 NaN, 0 infinite\)$"):
            render("dicom", "id", self.float_rgb_dicom_bytes(rgb), "inferno")

    def test_finite_float_rgb_dicom_is_unsupported(self):
        rgb = np.full((40, 60, 3), 0.5, dtype=np.float32)
        with self.assertRaisesRegex(Unsupported, r"^unsupported: float colour pixel data$"):
            render("dicom", "id", self.float_rgb_dicom_bytes(rgb), "inferno")

    def test_mode_f_tiff_with_non_finite_value_is_unsupported(self):
        def tiff(arr: np.ndarray) -> bytes:
            buf = io.BytesIO()
            Image.fromarray(arr.astype(np.float32), "F").save(buf, "TIFF")
            return buf.getvalue()

        finite = self.float_pixels()
        out = render("raster", "id", tiff(finite), "inferno")
        self.assertEqual(out.rgb.ndim, 3)  # cropped, so the shape is not the input's
        self.assertGreater(len(np.unique(out.rgb)), 1)
        for bad, counts in ((np.nan, "1 NaN, 0 infinite"), (-np.inf, "0 NaN, 1 infinite")):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(Unsupported, rf"^unsupported: non-finite pixel values \({counts}\)$"),
            ):
                render("raster", "id", tiff(self.float_pixels(bad)), "inferno")

    def test_dicom_without_pixel_data_is_unsupported(self):
        ds = pydicom.dcmread(io.BytesIO(self.dicom_bytes(_good_pixels())))
        del ds.PixelData
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        with self.assertRaisesRegex(Unsupported, r"^unsupported: no pixel data \(Secondary Capture Image Storage\)$"):
            render("dicom", "id", buf.getvalue(), "inferno")

    def test_multiframe_dicom_is_unsupported(self):
        pixels = np.ones((2, 16, 16), dtype=np.uint16)
        with self.assertRaisesRegex(Unsupported, r"^unsupported: multi-frame DICOM \(2 frames\)$"):
            render("dicom", "id", self.dicom_bytes(pixels), "inferno")

    def test_multiframe_colour_dicom_reports_multiframe(self):
        pixels = RNG.integers(0, 255, (3, 16, 16, 3), dtype=np.uint8)
        with self.assertRaisesRegex(Unsupported, r"^unsupported: multi-frame DICOM \(3 frames\)$"):
            render("dicom", "id", self.dicom_bytes(pixels, "RGB"), "inferno")

    def test_single_frame_rgb_dicom_is_shown_as_is(self):
        pixels = RNG.integers(0, 255, (32, 40, 3), dtype=np.uint8)
        out = render("dicom", "id", self.dicom_bytes(pixels, "RGB"), "inferno")
        self.assertEqual(out.rgb.dtype, np.uint8)
        np.testing.assert_array_equal(out.rgb, pixels)

    def test_high_bit_rgb_dicom_uses_its_full_range(self):
        pixels = np.full((32, 40, 3), 65535, dtype=np.uint16)
        pixels[8:24, 10:30, 0] = 0
        out = render("dicom", "id", self.dicom_bytes(pixels, "RGB"), "inferno")
        self.assertEqual(out.rgb.dtype, np.uint8)
        self.assertEqual(out.rgb.max(), 255)
        self.assertEqual(out.rgb.min(), 0)
        pixels[:] = 32768  # the top bit set: unchanged scaling by 16 bits
        self.assertEqual(preprocess_module._to_uint8(pixels, 16).max(), 128)

    def test_rgb_dicom_is_scaled_by_bits_stored_alone(self):
        pixels = np.zeros((40, 60, 3), dtype=np.uint16)
        pixels[_text_mask()] = 4095
        for stored, text in ((16, round(4095 / 65535 * 255)), (12, 255)):
            with self.subTest(bits_stored=stored):
                ds = pydicom.dcmread(io.BytesIO(self.dicom_bytes(pixels, "RGB")))
                ds.BitsStored, ds.HighBit = stored, stored - 1
                rgb, _ = preprocess_module._colour_dicom(ds, "RGB")
                np.testing.assert_array_equal(rgb, np.where(_text_mask()[..., None], text, 0).repeat(3, axis=2))

    def _palette_dataset(self, lut_bits: int, entries: np.ndarray) -> pydicom.Dataset:
        ds = pydicom.dcmread(
            io.BytesIO(self.dicom_bytes(np.arange(256, dtype=np.uint8).reshape(16, 16), "PALETTE COLOR"))
        )
        for element in (0x1101, 0x1102, 0x1103):  # red, green, blue
            ds.add_new((0x0028, element), "US", [256, 0, lut_bits])
            ds.add_new((0x0028, element + 0x100), "OW", entries.astype("<u2").tobytes())
        return ds

    def test_palette_entries_are_scaled_by_the_descriptors_depth(self):
        wide = np.arange(256)[::-1] * 257  # genuinely 16-bit entries: some above 255
        rgb, _ = preprocess_module._colour_dicom(self._palette_dataset(16, wide), "PALETTE COLOR")
        np.testing.assert_array_equal(rgb[..., 0].ravel(), preprocess_module._to_uint8(wide, 16))
        self.assertEqual(rgb.max(), 255)
        mixed = np.zeros(256, dtype=int)
        mixed[0], mixed[255] = 256, 255  # a single entry above 255 keeps 16-bit scaling
        rgb, _ = preprocess_module._colour_dicom(self._palette_dataset(16, mixed), "PALETTE COLOR")
        np.testing.assert_array_equal(rgb[..., 0].ravel(), preprocess_module._to_uint8(mixed, 16))

    def test_palette_with_8_bit_entries_under_a_16_bit_descriptor_renders_them_at_full_brightness(self):
        entries = np.arange(256)[::-1]
        ds = self._palette_dataset(16, entries)
        rgb, _ = preprocess_module._colour_dicom(ds, "PALETTE COLOR")
        np.testing.assert_array_equal(rgb[..., 0].ravel(), entries)
        self.assertEqual(rgb.max(), 255)
        green = self._palette_dataset(16, entries)  # one table with a 16-bit entry is enough to scale by 16 bits
        green.GreenPaletteColorLookupTableData = (entries * 0 + 1000).astype("<u2").tobytes()
        rgb, _ = preprocess_module._colour_dicom(green, "PALETTE COLOR")
        np.testing.assert_array_equal(rgb[..., 0].ravel(), preprocess_module._to_uint8(entries, 16))

    def test_palette_with_8_bit_entries_padded_into_16_bit_words_renders_the_entries(self):
        entries = np.arange(256)[::-1]  # descriptor depth 8: the 16-bit words only pad the entries
        ds = self._palette_dataset(8, entries)
        self.assertEqual(pydicom.pixels.apply_color_lut(np.arange(256).reshape(16, 16), ds).dtype, np.uint16)
        rgb, _ = preprocess_module._colour_dicom(ds, "PALETTE COLOR")
        np.testing.assert_array_equal(rgb[..., 0].ravel(), entries)

    def test_palette_scales_by_the_descriptor_depth_and_clips_larger_entries(self):
        entries = np.arange(256) * 257
        rgb, _ = preprocess_module._colour_dicom(self._palette_dataset(8, entries), "PALETTE COLOR")
        out = rgb[..., 0].ravel()  # indices are sorted, so the outputs must be non-decreasing
        self.assertTrue(np.all(np.diff(out.astype(int)) >= 0))
        self.assertEqual((out[0], out[-1]), (0, 255))

    def test_to_uint8_scales_by_the_depth_given(self):
        to_uint8 = preprocess_module._to_uint8
        eight = np.arange(256, dtype=np.uint8)
        np.testing.assert_array_equal(to_uint8(eight, 8), eight)
        np.testing.assert_array_equal(to_uint8(eight.astype(np.uint16), 8), eight)
        np.testing.assert_array_equal(to_uint8(np.array([0, 2048, 4095], dtype=np.uint16), 12), [0, 128, 255])
        np.testing.assert_array_equal(to_uint8(np.array([0, 2048, 4095], dtype=np.uint16), 16), [0, 8, 16])
        np.testing.assert_array_equal(to_uint8(np.array([0, 40000, 65535], dtype=np.uint16), 16), [0, 156, 255])
        np.testing.assert_array_equal(to_uint8(np.array([-5, 3, 300], dtype=np.int16), 8), [0, 3, 255])  # clips

    def test_to_uint8_accepts_an_empty_array(self):
        out = preprocess_module._to_uint8(np.zeros((0, 4, 3), dtype=np.uint16), 16)
        self.assertEqual((out.shape, out.dtype), ((0, 4, 3), np.uint8))

    def test_bundled_colour_dicoms_render_rgb_uint8(self):
        for name in ("examples_rgb_color.dcm", "SC_ybr_full_422_uncompressed.dcm", "examples_palette.dcm"):
            with self.subTest(name=name):
                path = get_testdata_file(name, download=False)
                if path is None:
                    self.skipTest(f"{name} not bundled")
                out = render("dicom", "id", Path(path).read_bytes(), "inferno")
                self.assertEqual(out.rgb.ndim, 3)
                self.assertEqual(out.rgb.shape[2], 3)
                self.assertEqual(out.rgb.dtype, np.uint8)
                if name == "examples_palette.dcm":
                    self.assertFalse(np.array_equal(out.rgb[..., 0], out.rgb[..., 1]))
                    self.assertFalse(np.array_equal(out.rgb[..., 1], out.rgb[..., 2]))

    def test_bundled_compressed_dicoms_render(self):
        for name in (
            "MR_small_jpeg_ls_lossless.dcm",  # JPEG-LS
            "SC_rgb_jpeg_gdcm.dcm",  # JPEG Lossless (Process 14)
            "JPEG2000.dcm",
            "HTJ2KLossless_08_RGB.dcm",
            "rtdose_rle_1frame.dcm",  # RLE
        ):
            with self.subTest(name=name):
                # an already-downloaded external file (e.g. the HTJ2K ones) is found by get_testdata_files only
                path = get_testdata_file(name, download=False) or next(iter(get_testdata_files(name)), None)
                if path is None:
                    self.skipTest(f"{name} not available")
                out = render("dicom", "id", Path(path).read_bytes(), "inferno")
                self.assertEqual(out.rgb.shape[2], 3)
                self.assertEqual(out.rgb.dtype, np.uint8)

    def test_every_bundled_compressed_dicom_decodes(self):
        # Not decodable with permissively licensed codecs (pylibjpeg-libjpeg is GPL-3), or corrupt on purpose.
        undecodable = {
            "JPEG-lossy.dcm": "12-bit JPEG Extended",
            "JPGExtended.dcm": "12-bit JPEG Extended",
            "JLSL_08_07_0_1F.dcm": "JPEG-LS with 7-bit samples",
            "JPEG2000-embedded-sequence-delimiter.dcm": "corrupt J2K codestream",
        }
        paths = get_testdata_files("*.dcm")
        if not paths:
            self.skipTest("no bundled test data")
        checked = 0
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for path in sorted(paths):
                name = Path(path).name
                dcm = None
                with contextlib.suppress(Exception):  # deliberately broken files in pydicom's test set
                    dcm = pydicom.dcmread(path)
                if dcm is None:
                    continue
                syntax = dcm.file_meta.get("TransferSyntaxUID")
                if syntax is None or not syntax.is_compressed or "PixelData" not in dcm:
                    continue
                if name in undecodable:
                    with self.assertRaises(RuntimeError, msg=name):
                        dcm.pixel_array  # noqa: B018
                    continue
                with self.subTest(name=name, syntax=syntax.name):
                    self.assertGreater(dcm.pixel_array.size, 0)
                checked += 1
        self.assertGreater(checked, 0)

    def test_undecodable_compressed_dicom_names_its_transfer_syntax(self):
        path = get_testdata_file("JPEG-lossy.dcm", download=False)  # 12-bit JPEG Extended: no installed codec
        if path is None:
            self.skipTest("file not bundled")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with self.assertRaises(DecodeError) as ctx:
                render("dicom", "id", Path(path).read_bytes(), "inferno")
        reason = _failed("id", ctx.exception).reason
        self.assertTrue(reason.startswith("cannot decode JPEG Extended (Process 2 and 4): "), reason)
        self.assertNotIn("0x", reason)
        self.assertNotIn("\n", reason)

    def test_decoder_failure_in_colour_dicom_names_its_transfer_syntax(self):
        path = get_testdata_file("SC_rgb_jpeg_gdcm.dcm", download=False)
        if path is None:
            self.skipTest("file not bundled")
        boom = mock.PropertyMock(side_effect=RuntimeError("codec <Foo object at 0x7f3a2c1b9e40> broke\nline two"))
        with mock.patch.object(pydicom.FileDataset, "pixel_array", boom), self.assertRaises(DecodeError) as ctx:
            render("dicom", "id", Path(path).read_bytes(), "inferno")
        self.assertEqual(
            _failed("id", ctx.exception).reason,
            "cannot decode JPEG Lossless, Non-Hierarchical, First-Order Prediction (Process 14 [Selection Value 1]): "
            "RuntimeError: codec <data> broke",
        )

    def test_bundled_multiframe_dicoms_stay_unsupported(self):
        for name, frames in (("examples_ybr_color.dcm", 30), ("SC_rgb_rle_2frame.dcm", 2), ("rtdose.dcm", 15)):
            with self.subTest(name=name):
                path = get_testdata_file(name, download=False)
                if path is None:
                    self.skipTest(f"{name} not bundled")
                with self.assertRaisesRegex(Unsupported, rf"^unsupported: multi-frame DICOM \({frames} frames\)$"):
                    render("dicom", "id", Path(path).read_bytes(), "inferno")

    def test_other_photometric_interpretation_is_unsupported(self):
        ds = pydicom.dcmread(io.BytesIO(self.dicom_bytes(_good_pixels())))
        ds.PhotometricInterpretation = "HSV"
        with self.assertRaisesRegex(Unsupported, r"^unsupported: photometric interpretation HSV$"):
            preprocess_module.preprocess_dicom(ds)

    def test_failure_reason_has_no_memory_addresses(self):
        skipped = _failed("id", OSError("cannot identify image file <_io.BytesIO object at 0x7f3a2c1b9e40>"))
        self.assertEqual(skipped.reason, "OSError: cannot identify image file <data>")

    def test_reasons_drop_surrogates_and_line_separators(self):
        self.assertEqual(_failed("id", ValueError("a\udcffb\u2028\x85c")).reason, "ValueError: a b c")

    def test_dicom_reasons_are_single_line_and_tolerate_multivalued_sop_class(self):
        ds = pydicom.dcmread(io.BytesIO(self.dicom_bytes(_good_pixels())))
        del ds.PixelData
        ds.SOPClassUID = ["1.2.3", "4.5.6"]
        with self.assertRaisesRegex(Unsupported, r"^unsupported: no pixel data$"):
            preprocess_module.preprocess_dicom(ds)
        self.assertEqual(_failed("id", ValueError("a\x00b\x1bc\x7f")).reason, "ValueError: a b c")

    def test_bare_dataset_without_sop_class_or_pixel_data_is_not_dicom(self):
        ds = pydicom.Dataset()
        ds.PatientName = "X"
        ds.StudyDate = "20200101"
        buf = io.BytesIO()
        ds.save_as(buf, implicit_vr=True, little_endian=True)
        with self.assertRaisesRegex(
            pydicom.errors.InvalidDicomError, r"^not DICOM \(no preamble, no SOP Class UID and no pixel data\)$"
        ):
            render("dicom", "id", buf.getvalue(), "inferno")

    def test_bare_acr_nema_image_without_sop_class_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x"
            write_dicom(path, _good_pixels(), preamble=False)
            ds = pydicom.dcmread(path, force=True)
        del ds.SOPClassUID
        del ds.SOPInstanceUID
        buf = io.BytesIO()
        ds.save_as(buf, implicit_vr=True, little_endian=True)
        out = render("dicom", "id", buf.getvalue(), "gray")
        self.assertEqual(out.rgb.dtype, np.uint8)

    def test_failure_reason_has_no_tabs_or_newlines(self):
        skipped = _failed("id", ValueError("bad\tthing\nhappened"))
        self.assertEqual(skipped.reason, "ValueError: bad thing happened")
        self.assertEqual(_failed("id", Unsupported("x")).reason, "unsupported: x")


class PreprocessCliTest(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self).resolve()
        make_mixed_source(self.root)

    def invoke(self, *args, source: str = "src"):
        with quiet():
            return invoke_cli(
                "preprocess", str(self.root / source), "--work-dir", str(self.root / "work"), *args, env=CLI_ENV
            )

    def test_invalid_options_exit_2_before_any_output(self):
        for args in (("--batch-size", "0"), ("--batch-size", "-3"), ("--colormap", "nope"), ("--jobs", "0")):
            with self.subTest(args=args):
                before = sorted(self.root.iterdir())
                result = self.invoke(*args)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertEqual(sorted(self.root.iterdir()), before)

    def test_reversed_colormap_is_accepted(self):
        self.assertEqual(self.invoke("--colormap", "viridis_r", "--allow-skipped").exit_code, 0)

    def test_all_good_source_exits_0(self):
        good = self.root / "good"
        good.mkdir()
        write_dicom(good / "a.dcm", _good_pixels())
        result = self.invoke(source="good")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Found 1 inputs: wrote 1 images in 1 batches; 0 skipped (0 failed, 0 ignored;", result.output)
        self.assertNotIn("note", result.output)
        self.assertNotIn("Error", result.output)

    def test_notes_are_summarized_without_changing_the_exit_code(self):
        notable = self.root / "notable"
        notable.mkdir()
        _dicom_with_overlays(notable / "a.dcm", 0x6000)
        work = self.root / "work"
        result = self.invoke(source="notable")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(result.output.rstrip().endswith(f"; 1 note (1 overlay-plane; see {work / 'notes.tsv'})"))
        _dicom_with_icon(notable / "b.dcm")
        shutil.rmtree(work)
        result = self.invoke(source="notable")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"; 2 notes (1 overlay-plane, 1 icon-image; see {work / 'notes.tsv'})", result.output)

    def test_draw_overlays_flag_is_listed_and_recorded(self):
        help_ = self.invoke("--help")
        self.assertIn("--draw-overlays", help_.output)
        notable = self.root / "notable"
        notable.mkdir()
        _dicom_with_overlays(notable / "a.dcm", 0x6000)
        work = self.root / "work"
        for flag, expected in ((None, False), ("--draw-overlays", True)):
            shutil.rmtree(work, ignore_errors=True)
            result = self.invoke(*([flag] if flag else []), source="notable")
            self.assertEqual(result.exit_code, 0, result.output)
            record = json.loads((work / "preprocess.json").read_text())
            self.assertIs(record["parameters"]["draw_overlays"], expected)

    def test_failures_exit_1(self):
        result = self.invoke()
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Found 7 inputs: wrote 5 images in 1 batches; 2 skipped (2 failed, 0 ignored;", result.output)
        self.assertIn("--allow-skipped", result.output)
        self.assertIn("preprocess into a new --work-dir", result.output)
        self.assertNotIn("Re-run", result.output)  # a re-run into the same work directory is refused
        self.assertTrue((self.root / "work" / "manifest.tsv").exists())
        self.assertTrue((self.root / "work" / "skipped.tsv").exists())

    def test_allow_skipped_exits_0(self):
        result = self.invoke("--allow-skipped")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"2 skipped (2 failed, 0 ignored; see {self.root / 'work' / 'skipped.tsv'})", result.output)

    def test_existing_work_dir_exits_1(self):
        self.invoke("--allow-skipped")
        manifest = (self.root / "work" / "manifest.tsv").read_bytes()
        result = self.invoke("--allow-skipped")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("already exists", result.output)
        self.assertEqual((self.root / "work" / "manifest.tsv").read_bytes(), manifest)

    def test_jobs_default_follows_slurm_and_is_recorded(self):
        result = self.invoke("--allow-skipped")
        self.assertEqual(result.exit_code, 0, result.output)
        record = json.loads((self.root / "work" / "preprocess.json").read_text())
        self.assertEqual(record["parameters"]["jobs"], 1)
        seen = []

        def fake_run(*args, **kwargs):
            seen.append(kwargs["jobs"])
            raise WorkerCrashed("a preprocess worker died")

        with (
            mock.patch("image_review.preprocess.run_preprocess", fake_run),
            mock.patch.object(os, "sched_getaffinity", lambda pid: set(range(8)), create=True),
            quiet(),
        ):
            crashed = invoke_cli(
                "preprocess", str(self.root / "src"), env={"SLURM_CPUS_PER_TASK": "5"}, catch_exceptions=False
            )
        self.assertEqual(seen, [5])
        self.assertEqual(crashed.exit_code, 1, crashed.output)
        self.assertIn("a preprocess worker died", crashed.output)


class DefaultJobsTest(unittest.TestCase):
    def setUp(self):
        tmp = temp_dir(self)
        self.cgroups = tmp / "cgroup"  # a fake cgroup v2 mount
        self.cgroups.mkdir()
        self.cpu_max = self.cgroups / "cpu.max"  # missing until a test writes it
        self.proc_cgroup = tmp / "proc-self-cgroup"
        self.proc_cgroup.write_text("0::/\n")  # as in a container: the namespace root
        for patch in (
            mock.patch.dict(os.environ),
            mock.patch.object(cli_module, "CGROUP_ROOT", self.cgroups),
            mock.patch.object(cli_module, "PROC_SELF_CGROUP", self.proc_cgroup),
            mock.patch.object(os, "sched_getaffinity", lambda pid: set(range(7)), create=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("SLURM_CPUS_PER_TASK", None)

    def test_slurm_cpus_per_task_when_valid(self):
        self.cpu_max.write_text("100000 100000\n")  # a quota does not override Slurm's allocation
        os.environ["SLURM_CPUS_PER_TASK"] = "3"
        self.assertEqual(default_jobs(), 3)

    def test_slurm_cpus_per_task_is_capped_by_affinity(self):
        os.environ["SLURM_CPUS_PER_TASK"] = "32"
        self.assertEqual(default_jobs(), 7)

    def test_usable_cpus_when_slurm_is_unset_or_invalid(self):
        for value in (None, "", "0", "-2", "abc", "2.5", "\u00b2"):
            with self.subTest(value=value):
                os.environ.pop("SLURM_CPUS_PER_TASK", None)
                if value is not None:
                    os.environ["SLURM_CPUS_PER_TASK"] = value
                self.assertEqual(default_jobs(), 7)
                with (
                    mock.patch.object(os, "sched_getaffinity", None, create=True),
                    mock.patch.object(os, "cpu_count", return_value=9),
                ):
                    self.assertEqual(default_jobs(), 9)
                with (
                    mock.patch.object(os, "sched_getaffinity", None, create=True),
                    mock.patch.object(os, "cpu_count", return_value=None),
                ):
                    self.assertEqual(default_jobs(), 1)

    def test_cgroup_quota_caps_the_usable_cpus(self):
        for content, expected in (
            ("200000 100000\n", 2),
            ("150000 100000\n", 2),  # rounded up
            ("50000 100000\n", 1),
            ("1000000 100000\n", 7),  # never above the affinity
            ("max 100000\n", 7),
            ("garbage\n", 7),
            ("100 0\n", 7),
            ("", 7),
        ):
            with self.subTest(content=content):
                self.cpu_max.write_text(content)
                self.assertEqual(default_jobs(), expected)
        self.cpu_max.unlink()
        self.assertEqual(default_jobs(), 7)

    def test_smallest_quota_of_the_cgroup_and_its_ancestors(self):
        scope = self.cgroups / "user.slice" / "user-1002.slice" / "session-9.scope"
        scope.mkdir(parents=True)
        self.proc_cgroup.write_text("12:cpu,cpuacct:/ignored-v1\n0::/user.slice/user-1002.slice/session-9.scope\n")
        self.cpu_max.write_text("max 100000\n")
        (self.cgroups / "user.slice" / "cpu.max").write_text("600000 100000\n")
        (self.cgroups / "user.slice" / "user-1002.slice" / "cpu.max").write_text("200000 100000\n")  # CPUQuota=200%
        self.assertEqual(default_jobs(), 2)  # the scope itself has no cpu.max
        (scope / "cpu.max").write_text("max 100000\n")
        self.assertEqual(default_jobs(), 2)
        (scope / "cpu.max").write_text("100000 100000\n")
        self.assertEqual(default_jobs(), 1)

    def test_cgroup_path_falls_back_to_the_mount_root(self):
        (self.cgroups / "outside").mkdir()
        (self.cgroups / "outside" / "cpu.max").write_text("100000 100000\n")
        self.cpu_max.write_text("300000 100000\n")
        for content in ("0::/../../outside\n", "1:name=systemd:/x\n", "garbage", None):
            with self.subTest(content=content):
                if content is None:
                    self.proc_cgroup.unlink()  # not Linux
                else:
                    self.proc_cgroup.write_text(content)
                self.assertEqual(default_jobs(), 3)


def make_parallel_source(root: Path) -> list[Path]:
    """Twelve-odd inputs of mixed size and kind, so pooled renders finish out of order, plus failed and ignored ones."""
    src = root / "src"
    src.mkdir()
    for i, side in enumerate((300, 40, 200, 64, 260, 32)):
        gradient = np.linspace(0, 3000, side * side).reshape(side, side)
        write_dicom(src / f"d{i}.dcm", (gradient + RNG.integers(0, 200, (side, side))).astype(np.uint16))
    write_dicom(src / "d1.dcm", _with_unused_bit(_text_pixels(), 15), BitsStored=12, HighBit=11)  # a note to merge
    _dicom_with_overlays(src / "m1.dcm", 0x6000, photometric="MONOCHROME1")
    _dicom_with_icon(src / "icon.dcm")
    (src / "j2k.dcm").write_bytes(Path(get_testdata_file("JPEG2000.dcm", download=False)).read_bytes())
    (src / "p.png").write_bytes(_png_bytes(RNG.integers(0, 255, (90, 120), dtype=np.uint8)))
    (src / "broken.jpg").write_bytes(b"\xff\xd8\xff truncated jpeg")  # failed
    (src / "notes.txt").write_text("not an image")  # ignored
    archive = root / "scans.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.png", _png_bytes(RNG.integers(0, 255, (50, 70, 3), dtype=np.uint8)))
        zf.writestr("b/c.png", _png_bytes(RNG.integers(0, 255, (30, 20), dtype=np.uint8)))
        zf.writestr("bad.png", b"\x89PNG\r\n\x1a\n broken")  # failed
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
            zf.writestr("a.png", _solid_png(255))  # a.png#2 ...
        zf.writestr("a.png#2", _solid_png(128))  # ... collides with this one: both fail
    return [src, archive]


def _tree(work: Path) -> dict[str, bytes]:
    """Every file under `work` by relative path; preprocess.json without its time and jobs."""
    files = {p.relative_to(work).as_posix(): p.read_bytes() for p in sorted(work.rglob("*")) if p.is_file()}
    record = json.loads(files.pop("preprocess.json"))
    del record["created"], record["parameters"]["jobs"]
    return {**files, "preprocess.json": json.dumps(record).encode()}


def _exit_in_worker(candidate, colormap, *, draw_overlays):
    """A pool worker function that kills its process on the input named `crash.png`."""
    if candidate.image_id.endswith("crash.png"):
        os._exit(1)
    return render_and_encode(candidate, colormap, draw_overlays=draw_overlays)


def _hang_in_worker(candidate, colormap, *, draw_overlays):
    """A pool worker function that marks that it started (beside the source directory), then hangs."""
    path = Path(candidate.image_id)
    (path.parent.parent / f"started-{path.name}").touch()
    time.sleep(60)
    return render_and_encode(candidate, colormap, draw_overlays=draw_overlays)


def _probe_worker(candidate, colormap, *, draw_overlays):
    """A pool worker function reporting the worker's BLAS thread variables and SIGINT state as a skipped row."""
    state: dict[str, object] = {name: os.environ.get(name) for name in preprocess_module._BLAS_THREAD_VARS}
    state["sigint_ignored"] = signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    state["sigint_blocked"] = signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, [])
    return SkippedRow(candidate.image_id, "failed", json.dumps(state))


def _start_large_result() -> None:
    """Write the start of a 10 MB result to the pool's result pipe, as a worker cut off mid-send leaves it.

    The pool's manager thread then reads the rest of that message, which never comes, until the pipe's
    every write end is closed.
    """
    import gc
    import struct
    from multiprocessing.queues import SimpleQueue

    [results] = [o for o in gc.get_objects() if isinstance(o, SimpleQueue)]
    os.write(results._writer.fileno(), struct.pack("!i", 10_000_000) + bytes(1000))


def _die_mid_send(candidate, colormap, *, draw_overlays):
    """A pool worker function that dies part way through sending a large result (as the OOM killer would)."""
    _start_large_result()
    os._exit(1)


def _hang_mid_send(candidate, colormap, *, draw_overlays):
    """A pool worker function that stalls part way through sending a large result, after marking that it did."""
    _start_large_result()
    path = Path(candidate.image_id)
    (path.parent.parent / f"started-{path.name}").touch()
    time.sleep(60)
    return render_and_encode(candidate, colormap, draw_overlays=draw_overlays)


class _CountedFuture(Future):
    def __init__(self, outstanding: set[Future]) -> None:
        super().__init__()
        self.outstanding = outstanding

    def result(self, timeout=None):
        self.outstanding.discard(self)
        return super().result(timeout)


class _CountingExecutor(Executor):
    """Runs each call at once; counts the futures whose result the caller has not taken yet."""

    def __init__(self) -> None:
        self.outstanding: set[Future] = set()
        self.peak = 0
        self.submitted = 0

    def submit(self, fn, /, *args, **kwargs):
        future = _CountedFuture(self.outstanding)
        future.set_result(fn(*args, **kwargs))
        self.outstanding.add(future)
        self.submitted += 1
        self.peak = max(self.peak, len(self.outstanding))
        return future


class _BrokenExecutor(Executor):
    """Fails every call as a pool whose worker died does."""

    def submit(self, fn, /, *args, **kwargs):
        future: Future = Future()
        future.set_exception(BrokenProcessPool())
        return future


class ParallelTest(unittest.TestCase):
    """--jobs N renders in N spawned worker processes; the output is the same as rendering in-process."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name).resolve()
        cls.sources = make_parallel_source(cls.root)
        with quiet():
            cls.serial = run_preprocess(cls.sources, cls.root / "serial", batch_size=4, colormap="viridis", jobs=1)
            with mock.patch.object(preprocess_module, "ProcessPoolExecutor", wraps=ProcessPoolExecutor) as pool:
                cls.pooled = run_preprocess(cls.sources, cls.root / "pooled", batch_size=4, colormap="viridis", jobs=2)
            for name, jobs in (("serial-drawn", 1), ("pooled-drawn", 2)):
                run_preprocess(
                    cls.sources, cls.root / name, batch_size=4, colormap="viridis", jobs=jobs, draw_overlays=True
                )
        cls.pool_calls = pool.call_args_list

    def setUp(self):
        self.tmp = temp_dir(self).resolve()

    def test_output_is_identical_to_jobs_1(self):
        self.assertEqual(self.serial.found, 17)
        self.assertEqual(self.serial.written, 12)  # 10 files, 2 zip entries
        self.assertEqual([s.kind for s in self.serial.skipped], ["failed", "ignored", "failed", "failed", "failed"])
        self.assertEqual([s.reason for s in self.serial.skipped].count(COLLISION_REASON), 2)
        self.assertEqual(self.serial.notes, {**NO_NOTES, "overlay-plane": 1, "overlay-unused-bits": 1, "icon-image": 1})
        pooled_paths = {
            "skipped_path": self.root / "pooled" / "skipped.tsv",
            "notes_path": self.root / "pooled" / "notes.tsv",
        }
        self.assertEqual(self.pooled, replace(self.serial, **pooled_paths))
        serial, pooled = _tree(self.root / "serial"), _tree(self.root / "pooled")
        self.assertEqual(sorted(serial), sorted(pooled))
        for name in serial:
            with self.subTest(name=name):
                self.assertEqual(serial[name], pooled[name])
        jobs = [
            json.loads((self.root / w / "preprocess.json").read_text())["parameters"]["jobs"]
            for w in ("serial", "pooled")
        ]
        self.assertEqual(jobs, [1, 2])

    def test_output_with_draw_overlays_is_identical_to_jobs_1(self):
        serial, pooled = _tree(self.root / "serial-drawn"), _tree(self.root / "pooled-drawn")
        self.assertEqual(sorted(serial), sorted(pooled))
        for name in serial:
            with self.subTest(name=name):
                self.assertEqual(serial[name], pooled[name])
        self.assertTrue(json.loads(serial["preprocess.json"])["parameters"]["draw_overlays"])
        self.assertNotEqual(serial, _tree(self.root / "serial"))  # the overlay is drawn

    def test_workers_are_spawned(self):
        [call] = self.pool_calls
        self.assertEqual(call.kwargs["max_workers"], 2)
        self.assertEqual(call.kwargs["mp_context"].get_start_method(), "spawn")
        self.assertIs(call.kwargs["mp_context"], multiprocessing.get_context("spawn"))
        self.assertEqual(multiprocessing.active_children(), [])

    def test_jobs_1_uses_no_pool(self):
        with mock.patch.object(preprocess_module, "ProcessPoolExecutor") as pool, quiet():
            run_preprocess(self.sources[1:], self.tmp / "work", jobs=1)
        pool.assert_not_called()

    def test_in_flight_inputs_are_bounded(self):
        executor = _CountingExecutor()
        with (
            mock.patch.object(preprocess_module, "_worker_pool", return_value=contextlib.nullcontext(executor)),
            quiet(),
        ):
            result = run_preprocess(self.sources, self.tmp / "work", batch_size=4, colormap="viridis", jobs=3)
        self.assertEqual(executor.submitted, 16)  # every candidate: not the ignored text file
        self.assertEqual(executor.peak, preprocess_module.IN_FLIGHT_PER_JOB * 3)
        self.assertEqual(executor.outstanding, set())
        self.assertEqual(_tree(self.tmp / "work"), _tree(self.root / "serial"))
        self.assertEqual(result.written, self.serial.written)

    def test_worker_crash_fails_the_run(self):
        src = self.tmp / "src"
        src.mkdir()
        for name in ("a.png", "crash.png", "z.png"):
            (src / name).write_bytes(_solid_png(len(name)))
        work = self.tmp / "work"
        with (
            mock.patch.object(preprocess_module, "render_and_encode", _exit_in_worker),
            quiet(),
            self.assertRaises(WorkerCrashed) as ctx,
        ):
            run_preprocess([src], work, jobs=2)
        self.assertIn("worker died", str(ctx.exception))
        self.assertIn("--jobs 1", str(ctx.exception))
        self.assertFalse(work.exists())
        self.assertFalse((self.tmp / ".work.partial").exists())
        self.assertEqual(multiprocessing.active_children(), [])

    def test_workers_run_single_threaded_blas_and_ignore_sigint(self):
        src = self.tmp / "src"
        src.mkdir()
        (src / "a.png").write_bytes(_solid_png(1))
        with mock.patch.dict(os.environ, {"OMP_NUM_THREADS": "3"}):  # set by the user: left alone
            os.environ.pop("OPENBLAS_NUM_THREADS", None)
            os.environ.pop("MKL_NUM_THREADS", None)
            with mock.patch.object(preprocess_module, "render_and_encode", _probe_worker), quiet():
                result = run_preprocess([src], self.tmp / "work", jobs=2)
            self.assertNotIn("OPENBLAS_NUM_THREADS", os.environ)  # restored afterwards
            self.assertNotIn("MKL_NUM_THREADS", os.environ)
            self.assertEqual(os.environ["OMP_NUM_THREADS"], "3")
        [row] = result.skipped
        self.assertEqual(
            json.loads(row.reason),
            {
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "3",
                "MKL_NUM_THREADS": "1",
                "sigint_ignored": True,
                "sigint_blocked": False,
            },
        )

    def test_worker_dying_mid_send_fails_the_run(self):
        src = self.tmp / "src"
        src.mkdir()
        (src / "a.png").write_bytes(_solid_png(1))
        work = self.tmp / "work"
        start = time.monotonic()
        faulthandler.dump_traceback_later(120, exit=True)  # a regression waits forever: fail loudly, not forever
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        with (
            mock.patch.object(preprocess_module, "render_and_encode", _die_mid_send),
            quiet(),
            self.assertRaises(WorkerCrashed) as ctx,
        ):
            run_preprocess([src], work, jobs=2)
        self.assertLess(time.monotonic() - start, 30)
        self.assertIn(f"1 input(s) were in flight, starting with {(src / 'a.png').as_posix()}", str(ctx.exception))
        self.assertIn("exited with code 1", str(ctx.exception.__cause__))
        self.assertEqual(multiprocessing.active_children(), [])
        self.assertFalse(work.exists())
        self.assertFalse((self.tmp / ".work.partial").exists())

    def test_worker_crash_names_the_inputs_in_flight(self):
        # The skipped row waits in the queue behind a.png, so the count must be of inputs in flight, not queue entries
        items = [
            Candidate(ImageId("a.png"), "raster", b""),
            SkippedRow(ImageId("s.txt"), "ignored", "not an image"),
            Candidate(ImageId("b.png"), "raster", b""),
        ]
        outcomes = preprocess_module._outcomes_pooled(items, "viridis", _BrokenExecutor(), limit=4, draw_overlays=False)
        with self.assertRaises(WorkerCrashed) as ctx:
            list(outcomes)
        self.assertIn("2 input(s) were in flight, starting with a.png", str(ctx.exception))

    def test_sigterm_while_a_worker_is_sending_does_not_hang(self):
        src = self.tmp / "src"
        src.mkdir()
        (src / "a.png").write_bytes(_solid_png(1))
        main = threading.main_thread().ident
        assert main is not None

        def terminate_once_sending():
            deadline = time.monotonic() + 30
            while not list(self.tmp.glob("started-*")) and time.monotonic() < deadline:
                time.sleep(0.05)
            signal.pthread_kill(main, signal.SIGTERM)

        work = self.tmp / "work"
        killer = threading.Thread(target=terminate_once_sending)
        start = time.monotonic()
        faulthandler.dump_traceback_later(120, exit=True)  # a regression hangs in shutdown: fail loudly, not forever
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        with (
            mock.patch.object(preprocess_module, "render_and_encode", _hang_mid_send),
            quiet(),
            interrupt_on(*TERMINATION_SIGNALS),
            self.assertRaises(KeyboardInterrupt),
        ):
            killer.start()
            run_preprocess([src], work, jobs=2)
        killer.join()
        self.assertTrue(list(self.tmp.glob("started-*")))
        self.assertLess(time.monotonic() - start, 30)
        self.assertEqual(multiprocessing.active_children(), [])
        self.assertFalse(work.exists())
        self.assertFalse((self.tmp / ".work.partial").exists())

    def test_sigterm_stops_workers_promptly(self):
        src = self.tmp / "src"
        src.mkdir()
        for name in ("a.png", "b.png", "c.png"):
            (src / name).write_bytes(_solid_png(len(name)))
        main = threading.main_thread().ident
        assert main is not None

        def terminate_once_started():
            deadline = time.monotonic() + 30
            while not list(self.tmp.glob("started-*")) and time.monotonic() < deadline:
                time.sleep(0.05)
            signal.pthread_kill(main, signal.SIGTERM)

        work = self.tmp / "work"
        killer = threading.Thread(target=terminate_once_started)
        start = time.monotonic()
        with (
            mock.patch.object(preprocess_module, "render_and_encode", _hang_in_worker),
            quiet(),
            interrupt_on(*TERMINATION_SIGNALS),
            self.assertRaises(KeyboardInterrupt),
        ):
            killer.start()
            run_preprocess([src], work, jobs=2)
        killer.join()
        self.assertTrue(list(self.tmp.glob("started-*")))
        self.assertLess(time.monotonic() - start, 30)  # did not wait for the 60 s renders
        self.assertEqual(multiprocessing.active_children(), [])
        self.assertFalse(work.exists())
        self.assertFalse((self.tmp / ".work.partial").exists())


def _compress_image_reference(image: np.ndarray) -> np.ndarray:
    """The original 2-D skimage erosion implementation of compress_image."""
    same_vert = image == np.roll(image, 1, axis=0)
    same_horiz = image == np.roll(image, 1, axis=1)
    both = same_vert & same_horiz
    if both.ndim == 3:
        both = np.all(both, axis=2)
    uniform = ski.morphology.erosion(both, np.ones((5, 5), dtype=bool))
    image = np.delete(image, np.all(uniform, axis=1), axis=0)
    return np.delete(image, np.all(uniform, axis=0), axis=1)


def _compress_image_cases(rng: np.random.Generator, count: int) -> list[np.ndarray]:
    def banded(shape: tuple[int, ...]) -> np.ndarray:
        img = np.zeros(shape, dtype=np.uint8)
        for axis in (0, 1):
            edges = np.sort(rng.integers(0, shape[axis] + 1, size=rng.integers(1, 6)))
            for lo, hi in zip(edges[::2], edges[1::2], strict=False):
                index = [slice(None)] * len(shape)
                index[axis] = slice(lo, hi)
                img[tuple(index)] = rng.integers(0, 256, size=shape[2:] or None, dtype=np.uint8)
        return img

    def bordered(shape: tuple[int, ...]) -> np.ndarray:
        img = np.zeros(shape, dtype=np.uint8)
        top, left = rng.integers(0, shape[0] // 2 + 1), rng.integers(0, shape[1] // 2 + 1)
        bottom, right = rng.integers(top + 1, shape[0] + 1), rng.integers(left + 1, shape[1] + 1)
        inner = img[top:bottom, left:right]
        inner[...] = rng.integers(0, 256, size=inner.shape, dtype=np.uint8)
        return img

    cases = [
        np.zeros((0, 0), np.uint8),
        np.zeros((1, 1), np.uint8),
        np.full((40, 30), 7, np.uint8),
        np.full((40, 30, 3), 7, np.uint8),
    ]
    makers = ("noise", "banded", "bordered", "uniform", "small")
    while len(cases) < count:
        kind = makers[len(cases) % len(makers)]
        channels = (3,) if rng.random() < 0.4 else ()
        h, w = (int(n) for n in rng.integers(1, 80, size=2))
        if kind == "small":
            h, w = (int(n) for n in rng.integers(1, 6, size=2))
            if rng.random() < 0.5:
                h, w = (h, 60) if rng.random() < 0.5 else (60, w)
        shape = (h, w, *channels)
        if kind == "noise":
            img = rng.integers(0, int(rng.choice([2, 256])), size=shape, dtype=np.uint8)
        elif kind == "banded":
            img = banded(shape)
        elif kind == "bordered":
            img = bordered(shape)
        elif kind == "uniform":
            img = np.full(shape, rng.integers(0, 256), dtype=np.uint8)
        else:
            img = rng.integers(0, 3, size=shape, dtype=np.uint8)
        cases.append(img)
    return cases


class CompressImageTest(unittest.TestCase):
    def test_matches_2d_erosion_reference(self):
        for i, image in enumerate(_compress_image_cases(np.random.default_rng(12345), 300)):
            with self.subTest(case=i, shape=image.shape):
                expected = _compress_image_reference(image)
                actual = compress_image(image)
                self.assertEqual(actual.shape, expected.shape)
                self.assertEqual(actual.dtype, expected.dtype)
                self.assertTrue(np.array_equal(actual, expected))


if __name__ == "__main__":
    unittest.main()
