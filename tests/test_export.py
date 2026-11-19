import csv
import dataclasses
import errno
import grp
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from image_review import atomic as atomic_module
from image_review import cli as cli_module
from image_review import lock as lock_module
from image_review.export import (
    AMBIGUOUS_ICON,
    NOTE_KINDS,
    SHOWN_NOTE_KINDS,
    STALE_VERDICT,
    UNRENDERED_NOTE_KINDS,
    ExportRow,
    export_rows,
    has_unsafe_char,
    split_allowlist,
    unreviewed_content,
)
from image_review.lock import boot_id, this_process
from image_review.review_db import HEADER, LEGACY_HEADERS, Decision
from image_review.status import ImageId, Key
from image_review.store import LocalStore, ManifestEntry, NoteRow, SkippedRow
from tests.fixtures import ROWS, invoke_cli, make_work_dir, mark, temp_dir

KEYS = [key for _, key, _ in ROWS]
MANIFEST_HEADER = ["batch", "preprocessed_path", "image_id", "source_sha256", "jpeg_sha256"]
LEGACY_MANIFEST_HEADER = MANIFEST_HEADER[:3]


def sha(text: str) -> str:
    """A stand-in SHA-256 for a source or JPG (export never reads either)."""
    return hashlib.sha256(text.encode()).hexdigest()


# (batch, preprocessed_path, image_id); an icon shares its file's source hash
MANIFEST = [
    ("b1", "b1/a.jpg", "/src/a.dcm"),
    ("b1", "b1/a_again.jpg", "/src/a.dcm"),  # two keys share an image_id
    ("b1", "b1/b.jpg", "/src/b.dcm"),
    ("b1", "b1/b_icon.jpg", "/src/b.dcm#icon"),  # CLEAN icon of a DIRTY file
    ("b1", "b1/c.jpg", "/src/c.dcm"),
    ("b2", "b2/d.jpg", "/src/d.dcm"),
    ("b2", "b2/e.jpg", "/src/e.dcm"),
    ("b2", "b2/h.jpg", "/src/h.dcm"),
    ("b2", "b2/h_icon.jpg", "/src/h.dcm#icon"),  # DIRTY icon of a CLEAN file
    ("b2", "b2/i.jpg", "/src/i.dcm"),  # CLEAN; its icon failed (skipped.tsv)
    ("b2", "b2/j.jpg", "/src/j.dcm"),  # CLEAN, but also failed in skipped.tsv
    ("b2", "b2/k.jpg", "/src/k.dcm"),
    ("b2", "b2/m_icon.jpg", "/src/m.dcm#icon"),  # an icon without its file
]
MANIFEST_WITH_HASHES = [(b, k, i, sha(i.removesuffix("#icon")), sha(k)) for b, k, i in MANIFEST]
T = "2026-01-0{}T00:00:0{}+00:00".format
REVIEW = [
    ("/src/a.dcm", "b1", "CLEAN", "1", T(1, 1), "alice", "single", "1", "0.1", ""),
    ("/src/b.dcm", "b1", "DIRTY", "1", T(1, 2), "alice", "single", "1", "0.1", ""),
    ("/src/b.dcm#icon", "b1", "CLEAN", "1", T(1, 2), "alice", "single", "1", "0.1", ""),
    ("/src/d.dcm", "b2", "DIRTY", "1", T(1, 3), "bob", "grid", "2", "0.1", ""),
    ("/src/e.dcm", "b2", "CLEAN", "1", T(1, 3), "bob", "grid", "2", "0.1", ""),
    ("/src/e.dcm", "b2", "UNREVIEWED", "1", T(1, 4), "bob", "undo", "1", "0.1", ""),  # tombstone
    ("/src/b.dcm", "b1", "DIRTY", "2", T(2, 5), "carol", "single", "1", "0.1", ""),
    ("/src/gone.dcm", "b9", "CLEAN", "1", T(2, 6), "carol", "single", "1", "0.1", ""),  # not in the manifest
    ("/src/h.dcm", "b2", "CLEAN", "1", T(2, 7), "dave", "single", "1", "0.1", ""),
    ("/src/h.dcm#icon", "b2", "DIRTY", "1", T(2, 7), "dave", "single", "1", "0.1", ""),
    ("/src/i.dcm", "b2", "CLEAN", "1", T(2, 8), "dave", "single", "1", "0.1", ""),
    ("/src/j.dcm", "b2", "CLEAN", "1", T(2, 8), "dave", "single", "1", "0.1", ""),
    ("/src/k.dcm", "b2", "CLEAN", "1", T(2, 9), "dave", "single", "1", "0.1", ""),
    ("/src/k.dcm", "b2", "DIRTY", "1", T(3, 1), "erin", "single", "1", "0.1", ""),
    ("/src/k.dcm", "b2", "CLEAN", "1", T(3, 2), "frank", "undo", "1", "0.1", ""),  # undo restores CLEAN
    ("/src/m.dcm#icon", "b2", "DIRTY", "1", T(3, 3), "erin", "single", "1", "0.1", ""),
]
SKIPPED = (
    "image_id\tkind\treason\n"
    "/src/f.dcm\tfailed\tcannot decode pixel data\n"
    "/src/notes.txt\tignored\tnot an image\n"
    "/src/i.dcm#icon\tfailed\tValueError: bad icon\n"
    "/src/j.dcm\tfailed\tDecodeError: truncated\n"
    "/src/g.zip::x.dcm\tfailed\tunsupported: nested zip\n"
    "/src/f.dcm\tfailed\tcannot decode pixel data\n"  # repeated: one row
)
# Only a.dcm and k.dcm are CLEAN with a hash that no file that is not CLEAN shares
EXPECTED_ALLOWLIST = (
    "source_sha256\timage_id\tpass_number\ttimestamp\treviewer\n"
    f"{sha('/src/a.dcm')}\t/src/a.dcm\t1\t2026-01-01T00:00:01+00:00\talice\n"
    f"{sha('/src/k.dcm')}\t/src/k.dcm\t1\t2026-01-03T00:00:02+00:00\tfrank\n"
).encode()
REPORT_HEADER_LINE = "image_id\tstatus\tpass_number\ttimestamp\treviewer\treason\tsource_sha256"
# d.dcm is DIRTY from pass 1 while b.dcm is in pass 2 (the current pass is 1: c.dcm is unreviewed)
EXPECTED_REPORT = (
    f"{REPORT_HEADER_LINE}\n"
    f"/src/b.dcm\tDIRTY\t2\t2026-01-02T00:00:05+00:00\tcarol\t\t{sha('/src/b.dcm')}\n"
    f"/src/c.dcm\tUNREVIEWED\t\t\t\t\t{sha('/src/c.dcm')}\n"
    f"/src/d.dcm\tDIRTY\t1\t2026-01-01T00:00:03+00:00\tbob\t\t{sha('/src/d.dcm')}\n"
    f"/src/e.dcm\tUNREVIEWED\t\t\t\t\t{sha('/src/e.dcm')}\n"
    f"/src/h.dcm\tDIRTY\t1\t2026-01-02T00:00:07+00:00\tdave\ticon DIRTY\t{sha('/src/h.dcm')}\n"
    f"/src/i.dcm\tNOT_REVIEWED\t1\t2026-01-02T00:00:08+00:00\tdave\ticon: ValueError: bad icon\t{sha('/src/i.dcm')}\n"
    f"/src/j.dcm\tNOT_REVIEWED\t\t\t\tDecodeError: truncated\t{sha('/src/j.dcm')}\n"
    f"/src/m.dcm\tDIRTY\t\t\t\tmain image missing; icon DIRTY\t{sha('/src/m.dcm')}\n"
    "/src/f.dcm\tNOT_REVIEWED\t\t\t\tcannot decode pixel data\t\n"  # never rendered: no source hash
    "/src/g.zip::x.dcm\tNOT_REVIEWED\t\t\t\tunsupported: nested zip\t\n"
    "/src/notes.txt\tIGNORED\t\t\t\tnot an image\t\n"  # not an image: reported, never allowlisted
).encode()
NOTES_HEADER_LINE = "image_id\tkind\tdetail\n"
NO_HASH = "not allowlisted: no source_sha256 (work directory from an older version)"


