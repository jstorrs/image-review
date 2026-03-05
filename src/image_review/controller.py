import csv
import random
from collections import defaultdict
from pathlib import Path

import pygame as pg
import skimage as ski

from .grid_packer import pack_into_grids
from .review_db import ReviewDB
from .viewer import ImageViewer

AUTOPLAY_EVENT = pg.USEREVENT + 1
ADVANCE_EVENT = pg.USEREVENT + 2


def _load_surface(path: str) -> pg.Surface:
    img = ski.io.imread(path)
    return pg.surfarray.make_surface(img.transpose(1, 0, 2))


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
        self._image_batch = {row["image_id"]: row["batch"] for row in self.manifest}

        if pass_number is None:
            self.pass_number = self.db.current_pass(self.manifest)
        else:
            self.pass_number = pass_number

        self.autoplay = False
        self._cursor = -1
        self._dirty = True
        self._showing_help = False
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

        splash_lines = [
            f"batch: {self.batch}",
            f"pass: {self.pass_number}",
            f"mode: {self.mode}",
        ]
        self._viewer.show_splash(splash_lines, footer="Computing grids...")

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

        splash_lines.append(f"{len(self._items)} items")
        self._viewer.show_splash(splash_lines)
        self._showing_splash = True

    def _restart_in_mode(self, new_mode: str):
        self.mode = new_mode
        self._cursor = -1
        self.autoplay = False
        self._dirty = True

        if new_mode == "grid":
            self._init_grid_mode()
        else:
            self._init_single_mode()

        if not self._items:
            self._viewer.show_message(f"No items for {new_mode} mode")
            return

        splash_lines = [
            f"batch: {self.batch}",
            f"pass: {self.pass_number}",
            f"mode: {self.mode}",
            f"{len(self._items)} items",
        ]
        self._viewer.show_splash(splash_lines)
        self._showing_splash = True

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
            path = self.work_dir / item["preprocessed_path"]
            surface = _load_surface(str(path))
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

    def run(self):
        if not self._items:
            print(f"No images to review for pass {self.pass_number}.")
            return

        batch_info = f", batch {self.batch}" if self.batch else ""
        print(f"Starting {self.mode} review, pass {self.pass_number}{batch_info}, {len(self._items)} items")

        if not self._showing_splash:
            splash_lines = [
                f"batch: {self.batch}",
                f"pass: {self.pass_number}",
                f"mode: {self.mode}",
                f"{len(self._items)} items",
            ]
            self._viewer.show_splash(splash_lines)
            self._showing_splash = True

        joysticks = {}
        running = True
        while running:
            for event in pg.event.get():
                match event.type:
                    case pg.JOYBUTTONDOWN:
                        if self._showing_splash:
                            continue
                        if self._showing_help:
                            self._showing_help = False
                            self._show_current()
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
                        if self._showing_help:
                            self._showing_help = False
                            self._show_current()
                            continue
                        if event.hat == 0:
                            if event.value[0] < -0.5:
                                self.prev_image()
                            elif event.value[0] > 0.5:
                                self.next_image()
                    case pg.KEYDOWN:
                        if self._showing_splash:
                            if event.key in (pg.K_ESCAPE, pg.K_q):
                                running = False
                            elif event.key == pg.K_SPACE:
                                self._showing_splash = False
                                self.next_image()
                            continue
                        if self._showing_help:
                            self._showing_help = False
                            self._show_current()
                            continue
                        match event.key:
                            case pg.K_ESCAPE | pg.K_q:
                                running = False
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
                                self._showing_help = True
                                self._viewer.show_help()
                            case pg.K_LEFT:
                                self.prev_image()
                            case pg.K_RIGHT:
                                self.next_image()
                    case pg.WINDOWRESIZED:
                        self._viewer.resize()
                        self._dirty = True
                    case x if x == AUTOPLAY_EVENT:
                        if self.autoplay:
                            self.next_image()
                    case x if x == ADVANCE_EVENT:
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

        self._viewer.cleanup()
