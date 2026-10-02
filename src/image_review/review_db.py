import contextlib
import csv
import errno
import io
import logging
import os
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

from .access import policy_of_dir
from .connection import package_version, parse_reviewer
from .status import TODO_STATUSES, MarkMode, Status, Verdict

log = logging.getLogger(__name__)

HEADER = ["image_id", "batch", "status", "pass_number", "timestamp", "reviewer", "mode", "grid_size", "tool_version"]
LEGACY_HEADER = HEADER[:5]  # before the audit columns; migrate() rewrites such a file with HEADER

# fsync errors that only mean a directory cannot be synced on this filesystem
DIR_FSYNC_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF})

# A row's mode in review.tsv: a MarkMode, or "undo" for a row written by undo_many (a restore or a tombstone).
RowMode = MarkMode | Literal["undo"]
# A row's status in review.tsv: a Verdict, or UNREVIEWED in a tombstone (mode "undo"), which erases the image's
# earlier rows from the fold. FLAGGED is derived and never stored.
RowStatus = Verdict | Literal["UNREVIEWED"]
TOMBSTONE: RowStatus = "UNREVIEWED"


@dataclass(frozen=True)
class Decision:
    image_id: str
    batch: str
    status: RowStatus  # TOMBSTONE only in an undo row; latest() drops such an image_id
    pass_number: int
    timestamp: str
    reviewer: str  # the client's unauthenticated claim; "" in rows from before it was recorded
    mode: RowMode | Literal[""]  # "" in rows from before it was recorded
    grid_size: int | None  # how many keys the verdict covered; None in rows from before it was recorded
    tool_version: str  # image-review version that wrote the row; "" in rows from before it was recorded


def parse_decision(path: Path, line: int, fields: list[str], header: list[str] = HEADER) -> Decision:
    """One row of a review.tsv whose header is `header` (HEADER or LEGACY_HEADER); legacy rows get empty audit columns."""
    where = f"{path}:{line}"
    if len(fields) != len(header):
        raise ValueError(
            f"{where}: expected {len(header)} tab-separated fields ({', '.join(header)}), got {len(fields)}"
        )
    image_id, batch, status, pass_text, timestamp, reviewer, mode, size_text, tool_version = fields + [""] * (
        len(HEADER) - len(header)
    )
    if not image_id:
        raise ValueError(f"{where}: image_id is empty")
    if status not in get_args(Verdict) and not (status == TOMBSTONE and mode == "undo"):
        raise ValueError(
            f"{where}: status must be one of {', '.join(get_args(Verdict))} ({TOMBSTONE} only in an undo row), got {status!r}"
        )
    try:
        pass_number = int(pass_text)
    except ValueError:
        raise ValueError(f"{where}: pass_number must be an integer, got {pass_text!r}") from None
    if pass_number < 1:
        raise ValueError(f"{where}: pass_number must be at least 1, got {pass_number}")
    if mode not in ("", *get_args(MarkMode), "undo"):
        raise ValueError(f"{where}: mode must be one of {', '.join(get_args(MarkMode))}, undo or empty, got {mode!r}")
    grid_size = None
    if size_text:
        try:
            grid_size = int(size_text)
        except ValueError:
            raise ValueError(f"{where}: grid_size must be an integer or empty, got {size_text!r}") from None
        if grid_size < 1:
            raise ValueError(f"{where}: grid_size must be at least 1, got {grid_size}")
    # status and mode checked above
    return Decision(image_id, batch, status, pass_number, timestamp, reviewer, mode, grid_size, tool_version)  # type: ignore[arg-type]


@dataclass(frozen=True)
class PendingTruncate:
    """A torn tail of review.tsv, from `offset` to the end, as seen in the file `ino` when it was `size` bytes."""

    ino: int
    size: int | None  # None: unknown after a failed append; only the inode is checked
    offset: int

    def matches(self, st: os.stat_result) -> bool:
        return st.st_ino == self.ino and self.size in (None, st.st_size)


@dataclass(frozen=True)
class LegacyLog:
    """A review.tsv with LEGACY_HEADER, as loaded: every row, and the file (inode, size) they came from."""

    ino: int
    size: int
    decisions: list[Decision]


def _format(header: bool, decisions: Iterable[Decision]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="\t")  # csv's default "\r\n" line ending, as review.tsv has always used
    if header:
        writer.writerow(HEADER)
    writer.writerows(
        [
            d.image_id,
            d.batch,
            d.status,
            d.pass_number,
            d.timestamp,
            d.reviewer,
            d.mode,
            "" if d.grid_size is None else d.grid_size,
            d.tool_version,
        ]
        for d in decisions
    )
    return buf.getvalue()


