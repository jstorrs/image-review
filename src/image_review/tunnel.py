"""SSH local port-forward to a server only reachable through a login node.

Must stay importable without pygame/numpy/skimage. The tunnel carries the
already-pinned TLS connection end to end; it adds no trust of its own.
"""

import logging
import re
import shlex
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .signals import TERMINATION_SIGNALS, interrupt_on

log = logging.getLogger(__name__)

POLL_SECONDS = 0.1
TERMINATE_WAIT_SECONDS = 5


_VIA_PATTERN = re.compile(r"[A-Za-z0-9._@%:\[\]/-]+")


class TunnelError(Exception):
    """The SSH tunnel could not be established."""


def parse_via(raw: str) -> str:
    """Validate an ssh destination (`user@host`, `host` or a config alias)."""
    if not raw:
        raise ValueError("destination is empty")
    if raw.startswith("-"):
        raise ValueError("destination must not start with '-'")
    if not _VIA_PATTERN.fullmatch(raw):
        raise ValueError("destination may only contain letters, digits and . _ @ % : [ ] / -")
    return raw


def _free_local_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _accepts_connections(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(TERMINATE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@contextmanager
def ssh_tunnel(via: str, host: str, port: int, *, ready_timeout: float = 120) -> Iterator[int]:
    """Forward a free 127.0.0.1 port to host:port through `ssh via`; yields the local port."""
    local = _free_local_port()
    target = f"[{host}]" if ":" in host else host
    argv = [
        "ssh",
        "-N",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ControlPath=none",  # no shared master: the forward must die with this process
        "-L", f"127.0.0.1:{local}:{target}:{port}",
        "--",
        via,
    ]  # fmt: skip
    log.debug("starting tunnel: %s", shlex.join(argv))  # host, ports and login node only; no credentials
    with interrupt_on(*TERMINATION_SIGNALS):  # so ssh is torn down
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL)  # stdin/stderr inherited for prompts and errors
        except FileNotFoundError:
            raise TunnelError("ssh not found on PATH; install an OpenSSH client to use --via") from None
        except OSError as e:
            raise TunnelError(f"cannot run ssh: {e.strerror or e}") from e
        try:
            _wait_ready(proc, local, via, ready_timeout)
            yield local
        finally:
            _stop(proc)


def _wait_ready(proc: subprocess.Popen, local: int, via: str, ready_timeout: float) -> None:
    deadline = time.monotonic() + ready_timeout
    while True:
        code = proc.poll()
        if code == 0:
            raise TunnelError(
                "ssh exited with status 0 before the tunnel was ready; it may have gone to the background "
                f"and still be forwarding 127.0.0.1:{local}. Stop it and remove ForkAfterAuthentication / ControlPersist from ssh_config."
            )
        if code is not None:
            raise TunnelError(f"ssh exited with status {code} before the tunnel was ready (see the ssh output above)")
        if _accepts_connections(local):
            return
        if time.monotonic() >= deadline:
            raise TunnelError(f"ssh tunnel via {via} was not ready after {ready_timeout:g} s")
        time.sleep(POLL_SECONDS)