def write_tsv(path: Path, header: list[str], rows: list[tuple[str, ...]]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(header)
        writer.writerows(rows)


def invoke(*args, env=None):
    return invoke_cli(*args, env=env)


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class ExportTestCase(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.work = self.root / "work"
        self.work.mkdir(mode=0o700)
        write_tsv(self.work / "manifest.tsv", MANIFEST_HEADER, MANIFEST_WITH_HASHES)
        write_tsv(self.work / "review.tsv", HEADER, REVIEW)
        (self.work / "skipped.tsv").write_text(SKIPPED)
        (self.work / "notes.tsv").write_text(NOTES_HEADER_LINE)

    def export(self, *args, env=None):
        return invoke("export", "--work-dir", str(self.work), *args, env=env)

    def report_lines(self, *args):
        """The report's data lines (its header checked), from an export with --report to a new file."""
        out = self.root / "report.tsv"
        out.unlink(missing_ok=True)
        result = self.export("--report", str(out), *args)
        self.assertEqual(result.exit_code, 0, result.output)
        lines = out.read_text().splitlines()
        self.assertEqual(lines[0], REPORT_HEADER_LINE)
        return lines[1:]


class TestExportRows(ExportTestCase):
    def test_stdout_matches_expected_bytes(self):
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout_bytes, EXPECTED_ALLOWLIST)
        self.assertIn(
            "2 files allowlisted; 11 in the report (4 DIRTY, 2 UNREVIEWED, 4 NOT_REVIEWED, 1 IGNORED, "
            "0 CLEAN not allowlisted)",
            result.stderr,
        )

    def test_report_matches_expected_bytes(self):
        out = self.root / "report.tsv"
        result = self.export("--report", str(out))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout_bytes, EXPECTED_ALLOWLIST)
        self.assertEqual(out.read_bytes(), EXPECTED_REPORT)

    def test_clean_without_hash_is_only_in_the_report(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/a.jpg", "/src/a.dcm")])
        (self.work / "skipped.tsv").unlink()
        self.assertEqual(self.report_lines(), [f"/src/a.dcm\tCLEAN\t1\t2026-01-01T00:00:01+00:00\talice\t{NO_HASH}\t"])
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout.splitlines()[1:], [])
        self.assertIn("0 files allowlisted; 1 in the report (", result.stderr)
        self.assertIn("1 CLEAN not allowlisted)", result.stderr)

    def test_clean_and_dirty_with_the_same_content_are_both_denied(self):
        same = sha("same bytes")
        write_tsv(
            self.work / "manifest.tsv",
            MANIFEST_HEADER,
            [("b1", "b1/a.jpg", "/src/a.dcm", same, sha("a")), ("b1", "b1/b.jpg", "/src/b.dcm", same, sha("b"))],
        )
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n")
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout.splitlines()[1:], [])
        self.assertEqual(
            self.report_lines(),
            [
                (
                    f"/src/a.dcm\tCLEAN\t1\t2026-01-01T00:00:01+00:00\talice\t"
                    f"not allowlisted: same content as a file that is not CLEAN\t{same}"
                ),
                f"/src/b.dcm\tDIRTY\t2\t2026-01-02T00:00:05+00:00\tcarol\t\t{same}",
            ],
        )

    def test_flagged_exports_as_dirty(self):
        make_work_dir(self.work)  # the fixture's ROWS manifest
        (self.work / "review.tsv").unlink()
        (self.work / "skipped.tsv").unlink()
        with LocalStore(self.work) as store:
            mark(store, KEYS[:1], "DIRTY")
            store.mark(KEYS[1:], "CLEAN", 1, reviewer="tester", mode="grid")
        self.assertIn("FLAGGED:         1", invoke("status", "--work-dir", str(self.work)).stdout)
        lines = self.report_lines()
        self.assertEqual(lines[0].split("\t")[:3], [ROWS[0][2], "DIRTY", "1"])

    def test_legacy_header_review(self):
        cases = {
            "five columns": (
                LEGACY_HEADERS[0],
                [("/src/a.dcm", "b1", "DIRTY", "1", "t1"), ("/src/a.dcm", "b1", "CLEAN", "2", "t2")],
                "",  # no reviewer
            ),
            "nine columns": (
                LEGACY_HEADERS[1],
                [
                    ("/src/a.dcm", "b1", "DIRTY", "1", "t1", "al", "single", "1", "0.4.0"),
                    ("/src/a.dcm", "b1", "CLEAN", "2", "t2", "bo", "single", "1", "0.4.0"),
                ],
                "bo",
            ),
        }
        for name, (header, rows, reviewer) in cases.items():
            with self.subTest(name):
                write_tsv(self.work / "review.tsv", header, rows)
                before = (self.work / "review.tsv").read_bytes()
                result = self.export()
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(result.stdout.splitlines()[1], f"{sha('/src/a.dcm')}\t/src/a.dcm\t2\tt2\t{reviewer}")
                self.assertEqual((self.work / "review.tsv").read_bytes(), before)  # a reader does not migrate it

    def test_without_skipped_rows_or_review_file(self):
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n")
        (self.work / "review.tsv").unlink()
        rows = [line.split("\t") for line in self.report_lines()]
        files = dict.fromkeys(m[2].removesuffix("#icon") for m in MANIFEST)
        expected = [[f, "NOT_REVIEWED" if f == "/src/m.dcm" else "UNREVIEWED"] for f in files]  # m: icon only
        self.assertEqual([r[:2] for r in rows], expected)

    def test_clean_icon_without_its_file_is_not_clean(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/s.jpg", "/src/scan.dcm#icon")])
        write_tsv(
            self.work / "review.tsv",
            HEADER,
            [("/src/scan.dcm#icon", "b1", "CLEAN", "1", "t", "al", "single", "1", "0.1", "")],
        )
        (self.work / "skipped.tsv").unlink()  # scan.dcm is in neither file (an ignored one says why instead, below)
        self.assertEqual(self.report_lines(), ["/src/scan.dcm\tNOT_REVIEWED\t\t\t\tmain image missing\t"])

    def test_ignored_main_with_an_icon_is_one_not_reviewed_row_saying_why(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/s.jpg", "/src/scan.dcm#icon")])
        (self.work / "review.tsv").unlink()
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n/src/scan.dcm\tignored\tDICOMDIR\n")
        self.assertEqual(self.report_lines(), ["/src/scan.dcm\tNOT_REVIEWED\t\t\t\tDICOMDIR; icon UNREVIEWED\t"])

    def test_failed_and_ignored_is_one_not_reviewed_row(self):
        (self.work / "skipped.tsv").write_text(
            "image_id\tkind\treason\n/src/x.dcm\tignored\tnot an image\n/src/x.dcm\tfailed\tbad\n"
        )
        self.assertEqual(
            [line for line in self.report_lines() if line.startswith("/src/x.dcm")],
            ["/src/x.dcm\tNOT_REVIEWED\t\t\t\tbad\t"],
        )

    def test_clean_image_also_ignored_is_not_clean(self):
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n/src/a.dcm\tignored\tnot an image\n")
        lines = [line for line in self.report_lines() if line.startswith("/src/a.dcm")]
        self.assertEqual(lines, [f"/src/a.dcm\tNOT_REVIEWED\t\t\t\tnot an image\t{sha('/src/a.dcm')}"])

    def test_ignored_icon_name_is_its_own_row(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/a.jpg", "/src/a.dcm")])
        (self.work / "skipped.tsv").write_text(
            "image_id\tkind\treason\n"
            "/src/a.dcm#icon\tignored\tnot an image\n"  # a file named so: not folded into a.dcm
            "/src/z.dcm#icon\tignored\tnot an image\n"
            "/src/a.dcm#icon\tignored\tsecond reason\n"  # repeated: one row, first reason
        )
        self.assertEqual(
            self.report_lines(),
            [
                f"/src/a.dcm\tCLEAN\t1\t2026-01-01T00:00:01+00:00\talice\t{NO_HASH}\t",  # legacy manifest
                "/src/a.dcm#icon\tIGNORED\t\t\t\tnot an image\t",
                "/src/z.dcm#icon\tIGNORED\t\t\t\tnot an image\t",
            ],
        )

    def test_ignored_icon_in_the_manifest_folds_into_its_file(self):
        write_tsv(
            self.work / "manifest.tsv",
            LEGACY_MANIFEST_HEADER,
            [("b1", "b1/h.jpg", "/src/h.dcm"), ("b1", "b1/h_icon.jpg", "/src/h.dcm#icon")],
        )
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n/src/h.dcm#icon\tignored\tnot an image\n")
        self.assertEqual(
            self.report_lines(),
            ["/src/h.dcm\tNOT_REVIEWED\t1\t2026-01-02T00:00:07+00:00\tdave\ticon: not an image\t"],  # both CLEAN
        )

    def test_unsafe_ignored_image_id_is_refused_even_without_report(self):
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n/src/a\x85b.pdf\tignored\tnot an image\n")
        result = self.export()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("cannot export '/src/a\\x85b.pdf'", result.output)
        self.assertEqual(result.stdout, "")
        out = self.root / "allowlist.tsv"
        result = self.export("--output", str(out))
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(out.exists())

    def test_hashed_manifest_without_skipped_is_refused(self):
        (self.work / "skipped.tsv").unlink()
        out = self.root / "allow.tsv"
        result = self.export("--output", str(out))
        self.assertEqual(result.exit_code, 1)
        self.assertIn("skipped.tsv: missing", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertFalse(out.exists())
        self.assertEqual(invoke("status", "--work-dir", str(self.work)).exit_code, 0)  # status only counts

    def test_legacy_manifest_without_skipped_still_exports(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/a.jpg", "/src/a.dcm")])
        (self.work / "skipped.tsv").unlink()
        self.assertEqual(self.export().exit_code, 0)

    def test_conflicting_source_hashes_for_one_image_id_are_refused(self):
        write_tsv(
            self.work / "manifest.tsv",
            MANIFEST_HEADER,
            [
                ("b1", "b1/a.jpg", "/src/a.dcm", sha("x"), sha("a")),
                ("b1", "b1/b.jpg", "/src/a.dcm", sha("y"), sha("b")),
            ],
        )
        result = self.export()
        self.assertEqual(result.exit_code, 1)
        self.assertIn(
            "manifest.tsv:3: image_id '/src/a.dcm' has a different source_sha256 than on line 2", result.output
        )

    def test_malformed_skipped_is_a_clean_error(self):
        (self.work / "skipped.tsv").write_text("wrong\n")
        result = self.export()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("skipped.tsv:1", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_quote_is_written_as_is(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/q.jpg", '/src/"q".dcm')])
        self.assertEqual(self.report_lines()[0], '/src/"q".dcm\tUNREVIEWED\t\t\t\t\t')  # legacy manifest: no hash

    def test_unsafe_image_id_is_refused(self):
        for image_id in (
            "/src/t\tb.dcm",
            "/src/a\nb.dcm",
            "/src/a\rb.dcm",
            "/src/a\x85b.dcm",
            "/src/a\u2028b.dcm",
            "/src/a\u2029b.dcm",
            "/src/a\x0bb.dcm",
            "/src/a\x7fb.dcm",
            '"/src/q.dcm',
        ):
            with self.subTest(image_id=image_id):
                write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/x.jpg", image_id)])
                out = self.root / "out.tsv"
                result = self.export("--output", str(out))
                self.assertEqual(result.exit_code, 1)
                self.assertIn(f"cannot export {image_id!r}", result.output)
                self.assertFalse(out.exists())

    def test_reviewer_starting_with_a_quote_is_refused(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/a.jpg", "/src/a.dcm")])
        write_tsv(
            self.work / "review.tsv",
            HEADER,
            [("/src/a.dcm", "b1", "CLEAN", "1", "t", '"mallory', "single", "1", "0.1", "")],
        )
        result = self.export()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("cannot export '/src/a.dcm': its reviewer", result.output)
        self.assertEqual(result.stdout, "")


def row(image_id: str, status: str, source_sha256: str, reason: str = "") -> ExportRow:
    return ExportRow(image_id, status, None, "", "", reason, source_sha256)


class TestSplitAllowlist(unittest.TestCase):
    def test_every_row_lands_in_exactly_one_list_in_order(self):
        rows = [
            row("/a", "CLEAN", "h1"),
            row("/b", "DIRTY", "h2"),
            row("/c", "CLEAN", ""),
            row("/d", "CLEAN", "h2"),  # same bytes as the DIRTY /b
            row("/e", "UNREVIEWED", "h3"),
            row("/f", "CLEAN", "h4"),
            row("/g", "IGNORED", ""),  # an empty hash is shared with nothing
            row("/h", "NOT_REVIEWED", "h5"),
            row("/i", "CLEAN", "h1"),  # same bytes as a CLEAN file: fine
        ]
        allowed, report = split_allowlist(rows)
        self.assertEqual([r.image_id for r in allowed], ["/a", "/f", "/i"])
        self.assertEqual([r.image_id for r in report], ["/b", "/c", "/d", "/e", "/g", "/h"])
        self.assertEqual(sorted(r.image_id for r in allowed + report), [r.image_id for r in rows])
        self.assertEqual(allowed, [rows[0], rows[5], rows[8]])  # unchanged
        self.assertEqual(
            [r.status for r in report], ["DIRTY", "CLEAN", "CLEAN", "UNREVIEWED", "IGNORED", "NOT_REVIEWED"]
        )

    def test_denied_clean_row_says_why(self):
        _, report = split_allowlist(
            [row("/a", "CLEAN", "", "icon note"), row("/b", "CLEAN", "h"), row("/c", "DIRTY", "h")]
        )
        self.assertEqual(
            [r.reason for r in report],
            [f"icon note; {NO_HASH}", "not allowlisted: same content as a file that is not CLEAN", ""],
        )

    def test_same_content_as_any_status_that_is_not_clean_is_denied(self):
        for other in ("DIRTY", "UNREVIEWED", "NOT_REVIEWED", "IGNORED"):
            with self.subTest(other=other):
                allowed, report = split_allowlist([row("/a", "CLEAN", "h"), row("/b", other, "h")])
                self.assertEqual(allowed, [])
                self.assertEqual([(r.image_id, r.status) for r in report], [("/a", "CLEAN"), ("/b", other)])
                self.assertEqual(report[0].reason, "not allowlisted: same content as a file that is not CLEAN")


class TestIconNamedFile(unittest.TestCase):
    """`X#icon` with a different hash from `X` may be X's icon or a file merely so named: both rows are NOT_REVIEWED."""

    @staticmethod
    def entry(image_id: str, source: str | None) -> ManifestEntry:
        return ManifestEntry(
            "b", Key(image_id), ImageId(image_id), source, None if source is None else sha("j" + image_id)
        )

    @staticmethod
    def clean(entries: list[ManifestEntry]) -> dict[ImageId, Decision]:
        return {
            e.image_id: Decision(
                image_id=e.image_id,
                batch="b",
                status="CLEAN",
                pass_number=1,
                timestamp="t",
                reviewer="r",
                mode="single",
                grid_size=1,
                tool_version="0.1",
                source_sha256="",
            )
            for e in entries
        }

    def pair(self, order: tuple[int, int]) -> list[ManifestEntry]:
        entries = [self.entry("scan.png#icon", sha("1")), self.entry("scan.png", sha("2"))]
        return [entries[i] for i in order]

    def test_differing_hashes_make_both_rows_not_reviewed_with_their_own_hashes(self):
        for order in ((0, 1), (1, 0)):
            with self.subTest(order=order):
                entries = self.pair(order)
                rows = export_rows(entries, self.clean(entries), [], {})
                self.assertEqual(
                    [(r.image_id, r.status, r.reason, r.source_sha256) for r in rows],
                    [
                        ("scan.png", "NOT_REVIEWED", AMBIGUOUS_ICON, sha("2")),
                        ("scan.png#icon", "NOT_REVIEWED", AMBIGUOUS_ICON, sha("1")),
                    ],
                )
                allowed, report = split_allowlist(rows)
                self.assertEqual(allowed, [])
                self.assertEqual(len(report), 2)

    def test_a_clean_file_with_either_hash_is_denied(self):
        for order in ((0, 1), (1, 0)):
            for other in (sha("1"), sha("2")):
                with self.subTest(order=order, other=other):
                    entries = [*self.pair(order), self.entry("other.png", other)]
                    allowed, report = split_allowlist(export_rows(entries, self.clean(entries), [], {}))
                    self.assertEqual(allowed, [])
                    denied = next(r for r in report if r.image_id == "other.png")
                    self.assertEqual(denied.status, "CLEAN")
                    self.assertEqual(denied.reason, "not allowlisted: same content as a file that is not CLEAN")

    def test_a_failed_icon_beside_a_file_named_like_it_is_not_reviewed(self):
        entries = [self.entry("scan.dcm", sha("2")), self.entry("scan.dcm#icon", sha("1"))]
        skipped = [SkippedRow(ImageId("scan.dcm#icon"), "failed", "ValueError: bad icon")]
        rows = export_rows(entries, self.clean(entries), skipped, {})
        self.assertEqual(
            [(r.image_id, r.status, r.reason) for r in rows],
            [
                ("scan.dcm", "NOT_REVIEWED", f"icon: ValueError: bad icon; {AMBIGUOUS_ICON}"),
                ("scan.dcm#icon", "NOT_REVIEWED", f"ValueError: bad icon; {AMBIGUOUS_ICON}"),
            ],
        )
        allowed, _ = split_allowlist(rows)
        self.assertEqual(allowed, [])

    def test_equal_hashes_or_a_legacy_manifest_fold_into_one_row(self):
        # (None, hash): an `a.dcm` listed without a hash never borrows its icon's.
        for main, icon in ((sha("1"), sha("1")), (None, None), (sha("1"), None), (None, sha("1"))):
            for icon_first in (True, False):
                with self.subTest(main=main, icon=icon, icon_first=icon_first):
                    entries = [self.entry("a.dcm#icon", icon), self.entry("a.dcm", main)]
                    if not icon_first:
                        entries.reverse()
                    rows = export_rows(entries, self.clean(entries), [], {})
                    self.assertEqual(
                        [(r.image_id, r.status, r.reason, r.source_sha256) for r in rows],
                        [("a.dcm", "CLEAN", "", main or "")],
                    )

    def test_the_ambiguous_icon_row_comes_where_it_first_appears(self):
        entries = [
            self.entry("scan.png", sha("2")),
            self.entry("other.png", sha("3")),
            self.entry("scan.png#icon", sha("1")),
        ]
        rows = export_rows(entries, self.clean(entries), [], {})
        self.assertEqual([r.image_id for r in rows], ["scan.png", "other.png", "scan.png#icon"])

    def test_a_dirty_part_of_an_ambiguous_pair_stays_dirty(self):
        for dirty in ("scan.png", "scan.png#icon"):
            with self.subTest(dirty=dirty):
                entries = self.pair((1, 0))
                decisions = self.clean(entries)
                decisions[ImageId(dirty)] = dataclasses.replace(decisions[ImageId(dirty)], status="DIRTY")
                rows = export_rows(entries, decisions, [], {})
                statuses = {r.image_id: r.status for r in rows}
                self.assertEqual(statuses[dirty], "DIRTY")
                self.assertTrue(all(AMBIGUOUS_ICON in r.reason for r in rows))
                allowed, _ = split_allowlist(rows)
                self.assertEqual(allowed, [])

    def test_icon_without_its_main_keeps_the_icons_hash(self):
        entries = [self.entry("a.dcm#icon", sha("1"))]
        rows = export_rows(entries, self.clean(entries), [], {})
        self.assertEqual(
            [(r.image_id, r.status, r.reason, r.source_sha256) for r in rows],
            [("a.dcm", "NOT_REVIEWED", "main image missing", sha("1"))],
        )

    def test_icon_of_a_failed_main_folds_as_not_reviewed_with_the_icons_hash(self):
        entries = [self.entry("a.dcm#icon", sha("1"))]
        rows = export_rows(entries, self.clean(entries), [SkippedRow(ImageId("a.dcm"), "failed", "bad")], {})
        self.assertEqual(
            [(r.image_id, r.status, r.reason, r.source_sha256) for r in rows],
            [("a.dcm", "NOT_REVIEWED", "bad", sha("1"))],
        )

    def test_an_empty_main_reason_adds_no_empty_note(self):
        entries = [self.entry("a.dcm", sha("1")), self.entry("a.dcm#icon", sha("1"))]
        decisions = self.clean(entries)
        icon = ImageId("a.dcm#icon")
        decisions[icon] = dataclasses.replace(decisions[icon], status="DIRTY")
        rows = export_rows(entries, decisions, [SkippedRow(ImageId("a.dcm"), "failed", "")], {})
        self.assertEqual([(r.image_id, r.status, r.reason) for r in rows], [("a.dcm", "DIRTY", "icon DIRTY")])

    def test_a_chain_of_icon_names_gives_each_hash_a_not_reviewed_row(self):
        entries = [self.entry("x", sha("1")), self.entry("x#icon", sha("2")), self.entry("x#icon#icon", sha("3"))]
        rows = export_rows(entries, self.clean(entries), [], {})
        self.assertEqual(
            [(r.image_id, r.status, r.reason, r.source_sha256) for r in rows],
            [
                ("x", "NOT_REVIEWED", AMBIGUOUS_ICON, sha("1")),
                ("x#icon", "NOT_REVIEWED", AMBIGUOUS_ICON, sha("2")),
                ("x#icon#icon", "NOT_REVIEWED", AMBIGUOUS_ICON, sha("3")),
            ],
        )
        self.assertEqual(len({r.image_id for r in rows}), len(rows))
        others = [self.entry(f"other{n}.png", sha(n)) for n in "123"]
        allowed, _ = split_allowlist(export_rows(entries + others, self.clean(entries + others), [], {}))
        self.assertEqual(allowed, [])


def note(image_id: str, kind: str, detail: str = "d") -> NoteRow:
    return NoteRow(ImageId(image_id), kind, detail)


ZERO_TRAILING = "12 bytes after the JPEG end; all zero bytes"


class TestUnreviewedContent(unittest.TestCase):
    """Notes of pixel content the review image does not show keep a file out of the allowlist."""

    entry = staticmethod(TestIconNamedFile.entry)
    clean = staticmethod(TestIconNamedFile.clean)

    def test_every_note_kind_is_classified_once(self):
        self.assertEqual(UNRENDERED_NOTE_KINDS & SHOWN_NOTE_KINDS, frozenset())
        self.assertEqual(UNRENDERED_NOTE_KINDS | SHOWN_NOTE_KINDS, frozenset(NOTE_KINDS))

    def test_each_unrendered_kind_blocks_and_shown_ones_do_not(self):
        for kind in NOTE_KINDS:
            with self.subTest(kind=kind):
                expected = {} if kind in SHOWN_NOTE_KINDS else {"a": f"unreviewed embedded content: {kind}"}
                self.assertEqual(unreviewed_content([note("a", kind)]), expected)

    def test_kinds_are_deduplicated_in_note_kinds_order(self):
        notes = [
            note("a", "trailing-data"),
            note("b", "alpha"),
            note("a", "icon-image"),
            note("a", "trailing-data", "other"),
            note("a", "overlay-plane"),
        ]
        self.assertEqual(
            unreviewed_content(notes),
            {"a": "unreviewed embedded content: overlay-plane, icon-image, trailing-data"},
        )

    def test_all_zero_trailing_data_does_not_block(self):
        self.assertEqual(unreviewed_content([note("a", "trailing-data", ZERO_TRAILING)]), {})
        not_zero = "12 bytes after the JPEG end; they begin with a JPEG signature"
        self.assertEqual(
            unreviewed_content([note("a", "trailing-data", not_zero)]),
            {"a": "unreviewed embedded content: trailing-data"},
        )

    def test_precedence_over_decisions_and_skipped_rows(self):
        entries = [self.entry(i, sha(i)) for i in ("clean", "dirty", "none", "failed", "ignored", "quiet")]
        decisions = self.clean(entries)
        decisions[ImageId("dirty")] = dataclasses.replace(decisions[ImageId("dirty")], status="DIRTY")
        del decisions[ImageId("none")]
        skipped = [SkippedRow(ImageId("failed"), "failed", "bad"), SkippedRow(ImageId("ignored"), "ignored", "odd")]
        why = "unreviewed embedded content: icon-image"
        unreviewed = {ImageId(i): why for i in ("clean", "dirty", "none", "failed", "ignored", "unknown")}
        rows = export_rows(entries, decisions, skipped, unreviewed)
        self.assertEqual(
            [(r.image_id, r.status, r.pass_number, r.reviewer, r.reason) for r in rows],
            [
                ("clean", "NOT_REVIEWED", None, "", why),  # decision fields empty, as for any NOT_REVIEWED
                ("dirty", "DIRTY", 1, "r", ""),
                ("none", "NOT_REVIEWED", None, "", why),
                ("failed", "NOT_REVIEWED", None, "", "bad"),
                ("ignored", "NOT_REVIEWED", None, "", "odd"),
                ("quiet", "CLEAN", 1, "r", ""),
            ],  # "unknown" is in neither file: no row
        )
        allowed, _ = split_allowlist(rows)
        self.assertEqual([r.image_id for r in allowed], ["quiet"])

    def test_a_legacy_icon_folds_with_its_files_note(self):
        entries = [self.entry("a.dcm", sha("1")), self.entry("a.dcm#icon", sha("1"))]
        why = "unreviewed embedded content: icon-image"
        rows = export_rows(entries, self.clean(entries), [], {ImageId("a.dcm"): why})
        self.assertEqual([(r.image_id, r.status, r.reason) for r in rows], [("a.dcm", "NOT_REVIEWED", why)])

    def test_a_note_on_a_file_named_like_an_icon_folds_into_its_main(self):
        # equal hashes, so `a.png#icon` folds into `a.png`; its own note makes the folded row NOT_REVIEWED
        entries = [self.entry("a.png", sha("1")), self.entry("a.png#icon", sha("1"))]
        unreviewed = unreviewed_content([note("a.png#icon", "trailing-data", "9 bytes after the PNG end")])
        rows = export_rows(entries, self.clean(entries), [], unreviewed)
        self.assertEqual(
            [(r.image_id, r.status, r.reason) for r in rows],
            [("a.png", "NOT_REVIEWED", "icon: unreviewed embedded content: trailing-data")],
        )
        self.assertEqual(split_allowlist(rows)[0], [])


class TestStaleVerdict(unittest.TestCase):
    """A decision whose source_sha256 differs from the manifest's for that part is a verdict on other content."""

    entry = staticmethod(TestIconNamedFile.entry)

    @staticmethod
    def decided(entries: list[ManifestEntry], status: str, recorded: str) -> dict[ImageId, Decision]:
        """A `status` decision on every entry, recorded with source_sha256 `recorded`."""
        return {
            i: dataclasses.replace(d, status=status, source_sha256=recorded)
            for i, d in TestIconNamedFile.clean(entries).items()
        }

    def summary(self, rows: list[ExportRow]) -> list[tuple[str, str, int | None, str]]:
        return [(r.image_id, r.status, r.pass_number, r.reason) for r in rows]

    def test_a_stale_verdict_is_unreviewed_and_never_allowlisted(self):
        for status in ("CLEAN", "DIRTY"):
            with self.subTest(status=status):
                entries = [self.entry("a.dcm", sha("new"))]
                rows = export_rows(entries, self.decided(entries, status, sha("old")), [], {})
                self.assertEqual(self.summary(rows), [("a.dcm", "UNREVIEWED", None, STALE_VERDICT)])
                allowed, _ = split_allowlist(rows)
                self.assertEqual(allowed, [])

    def test_a_verdict_on_this_content_or_without_a_hash_stands(self):
        for recorded in (sha("1"), ""):  # "": recorded before the hash was, or migrated: trusted by image_id
            with self.subTest(recorded=recorded):
                entries = [self.entry("a.dcm", sha("1"))]
                rows = export_rows(entries, self.decided(entries, "CLEAN", recorded), [], {})
                self.assertEqual(self.summary(rows), [("a.dcm", "CLEAN", 1, "")])
                allowed, _ = split_allowlist(rows)
                self.assertEqual([r.image_id for r in allowed], ["a.dcm"])

    def test_a_legacy_manifest_has_nothing_to_compare(self):
        entries = [self.entry("a.dcm", None)]
        rows = export_rows(entries, self.decided(entries, "DIRTY", sha("old")), [], {})
        self.assertEqual(self.summary(rows), [("a.dcm", "DIRTY", 1, "")])

    def test_skipped_rows_and_unreviewed_content_take_precedence(self):
        entries = [self.entry(i, sha("new")) for i in ("failed", "noted")]
        why = "unreviewed embedded content: icon-image"
        skipped = [SkippedRow(ImageId("failed"), "failed", "bad")]
        # a stale DIRTY is no decision, so the unreviewed content decides, as for an image never reviewed
        rows = export_rows(entries, self.decided(entries, "DIRTY", sha("old")), skipped, {ImageId("noted"): why})
        self.assertEqual(
            self.summary(rows), [("failed", "NOT_REVIEWED", None, "bad"), ("noted", "NOT_REVIEWED", None, why)]
        )

    def test_a_stale_icon_makes_its_file_unreviewed(self):
        entries = [self.entry("a.dcm", sha("1")), self.entry("a.dcm#icon", sha("1"))]
        decisions = self.decided(entries, "CLEAN", sha("1"))
        icon = ImageId("a.dcm#icon")
        decisions[icon] = dataclasses.replace(decisions[icon], source_sha256=sha("old"))
        rows = export_rows(entries, decisions, [], {})
        self.assertEqual(self.summary(rows), [("a.dcm", "UNREVIEWED", 1, f"icon UNREVIEWED: {STALE_VERDICT}")])
        allowed, _ = split_allowlist(rows)
        self.assertEqual(allowed, [])


class TestExportStaleVerdict(unittest.TestCase):
    """End to end: a verdict given, then the file's hash in the manifest changes (replaced and preprocessed again)."""

    def setUp(self):
        self.work = temp_dir(self)
        make_work_dir(self.work, hashed=True)
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n")
        (self.work / "notes.tsv").write_text(NOTES_HEADER_LINE)

    def test_changed_file_is_unreviewed_and_not_allowlisted(self):
        with LocalStore(self.work) as store:
            mark(store, KEYS, "CLEAN")
        changed = ROWS[0][2]
        with open(self.work / "manifest.tsv", newline="") as f:
            header, *rows = csv.reader(f, delimiter="\t")
        write_tsv(
            self.work / "manifest.tsv",
            header,
            [(*r[:3], sha("replaced") if r[2] == changed else r[3], r[4]) for r in rows],
        )
        out = self.work.parent / f"{self.work.name}-report.tsv"
        self.addCleanup(out.unlink, missing_ok=True)
        result = invoke("export", "--work-dir", str(self.work), "--report", str(out))
        self.assertEqual(result.exit_code, 0, result.output)
        allowlisted = [line.split("\t")[1] for line in result.stdout.splitlines()[1:]]
        self.assertEqual(allowlisted, [r[2] for r in ROWS[1:]])
        report = [line.split("\t") for line in out.read_text().splitlines()[1:]]
        self.assertEqual(report, [[changed, "UNREVIEWED", "", "", "", STALE_VERDICT, sha("replaced")]])
        status = invoke("status", "--work-dir", str(self.work)).stdout
        self.assertIn("UNREVIEWED:      1", status)
        self.assertIn("Current pass: 1", status)


class TestExportNotes(ExportTestCase):
    def write_notes(self, *rows: str) -> None:
        (self.work / "notes.tsv").write_text(NOTES_HEADER_LINE + "".join(f"{r}\n" for r in rows))

    def allowlisted(self) -> list[str]:
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        return [line.split("\t")[1] for line in result.stdout.splitlines()[1:]]

    def test_notes_deny_clean_files_and_leave_the_rest(self):
        self.write_notes(
            "/src/a.dcm\ticon-image\tIconImageSequence: 64x64 MONOCHROME2",
            "/src/b.dcm\tembedded-thumbnail\tEXIF IFD1 thumbnail (4096 bytes)",
            "/src/c.dcm\ttrailing-data\t9 bytes after the JPEG end",
            "/src/j.dcm\ticon-image\tx",
            f"/src/k.dcm\ttrailing-data\t{ZERO_TRAILING}",
            "/src/k.dcm\talpha\t1 of 2 pixels not opaque",
            "/src/gone.dcm\ticon-image\tx",  # not in the manifest: ignored
        )
        self.assertEqual(self.allowlisted(), ["/src/k.dcm"])
        lines = {line.split("\t")[0]: line for line in self.report_lines()}
        self.assertEqual(
            lines["/src/a.dcm"],
            f"/src/a.dcm\tNOT_REVIEWED\t\t\t\tunreviewed embedded content: icon-image\t{sha('/src/a.dcm')}",
        )
        self.assertEqual(
            lines["/src/b.dcm"], f"/src/b.dcm\tDIRTY\t2\t2026-01-02T00:00:05+00:00\tcarol\t\t{sha('/src/b.dcm')}"
        )
        self.assertEqual(
            lines["/src/c.dcm"],
            f"/src/c.dcm\tNOT_REVIEWED\t\t\t\tunreviewed embedded content: trailing-data\t{sha('/src/c.dcm')}",
        )
        self.assertEqual(
            lines["/src/j.dcm"], f"/src/j.dcm\tNOT_REVIEWED\t\t\t\tDecodeError: truncated\t{sha('/src/j.dcm')}"
        )
        self.assertNotIn("/src/gone.dcm", lines)

    def test_overlay_plane_blocks_even_when_drawn(self):
        # a drawn plane can be clipped to the image or lost against bright pixels: fail closed
        self.write_notes("/src/a.dcm\toverlay-plane\tgroup 0x6000", "/src/a.dcm\toverlay-unused-bits\tbit 15")
        for text in ('{"parameters": {"draw_overlays": true}}', None):
            with self.subTest(preprocess_json=text):
                (self.work / "preprocess.json").unlink(missing_ok=True)
                if text is not None:
                    (self.work / "preprocess.json").write_text(text)
                self.assertEqual(self.allowlisted(), ["/src/k.dcm"])
                self.assertIn(
                    "\tunreviewed embedded content: overlay-plane, overlay-unused-bits\t", self.report_lines()[0]
                )

    def test_unused_bits_deny_a_clean_file(self):
        # shown as a view of their own, but the view cannot vouch that every bit pattern was seen
        self.write_notes("/src/k.dcm\toverlay-unused-bits\tbits 8, 9, 10, 11")
        self.assertEqual(self.allowlisted(), ["/src/a.dcm"])
        self.assertIn(
            f"/src/k.dcm\tNOT_REVIEWED\t\t\t\tunreviewed embedded content: overlay-unused-bits\t{sha('/src/k.dcm')}",
            self.report_lines(),
        )

    def test_embedded_thumbnail_denies_a_clean_file(self):
        self.write_notes("/src/k.dcm\tembedded-thumbnail\tEXIF IFD1 thumbnail (4096 bytes)")
        self.assertEqual(self.allowlisted(), ["/src/a.dcm"])
        lines = {line.split("\t")[0]: line for line in self.report_lines()}
        self.assertEqual(
            lines["/src/k.dcm"],
            f"/src/k.dcm\tNOT_REVIEWED\t\t\t\tunreviewed embedded content: embedded-thumbnail\t{sha('/src/k.dcm')}",
        )

    def test_hashed_manifest_without_notes_is_refused(self):
        (self.work / "notes.tsv").unlink()
        out = self.root / "allow.tsv"
        result = self.export("--output", str(out))
        self.assertEqual(result.exit_code, 1)
        self.assertIn("notes.tsv: missing; work directories made before 0.4.0 have none", result.output)
        self.assertIn("preprocess the sources again into a new work directory", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertFalse(out.exists())

    def test_legacy_manifest_without_notes_still_exports(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/a.jpg", "/src/a.dcm")])
        (self.work / "notes.tsv").unlink()
        self.assertEqual(self.allowlisted(), [])  # a legacy CLEAN row has no hash: never allowlisted

    def test_malformed_notes_is_a_clean_error(self):
        cases = {
            "bad header": (b"image_id\tkind\treason\n", "notes.tsv:1:"),
            "short row": (b"image_id\tkind\tdetail\n/src/a.dcm\ticon-image\n", "notes.tsv:2:"),
            "unknown kind": (b"image_id\tkind\tdetail\n/src/a.dcm\tpreamble\tx\n", "notes.tsv:2:"),
            "not UTF-8": (b"image_id\tkind\tdetail\n\xe9\talpha\tx\n", "notes.tsv: not valid UTF-8"),
        }
        for name, (data, message) in cases.items():
            with self.subTest(name):
                (self.work / "notes.tsv").write_bytes(data)
                result = self.export()
                self.assertEqual(result.exit_code, 1)
                self.assertIn(message, result.output)
                self.assertNotIn("Traceback", result.output)
                self.assertEqual(result.stdout, "")


class TestHasUnsafeChar(unittest.TestCase):
    def test_control_characters_and_line_separators(self):
        for text in ("a\tb", "a\nb", "\r", "\x00", "\x0b", "\x7f", "\x85", "\x9f", "\u2028", "\u2029"):
            with self.subTest(text=text):
                self.assertTrue(has_unsafe_char(text))
        for text in ("", "/src/a b.dcm", '"q', "\\x0a", "\u00e9\u65e5", "\u00a0", "\u200b"):
            with self.subTest(text=text):
                self.assertFalse(has_unsafe_char(text))


class TestExportCommand(ExportTestCase):
    def test_output_file_matches_and_mode_follows_policy(self):
        old = os.umask(0o022)  # would strip a group write bit from a plain create
        self.addCleanup(os.umask, old)
        for name, dir_mode, file_mode in [("private", 0o700, 0o600), ("group", 0o2770, 0o660)]:
            with self.subTest(name):
                os.chmod(self.work, dir_mode)
                out = self.root / f"{name}.tsv"
                result = self.export("--output", str(out))
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(result.stdout, "")
                self.assertEqual(out.read_bytes(), EXPECTED_ALLOWLIST)
                self.assertEqual(stat.S_IMODE(out.stat().st_mode), file_mode)
                self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".")), [])

    def test_report_file_matches_and_mode_follows_policy(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        for name, dir_mode, file_mode in [("private", 0o700, 0o600), ("group", 0o2770, 0o660)]:
            with self.subTest(name):
                os.chmod(self.work, dir_mode)
                report, out = self.root / f"{name}-report.tsv", self.root / f"{name}.tsv"
                result = self.export("--report", str(report), "--output", str(out))
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(report.read_bytes(), EXPECTED_REPORT)
                self.assertEqual(out.read_bytes(), EXPECTED_ALLOWLIST)
                for f in (report, out):
                    self.assertEqual(stat.S_IMODE(f.stat().st_mode), file_mode)

    def test_report_and_output_naming_the_same_file_is_refused(self):
        out = self.root / "result.tsv"
        result = self.export("--report", str(out), "--output", str(self.root / "work" / ".." / "result.tsv"))
        self.assertEqual(result.exit_code, 2)
        self.assertIn("same file", result.output)
        self.assertFalse(out.exists())

    def test_existing_report_writes_nothing(self):
        report, out = self.root / "report.tsv", self.root / "allowlist.tsv"
        report.write_text("keep me")
        for args in ((), ("--output", str(out))):
            with self.subTest(args=args):
                result = self.export("--report", str(report), *args)
                self.assertEqual(result.exit_code, 1)
                self.assertIn("already exists", result.output)
                self.assertEqual(result.stdout, "")
                self.assertFalse(out.exists())
                self.assertEqual(report.read_text(), "keep me")

    def test_existing_output_with_report_writes_nothing(self):
        report, out = self.root / "report.tsv", self.root / "allowlist.tsv"
        out.write_text("stale allowlist")
        result = self.export("--report", str(report), "--output", str(out))
        self.assertEqual(result.exit_code, 1)
        self.assertIn(f"{out} already exists", result.output)
        self.assertFalse(report.exists())
        self.assertEqual(out.read_text(), "stale allowlist")

    def test_dangling_symlink_counts_as_existing(self):
        out = self.root / "allowlist.tsv"
        out.symlink_to(self.root / "nowhere.tsv")
        result = self.export("--output", str(out))
        self.assertEqual(result.exit_code, 1)
        self.assertIn("already exists", result.output)
        self.assertFalse((self.root / "nowhere.tsv").exists())

    def test_group_output_gets_the_work_dir_group(self):
        others = sorted(set(os.getgroups()) - {os.getegid()})
        if not others:
            self.skipTest("the user belongs to only one group")
        os.chown(self.work, -1, others[0])
        os.chmod(self.work, 0o2770)
        out = self.root / "result.tsv"  # self.root is not setgid: the file would get the user's own group
        result = self.export("--output", str(out))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(out.stat().st_gid, others[0], grp.getgrgid(others[0]).gr_name)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o660)

    def test_file_stays_private_until_group_and_mode_are_set(self):
        os.chmod(self.work, 0o2770)
        seen = []
        real_fchown = os.fchown

        def spy_fchown(fd, uid, gid):
            seen.append(("fchown", stat.S_IMODE(os.fstat(fd).st_mode)))
            return real_fchown(fd, uid, gid)

        out = self.root / "result.tsv"
        old = os.umask(0o002)  # would let a plain create be group-readable from the start
        self.addCleanup(os.umask, old)
        with mock.patch.object(atomic_module.os, "fchown", spy_fchown):
            result = self.export("--output", str(out))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [("fchown", 0o600)])
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o660)

    def test_group_that_cannot_be_set_falls_back_to_private(self):
        os.chmod(self.work, 0o2770)
        out = self.root / "result.tsv"
        with mock.patch.object(atomic_module.os, "fchown", side_effect=PermissionError(errno.EPERM, "nope")):
            result = self.export("--output", str(out))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertIn("WARNING", result.stderr)
        self.assertIn("making it private (0600)", result.stderr)

    def test_refuses_to_overwrite(self):
        out = self.root / "result.tsv"
        out.write_text("keep me")
        result = self.export("--output", str(out))
        self.assertEqual(result.exit_code, 1)
        self.assertIn("already exists", result.output)
        self.assertEqual(out.read_text(), "keep me")
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".")], [])

    def test_without_hard_links_creates_directly(self):
        out = self.root / "result.tsv"
        with mock.patch.object(atomic_module.os, "link", side_effect=OSError(errno.EPERM, "no links")):
            result = self.export("--output", str(out))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(out.read_bytes(), EXPECTED_ALLOWLIST)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".")], [])

    def test_lost_link_reply_counts_as_made(self):
        real_link = os.link

        def lost_reply(src, dst):
            real_link(src, dst)
            raise FileExistsError(errno.EEXIST, "File exists")

        out = self.root / "result.tsv"
        with mock.patch.object(atomic_module.os, "link", lost_reply):
            result = self.export("--output", str(out))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(out.read_bytes(), EXPECTED_ALLOWLIST)
        self.assertEqual(out.stat().st_nlink, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["result.tsv", "work"])

    def test_interrupted_write_leaves_nothing(self):
        out = self.root / "result.tsv"
        with mock.patch.object(atomic_module.os, "link", side_effect=KeyboardInterrupt):
            result = self.export("--output", str(out))
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["work"])


