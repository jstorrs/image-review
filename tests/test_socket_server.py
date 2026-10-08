import getpass
import io
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import image_review
from image_review import cli
from image_review.cli import PACKAGE_LOGGER, browser_url, ssh_forward_command
from image_review.connection import parse_token
from image_review.server import (
    INSTANCE_HEADER,
    ReviewServer,
    clear_stale_socket,
    default_socket_path,
    is_local_host,
    load_assets,
    make_unix_server,
    parse_socket_path,
    write_private_file,
)
from image_review.store import LocalStore
from tests.fixtures import (
    CLEAN_ENV,
    HAS_AF_UNIX,
    UnixHTTPConnection,
    invoke_cli,
    make_work_dir,
    socket_dir,
    start_unix_server,
    temp_dir,
)

WEB_DIR = Path(image_review.__file__).parent / "web"
ASSETS = [
    ("/", "index.html", "text/html; charset=utf-8"),
    ("/app.js", "app.js", "text/javascript; charset=utf-8"),
    ("/app.css", "app.css", "text/css; charset=utf-8"),
]
SERVER_LOGGER = f"{PACKAGE_LOGGER}.server"

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

    def request(self, method, path, body=None, token="default", host="localhost:8080", instance="default"):
        """One request through http.client, with `host` sent exactly as given (http.client would rewrite "::1").

        `instance` is the X-Review-Instance sent: this server's by default, None for none.
        """
        token = self.token if token == "default" else token
        instance = self.server.instance if instance == "default" else instance
        conn = UnixHTTPConnection(self.path)
        self.addCleanup(conn.close)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host)
        if token is not None:
            conn.putheader("Authorization", f"Bearer {token}")
        if instance is not None:
            conn.putheader(INSTANCE_HEADER, instance)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp, resp.read()

    def raw_request(
        self,
        method: str,
        path: str,
        token: str | None,
        host: str = "localhost",
        body: bytes = b"",
        instance: str | None = None,
    ) -> bytes:
        """Headers and body in one write, so a server that replies early and closes cannot cause EPIPE."""
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", f"Content-Length: {len(body)}"]
        if token is not None:
            lines.append(f"Authorization: Bearer {token}")
        if instance is not None:
            lines.append(f"{INSTANCE_HEADER}: {instance}")
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

    def test_grid_clean_over_dirty_key_is_409(self):
        def mark(keys, status, mode):
            body = {"keys": keys, "status": status, "pass": 1, "reviewer": "tester", "mode": mode}
            return self.request("POST", "/mark", body=json.dumps(body).encode())

        self.assertEqual(mark(["batch_001/a.jpg"], "DIRTY", "single")[0].status, 200)
        resp, data = mark(["batch_001/a.jpg", "batch_001/b.jpg"], "CLEAN", "grid")
        self.assertEqual((resp.status, json.loads(data)), (409, {"error": "grid holds a DIRTY or FLAGGED image"}))
        resp, data = self.request("GET", "/statuses?pass=1")
        self.assertEqual(json.loads(data)["batch_001/b.jpg"], "UNREVIEWED")

    def test_log_line_uses_unix_peer_and_hides_token(self):
        with self.assertLogs(SERVER_LOGGER, "INFO") as logs:
            self.request("GET", "/current_pass")
            self.request("GET", "/current_pass", host="evil.example")
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(messages, ["unix GET /current_pass 200", "unix GET /current_pass 400"])
        for text in [*messages, *logs.output]:
            self.assertNotIn(self.token, text)
            self.assertNotIn("Error", text)


# The API routes a page loaded from another serve is refused on (ROUTES less /version and /current_pass)
INSTANCE_ROUTES = [
    ("GET", "/manifest"),
    ("GET", "/image?key=batch_001/a.jpg"),
    ("GET", "/statuses?pass=1"),
    ("GET", "/skipped"),
    ("POST", "/mark"),
    ("POST", "/undo"),
    ("POST", "/grids"),
    ("GET", "/nope"),
    ("DELETE", "/manifest"),
]
MARK_A = {"keys": ["batch_001/a.jpg"], "status": "CLEAN", "pass": 1, "reviewer": "tester", "mode": "single"}
UNDO = {"pass": 1, "reviewer": "tester"}
GRIDS_ALL = {"keys": ["batch_001/a.jpg", "batch_001/b.jpg"], "width": 640, "height": 480, "rotation": "auto"}
STALE_BODY = {"error": "the page was loaded from another serve; reconnect"}


class TestServerInstance(SocketServerTestCase):
    def review_tsv(self) -> bytes:
        path = self.work_dir / "review.tsv"
        return path.read_bytes() if path.exists() else b""

    def test_every_response_names_the_instance(self):
        instance = self.server.instance
        self.assertGreaterEqual(len(instance), 16)
        cases = [
            self.request("GET", "/current_pass"),
            self.request("GET", "/manifest"),
            self.request("GET", "/manifest", instance=None),  # 412
            self.request("GET", "/current_pass", token=None),  # 401
            self.request("GET", "/current_pass", host="evil.example"),  # 400
            self.request("GET", "/app.js", token=None),
        ]
        for resp, _ in cases:
            with self.subTest(status=resp.status):
                self.assertEqual(resp.getheader(INSTANCE_HEADER), instance)
        reply = self.raw(b"FOO / HTTP/1.1\r\nHost: localhost\r\n\r\n")  # a stdlib error response
        self.assertIn(f"\r\n{INSTANCE_HEADER}: {instance}\r\n".encode(), reply)

    def test_each_start_gets_a_new_instance(self):
        other_dir = temp_dir(self)
        make_work_dir(other_dir)
        other, _, stop = start_unix_server(other_dir, socket_dir(self) / "other.sock")
        self.addCleanup(stop)
        self.assertNotEqual(other.instance, self.server.instance)

    def test_version_and_current_pass_need_no_instance(self):
        for instance in (None, "stale"):
            with self.subTest(instance=instance):
                resp, data = self.request("GET", "/current_pass", instance=instance)
                self.assertEqual((resp.status, json.loads(data)), (200, {"pass": 1}))
                resp, data = self.request("GET", "/version", instance=instance)
                self.assertEqual(resp.status, 200)
                self.assertIn("api", json.loads(data))

    def test_other_routes_without_the_instance_are_412(self):
        before = self.review_tsv()
        bodies = {"/mark": MARK_A, "/undo": UNDO, "/grids": GRIDS_ALL}
        wrong = [None, "", "stale", self.server.instance + "x", self.server.instance.upper()]
        with mock.patch.object(self.server.store, "image_bytes") as image_bytes:
            for (method, path), instance in itertools.product(INSTANCE_ROUTES, wrong):
                with self.subTest(method=method, path=path, instance=instance):
                    body = json.dumps(bodies[path]).encode() if path in bodies else b""
                    # One write: the 412 closes without reading a body, which http.client could hit with EPIPE
                    reply = self.raw_request(method, path, self.token, body=body, instance=instance)
                    head, _, data = reply.partition(b"\r\n\r\n")
                    self.assertTrue(head.startswith(b"HTTP/1.1 412 "), reply)
                    self.assertIn(b"\r\nConnection: close\r\n", head)
                    self.assertEqual(json.loads(data), STALE_BODY)
            image_bytes.assert_not_called()  # no image read, no grid packed
        self.assertEqual(self.review_tsv(), before)
        self.assertEqual(self.server.image_sizes, {})

    def test_a_repeated_instance_header_is_412(self):
        instance = self.server.instance
        auth = f"Authorization: Bearer {self.token}\r\n{INSTANCE_HEADER}: {instance}\r\n".encode()
        headers = b"GET /manifest HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n" + auth
        self.assertTrue(self.raw(headers + b"\r\n").startswith(b"HTTP/1.1 200 "))
        reply = self.raw(headers + f"{INSTANCE_HEADER}: {instance}\r\n\r\n".encode())
        self.assertTrue(reply.startswith(b"HTTP/1.1 412 "), reply)

    def test_mark_and_undo_from_a_stale_page_record_nothing(self):
        resp, _ = self.request("POST", "/mark", body=json.dumps(MARK_A).encode())
        self.assertEqual(resp.status, 200)
        before = self.review_tsv()
        for path, body in [("/mark", {**MARK_A, "status": "DIRTY"}), ("/undo", UNDO)]:
            with self.subTest(path=path):
                reply = self.raw_request("POST", path, self.token, body=json.dumps(body).encode(), instance="stale")
                self.assertTrue(reply.startswith(b"HTTP/1.1 412 "), reply)
        self.assertEqual(self.review_tsv(), before)
        resp, data = self.request("GET", "/statuses?pass=1")
        self.assertEqual(json.loads(data)["batch_001/a.jpg"], "CLEAN")

    def test_with_the_instance_every_route_answers_as_before(self):
        resp, data = self.request("GET", "/manifest")
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(data))
        self.assertEqual(self.request("GET", "/image?key=batch_001/a.jpg")[0].status, 200)
        self.assertEqual(self.request("GET", "/skipped")[0].status, 200)
        self.assertEqual(self.request("POST", "/grids", body=json.dumps(GRIDS_ALL).encode())[0].status, 200)
        self.assertEqual(self.request("GET", "/nope")[0].status, 404)

    def test_without_the_token_it_is_401_whatever_the_instance(self):
        for (method, path), token, instance in itertools.product(
            INSTANCE_ROUTES, (None, "wrong"), (None, "stale", self.server.instance)
        ):
            with self.subTest(method=method, path=path, token=token, instance=instance):
                body = b"{}" if method == "POST" else b""
                reply = self.raw_request(method, path, token, body=body, instance=instance)
                head, _, data = reply.partition(b"\r\n\r\n")
                self.assertTrue(head.startswith(b"HTTP/1.1 401 "), reply)
                self.assertEqual(data, b"")

    def test_bad_host_is_400_before_the_instance(self):
        resp, data = self.request("GET", "/manifest", host="evil.example", instance=None)
        self.assertEqual((resp.status, data), (400, b""))

    def test_the_instance_is_never_logged(self):
        with self.assertLogs(SERVER_LOGGER, "INFO") as logs:
            self.request("GET", "/current_pass")
            self.request("GET", "/manifest", instance="stale")
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(messages, ["unix GET /current_pass 200", "unix GET /manifest 412"])
        for text in logs.output:
            self.assertNotIn(self.server.instance, text)


