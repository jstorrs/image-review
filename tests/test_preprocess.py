"""Preprocess: every input lands in exactly one of manifest.tsv or skipped.tsv."""

import contextlib
import csv
import faulthandler
import hashlib
import io
import json
import multiprocessing
import os
import signal
import stat
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np
import pydicom
import skimage as ski
from PIL import Image, JpegImagePlugin, PngImagePlugin
from pydicom.data import get_testdata_file, get_testdata_files
from pydicom.dataset import FileMetaDataset
from pydicom.uid import MediaStorageDirectoryStorage, generate_uid

from image_review import cli as cli_module
from image_review import preprocess as preprocess_module
from image_review.cli import default_jobs
from image_review.preprocess import (
    COLLISION_REASON,
    SIDE_BY_SIDE_GAP,
    Candidate,
    DecodeError,
    Rejected,
    Skipped,
    Unsupported,
    WorkDirExists,
    WorkerCrashed,
    _failed,
    classify,
    colliding_ids,
    compress_image,
    decode_raster,
    discover,
    render,
    render_and_encode,
    run_preprocess,
)
from image_review.signals import TERMINATION_SIGNALS, interrupt_on
from tests.fixtures import add_overlay, invoke_cli, write_dicom

RNG = np.random.default_rng(0)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
APPLEDOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + bytes(100)
# SLURM_CPUS_PER_TASK=1 renders in-process, with no worker start-up per run
CLI_ENV = {"SLURM_CPUS_PER_TASK": "1"}


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


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="") as f:
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


class MixedSourceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
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
            with open(root / "work" / "skipped.tsv", newline="") as f:
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


def _dicom_with_icon(path: Path, icon_pixels: np.ndarray | None = None) -> None:
    """A DICOM with an 8x8 8-bit icon: `icon_pixels`, or a ramp by default."""
    icon = pydicom.Dataset()
    icon.Rows, icon.Columns, icon.SamplesPerPixel = 8, 8, 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.BitsAllocated, icon.BitsStored, icon.HighBit, icon.PixelRepresentation = 8, 8, 7, 0
    icon.PixelData = (np.arange(64, dtype=np.uint8) if icon_pixels is None else icon_pixels).tobytes()
    write_dicom(path, _good_pixels(), IconImageSequence=[icon])


def _png_text(key: str, value: str) -> PngImagePlugin.PngInfo:
    info = PngImagePlugin.PngInfo()
    info.add_text(key, value)
    return info


def _solid_png(value: int) -> bytes:
    return _png_bytes(np.full((40, 60, 3), value, dtype=np.uint8))


