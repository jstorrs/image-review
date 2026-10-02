"""The work directory's lock file, review.lock (stdlib only; importable on the server side)."""

import contextlib
import getpass
import json
import os
import secrets
import socket
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .access import policy_of_dir
from .atomic import publish

LOCK_NAME = "review.lock"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


@dataclass(frozen=True)
class LockHolder:
    """Who holds a work directory's lock, as recorded in review.lock."""

    host: str
    boot_id: str  # "" where unavailable (non-Linux) or in locks from older versions
    user: str
    pid: int
    started: str  # UTC, ISO 8601


class WorkDirLocked(Exception):
    """Another writer holds the work directory (or its lock file cannot be read)."""

    def __init__(self, path: Path, holder: LockHolder | None, detail: str = ""):
        self.path = path
        self.holder = holder
        if holder is None:
            message = (
                f"work directory lock file {path} is unreadable or corrupt ({detail}); "
                f"if no image-review process is using this work directory, delete {path} by hand and retry"
            )
        else:
            message = (
                f"work directory is in use by {holder.user} on {holder.host} (pid {holder.pid}) since {holder.started}; "
                f"lock file {path}. If that process is gone, delete {path} by hand"
            )
        super().__init__(message)


def boot_id() -> str:
    """This boot's id (tells apart machines sharing a hostname, and reboots); "" where unavailable."""
    try:
        return BOOT_ID_PATH.read_text().strip()
    except OSError:
        return ""


