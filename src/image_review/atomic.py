"""Create files atomically, never overwriting (stdlib only; importable on the server side)."""

import contextlib
import errno
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger(__name__)

LINK_UNSUPPORTED = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}  # vfat/exFAT, SMB, many FUSE mounts

# fsync errors that only mean a directory cannot be synced on this filesystem
DIR_FSYNC_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF})


def fsync_dir(path: Path) -> None:
    """Sync directory path, so a file just created or renamed in it survives a power loss. Where the filesystem cannot
    sync directories (DIR_FSYNC_UNSUPPORTED) this does nothing; any other OSError is raised."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        if e.errno not in DIR_FSYNC_UNSUPPORTED:
            raise


def sync_parent(path: Path) -> None:
    """Sync path's parent directory so its new name (a file's or a directory's) is durable, best effort: path is
    already in place, so a failure (EIO, or EACCES opening a write-only drop directory) is only logged, never turned
    into a failed publish or run."""
    try:
        fsync_dir(path.parent)
    except OSError as e:
        log.warning(
            "created %s but could not sync its directory (%s %s); it may not survive a power loss",
            path,
            errno.errorcode.get(e.errno or 0, e.errno),
            e.strerror,
        )


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
    The directory is then synced, best effort (a failure is logged, not raised). tmp is removed on every exit but a
    hard kill.
    """
    try:
        file_mode, group = _write_file(tmp, file_mode, group, text)  # as the sibling ended up; warned about once
        try:
            os.link(tmp, path)
        except OSError as e:
            # NFS: the link was made but the reply was lost (a retransmitted link then fails, often with EEXIST).
            # Comparing identities, not tmp's link count, needs no assumption about what else links to tmp.
            linked = False
            with contextlib.suppress(OSError):
                linked = os.path.samefile(tmp, path)
            if not linked:
                if e.errno not in LINK_UNSUPPORTED:
                    raise
                _write_file(path, file_mode, group, text)
        sync_parent(path)  # the new name itself, not just the file's bytes
    finally:
        tmp.unlink(missing_ok=True)


def write_new_file(path: Path, file_mode: int, group: int | None, text: str) -> None:
    """Create path holding text (UTF-8), never overwriting: FileExistsError if it exists.

    Written through a unique hidden sibling and hard-linked into place; see publish.
    """
    publish(path, path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp"), file_mode, group, text)
