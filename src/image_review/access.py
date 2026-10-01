"""Access policy for work directories (stdlib only; importable on the server side).

A work directory holds PHI, so it is `private` (owner only) unless the creator
opts into `group`: readable and writable by the directory's Unix group, for
teams sharing a study group. Nothing is ever world-accessible. The policy is
only mode bits; the tool never calls chgrp and never touches ACLs. It is not
stored anywhere: later writers recover it from the work directory's own mode
with `access_of`.
"""

import os
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Access = Literal["private", "group"]

MANIFEST_NAME = "manifest.tsv"


@dataclass(frozen=True)
class Modes:
    dir_mode: int
    file_mode: int
    umask: int  # applied around creation so no file is ever created more open than file_mode


def modes(access: Access) -> Modes:
    match access:
        case "private":
            return Modes(dir_mode=0o700, file_mode=0o600, umask=0o077)
        case "group":
            return Modes(dir_mode=0o2770, file_mode=0o660, umask=0o007)  # setgid: new entries inherit the group


def access_of(st_mode: int) -> Access:
    """The policy a work directory was created with: `group` if its group bits grant read, write and execute."""
    return "group" if st_mode & 0o070 == 0o070 else "private"


def world_accessible(st_mode: int) -> bool:
    return st_mode & 0o007 != 0


def policy_of_dir(work_dir: Path) -> Modes:
    return modes(access_of(os.stat(work_dir).st_mode))


def world_access_warning(work_dir: Path) -> str | None:
    """A warning for the first of the work dir and its manifest that others can access, else None."""
    for path in (work_dir, work_dir / MANIFEST_NAME):
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        if world_accessible(mode):
            return f"warning: {path} is accessible to all users (mode {mode:04o}); run `chmod -R o-rwx {shlex.quote(str(work_dir))}`"
    return None
