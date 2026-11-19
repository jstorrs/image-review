import contextlib
import csv
import dataclasses
import errno
import hashlib
import io
import os
import stat
import tempfile
import types
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from image_review import review_db as review_db_module
from image_review.connection import MAX_PASS, package_version
from image_review.export import split_allowlist
from image_review.lock import LOCK_NAME
from image_review.review_db import HEADER, Decision, ReviewDB, ReviewLogChanged
from image_review.status import TODO_STATUSES, GridCleanRefused, ImageId
from image_review.store import (
    LocalStore,
    ManifestEntry,
    ManifestRow,
    NoteRow,
    SkippedCounts,
    SkippedRow,
    batch_summary,
    load_manifest,
    load_notes,
    load_skipped,
    skipped_counts,
    summary,
)
from tests.fixtures import ROWS, StoreTestCase, make_work_dir, mark, recorded_dir_fsyncs, temp_dir


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


class TestSkipped(StoreTestCase):
    def write(self, text: str) -> None:
        (self.work_dir / "skipped.tsv").write_text(text)

    def test_absent_is_zero(self):
        self.assertEqual(self.store.skipped(), SkippedCounts(0, 0))
        self.assertEqual(load_skipped(self.work_dir), [])

    def test_oversized_field_names_file_and_line(self):
        self.write(f"image_id\tkind\treason\n/a\tfailed\t{'x' * 200_000}\n/b\tignored\tr\n")
        with self.assertRaisesRegex(ValueError, r"skipped\.tsv:2: field larger than field limit"):
            load_skipped(self.work_dir)

    def test_header_only_is_zero(self):
        self.write("image_id\tkind\treason\n")
        self.assertEqual(self.store.skipped(), SkippedCounts(0, 0))
        self.assertFalse(self.store.skipped().any)

    def test_counts_kinds(self):
        self.write('image_id\tkind\treason\na\tfailed\tx\nb\tignored\ty\nc\tfailed\t"multi\nline"\n')
        self.assertEqual(self.store.skipped(), SkippedCounts(failed=2, ignored=1))

    def test_skipped_counts_of_rows(self):
        rows = [SkippedRow(ImageId("a"), "failed", "x"), SkippedRow(ImageId("b"), "ignored", "y")]
        self.assertEqual(skipped_counts(rows + rows[:1]), SkippedCounts(failed=2, ignored=1))
        self.assertEqual(skipped_counts([]), SkippedCounts(0, 0))

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

    def test_non_utf8_names_file(self):
        (self.work_dir / "skipped.tsv").write_bytes(b"image_id\tkind\treason\n\xe9\tfailed\tx\n")
        with self.assertRaisesRegex(ValueError, r"skipped\.tsv: not valid UTF-8"):
            self.store.skipped()


class TestNotes(StoreTestCase):
    def write(self, text: str) -> None:
        (self.work_dir / "notes.tsv").write_text(text)

    def test_absent_is_empty_unless_required(self):
        self.assertEqual(load_notes(self.work_dir, required=False), [])
        with self.assertRaisesRegex(ValueError, r"notes\.tsv: missing; work directories made before 0\.4\.0 have none"):
            load_notes(self.work_dir, required=True)

    def test_rows_in_file_order(self):
        self.write("image_id\tkind\tdetail\n/b\ticon-image\tIconImageSequence\n/a\talpha\t\n/b\ticon-image\tagain\n")
        self.assertEqual(
            load_notes(self.work_dir, required=True),
            [
                NoteRow(ImageId("/b"), "icon-image", "IconImageSequence"),
                NoteRow(ImageId("/a"), "alpha", ""),
                NoteRow(ImageId("/b"), "icon-image", "again"),
            ],
        )

    def test_malformed_names_file_and_line(self):
        cases = {
            "bad header": ("image_id\tkind\treason\n", 1),
            "empty file": ("", 1),
            "unknown kind": ("image_id\tkind\tdetail\na\talpha\tx\nb\tpreamble\ty\n", 3),
            "short row": ("image_id\tkind\tdetail\na\talpha\n", 2),
            "long row": ("image_id\tkind\tdetail\na\talpha\tx\ty\n", 2),
        }
        for name, (text, line) in cases.items():
            with self.subTest(name=name):
                self.write(text)
                with self.assertRaisesRegex(ValueError, rf"notes\.tsv:{line}:"):
                    load_notes(self.work_dir, required=False)

    def test_non_utf8_names_file(self):
        (self.work_dir / "notes.tsv").write_bytes(b"image_id\tkind\tdetail\n\xe9\talpha\tx\n")
        with self.assertRaisesRegex(ValueError, r"notes\.tsv: not valid UTF-8"):
            load_notes(self.work_dir, required=False)


