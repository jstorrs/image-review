import getpass
import io
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
import time
import unittest
from pathlib import Path
from unittest import mock

import image_review
from image_review import cli
from image_review.cli import PACKAGE_LOGGER, browser_url, ssh_forward_command
from image_review.server import (
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
                self.assertNotRegex(text, r"(?i)style\s*=|https?://")
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

    @unittest.skipUnless(shutil.which("node"), "needs node")
    def test_script_helpers(self):
        # The pure helpers come before the "---- Token" section and touch no DOM
        checks = r"""
        const vm = require("vm");
        const assert = require("assert");
        const src = require("fs").readFileSync(process.argv[1], "utf8");
        assert(src.includes("// ---- Token"), "app.js lost its '// ---- Token' marker after the pure helpers");
        const m = vm.runInNewContext(src.slice(0, src.indexOf("// ---- Token")) +
          ";({validReviewer, shuffle, isCompleteJpeg, nextTodoIndex, countTodo, describeStatuses, parseStatusMap})");
        assert(m.validReviewer("a") && m.validReviewer("\u{1F600}".repeat(64)));
        assert(!m.validReviewer("") && !m.validReviewer("   ") && !m.validReviewer("x".repeat(65)));
        assert.deepStrictEqual(m.shuffle([1, 2, 3, 4], () => 0.99), [1, 2, 3, 4]);
        assert.deepStrictEqual([...m.shuffle([3, 1, 2])].sort(), [1, 2, 3]);
        assert(m.isCompleteJpeg(new Uint8Array([0xff, 0xd8, 0, 0xff, 0xd9])));
        assert(!m.isCompleteJpeg(new Uint8Array([0xff, 0xd8, 0, 0xff])));
        assert(!m.isCompleteJpeg(new Uint8Array([0xff, 0xd8, 0, 0xff, 0])));
        assert(!m.isCompleteJpeg(new Uint8Array([0, 0xd8, 0, 0xff, 0xd9])));
        const st = new Map([["a", "CLEAN"], ["b", "FLAGGED"], ["c", "UNREVIEWED"]]);
        assert.strictEqual(m.nextTodoIndex(["a", "b", "c"], st, 1), 2);
        assert.strictEqual(m.nextTodoIndex(["a", "b", "c"], st, 2), 1);
        assert.strictEqual(m.nextTodoIndex(["a"], st, 0), -1);
        assert.strictEqual(m.countTodo(["a", "b", "c"], st), 2);
        assert.strictEqual(m.describeStatuses(new Map([["a", "CLEAN"]])), "a is CLEAN");
        assert.throws(() => m.parseStatusMap({ a: "BOGUS" }));
        """
        node = shutil.which("node")
        assert node is not None
        result = subprocess.run(
            [node, "-e", checks, str(WEB_DIR / "app.js")], capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)


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
