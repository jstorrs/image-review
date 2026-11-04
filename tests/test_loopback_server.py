"""The loopback TCP server (`make_loopback_server`): the socket server's request tests over 127.0.0.1, plus the
port-aware Host rule, the Origin rule and the TCP specifics."""

import http.client
import itertools
import json
import socket
import sys
import time
import unittest
from unittest import mock

from image_review.server import LOOPBACK_HOST, LoopbackReviewServer, ReviewServer, make_loopback_server
from tests import test_socket_server as socket_tests
from tests.fixtures import make_work_dir, start_loopback_server, temp_dir


class LoopbackServerTestCase(socket_tests.SocketServerTestCase):
    """SocketServerTestCase over 127.0.0.1:port; requests send Host: 127.0.0.1:port unless told otherwise."""

    PEER = LOOPBACK_HOST
    # Undoes only SocketServerTestCase's AF_UNIX skip, which TCP does not need. It also clears any skip a reused
    # socket test class adds later, so give such a class its own loopback skip.
    __unittest_skip__ = False

    def start_server(self) -> None:
        self.server, self.token, stop = start_loopback_server(self.work_dir)
        self.addCleanup(stop)
        self.port = self.server.host_port
        self.host = f"{LOOPBACK_HOST}:{self.port}"

    def request(self, method, path, body=None, token="default", host=None, instance="default", origin=None):
        return super().request(method, path, body, token, self.host if host is None else host, instance, origin)

    def connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(LOOPBACK_HOST, self.port, timeout=10)

    def connect(self) -> socket.socket:
        sock = socket.create_connection((LOOPBACK_HOST, self.port), timeout=10)
        self.addCleanup(sock.close)
        return sock

    def raw(self, data: bytes) -> bytes:
        # The socket tests' raw requests send Host: localhost, which socket mode takes; here only 127.0.0.1:port is
        data = data.replace(b"\r\nHost: localhost\r\n", f"\r\nHost: {self.host}\r\n".encode())
        sock = self.connect()
        sock.sendall(data)
        return socket_tests.read_until_closed(sock, 10)


class TestLoopbackRequests(LoopbackServerTestCase, socket_tests.TestSocketRequests):
    pass


class TestLoopbackInstance(LoopbackServerTestCase, socket_tests.TestServerInstance):
    def test_each_start_gets_a_new_instance(self):
        other_dir = temp_dir(self)
        make_work_dir(other_dir)
        other, _, stop = start_loopback_server(other_dir)
        self.addCleanup(stop)
        self.assertNotEqual(other.instance, self.server.instance)


class TestLoopbackConnectionLimit(LoopbackServerTestCase, socket_tests.TestSocketConnectionLimit):
    pass


class TestLoopbackRequestDeadline(LoopbackServerTestCase, socket_tests.TestSocketRequestDeadline):
    pass


class TestLoopbackAssets(LoopbackServerTestCase, socket_tests.TestPublicAssets):
    def test_tokenless_asset_request_closes_its_connection(self):
        resp, _ = self.request("GET", "/app.css", token=None)
        self.assertEqual((resp.status, resp.getheader("Connection")), (200, "close"))
        sock = self.connect()
        sock.sendall(f"GET /app.css HTTP/1.1\r\nHost: {self.host}\r\n\r\n".encode())
        reply = socket_tests.read_until_closed(sock, 5)  # the server closes after the reply
        self.assertTrue(reply.startswith(b"HTTP/1.1 200 "), reply[:60])
        resp, _ = self.request("GET", "/app.css")
        self.assertEqual((resp.status, resp.getheader("Connection")), (200, None))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return probe.getsockname()[1]


