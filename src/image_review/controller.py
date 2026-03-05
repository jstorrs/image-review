import csv
import random
import sys
from collections import defaultdict
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


class ReviewSession:
    def __init__(
        self,
        work_dir: Path,
        mode: str = "single",
        pass_number: int | None = None,
        batch: str | None = None,
    ):
        self.work_dir = work_dir
        self.mode = mode
        self.batch = batch
        self.db = ReviewDB(work_dir)
        self.manifest = load_manifest(work_dir)

        if pass_number is None:
            self.pass_number = self.db.current_pass(self.manifest)
        else:
            self.pass_number = pass_number

        self.autoplay = False
        self._cursor = -1
        self._dirty = True
        self._showing_splash = False

        self._viewer = None

        if self.batch is None:
            self.batch = self._auto_select_batch()

        if mode == "grid":
            self._init_grid_mode()
        else:
            self._init_single_mode()

    def _auto_select_batch(self) -> str | None:
        """Find the first batch that still has images needing review."""
        batches = sorted({row["batch"] for row in self.manifest})
        for batch in batches:
            if self.db.images_for_review(self.manifest, self.pass_number, batch):
                return batch
        return None

    def _init_single_mode(self):
        rows = self.db.images_for_review(self.manifest, self.pass_number, self.batch)
        random.shuffle(rows)
        self._items = rows
        if self._viewer is None:
            self._viewer = ImageViewer()

    def _init_grid_mode(self):
        if self._viewer is None:
            self._viewer = ImageViewer()

        self._viewer.show_splash([self._info_line()], footer="Computing grids...", mode=self.mode)

        grid_w, grid_h = self._viewer.screen.get_size()
        grid_h -= self._viewer.border

        review_rows = self.db.images_for_review(self.manifest, self.pass_number, self.batch)
        grid_specs = pack_into_grids(review_rows, self.work_dir, grid_w, grid_h)

        items = [
            {"surface": gs.surface, "image_ids": gs.image_ids, "batch": gs.batch}
            for gs in grid_specs
        ]
        buckets = defaultdict(list)
        for item in items:
            n = len(item["image_ids"])
            buckets[0 if n == 1 else (n // 4) + 1].append(item)
        sorted_items = []
        for key in sorted(buckets, reverse=True):
            random.shuffle(buckets[key])
            sorted_items.extend(buckets[key])
        self._items = sorted_items

        self._show_splash()

    def _show_splash(self):
        self._viewer.show_splash(
            [self._info_line(len(self._items))],
            footer=self._splash_footer(),
            mode=self.mode,
        )
        self._showing_splash = True

    def _splash_footer(self) -> list[str]:
        other = "grid" if self.mode == "single" else "single"
        return [
            f"Press [space] for {self.mode} image review",
            f"Press [m] for {other} image review",
        ]

    def _info_line(self, n_items: int | None = None) -> str:
        parts = [f"{self.batch} pass {self.pass_number}"]
        if n_items is not None:
            parts.append(f"{n_items} images")
        parts.append(f"{self.mode} image review")
        return " - ".join(parts)

    def _restart_in_mode(self, new_mode: str):
        pg.time.set_timer(ADVANCE_EVENT, 0)
        pg.time.set_timer(AUTOPLAY_EVENT, 0)
        self.mode = new_mode
        self._cursor = -1
        self.autoplay = False
        self._dirty = True
        self._showing_splash = False

        if new_mode == "grid":
            self._init_grid_mode()
        else:
            self._init_single_mode()

        if not self._items:
            self._viewer.show_message(f"No items for {new_mode} mode")
            return

        if not self._showing_splash:
            self._show_splash()

    def _show_current(self):
        if not self._items:
            return
        item = self._items[self._cursor]
        info = f"{self._cursor + 1} / {len(self._items)}"

        if self.mode == "grid":
            surface = item["surface"]
            status = _grid_status(self.db, item["image_ids"], self.pass_number)
            self._viewer.set_image(surface, f"grid ({len(item['image_ids'])} images)", status, info)
        else:
            path = safe_path(self.work_dir, item["preprocessed_path"])
            try:
                surface = load_surface(str(path))
            except Exception as exc:
                print(f"WARNING: cannot load {path}: {exc}", file=sys.stderr)
                self.next_image()
                return
            status = self.db.get_status(item["image_id"], self.pass_number)
            self._viewer.set_image(surface, item["preprocessed_path"], status, info)
        self._dirty = True

    def next_image(self, *, autoplay=False):
        if not self._items:
            return
        self._cursor = (self._cursor + 1) % len(self._items)
        self._show_current()
        if autoplay or self.autoplay:
            self.autoplay = True
            pg.time.set_timer(AUTOPLAY_EVENT, 500, 1)

    def prev_image(self):
        if not self._items:
            return
        self._cursor = (self._cursor - 1) % len(self._items)
        self._show_current()
        self.autoplay = False

    def _mark(self, status: str):
        if not self._items:
            return
        item = self._items[self._cursor]
        if self.mode == "grid":
            self.db.mark_many(item["image_ids"], item["batch"], status, self.pass_number)
        else:
            self.db.mark(item["image_id"], item["batch"], status, self.pass_number)
        self._viewer.set_status(status)
        self._dirty = True
        pg.time.set_timer(ADVANCE_EVENT, 200, 1)

    def mark_clean(self):
        self._mark("CLEAN")

    def mark_dirty(self):
        self._mark("DIRTY")

    def _handle_splash_key(self, key) -> bool:
        """Handle key press while splash is shown. Returns True to quit."""
        if key in (pg.K_ESCAPE, pg.K_q):
            return True
        if key in (pg.K_SPACE, pg.K_h):
            self._showing_splash = False
            if self._cursor == -1:
                self.next_image()
            else:
                self._show_current()
        elif key == pg.K_m:
            new_mode = "grid" if self.mode == "single" else "single"
            self._restart_in_mode(new_mode)
        return False

    def _handle_review_key(self, key) -> bool:
        """Handle key press during review. Returns True to quit."""
        match key:
            case pg.K_ESCAPE | pg.K_q:
                return True
            case pg.K_c:
                self.autoplay = False
                self.mark_clean()
            case pg.K_d:
                self.autoplay = False
                self.mark_dirty()
            case pg.K_w:
                pg.display.toggle_fullscreen()
                self._dirty = True
            case pg.K_SPACE:
                if self.autoplay:
                    self.autoplay = False
                else:
                    self.next_image(autoplay=True)
            case pg.K_m:
                new_mode = "grid" if self.mode == "single" else "single"
                self._restart_in_mode(new_mode)
            case pg.K_h:
                self._show_splash()
            case pg.K_LEFT:
                self.prev_image()
            case pg.K_RIGHT:
                self.next_image()
        return False

    def run(self):
        if not self._items:
            print(f"No images to review for pass {self.pass_number}.")
            return

        batch_info = f", batch {self.batch}" if self.batch else ""
        print(f"Starting {self.mode} review, pass {self.pass_number}{batch_info}, {len(self._items)} items")

        if not self._showing_splash:
            self._show_splash()

        joysticks = {}
        running = True
        while running:
            for event in pg.event.get():
                match event.type:
                    case pg.JOYBUTTONDOWN:
                        if self._showing_splash:
                            continue
                        match event.button:
                            case 1:
                                self.mark_clean()
                            case 3:
                                self.mark_dirty()
                            case 7:
                                running = False
                    case pg.JOYHATMOTION:
                        if self._showing_splash:
                            continue
                        if event.hat == 0:
                            if event.value[0] < -0.5:
                                self.prev_image()
                            elif event.value[0] > 0.5:
                                self.next_image()
                    case pg.KEYDOWN:
                        if self._showing_splash:
                            if self._handle_splash_key(event.key):
                                running = False
                            continue
                        if self._handle_review_key(event.key):
                            running = False
                    case pg.WINDOWRESIZED:
                        self._viewer.resize()
                        self._dirty = True
                    case x if x == AUTOPLAY_EVENT:
                        if self.autoplay:
                            self.next_image()
                    case x if x == ADVANCE_EVENT:
                        if not self._showing_splash:
                            self.next_image()
                    case pg.JOYDEVICEADDED:
                        joy = pg.joystick.Joystick(event.device_index)
                        joysticks[joy.get_instance_id()] = joy
                    case pg.JOYDEVICEREMOVED:
                        del joysticks[event.instance_id]
                    case pg.QUIT:
                        running = False

            if self._dirty and not self._showing_splash:
                self._viewer.refresh()
                self._dirty = False

