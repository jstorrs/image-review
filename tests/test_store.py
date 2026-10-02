import contextlib
import csv
import errno
import hashlib
import io
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import numpy as np
import pygame as pg
import skimage as ski

from image_review import review_db as review_db_module
from image_review.connection import package_version
from image_review.controller import ReviewSession
from image_review.review_db import HEADER, ReviewDB
from image_review.store import (
    LOCK_NAME,
    LocalStore,
    ManifestEntry,
    ManifestRow,
    SkippedCounts,
    batch_summary,
    filter_rows,
    load_manifest,
    summary,
)
from image_review.util import load_surface
from tests.fixtures import ROWS, StoreTestCase, _jpeg_bytes, make_work_dir


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
        self.write('image_id\tkind\treason\na\tfailed\tx\nb\tignored\ty\nc\tfailed\t"multi\nline"\n')
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
            {
                "batch_001/a.jpg": "FLAGGED",
                "batch_001/b.jpg": "CLEAN",
                "batch_002/c.jpg": "CLEAN",
                "batch_002/d.jpg": "CLEAN",
            },
        )
        self.assertEqual(
            self.store.mark(["batch_001/a.jpg"], "CLEAN", 2, reviewer="tester", mode="single"),
            {"batch_001/a.jpg": "CLEAN"},
        )
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
            self.assertEqual(
                store.undo(1, reviewer="tester"), {"batch_001/a.jpg": "UNREVIEWED", "batch_001/b.jpg": "UNREVIEWED"}
            )
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
        for header in (
            "image_id\tbatch\tstatus\tpass_number",
            "image_id\tbatch\tstatus\tpass_number\ttimestamp\textra",
        ):
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
            "FLAGGED status": (
                self.NEW_HEADER,
                good,
                f"/src/b.dcm\tbatch_001\tFLAGGED\t1\t{ts}\talice\tsingle\t1\t0.2.0",
            ),
            "FLAGGED in an undo row": (
                self.NEW_HEADER,
                good,
                f"/src/b.dcm\tbatch_001\tFLAGGED\t1\t{ts}\talice\tundo\t1\t0.2.0",
            ),
            "UNREVIEWED outside an undo row": (
                self.NEW_HEADER,
                good,
                f"/src/b.dcm\tbatch_001\tUNREVIEWED\t1\t{ts}\talice\tsingle\t1\t0.2.0",
            ),
            "UNREVIEWED in a migrated row": (
                self.NEW_HEADER,
                good,
                f"/src/b.dcm\tbatch_001\tUNREVIEWED\t1\t{ts}\t\t\t\t",
            ),
            "bad grid_size": (
                self.NEW_HEADER,
                good,
                f"/src/b.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tgrid\tfour\t0.2.0",
            ),
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

    HASHED_HEADER = "batch\tpreprocessed_path\timage_id\tsource_sha256\tjpeg_sha256"
    SOURCE_HASH = hashlib.sha256(b"source").hexdigest()
    JPEG_HASH = hashlib.sha256(b"jpeg").hexdigest()

    def test_legacy_manifest_loads_without_hashes(self):
        self.write_manifest(self.MANIFEST_HEADER, "batch_001\tbatch_001/a.jpg\t/src/a.dcm")
        self.assertEqual(load_manifest(self.work_dir), [ManifestEntry("batch_001", "batch_001/a.jpg", "/src/a.dcm")])
        self.assertEqual(LocalStore(self.work_dir, read_only=True).image_bytes("batch_001/a.jpg")[:2], b"\xff\xd8")

    def test_hashed_manifest_loads_hashes(self):
        self.write_manifest(
            self.HASHED_HEADER, f"batch_001\tbatch_001/a.jpg\t/src/a.dcm\t{self.SOURCE_HASH}\t{self.JPEG_HASH}"
        )
        self.assertEqual(
            load_manifest(self.work_dir),
            [ManifestEntry("batch_001", "batch_001/a.jpg", "/src/a.dcm", self.SOURCE_HASH, self.JPEG_HASH)],
        )

    def test_malformed_hash_names_file_and_line(self):
        good = f"batch_001\tbatch_001/a.jpg\t/src/a.dcm\t{self.SOURCE_HASH}\t{self.JPEG_HASH}"
        row = "batch_001\tbatch_001/b.jpg\t/src/b.dcm\t{}\t{}".format
        cases = {
            "uppercase source": (row(self.SOURCE_HASH.upper(), self.JPEG_HASH), "source_sha256"),
            "short jpeg": (row(self.SOURCE_HASH, self.JPEG_HASH[:63]), "jpeg_sha256"),
            "long jpeg": (row(self.SOURCE_HASH, self.JPEG_HASH + "0"), "jpeg_sha256"),
            "non-hex source": (row("g" * 64, self.JPEG_HASH), "source_sha256"),
            "prefixed source": (row(f"sha256:{self.SOURCE_HASH[7:]}", self.JPEG_HASH), "source_sha256"),
            "trailing space in hash": (row(self.SOURCE_HASH, f"{self.JPEG_HASH} "), "jpeg_sha256"),
            # a quoted field spans two lines; csv reports the line it ends on
            "trailing newline in hash": (row(self.SOURCE_HASH, f'"{self.JPEG_HASH}\n"'), "4: jpeg_sha256"),
            "empty jpeg": (row(self.SOURCE_HASH, ""), "non-empty"),
            "legacy-width row": ("batch_001\tbatch_001/b.jpg\t/src/b.dcm", "expected 5"),
        }
        for name, (line, message) in cases.items():
            with self.subTest(name):
                self.write_manifest(self.HASHED_HEADER, good, line)
                with self.assertRaisesRegex(ValueError, rf"manifest\.tsv:(3: .*)?{message}"):
                    load_manifest(self.work_dir)

    def test_hashed_row_under_legacy_header_is_refused(self):
        self.write_manifest(
            self.MANIFEST_HEADER, f"batch_001\tbatch_001/a.jpg\t/src/a.dcm\t{self.SOURCE_HASH}\t{self.JPEG_HASH}"
        )
        with self.assertRaisesRegex(ValueError, r"manifest\.tsv:2: expected 3"):
            load_manifest(self.work_dir)


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
        with self.assertNoLogs("image_review"):
            return ReviewDB(self.work_dir)

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
        with self.assertLogs("image_review.review_db", "WARNING") as logs:
            db = ReviewDB(self.work_dir)
        self.assertIn("ignoring the unfinished last line", logs.records[0].getMessage())
        self.assertIn("review.tsv:3:", logs.records[0].getMessage())
        self.assertEqual(set(db._rows), {"x"})
        db.mark("z", "batch_001", "DIRTY", 1, reviewer="tester", mode="single")
        after = self.path.read_bytes()
        self.assertTrue(after.startswith(intact))
        self.assertTrue(after[len(intact) :].startswith(b"z\tbatch_001\tDIRTY\t1\t"))
        self.assertEqual(len(after.splitlines()), 3)  # header, x, z: the fragment is gone
        self.assertEqual(set(self.reload()._rows), {"x", "z"})

    def test_torn_last_line_that_parses_is_kept(self):
        ReviewDB(self.work_dir).mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        with open(self.path, "ab") as f:
            f.write(
                b"y\tbatch_001\tDIRTY\t1\t2026-01-01T00:00:00+00:00\tr\tsingle\t1\t0.1"
            )  # every field written, line ending not
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
        with self.assertLogs("image_review.review_db", "WARNING") as logs:
            db = ReviewDB(self.work_dir)
        self.assertIn("ignoring the unfinished last line", logs.records[0].getMessage())
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
        with (
            mock.patch.object(review_db_module.os, "write", short_then_full),
            mock.patch.object(review_db_module.os, "ftruncate", side_effect=OSError(errno.EIO, "I/O error")),
            self.assertRaises(OSError),
        ):
            db.mark_many(
                [("y", "batch_001"), ("z", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single"
            )  # the rollback fails too: the fragment stays
        db.mark("w", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(self.reload()._rows, db._rows)

    def test_stale_truncate_refuses(self):
        ReviewDB(self.work_dir).mark_many(
            [("x", "batch_001"), ("y", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
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
        lines = [
            "image_id\tbatch\tstatus\tpass_number\ttimestamp",
            f"a\tb\tCLEAN\t1\t{ts}",
            f"bad\tb\tdirty\t1\t{ts}",
            f"c\tb\tDIRTY\t2\t{ts}",
        ]
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
        self.store.mark(
            ["batch_001/b.jpg", "batch_002/c.jpg", "batch_002/d.jpg"], "DIRTY", 1, reviewer="Bob Q", mode="grid"
        )
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
        return [[*line.split("\t"), "", "", "", ""] for line in self.OLD_ROWS]

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
        self.assertEqual(
            [p.name for p in self.work_dir.iterdir() if "review.tsv" in p.name], ["review.tsv"]
        )  # no temp left
        migrated, inode = self.path.read_bytes(), self.path.stat().st_ino
        with LocalStore(self.work_dir) as store:
            pass
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_ino), (migrated, inode))

    def test_appends_after_migration_use_new_columns(self):
        with LocalStore(self.work_dir) as store:
            store.mark(["batch_002/c.jpg"], "DIRTY", 1, reviewer="alice", mode="single")
        _, rows = self.stored_rows()
        self.assertEqual(rows[:3], self.expected_rows())
        self.assertEqual(
            rows[3][:3] + rows[3][5:8], ["/src/patient_lee/c.dcm", "batch_002", "DIRTY", "alice", "single", "1"]
        )

    def test_read_only_store_leaves_old_file_alone(self):
        inode = self.path.stat().st_ino
        store = LocalStore(self.work_dir, read_only=True)
        self.assertEqual(store.statuses(1)["batch_001/b.jpg"], "CLEAN")
        self.assertEqual(store.current_pass(), 1)  # c and d unreviewed
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_ino), (self.old, inode))

    def test_torn_tail_of_old_file_is_dropped_by_migration(self):
        with open(self.path, "ab") as f:
            f.write(b"/src/patient_lee/c.dcm\tbatch_0")
        with self.assertLogs("image_review.review_db", "WARNING") as logs, LocalStore(self.work_dir) as store:
            store.mark(["batch_002/d.jpg"], "CLEAN", 1, reviewer="alice", mode="single")
        self.assertIn("unfinished last line", logs.records[0].getMessage())
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

        for name, attr, fake, exc in [
            ("fsync", "fsync", fail_fsync, OSError),
            ("replace", "replace", interrupt_replace, KeyboardInterrupt),
        ]:
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
        with self.assertLogs("image_review.review_db", "WARNING") as logs:
            ro = LocalStore(self.work_dir, read_only=True)
        self.assertIn("ignoring the unfinished last line", logs.records[0].getMessage())
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
            {
                k: row[k]
                for k in ("image_id", "batch", "status", "pass_number", "reviewer", "mode", "grid_size", "tool_version")
            },
            {
                "image_id": ROWS[0][2],
                "batch": "batch_001",
                "status": "UNREVIEWED",
                "pass_number": "1",
                "reviewer": "bob",
                "mode": "undo",
                "grid_size": "1",
                "tool_version": package_version(),
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
        with (
            mock.patch.object(ReviewDB, "_append", side_effect=OSError(errno.ENOSPC, "full")),
            self.assertRaises(OSError),
        ):
            self.store.undo(1, reviewer="alice")
        with self.assertRaises(ValueError):
            self.store.undo(1, reviewer="a\tb")
        self.assertEqual(self.store.statuses(1)[self.A], "CLEAN")
        self.assertEqual(self.store.undo(1, reviewer="alice"), {self.A: "UNREVIEWED"})

    def test_failed_mark_is_not_pushed(self):
        self.store.mark([self.A], "CLEAN", 1, reviewer="alice", mode="single")
        with (
            mock.patch.object(ReviewDB, "_append", side_effect=OSError(errno.ENOSPC, "full")),
            self.assertRaises(OSError),
        ):
            self.store.mark([self.B], "DIRTY", 1, reviewer="alice", mode="single")
        self.assertEqual(self.store.undo(1, reviewer="alice"), {self.A: "UNREVIEWED"})
        self.assertEqual(self.store.undo(1, reviewer="alice"), {})

    def test_mark_many_still_refuses_unreviewed(self):
        with self.assertRaises(ValueError):
            self.store.mark([self.A], "UNREVIEWED", 1, reviewer="alice", mode="single")  # type: ignore[arg-type]
        self.assertFalse((self.work_dir / "review.tsv").exists())


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
            {
                "batch_001/a.jpg": "FLAGGED",
                "batch_001/b.jpg": "CLEAN",
                "batch_002/c.jpg": "DIRTY",
                "batch_002/d.jpg": "UNREVIEWED",
            },
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
        self.assertEqual(
            summary(self.rows, self.statuses), {"CLEAN": 1, "DIRTY": 1, "UNREVIEWED": 1, "FLAGGED": 1, "total": 4}
        )


if __name__ == "__main__":
    unittest.main()
