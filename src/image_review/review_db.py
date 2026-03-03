import csv
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path


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
                self._rows[row["image_id"]] = row

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.work_dir, suffix=".tsv")
        try:
            with os.fdopen(fd, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=self.HEADER, delimiter="\t")
                writer.writeheader()
                writer.writerows(self._rows.values())
            os.replace(tmp, self.review_path)
        except BaseException:
            os.unlink(tmp)
            raise

    def mark(self, image_id: str, batch: str, status: str, pass_number: int) -> None:
        self._rows[image_id] = {
            "image_id": image_id,
            "batch": batch,
            "status": status,
            "pass_number": str(pass_number),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._save()

    def mark_many(self, image_ids: list[str], batch: str, status: str, pass_number: int) -> None:
        ts = datetime.now(UTC).isoformat()
        for image_id in image_ids:
            self._rows[image_id] = {
                "image_id": image_id,
                "batch": batch,
                "status": status,
                "pass_number": str(pass_number),
                "timestamp": ts,
            }
        self._save()

    def get_status(self, image_id: str) -> str:
        row = self._rows.get(image_id)
        return row["status"] if row else "UNREVIEWED"

    def images_for_review(self, manifest_rows: list[dict], pass_number: int, batch: str | None = None) -> list[dict]:
        """Return manifest rows that need review for the given pass.

        Pass 1: all images.
        Pass N>1: only images marked DIRTY in pass N-1.
        """
        result = []
        for row in manifest_rows:
            if batch and row["batch"] != batch:
                continue
            status = self.get_status(row["image_id"])
            if pass_number == 1:
                if status == "UNREVIEWED":
                    result.append(row)
            else:
                if status == "DIRTY":
                    result.append(row)
        return result

    def current_pass(self, manifest_rows: list[dict]) -> int:
        """Auto-detect the current pass number.

        If any images are UNREVIEWED, we're on pass 1.
        Otherwise, next pass = max pass_number + 1.
        """
        has_unreviewed = any(
            self.get_status(row["image_id"]) == "UNREVIEWED"
            for row in manifest_rows
        )
        if has_unreviewed:
            return 1
        max_pass = 0
        for row in self._rows.values():
            max_pass = max(max_pass, int(row["pass_number"]))
        return max_pass + 1

    def summary(self, manifest_rows: list[dict]) -> dict[str, int]:
        counts = {"CLEAN": 0, "DIRTY": 0, "UNREVIEWED": 0, "total": 0}
        for row in manifest_rows:
            status = self.get_status(row["image_id"])
            counts[status] = counts.get(status, 0) + 1
            counts["total"] += 1
        return counts

    def batch_summary(self, manifest_rows: list[dict]) -> dict[str, dict[str, int]]:
        batches: dict[str, dict[str, int]] = {}
        for row in manifest_rows:
            batch = row["batch"]
            if batch not in batches:
                batches[batch] = {"CLEAN": 0, "DIRTY": 0, "UNREVIEWED": 0, "total": 0}
            status = self.get_status(row["image_id"])
            batches[batch][status] = batches[batch].get(status, 0) + 1
            batches[batch]["total"] += 1
        return batches
