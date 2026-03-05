import csv
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

VALID_STATUSES = {"CLEAN", "DIRTY"}


class ReviewDB:
    HEADER = ["image_id", "batch", "status", "pass_number", "timestamp"]

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.review_path = work_dir / "review.tsv"
        self._rows: dict[str, dict] = {}  # keyed by image_id
        if self.review_path.exists():
            self._load()

    def _load(self) -> None:
        with open(self.review_path, newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                try:
                    row["pass_number"] = int(row["pass_number"])
                    self._rows[row["image_id"]] = row
                except (ValueError, KeyError) as exc:
                    print(f"WARNING: skipping malformed row in {self.review_path}: {exc}", file=sys.stderr)

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.work_dir, suffix=".tsv")
        try:
            with os.fdopen(fd, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self.HEADER, delimiter="\t")
                writer.writeheader()
                for row in self._rows.values():
                    writer.writerow({**row, "pass_number": str(row["pass_number"])})
            os.replace(tmp, self.review_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def mark(self, image_id: str, batch: str, status: str, pass_number: int) -> None:
        self.mark_many([image_id], batch, status, pass_number)

    def mark_many(self, image_ids: list[str], batch: str, status: str, pass_number: int) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {status!r}, must be one of {VALID_STATUSES}")
        ts = datetime.now(UTC).isoformat()
        for image_id in image_ids:
            self._rows[image_id] = {
                "image_id": image_id,
                "batch": batch,
                "status": status,
                "pass_number": pass_number,
                "timestamp": ts,
            }
        self._save()

    def get_status(self, image_id: str, current_pass: int) -> str:
        row = self._rows.get(image_id)
        if not row:
            return "UNREVIEWED"
        if row["pass_number"] == current_pass:
            return row["status"]
        if row["status"] == "CLEAN":
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

    def current_pass(self, manifest_rows: list[dict]) -> int:
        """Auto-detect the current pass number.

        If any manifest image has no row in _rows, we're on pass 1.
        Otherwise, next pass = max pass_number + 1.
        """
        manifest_ids = {row["image_id"] for row in manifest_rows}
        if any(image_id not in self._rows for image_id in manifest_ids):
            return 1
        max_pass = max(
            (r["pass_number"] for r in self._rows.values() if r["image_id"] in manifest_ids),
            default=0,
        )
        # Stay on max_pass if it still has unfinished work
        if any(self.get_status(r["image_id"], max_pass) == "UNREVIEWED" for r in manifest_rows):
            return max_pass
        return max_pass + 1

    def summary(self, manifest_rows: list[dict], pass_number: int) -> dict[str, int]:
        counts = {"CLEAN": 0, "DIRTY": 0, "UNREVIEWED": 0, "total": 0}
        for row in manifest_rows:
            status = self.get_status(row["image_id"], pass_number)
            counts[status] += 1
            counts["total"] += 1
        return counts

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
