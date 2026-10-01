import contextlib
import csv
import errno
import getpass
import json
import os
import secrets
import socket
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, Self

from .access import policy_of_dir
from .review_db import ReviewDB
from .status import Status, Verdict

__all__ = ["Status", "Verdict"]  # re-exported for callers that import them from here


@dataclass(frozen=True)
class ManifestRow:
    key: str  # preprocessed_path; the only identifier the client ever sees
    batch: str


@dataclass(frozen=True)
class ManifestEntry:
    batch: str
    key: str  # preprocessed_path
    image_id: str  # original source path; stays server-side


@dataclass(frozen=True)
class SkippedCounts:
    """Inputs preprocess could not put in the manifest: unrenderable (failed) or not images (ignored)."""

    failed: int
    ignored: int

    @property
    def any(self) -> bool:
        return self.failed > 0 or self.ignored > 0


class StoreUnavailable(Exception):
    """The store cannot be reached (as opposed to a key or image being bad)."""


class ReviewStore(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(self, *exc_info: object) -> None: ...

    def close(self) -> None:
        """Release what the store holds (connections, the work dir lock). Idempotent."""
        ...

    def manifest(self) -> list[ManifestRow]: ...

    def image_bytes(self, key: str) -> bytes: ...

    def image_bytes_many(self, keys: list[str]) -> dict[str, bytes]:
        """Bytes for the keys that loaded; missing or unloadable keys are omitted with a stderr warning."""
        ...

    def statuses(self, pass_number: int) -> dict[str, Status]:
        """Key -> status for every manifest row."""
        ...

    def mark(self, keys: list[str], batch: str, status: Verdict, pass_number: int) -> dict[str, Status]:
        """Record a verdict; returns the new status of every key affected (incl. keys sharing an image_id)."""
        ...

    def current_pass(self) -> int: ...

    def skipped(self) -> SkippedCounts | None:
        """Counts from preprocess's skipped.tsv; None if the work dir has none. ValueError if it is malformed."""
        ...


def safe_path(work_dir: Path, relative: str) -> Path:
    """Resolve a relative path within work_dir, rejecting traversal attempts."""
    resolved = (work_dir / relative).resolve()
    if not resolved.is_relative_to(work_dir.resolve()):
        raise ValueError(f"Path escapes work directory: {relative}")
    return resolved


MANIFEST_HEADER = ["batch", "preprocessed_path", "image_id"]


def load_manifest(work_dir: Path) -> list[ManifestEntry]:
    """Parse manifest.tsv strictly. ValueError (naming file:line) if malformed; FileNotFoundError if absent."""
    path = work_dir / "manifest.tsv"
    entries: list[ManifestEntry] = []
    key_lines: dict[str, int] = {}
    with open(path, newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        if next(reader, None) != MANIFEST_HEADER:
            raise ValueError(f"{path}:1: header must be {', '.join(MANIFEST_HEADER)}")
        for fields in reader:
            line = reader.line_num
            if len(fields) != len(MANIFEST_HEADER) or not all(fields):
                raise ValueError(f"{path}:{line}: expected {len(MANIFEST_HEADER)} non-empty tab-separated fields ({', '.join(MANIFEST_HEADER)})")
            batch, key, image_id = fields
            if key in key_lines:
                raise ValueError(f"{path}:{line}: duplicate preprocessed_path {key!r} (first seen on line {key_lines[key]})")
            key_lines[key] = line
            entries.append(ManifestEntry(batch, key, image_id))
    return entries


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
    return LockHolder(socket.gethostname(), boot_id(), user, os.getpid(), datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def parse_lock(text: str) -> LockHolder:
    """Parse review.lock's JSON; ValueError if it is not a complete holder record."""
    data = json.loads(text)
    fields = data if isinstance(data, dict) else {}
    host, user, pid, started = (fields.get(k) for k in ("host", "user", "pid", "started"))
    boot = fields.get("boot_id", "")
    if not (isinstance(host, str) and isinstance(user, str) and isinstance(started, str) and isinstance(boot, str) and type(pid) is int and pid > 0):
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
LINK_UNSUPPORTED = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}  # vfat/exFAT, SMB, many FUSE mounts


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


def _write_new(path: Path, file_mode: int, text: str) -> None:
    """Create path (O_EXCL; FileExistsError if present) holding text, synced; removed again on failure."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, file_mode)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), file_mode)  # the umask may have stripped group bits teammates need to read it
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _create_lock(path: Path, file_mode: int, me: LockHolder) -> bool:
    """Atomically create path holding me's record; False if it already exists.

    The record is written in full to a unique sibling, then hard-linked into place,
    so a lock is never observed empty or half-written. Where hard links are not
    supported, path is created directly and readers wait briefly for its contents.
    """
    tmp = path.with_name(_sibling_name(me))
    _write_new(tmp, file_mode, _record(me))
    try:
        try:
            os.link(tmp, path)
        except OSError as e:
            if os.stat(tmp).st_nlink == 2:
                return True  # NFS: the link was made but the reply was lost
            if isinstance(e, FileExistsError):
                return False
            if e.errno not in LINK_UNSUPPORTED:
                raise
        else:
            return True
    finally:
        tmp.unlink(missing_ok=True)
    try:
        _write_new(path, file_mode, _record(me))
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
                try:
                    holder = parse_lock(text)
                except ValueError as e:
                    raise WorkDirLocked(path, None, str(e) or "empty") from e
                if not (attempt == 0 and is_stale(holder, me)):
                    raise WorkDirLocked(path, holder)
                _unlink_if_same(path, file_id)
        except FileNotFoundError:
            pass  # released between our create and read; retry
        except OSError as e:
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


def load_skipped_counts(work_dir: Path) -> SkippedCounts | None:
    path = work_dir / "skipped.tsv"
    if not path.exists():
        return None
    failed = ignored = 0
    with open(path, newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        if next(reader, None) != ["image_id", "kind", "reason"]:
            raise ValueError(f"{path}:1: header must be image_id, kind, reason")
        for row in reader:
            if len(row) != 3 or row[1] not in ("failed", "ignored"):
                raise ValueError(f"{path}:{reader.line_num}: expected image_id, kind (failed or ignored), reason")
            failed += row[1] == "failed"
            ignored += row[1] == "ignored"
    return SkippedCounts(failed=failed, ignored=ignored)


class LocalStore:
    """The work directory itself. Writable (the default) holds work_dir/review.lock until close();
    read_only takes no lock and refuses mark."""

    def __init__(self, work_dir: Path, read_only: bool = False):
        self.work_dir = work_dir
        self.read_only = read_only
        entries = load_manifest(work_dir)  # written once by preprocess; safe to read before locking
        self._rows = [ManifestRow(key=e.key, batch=e.batch) for e in entries]
        self._image_ids = {e.key: e.image_id for e in entries}
        self._keys_by_image_id: dict[str, list[str]] = {}
        for key, iid in self._image_ids.items():
            self._keys_by_image_id.setdefault(iid, []).append(key)
        # Lock before loading review.tsv, so the state loaded is not one another writer is about to overwrite.
        self._lock: LockHolder | None = None if read_only else acquire_lock(work_dir)
        try:
            self._db = ReviewDB(work_dir)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            release_lock(self.work_dir, lock)

    def manifest(self) -> list[ManifestRow]:
        return list(self._rows)

    def image_bytes(self, key: str) -> bytes:
        if key not in self._image_ids:
            raise KeyError(key)
        return safe_path(self.work_dir, key).read_bytes()

    def image_bytes_many(self, keys: list[str]) -> dict[str, bytes]:
        found: dict[str, bytes] = {}
        for key in keys:
            try:
                found[key] = self.image_bytes(key)
            except (KeyError, ValueError, OSError) as exc:
                print(f"WARNING: cannot load {key}: {exc}", file=sys.stderr)
        return found

    def statuses(self, pass_number: int) -> dict[str, Status]:
        return {key: self._db.get_status(iid, pass_number) for key, iid in self._image_ids.items()}

    def mark(self, keys: list[str], batch: str, status: Verdict, pass_number: int) -> dict[str, Status]:
        if self._lock is None:
            state = "opened read-only" if self.read_only else "closed"
            raise PermissionError(f"store for {self.work_dir} was {state}; cannot record verdicts")
        image_ids = [self._image_ids[key] for key in keys]
        self._db.mark_many(image_ids, batch, status, pass_number)
        return {
            k: self._db.get_status(iid, pass_number)
            for iid in dict.fromkeys(image_ids)
            for k in self._keys_by_image_id[iid]
        }

    def current_pass(self) -> int:
        return self._db.current_pass(self._image_ids.values())

    def skipped(self) -> SkippedCounts | None:
        return load_skipped_counts(self.work_dir)


def filter_rows(
    rows: list[ManifestRow],
    statuses: dict[str, Status],
    status_filter: str = "unreviewed",
    batch: str | None = None,
) -> list[ManifestRow]:
    """Filter rows by status ("unreviewed", "clean", or "all") and optional batch."""
    if status_filter not in ("all", "clean", "unreviewed"):
        raise ValueError(f"Invalid status_filter {status_filter!r}, must be 'unreviewed', 'clean', or 'all'")
    selected = [r for r in rows if not batch or r.batch == batch]
    if status_filter == "all":
        return selected
    target = "CLEAN" if status_filter == "clean" else "UNREVIEWED"
    return [r for r in selected if statuses[r.key] == target]


def batch_summary(rows: list[ManifestRow], statuses: dict[str, Status]) -> dict[str, dict[str, int]]:
    batches: dict[str, dict[str, int]] = {}
    for row in rows:
        counts = batches.setdefault(row.batch, {"CLEAN": 0, "DIRTY": 0, "UNREVIEWED": 0, "total": 0})
        counts[statuses[row.key]] += 1
        counts["total"] += 1
    return batches


def summary(rows: list[ManifestRow], statuses: dict[str, Status]) -> dict[str, int]:
    totals = {"CLEAN": 0, "DIRTY": 0, "UNREVIEWED": 0, "total": 0}
    for bc in batch_summary(rows, statuses).values():
        for k in totals:
            totals[k] += bc[k]
    return totals
