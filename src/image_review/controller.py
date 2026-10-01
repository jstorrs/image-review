import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

import pygame as pg

from .grid_packer import pack_into_grids
from .store import (
    TODO_STATUSES,
    ManifestRow,
    MarkMode,
    ReviewStore,
    Status,
    StatusFilter,
    StoreUnavailable,
    Verdict,
    filter_rows,
)
from .util import load_surface
from .viewer import ImageViewer, placeholder_surface

AUTOPLAY_EVENT = pg.USEREVENT + 1
ADVANCE_EVENT = pg.USEREVENT + 2

# A verdict key or button only counts once the current item has been painted for this long, so
# input queued while the UI was blocked, or typed as the screen changed, never judges an unseen item.
MIN_DWELL_MS = 200
# Input events dropped after a blocking step (grid build, mode restart).
VERDICT_INPUT_EVENTS = (pg.KEYDOWN, pg.JOYBUTTONDOWN)


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


def _grid_status(snapshot: dict[str, Status], keys: tuple[str, ...]) -> str:
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


@dataclass(frozen=True)
class ReviewItem:
    """One reviewable item: a single image (surface None, loaded when displayed) or a grid
    (composited when built). A grid's status and CLEAN refusal follow the grid rules even
    when it holds one key (an overflow image), so `grid` is kept rather than read off len(keys)."""

    keys: tuple[str, ...]
    label: str
    surface: pg.Surface | None
    grid: bool


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
        allow_rotation: bool = True,
    ):
        self.store = store
        self.reviewer = reviewer  # checked by the CLI (connection.parse_reviewer); recorded with each verdict
        self.mode = mode
        self.batch = batch
        self._explicit_batch = batch is not None  # --batch restricts the session: b never leaves it
        self.status_filter = status_filter
        self.allow_rotation = allow_rotation
        self.manifest = store.manifest()

        self._explicit_pass = pass_number is not None  # --pass: b keeps it rather than re-reading the current pass
        if pass_number is None:
            self.pass_number = store.current_pass()
        else:
            self.pass_number = pass_number
        self._statuses = store.statuses(self.pass_number)

        self.autoplay = False
        self._cursor = -1
        self._dirty = True
        self._ui_state = UIState.REVIEWING
        self._shown_at: int | None = None  # ticks when the current item was first painted
        self._advance_pending = False  # a post-mark advance is due; an already-queued ADVANCE_EVENT obeys this
        self._todo_only = False
        self._marked_this_session: set[str] = set()  # keys marked here (less those undone): done under clean/all
        self._undoable = 0  # this session's marks since the current mode started, less those undone
        self._unloadable: set[str] = set()  # keys that failed to load: shown as placeholders, never marked CLEAN

        self._viewer = ImageViewer()
        self._joysticks = {}
        self._display_select = False
        self._pre_display_index = 0

        if self.batch is None:
            self.batch = self._auto_select_batch()

        if mode == "grid":
            self._viewer.show_message("Computing grids...")
            self._init_grid_mode()
            pg.event.clear(VERDICT_INPUT_EVENTS)
        else:
            self._init_single_mode()

    def _store_lost(self, exc: StoreUnavailable):
        self._stop_timers()
        print(f"Lost connection to server: {exc}. Progress up to the last mark is saved on the server.", file=sys.stderr)
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

    def _switch_to_grid(self, allow_rotation: bool):
        self.allow_rotation = allow_rotation
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
        shown = {row.key for row in self._review_rows(batch)}
        selected = filter_rows(self.manifest, self._statuses, self.status_filter, batch)
        return sum(1 for row in selected if row.key not in shown and self._key_todo(row.key))

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

    def _init_single_mode(self):
        rows = self._review_rows(self.batch)
        random.shuffle(rows)
        self._items = [ReviewItem(keys=(row.key,), label=row.key, surface=None, grid=False) for row in rows]
        self._todo_count = self._count_todo()

    def _init_grid_mode(self):
        grid_w, grid_h = self._viewer.screen.get_size()
        grid_h -= self._viewer.border

        review_rows = self._review_rows(self.batch)
        grid_specs, unloadable = pack_into_grids(review_rows, self.store, grid_w, grid_h, allow_rotation=self.allow_rotation)

        items = [
            ReviewItem(keys=tuple(gs.keys), label=f"grid ({len(gs.keys)} images)", surface=gs.surface, grid=True)
            for gs in grid_specs
        ]
        random.shuffle(items)
        items.sort(key=lambda item: len(item.keys), reverse=True)
        # Each unloadable image becomes a single image after the grids: it follows the single-image rules,
        # and _show_current retries it and draws its placeholder only when it is shown
        items += [ReviewItem(keys=(key,), label=key, surface=None, grid=False) for key in unloadable]
        self._items = items
        self._todo_count = self._count_todo()

    def _show_display_select(self):
        self._stop_timers()
        self._shown_at = None
        self._viewer.show_splash(
            self._viewer.display_lines(),
            footer="Press [1]-[9] to switch, [space] to confirm",
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
            [self._info_line(len(self._items))],
            footer=footer_lines,
        )
        self._ui_state = UIState.SPLASH

    def _info_line(self, n_items: int | None = None) -> str:
        parts = [f"{self.batch} pass {self.pass_number}"] if self.batch else [f"pass {self.pass_number}"]
        batches = self._batches()
        if self.batch in batches:
            parts.append(f"batch {batches.index(self.batch) + 1}/{len(batches)}")
        if self.status_filter != "unreviewed":
            parts.append(f"filter: {self.status_filter}")
        if n_items is not None:
            parts.append(f"{n_items} images")
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
        self._dirty = True
        try:
            if refetch_statuses:
                self._statuses = self.store.statuses(self.pass_number)
            if new_mode == "grid":
                self._viewer.show_message("Computing grids...")
                self._init_grid_mode()
            else:
                self._init_single_mode()
        except StoreUnavailable as exc:
            self._items = []  # half-switched state: nothing consistent to show
            self._store_lost(exc)
            return
        finally:
            pg.event.clear(VERDICT_INPUT_EVENTS)  # pressed while blocked, before anything new was shown

        if not self._items:
            self._viewer.show_message(self._held_back_message(self.batch, in_session=True) or f"No items for {new_mode} mode")
            self._ui_state = UIState.END_MESSAGE
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
        pass_changed = self.pass_number != old_pass
        batches = [b for b in self._batches() if not self._explicit_batch or b == self.batch]
        batch = next_batch(batches, None if pass_changed else self.batch, self._batch_has_todo, wrap=True)
        if batch is None:
            # The items may belong to a pass that has ended: drop them, so only quitting (or a mode switch) remains
            self._items = []
            self._undoable = 0
            self._ui_state = UIState.END_MESSAGE
            message = self._held_back_message(self.batch if self._explicit_batch else None, in_session=True)
            if message:  # so s opens the first batch with images grid mode left out
                self.batch = next_batch(batches, None, lambda b: self._held_back_count(b) > 0, wrap=False)
            self._viewer.show_message(message or self._all_done_message(old_pass, current_pass))
            return
        self.batch = batch
        self._restart_in_mode(self.mode, refetch_statuses=False)
        if pass_changed and self._ui_state == UIState.REVIEWING:
            self._notify(f"Now pass {self.pass_number}")

    def _all_done_message(self, old_pass: int, current_pass: int) -> str:
        """What b shows when no batch has todo left (and none holds images back from grid mode)."""
        if self.pass_number > old_pass and self.status_filter == "unreviewed":  # under clean/all a re-check advances the pass
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
        return self._end_message(NO_TODO_THIS_WAY_MESSAGE if self._todo_count else NO_TODO_MESSAGE)

    def _end_message(self, text: str) -> str:
        """An end-of-list message with the batch's todo count and the b hint."""
        todo = f" - {self._todo_count} todo left" if self._todo_count else ""
        return f"{text}{todo} - [b] next batch"

    def _is_todo(self, item: ReviewItem) -> bool:
        """Under clean/all every item is a re-check, so it is todo until marked in this session, and
        again once a key is UNREVIEWED or FLAGGED (see _key_todo).

        Any todo key keeps a grid todo, so a partly undone grid comes back.
        """
        if self.status_filter == "unreviewed":
            return self._item_status(item) in TODO_STATUSES
        return any(self._key_todo(key) for key in item.keys)

    def _count_todo(self) -> int:
        return sum(1 for item in self._items if self._is_todo(item))

    def next_todo(self, direction: int = 1, *, wrap: bool = True) -> bool:
        """Navigate to next todo item. Returns True if found."""
        idx = next_index(
            len(self._items), self._cursor, direction,
            is_todo=lambda i: self._is_todo(self._items[i]), wrap=wrap,
        )
        if idx is None:
            return False
        self._cursor = idx
        self._show_current()
        return True

    def _show_current(self):
        self._shown_at = None
        self._advance_pending = False  # the advance belonged to the item being replaced
        if not self._items:
            return
        item = self._items[self._cursor]

        surface = item.surface
        if surface is None:
            key = item.keys[0]
            reason = detail = None  # reason: shown on the placeholder; detail: the error, for stderr only
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
                print(f"WARNING: cannot load {key}: {detail}", file=sys.stderr)
                self._unloadable.add(key)
                surface = _placeholder(key, reason)

        status = self._item_status(item)
        info = f"{self._cursor + 1} / {len(self._items)} ({self._todo_count} todo)"
        self._viewer.set_image(surface, item.label, status, info)
        self._dirty = True

    def _item_status(self, item: ReviewItem) -> str:
        if item.grid:
            return _grid_status(self._statuses, item.keys)
        return self._statuses[item.keys[0]]

    def _continue_autoplay(self, direction: int, autoplay: bool = False):
        if direction == 1 and (autoplay or self.autoplay):
            self.autoplay = True
            pg.time.set_timer(AUTOPLAY_EVENT, 500, 1)
        elif direction == -1:
            self.autoplay = False

    def _navigate(self, direction: int, *, autoplay: bool = False):
        if not self._items:
            return

        if self._todo_only:
            if self.next_todo(direction, wrap=False):
                if self._ui_state == UIState.REVIEWING:
                    self._continue_autoplay(direction, autoplay)
            else:
                self._stop_autoplay()
                self._ui_state = UIState.END_MESSAGE
                self._viewer.show_message(self._no_todo_message())
            return

        idx = next_index(len(self._items), self._cursor, direction, is_todo=None, wrap=False)
        if idx is None:
            self._stop_autoplay()
            self._ui_state = UIState.END_MESSAGE
            self._viewer.show_message(self._end_message(END_OF_LIST_MESSAGE))
            return

        self._cursor = idx
        self._show_current()
        if self._ui_state == UIState.REVIEWING:
            self._continue_autoplay(direction, autoplay)

    def next_image(self, *, autoplay=False):
        self._navigate(1, autoplay=autoplay)

    def prev_image(self):
        self._navigate(-1)

    def _mark(self, status: Verdict):
        if not self._items or self._cursor < 0:
            return
        item = self._items[self._cursor]
        if status == "CLEAN" and not self._unloadable.isdisjoint(item.keys):
            print(f"WARNING: {UNLOADABLE_CLEAN}: {', '.join(k for k in item.keys if k in self._unloadable)}", file=sys.stderr)
            self._viewer.set_info(UNLOADABLE_CLEAN)
            self._dirty = True
            return
        if item.grid and status == "CLEAN" and _grid_clean_refused(self._statuses, item.keys):
            print(f"WARNING: {GRID_HAS_DIRTY}", file=sys.stderr)
            self._viewer.set_info(GRID_HAS_DIRTY)
            self._dirty = True
            return
        try:
            changed = self.store.mark(list(item.keys), status, self.pass_number, reviewer=self.reviewer, mode=self.mode)
        except StoreUnavailable as exc:
            self._store_lost(exc)
            return
        self._undoable += 1
        self._marked_this_session.update(item.keys)
        self._statuses.update(changed)
        self._todo_count = self._count_todo()
        self._viewer.set_status(status)
        self._dirty = True
        pg.time.set_timer(ADVANCE_EVENT, 200, 1)
        self._advance_pending = True

    def _notify(self, text: str):
        """Show text in the info bar while reviewing, else as the screen's message."""
        if self._ui_state == UIState.REVIEWING:
            self._viewer.set_info(text)
            self._dirty = True
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
        self._todo_count = self._count_todo()
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

    def _handle_splash_key(self, key) -> bool:
        """Handle key press while splash is shown. Returns True to quit."""
        if key in (pg.K_ESCAPE, pg.K_q):
            return True
        if self._display_select and pg.K_1 <= key <= pg.K_9:
            idx = key - pg.K_1
            if self._viewer.switch_display(idx):
                self._show_display_select()
            return False
        if key in (pg.K_SPACE, pg.K_h):
            if self._display_select:
                self._display_select = False
                if self._viewer._display_index != self._pre_display_index:
                    if self.mode == "grid":
                        self._restart_in_mode("grid")
                        return False
            self._ui_state = UIState.REVIEWING
            if self._cursor == -1:
                self.next_image()
            else:
                self._show_current()
        elif key == pg.K_m:
            if pg.key.get_mods() & pg.KMOD_SHIFT:
                self._switch_to_grid(False)
            else:
                self._switch_to_grid(True)
        elif key == pg.K_s:
            self._switch_to_single()
        return False

    def _handle_end_key(self, key) -> bool:
        """Handle key press at end-of-list screen. Returns True to quit."""
        if key in (pg.K_ESCAPE, pg.K_q):
            return True
        direction = None
        if key in (pg.K_RIGHT, pg.K_SPACE):
            direction = 1
        elif key == pg.K_LEFT:
            direction = -1
        if direction is not None and self._items:  # no items: keep the message (e.g. lost connection)
            self._ui_state = UIState.REVIEWING
            if self._todo_only:
                if direction == 1:
                    self._cursor = -1
                if not self.next_todo(direction):
                    self._ui_state = UIState.END_MESSAGE
                    self._viewer.show_message(self._no_todo_message())
            else:
                self._cursor = 0 if direction == 1 else len(self._items) - 1
                self._show_current()
        elif key == pg.K_m:
            if pg.key.get_mods() & pg.KMOD_SHIFT:
                self._switch_to_grid(False)
            else:
                self._switch_to_grid(True)
        elif key == pg.K_s:
            self._switch_to_single()
        elif key == pg.K_z:
            self._undo()
        elif key == pg.K_b:
            self._next_batch()
        return False

    def _handle_review_key(self, key, now: int) -> bool:
        """Handle key press during review. Returns True to quit."""
        match key:
            case pg.K_ESCAPE | pg.K_q:
                return True
            case pg.K_c:
                self._verdict_input("CLEAN", now)
            case pg.K_d:
                self._verdict_input("DIRTY", now)
            case pg.K_z:
                self._undo()
            case pg.K_w:
                self._display_select = True
                self._pre_display_index = self._viewer._display_index
                self._show_display_select()
                self._ui_state = UIState.SPLASH
            case pg.K_SPACE:
                if self.autoplay:
                    self._stop_autoplay()
                else:
                    self.next_image(autoplay=True)
            case pg.K_m:
                if pg.key.get_mods() & pg.KMOD_SHIFT:
                    self._switch_to_grid(False)
                else:
                    self._switch_to_grid(True)
            case pg.K_s:
                self._switch_to_single()
            case pg.K_n:
                self._stop_autoplay()
                self._cancel_advance()
                self.next_todo()
            case pg.K_u:
                self._todo_only = not self._todo_only
                self._viewer.set_todo_only(self._todo_only)
                self._dirty = True
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
        return False

    def run(self):
        if not self._items:
            filter_msg = f" (filter: {self.status_filter})" if self.status_filter != "unreviewed" else ""
            print(self._held_back_message(self.batch, in_session=False) or f"No images to review for pass {self.pass_number}{filter_msg}.")
            return

        batch_info = f", batch {self.batch}" if self.batch else ""
        filter_info = f", filter {self.status_filter}" if self.status_filter != "unreviewed" else ""
        print(f"Starting {self.mode} review, pass {self.pass_number}{batch_info}{filter_info}, {len(self._items)} items")

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
            case pg.JOYBUTTONDOWN:
                if self._ui_state != UIState.REVIEWING:
                    return False
                match event.button:
                    case 1:
                        self._verdict_input("CLEAN", now)
                    case 3:
                        self._verdict_input("DIRTY", now)
                    case 7:
                        return True
            case pg.JOYHATMOTION:
                if self._ui_state == UIState.REVIEWING and event.hat == 0:
                    self._cancel_advance()
                    if event.value[0] < 0:
                        self.prev_image()
                    elif event.value[0] > 0:
                        self.next_image()
            case pg.KEYDOWN:
                match self._ui_state:
                    case UIState.DISCONNECTED:
                        return event.key in (pg.K_ESCAPE, pg.K_q)
                    case UIState.END_MESSAGE:
                        return self._handle_end_key(event.key)
                    case UIState.SPLASH:
                        return self._handle_splash_key(event.key)
                    case UIState.REVIEWING:
                        return self._handle_review_key(event.key, now)
            case pg.WINDOWRESIZED:
                self._viewer.resize()
                self._dirty = True
            case x if x == AUTOPLAY_EVENT:
                if self.autoplay and self._ui_state == UIState.REVIEWING:
                    self.next_image()
            case x if x == ADVANCE_EVENT:
                if self._advance_pending and self._ui_state == UIState.REVIEWING:
                    self._advance_pending = False
                    self.next_image()
            case pg.JOYDEVICEADDED:
                joy = pg.joystick.Joystick(event.device_index)
                self._joysticks[joy.get_instance_id()] = joy
                self._viewer.set_joystick_count(len(self._joysticks))
                self._dirty = True
            case pg.JOYDEVICEREMOVED:
                self._joysticks.pop(event.instance_id, None)
                self._viewer.set_joystick_count(len(self._joysticks))
                self._dirty = True
            case pg.QUIT:
                return True
        return False

    def refresh_if_needed(self):
        """Repaint the current item if something changed. Only in REVIEWING: the splash, help and
        message screens are painted once by the viewer and a repaint would cover them."""
        if not self._dirty or self._ui_state != UIState.REVIEWING:
            return
        self._viewer.refresh()
        self._dirty = False
        if self._shown_at is None:
            self._shown_at = pg.time.get_ticks()
