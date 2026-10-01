"""ReviewStore client for `image-review serve` over HTTPS with a pinned certificate.

Must stay importable without pygame/numpy/skimage. Images are only ever held
in memory; nothing is cached on disk.
"""

import hmac
import http.client
import json
import logging
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Self, get_args
from urllib.parse import urlencode

from .connection import API_VERSION, RemoteTarget, cert_fingerprint
from .store import (
    ManifestRow,
    MarkMode,
    SkippedCounts,
    Status,
    StoreUnavailable,
    Verdict,
)

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
MAX_WORKERS = 8

# Connection died while idle (server closes idle connections after 60 s): reconnect and retry once
_STALE_CONNECTION = (
    ConnectionResetError,  # includes http.client.RemoteDisconnected
    BrokenPipeError,
    ssl.SSLEOFError,
    ssl.SSLZeroReturnError,
    http.client.CannotSendRequest,
    http.client.ResponseNotReady,
)


class RemoteError(StoreUnavailable):
    """The server is unreachable or replied with something unusable."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status when the server answered, else None


class ApiMismatch(RemoteError):
    """The server speaks a different wire API version than this client."""


class FingerprintMismatch(RemoteError):
    """The server certificate does not match the pinned fingerprint."""


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that trusts exactly one certificate, checked on every (re)connect."""

    def __init__(self, host: str, port: int, fingerprint: str, timeout: float = TIMEOUT_SECONDS):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(host, port, context=context, timeout=timeout)
        self._fingerprint = fingerprint

    def connect(self) -> None:
        # http.client reconnects transparently, so the pin is checked here, before any request is sent
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        actual = cert_fingerprint(der) if der else ""
        if not hmac.compare_digest(actual.encode(), self._fingerprint.encode()):
            self.close()
            raise FingerprintMismatch("server certificate does not match the pinned fingerprint")


def _load_json(data: bytes):
    try:
        return json.loads(data)
    except (ValueError, RecursionError) as e:
        raise RemoteError("server sent invalid JSON") from e


def parse_manifest(data: bytes) -> list[ManifestRow]:
    payload = _load_json(data)
    if not isinstance(payload, list):
        raise RemoteError("malformed manifest from server")
    rows = []
    for entry in payload:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("key"), str)
            or not isinstance(entry.get("batch"), str)
        ):
            raise RemoteError("malformed manifest from server")
        rows.append(ManifestRow(key=entry["key"], batch=entry["batch"]))
    return rows


def parse_statuses(data: bytes) -> dict[str, Status]:
    payload = _load_json(data)
    valid = get_args(Status)
    if not isinstance(payload, dict) or not all(isinstance(v, str) and v in valid for v in payload.values()):
        raise RemoteError("malformed statuses from server")
    return payload


