import contextlib
import csv
import http.client
import io
import json
import logging
import os
import signal
import socket
import socketserver
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from image_review.cli import PACKAGE_LOGGER, LogFormatter, cli
from image_review.connection import API_VERSION, RemoteTarget
from image_review.server import HANDSHAKE_TIMEOUT_SECONDS, ReviewServer, make_server
from image_review.store import LocalStore, load_manifest
from tests.fixtures import ROWS, invoke_cli, make_work_dir, start_server, temp_dir

FP = "a" * 64
SERVER_LOGGER = f"{PACKAGE_LOGGER}.server"


class MakeServerChecksTest(unittest.TestCase):
    def setUp(self):
        work_dir = temp_dir(self)
        make_work_dir(work_dir)
        self.store = LocalStore(work_dir)
        self.addCleanup(self.store.close)

    def assert_refused(self, host: str, message: str, *, binds: bool):
        close = mock.patch.object(ReviewServer, "server_close", autospec=True, side_effect=ReviewServer.server_close)
        with close as closed, self.assertRaisesRegex(ValueError, message):
            make_server(self.store, host, 0)
        self.assertEqual(closed.call_count, 1 if binds else 0)

    def test_wildcard_hosts_refused_before_binding(self):
        for host in ["0.0.0.0", "::", "", "  ", "0:0:0:0:0:0:0:0"]:
            with self.subTest(host=host):
                self.assert_refused(host, "Refusing to bind a wildcard", binds=False)

    def test_unspecified_bound_address_closes_socket(self):
        self.assert_refused("0", "Refusing to bind a wildcard", binds=True)

    def test_unadvertisable_host_closes_socket(self):
        self.assert_refused("127.1", "cannot be advertised", binds=True)

    def test_tls_manifest_failure_binds_nothing(self):
        work_dir = temp_dir(self)
        make_work_dir(work_dir)
        store = LocalStore(work_dir)
        self.addCleanup(store.close)
        with (
            mock.patch.object(store, "manifest", side_effect=OSError),
            mock.patch.object(socketserver.TCPServer, "server_bind") as server_bind,
            self.assertRaises(OSError),
        ):
            make_server(store, "127.0.0.1", 0)
        server_bind.assert_not_called()


