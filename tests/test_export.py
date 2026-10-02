import csv
import dataclasses
import errno
import grp
import hashlib
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from image_review import atomic as atomic_module
from image_review import cli as cli_module
from image_review import lock as lock_module
from image_review.export import has_unsafe_char
from image_review.lock import boot_id, this_process
from image_review.remote import RemoteStore
from image_review.review_db import HEADER, LEGACY_HEADER
from image_review.store import LocalStore
from tests.fixtures import ROWS, invoke_cli, make_work_dir, mark, start_server, temp_dir

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
    ("/src/a.dcm", "b1", "CLEAN", "1", T(1, 1), "alice", "single", "1", "0.1"),
    ("/src/b.dcm", "b1", "DIRTY", "1", T(1, 2), "alice", "single", "1", "0.1"),
    ("/src/b.dcm#icon", "b1", "CLEAN", "1", T(1, 2), "alice", "single", "1", "0.1"),
    ("/src/d.dcm", "b2", "DIRTY", "1", T(1, 3), "bob", "grid", "2", "0.1"),
    ("/src/e.dcm", "b2", "CLEAN", "1", T(1, 3), "bob", "grid", "2", "0.1"),
    ("/src/e.dcm", "b2", "UNREVIEWED", "1", T(1, 4), "bob", "undo", "1", "0.1"),  # tombstone
    ("/src/b.dcm", "b1", "DIRTY", "2", T(2, 5), "carol", "single", "1", "0.1"),
    ("/src/gone.dcm", "b9", "CLEAN", "1", T(2, 6), "carol", "single", "1", "0.1"),  # not in the manifest
    ("/src/h.dcm", "b2", "CLEAN", "1", T(2, 7), "dave", "single", "1", "0.1"),
    ("/src/h.dcm#icon", "b2", "DIRTY", "1", T(2, 7), "dave", "single", "1", "0.1"),
    ("/src/i.dcm", "b2", "CLEAN", "1", T(2, 8), "dave", "single", "1", "0.1"),
    ("/src/j.dcm", "b2", "CLEAN", "1", T(2, 8), "dave", "single", "1", "0.1"),
    ("/src/k.dcm", "b2", "CLEAN", "1", T(2, 9), "dave", "single", "1", "0.1"),
    ("/src/k.dcm", "b2", "DIRTY", "1", T(3, 1), "erin", "single", "1", "0.1"),
    ("/src/k.dcm", "b2", "CLEAN", "1", T(3, 2), "frank", "undo", "1", "0.1"),  # undo restores CLEAN
    ("/src/m.dcm#icon", "b2", "DIRTY", "1", T(3, 3), "erin", "single", "1", "0.1"),
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
# d.dcm is DIRTY from pass 1 while b.dcm is in pass 2 (the current pass is 1: c.dcm is unreviewed)
EXPECTED = (
    "image_id\tstatus\tpass_number\ttimestamp\treviewer\treason\tsource_sha256\n"
    f"/src/a.dcm\tCLEAN\t1\t2026-01-01T00:00:01+00:00\talice\t\t{sha('/src/a.dcm')}\n"
    f"/src/b.dcm\tDIRTY\t2\t2026-01-02T00:00:05+00:00\tcarol\t\t{sha('/src/b.dcm')}\n"
    f"/src/c.dcm\tUNREVIEWED\t\t\t\t\t{sha('/src/c.dcm')}\n"
    f"/src/d.dcm\tDIRTY\t1\t2026-01-01T00:00:03+00:00\tbob\t\t{sha('/src/d.dcm')}\n"
    f"/src/e.dcm\tUNREVIEWED\t\t\t\t\t{sha('/src/e.dcm')}\n"
    f"/src/h.dcm\tDIRTY\t1\t2026-01-02T00:00:07+00:00\tdave\ticon DIRTY\t{sha('/src/h.dcm')}\n"
    f"/src/i.dcm\tNOT_REVIEWED\t1\t2026-01-02T00:00:08+00:00\tdave\ticon: ValueError: bad icon\t{sha('/src/i.dcm')}\n"
    f"/src/j.dcm\tNOT_REVIEWED\t\t\t\tDecodeError: truncated\t{sha('/src/j.dcm')}\n"
    f"/src/k.dcm\tCLEAN\t1\t2026-01-03T00:00:02+00:00\tfrank\t\t{sha('/src/k.dcm')}\n"
    f"/src/m.dcm\tDIRTY\t\t\t\tmain image missing; icon DIRTY\t{sha('/src/m.dcm')}\n"
    "/src/f.dcm\tNOT_REVIEWED\t\t\t\tcannot decode pixel data\t\n"  # never rendered: no source hash
    "/src/g.zip::x.dcm\tNOT_REVIEWED\t\t\t\tunsupported: nested zip\t\n"
    "/src/notes.txt\tIGNORED\t\t\t\tnot an image\t\n"  # not an image: listed, never CLEAN
).encode()


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

    def export(self, *args, env=None):
        return invoke("export", "--work-dir", str(self.work), *args, env=env)


