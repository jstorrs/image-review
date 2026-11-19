import functools
import importlib.metadata
import re
from typing import NewType, TypeGuard

MAX_REVIEWER_LENGTH = 64
MAX_PASS = 10**6  # a pass number above this is refused everywhere, and current_pass() never answers above it
MIN_TOKEN_LENGTH = 22  # 128 bits as base64url; 32 hex characters also pass
MAX_TOKEN_LENGTH = 256

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


@functools.cache  # resolved once: the running code's version, without a metadata scan per mark
def package_version() -> str:
    """This process's image-review version, or "unknown" when it is not installed as a package."""
    try:
        return importlib.metadata.version("image-review")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def is_int_at_least(value: object, minimum: int) -> TypeGuard[int]:
    """True for a JSON integer (never a bool) that is at least `minimum`."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def parse_reviewer(value: object) -> str:
    """A reviewer name: 1-64 printable characters (no tab, newline or other control character), not all spaces.

    It is the client's own, unauthenticated claim, recorded as given. ValueError otherwise.
    """
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_REVIEWER_LENGTH
        or not value.isprintable()
        or not value.strip()
    ):
        raise ValueError(
            f"reviewer must be 1-{MAX_REVIEWER_LENGTH} printable characters, not all spaces (no tabs, newlines or control characters)"
        )
    return value


Token = NewType("Token", str)  # a token that passed parse_token (or was generated)


def parse_token(raw: str) -> Token:
    """An access token supplied by the operator: letters, digits, `_` and `-`, 22-256 characters.

    Surrounding whitespace is refused, not stripped. The message never contains the value. ValueError otherwise.
    """
    if not MIN_TOKEN_LENGTH <= len(raw) <= MAX_TOKEN_LENGTH or not _TOKEN_PATTERN.fullmatch(raw):
        raise ValueError(
            f"must be {MIN_TOKEN_LENGTH}-{MAX_TOKEN_LENGTH} characters from A-Z a-z 0-9 _ - (no spaces or newlines)"
        )
    return Token(raw)
