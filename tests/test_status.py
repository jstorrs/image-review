import os
import unittest
from collections import Counter

from image_review.cli import status_report
from image_review.status import MARK_MODES, VERDICTS, parse_choice
from image_review.store import SkippedCounts
from tests.fixtures import ROWS, StoreTestCase, invoke_cli, mark, temp_dir

KEYS = [key for _, key, _ in ROWS]


class ParseChoiceTest(unittest.TestCase):
    def test_returns_the_member(self):
        self.assertEqual(parse_choice("CLEAN", VERDICTS), "CLEAN")
        self.assertEqual(parse_choice("grid", MARK_MODES), "grid")

    def test_non_members_are_none(self):
        for value in ("clean", "", "single", None, 1, b"CLEAN"):
            with self.subTest(value=value):
                self.assertIsNone(parse_choice(value, VERDICTS))


SKIPPED_TSV = "image_id\tkind\treason\n/src/p/x.dcm\tfailed\tbad\n/src/p/y.dcm\tfailed\tbad\n/src/p/z.txt\tignored\tnot an image\n"

STATUS_REPORT = (
    "\nOverall: 4 images (pass 1)\n"
    "  CLEAN:           1\n"
    "  DIRTY:           1\n"
    "  UNREVIEWED:      2\n"
    "  FLAGGED:         0\n"
    "\n"
    "Batch            Total  Clean  Dirty  Unrev   Flag\n"
    "----------------------------------------------------\n"
    "batch_001            2      1      0      1      0\n"
    "batch_002            2      0      1      1      0\n"
    "\n"
    "Current pass: 1\n"
    "Skipped during preprocess: 2 failed, 1 ignored (see skipped.tsv in the work dir)\n"
)

STATUS_COUNTS = Counter({"CLEAN": 1, "DIRTY": 1, "UNREVIEWED": 2})
STATUS_BATCHES = {
    "batch_001": Counter({"CLEAN": 1, "UNREVIEWED": 1}),
    "batch_002": Counter({"DIRTY": 1, "UNREVIEWED": 1}),
}


class TestStatusReport(unittest.TestCase):
    def test_two_batches_with_skipped(self):
        self.assertEqual(status_report(STATUS_COUNTS, STATUS_BATCHES, 1, SkippedCounts(2, 1)), STATUS_REPORT)

    def test_one_batch_and_no_skips_omit_those_sections(self):
        report = status_report(STATUS_COUNTS, {"batch_001": STATUS_BATCHES["batch_001"]}, 1, SkippedCounts(0, 0))
        self.assertEqual(report, STATUS_REPORT.split("\nBatch", maxsplit=1)[0] + "\nCurrent pass: 1\n")
        self.assertEqual(report, status_report(STATUS_COUNTS, {}, 1, SkippedCounts(0, 0)))


class TestStatusCommand(StoreTestCase):
    def status(self, *args: str):
        self.store.close()  # status opens the work dir itself, read-only
        return invoke_cli("status", "--work-dir", str(self.work_dir), *args)

    def test_prints_the_report(self):
        mark(self.store, [KEYS[0]], "CLEAN")
        mark(self.store, [KEYS[2]], "DIRTY")
        (self.work_dir / "skipped.tsv").write_text(SKIPPED_TSV)
        result = self.status()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout, STATUS_REPORT)

    def test_silent_without_skips(self):
        (self.work_dir / "skipped.tsv").write_text("image_id\tkind\treason\n")
        self.assertNotIn("Skipped", self.status().stdout)
        (self.work_dir / "skipped.tsv").unlink()
        self.assertNotIn("Skipped", self.status().stdout)

    def test_malformed_skipped_is_clean_error(self):
        (self.work_dir / "skipped.tsv").write_text("wrong\n")
        result = self.status()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("skipped.tsv:1", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_default_dir_missing(self):
        empty = temp_dir(self)
        previous = os.getcwd()
        os.chdir(empty)
        self.addCleanup(os.chdir, previous)
        result = invoke_cli("status")
        self.assertEqual(result.exit_code, 2)
        self.assertIn("Invalid value for '--work-dir': Path './review_work' does not exist.", result.output)


class TestRemovedRemoteOptions(StoreTestCase):
    def test_status_and_review_refuse_remote_and_via(self):
        for command in ("status", "review"):
            for option in ("--remote", "--via"):
                with self.subTest(command=command, option=option):
                    result = invoke_cli(command, option, "x", "--work-dir", str(self.work_dir))
                    self.assertEqual(result.exit_code, 2, result.output)
                    self.assertIn(f"No such option '{option}'", result.output)

    def test_remote_environment_is_ignored(self):
        self.store.close()
        result = invoke_cli("status", "--work-dir", str(self.work_dir), env={"IMAGE_REVIEW_REMOTE": "ir://x"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Overall: 4 images", result.stdout)


class TestHelpTexts(unittest.TestCase):
    def test_workflow_lists_review_and_serve(self):
        out = invoke_cli("--help").output
        self.assertRegex(out, r"\d\. review\s+— review in a browser on this machine \(or `serve`, ")


if __name__ == "__main__":
    unittest.main()
