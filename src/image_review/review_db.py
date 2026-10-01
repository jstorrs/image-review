import contextlib
import csv
import io
import os
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from .access import policy_of_dir
from .status import TODO_STATUSES, Status, Verdict

HEADER = ["image_id", "batch", "status", "pass_number", "timestamp"]


@dataclass(frozen=True)
class Decision:
    image_id: str
    batch: str
    status: Verdict
    pass_number: int
    timestamp: str


def parse_decision(path: Path, line: int, fields: list[str]) -> Decision:
    where = f"{path}:{line}"
    if len(fields) != len(HEADER):
        raise ValueError(f"{where}: expected {len(HEADER)} tab-separated fields ({', '.join(HEADER)}), got {len(fields)}")
    image_id, batch, status, pass_text, timestamp = fields
    if not image_id:
        raise ValueError(f"{where}: image_id is empty")
    if status not in get_args(Verdict):
        raise ValueError(f"{where}: status must be one of {', '.join(get_args(Verdict))}, got {status!r}")
    try:
        pass_number = int(pass_text)
    except ValueError:
        raise ValueError(f"{where}: pass_number must be an integer, got {pass_text!r}") from None
    if pass_number < 1:
        raise ValueError(f"{where}: pass_number must be at least 1, got {pass_number}")
    return Decision(image_id, batch, status, pass_number, timestamp)  # type: ignore[arg-type]  # status checked above


@dataclass(frozen=True)
class PendingTruncate:
    """A torn tail of review.tsv, from `offset` to the end, as seen in the file `ino` when it was `size` bytes."""

    ino: int
    size: int | None  # None: unknown after a failed append; only the inode is checked
    offset: int

    def matches(self, st: os.stat_result) -> bool:
        return st.st_ino == self.ino and self.size in (None, st.st_size)


def _header_line() -> str:
    buf = io.StringIO()
    csv.writer(buf, delimiter="\t").writerow(HEADER)
    return buf.getvalue()


def parse_log(path: Path, data: bytes) -> dict[str, Decision]:
    """Fold review.tsv's bytes into the latest decision per image_id; an empty file holds none."""
    if not data:
        return {}  # created but the first append never landed
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not valid UTF-8 at byte {exc.start}") from None
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t")
    if next(reader, None) != HEADER:
        raise ValueError(f"{path}:1: header must be {', '.join(HEADER)}")
    rows: dict[str, Decision] = {}
    for fields in reader:
        decision = parse_decision(path, reader.line_num, fields)
        rows[decision.image_id] = decision  # last row wins
    return rows


class ReviewDB:
    HEADER = HEADER

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.review_path = work_dir / "review.tsv"
        self._rows: dict[str, Decision] = {}  # keyed by image_id; last row wins
        self._truncate: PendingTruncate | None = None  # a torn tail to drop before the next append
        if self.review_path.exists():
            self._load()

    def _load(self) -> None:
        with open(self.review_path, "rb") as f:
            st = os.fstat(f.fileno())
            data = f.read()
        try:
            self._rows = parse_log(self.review_path, data)
        except ValueError as exc:
            end = max(data.rfind(b"\n"), data.rfind(b"\r")) + 1  # just past the last complete line; csv also ends lines at a bare \r
            if end == len(data):
                raise
            self._rows = parse_log(self.review_path, data[:end])  # raises if the problem is not only the last line
            print(f"WARNING: ignoring the unfinished last line of {self.review_path} (an interrupted write): {exc}", file=sys.stderr)
            self._truncate = PendingTruncate(st.st_ino, len(data), end)

    def _append(self, decisions: list[Decision]) -> None:
        """Append decisions to review.tsv as one buffer (written in a loop that handles short writes), then fsync.

        Earlier bytes never change, except a torn tail: one found by _load, or left by a failed append, is cut off first.
        """
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter="\t")  # csv's default "\r\n" line ending, as review.tsv has always used
        writer.writerows([d.image_id, d.batch, d.status, d.pass_number, d.timestamp] for d in decisions)
        file_mode = policy_of_dir(self.work_dir).file_mode
        fd = os.open(self.review_path, os.O_RDWR | os.O_APPEND | os.O_CREAT, file_mode)
        try:
            st = os.fstat(fd)
            start = st.st_size
            if self._truncate is not None:
                if not self._truncate.matches(st):
                    raise RuntimeError(f"{self.review_path} changed since it was loaded; not truncating its unfinished last line")
                os.ftruncate(fd, self._truncate.offset)  # drop the torn tail
                start = self._truncate.offset
            if start == 0:
                if stat.S_IMODE(st.st_mode) != file_mode:
                    os.fchmod(fd, file_mode)  # the umask may have stripped group bits teammates need to read it
                payload = _header_line() + buf.getvalue()
            else:
                last = os.pread(fd, 1, start - 1)
                prefix = "" if last == b"\n" else "\n" if last == b"\r" else "\r\n"  # finish a last line that parsed but lacks its ending
                payload = prefix + buf.getvalue()
            remaining = payload.encode("utf-8")
            try:
                while remaining:
                    remaining = remaining[os.write(fd, remaining):]  # os.write may write less than asked
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

    def mark(self, image_id: str, batch: str, status: Verdict, pass_number: int) -> None:
        self.mark_many([image_id], batch, status, pass_number)

    def mark_many(self, image_ids: list[str], batch: str, status: Verdict, pass_number: int) -> None:
        if status not in get_args(Verdict):
            raise ValueError(f"Invalid status {status!r}, must be one of {get_args(Verdict)}")
        ts = datetime.now(UTC).isoformat()
        decisions = []
        for image_id in image_ids:
            existing = self._rows.get(image_id)
            recorded_pass = max(existing.pass_number, pass_number) if existing else pass_number  # never decreases
            decisions.append(Decision(image_id, batch, status, recorded_pass, ts))
        self._append(decisions)  # on disk first; a failed append changes neither memory nor, once its tail is cut, the file
        self._rows.update((d.image_id, d) for d in decisions)

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
