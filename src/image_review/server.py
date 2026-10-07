"""Bearer-token server exposing a ReviewStore: HTTPS over TCP, or plain HTTP on a Unix socket.

Must stay importable without pygame/numpy/skimage. Original image_ids never
leave the store; clients only ever see keys (preprocessed paths).
"""

import datetime
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import socketserver
import ssl
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .connection import (
    API_VERSION,
    RemoteTarget,
    cert_fingerprint,
    is_int_at_least,
    package_version,
    parse_reviewer,
)
from .status import MARK_MODES, VERDICTS, Key, MarkMode, Verdict, parse_choice
from .store import ReviewStore

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 1 << 20
CERT_VALIDITY = datetime.timedelta(days=30)
HANDLER_TIMEOUT_SECONDS = 60
HANDSHAKE_TIMEOUT_SECONDS = 10


class BadRequest(Exception):
    pass


@dataclass(frozen=True)
class MarkRequest:
    keys: list[Key]
    status: Verdict
    pass_number: int
    reviewer: str
    mode: MarkMode


@dataclass(frozen=True)
class UndoRequest:
    pass_number: int
    reviewer: str


BODY_ROUTES = frozenset({"/mark", "/undo"})  # the POST routes, the only ones that take a body


def _one(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name, [])
    if len(values) != 1:
        raise BadRequest(f"{name} must appear exactly once")
    return values[0]


def parse_pass(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise BadRequest("pass must be an integer >= 1")
    return value


def _json_object(body: bytes) -> dict:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError) as e:
        raise BadRequest("invalid JSON") from e
    if not isinstance(data, dict):
        raise BadRequest("body must be an object")
    return data


def _pass_field(value: object) -> int:
    if not is_int_at_least(value, 1):
        raise BadRequest("pass must be an integer >= 1")
    return value


def _reviewer_field(value: object) -> str:
    try:
        return parse_reviewer(value)
    except ValueError as e:
        raise BadRequest(str(e)) from e


def parse_mark(body: bytes, known_keys: frozenset[Key]) -> MarkRequest:
    data = _json_object(body)
    keys, status, pass_number, reviewer, mode = (data.get(k) for k in ("keys", "status", "pass", "reviewer", "mode"))
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
        raise BadRequest("keys must be a non-empty list of strings")
    if not all(k in known_keys for k in keys):
        raise BadRequest("unknown key")
    verdict = parse_choice(status, VERDICTS)
    if verdict is None:
        raise BadRequest("status must be CLEAN or DIRTY")
    pass_number = _pass_field(pass_number)
    reviewer = _reviewer_field(reviewer)
    mark_mode = parse_choice(mode, MARK_MODES)
    if mark_mode is None:
        raise BadRequest("mode must be single or grid")
    return MarkRequest(
        keys=[Key(k) for k in keys],
        status=verdict,
        pass_number=pass_number,
        reviewer=reviewer,
        mode=mark_mode,
    )


def parse_undo(body: bytes) -> UndoRequest:
    data = _json_object(body)
    return UndoRequest(pass_number=_pass_field(data.get("pass")), reviewer=_reviewer_field(data.get("reviewer")))


def generate_cert(host: str) -> tuple[x509.Certificate, bytes]:
    """Self-signed EC P-256 certificate; returns (certificate, PEM private key)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + CERT_VALIDITY)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert, key_pem


def make_ssl_context(cert: x509.Certificate, key_pem: bytes) -> ssl.SSLContext:
    """Load the cert/key via a private temp dir that is removed immediately."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory() as tmp:  # mkdtemp creates it 0700
        cert_path, key_path = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.touch(mode=0o600)
        key_path.write_bytes(key_pem)
        context.load_cert_chain(cert_path, key_path)
    return context


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    close: bool = False  # close the connection after replying (e.g. unread request body)


def json_reply(payload) -> Reply:
    return Reply(200, json.dumps(payload).encode())


class ReviewHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = HANDLER_TIMEOUT_SECONDS
    disable_nagle_algorithm = True  # headers and body are separate writes
    server: "ReviewServer"

    def setup(self) -> None:
        # Bound pre-auth time: the TLS handshake (deferred from accept) gets a short timeout.
        # A failure propagates to server.handle_error, which logs the class name and closes.
        self.request.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
        self.request.do_handshake()
        super().setup()  # applies the normal timeout

    def end_headers(self) -> None:
        # Here rather than in _send so stdlib send_error responses carry them too
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_error(self, code, message=None, explain=None) -> None:
        if self.request_version == "HTTP/0.9":  # unparseable request line: still send a real response
            self.request_version = "HTTP/1.1"
        super().send_error(code, message, explain)

    def _send(self, reply: Reply) -> None:
        if reply.close:
            self.close_connection = True
        self.send_response(reply.status)
        if reply.body:
            self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if reply.body:  # a client done with an empty reply may have closed; writing b"" would then hit EPIPE
            self.wfile.write(reply.body)

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.token}".encode()
        given = self.headers.get("Authorization", "").encode("utf-8", "replace")
        return hmac.compare_digest(given, expected)

    def _handle(self, method: str) -> None:
        if not self._authorized():
            self._send(Reply(401, close=True))  # any request body is left unread
            return
        try:
            reply = self._route(method)
        except BadRequest:
            reply = Reply(400, close=True)
        except Exception as e:  # noqa: BLE001 - any store failure must become a 500, not a dead connection
            log.error("internal error: %s", type(e).__name__)  # noqa: TRY400 - class name only, never the message
            reply = Reply(500, close=True)
        self._send(reply)

    def _route(self, method: str) -> Reply:
        if "Transfer-Encoding" in self.headers:
            raise BadRequest("Transfer-Encoding not supported")
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        has_body = method == "POST" and url.path in BODY_ROUTES
        if not has_body and not all(v.strip() == "0" for v in self.headers.get_all("Content-Length", [])):
            raise BadRequest("unexpected request body")
        store, lock = self.server.store, self.server.store_lock
        match (method, url.path):
            case ("GET", "/version"):
                return json_reply({"api": API_VERSION, "version": package_version()})
            case ("GET", "/manifest"):
                with lock:
                    rows = store.manifest()
                return json_reply([{"key": r.key, "batch": r.batch} for r in rows])
            case ("GET", "/image"):
                return self._image(query)
            case ("GET", "/statuses"):
                pass_number = parse_pass(_one(query, "pass"))
                with lock:
                    statuses = store.statuses(pass_number)
                return json_reply(statuses)
            case ("GET", "/current_pass"):
                with lock:
                    current = store.current_pass()
                return json_reply({"pass": current})
            case ("GET", "/skipped"):
                with lock:
                    skipped = store.skipped()
                return json_reply({"failed": skipped.failed, "ignored": skipped.ignored})
            case ("POST", "/mark"):
                req = parse_mark(self._read_body(), self.server.known_keys)
                with lock:
                    changed = store.mark(req.keys, req.status, req.pass_number, reviewer=req.reviewer, mode=req.mode)
                return json_reply(changed)
            case ("POST", "/undo"):
                undo = parse_undo(self._read_body())
                with lock:
                    changed = store.undo(undo.pass_number, reviewer=undo.reviewer)
                return json_reply(changed)  # keys only, like /mark
            case _:
                return Reply(404, close=True)

    def _image(self, query: dict[str, list[str]]) -> Reply:
        key = _one(query, "key")
        if key not in self.server.known_keys:
            return Reply(404)  # as the store answers an unknown key
        try:
            data = self.server.store.image_bytes(Key(key))  # no lock: read-only file access
        except (KeyError, ValueError, OSError):
            return Reply(404)
        return Reply(200, data, "image/jpeg")

    def _read_body(self) -> bytes:
        lengths = self.headers.get_all("Content-Length", [])
        try:
            length = int(lengths[0]) if len(lengths) == 1 else -1
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY_BYTES:
            raise BadRequest("missing, repeated or oversized Content-Length")
        return self.rfile.read(length)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_OTHER(self) -> None:
        self._handle(self.command)

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_OTHER

    def log_request(self, code="-", size="-") -> None:
        # One INFO line per response: peer, method, path, status. The formatter adds the time.
        # Command and path are attacker-controlled: escape control chars; drop the query (it carries keys)
        peer = self._peer()
        command = str(getattr(self, "command", None) or "-")
        path = str(getattr(self, "path", "-")).split("?", 1)[0]
        log.info("%s %s %s %s", _escape(peer), _escape(command), _escape(path), code)

    def _peer(self) -> str:
        return str(self.client_address[0])

    def log_message(self, format, *args) -> None:
        pass  # stdlib error messages can echo request lines; log_request is the only log


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: str | tuple[str, int],
        store: ReviewStore,
        token: str,
        *,
        handler: type[ReviewHandler] = ReviewHandler,
    ):
        # Read the manifest before binding, so a failure here leaves no socket (or socket file) behind
        rows = store.manifest()
        self.known_keys: frozenset[Key] = frozenset(r.key for r in rows)
        self.store = store
        self.token = token
        self.store_lock = threading.Lock()
        super().__init__(address, handler)  # type: ignore[arg-type]  # typeshed omits AF_UNIX path addresses

    def handle_error(self, request, client_address) -> None:
        exc_type = sys.exc_info()[0]
        log.warning("connection error: %s", exc_type.__name__ if exc_type else "unknown")


