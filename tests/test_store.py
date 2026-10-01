import contextlib
import csv
import errno
import gc
import io
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
import weakref
from pathlib import Path
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import numpy as np
import pygame as pg
import skimage as ski
from click.testing import CliRunner
from fixtures import ROWS, make_work_dir
from PIL import Image

from image_review import controller as controller_module
from image_review import grid_packer as grid_packer_module
from image_review import review_db as review_db_module
from image_review import store as store_module
from image_review.cli import cli, unknown_batch_message
from image_review.connection import package_version
from image_review.controller import (
    ADVANCE_EVENT,
    AUTOPLAY_EVENT,
    GRID_HAS_DIRTY,
    MIN_DWELL_MS,
    NO_TODO_MESSAGE,
    NOTHING_TO_UNDO,
    UNLOADABLE_CLEAN,
    ReviewItem,
    ReviewSession,
    UIState,
    _dwell_elapsed,
    next_batch,
    next_index,
)
from image_review.grid_packer import fit_size, pack_into_grids
from image_review.review_db import HEADER, ReviewDB
from image_review.store import (
    LOCK_NAME,
    LocalStore,
    ManifestRow,
    SkippedCounts,
    WorkDirLocked,
    batch_summary,
    boot_id,
    filter_rows,
    summary,
)
from image_review.util import load_surface


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        make_work_dir(self.work_dir)
        self.store = LocalStore(self.work_dir)
        self.addCleanup(self.store.close)


