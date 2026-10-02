import csv
import hashlib
import io
import os
import unittest
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame as pg
from PIL import Image

from image_review import controller as controller_module
from image_review import store as store_module
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
from image_review.store import (
    LocalStore,
    ManifestRow,
    load_manifest,
)
from image_review.util import load_surface
from image_review.viewer import ImageViewer, scale_percent
from tests.fixtures import StoreTestCase, _jpeg_bytes, dropping_packer, make_work_dir, mark, write_manifest


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
                s._switch_to_grid("auto")
            self.assertEqual(pack.call_count, 1)
            self.assertEqual(sorted(item.keys for item in s._items), sorted(item.keys for item in first))
            s._switch_to_grid("never")  # another key replaces the only cached result
            s._switch_to_grid("auto")
            self.assertEqual(pack.call_count, 3)

    def test_grid_cache_key_distinguishes_rotation_policies(self):
        with mock.patch.object(controller_module, "pack_into_grids", wraps=controller_module.pack_into_grids) as pack:
            s = ReviewSession(self.store, reviewer="tester", mode="grid")
            self.assertEqual(s._grid_cache[0][2], "auto")
            s._switch_to_grid("auto")
            self.assertEqual(pack.call_count, 1)
            s._switch_to_grid("never")
            self.assertEqual(pack.call_count, 2)
            self.assertEqual(s._grid_cache[0][2], "never")

    def test_grid_size_change_packs_again(self):
        with mock.patch.object(controller_module, "pack_into_grids", wraps=controller_module.pack_into_grids) as pack:
            s = ReviewSession(self.store, reviewer="tester", mode="grid")
            s._switch_to_single()
            w, h = s._viewer.screen.get_size()
            pg.display.set_mode((w // 2, h // 2))
            s._switch_to_grid("auto")
            self.assertEqual(pack.call_count, 2)

    def test_grid_cache_never_shows_a_key_marked_dirty(self):
        with mock.patch.object(controller_module, "pack_into_grids", wraps=controller_module.pack_into_grids) as pack:
            s = ReviewSession(self.store, reviewer="tester", mode="grid", status_filter="all")
            s._cursor = 0
            s._mark("CLEAN")  # still eligible under all: the rows, so the cache, are unchanged
            s._switch_to_single()
            s._switch_to_grid("auto")
            self.assertEqual(pack.call_count, 1)
            s._cursor = 0
            dirty = s._items[0].keys
            s._mark("DIRTY")
            s._switch_to_single()
            s._switch_to_grid("auto")
            self.assertEqual(pack.call_count, 2)
        self.assertFalse(set(dirty) & self.grid_keys(s))

    def finish_pass_one(self):
        """Pass 1 ends with a DIRTY and b, c, d CLEAN."""
        mark(self.store, ["batch_001/a.jpg"], "DIRTY")
        mark(self.store, ["batch_001/b.jpg"], "CLEAN")
        mark(self.store, ["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")

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
        mark(self.store, ["batch_001/a.jpg"], "DIRTY")
        s = ReviewSession(self.store, reviewer="tester", mode="grid", status_filter="all")
        self.assertEqual(s.pass_number, 1)
        self.assertEqual(self.grid_keys(s), {"batch_001/b.jpg"})
        s = ReviewSession(self.store, reviewer="tester", mode="grid", status_filter="all", batch="batch_002")
        self.assertEqual(self.grid_keys(s), {"batch_002/c.jpg", "batch_002/d.jpg"})

    def test_auto_select_batch_uses_grid_eligibility(self):
        mark(self.store, ["batch_001/a.jpg"], "DIRTY")
        mark(self.store, ["batch_001/b.jpg"], "CLEAN")
        mark(self.store, ["batch_002/c.jpg"], "CLEAN")
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
        with (
            mock.patch.object(self.store, "mark") as mark,
            mock.patch.object(s._viewer, "set_info") as set_info,
            mock.patch("sys.stderr"),
        ):
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
        mark(self.store, ["batch_001/a.jpg"], "CLEAN", s.pass_number)
        self.assertEqual(s._statuses["batch_001/a.jpg"], "UNREVIEWED")
        s._restart_in_mode("grid")
        self.assertEqual(s._statuses["batch_001/a.jpg"], "CLEAN")

    def recheck_session(self, status_filter: str) -> ReviewSession:
        """A pass 1 single-mode session over already reviewed images: batch_002 (c, d CLEAN), or batch_001 (a DIRTY, b CLEAN) under all."""
        self.finish_pass_one()
        batch = "batch_001" if status_filter == "all" else "batch_002"
        s = ReviewSession(
            self.store, reviewer="tester", mode="single", status_filter=status_filter, batch=batch, pass_number=1
        )
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


def button(b: int) -> pg.event.Event:
    return pg.event.Event(pg.CONTROLLERBUTTONDOWN, button=b, instance_id=0)


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

    def reviewing(self, mode: str = "single", **kwargs) -> ReviewSession:
        """A session past the splash with its first item painted and seen for the full dwell."""
        s = ReviewSession(self.store, reviewer="tester", mode=mode, **kwargs)
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
        s.handle_events([key(pg.K_s), button(pg.CONTROLLER_BUTTON_B)])
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
        show_message.assert_called_once_with(
            "No more todo images this way - 1 todo left - [Left/Right] wrap - [b] next batch"
        )
        s.handle_events([key(pg.K_LEFT)])  # wraps to the last todo item, not the CLEAN last item
        self.assertEqual((s._ui_state, s._cursor), (UIState.REVIEWING, 0))

    def test_todo_only_right_from_end_screen_skips_a_clean_first_item(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])  # item 0 CLEAN: item 1 is the first todo item
        s.handle_events([key(pg.K_RIGHT), key(pg.K_u)])
        self.assertEqual(s._cursor, 1)
        s.handle_events([key(pg.K_RIGHT)])
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        s.handle_events([key(pg.K_RIGHT)])
        self.assertEqual((s._ui_state, s._cursor), (UIState.REVIEWING, 1))

    def test_todo_only_end_screen_wraps_to_first_and_last_todo(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_u), key(pg.K_RIGHT), key(pg.K_RIGHT)])
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        for wrap_key, onto, back_key in ((pg.K_LEFT, 1, pg.K_RIGHT), (pg.K_RIGHT, 0, pg.K_LEFT)):
            with self.subTest(key=wrap_key):
                s.handle_events([key(wrap_key)])
                self.assertEqual((s._ui_state, s._cursor), (UIState.REVIEWING, onto))
                s.handle_events([key(back_key)])  # off the end of the list again
                self.assertEqual(s._ui_state, UIState.END_MESSAGE)

    def test_empty_mode_message_is_not_painted_over(self):
        s = self.reviewing()
        mark(self.store, ["batch_001/a.jpg", "batch_001/b.jpg"], "DIRTY")
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
            s._switch_to_grid("auto")
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
            pg.event.post(button(pg.CONTROLLER_BUTTON_B))
            return real_pack(*args, **kwargs)

        pg.event.clear()
        with mock.patch.object(controller_module, "pack_into_grids", pack_while_typing):
            s._switch_to_grid("auto")
        self.assertEqual(pg.event.get((pg.KEYDOWN, pg.CONTROLLERBUTTONDOWN)), [])

    def test_correction_after_dwell(self):
        s = self.reviewing()
        item_key = s._items[s._cursor].keys[0]
        s.handle_events([key(pg.K_c)])
        self.paint(s)
        self.now += 50
        s.handle_events([key(pg.K_d)])
        self.assertEqual(s._statuses[item_key], "DIRTY")

    def test_right_stops_autoplay_and_space_toggles_it(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_SPACE)])
        self.assertTrue(s.autoplay)
        s.handle_events([key(pg.K_SPACE)])
        self.assertFalse(s.autoplay)
        s.handle_events([key(pg.K_LEFT)])
        s.autoplay = True
        with mock.patch.object(s, "next_image", wraps=s.next_image) as next_image:
            s.handle_events([key(pg.K_RIGHT)])
        self.assertFalse(s.autoplay)
        next_image.assert_called_once_with()  # Right kept its action

    def test_splash_f_toggles_fullscreen(self):
        s = ReviewSession(self.store, reviewer="tester", mode="single")
        s._show_splash()
        with mock.patch.object(pg.display, "toggle_fullscreen") as toggle:
            s.handle_events([key(pg.K_f)])
        toggle.assert_called_once_with()
        self.assertEqual(s._ui_state, UIState.SPLASH)

    def test_n_with_no_todo_shows_the_message_and_stays(self):
        s = self.reviewing()
        for k in s._statuses:
            s._statuses[k] = "CLEAN"
        cursor = s._cursor
        with mock.patch.object(s._viewer, "set_info") as set_info:
            s.handle_events([key(pg.K_n)])
        set_info.assert_called_once_with(NO_TODO_MESSAGE)
        self.assertEqual((s._cursor, s._ui_state), (cursor, UIState.REVIEWING))

    def test_status_bar_renders_the_status_word(self):
        s = self.reviewing()
        font = s._viewer.font
        with mock.patch.object(s._viewer, "font", wraps=font) as spy:
            s._viewer.set_status("FLAGGED")
            s._viewer.refresh()
        self.assertIn("FLAGGED", [call.args[2] for call in spy.render_to.call_args_list])

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
        self.assertEqual(s._ui_state, UIState.DISPLAY_SELECT)

    def test_display_select_confirm_resumes_in_single_mode(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])
        cursor = s._cursor
        for confirm in (key(pg.K_SPACE), key(pg.K_h), button(pg.CONTROLLER_BUTTON_A)):
            with self.subTest(confirm=confirm):
                s.handle_events([key(pg.K_w)])
                self.assertEqual(s._ui_state, UIState.DISPLAY_SELECT)
                s.handle_events([confirm])
                self.assertEqual((s._ui_state, s._cursor, s._undoable), (UIState.REVIEWING, cursor, 1))

    def test_display_select_confirm_restarts_grid_mode_after_a_display_change(self):
        s = self.reviewing("grid")

        def fake_switch(idx: int) -> bool:
            s._viewer._display_index = idx
            return True

        for confirm in (key(pg.K_SPACE), key(pg.K_h), button(pg.CONTROLLER_BUTTON_A)):
            with self.subTest(confirm=confirm):
                s.handle_events([key(pg.K_w)])
                with mock.patch.object(s._viewer, "switch_display", side_effect=fake_switch):
                    s.handle_events([key(pg.K_2 if s._viewer.display_index == 0 else pg.K_1)])
                with mock.patch.object(s, "_restart_in_mode", wraps=s._restart_in_mode) as restart:
                    s.handle_events([confirm])
                restart.assert_called_once_with("grid")
                self.assertEqual(s._ui_state, UIState.REVIEWING)

    def test_help_after_display_switch_and_mode_change_is_plain_help(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_w)])
        self.assertEqual(s._ui_state, UIState.DISPLAY_SELECT)

        def fake_switch(idx: int) -> bool:
            s._viewer._display_index = idx
            return True

        with mock.patch.object(s._viewer, "switch_display", side_effect=fake_switch) as switch:
            s.handle_events([key(pg.K_2)])
            switch.assert_called_once_with(1)
            s.handle_events([key(pg.K_m)])
            self.assertEqual((s.mode, s._ui_state), ("grid", UIState.REVIEWING))
            self.paint(s)
            self.now += MIN_DWELL_MS
            s.handle_events([key(pg.K_c)])
            self.assertEqual(s._undoable, 1)
            cursor = s._cursor
            s.handle_events([key(pg.K_h)])
            self.assertEqual(s._ui_state, UIState.SPLASH)
            s.handle_events([key(pg.K_1)])
            switch.assert_called_once_with(1)  # digits do nothing on the help screen
            s.handle_events([key(pg.K_SPACE)])
        self.assertEqual((s._ui_state, s._cursor, s._undoable), (UIState.REVIEWING, cursor, 1))

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


