"""Create files atomically, never overwriting (stdlib only; importable on the server side)."""

import contextlib
import errno
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger(__name__)

LINK_UNSUPPORTED = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}  # vfat/exFAT, SMB, many FUSE mounts


def _write_file(path: Path, file_mode: int, group: int | None, text: str) -> tuple[int, int | None]:
    """Create path (O_EXCL) with file_mode, owned by `group` if given, holding text (UTF-8), synced; removed again on
    failure. If the group cannot be set, the file is made 0600 instead and a warning logged. Returns the (file_mode,
    group) actually applied: the arguments, or (0o600, None) after that fallback."""
    # private until a pending group and the mode are set; with no group, file_mode at once (the umask may strip bits,
    # restored by fchmod below), so a teammate who opens a directly created lock before it is filled can read it
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600 if group is not None else file_mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            if group is not None:
                try:
                    os.fchown(f.fileno(), -1, group)
                except OSError as e:
                    log.warning("cannot give %s to group %d (%s); making it private (0600) instead", path, group, e)
                    file_mode, group = 0o600, None
            os.fchmod(f.fileno(), file_mode)  # exact (no umask), and only once the group is right
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return file_mode, group


def publish(path: Path, tmp: Path, file_mode: int, group: int | None, text: str) -> None:
    """Create path holding text, never overwriting: FileExistsError if path exists (or, by a random-name collision,
    tmp, which is then removed).

    text is written and synced in the unique sibling tmp (see _write_file for mode and group), then hard-linked into
    place, so path appears whole or not at all. Where hard links are not supported (LINK_UNSUPPORTED), path is created
    directly with O_EXCL, so a concurrent creator still loses with FileExistsError (but a crash can leave path partial).
    tmp is removed on every exit but a hard kill.
    """
    try:
        file_mode, group = _write_file(tmp, file_mode, group, text)  # as the sibling ended up; warned about once
        try:
            os.link(tmp, path)
        except OSError as e:
            # NFS: the link was made but the reply was lost (a retransmitted link then fails, often with EEXIST).
            # Comparing identities, not tmp's link count, needs no assumption about what else links to tmp.
            with contextlib.suppress(OSError):
                if os.path.samefile(tmp, path):
                    return
            if isinstance(e, FileExistsError) or e.errno not in LINK_UNSUPPORTED:
                raise
            _write_file(path, file_mode, group, text)
    finally:
        tmp.unlink(missing_ok=True)


def write_new_file(path: Path, file_mode: int, group: int | None, text: str) -> None:
    """Create path holding text (UTF-8), never overwriting: FileExistsError if it exists.

    Written through a unique hidden sibling and hard-linked into place; see publish.
    """
    publish(path, path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp"), file_mode, group, text)
