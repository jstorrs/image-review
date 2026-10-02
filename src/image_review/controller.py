import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import NamedTuple

import pygame as pg
from pygame._sdl2 import controller as sdl_controller

from .grid_packer import GridSpec, Rotation, pack_into_grids
from .status import TODO_STATUSES, MarkMode, Status, Verdict
from .store import (
    ManifestRow,
    ReviewStore,
    StatusFilter,
    StoreUnavailable,
    filter_rows,
)
from .util import load_surface
from .viewer import ImageViewer, placeholder_surface

log = logging.getLogger(__name__)

AUTOPLAY_EVENT = pg.USEREVENT + 1
ADVANCE_EVENT = pg.USEREVENT + 2

# A verdict key or button only counts once the current item has been painted for this long, so
# input queued while the UI was blocked, or typed as the screen changed, never judges an unseen item.
MIN_DWELL_MS = 200
# Input events dropped after a blocking step (grid build, mode restart).
VERDICT_INPUT_EVENTS = (pg.KEYDOWN, pg.CONTROLLERBUTTONDOWN)
# Gamepad buttons that act during review, as the key each one presses: one code path for both.
REVIEW_BUTTON_KEYS = {
    pg.CONTROLLER_BUTTON_B: pg.K_c,
    pg.CONTROLLER_BUTTON_Y: pg.K_d,
    pg.CONTROLLER_BUTTON_DPAD_LEFT: pg.K_LEFT,
    pg.CONTROLLER_BUTTON_DPAD_RIGHT: pg.K_RIGHT,
}


def _dwell_elapsed(shown_at: int | None, now: int) -> bool:
    """True once an item painted at `shown_at` (None: not painted yet) has been on screen for MIN_DWELL_MS."""
    return shown_at is not None and now - shown_at >= MIN_DWELL_MS


# A grid verdict applies to every image in it, so grids only hold images not yet judged
# DIRTY (this pass) or FLAGGED (DIRTY in another pass): one keypress must never clear those.
GRID_ELIGIBLE: frozenset[Status] = frozenset({"UNREVIEWED", "CLEAN"})
GRID_HAS_DIRTY = "grid contains an image already marked DIRTY - review it in single mode"
NOTHING_TO_UNDO = "Nothing to undo"
UNLOADABLE_CLEAN = "cannot mark CLEAN: image could not be loaded"
END_OF_LIST_MESSAGE = "End of list"
NO_TODO_MESSAGE = "No todo images remaining"
NO_TODO_THIS_WAY_MESSAGE = "No more todo images this way"


def _placeholder(key: str, reason: str) -> pg.Surface:
    return placeholder_surface(f"Cannot load image: {key}\n{reason}\nIt can only be marked DIRTY")


def _grid_status(snapshot: dict[str, Status], keys: tuple[str, ...]) -> Status:
    statuses = {snapshot[key] for key in keys}
    if not statuses <= GRID_ELIGIBLE:
        return "DIRTY"  # e.g. a key sharing an image_id with one marked DIRTY elsewhere this session
    if statuses & TODO_STATUSES:
        return "UNREVIEWED"
    return "CLEAN"


def _grid_clean_refused(snapshot: dict[str, Status], keys: tuple[str, ...]) -> bool:
    """CLEAN on a grid holding a DIRTY or FLAGGED image is refused, unless the whole grid is
    DIRTY (reversing that grid's own verdict)."""
    statuses = {snapshot[key] for key in keys}
    return not statuses <= GRID_ELIGIBLE and statuses != {"DIRTY"}


class GridCacheKey(NamedTuple):
    """What a grid build depends on: the review rows' keys in order, the bin size the grids were
    packed for and the rotation policy."""

    keys: tuple[str, ...]
    size: tuple[int, int]
    rotation: Rotation


@dataclass(frozen=True)
class ReviewItem:
    """One reviewable item: a single image (surface None, loaded when displayed) or a grid
    (composited when built). A grid's status and CLEAN refusal follow the grid rules even
    when it holds one key, so `grid` is kept rather than read off len(keys)."""

    keys: tuple[str, ...]
    label: str
    surface: pg.Surface | None
    grid: bool
    source_scale: float = 1.0  # a grid's smallest image scale vs. its source; shown with the display scale


