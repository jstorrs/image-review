from pathlib import Path

import pygame as pg
import skimage as ski


def safe_path(work_dir: Path, relative: str) -> Path:
    """Resolve a relative path within work_dir, rejecting traversal attempts."""
    resolved = (work_dir / relative).resolve()
    if not resolved.is_relative_to(work_dir.resolve()):
        raise ValueError(f"Path escapes work directory: {relative}")
    return resolved


def load_surface(path: str) -> pg.Surface:
    img = ski.io.imread(path)
    return pg.surfarray.make_surface(img.transpose(1, 0, 2))