class TestPublicAssets(SocketServerTestCase):
    def test_assets_served_without_token(self):
        for path, name, content_type in ASSETS:
            with self.subTest(path=path):
                resp, data = self.request("GET", path, token=None)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.getheader("Content-Type"), content_type)
                self.assertEqual(resp.getheader("Cache-Control"), "no-store")
                self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
                self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
                self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))
                self.assertEqual(data, (WEB_DIR / name).read_bytes())

    def test_query_is_ignored(self):
        resp, data = self.request("GET", "/?x=1", token=None)
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, (WEB_DIR / "index.html").read_bytes())

    def test_everything_else_needs_the_token(self):
        for method, path in [("POST", "/"), ("HEAD", "/"), ("GET", "/index.html"), ("GET", "/favicon.ico")]:
            with self.subTest(method=method, path=path):
                head, _, data = self.raw_request(method, path, None).partition(b"\r\n\r\n")
                self.assertTrue(head.startswith(b"HTTP/1.1 401 "), head)
                self.assertEqual(data, b"")

    def test_odd_targets_need_the_token(self):
        for target in ["http://localhost/version", "//version", "/%61pp.js", "/app.js;x"]:
            with self.subTest(target=target):
                reply = self.raw(f"GET {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
                self.assertTrue(reply.startswith(b"HTTP/1.1 401 "), reply)

    def test_unparseable_target_is_401_without_a_warning(self):
        with self.assertNoLogs(SERVER_LOGGER, "WARNING"):
            reply = self.raw(b"GET http://[/ HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.1 401 "), reply)

    def test_stdlib_error_response_carries_the_csp(self):
        reply = self.raw(b"FOO / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.1 501 "), reply)
        self.assertIn(b"\r\nContent-Security-Policy: ", reply)

    def test_body_or_chunking_on_an_asset_is_400(self):
        reply = self.raw_request("GET", "/", None, body=b"x")
        self.assertTrue(reply.startswith(b"HTTP/1.1 400 "), reply)
        reply = self.raw(b"GET / HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.1 400 "), reply)

    def test_bad_host_on_an_asset_is_400(self):
        resp, data = self.request("GET", "/", token=None, host="evil.example")
        self.assertEqual((resp.status, data), (400, b""))

    def test_api_responses_carry_the_csp(self):
        for token in (self.token, None):
            with self.subTest(token=token):
                resp, _ = self.request("GET", "/current_pass", token=token)
                self.assertIn("frame-ancestors 'none'", resp.getheader("Content-Security-Policy"))
                self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")