class ServerTestCase(unittest.TestCase):
    HASHED = False  # manifest.tsv with the hash columns

    def setUp(self):
        self.work_dir = temp_dir(self)
        make_work_dir(self.work_dir, hashed=self.HASHED)
        self.server, self.target, stop = start_server(self.work_dir)
        self.addCleanup(stop)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.ctx = ctx

    def request(self, method, path, body=None, token="default", headers=None):
        token = self.target.token if token == "default" else token
        hdrs = dict(headers or {})
        if token is not None:
            hdrs["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPSConnection("127.0.0.1", self.target.port, context=self.ctx, timeout=10)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp, data, conn

    def raw(self, data: bytes) -> bytes:
        """Send raw bytes over TLS and read until the server closes."""
        with (
            socket.create_connection(("127.0.0.1", self.target.port), timeout=10) as sock,
            self.ctx.wrap_socket(sock) as tls,
        ):
            tls.sendall(data)
            chunks = []
            while chunk := tls.recv(65536):
                chunks.append(chunk)
        return b"".join(chunks)

    def auth_header(self) -> str:
        return f"Authorization: Bearer {self.target.token}\r\n"

    def get_json(self, path):
        resp, data, _ = self.request("GET", path)
        self.assertEqual(resp.status, 200)
        return json.loads(data)

    def post_mark(self, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return self.request("POST", "/mark", body=body)[:2]

    def post_undo(self, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return self.request("POST", "/undo", body=body)[:2]


class TestTransport(ServerTestCase):
    def test_peer_cert_matches_fingerprint(self):
        from image_review.connection import cert_fingerprint

        _, _, conn = self.request("GET", "/current_pass")
        der = conn.sock.getpeercert(binary_form=True)
        self.assertEqual(cert_fingerprint(der), self.target.fingerprint)

    def test_security_headers_and_persistent_connection(self):
        resp, _, conn = self.request("GET", "/current_pass")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        conn.request("GET", "/current_pass", headers={"Authorization": f"Bearer {self.target.token}"})
        self.assertEqual(conn.getresponse().status, 200)


class TestConnectionHandling(ServerTestCase):
    def test_keep_alive_not_delayed_by_nagle(self):
        conn = http.client.HTTPSConnection("127.0.0.1", self.target.port, context=self.ctx, timeout=10)
        self.addCleanup(conn.close)
        headers = {"Authorization": f"Bearer {self.target.token}"}
        # Nagle + delayed ACK costs ~40 ms per request (~1.2 s for 30), every time.
        # A loaded machine can slow one run, but not all of three, so any fast run passes.
        elapsed = []
        for _ in range(3):
            start = time.perf_counter()
            for _ in range(30):
                conn.request("GET", "/current_pass", headers=headers)
                self.assertEqual(conn.getresponse().read(), b'{"pass": 1}')
            elapsed.append(time.perf_counter() - start)
            if elapsed[-1] < 0.9:
                return
        self.fail(f"30 keep-alive requests took {elapsed} s in 3 attempts; want one under 0.9 s")

    def test_connection_close_header_and_reconnect(self):
        cases = {
            401: ("GET", "/manifest", None),
            404: ("GET", "/nope", "default"),
            400: ("GET", "/statuses", "default"),
        }
        for status, (method, path, token) in cases.items():
            with self.subTest(status=status):
                resp, _, conn = self.request(method, path, token=token)
                self.assertEqual(resp.status, status)
                self.assertEqual(resp.getheader("Connection"), "close")
                conn.request("GET", "/current_pass", headers={"Authorization": f"Bearer {self.target.token}"})
                again = conn.getresponse()
                self.assertEqual(again.status, 200)
                again.read()

    def test_successful_reply_keeps_connection(self):
        resp, _, _ = self.request("GET", "/current_pass")
        self.assertIsNone(resp.getheader("Connection"))

    def test_malformed_request_line_gets_400_with_headers(self):
        with contextlib.redirect_stderr(io.StringIO()):
            data = self.raw(b"GARBAGE\r\n\r\n")
        self.assertTrue(data.startswith(b"HTTP/1.1 400"), data[:60])
        self.assertIn(b"Cache-Control: no-store", data)
        self.assertIn(b"X-Content-Type-Options: nosniff", data)

    def test_transfer_encoding_rejected(self):
        data = self.raw(
            (
                f"POST /mark HTTP/1.1\r\nHost: x\r\n{self.auth_header()}Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
            ).encode()
        )
        self.assertTrue(data.startswith(b"HTTP/1.1 400"))
        self.assertIn(b"Connection: close", data)

    def test_body_on_bodyless_route_rejected(self):
        resp, _, _ = self.request("GET", "/manifest", body=b"abc")
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.getheader("Connection"), "close")

    def test_duplicate_content_length_on_get_rejected(self):
        data = self.raw(
            (
                f"GET /manifest HTTP/1.1\r\nHost: x\r\n{self.auth_header()}Content-Length: 0\r\nContent-Length: 5\r\n\r\n"
            ).encode()
        )
        self.assertTrue(data.startswith(b"HTTP/1.1 400"))

    def test_duplicate_content_length_on_mark_rejected(self):
        data = self.raw(
            (
                f"POST /mark HTTP/1.1\r\nHost: x\r\n{self.auth_header()}Content-Length: 2\r\nContent-Length: 2\r\n\r\n{{}}"
            ).encode()
        )
        self.assertTrue(data.startswith(b"HTTP/1.1 400"))

    def test_handler_failure_is_500_and_server_survives(self):
        body = json.dumps(
            {"keys": ["batch_001/a.jpg"], "status": "CLEAN", "pass": 1, "reviewer": "tester", "mode": "single"}
        ).encode()
        with (
            self.assertLogs(SERVER_LOGGER, "INFO") as logs,
            mock.patch.object(self.server.store, "mark", side_effect=OSError("secret-detail")),
        ):
            resp, data, conn = self.request("POST", "/mark", body=body)
            self.assertEqual((resp.status, data), (500, b""))
            self.assertEqual(resp.getheader("Connection"), "close")
        errors = [r for r in logs.records if r.levelno == logging.ERROR]
        self.assertEqual([r.getMessage() for r in errors], ["internal error: OSError"])
        self.assertNotIn("secret-detail", "\n".join(logs.output))
        conn.request("GET", "/current_pass", headers={"Authorization": f"Bearer {self.target.token}"})
        self.assertEqual(conn.getresponse().status, 200)

    def test_deeply_nested_json_is_400(self):
        resp, _ = self.post_mark(b"[" * 200000)
        self.assertEqual(resp.status, 400)


class TestLogging(ServerTestCase):
    def logged(self, logs) -> tuple[list[str], str]:
        """Each record's message, and all records as the CLI's formatter renders them."""
        formatter = LogFormatter()
        return [r.getMessage() for r in logs.records], "\n".join(formatter.format(r) for r in logs.records)

    def test_logs_escape_control_chars_and_drop_query(self):
        with self.assertLogs(SERVER_LOGGER, "INFO") as logs:
            self.raw(b"GET /a\x1b[31mb?key=SECRETKEY HTTP/1.1\r\nHost: x\r\n\r\n")
        messages, formatted = self.logged(logs)
        self.assertEqual(messages, ["127.0.0.1 GET /a\\x1b[31mb 401"])
        for text in (*messages, formatted):
            self.assertNotIn("\x1b", text)
            self.assertNotIn("SECRETKEY", text)
            self.assertNotIn("?", text)

    def test_request_line_format(self):
        with self.assertLogs(SERVER_LOGGER, "INFO") as logs:
            self.request("GET", "/current_pass")
        (record,) = logs.records
        self.assertEqual(record.levelno, logging.INFO)
        self.assertEqual(record.getMessage(), "127.0.0.1 GET /current_pass 200")
        _, formatted = self.logged(logs)
        self.assertRegex(
            formatted,
            r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d INFO image_review.server: 127.0.0.1 GET /current_pass 200$",
        )

    def test_logs_never_contain_token_or_key(self):
        with self.assertLogs(SERVER_LOGGER, "INFO") as logs:
            self.request("GET", "/image?key=batch_001%2Fa.jpg")
            self.request("GET", "/image?key=batch_001%2Fa.jpg", token="wrong")
            self.request("GET", "/image?key=batch_001%2Fnope.jpg")
            self.request("GET", "/statuses?pass=zzz")
            self.request("POST", "/mark", body=b"{}")
        messages, formatted = self.logged(logs)
        self.assertEqual(
            messages,
            [
                "127.0.0.1 GET /image 200",
                "127.0.0.1 GET /image 401",
                "127.0.0.1 GET /image 404",
                "127.0.0.1 GET /statuses 400",
                "127.0.0.1 POST /mark 400",
            ],
        )
        for text in (*messages, formatted):
            self.assertNotIn(self.target.token, text)
            self.assertNotIn("wrong", text)
            self.assertNotIn("batch_001", text)
            self.assertNotIn("key=", text)
            self.assertNotIn("pass=", text)
            self.assertNotIn("?", text)

    def test_connection_error_logs_class_name_only(self):
        with self.assertLogs(SERVER_LOGGER, "WARNING") as logs:
            self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(logs.records[0].getMessage(), "connection error: unknown")
        try:
            raise ssl.SSLError("secret-detail")
        except ssl.SSLError:
            with self.assertLogs(SERVER_LOGGER, "WARNING") as logs:
                self.server.handle_error(None, ("127.0.0.1", 1))
        (record,) = logs.records
        self.assertEqual((record.levelno, record.getMessage()), (logging.WARNING, "connection error: SSLError"))


class TestPreAuthIdleSockets(ServerTestCase):
    def test_idle_raw_sockets_do_not_block_real_client(self):
        idle = [socket.create_connection(("127.0.0.1", self.target.port), timeout=10) for _ in range(40)]
        for sock in idle:
            self.addCleanup(sock.close)
        start = time.perf_counter()
        resp, data, _ = self.request("GET", "/current_pass")
        self.assertEqual((resp.status, data), (200, b'{"pass": 1}'))
        # Blocked behind the idle sockets, the request would wait out the handshake timeout.
        self.assertLess(time.perf_counter() - start, HANDSHAKE_TIMEOUT_SECONDS / 2)


class TestNoKeyFilesLeft(unittest.TestCase):
    def test_make_server_leaves_temp_dir_empty(self):
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as scratch:
            make_work_dir(Path(work))
            with mock.patch.object(tempfile, "tempdir", scratch), LocalStore(Path(work)) as store:
                server, _ = make_server(store, "127.0.0.1", 0)
            server.server_close()
            self.assertEqual(os.listdir(scratch), [])


class TestServeCommand(unittest.TestCase):
    def setUp(self):
        root = temp_dir(self)
        self.work = root / "work"
        self.work.mkdir()
        make_work_dir(self.work)
        self.home = root / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_tty_writes_private_file_and_hides_token(self):
        seen = {}
        closed = []
        orig_close = ReviewServer.server_close

        def fake_serve(server, *a, **k):
            files = list((self.home / ".image-review").glob("connection-*.txt"))
            seen["files"] = files
            seen["file_mode"] = stat.S_IMODE(files[0].stat().st_mode)
            seen["dir_mode"] = stat.S_IMODE(files[0].parent.stat().st_mode)
            seen["uri"] = files[0].read_text().strip()
            raise KeyboardInterrupt

        def fake_close(server):
            closed.append(True)
            orig_close(server)

        with (
            mock.patch.object(ReviewServer, "serve_forever", fake_serve),
            mock.patch.object(ReviewServer, "server_close", fake_close),
        ):
            result = invoke_cli("serve", "--work-dir", str(self.work), "--bind", "127.0.0.1")
        self.assertEqual(result.exit_code, 0, result.output)
        target = RemoteTarget.parse(seen["uri"])
        self.assertNotIn(target.token, result.output)
        self.assertNotIn("ir://", result.output.replace("--remote", ""))
        self.assertNotIn("$(cat ", result.output)  # the file exists on the cluster, not the laptop
        self.assertIn(f"$(ssh <user>@<login-node> cat {seen['files'][0]})", result.output)
        self.assertIn(str(seen["files"][0]), result.output)
        self.assertEqual(seen["file_mode"], 0o600)
        self.assertEqual(seen["dir_mode"], 0o700)
        self.assertEqual(list((self.home / ".image-review").iterdir()), [])
        self.assertEqual(closed, [True])
        self.assertFalse((self.work / "review.lock").exists())

    def test_locked_work_dir_exits_1(self):
        lock = self.work / "review.lock"
        lock.write_text(
            json.dumps({"host": "node042", "user": "alice", "pid": 1234, "started": "2026-09-30T12:00:00Z"})
        )
        with mock.patch.object(ReviewServer, "serve_forever") as serve_forever:
            result = invoke_cli("serve", "--work-dir", str(self.work), "--bind", "127.0.0.1")
        self.assertEqual(result.exit_code, 1, result.output)
        serve_forever.assert_not_called()
        for part in ("alice", "node042", "pid 1234", str(lock)):
            self.assertIn(part, result.output)
        self.assertTrue(lock.exists())

    def test_serve_holds_lock_while_serving(self):
        seen = []

        def fake_serve(server, *a, **k):
            seen.append(json.loads((self.work / "review.lock").read_text())["pid"])
            raise KeyboardInterrupt

        with mock.patch.object(ReviewServer, "serve_forever", fake_serve):
            result = invoke_cli("serve", "--work-dir", str(self.work), "--bind", "127.0.0.1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [os.getpid()])
        self.assertFalse((self.work / "review.lock").exists())

    def test_loose_existing_dir_is_tightened(self):
        d = self.home / ".image-review"
        d.mkdir(mode=0o755)
        d.chmod(0o755)
        from image_review.server import write_connection_file

        path = write_connection_file(RemoteTarget("h", 1, "t", FP))
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_write_private_file_replaces_stale_file(self):
        from image_review.server import write_private_file

        d = self.home / ".image-review"
        d.mkdir(mode=0o700)
        stale = d / "x.txt"
        stale.write_text("old")
        stale.chmod(0o644)
        path = write_private_file("x.txt", "new\n")
        self.assertEqual(path, stale)
        self.assertEqual(path.read_text(), "new\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_tty_prints_connection_string(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        with (
            mock.patch.object(ReviewServer, "serve_forever", side_effect=KeyboardInterrupt),
            mock.patch("sys.stdout", Tty()) as out,
        ):
            code = cli.main(["serve", "--work-dir", str(self.work), "--bind", "127.0.0.1"], standalone_mode=False)
        self.assertIn("ir://127.0.0.1:", out.getvalue())
        self.assertIsNone(code)

    def test_sigterm_cleans_up_connection_file(self):
        env = {**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen(
            [sys.executable, "-m", "image_review.cli", "serve", "--work-dir", str(self.work), "--bind", "127.0.0.1"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.kill)
        directory = self.home / ".image-review"
        deadline = time.monotonic() + 20
        while not list(directory.glob("connection-*.txt")):
            self.assertLess(time.monotonic(), deadline, "server never wrote its connection file")
            self.assertIsNone(proc.poll())
            time.sleep(0.05)
        time.sleep(0.3)  # let serve_forever start
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(list(directory.iterdir()), [])
        self.assertFalse((self.work / "review.lock").exists())

    def test_missing_manifest(self):
        empty = self.home / "empty"
        empty.mkdir()
        result = invoke_cli("serve", "--work-dir", str(empty))
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No preprocessed data found", result.output)

    def test_bad_bind_and_port(self):
        cases = [
            (["--bind", "0.0.0.0"], "Refusing to bind a wildcard"),
            (["--bind", "::"], "Refusing to bind a wildcard"),
            (["--bind", ""], "Refusing to bind a wildcard"),
            (["--bind", "::1"], "Cannot listen on ::1"),
            (["--bind", "0"], "Refusing to bind a wildcard"),
            (["--bind", "127.1"], "cannot be advertised"),
            (["--port", "70000"], "Invalid value for '--port'"),
            (["--port", "-1"], "Invalid value for '--port'"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                result = invoke_cli("serve", "--work-dir", str(self.work), *args)
                self.assertEqual(result.exit_code, 2 if "--port" in args else 1, result.output)
                self.assertNotIsInstance(result.exception, OSError)
                self.assertNotIn("Traceback", result.output)
                self.assertIn(message, result.output)
                self.assertFalse((self.work / "review.lock").exists())

    def test_connection_file_failure_releases_lock(self):
        with (
            mock.patch("image_review.server.write_connection_file", side_effect=OSError("disk full")),
            mock.patch.object(ReviewServer, "serve_forever") as serve_forever,
        ):
            result = invoke_cli("serve", "--work-dir", str(self.work), "--bind", "127.0.0.1")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Cannot write connection file: disk full", result.output)
        serve_forever.assert_not_called()
        self.assertFalse((self.work / "review.lock").exists())

    def test_store_closed_under_store_lock(self):
        servers = []
        lock_held_at_close = []
        real_close = LocalStore.close

        def fake_serve(server, *a, **k):
            servers.append(server)
            raise KeyboardInterrupt

        def close(store):
            lock_held_at_close.append(servers[0].store_lock.locked())
            real_close(store)

        with (
            mock.patch.object(ReviewServer, "serve_forever", fake_serve),
            mock.patch.object(LocalStore, "close", close),
        ):
            result = invoke_cli("serve", "--work-dir", str(self.work), "--bind", "127.0.0.1")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(lock_held_at_close[0])  # a handler thread's mark cannot interleave with the release
        self.assertFalse((self.work / "review.lock").exists())


class TestAuth(ServerTestCase):
    def test_missing_and_wrong_token_rejected_everywhere(self):
        requests = [
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
        for method, path in requests:
            for token in (None, "wrong", ""):
                with self.subTest(method=method, path=path, token=token):
                    resp, data, _ = self.request(method, path, token=token, body=b"{}" if method == "POST" else None)
                    self.assertEqual(resp.status, 401)
                    self.assertEqual(data, b"")
                    self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_no_public_routes_or_csp_over_tls(self):
        resp, data, _ = self.request("GET", "/")
        self.assertEqual((resp.status, data), (404, b""))
        self.assertIsNone(resp.getheader("Content-Security-Policy"))
        self.assertIsNone(resp.getheader("Referrer-Policy"))
        resp, data, _ = self.request("GET", "/", token=None)
        self.assertEqual((resp.status, data), (401, b""))
        self.assertIsNone(resp.getheader("Content-Security-Policy"))
        self.assertIsNone(resp.getheader("Referrer-Policy"))

    def test_unknown_path_with_token_is_404(self):
        resp, data, _ = self.request("GET", "/nope")
        self.assertEqual((resp.status, data), (404, b""))


class TestReads(ServerTestCase):
    def test_manifest_has_keys_and_no_image_ids(self):
        resp, data, _ = self.request("GET", "/manifest")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data), [{"key": key, "batch": batch} for batch, key, _ in ROWS])
        for _, _, image_id in ROWS:
            self.assertNotIn(image_id.encode(), data)
            self.assertNotIn(Path(image_id).name.encode(), data)

    def test_image_bytes(self):
        resp, data, _ = self.request("GET", "/image?key=batch_001%2Fa.jpg")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "image/jpeg")
        self.assertEqual(data, (self.work_dir / "batch_001/a.jpg").read_bytes())

    def test_image_unknown_or_traversal_404(self):
        for key in ("batch_001/nope.jpg", "../manifest.tsv", "batch_001/../../etc/passwd", "/src/patient_smith/a.dcm"):
            with self.subTest(key=key):
                from urllib.parse import quote

                resp, data, _ = self.request("GET", f"/image?key={quote(key, safe='')}")
                self.assertEqual((resp.status, data), (404, b""))

    def test_work_dir_files_are_not_images(self):
        (self.work_dir / "preprocess.json").write_text('{"sources": ["/src/patient_smith"]}')
        for key in ("preprocess.json", "manifest.tsv", "skipped.tsv", "review.tsv", "./preprocess.json"):
            with self.subTest(key=key):
                from urllib.parse import quote

                resp, data, _ = self.request("GET", f"/image?key={quote(key, safe='')}")
                self.assertEqual((resp.status, data), (404, b""))

    def test_image_without_key_is_400(self):
        resp, _, _ = self.request("GET", "/image")
        self.assertEqual(resp.status, 400)

    def test_version(self):
        from importlib.metadata import version

        resp, data, _ = self.request("GET", "/version")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data), {"api": API_VERSION, "version": version("image-review")})

    def test_api_version_is_7(self):
        self.assertEqual(API_VERSION, 7)
        self.assertEqual(self.get_json("/version")["api"], 7)

    def test_skipped_absent_is_zero_counts(self):
        resp, data, _ = self.request("GET", "/skipped")
        self.assertEqual((resp.status, json.loads(data)), (200, {"failed": 0, "ignored": 0}))

    def test_skipped_counts_only_no_paths(self):
        rows = [(f"{image_id}", "failed", "cannot read /src/secret/dir") for _, _, image_id in ROWS[:2]]
        rows.append(("/src/patient_x/notes.txt", "ignored", "not an image"))
        text = "image_id\tkind\treason\n" + "".join("\t".join(r) + "\n" for r in rows)
        (self.work_dir / "skipped.tsv").write_text(text)
        resp, data, _ = self.request("GET", "/skipped")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data), {"failed": 2, "ignored": 1})
        self.assertNotIn(b"/", data)
        for _, _, image_id in ROWS:
            self.assertNotIn(image_id.encode(), data)

    def test_statuses_and_current_pass(self):
        self.assertEqual(self.get_json("/current_pass"), {"pass": 1})
        statuses = self.get_json("/statuses?pass=1")
        self.assertEqual(statuses, {key: "UNREVIEWED" for _, key, _ in ROWS})

    def test_statuses_bad_pass_is_400(self):
        for path in ("/statuses", "/statuses?pass=x", "/statuses?pass=0", "/statuses?pass=-1"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status, 400)

    def test_repeated_query_parameter_is_400(self):
        for path in ("/statuses?pass=1&pass=1", "/image?key=batch_001%2Fa.jpg&key=batch_001%2Fb.jpg", "/image"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status, 400)


class TestHashedManifest(ServerTestCase):
    HASHED = True

    def test_manifest_has_no_hashes(self):
        resp, data, _ = self.request("GET", "/manifest")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data), [{"key": key, "batch": batch} for batch, key, _ in ROWS])
        for entry in load_manifest(self.work_dir):
            for digest in (entry.source_sha256, entry.jpeg_sha256):
                self.assertIsNotNone(digest)
                self.assertNotIn(str(digest).encode(), data)

    def test_image_not_matching_its_hash_is_404(self):
        path = self.work_dir / "batch_001/a.jpg"
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0x01
        path.write_bytes(bytes(data))
        resp, body, _ = self.request("GET", "/image?key=batch_001%2Fa.jpg")
        self.assertEqual((resp.status, body), (404, b""))
        resp, body, _ = self.request("GET", "/image?key=batch_001%2Fb.jpg")
        self.assertEqual(resp.status, 200)


