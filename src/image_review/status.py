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
