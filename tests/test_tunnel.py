import contextlib
import dataclasses
import json
import os
import signal
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

from image_review.tunnel import TunnelError, parse_ssh_host, parse_via, ssh_tunnel
from tests.fixtures import invoke_cli, make_work_dir, start_server, temp_dir

FAKE_SSH = f"""#!{sys.executable}
import json, os, signal, socket, sys, threading

argv = sys.argv[1:]
if os.environ.get("FAKE_SSH_MODE") == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.environ["FAKE_SSH_LOG"], "w") as f:
    json.dump({{"argv": argv, "pid": os.getpid()}}, f)
mode = os.environ.get("FAKE_SSH_MODE", "forward")
if mode == "exit":
    sys.exit(255)
if mode == "exit0":
    sys.exit(0)
if mode in ("hang", "stubborn"):
    signal.pause()
spec = argv[argv.index("-L") + 1]
_, lport, rest = spec.split(":", 2)
host, port = rest.rsplit(":", 1)
host = host.strip("[]")
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", int(lport)))
listener.listen()
signal.signal(signal.SIGTERM, lambda *a: os._exit(0))


def pump(src, dst):
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def serve(client):
    try:
        upstream = socket.create_connection((host, int(port)))
    except OSError:
        client.close()
        return
    threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
    pump(upstream, client)


while True:
    conn, _ = listener.accept()
    threading.Thread(target=serve, args=(conn,), daemon=True).start()
"""


@contextlib.contextmanager
def spy_popen():
    """Patch tunnel's Popen to record the processes it starts."""
    procs: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def factory(*args, **kwargs):
        procs.append(real_popen(*args, **kwargs))
        return procs[-1]

    with mock.patch("image_review.tunnel.subprocess.Popen", side_effect=factory):
        yield procs


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a terminated but unreaped child would still answer; Popen.wait() reaps ours
    return True


@unittest.skipIf(sys.platform == "win32", "fake ssh needs POSIX")
class FakeSshTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        ssh = bin_dir / "ssh"
        ssh.write_text(FAKE_SSH)
        ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "ssh.json"
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SSH_LOG": str(self.log),
            "FAKE_SSH_MODE": "forward",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def kill_if_alive(pid: int) -> None:
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL)

    def ssh_record(self) -> dict:
        return json.loads(self.log.read_text())


