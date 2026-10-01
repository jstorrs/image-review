"""Preprocess: every input lands in exactly one of manifest.tsv or skipped.tsv."""

import contextlib
import csv
import io
import os
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np
import pydicom
import skimage as ski
from click.testing import CliRunner
from fixtures import write_dicom
from PIL import Image

from image_review import preprocess as preprocess_module
from image_review.cli import cli
from image_review.preprocess import (
    SIDE_BY_SIDE_GAP,
    Unsupported,
    WorkDirExists,
    _failed,
    decode_raster,
    render,
    run_preprocess,
)

RNG = np.random.default_rng(0)


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
    paths["corrupt_jpg"].write_bytes(b"this is not a jpeg")
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
        expected = {self.paths[k].as_posix() for k in ("good", "zero", "rgba", "la")}
        self.assertEqual(rendered, expected)

        reasons = {r["image_id"]: r["reason"] for r in skipped}
        self.assertTrue(all(r["kind"] == "failed" for r in skipped))
        self.assertTrue(reasons[self.paths["rgb"].as_posix()].startswith("unsupported: photometric interpretation RGB"))
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

    def test_batches_are_numbered_by_write_order(self):
        work = self.root / "work"
        result = self.run_quietly(work, batch_size=3)
        keys = [r["preprocessed_path"] for r in _read_tsv(work / "manifest.tsv")]
        self.assertEqual(
            keys,
            ["batch_001/img_00001.jpg", "batch_001/img_00002.jpg", "batch_001/img_00003.jpg", "batch_002/img_00001.jpg"],
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
                zf.writestr("dir/broken.png", b"not a png")
                zf.writestr("dir/notes.txt", b"ignored by extension")
            with quiet():
                result = run_preprocess([archive], root / "work")
            manifest = _read_tsv(root / "work" / "manifest.tsv")
            skipped = _read_tsv(root / "work" / "skipped.tsv")
            self.assertEqual([r["image_id"] for r in manifest], [f"{archive.as_posix()}::dir/good.dcm"])
            self.assertEqual([r["image_id"] for r in skipped], [f"{archive.as_posix()}::dir/broken.png"])
            self.assertEqual(result.found, 2)


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
        with quiet(), mock.patch("image_review.preprocess._write_tsv", side_effect=OSError("disk full")), self.assertRaises(OSError):
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

        with quiet(), mock.patch("image_review.preprocess._process", side_effect=interrupt_on_second), self.assertRaises(KeyboardInterrupt):
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
                self.assertEqual(img.shape, (40, 120 + SIDE_BY_SIDE_GAP, 3) if mode == "RGBA" else (40, 120 + SIDE_BY_SIDE_GAP))
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

    def test_dicom_without_pixel_data_is_unsupported(self):
        ds = pydicom.dcmread(io.BytesIO(self.dicom_bytes(_good_pixels())))
        del ds.PixelData
        buf = io.BytesIO()
        ds.save_as(buf, enforce_file_format=True)
        with self.assertRaisesRegex(Unsupported, r"^unsupported: no pixel data$"):
            render("dicom", "id", buf.getvalue(), "inferno")

    def test_multiframe_dicom_is_unsupported(self):
        pixels = np.ones((2, 16, 16), dtype=np.uint16)
        with self.assertRaisesRegex(Unsupported, r"^unsupported: multi-frame DICOM \(2 frames\)$"):
            render("dicom", "id", self.dicom_bytes(pixels), "inferno")

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
        env = {"IMAGE_REVIEW_REMOTE": None, "IMAGE_REVIEW_VIA": None}  # ignore the developer's environment
        with quiet():
            return CliRunner().invoke(cli, ["preprocess", str(self.root / source), "--work-dir", str(self.root / "work"), *args], env=env)

    def test_all_good_source_exits_0(self):
        good = self.root / "good"
        good.mkdir()
        write_dicom(good / "a.dcm", _good_pixels())
        result = self.invoke(source="good")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Found 1 inputs: wrote 1 images in 1 batches; 0 skipped", result.output)
        self.assertNotIn("Error", result.output)

    def test_failures_exit_1(self):
        result = self.invoke()
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Found 7 inputs: wrote 4 images in 1 batches; 3 skipped", result.output)
        self.assertIn("--allow-skipped", result.output)
        self.assertTrue((self.root / "work" / "manifest.tsv").exists())
        self.assertTrue((self.root / "work" / "skipped.tsv").exists())

    def test_allow_skipped_exits_0(self):
        result = self.invoke("--allow-skipped")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"3 skipped (see {self.root / 'work' / 'skipped.tsv'})", result.output)

    def test_existing_work_dir_exits_1(self):
        self.invoke("--allow-skipped")
        manifest = (self.root / "work" / "manifest.tsv").read_bytes()
        result = self.invoke("--allow-skipped")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("already exists", result.output)
        self.assertEqual((self.root / "work" / "manifest.tsv").read_bytes(), manifest)


if __name__ == "__main__":
    unittest.main()
