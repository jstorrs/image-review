import csv
import random
import sys
from enum import Enum, auto
from pathlib import Path

import pygame as pg

from .grid_packer import pack_into_grids
from .review_db import ReviewDB
from .util import load_surface, safe_path
from .viewer import ImageViewer

AUTOPLAY_EVENT = pg.USEREVENT + 1
ADVANCE_EVENT = pg.USEREVENT + 2


def load_manifest(work_dir: Path) -> list[dict]:
    manifest_path = work_dir / "manifest.tsv"
    with open(manifest_path, newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _grid_status(db: ReviewDB, image_ids: list[str], pass_number: int) -> str:
    statuses = {db.get_status(iid, pass_number) for iid in image_ids}
    if statuses == {"CLEAN"}:
        return "CLEAN"
    if "UNREVIEWED" in statuses:
        return "UNREVIEWED"
    return "DIRTY"


class UIState(Enum):
    SPLASH = auto()
    REVIEWING = auto()
    END_MESSAGE = auto()


class ReviewSession:
    def __init__(
        self,
        work_dir: Path,
        mode: str = "single",
        pass_number: int | None = None,
        batch: str | None = None,
        status_filter: str = "unreviewed",
    ):
        self.work_dir = work_dir
        self.mode = mode
        self.batch = batch
        self.status_filter = status_filter
        self.db = ReviewDB(work_dir)
        self.manifest = load_manifest(work_dir)

        if pass_number is None:
            self.pass_number = self.db.current_pass(self.manifest)
        else:
            self.pass_number = pass_number

        self.autoplay = False
        self._cursor = -1
        self._dirty = True
        self._ui_state = UIState.REVIEWING
        self._todo_only = False

        self._viewer = ImageViewer()
        self._joysticks = {}

        if self.batch is None:
            self.batch = self._auto_select_batch()

        if mode == "grid":
            self._init_grid_mode()
        else:
            self._init_single_mode()

    def _stop_autoplay(self):
        self.autoplay = False
        pg.time.set_timer(AUTOPLAY_EVENT, 0)

    def _toggle_mode(self):
        new_mode = "grid" if self.mode == "single" else "single"
        self._restart_in_mode(new_mode)

    def _auto_select_batch(self) -> str | None:
        """Find the first batch that has images matching the status filter."""
        batches = sorted({row["batch"] for row in self.manifest})
        for batch in batches:
            if self.db.images_by_status(self.manifest, self.pass_number, self.status_filter, batch):
                return batch
        return None

    def _init_single_mode(self):
        rows = self.db.images_by_status(self.manifest, self.pass_number, self.status_filter, self.batch)
        random.shuffle(rows)
        self._items = rows
        self._todo_count = self._count_todo()

    def _init_grid_mode(self):
        self._viewer.show_splash([self._info_line()], footer="Computing grids...", mode=self.mode)

        grid_w, grid_h = self._viewer.screen.get_size()
        grid_h -= self._viewer.border

        review_rows = self.db.images_by_status(self.manifest, self.pass_number, self.status_filter, self.batch)
        grid_specs = pack_into_grids(review_rows, self.work_dir, grid_w, grid_h)

        items = [
            {"surface": gs.surface, "image_ids": gs.image_ids, "batch": gs.batch}
            for gs in grid_specs
        ]
        random.shuffle(items)
        items.sort(key=lambda item: len(item["image_ids"]), reverse=True)
        self._items = items
        self._todo_count = self._count_todo()

        self._show_splash()

    def _show_splash(self):
        other = "grid" if self.mode == "single" else "single"
        self._viewer.show_splash(
            [self._info_line(len(self._items))],
            footer=[
                f"Press [space] for {self.mode} image review",
                f"Press [m] for {other} image review",
            ],
            mode=self.mode,
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

    def _restart_in_mode(self, new_mode: str):
        pg.time.set_timer(ADVANCE_EVENT, 0)
        self._stop_autoplay()
        self.mode = new_mode
        self._cursor = -1
        self._dirty = True

        if new_mode == "grid":
            self._init_grid_mode()
        else:
            self._init_single_mode()

        if not self._items:
            self._viewer.show_message(f"No items for {new_mode} mode")
            self._ui_state = UIState.END_MESSAGE
            return

        self._ui_state = UIState.REVIEWING
        self.next_image()

    def _is_todo(self, status: str) -> bool:
        if self.status_filter == "clean":
            return status == "CLEAN"
        return status == "UNREVIEWED"

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
        if not self._items:
            return
        item = self._items[self._cursor]

        if self.mode != "grid":
            # Iteratively skip unloadable images to avoid recursion
            start = self._cursor
            while True:
                path = safe_path(self.work_dir, item["preprocessed_path"])
                try:
                    surface = load_surface(str(path))
                    break
                except Exception as exc:
                    print(f"WARNING: cannot load {path}: {exc}", file=sys.stderr)
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
            self._viewer.set_image(surface, f"grid ({len(item['image_ids'])} images)", status, info)
        else:
            self._viewer.set_image(surface, item["preprocessed_path"], status, info)
        self._dirty = True

    def _item_status(self, item) -> str:
        if self.mode == "grid":
            return _grid_status(self.db, item["image_ids"], self.pass_number)
        return self.db.get_status(item["image_id"], self.pass_number)

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
        self._continue_autoplay(direction, autoplay)

    def next_image(self, *, autoplay=False):
        self._navigate(1, autoplay=autoplay)

    def prev_image(self):
        self._navigate(-1)

    def _mark(self, status: str):
        if not self._items or self._cursor < 0:
            return
        item = self._items[self._cursor]
        if self.mode == "grid":
            self.db.mark_many(item["image_ids"], item["batch"], status, self.pass_number)
        else:
            self.db.mark(item["image_id"], item["batch"], status, self.pass_number)
        self._todo_count = self._count_todo()
        self._viewer.set_status(status)
        self._dirty = True
        pg.time.set_timer(ADVANCE_EVENT, 200, 1)

    def _handle_splash_key(self, key) -> bool:
        """Handle key press while splash is shown. Returns True to quit."""
        if key in (pg.K_ESCAPE, pg.K_q):
            return True
        if key in (pg.K_SPACE, pg.K_h):
            self._ui_state = UIState.REVIEWING
            if self._cursor == -1:
                self.next_image()
            else:
                self._show_current()
        elif key == pg.K_m:
            self._toggle_mode()
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
        if direction is not None:
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
            self._toggle_mode()
        return False

    def _handle_review_key(self, key) -> bool:
        """Handle key press during review. Returns True to quit."""
        match key:
            case pg.K_ESCAPE | pg.K_q:
                return True
            case pg.K_c:
                self._stop_autoplay()
                self._mark("CLEAN")
            case pg.K_d:
                self._stop_autoplay()
                self._mark("DIRTY")
            case pg.K_w:
                num_displays = len(pg.display.get_desktop_sizes())
                if num_displays > 1:
                    next_idx = (self._viewer._display_index + 1) % num_displays
                    if self._viewer.switch_display(next_idx):
                        if self.mode == "grid":
                            self._restart_in_mode("grid")
                        else:
                            self._dirty = True
            case pg.K_SPACE:
                if self.autoplay:
                    self._stop_autoplay()
                else:
                    self.next_image(autoplay=True)
            case pg.K_m:
                self._toggle_mode()
            case pg.K_n:
                self._stop_autoplay()
                pg.time.set_timer(ADVANCE_EVENT, 0)
                self.next_todo()
            case pg.K_u:
                self._todo_only = not self._todo_only
                self._viewer.set_todo_only(self._todo_only)
                self._dirty = True
            case pg.K_h:
                self._show_splash()
            case pg.K_LEFT:
                pg.time.set_timer(ADVANCE_EVENT, 0)
                self.prev_image()
            case pg.K_RIGHT:
                pg.time.set_timer(ADVANCE_EVENT, 0)
                self.next_image()
        return False

    def run(self):
        if not self._items:
            filter_msg = f" (filter: {self.status_filter})" if self.status_filter != "unreviewed" else ""
            print(f"No images to review for pass {self.pass_number}{filter_msg}.")
            return

        batch_info = f", batch {self.batch}" if self.batch else ""
        filter_info = f", filter {self.status_filter}" if self.status_filter != "unreviewed" else ""
        print(f"Starting {self.mode} review, pass {self.pass_number}{batch_info}{filter_info}, {len(self._items)} items")

        if self._ui_state != UIState.SPLASH:
            self._show_splash()

        clock = pg.time.Clock()
        running = True
        while running:
            for event in pg.event.get():
                match event.type:
                    case pg.JOYBUTTONDOWN:
                        if self._ui_state != UIState.REVIEWING:
                            continue
                        match event.button:
                            case 1:
                                self._stop_autoplay()
                                self._mark("CLEAN")
                            case 3:
                                self._stop_autoplay()
                                self._mark("DIRTY")
                            case 7:
                                running = False
                    case pg.JOYHATMOTION:
                        if self._ui_state != UIState.REVIEWING:
                            continue
                        if event.hat == 0:
                            pg.time.set_timer(ADVANCE_EVENT, 0)
                            if event.value[0] < 0:
                                self.prev_image()
                            elif event.value[0] > 0:
                                self.next_image()
                    case pg.KEYDOWN:
                        if self._ui_state == UIState.END_MESSAGE:
                            if self._handle_end_key(event.key):
                                running = False
                            continue
                        if self._ui_state == UIState.SPLASH:
                            if self._handle_splash_key(event.key):
                                running = False
                            continue
                        if self._handle_review_key(event.key):
                            running = False
                    case pg.WINDOWRESIZED:
                        self._viewer.resize()
                        self._dirty = True
                    case x if x == AUTOPLAY_EVENT:
                        if self.autoplay and self._ui_state != UIState.END_MESSAGE:
                            self.next_image()
                    case x if x == ADVANCE_EVENT:
                        if self._ui_state == UIState.REVIEWING:
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
                        running = False

            if self._dirty and self._ui_state != UIState.SPLASH:
                self._viewer.refresh()
                self._dirty = False

            clock.tick(60)