def next_index(n: int, cursor: int, direction: int, *, is_todo: Callable[[int], bool] | None, wrap: bool) -> int | None:
    """Index of the next item from `cursor` (-1: none shown yet) in `direction` (+1 or -1) that
    `is_todo` accepts (None: any item), or None when there is none.

    Without `wrap` the search stops at the end of the list (index 0 going forward, n - 1 going
    back), except from cursor -1. With `wrap` it goes round once, back to the cursor itself."""
    boundary = 0 if direction == 1 else n - 1
    for offset in range(1, n + 1):
        idx = (cursor + direction * offset) % n
        if not wrap and idx == boundary and cursor != -1:
            return None
        if is_todo is None or is_todo(idx):
            return idx
    return None


def next_batch(batches: list[str], current: str | None, has_rows: Callable[[str], bool], *, wrap: bool) -> str | None:
    """The first of the sorted `batches` after `current` (None: from the first) that `has_rows`
    accepts, or None when there is none.

    Without `wrap` the search stops after the last batch. With `wrap` it goes round once, back to
    `current` itself, so todo images left behind in earlier batches (or skipped in this one) are found."""
    cursor = batches.index(current) if current in batches else -1
    idx = next_index(len(batches), cursor, 1, is_todo=lambda i: has_rows(batches[i]), wrap=wrap)
    return None if idx is None else batches[idx]


class UIState(Enum):
    SPLASH = auto()
    DISPLAY_SELECT = auto()
    REVIEWING = auto()
    END_MESSAGE = auto()
    DISCONNECTED = auto()  # the store was lost: only quitting is possible