class TestWebFiles(unittest.TestCase):
    def test_load_assets(self):
        assets = load_assets()
        self.assertEqual(sorted(assets), ["/", "/app.css", "/app.js"])
        for reply in assets.values():
            self.assertTrue(reply.body)

    def test_page_is_csp_clean(self):
        html = (WEB_DIR / "index.html").read_text()
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotRegex(html, r"<script[^>]*>\s*\S[^<]*</script>")
        self.assertNotRegex(html, r"(?i)\son\w+\s*=")
        for name in ["index.html", "app.js", "app.css"]:
            text = (WEB_DIR / name).read_text()
            with self.subTest(name=name):
                # \b: a style attribute or `.style = "..."`, not a canvas's fillStyle or `.style.width =`
                self.assertNotRegex(text, r"(?i)\bstyle\s*=|https?://")
        self.assertIsNone(re.search(r"<style", html, re.IGNORECASE))

    def test_script_is_csp_clean(self):
        script = (WEB_DIR / "app.js").read_text()
        forbidden = [
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "eval(",
            "new Function",
            "document.write",
            'setAttribute("style"',
            "http://",
            "https://",
            "data:",
        ]
        for text in forbidden:
            with self.subTest(text=text):
                self.assertNotIn(text, script)

    def test_script_ids_exist_in_page(self):
        used = set(re.findall(r'\$\("([^"]+)"\)', (WEB_DIR / "app.js").read_text()))
        defined = set(re.findall(r'\bid="([^"]+)"', (WEB_DIR / "index.html").read_text()))
        self.assertIn("reviewer", used)
        self.assertLessEqual(used, defined)

    def test_list_end_stop_sign(self):
        # Past either end of the list: a stop sign on the stage (an inline octagon, coloured by the CSS), hidden by default
        html = (WEB_DIR / "index.html").read_text()
        stage = re.search(r'(?s)<main id="stage">(.*?)</main>', html)
        assert stage is not None
        screen = re.search(r'(?s)<div id="list-end" hidden>(.*?)</div>', stage[1])
        assert screen is not None
        octagon = re.search(r'<polygon class="stop-face" points="([^"]+)"', screen[1])
        assert octagon is not None
        self.assertEqual(len(octagon[1].split()), 8)
        self.assertIn('aria-hidden="true"', screen[1])
        self.assertIn('id="list-end-detail"', screen[1])
        self.assertNotRegex(screen[1], r"\b(fill|stroke)=")  # colours are the CSS's, light or dark
        css = (WEB_DIR / "app.css").read_text()
        for token in ["--stop:", "--stop-fg:", "fill: var(--stop);"]:
            with self.subTest(token=token):
                self.assertIn(token, css)
        script = (WEB_DIR / "app.js").read_text()
        self.assertNotIn("End of list", script)
        self.assertNotIn("Start of list", script)

    def test_buttons_never_take_focus(self):
        # Enter or Space on a focused button would click it past the key-repeat guard
        buttons = re.findall(r"<button\b[^>]*>", (WEB_DIR / "index.html").read_text())
        self.assertIn("reconnect", " ".join(buttons))
        self.assertIn("done", " ".join(buttons))
        for tag in buttons:
            with self.subTest(tag=tag):
                self.assertIn('tabindex="-1"', tag)

    def test_only_the_name_field_takes_focus(self):
        html = (WEB_DIR / "index.html").read_text()
        self.assertEqual(re.findall(r"<(input|select|textarea|a)\b[^>]*>", html), ["input"])
        self.assertRegex(html, r'<input id="reviewer"')
        self.assertNotRegex(html, r'tabindex="(?!-1")')

    def test_bar_has_a_fixed_height(self):
        # A text or visibility change in the bar must never resize the stage: that repacks the grids and clears undo
        css = (WEB_DIR / "app.css").read_text()
        html = (WEB_DIR / "index.html").read_text()

        def rule(selector: str) -> str:
            # (not the last line of a selector list: `.left,\n.right {` is not the `.right` rule)
            found = re.search(r"(?m)(?<!,\n)^" + re.escape(selector) + r" \{\n(.*?)^\}", css, re.DOTALL)
            assert found is not None, selector
            return found[1]

        def declares(block: str, declaration: str) -> None:
            self.assertRegex(block, r"(?m)^\s+" + re.escape(declaration) + r"(\s*/\*.*\*/)?$")

        # The bar: one row of fixed height, and at or below a width breakpoint a second for the centre group
        bar = rule("#bar")
        for declaration in [
            "height: var(--bar-height);",
            "font-size: 15px;",  # fixed, as the px breakpoints are
            "flex: none;",
            "flex-direction: column;",
            "overflow: hidden;",
            "white-space: nowrap;",
        ]:
            with self.subTest(declaration=declaration):
                declares(bar, declaration)
        self.assertRegex(bar, r"--bar-height: [\d.]+em;")  # follows the bar's font size, not the root's
        self.assertRegex(bar, r"--centre-height: [\d.]+em;")
        row = rule(".row")
        for declaration in ["height: var(--bar-height);", "flex: none;", "flex-wrap: nowrap;", "overflow: hidden;"]:
            with self.subTest(declaration=declaration):
                declares(row, declaration)
        # The only other height the bar takes is set by the window's width alone, in px, as the bar's font
        two_rows = re.search(r"(?m)^@media \(max-width: 1408px\) \{\n  #bar \{\n(.*?)^  \}\n(.*?)^\}", css, re.DOTALL)
        assert two_rows is not None
        self.assertEqual(two_rows[1], "    height: calc(var(--bar-height) + var(--centre-height));\n")
        centre_row = re.search(r"(?m)^  \.centre \{\n(.*?)^  \}", two_rows[2], re.DOTALL)
        assert centre_row is not None
        for declaration in ["position: absolute;", "top: var(--bar-height);", "height: var(--centre-height);"]:
            with self.subTest(declaration=declaration):
                declares(centre_row[1], declaration)
        self.assertRegex(css, r"(?m)^@media \(max-width: 960px\) \{\n  #bar \{\n    --pad: 0\.25em;")
        bar_css = css[css.index("#bar {") : css.index("/* ---- Overlays")]
        self.assertEqual(
            re.findall(r"(?m)^\s+height: (.*);", bar_css),
            [
                "var(--bar-height)",
                "var(--bar-height)",
                "1px",
                "calc(var(--bar-height) + var(--centre-height))",
                "var(--centre-height)",
            ],
        )  # the bar, the row, the empty status (out of the flow), and the two above
        # In px, as the bar's font: an em breakpoint would follow the browser's default font size, the text would not
        self.assertNotRegex(css, r"@media \(max-width: [\d.]+r?em\)")
        self.assertNotIn("flex-wrap: wrap", css)
        # Left, centre (the status message, else mode, progress and place) and right, ending with Done/Reconnect
        groups = re.fullmatch(
            r'(?s).*<footer id="bar"[^>]*>\s*<div class="row">\s*'
            r'<div class="left">(.*?)</div>\s*<div class="centre">(.*?)</div>\s*<div class="right">(.*?)</div>\s*'
            r"</div>\s*</footer>.*",
            html,
        )
        assert groups is not None
        ids = [re.findall(r'id="([^"]+)"', group) for group in groups.groups()]
        self.assertEqual(ids[0], ["prev", "next", "clean", "dirty", "undo", "item-status", "scale"])
        self.assertEqual(ids[1], ["status", "mode", "progress", "where"])
        self.assertEqual(ids[2], ["reviewer-chip", "reviewer-name", "help-button", "done", "reconnect"])
        self.assertIn('<span id="status" role="status">', groups[2])
        # The centre gives way first, then the reviewer's name; the right group never shrinks below its content
        centre = rule(".centre")
        for declaration in ["flex: 1 1 0;", "min-width: 0;", "overflow: hidden;", "justify-content: center;"]:
            with self.subTest(declaration=declaration):
                declares(centre, declaration)
        left = rule(".left")
        declares(left, "min-width: 0;")
        declares(left, "overflow: hidden;")
        right = rule(".right")
        self.assertNotIn("min-width", right)
        self.assertNotIn("overflow", right)
        declares(right, "flex: 0 1000000 auto;")  # shrinks first, in effect, but never below its content
        declares(right, "margin-left: auto;")  # at the right end when the centre has its own row
        declares(rule("#reviewer-chip"), "grid-template-columns: minmax(0, max-content) auto;")
        # The narrowest windows shorten Clean, Dirty and Undo, so the scale badge is never clipped; the full names stay
        self.assertRegex(
            css,
            r"(?m)^@media \(max-width: 640px\) \{\n  #bar \.long \{\n    display: none;\n  \}\n\n"
            r"  #bar \.short \{\n    display: inline;\n  \}\n\}",
        )
        declares(rule(".short"), "display: none;")
        for name, key in [("Clean", "c"), ("Dirty", "d"), ("Undo", "z")]:
            with self.subTest(button=name):
                self.assertRegex(groups[1], rf'<button [^>]*aria-label="{name} \({key}\)"[^>]*>')
        declares(rule("#status:not(:empty) ~ *"), "display: none;")
        empty = rule("#status:empty")  # still rendered, so its live region stays, but out of the flow and unseen
        self.assertNotIn("display", empty)
        declares(empty, "position: absolute;")
        declares(empty, "clip-path: inset(50%);")
        cut = rule("#mode,\n#progress,\n#where,\n#status,\n#reviewer-name")
        declares(cut, "text-overflow: ellipsis;")
        declares(cut, "min-width: 0;")
        # The overlays float over the stage, out of its layout
        declares(rule(".overlay"), "position: absolute;")
        declares(rule("#stage"), "position: relative;")
        # Done and Reconnect share a place that is kept while either is hidden
        declares(rule(".end > [hidden]"), "visibility: hidden;")
        for status_name in ["CLEAN", "DIRTY", "FLAGGED"]:
            with self.subTest(status=status_name):
                self.assertIn("--tint:", rule(f'#bar[data-status="{status_name}"]'))
        self.assertIn("@media (prefers-color-scheme: light)", css)

    def run_helpers(self, checks: str, data: object = None) -> object:
        """Run `checks` under node with `m`, a context holding app.js's pure helpers (those before the
        "---- Token" section, which touch no DOM), and `data` as parsed JSON; returns what the checks
        print as JSON (None if nothing)."""
        prelude = r"""
        const vm = require("vm");
        const assert = require("assert");
        const src = require("fs").readFileSync(process.argv[1], "utf8");
        assert(src.includes("// ---- Token"), "app.js lost its '// ---- Token' marker after the pure helpers");
        const m = vm.createContext({});
        vm.runInContext(src.slice(0, src.indexOf("// ---- Token")), m);
        const data = JSON.parse(process.argv[2]);
        const plain = (value) => JSON.parse(JSON.stringify(value)); // into this realm, for deepStrictEqual
        """
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", prelude + checks, str(WEB_DIR / "app.js"), json.dumps(data)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_script_helpers(self):
        self.run_helpers(r"""
        assert(m.validReviewer("a") && m.validReviewer("\u{1F600}".repeat(64)));
        assert(!m.validReviewer("") && !m.validReviewer("   ") && !m.validReviewer("x".repeat(65)));
        assert.deepStrictEqual(plain(m.shuffle([1, 2, 3, 4], () => 0.99)), [1, 2, 3, 4]);
        assert.deepStrictEqual(plain(m.shuffle([3, 1, 2])).sort(), [1, 2, 3]);
        assert(m.isCompleteJpeg(new Uint8Array([0xff, 0xd8, 0, 0xff, 0xd9])));
        assert(!m.isCompleteJpeg(new Uint8Array([0xff, 0xd8, 0, 0xff])));
        assert(!m.isCompleteJpeg(new Uint8Array([0xff, 0xd8, 0, 0xff, 0])));
        assert(!m.isCompleteJpeg(new Uint8Array([0, 0xd8, 0, 0xff, 0xd9])));
        const st = new Map([["a", "CLEAN"], ["b", "FLAGGED"], ["c", "UNREVIEWED"]]);
        const single = (key) => ({ kind: "single", key });
        const items = ["a", "b", "c"].map(single);
        assert.deepStrictEqual(plain(m.itemKeys(single("a"))), ["a"]);
        assert.strictEqual(m.itemStatus(single("b"), st), "FLAGGED");
        assert.strictEqual(m.itemStatus(single("zz"), st), undefined);
        assert.strictEqual(m.nextTodoIndex(items, st, 1), 2);
        assert.strictEqual(m.nextTodoIndex(items, st, 2), 1);
        assert.strictEqual(m.nextTodoIndex([single("a")], st, 0), -1);
        assert.strictEqual(m.countTodo(items, st), 2);
        assert.strictEqual(m.describeStatuses(new Map([["a", "CLEAN"]])), "a is CLEAN");
        assert.throws(() => m.parseStatusMap({ a: "BOGUS" }));
        // Grid items: their keys, and a status by the grid rules
        const grid = { kind: "grid", placements: [], keys: ["a", "c"] };
        assert.deepStrictEqual(plain(m.itemKeys(grid)), ["a", "c"]);
        assert.strictEqual(m.itemStatus(grid, st), "UNREVIEWED");
        assert.strictEqual(m.itemStatus({ kind: "grid", placements: [], keys: ["a", "b"] }, st), "DIRTY");
        assert.strictEqual(m.countTodo([grid, single("a")], st), 1);
        """)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_grid_mode_in_a_stub_page(self):
        # The whole of app.js against a stub DOM: see tests/web_harness.js for the checks
        node = shutil.which("node")
        assert node is not None
        harness = Path(__file__).with_name("web_harness.js")
        result = subprocess.run(
            [node, str(harness), str(WEB_DIR / "app.js")], capture_output=True, text=True, timeout=60, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # Every test ran to the end (one stuck on an unsettled promise would end node early)
        count = len(re.findall(r"^\s*tests\[", harness.read_text(), re.MULTILINE))
        ran = re.fullmatch(r"(\d+)/(\d+) passed", result.stdout.strip())
        assert ran is not None, result.stdout
        self.assertEqual(ran[1], ran[2])
        self.assertGreaterEqual(int(ran[2]), count)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_grid_status_parity(self):
        from image_review.status import grid_clean_refused, grid_status

        cases = [
            dict(zip([f"k{i}" for i in range(n)], combo, strict=True))
            for n in range(1, 4)
            for combo in itertools.product(["CLEAN", "DIRTY", "UNREVIEWED", "FLAGGED"], repeat=n)
        ]
        self.assertEqual(len(cases), 4 + 16 + 64)
        got = self.run_helpers(
            r"""
            console.log(JSON.stringify(data.map((statuses) => {
              const keys = Object.keys(statuses);
              const map = new Map(Object.entries(statuses));
              return [m.gridStatus(keys, map), m.gridCleanRefused(keys, map)];
            })));
            """,
            cases,
        )
        expected = [
            [grid_status(statuses, tuple(statuses)), grid_clean_refused(statuses, tuple(statuses))]
            for statuses in cases
        ]
        self.assertEqual(got, expected)

    def test_verdict_message_parity(self):
        from image_review import controller

        script = (WEB_DIR / "app.js").read_text()
        for name in ["GRID_HAS_DIRTY", "IMAGE_HAS_DIRTY", "NOTHING_TO_UNDO", "UNLOADABLE_CLEAN"]:
            with self.subTest(name=name):
                found = re.search(rf'^const {name} = "([^"]*)";$', script, re.MULTILINE)
                assert found is not None, name
                self.assertEqual(found[1], getattr(controller, name))

    def test_instance_header_name_matches_the_server(self):
        from image_review.server import INSTANCE_HEADER

        found = re.search(r'^const INSTANCE_HEADER = "([^"]*)";', (WEB_DIR / "app.js").read_text(), re.MULTILINE)
        assert found is not None
        self.assertEqual(found[1], INSTANCE_HEADER)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_grid_eligibility_parity(self):
        from image_review.controller import ReviewSession, next_batch
        from image_review.store import ManifestRow

        # Two batches interleaved in the manifest, so manifest order differs from batch order
        rows = [ManifestRow(f"k{i}", "b2" if i % 2 == 0 else "b1") for i in range(4)]
        batches = ["b1", "b2"]
        cases = [
            dict(zip([r.key for r in rows], combo, strict=True))
            for combo in itertools.product(["CLEAN", "DIRTY", "UNREVIEWED", "FLAGGED"], repeat=len(rows))
        ]
        expected = []
        for statuses in cases:
            session = SimpleNamespace(
                manifest=rows,
                _statuses=statuses,
                status_filter="unreviewed",
                mode="grid",
                pass_number=3,
                _marked_this_session=set(),
            )
            session._key_todo = lambda key, s=session: ReviewSession._key_todo(s, key)
            session._held_back_count = lambda batch, s=session: ReviewSession._held_back_count(s, batch)

            def has_rows(batch, s=session):
                return bool(ReviewSession._review_rows(s, batch))

            expected.append(
                {
                    "keys": {b: [r.key for r in ReviewSession._review_rows(session, b)] for b in batches},
                    "held": {b or "all": ReviewSession._held_back_count(session, b) for b in [*batches, None]},
                    "message": ReviewSession._held_back_message(session, None, in_session=True),
                    "first": next_batch(batches, None, has_rows, wrap=False),
                    "after": {b: next_batch(batches, b, has_rows, wrap=True) for b in batches},
                }
            )
        got = self.run_helpers(
            r"""
            const manifest = ["k0", "k1", "k2", "k3"].map((key, i) => ({ key, batch: i % 2 === 0 ? "b2" : "b1" }));
            const batches = plain(m.sortedBatches(manifest));
            assert.deepStrictEqual(batches, ["b1", "b2"]);
            // By code point, as Python: U+FF5E before U+1F600 (UTF-16 code units would put it after)
            const astral = [{ key: "x", batch: "\u{1F600}" }, { key: "y", batch: "\uFF5E" }, { key: "z", batch: "b" }];
            assert.deepStrictEqual(plain(m.sortedBatches(astral)), ["b", "\uFF5E", "\u{1F600}"]);
            console.log(JSON.stringify(data.map((statuses) => {
              const map = new Map(Object.entries(statuses));
              const withGrids = m.gridBatches(manifest, map);
              const accepts = (batch) => withGrids.has(batch);
              return {
                keys: Object.fromEntries(batches.map((b) => [b, m.gridKeys(manifest, map, b)])),
                held: Object.fromEntries([...batches, null].map((b) => [b || "all", m.heldBackCount(manifest, map, b)])),
                message: m.heldBackMessage(3, m.heldBackCount(manifest, map, null)),
                first: m.nextBatch(batches, null, accepts),
                after: Object.fromEntries(batches.map((b) => [b, m.nextBatch(batches, b, accepts)])),
              };
            })));
            """,
            cases,
        )
        self.assertEqual(got, expected)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_parse_grid_plan(self):
        self.run_helpers(r"""
        const place = (key, x, y, w, h, more = {}) => ({ key, x, y, w, h, rotated: false, source: [w, h], ...more });
        const good = () => ({
          grids: [[place("a", 0, 0, 100, 50), place("b", 100, 0, 50, 100, { rotated: true, source: [100, 50] })]],
          left_out: ["c"],
        });
        const sent = ["a", "b", "c"];
        const plan = m.parseGridPlan(good(), sent, 300, 200);
        assert.deepStrictEqual(plain(plan.leftOut), ["c"]);
        assert.deepStrictEqual(plain(plan.grids[0].map((p) => p.key)), ["a", "b"]);
        assert.strictEqual(plan.width, 300);
        // Shrunk to fit, with rounding: 1001x500 at 400 wide is 400x199
        m.parseGridPlan({ grids: [[place("a", 0, 0, 400, 199, { source: [1001, 500] })]], left_out: [] }, ["a"], 400, 400);
        const bad = (mutate, keys = sent, w = 300, h = 200) => {
          const reply = good();
          mutate(reply);
          assert.throws(() => m.parseGridPlan(reply, keys, w, h), (e) => e.constructor.name === "BadReply");
        };
        bad((r) => { r.grids[0][1].x = 50; }); // overlaps a
        bad((r) => { r.grids[0][1].x = 260; }); // past the right edge
        bad((r) => { r.grids[0][1].y = 101; }); // past the bottom
        bad((r) => { r.grids[0][0].x = -1; });
        bad((r) => { r.left_out.push("a"); }); // a twice
        bad((r) => { r.grids.push([place("a", 0, 100, 100, 50)]); }); // a twice, across grids
        bad((r) => { r.left_out = []; }); // c missing
        bad((r) => { r.left_out.push("z"); }); // never sent
        bad((r) => { r.grids[0][0].key = "z"; });
        bad((r) => { r.grids[0][0].w = 100.5; });
        bad((r) => { r.grids[0][0].y = "0"; });
        bad((r) => { r.grids[0][0].w = 0; });
        bad((r) => { r.grids[0][0].rotated = 1; });
        bad((r) => { r.grids[0][0].source = [100]; });
        bad((r) => { r.grids[0][0].source = [100, 0]; });
        bad((r) => { r.grids[0][0].source = [100, 50.5]; });
        bad((r) => { r.grids[0][0].source = [50, 25]; }); // enlarged
        bad((r) => { r.grids[0][0].source = [100, 100]; }); // aspect differs
        bad((r) => { r.grids[0][1].rotated = false; }); // the rect is not the source's fit upright
        bad((r) => { r.grids.push([]); });
        bad((r) => { r.grids = {}; });
        bad((r) => { delete r.left_out; });
        bad(() => {}, sent, 0, 200);
        bad(() => {}, sent, 300.5, 200);
        assert.throws(() => m.parseGridPlan(null, sent, 300, 200));
        """)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_order_and_demote_grid_items(self):
        self.run_helpers(r"""
        const place = (key) => ({ key, x: 0, y: 0, w: 1, h: 1, rotated: false, source: [1, 1] });
        const plan = { grids: [["a"], ["b", "c"], ["d"], ["e", "f", "g"]].map((g) => g.map(place)), leftOut: ["x", "y"] };
        // random() = 0 always swaps with the first: [g0..g3] -> [g1, g2, g3, g0]
        const items = m.orderGridItems(plan, () => 0);
        assert.deepStrictEqual(plain(items.map((item) => [item.kind, item.keys || item.key])), [
          ["grid", ["e", "f", "g"]], ["grid", ["b", "c"]], ["grid", ["d"]], ["grid", ["a"]], ["single", "x"], ["single", "y"],
        ]);
        assert.deepStrictEqual(plain(items[0].placements.map((p) => p.key)), ["e", "f", "g"]);
        // A failed key leaves the grid as a single item; an emptied grid is dropped; no key is listed twice
        const less = m.demoteKeys(items, 0, ["f"]);
        assert.deepStrictEqual(plain(less.map((item) => item.keys || item.key)), [["e", "g"], ["b", "c"], ["d"], ["a"], "x", "y", "f"]);
        assert.deepStrictEqual(plain(less[0].placements.map((p) => p.key)), ["e", "g"]);
        assert.deepStrictEqual(plain(items[0].keys), ["e", "f", "g"]); // the input is left alone
        const gone = m.demoteKeys(items, 2, ["d"]);
        assert.deepStrictEqual(plain(gone.map((item) => item.keys || item.key)), [["e", "f", "g"], ["b", "c"], ["a"], "x", "y", "d"]);
        const listed = m.demoteKeys([...items, { kind: "single", key: "a" }], 3, ["a"]);
        assert.deepStrictEqual(plain(listed.map((item) => item.keys || item.key)), [["e", "f", "g"], ["b", "c"], ["d"], "x", "y", "a"]);
        """)

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_placement_transform_and_min_scale(self):
        self.run_helpers(r"""
        const apply = ([a, b, c, d, e, f], [u, v]) => [a * u + c * v + e, b * u + d * v + f];
        const corners = (p) => {
          const [sw, sh] = p.source;
          const t = m.placementTransform(p);
          return [[0, 0], [sw, 0], [sw, sh], [0, sh]].map((uv) => apply(t, uv));
        };
        // Upright: source corners to the rect's, in order
        const upright = { key: "a", x: 10, y: 20, w: 50, h: 25, rotated: false, source: [200, 100] };
        assert.deepStrictEqual(plain(corners(upright)), [[10, 20], [60, 20], [60, 45], [10, 45]]);
        // Rotated clockwise (pygame rotate(-90)): top-left to top-right, top-right to bottom-right, ...
        const turned = { key: "b", x: 10, y: 20, w: 25, h: 50, rotated: true, source: [200, 100] };
        assert.deepStrictEqual(plain(corners(turned)), [[35, 20], [35, 70], [10, 70], [10, 20]]);
        assert.strictEqual(m.minScale([]), 1);
        assert.strictEqual(m.minScale([{ ...upright, w: 200, h: 100 }]), 1);
        assert.strictEqual(m.minScale([upright, { ...turned, w: 40, h: 80 }]), 0.25);
        assert.strictEqual(m.minScale([{ ...turned, w: 40, h: 80 }]), 0.4);
        """)


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


class TestBrowserHelpers(unittest.TestCase):
    def test_browser_url_keeps_token_in_fragment(self):
        self.assertEqual(browser_url("tok"), "http://127.0.0.1:8080/#tok")
        self.assertEqual(browser_url("tok", 9000), "http://127.0.0.1:9000/#tok")

    def test_ssh_command_with_placeholder_login(self):
        command = ssh_forward_command(Path("/home/a/ir.sock"), "node1.example", "alice", "alice@<login-node>")
        self.assertEqual(
            command,
            "ssh -N -o ExitOnForwardFailure=yes -o ControlPath=none -J 'alice@<login-node>'"
            " -L 127.0.0.1:8080:/home/a/ir.sock alice@node1.example",
        )

    def test_ssh_command_with_via(self):
        command = ssh_forward_command(Path("/tmp/ir.sock"), "node1", "alice", "bob@login", port=9000)
        self.assertIn("-J bob@login ", command)
        self.assertIn("-L 127.0.0.1:9000:/tmp/ir.sock ", command)
        self.assertNotIn("<login-node>", command)

    def test_ssh_command_direct_has_no_jump(self):
        command = ssh_forward_command(Path("/tmp/my dir/ir.sock"), "node1", "alice", None)
        self.assertNotIn("-J", command)
        self.assertEqual(
            shlex.split(command),
            [
                *["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ControlPath=none"],
                *["-L", "127.0.0.1:8080:/tmp/my dir/ir.sock", "alice@node1"],
            ],
        )
        self.assertIn("-L '127.0.0.1:8080:/tmp/my dir/ir.sock' alice@node1", command)

    def test_ssh_command_quotes_a_path_with_a_space(self):
        command = ssh_forward_command(Path("/tmp/my dir/ir.sock"), "node1", "alice", "alice@<login-node>")
        self.assertIn("-L '127.0.0.1:8080:/tmp/my dir/ir.sock' ", command)
        self.assertEqual(shlex.split(command)[-3:], ["-L", "127.0.0.1:8080:/tmp/my dir/ir.sock", "alice@node1"])

    def test_ssh_command_quotes_each_piece(self):
        command = ssh_forward_command(Path("/tmp/ir.sock"), "node1", "alice", "alice@[fe80::1]")
        words = shlex.split(command)
        self.assertEqual(words[words.index("-J") + 1], "alice@[fe80::1]")
        self.assertIn("-J 'alice@[fe80::1]' ", command)


@unittest.skipUnless(HAS_AF_UNIX, "needs AF_UNIX")
class TestPrivateFile(unittest.TestCase):
    def test_failed_write_leaves_no_file(self):
        home = socket_dir(self)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            with mock.patch("os.fdopen", side_effect=OSError("boom")), self.assertRaises(OSError):
                write_private_file("x.txt", "secret\n")
            self.assertEqual(list((home / ".image-review").iterdir()), [])


class TestParseToken(unittest.TestCase):
    def test_accepted(self):
        for raw in ("0123456789abcdef0123456789abcdef", "A" * 22, "a-_" * 8, "x" * 256):
            with self.subTest(length=len(raw)):
                self.assertEqual(parse_token(raw), raw)

    def test_rejected_without_echoing_the_value(self):
        good = "0123456789abcdef0123456789abcdef"
        cases = {
            "too short": "a" * 21,
            "empty": "",
            "too long": "a" * 257,
            "plus": good[:-1] + "+",
            "slash": good[:-1] + "/",
            "equals": good[:-1] + "=",
            "inner space": good[:10] + " " + good[11:],
            "leading space": " " + good,
            "trailing space": good + " ",
            "trailing newline": good + "\n",
            "non-ascii": good[:-1] + "\u00e9",
        }
        for name, raw in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError) as cm:
                    parse_token(raw)
                self.assertNotIn(raw.strip() or "\0", str(cm.exception))