class TestMark(ServerTestCase):
    def test_mark_round_trip(self):
        resp, data = self.post_mark(
            {
                "keys": ["batch_001/a.jpg", "batch_001/b.jpg"],
                "status": "CLEAN",
                "pass": 1,
                "reviewer": "tester",
                "mode": "single",
            }
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data), {"batch_001/a.jpg": "CLEAN", "batch_001/b.jpg": "CLEAN"})
        statuses = self.get_json("/statuses?pass=1")
        self.assertEqual(statuses["batch_001/a.jpg"], "CLEAN")
        self.assertEqual(statuses["batch_002/c.jpg"], "UNREVIEWED")
        with open(self.work_dir / "review.tsv", newline="") as f:
            stored = {r["image_id"] for r in csv.DictReader(f, delimiter="\t")}
        self.assertEqual(stored, {"/src/patient_smith/a.dcm", "/src/patient_jones/b.dcm"})

    def test_reviewer_at_limits_is_accepted(self):
        for reviewer in ("r", "r" * 64, "Dr. Émilie O'Neil"):
            with self.subTest(reviewer=reviewer):
                resp, _ = self.post_mark(
                    {"keys": ["batch_001/a.jpg"], "status": "CLEAN", "pass": 1, "reviewer": reviewer, "mode": "grid"}
                )
                self.assertEqual(resp.status, 200)

    def test_prior_pass_dirty_is_flagged(self):
        self.post_mark(
            {"keys": ["batch_001/a.jpg"], "status": "DIRTY", "pass": 1, "reviewer": "tester", "mode": "single"}
        )
        self.assertEqual(self.get_json("/statuses?pass=2")["batch_001/a.jpg"], "FLAGGED")

    def test_bad_bodies_are_400(self):
        good = {"keys": ["batch_001/a.jpg"], "status": "DIRTY", "pass": 1, "reviewer": "tester", "mode": "single"}
        bad = {
            "status UNREVIEWED": {**good, "status": "UNREVIEWED"},
            "status FLAGGED": {**good, "status": "FLAGGED"},
            "keys not a list": {**good, "keys": "batch_001/a.jpg"},
            "empty keys": {**good, "keys": []},
            "non-str key": {**good, "keys": [1]},
            "unknown key": {**good, "keys": ["batch_001/zzz.jpg"]},
            "reviewer with tab": {**good, "reviewer": "a\tb"},
            "reviewer with newline": {**good, "reviewer": "a\nb"},
            "reviewer with control char": {**good, "reviewer": "a\x1bb"},
            "reviewer empty": {**good, "reviewer": ""},
            "reviewer only spaces": {**good, "reviewer": "  "},
            "reviewer too long": {**good, "reviewer": "r" * 65},
            "reviewer not a string": {**good, "reviewer": 7},
            "missing reviewer": {k: v for k, v in good.items() if k != "reviewer"},
            "bad mode": {**good, "mode": "undo"},
            "mode not a string": {**good, "mode": 1},
            "missing mode": {k: v for k, v in good.items() if k != "mode"},
            "pass not int": {**good, "pass": "1"},
            "pass bool": {**good, "pass": True},
            "pass zero": {**good, "pass": 0},
            "missing field": {k: v for k, v in good.items() if k != "status"},
            "not an object": [good],
            "invalid json": b"{not json",
        }
        for name, payload in bad.items():
            with self.subTest(name):
                resp, _ = self.post_mark(payload)
                self.assertEqual(resp.status, 400)
        self.assertEqual(set(self.get_json("/statuses?pass=1").values()), {"UNREVIEWED"})

    def test_oversized_body_is_400(self):
        # Declared length only: the server must reject before reading the body.
        resp, _, _ = self.request("POST", "/mark", headers={"Content-Length": str((1 << 20) + 1)})
        self.assertEqual(resp.status, 400)


