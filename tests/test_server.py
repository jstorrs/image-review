import contextlib
import csv
import http.client
import io
import json
import logging
import select
import socket
import time
import unittest
from pathlib import Path
from unittest import mock

from image_review.cli import PACKAGE_LOGGER, LogFormatter
from image_review.connection import MAX_PASS, is_int_at_least, package_version
from image_review.review_db import ReviewLogChanged
from image_review.server import INSTANCE_HEADER, ReviewHandler, ReviewServer
from image_review.store import load_manifest
from tests.fixtures import (
    HAS_AF_UNIX,
    ROWS,
    UnixHTTPConnection,
    make_work_dir,
    socket_dir,
    start_unix_server,
    temp_dir,
)

SERVER_LOGGER = f"{PACKAGE_LOGGER}.server"


@unittest.skipUnless(HAS_AF_UNIX, "needs AF_UNIX")
class ServerTestCase(unittest.TestCase):
    """The handler tests, over the Unix socket server that serve uses.

    Raw requests send Host: localhost, as the socket handler answers any other Host with 400.
    """

    HASHED = False  # manifest.tsv with the hash columns

    def setUp(self):
        self.work_dir = temp_dir(self)
        make_work_dir(self.work_dir, hashed=self.HASHED)
        self.path = socket_dir(self) / "ir.sock"
        self.server, self.token, stop = start_unix_server(self.work_dir, self.path)
        self.addCleanup(stop)

    def request(self, method, path, body=None, token="default", headers=None):
        """One request with Host localhost:8080 and this server's instance, which the socket handler requires.

        Headers and body go in one write (http.client makes two): a server that replies early and closes, as on a
        401, would otherwise fail the body's write with EPIPE. The connection serves further conn.request calls.
        """
        token = self.token if token == "default" else token
        hdrs = {"Host": "localhost:8080", INSTANCE_HEADER: self.server.instance, **(headers or {})}
        if token is not None:
            hdrs["Authorization"] = f"Bearer {token}"
        if not any(k.lower() == "content-length" for k in hdrs):
            if body is not None:
                hdrs["Content-Length"] = str(len(body))
            elif method in ("POST", "PUT"):
                hdrs["Content-Length"] = "0"  # as http.client sends for a bodyless POST or PUT
        conn = UnixHTTPConnection(self.path)
        self.addCleanup(conn.close)
        conn.sock = self.connect()
        head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
        conn.sock.sendall(head.encode() + (body or b""))
        resp = conn.response_class(conn.sock, method=method)
        resp.begin()
        if resp.will_close:
            conn.close()  # as getresponse does, so the next conn.request reconnects
        data = resp.read()
        return resp, data, conn

    def connect(self) -> socket.socket:
        """A connected socket. While the listen backlog is full AF_UNIX refuses at once, with EAGAIN on Linux and
        ECONNREFUSED on macOS; like TCP, wait."""
        deadline = time.monotonic() + 10
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(sock.close)
            sock.settimeout(10)
            try:
                sock.connect(str(self.path))
            except (BlockingIOError, ConnectionRefusedError):
                sock.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
                continue
            return sock

    def raw(self, data: bytes) -> bytes:
        """Send raw bytes over the socket and read until the server closes."""
        sock = self.connect()
        sock.sendall(data)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
        return b"".join(chunks)

    def auth_headers(self) -> dict[str, str]:
        """The token and instance, for a further request on a connection from request()."""
        return {"Authorization": f"Bearer {self.token}", INSTANCE_HEADER: self.server.instance}

    def auth_header(self) -> str:
        """The token and instance lines of a raw request; it also needs Host: localhost."""
        return f"Authorization: Bearer {self.token}\r\n{INSTANCE_HEADER}: {self.server.instance}\r\n"

    def get_json(self, path):
        resp, data, _ = self.request("GET", path)
        self.assertEqual(resp.status, 200)
        return json.loads(data)

    def post_mark(self, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return self.request("POST", "/mark", body=body)[:2]

    def mark(self, keys, status, mode="grid", pass_number=1):
        return self.post_mark({"keys": keys, "status": status, "pass": pass_number, "reviewer": "r", "mode": mode})

    def recorded(self) -> list[tuple[str, str]]:
        with open(self.work_dir / "review.tsv", newline="") as f:
            return [(r["image_id"], r["status"]) for r in csv.DictReader(f, delimiter="\t")]

    def post_undo(self, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return self.request("POST", "/undo", body=body)[:2]


class TestTransport(ServerTestCase):
    def test_security_headers_and_persistent_connection(self):
        resp, _, conn = self.request("GET", "/current_pass")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        conn.request("GET", "/current_pass", headers=self.auth_headers())
        self.assertEqual(conn.getresponse().status, 200)


class TestConnectionHandling(ServerTestCase):
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
                conn.request("GET", "/current_pass", headers=self.auth_headers())
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
                f"POST /mark HTTP/1.1\r\nHost: localhost\r\n{self.auth_header()}Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
            ).encode()
        )
        self.assertTrue(data.startswith(b"HTTP/1.1 400"))
        self.assertIn(b"Connection: close", data)

    def test_post_without_content_length_rejected(self):
        data = self.raw(f"POST /mark HTTP/1.1\r\nHost: localhost\r\n{self.auth_header()}\r\n".encode())
        self.assertTrue(data.startswith(b"HTTP/1.1 400"), data[:60])
        self.assertIn(b"Connection: close", data)

    def test_body_on_bodyless_route_rejected(self):
        resp, _, _ = self.request("GET", "/manifest", body=b"abc")
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.getheader("Connection"), "close")

    def test_duplicate_content_length_on_get_rejected(self):
        data = self.raw(
            (
                f"GET /manifest HTTP/1.1\r\nHost: localhost\r\n{self.auth_header()}Content-Length: 0\r\nContent-Length: 5\r\n\r\n"
            ).encode()
        )
        self.assertTrue(data.startswith(b"HTTP/1.1 400"))

    def test_duplicate_content_length_on_mark_rejected(self):
        data = self.raw(
            (
                f"POST /mark HTTP/1.1\r\nHost: localhost\r\n{self.auth_header()}Content-Length: 2\r\nContent-Length: 2\r\n\r\n{{}}"
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
        conn.request("GET", "/current_pass", headers=self.auth_headers())
        self.assertEqual(conn.getresponse().status, 200)

    def test_store_timeout_is_500_not_a_silent_close(self):
        # A store's own TimeoutError (e.g. ETIMEDOUT on NFS) is not a request read cut off
        with (
            self.assertLogs(SERVER_LOGGER, "INFO") as logs,
            mock.patch.object(self.server.store, "current_pass", side_effect=TimeoutError("secret-detail")),
        ):
            resp, data, _ = self.request("GET", "/current_pass")
        self.assertEqual((resp.status, data), (500, b""))
        errors = [r.getMessage() for r in logs.records if r.levelno == logging.ERROR]
        self.assertEqual(errors, ["internal error: TimeoutError"])

    def test_review_log_changed_is_500_naming_the_class(self):
        body = json.dumps(
            {"keys": ["batch_001/a.jpg"], "status": "CLEAN", "pass": 1, "reviewer": "tester", "mode": "single"}
        ).encode()
        with (
            self.assertLogs(SERVER_LOGGER, "INFO") as logs,
            mock.patch.object(self.server.store, "mark", side_effect=ReviewLogChanged("review.tsv changed")),
        ):
            resp, data, _ = self.request("POST", "/mark", body=body)
        self.assertEqual((resp.status, data), (500, b""))
        errors = [r.getMessage() for r in logs.records if r.levelno == logging.ERROR]
        self.assertEqual(errors, ["internal error: ReviewLogChanged"])

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
            self.raw(b"GET /a\x1b[31mb?key=SECRETKEY HTTP/1.1\r\nHost: localhost\r\n\r\n")
        messages, formatted = self.logged(logs)
        self.assertEqual(messages, ["unix GET /a\\x1b[31mb 401"])  # AF_UNIX has no peer address
        for text in (*messages, formatted):
            self.assertNotIn("\x1b", text)
            self.assertNotIn("SECRETKEY", text)
            self.assertNotIn("?", text)

    def test_request_line_format(self):
        with self.assertLogs(SERVER_LOGGER, "INFO") as logs:
            self.request("GET", "/current_pass")
        (record,) = logs.records
        self.assertEqual(record.levelno, logging.INFO)
        self.assertEqual(record.getMessage(), "unix GET /current_pass 200")  # AF_UNIX has no peer address
        _, formatted = self.logged(logs)
        self.assertRegex(
            formatted,
            r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d INFO image_review.server: unix GET /current_pass 200$",
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
            [  # AF_UNIX has no peer address
                "unix GET /image 200",
                "unix GET /image 401",
                "unix GET /image 404",
                "unix GET /statuses 400",
                "unix POST /mark 400",
            ],
        )
        for text in (*messages, formatted):
            self.assertNotIn(self.token, text)
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
            raise ConnectionResetError("secret-detail")
        except ConnectionResetError:
            with self.assertLogs(SERVER_LOGGER, "WARNING") as logs:
                self.server.handle_error(None, ("127.0.0.1", 1))
        (record,) = logs.records
        self.assertEqual(
            (record.levelno, record.getMessage()), (logging.WARNING, "connection error: ConnectionResetError")
        )


class TestPreAuthIdleSockets(ServerTestCase):
    def test_idle_raw_sockets_do_not_block_real_client(self):
        for _ in range(40):
            self.connect()
        start = time.perf_counter()
        resp, data, _ = self.request("GET", "/current_pass")
        self.assertEqual((resp.status, data), (200, b'{"pass": 1}'))
        # Blocked behind the idle sockets, the request would wait until they time out: 60 s, the shorter of the
        # request deadline and the per-receive timeout (no handshake here).
        self.assertLess(time.perf_counter() - start, 5)


def read_until_closed(sock: socket.socket, limit: float) -> bytes:
    """Everything the server sends before it closes; fails (TimeoutError) if it is still open after `limit` s."""
    sock.settimeout(limit)
    chunks = []
    try:
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    except (BrokenPipeError, ConnectionResetError):  # closed with our bytes unread
        pass
    return b"".join(chunks)


def trickle_until_closed(sock: socket.socket, data: bytes, limit: float) -> tuple[float, int]:
    """Send `data` a byte every 0.1 s, far inside the per-receive timeout, until the server closes.

    Returns the seconds taken and the bytes sent.
    """
    start = time.monotonic()
    for sent, byte in enumerate(data):
        if time.monotonic() - start > limit:
            break
        try:
            sock.sendall(bytes([byte]))
        except (BrokenPipeError, ConnectionResetError):
            return time.monotonic() - start, sent
        if not select.select([sock], [], [], 0.1)[0]:
            continue
        try:
            reply = sock.recv(65536)
        except ConnectionResetError:
            reply = b""
        if reply != b"":
            raise AssertionError("server replied to a trickled request")
        return time.monotonic() - start, sent + 1
    raise AssertionError(f"server still open after {limit} s of trickling")


class TestConnectionLimit(ServerTestCase):
    CAP = 3

    def setUp(self):
        patcher = mock.patch.object(ReviewServer, "max_connections", self.CAP)  # read when the server is made
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def hold(self) -> socket.socket:
        """A connection waiting idle for its request line: it holds a slot."""
        return self.connect()

    def assert_refused(self):
        start = time.monotonic()
        with self.connect() as sock:
            # Given a thread it would wait 60 s for its request line (the shorter of the request deadline and the
            # per-receive timeout); refused, it closes at once
            self.assertEqual(read_until_closed(sock, 5), b"")
        self.assertLess(time.monotonic() - start, 5)

    def get_when_free(self) -> None:
        """GET /current_pass, retried while the server is still full; the connection then holds a slot."""
        deadline = time.monotonic() + 10
        while True:
            try:
                resp, data, _ = self.request("GET", "/current_pass")
            except (OSError, http.client.HTTPException):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
                continue
            self.assertEqual((resp.status, data), (200, b'{"pass": 1}'))
            return

    def limit_warnings(self, logs) -> list[str]:
        return [r.getMessage() for r in logs.records if "connection limit" in r.getMessage()]

    def test_full_server_refuses_at_accept_and_warns_once(self):
        held = [self.hold() for _ in range(self.CAP)]
        with self.assertLogs(SERVER_LOGGER, "WARNING") as logs:
            for _ in range(3):
                self.assert_refused()
        self.assertEqual(
            [r.getMessage() for r in logs.records],
            ["connection limit (3) reached; refusing connections until one closes"],
        )
        held[0].close()  # its thread ends and frees the slot
        self.get_when_free()  # keeps its connection, so the server is full again
        with self.assertLogs(SERVER_LOGGER, "WARNING") as logs:
            self.assert_refused()
            self.assert_refused()
        self.assertEqual(len(self.limit_warnings(logs)), 1)  # warned again: it filled again


class TestRequestDeadline(ServerTestCase):
    DEADLINE = 1.0

    def setUp(self):
        patcher = mock.patch.object(ReviewHandler, "request_deadline", self.DEADLINE)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_trickled_headers_cut_off_at_the_deadline(self):
        sock = self.connect()
        sock.sendall(b"GET /current_pass HTTP/1.1\r\nHost: localhost\r\n")
        elapsed, sent = trickle_until_closed(sock, b"X-Pad: " + b"a" * 1000, limit=10 * self.DEADLINE)
        self.assertGreater(elapsed, self.DEADLINE / 2)
        self.assertGreater(sent, 3)  # trickled for a while, not closed as idle

    def test_slow_body_closed_without_reply_or_error_log(self):
        sock = self.connect()
        head = f"POST /mark HTTP/1.1\r\nHost: localhost\r\n{self.auth_header()}Content-Length: 100\r\n\r\n"
        with self.assertNoLogs(SERVER_LOGGER, "ERROR"):
            sock.sendall(head.encode() + b'{"keys": ')  # the rest of the body never comes
            self.assertEqual(read_until_closed(sock, 10 * self.DEADLINE), b"")
        self.get_json("/current_pass")  # the server carries on

    def test_keep_alive_requests_each_get_the_deadline(self):
        deadline = 2.0  # longer than the class's, for headroom on slow runners
        conn = UnixHTTPConnection(self.path)
        self.addCleanup(conn.close)
        headers = self.auth_headers()
        with mock.patch.object(ReviewHandler, "request_deadline", deadline):
            for _ in range(4):  # together longer than one deadline; each request alone well within it
                conn.request("GET", "/current_pass", headers=headers)
                self.assertEqual(conn.getresponse().read(), b'{"pass": 1}')
                time.sleep(deadline / 3)

    def test_idle_keep_alive_connection_closed_after_the_deadline(self):
        _, _, conn = self.request("GET", "/current_pass")
        start = time.monotonic()
        self.assertEqual(read_until_closed(conn.sock, 10 * self.DEADLINE), b"")
        self.assertGreater(time.monotonic() - start, self.DEADLINE / 2)


class TestRequestBudget(ServerTestCase):
    MARK = json.dumps(
        {"keys": ["batch_001/a.jpg"], "status": "CLEAN", "pass": 1, "reviewer": "tester", "mode": "single"}
    ).encode()

    def send(self, head: str, body: bytes = b"") -> bytes:
        """Send one request (with `Connection: close`) and read until the server closes."""
        sock = self.connect()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):  # it may close mid-send
            sock.sendall(f"{head}Connection: close\r\n\r\n".encode() + body)
        return read_until_closed(sock, 10)

    def test_headers_over_the_budget_closed_without_reply(self):
        pad = "".join(f"X-Pad-{i}: {'a' * 30000}\r\n" for i in range(3))  # each line and the count within stdlib limits
        with self.assertNoLogs(SERVER_LOGGER, "ERROR"):
            self.assertEqual(self.send(f"GET /current_pass HTTP/1.1\r\nHost: localhost\r\n{pad}"), b"")
        self.get_json("/current_pass")  # the server carries on

    def test_body_beyond_the_head_budget_is_read_after_the_token(self):
        # Headers near the budget, then a body (padded JSON) larger than it: the Content-Length raises the budget
        body = self.MARK + b" " * 100_000
        head = (
            f"POST /mark HTTP/1.1\r\nHost: localhost\r\n{self.auth_header()}X-Pad: {'a' * 60000}\r\n"
            f"Content-Length: {len(body)}\r\n"
        )
        reply = self.send(head, body)
        self.assertTrue(reply.startswith(b"HTTP/1.1 200 "), reply[:60])
        self.assertEqual(json.loads(reply.partition(b"\r\n\r\n")[2]), {"batch_001/a.jpg": "CLEAN"})


class TestAuth(ServerTestCase):
    def test_missing_and_wrong_token_rejected_everywhere(self):
        requests = [
            ("GET", "/manifest"),
            ("GET", "/image?key=batch_001/a.jpg"),
            ("GET", "/statuses?pass=1"),
            ("GET", "/current_pass"),
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

    def test_statuses_and_current_pass(self):
        self.assertEqual(self.get_json("/current_pass"), {"pass": 1})
        statuses = self.get_json("/statuses?pass=1")
        self.assertEqual(statuses, {key: "UNREVIEWED" for _, key, _ in ROWS})

    def test_statuses_bad_pass_is_400(self):
        for path in (
            "/statuses",
            "/statuses?pass=x",
            "/statuses?pass=0",
            "/statuses?pass=-1",
            f"/statuses?pass={MAX_PASS + 1}",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status, 400)

    def test_pass_maximum_is_accepted(self):
        self.assertEqual(self.request("GET", f"/statuses?pass={MAX_PASS}")[0].status, 200)
        resp, _ = self.post_mark(
            {"keys": ["batch_001/a.jpg"], "status": "DIRTY", "pass": MAX_PASS, "reviewer": "tester", "mode": "single"}
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.post_undo({"pass": MAX_PASS, "reviewer": "tester"})[0].status, 200)
        self.assertEqual(self.get_json("/current_pass"), {"pass": 1})

    def test_current_pass_answers_at_the_maximum_pass_when_every_key_is_marked_there(self):
        keys = [key for _, key, _ in ROWS]
        resp, _ = self.mark(keys, "CLEAN", mode="grid", pass_number=MAX_PASS)
        self.assertEqual(resp.status, 200)
        resp, data, _ = self.request("GET", "/current_pass")
        self.assertEqual((resp.status, json.loads(data)), (200, {"pass": MAX_PASS}))

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
                "mode": "grid",
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
            "pass above the maximum": {**good, "pass": MAX_PASS + 1},
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
        mark_ = self.server.store.mark

        def recording_mark(*args, **kwargs):
            held.append(lock.locked())
            return mark_(*args, **kwargs)

        with mock.patch.object(self.server.store, "mark", side_effect=recording_mark):
            resp, _ = self.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "CLEAN")
        self.assertEqual(resp.status, 200)
        self.assertEqual(held, [True])

    def test_409_keeps_the_connection_open(self):
        self.mark(["batch_001/a.jpg"], "DIRTY", "single")
        body = {"keys": ["batch_001/a.jpg", "batch_001/b.jpg"], "status": "CLEAN", "pass": 1, "reviewer": "r"}
        resp, _, conn = self.request("POST", "/mark", body=json.dumps({**body, "mode": "grid"}).encode())
        self.assertEqual(resp.status, 409)
        self.assertIsNone(resp.getheader("Connection"))
        conn.request("GET", "/current_pass", headers=self.auth_headers())
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


class TestMarkKeyCount(ServerTestCase):
    """A single mark names exactly one key and no mark repeats a key: a multi-key "single" CLEAN would
    otherwise skip the grid CLEAN refusal."""

    def test_two_key_single_is_400_and_records_nothing(self):
        self.assertEqual(self.mark(["batch_001/a.jpg"], "DIRTY", "single")[0].status, 200)
        before = self.recorded()
        resp, _ = self.mark(["batch_001/a.jpg", "batch_001/b.jpg"], "CLEAN", "single")
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.recorded(), before)
        self.assertEqual(self.get_json("/statuses?pass=1")["batch_001/a.jpg"], "DIRTY")

    def test_repeated_key_is_400_in_either_mode(self):
        for mode in ("single", "grid"):
            with self.subTest(mode=mode):
                resp, _ = self.mark(["batch_001/a.jpg", "batch_001/a.jpg"], "DIRTY", mode)
                self.assertEqual(resp.status, 400)
                self.assertEqual(self.get_json("/statuses?pass=1")["batch_001/a.jpg"], "UNREVIEWED")

    def test_one_key_single_is_200(self):
        resp, data = self.mark(["batch_001/a.jpg"], "CLEAN", "single")
        self.assertEqual((resp.status, json.loads(data)), (200, {"batch_001/a.jpg": "CLEAN"}))


