import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame as pg
from fixtures import ROWS, make_work_dir

from image_review.controller import ReviewSession
from image_review.review_db import ReviewDB
from image_review.store import (
    LocalStore,
    ManifestRow,
    SkippedCounts,
    batch_summary,
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
        self.store.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "batch_001", "CLEAN", 1)
        self.store.mark(["batch_002/c.jpg"], "batch_002", "DIRTY", 1)
        statuses = LocalStore(self.work_dir).statuses(1)
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
        self.store.mark(["batch_001/a.jpg"], "batch_001", "CLEAN", 1)
        self.store.mark(["batch_001/b.jpg"], "batch_001", "DIRTY", 1)
        statuses = self.store.statuses(2)
        self.assertEqual(statuses["batch_001/a.jpg"], "CLEAN")
        self.assertEqual(statuses["batch_001/b.jpg"], "UNREVIEWED")


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
            changed = store.mark(["batch_001/a.jpg"], "batch_001", "CLEAN", 1)
            self.assertEqual(changed, {"batch_001/a.jpg": "CLEAN", "batch_001/b.jpg": "CLEAN"})
            statuses = store.statuses(1)
            self.assertEqual({k: statuses[k] for k in changed}, changed)
            self.assertEqual(statuses["batch_002/c.jpg"], "UNREVIEWED")


class TestMissingManifest(unittest.TestCase):
    def test_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(FileNotFoundError):
            LocalStore(Path(tmp))


class TestSession(StoreTestCase):
    def setUp(self):
        super().setUp()
        pg.init()
        self.addCleanup(pg.quit)
        patcher = mock.patch.object(pg.display, "toggle_fullscreen", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_single_mode_mark_updates_snapshot(self):
        s = ReviewSession(self.store, mode="single")
        s._cursor = 0
        key = s._items[0].key
        before = s._todo_count
        s._mark("CLEAN")
        self.assertEqual(s._statuses[key], "CLEAN")
        self.assertEqual(s._todo_count, before - 1)

    def test_grid_mode_mark_updates_all_keys(self):
        s = ReviewSession(self.store, mode="grid")
        s._cursor = 0
        keys = s._items[0]["keys"]
        s._mark("DIRTY")
        self.assertTrue(keys)
        self.assertEqual({s._statuses[k] for k in keys}, {"DIRTY"})

    def test_restart_refetches_statuses(self):
        s = ReviewSession(self.store, mode="single")
        self.store.mark(["batch_001/a.jpg"], "batch_001", "CLEAN", s.pass_number)
        self.assertEqual(s._statuses["batch_001/a.jpg"], "UNREVIEWED")
        s._restart_in_mode("grid")
        self.assertEqual(s._statuses["batch_001/a.jpg"], "CLEAN")


class TestPureFunctions(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store.mark(["batch_001/a.jpg"], "batch_001", "CLEAN", 1)
        self.store.mark(["batch_002/c.jpg"], "batch_002", "DIRTY", 1)
        self.db = ReviewDB(self.work_dir)
        self.raw = [{"batch": b, "preprocessed_path": k, "image_id": i} for b, k, i in ROWS]
        self.rows = self.store.manifest()
        self.statuses = self.store.statuses(1)

    def test_filter_matches_review_db(self):
        for status_filter in ("unreviewed", "clean", "all"):
            for batch in (None, "batch_001", "batch_002"):
                with self.subTest(status_filter=status_filter, batch=batch):
                    expected = [r["preprocessed_path"] for r in self.db.images_by_status(self.raw, 1, status_filter, batch)]
                    actual = [r.key for r in filter_rows(self.rows, self.statuses, status_filter, batch)]
                    self.assertEqual(actual, expected)

    def test_filter_rejects_bad_filter(self):
        with self.assertRaises(ValueError):
            filter_rows(self.rows, self.statuses, "bogus")

    def test_summaries_match_review_db(self):
        self.assertEqual(batch_summary(self.rows, self.statuses), self.db.batch_summary(self.raw, 1))
        self.assertEqual(summary(self.rows, self.statuses), self.db.summary(self.raw, 1))


if __name__ == "__main__":
    unittest.main()
