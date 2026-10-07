import json
import os
import shutil
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from image_review.cli import PACKAGE_LOGGER
from image_review.server import (
    clear_stale_socket,
    default_socket_path,
    is_local_host,
    make_unix_server,
    parse_socket_path,
)
from image_review.store import LocalStore
from tests.fixtures import UnixHTTPConnection, make_work_dir, start_unix_server, temp_dir

SERVER_LOGGER = f"{PACKAGE_LOGGER}.server"
HAS_AF_UNIX = hasattr(socket, "AF_UNIX")

# As TestAuth in test_server.py
ROUTES = [
    ("GET", "/manifest"),
    ("GET", "/image?key=batch_001/a.jpg"),
    ("GET", "/statuses?pass=1"),
    ("GET", "/current_pass"),
    ("GET", "/skipped"),
    ("GET", "/version"),
    ("POST", "/mark"),
    ("POST", "/undo"),
    ("GET", "/nope"),
    ("DELETE", "/manifest"),
]


def socket_dir(testcase: unittest.TestCase) -> Path:
    """A fresh directory under /tmp: macOS $TMPDIR is too long for sun_path."""
    path = Path(tempfile.mkdtemp(dir="/tmp"))
    testcase.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


class IsLocalHostTest(unittest.TestCase):
    def test_accepted(self):
        for value in [
            "localhost",
            "localhost:8080",
            "LOCALHOST:8080",
            "127.0.0.1",
            "127.0.0.1:1",
            "[::1]",
            "[::1]:8080",
            "localhost:65535",
        ]:
            with self.subTest(value=value):
                self.assertTrue(is_local_host([value]))

    def test_rejected(self):
        for value in [
            "evil.example",
            "localhost.evil.example",
            "::1",
            "::1:8080",
            "localhost:0",
            "localhost:65536",
            "localhost:abc",
            "localhost:",
            "localhost:٨٠",  # non-ASCII digits
            "localho\u017ft",  # long s: matches "localhost" only under Unicode case folding
            "localhost:" + "1" * 5000,  # past int()'s digit limit
            "localhost:000080",
            "127.0.0.2",
            "",
        ]:
            with self.subTest(value=value):
                self.assertFalse(is_local_host([value]))

    def test_missing_or_repeated(self):
        self.assertFalse(is_local_host([]))
        self.assertFalse(is_local_host(["localhost", "localhost"]))


@unittest.skipUnless(HAS_AF_UNIX, "needs AF_UNIX")
class SocketServerTestCase(unittest.TestCase):
    def setUp(self):
        self.work_dir = temp_dir(self)
        make_work_dir(self.work_dir)
        self.path = socket_dir(self) / "ir.sock"
        self.server, self.token, stop = start_unix_server(self.work_dir, self.path)
        self.addCleanup(stop)

    def request(self, method, path, body=None, token="default", host="localhost:8080"):
        """One request through http.client, with `host` sent exactly as given (http.client would rewrite "::1")."""
        token = self.token if token == "default" else token
        conn = UnixHTTPConnection(self.path)
        self.addCleanup(conn.close)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host)
        if token is not None:
            conn.putheader("Authorization", f"Bearer {token}")
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp, resp.read()

    def raw_request(
        self, method: str, path: str, token: str | None, host: str = "localhost", body: bytes = b""
    ) -> bytes:
        """Headers and body in one write, so a server that replies early and closes cannot cause EPIPE."""
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", f"Content-Length: {len(body)}"]
        if token is not None:
            lines.append(f"Authorization: Bearer {token}")
        return self.raw(("\r\n".join(lines) + "\r\n\r\n").encode() + body)

    def raw(self, data: bytes) -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect(str(self.path))
            sock.sendall(data)
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        return b"".join(chunks)