class Tty(io.StringIO):
    def isatty(self):
        return True


@unittest.skipUnless(HAS_AF_UNIX, "needs AF_UNIX")
class TestServeSocketCommand(unittest.TestCase):
    def setUp(self):
        self.work = temp_dir(self) / "work"
        self.work.mkdir()
        make_work_dir(self.work)
        self.home = socket_dir(self)  # short: the default socket lives under it
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = self.home / ".image-review"

    def serve(self, *args: str, **kwargs):
        """Run `serve --work-dir ... *args` with serve_forever replaced by a KeyboardInterrupt; returns the result."""
        with mock.patch.object(ReviewServer, "serve_forever", side_effect=KeyboardInterrupt):
            return invoke_cli("serve", "--work-dir", str(self.work), *args, **kwargs)

    def test_non_tty_writes_private_url_file_and_hides_token(self):
        seen = {}

        def fake_serve(server, *a, **k):
            (url_file,) = self.dir.glob("browser-*.txt")
            seen["mode"] = stat.S_IMODE(url_file.stat().st_mode)
            seen["url"] = url_file.read_text().strip()
            seen["file"] = url_file
            seen["sockets"] = list(self.dir.glob("serve-*.sock"))
            raise KeyboardInterrupt

        with mock.patch.object(ReviewServer, "serve_forever", fake_serve):
            result = invoke_cli("serve", "--work-dir", str(self.work), "--socket")
        self.assertEqual(result.exit_code, 0, result.output)
        match = re.fullmatch(r"http://127\.0\.0\.1:8080/#(\S+)", seen["url"])
        assert match is not None
        self.assertNotIn(match.group(1), result.output)
        self.assertEqual(seen["mode"], 0o600)
        (sock,) = seen["sockets"]
        self.assertIn("experimental", result.output)
        self.assertIn(f"-L 127.0.0.1:8080:{sock} ", result.output)
        self.assertIn("-J ", result.output)
        self.assertIn("@<login-node>", result.output)
        self.assertIn(str(seen["file"]), result.output)
        self.assertIn("ssh ", result.output)
        self.assertIn("'<user>@<login-node>' cat ".replace("<user>", getpass.getuser()), result.output)
        self.assertEqual(list(self.dir.iterdir()), [])  # socket and URL file are gone
        self.assertFalse((self.work / "review.lock").exists())

    def test_via_fills_in_the_login_node(self):
        result = self.serve("--socket", "--via", "alice@login")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("-J alice@login ", result.output)
        self.assertIn("ssh alice@login cat ", result.output)
        self.assertIn("-o ControlPath=none ", result.output)
        self.assertNotIn("<login-node>", result.output)

    def test_via_from_environment_in_socket_mode(self):
        result = self.serve("--socket", env={"IMAGE_REVIEW_VIA": "carol@login"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("-J carol@login ", result.output)

    def test_direct_omits_jump_and_reads_url_from_the_node(self):
        result = self.serve("--socket", "--direct")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("-J", result.output)
        self.assertNotIn("<login-node>", result.output)
        self.assertIn("-o ControlPath=none -L 127.0.0.1:8080:", result.output)
        node = socket.getfqdn()
        self.assertIn(f"ssh {shlex.quote(f'{getpass.getuser()}@{node}')} cat ", result.output)

    def test_ssh_host_names_the_node(self):
        user = getpass.getuser()
        result = self.serve("--socket", "--ssh-host", "node042.example.org")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"-L 127.0.0.1:8080:{self.dir}", result.output)
        self.assertIn(f" {user}@node042.example.org\n", result.output)
        self.assertNotIn(socket.getfqdn() + "\n", result.output.replace(f"{user}@node042.example.org", ""))

    def test_ssh_host_with_direct_names_the_node_in_the_cat_line(self):
        user = getpass.getuser()
        result = self.serve("--socket", "--direct", "--ssh-host", "node042.example.org")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f" {user}@node042.example.org\n", result.output)
        self.assertIn(f"ssh {user}@node042.example.org cat ", result.output)

    def test_default_node_is_the_fqdn(self):
        result = self.serve("--socket")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(shlex.quote(f"{getpass.getuser()}@{socket.getfqdn()}"), result.output)

    def test_ssh_host_requires_socket_mode(self):
        result = self.serve("--ssh-host", "node042.example.org")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("--ssh-host requires --socket", result.output)
        self.assertFalse((self.work / "review.lock").exists())
        self.assertFalse(self.dir.exists())

    def test_bad_ssh_host_refused(self):
        for bad in ["", "-oProxyCommand=x", "a@b", "a b", "a;b"]:
            with self.subTest(bad=bad):
                result = self.serve("--socket", "--ssh-host", bad)
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIsInstance(result.exception, SystemExit)
                self.assertNotIn("Traceback", result.output)
                self.assertIn("Invalid --ssh-host", result.output)
                self.assertFalse((self.work / "review.lock").exists())
                self.assertFalse(self.dir.exists())

    def test_direct_from_environment_in_socket_mode(self):
        result = self.serve("--socket", env={"IMAGE_REVIEW_DIRECT": "1"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("-J", result.output)

    def test_direct_ignores_via_from_environment(self):
        result = self.serve("--socket", "--direct", env={"IMAGE_REVIEW_VIA": "carol@login"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("-J", result.output)
        self.assertNotIn("carol@login", result.output)

    def test_direct_and_via_on_the_command_line_conflict(self):
        for args, env in (
            (["--socket", "--direct", "--via", "a@b"], {}),
            (["--socket", "--via", "a@b"], {"IMAGE_REVIEW_DIRECT": "1"}),
        ):
            with self.subTest(args=args, env=env):
                result = self.serve(*args, env=env)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertIn("mutually exclusive", result.output)
                self.assertFalse((self.work / "review.lock").exists())
                self.assertFalse(self.dir.exists())

    def test_tty_prints_url(self):
        seen = []

        def fake_serve(server, *a, **k):
            seen.append(list(self.dir.glob("serve-*.sock")))
            raise KeyboardInterrupt

        with (
            mock.patch.object(ReviewServer, "serve_forever", fake_serve),
            mock.patch("sys.stdout", Tty()) as out,
            mock.patch.dict(os.environ),
        ):
            for name in CLEAN_ENV:  # as invoke_cli: a developer's exports must not leak in
                os.environ.pop(name, None)
            cli.cli.main(["serve", "--work-dir", str(self.work), "--socket"], standalone_mode=False)
        text = out.getvalue()
        self.assertRegex(text, r"http://127\.0\.0\.1:8080/#\S+")
        self.assertIn("treat it like a password", text)
        self.assertEqual(len(seen[0]), 1)
        self.assertEqual(list(self.dir.iterdir()), [])  # socket gone, no URL file

    def test_relative_socket_path_is_announced_absolute(self):
        cwd = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, cwd)
        result = self.serve("--socket-path", "ir.sock")
        self.assertEqual(result.exit_code, 0, result.output)
        expected = Path.cwd() / "ir.sock"  # resolved: /tmp is a symlink on macOS
        self.assertIn(f"-L 127.0.0.1:8080:{expected} ", result.output)

    def test_empty_socket_path_is_refused_not_defaulted(self):
        result = self.serve("--socket-path", "")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertIn("must not be empty", result.output)
        self.assertFalse(self.dir.exists() and list(self.dir.glob("serve-*.sock")))
        self.assertFalse((self.work / "review.lock").exists())

    def test_socket_path_from_environment_with_socket(self):
        path = self.home / "env.sock"
        seen = []

        def fake_serve(server, *a, **k):
            seen.append(stat.S_ISSOCK(path.lstat().st_mode))
            raise KeyboardInterrupt

        with mock.patch.object(ReviewServer, "serve_forever", fake_serve):
            result = invoke_cli(
                "serve", "--work-dir", str(self.work), "--socket", env={"IMAGE_REVIEW_SOCKET_PATH": str(path)}
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [True])
        self.assertIn(f"-L 127.0.0.1:8080:{path} ", result.output)
        self.assertFalse(path.exists())

    def test_socket_path_from_environment_alone_stays_tcp(self):
        path = self.home / "env.sock"
        seen = []

        def fake_serve(server, *a, **k):
            seen.append(type(server).__name__)
            raise KeyboardInterrupt

        with mock.patch.object(ReviewServer, "serve_forever", fake_serve):
            result = invoke_cli("serve", "--work-dir", str(self.work), env={"IMAGE_REVIEW_SOCKET_PATH": str(path)})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, ["ReviewServer"])
        self.assertFalse(path.exists())
        self.assertNotIn("experimental", result.output)

    def test_socket_path_option_overrides_environment(self):
        env_path = self.home / "env.sock"
        path = self.home / "cli.sock"
        result = self.serve("--socket-path", str(path), env={"IMAGE_REVIEW_SOCKET_PATH": str(env_path)})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"-L 127.0.0.1:8080:{path} ", result.output)
        self.assertNotIn(str(env_path), result.output)

    def test_empty_socket_path_from_environment_counts_as_unset(self):
        # click drops empty environment values, so the default path is used (no Path("") = cwd hazard)
        with_socket = self.serve("--socket", env={"IMAGE_REVIEW_SOCKET_PATH": ""})
        self.assertEqual(with_socket.exit_code, 0, with_socket.output)
        self.assertIn(f"-L 127.0.0.1:8080:{self.dir}/serve-", with_socket.output)
        without_socket = self.serve(env={"IMAGE_REVIEW_SOCKET_PATH": ""})
        self.assertEqual(without_socket.exit_code, 0, without_socket.output)
        self.assertNotIn("experimental", without_socket.output)

    def test_token_from_environment_is_used_and_kept_across_restarts(self):
        token = "0123456789abcdef0123456789abcdef"
        urls = []
        replies = []

        def fake_serve(server, *a, **k):
            (url_file,) = self.dir.glob("browser-*.txt")
            urls.append(url_file.read_text().strip())
            sock = next(self.dir.glob("serve-*.sock"))
            server.timeout = 10
            for bearer in (token, "f" * 32):
                worker = threading.Thread(target=server.handle_request, daemon=True)
                worker.start()
                conn = UnixHTTPConnection(sock)
                conn.request("GET", "/current_pass", headers={"Authorization": f"Bearer {bearer}"})
                resp = conn.getresponse()
                resp.read()
                conn.close()
                worker.join()
                replies.append(resp.status)
            raise KeyboardInterrupt

        for _ in range(2):
            with mock.patch.object(ReviewServer, "serve_forever", fake_serve):
                result = invoke_cli(
                    "serve", "--work-dir", str(self.work), "--socket", env={"IMAGE_REVIEW_TOKEN": token}
                )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Using the token from $IMAGE_REVIEW_TOKEN", result.output)
            self.assertNotIn(token, result.output)
        self.assertEqual(urls, [browser_url(token)] * 2)
        self.assertEqual(replies, [200, 401, 200, 401])

    def test_token_from_environment_in_the_tty_url(self):
        token = "A" * 22
        with mock.patch("sys.stdout", Tty()) as out, mock.patch.dict(os.environ):
            for name in CLEAN_ENV:
                os.environ.pop(name, None)
            os.environ["IMAGE_REVIEW_TOKEN"] = token
            with mock.patch.object(ReviewServer, "serve_forever", side_effect=KeyboardInterrupt):
                cli.cli.main(["serve", "--work-dir", str(self.work), "--socket"], standalone_mode=False)
        self.assertIn(f"\n{browser_url(token)}\n", out.getvalue())
        self.assertEqual(out.getvalue().count(token), 1)

    def test_invalid_token_from_environment_is_refused(self):
        for bad in ("short", "a" * 21 + "+", " " + "a" * 30, "a" * 300):
            with self.subTest(length=len(bad)):
                result = self.serve("--socket", env={"IMAGE_REVIEW_TOKEN": bad})
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("$IMAGE_REVIEW_TOKEN", result.output)
                self.assertIn("A-Z a-z 0-9", result.output)
                self.assertNotIn(bad.strip(), result.output)
                self.assertNotIn("Traceback", result.output)
                self.assertFalse(self.dir.exists() and list(self.dir.iterdir()))
                self.assertFalse((self.work / "review.lock").exists())

    def test_token_from_environment_ignored_without_socket(self):
        token = "0123456789abcdef0123456789abcdef"
        seen = []

        def fake_serve(server, *a, **k):
            seen.append(type(server).__name__)
            self.assertNotEqual(getattr(server, "token", None), token)
            raise KeyboardInterrupt

        for value in (token, "not a valid token"):  # ignored, so not even parsed
            with mock.patch.object(ReviewServer, "serve_forever", fake_serve):
                result = invoke_cli("serve", "--work-dir", str(self.work), env={"IMAGE_REVIEW_TOKEN": value})
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn(value, result.output)
            self.assertNotIn("IMAGE_REVIEW_TOKEN", result.output)
            leftovers = [p.read_text() for p in self.dir.iterdir()] if self.dir.exists() else []
            self.assertFalse(any(value in text for text in leftovers))
        self.assertEqual(seen, ["ReviewServer"] * 2)

    def test_empty_token_from_environment_counts_as_unset(self):
        result = self.serve("--socket", env={"IMAGE_REVIEW_TOKEN": ""})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("IMAGE_REVIEW_TOKEN", result.output)

    def test_no_token_in_environment_gives_a_fresh_one_each_start(self):
        urls = []

        def fake_serve(server, *a, **k):
            (url_file,) = self.dir.glob("browser-*.txt")
            urls.append(url_file.read_text().strip())
            raise KeyboardInterrupt

        for _ in range(2):
            with mock.patch.object(ReviewServer, "serve_forever", fake_serve):
                result = invoke_cli("serve", "--work-dir", str(self.work), "--socket")
            self.assertNotIn("IMAGE_REVIEW_TOKEN", result.output)
        self.assertEqual(len(urls), 2)
        self.assertNotEqual(urls[0], urls[1])

    def test_token_from_environment_is_never_logged(self):
        # cli.log_to swaps the package logger's handlers, so assertLogs would not see serve's records:
        # check what the command emitted instead
        token = "0123456789abcdef0123456789abcdef"
        with mock.patch.object(ReviewServer, "serve_forever", side_effect=KeyboardInterrupt):
            result = invoke_cli(
                "-v", "serve", "--work-dir", str(self.work), "--socket", env={"IMAGE_REVIEW_TOKEN": token}
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(token, result.output)
        self.assertNotIn(token, result.stderr)

    def test_invalid_token_is_refused_before_the_work_dir_is_opened(self):
        with mock.patch.object(cli, "open_local_store") as opened:
            result = self.serve("--socket", env={"IMAGE_REVIEW_TOKEN": "short"})
        self.assertEqual(result.exit_code, 1, result.output)
        opened.assert_not_called()

    def test_busy_path_is_refused_and_leaves_the_first_server(self):
        path = self.home / "busy.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(path))
        listener.listen(1)
        result = self.serve("--socket-path", str(path))
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Another server is listening", result.output)
        self.assertTrue(stat.S_ISSOCK(path.lstat().st_mode))
        self.assertFalse((self.work / "review.lock").exists())

    def test_socket_path_implies_socket(self):
        path = self.home / "my.sock"
        seen = []

        def fake_serve(server, *a, **k):
            seen.append(stat.S_ISSOCK(path.lstat().st_mode))
            raise KeyboardInterrupt

        with mock.patch.object(ReviewServer, "serve_forever", fake_serve):
            result = invoke_cli("serve", "--work-dir", str(self.work), "--socket-path", str(path))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(seen, [True])
        self.assertIn(f"-L 127.0.0.1:8080:{path} ", result.output)
        self.assertFalse(path.exists())

    def test_conflicting_options_exit_2(self):
        for args in (
            ["--socket", "--bind", "x"],
            ["--socket-path", str(self.home / "a.sock"), "--bind", "x"],
            ["--socket", "--port", "1"],
            ["--socket", "--port", "0"],
            ["--via", "a@b"],
            ["--direct"],
        ):
            with self.subTest(args=args):
                result = self.serve(*args)
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertFalse((self.work / "review.lock").exists())
                self.assertFalse(self.dir.exists())

    def test_via_from_environment_ignored_without_socket(self):
        result = self.serve("--bind", "127.0.0.1", env={"IMAGE_REVIEW_VIA": "carol@login"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("image-review review --remote", result.output)
        self.assertNotIn("Unix socket", result.output)

    def test_direct_from_environment_ignored_without_socket(self):
        result = self.serve("--bind", "127.0.0.1", env={"IMAGE_REVIEW_DIRECT": "1"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("image-review review --remote", result.output)
        self.assertNotIn("Unix socket", result.output)

    def test_bad_via_refused(self):
        result = self.serve("--socket", "--via", "-oProxyCommand=x")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Invalid --via", result.output)
        self.assertFalse((self.work / "review.lock").exists())

    def test_path_too_long_exits_1_and_releases_lock(self):
        result = self.serve("--socket-path", str(self.home / ("x" * 120)))
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertNotIsInstance(result.exception, OSError)
        self.assertNotIn("Traceback", result.output)
        self.assertIn("limit is", result.output)
        self.assertFalse((self.work / "review.lock").exists())

    def test_missing_parent_exits_1(self):
        result = self.serve("--socket-path", str(self.home / "nope" / "a.sock"))
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Cannot listen on socket", result.output)
        self.assertFalse((self.work / "review.lock").exists())

    def test_url_file_failure_releases_everything(self):
        with mock.patch("image_review.server.write_private_file", side_effect=OSError("disk full")):
            result = self.serve("--socket")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Cannot write browser URL file: disk full", result.output)
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertFalse((self.work / "review.lock").exists())

    def test_sigterm_removes_socket_and_url_file(self):
        path = self.home / "t.sock"
        env = {**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "image_review.cli",
                "serve",
                "--work-dir",
                str(self.work),
                "--socket-path",
                str(path),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 20
        while not list(self.dir.glob("browser-*.txt")):
            self.assertLess(time.monotonic(), deadline, "server never wrote its URL file")
            self.assertIsNone(proc.poll())
            time.sleep(0.05)
        self.assertTrue(path.exists())
        time.sleep(0.3)  # let serve_forever start
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, err)
        self.assertFalse(path.exists())
        self.assertEqual(list(self.dir.glob("browser-*.txt")), [])
        self.assertFalse((self.work / "review.lock").exists())


if __name__ == "__main__":
    unittest.main()
