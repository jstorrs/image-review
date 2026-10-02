"""Export rows: one per source file, folded from the manifest, skipped.tsv and decisions (stdlib only)."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .review_db import Decision
    from .store import ManifestEntry, SkippedRow

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