class TestSocketRequests(SocketServerTestCase):
    def test_current_pass(self):
        resp, data = self.request("GET", "/current_pass")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data), {"pass": 1})
        resp, data = self.request("GET", "/current_pass", token=None)
        self.assertEqual((resp.status, data), (401, b""))

    def test_every_route_needs_the_token(self):
        for method, path in ROUTES:
            for token in (None, "wrong", ""):
                with self.subTest(method=method, path=path, token=token):
                    reply = self.raw_request(method, path, token, body=b"{}" if method == "POST" else b"")
                    head, _, data = reply.partition(b"\r\n\r\n")
                    self.assertTrue(head.startswith(b"HTTP/1.1 401 "), reply)
                    self.assertEqual(data, b"")
                    self.assertIn(b"\r\nCache-Control: no-store", head)

    def test_tokenless_requests_log_no_connection_errors(self):
        # An empty reply body used to be written as b"" after the client had closed: EPIPE, logged as a WARNING
        with self.assertNoLogs(SERVER_LOGGER, "WARNING"):
            for _ in range(200):
                resp, data = self.request("GET", "/current_pass", token=None)
                self.assertEqual((resp.status, data), (401, b""))

    def test_bad_host_is_400_even_with_token(self):
        for host in ["evil.example", "localhost.evil.example", "::1", "localhost:0"]:
            with self.subTest(host=host):
                resp, data = self.request("GET", "/current_pass", host=host)
                self.assertEqual((resp.status, data), (400, b""))
                self.assertEqual(resp.getheader("Connection"), "close")

    def test_bad_host_is_checked_before_the_token(self):
        for token in (None, "wrong"):
            with self.subTest(token=token):
                resp, data = self.request("GET", "/current_pass", token=token, host="evil.example")
                self.assertEqual((resp.status, data), (400, b""))

    def test_repeated_host_is_400(self):
        auth = f"Authorization: Bearer {self.token}\r\n".encode()
        reply = self.raw(b"GET /current_pass HTTP/1.1\r\nHost: localhost\r\nHost: localhost\r\n" + auth + b"\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.1 400 "), reply)

    def test_missing_host_is_400(self):
        auth = f"Authorization: Bearer {self.token}\r\n".encode()
        reply = self.raw(b"GET /current_pass HTTP/1.0\r\n" + auth + b"\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.1 400 "), reply)

    def test_mark_then_undo(self):
        body = {"keys": ["batch_001/a.jpg"], "status": "CLEAN", "pass": 1, "reviewer": "tester", "mode": "single"}
        resp, data = self.request("POST", "/mark", body=json.dumps(body).encode())
        self.assertEqual((resp.status, json.loads(data)), (200, {"batch_001/a.jpg": "CLEAN"}))
        resp, data = self.request("GET", "/statuses?pass=1")
        self.assertEqual(json.loads(data)["batch_001/a.jpg"], "CLEAN")
        resp, data = self.request("POST", "/undo", body=json.dumps({"pass": 1, "reviewer": "tester"}).encode())
        self.assertEqual((resp.status, json.loads(data)), (200, {"batch_001/a.jpg": "UNREVIEWED"}))
        resp, data = self.request("GET", "/statuses?pass=1")
        self.assertEqual(json.loads(data)["batch_001/a.jpg"], "UNREVIEWED")

    def test_log_line_uses_unix_peer_and_hides_token(self):
        with self.assertLogs(SERVER_LOGGER, "INFO") as logs:
            self.request("GET", "/current_pass")
            self.request("GET", "/current_pass", host="evil.example")
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(messages, ["unix GET /current_pass 200", "unix GET /current_pass 400"])
        for text in [*messages, *logs.output]:
            self.assertNotIn(self.token, text)
            self.assertNotIn("Error", text)


@unittest.skipUnless(HAS_AF_UNIX, "needs AF_UNIX")
class TestSocketFile(unittest.TestCase):
    def setUp(self):
        self.work_dir = temp_dir(self)
        make_work_dir(self.work_dir)
        self.store = LocalStore(self.work_dir)
        self.addCleanup(self.store.close)
        self.dir = socket_dir(self)
        self.path = self.dir / "ir.sock"

    def serve(self):
        server, _ = make_unix_server(self.store, self.path)
        self.addCleanup(server.server_close)
        return server

    def test_socket_is_private_and_removed_on_close(self):
        server = self.serve()
        info = self.path.lstat()
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        server.server_close()
        self.assertFalse(self.path.exists())

    def test_close_releases_socket_even_if_unlink_fails(self):
        server = self.serve()
        with mock.patch("os.unlink", side_effect=PermissionError), self.assertRaises(PermissionError):
            server.server_close()
        self.assertEqual(server.socket.fileno(), -1)

    def test_replacement_file_left_alone_on_close(self):
        server = self.serve()
        self.path.unlink()
        self.path.write_text("not ours")
        server.server_close()
        self.assertEqual(self.path.read_text(), "not ours")

    def test_stale_socket_replaced(self):
        # Kept open: bound but not listening refuses connections, and pins the old inode so its number cannot be reused
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(self.path))
            old = self.path.lstat().st_ino
            self.serve()
            self.assertNotEqual(self.path.lstat().st_ino, old)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(self.path))  # the new server is listening there

    def test_live_listener_refused(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as live:
            live.bind(str(self.path))
            live.listen()
            before = self.path.lstat()
            with self.assertRaisesRegex(ValueError, "Another server is listening"):
                make_unix_server(self.store, self.path)
            after = self.path.lstat()
            self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))

    def test_regular_file_refused(self):
        self.path.write_text("keep")
        with self.assertRaisesRegex(ValueError, "not a socket"):
            make_unix_server(self.store, self.path)
        self.assertEqual(self.path.read_text(), "keep")

    def test_long_path_refused(self):
        path = self.dir / ("x" * (200 - len(str(self.dir)) - 1))
        self.assertEqual(len(os.fsencode(path)), 200)
        with self.assertRaisesRegex(ValueError, r"200 bytes.*/tmp/ir\.sock"):
            make_unix_server(self.store, path)
        self.assertFalse(path.exists())

    def test_colon_or_control_char_refused(self):
        for name in ["a:b.sock", "a\nb.sock", "a\x7fb.sock"]:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "control characters"):
                parse_socket_path(self.dir / name)

    def test_relative_path_made_absolute(self):
        self.assertEqual(parse_socket_path("ir.sock"), Path.cwd() / "ir.sock")

    def test_missing_parent_raises_oserror_and_leaves_nothing(self):
        path = self.dir / "missing" / "ir.sock"
        with self.assertRaises(OSError):
            make_unix_server(self.store, path)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_clear_stale_socket_ignores_absent_path(self):
        clear_stale_socket(self.path)
        self.assertFalse(self.path.exists())

    def test_manifest_failure_binds_nothing(self):
        with mock.patch.object(self.store, "manifest", side_effect=OSError), self.assertRaises(OSError):
            make_unix_server(self.store, self.path)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_default_socket_path(self):
        home = temp_dir(self)
        with (
            mock.patch.dict(os.environ, {"HOME": str(home)}),
            mock.patch("socket.gethostname", return_value="node1.cluster.example"),
        ):
            path = default_socket_path()
        self.assertEqual(path, home / ".image-review" / f"serve-node1-{os.getpid()}.sock")
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