class TestLoopbackBinding(LoopbackServerTestCase):
    def test_bound_to_ipv4_loopback_on_the_real_port(self):
        self.assertIsInstance(self.server, LoopbackReviewServer)
        self.assertEqual(self.server.address_family, socket.AF_INET)
        self.assertEqual(self.server.server_address, (LOOPBACK_HOST, self.port))
        self.assertEqual(self.server.socket.getsockname(), (LOOPBACK_HOST, self.port))
        self.assertEqual((self.server.server_name, self.server.server_port), (LOOPBACK_HOST, self.port))
        self.assertGreater(self.port, 0)

    @unittest.skipUnless(hasattr(socket, "SO_REUSEPORT"), "no SO_REUSEPORT")
    def test_no_reuse_port(self):
        self.assertFalse(ReviewServer.allow_reuse_port)
        self.assertEqual(self.server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT), 0)

    @unittest.skipIf(sys.platform == "win32", "SO_REUSEADDR allows port sharing on Windows")
    def test_reuse_address_outside_windows(self):
        self.assertNotEqual(self.server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)

    def test_startup_does_no_name_lookup(self):
        # HTTPServer.server_bind's socket.getfqdn is a reverse DNS lookup: it can stall an offline machine
        with (
            mock.patch("socket.getfqdn", return_value=LOOPBACK_HOST) as getfqdn,
            mock.patch("socket.gethostbyaddr", side_effect=OSError) as gethostbyaddr,
        ):
            server, _ = make_loopback_server(self.server.store)
            server.server_close()
        getfqdn.assert_not_called()
        gethostbyaddr.assert_not_called()

    def test_ipv6_loopback_is_refused(self):
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind(("::1", 0))
        except OSError:
            self.skipTest("IPv6 loopback unavailable")
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            with self.assertRaises(ConnectionRefusedError):
                sock.connect(("::1", self.port))

    def test_a_port_in_use_raises_oserror(self):
        with self.assertRaises(OSError):
            make_loopback_server(self.server.store, self.port)  # this server is listening on it

    def test_restart_on_the_same_port_right_after_stop(self):
        # A client that the server closed first leaves the server's side of the connection in TIME_WAIT
        resp, _ = self.request("GET", "/current_pass", token=None)
        self.assertEqual((resp.status, resp.getheader("Connection")), (401, "close"))
        port = self.port
        self.server.shutdown()
        self.server.server_close()
        server, _ = make_loopback_server(self.server.store, port)
        server.server_close()
        self.assertEqual(server.host_port, port)

    def test_port_must_be_0_or_unprivileged(self):
        for port in (-1, 1, 80, 443, 1023, 65536, 100000):
            with self.subTest(port=port), self.assertRaises(ValueError):
                make_loopback_server(self.server.store, port)
        for port in (1024, 65535):
            with self.subTest(port=port):
                try:
                    server, _ = make_loopback_server(self.server.store, port)
                except OSError:  # in use on this machine: not the check under test
                    continue
                server.server_close()

    def test_token_reused_when_given(self):
        free = free_port()
        server, token = make_loopback_server(self.server.store, free, self.token)
        self.addCleanup(server.server_close)
        self.assertEqual((token, server.host_port), (self.token, free))


class TestLoopbackHost(LoopbackServerTestCase):
    def test_only_127_0_0_1_with_this_port_is_accepted(self):
        resp, data = self.request("GET", "/current_pass", host=f"127.0.0.1:{self.port}")
        self.assertEqual((resp.status, json.loads(data)), (200, {"pass": 1}))

    def test_any_other_host_is_400_before_the_token(self):
        other = self.port + 1 if self.port < 65535 else self.port - 1
        hosts = [
            f"localhost:{self.port}",  # browsers try ::1 first, where another local user could listen
            f"LOCALHOST:{self.port}",
            f"127.0.0.1:{other}",
            f"[::1]:{self.port}",
            f"evil:{self.port}",
            "127.0.0.1",
            "localhost",
        ]
        for host, token in itertools.product(hosts, (None, "default")):
            with self.subTest(host=host, token=token):
                resp, data = self.request("GET", "/current_pass", token=token, host=host)
                self.assertEqual((resp.status, data), (400, b""))
                self.assertEqual(resp.getheader("Connection"), "close")

    def test_missing_or_repeated_host_is_400(self):
        auth = f"Authorization: Bearer {self.token}\r\n".encode()
        host = f"Host: {self.host}\r\n".encode()
        for head in [b"GET /current_pass HTTP/1.0\r\n", b"GET /current_pass HTTP/1.1\r\n" + host + host]:
            with self.subTest(head=head):
                reply = self.raw(head + auth + b"\r\n")
                self.assertTrue(reply.startswith(b"HTTP/1.1 400 "), reply)


class TestLoopbackOrigin(LoopbackServerTestCase):
    def test_absent_or_this_origin_is_allowed(self):
        for origin in [None, f"http://127.0.0.1:{self.port}"]:
            with self.subTest(origin=origin):
                resp, data = self.request("GET", "/current_pass", origin=origin)
                self.assertEqual((resp.status, json.loads(data)), (200, {"pass": 1}))
                resp, _ = self.request("GET", "/", token=None, origin=origin)
                self.assertEqual(resp.status, 200)
        body = json.dumps(socket_tests.MARK_A).encode()
        resp, _ = self.request("POST", "/mark", body=body, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(resp.status, 200)

    def test_any_other_origin_is_400_before_the_token(self):
        other = self.port + 1 if self.port < 65535 else self.port - 1
        origins = [
            "http://evil.example",
            "null",
            f"https://127.0.0.1:{self.port}",
            f"http://127.0.0.1:{other}",
            "http://127.0.0.1",
            f"http://localhost:{self.port}",
            f"http://[::1]:{self.port}",
        ]
        for (method, path), origin in itertools.product([("GET", "/current_pass"), ("GET", "/app.js")], origins):
            with self.subTest(path=path, origin=origin):
                resp, data = self.request(method, path, token=None, origin=origin)  # 400, not 401
                self.assertEqual((resp.status, data), (400, b""))
                self.assertEqual(resp.getheader("Connection"), "close")


class TestLoopbackKeepAlive(LoopbackServerTestCase):
    def test_keep_alive_not_delayed_by_nagle(self):
        conn = self.connection()
        self.addCleanup(conn.close)
        headers = {"Host": self.host, "Authorization": f"Bearer {self.token}"}
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


if __name__ == "__main__":
    unittest.main()
