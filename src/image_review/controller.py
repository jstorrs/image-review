import random
import sys
from enum import Enum, auto

import pygame as pg

from .grid_packer import pack_into_grids
from .store import (
    TODO_STATUSES,
    ManifestRow,
    MarkMode,
    ReviewStore,
    Status,
    StoreUnavailable,
    Verdict,
    filter_rows,
)
from .util import load_surface
from .viewer import ImageViewer

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


def _grid_status(snapshot: dict[str, Status], keys: list[str]) -> str:
    statuses = {snapshot[key] for key in keys}
    if not statuses <= GRID_ELIGIBLE:
        return "DIRTY"  # e.g. a key sharing an image_id with one marked DIRTY elsewhere this session
    if statuses & TODO_STATUSES:
        return "UNREVIEWED"
    return "CLEAN"


def _grid_clean_refused(snapshot: dict[str, Status], keys: list[str]) -> bool:
    """CLEAN on a grid holding a DIRTY or FLAGGED image is refused, unless the whole grid is
    DIRTY (reversing that grid's own verdict)."""
    statuses = {snapshot[key] for key in keys}
    return not statuses <= GRID_ELIGIBLE and statuses != {"DIRTY"}


class UIState(Enum):
    SPLASH = auto()
    REVIEWING = auto()
    END_MESSAGE = auto()


