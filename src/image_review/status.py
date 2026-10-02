from typing import Literal

# FLAGGED: marked DIRTY in another pass and not yet re-reviewed in this one. Derived, never stored.
Status = Literal["CLEAN", "DIRTY", "UNREVIEWED", "FLAGGED"]
Verdict = Literal["CLEAN", "DIRTY"]

# Statuses that still need a verdict in the current pass.
TODO_STATUSES: frozenset[Status] = frozenset({"UNREVIEWED", "FLAGGED"})

# How a verdict was given: on one image, or on every image of a grid at once. A client may only give these.
MarkMode = Literal["single", "grid"]