def this_process() -> LockHolder:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):  # no USER/LOGNAME and no passwd entry (some containers): KeyError <3.13, OSError >=3.13
        user = str(os.getuid())
    return LockHolder(
        socket.gethostname(), boot_id(), user, os.getpid(), datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def parse_lock(text: str) -> LockHolder:
    """Parse review.lock's JSON; ValueError if it is not a complete holder record."""
    data = json.loads(text)
    fields = data if isinstance(data, dict) else {}
    host, user, pid, started = (fields.get(k) for k in ("host", "user", "pid", "started"))
    boot = fields.get("boot_id", "")
    if not (
        isinstance(host, str)
        and isinstance(user, str)
        and isinstance(started, str)
        and isinstance(boot, str)
        and type(pid) is int
        and pid > 0
    ):
        raise ValueError("expected a JSON object with host, user, a positive pid and started")
    return LockHolder(host, boot, user, pid, started)


def same_machine(holder: LockHolder, me: LockHolder) -> bool:
    """Whether holder's pid can be checked here: same hostname and same (known) boot."""
    return holder.host == me.host and holder.boot_id != "" and holder.boot_id == me.boot_id


def pid_gone(pid: int) -> bool:
    """True if no process with this pid exists here. False if it exists (even another user's) or cannot be checked."""
    # Never call os.kill(pid, 0) on Windows: signal 0 is CTRL_C_EVENT there. Callers only ask about pids
    # recorded with this machine's boot_id (Linux /proc), which already rules Windows out; this guard makes sure.
    if os.name != "posix":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # exists, owned by another user
    return False


def is_stale(holder: LockHolder, me: LockHolder) -> bool:
    """True only if the holder ran on this machine, this boot, and its process no longer exists."""
    if not same_machine(holder, me):
        return False  # cannot check another machine's processes
    return pid_gone(holder.pid)


FileId = tuple[int, int]  # (st_dev, st_ino)
EMPTY_LOCK_WAIT = 1.0  # seconds a reader waits for a directly created lock to be filled in


@contextlib.contextmanager
def _open_lock(path: Path) -> Iterator[tuple[FileId, str]]:
    """Read the lock and keep it open for the block: an open inode cannot be freed, so its number
    cannot be reused by a re-created lock while we compare against it (NFS silly-renames instead)."""
    with open(path) as f:
        st = os.fstat(f.fileno())
        text = f.read()
        deadline = time.monotonic() + EMPTY_LOCK_WAIT
        while not text and time.monotonic() < deadline:  # a direct (no-link) create not yet written
            time.sleep(0.05)
            f.seek(0)
            text = f.read()
        yield (st.st_dev, st.st_ino), text


def _sibling_name(me: LockHolder) -> str:
    return f"{LOCK_NAME}.{me.host}.{me.boot_id or '-'}.{me.pid}.{secrets.token_hex(8)}"


def _record(me: LockHolder) -> str:
    return json.dumps({"host": me.host, "boot_id": me.boot_id, "user": me.user, "pid": me.pid, "started": me.started})


def _create_lock(path: Path, file_mode: int, me: LockHolder) -> bool:
    """Atomically create path holding me's record; False if it already exists.

    The record is written in full to a unique sibling, then hard-linked into place (see publish), so a lock is never
    observed empty or half-written. Where hard links are not supported, path is created directly and readers wait
    briefly for its contents. No group: the work directory is setgid where it has one, and with no group pending the
    lock is opened with file_mode, so it is readable by teammates once fchmod'ed, before anything is written (an empty
    direct-created lock included).
    """
    try:
        publish(path, path.with_name(_sibling_name(me)), file_mode, None, _record(me))
    except FileExistsError:
        return False
    return True


def _sweep_siblings(work_dir: Path, me: LockHolder) -> None:
    """Best effort: remove review.lock.* temp files left by dead processes of this machine and boot."""
    if not me.boot_id:
        return
    prefix = f"{LOCK_NAME}.{me.host}.{me.boot_id}."
    for sibling in work_dir.glob(f"{LOCK_NAME}.*"):
        rest = sibling.name.removeprefix(prefix)
        pid_text, _, _ = rest.partition(".")
        if rest == sibling.name or not pid_text.isdigit() or int(pid_text) == me.pid:
            continue
        if pid_gone(int(pid_text)):
            with contextlib.suppress(OSError):
                sibling.unlink()


def _unlink_if_same(path: Path, file_id: FileId) -> None:
    """Remove path only if it is still the file we inspected; the caller keeps that file open,
    so a re-created lock is a different inode."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    if (st.st_dev, st.st_ino) == file_id:
        path.unlink(missing_ok=True)


def acquire_lock(work_dir: Path) -> LockHolder:
    """Create work_dir/review.lock for this process, reclaiming a stale one from this machine once.

    WorkDirLocked if another process holds it. Not flock: unreliable on Lustre/NFS.
    """
    path = work_dir / LOCK_NAME
    file_mode = policy_of_dir(work_dir).file_mode
    me = this_process()
    _sweep_siblings(work_dir, me)
    for attempt in range(2):
        if _create_lock(path, file_mode, me):
            return me
        try:
            with _open_lock(path) as (file_id, text):
                holder = parse_lock(text)
                if not (attempt == 0 and is_stale(holder, me)):
                    raise WorkDirLocked(path, holder)
                _unlink_if_same(path, file_id)
        except FileNotFoundError:
            pass  # released between our create and read; retry
        except (OSError, ValueError) as e:
            raise WorkDirLocked(path, None, str(e)) from e
    raise WorkDirLocked(path, None, "it kept changing while being checked")


def release_lock(work_dir: Path, me: LockHolder) -> None:
    """Remove work_dir/review.lock if it still names this process; otherwise leave it."""
    path = work_dir / LOCK_NAME
    try:
        with _open_lock(path) as (file_id, text):
            holder = parse_lock(text)
            if (holder.host, holder.boot_id, holder.pid) == (me.host, me.boot_id, me.pid):
                _unlink_if_same(path, file_id)
    except (OSError, ValueError):
        return


def live_writer(work_dir: Path) -> WorkDirLocked | None:
    """Why the work directory looks open in a writer (review or serve): its review.lock, unless there is none or it
    names a process of this machine and boot that no longer exists. An unreadable lock counts as held."""
    path = work_dir / LOCK_NAME
    try:
        with _open_lock(path) as (_, text):
            holder = parse_lock(text)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        return WorkDirLocked(path, None, str(e))
    return None if is_stale(holder, this_process()) else WorkDirLocked(path, holder)
