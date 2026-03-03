#!/usr/bin/env python

import argparse
import io
import logging
import random
import sys
from datetime import UTC, datetime
from enum import Enum, Flag, auto
from pathlib import Path
from zipfile import ZipFile

import matplotlib.pyplot as plt
import numpy as np
import pydicom
import pygame as pg
import pygame.freetype
import skimage as ski
from tqdm import tqdm

logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.DEBUG)
stdout_handler.setFormatter(formatter)

file_handler = logging.FileHandler("ratings.log")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)


logger.addHandler(file_handler)
logger.addHandler(stdout_handler)

TZ = datetime.now(UTC).astimezone().tzinfo
pg.init()


class ReviewState(Enum):
    TODO = pg.Color(128, 128, 128)
    FAIL = pg.Color(255, 128, 128)
    PASS = pg.Color(128, 255, 128)


class ReviewFlags(Flag):
    NONE = 0
    FOLLOWUP = auto()


class Image:
    def __init__(
        self,
        name: str,
        image: np.ndarray,
        review: ReviewState = ReviewState.TODO,
        flags: ReviewFlags = ReviewFlags.NONE,
        info: str = "",
        prefix: str = "",
    ):
        self.name = f"{prefix}{name}"
        self.review = review
        self.flags = flags
        self.info = info
        self._surf = pg.surfarray.make_surface(np.transpose(image, (1, 0, 2)))

    def __repr__(self) -> str:
        return f"<Image name={self.name} review={self.review.name} flags={self.flags.name}>"

    def scale_to_fit(self, X: int, Y: int) -> pg.Surface:
        XY = np.array((X, Y))
        xy = np.array(self._surf.get_size())
        size = (min(XY / xy) * xy).round()
        return pg.transform.smoothscale(self._surf, size)

    @classmethod
    def from_dicom(cls, path, dcm=None, colormap="cividis", prefix=""):
        dcm = dcm or pydicom.dcmread(path)
        img = ski.util.img_as_float32(dcm.pixel_array)
        img = ski.exposure.equalize_adapthist(img, int(max(img.shape) / 8))
        match dcm.PhotometricInterpretation:
            case "MONOCHROME1":
                img = ski.util.invert(img)
            case "MONOCHROME2":
                pass
        cm = plt.get_cmap(colormap)
        rgb = ski.util.img_as_ubyte(cm(img))
        return cls(
            name=path.name,
            image=rgb[:, :, :3],
            prefix=prefix,
        )

    @classmethod
    def from_file(cls, path):
        return cls(
            name=path.name,
            image=ski.io.imread(path),
        )

    @classmethod
    def from_zip(cls, path):
        with ZipFile(path) as zf:
            files = [
                item.filename
                for item in zf.infolist()
                if item.filename.endswith(".dcm")
            ]
            for dcm in tqdm(files, desc=path.name, position=1, leave=False):
                with zf.open(dcm, "r") as fp:
                    yield cls.from_dicom(
                        Path(dcm),
                        pydicom.dcmread(io.BytesIO(fp.read())),
                        prefix=f"[{path.name}] ",
                    )

    @classmethod
    def iter_from(cls, *paths):
        for p in paths:
            path = Path(p)
            match path.suffix:
                case ".zip":
                    yield from cls.from_zip(path)
                case ".dcm":
                    yield cls.from_dicom(path)
                case _:
                    yield cls.from_file(path)


class TextPosition(Enum):
    TOP = auto()
    BOTTOM = auto()
    LEFT = auto()
    CENTER = auto()
    RIGHT = auto()


class Viewer:
    border: int = 50
    radius: int = 20

    font = pg.freetype.SysFont("ubuntu", 36)
    font.fgcolor = pg.Color(64, 64, 64)
    font.strong = True

    def __init__(self, image: Image):
        self.screen = pg.display.set_mode((0, 0), pg.FULLSCREEN | pg.RESIZABLE)
        pygame.mouse.set_visible(False)
        self.image = image

    @property
    def image(self) -> Image:
        return self._image

    @image.setter
    def image(self, image: Image) -> None:
        self._image = image
        pg.display.set_caption(f"{self.image.name}")

    def resize(self, X: int = 0, Y: int = 0) -> None:
        if X == 0 or Y == 0:
            X, Y = self.screen.get_size()
        Y = Y - self.border
        self.content = self.image.scale_to_fit(X, Y)
        x, y = self.content.get_size()
        self.offset = int((X - x) / 2), int((Y - y) / 2)
        self._last = 0

    def _text_left(self, text):
        bbox = self.font.get_rect(text)
        X, Y = self.screen.get_size()
        self.font.render_to(
            self.screen,
            (int(bbox.height / 2), int(Y - (self.border + bbox.height) / 2)),
            text,
        )

    def _text_right(self, text):
        bbox = self.font.get_rect(text)
        X, Y = self.screen.get_size()
        self.font.render_to(
            self.screen,
            (
                X - int(bbox.height / 2) - bbox.width,
                int(Y - (self.border + bbox.height) / 2),
            ),
            text,
        )

    def _text_center(self, text):
        bbox = self.font.get_rect(text)
        X, Y = self.screen.get_size()
        self.font.render_to(
            self.screen,
            (
                int((X - bbox.width) / 2),
                int(Y - (self.border + bbox.height) / 2),
            ),
            text,
        )

    def refresh(self) -> None:
        X, Y = self.screen.get_size()
        self.screen.fill(pg.Color(64, 64, 64))
        pg.draw.rect(
            self.screen,
            self.image.review.value,
            pg.Rect(0, Y - self.border, X, self.border),
        )
        self._text_right(self.image.name)
        self._text_center(self.image.info)
        if self.image.flags != ReviewFlags.NONE:
            self._text_left(self.image.flags.name)
        if hasattr(self, "content"):
            self.screen.blit(self.content, self.offset)
        pg.display.flip()