def parse_pass(data: bytes) -> int:
    payload = _load_json(data)
    value = payload.get("pass") if isinstance(payload, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RemoteError("malformed pass number from server")
    return value


def parse_version(data: bytes) -> int:
    payload = _load_json(data)
    value = payload.get("api") if isinstance(payload, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RemoteError("malformed version from server")
    return value


def parse_skipped(data: bytes) -> SkippedCounts | None:
    payload = _load_json(data)
    if payload is None:
        return None
    if not isinstance(payload, dict) or set(payload) != {"failed", "ignored"}:
        raise RemoteError("malformed skipped counts from server")
    counts = []
    for name in ("failed", "ignored"):
        value = payload[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RemoteError("malformed skipped counts from server")
        counts.append(value)
    return SkippedCounts(failed=counts[0], ignored=counts[1])


class RemoteStore:
    def __init__(self, target: RemoteTarget, connect_host: str | None = None, connect_port: int | None = None):
        self._host = connect_host or target.host
        self._port = connect_port or target.port
        self._fingerprint = target.fingerprint
        self._auth = {"Authorization": f"Bearer {target.token}"}
        self._local = threading.local()
        self._connections: list[PinnedHTTPSConnection] = []
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            conn.close()

    def _connection(self) -> PinnedHTTPSConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = PinnedHTTPSConnection(self._host, self._port, self._fingerprint)
            self._local.conn = conn
            with self._lock:
                self._connections.append(conn)
        return conn

    def _request(self, method: str, path: str, body: bytes | None = None, *, retry: bool = True) -> tuple[int, bytes]:
        """One request on this thread's connection; with `retry`, resent once on a fresh connection after a stale one."""
        headers = dict(self._auth)
        if body is not None:
            headers["Content-Type"] = "application/json"
        for attempt in (1, 2):
            conn = self._connection()
            try:
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                return response.status, response.read()
            except FingerprintMismatch:
                raise
            except _STALE_CONNECTION as e:
                conn.close()
                if attempt == 2 or not retry:
                    raise RemoteError(f"connection lost: {type(e).__name__}") from e
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                raise RemoteError(f"{type(e).__name__}: {e}") from e
        raise AssertionError("unreachable")

    def _get(self, path: str) -> bytes:
        status, data = self._request("GET", path)
        if status != 200:
            raise RemoteError(f"server returned HTTP {status}", status)
        return data

    def check_api(self) -> None:
        """Raise ApiMismatch unless the server speaks this client's wire API version."""
        advice = "install the same image-review version on both machines"
        try:
            server = parse_version(self._get("/version"))
        except RemoteError as e:
            if e.status == 404:
                raise ApiMismatch(f"server is too old to report its API version; {advice}") from e
            raise
        if server != API_VERSION:
            raise ApiMismatch(f"server speaks API v{server}, this client v{API_VERSION}; {advice}")

    def manifest(self) -> list[ManifestRow]:
        return parse_manifest(self._get("/manifest"))

    def image_bytes(self, key: str) -> bytes:
        status, data = self._request("GET", f"/image?{urlencode({'key': key})}")
        if status == 404:
            raise KeyError(key)
        if status != 200:
            raise RemoteError(f"server returned HTTP {status}", status)
        return data

    def image_bytes_many(self, keys: list[str]) -> dict[str, bytes]:
        futures = {key: self._pool.submit(self.image_bytes, key) for key in dict.fromkeys(keys)}
        found: dict[str, bytes] = {}
        try:
            for key, future in futures.items():
                try:
                    found[key] = future.result()
                except KeyError as exc:
                    log.warning("cannot load %s: %r", key, exc)
        except BaseException:
            for future in futures.values():
                future.cancel()
            raise
        return found

    def statuses(self, pass_number: int) -> dict[str, Status]:
        return parse_statuses(self._get(f"/statuses?{urlencode({'pass': pass_number})}"))

    def mark(
        self, keys: list[str], status: Verdict, pass_number: int, *, reviewer: str, mode: MarkMode
    ) -> dict[str, Status]:
        body = json.dumps(
            {"keys": keys, "status": status, "pass": pass_number, "reviewer": reviewer, "mode": mode}
        ).encode()
        code, data = self._request("POST", "/mark", body)
        if code != 200:
            raise RemoteError(f"server returned HTTP {code}", code)
        return parse_statuses(data)

    def undo(self, pass_number: int, *, reviewer: str) -> dict[str, Status]:
        # Not idempotent: a resend after a lost reply would undo a second mark. So it is sent once, on a
        # fresh connection (an idle one may have been closed by the server), and any failure is an outage.
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()  # the next request reconnects, and the pin is checked again
        body = json.dumps({"pass": pass_number, "reviewer": reviewer}).encode()
        code, data = self._request("POST", "/undo", body, retry=False)
        if code != 200:
            raise RemoteError(f"server returned HTTP {code}", code)
        return parse_statuses(data)

    def current_pass(self) -> int:
        return parse_pass(self._get("/current_pass"))

    def skipped(self) -> SkippedCounts | None:
        return parse_skipped(self._get("/skipped"))
