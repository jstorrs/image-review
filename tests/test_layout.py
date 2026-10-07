"""The pure grid layout core: JPEG header sizes and grid plans. Must not import pygame."""

import io
import subprocess
import sys
import unittest

from PIL import Image

from image_review.layout import GridPlan, PlacedRect, fit_size, jpeg_size, plan_grids
from tests.fixtures import dropping_packer

GRID_W, GRID_H = 1920, 1030


def jpeg(size: tuple[int, int], mode: str = "RGB", **save_options) -> bytes:
    """A solid JPEG made with Pillow."""
    buf = io.BytesIO()
    Image.new(mode, size, 90 if mode == "L" else (90, 120, 150)).save(buf, "JPEG", **save_options)
    return buf.getvalue()


def pil_size(buf: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(buf)) as im:
        return im.size


def sof_offset(buf: bytes) -> int:
    """The offset of the baseline SOF0 marker in a Pillow JPEG."""
    return buf.index(b"\xff\xc0")


class PlanGridsTest(unittest.TestCase):
    def all_rects(self, plan: GridPlan) -> list[PlacedRect]:
        return [rect for rects in plan.bins for rect in rects]

    def test_deterministic(self):
        sizes = {i: (300 + 37 * i, 200 + 53 * (i % 7)) for i in range(40)}
        for rotation in ("auto", "always", "never"):
            with self.subTest(rotation=rotation):
                self.assertEqual(
                    plan_grids(sizes, GRID_W, GRID_H, rotation), plan_grids(sizes, GRID_W, GRID_H, rotation)
                )

    def test_every_rect_placed_once(self):
        sizes = {i: (300 + 37 * i, 200 + 53 * (i % 7)) for i in range(40)}
        plan = plan_grids(sizes, GRID_W, GRID_H, "auto")
        self.assertEqual(sorted(r.rect_id for r in self.all_rects(plan)), list(range(40)))
        self.assertEqual(plan.unpacked, frozenset())
        self.assertTrue(all(plan.bins))

    def test_never_does_not_rotate_where_always_does(self):
        sizes = {0: (1000, 1800), 1: (1000, 1800)}
        always = plan_grids(sizes, GRID_W, GRID_H, "always")
        never = plan_grids(sizes, GRID_W, GRID_H, "never")
        self.assertTrue(always.rotated)
        self.assertFalse(never.rotated)
        self.assertEqual({(r.w, r.h) for r in self.all_rects(always)}, {(1800, 1000)})
        self.assertEqual({(r.w, r.h) for r in self.all_rects(never)}, {(572, 1030)})

    def test_auto_keeps_rotation_that_saves_a_bin(self):
        sizes = dict.fromkeys(range(5), (400, 600))
        never = plan_grids(sizes, GRID_W, GRID_H, "never")
        auto = plan_grids(sizes, GRID_W, GRID_H, "auto")
        self.assertEqual(len(never.bins), 2)
        self.assertEqual(len(auto.bins), 1)
        self.assertTrue(auto.rotated)
        self.assertEqual(auto, plan_grids(sizes, GRID_W, GRID_H, "always"))

    def test_auto_does_not_rotate_when_it_saves_no_bin(self):
        sizes = dict.fromkeys(range(12), (512, 384))  # 6 per bin either way
        auto = plan_grids(sizes, GRID_W, GRID_H, "auto")
        self.assertFalse(auto.rotated)
        self.assertEqual(auto, plan_grids(sizes, GRID_W, GRID_H, "never"))
        self.assertEqual(len(auto.bins), len(plan_grids(sizes, GRID_W, GRID_H, "always").bins))

    def test_unpacked_rects_are_reported(self):
        sizes = {0: (3000, 2500), 1: (100, 60), 2: (100, 60)}
        with dropping_packer(1):
            plan = plan_grids(sizes, GRID_W, GRID_H, "auto")
        self.assertEqual(plan.unpacked, frozenset({1}))
        self.assertEqual(sorted(r.rect_id for r in self.all_rects(plan)), [0, 2])