class TestExportRows(ExportTestCase):
    def test_stdout_matches_expected_bytes(self):
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout_bytes, EXPECTED)

    def test_flagged_exports_as_dirty(self):
        make_work_dir(self.work)  # the fixture's ROWS manifest
        (self.work / "review.tsv").unlink()
        (self.work / "skipped.tsv").unlink()
        with LocalStore(self.work) as store:
            mark(store, KEYS[:1], "DIRTY")
            store.mark(KEYS[1:], "CLEAN", 1, reviewer="tester", mode="grid")
        self.assertIn("FLAGGED:         1", invoke("status", "--work-dir", str(self.work)).stdout)
        lines = self.export().stdout.splitlines()
        self.assertEqual(lines[1].split("\t")[:3], [ROWS[0][2], "DIRTY", "1"])

    def test_legacy_header_review(self):
        write_tsv(
            self.work / "review.tsv",
            LEGACY_HEADER,
            [("/src/a.dcm", "b1", "DIRTY", "1", "t1"), ("/src/a.dcm", "b1", "CLEAN", "2", "t2")],
        )
        before = (self.work / "review.tsv").read_bytes()
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            result.stdout.splitlines()[1], f"/src/a.dcm\tCLEAN\t2\tt2\t\t\t{sha('/src/a.dcm')}"
        )  # no reviewer
        self.assertEqual((self.work / "review.tsv").read_bytes(), before)  # a reader does not migrate it

    def test_without_skipped_or_review_files(self):
        (self.work / "skipped.tsv").unlink()
        (self.work / "review.tsv").unlink()
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        rows = [line.split("\t") for line in result.stdout.splitlines()[1:]]
        files = dict.fromkeys(m[2].removesuffix("#icon") for m in MANIFEST)
        expected = [[f, "NOT_REVIEWED" if f == "/src/m.dcm" else "UNREVIEWED"] for f in files]  # m: icon only
        self.assertEqual([r[:2] for r in rows], expected)

    def test_clean_icon_without_its_file_is_not_clean(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/s.jpg", "/src/scan.dcm#icon")])
        write_tsv(
            self.work / "review.tsv",
            HEADER,
            [("/src/scan.dcm#icon", "b1", "CLEAN", "1", "t", "al", "single", "1", "0.1")],
        )
        (self.work / "skipped.tsv").unlink()  # scan.dcm is in neither file (an ignored one says why instead, below)
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout.splitlines()[1], "/src/scan.dcm\tNOT_REVIEWED\t\t\t\tmain image missing\t")

    def test_ignored_main_with_an_icon_is_one_not_reviewed_row_saying_why(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/s.jpg", "/src/scan.dcm#icon")])
        (self.work / "review.tsv").unlink()
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n/src/scan.dcm\tignored\tDICOMDIR\n")
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            result.stdout.splitlines()[1:], ["/src/scan.dcm\tNOT_REVIEWED\t\t\t\tDICOMDIR; icon UNREVIEWED\t"]
        )

    def test_failed_and_ignored_is_one_not_reviewed_row(self):
        (self.work / "skipped.tsv").write_text(
            "image_id\tkind\treason\n/src/x.dcm\tignored\tnot an image\n/src/x.dcm\tfailed\tbad\n"
        )
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            [line for line in result.stdout.splitlines() if line.startswith("/src/x.dcm")],
            ["/src/x.dcm\tNOT_REVIEWED\t\t\t\tbad\t"],
        )

    def test_clean_image_also_ignored_is_not_clean(self):
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n/src/a.dcm\tignored\tnot an image\n")
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        lines = [line for line in result.stdout.splitlines() if line.startswith("/src/a.dcm")]
        self.assertEqual(lines, [f"/src/a.dcm\tNOT_REVIEWED\t\t\t\tnot an image\t{sha('/src/a.dcm')}"])

    def test_ignored_icon_name_is_its_own_row(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/a.jpg", "/src/a.dcm")])
        (self.work / "skipped.tsv").write_text(
            "image_id\tkind\treason\n"
            "/src/a.dcm#icon\tignored\tnot an image\n"  # a file named so: not folded into a.dcm
            "/src/z.dcm#icon\tignored\tnot an image\n"
            "/src/a.dcm#icon\tignored\tsecond reason\n"  # repeated: one row, first reason
        )
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            result.stdout.splitlines()[1:],
            [
                "/src/a.dcm\tCLEAN\t1\t2026-01-01T00:00:01+00:00\talice\t\t",
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
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            result.stdout.splitlines()[1:],
            ["/src/h.dcm\tNOT_REVIEWED\t1\t2026-01-02T00:00:07+00:00\tdave\ticon: not an image\t"],  # both CLEAN
        )

    def test_unsafe_ignored_image_id_is_refused(self):
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n/src/a\x85b.pdf\tignored\tnot an image\n")
        result = self.export()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("cannot export '/src/a\\x85b.pdf'", result.output)
        self.assertEqual(result.stdout, "")

    def test_malformed_skipped_is_a_clean_error(self):
        (self.work / "skipped.tsv").write_text("wrong\n")
        result = self.export()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("skipped.tsv:1", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_quote_is_written_as_is(self):
        write_tsv(self.work / "manifest.tsv", LEGACY_MANIFEST_HEADER, [("b1", "b1/q.jpg", '/src/"q".dcm')])
        result = self.export()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            result.stdout_bytes.splitlines()[1], b'/src/"q".dcm\tUNREVIEWED\t\t\t\t\t'
        )  # legacy manifest: no hash

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
            [("/src/a.dcm", "b1", "CLEAN", "1", "t", '"mallory', "single", "1", "0.1")],
        )
        result = self.export()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("cannot export '/src/a.dcm': its reviewer", result.output)
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
                self.assertEqual(out.read_bytes(), EXPECTED)
                self.assertEqual(stat.S_IMODE(out.stat().st_mode), file_mode)
                self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".")), [])

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
        self.assertEqual(out.read_bytes(), EXPECTED)
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
        self.assertEqual(out.read_bytes(), EXPECTED)
        self.assertEqual(out.stat().st_nlink, 1)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["result.tsv", "work"])

    def test_interrupted_write_leaves_nothing(self):
        out = self.root / "result.tsv"
        with mock.patch.object(atomic_module.os, "link", side_effect=KeyboardInterrupt):
            result = self.export("--output", str(out))
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["work"])

    def test_remote_is_refused(self):
        result = invoke("export", "--remote", "ir://127.0.0.1:1/?token=x&fp=y")
        self.assertEqual(result.exit_code, 2)
        self.assertIn("image_ids stay on the server", result.output)

    def test_remote_environment_is_ignored(self):
        result = self.export(env={"IMAGE_REVIEW_REMOTE": "ir://127.0.0.1:1/?x"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout_bytes, EXPECTED)


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
        self.assertEqual(result.stdout_bytes, EXPECTED)
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
                self.assertEqual(result.stdout_bytes, b"" if code else EXPECTED)

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
        self.assertEqual(result.stdout_bytes, EXPECTED)
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

    def test_remote(self):
        _, target, stop = start_server(self.work)
        self.addCleanup(stop)
        uri = target.to_uri()
        self.assertEqual(self.check("--remote", uri), 1)
        with RemoteStore(target) as store:
            mark(store, KEYS[:1], "DIRTY")
            store.mark(KEYS[1:], "CLEAN", 1, reviewer="tester", mode="grid")
        self.assertEqual(self.check("--remote", uri), 0)  # pass 2, one FLAGGED: every image has a verdict
        (self.work / "skipped.tsv").write_text("image_id\tkind\treason\n/src/x.dcm\tfailed\tbad\n")
        self.assertEqual(self.check("--remote", uri), 1)


if __name__ == "__main__":
    unittest.main()
