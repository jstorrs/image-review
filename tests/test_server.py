import contextlib
import csv
import http.client
import io
import json
import os
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner
from fixtures import ROWS, make_work_dir

from image_review.cli import cli
from image_review.connection import API_VERSION, RemoteTarget
from image_review.server import ReviewServer, make_server
from image_review.store import LocalStore

FP = "a" * 64


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work_dir = Path(tmp.name)
        make_work_dir(self.work_dir)
        self.server, self.target = make_server(LocalStore(self.work_dir), "127.0.0.1", 0)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
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
        start = time.perf_counter()
        for _ in range(30):
            conn.request("GET", "/current_pass", headers=headers)
            self.assertEqual(conn.getresponse().read(), b'{"pass": 1}')
        self.assertLess(time.perf_counter() - start, 0.9)  # Nagle + delayed ACK would be ~1.2s

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
            (f"POST /mark HTTP/1.1\r\nHost: x\r\n{self.auth_header()}Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n").encode()
        )
        self.assertTrue(data.startswith(b"HTTP/1.1 400"))
        self.assertIn(b"Connection: close", data)

    def test_body_on_bodyless_route_rejected(self):
        resp, _, _ = self.request("GET", "/manifest", body=b"abc")
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.getheader("Connection"), "close")

    def test_duplicate_content_length_on_get_rejected(self):
        data = self.raw(
            (f"GET /manifest HTTP/1.1\r\nHost: x\r\n{self.auth_header()}Content-Length: 0\r\nContent-Length: 5\r\n\r\n").encode()
        )
        self.assertTrue(data.startswith(b"HTTP/1.1 400"))

    def test_duplicate_content_length_on_mark_rejected(self):
        data = self.raw(
            (f"POST /mark HTTP/1.1\r\nHost: x\r\n{self.auth_header()}Content-Length: 2\r\nContent-Length: 2\r\n\r\n{{}}").encode()
        )
        self.assertTrue(data.startswith(b"HTTP/1.1 400"))

    def test_handler_failure_is_500_and_server_survives(self):
        err = io.StringIO()
        body = json.dumps({"keys": ["batch_001/a.jpg"], "batch": "batch_001", "status": "CLEAN", "pass": 1}).encode()
        with (
            contextlib.redirect_stderr(err),
            mock.patch.object(self.server.store, "mark", side_effect=OSError("secret-detail")),
        ):
            resp, data, conn = self.request("POST", "/mark", body=body)
            self.assertEqual((resp.status, data), (500, b""))
            self.assertEqual(resp.getheader("Connection"), "close")
            self.assertIn("OSError", err.getvalue())
            self.assertNotIn("secret-detail", err.getvalue())
        conn.request("GET", "/current_pass", headers={"Authorization": f"Bearer {self.target.token}"})
        self.assertEqual(conn.getresponse().status, 200)

    def test_deeply_nested_json_is_400(self):
        resp, _ = self.post_mark(b"[" * 200000)
        self.assertEqual(resp.status, 400)


