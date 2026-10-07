import csv
import http.client
import json
import os
import ssl
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

from PIL import Image

from image_review import server as server_module
from image_review.grid_packer import pack_into_grids
from image_review.layout import fit_size
from image_review.server import BadRequest, parse_grids
from image_review.status import Key
from tests.fixtures import (
    HAS_AF_UNIX,
    ROWS,
    UnixHTTPConnection,
    dropping_packer,
    make_work_dir,
    socket_dir,
    start_server,
    start_unix_server,
    temp_dir,
)

KEYS = [key for _, key, _ in ROWS]

# Varied sizes and aspects, some larger than every grid; the eight 330x250 pack tighter rotated in 640x400
PARITY_SIZES = [
    (300, 120),
    (120, 300),
    (90, 600),
    (640, 200),
    (200, 640),
    (1200, 900),
    (50, 50),
    (400, 260),
    (260, 400),
    (80, 700),
    (3000, 400),
    (150, 150),
    *[(330, 250)] * 8,
]


def make_sized_work_dir(root: Path, sizes: list[tuple[int, int]]) -> list[Key]:
    """A work dir of solid JPGs of the given sizes, with a legacy manifest; returns their keys in order."""
    (root / "b").mkdir()
    keys = [Key(f"b/{i:02d}.jpg") for i in range(len(sizes))]
    for key, size in zip(keys, sizes, strict=True):
        Image.new("RGB", size, (90, 90, 90)).save(root / key, "JPEG", quality=95)
    with open(root / "manifest.tsv", "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["batch", "preprocessed_path", "image_id"])
        writer.writerows(("b", key, f"/src/{key}") for key in keys)
    return keys


@unittest.skipUnless(HAS_AF_UNIX, "needs AF_UNIX")
class GridsTestCase(unittest.TestCase):
    def setUp(self):
        self.work_dir = temp_dir(self)
        self.keys = self.make_work_dir()
        self.path = socket_dir(self) / "ir.sock"
        self.server, self.token, stop = start_unix_server(self.work_dir, self.path)
        self.addCleanup(stop)

    def make_work_dir(self) -> list[Key]:
        make_work_dir(self.work_dir)
        return [Key(k) for k in KEYS]

    def connect(self) -> UnixHTTPConnection:
        conn = UnixHTTPConnection(self.path)
        self.addCleanup(conn.close)
        return conn

    def post(self, body, token="default", conn=None) -> tuple[http.client.HTTPResponse, bytes]:
        token = self.token if token == "default" else token
        conn = conn or self.connect()
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        conn.request("POST", "/grids", body=data, headers=headers)
        resp = conn.getresponse()
        return resp, resp.read()

    def grids(self, keys, width=640, height=480, rotation="auto") -> dict:
        resp, data = self.post({"keys": list(keys), "width": width, "height": height, "rotation": rotation})
        self.assertEqual(resp.status, 200, data)
        return json.loads(data)


class TestValidation(GridsTestCase):
    def test_bad_bodies_are_400(self):
        good = {"keys": KEYS[:2], "width": 640, "height": 480, "rotation": "auto"}
        cases = {
            "not json": b"{",
            "array": [good],
            "string": "keys",
            "missing keys": {k: v for k, v in good.items() if k != "keys"},
            "keys not a list": {**good, "keys": KEYS[0]},
            "empty keys": {**good, "keys": []},
            "non-string key": {**good, "keys": [KEYS[0], 1]},
            "duplicate keys": {**good, "keys": [KEYS[0], KEYS[0]]},
            "unknown key": {**good, "keys": [KEYS[0], "batch_001/zzz.jpg"]},
            "missing width": {k: v for k, v in good.items() if k != "width"},
            "bool width": {**good, "width": True},
            "float width": {**good, "width": 640.0},
            "string width": {**good, "width": "640"},
            "bool height": {**good, "height": False},
            "float height": {**good, "height": 480.5},
            "string height": {**good, "height": "480"},
            "width too small": {**good, "width": 255},
            "width too large": {**good, "width": 16385},
            "height too small": {**good, "height": 255},
            "height too large": {**good, "height": 16385},
            "bad rotation": {**good, "rotation": "sometimes"},
            "missing rotation": {k: v for k, v in good.items() if k != "rotation"},
        }
        for name, body in cases.items():
            with self.subTest(name):
                resp, data = self.post(body)
                self.assertEqual((resp.status, data), (400, b""))
        resp, _ = self.post(good)
        self.assertEqual(resp.status, 200)

    def test_bounds_are_inclusive(self):
        for side in (256, 16384):
            with self.subTest(side=side):
                self.grids(KEYS, width=side, height=side)

    def test_key_cap(self):
        known = frozenset(Key(k) for k in KEYS)
        body = {"keys": KEYS, "width": 640, "height": 480, "rotation": "never"}
        with mock.patch.object(server_module, "MAX_GRID_KEYS", len(KEYS) - 1):
            with self.assertRaises(BadRequest):
                parse_grids(json.dumps(body).encode(), known)
            resp, _ = self.post(body)
            self.assertEqual(resp.status, 400)
        with mock.patch.object(server_module, "MAX_GRID_KEYS", len(KEYS)):
            self.assertEqual(parse_grids(json.dumps(body).encode(), known).keys, tuple(KEYS))

    def test_needs_the_token(self):
        for token in (None, "wrong"):
            with self.subTest(token=token):
                resp, data = self.post({"keys": KEYS, "width": 640, "height": 480, "rotation": "auto"}, token=token)
                self.assertEqual((resp.status, data), (401, b""))


class TestGrids(GridsTestCase):
    def test_response_shape(self):
        result = self.grids(KEYS)
        self.assertEqual(result["left_out"], [])
        self.assertEqual(len(result["grids"]), 1)
        placements = result["grids"][0]
        self.assertEqual(sorted(p["key"] for p in placements), sorted(KEYS))
        for p in placements:
            self.assertEqual(set(p), {"key", "x", "y", "w", "h", "rotated", "source"})
            self.assertEqual(p["source"], [20, 12])
            self.assertFalse(p["rotated"])
            self.assertEqual((p["w"], p["h"]), (20, 12))

    def test_identical_requests_give_identical_bytes(self):
        body = {"keys": KEYS[::-1], "width": 300, "height": 256, "rotation": "always"}
        first, second = self.post(body), self.post(body)
        self.assertEqual(first[0].status, 200)
        self.assertEqual(first[1], second[1])

    def test_busy_is_503_and_keeps_the_connection(self):
        conn = self.connect()
        body = {"keys": KEYS, "width": 640, "height": 480, "rotation": "auto"}
        with self.server.grid_slot:
            resp, data = self.post(body, conn=conn)
            self.assertEqual((resp.status, data), (503, b""))
            self.assertIsNone(resp.getheader("Connection"))
        resp, _ = self.post(body, conn=conn)  # the same connection
        self.assertEqual(resp.status, 200)

    def test_sizes_are_memoised(self):
        store = self.server.store
        with mock.patch.object(store, "image_bytes", wraps=store.image_bytes) as image_bytes:
            first = self.grids(KEYS)
            self.assertEqual(image_bytes.call_count, len(KEYS))
            second = self.grids(KEYS, width=1024, height=768)
            self.assertEqual(image_bytes.call_count, len(KEYS))
        self.assertEqual(first["left_out"], second["left_out"])

    def test_unreadable_header_is_left_out_and_retried(self):
        good = (self.work_dir / KEYS[1]).read_bytes()
        (self.work_dir / KEYS[1]).write_bytes(b"not a jpeg")
        self.assertEqual(self.grids(KEYS)["left_out"], [KEYS[1]])
        (self.work_dir / KEYS[1]).write_bytes(good)  # failures are not memoised
        self.assertEqual(self.grids(KEYS)["left_out"], [])

    def test_nothing_about_keys_is_logged(self):
        (self.work_dir / KEYS[2]).unlink()
        with self.assertLogs("image_review", "DEBUG") as logs:
            self.grids(KEYS)
        for text in logs.output:
            for key in KEYS:
                self.assertNotIn(key, text)
            self.assertNotIn(str(self.work_dir), text)


class TestUnreadableFiles(GridsTestCase):
    def make_work_dir(self) -> list[Key]:
        make_work_dir(self.work_dir, hashed=True)
        return [Key(k) for k in KEYS]

    def test_hash_mismatch_and_missing_file_are_left_out(self):
        with open(self.work_dir / KEYS[0], "ab") as f:
            f.write(b"\0")  # still a readable header, but no longer the manifest's hash
        (self.work_dir / KEYS[2]).unlink()
        result = self.grids(KEYS)
        self.assertEqual(result["left_out"], [KEYS[0], KEYS[2]])
        self.assertEqual(sorted(p["key"] for g in result["grids"] for p in g), [KEYS[1], KEYS[3]])


class TestParity(GridsTestCase):
    def make_work_dir(self) -> list[Key]:
        return make_sized_work_dir(self.work_dir, PARITY_SIZES)

    def assert_well_placed(self, result: dict, width: int, height: int, rotation: str) -> None:
        placements = [p for grid in result["grids"] for p in grid]
        fits = [(p["h"], p["w"]) if p["rotated"] else (p["w"], p["h"]) for p in placements]
        # The size each image was fit at, before any rotation: fit_size with rotation allowed exactly when the
        # plan rotated (always; never not; auto either way, but the same for every image)
        allowed = {"always": [True], "never": [False], "auto": [False, True]}[rotation]
        self.assertTrue(
            any(fits == [fit_size(*p["source"], width, height, allow) for p in placements] for allow in allowed),
            (rotation, placements),
        )
        if rotation == "never":
            self.assertFalse(any(p["rotated"] for p in placements))
        for grid in result["grids"]:
            for p in grid:
                self.assertTrue(p["x"] >= 0 and p["x"] + p["w"] <= width, p)
                self.assertTrue(p["y"] >= 0 and p["y"] + p["h"] <= height, p)
                self.assertGreater(p["w"], 0)
                self.assertGreater(p["h"], 0)
            for i, a in enumerate(grid):
                for b in grid[i + 1 :]:
                    overlap = (
                        a["x"] < b["x"] + b["w"]
                        and b["x"] < a["x"] + a["w"]
                        and a["y"] < b["y"] + b["h"]
                        and b["y"] < a["y"] + a["h"]
                    )
                    self.assertFalse(overlap, (a, b))

    def test_matches_pack_into_grids(self):
        (self.work_dir / self.keys[4]).unlink()  # a missing file is left out by both
        any_rotated = False
        for rotation in ("auto", "always", "never"):
            for width, height in [(640, 400), (256, 1024), (1920, 1080)]:
                with self.subTest(rotation=rotation, width=width, height=height):
                    result = self.assert_parity(width, height, rotation)
                    self.assertIn(self.keys[4], result["left_out"])
                    any_rotated |= any(p["rotated"] for g in result["grids"] for p in g)
        self.assertTrue(any_rotated, "no case rotated an image")

    def test_unpacked_key_is_left_out(self):
        (self.work_dir / self.keys[4]).unlink()
        for rotation in ("auto", "always", "never"):
            with self.subTest(rotation=rotation), dropping_packer(2):  # the packer places every key but keys[2]
                result = self.assert_parity(640, 400, rotation)
                self.assertEqual(result["left_out"], [self.keys[2], self.keys[4]])
                self.assertNotIn(self.keys[2], [p["key"] for g in result["grids"] for p in g])

    def assert_parity(self, width: int, height: int, rotation: str) -> dict:
        """/grids and pack_into_grids agree on the grids' keys and the keys left out; returns the /grids result."""
        result = self.grids(self.keys, width, height, rotation)
        with self.assertLogs("image_review", "WARNING"):  # the missing file, from the store
            expected, left_out = pack_into_grids(self.keys, self.server.store, width, height, rotation=rotation)
        self.assertEqual([[p["key"] for p in g] for g in result["grids"]], [list(g.keys) for g in expected])
        self.assertEqual(result["left_out"], left_out)
        self.assert_well_placed(result, width, height, rotation)
        return result

    def test_auto_rotates_when_that_saves_a_grid(self):
        auto = self.grids(self.keys, 640, 400, "auto")
        never = self.grids(self.keys, 640, 400, "never")
        always = self.grids(self.keys, 640, 400, "always")
        self.assertLess(len(auto["grids"]), len(never["grids"]))
        self.assertEqual(auto, always)


class TestOverTls(unittest.TestCase):
    def setUp(self):
        self.work_dir = temp_dir(self)
        make_work_dir(self.work_dir)
        self.server, self.target, stop = start_server(self.work_dir)
        self.addCleanup(stop)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def post(self, body: bytes | None) -> int:
        conn = http.client.HTTPSConnection("127.0.0.1", self.target.port, context=self.ctx, timeout=10)
        self.addCleanup(conn.close)
        conn.request("POST", "/grids", body=body, headers={"Authorization": f"Bearer {self.target.token}"})
        resp = conn.getresponse()
        resp.read()
        return resp.status

    def test_grids_is_not_served(self):
        self.assertEqual(self.post(None), 404)

    def test_body_on_grids_is_refused(self):
        body = json.dumps({"keys": KEYS, "width": 640, "height": 480, "rotation": "auto"}).encode()
        self.assertEqual(self.post(body), 400)


if __name__ == "__main__":
    unittest.main()
