import csv
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from .access import policy_of_dir
from .status import Status, Verdict

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


class ReviewDB:
    HEADER = HEADER

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.review_path = work_dir / "review.tsv"
        self._rows: dict[str, Decision] = {}  # keyed by image_id; last row wins
        if self.review_path.exists():
            self._load()

    def _load(self) -> None:
        with open(self.review_path, newline="") as f:
            reader = csv.reader(f, delimiter="\t")
            if next(reader, None) != HEADER:
                raise ValueError(f"{self.review_path}:1: header must be {', '.join(HEADER)}")
            for fields in reader:
                decision = parse_decision(self.review_path, reader.line_num, fields)
                self._rows[decision.image_id] = decision

    def _save(self) -> None:
        file_mode = policy_of_dir(self.work_dir).file_mode
        fd, tmp = tempfile.mkstemp(dir=self.work_dir, suffix=".tsv")
        try:
            with os.fdopen(fd, "w", newline="") as f:
                os.fchmod(f.fileno(), file_mode)  # mkstemp is always 0600; follow the work dir's policy
                writer = csv.writer(f, delimiter="\t")
                writer.writerow(HEADER)
                for d in self._rows.values():
                    writer.writerow([d.image_id, d.batch, d.status, d.pass_number, d.timestamp])
            os.replace(tmp, self.review_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def mark(self, image_id: str, batch: str, status: Verdict, pass_number: int) -> None:
        self.mark_many([image_id], batch, status, pass_number)

    def mark_many(self, image_ids: list[str], batch: str, status: Verdict, pass_number: int) -> None:
        if status not in get_args(Verdict):
            raise ValueError(f"Invalid status {status!r}, must be one of {get_args(Verdict)}")
        ts = datetime.now(UTC).isoformat()
        for image_id in image_ids:
            self._rows[image_id] = Decision(image_id, batch, status, pass_number, ts)
        self._save()

    def get_status(self, image_id: str, current_pass: int) -> Status:
        row = self._rows.get(image_id)
        if not row:
            return "UNREVIEWED"
        if row.pass_number == current_pass:
            return row.status
        if row.status == "CLEAN":
            return "CLEAN"
        # DIRTY from a prior pass → treat as UNREVIEWED
        return "UNREVIEWED"

    def images_by_status(self, manifest_rows: list[dict], pass_number: int, status_filter: str = "unreviewed", batch: str | None = None) -> list[dict]:
        """Return manifest rows filtered by status.

        status_filter: "unreviewed", "clean", or "all".
        """
        if status_filter not in ("all", "clean", "unreviewed"):
            raise ValueError(f"Invalid status_filter {status_filter!r}, must be 'unreviewed', 'clean', or 'all'")
        rows = [r for r in manifest_rows if not batch or r["batch"] == batch]
        if status_filter == "all":
            return rows
        target = "CLEAN" if status_filter == "clean" else "UNREVIEWED"
        return [r for r in rows if self.get_status(r["image_id"], pass_number) == target]

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
        if any(self.get_status(i, max_pass) == "UNREVIEWED" for i in ids):
            return max_pass
        return max_pass + 1

    def summary(self, manifest_rows: list[dict], pass_number: int) -> dict[str, int]:
        totals = {"CLEAN": 0, "DIRTY": 0, "UNREVIEWED": 0, "total": 0}
        for bc in self.batch_summary(manifest_rows, pass_number).values():
            for k in totals:
                totals[k] += bc[k]
        return totals

    def batch_summary(self, manifest_rows: list[dict], pass_number: int) -> dict[str, dict[str, int]]:
        batches: dict[str, dict[str, int]] = {}
        for row in manifest_rows:
            batch = row["batch"]
            if batch not in batches:
                batches[batch] = {"CLEAN": 0, "DIRTY": 0, "UNREVIEWED": 0, "total": 0}
            status = self.get_status(row["image_id"], pass_number)
            batches[batch][status] += 1
            batches[batch]["total"] += 1
        return batches
