"""Export rows: one per source file, folded from the manifest, skipped.tsv and decisions; split into the allowlist of
releasable files and the report of the rest (stdlib only)."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, NewType

from .status import ImageId

if TYPE_CHECKING:
    from .review_db import Decision
    from .store import ManifestEntry, SkipKind, SkippedRow

# What an export says about one source file. NOT_REVIEWED: preprocess failed to render it (or part of it), so nobody
# saw that part. IGNORED: preprocess did not take it for an image (e.g. a PDF), so nobody looked at it at all.
ExportStatus = Literal["CLEAN", "DIRTY", "UNREVIEWED", "NOT_REVIEWED", "IGNORED"]
ALLOWLIST_HEADER = ["source_sha256", "image_id", "pass_number", "timestamp", "reviewer"]
REPORT_HEADER = ["image_id", "status", "pass_number", "timestamp", "reviewer", "reason", "source_sha256"]
ICON_SUFFIX = "#icon"  # a DICOM's embedded icon image
# A part's status: never IGNORED, since an ignored input that is not a part gets a row of its own, never folded.
_PartStatus = Literal["CLEAN", "DIRTY", "UNREVIEWED", "NOT_REVIEWED"]
# The worst part decides a file's status: CLEAN only if every part is CLEAN.
_SEVERITY: dict[_PartStatus, int] = {"CLEAN": 0, "UNREVIEWED": 1, "NOT_REVIEWED": 2, "DIRTY": 3}
# U+2028/U+2029 and control characters: str.splitlines() breaks lines at some, and a TSV cell cannot hold a tab.
_LINE_SEPARATORS = frozenset("\u2028\u2029")


@dataclass(frozen=True)
class ExportRow:
    image_id: ImageId  # a source file, or a ZIP entry (`<zip>::<name>`)
    status: ExportStatus
    pass_number: int | None  # from the main image's latest decision; None, and timestamp and reviewer "", without one
    timestamp: str
    reviewer: str
    reason: str  # why the row is not simply its main image's verdict (skip reasons, the icon's state); often ""
    source_sha256: str  # the source file's SHA-256 from the manifest; "" when it has none (legacy, or never rendered)


# A row split_allowlist allowed: only it may be formatted as the allowlist.
AllowedRow = NewType("AllowedRow", ExportRow)


@dataclass(frozen=True)
class _Part:
    """One manifest or skipped image_id, before an icon is folded into its file."""

    status: _PartStatus
    decision: Decision | None  # None for UNREVIEWED and NOT_REVIEWED
    reason: str  # the skip reason, for NOT_REVIEWED


def _first_reasons(skipped: list[SkippedRow], kind: SkipKind) -> dict[ImageId, str]:
    """The first reason per image_id among the skipped rows of `kind`, in skipped.tsv order."""
    reasons: dict[ImageId, str] = {}
    for row in skipped:
        if row.kind == kind:
            reasons.setdefault(row.image_id, row.reason)
    return reasons


def _parts(
    entries: list[ManifestEntry],
    decisions: dict[ImageId, Decision],
    failed: dict[ImageId, str],
    ignored: dict[ImageId, str],
) -> dict[ImageId, _Part]:
    """Each manifest or failed image_id once, manifest order then failed order. A `failed` row makes it NOT_REVIEWED
    (with its reason), even if the manifest has it too; so does an `ignored` row for a manifest image_id (the two
    disagree, so nobody can vouch for it)."""
    parts: dict[ImageId, _Part] = {}
    for image_id in dict.fromkeys([*(e.image_id for e in entries), *failed]):
        decision = decisions.get(image_id)
        if image_id in failed:
            parts[image_id] = _Part("NOT_REVIEWED", None, failed[image_id])
        elif image_id in ignored:
            parts[image_id] = _Part("NOT_REVIEWED", None, ignored[image_id])
        elif decision is None:
            parts[image_id] = _Part("UNREVIEWED", None, "")
        else:
            parts[image_id] = _Part(decision.status, decision, "")  # CLEAN or DIRTY: latest() drops tombstones
    return parts


_MAIN_MISSING = _Part("NOT_REVIEWED", None, "main image missing")  # an icon without its file: never CLEAN


def _main(f: ImageId, parts: dict[ImageId, _Part], ignored: dict[ImageId, str]) -> _Part:
    """File `f`'s main part: its own part; else NOT_REVIEWED with its `ignored` reason; else `_MAIN_MISSING`."""
    if f in parts:
        return parts[f]
    if f in ignored:
        return _Part("NOT_REVIEWED", None, ignored[f])
    return _MAIN_MISSING