_LOCAL_HOST = re.compile(r"(localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?", re.IGNORECASE | re.ASCII)


def is_local_host(values: list[str]) -> bool:
    """True for exactly one Host header naming the loopback (localhost, 127.0.0.1, [::1]), optionally with a port."""
    if len(values) != 1:
        return False
    match = _LOCAL_HOST.fullmatch(values[0].strip(" \t"))  # optional whitespace is not part of the value
    if match is None:
        return False
    port = match.group(2)
    return port is None or 1 <= int(port) <= 65535


class UnixReviewHandler(ReviewHandler):
    """Plain HTTP over a Unix socket, reached through `ssh -L`. Experimental."""

    disable_nagle_algorithm = False  # TCP_NODELAY fails on AF_UNIX

    def setup(self) -> None:
        # No TLS handshake to bound: skip ReviewHandler.setup. The handler timeout still bounds pre-auth time.
        socketserver.StreamRequestHandler.setup(self)

    def _peer(self) -> str:
        return "unix"  # accept() gives no peer address on AF_UNIX

    def _handle(self, method: str) -> None:
        # Before the token check: a browser tricked by DNS rebinding sends the attacker's host name
        if not is_local_host(self.headers.get_all("Host", [])):
            self._send(Reply(400, close=True))
            return
        super()._handle(method)


class UnixReviewServer(ReviewServer):
    allow_reuse_address = False

    def __init__(self, path: Path, store: ReviewStore, token: str):
        self.address_family = socket.AF_UNIX  # here, not on the class: Windows has no AF_UNIX and must still import
        self._bound: tuple[int, int] | None = None  # (st_dev, st_ino) of our socket file; a failed bind closes
        super().__init__(str(path), store, token, handler=UnixReviewHandler)

    def server_bind(self) -> None:
        # Not HTTPServer.server_bind: it treats the address as (host, port)
        socketserver.TCPServer.server_bind(self)
        path = str(self.server_address)
        info = os.lstat(path)
        self._bound = (info.st_dev, info.st_ino)  # before chmod, so a failing chmod still lets server_close unlink
        # chmod before listen(): connecting to a bound socket that is not listening is refused, so no connect
        # can succeed before the mode is 0600. Also narrows the ACL mask under a default ACL.
        os.chmod(path, 0o600)
        self.server_name = "localhost"
        self.server_port = 0

    def server_close(self) -> None:
        # Unlink before closing: while our socket is open its inode cannot be freed and reused by a replacement
        try:
            if self._bound is not None:
                path = str(self.server_address)
                bound, self._bound = self._bound, None
                try:
                    info = os.lstat(path)
                    if (info.st_dev, info.st_ino) == bound:  # leave alone a file that replaced ours
                        os.unlink(path)
                except FileNotFoundError:
                    pass
        finally:
            super().server_close()  # the listening socket is closed even if the unlink fails


def _escape(text: str) -> str:
    return text.encode("unicode_escape").decode("ascii")


_WILDCARD_MESSAGE = "Refusing to bind a wildcard address; pass this node's hostname with --bind."


