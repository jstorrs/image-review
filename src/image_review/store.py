import csv
import hashlib
import io
import logging
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self, get_args

from .access import MANIFEST_NAME
from .export import ExportRow, export_rows
from .lock import LockHolder, acquire_lock, release_lock
from .review_db import Change, ReviewDB, decode_utf8
from .status import TODO_STATUSES, ImageId, Key, MarkMode, Status, Verdict, parse_choice

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManifestRow:
    key: Key  # preprocessed_path; the only identifier the client ever sees
    batch: str


@dataclass(frozen=True)
class ManifestEntry:
    batch: str
    key: Key  # preprocessed_path
    image_id: ImageId  # original source path; stays server-side
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

    def image_bytes(self, key: Key) -> bytes: ...

    def image_bytes_many(self, keys: list[Key]) -> dict[Key, bytes]:
        """Bytes for the keys that loaded; missing or unloadable keys are omitted with a logged warning."""
        ...

    def statuses(self, pass_number: int) -> dict[Key, Status]:
        """Key -> status for every manifest row."""
        ...

    def mark(
        self, keys: list[Key], status: Verdict, pass_number: int, *, reviewer: str, mode: MarkMode
    ) -> dict[Key, Status]:
        """Record a verdict on keys, given by `reviewer` (an unauthenticated claim) in `mode`.

        Returns the new status of every key affected (incl. keys sharing an image_id).
        """
        ...

    def undo(self, pass_number: int, *, reviewer: str) -> dict[Key, Status]:
        """Undo the latest mark not yet undone, restoring each image's decision from before it; `reviewer` is recorded.

        Returns the new status of every key affected, as mark does; {} when there is nothing to undo. The marks
        that can be undone are the store's (the server's, for a remote store), held in memory since it was opened.
        """
        ...

    def current_pass(self) -> int: ...

    def skipped(self) -> SkippedCounts:
        """Counts from preprocess's skipped.tsv (zero if the work dir has none). ValueError if it is malformed."""
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
    """Parse manifest.tsv strictly. ValueError (naming file:line, or file and byte offset if not UTF-8) if malformed;
    FileNotFoundError if absent.

    Both the current header and the legacy 3-column one (no hashes) are accepted; every row has the header's width.
    """
    path = work_dir / MANIFEST_NAME
    entries: list[ManifestEntry] = []
    key_lines: dict[str, int] = {}
    reader = csv.reader(io.StringIO(decode_utf8(path, path.read_bytes()), newline=""), delimiter="\t")
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
        entries.append(ManifestEntry(batch, Key(key), ImageId(image_id), source_sha256, jpeg_sha256))
    return entries


def check_jpeg_hash(key: Key, data: bytes, expected: str | None) -> bytes:
    """data, if it matches the recorded SHA-256 (or none is recorded). ValueError otherwise: the image is unloadable.

    Pillow decodes a JPG cut short but still ending in EOI without complaint (grey rows); the hash catches it.
    """
    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"{key} does not match its recorded hash")
    return data


SKIPPED_NAME = "skipped.tsv"
SKIPPED_HEADER = ["image_id", "kind", "reason"]
SkipKind = Literal["failed", "ignored"]
SKIP_KINDS: tuple[SkipKind, ...] = get_args(SkipKind)


@dataclass(frozen=True)
class SkippedRow:
    """One row of skipped.tsv: an input preprocess left out of the manifest. image_id is a source path, as in the manifest."""

    image_id: ImageId
    kind: SkipKind
    reason: str


def load_skipped(work_dir: Path) -> list[SkippedRow]:
    """Parse skipped.tsv strictly, in file order; empty if the work dir has none. ValueError (naming file:line, or file and byte offset if not UTF-8) if malformed."""
    path = work_dir / SKIPPED_NAME
    if not path.exists():
        return []
    rows: list[SkippedRow] = []
    reader = csv.reader(io.StringIO(decode_utf8(path, path.read_bytes()), newline=""), delimiter="\t")
    if next(reader, None) != SKIPPED_HEADER:
        raise ValueError(f"{path}:1: header must be {', '.join(SKIPPED_HEADER)}")
    for fields in reader:
        kind = parse_choice(fields[1], SKIP_KINDS) if len(fields) == len(SKIPPED_HEADER) else None
        if kind is None:
            raise ValueError(f"{path}:{reader.line_num}: expected image_id, kind (failed or ignored), reason")
        rows.append(SkippedRow(ImageId(fields[0]), kind, fields[2]))
    return rows