class TestMarkAndStatuses(StoreTestCase):
    def test_initially_unreviewed(self):
        self.assertEqual(self.store.current_pass(), 1)
        self.assertEqual(set(self.store.statuses(1).values()), {"UNREVIEWED"})

    def test_mark_round_trip_stores_image_id(self):
        mark(self.store, ["batch_001/a.jpg", "batch_001/b.jpg"], "CLEAN")
        mark(self.store, ["batch_002/c.jpg"], "DIRTY")
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

    def test_mark_unknown_key_raises_and_writes_nothing(self):
        with self.assertRaises(KeyError):
            mark(self.store, ["batch_001/a.jpg", "nope.jpg"], "CLEAN")
        self.assertFalse((self.work_dir / "review.tsv").exists())

    def assert_refused_writes_nothing(self, exc, keys, status, pass_number, mode, *, tsv: bytes | None):
        """The mark raises `exc`; review.tsv is as `tsv` (None: absent). Tests check the undo stack."""
        with self.assertRaises(exc):
            self.store.mark(keys, status, pass_number, reviewer="tester", mode=mode)
        path = self.work_dir / "review.tsv"
        self.assertEqual(path.read_bytes() if path.exists() else None, tsv)

    def test_single_mark_of_two_keys_raises_and_writes_nothing(self):
        keys = ["batch_001/a.jpg", "batch_001/b.jpg"]
        self.assert_refused_writes_nothing(ValueError, keys, "CLEAN", 1, "single", tsv=None)
        self.assert_refused_writes_nothing(ValueError, [], "CLEAN", 1, "single", tsv=None)
        self.assertEqual(self.store.undo(1, reviewer="tester"), {})

    def test_single_refusal_leaves_the_undo_stack_alone(self):
        mark(self.store, ["batch_001/a.jpg"], "CLEAN")
        before = (self.work_dir / "review.tsv").read_bytes()
        self.assert_refused_writes_nothing(
            ValueError, ["batch_001/a.jpg", "batch_001/b.jpg"], "DIRTY", 1, "single", tsv=before
        )
        self.assertEqual(self.store.undo(1, reviewer="tester"), {"batch_001/a.jpg": "UNREVIEWED"})

    def test_repeated_keys_raise_in_either_mode_and_write_nothing(self):
        keys = ["batch_001/a.jpg", "batch_001/b.jpg", "batch_001/a.jpg"]
        self.assert_refused_writes_nothing(ValueError, keys, "CLEAN", 1, "grid", tsv=None)
        self.assert_refused_writes_nothing(ValueError, keys, "DIRTY", 1, "grid", tsv=None)
        self.assert_refused_writes_nothing(ValueError, ["batch_001/a.jpg"] * 2, "CLEAN", 1, "single", tsv=None)
        self.assertEqual(self.store.undo(1, reviewer="tester"), {})

    def test_unknown_key_is_a_key_error_before_the_shape_rules(self):
        self.assert_refused_writes_nothing(KeyError, ["nope.jpg", "nope.jpg"], "CLEAN", 1, "single", tsv=None)
        self.assert_refused_writes_nothing(KeyError, ["batch_001/a.jpg", "nope.jpg"], "CLEAN", 1, "grid", tsv=None)

    def test_grid_clean_over_a_dirty_key_is_refused_and_writes_nothing(self):
        mark(self.store, ["batch_001/a.jpg"], "DIRTY")
        before = (self.work_dir / "review.tsv").read_bytes()
        self.assert_refused_writes_nothing(
            GridCleanRefused, ["batch_001/a.jpg", "batch_001/b.jpg"], "CLEAN", 1, "grid", tsv=before
        )
        self.assertEqual(self.store.statuses(1)["batch_001/b.jpg"], "UNREVIEWED")
        self.assertEqual(self.store.undo(1, reviewer="tester"), {"batch_001/a.jpg": "UNREVIEWED"})  # the DIRTY mark

    def test_grid_clean_over_a_flagged_key_is_refused_and_writes_nothing(self):
        mark(self.store, ["batch_001/a.jpg"], "DIRTY")
        mark(self.store, ["batch_001/b.jpg"], "CLEAN")
        self.assertEqual(self.store.statuses(2)["batch_001/a.jpg"], "FLAGGED")
        before = (self.work_dir / "review.tsv").read_bytes()
        self.assert_refused_writes_nothing(
            GridCleanRefused, ["batch_001/a.jpg", "batch_002/c.jpg"], "CLEAN", 2, "grid", tsv=before
        )
        self.assertEqual(self.store.statuses(2)["batch_002/c.jpg"], "UNREVIEWED")

    def test_grid_clean_is_allowed_when_every_key_is_dirty(self):
        keys = ["batch_001/a.jpg", "batch_001/b.jpg"]
        mark(self.store, keys, "DIRTY")
        self.assertEqual(mark(self.store, keys, "CLEAN"), dict.fromkeys(keys, "CLEAN"))

    def test_grid_dirty_is_allowed_over_anything(self):
        mark(self.store, ["batch_001/a.jpg"], "DIRTY")
        mark(self.store, ["batch_001/b.jpg"], "CLEAN")
        keys = ["batch_001/a.jpg", "batch_001/b.jpg", "batch_002/c.jpg"]
        self.assertEqual(mark(self.store, keys, "DIRTY"), dict.fromkeys(keys, "DIRTY"))

    def test_grid_rule_is_judged_per_pass(self):
        keys = ["batch_001/a.jpg", "batch_002/c.jpg"]
        mark(self.store, keys, "DIRTY")  # DIRTY in pass 1, so FLAGGED in pass 2
        with self.assertRaises(GridCleanRefused):
            mark(self.store, keys, "CLEAN", 2)
        self.assertEqual(mark(self.store, keys, "CLEAN", 1), dict.fromkeys(keys, "CLEAN"))  # all DIRTY there

    def test_prior_pass_semantics(self):
        mark(self.store, ["batch_001/a.jpg"], "CLEAN")
        mark(self.store, ["batch_001/b.jpg"], "DIRTY")
        statuses = self.store.statuses(2)
        self.assertEqual(statuses["batch_001/a.jpg"], "CLEAN")
        self.assertEqual(statuses["batch_001/b.jpg"], "FLAGGED")

    def test_pass_one_dirty_is_flagged_in_pass_two(self):
        mark(self.store, ["batch_001/a.jpg"], "DIRTY")
        mark(self.store, ["batch_001/b.jpg"], "CLEAN")
        mark(self.store, ["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")
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
            mark(self.store, ["batch_001/a.jpg"], "CLEAN", 2),
            {"batch_001/a.jpg": "CLEAN"},
        )
        self.assertEqual(self.store.current_pass(), 3)

    def test_flagged_keeps_pass_open(self):
        mark(self.store, ["batch_001/a.jpg", "batch_001/b.jpg"], "DIRTY")
        mark(self.store, ["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")
        mark(self.store, ["batch_001/a.jpg"], "CLEAN", 2)
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
            changed = mark(store, ["batch_001/a.jpg"], "CLEAN")
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
        self.work_dir = temp_dir(self)
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
            "pass above the maximum": (self.REVIEW_HEADER, good, f"/src/b.dcm\tbatch_001\tCLEAN\t{MAX_PASS + 1}\t{ts}"),
            "empty image_id": (self.REVIEW_HEADER, good, f"\tbatch_001\tCLEAN\t1\t{ts}"),
        }
        for name, lines in cases.items():
            with self.subTest(name):
                self.write_review(*lines)
                with self.assertRaisesRegex(ValueError, r"review\.tsv:3: "):
                    LocalStore(self.work_dir)

    def test_review_row_at_the_maximum_pass_loads(self):
        self.write_review(self.REVIEW_HEADER, f"/src/a.dcm\tbatch_001\tCLEAN\t{MAX_PASS}\t{self.GOOD_TS}")
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
        good = f"/src/a.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tsingle\t1\t0.2.0\t"
        cases = {
            "old-width row under new header": (self.NEW_HEADER, good, f"/src/b.dcm\tbatch_001\tCLEAN\t1\t{ts}"),
            "bad mode": (self.NEW_HEADER, good, f"/src/b.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tbogus\t1\t0.2.0\t"),
            "FLAGGED status": (
                self.NEW_HEADER,
                good,
                f"/src/b.dcm\tbatch_001\tFLAGGED\t1\t{ts}\talice\tsingle\t1\t0.2.0\t",
            ),
            "FLAGGED in an undo row": (
                self.NEW_HEADER,
                good,
                f"/src/b.dcm\tbatch_001\tFLAGGED\t1\t{ts}\talice\tundo\t1\t0.2.0\t",
            ),
            "UNREVIEWED outside an undo row": (
                self.NEW_HEADER,
                good,
                f"/src/b.dcm\tbatch_001\tUNREVIEWED\t1\t{ts}\talice\tsingle\t1\t0.2.0\t",
            ),
            "UNREVIEWED in a migrated row": (
                self.NEW_HEADER,
                good,
                f"/src/b.dcm\tbatch_001\tUNREVIEWED\t1\t{ts}\t\t\t\t\t",
            ),
            "bad grid_size": (
                self.NEW_HEADER,
                good,
                f"/src/b.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tgrid\tfour\t0.2.0\t",
            ),
            "zero grid_size": (
                self.NEW_HEADER,
                good,
                f"/src/b.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tgrid\t0\t0.2.0\t",
            ),
            "new-width row under old header": (self.REVIEW_HEADER, f"/src/a.dcm\tbatch_001\tCLEAN\t1\t{ts}", good),
        }
        for name, lines in cases.items():
            with self.subTest(name):
                self.write_review(*lines)
                with self.assertRaisesRegex(ValueError, r"review\.tsv:3: "):
                    LocalStore(self.work_dir, read_only=True)

    def test_bad_source_hash_names_file_and_line(self):
        ts = self.GOOD_TS
        row = f"/src/b.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tsingle\t1\t0.5.0\t"
        good = f"/src/a.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tsingle\t1\t0.5.0\t{'a' * 64}"
        for name, digest in {
            "short": "a" * 63,
            "long": "a" * 65,
            "uppercase": "A" * 64,
            "non-hex": "g" * 64,
            "padded": " " + "a" * 63,
        }.items():
            with self.subTest(name):
                self.write_review(self.NEW_HEADER, good, row + digest)
                with self.assertRaisesRegex(
                    ValueError, r"review\.tsv:3: source_sha256 must be 64 lowercase hex characters or empty$"
                ):
                    LocalStore(self.work_dir, read_only=True)

    def test_empty_or_valid_source_hash_loads(self):
        ts = self.GOOD_TS
        self.write_review(
            self.NEW_HEADER,
            f"/src/patient_smith/a.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tsingle\t1\t0.5.0\t{'0123456789abcdef' * 4}",
            f"/src/patient_jones/b.dcm\tbatch_001\tDIRTY\t1\t{ts}\talice\tsingle\t1\t0.5.0\t",
        )
        db = ReviewDB(self.work_dir, sources={})
        self.assertEqual(db._rows[ImageId("/src/patient_smith/a.dcm")].source_sha256, "0123456789abcdef" * 4)
        self.assertEqual(db._rows[ImageId("/src/patient_jones/b.dcm")].source_sha256, "")

    def test_undo_rows_load_and_tombstone_folds(self):
        ts = self.GOOD_TS
        self.write_review(
            self.NEW_HEADER,
            f"/src/patient_smith/a.dcm\tbatch_001\tCLEAN\t1\t{ts}\talice\tsingle\t1\t0.2.0\t",
            f"/src/patient_jones/b.dcm\tbatch_001\tDIRTY\t2\t{ts}\talice\tsingle\t1\t0.2.0\t",
            f"/src/patient_jones/b.dcm\tbatch_001\tCLEAN\t3\t{ts}\talice\tsingle\t1\t0.2.0\t",
            f"/src/patient_smith/a.dcm\tbatch_001\tUNREVIEWED\t1\t{ts}\talice\tundo\t2\t0.2.0\t",
            f"/src/patient_jones/b.dcm\tbatch_001\tDIRTY\t2\t{ts}\talice\tundo\t2\t0.2.0\t",
        )
        statuses = LocalStore(self.work_dir, read_only=True).statuses(2)
        self.assertEqual((statuses["batch_001/a.jpg"], statuses["batch_001/b.jpg"]), ("UNREVIEWED", "DIRTY"))

    def test_migrated_rows_with_empty_audit_columns_load(self):
        self.write_review(self.NEW_HEADER, f"/src/patient_smith/a.dcm\tbatch_001\tCLEAN\t1\t{self.GOOD_TS}\t\t\t\t\t")
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

    def test_non_utf8_manifest_names_file(self):
        (self.work_dir / "manifest.tsv").write_bytes(b"batch\tpreprocessed_path\timage_id\nb\tk.jpg\t\xe9\n")
        with self.assertRaisesRegex(ValueError, r"manifest\.tsv: not valid UTF-8"):
            load_manifest(self.work_dir)

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

    def test_conflicting_source_sha256_for_one_image_id_names_both_lines(self):
        other = hashlib.sha256(b"other").hexdigest()
        self.write_manifest(
            self.HASHED_HEADER,
            f"b\tb/a.jpg\t/src/a.dcm\t{self.SOURCE_HASH}\t{self.JPEG_HASH}",
            f"b\tb/b.jpg\t/src/b.dcm\t{other}\t{self.JPEG_HASH}",
            f"b\tb/c.jpg\t/src/a.dcm\t{other}\t{self.JPEG_HASH}",
        )
        with self.assertRaisesRegex(
            ValueError, r"manifest\.tsv:4: image_id '/src/a\.dcm' has a different source_sha256 than on line 2"
        ):
            load_manifest(self.work_dir)

    def test_equal_source_sha256_for_one_image_id_loads(self):
        self.write_manifest(
            self.HASHED_HEADER,
            f"b\tb/a.jpg\t/src/a.dcm\t{self.SOURCE_HASH}\t{self.JPEG_HASH}",
            f"b\tb/b.jpg\t/src/a.dcm\t{self.SOURCE_HASH}\t{self.JPEG_HASH}",
        )
        self.assertEqual(len(load_manifest(self.work_dir)), 2)

    def test_load_skipped_required_names_missing_file(self):
        self.assertEqual(load_skipped(self.work_dir), [])
        with self.assertRaisesRegex(ValueError, r"skipped\.tsv: missing; .*incomplete"):
            load_skipped(self.work_dir, required=True)

    def test_export_rows_need_notes_beside_a_hashed_manifest(self):
        cases = {
            "hashed": (self.HASHED_HEADER, f"b\tb/a.jpg\t/src/a.dcm\t{self.SOURCE_HASH}\t{self.JPEG_HASH}", True),
            "legacy": (self.MANIFEST_HEADER, "b\tb/a.jpg\t/src/a.dcm", False),
            "empty": (self.HASHED_HEADER, None, True),
        }
        for name, (header, row, refused) in cases.items():
            with self.subTest(name):
                self.write_manifest(header, *([row] if row else []))
                (self.work_dir / "skipped.tsv").write_text("image_id\tkind\treason\n")
                (self.work_dir / "notes.tsv").unlink(missing_ok=True)
                with LocalStore(self.work_dir, read_only=True) as store:
                    if refused:
                        with self.assertRaisesRegex(ValueError, r"notes\.tsv: missing"):
                            store.export_rows()
                    else:
                        self.assertEqual([r.image_id for r in store.export_rows()], ["/src/a.dcm"])

    def test_export_rows_need_skipped_beside_a_hashed_manifest(self):
        cases = {
            "hashed": (self.HASHED_HEADER, f"b\tb/a.jpg\t/src/a.dcm\t{self.SOURCE_HASH}\t{self.JPEG_HASH}", True),
            "legacy": (self.MANIFEST_HEADER, "b\tb/a.jpg\t/src/a.dcm", False),
            "empty": (self.HASHED_HEADER, None, True),
            "empty legacy": (self.MANIFEST_HEADER, None, True),  # no rows counts as hashed: the safe direction
        }
        for name, (header, row, refused) in cases.items():
            with self.subTest(name):
                self.write_manifest(header, *([row] if row else []))
                (self.work_dir / "skipped.tsv").unlink(missing_ok=True)
                with LocalStore(self.work_dir, read_only=True) as store:
                    self.assertEqual(store.skipped(), SkippedCounts(0, 0))  # counts alone never need the file
                    if refused:
                        with self.assertRaisesRegex(ValueError, r"skipped\.tsv: missing"):
                            store.export_rows()
                    else:
                        self.assertEqual([r.image_id for r in store.export_rows()], ["/src/a.dcm"])

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

    def test_oversized_manifest_field_names_file_and_line(self):
        self.write_manifest(
            self.MANIFEST_HEADER, "b\tk.jpg\t/src/a.dcm", f"b\tk2.jpg\t{'x' * 200_000}", "b\tk3.jpg\t/c"
        )
        with self.assertRaisesRegex(ValueError, r"manifest\.tsv:3: field larger than field limit"):
            load_manifest(self.work_dir)

    def test_oversized_manifest_header_names_file_and_line_1(self):
        self.write_manifest("batch\t" + "x" * 200_000)
        with self.assertRaisesRegex(ValueError, r"manifest\.tsv:1: field larger than field limit"):
            load_manifest(self.work_dir)

    def test_hashed_row_under_legacy_header_is_refused(self):
        self.write_manifest(
            self.MANIFEST_HEADER, f"batch_001\tbatch_001/a.jpg\t/src/a.dcm\t{self.SOURCE_HASH}\t{self.JPEG_HASH}"
        )
        with self.assertRaisesRegex(ValueError, r"manifest\.tsv:2: expected 3"):
            load_manifest(self.work_dir)


