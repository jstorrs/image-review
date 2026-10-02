import csv
import gc
import io
import os
import tempfile
import unittest
import weakref
from pathlib import Path
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame as pg
from PIL import Image

from image_review import grid_packer as grid_packer_module
from image_review.grid_packer import PlacedRect, fit_size, pack_into_grids
from image_review.store import LocalStore
from image_review.util import load_surface
from tests.fixtures import dropping_packer


class TestPackShrinksOversize(unittest.TestCase):
    def setUp(self):
        self.store = self.make_store({"big/a.jpg": (3000, 2500), "big/b.jpg": (100, 60), "big/c.jpg": (100, 60)})

    def make_store(
        self, sizes: dict[str, tuple[int, int]], colours: dict[str, tuple[int, int, int]] | None = None
    ) -> LocalStore:
        """A store over solid JPGs of the given sizes (grey unless `colours` names one), one batch per directory."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = self.root = Path(tmp.name)
        rows = []
        for name, size in sizes.items():
            (root / name).parent.mkdir(exist_ok=True)
            colour = (colours or {}).get(name, (90, 90, 90))
            Image.new("RGB", size, colour).save(root / name, "JPEG", quality=95, subsampling=0)
            rows.append((name.split("/")[0], name, f"/src/{name}"))
        with open(root / "manifest.tsv", "w", newline="") as f:
            writer = csv.writer(f, delimiter="\t")
            writer.writerow(["batch", "preprocessed_path", "image_id"])
            writer.writerows(rows)
        store = LocalStore(root)
        self.addCleanup(store.close)
        return store

    def quiet_stderr(self) -> io.StringIO:
        patcher = mock.patch("sys.stderr", io.StringIO())
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_nothing_larger_than_the_bin_is_kept(self):
        calls: list[tuple[int, int]] = []
        grids, unloadable = pack_into_grids(
            self.store.manifest(), self.store, 1920, 1030, on_progress=lambda i, n: calls.append((i, n))
        )
        self.assertEqual(unloadable, [])
        self.assertEqual(sorted(k for gs in grids for k in gs.keys), ["big/a.jpg", "big/b.jpg", "big/c.jpg"])
        for gs in grids:
            w, h = gs.surface.get_size()
            self.assertTrue(w <= 1920 and h <= 1030, (w, h))
        self.assertEqual(calls, [(1, 3), (2, 3), (3, 3)])

    def test_truncated_image_is_unloadable(self):
        self.quiet_stderr()
        path = self.root / "big/b.jpg"
        data = path.read_bytes()
        scan = data.index(b"\xff\xda")  # start of scan: everything before it is header
        path.write_bytes(data[: scan + (len(data) - scan) // 2])
        with Image.open(path) as im:  # the header still reads: the failure comes at decode time
            self.assertEqual(im.size, (100, 60))
        grids, unloadable = pack_into_grids(self.store.manifest()[1:], self.store, 1920, 1030)
        self.assertEqual(unloadable, ["big/b.jpg"])
        self.assertEqual([gs.keys for gs in grids], [["big/c.jpg"]])
        grids, unloadable = pack_into_grids(self.store.manifest()[1:2], self.store, 1920, 1030)
        self.assertEqual((grids, unloadable), ([], ["big/b.jpg"]))  # a bin left with no keys is dropped

    def test_unreadable_header_is_unloadable(self):
        (self.root / "big/b.jpg").write_bytes(b"not a jpeg")
        calls: list[tuple[int, int]] = []
        with (
            mock.patch.object(grid_packer_module, "load_surface", wraps=load_surface) as decode,
            self.assertLogs("image_review.grid_packer", "WARNING") as logs,
        ):
            grids, unloadable = pack_into_grids(
                self.store.manifest(), self.store, 1920, 1030, on_progress=lambda i, n: calls.append((i, n))
            )
        self.assertEqual(unloadable, ["big/b.jpg"])
        self.assertNotIn("big/b.jpg", {k for gs in grids for k in gs.keys})
        self.assertEqual(decode.call_count, 2)  # never decoded: left out at the header
        self.assertIn("cannot load big/b.jpg", "\n".join(logs.output))
        self.assertEqual(calls[-1], (3, 3))

    def test_decodes_one_bin_at_a_time(self):
        """Each 300x250 image fills its own 400x300 bin: when one is decoded, no earlier one is alive."""
        names = [f"many/{i}.jpg" for i in range(6)]
        store = self.make_store(dict.fromkeys(names, (300, 250)))
        decoded: list[weakref.ref] = []
        live_at_decode: list[int] = []

        def counting_load(buf: bytes) -> pg.Surface:
            gc.collect()
            live_at_decode.append(sum(ref() is not None for ref in decoded))
            surface = load_surface(buf)
            decoded.append(weakref.ref(surface))
            return surface

        with mock.patch.object(grid_packer_module, "load_surface", counting_load):
            grids, unloadable = pack_into_grids(store.manifest(), store, 400, 300)
        self.assertEqual(unloadable, [])
        self.assertEqual(sorted(gs.keys[0] for gs in grids), names)
        self.assertEqual([len(gs.keys) for gs in grids], [1] * 6)
        self.assertEqual(live_at_decode, [0] * 6)

    def test_every_key_covers_its_fit_size(self):
        """Each grid key's colour covers its fit_size area: shrunk, rotated and small images are all fully drawn."""
        images = {
            "pix/upright.jpg": ((3000, 2500), (200, 40, 40)),  # shrunk, upright
            "pix/rotates.jpg": ((1000, 1800), (40, 200, 40)),  # fits only rotated (or shrunk without rotation)
            "pix/shrunk_rotated.jpg": ((1500, 2500), (40, 40, 200)),  # fits as 1030x1716, placed rotated
            "pix/s1.jpg": ((100, 60), (200, 200, 40)),
            "pix/s2.jpg": ((60, 100), (200, 40, 200)),
            "pix/s3.jpg": ((120, 80), (40, 200, 200)),
        }
        store = self.make_store({n: size for n, (size, _) in images.items()}, {n: c for n, (_, c) in images.items()})
        for rot in (True, False):
            with self.subTest(allow_rotation=rot):
                grids, left_out = pack_into_grids(
                    store.manifest(), store, 1920, 1030, rotation="always" if rot else "never"
                )
                self.assertEqual(left_out, [])
                self.assertEqual(sorted(k for gs in grids for k in gs.keys), sorted(images))
                for gs in grids:
                    for k in gs.keys:
                        (w, h), colour = images[k]
                        fw, fh = fit_size(w, h, 1920, 1030, rot)
                        count = pg.mask.from_threshold(gs.surface, colour, (10, 10, 10, 255)).count()
                        self.assertAlmostEqual(count / (fw * fh), 1, delta=0.02, msg=k)

    def test_decoded_size_differing_from_header_is_left_out(self):
        bad = (self.root / "big/a.jpg").read_bytes()

        def wrong_size(buf: bytes) -> pg.Surface:
            return pg.Surface((7, 5), 0, 24) if buf == bad else load_surface(buf)

        calls: list[tuple[int, int]] = []
        with (
            mock.patch.object(grid_packer_module, "load_surface", wrong_size),
            self.assertLogs("image_review.grid_packer", "WARNING") as logs,
        ):
            grids, left_out = pack_into_grids(
                self.store.manifest(), self.store, 1920, 1030, on_progress=lambda i, n: calls.append((i, n))
            )
        self.assertEqual(left_out, ["big/a.jpg"])
        self.assertNotIn("big/a.jpg", {k for gs in grids for k in gs.keys})
        self.assertIn("cannot load big/a.jpg", "\n".join(logs.output))
        self.assertEqual(calls[-1], (3, 3))

    def test_key_left_unpacked_is_left_out(self):
        calls: list[tuple[int, int]] = []
        with dropping_packer(1), self.assertLogs("image_review.grid_packer", "WARNING") as logs:
            grids, left_out = pack_into_grids(
                self.store.manifest(), self.store, 1920, 1030, on_progress=lambda i, n: calls.append((i, n))
            )
        self.assertEqual(left_out, ["big/b.jpg"])
        self.assertEqual(sorted(k for gs in grids for k in gs.keys), ["big/a.jpg", "big/c.jpg"])
        self.assertIn("big/b.jpg was not packed", "\n".join(logs.output))
        self.assertEqual(calls[-1], (3, 3))

    def pack_recording(self, store: LocalStore, rotation: str) -> tuple[list[PlacedRect], int]:
        """The placements `pack_into_grids` composites under `rotation`, and the number of grids."""
        placed: list[PlacedRect] = []
        real = grid_packer_module._composite_bin

        def record(rects, *args, **kwargs):
            placed.extend(rects)
            return real(rects, *args, **kwargs)

        with mock.patch.object(grid_packer_module, "_composite_bin", record):
            grids, left_out = pack_into_grids(store.manifest(), store, 1920, 1030, rotation=rotation)
        self.assertEqual(left_out, [])
        return placed, len(grids)

    def test_auto_does_not_rotate_when_it_saves_no_grid(self):
        store = self.make_store({f"u/{i}.jpg": (512, 384) for i in range(12)})  # 6 per grid either way
        placed, n_grids = self.pack_recording(store, "auto")
        self.assertEqual(len(placed), 12)
        self.assertEqual({(r.w, r.h) for r in placed}, {(512, 384)})
        self.assertEqual(n_grids, self.pack_recording(store, "never")[1])

    def test_auto_keeps_rotation_that_saves_a_grid(self):
        store = self.make_store({f"t/{i}.jpg": (400, 600) for i in range(5)})
        never, never_grids = self.pack_recording(store, "never")
        self.assertEqual((never_grids, {(r.w, r.h) for r in never}), (2, {(400, 600)}))
        for rotation in ("auto", "always"):
            with self.subTest(rotation=rotation):
                placed, n_grids = self.pack_recording(store, rotation)
                self.assertEqual(n_grids, 1)
                self.assertIn((600, 400), {(r.w, r.h) for r in placed})

    def test_never_does_not_rotate_where_always_does(self):
        store = self.make_store({"p/a.jpg": (1000, 1800), "p/b.jpg": (1000, 1800)})
        self.assertEqual({(r.w, r.h) for r in self.pack_recording(store, "always")[0]}, {(1800, 1000)})
        self.assertEqual({(r.w, r.h) for r in self.pack_recording(store, "never")[0]}, {(572, 1030)})

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


if __name__ == "__main__":
    unittest.main()
