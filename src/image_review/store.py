import contextlib
import csv
import errno
import getpass
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import time
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, Self, get_args

from .access import MANIFEST_NAME, policy_of_dir
from .review_db import Change, Decision, ReviewDB
from .status import TODO_STATUSES, MarkMode, Status, Verdict

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManifestRow:
    key: str  # preprocessed_path; the only identifier the client ever sees
    batch: str


@dataclass(frozen=True)
class ManifestEntry:
    batch: str
    key: str  # preprocessed_path
    image_id: str  # original source path; stays server-side
    # SHA-256 (64 lowercase hex) of the source file or ZIP entry, shared by X and X#icon; None in a 3-column manifest.
    # Derived from PHI content: stays server-side like image_id.
    source_sha256: str | None = None
    jpeg_sha256: str | None = None  # SHA-256 of the JPG as written; image_bytes checks it. None in a 3-column manifest


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
        """Bytes for the keys that loaded; missing or unloadable keys are omitted with a logged warning."""
        ...

    def statuses(self, pass_number: int) -> dict[str, Status]:
        """Key -> status for every manifest row."""
        ...

    def mark(
        self, keys: list[str], status: Verdict, pass_number: int, *, reviewer: str, mode: MarkMode
    ) -> dict[str, Status]:
        """Record a verdict on keys, given by `reviewer` (an unauthenticated claim) in `mode`.

        Returns the new status of every key affected (incl. keys sharing an image_id).
        """
        ...

    def undo(self, pass_number: int, *, reviewer: str) -> dict[str, Status]:
        """Undo the latest mark not yet undone, restoring each image's decision from before it; `reviewer` is recorded.

        Returns the new status of every key affected, as mark does; {} when there is nothing to undo. The marks
        that can be undone are the store's (the server's, for a remote store), held in memory since it was opened.
        """
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


MANIFEST_HEADER = ["batch", "preprocessed_path", "image_id", "source_sha256", "jpeg_sha256"]
LEGACY_MANIFEST_HEADER = MANIFEST_HEADER[:3]  # before the hash columns; still loads, with both hashes None
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _parse_sha256(text: str, name: str) -> str:
    """A SHA-256 hex digest: exactly 64 lowercase hex characters. ValueError otherwise."""
    if not _SHA256.fullmatch(text):
        raise ValueError(f"{name} must be 64 lowercase hex characters")
    return text


def load_manifest(work_dir: Path) -> list[ManifestEntry]:
    """Parse manifest.tsv strictly. ValueError (naming file:line) if malformed; FileNotFoundError if absent.

    Both the current header and the legacy 3-column one (no hashes) are accepted; every row has the header's width.
    """
    path = work_dir / MANIFEST_NAME
    entries: list[ManifestEntry] = []
    key_lines: dict[str, int] = {}
    with open(path, newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        if header not in (MANIFEST_HEADER, LEGACY_MANIFEST_HEADER):
            raise ValueError(
                f"{path}:1: header must be {', '.join(MANIFEST_HEADER)} (or {', '.join(LEGACY_MANIFEST_HEADER)})"
            )
        for fields in reader:
            line = reader.line_num
            if len(fields) != len(header) or not all(fields):
                raise ValueError(
                    f"{path}:{line}: expected {len(header)} non-empty tab-separated fields ({', '.join(header)})"
                )
            batch, key, image_id, *hashes = fields
            try:
                source_sha256, jpeg_sha256 = (
                    (_parse_sha256(hashes[0], "source_sha256"), _parse_sha256(hashes[1], "jpeg_sha256"))
                    if hashes
                    else (None, None)
                )
            except ValueError as e:
                raise ValueError(f"{path}:{line}: {e}") from None
            if key in key_lines:
                raise ValueError(
                    f"{path}:{line}: duplicate preprocessed_path {key!r} (first seen on line {key_lines[key]})"
                )
            key_lines[key] = line
            entries.append(ManifestEntry(batch, key, image_id, source_sha256, jpeg_sha256))
    return entries


def check_jpeg_hash(key: str, data: bytes, expected: str | None) -> bytes:
    """data, if it matches the recorded SHA-256 (or none is recorded). ValueError otherwise: the image is unloadable.

    Pillow decodes a JPG cut short but still ending in EOI without complaint (grey rows); the hash catches it.
    """
    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"{key} does not match its recorded hash")
    return data


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