class CollisionTest(unittest.TestCase):
    """An image_id naming two different images would share one verdict: every input involved fails instead."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
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
            {"inputs": inputs, "written": len(kept), "skipped_failed": len(expected_ids), "skipped_ignored": 0},
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

    def test_file_named_like_a_dicom_icon(self):
        _dicom_with_icon(self.src / "a.dcm")
        (self.src / "a.dcm#icon").write_bytes(_solid_png(255))
        (self.src / "b.png").write_bytes(_solid_png(0))
        self.run_quietly()
        dicom = (self.src / "a.dcm").as_posix()
        # a.dcm fails as a whole: its main image is not kept without its icon
        self.assert_collision([dicom, f"{dicom}#icon"], kept=[(self.src / "b.png").as_posix()], inputs=3)
        self.assert_cli_exit_codes()

    def test_input_whose_main_image_and_icon_both_collide_is_listed_once(self):
        _dicom_with_icon(self.root / "x.dcm")
        dicom = (self.root / "x.dcm").read_bytes()
        archive = self.src / "z.zip"
        with warnings.catch_warnings(), zipfile.ZipFile(archive, "w") as zf:
            warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
            zf.writestr("a.dcm", dicom)
            zf.writestr("a.dcm", dicom)  # becomes a.dcm#2, icon a.dcm#2#icon
            # random noise makes the main image differ; the icon differs too
            _dicom_with_icon(self.root / "y.dcm", np.full(64, 200, dtype=np.uint8))
            zf.writestr("a.dcm#2", (self.root / "y.dcm").read_bytes())
        self.run_quietly()
        base = f"{archive.as_posix()}::a.dcm"
        self.assert_collision([f"{base}#2", f"{base}#2"], kept=[base, f"{base}#icon"], inputs=3)

    def test_dicom_with_icon_is_not_a_collision(self):
        _dicom_with_icon(self.src / "a.dcm")
        result = self.run_quietly()
        dicom = (self.src / "a.dcm").as_posix()
        self.assertEqual([r["image_id"] for r in _read_tsv(self.work / "manifest.tsv")], [dicom, f"{dicom}#icon"])
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
        rendered = [render("raster", "x", data, "gray")[0].rgb for data in (plain, buf.getvalue())]
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

    def test_colliding_dicom_with_a_failed_icon_is_one_row(self):
        broken_icon = pydicom.Dataset()
        broken_icon.Rows = 4  # no pixel data: the icon fails to render
        write_dicom(self.root / "a.dcm", _good_pixels(), IconImageSequence=[broken_icon])
        archive = self.src / "z.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.dcm", (self.root / "a.dcm").read_bytes())
        (self.src / "z.zip::a.dcm").write_bytes(_solid_png(255))
        self.run_quietly()
        dicom = f"{archive.as_posix()}::a.dcm"
        self.assert_collision([dicom, dicom], kept=[], inputs=2)  # no `#icon` row is left behind


class ProvenanceTest(unittest.TestCase):
    """preprocess.json records how the work dir was made; manifest.tsv records source and JPG hashes."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
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
                "tail_fraction": preprocess_module.TAIL_FRACTION,
                "jpeg_quality": preprocess_module.JPEG_QUALITY,
                "jpeg_subsampling": preprocess_module.JPEG_SUBSAMPLING,
                "access": "private",
                "jobs": 1,
            },
        )
        self.assertLessEqual({"pydicom", "numpy", "scikit-image", "Pillow", "matplotlib"}, set(record["libraries"]))
        self.assertEqual(record["libraries"]["pydicom"], pydicom.__version__)
        self.assertEqual(record["counts"], {"inputs": 5, "written": 4, "skipped_failed": 1, "skipped_ignored": 1})
        self.assertEqual((self.result.found, self.result.written), (5, 4))
        self.assertEqual(stat.S_IMODE((self.work / "preprocess.json").stat().st_mode), 0o600)

    def test_preprocess_json_follows_group_policy(self):
        work = self.root / "shared"
        with quiet():
            run_preprocess([self.src], work, access="group")
        self.assertEqual(stat.S_IMODE((work / "preprocess.json").stat().st_mode), 0o660)
        self.assertEqual(json.loads((work / "preprocess.json").read_text())["parameters"]["access"], "group")

    def test_source_sha256_of_a_file_is_shared_by_its_icon(self):
        main = (self.src / "icon.dcm").as_posix()
        expected = hashlib.sha256((self.src / "icon.dcm").read_bytes()).hexdigest()
        self.assertEqual(self.manifest[main]["source_sha256"], expected)
        self.assertEqual(self.manifest[f"{main}#icon"]["source_sha256"], expected)
        plain = (self.src / "plain.dcm").as_posix()
        self.assertEqual(
            self.manifest[plain]["source_sha256"], hashlib.sha256((self.src / "plain.dcm").read_bytes()).hexdigest()
        )

    def test_source_sha256_of_a_zip_entry_is_of_the_entry(self):
        row = self.manifest[f"{self.archive.as_posix()}::dir/scan.png"]
        self.assertEqual(row["source_sha256"], hashlib.sha256(self.entry).hexdigest())
        self.assertNotEqual(row["source_sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def test_jpeg_sha256_matches_the_file(self):
        self.assertEqual(len(self.manifest), 4)
        for row in self.manifest.values():
            data = (self.work / row["preprocessed_path"]).read_bytes()
            self.assertEqual(row["jpeg_sha256"], hashlib.sha256(data).hexdigest())

    def test_source_is_read_once(self):
        reads = []

        def counting(path: Path) -> bytes:
            reads.append(path)
            return real(path)

        real = Path.read_bytes
        with mock.patch.object(Path, "read_bytes", counting), quiet():
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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

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
            [Skipped(p.as_posix(), "ignored", "zip contains no files") for p in (empty, dirs_only)],
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
            [Skipped((s1 / "s2link").as_posix(), "ignored", f"symlinked directory already included via SOURCE {s2}")],
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
        self.assertEqual(result.skipped, [Skipped(path.as_posix(), "ignored", "DICOMDIR index")])

    def test_nested_zip_is_failed(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as zf:
            zf.writestr("a.png", _png_bytes(np.zeros((40, 60, 3), dtype=np.uint8)))
        archive = self.root / "outer.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("inner.zip", nested.getvalue())
        result = self.run_quietly([archive])
        self.assertEqual(
            result.skipped, [Skipped(f"{archive.as_posix()}::inner.zip", "failed", "unsupported: nested zip")]
        )


class RunErrorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

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

    def test_render_returning_nothing_is_a_failure(self):
        write_dicom(self.root / "good.dcm", _good_pixels())
        with quiet(), mock.patch("image_review.preprocess.render", return_value=[]):
            result = run_preprocess([self.root / "good.dcm"], self.root / "work")
        self.assertEqual(result.written, 0)
        self.assertEqual([(s.kind, s.reason) for s in result.skipped], [("failed", "rendered no images")])

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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
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
        real = preprocess_module._process
        calls = []

        def interrupt_on_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(*args, **kwargs)

        with (
            quiet(),
            mock.patch("image_review.preprocess._process", side_effect=interrupt_on_second),
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


class DecodeRasterTest(unittest.TestCase):
    def test_cmyk_jpeg_keeps_black_text(self):
        cmyk = np.zeros((40, 60, 4), dtype=np.uint8)
        cmyk[_text_mask(), 3] = 255  # text only in K
        buf = io.BytesIO()
        Image.fromarray(cmyk, mode="CMYK").save(buf, "JPEG", quality=95)
        img = decode_raster(buf.getvalue())
        self.assertEqual(img.shape, (40, 60, 3))
        self.assertLess(img[_text_mask()].mean(), 0.25)
        self.assertGreater(img[~_text_mask()].mean(), 0.75)

    def test_alpha_only_text_visible_in_composited_left_half(self):
        alpha = np.where(_text_mask(), 255, 0).astype(np.uint8)
        rgba = np.dstack([np.zeros((40, 60, 3), dtype=np.uint8), alpha])
        la = np.dstack([np.zeros((40, 60), dtype=np.uint8), alpha])
        for arr, mode in ((rgba, "RGBA"), (la, "LA")):
            with self.subTest(mode=mode):
                img = decode_raster(_png_bytes(arr, mode))
                self.assertEqual(
                    img.shape, (40, 120 + SIDE_BY_SIDE_GAP, 3) if mode == "RGBA" else (40, 120 + SIDE_BY_SIDE_GAP)
                )
                left, right = _halves(img, 60)
                self.assertTrue((left[_text_mask()] == 0).all())
                self.assertTrue((left[~_text_mask()] == 0.5).all())
                self.assertTrue((right == 0).all())

    def test_text_under_transparent_pixels_visible_in_raw_right_half(self):
        rgb = np.full((40, 60, 3), 255, dtype=np.uint8)
        rgb[_text_mask()] = 0
        rgba = np.dstack([rgb, np.zeros((40, 60), dtype=np.uint8)])  # fully transparent: an erased layer
        img = decode_raster(_png_bytes(rgba, "RGBA"))
        left, right = _halves(img, 60)
        self.assertTrue((left == 0.5).all())
        self.assertTrue((right[_text_mask()] == 0).all())
        self.assertTrue((right[~_text_mask()] == 1).all())
        [out] = render("raster", "id", _png_bytes(rgba, "RGBA"), "inferno")
        self.assertEqual(out.rgb.shape, (40, 120 + SIDE_BY_SIDE_GAP, 3))

    def test_opaque_alpha_is_dropped(self):
        rgb = RNG.integers(0, 255, (40, 60, 3), dtype=np.uint8)
        rgba = np.dstack([rgb, np.full((40, 60), 255, dtype=np.uint8)])
        np.testing.assert_array_equal(ski.util.img_as_ubyte(decode_raster(_png_bytes(rgba, "RGBA"))), rgb)

    def test_16_bit_grayscale_png_stays_grayscale(self):
        gray = (np.arange(40 * 60).reshape(40, 60) * 20).astype(np.uint16)
        img = decode_raster(_png_bytes(gray))
        self.assertEqual(img.ndim, 2)
        np.testing.assert_allclose(img, gray / 65535, atol=1e-6)
        [out] = render("raster", "id", _png_bytes(gray), "inferno")
        self.assertEqual(out.rgb.shape, (40, 60, 3))

    def test_16_bit_grayscale_with_trns_keeps_full_range(self):
        gray = np.full((40, 60), 30000, dtype=np.uint16)
        gray[_text_mask()] = 50000
        gray[:5, :5] = 0  # the transparent key
        buf = io.BytesIO()
        Image.fromarray(gray).save(buf, "PNG", transparency=0)
        img = decode_raster(buf.getvalue())
        self.assertEqual(img.ndim, 2)
        left, right = _halves(img, 60)
        for view in (left, right):
            self.assertAlmostEqual(float(view[20, 20]), 50000 / 65535, places=5)
            self.assertAlmostEqual(float(view[10, 5]), 30000 / 65535, places=5)
        self.assertEqual(left[0, 0], 0.5)
        self.assertEqual(right[0, 0], 0)

    def test_mpo_frames_render_side_by_side(self):
        buf = io.BytesIO()
        primary = Image.new("RGB", (60, 40), (255, 255, 255))
        gain_map = Image.new("L", (30, 20), 128)
        primary.save(buf, "MPO", save_all=True, append_images=[gain_map])
        img = decode_raster(buf.getvalue())
        self.assertEqual(img.shape, (40, 60 + SIDE_BY_SIDE_GAP + 30, 3))
        left, right = _halves(img, 60)
        self.assertGreater(left.min(), 0.95)
        self.assertAlmostEqual(float(right[:20].mean()), 128 / 255, delta=0.02)
        self.assertTrue((right[20:] == 0).all())  # padded below the shorter frame
        [out] = render("raster", "id", buf.getvalue(), "inferno")
        self.assertEqual(out.rgb.shape, (40, 94, 3))

    def test_animated_png_is_unsupported(self):
        buf = io.BytesIO()
        frames = [Image.new("L", (20, 20), v) for v in (0, 255)]
        frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:])
        with self.assertRaisesRegex(Unsupported, r"^unsupported: multi-frame image \(2 frames\)$"):
            decode_raster(buf.getvalue())


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

    def test_overlay_is_drawn_at_its_origin(self):
        pixels = RNG.integers(0, 1001, (200, 200)).astype(np.uint16)  # noise: nothing is cropped
        block = np.ones((5, 8), dtype=bool)
        data = self.with_elements(pixels, edit=lambda ds: add_overlay(ds, block, origin=(10, 20)))
        [out, *rest] = render("dicom", "id", data, "gray")
        self.assertEqual(rest, [])
        self.assertEqual(out.rgb.shape, (200, 200, 3))
        self.assertGreaterEqual(out.rgb[9:14, 19:27].min(), 250)  # 1-based (10, 20) is zero-based (9, 19)
        self.assertLess(out.rgb[8, 19:27].mean(), 250)
        self.assertLess(out.rgb[9:14, 18].mean(), 250)

    def test_overlay_is_clipped_to_the_image(self):
        pixels = RNG.integers(0, 1001, (50, 60)).astype(np.uint16)
        block = np.ones((20, 20), dtype=bool)
        data = self.with_elements(pixels, edit=lambda ds: add_overlay(ds, block, origin=(40, 50)))
        [out] = render("dicom", "id", data, "gray")
        self.assertEqual(out.rgb.shape, (50, 60, 3))
        self.assertGreaterEqual(out.rgb[39:, 49:].min(), 250)

    def test_overlay_is_drawn_on_colour_images(self):
        pixels = RNG.integers(0, 200, (60, 80, 3)).astype(np.uint8)
        block = np.ones((4, 4), dtype=bool)
        data = self.with_elements(pixels, "RGB", lambda ds: add_overlay(ds, block, origin=(5, 6)))
        [out] = render("dicom", "id", data, "inferno")
        self.assertTrue((out.rgb[4:8, 5:9] == 255).all())

    def test_undecodable_overlay_fails_the_file_naming_the_group(self):
        def edit(ds):
            add_overlay(ds, np.ones((8, 8), dtype=bool), group=0x6002)
            ds[0x6002, 0x3000].value = b"\x00\x00"  # far too short for 8x8 bits

        data = self.with_elements(_good_pixels(), edit=edit)
        with self.assertRaisesRegex(ValueError, r"overlay 0x6002 cannot be decoded"):
            render("dicom", "id", data, "gray")

    def test_bundled_overlay_and_icon_are_rendered(self):
        path = get_testdata_file("examples_overlay.dcm", download=False)
        if path is None:
            self.skipTest("examples_overlay.dcm not bundled")
        data = Path(path).read_bytes()

        def strip(ds):
            for tag in [e.tag for e in ds if 0x6000 <= e.tag.group <= 0x601E]:
                del ds[tag]

        bare = self.with_elements_from(data, strip)
        [main, icon] = render("dicom", "/x/a.dcm", data, "gray")
        [plain, plain_icon] = render("dicom", "/x/a.dcm", bare, "gray")
        self.assertEqual((main.image_id, icon.image_id), ("/x/a.dcm", "/x/a.dcm#icon"))
        self.assertEqual(plain_icon.image_id, "/x/a.dcm#icon")
        self.assertFalse(np.array_equal(main.rgb, plain.rgb))
        mask = pydicom.dcmread(path).overlay_array(0x6000).astype(bool)
        self.assertGreaterEqual(np.percentile(main.rgb[..., 0][mask], 5), 250)
        self.assertEqual(icon.rgb.ndim, 3)

    def with_elements_from(self, data: bytes, edit) -> bytes:
        ds = pydicom.dcmread(io.BytesIO(data))
        edit(ds)
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        return buf.getvalue()

    def test_failed_icon_is_a_skipped_row_beside_the_main_image(self):
        def edit(ds):
            item = pydicom.Dataset()
            item.Rows = 4  # no pixel data
            ds.IconImageSequence = [item]

        data = self.with_elements(_good_pixels(), edit=edit)
        [main, icon] = render("dicom", "/x/a.dcm", data, "gray")
        self.assertIsInstance(main, preprocess_module.Rendered)
        self.assertEqual(icon, Skipped("/x/a.dcm#icon", "failed", "unsupported: no pixel data"))

    def test_icon_row_lands_in_manifest_and_failed_icon_in_skipped_tsv(self):
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
            with open(root / "work" / "manifest.tsv", newline="") as f:
                ids = [row["image_id"] for row in csv.DictReader(f, delimiter="\t")]
        ok, bad = str(root / "src" / "ok.dcm"), str(root / "src" / "bad.dcm")
        self.assertEqual(sorted(ids), sorted([ok, f"{ok}#icon", bad]))
        self.assertEqual([(s.image_id, s.kind) for s in result.skipped], [(f"{bad}#icon", "failed")])
        self.assertEqual(result.found, 2)

    def test_all_zero_dicom_keeps_uncropped_image(self):
        with quiet():
            [out] = render("dicom", "id", self.dicom_bytes(np.zeros((40, 60), dtype=np.uint16)), "inferno")
        self.assertEqual(out.rgb.shape, (40, 60, 3))
        self.assertEqual(out.rgb.dtype, np.uint8)

    def test_monochrome1_is_inverted(self):
        pixels = _good_pixels()  # values grow from the top row to the bottom row
        [mono2] = render("dicom", "id", self.dicom_bytes(pixels, "MONOCHROME2"), "gray")
        [mono1] = render("dicom", "id", self.dicom_bytes(pixels, "MONOCHROME1"), "gray")
        self.assertGreater(mono2.rgb[-10:].mean(), mono2.rgb[:10].mean() + 100)
        self.assertLess(mono1.rgb[-10:].mean(), mono1.rgb[:10].mean() - 100)

    def luminance(self, rgb: np.ndarray) -> np.ndarray:
        return rgb @ np.array([0.299, 0.587, 0.114])

    def test_bright_text_on_bright_border_stays_visible(self):
        rng = np.random.default_rng(1)
        pixels = rng.integers(0, 1001, (200, 200)).astype(np.uint16)
        band = np.zeros((200, 200), dtype=bool)
        band[:, :12] = True
        band[:, -12:] = True
        pixels[band] = rng.integers(3800, 3951, int(band.sum()))
        text = np.zeros_like(band)
        text[20:24, 2:10] = True
        text[60:64, 2:10] = True
        text[100:104, -10:-2] = True
        pixels[text] = 4095
        for colormap in ("inferno", "gray"):
            with self.subTest(colormap=colormap):
                [out] = render("dicom", "id", self.dicom_bytes(pixels), colormap)
                self.assertEqual(out.rgb.shape[:2], pixels.shape)
                lum = self.luminance(out.rgb.astype(float))
                self.assertGreaterEqual(lum[text].mean() - lum[band & ~text].mean(), 20)

    def test_two_level_text_image_renders_text_bright(self):
        for dtype, low, high in ((np.uint16, 0, 4095), (np.int16, -1000, 3000)):
            with self.subTest(dtype=dtype.__name__):
                pixels = np.full((60, 80), low, dtype=dtype)
                pixels[20:30, 10:40:2] = high  # stripes, so cropping keeps the text
                [out] = render("dicom", "id", self.dicom_bytes(pixels), "gray")
                lum = self.luminance(out.rgb.astype(float))
                self.assertGreater(lum.max(), 200)
                self.assertLess(lum.min(), 50)

    def test_uniform_dicom_renders(self):
        [out] = render("dicom", "id", self.dicom_bytes(np.full((30, 40), 1234, dtype=np.uint16)), "gray")
        self.assertEqual(out.rgb.shape, (30, 40, 3))

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
        [out] = render("dicom", "id", self.dicom_bytes(pixels, "RGB"), "inferno")
        self.assertEqual(out.rgb.dtype, np.uint8)
        np.testing.assert_array_equal(out.rgb, pixels)

    def test_high_bit_rgb_dicom_scales_by_bits_stored(self):
        pixels = np.full((32, 40, 3), 65535, dtype=np.uint16)
        pixels[8:24, 10:30, 0] = 0
        [out] = render("dicom", "id", self.dicom_bytes(pixels, "RGB"), "inferno")
        self.assertEqual(out.rgb.dtype, np.uint8)
        self.assertEqual(out.rgb.max(), 255)
        self.assertEqual(out.rgb.min(), 0)

    def test_bundled_colour_dicoms_render_rgb_uint8(self):
        for name in ("examples_rgb_color.dcm", "SC_ybr_full_422_uncompressed.dcm", "examples_palette.dcm"):
            with self.subTest(name=name):
                path = get_testdata_file(name, download=False)
                if path is None:
                    self.skipTest(f"{name} not bundled")
                [out] = render("dicom", "id", Path(path).read_bytes(), "inferno")
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
                [out] = render("dicom", "id", Path(path).read_bytes(), "inferno")
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
        path = get_testdata_file("JPEG2000-embedded-sequence-delimiter.dcm", download=False)
        if path is None:
            self.skipTest("file not bundled")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with self.assertRaises(DecodeError) as ctx:
                render("dicom", "id", Path(path).read_bytes(), "inferno")
        reason = _failed("id", ctx.exception).reason
        self.assertTrue(reason.startswith("cannot decode JPEG 2000 Image Compression: "), reason)
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
        [out] = render("dicom", "id", buf.getvalue(), "gray")
        self.assertEqual(out.rgb.dtype, np.uint8)

    def test_failure_reason_has_no_tabs_or_newlines(self):
        skipped = _failed("id", ValueError("bad\tthing\nhappened"))
        self.assertEqual(skipped.reason, "ValueError: bad thing happened")
        self.assertEqual(_failed("id", Unsupported("x")).reason, "unsupported: x")


class PreprocessCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
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
        self.assertNotIn("Error", result.output)

    def test_failures_exit_1(self):
        result = self.invoke()
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Found 7 inputs: wrote 5 images in 1 batches; 2 skipped (2 failed, 0 ignored;", result.output)
        self.assertIn("--allow-skipped", result.output)
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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cgroups = Path(tmp.name) / "cgroup"  # a fake cgroup v2 mount
        self.cgroups.mkdir()
        self.cpu_max = self.cgroups / "cpu.max"  # missing until a test writes it
        self.proc_cgroup = Path(tmp.name) / "proc-self-cgroup"
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
    write_dicom(src / "m1.dcm", _good_pixels(), "MONOCHROME1")
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


def _exit_in_worker(kind, image_id, data, colormap):
    """A pool worker function that kills its process on the input named `crash.png`."""
    if image_id.endswith("crash.png"):
        os._exit(1)
    return render_and_encode(kind, image_id, data, colormap)


def _hang_in_worker(kind, image_id, data, colormap):
    """A pool worker function that marks that it started (beside the source directory), then hangs."""
    path = Path(image_id)
    (path.parent.parent / f"started-{path.name}").touch()
    time.sleep(60)
    return render_and_encode(kind, image_id, data, colormap)


def _probe_worker(kind, image_id, data, colormap):
    """A pool worker function reporting the worker's BLAS thread variables and SIGINT state as a skipped row."""
    state: dict[str, object] = {name: os.environ.get(name) for name in preprocess_module._BLAS_THREAD_VARS}
    state["sigint_ignored"] = signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    state["sigint_blocked"] = signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, [])
    return [Skipped(image_id, "failed", json.dumps(state))]


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


def _die_mid_send(kind, image_id, data, colormap):
    """A pool worker function that dies part way through sending a large result (as the OOM killer would)."""
    _start_large_result()
    os._exit(1)


def _hang_mid_send(kind, image_id, data, colormap):
    """A pool worker function that stalls part way through sending a large result, after marking that it did."""
    _start_large_result()
    path = Path(image_id)
    (path.parent.parent / f"started-{path.name}").touch()
    time.sleep(60)
    return render_and_encode(kind, image_id, data, colormap)


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
        cls.pool_calls = pool.call_args_list

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def test_output_is_identical_to_jobs_1(self):
        self.assertEqual(self.serial.found, 17)
        self.assertEqual(self.serial.written, 13)  # 10 files, an icon, 2 zip entries
        self.assertEqual([s.kind for s in self.serial.skipped], ["failed", "ignored", "failed", "failed", "failed"])
        self.assertEqual([s.reason for s in self.serial.skipped].count(COLLISION_REASON), 2)
        self.assertEqual(self.pooled, replace(self.serial, skipped_path=self.root / "pooled" / "skipped.tsv"))
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
