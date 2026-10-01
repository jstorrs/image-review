import csv
import errno
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
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame as pg
from click.testing import CliRunner
from fixtures import ROWS, make_work_dir

from image_review import controller as controller_module
from image_review import store as store_module
from image_review.cli import cli, unknown_batch_message
from image_review.controller import (
    ADVANCE_EVENT,
    AUTOPLAY_EVENT,
    GRID_HAS_DIRTY,
    MIN_DWELL_MS,
    ReviewSession,
    UIState,
    _dwell_elapsed,
)
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
        self.store.mark(["batch_001/a.jpg"], "batch_001", "CLEAN", 1)
        self.store.mark(["batch_001/b.jpg"], "batch_001", "DIRTY", 1)
        statuses = self.store.statuses(2)
        self.assertEqual(statuses["batch_001/a.jpg"], "CLEAN")
        self.assertEqual(statuses["batch_001/b.jpg"], "FLAGGED")

    def test_pass_one_dirty_is_flagged_in_pass_two(self):
        self.store.mark(["batch_001/a.jpg"], "batch_001", "DIRTY", 1)
        self.store.mark(["batch_001/b.jpg"], "batch_001", "CLEAN", 1)
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "batch_002", "CLEAN", 1)
        self.assertEqual(self.store.current_pass(), 2)
        self.assertEqual(
            self.store.statuses(2),
            {"batch_001/a.jpg": "FLAGGED", "batch_001/b.jpg": "CLEAN", "batch_002/c.jpg": "CLEAN", "batch_002/d.jpg": "CLEAN"},
        )
        self.assertEqual(self.store.mark(["batch_001/a.jpg"], "batch_001", "CLEAN", 2), {"batch_001/a.jpg": "CLEAN"})
        self.assertEqual(self.store.current_pass(), 3)

    def test_flagged_keeps_pass_open(self):
        self.store.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "batch_001", "DIRTY", 1)
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "batch_002", "CLEAN", 1)
        self.store.mark(["batch_001/a.jpg"], "batch_001", "CLEAN", 2)
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
            changed = store.mark(["batch_001/a.jpg"], "batch_001", "CLEAN", 1)
            self.assertEqual(changed, {"batch_001/a.jpg": "CLEAN", "batch_001/b.jpg": "CLEAN"})
            statuses = store.statuses(1)
            self.assertEqual({k: statuses[k] for k in changed}, changed)
            self.assertEqual(statuses["batch_002/c.jpg"], "UNREVIEWED")


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
            self.store.mark([key], key.split("/")[0], status, pass_number)

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
        self.store.mark(["batch_001/a.jpg"], "batch_001", "DIRTY", 3)
        result = self.store.mark(["batch_001/a.jpg"], "batch_001", "CLEAN", 1)
        self.assertEqual(result, {"batch_001/a.jpg": "CLEAN"})
        with open(self.work_dir / "review.tsv", newline="") as f:
            (row,) = csv.DictReader(f, delimiter="\t")
        self.assertEqual((row["status"], row["pass_number"]), ("CLEAN", "3"))

    def test_pass_one_grid_excludes_later_pass_dirty(self):
        self.mark_all("DIRTY", 3, ["batch_001/a.jpg"])
        self.append_manifest_row()
        s = ReviewSession(self.store, mode="grid", status_filter="all")
        self.assertEqual(s.pass_number, 1)
        keys = {k for item in s._items for k in item["keys"]}
        self.assertNotIn("batch_001/a.jpg", keys)
        self.assertIn("batch_001/b.jpg", keys)


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

    def finish_pass_one(self):
        """Pass 1 ends with a DIRTY and b, c, d CLEAN."""
        self.store.mark(["batch_001/a.jpg"], "batch_001", "DIRTY", 1)
        self.store.mark(["batch_001/b.jpg"], "batch_001", "CLEAN", 1)
        self.store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "batch_002", "CLEAN", 1)

    @staticmethod
    def grid_keys(s: ReviewSession) -> set[str]:
        return {k for item in s._items for k in item["keys"]}

    def test_pass_two_grid_excludes_flagged(self):
        self.finish_pass_one()
        s = ReviewSession(self.store, mode="grid")
        self.assertEqual(s.pass_number, 2)
        self.assertEqual(s._items, [])
        self.assertIsNone(s.batch)

    def test_pass_two_grid_all_filter_excludes_flagged(self):
        self.finish_pass_one()
        for batch in ("batch_001", None):
            with self.subTest(batch=batch):
                s = ReviewSession(self.store, mode="grid", status_filter="all", batch=batch)
                self.assertNotIn("batch_001/a.jpg", self.grid_keys(s))
                self.assertIn("batch_001/b.jpg", self.grid_keys(s))

    def test_pass_two_single_shows_flagged_as_todo(self):
        self.finish_pass_one()
        s = ReviewSession(self.store, mode="single")
        self.assertEqual(s.pass_number, 2)
        self.assertEqual(s.batch, "batch_001")
        self.assertEqual([r.key for r in s._items], ["batch_001/a.jpg"])
        self.assertEqual(s._todo_count, 1)
        s._cursor = 0
        s._mark("CLEAN")
        self.assertEqual(s._todo_count, 0)

    def test_pass_one_all_filter_grid_excludes_dirty(self):
        self.store.mark(["batch_001/a.jpg"], "batch_001", "DIRTY", 1)
        s = ReviewSession(self.store, mode="grid", status_filter="all")
        self.assertEqual(s.pass_number, 1)
        self.assertEqual(self.grid_keys(s), {"batch_001/b.jpg"})
        s = ReviewSession(self.store, mode="grid", status_filter="all", batch="batch_002")
        self.assertEqual(self.grid_keys(s), {"batch_002/c.jpg", "batch_002/d.jpg"})

    def test_auto_select_batch_uses_grid_eligibility(self):
        self.store.mark(["batch_001/a.jpg"], "batch_001", "DIRTY", 1)
        self.store.mark(["batch_001/b.jpg"], "batch_001", "CLEAN", 1)
        self.store.mark(["batch_002/c.jpg"], "batch_002", "CLEAN", 1)
        # pass 2: batch_001's only todo image is FLAGGED; batch_002 has d UNREVIEWED
        self.assertEqual(ReviewSession(self.store, mode="grid", pass_number=2).batch, "batch_002")
        self.assertEqual(ReviewSession(self.store, mode="single", pass_number=2).batch, "batch_001")

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
        s = ReviewSession(self.store, mode="grid")
        s._items = [
            {"surface": None, "keys": ["batch_001/a.jpg"], "batch": "batch_001"},
            {"surface": None, "keys": ["batch_001/b.jpg", "batch_002/c.jpg"], "batch": "batch_001"},
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

    def test_flagged_is_orange(self):
        s = ReviewSession(self.store, mode="single")
        self.assertEqual(s._viewer.STATUS_COLORS["FLAGGED"], pg.Color(255, 176, 64))
        s._viewer.set_status("FLAGGED")
        s._viewer.refresh()  # every Status has a colour; a missing one would raise

    def test_restart_refetches_statuses(self):
        s = ReviewSession(self.store, mode="single")
        self.store.mark(["batch_001/a.jpg"], "batch_001", "CLEAN", s.pass_number)
        self.assertEqual(s._statuses["batch_001/a.jpg"], "UNREVIEWED")
        s._restart_in_mode("grid")
        self.assertEqual(s._statuses["batch_001/a.jpg"], "CLEAN")


def key(k: int) -> pg.event.Event:
    return pg.event.Event(pg.KEYDOWN, key=k, mod=0)


class TestEventLoop(SessionTestCase):
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
        s = ReviewSession(self.store, mode=mode)
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

    def test_verdict_after_dwell_marks(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])
        self.mark.assert_called_once()

    def test_verdict_before_dwell_is_ignored(self):
        s = ReviewSession(self.store, mode="single")
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

    def assert_message_stays(self, s: ReviewSession) -> None:
        """With no items, the message survives the loop's refresh and the navigation keys."""
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        with mock.patch.object(s._viewer, "refresh") as refresh:
            s.refresh_if_needed()
            for k in (pg.K_SPACE, pg.K_RIGHT, pg.K_LEFT):
                self.assertTrue(s.handle_events([key(k)]))
                self.assertEqual(s._ui_state, UIState.END_MESSAGE)
                s.refresh_if_needed()
        refresh.assert_not_called()

    def test_empty_mode_message_is_not_painted_over(self):
        s = self.reviewing()
        self.store.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "batch_001", "DIRTY", 1)
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
        self.assert_message_stays(s)

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
        s = ReviewSession(self.store, mode="single")
        s._show_splash()
        s.handle_events([key(pg.K_SPACE)])
        self.paint(s)
        self.now += 5

        def slow_resize():
            self.now += MIN_DWELL_MS

        with mock.patch.object(s._viewer, "resize", slow_resize):
            s.handle_events([pg.event.Event(pg.WINDOWRESIZED), key(pg.K_c)])
        self.mark.assert_not_called()

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
        item_key = s._items[s._cursor].key
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