class TestParseVia(unittest.TestCase):
    def test_accepts(self):
        for ok in ["user@login.cluster", "login", "my-alias", "u@10.0.0.1"]:
            with self.subTest(ok=ok):
                self.assertEqual(parse_via(ok), ok)

    def test_rejects(self):
        for bad in [
            "-oProxyCommand=x",
            "",
            "a b",
            "a\nb",
            "a\tb",
            "a\x00b",
            "a\x1bb",
            "login$(cmd)",
            "login`cmd`",
            "a;b",
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_via(bad)


class TestParseSshHost(unittest.TestCase):
    def test_accepts(self):
        for ok in ["node042", "node042.example.org", "10.0.0.1", "[::1]", "a_b-c"]:
            with self.subTest(ok=ok):
                self.assertEqual(parse_ssh_host(ok), ok)

    def test_rejects(self):
        for bad in ["", "-oProxyCommand=x", "a@b", "u@node", "a b", "a\nb", "a;b", "a/b", "a%b", "$(x)"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_ssh_host(bad)


class TestSshTunnel(FakeSshTestCase):
    def test_argv_exact(self):
        with ssh_tunnel("me@login", "node1", 4242) as local:
            argv = self.ssh_record()["argv"]
        self.assertEqual(
            argv,
            [
                "-N",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=30",
                "-o", "ControlPath=none",
                "-L", f"127.0.0.1:{local}:node1:4242",
                "--",
                "me@login",
            ],
        )  # fmt: skip

    def test_terminated_after_normal_exit(self):
        with ssh_tunnel("login", "node1", 1):
            pid = self.ssh_record()["pid"]
            self.assertTrue(pid_alive(pid))
        self.assertFalse(pid_alive(pid))

    def test_terminated_when_body_raises(self):
        with self.assertRaises(RuntimeError), ssh_tunnel("login", "node1", 1):
            pid = self.ssh_record()["pid"]
            raise RuntimeError("boom")
        self.assertFalse(pid_alive(pid))

    def test_terminated_on_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt), ssh_tunnel("login", "node1", 1):
            pid = self.ssh_record()["pid"]
            raise KeyboardInterrupt
        self.assertFalse(pid_alive(pid))

    def test_ssh_exit_is_tunnel_error(self):
        os.environ["FAKE_SSH_MODE"] = "exit"
        with self.assertRaisesRegex(TunnelError, "ssh exited with status 255"), ssh_tunnel("login", "node1", 1):
            pass

    def test_timeout_is_tunnel_error_and_cleans_up(self):
        os.environ["FAKE_SSH_MODE"] = "hang"
        with (
            spy_popen() as procs,
            self.assertRaisesRegex(TunnelError, "not ready"),
            ssh_tunnel("login", "node1", 1, ready_timeout=0.5),
        ):
            pass
        self.assertFalse(pid_alive(procs[0].pid))

    def test_ssh_exit_zero_mentions_background(self):
        os.environ["FAKE_SSH_MODE"] = "exit0"
        with self.assertRaisesRegex(TunnelError, "status 0.*background"), ssh_tunnel("login", "node1", 1):
            pass

    def test_ipv6_target_bracketed(self):
        with ssh_tunnel("login", "::1", 99):
            spec = self.ssh_record()["argv"][self.ssh_record()["argv"].index("-L") + 1]
        self.assertTrue(spec.endswith(":[::1]:99"), spec)

    def test_kill_fallback(self):
        os.environ["FAKE_SSH_MODE"] = "stubborn"

        def interrupt_once_ssh_ignores_sigterm(port):
            if self.log.exists():  # the fake writes its log only after ignoring SIGTERM
                raise KeyboardInterrupt
            return False

        with (
            mock.patch("image_review.tunnel.TERMINATE_WAIT_SECONDS", 0.3),
            mock.patch("image_review.tunnel._accepts_connections", side_effect=interrupt_once_ssh_ignores_sigterm),
            spy_popen() as procs,
            self.assertRaises(KeyboardInterrupt),
            ssh_tunnel("login", "node1", 1, ready_timeout=30),
        ):
            pass
        self.assertEqual(procs[0].returncode, -signal.SIGKILL)
        self.assertFalse(pid_alive(procs[0].pid))

    def test_keyboard_interrupt_during_readiness(self):
        os.environ["FAKE_SSH_MODE"] = "hang"
        with (
            spy_popen() as procs,
            mock.patch("image_review.tunnel._accepts_connections", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
            ssh_tunnel("login", "node1", 1),
        ):
            pass
        self.assertFalse(pid_alive(procs[0].pid))

    def test_sigterm_reaps_ssh(self):
        script = (
            "import sys; from image_review.tunnel import ssh_tunnel\n"
            "with ssh_tunnel('login', 'node1', 1):\n"
            "    print('up', flush=True); sys.stdin.read()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stdin.close)
        self.assertEqual(proc.stdout.readline().strip(), "up")
        ssh_pid = self.ssh_record()["pid"]
        self.addCleanup(self.kill_if_alive, ssh_pid)
        proc.terminate()
        proc.wait(10)
        for _ in range(50):
            if not pid_alive(ssh_pid):
                break
            time.sleep(0.1)
        self.assertFalse(pid_alive(ssh_pid))

    def test_ssh_missing(self):
        with (
            mock.patch.dict(os.environ, {"PATH": str(self.tmp / "empty")}),
            self.assertRaisesRegex(TunnelError, "ssh not found on PATH"),
            ssh_tunnel("login", "node1", 1),
        ):
            pass


class TestUnreachableHint(unittest.TestCase):
    HINT = "login node may not be able to reach"

    def run_status(self, error: Exception):
        from image_review.connection import RemoteTarget

        target = RemoteTarget("127.0.0.1", 1, "tok", "0" * 64)

        class Boom:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                raise error

            def __exit__(self, *a):
                pass

        with (
            mock.patch("image_review.remote.RemoteStore", Boom),
            mock.patch("image_review.tunnel.ssh_tunnel", lambda *a, **k: contextlib.nullcontext(1)),
        ):
            return invoke_cli("status", "--remote", target.to_uri(), "--via", "me@login")

    def test_hint_on_transport_failure(self):
        from image_review.remote import RemoteError

        err = RemoteError("connection lost: ConnectionResetError")
        err.__cause__ = ConnectionResetError()
        result = self.run_status(err)
        self.assertIn(self.HINT, result.output)

    def test_no_hint_on_parse_error(self):
        from image_review.remote import RemoteError

        err = RemoteError("malformed manifest from server")
        err.__cause__ = ValueError()
        result = self.run_status(err)
        self.assertIn("Cannot reach server", result.output)
        self.assertNotIn(self.HINT, result.output)


class TestCliVia(FakeSshTestCase):
    def setUp(self):
        super().setUp()
        self.work_dir = self.tmp / "work"
        self.work_dir.mkdir()
        make_work_dir(self.work_dir)
        self.server, self.target, stop = start_server(self.work_dir)
        self.addCleanup(stop)

    def invoke(self, *args, **env):
        return invoke_cli(*args, env=env)

    def test_status_through_tunnel_identical_to_direct(self):
        direct = self.invoke("status", "--remote", self.target.to_uri())
        tunneled = self.invoke("status", "--remote", self.target.to_uri(), "--via", "me@login")
        self.assertEqual(direct.exit_code, 0, direct.output)
        self.assertEqual(tunneled.exit_code, 0, tunneled.output)
        self.assertEqual(tunneled.stdout, direct.stdout)
        record = self.ssh_record()
        self.assertEqual(record["argv"][-1], "me@login")
        self.assertRegex(
            record["argv"][record["argv"].index("-L") + 1], rf"127\.0\.0\.1:\d+:{self.target.host}:{self.target.port}"
        )
        self.assertFalse(pid_alive(record["pid"]))

    def test_via_adds_no_second_warning(self):
        result = self.invoke("status", "--remote", self.target.to_uri(), "--via", "me@login")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stderr.count("is deprecated"), 1)

    def test_via_envvar(self):
        result = self.invoke("status", IMAGE_REVIEW_REMOTE=self.target.to_uri(), IMAGE_REVIEW_VIA="me@login")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(pid_alive(self.ssh_record()["pid"]))

    def test_via_requires_remote(self):
        result = self.invoke("status", "--work-dir", str(self.work_dir), "--via", "me@login")
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--via requires --remote", result.output)
        self.assertFalse(self.log.exists())

    def test_via_envvar_ignored_in_local_mode(self):
        result = self.invoke("status", "--work-dir", str(self.work_dir), IMAGE_REVIEW_VIA="me@login")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Overall: 4 images", result.output)
        self.assertFalse(self.log.exists())

    def test_invalid_via(self):
        result = self.invoke("status", "--remote", self.target.to_uri(), "--via", "-oProxyCommand=x")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Invalid --via", result.output)
        self.assertFalse(self.log.exists())

    def test_ssh_failure_message(self):
        os.environ["FAKE_SSH_MODE"] = "exit"
        result = self.invoke("status", "--remote", self.target.to_uri(), "--via", "me@login")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("ssh exited", result.output)
        self.assertNotIn(self.target.token, result.output)

    def test_pin_enforced_through_tunnel(self):
        uri = dataclasses.replace(self.target, fingerprint="0" * 64).to_uri()
        result = self.invoke("status", "--remote", uri, "--via", "me@login")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("does NOT match", result.output)
        self.assertIn("(via me@login)", result.output)
        self.assertNotIn(self.target.token, result.output)
        self.assertFalse(pid_alive(self.ssh_record()["pid"]))


class TestImports(unittest.TestCase):
    def test_tunnel_is_lightweight(self):
        import subprocess

        code = "import sys, image_review.tunnel; assert not {'pygame','numpy','skimage'} & set(sys.modules)"
        subprocess.run([sys.executable, "-c", code], check=True)


if __name__ == "__main__":
    unittest.main()