class TestGridCleanRefusal(ServerTestCase):
    """/mark refuses a grid CLEAN over a DIRTY or FLAGGED key (status.grid_clean_refused): defence in
    depth behind the clients' own check, which stale statuses can defeat."""

    def mark(self, keys, status, mode="grid", pass_number=1):
        return self.post_mark({"keys": keys, "status": status, "pass": pass_number, "reviewer": "r", "mode": mode})

    def recorded(self) -> list[tuple[str, str]]:
        with open(self.work_dir / "review.tsv", newline="") as f:
            return [(r["image_id"], r["status"]) for r in csv.DictReader(f, delimiter="\t")]

    def test_grid_clean_over_dirty_key_is_409_and_records_nothing(self):
        self.assertEqual(self.mark(["batch_001/a.jpg"], "DIRTY", "single")[0].status, 200)
        before = self.recorded()
        with self.assertLogs(SERVER_LOGGER, "INFO") as logs:
            resp, data = self.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "CLEAN")
        self.assertEqual(resp.status, 409)
        self.assertEqual(json.loads(data), {"error": "grid holds a DIRTY or FLAGGED image"})
        self.assertEqual(self.recorded(), before)
        self.assertEqual(self.get_json("/statuses?pass=1")["batch_001/b.jpg"], "UNREVIEWED")
        for text in logs.output:
            self.assertNotIn("batch_001", text)

    def test_grid_clean_over_flagged_key_is_409(self):
        self.mark(["batch_001/a.jpg"], "DIRTY", "single")
        self.mark(["batch_001/b.jpg", "batch_002/c.jpg", "batch_002/d.jpg"], "CLEAN")
        before = self.recorded()
        self.assertEqual(self.get_json("/statuses?pass=2")["batch_001/a.jpg"], "FLAGGED")
        resp, _ = self.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "CLEAN", pass_number=2)
        self.assertEqual(resp.status, 409)
        self.assertEqual(self.recorded(), before)

    def test_check_uses_the_requests_pass(self):
        keys = ["batch_001/a.jpg", "batch_001/b.jpg"]
        self.mark(keys, "DIRTY")
        before = self.recorded()
        resp, _ = self.mark(keys, "CLEAN", pass_number=2)  # FLAGGED in pass 2
        self.assertEqual(resp.status, 409)
        self.assertEqual(self.recorded(), before)
        resp, _ = self.mark(keys, "CLEAN", pass_number=1)  # all DIRTY in pass 1: a reversal
        self.assertEqual(resp.status, 200)

    def test_check_runs_under_the_store_lock(self):
        lock = self.server.store_lock
        held = []
        statuses = self.server.store.statuses

        def recording_statuses(pass_number):
            held.append(lock.locked())
            return statuses(pass_number)

        with mock.patch.object(self.server.store, "statuses", side_effect=recording_statuses):
            resp, _ = self.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "CLEAN")
        self.assertEqual(resp.status, 200)
        self.assertEqual(held, [True])

    def test_409_keeps_the_connection_open(self):
        self.mark(["batch_001/a.jpg"], "DIRTY", "single")
        body = {"keys": ["batch_001/a.jpg", "batch_001/b.jpg"], "status": "CLEAN", "pass": 1, "reviewer": "r"}
        resp, _, conn = self.request("POST", "/mark", body=json.dumps({**body, "mode": "grid"}).encode())
        self.assertEqual(resp.status, 409)
        self.assertIsNone(resp.getheader("Connection"))
        conn.request("GET", "/current_pass", headers={"Authorization": f"Bearer {self.target.token}"})
        again = conn.getresponse()
        self.assertEqual((again.status, json.loads(again.read())), (200, {"pass": 1}))

    def test_all_dirty_grid_may_be_marked_clean(self):
        keys = ["batch_001/a.jpg", "batch_001/b.jpg"]
        self.mark(keys, "DIRTY")
        resp, data = self.mark(keys, "CLEAN")
        self.assertEqual((resp.status, json.loads(data)), (200, dict.fromkeys(keys, "CLEAN")))

    def test_single_clean_on_flagged_key_is_unchanged(self):
        # FLAGGED in pass 2: refused as a grid, accepted as a single
        self.mark(["batch_001/a.jpg"], "DIRTY", "single")
        self.assertEqual(self.mark(["batch_001/a.jpg"], "CLEAN", "grid", pass_number=2)[0].status, 409)
        resp, data = self.mark(["batch_001/a.jpg"], "CLEAN", "single", pass_number=2)
        self.assertEqual((resp.status, json.loads(data)), (200, {"batch_001/a.jpg": "CLEAN"}))

    def test_grid_dirty_over_dirty_key_is_allowed(self):
        self.mark(["batch_001/a.jpg"], "DIRTY", "single")
        resp, data = self.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "DIRTY")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data), {"batch_001/a.jpg": "DIRTY", "batch_001/b.jpg": "DIRTY"})