def _create_lock(path: Path, file_mode: int, me: LockHolder) -> bool:
    """Atomically create path holding me's record; False if it already exists.

    The record is written in full to a unique sibling, then hard-linked into place (see _publish), so a lock is never
    observed empty or half-written. Where hard links are not supported, path is created directly and readers wait
    briefly for its contents. No group: the work directory is setgid where it has one, and with no group pending the
    lock is opened with file_mode, so it is readable by teammates once fchmod'ed, before anything is written (an empty
    direct-created lock included).
    """
    try:
        _publish(path, path.with_name(_sibling_name(me)), file_mode, None, _record(me))
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


SKIPPED_NAME = "skipped.tsv"
SKIPPED_HEADER = ["image_id", "kind", "reason"]
SkipKind = Literal["failed", "ignored"]


@dataclass(frozen=True)
class SkippedRow:
    """One row of skipped.tsv: an input preprocess left out of the manifest. image_id is a source path, as in the manifest."""

    image_id: str
    kind: SkipKind
    reason: str


def load_skipped(work_dir: Path) -> list[SkippedRow] | None:
    """Parse skipped.tsv strictly, in file order; None if the work dir has none. ValueError (naming file:line) if malformed."""
    path = work_dir / SKIPPED_NAME
    if not path.exists():
        return None
    rows: list[SkippedRow] = []
    with open(path, newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        if next(reader, None) != SKIPPED_HEADER:
            raise ValueError(f"{path}:1: header must be {', '.join(SKIPPED_HEADER)}")
        for fields in reader:
            if len(fields) != len(SKIPPED_HEADER) or fields[1] not in get_args(SkipKind):
                raise ValueError(f"{path}:{reader.line_num}: expected image_id, kind (failed or ignored), reason")
            image_id, kind, reason = fields
            rows.append(SkippedRow(image_id, kind, reason))  # type: ignore[arg-type]  # kind checked above
    return rows


def load_skipped_counts(work_dir: Path) -> SkippedCounts | None:
    rows = load_skipped(work_dir)
    if rows is None:
        return None
    failed = sum(r.kind == "failed" for r in rows)
    return SkippedCounts(failed=failed, ignored=len(rows) - failed)


class LocalStore:
    """The work directory itself. Writable (the default) holds work_dir/review.lock until close();
    read_only takes no lock and refuses mark and undo.

    The undo stack holds this process's marks in memory only: it is lost when the store is closed or the process
    ends, and a server's stack is shared by every client it serves."""

    def __init__(self, work_dir: Path, read_only: bool = False):
        self.work_dir = work_dir
        self.read_only = read_only
        entries = load_manifest(work_dir)  # written once by preprocess; safe to read before locking
        self._entries = entries
        self._rows = [ManifestRow(key=e.key, batch=e.batch) for e in entries]
        self._by_key: dict[str, ManifestEntry] = {e.key: e for e in entries}
        self._keys_by_image_id: dict[str, list[str]] = {}
        for key, e in self._by_key.items():
            self._keys_by_image_id.setdefault(e.image_id, []).append(key)
        # Lock before loading review.tsv, so the state loaded is not one another writer is about to overwrite.
        self._lock: LockHolder | None = None if read_only else acquire_lock(work_dir)
        self._undo: list[list[Change]] = []  # one entry per mark, latest last
        try:
            self._db = ReviewDB(work_dir)
            if not read_only:
                self._db.migrate()  # under the lock: an old-header review.tsv gains the audit columns, once
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
        entry = self._by_key[key]
        return check_jpeg_hash(key, safe_path(self.work_dir, key).read_bytes(), entry.jpeg_sha256)

    def image_bytes_many(self, keys: list[str]) -> dict[str, bytes]:
        found: dict[str, bytes] = {}
        for key in keys:
            try:
                found[key] = self.image_bytes(key)
            except (KeyError, ValueError, OSError) as exc:
                log.warning("cannot load %s: %s", key, exc)
        return found

    def statuses(self, pass_number: int) -> dict[str, Status]:
        return {key: self._db.get_status(e.image_id, pass_number) for key, e in self._by_key.items()}

    def _require_writable(self) -> None:
        if self._lock is None:
            state = "opened read-only" if self.read_only else "closed"
            raise PermissionError(f"store for {self.work_dir} was {state}; cannot record verdicts")

    def _affected(self, image_ids: list[str], pass_number: int) -> dict[str, Status]:
        """The status of every key of these image_ids, in their order."""
        return {
            k: self._db.get_status(iid, pass_number)
            for iid in dict.fromkeys(image_ids)
            for k in self._keys_by_image_id[iid]
        }

    def mark(
        self, keys: list[str], status: Verdict, pass_number: int, *, reviewer: str, mode: MarkMode
    ) -> dict[str, Status]:
        self._require_writable()
        targets = [self._by_key[k] for k in keys]  # an unknown key raises before anything is written
        changes = self._db.mark_many(
            [(e.image_id, e.batch) for e in targets], status, pass_number, reviewer=reviewer, mode=mode
        )
        self._undo.append(changes)  # only once written
        return self._affected([e.image_id for e in targets], pass_number)

    def undo(self, pass_number: int, *, reviewer: str) -> dict[str, Status]:
        self._require_writable()
        if not self._undo:
            return {}
        changes = self._undo[-1]
        self._db.undo_many(changes, reviewer=reviewer)
        self._undo.pop()  # only once written: a failed undo can be retried
        return self._affected([c.written.image_id for c in changes], pass_number)

    def current_pass(self) -> int:
        return self._db.current_pass(e.image_id for e in self._by_key.values())

    def skipped(self) -> SkippedCounts | None:
        return load_skipped_counts(self.work_dir)

    def export_rows(self) -> list["ExportRow"]:
        """The study's result, one row per source file (see export_rows). Local only: image_ids never leave the work
        dir's machine. ValueError if skipped.tsv is malformed or review.tsv ends in a torn line."""
        return export_rows(self._entries, self._db.decisions(), load_skipped(self.work_dir) or [])


# The review --filter vocabulary, parsed by the CLI's click.Choice.
StatusFilter = Literal["unreviewed", "clean", "all"]


def filter_rows(
    rows: list[ManifestRow],
    statuses: dict[str, Status],
    status_filter: StatusFilter = "unreviewed",
    batch: str | None = None,
) -> list[ManifestRow]:
    """Filter rows by status and optional batch.

    "unreviewed" selects the todo statuses (UNREVIEWED and FLAGGED), "clean" selects CLEAN, "all" everything.
    """
    selected = [r for r in rows if not batch or r.batch == batch]
    if status_filter == "all":
        return selected
    targets: frozenset[Status] = frozenset({"CLEAN"}) if status_filter == "clean" else TODO_STATUSES
    return [r for r in selected if statuses[r.key] in targets]


def _tally(statuses: Iterable[Status]) -> dict[str, int]:
    """A count for each Status, plus "total"."""
    counts = {**dict.fromkeys(get_args(Status), 0), "total": 0}
    for status in statuses:
        counts[status] += 1
        counts["total"] += 1
    return counts


def batch_summary(rows: list[ManifestRow], statuses: dict[str, Status]) -> dict[str, dict[str, int]]:
    """Per batch: a count for each Status, plus "total"."""
    by_batch: dict[str, list[Status]] = {}
    for row in rows:
        by_batch.setdefault(row.batch, []).append(statuses[row.key])
    return {batch: _tally(group) for batch, group in by_batch.items()}


def summary(rows: list[ManifestRow], statuses: dict[str, Status]) -> dict[str, int]:
    """A count for each Status, plus "total"."""
    return _tally(statuses[row.key] for row in rows)


# What an export says about one source file. NOT_REVIEWED: preprocess failed to render it (or part of it), so nobody
# saw that part.
ExportStatus = Literal["CLEAN", "DIRTY", "UNREVIEWED", "NOT_REVIEWED"]
EXPORT_HEADER = ["image_id", "status", "pass_number", "timestamp", "reviewer", "reason", "source_sha256"]
ICON_SUFFIX = "#icon"  # a DICOM's embedded icon image
# The worst part decides a file's status: CLEAN only if every part is CLEAN.
_SEVERITY: dict[ExportStatus, int] = {"CLEAN": 0, "UNREVIEWED": 1, "NOT_REVIEWED": 2, "DIRTY": 3}
# U+2028/U+2029 and control characters: str.splitlines() breaks lines at some, and a TSV cell cannot hold a tab.
_LINE_SEPARATORS = frozenset("\u2028\u2029")


@dataclass(frozen=True)
class ExportRow:
    image_id: str  # a source file, or a ZIP entry (`<zip>::<name>`)
    status: ExportStatus
    pass_number: int | None  # from the main image's latest decision; None, and timestamp and reviewer "", without one
    timestamp: str
    reviewer: str
    reason: str  # why the row is not simply its main image's verdict (skip reasons, the icon's state); often ""
    source_sha256: str  # the source file's SHA-256 from the manifest; "" when it has none (legacy, or never rendered)


@dataclass(frozen=True)
class _Part:
    """One manifest or skipped image_id, before an icon is folded into its file."""

    status: ExportStatus
    decision: Decision | None  # None for UNREVIEWED and NOT_REVIEWED
    reason: str  # the skip reason, for NOT_REVIEWED


def _parts(entries: list[ManifestEntry], decisions: dict[str, Decision], skipped: list[SkippedRow]) -> dict[str, _Part]:
    """Each distinct image_id once, manifest order then skipped order. Any `failed` row makes it NOT_REVIEWED (with
    the first such row's reason), even if the manifest has it too."""
    failed: dict[str, str] = {}
    for row in skipped:
        if row.kind == "failed":
            failed.setdefault(row.image_id, row.reason)
    parts: dict[str, _Part] = {}
    for image_id in dict.fromkeys([*(e.image_id for e in entries), *failed]):
        decision = decisions.get(image_id)
        if image_id in failed:
            parts[image_id] = _Part("NOT_REVIEWED", None, failed[image_id])
        elif decision is None:
            parts[image_id] = _Part("UNREVIEWED", None, "")
        else:
            parts[image_id] = _Part(decision.status, decision, "")  # CLEAN or DIRTY: latest() drops tombstones
    return parts


_MAIN_MISSING = _Part("NOT_REVIEWED", None, "main image missing")  # an icon without its file: never CLEAN


def _fold(image_id: str, main: _Part | None, icon: _Part | None, source_sha256: str) -> ExportRow:
    """One file's row: the worst status of its main image and its icon; pass, timestamp and reviewer from the main
    image's decision. A missing main image counts as NOT_REVIEWED."""
    main = _MAIN_MISSING if main is None else main
    status = max((p.status for p in (main, icon) if p is not None), key=_SEVERITY.__getitem__)
    notes = []
    if main.status == "NOT_REVIEWED":
        notes.append(main.reason)
    if icon is not None and icon.status == "NOT_REVIEWED":
        notes.append(f"icon: {icon.reason}")
    elif icon is not None and icon.status != "CLEAN":
        notes.append(f"icon {icon.status}")
    d = main.decision
    if d is None:
        return ExportRow(image_id, status, None, "", "", "; ".join(notes), source_sha256)
    return ExportRow(image_id, status, d.pass_number, d.timestamp, d.reviewer, "; ".join(notes), source_sha256)


def export_rows(
    entries: list[ManifestEntry], decisions: dict[str, Decision], skipped: list[SkippedRow]
) -> list[ExportRow]:
    """One row per source file (a ZIP entry counts as one), in order of first appearance: manifest, then the
    `failed` rows of skipped.tsv. `ignored` rows (inputs that are not images) are left out.

    A part (manifest or failed image_id) is NOT_REVIEWED if skipped.tsv has a `failed` row for it, else its latest
    decision's status (CLEAN or DIRTY), else UNREVIEWED. A DIRTY from an earlier pass not yet re-reviewed (FLAGGED in
    `status`) is DIRTY: it still contains PHI. A DICOM's icon (`X#icon`) is folded into `X`'s row, whose status is
    the worse of the two (DIRTY, then NOT_REVIEWED, then UNREVIEWED, then CLEAN); an icon without its `X` is
    reported under `X`, as if `X` were NOT_REVIEWED. `decisions` is the latest decision per image_id (review_db.latest); ones for image_ids in
    neither file are ignored. A row's source_sha256 is the first one the manifest records for `X` or `X#icon` (they
    share it), else "".
    """
    parts = _parts(entries, decisions, skipped)
    hashes: dict[str, str] = {}
    for e in entries:
        if e.source_sha256 is not None:
            hashes.setdefault(e.image_id.removesuffix(ICON_SUFFIX), e.source_sha256)
    files = dict.fromkeys(image_id.removesuffix(ICON_SUFFIX) for image_id in parts)
    return [_fold(f, parts.get(f), parts.get(f + ICON_SUFFIX), hashes.get(f, "")) for f in files]


def format_export(rows: list[ExportRow]) -> str:
    """The export as TSV text: EXPORT_HEADER, then one line per row; tab-separated, LF line endings, no quoting.

    ValueError, naming the image_id, if a field could not be read back unambiguously: it holds a control character
    (C0 incl. tab, CR and LF, DEL, or C1 incl. U+0085) or U+2028/U+2029, or it starts with `"` (which CSV-aware
    readers take as the start of a quoted field).
    """
    lines = ["\t".join(EXPORT_HEADER)]
    for r in rows:
        fields = [r.image_id, r.status, "" if r.pass_number is None else str(r.pass_number), r.timestamp]
        fields += [r.reviewer, r.reason, r.source_sha256]
        for name, value in zip(EXPORT_HEADER, fields, strict=True):
            if value.startswith('"') or any(c in _LINE_SEPARATORS or unicodedata.category(c) == "Cc" for c in value):
                raise ValueError(
                    f"cannot export {r.image_id!r}: its {name} contains a control character or line separator, or "
                    'starts with ", which this unquoted TSV cannot hold'
                )
        lines.append("\t".join(fields))
    return "".join(f"{line}\n" for line in lines)


def _write_file(path: Path, file_mode: int, group: int | None, text: str) -> bool:
    """Create path (O_EXCL) with file_mode, owned by `group` if given, holding text (UTF-8), synced; removed again on
    failure. If the group cannot be set, the file is made 0600 instead and a warning logged. Returns whether `group`
    was applied (always True without one)."""
    # private until a pending group and the mode are set; with no group, file_mode at once (the umask may strip bits,
    # restored by fchmod below), so a teammate who opens a directly created lock before it is filled can read it
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600 if group is not None else file_mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            grouped = True
            if group is not None:
                try:
                    os.fchown(f.fileno(), -1, group)
                except OSError as e:
                    log.warning("cannot give %s to group %d (%s); making it private (0600) instead", path, group, e)
                    file_mode, grouped = 0o600, False
            os.fchmod(f.fileno(), file_mode)  # exact (no umask), and only once the group is right
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return grouped


def _publish(path: Path, tmp: Path, file_mode: int, group: int | None, text: str) -> None:
    """Create path holding text, never overwriting: FileExistsError if path exists (or, by a random-name collision,
    tmp, which is then removed).

    text is written and synced in the unique sibling tmp (see _write_file for mode and group), then hard-linked into
    place, so path appears whole or not at all. Where hard links are not supported (LINK_UNSUPPORTED), path is created
    directly with O_EXCL, so a concurrent creator still loses with FileExistsError (but a crash can leave path partial).
    tmp is removed on every exit but a hard kill.
    """
    try:
        if not _write_file(tmp, file_mode, group, text):
            file_mode, group = 0o600, None  # as the sibling ended up; warned about once
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

    Written through a unique hidden sibling and hard-linked into place; see _publish.
    """
    _publish(path, path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp"), file_mode, group, text)


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
