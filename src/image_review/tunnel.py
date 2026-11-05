"""Validation for the parts of the ssh command `serve` prints: the login node (--via) and the node name (--ssh-host).

The operator pastes that command into a shell, so neither value may start with '-' (it would read as an ssh option)
and each is limited to a small character set.
Must stay importable without numpy/skimage.
"""

import re

_VIA_PATTERN = re.compile(r"[A-Za-z0-9._@%:\[\]/-]+")
_HOST_PATTERN = re.compile(r"[A-Za-z0-9._:\[\]-]+")


def parse_via(raw: str) -> str:
    """Validate an ssh destination (`user@host`, `host` or a config alias)."""
    if not raw:
        raise ValueError("destination is empty")
    if raw.startswith("-"):
        raise ValueError("destination must not start with '-'")
    if not _VIA_PATTERN.fullmatch(raw):
        raise ValueError("destination may only contain letters, digits and . _ @ % : [ ] / -")
    return raw


def parse_ssh_host(raw: str) -> str:
    """Validate a host name to print in an ssh command (hostname, IPv4 or bracketed IPv6); no user part."""
    if not raw:
        raise ValueError("host name is empty")
    if raw.startswith("-"):
        raise ValueError("host name must not start with '-'")
    if "@" in raw:
        raise ValueError("give the host name only; the user name is added for you")
    if not _HOST_PATTERN.fullmatch(raw):
        raise ValueError("host name may only contain letters, digits and . _ : [ ] -")
    return raw
