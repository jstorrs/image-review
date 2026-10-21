import os
import unittest
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame as pg

from image_review.controller import GRID_HAS_DIRTY
from image_review.viewer import ImageViewer, fit_center_text


def fake_measure(text: str, size: int) -> int:
    return len(text) * size


class TestFitCenterText(unittest.TestCase):
    sizes = (36, 30, 24, 18)

    def test_fits_at_the_largest_size(self):
        self.assertEqual(fit_center_text("abcd", 144, fake_measure, self.sizes), ("abcd", 36))

    def test_shrinks_to_the_largest_size_that_fits(self):
        self.assertEqual(fit_center_text("abcd", 100, fake_measure, self.sizes), ("abcd", 24))

    def test_truncates_at_the_floor_when_still_too_wide(self):
        # At 18 px, 5 characters fit: 4 of the text plus the ellipsis
        self.assertEqual(fit_center_text("abcdefghij", 90, fake_measure, self.sizes), ("abcd…", 18))

    def test_no_room_draws_nothing(self):
        self.assertIsNone(fit_center_text("abcd", 0, fake_measure, self.sizes))
        self.assertIsNone(fit_center_text("abcd", -5, fake_measure, self.sizes))
        self.assertIsNone(fit_center_text("abcd", 10, fake_measure, self.sizes))  # not even "…" fits
        self.assertIsNone(fit_center_text("", 500, fake_measure, self.sizes))


class TestCenterTextInBar(unittest.TestCase):
    def test_long_message_stays_clear_of_status_and_scale(self):
        pg.init()
        self.addCleanup(pg.quit)
        patcher = mock.patch.object(pg.display, "toggle_fullscreen", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        viewer = ImageViewer()
        pg.display.set_mode((1280, 720))
        viewer.set_image(pg.Surface((3000, 2000)), "name.png", "UNREVIEWED", GRID_HAS_DIRTY)
        viewer.refresh()
        rects = viewer.bar_rects
        self.assertIn("scale", rects)
        center = rects["center"]
        self.assertGreaterEqual(center.left, rects["left"].right)
        self.assertLessEqual(center.right, rects["name"].left)
        for role in ("status", "left", "name", "scale"):
            self.assertFalse(center.colliderect(rects[role]), role)


if __name__ == "__main__":
    unittest.main()