class TestUndo(ServerTestCase):
    def test_undo_round_trip(self):
        self.post_mark(
            {"keys": ["batch_001/a.jpg"], "status": "DIRTY", "pass": 1, "reviewer": "tester", "mode": "single"}
        )
        self.post_mark(
            {
                "keys": ["batch_001/a.jpg", "batch_002/c.jpg"],
                "status": "CLEAN",
                "pass": 1,
                "reviewer": "tester",
                "mode": "single",
            }
        )
        resp, data = self.post_undo({"pass": 1, "reviewer": "tester"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data), {"batch_001/a.jpg": "DIRTY", "batch_002/c.jpg": "UNREVIEWED"})
        for _, _, image_id in ROWS:
            self.assertNotIn(image_id.encode(), data)
        statuses = self.get_json("/statuses?pass=1")
        self.assertEqual((statuses["batch_001/a.jpg"], statuses["batch_002/c.jpg"]), ("DIRTY", "UNREVIEWED"))
        self.assertEqual(
            json.loads(self.post_undo({"pass": 2, "reviewer": "tester"})[1]), {"batch_001/a.jpg": "UNREVIEWED"}
        )
        resp, data = self.post_undo({"pass": 1, "reviewer": "tester"})
        self.assertEqual((resp.status, json.loads(data)), (200, {}))

    def test_bad_bodies_are_400(self):
        self.post_mark(
            {"keys": ["batch_001/a.jpg"], "status": "DIRTY", "pass": 1, "reviewer": "tester", "mode": "single"}
        )
        good = {"pass": 1, "reviewer": "tester"}
        bad = {
            "missing pass": {"reviewer": "tester"},
            "pass zero": {**good, "pass": 0},
            "pass not int": {**good, "pass": "1"},
            "pass bool": {**good, "pass": True},
            "missing reviewer": {"pass": 1},
            "reviewer with tab": {**good, "reviewer": "a\tb"},
            "reviewer empty": {**good, "reviewer": ""},
            "reviewer too long": {**good, "reviewer": "r" * 65},
            "reviewer not a string": {**good, "reviewer": 7},
            "not an object": [good],
            "invalid json": b"{not json",
        }
        for name, payload in bad.items():
            with self.subTest(name):
                resp, _ = self.post_undo(payload)
                self.assertEqual(resp.status, 400)
        self.assertEqual(self.get_json("/statuses?pass=1")["batch_001/a.jpg"], "DIRTY")

    def test_undo_without_body_is_400(self):
        resp, _, _ = self.request("POST", "/undo")
        self.assertEqual(resp.status, 400)

    def test_get_undo_is_404(self):
        resp, _, _ = self.request("GET", "/undo")
        self.assertEqual(resp.status, 404)