class TestLogging(ServerTestCase):
    def test_logs_escape_control_chars_and_drop_query(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.raw(b"GET /a\x1b[31mb?key=SECRETKEY HTTP/1.1\r\nHost: x\r\n\r\n")
        log = err.getvalue()
        self.assertNotIn("\x1b", log)
        self.assertIn("\\x1b[31mb", log)
        self.assertNotIn("SECRETKEY", log)

    def test_logs_never_contain_token_or_key(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.request("GET", "/image?key=batch_001%2Fa.jpg")
            self.request("GET", "/image?key=batch_001%2Fa.jpg", token="wrong")
            self.request("GET", "/image?key=batch_001%2Fnope.jpg")
            self.request("GET", "/statuses?pass=zzz")
            self.request("POST", "/mark", body=b"{}")
        log = err.getvalue()
        self.assertIn("GET /image 200", log)
        self.assertNotIn(self.target.token, log)
        self.assertNotIn("batch_001", log)
        self.assertNotIn("key=", log)


class TestPreAuthIdleSockets(ServerTestCase):
    def test_idle_raw_sockets_do_not_block_real_client(self):
        idle = [socket.create_connection(("127.0.0.1", self.target.port), timeout=10) for _ in range(40)]
        for sock in idle:
            self.addCleanup(sock.close)
        start = time.perf_counter()
        resp, data, _ = self.request("GET", "/current_pass")
        self.assertEqual((resp.status, data), (200, b'{"pass": 1}'))
        self.assertLess(time.perf_counter() - start, 3)


class TestNoKeyFilesLeft(unittest.TestCase):
    def test_make_server_leaves_temp_dir_empty(self):
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as scratch:
            make_work_dir(Path(work))
            with mock.patch.object(tempfile, "tempdir", scratch):
                server, _ = make_server(LocalStore(Path(work)), "127.0.0.1", 0)
            server.server_close()
            self.assertEqual(os.listdir(scratch), [])


class TestServeCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
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
            result = CliRunner().invoke(cli, ["serve", "--work-dir", str(self.work), "--bind", "127.0.0.1"])
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

    def test_loose_existing_dir_is_tightened(self):
        d = self.home / ".image-review"
        d.mkdir(mode=0o755)
        d.chmod(0o755)
        from image_review.server import write_connection_file

        path = write_connection_file(RemoteTarget("h", 1, "t", FP))
        self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
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

    def test_missing_manifest(self):
        empty = self.home / "empty"
        empty.mkdir()
        result = CliRunner().invoke(cli, ["serve", "--work-dir", str(empty)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No preprocessed data found", result.output)

    def test_bad_bind_and_port(self):
        for args in (["--bind", "0.0.0.0"], ["--bind", "::"], ["--bind", ""], ["--bind", "::1"], ["--bind", "0"], ["--bind", "127.1"], ["--port", "70000"], ["--port", "-1"]):
            with self.subTest(args=args):
                result = CliRunner().invoke(cli, ["serve", "--work-dir", str(self.work), *args])
                self.assertEqual(result.exit_code, 2 if "--port" in args else 1, result.output)
                self.assertNotIsInstance(result.exception, OSError)
                self.assertNotIn("Traceback", result.output)


class TestAuth(ServerTestCase):
    def test_missing_and_wrong_token_rejected_everywhere(self):
        requests = [
            ("GET", "/manifest"),
            ("GET", "/image?key=batch_001/a.jpg"),
            ("GET", "/statuses?pass=1"),
            ("GET", "/current_pass"),
            ("GET", "/version"),
            ("POST", "/mark"),
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

    def test_image_without_key_is_400(self):
        resp, _, _ = self.request("GET", "/image")
        self.assertEqual(resp.status, 400)

    def test_version(self):
        from importlib.metadata import version

        resp, data, _ = self.request("GET", "/version")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data), {"api": API_VERSION, "version": version("image-review")})

    def test_statuses_and_current_pass(self):
        self.assertEqual(self.get_json("/current_pass"), {"pass": 1})
        statuses = self.get_json("/statuses?pass=1")
        self.assertEqual(statuses, {key: "UNREVIEWED" for _, key, _ in ROWS})

    def test_statuses_bad_pass_is_400(self):
        for path in ("/statuses", "/statuses?pass=x", "/statuses?pass=0", "/statuses?pass=-1"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status, 400)


class TestMark(ServerTestCase):
    def test_mark_round_trip(self):
        resp, data = self.post_mark(
            {"keys": ["batch_001/a.jpg", "batch_001/b.jpg"], "batch": "batch_001", "status": "CLEAN", "pass": 1}
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data), {"batch_001/a.jpg": "CLEAN", "batch_001/b.jpg": "CLEAN"})
        statuses = self.get_json("/statuses?pass=1")
        self.assertEqual(statuses["batch_001/a.jpg"], "CLEAN")
        self.assertEqual(statuses["batch_002/c.jpg"], "UNREVIEWED")
        with open(self.work_dir / "review.tsv", newline="") as f:
            stored = {r["image_id"] for r in csv.DictReader(f, delimiter="\t")}
        self.assertEqual(stored, {"/src/patient_smith/a.dcm", "/src/patient_jones/b.dcm"})

    def test_bad_bodies_are_400(self):
        good = {"keys": ["batch_001/a.jpg"], "batch": "batch_001", "status": "DIRTY", "pass": 1}
        bad = {
            "status UNREVIEWED": {**good, "status": "UNREVIEWED"},
            "keys not a list": {**good, "keys": "batch_001/a.jpg"},
            "empty keys": {**good, "keys": []},
            "non-str key": {**good, "keys": [1]},
            "unknown key": {**good, "keys": ["batch_001/zzz.jpg"]},
            "unknown batch": {**good, "batch": "x\ty"},
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


class TestRemoteTarget(unittest.TestCase):
    def test_round_trip(self):
        for host in ("node01.cluster.example", "node_01", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                t = RemoteTarget(host, 8443, "tok-en_1", FP)
                self.assertEqual(RemoteTarget.parse(t.to_uri()), t)
        self.assertEqual(
            RemoteTarget("h", 1, "t", FP).to_uri(), f"ir://h:1/?token=t&fp=sha256:{FP}"
        )

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