class TestExportLiveOrTorn(ExportTestCase):
    def test_live_writer_is_refused(self):
        with LocalStore(self.work):  # a writer holds the lock
            result = self.export()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("work directory is in use", result.output)
        self.assertIn("--allow-live", result.output)
        self.assertEqual(result.stdout, "")

    def test_allow_live_warns_and_writes_nothing_in_the_work_dir(self):
        with LocalStore(self.work):
            before = sorted(p.name for p in self.work.iterdir())
            result = self.export("--allow-live")
            after = sorted(p.name for p in self.work.iterdir())
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout_bytes, EXPECTED_ALLOWLIST)
        self.assertIn("WARNING", result.stderr)
        self.assertIn("--allow-live", result.stderr)
        self.assertEqual(after, before)

    def test_writer_appearing_during_export_is_refused(self):
        lock = self.work / lock_module.LOCK_NAME
        appeared = lock_module.WorkDirLocked(lock, this_process())
        for args, code in (((), 1), (("--allow-live",), 0)):
            with self.subTest(args=args):
                answers = iter([None, appeared])  # free at the start, held by the end
                with mock.patch.object(cli_module, "live_writer", side_effect=lambda _, a=answers: next(a)):
                    result = self.export(*args)
                self.assertEqual(result.exit_code, code, result.output)
                self.assertIn("work directory is in use", result.output if code else result.stderr)
                self.assertEqual(result.stdout_bytes, b"" if code else EXPECTED_ALLOWLIST)

    def test_unreadable_lock_is_refused(self):
        (self.work / lock_module.LOCK_NAME).write_text("not json")
        result = self.export()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("unreadable or corrupt", result.output)

    def test_stale_lock_of_this_machine_is_ignored(self):
        if not boot_id():
            self.skipTest("no boot id: a lock's liveness cannot be checked here")
        holder = dataclasses.replace(this_process(), pid=dead_pid())
        (self.work / lock_module.LOCK_NAME).write_text(json.dumps(dataclasses.asdict(holder)))
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout_bytes, EXPECTED_ALLOWLIST)
        self.assertNotIn("in use", result.stderr)

    def test_torn_review_tail_is_refused(self):
        with open(self.work / "review.tsv", "ab") as f:
            f.write(b"/src/c.dcm\tb1\tDIRTY\t1\tt9\tal\tsin")
        before = (self.work / "review.tsv").read_bytes()
        for args in ((), ("--allow-live",)):
            with self.subTest(args=args):
                result = self.export(*args)
                self.assertEqual(result.exit_code, 1)
                self.assertIn("unfinished line", result.output)
                self.assertIn("`review`", result.output)
                self.assertEqual(result.stdout, "")
        self.assertEqual((self.work / "review.tsv").read_bytes(), before)

    def test_cut_short_mark_is_refused(self):
        with open(self.work / "review.tsv", "ab") as f:
            f.write(b"/src/c.dcm\tb1\tDIRTY\t2\tt9\tal\tgrid\t3\tv\t\r\n")
        before = (self.work / "review.tsv").read_bytes()
        for args in ((), ("--allow-live",)):
            with self.subTest(args=args):
                result = self.export(*args)
                self.assertEqual(result.exit_code, 1)
                self.assertIn("cut short", result.output)
                self.assertIn("1 of 3 rows", result.output)
                self.assertEqual(result.stdout, "")
        self.assertEqual((self.work / "review.tsv").read_bytes(), before)


