import hashlib
import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

# Wire API version, shared by client and server. Any change to request/response
# shapes or to the Status vocabulary must bump it.
API_VERSION = 2

_FP_PATTERN = re.compile(r"sha256:([0-9a-f]{64})")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_LABEL = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
_HOSTNAME_PATTERN = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")


def cert_fingerprint(der: bytes) -> str:
    """Lowercase hex SHA-256 of a DER-encoded certificate."""
    return hashlib.sha256(der).hexdigest()


def _check_host(host: str) -> None:
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError("Connection string has an invalid IPv6 host") from None
        if "%" in host:
            raise ValueError("Connection string host must not contain a zone id")
    elif re.fullmatch(r"[0-9.]+", host):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise ValueError("Connection string has an invalid IPv4 host") from None
    elif len(host) > 253 or not _HOSTNAME_PATTERN.fullmatch(host):
        raise ValueError("Connection string has an invalid host name")


@dataclass(frozen=True)
class RemoteTarget:
    host: str
    port: int
    token: str = field(repr=False)
    fingerprint: str  # lowercase hex SHA-256 of the server certificate (DER)

    def to_uri(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"ir://{host}:{self.port}/?token={self.token}&fp=sha256:{self.fingerprint}"

    @classmethod
    def parse(cls, uri: str) -> "RemoteTarget":
        parts = urlsplit(uri)
        if parts.scheme != "ir":
            raise ValueError("Connection string must start with ir://")
        if "@" in parts.netloc:
            raise ValueError("Connection string must not contain user info")
        if parts.fragment or parts.path not in ("", "/"):
            raise ValueError("Connection string must have no path or fragment")
        host = parts.hostname
        if not host:
            raise ValueError("Connection string has no host")
        _check_host(host)
        try:
            port = parts.port
        except ValueError:
            raise ValueError("Connection string has an invalid port") from None
        if port is None:
            raise ValueError("Connection string has no port")
        if port < 1:
            raise ValueError("Connection string port must be 1-65535")
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        names = [k for k, _ in pairs]
        if sorted(names) != ["fp", "token"]:
            raise ValueError("Connection string needs exactly one token= and one fp=")
        query = dict(pairs)
        if not _TOKEN_PATTERN.fullmatch(query["token"]):
            raise ValueError("Connection string token must be non-empty [A-Za-z0-9_-]")
        if not (match := _FP_PATTERN.fullmatch(query["fp"])):
            raise ValueError("Connection string needs fp=sha256:<64 lowercase hex chars>")
        return cls(host=host, port=port, token=query["token"], fingerprint=match.group(1))