class ReviewSession:
    def __init__(
        self,
        store: ReviewStore,
        reviewer: str,
        mode: MarkMode = "single",
        pass_number: int | None = None,
        batch: str | None = None,
        status_filter: str = "unreviewed",
        allow_rotation: bool = True,
    ):
        self.store = store
        self.reviewer = reviewer  # checked by the CLI (connection.parse_reviewer); recorded with each verdict
        self.mode = mode
        self.batch = batch
        self.status_filter = status_filter
        self.allow_rotation = allow_rotation
        self.manifest = store.manifest()

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
        self._ui_state = UIState.END_MESSAGE

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

    def _held_back_count(self) -> int:
        """Rows the status filter selects that the current mode leaves out (grid mode: DIRTY and FLAGGED)."""
        return len(filter_rows(self.manifest, self._statuses, self.status_filter, self.batch)) - len(self._review_rows(self.batch))

    def _held_back_message(self) -> str | None:
        held = self._held_back_count()
        if not held:
            return None
        images = "image needs" if held == 1 else "images need"
        return f"No grid items for pass {self.pass_number}; {held} FLAGGED/DIRTY {images} single-mode review (--mode single)"

    def _auto_select_batch(self) -> str | None:
        """Find the first batch that has images the current mode may show."""
        batches = sorted({row.batch for row in self.manifest})
        for batch in batches:
            if self._review_rows(batch):
                return batch
        return None

    def _init_single_mode(self):
        rows = self._review_rows(self.batch)
        random.shuffle(rows)
        self._items = rows
        self._todo_count = self._count_todo()

    def _init_grid_mode(self):
        grid_w, grid_h = self._viewer.screen.get_size()
        grid_h -= self._viewer.border

        review_rows = self._review_rows(self.batch)
        grid_specs = pack_into_grids(review_rows, self.store, grid_w, grid_h, allow_rotation=self.allow_rotation)

        items = [
            {"surface": gs.surface, "keys": gs.keys, "batch": gs.batch}
            for gs in grid_specs
        ]
        random.shuffle(items)
        items.sort(key=lambda item: len(item["keys"]), reverse=True)
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
        if self.status_filter != "unreviewed":
            parts.append(f"filter: {self.status_filter}")
        if n_items is not None:
            parts.append(f"{n_items} images")
        parts.append(f"{self.mode} image review")
        return " - ".join(parts)

    def _restart_in_mode(self, new_mode: MarkMode):
        self._stop_timers()
        self.mode = new_mode
        self._cursor = -1
        self._shown_at = None
        self._dirty = True
        try:
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
            self._viewer.show_message(self._held_back_message() or f"No items for {new_mode} mode")
            self._ui_state = UIState.END_MESSAGE
            return

        self._ui_state = UIState.REVIEWING
        self.next_image()

    def _is_todo(self, status: str) -> bool:
        if self.status_filter == "clean":
            return status == "CLEAN"
        return status in TODO_STATUSES

    def _count_todo(self) -> int:
        return sum(1 for item in self._items if self._is_todo(self._item_status(item)))

    def next_todo(self, direction: int = 1, *, wrap: bool = True) -> bool:
        """Navigate to next todo item. Returns True if found."""
        if not self._items:
            return False
        n = len(self._items)
        boundary = 0 if direction == 1 else n - 1
        for offset in range(1, n + 1):
            idx = (self._cursor + direction * offset) % n
            if not wrap and idx == boundary and self._cursor != -1:
                return False
            if self._is_todo(self._item_status(self._items[idx])):
                self._cursor = idx
                self._show_current()
                return True
        return False

    def _show_current(self):
        self._shown_at = None
        self._advance_pending = False  # the advance belonged to the item being replaced
        if not self._items:
            return
        item = self._items[self._cursor]

        if self.mode != "grid":
            # Iteratively skip unloadable images to avoid recursion
            start = self._cursor
            while True:
                try:
                    surface = load_surface(self.store.image_bytes(item.key))
                    break
                except StoreUnavailable as exc:
                    self._store_lost(exc)
                    return
                except Exception as exc:
                    print(f"WARNING: cannot load {item.key}: {exc}", file=sys.stderr)
                    self._cursor += 1
                    if self._cursor >= len(self._items) or self._cursor == start:
                        self._viewer.show_message("No loadable images")
                        self._ui_state = UIState.END_MESSAGE
                        return
                    item = self._items[self._cursor]

        status = self._item_status(item)
        info = f"{self._cursor + 1} / {len(self._items)} ({self._todo_count} todo)"

        if self.mode == "grid":
            surface = item["surface"]
            self._viewer.set_image(surface, f"grid ({len(item['keys'])} images)", status, info)
        else:
            self._viewer.set_image(surface, item.key, status, info)
        self._dirty = True

    def _item_status(self, item: ManifestRow | dict) -> str:
        if self.mode == "grid":
            return _grid_status(self._statuses, item["keys"])
        return self._statuses[item.key]

    def _continue_autoplay(self, direction: int, autoplay: bool = False):
        if direction == 1 and (autoplay or self.autoplay):
            self.autoplay = True
            pg.time.set_timer(AUTOPLAY_EVENT, 500, 1)
        elif direction == -1:
            self.autoplay = False

    def _navigate(self, direction: int, *, autoplay: bool = False):
        if not self._items:
            return
        n = len(self._items)

        if self._todo_only:
            if self.next_todo(direction, wrap=False):
                if self._ui_state == UIState.REVIEWING:
                    self._continue_autoplay(direction, autoplay)
            else:
                self._stop_autoplay()
                self._ui_state = UIState.END_MESSAGE
                self._viewer.show_message("No todo images remaining")
            return

        at_boundary = (self._cursor == n - 1) if direction == 1 else (self._cursor == 0)
        if at_boundary:
            self._stop_autoplay()
            self._ui_state = UIState.END_MESSAGE
            self._viewer.show_message("End of list")
            return

        self._cursor = (self._cursor + direction) % n
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
        if self.mode == "grid":
            keys = item["keys"]
            if status == "CLEAN" and _grid_clean_refused(self._statuses, keys):
                print(f"WARNING: {GRID_HAS_DIRTY}", file=sys.stderr)
                self._viewer.set_info(GRID_HAS_DIRTY)
                self._dirty = True
                return
        else:
            keys = [item.key]
        try:
            changed = self.store.mark(keys, status, self.pass_number, reviewer=self.reviewer, mode=self.mode)
        except StoreUnavailable as exc:
            self._store_lost(exc)
            return
        self._statuses.update(changed)
        self._todo_count = self._count_todo()
        self._viewer.set_status(status)
        self._dirty = True
        pg.time.set_timer(ADVANCE_EVENT, 200, 1)
        self._advance_pending = True

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
                    self._viewer.show_message("No todo images remaining")
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
            print(self._held_back_message() or f"No images to review for pass {self.pass_number}{filter_msg}.")
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