def parse_log(path: Path, data: bytes) -> tuple[list[Decision], bool]:
    """Every decision in review.tsv's bytes, in file order, and whether the file has LEGACY_HEADER.

    An empty file holds none and counts as current.
    """
    if not data:
        return [], False  # created but the first append never landed
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not valid UTF-8 at byte {exc.start}") from None
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t")
    header = next(reader, None)
    if header not in (HEADER, LEGACY_HEADER):
        raise ValueError(f"{path}:1: header must be {', '.join(HEADER)}")
    decisions = [parse_decision(path, reader.line_num, fields, header) for fields in reader]
    return decisions, header == LEGACY_HEADER


def fold(rows: dict[str, Decision], decisions: Iterable[Decision]) -> None:
    """Apply decisions, in order, to rows (the last decision per image_id): a tombstone removes its image_id."""
    for d in decisions:
        if d.status == TOMBSTONE:
            rows.pop(d.image_id, None)
        else:
            rows[d.image_id] = d


def latest(decisions: Iterable[Decision]) -> dict[str, Decision]:
    """The last decision per image_id; an image_id whose last row is a tombstone has none."""
    rows: dict[str, Decision] = {}
    fold(rows, decisions)
    return rows


@dataclass(frozen=True)
class Change:
    """One row mark_many wrote, and the image's decision just before it (None: it had none)."""

    written: Decision
    previous: Decision | None