class TestOversizedField(ExportTestCase):
    """A TSV field over csv's 128 KiB limit is a parse error, never an uncaught csv.Error."""

    BIG = "x" * 200_000

    def assertCleanExit(self, result, code):
        self.assertIn(type(result.exception), (type(None), SystemExit), repr(result.exception))  # not a csv.Error
        self.assertEqual(result.exit_code, code, result.output)
        self.assertNotIn("Traceback", result.output)

    def test_huge_torn_review_tail(self):
        with open(self.work / "review.tsv", "ab") as f:
            f.write(b"\x00" * 200_000)
        before = (self.work / "review.tsv").read_bytes()
        status = invoke("status", "--work-dir", str(self.work))
        self.assertCleanExit(status, 0)
        self.assertIn("Overall: 13 images", status.stdout)
        self.assertIn("ignoring the unfinished last line", status.stderr)
        export = self.export()
        self.assertCleanExit(export, 1)
        self.assertIn("unfinished line", export.output)
        self.assertEqual(export.stdout, "")
        self.assertEqual((self.work / "review.tsv").read_bytes(), before)

    def test_oversized_notes_field_names_file_and_line(self):
        (self.work / "notes.tsv").write_text(f"{NOTES_HEADER_LINE}/a\talpha\t{self.BIG}\n/b\talpha\tr\n")
        result = self.export()
        self.assertCleanExit(result, 1)
        self.assertRegex(result.stderr, r"notes\.tsv:2: field larger")

    def test_oversized_field_is_cannot_read_work_directory(self):
        good = "\t".join(MANIFEST_WITH_HASHES[0])
        cases = {
            "review.tsv": lambda: (self.work / "review.tsv").write_bytes(
                (self.work / "review.tsv").read_bytes() + f"{self.BIG}\tb1\r\n".encode() + b"x\tb1\r\n"
            ),
            "manifest.tsv": lambda: (self.work / "manifest.tsv").write_text(
                "\t".join(MANIFEST_HEADER) + f"\r\n{good}\r\nb\tk\t{self.BIG}\t\t\r\n{good}\r\n"
            ),
            "skipped.tsv": lambda: (self.work / "skipped.tsv").write_text(
                f"image_id\tkind\treason\r\n/a\tfailed\t{self.BIG}\r\n/b\tignored\tr\r\n"
            ),
        }
        # skipped.tsv is read after the store opens, so its error is a ClickException without the prefix
        prefix = {"review.tsv": "Cannot read work directory: ", "manifest.tsv": "Cannot read work directory: "}
        for name, corrupt in cases.items():
            with self.subTest(name):
                self.setUp()  # a fresh, valid work dir for each file
                corrupt()
                for command in ("status", "export"):
                    result = invoke(command, "--work-dir", str(self.work))
                    self.assertCleanExit(result, 1)
                    self.assertRegex(result.stderr, rf"{prefix.get(name, '')}.*{re.escape(name)}:\d+: field larger")


