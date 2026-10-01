"""HTTPS + bearer-token server exposing a ReviewStore.

Must stay importable without pygame/numpy/skimage. Original image_ids never
leave the store; clients only ever see keys (preprocessed paths).
"""

import datetime
import hmac
import ipaddress
import json
import os
import re
import secrets
import ssl
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast, get_args
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .connection import (
    API_VERSION,
    RemoteTarget,
    cert_fingerprint,
    package_version,
    parse_reviewer,
)
from .store import MarkMode, ReviewStore, Verdict

MAX_BODY_BYTES = 1 << 20
CERT_VALIDITY = datetime.timedelta(days=30)
HANDLER_TIMEOUT_SECONDS = 60
HANDSHAKE_TIMEOUT_SECONDS = 10


class BadRequest(Exception):
    pass


@dataclass(frozen=True)
class MarkRequest:
    keys: list[str]
    status: Verdict
    pass_number: int
    reviewer: str
    mode: MarkMode


@dataclass(frozen=True)
class UndoRequest:
    pass_number: int
    reviewer: str


BODY_ROUTES = frozenset({"/mark", "/undo"})  # the POST routes, the only ones that take a body


def parse_pass(raw: str | None) -> int:
    try:
        value = int(raw) if raw is not None else 0
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
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BadRequest("pass must be an integer >= 1")
    return value


def _reviewer_field(value: object) -> str:
    try:
        return parse_reviewer(value)
    except ValueError as e:
        raise BadRequest(str(e)) from e


def parse_mark(body: bytes, known_keys: frozenset[str]) -> MarkRequest:
    data = _json_object(body)
    keys, status, pass_number, reviewer, mode = (data.get(k) for k in ("keys", "status", "pass", "reviewer", "mode"))
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
        raise BadRequest("keys must be a non-empty list of strings")
    if not all(k in known_keys for k in keys):
        raise BadRequest("unknown key")
    if status not in get_args(Verdict):
        raise BadRequest("status must be CLEAN or DIRTY")
    pass_number = _pass_field(pass_number)
    reviewer = _reviewer_field(reviewer)
    if mode not in get_args(MarkMode):
        raise BadRequest("mode must be single or grid")
    # The membership checks above guarantee these are valid Literal members; get_args() cannot narrow.
    return MarkRequest(
        keys=keys, status=cast(Verdict, status), pass_number=pass_number, reviewer=reviewer, mode=cast(MarkMode, mode)
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


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: ReviewStore, token: str):
        super().__init__(address, ReviewHandler)
        self.store = store
        self.token = token
        self.store_lock = threading.Lock()
        rows = store.manifest()
        self.known_keys = frozenset(r.key for r in rows)

    def handle_error(self, request, client_address) -> None:
        exc_type = sys.exc_info()[0]
        print(f"connection error: {exc_type.__name__ if exc_type else 'unknown'}", file=sys.stderr)


class ReviewHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = HANDLER_TIMEOUT_SECONDS
    disable_nagle_algorithm = True  # headers and body are separate writes
    server: ReviewServer

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
            print(f"internal error: {type(e).__name__}", file=sys.stderr)
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
        if method == "GET" and url.path == "/version":
            return json_reply({"api": API_VERSION, "version": package_version()})
        if method == "GET" and url.path == "/manifest":
            with lock:
                rows = store.manifest()
            return json_reply([{"key": r.key, "batch": r.batch} for r in rows])
        if method == "GET" and url.path == "/image":
            return self._image(query)
        if method == "GET" and url.path == "/statuses":
            pass_number = parse_pass(query.get("pass", [None])[0])
            with lock:
                statuses = store.statuses(pass_number)
            return json_reply(statuses)
        if method == "GET" and url.path == "/current_pass":
            with lock:
                current = store.current_pass()
            return json_reply({"pass": current})
        if method == "GET" and url.path == "/skipped":
            with lock:
                skipped = store.skipped()
            return json_reply(None if skipped is None else {"failed": skipped.failed, "ignored": skipped.ignored})
        if has_body and url.path == "/mark":
            req = parse_mark(self._read_body(), self.server.known_keys)
            with lock:
                changed = store.mark(req.keys, req.status, req.pass_number, reviewer=req.reviewer, mode=req.mode)
            return json_reply(changed)
        if has_body and url.path == "/undo":
            undo = parse_undo(self._read_body())
            with lock:
                changed = store.undo(undo.pass_number, reviewer=undo.reviewer)
            return json_reply(changed)  # keys only, like /mark
        return Reply(404, close=True)

    def _image(self, query: dict[str, list[str]]) -> Reply:
        keys = query.get("key", [])
        if len(keys) != 1:
            raise BadRequest("key required")
        try:
            data = self.server.store.image_bytes(keys[0])  # no lock: read-only file access
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
        # command and path are attacker-controlled: escape control chars; drop the query
        command = str(getattr(self, "command", None) or "-")
        path = str(getattr(self, "path", "-")).split("?", 1)[0]
        print(f"{_escape(command)} {_escape(path)} {code}", file=sys.stderr)

    def log_message(self, format, *args) -> None:
        pass  # stdlib error messages can echo request lines; log_request is the only log


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
    if host.strip() in ("", "0.0.0.0", "::") or _is_unspecified(host.strip()):
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


def write_connection_file(target: RemoteTarget) -> Path:
    """Write the connection string to ~/.image-review/ (dir 0700, file 0600, never briefly looser)."""
    directory = Path.home() / ".image-review"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError(f"{directory} is not a directory owned by you")
    if info.st_mode & 0o077:
        directory.chmod(0o700)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", target.host)
    path = directory / f"connection-{name}-{target.port}.txt"
    path.unlink(missing_ok=True)  # stale file from a crashed run
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(target.to_uri() + "\n")
    return path