class ReviewDB:
    HEADER = HEADER

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.review_path = work_dir / "review.tsv"
        self._rows: dict[str, Decision] = {}  # keyed by image_id; last row wins (see latest(): never a tombstone)
        self._truncate: PendingTruncate | None = None  # a torn tail to drop before the next append
        self._legacy: LegacyLog | None = None  # an old-header file; appends refuse until migrate()
        if self.review_path.exists():
            self._load()

    def _load(self) -> None:
        with open(self.review_path, "rb") as f:
            st = os.fstat(f.fileno())
            data = f.read()
        try:
            decisions, legacy = parse_log(self.review_path, data)
        except ValueError as exc:
            end = (
                max(data.rfind(b"\n"), data.rfind(b"\r")) + 1
            )  # just past the last complete line; csv also ends lines at a bare \r
            if end == len(data):
                raise
            decisions, legacy = parse_log(
                self.review_path, data[:end]
            )  # raises if the problem is not only the last line
            log.warning("ignoring the unfinished last line of %s (an interrupted write): %s", self.review_path, exc)
            self._truncate = PendingTruncate(st.st_ino, len(data), end)
        self._rows = latest(decisions)
        if legacy:
            self._legacy = LegacyLog(st.st_ino, len(data), decisions)

    def migrate(self) -> None:
        """Rewrite an old-header review.tsv with HEADER, every row kept and the new columns empty; else nothing.

        Only for a writer holding the work dir lock. The new file is written and synced beside the old one, then
        renamed over it, so readers see either file whole. A torn tail is dropped (it was never loaded).
        """
        if self._legacy is None:
            return
        legacy = self._legacy
        st = os.stat(self.review_path)
        if (st.st_ino, st.st_size) != (legacy.ino, legacy.size):
            raise RuntimeError(f"{self.review_path} changed since it was loaded; not migrating it")
        file_mode = policy_of_dir(self.work_dir).file_mode
        fd, tmp = tempfile.mkstemp(dir=self.work_dir, prefix=".review.tsv.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                os.fchmod(f.fileno(), file_mode)  # mkstemp creates 0600; teammates may need group access
                f.write(_format(True, legacy.decisions).encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.review_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self._legacy = None
        self._truncate = None  # the torn tail was not copied
        try:  # make the rename itself durable, where the filesystem can
            dir_fd = os.open(self.work_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            if exc.errno not in DIR_FSYNC_UNSUPPORTED:
                raise

    def _append(self, decisions: list[Decision]) -> None:
        """Append decisions to review.tsv as one buffer (written in a loop that handles short writes), then fsync.

        Earlier bytes never change, except a torn tail: one found by _load, or left by a failed append, is cut off first.
        An old-header file is refused: its rows would not match the header until migrate() rewrites it.
        """
        if self._legacy is not None:
            raise RuntimeError(
                f"{self.review_path} has the old {len(LEGACY_HEADER)}-column header; migrate() it before appending"
            )
        rows = _format(False, decisions)
        file_mode = policy_of_dir(self.work_dir).file_mode
        fd = os.open(self.review_path, os.O_RDWR | os.O_APPEND | os.O_CREAT, file_mode)
        try:
            st = os.fstat(fd)
            start = st.st_size
            if self._truncate is not None:
                if not self._truncate.matches(st):
                    raise RuntimeError(
                        f"{self.review_path} changed since it was loaded; not truncating its unfinished last line"
                    )
                os.ftruncate(fd, self._truncate.offset)  # drop the torn tail
                start = self._truncate.offset
            if start == 0:
                if stat.S_IMODE(st.st_mode) != file_mode:
                    os.fchmod(fd, file_mode)  # the umask may have stripped group bits teammates need to read it
                payload = _format(True, []) + rows
            else:
                last = os.pread(fd, 1, start - 1)
                prefix = (
                    "" if last == b"\n" else "\n" if last == b"\r" else "\r\n"
                )  # finish a last line that parsed but lacks its ending
                payload = prefix + rows
            remaining = payload.encode("utf-8")
            try:
                while remaining:
                    remaining = remaining[os.write(fd, remaining) :]  # os.write may write less than asked
                os.fsync(fd)
            except BaseException:
                self._truncate = PendingTruncate(st.st_ino, None, start)  # whatever landed is a torn tail
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, start)  # best effort; the next append retries it
                    self._truncate = PendingTruncate(st.st_ino, os.fstat(fd).st_size, start)
                raise
        finally:
            os.close(fd)
        self._truncate = None

    def mark(
        self, image_id: str, batch: str, status: Verdict, pass_number: int, *, reviewer: str, mode: MarkMode
    ) -> None:
        self.mark_many([(image_id, batch)], status, pass_number, reviewer=reviewer, mode=mode)

    def mark_many(
        self, targets: list[tuple[str, str]], status: Verdict, pass_number: int, *, reviewer: str, mode: MarkMode
    ) -> list[Change]:
        """Record one verdict on every (image_id, batch) in targets; grid_size is len(targets).

        Returns each row written with the decision it replaced, for undo_many.
        """
        if status not in get_args(Verdict):
            raise ValueError(f"Invalid status {status!r}, must be one of {get_args(Verdict)}")
        if mode not in get_args(MarkMode):
            raise ValueError(f"Invalid mode {mode!r}, must be one of {get_args(MarkMode)}")
        reviewer = parse_reviewer(reviewer)  # a tab or newline would corrupt the log
        ts = datetime.now(UTC).isoformat()
        tool_version = package_version()
        changes = []
        for image_id, batch in targets:
            existing = self._rows.get(image_id)
            recorded_pass = max(existing.pass_number, pass_number) if existing else pass_number  # never decreases
            changes.append(
                Change(
                    Decision(image_id, batch, status, recorded_pass, ts, reviewer, mode, len(targets), tool_version),
                    existing,
                )
            )
        decisions = [c.written for c in changes]
        self._append(
            decisions
        )  # on disk first; a failed append changes neither memory nor, once its tail is cut, the file
        self._rows.update((d.image_id, d) for d in decisions)
        return changes

    def undo_many(self, changes: list[Change], *, reviewer: str) -> None:
        """Undo changes (one mark_many's result) in one append: each image's previous decision is written again
        exactly (batch, status, pass_number; the pass may go down: it is a restore), or a tombstone in the undone
        row's batch and pass where there was none. Rows get mode "undo", `reviewer` and grid_size = images undone.
        """
        reviewer = parse_reviewer(reviewer)  # a tab or newline would corrupt the log
        ts = datetime.now(UTC).isoformat()
        tool_version = package_version()
        by_image = {c.written.image_id: c for c in changes}  # an image_id given twice (keys sharing it) is undone once
        decisions = [
            Decision(
                image_id,
                c.previous.batch,
                c.previous.status,
                c.previous.pass_number,
                ts,
                reviewer,
                "undo",
                len(by_image),
                tool_version,
            )
            if c.previous is not None
            else Decision(
                image_id,
                c.written.batch,
                TOMBSTONE,
                c.written.pass_number,
                ts,
                reviewer,
                "undo",
                len(by_image),
                tool_version,
            )
            for image_id, c in by_image.items()
        ]
        if not decisions:
            return
        self._append(decisions)  # on disk first, as in mark_many
        fold(self._rows, decisions)

    def decisions(self) -> dict[str, Decision]:
        """The latest decision per image_id (a copy; never a tombstone)."""
        return dict(self._rows)

    def has_torn_tail(self) -> bool:
        """Whether review.tsv ended in an unfinished line when loaded (dropped by the next append)."""
        return self._truncate is not None

    def get_status(self, image_id: str, current_pass: int) -> Status:
        row = self._rows.get(image_id)
        if not row:
            return "UNREVIEWED"
        if row.pass_number >= current_pass or row.status == "CLEAN":
            return row.status  # this pass or a later one, as recorded; CLEAN holds across passes
        return "FLAGGED"  # DIRTY in an earlier pass; needs re-review in this one

    def current_pass(self, image_ids: Iterable[str]) -> int:
        """Auto-detect the current pass number.

        If any manifest image has no row in _rows, we're on pass 1.
        Otherwise, next pass = max pass_number + 1.
        """
        ids = set(image_ids)
        if any(image_id not in self._rows for image_id in ids):
            return 1
        max_pass = max((self._rows[i].pass_number for i in ids), default=0)
        # Stay on max_pass if it still has unfinished work
        if any(self.get_status(i, max_pass) in TODO_STATUSES for i in ids):
            return max_pass
        return max_pass + 1