class TestStatusCheck(unittest.TestCase):
    def setUp(self):
        self.work = temp_dir(self)
        make_work_dir(self.work)

    def check(self, *args):
        result = invoke("status", "--check", *args)
        self.assertIn("Overall:", result.stdout)  # the report is printed either way
        return result.exit_code

    def local(self):
        return self.check("--work-dir", str(self.work))

    def test_unreviewed_fails(self):
        with LocalStore(self.work) as store:
            mark(store, KEYS[:3], "CLEAN")
        self.assertEqual(self.local(), 1)
        self.assertEqual(invoke("status", "--work-dir", str(self.work)).exit_code, 0)  # only with --check

    def test_flagged_in_pass_two_passes(self):
        # FLAGGED is a DIRTY verdict from an earlier pass: decided, so finished
        with LocalStore(self.work) as store:
            mark(store, KEYS[:1], "DIRTY")
            mark(store, KEYS[1:], "CLEAN")
        status = invoke("status", "--work-dir", str(self.work)).stdout
        self.assertIn("FLAGGED:         1", status)  # still reported, so a second pass remains available
        self.assertIn("UNREVIEWED:      0", status)
        self.assertEqual(self.local(), 0)

    def test_dirty_rolling_over_passes(self):
        # Re-marked DIRTY in pass 2: the pass ends, it is FLAGGED again in pass 3, and the study is still finished
        with LocalStore(self.work) as store:
            mark(store, KEYS[:1], "DIRTY")
            mark(store, KEYS[1:], "CLEAN")
            mark(store, KEYS[:1], "DIRTY", 2)
        self.assertIn("Current pass: 3", invoke("status", "--work-dir", str(self.work)).stdout)
        self.assertEqual(self.local(), 0)

    def test_dirty_verdicts_with_one_unreviewed_fails(self):
        # An image without a verdict keeps the current pass at 1, so earlier DIRTY verdicts show as DIRTY,
        # not FLAGGED: the two never coexist in one report. The unreviewed image alone makes it 1.
        with LocalStore(self.work) as store:
            mark(store, KEYS[:2], "DIRTY")
            mark(store, KEYS[2:3], "CLEAN")
        status = invoke("status", "--work-dir", str(self.work)).stdout
        self.assertIn("DIRTY:           2", status)
        self.assertIn("UNREVIEWED:      1", status)
        self.assertEqual(self.local(), 1)

    def test_failed_skip_fails(self):
        with LocalStore(self.work) as store:
            mark(store, KEYS, "CLEAN")
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n/src/x.dcm\tfailed\tbad\n")
        self.assertEqual(self.local(), 1)

    def test_all_reviewed_passes(self):
        with LocalStore(self.work) as store:
            mark(store, KEYS, "CLEAN")
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n/src/notes.txt\tignored\tnot an image\n")
        self.assertEqual(self.local(), 0)


if __name__ == "__main__":
    unittest.main()