def _is_unspecified(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_unspecified
    except ValueError:
        return False


def make_server(store: ReviewStore, host: str, port: int) -> tuple[ReviewServer, RemoteTarget]:
    """Create the TLS server. Raises ValueError for a wildcard or unadvertisable host, OSError if binding fails.

    No listening socket is left behind on failure.
    """
    if not host.strip() or _is_unspecified(host.strip()):
        raise ValueError(_WILDCARD_MESSAGE)
    token = secrets.token_urlsafe(16)
    cert, key_pem = generate_cert(host)
    context = make_ssl_context(cert, key_pem)
    server = ReviewServer((host, port), store, token)
    try:
        if _is_unspecified(str(server.server_address[0])):  # e.g. "0" binds to 0.0.0.0
            raise ValueError(_WILDCARD_MESSAGE)
        # Handshake happens in the handler thread so a stalled client can't block accept()
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        fingerprint = cert_fingerprint(cert.public_bytes(serialization.Encoding.DER))
        target = RemoteTarget(host=host, port=server.server_address[1], token=token, fingerprint=fingerprint)
        try:
            RemoteTarget.parse(target.to_uri())
        except ValueError as e:
            raise ValueError(
                f"--bind {host!r} cannot be advertised to clients ({e}); use a hostname or dotted IPv4 address."
            ) from None
    except BaseException:
        server.server_close()
        raise
    return server, target


def private_dir() -> Path:
    """Return ~/.image-review, created or tightened to 0700 and checked to be ours."""
    directory = Path.home() / ".image-review"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError(f"{directory} is not a directory owned by you")
    if info.st_mode & 0o077:
        directory.chmod(0o700)
    return directory


def safe_name(text: str) -> str:
    """Replace anything outside [A-Za-z0-9._-] so text is safe in a file name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)


def write_private_file(name: str, text: str) -> Path:
    """Write text to ~/.image-review/<name> (dir 0700, file 0600, never briefly looser)."""
    path = private_dir() / name
    path.unlink(missing_ok=True)  # stale file from a crashed run
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def write_connection_file(target: RemoteTarget) -> Path:
    """Write the connection string to ~/.image-review/ as a private file."""
    return write_private_file(f"connection-{safe_name(target.host)}-{target.port}.txt", target.to_uri() + "\n")


SUN_PATH_SIZE = 104 if sys.platform == "darwin" else 108  # sockaddr_un.sun_path, including the NUL
_UNSAFE_PATH_CHARS = re.compile(r"[:\x00-\x1f\x7f-\x9f]")


def parse_socket_path(raw: str | os.PathLike[str]) -> Path:
    """Absolute socket path that fits sun_path and can be written in `ssh -L port:path`. Raises ValueError."""
    path = Path(os.path.abspath(raw))  # ssh needs an absolute path
    if _UNSAFE_PATH_CHARS.search(str(path)):
        raise ValueError(f"Socket path {str(path)!r} must not contain ':' or control characters.")
    length = len(os.fsencode(path))
    if length >= SUN_PATH_SIZE:
        raise ValueError(
            f"Socket path is {length} bytes; the limit is {SUN_PATH_SIZE - 1}. Use a shorter path such as /tmp/ir.sock."
        )
    return path


def clear_stale_socket(path: Path) -> None:
    """Remove a socket file left by a dead server. Raises ValueError if something else, or a live server, is there."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise ValueError(f"{path} exists and is not a socket.")
    if info.st_uid != os.getuid():
        raise ValueError(f"{path} is owned by another user.")
    # Not race-free: two servers starting on one path, or a live server whose backlog is full on macOS
    # (ECONNREFUSED there), can see a live socket as stale and unlink it.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.setblocking(False)  # never wait on a listener; Linux answers a full backlog with EAGAIN
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink(missing_ok=True)
            return
        except FileNotFoundError:
            return  # removed since the lstat
        except BlockingIOError:
            pass  # listening, backlog full
    raise ValueError(f"Another server is listening on {path}.")


def default_socket_path() -> Path:
    """~/.image-review/serve-<host>-<pid>.sock; the short host name saves sun_path bytes."""
    short_host = socket.gethostname().split(".")[0]
    return private_dir() / f"serve-{safe_name(short_host)}-{os.getpid()}.sock"


def make_unix_server(store: ReviewStore, path: Path) -> tuple[UnixReviewServer, str]:
    """Create the Unix-socket server; returns (server, token). Raises ValueError for a bad or busy path,
    OSError if binding fails.

    No socket file is left behind on failure.
    """
    path = parse_socket_path(path)
    clear_stale_socket(path)
    token = secrets.token_urlsafe(16)
    server = UnixReviewServer(path, store, token)
    return server, token
