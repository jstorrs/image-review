from typing import Literal, NewType, get_args

# A manifest key: an image's preprocessed JPG path, the only identifier a client ever sees (never a source path).
Key = NewType("Key", str)
# A source image id: its source path, or `<zip>::<entry>` (possibly PHI); it never leaves LocalStore.
ImageId = NewType("ImageId", str)

# FLAGGED: marked DIRTY in another pass and not yet re-reviewed in this one. Derived, never stored.
Status = Literal["CLEAN", "DIRTY", "UNREVIEWED", "FLAGGED"]
Verdict = Literal["CLEAN", "DIRTY"]
VERDICTS: tuple[Verdict, ...] = get_args(Verdict)

# Statuses that still need a verdict in the current pass.
TODO_STATUSES: frozenset[Status] = frozenset({"UNREVIEWED", "FLAGGED"})

# How a verdict was given: on one image, or on every image of a grid at once. A client may only give these.
MarkMode = Literal["single", "grid"]
MARK_MODES: tuple[MarkMode, ...] = get_args(MarkMode)

# When images may be rotated 90 degrees in a grid: "auto" rotates only if that saves a grid
Rotation = Literal["auto", "always", "never"]
ROTATIONS: tuple[Rotation, ...] = get_args(Rotation)


def parse_choice[T: str](value: object, choices: tuple[T, ...]) -> T | None:
    """The member of `choices` equal to `value`, or None; unlike `in`, the result is narrowed to the Literal."""
    return next((c for c in choices if c == value), None)


# A grid verdict applies to every image in it, so grids only hold images not yet judged
# DIRTY (this pass) or FLAGGED (DIRTY in another pass): one keypress must never clear those.
GRID_ELIGIBLE: frozenset[Status] = frozenset({"UNREVIEWED", "CLEAN"})


class GridCleanRefused(Exception):
    """A store refused CLEAN on a grid by grid_clean_refused, recording nothing. Not an outage."""


def grid_status(snapshot: dict[Key, Status], keys: tuple[Key, ...]) -> Status:
    statuses = {snapshot[key] for key in keys}
    if not statuses <= GRID_ELIGIBLE:
        return "DIRTY"  # e.g. a key sharing an image_id with one marked DIRTY elsewhere this session
    if statuses & TODO_STATUSES:
        return "UNREVIEWED"
    return "CLEAN"


def grid_clean_refused(snapshot: dict[Key, Status], keys: tuple[Key, ...]) -> bool:
    """CLEAN on a grid holding a DIRTY or FLAGGED image is refused, unless the whole grid is
    DIRTY (reversing that grid's own verdict)."""
    statuses = {snapshot[key] for key in keys}
    return not statuses <= GRID_ELIGIBLE and statuses != {"DIRTY"}