def _jpeg_bytes(mode: str) -> bytes:
    """A 257x131 gradient-plus-noise JPG made like preprocess: Pillow, q95, 4:4:4."""
    rng = np.random.default_rng(0)
    y, x = np.mgrid[0:131, 0:257]
    base = np.stack([x * 255 // 256, y * 255 // 130, (x + y) % 256], axis=-1)
    pixels = np.clip(base + rng.integers(-20, 20, base.shape), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(pixels).convert(mode).save(buf, "JPEG", quality=95, subsampling=0)
    return buf.getvalue()


def _skimage_reference(buf: bytes) -> np.ndarray:
    """The old skimage-based decode, as (w, h, 3) like surfarray."""
    img = ski.io.imread(io.BytesIO(buf))
    if img.ndim == 2:
        img = np.stack([img, img, img], axis=-1)
    return img.transpose(1, 0, 2)


class TestManifestAndBytes(StoreTestCase):
    def test_manifest_keys_are_preprocessed_paths(self):
        self.assertEqual(
            self.store.manifest(),
            [ManifestRow(key=key, batch=batch) for batch, key, _ in ROWS],
        )

    def test_image_bytes_known_key(self):
        data = self.store.image_bytes("batch_001/a.jpg")
        self.assertEqual(data, (self.work_dir / "batch_001/a.jpg").read_bytes())

    def test_image_bytes_rejects_unknown_keys(self):
        for key in ("batch_001/nope.jpg", "../manifest.tsv", "batch_001/../../etc/passwd", "/src/patient_smith/a.dcm"):
            with self.subTest(key=key), self.assertRaises(KeyError):
                self.store.image_bytes(key)

    def test_load_surface_from_bytes(self):
        surface = load_surface(self.store.image_bytes("batch_002/c.jpg"))
        self.assertEqual(surface.get_size(), (20, 12))

    def test_load_surface_matches_skimage_decode(self):
        for mode in ("RGB", "L"):
            with self.subTest(mode=mode):
                buf = _jpeg_bytes(mode)
                diff = np.abs(pg.surfarray.array3d(load_surface(buf)).astype(int) - _skimage_reference(buf).astype(int))
                self.assertLessEqual(diff.max(), 1)

    def test_load_surface_grayscale_is_rgb_grey(self):
        surface = load_surface(_jpeg_bytes("L"))
        self.assertGreaterEqual(surface.get_bitsize(), 24)
        pixels = pg.surfarray.array3d(surface)
        np.testing.assert_array_equal(pixels[:, :, 0], pixels[:, :, 1])
        np.testing.assert_array_equal(pixels[:, :, 1], pixels[:, :, 2])

    def test_load_surface_raises_on_truncated(self):
        buf = _jpeg_bytes("RGB")
        for frac in (0.5, 0.9):
            with self.subTest(frac=frac), self.assertRaises(Exception):  # noqa: B017 - any raise means unloadable
                load_surface(buf[: int(len(buf) * frac)])

    def test_load_surface_raises_on_garbage(self):
        with self.assertRaises(Exception):  # noqa: B017 - callers treat any raise as unloadable
            load_surface(b"not a jpeg at all")


class TestSkipped(StoreTestCase):
    def write(self, text: str) -> None:
        (self.work_dir / "skipped.tsv").write_text(text)

    def test_absent_is_none(self):
        self.assertIsNone(self.store.skipped())

    def test_header_only_is_zero(self):
        self.write("image_id\tkind\treason\n")
        self.assertEqual(self.store.skipped(), SkippedCounts(0, 0))
        self.assertFalse(self.store.skipped().any)

    def test_counts_kinds(self):
        self.write("image_id\tkind\treason\na\tfailed\tx\nb\tignored\ty\nc\tfailed\t\"multi\nline\"\n")
        self.assertEqual(self.store.skipped(), SkippedCounts(failed=2, ignored=1))

    def test_malformed_names_file_and_line(self):
        cases = {
            "bad header": ("id\tkind\treason\n", 1),
            "empty file": ("", 1),
            "bad kind": ("image_id\tkind\treason\na\tfailed\tx\nb\tbroken\ty\n", 3),
            "short row": ("image_id\tkind\treason\na\tfailed\n", 2),
        }
        for name, (text, line) in cases.items():
            with self.subTest(name=name):
                self.write(text)
                with self.assertRaisesRegex(ValueError, rf"skipped\.tsv:{line}:"):
                    self.store.skipped()


class TestMarkAndStatuses(StoreTestCase):
    def test_initially_unreviewed(self):
        self.assertEqual(self.store.current_pass(), 1)
        self.assertEqual(set(self.store.statuses(1).values()), {"UNREVIEWED"})

    def test_mark_round_trip_stores_image_id(self):
        self.store.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_002/c.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        statuses = LocalStore(self.work_dir, read_only=True).statuses(1)
        self.assertEqual(statuses["batch_001/a.jpg"], "CLEAN")
        self.assertEqual(statuses["batch_001/b.jpg"], "CLEAN")
        self.assertEqual(statuses["batch_002/c.jpg"], "DIRTY")
        self.assertEqual(statuses["batch_002/d.jpg"], "UNREVIEWED")
        with open(self.work_dir / "review.tsv", newline="") as f:
            stored = {r["image_id"] for r in csv.DictReader(f, delimiter="\t")}
        self.assertEqual(
            stored,
            {"/src/patient_smith/a.dcm", "/src/patient_jones/b.dcm", "/src/patient_lee/c.dcm"},
        )

    def test_prior_pass_semantics(self):
        self.store.mark(["batch_001/a.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_001/b.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        statuses = self.store.statuses(2)
        self.assertEqual(statuses["batch_001/a.jpg"], "CLEAN")
        self.assertEqual(statuses["batch_001/b.jpg"], "FLAGGED")

    def test_pass_one_dirty_is_flagged_in_pass_two(self):
        self.store.mark(["batch_001/a.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_001/b.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(self.store.current_pass(), 2)
        self.assertEqual(
            self.store.statuses(2),
            {"batch_001/a.jpg": "FLAGGED", "batch_001/b.jpg": "CLEAN", "batch_002/c.jpg": "CLEAN", "batch_002/d.jpg": "CLEAN"},
        )
        self.assertEqual(self.store.mark(["batch_001/a.jpg"], "CLEAN", 2, reviewer="tester", mode="single"), {"batch_001/a.jpg": "CLEAN"})
        self.assertEqual(self.store.current_pass(), 3)

    def test_flagged_keeps_pass_open(self):
        self.store.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_001/a.jpg"], "CLEAN", 2, reviewer="tester", mode="single")
        self.assertEqual(self.store.statuses(2)["batch_001/b.jpg"], "FLAGGED")
        self.assertEqual(self.store.current_pass(), 2)


class TestSharedImageId(unittest.TestCase):
    def test_mark_returns_all_keys_sharing_image_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_work_dir(root)
            with open(root / "manifest.tsv", "w", newline="") as f:
                writer = csv.writer(f, delimiter="\t")
                writer.writerow(["batch", "preprocessed_path", "image_id"])
                writer.writerow(["batch_001", "batch_001/a.jpg", "/src/same.dcm"])
                writer.writerow(["batch_001", "batch_001/b.jpg", "/src/same.dcm"])
                writer.writerow(["batch_002", "batch_002/c.jpg", "/src/other.dcm"])
            store = LocalStore(root)
            self.addCleanup(store.close)
            changed = store.mark(["batch_001/a.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
            self.assertEqual(changed, {"batch_001/a.jpg": "CLEAN", "batch_001/b.jpg": "CLEAN"})
            statuses = store.statuses(1)
            self.assertEqual({k: statuses[k] for k in changed}, changed)
            self.assertEqual(statuses["batch_002/c.jpg"], "UNREVIEWED")
            self.assertEqual(store.undo(1, reviewer="tester"), {"batch_001/a.jpg": "UNREVIEWED", "batch_001/b.jpg": "UNREVIEWED"})
            store.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "DIRTY", 1, reviewer="tester", mode="grid")
            size = (root / "review.tsv").stat().st_size
            store.undo(1, reviewer="tester")
            with open(root / "review.tsv", "rb") as f:
                f.seek(size)
                self.assertEqual(f.read().count(b"\n"), 1)  # one undo row for the shared image_id


class TestMissingManifest(unittest.TestCase):
    def test_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(FileNotFoundError):
            LocalStore(Path(tmp))


class TestStrictLoading(unittest.TestCase):
    GOOD_TS = "2026-01-01T00:00:00+00:00"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        make_work_dir(self.work_dir)

    def write_review(self, *lines: str) -> None:
        (self.work_dir / "review.tsv").write_text("\n".join(lines) + "\n")

    def write_manifest(self, *lines: str) -> None:
        (self.work_dir / "manifest.tsv").write_text("\n".join(lines) + "\n")

    REVIEW_HEADER = "image_id\tbatch\tstatus\tpass_number\ttimestamp"
    MANIFEST_HEADER = "batch\tpreprocessed_path\timage_id"

    def test_review_problems_name_file_and_line(self):
        ts = self.GOOD_TS
        good = f"/src/a.dcm\tbatch_001\tCLEAN\t1\t{ts}"
        cases = {
            "short row": (self.REVIEW_HEADER, good, "/src/b.dcm\tbatch_001\tCLEAN"),
            "bad status": (self.REVIEW_HEADER, good, f"/src/b.dcm\tbatch_001\tdirty\t1\t{ts}"),
            "bad pass": (self.REVIEW_HEADER, good, f"/src/b.dcm\tbatch_001\tCLEAN\tone\t{ts}"),
            "zero pass": (self.REVIEW_HEADER, good, f"/src/b.dcm\tbatch_001\tCLEAN\t0\t{ts}"),
            "empty image_id": (self.REVIEW_HEADER, good, f"\tbatch_001\tCLEAN\t1\t{ts}"),
        }
        for name, lines in cases.items():
            with self.subTest(name):
                self.write_review(*lines)
                with self.assertRaisesRegex(ValueError, r"review\.tsv:3: "):
                    LocalStore(self.work_dir)

    def test_review_header_problems(self):
        for header in ("image_id\tbatch\tstatus\tpass_number", "image_id\tbatch\tstatus\tpass_number\ttimestamp\textra"):
            with self.subTest(header=header):
                self.write_review(header)
                with self.assertRaisesRegex(ValueError, r"review\.tsv:1: header"):
                    LocalStore(self.work_dir)

    NEW_HEADER = "\t".join(HEADER)

    def test_audit_column_problems_name_file_and_line(self):
        ts = self.GOOD_TS
        good = f"/src/a.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tsingle\t1\t0.2.0"
        cases = {
            "old-width row under new header": (self.NEW_HEADER, good, f"/src/b.dcm\tbatch_001\tCLEAN\t1\t{ts}"),
            "bad mode": (self.NEW_HEADER, good, f"/src/b.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tbogus\t1\t0.2.0"),
            "FLAGGED status": (self.NEW_HEADER, good, f"/src/b.dcm\tbatch_001\tFLAGGED\t1\t{ts}\talice\tsingle\t1\t0.2.0"),
            "FLAGGED in an undo row": (self.NEW_HEADER, good, f"/src/b.dcm\tbatch_001\tFLAGGED\t1\t{ts}\talice\tundo\t1\t0.2.0"),
            "UNREVIEWED outside an undo row": (self.NEW_HEADER, good, f"/src/b.dcm\tbatch_001\tUNREVIEWED\t1\t{ts}\talice\tsingle\t1\t0.2.0"),
            "UNREVIEWED in a migrated row": (self.NEW_HEADER, good, f"/src/b.dcm\tbatch_001\tUNREVIEWED\t1\t{ts}\t\t\t\t"),
            "bad grid_size": (self.NEW_HEADER, good, f"/src/b.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tgrid\tfour\t0.2.0"),
            "zero grid_size": (self.NEW_HEADER, good, f"/src/b.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tgrid\t0\t0.2.0"),
            "new-width row under old header": (self.REVIEW_HEADER, f"/src/a.dcm\tbatch_001\tCLEAN\t1\t{ts}", good),
        }
        for name, lines in cases.items():
            with self.subTest(name):
                self.write_review(*lines)
                with self.assertRaisesRegex(ValueError, r"review\.tsv:3: "):
                    LocalStore(self.work_dir, read_only=True)

    def test_undo_rows_load_and_tombstone_folds(self):
        ts = self.GOOD_TS
        self.write_review(
            self.NEW_HEADER,
            f"/src/patient_smith/a.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tsingle\t1\t0.2.0",
            f"/src/patient_jones/b.dcm\tbatch_001\tDIRTY\t2\t{ts}\talice\tsingle\t1\t0.2.0",
            f"/src/patient_jones/b.dcm\tbatch_001\tCLEAN\t3\t{ts}\talice\tsingle\t1\t0.2.0",
            f"/src/patient_smith/a.dcm\tbatch_001\tUNREVIEWED\t1\t{ts}\talice\tundo\t2\t0.2.0",
            f"/src/patient_jones/b.dcm\tbatch_001\tDIRTY\t2\t{ts}\talice\tundo\t2\t0.2.0",
        )
        statuses = LocalStore(self.work_dir, read_only=True).statuses(2)
        self.assertEqual((statuses["batch_001/a.jpg"], statuses["batch_001/b.jpg"]), ("UNREVIEWED", "DIRTY"))

    def test_migrated_rows_with_empty_audit_columns_load(self):
        self.write_review(self.NEW_HEADER, f"/src/patient_smith/a.dcm\tbatch_001\tCLEAN\t1\t{self.GOOD_TS}\t\t\t\t")
        self.assertEqual(LocalStore(self.work_dir, read_only=True).statuses(1)["batch_001/a.jpg"], "CLEAN")

    def test_valid_review_loads(self):
        self.write_review(self.REVIEW_HEADER, f"/src/patient_smith/a.dcm\tbatch_001\tCLEAN\t1\t{self.GOOD_TS}")
        self.assertEqual(LocalStore(self.work_dir, read_only=True).statuses(1)["batch_001/a.jpg"], "CLEAN")

    def test_manifest_problems_name_file_and_line(self):
        good = "batch_001\tbatch_001/a.jpg\t/src/a.dcm"
        cases = {
            "missing column": (self.MANIFEST_HEADER, good, "batch_001\tbatch_001/b.jpg"),
            "duplicate key": (self.MANIFEST_HEADER, good, "batch_001\tbatch_001/a.jpg\t/src/b.dcm"),
            "empty image_id": (self.MANIFEST_HEADER, good, "batch_001\tbatch_001/b.jpg\t"),
        }
        for name, lines in cases.items():
            with self.subTest(name):
                self.write_manifest(*lines)
                with self.assertRaisesRegex(ValueError, r"manifest\.tsv:3: "):
                    LocalStore(self.work_dir)

    def test_duplicate_key_names_both_lines(self):
        self.write_manifest(self.MANIFEST_HEADER, "b\tk.jpg\t/src/a.dcm", "b\tk.jpg\t/src/b.dcm")
        with self.assertRaisesRegex(ValueError, r"manifest\.tsv:3: .*line 2"):
            LocalStore(self.work_dir)

    def test_manifest_header_missing_column(self):
        self.write_manifest("batch\tpreprocessed_path")
        with self.assertRaisesRegex(ValueError, r"manifest\.tsv:1: header"):
            LocalStore(self.work_dir)


class TestReviewLog(unittest.TestCase):
    """review.tsv is an append-only log: one header, then one line per decision, last line per image_id wins."""

    HEADER_LINE = b"image_id\tbatch\tstatus\tpass_number\ttimestamp\treviewer\tmode\tgrid_size\ttool_version\r\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        os.chmod(self.work_dir, 0o700)
        self.path = self.work_dir / "review.tsv"

    def reload(self) -> ReviewDB:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            db = ReviewDB(self.work_dir)
        self.assertEqual(stderr.getvalue(), "")
        return db

    def test_marks_append_lines_and_reload(self):
        db = ReviewDB(self.work_dir)
        db.mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        db.mark_many([("y", "batch_002"), ("z", "batch_002")], "DIRTY", 2, reviewer="tester", mode="single")
        lines = self.path.read_bytes().splitlines(keepends=True)
        self.assertEqual(lines[0], self.HEADER_LINE)
        self.assertEqual(len(lines), 4)
        self.assertEqual(self.reload()._rows, db._rows)

    def test_same_image_twice_last_wins(self):
        db = ReviewDB(self.work_dir)
        db.mark("x", "batch_001", "DIRTY", 1, reviewer="tester", mode="single")
        db.mark("x", "batch_001", "CLEAN", 2, reviewer="tester", mode="single")
        self.assertEqual(len(self.path.read_bytes().splitlines()), 3)
        reloaded = self.reload()
        self.assertEqual((reloaded._rows["x"].status, reloaded._rows["x"].pass_number), ("CLEAN", 2))
        self.assertEqual(reloaded._rows, db._rows)

    def test_mark_is_a_pure_append(self):
        ReviewDB(self.work_dir).mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        before = self.path.read_bytes()
        inode = self.path.stat().st_ino
        ReviewDB(self.work_dir).mark("y", "batch_001", "DIRTY", 1, reviewer="tester", mode="single")
        after = self.path.read_bytes()
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertTrue(after.startswith(before))
        self.assertGreater(len(after), len(before))

    def test_torn_last_line_is_dropped_and_next_mark_starts_clean(self):
        ReviewDB(self.work_dir).mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        intact = self.path.read_bytes()
        with open(self.path, "ab") as f:
            f.write(b"y\tbatch_0")  # a crash mid-append
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            db = ReviewDB(self.work_dir)
        self.assertIn("WARNING: ignoring the unfinished last line", stderr.getvalue())
        self.assertIn("review.tsv:3:", stderr.getvalue())
        self.assertEqual(set(db._rows), {"x"})
        db.mark("z", "batch_001", "DIRTY", 1, reviewer="tester", mode="single")
        after = self.path.read_bytes()
        self.assertTrue(after.startswith(intact))
        self.assertTrue(after[len(intact):].startswith(b"z\tbatch_001\tDIRTY\t1\t"))
        self.assertEqual(len(after.splitlines()), 3)  # header, x, z: the fragment is gone
        self.assertEqual(set(self.reload()._rows), {"x", "z"})

    def test_torn_last_line_that_parses_is_kept(self):
        ReviewDB(self.work_dir).mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        with open(self.path, "ab") as f:
            f.write(b"y\tbatch_001\tDIRTY\t1\t2026-01-01T00:00:00+00:00\tr\tsingle\t1\t0.1")  # every field written, line ending not
        db = self.reload()
        self.assertEqual(set(db._rows), {"x", "y"})
        db.mark("z", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        self.assertIn(b"\t0.1\r\nz\t", self.path.read_bytes())  # its missing line ending, in the file's style
        self.assertEqual(set(self.reload()._rows), {"x", "y", "z"})

    def test_torn_between_cr_and_lf_is_kept(self):
        ReviewDB(self.work_dir).mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        with open(self.path, "ab") as f:
            f.write(b"y\tbatch_001\tDIRTY\t1\t2026-01-01T00:00:00+00:00\tr\tsingle\t1\t0.1\r")
        db = self.reload()
        self.assertEqual(set(db._rows), {"x", "y"})
        db.mark("z", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        data = self.path.read_bytes()
        self.assertIn(b"\t0.1\r\nz\t", data)
        self.assertNotIn(b"\r\r", data)
        self.assertNotIn(b"\n\r\n", data)  # no blank line
        self.assertEqual(data.count(b"\r"), data.count(b"\r\n"))
        self.assertEqual(set(self.reload()._rows), {"x", "y", "z"})

    def test_torn_multibyte_character_is_dropped(self):
        ReviewDB(self.work_dir).mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        with open(self.path, "ab") as f:
            f.write("é\tb".encode()[:2])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            db = ReviewDB(self.work_dir)
        self.assertIn("WARNING: ignoring the unfinished last line", stderr.getvalue())
        db.mark("z", "batch_001", "DIRTY", 1, reviewer="tester", mode="single")
        self.assertEqual(set(self.reload()._rows), {"x", "z"})

    def test_failed_append_is_rolled_back(self):
        db = ReviewDB(self.work_dir)
        db.mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        real_write = os.write
        calls = []

        def short_then_full(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                return real_write(fd, data[:10])  # a short write...
            raise OSError(errno.ENOSPC, "No space left on device")  # ...then the disk fills

        with mock.patch.object(review_db_module.os, "write", short_then_full), self.assertRaises(OSError):
            db.mark_many([("y", "batch_001"), ("z", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(db._rows), {"x"})
        db.mark("w", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(self.reload()._rows, db._rows)

    def test_failed_append_left_on_disk_is_cut_by_the_next(self):
        db = ReviewDB(self.work_dir)
        db.mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        real_write = os.write

        def short_then_full(fd, data):
            if short_then_full.done:
                raise OSError(errno.ENOSPC, "No space left on device")
            short_then_full.done = True
            return real_write(fd, data[:10])

        short_then_full.done = False
        with mock.patch.object(review_db_module.os, "write", short_then_full), \
                mock.patch.object(review_db_module.os, "ftruncate", side_effect=OSError(errno.EIO, "I/O error")), \
                self.assertRaises(OSError):
            db.mark_many([("y", "batch_001"), ("z", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")  # the rollback fails too: the fragment stays
        db.mark("w", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(self.reload()._rows, db._rows)

    def test_stale_truncate_refuses(self):
        ReviewDB(self.work_dir).mark_many([("x", "batch_001"), ("y", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        with open(self.path, "ab") as f:
            f.write(b"z\tbatch_0")
        with contextlib.redirect_stderr(io.StringIO()):
            db = ReviewDB(self.work_dir)
        with open(self.path, "ab") as f:
            f.write(b"01\tCLEAN\t1\tt\r\n")  # someone else finished the line since the load
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "changed since it was loaded"):
            db.mark("w", "batch_001", "DIRTY", 1, reviewer="tester", mode="single")
        self.assertEqual(self.path.read_bytes(), before)

    def test_bare_cr_file_with_bad_middle_line_raises(self):
        ts = "2026-01-01T00:00:00+00:00"
        lines = ["image_id\tbatch\tstatus\tpass_number\ttimestamp", f"a\tb\tCLEAN\t1\t{ts}", f"bad\tb\tdirty\t1\t{ts}", f"c\tb\tDIRTY\t2\t{ts}"]
        for name, data in [("terminated", "\r".join(lines) + "\r"), ("unterminated", "\r".join(lines))]:
            with self.subTest(name):
                self.path.write_bytes(data.encode())
                with self.assertRaisesRegex(ValueError, r"review\.tsv:3: "):
                    ReviewDB(self.work_dir)
                self.assertEqual(self.path.read_bytes(), data.encode())

    def test_torn_header_is_dropped(self):
        self.path.write_bytes(b"image_id\tba")
        with contextlib.redirect_stderr(io.StringIO()):
            db = ReviewDB(self.work_dir)
        db.mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        self.assertTrue(self.path.read_bytes().startswith(self.HEADER_LINE))
        self.assertEqual(set(self.reload()._rows), {"x"})

    def test_empty_file_loads_and_gets_a_header(self):
        self.path.touch()
        db = self.reload()
        db.mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        self.assertTrue(self.path.read_bytes().startswith(self.HEADER_LINE))

    def test_unparseable_line_before_the_last_still_raises(self):
        ReviewDB(self.work_dir).mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        with open(self.path, "ab") as f:
            f.write(b"garbage\r\ny\tbatch_0")
        with self.assertRaisesRegex(ValueError, r"review\.tsv:3: "):
            ReviewDB(self.work_dir)

    def test_unparseable_terminated_last_line_still_raises(self):
        ReviewDB(self.work_dir).mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        with open(self.path, "ab") as f:
            f.write(b"y\tbatch_0\r\n")
        with self.assertRaisesRegex(ValueError, r"review\.tsv:3: "):
            ReviewDB(self.work_dir)

    def test_file_mode_follows_policy(self):
        for dir_mode, file_mode in [(0o700, 0o600), (0o2770, 0o660)]:
            with self.subTest(dir_mode=oct(dir_mode)):
                self.path.unlink(missing_ok=True)
                os.chmod(self.work_dir, dir_mode)
                old_umask = os.umask(0o077)  # O_CREAT's mode alone would lose the group bits
                try:
                    ReviewDB(self.work_dir).mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
                finally:
                    os.umask(old_umask)
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), file_mode)


def review_rows(path: Path) -> list[dict[str, str]]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


class TestAuditColumns(StoreTestCase):
    def test_local_mark_writes_audit_columns(self):
        self.store.mark(["batch_001/a.jpg"], "CLEAN", 1, reviewer="alice", mode="single")
        self.store.mark(["batch_001/b.jpg", "batch_002/c.jpg", "batch_002/d.jpg"], "DIRTY", 1, reviewer="Bob Q", mode="grid")
        rows = review_rows(self.work_dir / "review.tsv")
        audit = [(r["reviewer"], r["mode"], r["grid_size"], r["tool_version"]) for r in rows]
        version = package_version()
        self.assertEqual(audit, [("alice", "single", "1", version)] + [("Bob Q", "grid", "3", version)] * 3)

    def test_each_key_batch_comes_from_the_manifest(self):
        self.store.mark(["batch_001/a.jpg", "batch_002/c.jpg"], "CLEAN", 1, reviewer="alice", mode="grid")
        batches = {r["image_id"]: r["batch"] for r in review_rows(self.work_dir / "review.tsv")}
        self.assertEqual(batches, {"/src/patient_smith/a.dcm": "batch_001", "/src/patient_lee/c.dcm": "batch_002"})

    def test_reviewer_with_quotes_round_trips(self):
        reviewer = 'Dr. "Q" O\'Neil'
        self.store.mark(["batch_001/a.jpg"], "CLEAN", 1, reviewer=reviewer, mode="single")
        with contextlib.redirect_stderr(io.StringIO()):
            db = ReviewDB(self.work_dir)
        self.assertEqual(db._rows["/src/patient_smith/a.dcm"].reviewer, reviewer)

    def test_bad_reviewer_is_refused_before_writing(self):
        for reviewer in ("a\tb", "a\nb", "", "  "):
            with self.subTest(reviewer=reviewer), self.assertRaises(ValueError):
                self.store.mark(["batch_001/a.jpg"], "CLEAN", 1, reviewer=reviewer, mode="single")
        self.assertFalse((self.work_dir / "review.tsv").exists())

    def test_bad_mode_is_refused_before_writing(self):
        with self.assertRaises(ValueError):
            self.store.mark(["batch_001/a.jpg"], "CLEAN", 1, reviewer="alice", mode="undo")  # type: ignore[arg-type]
        self.assertFalse((self.work_dir / "review.tsv").exists())


class TestMigration(unittest.TestCase):
    OLD_HEADER = "image_id\tbatch\tstatus\tpass_number\ttimestamp"
    OLD_ROWS = (
        "/src/patient_smith/a.dcm\tbatch_001\tDIRTY\t1\t2026-01-01T00:00:00+00:00",
        "/src/patient_jones/b.dcm\tbatch_001\tCLEAN\t1\t2026-01-01T00:00:01+00:00",
        "/src/patient_smith/a.dcm\tbatch_001\tCLEAN\t2\t2026-01-02T00:00:00+00:00",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        make_work_dir(self.work_dir)
        os.chmod(self.work_dir, 0o2770)  # group policy: the migrated file must be 0660, not mkstemp's 0600
        self.path = self.work_dir / "review.tsv"
        self.old = ("\r\n".join([self.OLD_HEADER, *self.OLD_ROWS]) + "\r\n").encode()
        self.path.write_bytes(self.old)

    def expected_rows(self) -> list[list[str]]:
        return [line.split("\t") + ["", "", "", ""] for line in self.OLD_ROWS]

    def stored_rows(self) -> tuple[list[str], list[list[str]]]:
        with open(self.path, newline="") as f:
            header, *rows = csv.reader(f, delimiter="\t")
        return header, rows

    def test_writable_store_migrates_once(self):
        inode = self.path.stat().st_ino
        with LocalStore(self.work_dir) as store:
            self.assertEqual(store.statuses(2)["batch_001/a.jpg"], "CLEAN")
        self.assertNotEqual(self.path.stat().st_ino, inode)
        header, rows = self.stored_rows()
        self.assertEqual(header, HEADER)
        self.assertEqual(rows, self.expected_rows())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o660)
        self.assertEqual([p.name for p in self.work_dir.iterdir() if "review.tsv" in p.name], ["review.tsv"])  # no temp left
        migrated, inode = self.path.read_bytes(), self.path.stat().st_ino
        with LocalStore(self.work_dir) as store:
            pass
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_ino), (migrated, inode))

    def test_appends_after_migration_use_new_columns(self):
        with LocalStore(self.work_dir) as store:
            store.mark(["batch_002/c.jpg"], "DIRTY", 1, reviewer="alice", mode="single")
        _, rows = self.stored_rows()
        self.assertEqual(rows[:3], self.expected_rows())
        self.assertEqual(rows[3][:3] + rows[3][5:8], ["/src/patient_lee/c.dcm", "batch_002", "DIRTY", "alice", "single", "1"])

    def test_read_only_store_leaves_old_file_alone(self):
        inode = self.path.stat().st_ino
        store = LocalStore(self.work_dir, read_only=True)
        self.assertEqual(store.statuses(1)["batch_001/b.jpg"], "CLEAN")
        self.assertEqual(store.current_pass(), 1)  # c and d unreviewed
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_ino), (self.old, inode))

    def test_torn_tail_of_old_file_is_dropped_by_migration(self):
        with open(self.path, "ab") as f:
            f.write(b"/src/patient_lee/c.dcm\tbatch_0")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), LocalStore(self.work_dir) as store:
            store.mark(["batch_002/d.jpg"], "CLEAN", 1, reviewer="alice", mode="single")
        self.assertIn("unfinished last line", stderr.getvalue())
        _, rows = self.stored_rows()
        self.assertEqual(rows[:3], self.expected_rows())
        self.assertEqual([r[0] for r in rows[3:]], ["/src/patient_kim/d.dcm"])

    def test_unmigrated_db_refuses_to_append(self):
        db = ReviewDB(self.work_dir)
        with self.assertRaisesRegex(RuntimeError, "old 5-column header"):
            db.mark("x", "batch_001", "CLEAN", 1, reviewer="alice", mode="single")
        self.assertEqual(self.path.read_bytes(), self.old)

    def test_failed_migration_leaves_old_file_and_releases_lock(self):
        def fail_fsync(fd):
            raise OSError(errno.EIO, "EIO")

        def interrupt_replace(src, dst):
            raise KeyboardInterrupt

        for name, attr, fake, exc in [("fsync", "fsync", fail_fsync, OSError), ("replace", "replace", interrupt_replace, KeyboardInterrupt)]:
            with self.subTest(name):
                inode = self.path.stat().st_ino
                fake_os = types.ModuleType("os")  # review_db's own os only: the lock file's fsync stays real
                fake_os.__dict__.update(os.__dict__)
                setattr(fake_os, attr, fake)
                with mock.patch.object(review_db_module, "os", fake_os), self.assertRaises(exc):
                    LocalStore(self.work_dir)
                self.assertEqual((self.path.read_bytes(), self.path.stat().st_ino), (self.old, inode))
                self.assertEqual(list(self.work_dir.glob(".review.tsv.*.tmp")), [])
                self.assertFalse((self.work_dir / LOCK_NAME).exists())

    def test_file_changed_since_load_is_not_migrated(self):
        db = ReviewDB(self.work_dir)
        with open(self.path, "ab") as f:
            f.write(self.OLD_ROWS[0].encode() + b"\r\n")
        changed = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "changed since it was loaded"):
            db.migrate()
        self.assertEqual(self.path.read_bytes(), changed)


class TestReadOnlyTornLog(StoreTestCase):
    def test_read_only_store_never_writes(self):
        self.store.mark(["batch_001/a.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        self.store.close()
        path = self.work_dir / "review.tsv"
        with open(path, "ab") as f:
            f.write(b"/src/patient_jones/b.dcm\tbatch_0")
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ro = LocalStore(self.work_dir, read_only=True)
        self.assertIn("WARNING: ignoring the unfinished last line", stderr.getvalue())
        self.assertEqual(ro.statuses(1)["batch_001/a.jpg"], "CLEAN")
        with self.assertRaises(PermissionError):
            ro.mark(["batch_001/b.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)


class TestPassMonotonic(StoreTestCase):
    def setUp(self):
        super().setUp()
        pg.init()
        self.addCleanup(pg.quit)
        patcher = mock.patch.object(pg.display, "toggle_fullscreen", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def append_manifest_row(self):
        with open(self.work_dir / "manifest.tsv", "a", newline="") as f:
            csv.writer(f, delimiter="\t").writerow(["batch_002", "batch_002/e.jpg", "/src/patient_new/e.dcm"])
        self.store.close()
        self.store = LocalStore(self.work_dir)
        self.addCleanup(self.store.close)

    def mark_all(self, status, pass_number, keys):
        for key in keys:
            self.store.mark([key], status, pass_number, reviewer="tester", mode="single")

    def test_current_pass_transitions(self):
        keys = [r[1] for r in ROWS]
        self.mark_all("DIRTY", 1, keys[:1])
        self.mark_all("CLEAN", 1, keys[1:])
        self.assertEqual(self.store.current_pass(), 2)
        self.mark_all("DIRTY", 1, keys[1:2])  # b: another DIRTY, so pass 2 has two FLAGGED
        self.mark_all("DIRTY", 2, keys[:1])  # a re-reviewed in pass 2; b still FLAGGED
        self.assertEqual(self.store.current_pass(), 2)
        self.mark_all("CLEAN", 2, keys[1:2])
        self.assertEqual(self.store.current_pass(), 3)
        self.append_manifest_row()
        self.assertEqual(self.store.current_pass(), 1)

    def test_pass_one_view_after_append_shows_later_verdicts(self):
        self.mark_all("DIRTY", 3, ["batch_001/a.jpg"])
        self.mark_all("CLEAN", 3, ["batch_001/b.jpg"])
        self.append_manifest_row()
        self.assertEqual(self.store.current_pass(), 1)
        statuses = self.store.statuses(1)
        self.assertEqual(statuses["batch_001/a.jpg"], "DIRTY")
        self.assertEqual(statuses["batch_001/b.jpg"], "CLEAN")
        self.assertEqual(statuses["batch_002/e.jpg"], "UNREVIEWED")

    def test_lower_pass_mark_keeps_recorded_pass(self):
        self.store.mark(["batch_001/a.jpg"], "DIRTY", 3, reviewer="tester", mode="single")
        result = self.store.mark(["batch_001/a.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(result, {"batch_001/a.jpg": "CLEAN"})
        with open(self.work_dir / "review.tsv", newline="") as f:
            *_, row = csv.DictReader(f, delimiter="\t")  # the log's last row for the image wins
        self.assertEqual((row["status"], row["pass_number"]), ("CLEAN", "3"))

    def test_pass_one_grid_excludes_later_pass_dirty(self):
        self.mark_all("DIRTY", 3, ["batch_001/a.jpg"])
        self.append_manifest_row()
        s = ReviewSession(self.store, reviewer="tester", mode="grid", status_filter="all")
        self.assertEqual(s.pass_number, 1)
        keys = {k for item in s._items for k in item.keys}
        self.assertNotIn("batch_001/a.jpg", keys)
        self.assertIn("batch_001/b.jpg", keys)


class TestUndo(StoreTestCase):
    A, B, C = ROWS[0][1], ROWS[1][1], ROWS[2][1]

    def last_row(self) -> dict[str, str]:
        return review_rows(self.work_dir / "review.tsv")[-1]

    def test_undo_of_first_mark_is_a_tombstone(self):
        self.store.mark([self.A], "CLEAN", 1, reviewer="alice", mode="single")
        self.assertEqual(self.store.undo(1, reviewer="bob"), {self.A: "UNREVIEWED"})
        self.assertEqual(self.store.statuses(1)[self.A], "UNREVIEWED")
        row = self.last_row()
        self.assertEqual(
            {k: row[k] for k in ("image_id", "batch", "status", "pass_number", "reviewer", "mode", "grid_size", "tool_version")},
            {
                "image_id": ROWS[0][2], "batch": "batch_001", "status": "UNREVIEWED", "pass_number": "1",
                "reviewer": "bob", "mode": "undo", "grid_size": "1", "tool_version": package_version(),
            },
        )
        reloaded = LocalStore(self.work_dir, read_only=True)
        self.assertEqual(reloaded.statuses(1), self.store.statuses(1))
        self.assertEqual(reloaded.current_pass(), 1)

    def test_tombstone_makes_image_unseen_for_current_pass(self):
        self.store.mark([self.A, self.B, self.C], "CLEAN", 1, reviewer="alice", mode="grid")
        self.store.mark([ROWS[3][1]], "CLEAN", 1, reviewer="alice", mode="single")
        self.assertEqual(self.store.current_pass(), 2)
        self.store.undo(2, reviewer="alice")
        self.assertEqual(self.store.current_pass(), 1)
        self.assertEqual(LocalStore(self.work_dir, read_only=True).current_pass(), 1)

    def test_undo_restores_previous_verdict_and_exact_pass(self):
        self.store.mark([self.A], "CLEAN", 2, reviewer="alice", mode="single")
        self.store.mark([self.A], "DIRTY", 3, reviewer="alice", mode="single")
        self.assertEqual(self.store.undo(3, reviewer="alice"), {self.A: "CLEAN"})
        row = self.last_row()
        self.assertEqual((row["status"], row["pass_number"], row["mode"]), ("CLEAN", "2", "undo"))
        with contextlib.redirect_stderr(io.StringIO()):
            db = ReviewDB(self.work_dir)
        self.assertEqual((db._rows[ROWS[0][2]].status, db._rows[ROWS[0][2]].pass_number), ("CLEAN", 2))

    def test_lower_pass_restore_keeps_original_pass(self):
        self.store.mark([self.A], "DIRTY", 1, reviewer="alice", mode="single")
        self.store.mark([self.A], "CLEAN", 3, reviewer="alice", mode="single")
        self.assertEqual(self.store.undo(3, reviewer="alice"), {self.A: "FLAGGED"})  # DIRTY in pass 1, seen from pass 3
        self.assertEqual(self.last_row()["pass_number"], "1")  # a restore may lower the recorded pass
        self.assertEqual(self.store.statuses(1)[self.A], "DIRTY")

    def test_undo_restores_the_never_decreased_pass(self):
        self.store.mark([self.A], "DIRTY", 3, reviewer="alice", mode="single")
        self.store.mark([self.A], "CLEAN", 1, reviewer="alice", mode="single")  # recorded as pass 3
        self.store.undo(1, reviewer="alice")
        row = self.last_row()
        self.assertEqual((row["status"], row["pass_number"]), ("DIRTY", "3"))

    def test_five_key_grid_is_undone_at_once(self):
        with open(self.work_dir / "manifest.tsv", "a", newline="") as f:
            csv.writer(f, delimiter="\t").writerow(["batch_002", "batch_002/e.jpg", "/src/patient_new/e.dcm"])
        self.store.close()
        self.store = LocalStore(self.work_dir)
        self.addCleanup(self.store.close)
        keys = [r.key for r in self.store.manifest()]
        self.assertEqual(len(keys), 5)
        self.store.mark(keys[:1], "DIRTY", 1, reviewer="alice", mode="single")
        self.store.mark(keys, "CLEAN", 1, reviewer="alice", mode="grid")
        changed = self.store.undo(1, reviewer="alice")
        self.assertEqual(changed, {keys[0]: "DIRTY", **dict.fromkeys(keys[1:], "UNREVIEWED")})
        undo_rows = review_rows(self.work_dir / "review.tsv")[-5:]
        self.assertEqual({(r["mode"], r["grid_size"]) for r in undo_rows}, {("undo", "5")})
        self.assertEqual(self.store.undo(1, reviewer="alice"), {keys[0]: "UNREVIEWED"})
        self.assertEqual(set(self.store.statuses(1).values()), {"UNREVIEWED"})

    def test_empty_stack_is_empty_dict_and_writes_nothing(self):
        self.assertEqual(self.store.undo(1, reviewer="alice"), {})
        self.store.mark([self.A], "CLEAN", 1, reviewer="alice", mode="single")
        self.store.undo(1, reviewer="alice")
        size = (self.work_dir / "review.tsv").stat().st_size
        self.assertEqual(self.store.undo(1, reviewer="alice"), {})
        self.assertEqual((self.work_dir / "review.tsv").stat().st_size, size)

    def test_stack_is_lost_when_the_store_is_reopened(self):
        self.store.mark([self.A], "CLEAN", 1, reviewer="alice", mode="single")
        self.store.close()
        self.store = LocalStore(self.work_dir)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.undo(1, reviewer="alice"), {})
        self.assertEqual(self.store.statuses(1)[self.A], "CLEAN")

    def test_failed_undo_keeps_the_entry(self):
        self.store.mark([self.A], "CLEAN", 1, reviewer="alice", mode="single")
        with mock.patch.object(ReviewDB, "_append", side_effect=OSError(errno.ENOSPC, "full")), self.assertRaises(OSError):
            self.store.undo(1, reviewer="alice")
        with self.assertRaises(ValueError):
            self.store.undo(1, reviewer="a\tb")
        self.assertEqual(self.store.statuses(1)[self.A], "CLEAN")
        self.assertEqual(self.store.undo(1, reviewer="alice"), {self.A: "UNREVIEWED"})

    def test_failed_mark_is_not_pushed(self):
        self.store.mark([self.A], "CLEAN", 1, reviewer="alice", mode="single")
        with mock.patch.object(ReviewDB, "_append", side_effect=OSError(errno.ENOSPC, "full")), self.assertRaises(OSError):
            self.store.mark([self.B], "DIRTY", 1, reviewer="alice", mode="single")
        self.assertEqual(self.store.undo(1, reviewer="alice"), {self.A: "UNREVIEWED"})
        self.assertEqual(self.store.undo(1, reviewer="alice"), {})

    def test_mark_many_still_refuses_unreviewed(self):
        with self.assertRaises(ValueError):
            self.store.mark([self.A], "UNREVIEWED", 1, reviewer="alice", mode="single")  # type: ignore[arg-type]
        self.assertFalse((self.work_dir / "review.tsv").exists())


class SessionTestCase(StoreTestCase):
    def setUp(self):
        super().setUp()
        pg.init()
        self.addCleanup(pg.quit)
        patcher = mock.patch.object(pg.display, "toggle_fullscreen", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestSession(SessionTestCase):
    def test_single_mode_mark_updates_snapshot(self):
        s = ReviewSession(self.store, reviewer="tester", mode="single")
        s._cursor = 0
        key = s._items[0].keys[0]
        before = s._todo_count
        s._mark("CLEAN")
        self.assertEqual(s._statuses[key], "CLEAN")
        self.assertEqual(s._todo_count, before - 1)

    def test_grid_mode_mark_updates_all_keys(self):
        s = ReviewSession(self.store, reviewer="tester", mode="grid")
        s._cursor = 0
        keys = s._items[0].keys
        s._mark("DIRTY")
        self.assertTrue(keys)
        self.assertEqual({s._statuses[k] for k in keys}, {"DIRTY"})

    def test_mode_switches_pack_once(self):
        with mock.patch.object(controller_module, "pack_into_grids", wraps=controller_module.pack_into_grids) as pack:
            s = ReviewSession(self.store, reviewer="tester", mode="grid")
            first = s._items
            for _ in range(2):
                s._switch_to_single()
                s._switch_to_grid(True)
            self.assertEqual(pack.call_count, 1)
            self.assertEqual(sorted(item.keys for item in s._items), sorted(item.keys for item in first))
            s._switch_to_grid(False)  # another key replaces the only cached result
            s._switch_to_grid(True)
            self.assertEqual(pack.call_count, 3)

    def test_grid_size_change_packs_again(self):
        with mock.patch.object(controller_module, "pack_into_grids", wraps=controller_module.pack_into_grids) as pack:
            s = ReviewSession(self.store, reviewer="tester", mode="grid")
            s._switch_to_single()
            w, h = s._viewer.screen.get_size()
            pg.display.set_mode((w // 2, h // 2))
            s._switch_to_grid(True)
            self.assertEqual(pack.call_count, 2)

    def test_grid_cache_never_shows_a_key_marked_dirty(self):
        with mock.patch.object(controller_module, "pack_into_grids", wraps=controller_module.pack_into_grids) as pack:
            s = ReviewSession(self.store, reviewer="tester", mode="grid", status_filter="all")
            s._cursor = 0
            s._mark("CLEAN")  # still eligible under all: the rows, so the cache, are unchanged
            s._switch_to_single()
            s._switch_to_grid(True)
            self.assertEqual(pack.call_count, 1)
            s._cursor = 0
            dirty = s._items[0].keys
            s._mark("DIRTY")
            s._switch_to_single()
            s._switch_to_grid(True)
            self.assertEqual(pack.call_count, 2)
        self.assertFalse(set(dirty) & self.grid_keys(s))

    def finish_pass_one(self):
        """Pass 1 ends with a DIRTY and b, c, d CLEAN."""
        self.store.mark(["batch_001/a.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_001/b.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")

    @staticmethod
    def grid_keys(s: ReviewSession) -> set[str]:
        return {k for item in s._items for k in item.keys}

    def test_pass_two_grid_excludes_flagged(self):
        self.finish_pass_one()
        s = ReviewSession(self.store, reviewer="tester", mode="grid")
        self.assertEqual(s.pass_number, 2)
        self.assertEqual(s._items, [])
        self.assertIsNone(s.batch)

    def test_pass_two_grid_all_filter_excludes_flagged(self):
        self.finish_pass_one()
        for batch in ("batch_001", None):
            with self.subTest(batch=batch):
                s = ReviewSession(self.store, reviewer="tester", mode="grid", status_filter="all", batch=batch)
                self.assertNotIn("batch_001/a.jpg", self.grid_keys(s))
                self.assertIn("batch_001/b.jpg", self.grid_keys(s))

    def test_pass_two_single_shows_flagged_as_todo(self):
        self.finish_pass_one()
        s = ReviewSession(self.store, reviewer="tester", mode="single")
        self.assertEqual(s.pass_number, 2)
        self.assertEqual(s.batch, "batch_001")
        self.assertEqual([item.keys[0] for item in s._items], ["batch_001/a.jpg"])
        self.assertEqual(s._todo_count, 1)
        s._cursor = 0
        s._mark("CLEAN")
        self.assertEqual(s._todo_count, 0)

    def test_pass_one_all_filter_grid_excludes_dirty(self):
        self.store.mark(["batch_001/a.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        s = ReviewSession(self.store, reviewer="tester", mode="grid", status_filter="all")
        self.assertEqual(s.pass_number, 1)
        self.assertEqual(self.grid_keys(s), {"batch_001/b.jpg"})
        s = ReviewSession(self.store, reviewer="tester", mode="grid", status_filter="all", batch="batch_002")
        self.assertEqual(self.grid_keys(s), {"batch_002/c.jpg", "batch_002/d.jpg"})

    def test_auto_select_batch_uses_grid_eligibility(self):
        self.store.mark(["batch_001/a.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_001/b.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_002/c.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        # pass 2: batch_001's only todo image is FLAGGED; batch_002 has d UNREVIEWED
        self.assertEqual(ReviewSession(self.store, reviewer="tester", mode="grid", pass_number=2).batch, "batch_002")
        self.assertEqual(ReviewSession(self.store, reviewer="tester", mode="single", pass_number=2).batch, "batch_001")

    def test_grid_clean_refused_when_shared_image_id_became_dirty(self):
        # a and b share an image_id; marking grid [a] DIRTY makes b DIRTY inside grid [b, c]
        with open(self.work_dir / "manifest.tsv", "w", newline="") as f:
            writer = csv.writer(f, delimiter="\t")
            writer.writerow(["batch", "preprocessed_path", "image_id"])
            writer.writerow(["batch_001", "batch_001/a.jpg", "/src/same.dcm"])
            writer.writerow(["batch_001", "batch_001/b.jpg", "/src/same.dcm"])
            writer.writerow(["batch_001", "batch_002/c.jpg", "/src/other.dcm"])
        self.store.close()
        self.store = LocalStore(self.work_dir)
        self.addCleanup(self.store.close)
        s = ReviewSession(self.store, reviewer="tester", mode="grid")
        s._items = [
            ReviewItem(keys=("batch_001/a.jpg",), label="grid (1 images)", surface=None, grid=True),
            ReviewItem(keys=("batch_001/b.jpg", "batch_002/c.jpg"), label="grid (2 images)", surface=None, grid=True),
        ]
        s._cursor = 0
        s._mark("DIRTY")
        self.assertEqual(s._statuses["batch_001/b.jpg"], "DIRTY")
        self.assertEqual(s._item_status(s._items[1]), "DIRTY")
        self.assertEqual(s._count_todo(), 0)
        s._cursor = 1
        with mock.patch.object(self.store, "mark", wraps=self.store.mark) as mark, mock.patch("sys.stderr"):
            s._mark("CLEAN")
            mark.assert_not_called()
            self.assertEqual(s._statuses["batch_001/b.jpg"], "DIRTY")
            self.assertEqual(s._viewer._info, GRID_HAS_DIRTY)
            # the all-DIRTY grid may reverse its own verdict
            s._cursor = 0
            s._mark("CLEAN")
            mark.assert_called_once()
        self.assertEqual(self.store.statuses(1)["batch_001/b.jpg"], "CLEAN")

    def test_one_key_grid_follows_grid_rules(self):
        # a one-image grid holding an image that became FLAGGED mid-session is not single-mode todo
        self.finish_pass_one()
        s = ReviewSession(self.store, reviewer="tester", mode="grid")
        self.assertEqual(s._statuses["batch_001/a.jpg"], "FLAGGED")
        s._items = [ReviewItem(keys=("batch_001/a.jpg",), label="grid (1 images)", surface=None, grid=True)]
        s._cursor = 0
        self.assertEqual(s._item_status(s._items[0]), "DIRTY")
        self.assertEqual(s._count_todo(), 0)
        with mock.patch.object(self.store, "mark") as mark, mock.patch.object(s._viewer, "set_info") as set_info, \
                mock.patch("sys.stderr"):
            s._mark("CLEAN")
        mark.assert_not_called()
        set_info.assert_called_once_with(GRID_HAS_DIRTY)

    def test_flagged_is_orange(self):
        s = ReviewSession(self.store, reviewer="tester", mode="single")
        self.assertEqual(s._viewer.STATUS_COLORS["FLAGGED"], pg.Color(255, 176, 64))
        s._viewer.set_status("FLAGGED")
        s._viewer.refresh()  # every Status has a colour; a missing one would raise

    def test_restart_refetches_statuses(self):
        s = ReviewSession(self.store, reviewer="tester", mode="single")
        self.store.mark(["batch_001/a.jpg"], "CLEAN", s.pass_number, reviewer="tester", mode="single")
        self.assertEqual(s._statuses["batch_001/a.jpg"], "UNREVIEWED")
        s._restart_in_mode("grid")
        self.assertEqual(s._statuses["batch_001/a.jpg"], "CLEAN")

    def recheck_session(self, status_filter: str) -> ReviewSession:
        """A pass 1 single-mode session over already reviewed images: batch_002 (c, d CLEAN), or batch_001 (a DIRTY, b CLEAN) under all."""
        self.finish_pass_one()
        batch = "batch_001" if status_filter == "all" else "batch_002"
        s = ReviewSession(self.store, reviewer="tester", mode="single", status_filter=status_filter, batch=batch, pass_number=1)
        self.assertEqual(s._todo_count, len(s._items))
        return s

    def mark_and_next_todo_skips(self, s: ReviewSession, status: str) -> None:
        s._cursor = 0
        marked = s._items[0].keys
        before = s._todo_count
        s._mark(status)
        self.assertEqual(s._todo_count, before - 1)
        s._cursor = -1
        self.assertTrue(s.next_todo())
        self.assertNotEqual(s._items[s._cursor].keys, marked)

    def test_clean_filter_marking_counts_as_done(self):
        s = self.recheck_session("clean")
        self.assertEqual(len(s._items), 2)
        self.mark_and_next_todo_skips(s, "CLEAN")

    def test_all_filter_marking_counts_as_done(self):
        s = self.recheck_session("all")
        self.assertEqual(len(s._items), 2)
        self.mark_and_next_todo_skips(s, "CLEAN")

    def test_all_filter_remarking_dirty_counts_as_done(self):
        s = self.recheck_session("all")
        dirty = next(i for i, item in enumerate(s._items) if item.keys == ("batch_001/a.jpg",))
        s._items.insert(0, s._items.pop(dirty))
        self.mark_and_next_todo_skips(s, "DIRTY")
        self.assertEqual(s._statuses["batch_001/a.jpg"], "DIRTY")

    def test_all_marked_means_no_todo_remaining(self):
        s = self.recheck_session("clean")
        for i in range(len(s._items)):
            s._cursor = i
            s._mark("CLEAN")
        self.assertEqual(s._todo_count, 0)
        s._cursor = -1
        self.assertFalse(s.next_todo())

    def check_undo_makes_item_todo_again(self, status_filter: str) -> None:
        s = self.recheck_session(status_filter)
        s._cursor = 0
        before = s._todo_count
        s._mark("CLEAN")
        self.assertEqual(s._todo_count, before - 1)
        s._undo()
        self.assertEqual(s._todo_count, before)
        s._cursor = -1
        self.assertTrue(s.next_todo())
        self.assertEqual(s._cursor, 0)

    def test_clean_filter_undo_makes_item_todo_again(self):
        self.check_undo_makes_item_todo_again("clean")

    def test_all_filter_undo_makes_item_todo_again(self):
        self.check_undo_makes_item_todo_again("all")


def key(k: int) -> pg.event.Event:
    return pg.event.Event(pg.KEYDOWN, key=k, mod=0)


class EventLoopTestCase(SessionTestCase):
    """Drive the run loop's two halves, handle_events and refresh_if_needed, with synthetic events."""

    def setUp(self):
        super().setUp()
        self.now = 10_000
        for patcher in (
            mock.patch.object(pg.time, "get_ticks", lambda: self.now),
            mock.patch.object(self.store, "mark", wraps=self.store.mark),
        ):
            patched = patcher.start()
            self.addCleanup(patcher.stop)
        self.mark = patched

    def reviewing(self, mode: str = "single") -> ReviewSession:
        """A session past the splash with its first item painted and seen for the full dwell."""
        s = ReviewSession(self.store, reviewer="tester", mode=mode)
        s._show_splash()
        self.assertTrue(s.handle_events([key(pg.K_SPACE)]))
        self.paint(s)
        self.now += MIN_DWELL_MS
        return s

    @staticmethod
    def paint(s: ReviewSession) -> None:
        with mock.patch.object(s._viewer, "refresh") as refresh:
            s.refresh_if_needed()
        refresh.assert_called_once()

    def assert_message_stays(self, s: ReviewSession, state: UIState = UIState.END_MESSAGE) -> None:
        """With no items, the message survives the loop's refresh and the navigation keys."""
        self.assertEqual(s._ui_state, state)
        with mock.patch.object(s._viewer, "refresh") as refresh:
            s.refresh_if_needed()
            for k in (pg.K_SPACE, pg.K_RIGHT, pg.K_LEFT):
                self.assertTrue(s.handle_events([key(k)]))
                self.assertEqual(s._ui_state, state)
                s.refresh_if_needed()
        refresh.assert_not_called()


class TestEventLoop(EventLoopTestCase):
    def test_verdict_after_dwell_marks(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])
        self.mark.assert_called_once()

    def test_verdict_before_dwell_is_ignored(self):
        s = ReviewSession(self.store, reviewer="tester", mode="single")
        s._show_splash()
        s.handle_events([key(pg.K_SPACE), key(pg.K_c)])  # before the first paint
        self.paint(s)
        self.now += MIN_DWELL_MS - 1
        s.handle_events([key(pg.K_c)])
        self.mark.assert_not_called()
        self.now += 1
        s.handle_events([key(pg.K_c)])
        self.mark.assert_called_once()

    def test_repaint_of_same_item_keeps_dwell(self):
        s = self.reviewing()
        s.handle_events([pg.event.Event(pg.WINDOWRESIZED)])
        self.paint(s)
        s.handle_events([key(pg.K_d)])
        self.mark.assert_called_once()

    def test_verdict_queued_behind_mode_switch_is_ignored(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_m), key(pg.K_c)])
        self.assertEqual(s.mode, "grid")
        self.assertEqual(s._ui_state, UIState.REVIEWING)
        self.mark.assert_not_called()

    def test_gamepad_verdict_queued_behind_mode_switch_is_ignored(self):
        s = self.reviewing("grid")
        s.handle_events([key(pg.K_s), pg.event.Event(pg.JOYBUTTONDOWN, button=1)])
        self.mark.assert_not_called()

    def test_verdict_in_batch_with_autoplay_advance_is_ignored(self):
        s = self.reviewing()
        s.autoplay = True
        s.handle_events([pg.event.Event(AUTOPLAY_EVENT), key(pg.K_c)])
        self.assertEqual(s._cursor, 1)
        self.assertFalse(s.autoplay)
        self.mark.assert_not_called()

    def test_verdict_in_batch_with_post_mark_advance_is_ignored(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])
        s.handle_events([pg.event.Event(ADVANCE_EVENT), key(pg.K_c)])
        self.assertEqual(s._cursor, 1)
        self.mark.assert_called_once()

    def test_n_skips_done_items(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])  # item 0 CLEAN
        s.handle_events([key(pg.K_n)])
        self.assertEqual((s._cursor, s._ui_state), (1, UIState.REVIEWING))
        s.handle_events([key(pg.K_n)])  # round past the done item 0, back to 1
        self.assertEqual((s._cursor, s._ui_state), (1, UIState.REVIEWING))

    def test_n_wraps_from_last_item_to_earlier_todo(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_RIGHT)])
        self.paint(s)
        self.now += MIN_DWELL_MS
        s.handle_events([key(pg.K_c)])  # the last item CLEAN
        self.assertEqual(s._cursor, len(s._items) - 1)
        s.handle_events([key(pg.K_n)])
        self.assertEqual((s._cursor, s._ui_state), (0, UIState.REVIEWING))

    def test_todo_only_right_from_last_todo_ends(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_RIGHT)])
        self.paint(s)
        self.now += MIN_DWELL_MS
        s.handle_events([key(pg.K_c)])  # the last item CLEAN: item 0 is the last todo item
        s.handle_events([key(pg.K_LEFT), key(pg.K_u)])
        self.assertEqual(s._cursor, 0)
        with mock.patch.object(s._viewer, "show_message") as show_message:
            s.handle_events([key(pg.K_RIGHT)])
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        show_message.assert_called_once_with("No more todo images this way - 1 todo left - [b] next batch")

    def test_empty_mode_message_is_not_painted_over(self):
        s = self.reviewing()
        self.store.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        s.handle_events([key(pg.K_m)])
        self.assertTrue(s._dirty)
        self.assert_message_stays(s)

    def test_outage_on_restart_message_is_not_painted_over(self):
        s = self.reviewing()
        with (
            mock.patch.object(self.store, "statuses", side_effect=store_module.StoreUnavailable("down")),
            mock.patch("sys.stderr"),
        ):
            s.handle_events([key(pg.K_m)])
        self.assertEqual(s._items, [])
        self.assert_message_stays(s, UIState.DISCONNECTED)

    def test_stale_advance_after_mode_switch_is_ignored(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])
        s.handle_events([key(pg.K_m), pg.event.Event(ADVANCE_EVENT)])
        self.assertEqual(s.mode, "grid")
        self.assertEqual(s._cursor, 0)
        self.assertEqual(s._ui_state, UIState.REVIEWING)

    def test_stale_advance_after_manual_navigation_is_ignored(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])
        s.handle_events([key(pg.K_RIGHT), pg.event.Event(ADVANCE_EVENT)])
        self.assertEqual(s._cursor, 1)  # the last item: a second advance would reach "End of list"
        self.assertEqual(s._ui_state, UIState.REVIEWING)

    def test_post_mark_advance_moves_on(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])
        s.handle_events([pg.event.Event(ADVANCE_EVENT)])
        self.assertEqual(s._cursor, 1)
        s.handle_events([pg.event.Event(ADVANCE_EVENT)])  # one mark, one advance
        self.assertEqual(s._cursor, 1)
        self.assertEqual(s._ui_state, UIState.REVIEWING)

    def test_slow_event_earlier_in_batch_does_not_count_toward_dwell(self):
        s = ReviewSession(self.store, reviewer="tester", mode="single")
        s._show_splash()
        s.handle_events([key(pg.K_SPACE)])
        self.paint(s)
        self.now += 5

        def slow_resize():
            self.now += MIN_DWELL_MS

        with mock.patch.object(s._viewer, "resize", slow_resize):
            s.handle_events([pg.event.Event(pg.WINDOWRESIZED), key(pg.K_c)])
        self.mark.assert_not_called()

    def test_mark_records_reviewer_and_current_mode(self):
        s = ReviewSession(self.store, reviewer="rev", mode="single", status_filter="all")
        with mock.patch.object(self.store, "mark", wraps=self.store.mark) as mark:
            s._switch_to_grid(True)
            s._mark("CLEAN")
            self.assertEqual(mark.call_args.kwargs, {"reviewer": "rev", "mode": "grid"})
            s._switch_to_single()
            s._mark("DIRTY")
            self.assertEqual(mark.call_args.kwargs, {"reviewer": "rev", "mode": "single"})

    def test_input_during_grid_build_is_discarded(self):
        s = self.reviewing()
        real_pack = controller_module.pack_into_grids

        def pack_while_typing(*args, **kwargs):
            pg.event.post(key(pg.K_c))
            pg.event.post(pg.event.Event(pg.JOYBUTTONDOWN, button=1))
            return real_pack(*args, **kwargs)

        pg.event.clear()
        with mock.patch.object(controller_module, "pack_into_grids", pack_while_typing):
            s._switch_to_grid(True)
        self.assertEqual(pg.event.get((pg.KEYDOWN, pg.JOYBUTTONDOWN)), [])

    def test_correction_after_dwell(self):
        s = self.reviewing()
        item_key = s._items[s._cursor].keys[0]
        s.handle_events([key(pg.K_c)])
        self.paint(s)
        self.now += 50
        s.handle_events([key(pg.K_d)])
        self.assertEqual(s._statuses[item_key], "DIRTY")

    def test_help_stops_autoplay(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_SPACE)])
        self.assertTrue(s.autoplay)
        cursor = s._cursor
        s.handle_events([key(pg.K_h)])
        self.assertEqual(s._ui_state, UIState.SPLASH)
        self.assertFalse(s.autoplay)
        s.autoplay = True  # a timer event already queued must not act behind the help screen
        s.handle_events([pg.event.Event(AUTOPLAY_EVENT), pg.event.Event(ADVANCE_EVENT)])
        self.assertEqual(s._cursor, cursor)
        self.assertEqual(s._ui_state, UIState.SPLASH)

    def test_display_select_stops_autoplay(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_SPACE), key(pg.K_w)])
        self.assertFalse(s.autoplay)
        self.assertEqual(s._ui_state, UIState.SPLASH)

    def test_quit_stops_the_batch(self):
        s = self.reviewing()
        self.assertFalse(s.handle_events([key(pg.K_q), key(pg.K_c)]))
        self.mark.assert_not_called()

    def test_undo_restores_status_cursor_and_dwell(self):
        s = self.reviewing()
        item_key = s._items[0].keys[0]
        todo = s._todo_count
        s.handle_events([key(pg.K_c)])
        s.handle_events([pg.event.Event(ADVANCE_EVENT)])
        self.paint(s)
        self.now += MIN_DWELL_MS
        self.assertEqual((s._cursor, s._todo_count), (1, todo - 1))
        s.handle_events([key(pg.K_z), key(pg.K_d)])  # a verdict right after the undo is not counted
        self.assertEqual((s._cursor, s._statuses[item_key], s._todo_count), (0, "UNREVIEWED", todo))
        self.assertEqual(self.store.statuses(s.pass_number)[item_key], "UNREVIEWED")
        self.assertIsNone(s._shown_at)
        self.mark.assert_called_once()
        self.paint(s)
        self.now += MIN_DWELL_MS
        s.handle_events([key(pg.K_d)])
        self.assertEqual(s._statuses[item_key], "DIRTY")

    def test_undo_cancels_post_mark_advance(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])
        s.handle_events([key(pg.K_z), pg.event.Event(ADVANCE_EVENT)])
        self.assertEqual(s._cursor, 0)
        self.assertEqual(s._statuses[s._items[0].keys[0]], "UNREVIEWED")

    def test_undo_of_grid_restores_every_key(self):
        s = self.reviewing("grid")
        keys = s._items[0].keys
        s.handle_events([key(pg.K_d)])
        self.assertEqual({s._statuses[k] for k in keys}, {"DIRTY"})
        s.handle_events([key(pg.K_z)])
        self.assertEqual({s._statuses[k] for k in keys}, {"UNREVIEWED"})
        self.assertEqual((s._cursor, s._item_status(s._items[0])), (0, "UNREVIEWED"))
        self.assertIsNone(s._shown_at)

    def test_undo_on_end_of_list_returns_to_the_item(self):
        s = self.reviewing()
        for _ in s._items:
            s.handle_events([key(pg.K_c)])
            s.handle_events([pg.event.Event(ADVANCE_EVENT)])
            if s._ui_state == UIState.REVIEWING:
                self.paint(s)
            self.now += MIN_DWELL_MS
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        last_key = s._items[-1].keys[0]
        s.handle_events([key(pg.K_z)])
        self.assertEqual(s._ui_state, UIState.REVIEWING)
        self.assertEqual((s._cursor, s._statuses[last_key]), (len(s._items) - 1, "UNREVIEWED"))
        self.paint(s)

    def test_nothing_to_undo(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_z)])
        self.assertEqual(s._viewer._info, NOTHING_TO_UNDO)
        self.assertEqual(s._cursor, 0)
        self.assertEqual(s._ui_state, UIState.REVIEWING)

    def test_undo_outage_is_a_lost_connection_that_only_quits(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])
        cursor = s._cursor
        with (
            mock.patch.object(self.store, "undo", side_effect=store_module.StoreUnavailable("down")) as undo,
            mock.patch("sys.stderr"),
        ):
            s.handle_events([key(pg.K_z)])
            self.assertEqual(s._ui_state, UIState.DISCONNECTED)
            for k in (pg.K_z, pg.K_RIGHT, pg.K_SPACE, pg.K_LEFT, pg.K_n, pg.K_m, pg.K_s, pg.K_c, pg.K_b):
                self.assertTrue(s.handle_events([key(k)]))
            undo.assert_called_once()
        self.mark.assert_called_once()
        self.assertEqual((s._ui_state, s._cursor), (UIState.DISCONNECTED, cursor))
        self.assertFalse(s.handle_events([key(pg.K_q)]))

    def test_undo_never_reaches_a_mark_from_before_a_mode_switch(self):
        s = self.reviewing()
        item_key = s._items[s._cursor].keys[0]
        s.handle_events([key(pg.K_d)])
        s.handle_events([key(pg.K_m)])  # grid mode: the DIRTY image is in no grid
        self.assertEqual(s._ui_state, UIState.REVIEWING)
        with mock.patch.object(self.store, "undo", wraps=self.store.undo) as undo:
            s.handle_events([key(pg.K_z)])
            undo.assert_not_called()
        self.assertEqual(s._viewer._info, NOTHING_TO_UNDO)
        self.assertEqual(self.store.statuses(s.pass_number)[item_key], "DIRTY")

    def test_undo_count_follows_marks_and_undos(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])
        s.handle_events([key(pg.K_z)])
        self.assertEqual(s._undoable, 0)
        with mock.patch.object(self.store, "undo", wraps=self.store.undo) as undo:
            s.handle_events([key(pg.K_z)])
            undo.assert_not_called()
        self.assertEqual(s._viewer._info, NOTHING_TO_UNDO)

    def test_key_repeat_is_off(self):
        ReviewSession(self.store, reviewer="tester", mode="single")
        self.assertEqual(pg.key.get_repeat(), (0, 0))  # a held z undoes one mark, not many


class TestNextBatchKey(EventLoopTestCase):
    """b on the end-of-list screen moves on to the next batch (batch_001: a, b; batch_002: c, d)."""

    def start(self, **kwargs) -> ReviewSession:
        """A session (single mode unless given) past the splash, its first item seen for the full dwell."""
        s = ReviewSession(self.store, reviewer="tester", **{"mode": "single", **kwargs})
        s._show_splash()
        s.handle_events([key(pg.K_SPACE)])
        if s._ui_state == UIState.REVIEWING:
            self.paint(s)
        self.now += MIN_DWELL_MS
        return s

    def finish(self, s: ReviewSession, dirty: str | None = None) -> None:
        """Mark every item of the batch (an item holding `dirty` DIRTY, the rest CLEAN), reaching the end of the list."""
        for _ in range(len(s._items)):
            s.handle_events([key(pg.K_d if dirty in s._items[s._cursor].keys else pg.K_c)])
            s.handle_events([pg.event.Event(ADVANCE_EVENT)])
            if s._ui_state == UIState.REVIEWING:
                self.paint(s)
            self.now += MIN_DWELL_MS
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)

    @staticmethod
    def skip(s: ReviewSession) -> None:
        """Walk past every item without marking, to the end of the list."""
        for _ in s._items:
            s.handle_events([key(pg.K_RIGHT)])

    def press_b(self, s: ReviewSession) -> mock.Mock:
        with mock.patch.object(s._viewer, "show_message") as show_message:
            s.handle_events([key(pg.K_b)])
        if s._ui_state == UIState.REVIEWING:
            self.paint(s)
            self.now += MIN_DWELL_MS
        return show_message

    def test_end_of_list_message_counts_todo_and_offers_b(self):
        s = self.reviewing()
        with mock.patch.object(s._viewer, "show_message") as show_message:
            self.skip(s)
        show_message.assert_called_once_with("End of list - 2 todo left - [b] next batch")
        s = self.reviewing()
        with mock.patch.object(s._viewer, "show_message") as show_message:
            self.finish(s)
        show_message.assert_called_once_with("End of list - [b] next batch")

    def test_todo_only_end_with_nothing_left(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_u)])
        with mock.patch.object(s._viewer, "show_message") as show_message:
            self.finish(s)
        show_message.assert_called_once_with(f"{NO_TODO_MESSAGE} - [b] next batch")

    def test_b_loads_the_next_batch(self):
        s = self.reviewing()
        self.assertEqual(s.batch, "batch_001")
        self.assertIn("batch 1/2", s._info_line())
        self.finish(s)
        with mock.patch.object(self.store, "statuses", wraps=self.store.statuses) as statuses:
            self.press_b(s)
        statuses.assert_called_once()  # the rebuild reuses the snapshot b fetched
        self.assertEqual((s.batch, s.pass_number), ("batch_002", 1))
        self.assertEqual((s._ui_state, s._cursor), (UIState.REVIEWING, 0))
        self.assertEqual({item.keys[0] for item in s._items}, {"batch_002/c.jpg", "batch_002/d.jpg"})
        self.assertIn("batch 2/2", s._info_line())

    def test_b_in_grid_mode_rebuilds_grids(self):
        s = self.reviewing("grid")
        self.finish(s)
        self.press_b(s)
        self.assertEqual((s.batch, s.mode, s._ui_state), ("batch_002", "grid", UIState.REVIEWING))
        self.assertEqual({k for item in s._items for k in item.keys}, {"batch_002/c.jpg", "batch_002/d.jpg"})

    def test_explicit_pass_is_kept_after_the_pass_advances(self):
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        s = self.start(pass_number=1)
        self.finish(s, dirty="batch_001/a.jpg")
        self.assertEqual(self.store.current_pass(), 2)
        show_message = self.press_b(s)
        show_message.assert_called_once_with("All batches done for pass 1 (current pass is 2)")
        self.assertEqual((s._ui_state, s.pass_number), (UIState.END_MESSAGE, 1))
        self.assert_message_stays(s)
        self.assertTrue(s.handle_events([key(pg.K_b)]))
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        self.assertFalse(s.handle_events([key(pg.K_q)]))

    def test_undo_after_all_done_is_nothing_to_undo(self):
        s = self.start(pass_number=1)
        self.finish(s)
        self.press_b(s)
        self.finish(s)
        self.press_b(s)
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        with mock.patch.object(self.store, "undo", wraps=self.store.undo) as undo:
            show_message = self.press_key(s, pg.K_z)
        undo.assert_not_called()
        show_message.assert_called_once_with(NOTHING_TO_UNDO)

    @staticmethod
    def press_key(s: ReviewSession, k: int) -> mock.Mock:
        with mock.patch.object(s._viewer, "show_message") as show_message:
            s.handle_events([key(k)])
        return show_message

    def test_auto_pass_advances_to_the_flagged_batch(self):
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        s = self.reviewing()
        self.assertEqual(s.batch, "batch_001")
        self.finish(s, dirty="batch_001/a.jpg")
        self.press_b(s)
        self.assertEqual((s.pass_number, s.batch), (2, "batch_001"))
        self.assertEqual([item.keys[0] for item in s._items], ["batch_001/a.jpg"])
        self.assertEqual(s._statuses["batch_001/a.jpg"], "FLAGGED")
        self.assertEqual(s._ui_state, UIState.REVIEWING)
        self.assertEqual(s._viewer._info, "Now pass 2")

    def test_new_pass_starts_from_the_first_batch(self):
        self.store.mark(["batch_002/c.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        s = self.reviewing()
        self.assertEqual(s.batch, "batch_001")
        self.finish(s, dirty="batch_001/a.jpg")
        self.press_b(s)
        self.assertEqual((s.pass_number, s.batch), (2, "batch_001"))  # not batch_002, the next one after it

    def test_auto_pass_with_nothing_in_the_new_pass(self):
        s = self.reviewing()
        self.finish(s)
        self.press_b(s)
        self.finish(s)
        show_message = self.press_b(s)
        show_message.assert_called_once_with("Pass 1 complete - nothing to review in pass 2")
        self.assertEqual((s._ui_state, s.pass_number), (UIState.END_MESSAGE, 2))

    def test_grid_mode_names_flagged_images_held_back(self):
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        s = self.reviewing("grid")
        self.assertEqual(s.batch, "batch_001")
        self.finish(s, dirty="batch_001/a.jpg")  # the one grid holds a and b: both DIRTY
        show_message = self.press_b(s)
        self.assertEqual(s.pass_number, 2)
        show_message.assert_called_once_with(
            "No grid items for pass 2; 2 FLAGGED/DIRTY images need single-mode review - press [s]"
        )
        self.assertEqual((s._ui_state, s.batch), (UIState.END_MESSAGE, "batch_001"))
        s.handle_events([key(pg.K_s)])
        self.assertEqual((s.mode, s._ui_state), ("single", UIState.REVIEWING))
        self.assertEqual({item.keys[0] for item in s._items}, {"batch_001/a.jpg", "batch_001/b.jpg"})

    def test_held_back_images_move_the_batch_for_s(self):
        self.store.mark(["batch_001/a.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_001/b.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        s = self.reviewing("grid")
        self.assertEqual(s.batch, "batch_002")
        self.finish(s)
        show_message = self.press_b(s)
        self.assertEqual(s.pass_number, 2)
        show_message.assert_called_once_with(
            "No grid items for pass 2; 1 FLAGGED/DIRTY image needs single-mode review - press [s]"
        )
        self.assertEqual(s.batch, "batch_001")
        s.handle_events([key(pg.K_s)])
        self.assertEqual([item.keys[0] for item in s._items], ["batch_001/a.jpg"])

    def test_grid_all_filter_does_not_hold_back_a_judged_dirty_image(self):
        s = self.start(status_filter="all", pass_number=1)
        self.finish(s, dirty="batch_001/a.jpg")  # a DIRTY in this pass and marked here: done, not held back
        s.handle_events([key(pg.K_m)])
        self.assertEqual((s.mode, s.batch), ("grid", "batch_001"))
        self.skip(s)
        self.press_b(s)
        self.assertEqual(s.batch, "batch_002")
        self.finish(s)
        show_message = self.press_b(s)
        show_message.assert_called_once_with("All batches done for pass 1 (current pass is 2)")

    def test_all_filter_finds_a_marked_image_flagged_in_the_new_pass(self):
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        s = self.start(status_filter="all")
        self.assertEqual(s.batch, "batch_001")
        self.finish(s, dirty="batch_001/a.jpg")  # a was marked in this session, and is FLAGGED in pass 2
        self.press_b(s)
        self.assertEqual((s.pass_number, s.batch, s._ui_state), (2, "batch_001", UIState.REVIEWING))
        self.assertEqual(s._todo_count, 1)
        a = next(item for item in s._items if item.keys == ("batch_001/a.jpg",))
        self.assertTrue(s._is_todo(a))

    def test_explicit_batch_after_the_pass_ends(self):
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        s = self.start(batch="batch_001")
        self.finish(s)
        show_message = self.press_b(s)
        show_message.assert_called_once_with("Pass 1 complete - nothing to review in batch_001 for pass 2")

    def test_unreviewed_wraps_to_todo_left_in_an_earlier_batch(self):
        s = self.reviewing()
        self.skip(s)  # batch_001 left unmarked
        self.press_b(s)
        self.assertEqual(s.batch, "batch_002")
        self.skip(s)
        self.press_b(s)
        self.assertEqual((s.batch, s.pass_number, s._ui_state), ("batch_001", 1, UIState.REVIEWING))

    def test_clean_filter_wraps_until_everything_is_rechecked(self):
        for batch_keys in (["batch_001/a.jpg", "batch_001/b.jpg"], ["batch_002/c.jpg", "batch_002/d.jpg"]):
            self.store.mark(batch_keys, "CLEAN", 1, reviewer="tester", mode="single")
        s = self.start(status_filter="clean")
        self.assertEqual(s.batch, "batch_001")
        self.skip(s)  # batch_001 shown but not re-checked
        self.press_b(s)
        self.assertEqual((s.batch, s._ui_state), ("batch_002", UIState.REVIEWING))
        self.finish(s)
        self.press_b(s)
        self.assertEqual((s.batch, s._ui_state), ("batch_001", UIState.REVIEWING))  # found again by wrapping
        self.finish(s)
        show_message = self.press_b(s)
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        show_message.assert_called_once()
        show_message.assert_called_once_with(f"All batches done for pass {s.pass_number}")

    def test_explicit_batch_restricts_b(self):
        s = self.start(batch="batch_002")
        self.skip(s)  # c and d skipped
        self.press_b(s)
        self.assertEqual((s.batch, s._ui_state), ("batch_002", UIState.REVIEWING))  # reloaded with its todo
        self.finish(s)
        show_message = self.press_b(s)
        show_message.assert_called_once_with("Batch batch_002 done for pass 1")
        self.assertEqual((s.batch, s._ui_state), ("batch_002", UIState.END_MESSAGE))  # batch_001 still todo

    def test_b_outage_is_a_lost_connection(self):
        s = self.reviewing()
        self.skip(s)
        with (
            mock.patch.object(self.store, "statuses", side_effect=store_module.StoreUnavailable("down")),
            mock.patch("sys.stderr"),
        ):
            s.handle_events([key(pg.K_b)])
        self.assertEqual(s._ui_state, UIState.DISCONNECTED)
        self.assert_message_stays(s, UIState.DISCONNECTED)

    def test_b_while_reviewing_does_nothing(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_b)])
        self.assertEqual((s.batch, s._cursor, s._ui_state), ("batch_001", 0, UIState.REVIEWING))


CORRUPT = "batch_001/a.jpg"


class TestUnloadable(EventLoopTestCase):
    """One preprocessed JPG is garbage: it is shown as a placeholder that can only be marked DIRTY."""

    def setUp(self):
        super().setUp()
        (self.work_dir / CORRUPT).write_bytes(b"not a jpeg")
        stderr = mock.patch("sys.stderr", io.StringIO())
        self.stderr = stderr.start()
        self.addCleanup(stderr.stop)

    def show(self, s: ReviewSession, index: int) -> None:
        """Put item `index` on screen and let its dwell elapse."""
        s._cursor = index
        s._show_current()
        self.paint(s)
        self.now += MIN_DWELL_MS

    @staticmethod
    def index_of(s: ReviewSession, key: str) -> int:
        return next(i for i, item in enumerate(s._items) if key in item.keys)

    def walk(self, s: ReviewSession, arrow: int) -> list[str]:
        """Labels shown while pressing `arrow` until the end of the list; the cursor stays in range."""
        labels = []
        while s._ui_state == UIState.REVIEWING:
            self.assertIn(s._cursor, range(len(s._items)))
            labels.append(s._viewer._name)
            s.handle_events([key(arrow)])
            if s._ui_state == UIState.REVIEWING:
                self.paint(s)
        self.assertIn(s._cursor, range(len(s._items)))
        return labels

    def test_single_navigation_reaches_placeholder_both_ways(self):
        s = self.reviewing()
        self.assertEqual(s.batch, "batch_001")
        forward = self.walk(s, pg.K_RIGHT)
        self.assertEqual(sorted(forward), ["batch_001/a.jpg", "batch_001/b.jpg"])
        self.assertEqual(s._cursor, len(s._items) - 1)
        self.assertIn(CORRUPT, s._unloadable)
        self.assertIn(f"cannot load {CORRUPT}", self.stderr.getvalue())
        s.handle_events([key(pg.K_LEFT)])  # from "End of list" back onto the last item
        self.paint(s)
        self.assertEqual(self.walk(s, pg.K_LEFT), forward[::-1])
        self.assertEqual(s._cursor, 0)

    def test_single_placeholder_refuses_clean_and_records_dirty(self):
        s = self.reviewing()
        self.show(s, self.index_of(s, CORRUPT))
        self.assertEqual(s._viewer._name, CORRUPT)
        s.handle_events([key(pg.K_c)])
        self.mark.assert_not_called()
        self.assertEqual(s._viewer._info, UNLOADABLE_CLEAN)
        self.assertEqual(s._statuses[CORRUPT], "UNREVIEWED")
        s.handle_events([key(pg.K_d)])
        self.mark.assert_called_once()
        self.assertEqual(self.store.statuses(1)[CORRUPT], "DIRTY")

    def test_grid_mode_has_single_placeholder_item(self):
        s = self.reviewing("grid")
        grids = [item for item in s._items if item.grid]
        self.assertTrue(grids)
        self.assertNotIn(CORRUPT, {k for item in grids for k in item.keys})
        index = self.index_of(s, CORRUPT)
        self.assertEqual(s._items[index], ReviewItem(keys=(CORRUPT,), label=CORRUPT, surface=None, grid=False))
        self.show(s, index)  # the placeholder is drawn only when the item is shown
        self.assertEqual(s._unloadable, {CORRUPT})
        self.assertEqual(s._viewer._name, CORRUPT)
        s.handle_events([key(pg.K_c)])
        self.mark.assert_not_called()
        self.assertEqual(s._viewer._info, UNLOADABLE_CLEAN)
        s.handle_events([key(pg.K_d)])
        self.assertEqual(self.mark.call_args.args[:2], ([CORRUPT], "DIRTY"))
        self.assertEqual(self.store.statuses(1)[CORRUPT], "DIRTY")

    def test_image_that_loads_again_can_be_marked_clean(self):
        s = self.reviewing()
        index = self.index_of(s, CORRUPT)
        self.show(s, index)
        self.assertIn(CORRUPT, s._unloadable)
        (self.work_dir / CORRUPT).write_bytes((self.work_dir / "batch_001/b.jpg").read_bytes())
        self.show(s, index)
        self.assertNotIn(CORRUPT, s._unloadable)
        s.handle_events([key(pg.K_c)])
        self.mark.assert_called_once()
        self.assertEqual(self.store.statuses(1)[CORRUPT], "CLEAN")

    def test_marking_placeholder_dirty_lets_batch_and_pass_advance(self):
        s = self.reviewing()
        for index in range(len(s._items)):
            self.show(s, index)
            s.handle_events([key(pg.K_d if s._items[index].keys[0] == CORRUPT else pg.K_c)])
        self.assertEqual(s._count_todo(), 0)
        self.assertEqual(ReviewSession(self.store, reviewer="tester", mode="single").batch, "batch_002")
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        self.store.close()
        with LocalStore(self.work_dir, read_only=True) as fresh:
            self.assertEqual(fresh.current_pass(), 2)
            self.assertEqual(fresh.statuses(2)[CORRUPT], "FLAGGED")

    def test_pack_into_grids_reports_unloadable_key(self):
        rows = [row for row in self.store.manifest() if row.batch == "batch_001"]
        grids, unloadable = pack_into_grids(rows, self.store, 400, 300)
        self.assertEqual(list(unloadable), [CORRUPT])
        self.assertEqual([gs.keys for gs in grids], [["batch_001/b.jpg"]])
        missing = [*rows, ManifestRow(key="batch_009/missing.jpg", batch="batch_009")]
        _, unloadable = pack_into_grids(missing, self.store, 400, 300)
        self.assertEqual(list(unloadable), [CORRUPT, "batch_009/missing.jpg"])


def dropping_packer(rect_id: int):
    """Patch grid_packer.newPacker so its packer leaves out the rect `rect_id` (the item's index)."""
    real = grid_packer_module.newPacker

    def factory(*args, **kwargs):
        packer = real(*args, **kwargs)
        rect_list = packer.rect_list
        packer.rect_list = lambda: [r for r in rect_list() if r[5] != rect_id]
        return packer

    return mock.patch.object(grid_packer_module, "newPacker", factory)


class TestLeftUnpacked(EventLoopTestCase):
    def test_unpacked_key_is_a_loadable_single_item(self):
        with dropping_packer(0), mock.patch("sys.stderr", io.StringIO()):
            s = self.reviewing("grid")
        dropped = s._review_rows(s.batch)[0].key
        index = next(i for i, item in enumerate(s._items) if dropped in item.keys)
        self.assertEqual(s._items[index], ReviewItem(keys=(dropped,), label=dropped, surface=None, grid=False))
        self.assertNotIn(dropped, {k for item in s._items if item.grid for k in item.keys})
        self.assertNotIn(dropped, s._unloadable)
        s._cursor = index
        s._show_current()
        self.paint(s)
        self.now += MIN_DWELL_MS
        self.assertEqual(s._viewer._name, dropped)
        self.assertNotIn(dropped, s._unloadable)
        s.handle_events([key(pg.K_c)])
        self.mark.assert_called_once()
        self.assertEqual(self.mark.call_args.args[:2], ([dropped], "CLEAN"))


class TestPackShrinksOversize(unittest.TestCase):
    def setUp(self):
        self.store = self.make_store({"big/a.jpg": (3000, 2500), "big/b.jpg": (100, 60), "big/c.jpg": (100, 60)})

    def make_store(self, sizes: dict[str, tuple[int, int]], colours: dict[str, tuple[int, int, int]] | None = None) -> LocalStore:
        """A store over solid JPGs of the given sizes (grey unless `colours` names one), one batch per directory."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = self.root = Path(tmp.name)
        rows = []
        for name, size in sizes.items():
            (root / name).parent.mkdir(exist_ok=True)
            colour = (colours or {}).get(name, (90, 90, 90))
            Image.new("RGB", size, colour).save(root / name, "JPEG", quality=95, subsampling=0)
            rows.append((name.split("/")[0], name, f"/src/{name}"))
        with open(root / "manifest.tsv", "w", newline="") as f:
            writer = csv.writer(f, delimiter="\t")
            writer.writerow(["batch", "preprocessed_path", "image_id"])
            writer.writerows(rows)
        store = LocalStore(root)
        self.addCleanup(store.close)
        return store

    def quiet_stderr(self) -> io.StringIO:
        patcher = mock.patch("sys.stderr", io.StringIO())
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_nothing_larger_than_the_bin_is_kept(self):
        calls: list[tuple[int, int]] = []
        grids, unloadable = pack_into_grids(
            self.store.manifest(), self.store, 1920, 1030, on_progress=lambda i, n: calls.append((i, n))
        )
        self.assertEqual(unloadable, [])
        self.assertEqual(sorted(k for gs in grids for k in gs.keys), ["big/a.jpg", "big/b.jpg", "big/c.jpg"])
        for gs in grids:
            w, h = gs.surface.get_size()
            self.assertTrue(w <= 1920 and h <= 1030, (w, h))
        self.assertEqual(calls, [(1, 3), (2, 3), (3, 3)])

    def test_truncated_image_is_unloadable(self):
        self.quiet_stderr()
        path = self.root / "big/b.jpg"
        data = path.read_bytes()
        scan = data.index(b"\xff\xda")  # start of scan: everything before it is header
        path.write_bytes(data[: scan + (len(data) - scan) // 2])
        with Image.open(path) as im:  # the header still reads: the failure comes at decode time
            self.assertEqual(im.size, (100, 60))
        grids, unloadable = pack_into_grids(self.store.manifest()[1:], self.store, 1920, 1030)
        self.assertEqual(unloadable, ["big/b.jpg"])
        self.assertEqual([gs.keys for gs in grids], [["big/c.jpg"]])
        grids, unloadable = pack_into_grids(self.store.manifest()[1:2], self.store, 1920, 1030)
        self.assertEqual((grids, unloadable), ([], ["big/b.jpg"]))  # a bin left with no keys is dropped

    def test_unreadable_header_is_unloadable(self):
        stderr = self.quiet_stderr()
        (self.root / "big/b.jpg").write_bytes(b"not a jpeg")
        calls: list[tuple[int, int]] = []
        with mock.patch.object(grid_packer_module, "load_surface", wraps=load_surface) as decode:
            grids, unloadable = pack_into_grids(
                self.store.manifest(), self.store, 1920, 1030, on_progress=lambda i, n: calls.append((i, n))
            )
        self.assertEqual(unloadable, ["big/b.jpg"])
        self.assertNotIn("big/b.jpg", {k for gs in grids for k in gs.keys})
        self.assertEqual(decode.call_count, 2)  # never decoded: left out at the header
        self.assertIn("cannot load big/b.jpg", stderr.getvalue())
        self.assertEqual(calls[-1], (3, 3))

    def test_decodes_one_bin_at_a_time(self):
        """Each 300x250 image fills its own 400x300 bin: when one is decoded, no earlier one is alive."""
        names = [f"many/{i}.jpg" for i in range(6)]
        store = self.make_store(dict.fromkeys(names, (300, 250)))
        decoded: list[weakref.ref] = []
        live_at_decode: list[int] = []

        def counting_load(buf: bytes) -> pg.Surface:
            gc.collect()
            live_at_decode.append(sum(ref() is not None for ref in decoded))
            surface = load_surface(buf)
            decoded.append(weakref.ref(surface))
            return surface

        with mock.patch.object(grid_packer_module, "load_surface", counting_load):
            grids, unloadable = pack_into_grids(store.manifest(), store, 400, 300)
        self.assertEqual(unloadable, [])
        self.assertEqual(sorted(gs.keys[0] for gs in grids), names)
        self.assertEqual([len(gs.keys) for gs in grids], [1] * 6)
        self.assertEqual(live_at_decode, [0] * 6)

    def test_every_key_covers_its_fit_size(self):
        """Each grid key's colour covers its fit_size area: shrunk, rotated and small images are all fully drawn."""
        images = {
            "pix/upright.jpg": ((3000, 2500), (200, 40, 40)),  # shrunk, upright
            "pix/rotates.jpg": ((1000, 1800), (40, 200, 40)),  # fits only rotated (or shrunk without rotation)
            "pix/shrunk_rotated.jpg": ((1500, 2500), (40, 40, 200)),  # fits as 1030x1716, placed rotated
            "pix/s1.jpg": ((100, 60), (200, 200, 40)),
            "pix/s2.jpg": ((60, 100), (200, 40, 200)),
            "pix/s3.jpg": ((120, 80), (40, 200, 200)),
        }
        store = self.make_store({n: size for n, (size, _) in images.items()}, {n: c for n, (_, c) in images.items()})
        for rot in (True, False):
            with self.subTest(allow_rotation=rot):
                grids, left_out = pack_into_grids(store.manifest(), store, 1920, 1030, allow_rotation=rot)
                self.assertEqual(left_out, [])
                self.assertEqual(sorted(k for gs in grids for k in gs.keys), sorted(images))
                for gs in grids:
                    for k in gs.keys:
                        (w, h), colour = images[k]
                        fw, fh = fit_size(w, h, 1920, 1030, rot)
                        count = pg.mask.from_threshold(gs.surface, colour, (10, 10, 10, 255)).count()
                        self.assertAlmostEqual(count / (fw * fh), 1, delta=0.02, msg=k)

    def test_decoded_size_differing_from_header_is_left_out(self):
        stderr = self.quiet_stderr()
        bad = (self.root / "big/a.jpg").read_bytes()

        def wrong_size(buf: bytes) -> pg.Surface:
            return pg.Surface((7, 5), 0, 24) if buf == bad else load_surface(buf)

        calls: list[tuple[int, int]] = []
        with mock.patch.object(grid_packer_module, "load_surface", wrong_size):
            grids, left_out = pack_into_grids(
                self.store.manifest(), self.store, 1920, 1030, on_progress=lambda i, n: calls.append((i, n))
            )
        self.assertEqual(left_out, ["big/a.jpg"])
        self.assertNotIn("big/a.jpg", {k for gs in grids for k in gs.keys})
        self.assertIn("cannot load big/a.jpg", stderr.getvalue())
        self.assertEqual(calls[-1], (3, 3))

    def test_key_left_unpacked_is_left_out(self):
        stderr = self.quiet_stderr()
        calls: list[tuple[int, int]] = []
        with dropping_packer(1):
            grids, left_out = pack_into_grids(
                self.store.manifest(), self.store, 1920, 1030, on_progress=lambda i, n: calls.append((i, n))
            )
        self.assertEqual(left_out, ["big/b.jpg"])
        self.assertEqual(sorted(k for gs in grids for k in gs.keys), ["big/a.jpg", "big/c.jpg"])
        self.assertIn("big/b.jpg was not packed", stderr.getvalue())
        self.assertEqual(calls[-1], (3, 3))

    def test_fit_size_rotated_orientation_wins(self):
        self.assertEqual(fit_size(1500, 2500, 1920, 1030, True), (1030, 1716))

    def test_fit_size_always_fits_an_allowed_orientation(self):
        for w, h in [(1921, 1), (1, 1031), (3000, 2500), (1920, 1031), (1921, 1030), (7, 4000), (4000, 7), (1031, 1921), (999, 1999)]:
            for rot in (False, True):
                with self.subTest(w=w, h=h, rot=rot):
                    nw, nh = fit_size(w, h, 1920, 1030, rot)
                    self.assertTrue(nw >= 1 and nh >= 1)
                    self.assertTrue((nw <= 1920 and nh <= 1030) or (rot and nw <= 1030 and nh <= 1920))

    def test_oversize_image_keeps_aspect_ratio(self):
        self.assertEqual(fit_size(3000, 2500, 1920, 1030, False), (1236, 1030))
        self.assertEqual(fit_size(3000, 2500, 1920, 1030, True), (1236, 1030))
        self.assertEqual(fit_size(1000, 1800, 1920, 1030, True), (1000, 1800))  # fits only rotated: untouched
        self.assertEqual(fit_size(1000, 1800, 1920, 1030, False), (572, 1030))

class TestDwell(unittest.TestCase):
    def test_dwell_elapsed(self):
        self.assertFalse(_dwell_elapsed(None, 10_000))
        self.assertFalse(_dwell_elapsed(1_000, 1_000 + MIN_DWELL_MS - 1))
        self.assertTrue(_dwell_elapsed(1_000, 1_000 + MIN_DWELL_MS))


class TestNextIndex(unittest.TestCase):
    def test_steps_forward_and_back(self):
        self.assertEqual(next_index(4, 1, 1, is_todo=None, wrap=False), 2)
        self.assertEqual(next_index(4, 1, -1, is_todo=None, wrap=False), 0)

    def test_stops_at_either_end_without_wrap(self):
        self.assertIsNone(next_index(4, 3, 1, is_todo=None, wrap=False))
        self.assertIsNone(next_index(4, 0, -1, is_todo=None, wrap=False))
        self.assertIsNone(next_index(1, 0, 1, is_todo=None, wrap=False))

    def test_wraps_at_either_end(self):
        self.assertEqual(next_index(4, 3, 1, is_todo=None, wrap=True), 0)
        self.assertEqual(next_index(4, 0, -1, is_todo=None, wrap=True), 3)

    def test_todo_only_without_wrap(self):
        todo = {0, 2}.__contains__
        self.assertEqual(next_index(4, 0, 1, is_todo=todo, wrap=False), 2)
        self.assertIsNone(next_index(4, 2, 1, is_todo=todo, wrap=False))  # 3 is not todo, then the end
        self.assertEqual(next_index(4, 2, -1, is_todo=todo, wrap=False), 0)
        self.assertIsNone(next_index(4, 0, -1, is_todo=todo, wrap=False))

    def test_todo_only_with_wrap(self):
        todo = {0, 2}.__contains__
        self.assertEqual(next_index(4, 2, 1, is_todo=todo, wrap=True), 0)
        self.assertEqual(next_index(4, 0, -1, is_todo=todo, wrap=True), 2)
        self.assertEqual(next_index(4, 1, 1, is_todo={1}.__contains__, wrap=True), 1)  # round to the cursor itself
        self.assertIsNone(next_index(4, 1, 1, is_todo=lambda i: False, wrap=True))

    def test_empty_list(self):
        for direction in (1, -1):
            for wrap in (False, True):
                with self.subTest(direction=direction, wrap=wrap):
                    self.assertIsNone(next_index(0, -1, direction, is_todo=None, wrap=wrap))
                    self.assertIsNone(next_index(0, -1, direction, is_todo=lambda i: True, wrap=wrap))

    def test_cursor_before_first_item(self):
        self.assertEqual(next_index(4, -1, 1, is_todo=None, wrap=False), 0)
        self.assertEqual(next_index(4, -1, 1, is_todo={2}.__contains__, wrap=False), 2)
        self.assertEqual(next_index(4, -1, 1, is_todo={3}.__contains__, wrap=False), 3)
        self.assertEqual(next_index(4, -1, -1, is_todo=None, wrap=False), 2)  # the inherited formula's result; unreachable in production
        self.assertEqual(next_index(1, -1, -1, is_todo=None, wrap=False), 0)


class TestNextBatch(unittest.TestCase):
    BATCHES = ("b1", "b2", "b3", "b4")

    def check(self, current: str | None, with_rows: set[str], *, wrap: bool) -> str | None:
        return next_batch(list(self.BATCHES), current, with_rows.__contains__, wrap=wrap)

    def test_first_later_batch_with_rows(self):
        self.assertEqual(self.check("b1", {"b1", "b3", "b4"}, wrap=False), "b3")
        self.assertEqual(self.check("b1", {"b1", "b3", "b4"}, wrap=True), "b3")

    def test_without_wrap_stops_after_the_last_batch(self):
        self.assertIsNone(self.check("b4", set(self.BATCHES), wrap=False))
        self.assertIsNone(self.check("b2", {"b1", "b2"}, wrap=False))

    def test_wrap_goes_round_to_earlier_batches_then_the_current_one(self):
        self.assertEqual(self.check("b3", {"b1", "b2"}, wrap=True), "b1")
        self.assertEqual(self.check("b3", {"b3"}, wrap=True), "b3")
        self.assertIsNone(self.check("b3", set(), wrap=True))

    def test_no_current_searches_from_the_first(self):
        for wrap in (False, True):
            with self.subTest(wrap=wrap):
                self.assertEqual(self.check(None, {"b2", "b4"}, wrap=wrap), "b2")
                self.assertIsNone(self.check(None, set(), wrap=wrap))

    def test_unknown_current_searches_from_the_first(self):
        self.assertEqual(self.check("b9", {"b1"}, wrap=False), "b1")

    def test_no_batches(self):
        self.assertIsNone(next_batch([], None, lambda b: True, wrap=True))


class TestPureFunctions(StoreTestCase):
    """Pass 2 with every status present: a FLAGGED, b CLEAN, c DIRTY, d UNREVIEWED."""

    def setUp(self):
        super().setUp()
        self.store.mark(["batch_001/a.jpg", "batch_002/c.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_001/b.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        self.store.mark(["batch_002/c.jpg"], "DIRTY", 2, reviewer="tester", mode="single")
        self.rows = self.store.manifest()
        self.statuses = self.store.statuses(2)

    def test_statuses(self):
        self.assertEqual(
            self.statuses,
            {"batch_001/a.jpg": "FLAGGED", "batch_001/b.jpg": "CLEAN", "batch_002/c.jpg": "DIRTY", "batch_002/d.jpg": "UNREVIEWED"},
        )

    def test_filter(self):
        expected = {
            ("unreviewed", None): ["batch_001/a.jpg", "batch_002/d.jpg"],
            ("unreviewed", "batch_001"): ["batch_001/a.jpg"],
            ("unreviewed", "batch_002"): ["batch_002/d.jpg"],
            ("clean", None): ["batch_001/b.jpg"],
            ("clean", "batch_001"): ["batch_001/b.jpg"],
            ("clean", "batch_002"): [],
            ("all", None): ["batch_001/a.jpg", "batch_001/b.jpg", "batch_002/c.jpg", "batch_002/d.jpg"],
            ("all", "batch_001"): ["batch_001/a.jpg", "batch_001/b.jpg"],
            ("all", "batch_002"): ["batch_002/c.jpg", "batch_002/d.jpg"],
        }
        for (status_filter, batch), keys in expected.items():
            with self.subTest(status_filter=status_filter, batch=batch):
                self.assertEqual([r.key for r in filter_rows(self.rows, self.statuses, status_filter, batch)], keys)

    def test_summaries(self):
        self.assertEqual(
            batch_summary(self.rows, self.statuses),
            {
                "batch_001": {"CLEAN": 1, "DIRTY": 0, "UNREVIEWED": 0, "FLAGGED": 1, "total": 2},
                "batch_002": {"CLEAN": 0, "DIRTY": 1, "UNREVIEWED": 1, "FLAGGED": 0, "total": 2},
            },
        )
        self.assertEqual(summary(self.rows, self.statuses), {"CLEAN": 1, "DIRTY": 1, "UNREVIEWED": 1, "FLAGGED": 1, "total": 4})


THIS_BOOT = boot_id()


class LockTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        make_work_dir(self.work_dir)
        self.lock_path = self.work_dir / LOCK_NAME

    def open(self, **kwargs) -> LocalStore:
        store = LocalStore(self.work_dir, **kwargs)
        self.addCleanup(store.close)
        return store

    def write_lock(self, host: str, pid: int, user: str = "alice", boot: str | None = THIS_BOOT) -> None:
        record = {"host": host, "user": user, "pid": pid, "started": "2026-09-30T12:00:00Z"}
        if boot is not None:
            record["boot_id"] = boot
        self.lock_path.write_text(json.dumps(record))

    def holder(self) -> dict:
        return json.loads(self.lock_path.read_text())


def finished_pid() -> int:
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True)
    return int(finished.stdout)


class TestWorkDirLock(LockTestCase):
    def test_second_writer_refused_until_first_closes(self):
        first = self.open()
        self.assertEqual(self.holder()["pid"], os.getpid())
        with self.assertRaises(WorkDirLocked) as ctx:
            LocalStore(self.work_dir)
        self.assertIn(str(self.lock_path), str(ctx.exception))
        self.assertIn(f"pid {os.getpid()}", str(ctx.exception))
        first.close()
        self.assertFalse(self.lock_path.exists())
        with LocalStore(self.work_dir) as second:
            self.assertTrue(self.lock_path.exists())
            second.mark([ROWS[0][1]], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertFalse(self.lock_path.exists())

    def test_acquire_leaves_only_the_complete_lock(self):
        self.open()
        self.assertEqual([p.name for p in self.work_dir.iterdir() if p.name.startswith(LOCK_NAME)], [LOCK_NAME])
        self.assertEqual(set(self.holder()), {"host", "boot_id", "user", "pid", "started"})

    def test_link_reply_lost_on_nfs_counts_as_acquired(self):
        real_link = os.link

        def link_then_fail(src, dst):
            real_link(src, dst)
            raise OSError(5, "simulated lost reply")

        with mock.patch("os.link", link_then_fail):
            self.open()
        self.assertEqual(self.holder()["pid"], os.getpid())

    @unittest.skipUnless(THIS_BOOT, "no boot_id on this platform")
    def test_stale_lock_on_this_machine_is_reclaimed(self):
        self.write_lock(socket.gethostname(), finished_pid())
        self.open()
        self.assertEqual(self.holder()["pid"], os.getpid())

    def test_same_hostname_other_boot_or_old_lock_is_refused(self):
        for boot in ("another-boot-id", "", None):
            with self.subTest(boot=boot):
                self.write_lock(socket.gethostname(), finished_pid(), boot=boot)
                with self.assertRaises(WorkDirLocked) as ctx:
                    LocalStore(self.work_dir)
                self.assertIn("by hand", str(ctx.exception))
                self.assertEqual(self.holder()["user"], "alice")

    def test_lock_from_another_host_is_refused(self):
        self.write_lock("node042", 1234)
        with self.assertRaises(WorkDirLocked) as ctx:
            LocalStore(self.work_dir)
        message = str(ctx.exception)
        for part in ("alice", "node042", "pid 1234", "2026-09-30T12:00:00Z", str(self.lock_path), "by hand"):
            self.assertIn(part, message)
        self.assertEqual(self.holder()["host"], "node042")

    def test_live_process_of_another_user_is_refused(self):
        self.write_lock(socket.gethostname(), 1234, user="bob")
        with mock.patch("os.kill", side_effect=PermissionError) as kill, self.assertRaises(WorkDirLocked) as ctx:
            LocalStore(self.work_dir)
        if THIS_BOOT:
            kill.assert_called_once_with(1234, 0)
        for part in ("bob", "pid 1234", "2026-09-30T12:00:00Z", "by hand"):  # pid reuse: the user may still need to remove it
            self.assertIn(part, str(ctx.exception))
        self.assertEqual(self.holder()["pid"], 1234)

    @unittest.skipUnless(THIS_BOOT, "no boot_id on this platform")
    def test_concurrent_reclaimers_cannot_both_acquire(self):
        # tmpfs and disk filesystems reuse freed inode numbers differently; run on both where possible
        roots = [None, *([Path.home()] if os.access(Path.home(), os.W_OK) else [])]
        for root in roots:
            with self.subTest(root=root):
                work_dir = Path(tempfile.mkdtemp(dir=root, prefix=".image-review-lock-test-"))
                self.addCleanup(shutil.rmtree, work_dir, ignore_errors=True)
                make_work_dir(work_dir)
                self.assert_one_reclaimer_wins(work_dir)

    def assert_one_reclaimer_wins(self, work_dir: Path) -> None:
        lock_path = work_dir / LOCK_NAME
        stale = {"host": socket.gethostname(), "boot_id": THIS_BOOT, "user": "alice", "pid": finished_pid(), "started": "t"}
        lock_path.write_text(json.dumps(stale))
        real_is_stale = store_module.is_stale
        a_started = False
        winners: list[LocalStore] = []

        def a_reclaims_first(holder, me):
            nonlocal a_started
            if not a_started:  # B has read the stale lock (and holds it open); A reclaims and re-creates it now
                a_started = True
                winners.append(LocalStore(work_dir))  # A's own is_stale call goes straight through
            return real_is_stale(holder, me)

        with mock.patch.object(store_module, "is_stale", a_reclaims_first), self.assertRaises(WorkDirLocked):
            LocalStore(work_dir)  # B
        self.assertEqual(len(winners), 1)
        self.assertTrue(lock_path.exists())  # A's lock survived B's stale decision
        winners[0].close()
        self.assertFalse(lock_path.exists())

    @mock.patch.object(store_module, "EMPTY_LOCK_WAIT", 0.1)
    def test_corrupt_lock_is_refused(self):
        for text in ("", "not json", "[]", '{"host": "h", "user": "u", "pid": "1", "started": "t"}', '{"host": "h", "user": "u", "pid": 0, "started": "t"}'):
            with self.subTest(text=text):
                self.lock_path.write_text(text)
                with self.assertRaises(WorkDirLocked) as ctx:
                    LocalStore(self.work_dir)
                self.assertIn(str(self.lock_path), str(ctx.exception))
                self.assertIn("corrupt", str(ctx.exception))
                self.assertIn("by hand", str(ctx.exception))
                self.assertEqual(self.lock_path.read_text(), text)

    def test_hard_links_unsupported_falls_back_to_direct_create(self):
        for err in (errno.EPERM, errno.ENOTSUP, errno.ENOSYS):
            with self.subTest(errno=errno.errorcode[err]):
                with mock.patch("os.link", side_effect=OSError(err, os.strerror(err))):
                    store = LocalStore(self.work_dir)
                self.assertEqual(self.holder()["pid"], os.getpid())
                self.assertEqual(set(self.holder()), {"host", "boot_id", "user", "pid", "started"})
                self.assertEqual([p.name for p in self.work_dir.iterdir() if p.name.startswith(LOCK_NAME)], [LOCK_NAME])
                with mock.patch("os.link", side_effect=OSError(err, os.strerror(err))), self.assertRaises(WorkDirLocked):
                    LocalStore(self.work_dir)
                store.close()
                self.assertFalse(self.lock_path.exists())

    def test_empty_lock_filled_within_wait_is_held(self):
        self.lock_path.write_text("")
        real_sleep = time.sleep

        def writer_finishes(seconds):  # the direct-create writer fills in its record while we wait
            self.write_lock("node042", 1234)
            real_sleep(seconds)

        with mock.patch("time.sleep", side_effect=writer_finishes), self.assertRaises(WorkDirLocked) as ctx:
            LocalStore(self.work_dir)
        self.assertEqual((ctx.exception.holder.host, ctx.exception.holder.pid), ("node042", 1234))
        self.assertNotIn("corrupt", str(ctx.exception))

    def test_sweeps_leftover_siblings_of_dead_processes(self):
        host, dead, live = socket.gethostname(), finished_pid(), os.getppid()
        names = {
            "dead": f"{LOCK_NAME}.{host}.{THIS_BOOT or '-'}.{dead}.abcd",
            "live": f"{LOCK_NAME}.{host}.{THIS_BOOT or '-'}.{live}.abcd",
            "other_boot": f"{LOCK_NAME}.{host}.another-boot.{dead}.abcd",
            "other_host": f"{LOCK_NAME}.node042.{THIS_BOOT or '-'}.{dead}.abcd",
        }
        for name in names.values():
            (self.work_dir / name).write_text("{}")
        self.open()
        left = {key for key, name in names.items() if (self.work_dir / name).exists()}
        self.assertEqual(left, {"live", "other_boot", "other_host"} if THIS_BOOT else set(names))

    def test_user_without_passwd_entry_falls_back_to_uid(self):
        with mock.patch("getpass.getuser", side_effect=KeyError("getpwuid(): uid not found")):
            self.open()
        self.assertEqual(self.holder()["user"], str(os.getuid()))

    def test_read_only_ignores_lock_and_refuses_mark(self):
        self.open()
        reader = self.open(read_only=True)
        self.assertEqual(set(reader.statuses(1).values()), {"UNREVIEWED"})
        with self.assertRaises(PermissionError):
            reader.mark([ROWS[0][1]], "CLEAN", 1, reviewer="tester", mode="single")
        reader.close()
        self.assertTrue(self.lock_path.exists())  # the writer's lock is untouched

    def test_closed_store_refuses_mark(self):
        store = self.open()
        store.close()
        with self.assertRaises(PermissionError):
            store.mark([ROWS[0][1]], "CLEAN", 1, reviewer="tester", mode="single")

    def test_read_only_and_closed_stores_refuse_undo(self):
        writer = self.open()
        writer.mark([ROWS[0][1]], "CLEAN", 1, reviewer="tester", mode="single")
        reader = self.open(read_only=True)
        with self.assertRaises(PermissionError):
            reader.undo(1, reviewer="tester")
        reader.close()
        writer.close()
        with self.assertRaises(PermissionError):
            writer.undo(1, reviewer="tester")
        self.assertEqual(self.open(read_only=True).statuses(1)[ROWS[0][1]], "CLEAN")

    def test_close_is_idempotent_and_keeps_another_process_lock(self):
        store = self.open()
        store.close()
        store.close()
        self.assertFalse(self.lock_path.exists())
        store = self.open()
        self.write_lock(socket.gethostname(), os.getpid() + 1)  # e.g. reclaimed by another process
        store.close()
        self.assertEqual(self.holder()["pid"], os.getpid() + 1)

    def test_bad_manifest_takes_no_lock_and_bad_review_releases_it(self):
        (self.work_dir / "review.tsv").write_text("bad header\n")
        with self.assertRaises(ValueError):
            LocalStore(self.work_dir)
        self.assertFalse(self.lock_path.exists())
        with mock.patch.object(store_module, "acquire_lock") as acquire:
            (self.work_dir / "manifest.tsv").write_text("bad header\n")
            with self.assertRaises(ValueError):
                LocalStore(self.work_dir)
        acquire.assert_not_called()

    def test_lock_file_mode_follows_policy(self):
        for dir_mode, file_mode in ((0o700, 0o600), (0o2770, 0o660)):
            with self.subTest(dir_mode=oct(dir_mode)):
                os.chmod(self.work_dir, dir_mode)
                with LocalStore(self.work_dir):
                    self.assertEqual(stat.S_IMODE(self.lock_path.stat().st_mode), file_mode)


class TestLockCli(LockTestCase):
    def invoke(self, *args, env=None):
        env = {"IMAGE_REVIEW_REMOTE": None, "IMAGE_REVIEW_VIA": None, "IMAGE_REVIEW_ACCESS": None, "IMAGE_REVIEW_REVIEWER": None, **(env or {})}
        return CliRunner().invoke(cli, [*args, "--work-dir", str(self.work_dir)], env=env)

    def test_review_on_locked_dir_exits_1(self):
        self.write_lock("node042", 1234)
        with mock.patch("image_review.controller.ReviewSession") as session:
            result = self.invoke("review")
        self.assertEqual(result.exit_code, 1, result.output)
        session.assert_not_called()
        for part in ("alice", "node042", "pid 1234", str(self.lock_path)):
            self.assertIn(part, result.output)

    def test_review_holds_lock_while_running(self):
        seen = []
        with mock.patch("image_review.controller.ReviewSession") as session:
            session.return_value.run.side_effect = lambda: seen.append(self.holder()["pid"])
            result = self.invoke("review")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [os.getpid()])
        self.assertFalse(self.lock_path.exists())

    def test_review_hangup_releases_lock_and_restores_handler(self):
        before = signal.getsignal(signal.SIGHUP)
        with mock.patch("image_review.controller.ReviewSession") as session:
            session.return_value.run.side_effect = lambda: os.kill(os.getpid(), signal.SIGHUP)
            result = self.invoke("review")
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(self.lock_path.exists())
        self.assertEqual(signal.getsignal(signal.SIGHUP), before)

    def assert_rejected(self, *args):
        with mock.patch("image_review.controller.ReviewSession") as session, mock.patch("pygame.init") as init:
            result = self.invoke("review", *args)
        self.assertEqual(result.exit_code, 2, result.output)
        session.assert_not_called()
        init.assert_not_called()
        self.assertFalse(self.lock_path.exists())
        return result

    def test_review_rejects_nonpositive_pass(self):
        for value in ("0", "-1"):
            self.assert_rejected("--pass", value)

    def test_review_rejects_unknown_batch_listing_known(self):
        result = self.assert_rejected("--batch", "nope")
        self.assertIn("batch_001", result.output)
        self.assertIn("batch_002", result.output)

    def test_review_rejects_empty_batch(self):
        self.assert_rejected("--batch", "")

    def test_review_rejects_bad_reviewer(self):
        for value in ("a\tb", "a\nb", "", "  ", "r" * 65):
            with self.subTest(reviewer=value):
                result = self.assert_rejected("--reviewer", value)
                self.assertIn("--reviewer", result.output)
                self.assertIn("printable", result.output)

    def test_review_passes_reviewer_to_session(self):
        cases = [((), {}, "login"), (("--reviewer", "Dr. Lee"), {}, "Dr. Lee"), ((), {"IMAGE_REVIEW_REVIEWER": "env name"}, "env name")]
        for args, env, expected in cases:
            with self.subTest(args=args, env=env), mock.patch("image_review.controller.ReviewSession") as session, \
                    mock.patch("image_review.cli.getpass.getuser", return_value="login"):
                result = self.invoke("review", *args, env=env)
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(session.call_args.kwargs["reviewer"], expected)

    def test_review_migration_conflict_is_a_clean_error(self):
        with mock.patch.object(ReviewDB, "migrate", side_effect=RuntimeError("review.tsv changed since it was loaded")), \
                mock.patch("image_review.controller.ReviewSession") as session:
            result = self.invoke("review")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("changed since it was loaded", result.output)
        self.assertNotIn("Traceback", result.output)
        session.assert_not_called()
        self.assertFalse(self.lock_path.exists())

    def test_review_without_user_name_asks_for_reviewer(self):
        with mock.patch("image_review.cli.getpass.getuser", side_effect=OSError("no user")):
            result = self.assert_rejected()
        self.assertIn("--reviewer", result.output)

    def test_unknown_batch_message_lists_at_most_five(self):
        known = {f"batch_{i:03d}" for i in range(1, 9)}
        message = unknown_batch_message("x", known)
        self.assertIn("batch_005", message)
        self.assertNotIn("batch_006", message)
        self.assertIsNone(unknown_batch_message("batch_003", known))

    def test_review_valid_batch_reaches_session(self):
        with mock.patch("image_review.controller.ReviewSession") as session, mock.patch("pygame.init"), mock.patch("pygame.quit"):
            result = self.invoke("review", "--batch", "batch_002")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(session.call_args.kwargs["batch"], "batch_002")

    def test_review_rejects_unknown_filter(self):
        self.assert_rejected("--filter", "bogus")
        with mock.patch("image_review.controller.ReviewSession") as session, mock.patch("pygame.init"), mock.patch("pygame.quit"):
            result = self.invoke("review", "--filter", "clean")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(session.call_args.kwargs["status_filter"], "clean")

    def test_unwritable_work_dir_says_write(self):
        with mock.patch("os.open", side_effect=PermissionError(13, "Permission denied", str(self.lock_path) + ".x")):
            result = self.invoke("review")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn(f"Cannot write to work directory {self.work_dir}", result.output)

    def test_missing_manifest_message_kept(self):
        (self.work_dir / "manifest.tsv").unlink()
        result = self.invoke("review")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("No preprocessed data found", result.output)
        self.assertFalse(self.lock_path.exists())

    def test_status_counts_flagged_after_pass_one(self):
        with LocalStore(self.work_dir) as store:
            store.mark(["batch_001/a.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
            store.mark(["batch_001/b.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
            store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        result = self.invoke("status")
        self.assertEqual(result.exit_code, 0, result.output)
        for line in ("  CLEAN:           3", "  DIRTY:           0", "  UNREVIEWED:      0", "  FLAGGED:         1", "Current pass: 2"):
            self.assertIn(line + "\n", result.output)
        self.assertIn(f"{'Batch':<15} {'Total':>6} {'Clean':>6} {'Dirty':>6} {'Unrev':>6} {'Flag':>6}", result.output)
        self.assertIn(f"{'batch_001':<15} {2:>6} {1:>6} {0:>6} {0:>6} {1:>6}", result.output)

    def test_grid_review_names_held_back_images(self):
        with LocalStore(self.work_dir) as store:
            store.mark(["batch_001/a.jpg"], "DIRTY", 1, reviewer="tester", mode="single")
            store.mark(["batch_001/b.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
            store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN", 1, reviewer="tester", mode="single")
        with mock.patch.object(pg.display, "toggle_fullscreen", lambda: None):
            result = self.invoke("review", "--mode", "grid")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No grid items for pass 2; 1 FLAGGED/DIRTY image needs single-mode review (--mode single)", result.output)
        self.assertNotIn("No images to review", result.output)

    def test_status_works_on_locked_dir(self):
        self.write_lock("node042", 1234)
        result = self.invoke("status")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("UNREVIEWED:", result.output)
        self.assertEqual(self.holder()["host"], "node042")

if __name__ == "__main__":
    unittest.main()