class TestReviewLog(unittest.TestCase):
    """review.tsv is an append-only log: one header, then one line per decision, last line per image_id wins."""

    HEADER_LINE = (
        b"image_id\tbatch\tstatus\tpass_number\ttimestamp\treviewer\tmode\tgrid_size\ttool_version\tsource_sha256\r\n"
    )

    def test_decision_fields_match_header_order(self):
        # _tsv writes dataclasses.astuple(decision) under HEADER, so the two must agree in order
        self.assertEqual([f.name for f in dataclasses.fields(Decision)], HEADER)

    def setUp(self):
        self.work_dir = temp_dir(self)
        os.chmod(self.work_dir, 0o700)
        self.path = self.work_dir / "review.tsv"

    def reload(self) -> ReviewDB:
        with self.assertNoLogs("image_review"):
            return ReviewDB(self.work_dir, sources={})

    def test_marks_append_lines_and_reload(self):
        db = ReviewDB(self.work_dir, sources={})
        db.mark_many([("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        db.mark_many([("y", "batch_002"), ("z", "batch_002")], "DIRTY", 2, reviewer="tester", mode="single")
        lines = self.path.read_bytes().splitlines(keepends=True)
        self.assertEqual(lines[0], self.HEADER_LINE)
        self.assertEqual(len(lines), 4)
        self.assertEqual(self.reload()._rows, db._rows)

    def test_creating_review_tsv_syncs_the_directory_but_a_later_append_does_not(self):
        db = ReviewDB(self.work_dir, sources={})
        with recorded_dir_fsyncs() as synced:
            db.mark_many([("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
            self.assertEqual(len(synced), 1)
            db.mark_many([("y", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
            self.assertEqual(len(synced), 1)
        with recorded_dir_fsyncs() as synced:  # an existing file, freshly loaded
            ReviewDB(self.work_dir, sources={}).mark_many(
                [("z", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
            )
        self.assertEqual(synced, [])

    def test_directory_sync_error_on_creation_writes_nothing_and_a_retry_works(self):
        db = ReviewDB(self.work_dir, sources={})
        with recorded_dir_fsyncs(errno.EINVAL):  # unsupported: tolerated
            db.mark_many([("w", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.path.unlink()
        db = ReviewDB(self.work_dir, sources={})
        with recorded_dir_fsyncs(errno.EIO), self.assertRaises(OSError):
            db.mark_many([("x", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")
        self.assertEqual(self.path.read_bytes(), b"")  # nothing written; memory matches
        self.assertEqual(db._rows, {})
        self.assertEqual(ReviewDB(self.work_dir, sources={})._rows, {})
        db.mark_many([("x", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")  # retry on the same db
        self.assertEqual(db._rows["x"].status, "DIRTY")
        self.assertEqual(ReviewDB(self.work_dir, sources={})._rows, db._rows)
        self.assertEqual(self.path.read_bytes().splitlines()[0] + b"\r\n", self.HEADER_LINE)

    def test_same_image_twice_last_wins(self):
        db = ReviewDB(self.work_dir, sources={})
        db.mark_many([("x", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")
        db.mark_many([("x", "batch_001")], "CLEAN", 2, reviewer="tester", mode="single")
        self.assertEqual(len(self.path.read_bytes().splitlines()), 3)
        reloaded = self.reload()
        self.assertEqual((reloaded._rows["x"].status, reloaded._rows["x"].pass_number), ("CLEAN", 2))
        self.assertEqual(reloaded._rows, db._rows)

    def test_mark_is_a_pure_append(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        before = self.path.read_bytes()
        inode = self.path.stat().st_ino
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("y", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single"
        )
        after = self.path.read_bytes()
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertTrue(after.startswith(before))
        self.assertGreater(len(after), len(before))

    def test_torn_last_line_is_dropped_and_next_mark_starts_clean(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        intact = self.path.read_bytes()
        with open(self.path, "ab") as f:
            f.write(b"y\tbatch_0")  # a crash mid-append
        with self.assertLogs("image_review.review_db", "WARNING") as logs:
            db = ReviewDB(self.work_dir, sources={})
        self.assertIn("ignoring the unfinished last line", logs.records[0].getMessage())
        self.assertIn("review.tsv:3:", logs.records[0].getMessage())
        self.assertEqual(set(db._rows), {"x"})
        db.mark_many([("z", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")
        after = self.path.read_bytes()
        self.assertTrue(after.startswith(intact))
        self.assertTrue(after[len(intact) :].startswith(b"z\tbatch_001\tDIRTY\t1\t"))
        self.assertEqual(len(after.splitlines()), 3)  # header, x, z: the fragment is gone
        self.assertEqual(set(self.reload()._rows), {"x", "z"})

    def test_huge_unterminated_tail_is_a_torn_line(self):
        # a zero-filled extension after a crash: csv refuses a field over 128 KiB, which must not escape as csv.Error
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        intact = self.path.read_bytes()
        with open(self.path, "ab") as f:
            f.write(b"\x00" * 200_000)
        with self.assertLogs("image_review.review_db", "WARNING") as logs:
            db = ReviewDB(self.work_dir, sources={})
        self.assertIn("ignoring the unfinished last line", logs.records[0].getMessage())
        self.assertIn("field larger than field limit", logs.records[0].getMessage())
        self.assertEqual(set(db._rows), {"x"})
        db.mark_many([("z", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")
        after = self.path.read_bytes()
        self.assertTrue(after.startswith(intact))
        self.assertNotIn(b"\x00", after)
        self.assertEqual(set(self.reload()._rows), {"x", "z"})

    def test_oversized_field_on_a_complete_line_is_a_value_error(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        with open(self.path, "ab") as f:
            f.write(b"y" * 200_000 + b"\tbatch_001\r\n")
            f.write(b"z\tbatch_001\r\n")
        with self.assertRaisesRegex(ValueError, r"review\.tsv:3: field larger than field limit"):
            ReviewDB(self.work_dir, sources={})

    def test_torn_last_line_that_parses_is_kept(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        with open(self.path, "ab") as f:
            f.write(self.TORN_ROW + b"a" * 64)  # every field written, line ending not
        db = self.reload()
        self.assertEqual(set(db._rows), {"x", "y"})
        self.assertEqual(db._rows[ImageId("y")].source_sha256, "a" * 64)
        db.mark_many([("z", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertIn(b"a" * 64 + b"\r\nz\t", self.path.read_bytes())  # its missing line ending, in the file's style
        self.assertEqual(set(self.reload()._rows), {"x", "y", "z"})

    TORN_ROW = b"y\tbatch_001\tDIRTY\t1\t2026-01-01T00:00:00+00:00\tr\tsingle\t1\t0.1\t"  # up to its hash

    def test_torn_last_line_without_its_hash_is_dropped(self):
        for name, tail in (("cut at the tab before the hash", b""), ("cut inside the hash", b"a" * 30)):
            with self.subTest(name):
                self.path.unlink(missing_ok=True)
                ReviewDB(self.work_dir, sources={}).mark_many(
                    [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
                )
                with open(self.path, "ab") as f:
                    f.write(self.TORN_ROW + tail)  # a row written with a hash, cut short before its end
                with self.assertLogs("image_review.review_db", "WARNING") as logs:
                    db = ReviewDB(self.work_dir, sources={})
                self.assertIn("ignoring the unfinished last line", logs.records[0].getMessage())
                self.assertEqual(set(db._rows), {"x"})
                with self.assertRaisesRegex(ValueError, "unfinished line"):
                    db.decisions()
                db.mark_many([("z", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
                self.assertNotIn(b"\ny\t", self.path.read_bytes())  # dropped by the next append
                self.assertEqual(set(self.reload().decisions()), {"x", "z"})

    def test_torn_between_cr_and_lf_is_kept(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        with open(self.path, "ab") as f:
            f.write(b"y\tbatch_001\tDIRTY\t1\t2026-01-01T00:00:00+00:00\tr\tsingle\t1\t0.1\t\r")
        db = self.reload()
        self.assertEqual(set(db._rows), {"x", "y"})
        db.mark_many([("z", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        data = self.path.read_bytes()
        self.assertIn(b"\t0.1\t\r\nz\t", data)
        self.assertNotIn(b"\r\r", data)
        self.assertNotIn(b"\n\r\n", data)  # no blank line
        self.assertEqual(data.count(b"\r"), data.count(b"\r\n"))
        self.assertEqual(set(self.reload()._rows), {"x", "y", "z"})

    def test_torn_multibyte_character_is_dropped(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        with open(self.path, "ab") as f:
            f.write("é\tb".encode()[:2])
        with self.assertLogs("image_review.review_db", "WARNING") as logs:
            db = ReviewDB(self.work_dir, sources={})
        self.assertIn("ignoring the unfinished last line", logs.records[0].getMessage())
        db.mark_many([("z", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")
        self.assertEqual(set(self.reload()._rows), {"x", "z"})

    def test_failed_append_is_rolled_back(self):
        db = ReviewDB(self.work_dir, sources={})
        db.mark_many([("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
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
        db.mark_many([("w", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(self.reload()._rows, db._rows)

    def test_failed_append_left_on_disk_is_cut_by_the_next(self):
        db = ReviewDB(self.work_dir, sources={})
        db.mark_many([("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
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
        db.mark_many([("w", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(self.reload()._rows, db._rows)

    def test_stale_truncate_refuses(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001"), ("y", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        with open(self.path, "ab") as f:
            f.write(b"z\tbatch_0")
        with contextlib.redirect_stderr(io.StringIO()):
            db = ReviewDB(self.work_dir, sources={})
        with open(self.path, "ab") as f:
            f.write(b"01\tCLEAN\t1\tt\r\n")  # someone else finished the line since the load
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ReviewLogChanged, "changed since it was loaded"):
            db.mark_many([("w", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")
        self.assertEqual(self.path.read_bytes(), before)

    def test_append_by_another_writer_since_load_refuses(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        db = ReviewDB(self.work_dir, sources={})
        db.mark_many([("y", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")  # its own marks: fine
        ReviewDB(self.work_dir, sources={}).mark_many([("x", "batch_001")], "DIRTY", 1, reviewer="other", mode="single")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ReviewLogChanged, "changed since it was loaded; not appending"):
            db.mark_many([("z", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(set(db._rows), {"x", "y"})

    def test_file_created_by_another_writer_since_load_refuses(self):
        db = ReviewDB(self.work_dir, sources={})  # no review.tsv yet
        ReviewDB(self.work_dir, sources={}).mark_many([("x", "batch_001")], "DIRTY", 1, reviewer="other", mode="single")
        before = self.path.read_bytes()
        with self.assertRaises(ReviewLogChanged):
            db.mark_many([("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(self.path.read_bytes(), before)

    def test_file_replaced_since_load_refuses(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        db = ReviewDB(self.work_dir, sources={})
        same_size = self.path.read_bytes().replace(b"CLEAN", b"DIRTY")
        replacement = self.work_dir / "replacement.tsv"
        replacement.write_bytes(same_size)  # created while review.tsv still exists: a new inode, the same size
        os.replace(replacement, self.path)
        with self.assertRaises(ReviewLogChanged):
            db.mark_many([("y", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(self.path.read_bytes(), same_size)

    def test_concurrent_append_refuses_both_writers_next_mark(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        a, b = ReviewDB(self.work_dir, sources={}), ReviewDB(self.work_dir, sources={})
        real_write = os.write

        def a_appends_first(fd, data):  # A appends between B's check and B's write
            if not a_appends_first.done:
                a_appends_first.done = True
                a.mark_many([("y", "batch_001")], "DIRTY", 1, reviewer="alice", mode="single")
            return real_write(fd, data)

        a_appends_first.done = False
        with mock.patch.object(review_db_module.os, "write", a_appends_first):
            b.mark_many([("z", "batch_001")], "CLEAN", 1, reviewer="bob", mode="single")
        self.assertEqual(set(self.reload()._rows), {"x", "y", "z"})  # both landed, once
        for db in (a, b):
            with self.assertRaises(ReviewLogChanged):
                db.mark_many([("y", "batch_001")], "CLEAN", 1, reviewer="tester", mode="grid")
        self.assertEqual(self.reload()._rows["y"].status, "DIRTY")

    def test_file_deleted_since_load_refuses_and_is_not_recreated(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        db = ReviewDB(self.work_dir, sources={})
        self.path.unlink()
        with self.assertRaisesRegex(ReviewLogChanged, "changed since it was loaded; not appending"):
            db.mark_many([("y", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertFalse(self.path.exists())

    def test_append_after_failed_rollback_refuses_if_file_grew(self):
        db = ReviewDB(self.work_dir, sources={})
        db.mark_many([("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
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
            db.mark_many([("y", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")
        with open(self.path, "ab") as f:
            f.write(b"\r\nw\tbatch_001\tDIRTY\t1\tt\tother\tsingle\t1\t0.1\r\n")  # another writer, after the fragment
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ReviewLogChanged, "not truncating"):
            db.mark_many([("z", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(self.path.read_bytes(), before)

    def test_undo_after_own_marks_appends(self):
        db = ReviewDB(self.work_dir, sources={})
        changes = db.mark_many([("x", "batch_001")], "DIRTY", 1, reviewer="tester", mode="single")
        db.undo_many(changes, reviewer="tester")
        db.mark_many([("y", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertEqual(self.reload()._rows, db._rows)

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
                    ReviewDB(self.work_dir, sources={})
                self.assertEqual(self.path.read_bytes(), data.encode())

    def test_torn_header_is_dropped(self):
        self.path.write_bytes(b"image_id\tba")
        with contextlib.redirect_stderr(io.StringIO()):
            db = ReviewDB(self.work_dir, sources={})
        db.mark_many([("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertTrue(self.path.read_bytes().startswith(self.HEADER_LINE))
        self.assertEqual(set(self.reload()._rows), {"x"})

    def test_empty_file_loads_and_gets_a_header(self):
        self.path.touch()
        db = self.reload()
        db.mark_many([("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single")
        self.assertTrue(self.path.read_bytes().startswith(self.HEADER_LINE))

    def test_unparseable_line_before_the_last_still_raises(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        with open(self.path, "ab") as f:
            f.write(b"garbage\r\ny\tbatch_0")
        with self.assertRaisesRegex(ValueError, r"review\.tsv:3: "):
            ReviewDB(self.work_dir, sources={})

    def test_unparseable_terminated_last_line_still_raises(self):
        ReviewDB(self.work_dir, sources={}).mark_many(
            [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
        )
        with open(self.path, "ab") as f:
            f.write(b"y\tbatch_0\r\n")
        with self.assertRaisesRegex(ValueError, r"review\.tsv:3: "):
            ReviewDB(self.work_dir, sources={})

    def test_file_mode_follows_policy(self):
        for dir_mode, file_mode in [(0o700, 0o600), (0o2770, 0o660)]:
            with self.subTest(dir_mode=oct(dir_mode)):
                self.path.unlink(missing_ok=True)
                os.chmod(self.work_dir, dir_mode)
                old_umask = os.umask(0o077)  # O_CREAT's mode alone would lose the group bits
                try:
                    ReviewDB(self.work_dir, sources={}).mark_many(
                        [("x", "batch_001")], "CLEAN", 1, reviewer="tester", mode="single"
                    )
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

    def test_legacy_manifest_records_no_source_hash(self):
        self.store.mark(["batch_001/a.jpg", "batch_002/c.jpg"], "CLEAN", 1, reviewer="alice", mode="grid")
        self.assertEqual([r["source_sha256"] for r in review_rows(self.work_dir / "review.tsv")], ["", ""])

    def test_each_key_batch_comes_from_the_manifest(self):
        self.store.mark(["batch_001/a.jpg", "batch_002/c.jpg"], "CLEAN", 1, reviewer="alice", mode="grid")
        batches = {r["image_id"]: r["batch"] for r in review_rows(self.work_dir / "review.tsv")}
        self.assertEqual(batches, {"/src/patient_smith/a.dcm": "batch_001", "/src/patient_lee/c.dcm": "batch_002"})

    def test_reviewer_with_quotes_round_trips(self):
        reviewer = 'Dr. "Q" O\'Neil'
        self.store.mark(["batch_001/a.jpg"], "CLEAN", 1, reviewer=reviewer, mode="single")
        with contextlib.redirect_stderr(io.StringIO()):
            db = ReviewDB(self.work_dir, sources={})
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

    def test_bad_pass_number_is_refused_before_writing(self):
        for pass_number in (0, -1, MAX_PASS + 1):
            with self.subTest(pass_number=pass_number), self.assertRaises(ValueError):
                self.store.mark(["batch_001/a.jpg"], "CLEAN", pass_number, reviewer="alice", mode="single")
        self.assertFalse((self.work_dir / "review.tsv").exists())


class TestSourceHashColumn(StoreTestCase):
    """Each verdict row records the manifest's source_sha256 for its image_id, as it was when the verdict was given."""

    def make_work_dir(self) -> None:
        make_work_dir(self.work_dir, hashed=True)

    @staticmethod
    def digest(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def test_mark_records_the_manifest_hash(self):
        self.store.mark(["batch_001/a.jpg"], "DIRTY", 1, reviewer="alice", mode="single")
        self.store.mark(["batch_001/b.jpg", "batch_002/c.jpg"], "CLEAN", 1, reviewer="alice", mode="grid")
        rows = review_rows(self.work_dir / "review.tsv")
        self.assertEqual(
            [(r["image_id"], r["source_sha256"]) for r in rows],
            [(image_id, self.digest(image_id)) for image_id in (ROWS[0][2], ROWS[1][2], ROWS[2][2])],
        )
        self.store.close()
        with contextlib.redirect_stderr(io.StringIO()):
            reloaded = ReviewDB(self.work_dir, sources={})  # read back from disk, not from the manifest
        self.assertEqual(reloaded._rows[ImageId(ROWS[0][2])].source_sha256, self.digest(ROWS[0][2]))

    def test_undo_restore_carries_the_previous_rows_hash(self):
        old, new = "1" * 64, "2" * 64
        x = ImageId("x")
        ReviewDB(self.work_dir, sources={x: old}).mark_many([(x, "b")], "CLEAN", 1, reviewer="al", mode="single")
        db = ReviewDB(self.work_dir, sources={x: new})  # the file was replaced and preprocessed again
        changes = db.mark_many([(x, "b")], "DIRTY", 1, reviewer="al", mode="single")
        db.undo_many(changes, reviewer="al")
        rows = review_rows(self.work_dir / "review.tsv")
        self.assertEqual(
            [(r["status"], r["mode"], r["source_sha256"]) for r in rows],
            [("CLEAN", "single", old), ("DIRTY", "single", new), ("CLEAN", "undo", old)],
        )

    def test_undo_tombstone_carries_the_written_rows_hash(self):
        written, current = "1" * 64, "2" * 64
        x = ImageId("x")
        changes = ReviewDB(self.work_dir, sources={x: written}).mark_many(
            [(x, "b")], "CLEAN", 1, reviewer="al", mode="single"
        )
        ReviewDB(self.work_dir, sources={x: current}).undo_many(changes, reviewer="al")
        rows = review_rows(self.work_dir / "review.tsv")
        self.assertEqual(
            [(r["status"], r["source_sha256"]) for r in rows], [("CLEAN", written), ("UNREVIEWED", written)]
        )


class TestStaleVerdicts(unittest.TestCase):
    """A row whose source_sha256 differs from the manifest's for its image_id is a verdict on other content: it reads
    as none. A row without a hash, or any row when the manifest has none to compare, is trusted by image_id."""

    OLD, NEW = "1" * 64, "2" * 64
    X, Y = ImageId("x"), ImageId("y")

    def setUp(self):
        self.work_dir = temp_dir(self)

    def record(self, sources: dict[ImageId, str], image_id: ImageId, status, pass_number: int = 1) -> None:
        ReviewDB(self.work_dir, sources=sources).mark_many(
            [(image_id, "b")], status, pass_number, reviewer="al", mode="single"
        )

    def test_stale_verdict_is_unreviewed(self):
        for status in ("CLEAN", "DIRTY"):
            with self.subTest(status=status):
                (self.work_dir / "review.tsv").unlink(missing_ok=True)
                self.record({self.X: self.OLD}, self.X, status)
                db = ReviewDB(self.work_dir, sources={self.X: self.NEW})  # the file was replaced since
                self.assertEqual([db.get_status(self.X, p) for p in (1, 2)], ["UNREVIEWED", "UNREVIEWED"])
                self.assertEqual(db.decisions()[self.X].status, status)  # the raw row, for export to explain

    def test_stale_only_image_is_on_pass_one(self):
        self.record({self.X: self.OLD}, self.X, "CLEAN", 3)
        self.record({self.Y: self.NEW}, self.Y, "CLEAN", 3)
        both = [self.X, self.Y]
        self.assertEqual(ReviewDB(self.work_dir, sources={self.X: self.OLD, self.Y: self.NEW}).current_pass(both), 4)
        db = ReviewDB(self.work_dir, sources={self.X: self.NEW, self.Y: self.NEW})
        self.assertEqual(db.current_pass(both), 1)  # x has no verdict on its content: not at max_pass
        self.assertIn(db.get_status(self.X, 3), TODO_STATUSES)

    def test_hashless_row_is_trusted_by_a_hashed_manifest(self):
        self.record({}, self.X, "CLEAN")  # recorded before the hash was, or migrated
        db = ReviewDB(self.work_dir, sources={self.X: self.NEW})
        self.assertEqual((db.get_status(self.X, 1), db.current_pass([self.X])), ("CLEAN", 2))

    def test_hashed_row_is_trusted_by_a_legacy_manifest(self):
        self.record({self.X: self.OLD}, self.X, "DIRTY")
        db = ReviewDB(self.work_dir, sources={})  # nothing to compare with
        self.assertEqual((db.get_status(self.X, 1), db.current_pass([self.X])), ("DIRTY", 2))
        self.assertEqual(db.get_status(self.X, 2), "FLAGGED")

    def test_marking_a_stale_image_keeps_its_pass_and_undo_restores_the_raw_row(self):
        self.record({self.X: self.OLD}, self.X, "DIRTY", 3)
        db = ReviewDB(self.work_dir, sources={self.X: self.NEW})
        changes = db.mark_many([(self.X, "b")], "CLEAN", 1, reviewer="al", mode="single")
        self.assertEqual(changes[0].written.pass_number, 3)  # never decreases, even over a stale row
        self.assertEqual(db.get_status(self.X, 1), "CLEAN")
        db.undo_many(changes, reviewer="al")
        self.assertEqual(db.get_status(self.X, 1), "UNREVIEWED")
        rows = review_rows(self.work_dir / "review.tsv")
        verdict = ("batch", "status", "pass_number", "source_sha256")
        self.assertEqual(
            [tuple(r[c] for c in verdict) for r in rows],
            [("b", "DIRTY", "3", self.OLD), ("b", "CLEAN", "3", self.NEW), ("b", "DIRTY", "3", self.OLD)],
        )
        reloaded = ReviewDB(self.work_dir, sources={self.X: self.NEW})
        self.assertEqual(reloaded.get_status(self.X, 1), "UNREVIEWED")
        self.assertEqual(reloaded.decisions()[self.X].source_sha256, self.OLD)

    def test_grid_clean_over_a_stale_dirty_is_accepted(self):
        make_work_dir(self.work_dir, hashed=True)
        keys = [key for _, key, _ in ROWS[:2]]
        with LocalStore(self.work_dir) as store:
            store.mark(keys[:1], "DIRTY", 1, reviewer="al", mode="single")
        manifest = self.work_dir / "manifest.tsv"
        with open(manifest, newline="") as f:
            header, *rows = csv.reader(f, delimiter="\t")
        with open(manifest, "w", newline="") as f:  # the first file was replaced and preprocessed again
            csv.writer(f, delimiter="\t").writerows([header, [*rows[0][:3], self.NEW, rows[0][4]], *rows[1:]])
        with LocalStore(self.work_dir) as store:
            self.assertEqual(store.statuses(1)[keys[0]], "UNREVIEWED")  # its DIRTY was on other content
            self.assertEqual(
                store.mark(keys, "CLEAN", 1, reviewer="al", mode="grid"), dict.fromkeys(keys, "CLEAN")
            )  # not GridCleanRefused


class TestMigration(unittest.TestCase):
    """The 5-column file from before the audit columns; TestMigrationFromNineColumns reruns these on 0.4.0's."""

    OLD_HEADER = "image_id\tbatch\tstatus\tpass_number\ttimestamp"
    OLD_ROWS = (
        "/src/patient_smith/a.dcm\tbatch_001\tDIRTY\t1\t2026-01-01T00:00:00+00:00",
        "/src/patient_jones/b.dcm\tbatch_001\tCLEAN\t1\t2026-01-01T00:00:01+00:00",
        "/src/patient_smith/a.dcm\tbatch_001\tCLEAN\t2\t2026-01-02T00:00:00+00:00",
    )

    def setUp(self):
        self.work_dir = temp_dir(self)
        make_work_dir(self.work_dir, hashed=True)  # hashes the migrated rows must not be given
        os.chmod(self.work_dir, 0o2770)  # group policy: the migrated file must be 0660, not mkstemp's 0600
        self.path = self.work_dir / "review.tsv"
        self.old = ("\r\n".join([self.OLD_HEADER, *self.OLD_ROWS]) + "\r\n").encode()
        self.path.write_bytes(self.old)

    def expected_rows(self) -> list[list[str]]:
        """The old rows, every column the old header lacks empty (source_sha256 too, though the manifest has one)."""
        return [[*fields, *[""] * (len(HEADER) - len(fields))] for fields in (r.split("\t") for r in self.OLD_ROWS)]

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
        self.assertEqual(rows[3][9], hashlib.sha256(b"/src/patient_lee/c.dcm").hexdigest())  # the fixture's hash

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
        db = ReviewDB(self.work_dir, sources={})
        with self.assertRaisesRegex(RuntimeError, f"old {len(self.OLD_HEADER.split())}-column header"):
            db.mark_many([("x", "batch_001")], "CLEAN", 1, reviewer="alice", mode="single")
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
        db = ReviewDB(self.work_dir, sources={})
        with open(self.path, "ab") as f:
            f.write(self.OLD_ROWS[0].encode() + b"\r\n")
        changed = self.path.read_bytes()
        with self.assertRaisesRegex(ReviewLogChanged, "changed since it was loaded"):
            db.migrate()
        self.assertEqual(self.path.read_bytes(), changed)


class TestMigrationFromNineColumns(TestMigration):
    """The 9-column file 0.3.0 and 0.4.0 write, before source_sha256."""

    OLD_HEADER = "\t".join(HEADER[:9])
    OLD_ROWS = (
        "/src/patient_smith/a.dcm\tbatch_001\tDIRTY\t1\t2026-01-01T00:00:00+00:00\talice\tsingle\t1\t0.4.0",
        "/src/patient_jones/b.dcm\tbatch_001\tCLEAN\t1\t2026-01-01T00:00:01+00:00\t\t\t\t",  # migrated before
        "/src/patient_smith/a.dcm\tbatch_001\tCLEAN\t2\t2026-01-02T00:00:00+00:00\tbob\tgrid\t1\t0.4.0",
    )


class TestTwoWriters(unittest.TestCase):
    """A second writer despite the lock (review.lock deleted by hand while the first runs) must not append a verdict
    decided from a stale view of review.tsv."""

    def setUp(self):
        self.work_dir = temp_dir(self)
        make_work_dir(self.work_dir)
        self.path = self.work_dir / "review.tsv"

    def open_both(self) -> tuple[LocalStore, LocalStore]:
        a = LocalStore(self.work_dir)
        self.addCleanup(a.close)
        (self.work_dir / LOCK_NAME).unlink()
        b = LocalStore(self.work_dir)
        self.addCleanup(b.close)
        return a, b

    def assert_grid_clean_refused(self, a: LocalStore, b: LocalStore) -> None:
        mark(a, ["batch_001/a.jpg"], "DIRTY")
        self.assertEqual(b.statuses(1)["batch_001/a.jpg"], "UNREVIEWED")  # B's view is stale
        after_a = self.path.read_bytes()
        with self.assertRaises(ReviewLogChanged):
            b.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "CLEAN", 1, reviewer="bob", mode="grid")
        self.assertEqual(self.path.read_bytes(), after_a)
        allowed, _ = split_allowlist(LocalStore(self.work_dir, read_only=True).export_rows())
        self.assertNotIn("/src/patient_smith/a.dcm", [r.image_id for r in allowed])

    def test_stale_grid_clean_does_not_override_dirty(self):
        with LocalStore(self.work_dir) as store:
            mark(store, ["batch_002/c.jpg"], "CLEAN")  # review.tsv exists when both load
        a, b = self.open_both()
        self.assert_grid_clean_refused(a, b)

    def test_stale_grid_clean_refused_when_review_tsv_was_absent(self):
        a, b = self.open_both()
        self.assertFalse(self.path.exists())
        self.assert_grid_clean_refused(a, b)


class TestCutShortMark(unittest.TestCase):
    """A crash can leave some rows of a multi-row mark, cut at a line ending; decisions() then refuses."""

    def setUp(self):
        self.work_dir = temp_dir(self)
        os.chmod(self.work_dir, 0o700)
        self.path = self.work_dir / "review.tsv"

    def write(self, *rows: tuple[str, str, int, str, str, str, int | str]) -> None:
        """Rows are (image_id, status, pass, timestamp, reviewer, mode, grid_size)."""
        lines = [HEADER] + [[i, "b", st, str(n), ts, rv, md, str(g), "v", ""] for i, st, n, ts, rv, md, g in rows]
        self.path.write_bytes("".join("\t".join(line) + "\r\n" for line in lines).encode())

    def test_grid_dirty_cut_to_one_row_refuses_export(self):
        self.write(
            ("x", "CLEAN", 1, "t1", "zq_reviewer", "grid", 3),
            ("y", "CLEAN", 1, "t1", "zq_reviewer", "grid", 3),
            ("z", "CLEAN", 1, "t1", "zq_reviewer", "grid", 3),
            ("x", "DIRTY", 2, "t2", "zq_reviewer", "grid", 3),
        )
        before = self.path.read_bytes()
        with self.assertLogs("image_review.review_db", "WARNING") as logs:
            db = ReviewDB(self.work_dir, sources={})
        message = logs.records[0].getMessage()
        self.assertIn(str(self.path), message)
        self.assertNotIn("x", message.replace(str(self.path), ""))  # no image_id, no reviewer
        with self.assertRaisesRegex(ValueError, r"cut short.*1 of 3 rows.*`review`.*export again") as ctx:
            db.decisions()
        self.assertNotIn("zq_reviewer", message + str(ctx.exception))
        self.assertEqual(self.path.read_bytes(), before)  # the DIRTY that landed stays
        self.assertEqual(db._rows["x"].status, "DIRTY")

    def test_next_append_clears_it(self):
        self.write(("x", "CLEAN", 1, "t1", "al", "grid", 2))
        with self.assertLogs("image_review.review_db", "WARNING"):
            db = ReviewDB(self.work_dir, sources={})
        with self.assertRaises(ValueError):
            db.decisions()
        db.mark_many([("y", "b")], "CLEAN", 1, reviewer="al", mode="single")
        self.assertEqual(set(db.decisions()), {"x", "y"})
        with self.assertNoLogs("image_review"):
            self.assertEqual(set(ReviewDB(self.work_dir, sources={}).decisions()), {"x", "y"})

    def test_complete_marks_never_trip(self):
        cases = {
            "complete group": [("x", "CLEAN", 1, "t", "al", "grid", 2), ("y", "CLEAN", 1, "t", "al", "grid", 2)],
            "single": [("x", "CLEAN", 1, "t", "al", "single", 1)],
            "two equal groups in one timestamp": [("x", "CLEAN", 1, "t", "al", "grid", 2)] * 4,
            "two singles in one timestamp": [("x", "CLEAN", 1, "t", "al", "single", 1)] * 2,
            "legacy rows": [("x", "CLEAN", 1, "t", "", "", ""), ("y", "CLEAN", 1, "t", "", "", "")],
            "earlier cut group followed by a complete one": [
                ("x", "CLEAN", 1, "t1", "al", "grid", 2),
                ("y", "CLEAN", 1, "t2", "al", "single", 1),
            ],
        }
        for name, rows in cases.items():
            with self.subTest(name):
                self.write(*rows)
                with self.assertNoLogs("image_review"):
                    db = ReviewDB(self.work_dir, sources={})
                db.decisions()

    def test_undo_group_is_complete(self):
        db = ReviewDB(self.work_dir, sources={})
        changes = db.mark_many([("x", "b"), ("y", "b"), ("z", "b")], "CLEAN", 1, reviewer="al", mode="grid")
        db.undo_many(changes, reviewer="al")
        with self.assertNoLogs("image_review"):
            self.assertEqual(ReviewDB(self.work_dir, sources={}).decisions(), {})

    def test_cut_undo_group_is_caught(self):
        self.write(("x", "CLEAN", 1, "t2", "al", "undo", 3))
        with self.assertLogs("image_review.review_db", "WARNING"):
            db = ReviewDB(self.work_dir, sources={})
        with self.assertRaisesRegex(ValueError, "1 of 3 rows"):
            db.decisions()

    def test_mark_with_source_hashes_cut_short_is_caught(self):
        sources = {ImageId(i): c * 64 for i, c in (("x", "1"), ("y", "2"), ("z", "3"))}
        ReviewDB(self.work_dir, sources=sources).mark_many(
            [(i, "b") for i in sources], "CLEAN", 1, reviewer="al", mode="grid"
        )
        lines = self.path.read_bytes().splitlines(keepends=True)
        self.assertEqual(lines[0].decode().rstrip("\r\n").split("\t"), HEADER)
        self.path.write_bytes(b"".join(lines[:-1]))  # the crash kept two of the three rows, cut at a line ending
        with self.assertLogs("image_review.review_db", "WARNING"):
            db = ReviewDB(self.work_dir, sources=sources)
        with self.assertRaisesRegex(ValueError, "2 of 3 rows"):
            db.decisions()

    def test_torn_line_and_cut_mark_both_reported(self):
        self.write(("x", "CLEAN", 1, "t1", "al", "grid", 2))
        with open(self.path, "ab") as f:
            f.write(b"y\tb\tCLEAN\t1\tt1\tal\tgr")
        with self.assertLogs("image_review.review_db", "WARNING") as logs:
            db = ReviewDB(self.work_dir, sources={})
        self.assertEqual(len(logs.records), 2)
        with self.assertRaises(ValueError) as ctx:
            db.decisions()
        text = str(ctx.exception)
        self.assertLess(text.index("unfinished line"), text.index("cut short"))


class TestReadOnlyTornLog(StoreTestCase):
    def test_read_only_store_never_writes(self):
        mark(self.store, ["batch_001/a.jpg"], "CLEAN")
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
            mark(ro, ["batch_001/b.jpg"], "DIRTY")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)


class TestPassMonotonic(StoreTestCase):
    def append_manifest_row(self):
        with open(self.work_dir / "manifest.tsv", "a", newline="") as f:
            csv.writer(f, delimiter="\t").writerow(["batch_002", "batch_002/e.jpg", "/src/patient_new/e.dcm"])
        self.store.close()
        self.store = LocalStore(self.work_dir)
        self.addCleanup(self.store.close)

    def mark_all(self, status, pass_number, keys):
        for key in keys:
            mark(self.store, [key], status, pass_number)

    def test_current_pass_stops_at_the_maximum_pass(self):
        keys = [r[1] for r in ROWS]
        self.mark_all("CLEAN", MAX_PASS, keys)  # accepted, and a fresh load takes the row back
        self.assertEqual(LocalStore(self.work_dir, read_only=True).current_pass(), MAX_PASS)
        self.assertEqual(self.store.current_pass(), MAX_PASS)  # not MAX_PASS + 1: the server would refuse it

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
        mark(self.store, ["batch_001/a.jpg"], "DIRTY", 3)
        result = mark(self.store, ["batch_001/a.jpg"], "CLEAN")
        self.assertEqual(result, {"batch_001/a.jpg": "CLEAN"})
        with open(self.work_dir / "review.tsv", newline="") as f:
            *_, row = csv.DictReader(f, delimiter="\t")  # the log's last row for the image wins
        self.assertEqual((row["status"], row["pass_number"]), ("CLEAN", "3"))


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
            db = ReviewDB(self.work_dir, sources={})
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
        self.store.mark(keys[:1], "CLEAN", 1, reviewer="alice", mode="single")
        self.store.mark(keys, "DIRTY", 1, reviewer="alice", mode="grid")
        changed = self.store.undo(1, reviewer="alice")
        self.assertEqual(changed, {keys[0]: "CLEAN", **dict.fromkeys(keys[1:], "UNREVIEWED")})
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
        mark(self.store, ["batch_001/a.jpg", "batch_002/c.jpg"], "DIRTY")
        mark(self.store, ["batch_001/b.jpg"], "CLEAN")
        mark(self.store, ["batch_002/c.jpg"], "DIRTY", 2)
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

    def test_summaries(self):
        self.assertEqual(
            batch_summary(self.rows, self.statuses),
            {
                "batch_001": Counter({"CLEAN": 1, "FLAGGED": 1}),
                "batch_002": Counter({"DIRTY": 1, "UNREVIEWED": 1}),
            },
        )
        self.assertEqual(
            summary(self.rows, self.statuses), Counter({"CLEAN": 1, "DIRTY": 1, "UNREVIEWED": 1, "FLAGGED": 1})
        )


if __name__ == "__main__":
    unittest.main()