class TestRemoteTarget(unittest.TestCase):
    def test_round_trip(self):
        for host in ("node01.cluster.example", "node_01", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                t = RemoteTarget(host, 8443, "tok-en_1", FP)
                self.assertEqual(RemoteTarget.parse(t.to_uri()), t)
        self.assertEqual(RemoteTarget("h", 1, "t", FP).to_uri(), f"ir://h:1/?token=t&fp=sha256:{FP}")

    def test_token_not_in_repr(self):
        self.assertNotIn("tok", repr(RemoteTarget("h", 1, "tok", FP)))

    def test_parse_rejections(self):
        bad = [
            f"http://h:1/?token=t&fp=sha256:{FP}",
            f"ir://h/?token=t&fp=sha256:{FP}",
            f"ir://:1/?token=t&fp=sha256:{FP}",
            f"ir://h:1/?fp=sha256:{FP}",
            f"ir://h:1/?token=&fp=sha256:{FP}",
            "ir://h:1/?token=t",
            f"ir://h:1/?token=t&fp={FP}",
            "ir://h:1/?token=t&fp=sha256:abcd",
            f"ir://h:1/?token=t&fp=sha256:{FP.upper()}",
            f"ir://h:notaport/?token=t&fp=sha256:{FP}",
            f"ir://h:0/?token=t&fp=sha256:{FP}",
            f"ir://h:70000/?token=t&fp=sha256:{FP}",
            f"ir://u:p@h:1/?token=t&fp=sha256:{FP}",
            f"ir://u@h:1/?token=t&fp=sha256:{FP}",
            f"ir://h:1/x?token=t&fp=sha256:{FP}",
            f"ir://h:1/?token=t&fp=sha256:{FP}#frag",
            f"ir://h:1/?token=t&fp=sha256:{FP}&extra=1",
            f"ir://h:1/?token=t&token=u&fp=sha256:{FP}",
            f"ir://h:1/?token=a%20b&fp=sha256:{FP}",
            f"ir://h:1/?token=a.b&fp=sha256:{FP}",
            f"ir://-h:1/?token=t&fp=sha256:{FP}",
            f"ir://h%41:1/?token=t&fp=sha256:{FP}",
            f"ir://h-:1/?token=t&fp=sha256:{FP}",
            f"ir://1.2.3.400:1/?token=t&fp=sha256:{FP}",
            f"ir://[::zz]:1/?token=t&fp=sha256:{FP}",
            f"ir://[fe80::1%25eth0]:1/?token=t&fp=sha256:{FP}",
            f"ir://h..x:1/?token=t&fp=sha256:{FP}",
            "",
        ]
        for uri in bad:
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                RemoteTarget.parse(uri)


if __name__ == "__main__":
    unittest.main()