def skipped_counts(rows: Iterable[SkippedRow]) -> SkippedCounts:
    """The one place the failed/ignored split is derived, so every report of it agrees."""
    kinds = [r.kind for r in rows]
    failed = kinds.count("failed")
    return SkippedCounts(failed=failed, ignored=len(kinds) - failed)


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
        self._by_key: dict[Key, ManifestEntry] = {e.key: e for e in entries}
        self._keys_by_image_id: dict[ImageId, list[Key]] = {}
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

    def image_bytes(self, key: Key) -> bytes:
        entry = self._by_key[key]
        return check_jpeg_hash(key, safe_path(self.work_dir, key).read_bytes(), entry.jpeg_sha256)

    def image_bytes_many(self, keys: list[Key]) -> dict[Key, bytes]:
        found: dict[Key, bytes] = {}
        for key in keys:
            try:
                found[key] = self.image_bytes(key)
            except (KeyError, ValueError, OSError) as exc:
                log.warning("cannot load %s: %s", key, exc)
        return found

    def statuses(self, pass_number: int) -> dict[Key, Status]:
        return {key: self._db.get_status(e.image_id, pass_number) for key, e in self._by_key.items()}

    def _require_writable(self) -> None:
        if self._lock is None:
            state = "opened read-only" if self.read_only else "closed"
            raise PermissionError(f"store for {self.work_dir} was {state}; cannot record verdicts")

    def _affected(self, image_ids: list[ImageId], pass_number: int) -> dict[Key, Status]:
        """The status of every key of these image_ids, in their order."""
        return {
            k: self._db.get_status(iid, pass_number)
            for iid in dict.fromkeys(image_ids)
            for k in self._keys_by_image_id[iid]
        }

    def mark(
        self, keys: list[Key], status: Verdict, pass_number: int, *, reviewer: str, mode: MarkMode
    ) -> dict[Key, Status]:
        self._require_writable()
        targets = [self._by_key[k] for k in keys]  # an unknown key raises before anything is written
        changes = self._db.mark_many(
            [(e.image_id, e.batch) for e in targets], status, pass_number, reviewer=reviewer, mode=mode
        )
        self._undo.append(changes)  # only once written
        return self._affected([e.image_id for e in targets], pass_number)

    def undo(self, pass_number: int, *, reviewer: str) -> dict[Key, Status]:
        self._require_writable()
        if not self._undo:
            return {}
        changes = self._undo[-1]
        self._db.undo_many(changes, reviewer=reviewer)
        self._undo.pop()  # only once written: a failed undo can be retried
        return self._affected([c.written.image_id for c in changes], pass_number)

    def current_pass(self) -> int:
        return self._db.current_pass(e.image_id for e in self._by_key.values())

    def skipped(self) -> SkippedCounts:
        return skipped_counts(load_skipped(self.work_dir))

    def export_rows(self) -> list["ExportRow"]:
        """The study's result, one row per source file (see export_rows). Local only: image_ids never leave the work
        dir's machine. ValueError if skipped.tsv is malformed or review.tsv ends in a torn line."""
        return export_rows(self._entries, self._db.decisions(), load_skipped(self.work_dir))


# The review --filter vocabulary, parsed by the CLI's click.Choice.
StatusFilter = Literal["unreviewed", "clean", "all"]


def filter_rows(
    rows: list[ManifestRow],
    statuses: dict[Key, Status],
    status_filter: StatusFilter = "unreviewed",
    batch: str | None = None,
) -> list[ManifestRow]:
    """Filter rows by status and optional batch.

    "unreviewed" selects the todo statuses (UNREVIEWED and FLAGGED), "clean" selects CLEAN, "all" everything.
    """
    selected = [r for r in rows if not batch or r.batch == batch]
    match status_filter:
        case "all":
            return selected
        case "clean":
            return [r for r in selected if statuses[r.key] == "CLEAN"]
        case "unreviewed":
            return [r for r in selected if statuses[r.key] in TODO_STATUSES]


def batch_summary(rows: list[ManifestRow], statuses: dict[Key, Status]) -> dict[str, Counter[Status]]:
    """Per batch: a count for each Status (a missing one counts 0); `.total()` is the batch's image count."""
    by_batch: dict[str, Counter[Status]] = {}
    for row in rows:
        by_batch.setdefault(row.batch, Counter())[statuses[row.key]] += 1
    return by_batch


def summary(rows: list[ManifestRow], statuses: dict[Key, Status]) -> Counter[Status]:
    """A count for each Status (a missing one counts 0); `.total()` is the image count."""
    return Counter(statuses[row.key] for row in rows)
