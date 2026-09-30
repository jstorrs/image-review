import csv
from pathlib import Path

import numpy as np
import skimage as ski

# (batch, preprocessed_path, image_id); image_ids deliberately differ from keys
ROWS = [
    ("batch_001", "batch_001/a.jpg", "/src/patient_smith/a.dcm"),
    ("batch_001", "batch_001/b.jpg", "/src/patient_jones/b.dcm"),
    ("batch_002", "batch_002/c.jpg", "/src/patient_lee/c.dcm"),
    ("batch_002", "batch_002/d.jpg", "/src/patient_kim/d.dcm"),
]


def make_work_dir(root: Path) -> None:
    for _, key, _ in ROWS:
        path = root / key
        path.parent.mkdir(exist_ok=True)
        ski.io.imsave(path, np.full((12, 20, 3), 128, dtype=np.uint8), check_contrast=False)
    with open(root / "manifest.tsv", "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["batch", "preprocessed_path", "image_id"])
        writer.writerows(ROWS)


def start_server(work_dir: Path, port: int = 0):
    """Serve work_dir on 127.0.0.1 in a thread; returns (server, target, stop)."""
    import threading

    from image_review.server import make_server
    from image_review.store import LocalStore

    server, target = make_server(LocalStore(work_dir), "127.0.0.1", port)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        thread.join()
        server.server_close()

    return server, target, stop