class FitSizeTest(unittest.TestCase):
    def test_fit_size_rotated_orientation_wins(self):
        self.assertEqual(fit_size(1500, 2500, 1920, 1030, True), (1030, 1716))

    def test_fit_size_always_fits_an_allowed_orientation(self):
        for w, h in [
            (1921, 1),
            (1, 1031),
            (3000, 2500),
            (1920, 1031),
            (1921, 1030),
            (7, 4000),
            (4000, 7),
            (1031, 1921),
            (999, 1999),
        ]:
            for rot in (False, True):
                with self.subTest(w=w, h=h, rot=rot):
                    nw, nh = fit_size(w, h, 1920, 1030, rot)
                    self.assertTrue(nw >= 1 and nh >= 1)
                    self.assertTrue((nw <= 1920 and nh <= 1030) or (rot and nw <= 1030 and nh <= 1920))

    def test_oversize_image_keeps_aspect_ratio(self):
        self.assertEqual(fit_size(3000, 2500, 1920, 1030, False), (1236, 1030))
        self.assertEqual(fit_size(3000, 2500, 1920, 1030, True), (1236, 1030))
        self.assertEqual(fit_size(1000, 1800, 1920, 1030, True), (1000, 1800))  # fits only rotated: untouched
        self.assertEqual(fit_size(1000, 1800, 1920, 1030, False), (572, 1030))


class JpegSizeTest(unittest.TestCase):
    def test_matches_pillow(self):
        cases = {
            "baseline": jpeg((257, 131), quality=95),
            "progressive": jpeg((640, 480), progressive=True),
            "grayscale": jpeg((300, 200), "L"),
            "4:2:0": jpeg((333, 222), subsampling=2),
            "4:4:4": jpeg((333, 222), subsampling=0),
            "large EXIF and ICC": jpeg(
                (120, 80), exif=b"Exif\x00\x00" + bytes(60000), icc_profile=bytes(range(256)) * 800
            ),
            "restart blocks": jpeg((200, 100), restart_marker_blocks=1),
            "restart rows": jpeg((200, 100), restart_marker_rows=1),
            "1x1": jpeg((1, 1)),
            "4097x3": jpeg((4097, 3)),
            "3x4097": jpeg((3, 4097), "L"),
        }
        for name, buf in cases.items():
            with self.subTest(name):
                self.assertEqual(jpeg_size(buf), pil_size(buf))

    def test_truncation_rejected_exactly_where_pillow_rejects_it(self):
        cases = {
            "baseline": jpeg((40, 24)),
            "progressive with EXIF": jpeg((40, 24), progressive=True, exif=b"Exif\x00\x00" + bytes(300)),
        }
        for name, buf in cases.items():
            for cut in range(len(buf) + 1):
                prefix = buf[:cut]
                try:
                    expected: tuple[int, int] | None = pil_size(prefix)
                except Exception:  # noqa: BLE001 - whatever Pillow raises, jpeg_size must raise ValueError
                    expected = None
                with self.subTest(name, cut=cut):
                    if expected is None:
                        self.assertRaises(ValueError, jpeg_size, prefix)
                    else:
                        self.assertEqual(jpeg_size(prefix), expected)

    def test_skips_fill_bytes_and_standalone_markers(self):
        buf = jpeg((71, 43))
        # After SOI: TEM, RST0 and fill bytes before the next marker
        crafted = buf[:2] + b"\xff\x01\xff\xd0\xff\xff\xff" + buf[3:]
        self.assertEqual(jpeg_size(crafted), (71, 43))

    def test_rejects_unreadable_headers(self):
        buf = jpeg((64, 32))
        sof = sof_offset(buf)
        zero_height = bytearray(buf)
        zero_height[sof + 5 : sof + 7] = b"\x00\x00"
        zero_width = bytearray(buf)
        zero_width[sof + 7 : sof + 9] = b"\x00\x00"
        cases = {
            "empty": b"",
            "not a JPEG": b"\x89PNG\r\n\x1a\n" + bytes(32),
            "SOI only": buf[:2],
            "truncated header": buf[: sof + 6],
            "truncated mid-segment": buf[:10],
            "truncated segment length": b"\xff\xd8\xff\xe0\x00",
            "segment length < 2": b"\xff\xd8\xff\xe0\x00\x01" + bytes(16),
            "SOS before SOF": b"\xff\xd8\xff\xda\x00\x08" + bytes(16),
            "EOI before SOF": b"\xff\xd8\xff\xd9",
            "zero height": bytes(zero_height),
            "zero width": bytes(zero_width),
        }
        for name, bad in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                jpeg_size(bad)


class ImportTest(unittest.TestCase):
    def test_server_and_layout_import_without_heavy_dependencies(self):
        code = (
            "import sys, image_review.server, image_review.layout\n"
            "print(sorted(m for m in ('pygame', 'PIL', 'numpy', 'skimage') if m in sys.modules))"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