class TestDwell(unittest.TestCase):
    def test_dwell_elapsed(self):
        self.assertFalse(_dwell_elapsed(None, 10_000))
        self.assertFalse(_dwell_elapsed(1_000, 1_000 + MIN_DWELL_MS - 1))
        self.assertTrue(_dwell_elapsed(1_000, 1_000 + MIN_DWELL_MS))


class TestPureFunctions(StoreTestCase):
    """Pass 2 with every status present: a FLAGGED, b CLEAN, c DIRTY, d UNREVIEWED."""

    def setUp(self):
        super().setUp()
        self.store.mark(["batch_001/a.jpg", "batch_002/c.jpg"], "batch_001", "DIRTY", 1)
        self.store.mark(["batch_001/b.jpg"], "batch_001", "CLEAN", 1)
        self.store.mark(["batch_002/c.jpg"], "batch_002", "DIRTY", 2)
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

    def test_filter_rejects_bad_filter(self):
        with self.assertRaises(ValueError):
            filter_rows(self.rows, self.statuses, "bogus")

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
            second.mark([ROWS[0][1]], "batch_001", "CLEAN", 1)
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
            reader.mark([ROWS[0][1]], "batch_001", "CLEAN", 1)
        reader.close()
        self.assertTrue(self.lock_path.exists())  # the writer's lock is untouched

    def test_closed_store_refuses_mark(self):
        store = self.open()
        store.close()
        with self.assertRaises(PermissionError):
            store.mark([ROWS[0][1]], "batch_001", "CLEAN", 1)

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
    def invoke(self, *args):
        env = {"IMAGE_REVIEW_REMOTE": None, "IMAGE_REVIEW_VIA": None, "IMAGE_REVIEW_ACCESS": None}
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
            store.mark(["batch_001/a.jpg"], "batch_001", "DIRTY", 1)
            store.mark(["batch_001/b.jpg"], "batch_001", "CLEAN", 1)
            store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "batch_002", "CLEAN", 1)
        result = self.invoke("status")
        self.assertEqual(result.exit_code, 0, result.output)
        for line in ("  CLEAN:           3", "  DIRTY:           0", "  UNREVIEWED:      0", "  FLAGGED:         1", "Current pass: 2"):
            self.assertIn(line + "\n", result.output)
        self.assertIn(f"{'Batch':<15} {'Total':>6} {'Clean':>6} {'Dirty':>6} {'Unrev':>6} {'Flag':>6}", result.output)
        self.assertIn(f"{'batch_001':<15} {2:>6} {1:>6} {0:>6} {0:>6} {1:>6}", result.output)

    def test_grid_review_names_held_back_images(self):
        with LocalStore(self.work_dir) as store:
            store.mark(["batch_001/a.jpg"], "batch_001", "DIRTY", 1)
            store.mark(["batch_001/b.jpg"], "batch_001", "CLEAN", 1)
            store.mark(["batch_002/c.jpg", "batch_002/d.jpg"], "batch_002", "CLEAN", 1)
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