class TestUndo(ServerTestCase):
    def test_undo_round_trip(self):
        self.post_mark(
            {"keys": ["batch_001/a.jpg"], "status": "CLEAN", "pass": 1, "reviewer": "tester", "mode": "single"}
        )
        self.post_mark(
            {
                "keys": ["batch_001/a.jpg", "batch_002/c.jpg"],
                "status": "DIRTY",
                "pass": 1,
                "reviewer": "tester",
                "mode": "grid",
            }
        )
        resp, data = self.post_undo({"pass": 1, "reviewer": "tester"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data), {"batch_001/a.jpg": "CLEAN", "batch_002/c.jpg": "UNREVIEWED"})
        for _, _, image_id in ROWS:
            self.assertNotIn(image_id.encode(), data)
        statuses = self.get_json("/statuses?pass=1")
        self.assertEqual((statuses["batch_001/a.jpg"], statuses["batch_002/c.jpg"]), ("CLEAN", "UNREVIEWED"))
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
            "pass above the maximum": {**good, "pass": MAX_PASS + 1},
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


class TestIsIntAtLeast(unittest.TestCase):
    def test_table(self):
        for value, minimum, expected in [
            (True, 0, False),
            (True, 1, False),
            (False, 0, False),
            (0, 0, True),
            (0, 1, False),
            (1, 0, True),
            (1, 1, True),
            (-1, 0, False),
            (-1, 1, False),
            (1.0, 0, False),
            (1.0, 1, False),
            ("1", 0, False),
            ("1", 1, False),
            (None, 0, False),
            (None, 1, False),
        ]:
            with self.subTest(value=value, minimum=minimum):
                self.assertIs(is_int_at_least(value, minimum), expected)


class TestPackageVersion(unittest.TestCase):
    def setUp(self):
        package_version.cache_clear()
        self.addCleanup(package_version.cache_clear)

    def test_installed_version(self):
        from importlib.metadata import version

        self.assertEqual(package_version(), version("image-review"))

    def test_unknown_when_not_installed(self):
        from importlib.metadata import PackageNotFoundError

        with mock.patch("importlib.metadata.version", side_effect=PackageNotFoundError):
            self.assertEqual(package_version(), "unknown")


if __name__ == "__main__":
    unittest.main()