class ReviewSession:
    def __init__(
        self,
        store: ReviewStore,
        reviewer: str,
        mode: MarkMode = "single",
        pass_number: int | None = None,
        batch: str | None = None,
        status_filter: StatusFilter = "unreviewed",
        rotation: Rotation = "auto",
    ):
        self.store = store
        self.reviewer = reviewer  # checked by the CLI (connection.parse_reviewer); recorded with each verdict
        self.mode = mode
        self.batch = batch
        self._explicit_batch = batch is not None  # --batch restricts the session: b never leaves it
        self.status_filter = status_filter
        self._default_rotation = rotation  # what m uses; M uses "never"
        self.rotation = rotation
        self.manifest = store.manifest()

        self._explicit_pass = pass_number is not None  # --pass: b keeps it rather than re-reading the current pass
        self.pass_number = store.current_pass() if pass_number is None else pass_number
        self._statuses = store.statuses(self.pass_number)

        self.autoplay = False
        self._cursor = -1
        self._ui_state = UIState.REVIEWING
        self._shown_at: int | None = None  # ticks when the current item was first painted
        self._advance_pending = False  # a post-mark advance is due; an already-queued ADVANCE_EVENT obeys this
        self._todo_only = False
        self._marked_this_session: set[str] = set()  # keys marked here (less those undone): done under clean/all
        self._undoable = 0  # this session's marks since the current mode started, less those undone
        self._unloadable: set[str] = set()  # keys that failed to load: shown as placeholders, never marked CLEAN
        # The last pack_into_grids result and its key, so switching modes back and forth packs once;
        # kept across single mode within a batch, dropped by b
        self._grid_cache: tuple[GridCacheKey, list[GridSpec], list[str]] | None = None

        self._viewer = ImageViewer()
        # Gamepads with an SDL game controller mapping, by instance id. SDL also sends JOYDEVICEADDED
        # for each of them; only the CONTROLLER* events are handled, so a pad is counted once.
        sdl_controller.init()
        self._gamepads: dict[int, sdl_controller.Controller] = {}
        self._pre_display_index = 0

        if self.batch is None:
            self.batch = self._auto_select_batch()

        self._build_items()

    def _store_lost(self, exc: StoreUnavailable):
        self._stop_timers()
        log.error("Lost connection to server: %s. Progress up to the last mark is saved on the server.", exc)
        self._viewer.show_message("Lost connection to server - progress saved. Press q to quit.")
        self._ui_state = UIState.DISCONNECTED

    def _stop_autoplay(self):
        self.autoplay = False
        pg.time.set_timer(AUTOPLAY_EVENT, 0)

    def _cancel_advance(self):
        """Cancel a pending post-mark advance, including an ADVANCE_EVENT already queued."""
        pg.time.set_timer(ADVANCE_EVENT, 0)
        self._advance_pending = False

    def _stop_timers(self):
        """Cancel autoplay and a pending post-mark advance."""
        self._cancel_advance()
        self._stop_autoplay()

    def _switch_to_grid(self, rotation: Rotation):
        self.rotation = rotation
        self._restart_in_mode("grid")

    def _switch_to_single(self):
        self._restart_in_mode("single")

    def _review_rows(self, batch: str | None) -> list[ManifestRow]:
        """Rows the current mode may show: the status filter's, limited to GRID_ELIGIBLE in grid mode."""
        rows = filter_rows(self.manifest, self._statuses, self.status_filter, batch)
        if self.mode == "grid":
            rows = [r for r in rows if self._statuses[r.key] in GRID_ELIGIBLE]
        return rows

    def _held_back_count(self, batch: str | None) -> int:
        """Todo rows (see _key_todo) of `batch` (None: every batch) the status filter selects that the
        current mode leaves out (grid mode: DIRTY and FLAGGED)."""
        if self.mode != "grid":
            return 0
        selected = filter_rows(self.manifest, self._statuses, self.status_filter, batch)
        return sum(1 for row in selected if self._statuses[row.key] not in GRID_ELIGIBLE and self._key_todo(row.key))

    def _held_back_message(self, batch: str | None, *, in_session: bool) -> str | None:
        """Names the todo rows of `batch` that grid mode leaves out, or None when there are none. In the
        session the hint is the s key; in the terminal, the --mode option."""
        held = self._held_back_count(batch)
        if not held:
            return None
        images = "image needs" if held == 1 else "images need"
        hint = " - press [s]" if in_session else " (--mode single)"
        return f"No grid items for pass {self.pass_number}; {held} FLAGGED/DIRTY {images} single-mode review{hint}"

    def _batches(self) -> list[str]:
        return sorted({row.batch for row in self.manifest})

    def _auto_select_batch(self) -> str | None:
        """Find the first batch that has images the current mode may show."""
        return next_batch(self._batches(), None, lambda b: bool(self._review_rows(b)), wrap=False)

    def _key_todo(self, key: str) -> bool:
        """A key is todo while its status is UNREVIEWED or FLAGGED; under clean/all, where every key is
        a re-check, also until it is marked in this session."""
        if self._statuses[key] in TODO_STATUSES:
            return True
        return self.status_filter != "unreviewed" and key not in self._marked_this_session

    def _batch_has_todo(self, batch: str) -> bool:
        """Whether `batch` has rows the current mode may show that are todo (see _key_todo)."""
        return any(self._key_todo(row.key) for row in self._review_rows(batch))

    def _single_items(self) -> list[ReviewItem]:
        rows = self._review_rows(self.batch)
        random.shuffle(rows)
        return [ReviewItem(keys=(row.key,), label=row.key, surface=None, grid=False) for row in rows]

    def _build_items(self):
        """Build the items for the current mode and batch, then drop verdict input queued while blocked."""
        try:
            if self.mode == "grid":
                self._viewer.show_message("Computing grids...")
                self._items = self._grid_items()
            else:
                self._items = self._single_items()
        finally:
            pg.event.clear(VERDICT_INPUT_EVENTS)  # pressed while blocked, before anything new was shown

    def _show_end(self, text: str):
        self._ui_state = UIState.END_MESSAGE
        self._viewer.show_message(text)

    def _show_grid_progress(self, done: int, total: int) -> None:
        if done % 25 == 0 or done == total:
            self._viewer.show_message(f"Computing grids... {done}/{total}")  # also flips the display
            pg.event.pump()  # keeps the OS from flagging the window; leaves queued key events alone

    def _grid_items(self) -> list[ReviewItem]:
        grid_w, grid_h = self._grid_size()

        review_rows = self._review_rows(self.batch)
        # A mark that changes which rows are eligible changes the key, so a cached grid never holds
        # a key the current snapshot excludes (e.g. one now DIRTY)
        cache_key = GridCacheKey(tuple(row.key for row in review_rows), (grid_w, grid_h), self.rotation)
        if self._grid_cache is None or self._grid_cache[0] != cache_key:
            self._grid_cache = None  # hold at most one result, and none if packing fails
            grid_specs, left_out = pack_into_grids(
                review_rows, self.store, grid_w, grid_h, rotation=self.rotation, on_progress=self._show_grid_progress
            )
            self._grid_cache = (cache_key, grid_specs, left_out)
        _, grid_specs, left_out = self._grid_cache

        items = [
            ReviewItem(
                keys=gs.keys,
                label=f"grid ({len(gs.keys)} images)",
                surface=gs.surface,
                grid=True,
                source_scale=gs.min_scale,
            )
            for gs in grid_specs
        ]
        random.shuffle(items)
        items.sort(key=lambda item: len(item.keys), reverse=True)
        # Each image left out of the grids (unloadable, or left unpacked) becomes a single image after them:
        # it follows the single-image rules, and _show_current loads it when it is shown, drawing a
        # placeholder only if that fails
        items += [ReviewItem(keys=(key,), label=key, surface=None, grid=False) for key in left_out]
        return items

    def _show_display_select(self):
        self._stop_timers()
        self._shown_at = None
        self._ui_state = UIState.DISPLAY_SELECT
        self._viewer.show_splash(
            self._viewer.display_lines(),
            footer=["Press [1]-[9] to switch, [space] to confirm"],
        )

    def _show_splash(self):
        self._stop_timers()
        self._shown_at = None
        footer_lines = [
            f"Press [space] for {self.mode} image review",
            "[s] single  [m] grid  [M] grid (no rotation)",
            "[f] toggle fullscreen",
        ]
        self._viewer.show_splash(
            [self._info_line()],
            footer=footer_lines,
        )
        self._ui_state = UIState.SPLASH

    def _info_line(self) -> str:
        parts = [f"{self.batch} pass {self.pass_number}"] if self.batch else [f"pass {self.pass_number}"]
        batches = self._batches()
        if self.batch in batches:
            parts.append(f"batch {batches.index(self.batch) + 1}/{len(batches)}")
        if self.status_filter != "unreviewed":
            parts.append(f"filter: {self.status_filter}")
        parts.append(f"{len(self._items)} images")
        parts.append(f"{self.mode} image review")
        return " - ".join(parts)

    def _restart_in_mode(self, new_mode: MarkMode, *, refetch_statuses: bool = True):
        """Rebuild the items for `new_mode` and the current batch and start at the first item.
        Without `refetch_statuses` the snapshot the caller just fetched is used."""
        self._stop_timers()
        self.mode = new_mode
        self._cursor = -1
        self._undoable = 0  # z only undoes marks it can show; the old mode's items are gone
        self._shown_at = None
        try:
            if refetch_statuses:
                self._statuses = self.store.statuses(self.pass_number)
            self._build_items()
        except StoreUnavailable as exc:
            self._items = []  # half-switched state: nothing consistent to show
            self._store_lost(exc)
            return

        if not self._items:
            self._show_end(self._held_back_message(self.batch, in_session=True) or f"No items for {new_mode} mode")
            return

        self._ui_state = UIState.REVIEWING
        self.next_image()

    def _next_batch(self):
        """Move on to the next batch with todo images, re-reading the pass (adopted unless --pass
        fixed it) and the statuses, and start it at its first item.

        The search wraps round once, so todo left behind in earlier batches (or skipped in this one)
        is found under every filter. On a pass change it starts from the first batch, and the
        marked set is kept (under clean/all a re-check of a finished pass advances the auto-detected
        pass itself); a marked key that is UNREVIEWED or FLAGGED again is todo anyway (_key_todo).
        --batch restricts it to that batch."""
        self._stop_timers()
        old_pass = self.pass_number
        try:
            current_pass = self.store.current_pass()
            if not self._explicit_pass:
                self.pass_number = current_pass
            self._statuses = self.store.statuses(self.pass_number)
        except StoreUnavailable as exc:
            self._items = []
            self._store_lost(exc)
            return
        self._grid_cache = None  # the batch is left (or the list ends): don't keep its canvases alive
        pass_changed = self.pass_number != old_pass
        batches = [b for b in self._batches() if not self._explicit_batch or b == self.batch]
        batch = next_batch(batches, None if pass_changed else self.batch, self._batch_has_todo, wrap=True)
        if batch is None:
            # The items may belong to a pass that has ended: drop them, so only quitting (or a mode switch) remains
            self._items = []
            self._undoable = 0
            message = self._held_back_message(self.batch if self._explicit_batch else None, in_session=True)
            if message:  # so s opens the first batch with images grid mode left out
                self.batch = next_batch(batches, None, lambda b: self._held_back_count(b) > 0, wrap=False)
            self._show_end(message or self._all_done_message(old_pass, current_pass))
            return
        self.batch = batch
        self._restart_in_mode(self.mode, refetch_statuses=False)
        if pass_changed and self._ui_state == UIState.REVIEWING:
            self._notify(f"Now pass {self.pass_number}")

    def _all_done_message(self, old_pass: int, current_pass: int) -> str:
        """What b shows when no batch has todo left (and none holds images back from grid mode)."""
        if (
            self.pass_number > old_pass and self.status_filter == "unreviewed"
        ):  # under clean/all a re-check advances the pass
            where = f"{self.batch} for pass" if self._explicit_batch else "pass"
            message = f"Pass {old_pass} complete - nothing to review in {where} {self.pass_number}"
        elif self._explicit_batch:
            message = f"Batch {self.batch} done for pass {self.pass_number}"
        else:
            message = f"All batches done for pass {self.pass_number}"
        if current_pass != self.pass_number:
            message += f" (current pass is {current_pass})"
        return message

    def _no_todo_message(self) -> str:
        """The end message of todo-only navigation: none left at all, or none in that direction."""
        if self._todo_count:
            return self._end_message(NO_TODO_THIS_WAY_MESSAGE, hint="[Left/Right] wrap")
        return self._end_message(NO_TODO_MESSAGE)

    def _end_message(self, text: str, hint: str = "") -> str:
        """An end-of-list message with the batch's todo count, an optional hint and the b hint."""
        todo = f" - {self._todo_count} todo left" if self._todo_count else ""
        hint = f" - {hint}" if hint else ""
        return f"{text}{todo}{hint} - [b] next batch"

    def _is_todo(self, item: ReviewItem) -> bool:
        """Under clean/all every item is a re-check, so it is todo until marked in this session, and
        again once a key is UNREVIEWED or FLAGGED (see _key_todo).

        Any todo key keeps a grid todo, so a partly undone grid comes back.
        """
        if self.status_filter == "unreviewed":
            return self._item_status(item) in TODO_STATUSES
        return any(self._key_todo(key) for key in item.keys)

    @property
    def _todo_count(self) -> int:
        return sum(1 for item in self._items if self._is_todo(item))

    def _seek(self, start: int, direction: int, *, todo_only: bool, wrap: bool) -> bool:
        """Show the next item from `start` in `direction`, among the todo items if `todo_only`.
        Start at -1 going forward, or at len(items) going back with `wrap`, to search the whole
        list. Returns False, leaving the cursor alone, when there is none."""
        idx = next_index(
            len(self._items),
            start,
            direction,
            is_todo=(lambda i: self._is_todo(self._items[i])) if todo_only else None,
            wrap=wrap,
        )
        if idx is None:
            return False
        self._cursor = idx
        self._show_current()
        return True

    def next_todo(self, direction: int = 1, *, wrap: bool = True) -> bool:
        """Navigate to next todo item. Returns True if found."""
        return self._seek(self._cursor, direction, todo_only=True, wrap=wrap)

    def _show_current(self):
        self._shown_at = None
        self._advance_pending = False  # the advance belonged to the item being replaced
        if not self._items:
            return
        item = self._items[self._cursor]

        surface = item.surface
        if surface is None:
            key = item.keys[0]
            reason = detail = None  # reason: shown on the placeholder; detail: the error, for the log only
            try:
                surface = load_surface(self.store.image_bytes(key))
            except StoreUnavailable as exc:
                self._store_lost(exc)
                return
            except KeyError:
                reason = detail = "image could not be fetched"
            except Exception as exc:  # noqa: BLE001 - a read or decode failure is an unloadable image, not an outage
                reason, detail = "image could not be read or decoded", f"{type(exc).__name__}: {exc}"
            if reason is None:
                self._unloadable.discard(key)
            else:
                log.warning("cannot load %s: %s", key, detail)
                self._unloadable.add(key)
                surface = _placeholder(key, reason)

        status = self._item_status(item)
        info = f"{self._cursor + 1} / {len(self._items)} ({self._todo_count} todo)"
        self._viewer.set_image(surface, item.label, status, info, item.source_scale)

    def _item_status(self, item: ReviewItem) -> Status:
        if item.grid:
            return _grid_status(self._statuses, item.keys)
        return self._statuses[item.keys[0]]

    def _navigate(self, direction: int, *, autoplay: bool = False):
        if not self._items:
            return

        if not self._seek(self._cursor, direction, todo_only=self._todo_only, wrap=False):
            self._stop_autoplay()
            self._show_end(self._no_todo_message() if self._todo_only else self._end_message(END_OF_LIST_MESSAGE))
        elif self._ui_state == UIState.REVIEWING and direction == 1 and (autoplay or self.autoplay):
            self.autoplay = True
            pg.time.set_timer(AUTOPLAY_EVENT, 500, 1)

    def next_image(self, *, autoplay=False):
        self._navigate(1, autoplay=autoplay)

    def prev_image(self):
        self._navigate(-1)

    def _mark(self, status: Verdict):
        if not self._items or self._cursor < 0:
            return
        item = self._items[self._cursor]
        if status == "CLEAN" and not self._unloadable.isdisjoint(item.keys):
            log.warning("%s: %s", UNLOADABLE_CLEAN, ", ".join(k for k in item.keys if k in self._unloadable))
            self._viewer.set_info(UNLOADABLE_CLEAN)
            return
        if item.grid and status == "CLEAN" and _grid_clean_refused(self._statuses, item.keys):
            log.warning("%s", GRID_HAS_DIRTY)
            self._viewer.set_info(GRID_HAS_DIRTY)
            return
        try:
            changed = self.store.mark(list(item.keys), status, self.pass_number, reviewer=self.reviewer, mode=self.mode)
        except StoreUnavailable as exc:
            self._store_lost(exc)
            return
        self._undoable += 1
        self._marked_this_session.update(item.keys)
        self._statuses.update(changed)
        self._viewer.set_status(status)
        pg.time.set_timer(ADVANCE_EVENT, 200, 1)
        self._advance_pending = True

    def _notify(self, text: str):
        """Show text in the info bar while reviewing, else as the screen's message."""
        if self._ui_state == UIState.REVIEWING:
            self._viewer.set_info(text)
        else:
            self._viewer.show_message(text)

    def _undo(self):
        """Undo this mode's latest mark and show the item holding its keys, for a fresh dwell."""
        self._stop_timers()
        if not self._undoable:  # never undo a mark this mode did not make (and so may not be able to show)
            self._notify(NOTHING_TO_UNDO)
            return
        try:
            changed = self.store.undo(self.pass_number, reviewer=self.reviewer)
        except StoreUnavailable as exc:
            self._store_lost(exc)
            return
        if not changed:  # the store's history is gone (e.g. a restarted server)
            self._undoable = 0
            self._notify(NOTHING_TO_UNDO)
            return
        self._undoable -= 1
        self._marked_this_session.difference_update(changed)
        self._statuses.update(changed)
        index = next((i for i, item in enumerate(self._items) if any(k in changed for k in item.keys)), None)
        if index is None:  # another client's mark on a shared server
            shown = ", ".join(f"{k} {v}" for k, v in list(changed.items())[:3])
            more = f" and {len(changed) - 3} more" if len(changed) > 3 else ""
            self._notify(f"Undid a mark not in this view: {shown}{more}")
            return
        self._cursor = index
        self._ui_state = UIState.REVIEWING  # also from the end-of-list screen, to undo the batch's last mark
        self._show_current()  # resets the dwell: c/d count only once the restored item has been seen

    def _verdict_input(self, status: Verdict, now: int):
        """A verdict key or button at tick `now`: stops autoplay, and marks only an item on screen
        for MIN_DWELL_MS."""
        self._stop_autoplay()
        if _dwell_elapsed(self._shown_at, now):
            self._mark(status)

    def _resume(self):
        """Leave the splash or display-select screen for the current image."""
        self._ui_state = UIState.REVIEWING
        if self._cursor == -1:
            self.next_image()
        else:
            self._show_current()

    def _handle_mode_key(self, key: int, mod: int) -> bool:
        """Switch mode on m (shift+m: no rotation) or s. Returns whether `key` was one of them."""
        match key:
            case pg.K_m if mod & pg.KMOD_SHIFT:
                self._switch_to_grid("never")
            case pg.K_m:
                self._switch_to_grid(self._default_rotation)
            case pg.K_s:
                self._switch_to_single()
            case _:
                return False
        return True

    def _handle_splash_key(self, key):
        """Handle key press while splash is shown."""
        if key in (pg.K_SPACE, pg.K_h):
            self._resume()
        elif key == pg.K_f:
            pg.display.toggle_fullscreen()

    def _handle_display_select_key(self, key):
        """Handle key press on the display-select screen."""
        if pg.K_1 <= key <= pg.K_9:
            if self._viewer.switch_display(key - pg.K_1):
                self._show_display_select()
        elif key in (pg.K_SPACE, pg.K_h):
            if self._viewer.display_index != self._pre_display_index and self.mode == "grid":
                self._restart_in_mode("grid")
            else:
                self._resume()
        elif key == pg.K_f:
            pg.display.toggle_fullscreen()

    def _handle_end_key(self, key):
        """Handle key press at end-of-list screen."""
        direction = None
        if key in (pg.K_RIGHT, pg.K_SPACE):
            direction = 1
        elif key == pg.K_LEFT:
            direction = -1
        if direction is not None and self._items:  # no items: keep the message (e.g. lost connection)
            self._ui_state = UIState.REVIEWING
            start = -1 if direction == 1 else len(self._items)
            if not self._seek(start, direction, todo_only=self._todo_only, wrap=True):
                self._show_end(self._no_todo_message())
        elif key == pg.K_z:
            self._undo()
        elif key == pg.K_b:
            self._next_batch()

    def _handle_review_key(self, key, now: int):
        """Handle key press during review."""
        if key != pg.K_SPACE:  # Space toggles autoplay below; any other key stops it
            self._stop_autoplay()
        match key:
            case pg.K_c:
                self._verdict_input("CLEAN", now)
            case pg.K_d:
                self._verdict_input("DIRTY", now)
            case pg.K_z:
                self._undo()
            case pg.K_w:
                self._pre_display_index = self._viewer.display_index
                self._show_display_select()
            case pg.K_SPACE:
                if self.autoplay:
                    self._stop_autoplay()
                else:
                    self.next_image(autoplay=True)
            case pg.K_n:
                self._cancel_advance()
                if not self.next_todo():  # stay on this item: the reviewer is mid-review
                    self._notify(NO_TODO_MESSAGE)
            case pg.K_u:
                self._todo_only = not self._todo_only
                self._viewer.set_todo_only(self._todo_only)
            case pg.K_f:
                pg.display.toggle_fullscreen()
            case pg.K_h:
                self._show_splash()
            case pg.K_LEFT:
                self._cancel_advance()
                self.prev_image()
            case pg.K_RIGHT:
                self._cancel_advance()
                self.next_image()

    def _handle_button(self, button: int, now: int) -> bool:
        """Handle a gamepad button, numbered by SDL's standard layout. Returns True to quit."""
        if button == pg.CONTROLLER_BUTTON_START:
            return True
        if self._ui_state == UIState.REVIEWING:
            self._stop_autoplay()  # no button toggles autoplay, so every button stops it
            if button in REVIEW_BUTTON_KEYS:
                self._handle_review_key(REVIEW_BUTTON_KEYS[button], now)
        elif button == pg.CONTROLLER_BUTTON_A:  # continue, as Space
            self._handle_screen_key(pg.K_SPACE, now)
        return False

    def _handle_screen_key(self, key: int, now: int):
        """Dispatch a key to the current screen's handler. DISCONNECTED takes no keys."""
        match self._ui_state:
            case UIState.END_MESSAGE:
                self._handle_end_key(key)
            case UIState.SPLASH:
                self._handle_splash_key(key)
            case UIState.DISPLAY_SELECT:
                self._handle_display_select_key(key)
            case UIState.REVIEWING:
                self._handle_review_key(key, now)

    def run(self):
        if not self._items:
            filter_msg = f" (filter: {self.status_filter})" if self.status_filter != "unreviewed" else ""
            print(
                self._held_back_message(self.batch, in_session=False)
                or f"No images to review for pass {self.pass_number}{filter_msg}."
            )
            return

        batch_info = f", batch {self.batch}" if self.batch else ""
        filter_info = f", filter {self.status_filter}" if self.status_filter != "unreviewed" else ""
        print(
            f"Starting {self.mode} review, pass {self.pass_number}{batch_info}{filter_info}, {len(self._items)} items"
        )

        if self._ui_state != UIState.SPLASH:
            self._show_splash()

        clock = pg.time.Clock()
        while self.handle_events(pg.event.get()):
            self.refresh_if_needed()
            clock.tick(60)

    def handle_events(self, events: list[pg.event.Event]) -> bool:
        """Apply one batch of events. Returns False to quit.

        The clock is read once, before the batch: time spent handling earlier events (a resize,
        a fullscreen toggle) must not count toward a verdict's dwell."""
        now = pg.time.get_ticks()
        return not any(self._handle_event(event, now) for event in events)

    def _handle_event(self, event: pg.event.Event, now: int) -> bool:
        """Apply one event of a batch read at tick `now`. Returns True to quit."""
        match event.type:
            case pg.CONTROLLERBUTTONDOWN:
                return self._handle_button(event.button, now)
            case pg.KEYDOWN:
                if event.key in (pg.K_ESCAPE, pg.K_q):
                    return True
                if self._ui_state == UIState.DISCONNECTED or self._handle_mode_key(event.key, event.mod):
                    return False
                self._handle_screen_key(event.key, now)
            case pg.WINDOWRESIZED:
                self._viewer.resize()  # grid mode repacks on the next tick if the size changed
            case x if x == AUTOPLAY_EVENT:
                if self.autoplay and self._ui_state == UIState.REVIEWING:
                    self.next_image()
            case x if x == ADVANCE_EVENT:
                if self._advance_pending and self._ui_state == UIState.REVIEWING:
                    self._advance_pending = False
                    self.next_image()
            case pg.CONTROLLERDEVICEADDED:
                pad = sdl_controller.Controller(event.device_index)
                self._gamepads[pad.as_joystick().get_instance_id()] = pad
                self._viewer.set_joystick_count(len(self._gamepads))
            case pg.CONTROLLERDEVICEREMOVED:
                self._gamepads.pop(event.instance_id, None)
                self._viewer.set_joystick_count(len(self._gamepads))
            case pg.QUIT:
                return True
        return False

    def _grid_size(self) -> tuple[int, int]:
        """The size grids are packed for: the window less the status bar."""
        w, h = self._viewer.screen.get_size()
        return w, h - self._viewer.border

    def _rebuild_grids_for_resize(self):
        """Repack the grids at the new window size (one rebuild however many resize events came).
        The cursor stays on the grid holding the current item's first key, else goes to the start; the
        dwell restarts, so a verdict never lands on a re-composited grid that has not been seen."""
        if not self._items:
            return  # nothing to rebuild
        self._stop_timers()
        self._undoable = 0  # a repack drops grids marked DIRTY, so z could no longer show what it undoes
        current = self._items[self._cursor].keys[0] if self._cursor >= 0 else None
        try:
            self._build_items()
        except StoreUnavailable as exc:
            self._items = []
            self._store_lost(exc)
            return
        if not self._items:  # everything left was marked meanwhile (or is held back): as _restart_in_mode
            self._show_end(
                self._held_back_message(self.batch, in_session=True) or self._end_message(END_OF_LIST_MESSAGE)
            )
            return
        self._cursor = next((i for i, item in enumerate(self._items) if current in item.keys), 0)
        self._show_current()

    def refresh_if_needed(self):
        """Repaint the current item if something changed. Only in REVIEWING: the splash, help and
        message screens are painted once by the viewer and a repaint would cover them."""
        if self._ui_state != UIState.REVIEWING:
            return
        # Grid mode repacks when the window size is no longer the one the cached grids were packed for
        if self.mode == "grid" and self._grid_cache is not None and self._grid_cache[0].size != self._grid_size():
            self._rebuild_grids_for_resize()
            if self._ui_state != UIState.REVIEWING:  # the rebuild ended on a message screen
                return
        # The dwell runs only while paints show the item's pixels: bars alone stop it, and it
        # restarts once the pixels return
        if self._viewer.refresh_if_dirty():
            if not self._viewer.image_shown:
                self._shown_at = None
            elif self._shown_at is None:
                self._shown_at = pg.time.get_ticks()