def _fold(image_id: ImageId, main: _Part, icon: _Part | None, source_sha256: str) -> ExportRow:
    """One file's row: the worst status of its main image and its icon; pass, timestamp and reviewer from the main
    image's decision."""
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
    entries: list[ManifestEntry], decisions: dict[ImageId, Decision], skipped: list[SkippedRow]
) -> list[ExportRow]:
    """One row per source file (a ZIP entry counts as one), in order of first appearance: manifest, then the
    `failed` rows of skipped.tsv, then the `ignored` rows (inputs that are not images) that no earlier row covers.

    A part (manifest or failed image_id) is NOT_REVIEWED if skipped.tsv has a `failed` row for it, or an `ignored`
    one (the first `failed` reason wins, else the first `ignored` one); else its latest decision's status (CLEAN or
    DIRTY), else UNREVIEWED. A DIRTY from an earlier pass not yet re-reviewed (FLAGGED in `status`) is DIRTY: it still
    contains PHI. A DICOM's icon (`X#icon`) is folded into `X`'s row, whose status is the worse of the two (DIRTY,
    then NOT_REVIEWED, then UNREVIEWED, then CLEAN); an icon without its `X` is reported under `X`, as if `X` were
    NOT_REVIEWED. `decisions` is the latest decision per image_id (review_db.latest); ones for image_ids in neither
    file are ignored. A row's source_sha256 is the first one the manifest records for `X` or `X#icon` (they share
    it), else "".

    An `ignored` image_id gets a row of its own, IGNORED with the first such row's reason, unless it is a part or the
    file of a folded row: a part's status already covers it (NOT_REVIEWED for a manifest image_id), and an ignored `X`
    with a manifest or failed `X#icon` but no part `X` is that row's main part, NOT_REVIEWED with the ignored reason
    (in place of `main image missing`). An ignored `X#icon` that is not a part is a source path like any other (a
    real file may be named so), so it is not folded into `X`.
    """
    failed = _first_reasons(skipped, "failed")
    ignored = _first_reasons(skipped, "ignored")
    parts = _parts(entries, decisions, failed, ignored)
    hashes: dict[ImageId, str] = {}
    for e in entries:
        if e.source_sha256 is not None:
            hashes.setdefault(ImageId(e.image_id.removesuffix(ICON_SUFFIX)), e.source_sha256)
    files = dict.fromkeys(ImageId(image_id.removesuffix(ICON_SUFFIX)) for image_id in parts)
    rows = [_fold(f, _main(f, parts, ignored), parts.get(ImageId(f + ICON_SUFFIX)), hashes.get(f, "")) for f in files]
    covered = parts.keys() | files.keys()
    rows += [ExportRow(i, "IGNORED", None, "", "", r, "") for i, r in ignored.items() if i not in covered]
    return rows


def has_unsafe_char(text: str) -> bool:
    """Whether `text` holds a control character (C0 incl. tab, CR and LF, DEL, or C1 incl. U+0085) or U+2028/U+2029."""
    return any(c in _LINE_SEPARATORS or unicodedata.category(c) == "Cc" for c in text)


def split_allowlist(rows: list[ExportRow]) -> tuple[list[AllowedRow], list[ExportRow]]:
    """(allowed, report), each in input order; every row lands in exactly one. A row is allowed only if it is CLEAN,
    has a source_sha256, and no row that is not CLEAN has the same source_sha256 (identical bytes cannot be both clean
    and not). A CLEAN row that is denied goes to the report, still CLEAN, with the reason it was not allowlisted."""
    unsafe_hashes = {r.source_sha256 for r in rows if r.status != "CLEAN" and r.source_sha256}
    allowed: list[AllowedRow] = []
    report: list[ExportRow] = []
    for r in rows:
        if r.status != "CLEAN":
            report.append(r)
        elif not r.source_sha256:
            report.append(_denied(r, "no source_sha256 (work directory from an older version)"))
        elif r.source_sha256 in unsafe_hashes:
            report.append(_denied(r, "same content as a file that is not CLEAN"))
        else:
            allowed.append(AllowedRow(r))
    return allowed, report


def _denied(row: ExportRow, why: str) -> ExportRow:
    """`row` with `not allowlisted: <why>` added to its reason."""
    return replace(row, reason="; ".join(n for n in (row.reason, f"not allowlisted: {why}") if n))


def _pass(row: ExportRow) -> str:
    return "" if row.pass_number is None else str(row.pass_number)


def format_allowlist(rows: list[AllowedRow]) -> str:
    """The allowlist as TSV text: ALLOWLIST_HEADER, then one line per row (see `_format_tsv`)."""
    return _format_tsv(
        ALLOWLIST_HEADER, [(r.image_id, [r.source_sha256, r.image_id, _pass(r), r.timestamp, r.reviewer]) for r in rows]
    )


def format_report(rows: list[ExportRow]) -> str:
    """The report as TSV text: REPORT_HEADER, then one line per row (see `_format_tsv`)."""
    return _format_tsv(
        REPORT_HEADER,
        [
            (r.image_id, [r.image_id, r.status, _pass(r), r.timestamp, r.reviewer, r.reason, r.source_sha256])
            for r in rows
        ],
    )


def _format_tsv(header: list[str], lines: list[tuple[str, list[str]]]) -> str:
    """`header`, then each (image_id, fields) line; tab-separated, LF line endings, no quoting.

    ValueError, naming the image_id, if a field could not be read back unambiguously: it holds a control character
    (C0 incl. tab, CR and LF, DEL, or C1 incl. U+0085) or U+2028/U+2029, or it starts with `"` (which CSV-aware
    readers take as the start of a quoted field).
    """
    out = ["\t".join(header)]
    for image_id, fields in lines:
        for name, value in zip(header, fields, strict=True):
            if value.startswith('"') or has_unsafe_char(value):
                raise ValueError(
                    f"cannot export {image_id!r}: its {name} contains a control character or line separator, or "
                    'starts with ", which this unquoted TSV cannot hold'
                )
        out.append("\t".join(fields))
    return "".join(f"{line}\n" for line in out)
