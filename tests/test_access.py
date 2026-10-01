"""Access policy: private (default) or group; nothing is ever world-accessible."""

import contextlib
import io
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from click.testing import CliRunner
from fixtures import make_work_dir, write_dicom

from image_review.access import (
    Modes,
    access_of,
    modes,
    world_access_warning,
    world_accessible,
)
from image_review.cli import cli
from image_review.preprocess import run_preprocess
from image_review.review_db import ReviewDB
from image_review.server import ReviewServer

ENV = {"IMAGE_REVIEW_REMOTE": None, "IMAGE_REVIEW_VIA": None, "IMAGE_REVIEW_ACCESS": None}


def _src(root: Path) -> Path:
    src = root / "src"
    src.mkdir()
    pixels = (np.linspace(0, 3000, 128 * 128).reshape(128, 128)).astype(np.uint16)
    write_dicom(src / "a.dcm", pixels)
    write_dicom(src / "b.dcm", pixels // 2)
    return src


def _modes_under(work: Path) -> dict[Path, int]:
    paths = [work, *work.rglob("*")]
    return {p: stat.S_IMODE(p.lstat().st_mode) for p in paths}


class ModesTest(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(modes("private"), Modes(0o700, 0o600, 0o077))
        self.assertEqual(modes("group"), Modes(0o2770, 0o660, 0o007))

    def test_no_policy_grants_other_bits(self):
        for access in ("private", "group"):
            m = modes(access)
            self.assertEqual(m.dir_mode & 0o007, 0)
            self.assertEqual(m.file_mode & 0o007, 0)
            self.assertTrue(m.umask & 0o007 == 0o007)

    def test_access_of(self):
        for mode, expected in [(0o700, "private"), (0o600, "private"), (0o2770, "group"), (0o770, "group"), (0o750, "private"), (0o775, "group"), (0o710, "private"), (0o720, "private"), (0o700 | stat.S_IFDIR, "private"), (0o2770 | stat.S_IFDIR, "group")]:
            with self.subTest(mode=oct(mode)):
                self.assertEqual(access_of(mode), expected)

    def test_world_accessible(self):
        self.assertFalse(world_accessible(0o2770))
        self.assertFalse(world_accessible(0o700))
        for mode in (0o755, 0o701, 0o702, 0o704):
            self.assertTrue(world_accessible(mode))


class PreprocessAccessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        os.chmod(self.root, 0o755)  # a permissive parent must not leak into the work dir
        self.src = _src(self.root)

    def run_pre(self, work: Path, access=None):
        with contextlib.redirect_stderr(io.StringIO()):
            if access is None:
                return run_preprocess([self.src], work)
            return run_preprocess([self.src], work, access=access)

    def check(self, work: Path, dir_mode: int, file_mode: int):
        found = _modes_under(work)
        self.assertGreater(len(found), 4)
        for path, mode in found.items():
            with self.subTest(path=path.relative_to(self.root).as_posix()):
                self.assertEqual(mode & 0o007, 0)
                self.assertEqual(mode, dir_mode if path.is_dir() else file_mode)

    def test_default_is_private(self):
        work = self.root / "work"
        self.run_pre(work)
        self.check(work, 0o700, 0o600)

    def test_private_with_two_batches(self):
        work = self.root / "work"
        with contextlib.redirect_stderr(io.StringIO()):
            run_preprocess([self.src], work, batch_size=1, access="private")
        self.assertTrue((work / "batch_002").is_dir())
        self.check(work, 0o700, 0o600)

    def test_group(self):
        work = self.root / "work"
        with contextlib.redirect_stderr(io.StringIO()):
            run_preprocess([self.src], work, batch_size=1, access="group")
        self.assertTrue((work / "batch_002").is_dir())
        self.check(work, 0o2770, 0o660)

    def test_umask_restored(self):
        before = os.umask(0o022)
        self.addCleanup(os.umask, before)
        self.run_pre(self.root / "work", "group")
        self.assertEqual(os.umask(0o022), 0o022)

    def test_umask_restored_on_error(self):
        before = os.umask(0o022)
        self.addCleanup(os.umask, before)
        with mock.patch("image_review.preprocess._process", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_pre(self.root / "work", "private")
        self.assertEqual(os.umask(0o022), 0o022)

    def test_setgid_parent_group_is_inherited(self):
        other_groups = [g for g in os.getgroups() if g != os.getegid()]
        if not other_groups:
            self.skipTest("no supplementary group to chown to")
        parent = self.root / "study"
        parent.mkdir()
        os.chown(parent, -1, other_groups[0])
        os.chmod(parent, 0o2770)
        work = parent / "work"
        self.run_pre(work, "group")
        for path in _modes_under(work):
            self.assertEqual(path.stat().st_gid, other_groups[0], path)
        self.check(work, 0o2770, 0o660)

    def test_default_acl_on_parent_cannot_add_other_bits(self):
        parent = self.root / "acl"
        parent.mkdir()
        try:
            subprocess.run(["setfacl", "-d", "-m", "o::rwx,g::rwx", str(parent)], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("setfacl or ACLs unavailable")
        for access, dir_mode, file_mode in [("private", 0o700, 0o600), ("group", 0o2770, 0o660)]:
            with self.subTest(access):
                work = parent / access
                self.run_pre(work, access)
                for path, mode in _modes_under(work).items():
                    self.assertEqual(mode & 0o007, 0, path)
                    if path.is_file():
                        self.assertEqual(mode & ~0o660, 0, path)


class CliAccessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.src = _src(self.root)

    def invoke(self, *args, env=None):
        return CliRunner().invoke(cli, list(args), env={**ENV, **(env or {})})

    def test_option_and_envvar(self):
        for name, args, env, dir_mode, file_mode in [
            ("opt_group", ["--access", "group"], None, 0o2770, 0o660),
            ("opt_private", ["--access", "private"], None, 0o700, 0o600),
            ("env_group", [], {"IMAGE_REVIEW_ACCESS": "group"}, 0o2770, 0o660),
            ("default", [], None, 0o700, 0o600),
        ]:
            with self.subTest(name):
                work = self.root / name
                result = self.invoke("preprocess", str(self.src), "--work-dir", str(work), *args, env=env)
                self.assertEqual(result.exit_code, 0, result.output)
                found = _modes_under(work)
                self.assertEqual(stat.S_IMODE(work.stat().st_mode), dir_mode)
                self.assertEqual(stat.S_IMODE((work / "manifest.tsv").stat().st_mode), file_mode)
                self.assertTrue(all(m & 0o007 == 0 for m in found.values()))

    def test_group_reports_the_unix_group(self):
        import grp

        work = self.root / "shared"
        result = self.invoke("preprocess", str(self.src), "--work-dir", str(work), "--access", "group")
        gid = work.stat().st_gid
        self.assertIn(f"Shared with Unix group '{grp.getgrgid(gid).gr_name}' (gid {gid})", result.output)
        private = self.invoke("preprocess", str(self.src), "--work-dir", str(self.root / "p"))
        self.assertNotIn("Shared with", private.output)

    def test_bad_value_rejected(self):
        result = self.invoke("preprocess", str(self.src), "--access", "world")
        self.assertEqual(result.exit_code, 2)


class ReviewDbModeTest(unittest.TestCase):
    def mark_in(self, dir_mode: int) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            os.chmod(work, dir_mode)
            db = ReviewDB(work)
            db.mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
            db.mark("y", "batch_001", "DIRTY", 1, reviewer="tester", mode="single")
            self.assertEqual(sorted(p.name for p in work.iterdir()), ["review.tsv"])
            return stat.S_IMODE((work / "review.tsv").stat().st_mode)

    def test_private(self):
        self.assertEqual(self.mark_in(0o700), 0o600)

    def test_group(self):
        self.assertEqual(self.mark_in(0o2770), 0o660)

    def test_group_work_dir_from_preprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with contextlib.redirect_stderr(io.StringIO()):
                run_preprocess([_src(root)], root / "work", access="group")
            ReviewDB(root / "work").mark("x", "batch_001", "CLEAN", 1, reviewer="tester", mode="single")
            self.assertEqual(stat.S_IMODE((root / "work" / "review.tsv").stat().st_mode), 0o660)


class WarningTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name) / "work"
        self.work.mkdir()
        make_work_dir(self.work)

    def invoke_status(self):
        return CliRunner().invoke(cli, ["status", "--work-dir", str(self.work)], env=ENV)

    def invoke_serve(self):
        with mock.patch.object(ReviewServer, "serve_forever", side_effect=KeyboardInterrupt), mock.patch.dict(os.environ, {"HOME": str(self.work.parent)}):
            return CliRunner().invoke(cli, ["serve", "--work-dir", str(self.work), "--bind", "127.0.0.1"], env=ENV)

    def test_warns_for_world_readable_dir(self):
        os.chmod(self.work, 0o755)
        result = self.invoke_status()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"warning: {self.work} is accessible to all users (mode 0755); run `chmod -R o-rwx {shlex.quote(str(self.work))}`", result.stderr)
        self.assertNotIn("warning", result.stdout)

    def test_warns_for_world_readable_manifest(self):
        os.chmod(self.work, 0o700)
        os.chmod(self.work / "manifest.tsv", 0o644)
        self.assertIn(f"warning: {self.work / 'manifest.tsv'} is accessible", self.invoke_status().stderr)

    def test_serve_warns(self):
        os.chmod(self.work, 0o755)
        self.assertIn("is accessible to all users (mode 0755)", self.invoke_serve().stderr)

    def test_malformed_work_dir_is_a_clean_error(self):
        (self.work / "review.tsv").write_text("image_id\tbatch\tstatus\tpass_number\ttimestamp\nx\tb\tdirty\t1\tt\n")
        for result in (self.invoke_status(), self.invoke_serve()):
            with self.subTest():
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("Cannot read work directory: ", result.stderr)
                self.assertIn("review.tsv:2:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_silent_for_private_and_group(self):
        for mode in (0o700, 0o2770):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.work, mode)
                os.chmod(self.work / "manifest.tsv", 0o600)
                self.assertEqual(self.invoke_status().stderr, "")
                self.assertNotIn("warning", self.invoke_serve().stderr)
                self.assertIsNone(world_access_warning(self.work))


if __name__ == "__main__":
    unittest.main()