class ImageDB(list):
    _cursor = -1

    def load_images(self, *paths):
        for image in Image.iter_from(*paths):
            self.append(image)

    def step(self, delta):
        self._cursor = (self._cursor + delta) % len(self)
        image = self[self._cursor]
        image.info = f"{self._cursor + 1} / {len(self)}"
        return image

    def next_image(self):
        return self.step(1)

    def prev_image(self):
        return self.step(-1)


class Controller:
    def __init__(self, db):
        self.db = db
        self.view = Viewer(self.db.next_image())
        self.autoplay = False

    @property
    def image(self) -> Image:
        return self.view.image

    @image.setter
    def image(self, image) -> None:
        self.view.image = image

    def resize(self):
        self.view.resize()

    def refresh(self):
        self.view.refresh()

    def next_image(self, *, autoplay=False):
        self.image = self.db.next_image()
        self.resize()
        if autoplay or self.autoplay:
            self.autoplay = True
            pg.time.set_timer(pg.USEREVENT, 500, 1)

    def prev_image(self):
        self.image = self.db.prev_image()
        self.resize()
        self.autoplay = False

    @property
    def review(self):
        return self.image.review

    @review.setter
    def review(self, review):
        self.image.review = review
        pg.time.set_timer(pg.USEREVENT, 200, 1)

    def mark_pass(self):
        self.review = ReviewState.PASS

    def mark_fail(self):
        self.review = ReviewState.FAIL

    def mark_todo(self):
        self.image.flags |= ReviewFlags.FOLLOWUP
        self.review = ReviewState.TODO

    def _toggle_flag(self, flag):
        if flag in self.image.flags:
            self.image.flags &= ~flag
        else:
            self.image.flags |= flag

    def toggle_followup(self):
        self._toggle_flag(ReviewFlags.FOLLOWUP)

    def set_followup(self):
        self.image.flags |= ReviewFlags.FOLLOWUP
        self.report()

    def unset_followup(self):
        self.image.flags &= ~ReviewFlags.FOLLOWUP

    def userevent(self):
        if not self.autoplay:
            self.report()
        self.next_image()

    def report(self):
        logger.info(
            "\t".join((self.image.name, self.image.review.name, self.image.flags.name)),
        )


def main(images):
    controller = Controller(images)
    joysticks = {}
    running = True
    while running:
        for event in pg.event.get():
            match event.type:
                case pg.JOYBUTTONDOWN:
                    match event.button:
                        case 0:
                            controller.mark_todo()
                        case 1:
                            controller.mark_pass()
                        case 3:
                            controller.mark_fail()
                        case 7:
                            running = False
                case pg.JOYAXISMOTION:
                    print(f"Axis {event.axis} = {event.value}")
                case pg.JOYHATMOTION:
                    match event.hat:
                        case 0:
                            if event.value[0] < -0.5:
                                controller.prev_image()
                            elif event.value[0] > 0.5:
                                controller.next_image()
                            elif event.value[1] < -0.5:
                                controller.unset_followup()
                            elif event.value[1] > 0.5:
                                controller.set_followup()
                case pg.KEYDOWN:
                    controller.autoplay = False
                    match event.key:
                        case pg.K_ESCAPE | pg.K_q:
                            running = False
                        case pg.K_p:
                            controller.mark_fail()
                        case pg.K_c:
                            controller.mark_pass()
                        case pg.K_f:
                            controller.toggle_followup()
                        case pg.K_d:
                            controller.mark_todo()
                        case pg.K_w:
                            pg.display.toggle_fullscreen()
                        case pg.K_SPACE:
                            controller.next_image(autoplay=True)
                        case pg.K_LEFT:
                            controller.prev_image()
                        case pg.K_RIGHT:
                            controller.next_image()
                case pg.WINDOWRESIZED:
                    controller.resize()
                case pg.USEREVENT:
                    controller.userevent()
                case pg.JOYDEVICEADDED:
                    joy = pg.joystick.Joystick(event.device_index)
                    joysticks[joy.get_instance_id()] = joy
                    print(f"Joystick {joy.get_instance_id()} connencted")
                case pg.JOYDEVICEREMOVED:
                    del joysticks[event.instance_id]
                    print(f"Joystick {event.instance_id} disconnected")
                case pg.QUIT:
                    running = False
            controller.refresh()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("images", nargs="*")
    args = parser.parse_args()

    images = ImageDB()
    for source in tqdm(args.images, position=0):
        images.load_images(source)

    random.shuffle(images)
    images.load_images("stop.jpg")
    main(images)
    pg.quit()
