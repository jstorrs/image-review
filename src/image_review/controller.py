import csv
import random
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


def _grid_status(db: ReviewDB, image_ids: list[str]) -> str:
    statuses = {db.get_status(iid) for iid in image_ids}
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

        if mode == "grid":
            self._init_grid_mode()
        else:
            self._init_single_mode()

    def _init_single_mode(self):
        rows = self.db.images_for_review(self.manifest, self.pass_number, self.batch)
        random.shuffle(rows)
        self._items = rows
        self._viewer = ImageViewer()

    def _init_grid_mode(self):
        self._viewer = ImageViewer()
        grid_w, grid_h = self._viewer.screen.get_size()
        grid_h -= self._viewer.border

        review_rows = self.db.images_for_review(self.manifest, self.pass_number, self.batch)
        grid_specs = pack_into_grids(review_rows, self.work_dir, grid_w, grid_h)

        items = [
            {"surface": gs.surface, "image_ids": gs.image_ids, "batch": gs.batch}
            for gs in grid_specs
        ]
        random.shuffle(items)
        self._items = items

    def _show_current(self):
        if not self._items:
            return
        item = self._items[self._cursor]
        info = f"{self._cursor + 1} / {len(self._items)}"

        if self.mode == "grid":
            surface = item["surface"]
            status = _grid_status(self.db, item["image_ids"])
            self._viewer.set_image(surface, f"grid ({len(item['image_ids'])} images)", status, info)
        else:
            path = self.work_dir / item["preprocessed_path"]
            surface = _load_surface(str(path))
            status = self.db.get_status(item["image_id"])
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

        print(f"Starting {self.mode} review, pass {self.pass_number}, {len(self._items)} items")
        self.next_image()

        joysticks = {}
        running = True
        while running:
            for event in pg.event.get():
                match event.type:
                    case pg.JOYBUTTONDOWN:
                        match event.button:
                            case 1:
                                self.mark_clean()
                            case 3:
                                self.mark_dirty()
                            case 7:
                                running = False
                    case pg.JOYHATMOTION:
                        if event.hat == 0:
                            if event.value[0] < -0.5:
                                self.prev_image()
                            elif event.value[0] > 0.5:
                                self.next_image()
                    case pg.KEYDOWN:
                        self.autoplay = False
                        match event.key:
                            case pg.K_ESCAPE | pg.K_q:
                                running = False
                            case pg.K_c:
                                self.mark_clean()
                            case pg.K_d:
                                self.mark_dirty()
                            case pg.K_w:
                                pg.display.toggle_fullscreen()
                                self._dirty = True
                            case pg.K_SPACE:
                                self.next_image(autoplay=True)
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

            if self._dirty:
                self._viewer.refresh()
                self._dirty = False

        self._viewer.cleanup()