class TestGamepad(EventLoopTestCase):
    def test_b_marks_clean_after_dwell(self):
        s = ReviewSession(self.store, reviewer="tester", mode="single")
        s._show_splash()
        s.handle_events([key(pg.K_SPACE)])
        self.paint(s)
        self.now += MIN_DWELL_MS - 1
        s.handle_events([button(pg.CONTROLLER_BUTTON_B)])
        self.mark.assert_not_called()
        self.now += 1
        s.handle_events([button(pg.CONTROLLER_BUTTON_B)])
        self.mark.assert_called_once()
        self.assertEqual(self.mark.call_args.args[1], "CLEAN")

    def test_y_marks_dirty(self):
        s = self.reviewing()
        s.handle_events([button(pg.CONTROLLER_BUTTON_Y)])
        self.mark.assert_called_once()
        self.assertEqual(self.mark.call_args.args[1], "DIRTY")

    def test_dpad_navigates_and_cancels_advance(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_c)])
        self.assertTrue(s._advance_pending)
        s.handle_events([button(pg.CONTROLLER_BUTTON_DPAD_RIGHT)])
        self.assertEqual(s._cursor, 1)
        self.assertFalse(s._advance_pending)
        s.handle_events([button(pg.CONTROLLER_BUTTON_DPAD_LEFT)])
        self.assertEqual(s._cursor, 0)

    def test_every_button_stops_autoplay(self):
        for b in (pg.CONTROLLER_BUTTON_DPAD_RIGHT, pg.CONTROLLER_BUTTON_A, pg.CONTROLLER_BUTTON_X):
            with self.subTest(button=b):
                s = self.reviewing()
                s.autoplay = True
                s.handle_events([button(b)])
                self.assertFalse(s.autoplay)
                self.assertEqual(s._cursor, 1 if b == pg.CONTROLLER_BUTTON_DPAD_RIGHT else 0)

    def test_review_buttons_do_nothing_on_splash(self):
        s = ReviewSession(self.store, reviewer="tester", mode="single")
        s._show_splash()
        for b in (pg.CONTROLLER_BUTTON_B, pg.CONTROLLER_BUTTON_Y, pg.CONTROLLER_BUTTON_DPAD_RIGHT):
            self.assertTrue(s.handle_events([button(b)]))
        self.assertEqual((s._ui_state, s._cursor), (UIState.SPLASH, -1))
        self.mark.assert_not_called()

    def test_a_continues_from_splash(self):
        s = ReviewSession(self.store, reviewer="tester", mode="single")
        s._show_splash()
        self.assertTrue(s.handle_events([button(pg.CONTROLLER_BUTTON_A)]))
        self.assertEqual((s._ui_state, s._cursor), (UIState.REVIEWING, 0))

    def test_a_continues_from_end_screen(self):
        s = self.reviewing()
        s.handle_events([key(pg.K_RIGHT), key(pg.K_RIGHT)])
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        self.assertTrue(s.handle_events([button(pg.CONTROLLER_BUTTON_A)]))
        self.assertEqual((s._ui_state, s._cursor), (UIState.REVIEWING, 0))

    def test_start_quits_in_every_state(self):
        s = ReviewSession(self.store, reviewer="tester", mode="single")
        s._show_splash()
        self.assertFalse(s.handle_events([button(pg.CONTROLLER_BUTTON_START)]))
        s = self.reviewing()
        self.assertFalse(s.handle_events([button(pg.CONTROLLER_BUTTON_START)]))
        s.handle_events([key(pg.K_RIGHT), key(pg.K_RIGHT)])
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        self.assertFalse(s.handle_events([button(pg.CONTROLLER_BUTTON_START)]))

    def test_start_quits_when_disconnected(self):
        s = self.reviewing()
        with (
            mock.patch.object(self.store, "statuses", side_effect=store_module.StoreUnavailable("down")),
            mock.patch("sys.stderr"),
        ):
            s.handle_events([key(pg.K_m)])
        self.assertEqual(s._ui_state, UIState.DISCONNECTED)
        self.assertTrue(s.handle_events([button(pg.CONTROLLER_BUTTON_A)]))
        self.assertFalse(s.handle_events([button(pg.CONTROLLER_BUTTON_START)]))

    def test_raw_joystick_buttons_are_ignored(self):
        s = self.reviewing()
        for b in (1, 3, 7):
            self.assertTrue(s.handle_events([pg.event.Event(pg.JOYBUTTONDOWN, button=b, instance_id=0)]))
        self.assertTrue(s.handle_events([pg.event.Event(pg.JOYHATMOTION, hat=0, value=(1, 0), instance_id=0)]))
        self.assertEqual((s._ui_state, s._cursor), (UIState.REVIEWING, 0))
        self.mark.assert_not_called()

    def test_device_add_and_remove_update_the_count(self):
        s = self.reviewing()
        pads = {0: mock.Mock(), 1: mock.Mock()}
        for instance_id, pad in pads.items():
            pad.as_joystick.return_value.get_instance_id.return_value = instance_id + 10
        with (
            mock.patch.object(controller_module.sdl_controller, "Controller", side_effect=lambda i: pads[i]) as opened,
            mock.patch.object(s._viewer, "set_joystick_count") as set_count,
        ):
            s.handle_events([pg.event.Event(pg.CONTROLLERDEVICEADDED, device_index=0)])
            s.handle_events([pg.event.Event(pg.CONTROLLERDEVICEADDED, device_index=1)])
            s.handle_events([pg.event.Event(pg.CONTROLLERDEVICEADDED, device_index=1)])  # the same pad again
            self.assertEqual([c.args for c in set_count.call_args_list], [(1,), (2,), (2,)])
            s.handle_events([pg.event.Event(pg.CONTROLLERDEVICEREMOVED, instance_id=10)])
            s.handle_events([pg.event.Event(pg.JOYDEVICEADDED, device_index=0)])  # SDL sends it too: not counted
            set_count.assert_called_with(1)
            self.assertEqual(set_count.call_count, 4)
        self.assertEqual(s._gamepads, {11: pads[1]})
        self.assertEqual([c.args for c in opened.call_args_list], [(0,), (1,), (1,)])


