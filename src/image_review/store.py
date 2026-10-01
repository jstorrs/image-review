import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .review_db import ReviewDB

Status = Literal["CLEAN", "DIRTY", "UNREVIEWED"]
Verdict = Literal["CLEAN", "DIRTY"]


@dataclass(frozen=True)
class ManifestRow:
    key: str  # preprocessed_path; the only identifier the client ever sees
    batch: str


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


def load_manifest(work_dir: Path) -> list[dict]:
    manifest_path = work_dir / "manifest.tsv"
    with open(manifest_path, newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


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
    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self._raw = load_manifest(work_dir)
        self._rows = [ManifestRow(key=r["preprocessed_path"], batch=r["batch"]) for r in self._raw]
        self._image_ids = {r["preprocessed_path"]: r["image_id"] for r in self._raw}
        self._keys_by_image_id: dict[str, list[str]] = {}
        for key, iid in self._image_ids.items():
            self._keys_by_image_id.setdefault(iid, []).append(key)
        self._db = ReviewDB(work_dir)

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
        image_ids = [self._image_ids[key] for key in keys]
        self._db.mark_many(image_ids, batch, status, pass_number)
        return {
            k: self._db.get_status(iid, pass_number)
            for iid in dict.fromkeys(image_ids)
            for k in self._keys_by_image_id[iid]
        }

    def current_pass(self) -> int:
        return self._db.current_pass(self._raw)

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
