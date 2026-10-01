"""CLI logging setup: -v/-q levels, the record format, and routing through tqdm during preprocess."""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from image_review.cli import PACKAGE_LOGGER, cli
from tests.fixtures import make_work_dir

ENV = {"IMAGE_REVIEW_REMOTE": None, "IMAGE_REVIEW_VIA": None, "IMAGE_REVIEW_ACCESS": None}
LINE = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d %s image_review\.cli: %s"


def _emit_one_per_level(work_dir: Path) -> None:
    log = logging.getLogger(f"{PACKAGE_LOGGER}.cli")
    log.debug("debug-line")
    log.info("info-line")
    log.warning("warning-line")


class LevelFlagsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        make_work_dir(self.work)
        self.work.chmod(0o700)

    def status_stderr(self, *flags: str) -> str:
        # status logs nothing on a healthy work dir; patch in one record per level at the point it would warn
        with mock.patch("image_review.cli.warn_if_world_accessible", _emit_one_per_level):
            result = CliRunner().invoke(cli, [*flags, "status", "--work-dir", str(self.work)], env=ENV)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Overall: 4 images", result.stdout)
        self.assertNotIn("-line", result.stdout)
        return result.stderr

    def test_default_is_info(self):
        err = self.status_stderr()
        self.assertNotIn("debug-line", err)
        self.assertNotIn("opened work directory", err)
        self.assertRegex(err, LINE % ("INFO", "info-line"))
        self.assertRegex(err, LINE % ("WARNING", "warning-line"))

    def test_quiet_suppresses_info(self):
        for flag in ("-q", "--quiet"):
            with self.subTest(flag=flag):
                err = self.status_stderr(flag)
                self.assertNotIn("debug-line", err)
                self.assertNotIn("info-line", err)
                self.assertRegex(err, LINE % ("WARNING", "warning-line"))

    def test_verbose_enables_debug(self):
        for flag in ("-v", "--verbose"):
            with self.subTest(flag=flag):
                err = self.status_stderr(flag)
                self.assertRegex(err, LINE % ("DEBUG", "debug-line"))
                self.assertRegex(err, LINE % ("DEBUG", "opened work directory .* \\(read-only\\)"))
                self.assertRegex(err, LINE % ("INFO", "info-line"))

    def test_verbose_and_quiet_conflict(self):
        result = CliRunner().invoke(cli, ["-v", "-q", "status", "--work-dir", str(self.work)], env=ENV)
        self.assertEqual(result.exit_code, 2)
        self.assertIn("mutually exclusive", result.output)

    def test_setup_is_undone_after_the_command(self):
        package = logging.getLogger(PACKAGE_LOGGER)
        before = package.handlers[:], package.level, package.propagate
        self.status_stderr("-v")
        self.assertEqual((package.handlers, package.level, package.propagate), before)


class PreprocessRoutingTest(unittest.TestCase):
    def test_records_go_through_tqdm_write_during_preprocess(self):
        package = logging.getLogger(PACKAGE_LOGGER)
        seen = []

        def fake_run(*args, **kwargs):
            seen.append(package.handlers[:])
            logging.getLogger(f"{PACKAGE_LOGGER}.preprocess").warning("skipping x: bad")
            raise RuntimeError("stop here")

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("image_review.preprocess.run_preprocess", fake_run),
            mock.patch("tqdm.tqdm.write") as write,
        ):
            result = CliRunner().invoke(cli, ["preprocess", tmp, "--work-dir", str(Path(tmp) / "work")], env=ENV)
        self.assertIsInstance(result.exception, RuntimeError)
        self.assertEqual(len(seen[0]), 1)
        self.assertNotEqual(type(seen[0][0]), logging.StreamHandler)  # swapped for tqdm's handler
        (call,) = write.call_args_list
        self.assertRegex(call.args[0], r"WARNING image_review\.preprocess: skipping x: bad$")


if __name__ == "__main__":
    unittest.main()