class TestScaleAndResize(EventLoopTestCase):
    resized = pg.event.Event(pg.WINDOWRESIZED)

    def bar_texts(self, viewer) -> dict[str, tuple]:
        """Draw the status bar and return the text -> (align, colour) of each _bar_text call."""
        with mock.patch.object(viewer, "_bar_text", wraps=viewer._bar_text) as bar_text:
            viewer.refresh()
        return {c.args[0]: (c.args[1], c.kwargs.get("color")) for c in bar_text.call_args_list}

    def test_scale_percent_shown_and_red_below_100(self):
        viewer = ImageViewer()
        pg.display.set_mode((800, 600))
        # Content area is 800 x (600 - 50 border): scale = min(800/2000, 550/2000) = 0.275 = 27.5%,
        # truncated to 27% (the plan's figure)
        viewer.set_image(pg.Surface((2000, 2000)), "big", "UNREVIEWED", "info")
        self.assertEqual(viewer._scale, 0.275)
        texts = self.bar_texts(viewer)
        self.assertEqual(texts["27%"], ("right", ImageViewer.SCALE_WARNING_COLOR))

    def test_scale_at_or_above_100_uses_normal_colour(self):
        viewer = ImageViewer()
        pg.display.set_mode((800, 600))
        viewer.set_image(pg.Surface((800, 550)), "exact", "UNREVIEWED", "info")
        self.assertEqual(self.bar_texts(viewer)["100%"], ("right", None))
        viewer.set_image(pg.Surface((400, 275)), "small", "UNREVIEWED", "info")  # resize enlarges it
        self.assertEqual(viewer._scale, 2.0)
        self.assertEqual(self.bar_texts(viewer)["200%"], ("right", None))

    def test_grid_resize_repacks_at_new_size_and_resets_dwell(self):
        s = self.reviewing("grid")
        self.assertIsNotNone(s._shown_at)
        keep = s._items[s._cursor].keys[0]
        w, h = s._viewer.screen.get_size()
        pg.display.set_mode((w // 2, h // 2))
        with mock.patch.object(controller_module, "pack_into_grids", wraps=controller_module.pack_into_grids) as pack:
            s.handle_events([self.resized])
            pack.assert_not_called()  # only marked stale; rebuilt on the next tick
            self.now += MIN_DWELL_MS
            s.refresh_if_needed()
        pack.assert_called_once()
        self.assertEqual(pack.call_args.args[2:4], (w // 2, h // 2 - s._viewer.border))
        self.assertIn(keep, s._items[s._cursor].keys)  # the cursor follows the current item's first key
        self.assertEqual(s._shown_at, self.now)  # repainted, so the dwell restarts
        s.handle_events([key(pg.K_c)])  # same batch time: not yet seen
        self.mark.assert_not_called()
        self.now += MIN_DWELL_MS
        s.handle_events([key(pg.K_c)])
        self.mark.assert_called_once()

    def test_grid_resize_stops_timers(self):
        s = self.reviewing("grid")
        s.autoplay = True
        s._advance_pending = True
        w, h = s._viewer.screen.get_size()
        pg.display.set_mode((w // 2, h // 2))
        s.handle_events([self.resized])
        s.refresh_if_needed()
        self.assertFalse(s.autoplay)
        self.assertFalse(s._advance_pending)

    def test_two_resize_events_in_a_batch_repack_once(self):
        s = self.reviewing("grid")
        w, h = s._viewer.screen.get_size()
        pg.display.set_mode((w // 2, h // 2))
        with mock.patch.object(controller_module, "pack_into_grids", wraps=controller_module.pack_into_grids) as pack:
            s.handle_events([self.resized, self.resized])
            s.refresh_if_needed()
            s.refresh_if_needed()
        self.assertEqual(pack.call_count, 1)

    def test_single_mode_resize_does_not_repack(self):
        s = self.reviewing("single")
        w, h = s._viewer.screen.get_size()
        pg.display.set_mode((w // 2, h // 2))
        with mock.patch.object(controller_module, "pack_into_grids") as pack:
            s.handle_events([self.resized])
            s.refresh_if_needed()
        pack.assert_not_called()
        self.assertFalse(s._grids_stale)

    def test_scale_percent_truncates_without_float_error(self):
        self.assertEqual(scale_percent(0.29), 29)  # 0.29 * 100 is 28.999999999999996
        self.assertEqual(scale_percent(0.275), 27)
        self.assertEqual(scale_percent(0.999), 99)
        self.assertEqual(scale_percent(1.0), 100)

    def test_grid_percent_is_the_smallest_images_effective_scale(self):
        Image.new("RGB", (4000, 3000)).save(self.work_dir / "batch_001/a.jpg")
        s = self.reviewing("grid")
        viewer = s._viewer
        item = s._items[s._cursor]
        w, h = s._grid_size()
        w4, h3 = fit_size(4000, 3000, w, h, True)
        self.assertEqual(item.source_scale, min(w4 / 4000, h3 / 3000))
        self.assertLess(item.source_scale, 1.0)
        percent = f"{scale_percent(viewer._scale * item.source_scale)}%"
        self.assertEqual(self.bar_texts(viewer)[percent], ("right", ImageViewer.SCALE_WARNING_COLOR))
        self.assertNotEqual(percent, "100%")

    def shrink(self, s: ReviewSession, size: tuple[int, int]) -> None:
        pg.display.set_mode(size)
        s.handle_events([self.resized])

    def test_resize_back_to_the_packed_size_does_not_repack(self):
        s = self.reviewing("grid")
        size = pg.display.get_surface().get_size()
        self.shrink(s, (size[0] // 2, size[1] // 2))
        self.shrink(s, size)  # back before the next tick
        with mock.patch.object(controller_module, "pack_into_grids") as pack:
            s.refresh_if_needed()
        pack.assert_not_called()
        self.assertFalse(s._grids_stale)

    def test_undo_after_rebuild_has_nothing_to_undo(self):
        s = self.reviewing("grid", status_filter="all")  # a CLEAN grid stays eligible, so the repack has items
        s._mark("CLEAN")
        self.assertEqual(s._undoable, 1)
        size = pg.display.get_surface().get_size()
        self.shrink(s, (size[0] // 2, size[1] // 2))
        s.refresh_if_needed()
        self.assertEqual(s._ui_state, UIState.REVIEWING)
        with mock.patch.object(self.store, "undo") as undo:
            s.handle_events([key(pg.K_z)])
        undo.assert_not_called()
        self.assertEqual(s._viewer._info, NOTHING_TO_UNDO)

    def test_store_lost_during_rebuild_keeps_the_message(self):
        s = self.reviewing("grid")
        size = pg.display.get_surface().get_size()
        self.shrink(s, (size[0] // 2, size[1] // 2))
        with (
            mock.patch.object(controller_module, "pack_into_grids", side_effect=store_module.StoreUnavailable("down")),
            mock.patch.object(s._viewer, "refresh") as refresh,
            mock.patch("sys.stderr", io.StringIO()),
        ):
            s.refresh_if_needed()
            s.refresh_if_needed()
        self.assertEqual(s._ui_state, UIState.DISCONNECTED)
        refresh.assert_not_called()

    def test_everything_marked_then_resize_ends_the_list(self):
        s = self.reviewing("grid")
        s._mark("CLEAN")  # the only grid: nothing is left to repack
        size = pg.display.get_surface().get_size()
        self.shrink(s, (size[0] // 2, size[1] // 2))
        with mock.patch.object(s._viewer, "refresh") as refresh:
            s.refresh_if_needed()
        self.assertEqual(s._ui_state, UIState.END_MESSAGE)
        self.assertEqual(s._items, [])
        refresh.assert_not_called()

    def test_cursor_falls_back_to_start_when_current_grid_was_marked(self):
        s = self.reviewing("grid")
        self.shrink(s, (30, 62))  # a 30 x 12 bin holds one image: two grids
        s.refresh_if_needed()
        self.assertEqual(len(s._items), 2)
        s._cursor = 1
        s._show_current()
        marked = s._items[1].keys
        s._mark("CLEAN")
        self.shrink(s, (31, 62))
        s.refresh_if_needed()
        self.assertEqual(s._ui_state, UIState.REVIEWING)
        self.assertEqual(s._cursor, 0)
        self.assertEqual(len(s._items), 1)
        self.assertFalse(set(marked) & set(s._items[0].keys))

    def test_queued_verdict_is_cleared_by_the_rebuild(self):
        s = self.reviewing("grid")
        size = pg.display.get_surface().get_size()
        self.shrink(s, (size[0] // 2, size[1] // 2))
        pg.event.clear()
        pg.event.post(key(pg.K_c))
        s.refresh_if_needed()
        self.now += MIN_DWELL_MS
        s.handle_events(pg.event.get())
        self.mark.assert_not_called()


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
        mark(self.store, ["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")
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
        mark(self.store, ["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")
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
        mark(self.store, ["batch_002/c.jpg"], "DIRTY")
        mark(self.store, ["batch_002/d.jpg"], "CLEAN")
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
        mark(self.store, ["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")
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
        mark(self.store, ["batch_001/a.jpg"], "DIRTY")
        mark(self.store, ["batch_001/b.jpg"], "CLEAN")
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
        mark(self.store, ["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")
        s = self.start(status_filter="all")
        self.assertEqual(s.batch, "batch_001")
        self.finish(s, dirty="batch_001/a.jpg")  # a was marked in this session, and is FLAGGED in pass 2
        self.press_b(s)
        self.assertEqual((s.pass_number, s.batch, s._ui_state), (2, "batch_001", UIState.REVIEWING))
        self.assertEqual(s._todo_count, 1)
        a = next(item for item in s._items if item.keys == ("batch_001/a.jpg",))
        self.assertTrue(s._is_todo(a))

    def test_explicit_batch_after_the_pass_ends(self):
        mark(self.store, ["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")
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
            mark(self.store, batch_keys, "CLEAN")
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
        # Items are shuffled: CORRUPT may be the first item shown, so capture from the session's start
        with self.assertLogs("image_review.controller", "WARNING") as logs:
            s = self.reviewing()
            self.assertEqual(s.batch, "batch_001")
            forward = self.walk(s, pg.K_RIGHT)
        self.assertEqual(sorted(forward), ["batch_001/a.jpg", "batch_001/b.jpg"])
        self.assertEqual(s._cursor, len(s._items) - 1)
        self.assertIn(CORRUPT, s._unloadable)
        self.assertIn(f"cannot load {CORRUPT}", "\n".join(logs.output))
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
        mark(self.store, ["batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")
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


class TestJpegHash(EventLoopTestCase):
    """manifest.tsv records each JPG's SHA-256; a JPG that no longer matches is unloadable everywhere."""

    def make_work_dir(self) -> None:
        make_work_dir(self.work_dir)
        (self.work_dir / CORRUPT).write_bytes(_jpeg_bytes("RGB"))  # big enough to cut mid-scan
        write_manifest(self.work_dir, hashed=True)

    def setUp(self):
        super().setUp()
        self.original = (self.work_dir / CORRUPT).read_bytes()
        stderr = mock.patch("sys.stderr", io.StringIO())
        stderr.start()
        self.addCleanup(stderr.stop)

    def flip_one_byte(self) -> None:
        data = bytearray(self.original)
        data[len(data) // 2] ^= 0x01
        (self.work_dir / CORRUPT).write_bytes(bytes(data))

    def truncate_keeping_eoi(self) -> bytes:
        scan = self.original.index(b"\xff\xda")  # start of scan
        cut = self.original[: scan + (len(self.original) - scan) // 2] + b"\xff\xd9"
        (self.work_dir / CORRUPT).write_bytes(cut)
        return cut

    def assert_placeholder(self, mode: str) -> None:
        with self.assertLogs("image_review.controller", "WARNING") as logs:
            s = self.reviewing(mode)
            index = next(i for i, item in enumerate(s._items) if CORRUPT in item.keys)
            self.assertEqual(s._items[index].keys, (CORRUPT,))  # never packed into a grid
            s._cursor = index
            s._show_current()
        self.assertEqual(s._unloadable, {CORRUPT})
        self.assertIn("does not match its recorded hash", "\n".join(logs.output))
        self.paint(s)
        self.now += MIN_DWELL_MS
        s.handle_events([key(pg.K_c)])
        self.mark.assert_not_called()
        self.assertEqual(s._viewer._info, UNLOADABLE_CLEAN)

    def test_intact_image_loads(self):
        self.assertEqual(self.store.image_bytes(CORRUPT), self.original)
        entry = next(e for e in load_manifest(self.work_dir) if e.key == CORRUPT)
        self.assertEqual(entry.jpeg_sha256, hashlib.sha256(self.original).hexdigest())

    def test_flipped_byte_image_bytes_raises(self):
        self.flip_one_byte()
        with self.assertRaisesRegex(ValueError, f"^{CORRUPT} does not match its recorded hash$"):
            self.store.image_bytes(CORRUPT)
        with self.assertLogs("image_review.store", "WARNING") as logs:
            found = self.store.image_bytes_many([CORRUPT, "batch_001/b.jpg"])
        self.assertEqual(list(found), ["batch_001/b.jpg"])
        self.assertIn(f"cannot load {CORRUPT}", "\n".join(logs.output))

    def test_flipped_byte_is_a_placeholder(self):
        self.flip_one_byte()
        for mode in ("single", "grid"):
            with self.subTest(mode=mode):
                self.assert_placeholder(mode)

    def test_flipped_byte_is_left_out_of_grids(self):
        self.flip_one_byte()
        rows = [row for row in self.store.manifest() if row.batch == "batch_001"]
        grids, left_out = pack_into_grids(rows, self.store, 400, 300)
        self.assertEqual(left_out, [CORRUPT])
        self.assertEqual([gs.keys for gs in grids], [["batch_001/b.jpg"]])

    def test_truncated_with_eoi_is_a_placeholder(self):
        cut = self.truncate_keeping_eoi()
        self.assertEqual(load_surface(cut).get_size(), (257, 131))  # the gap: Pillow decodes it, with grey rows
        self.assert_placeholder("single")


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
        self.assertEqual(
            next_index(4, -1, -1, is_todo=None, wrap=False), 2
        )  # the inherited formula's result; unreachable in production
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


if __name__ == "__main__":
    unittest.main()
