import contextlib
import dataclasses
import datetime
import functools
import getpass
import logging
import math
import os
import shlex
import socket
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, get_args

import click

from .access import Access, access_of, modes, world_access_warning
from .atomic import write_new_file
from .connection import RemoteTarget, Token, parse_reviewer, parse_token
from .export import ExportRow, ExportStatus, format_allowlist, format_report, split_allowlist
from .lock import LOCK_NAME, WorkDirLocked, live_writer
from .signals import HANGUP_SIGNALS, TERMINATION_SIGNALS, interrupt_on
from .status import MarkMode, Rotation, Status
from .store import LocalStore, ReviewStore, SkippedCounts, StatusFilter, batch_summary, skipped_counts, summary

if TYPE_CHECKING:
    from .remote import RemoteError, RemoteStore
    from .server import ReviewServer
    from .tunnel import TunnelError

DEFAULT_WORK_DIR = "./review_work"

PACKAGE_LOGGER = "image_review"  # every module logs under it; the CLI configures only this tree
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

log = logging.getLogger(f"{PACKAGE_LOGGER}.cli")  # not __name__: that is "__main__" under `python -m`

# The top-level modules each optional extra (pyproject.toml) provides and the commands import; the core
# dependencies (click, cryptography, rectpack) are always there. The codecs extra is loaded by pydicom only when needed.
EXTRA_MODULES: dict[str, frozenset[str]] = {
    "preprocess": frozenset({"matplotlib", "numpy", "pydicom", "PIL", "skimage", "scipy", "tqdm"}),
    "viewer": frozenset({"pygame", "PIL"}),
}


@contextlib.contextmanager
def requires_extra(extra: str) -> Iterator[None]:
    """Turn a missing module of `extra`, imported in the block, into a ClickException naming the install command.

    Only a ModuleNotFoundError for one of the extra's own top-level modules is translated; any other import failure
    (one of our modules, a broken install, a missing transitive dependency) propagates unchanged.
    """
    try:
        yield
    except ModuleNotFoundError as exc:
        if (exc.name or "").partition(".")[0] not in EXTRA_MODULES[extra]:
            raise
        raise click.ClickException(
            f"this command needs the {extra} extra: pip install 'image-review[{extra}]'"
        ) from exc


class LogFormatter(logging.Formatter):
    """LOG_FORMAT with asctime as strict ISO 8601 local time, e.g. 2026-10-01T14:03:07+02:00."""

    def __init__(self) -> None:
        super().__init__(LOG_FORMAT)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")


@contextlib.contextmanager
def log_to(handler: logging.Handler, level: int) -> Iterator[None]:
    """Send this package's log records at `level` and above to `handler` only, restoring the previous setup on exit."""
    package = logging.getLogger(PACKAGE_LOGGER)
    handler.setFormatter(LogFormatter())
    saved = package.handlers, package.level, package.propagate
    package.handlers, package.propagate = [handler], False
    package.setLevel(level)
    try:
        yield
    finally:
        package.handlers, package.propagate = saved[0], saved[2]
        package.setLevel(saved[1])


def work_dir_option(f):
    return click.option(
        "--work-dir",
        type=click.Path(exists=True),
        default=None,
        help=f"Work directory containing preprocessed data [default: {DEFAULT_WORK_DIR}].",
    )(f)


def remote_option(f):
    return click.option(
        "--remote",
        envvar="IMAGE_REVIEW_REMOTE",
        default=None,
        help="Review a server started with `image-review serve` (ir:// connection string; also read from $IMAGE_REVIEW_REMOTE).",
    )(f)


def via_option(f):
    return click.option(
        "--via",
        envvar="IMAGE_REVIEW_VIA",
        default=None,
        help="With --remote: reach the server through an SSH tunnel via this login node, e.g. user@login.cluster (also read from $IMAGE_REVIEW_VIA).",
    )(f)


def warn_if_world_accessible(work_dir: Path) -> None:
    warning = world_access_warning(work_dir)
    if warning is not None:
        log.warning(warning)


def open_local_store(path: Path, read_only: bool = False) -> LocalStore:
    """Open a LocalStore (writable ones take the work dir lock), translating failures into ClickExceptions."""
    try:
        store = LocalStore(path, read_only=read_only)
    except WorkDirLocked as e:
        raise click.ClickException(str(e)) from e
    except FileNotFoundError:
        raise click.ClickException("No preprocessed data found. Run `image-review preprocess` first.")
    except OSError as e:
        if e.filename is not None and Path(e.filename).name.startswith(LOCK_NAME):
            raise click.ClickException(f"Cannot write to work directory {path}: {e}") from e
        raise click.ClickException(f"Cannot read work directory: {e}") from e
    except ValueError as e:
        raise click.ClickException(f"Cannot read work directory: {e}") from e
    except RuntimeError as e:  # review.tsv changed under the lock while being migrated
        raise click.ClickException(f"Cannot update work directory: {e}") from e
    log.debug("opened work directory %s (%s)", path, "read-only" if read_only else "writable, locked")
    warn_if_world_accessible(path)
    return store


def _existing_work_dir(work_dir: str | None) -> Path:
    """The work directory, defaulting to DEFAULT_WORK_DIR; a BadParameter if it does not exist."""
    raw = work_dir if work_dir is not None else DEFAULT_WORK_DIR
    path = Path(raw)
    if not path.exists():
        raise click.BadParameter(f"Path '{raw}' does not exist.", param_hint="'--work-dir'")
    return path


@contextlib.contextmanager
def open_store(
    work_dir: str | None, remote: str | None, via: str | None = None, read_only: bool = False
) -> Iterator[ReviewStore]:
    """Open the local or remote store, translating startup failures into ClickExceptions.

    `read_only` applies to a local store: it takes no lock and refuses marks.
    """
    if remote is None:
        if (
            via is not None
            and click.get_current_context().get_parameter_source("via") is not click.core.ParameterSource.ENVIRONMENT
        ):
            raise click.UsageError("--via requires --remote.")
        with open_local_store(_existing_work_dir(work_dir), read_only=read_only) as local:
            yield local
        return

    if work_dir is not None:
        if click.get_current_context().get_parameter_source("remote") is click.core.ParameterSource.ENVIRONMENT:
            raise click.UsageError("IMAGE_REVIEW_REMOTE is set; unset it to use --work-dir.")
        raise click.UsageError("--remote and --work-dir are mutually exclusive.")
    from .remote import RemoteError
    from .tunnel import TunnelError, parse_via

    try:
        target = RemoteTarget.parse(remote)
    except ValueError as e:
        raise click.ClickException(f"Invalid --remote connection string: {e}")
    if via is not None:
        try:
            via = parse_via(via)
        except ValueError as e:
            raise click.ClickException(f"Invalid --via: {e}")
    where = f"{target.host}:{target.port}" + (f" (via {via})" if via else "")
    log.debug("connecting to server at %s", where)
    try:
        with _remote_store(target, via) as store:
            yield store
    except (TunnelError, RemoteError) as e:
        raise _remote_failure(e, where, via)


@contextlib.contextmanager
def _remote_store(target: RemoteTarget, via: str | None) -> Iterator["RemoteStore"]:
    """Open a RemoteStore (through an ssh tunnel when `via` is given) and check its API version.

    With a tunnel, the store gets a copy of `target` aimed at the forwarded local port; its pin and token are kept.
    """
    from .remote import RemoteStore
    from .tunnel import ssh_tunnel

    with contextlib.ExitStack() as stack:
        if via is not None:
            port = stack.enter_context(ssh_tunnel(via, target.host, target.port))
            target = dataclasses.replace(target, host="127.0.0.1", port=port)
        store = stack.enter_context(RemoteStore(target))  # closed before the tunnel
        store.check_api()
        yield store


def _remote_failure(e: "TunnelError | RemoteError", where: str, via: str | None) -> click.ClickException:
    """The user-facing error for a tunnel or remote-store failure at `where`."""
    from .remote import ApiMismatch, FingerprintMismatch
    from .tunnel import TunnelError

    if isinstance(e, (TunnelError, ApiMismatch)):
        return click.ClickException(str(e))
    if isinstance(e, FingerprintMismatch):
        return click.ClickException(
            f"The certificate presented by {where} does NOT match the connection string. "
            "The connection was aborted before any credentials were sent. "
            "Do not continue unless you know why the server's identity changed."
            + (
                " With --via, this can also happen if another local process grabbed the forwarded port; retry."
                if via
                else ""
            )
        )
    if e.status == 401:
        return click.ClickException(
            f"Server at {where} rejected the access token (connection string from a different or restarted server?)"
        )
    if e.status is not None:
        return click.ClickException(f"Server at {where} returned HTTP {e.status}")
    transport_failure = isinstance(e.__cause__, OSError)
    hint = (
        " (the login node may not be able to reach the server; see the ssh output above)"
        if via and transport_failure
        else ""
    )
    return click.ClickException(f"Cannot reach server at {where}: {e}{hint}")


class FullHelpGroup(click.Group):
    """A click Group that shows all subcommand help in the top-level --help."""

    def format_help(self, ctx, formatter):
        # Render the group's own help (docstring + options)
        super().format_help(ctx, formatter)

        # Append each subcommand's full help
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None:
                continue

            formatter.write("\n")
            with formatter.section(f"Command: {name}"):
                sub_ctx = click.Context(cmd, info_name=name, parent=ctx)
                cmd.format_help(sub_ctx, formatter)


@click.group(cls=FullHelpGroup)
@click.option("-v", "--verbose", is_flag=True, help="Also log debug messages.")
@click.option("-q", "--quiet", is_flag=True, help="Log only warnings and errors.")
@click.version_option()
@click.pass_context
def cli(ctx: click.Context, verbose: bool, quiet: bool):
    """Review DICOM / medical images for burned-in PHI.

    Workflow:

    \b
      1. preprocess  — convert source DICOMs/images to JPG batches
      2. review      — interactively classify images as CLEAN or DIRTY
      3. status      — check review progress and counts
      4. export      — write the result: one row per source image (on the machine holding the work dir)

    Diagnostics are logged to stderr as `time LEVEL module: message`.
    """
    if verbose and quiet:
        raise click.UsageError("-v/--verbose and -q/--quiet are mutually exclusive.")
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    ctx.with_resource(log_to(logging.StreamHandler(sys.stderr), level))


def _shared_with(work_dir: Path) -> str:
    import grp

    gid = work_dir.stat().st_gid
    try:
        name = grp.getgrgid(gid).gr_name
    except KeyError:
        return f"Shared with Unix group {gid}"
    return f"Shared with Unix group '{name}' (gid {gid})"


def _known_colormap(ctx: click.Context, param: click.Parameter, value: str) -> str:
    with requires_extra("preprocess"):  # the first preprocess import: callbacks run before the command
        import matplotlib

    if value not in matplotlib.colormaps:
        raise click.BadParameter(f"unknown colormap {value!r}; see matplotlib.colormaps for valid names")
    return value


CGROUP_ROOT = Path("/sys/fs/cgroup")  # the cgroup v2 mount
PROC_SELF_CGROUP = Path("/proc/self/cgroup")  # its "0::<path>" line names this process's cgroup v2


def _cpu_max(cgroup: Path) -> int | None:
    """A cgroup's CPU quota (`cpu.max`: "<quota> <period>"), rounded up; None for "max" or a missing or odd file."""
    try:
        quota, period = (cgroup / "cpu.max").read_text().split()
        return max(1, math.ceil(int(quota) / int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        return None


def _cgroup_cpus() -> int | None:
    """The smallest CPU quota of this process's cgroup v2 and its ancestors up to the mount root, or None.

    On a host that is e.g. a login node's per-user `CPUQuota=` on `user-UID.slice`; in a container the
    cgroup is the namespace root (`0::/`), whose `cpu.max` is the container's quota.
    """
    try:
        lines = PROC_SELF_CGROUP.read_text().splitlines()
    except OSError:  # not Linux
        lines = []
    path = next((line.removeprefix("0::") for line in lines if line.startswith("0::")), "/")
    parts = PurePosixPath(path).parts[1:]
    if ".." in parts:  # outside our cgroup namespace: only its root is visible
        parts = ()
    caps = [_cpu_max(CGROUP_ROOT.joinpath(*parts[:depth])) for depth in range(len(parts), -1, -1)]
    return min((cap for cap in caps if cap is not None), default=None)


def default_jobs() -> int:
    """The default for `preprocess --jobs`, resolved when the command runs.

    The CPUs this process may run on (its affinity, else the CPU count, else 1), capped by
    $SLURM_CPUS_PER_TASK when that is a whole number >= 1, else by the smallest cgroup v2 CPU quota
    (`cpu.max`) of this process's cgroup and its ancestors.
    """
    affinity = getattr(os, "sched_getaffinity", None)  # not on macOS or Windows
    usable = len(affinity(0)) if affinity is not None else (os.cpu_count() or 1)
    slurm = os.environ.get("SLURM_CPUS_PER_TASK", "").strip()
    if slurm.isdecimal() and int(slurm) >= 1:  # isdecimal: "²" is a digit int() rejects
        return min(int(slurm), usable)
    quota = _cgroup_cpus()
    return usable if quota is None else min(quota, usable)


@cli.command()
@click.argument("sources", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--batch-size", type=click.IntRange(min=1), default=300, show_default=True, help="Images per batch.")
@click.option(
    "--work-dir",
    "--output-dir",
    type=click.Path(),
    default=DEFAULT_WORK_DIR,
    show_default=True,
    help="Work directory for output.",
)
@click.option(
    "--colormap",
    type=str,
    callback=_known_colormap,
    default="inferno",
    show_default=True,
    help="Matplotlib colormap for rendering.",
)
@click.option(
    "--access",
    type=click.Choice(get_args(Access)),
    envvar="IMAGE_REVIEW_ACCESS",
    default="private",
    show_default=True,
    help="Who can use the work directory: private = owner only (dirs 0700, files 0600); group = readable/writable by the work dir's Unix group (dirs 2770, files 0660). Never world-readable. Also read from $IMAGE_REVIEW_ACCESS.",
)
@click.option(
    "--allow-skipped",
    is_flag=True,
    default=False,
    help="Exit 0 even if some inputs failed to preprocess (they are listed in skipped.tsv).",
)
@click.option(
    "--jobs",
    type=click.IntRange(min=1),
    default=default_jobs,
    show_default="$SLURM_CPUS_PER_TASK, else the usable CPUs capped by the cgroup v2 CPU quotas",
    help="Worker processes rendering inputs in parallel; 1 renders in this process. The output is the same for any value.",
)
def preprocess(sources, batch_size, work_dir, colormap, access, allow_skipped, jobs):
    """Normalize DICOM and image files to JPGs and organize them into batches.

    SOURCES are one or more ZIP files, directories, or image files to process.
    Inputs are recognized by content, not extension. Every input is listed in
    either manifest.tsv or skipped.tsv (as failed, or ignored when it is not an
    image). Exits 1 if any input failed, unless --allow-skipped is given.
    """
    with requires_extra("preprocess"):
        from tqdm.contrib.logging import logging_redirect_tqdm

        from .preprocess import WorkDirExists, WorkerCrashed, run_preprocess

    source_paths = [Path(s).resolve() for s in sources]

    try:
        with (
            interrupt_on(*TERMINATION_SIGNALS),  # SIGTERM/SIGHUP unwind like Ctrl-C: workers stop, staging goes
            logging_redirect_tqdm(loggers=[logging.getLogger(PACKAGE_LOGGER)]),  # log lines print above the bars
        ):
            result = run_preprocess(
                source_paths, Path(work_dir), batch_size=batch_size, colormap=colormap, access=access, jobs=jobs
            )
    except (WorkDirExists, WorkerCrashed) as exc:
        raise click.ClickException(str(exc)) from exc
    skipped = skipped_counts(result.skipped)
    failed = skipped.failed
    click.echo(
        f"Found {result.found} inputs: wrote {result.written} images in {result.batches} batches; "
        f"{len(result.skipped)} skipped ({failed} failed, {skipped.ignored} ignored; see {result.skipped_path})"
    )
    if access == "group":
        click.echo(_shared_with(Path(work_dir)))
    if failed and not allow_skipped:
        raise click.ClickException(
            f"{failed} input(s) failed to preprocess and will not be reviewed; see {result.skipped_path}. "
            "Re-run with --allow-skipped to accept this."
        )


def _reviewer(ctx: click.Context, param: click.Parameter, value: str | None) -> str:
    """--reviewer, else $IMAGE_REVIEW_REVIEWER, else the login name, checked like the server checks it."""
    if value is None:
        try:
            value = getpass.getuser()
        except (
            KeyError,
            OSError,
        ):  # no USER/LOGNAME and no passwd entry (some containers): KeyError <3.13, OSError >=3.13
            raise click.BadParameter("cannot determine your user name; pass --reviewer NAME") from None
    try:
        return parse_reviewer(value)
    except ValueError as e:
        raise click.BadParameter(str(e)) from None


def unknown_batch_message(batch: str, known: set[str]) -> str | None:
    """Why `batch` cannot be reviewed, or None when it names a batch in the manifest."""
    if batch in known:
        return None
    names = sorted(known)
    listed = ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
    return f"Unknown batch {batch!r}; known batches: {listed or '(none)'}."


@cli.command()
@click.option(
    "--mode", type=click.Choice(get_args(MarkMode)), default="single", show_default=True, help="Review display mode."
)
@click.option(
    "--pass",
    "pass_number",
    type=click.IntRange(min=1),
    default=None,
    help="Pass number, 1 or more (auto-detected if omitted).",
)
@click.option(
    "--batch",
    type=str,
    default=None,
    help="Restrict to a specific batch; [b] at the end of the list stays in it [default: the first batch with images matching the filter, and [b] moves on to the next].",
)
@click.option(
    "--filter",
    "status_filter",
    type=click.Choice(get_args(StatusFilter)),
    default="unreviewed",
    show_default=True,
    help="Which images to show: unreviewed = images still to do (UNREVIEWED and FLAGGED).",
)
@click.option(
    "--rotate",
    type=click.Choice(get_args(Rotation)),
    default="auto",
    show_default=True,
    help="Rotate images 90 degrees in grids: auto = only when that saves a grid.",
)
@click.option(
    "--reviewer",
    envvar="IMAGE_REVIEW_REVIEWER",
    default=None,
    callback=_reviewer,
    help="Name recorded with each verdict in review.tsv, 1-64 printable characters, not all spaces [default: your login name]. "
    "An unauthenticated claim: it is recorded as given, not verified. Also read from $IMAGE_REVIEW_REVIEWER.",
)
@work_dir_option
@remote_option
@via_option
def review(mode, pass_number, batch, status_filter, rotate, reviewer, work_dir, remote, via):
    """Open an interactive review session for classifying images.

    \b
    Keyboard controls:
      c / d       — mark image CLEAN / DIRTY
      Arrow keys  — navigate between images
      Space       — toggle autoplay
      q           — quit
    """
    with requires_extra("viewer"):
        import pygame as pg

        from .controller import ReviewSession

    with interrupt_on(*HANGUP_SIGNALS), open_store(work_dir, remote, via) as store:  # terminal or ssh session dropped
        if batch is not None and (problem := unknown_batch_message(batch, {row.batch for row in store.manifest()})):
            raise click.BadParameter(problem, param_hint="'--batch'")
        # after the store: SDL must not steal terminal focus during ssh password/MFA prompts
        pg.init()
        try:
            session = ReviewSession(
                store=store,
                reviewer=reviewer,
                mode=mode,
                pass_number=pass_number,
                batch=batch,
                status_filter=status_filter,
                rotation=rotate,
            )
            session.run()
        finally:
            pg.quit()


def status_report(
    counts: Counter[Status], batch_counts: dict[str, Counter[Status]], current: int, skipped: SkippedCounts
) -> str:
    """The text `status` prints: overall counts, a per-batch table when there are several batches, the pass, skips."""
    lines = ["", f"Overall: {counts.total()} images (pass {current})"]
    lines += [f"  {status + ':':<12}{counts[status]:>6}" for status in get_args(Status)]
    if len(batch_counts) > 1:
        lines += ["", f"{'Batch':<15} {'Total':>6} {'Clean':>6} {'Dirty':>6} {'Unrev':>6} {'Flag':>6}", "-" * 52]
        for batch_id in sorted(batch_counts):
            bc = batch_counts[batch_id]
            lines.append(
                f"{batch_id:<15} {bc.total():>6} {bc['CLEAN']:>6} {bc['DIRTY']:>6} {bc['UNREVIEWED']:>6} {bc['FLAGGED']:>6}"
            )
    lines += ["", f"Current pass: {current}"]
    if skipped.any:
        lines.append(
            f"Skipped during preprocess: {skipped.failed} failed, {skipped.ignored} ignored (see skipped.tsv in the work dir)"
        )
    return "\n".join(lines) + "\n"


@cli.command()
@work_dir_option
@remote_option
@via_option
@click.option(
    "--check",
    is_flag=True,
    default=False,
    help="Exit 1 if any image is UNREVIEWED (has no verdict) or any input failed to preprocess; else 0. "
    "FLAGGED images have a DIRTY verdict, so they count as decided.",
)
def status(work_dir, remote, via, check):
    """Report overall and per-batch review progress (CLEAN / DIRTY / UNREVIEWED / FLAGGED counts).

    With --check, the exit status also says whether the review is finished: every image has a verdict.
    """
    with open_store(work_dir, remote, via, read_only=True) as store:
        manifest = store.manifest()
        current = store.current_pass()
        statuses = store.statuses(current)
        try:
            skipped = store.skipped()
        except ValueError as e:
            raise click.ClickException(str(e)) from e

    counts = summary(manifest, statuses)
    print(status_report(counts, batch_summary(manifest, statuses), current, skipped), end="")

    # Finished when every image has a verdict: FLAGGED is a DIRTY verdict from an earlier pass; re-review is optional.
    if check and (counts["UNREVIEWED"] or skipped.failed):
        sys.exit(1)


def _report_counts(rows: list[ExportRow]) -> str:
    """Each export status's count among the report's rows, e.g. `4 DIRTY, ..., 0 CLEAN not allowlisted`: every
    status, CLEAN last."""
    statuses = [r.status for r in rows]
    order = sorted(get_args(ExportStatus), key=lambda s: s == "CLEAN")  # stable: the Literal's order otherwise
    return ", ".join(f"{statuses.count(s)} {'CLEAN not allowlisted' if s == 'CLEAN' else s}" for s in order)


@cli.command()
@work_dir_option
@click.option(
    "--output",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write the allowlist to this new file, with the work directory's file mode and group; an existing file is never overwritten [default: stdout].",
)
@click.option(
    "--report",
    type=click.Path(dir_okay=False),
    default=None,
    help="Also write every file not allowlisted, with its status and reason, to this new file, with the same mode and group as --output; written first, and never overwriting a file. For audit and follow-up only: never use it to choose what to release.",
)
@click.option(
    "--allow-live",
    is_flag=True,
    default=False,
    help="Export even while a writer (review or serve) has the work directory open; verdicts recorded later are missed.",
)
@click.option("--remote", default=None, hidden=True)  # declared only to refuse it; $IMAGE_REVIEW_REMOTE is not read
def export(work_dir, output, report, allow_live, remote):
    """Write the allowlist of releasable files as TSV: source_sha256, image_id, pass_number, timestamp, reviewer.

    Default deny: release a file only if both its path (image_id; a ZIP entry is `<zip>::<entry>`) and its SHA-256
    match a row; anything not listed is denied. A file is listed only if it was reviewed CLEAN (a DICOM's icon too),
    the manifest has its hash, and no file that is not CLEAN has the same recorded hash. --report writes the rest as TSV
    (image_id, status, pass_number, timestamp, reviewer, reason, source_sha256): DIRTY (a FLAGGED image is DIRTY),
    UNREVIEWED, NOT_REVIEWED when preprocess failed on it, IGNORED when preprocess did not take it for an image, and
    CLEAN ones not allowlisted, with why. image_ids are source paths and may hold PHI, so export runs only where the
    work directory is, never with --remote; $IMAGE_REVIEW_REMOTE is ignored.
    """
    if remote is not None:
        raise click.UsageError(
            "export does not work with --remote: image_ids stay on the server. Run export on the machine (cluster) "
            "holding the work directory, with --work-dir."
        )
    if report is not None and output is not None and os.path.realpath(report) == os.path.realpath(output):
        raise click.UsageError("--report and --output name the same file.")
    path = _existing_work_dir(work_dir)
    with interrupt_on(*TERMINATION_SIGNALS):  # a SIGTERM/SIGHUP unwinds like Ctrl-C, removing a half-written file
        writer = live_writer(path)
        _refuse_live(writer, allow_live)
        with open_local_store(path, read_only=True) as store:
            # A torn review.tsv, a malformed skipped.tsv, or an unsafe field in either text: both are built even
            # without --report, so one unsafe field anywhere refuses the whole export.
            try:
                allowed, denied = split_allowlist(store.export_rows())
                allowlist_text, report_text = format_allowlist(allowed), format_report(denied)
            except ValueError as e:
                raise click.ClickException(str(e)) from e
        if writer is None:  # one that opened the work directory while it was read may have changed verdicts
            _refuse_live(live_writer(path), allow_live)
        st = path.stat()
        access = access_of(st.st_mode)
        group = st.st_gid if access == "group" else None  # the study's group, wherever the file is created

        def write(name: str, text: str) -> None:
            try:
                write_new_file(Path(name), modes(access).file_mode, group, text)
            except FileExistsError:
                raise click.ClickException(f"{name} already exists; not overwriting it.") from None
            except OSError as e:
                raise click.ClickException(f"Cannot write {name}: {e}") from e

        # Refuse an existing target before writing either, so no new report sits beside a stale allowlist;
        # write_new_file's O_EXCL still guards against one created from now on.
        for name in (report, output):
            if name is not None and os.path.lexists(name):
                raise click.ClickException(f"{name} already exists; not overwriting it.")
        if report is not None:  # first: if the allowlist then fails, only the report exists, and it releases nothing
            write(report, report_text)
        if output is None:
            click.echo(allowlist_text.encode("utf-8"), nl=False)
        else:
            write(output, allowlist_text)
    log.info("%d files allowlisted; %d in the report (%s)", len(allowed), len(denied), _report_counts(denied))


@cli.command()
@click.option(
    "--work-dir",
    type=click.Path(exists=True),
    default=DEFAULT_WORK_DIR,
    show_default=True,
    help="Work directory containing preprocessed data.",
)
@click.option(
    "--bind", default=None, help="Hostname/IPv4 address to bind and advertise [default: this machine's FQDN]."
)
@click.option("--port", type=click.IntRange(0, 65535), default=0, help="Port to listen on (0 picks a free port).")
@click.option(
    "--socket",
    "socket_mode",
    is_flag=True,
    help="Experimental: serve plain HTTP on a Unix socket for browser review over SSH, instead of HTTPS over TCP.",
)
@click.option(
    "--socket-path",
    envvar="IMAGE_REVIEW_SOCKET_PATH",
    default=None,
    help="Experimental: the socket path; implies --socket [default: ~/.image-review/serve-HOST-PID.sock]. Also read from $IMAGE_REVIEW_SOCKET_PATH, but only with --socket (alone, the variable does not switch serve into socket mode).",
)
@click.option(
    "--via",
    envvar="IMAGE_REVIEW_VIA",
    default=None,
    help="With --socket: the login node (e.g. user@login.cluster) to put in the printed ssh command (also read from $IMAGE_REVIEW_VIA).",
)
@click.option(
    "--direct",
    envvar="IMAGE_REVIEW_DIRECT",
    is_flag=True,
    help="With --socket: your laptop reaches compute nodes without a jump host; the printed ssh command omits -J (also read from $IMAGE_REVIEW_DIRECT).",
)
@click.option(
    "--ssh-host",
    default=None,
    help="With --socket: the node name to put in the printed ssh command, if its own FQDN does not work from your laptop [default: this machine's FQDN].",
)
def serve(work_dir, bind, port, socket_mode, socket_path, via, direct, ssh_host):
    """Serve a work directory so a remote client can review it.

    By default this is HTTPS over TCP for the `review --remote` client. Images
    never leave this machine except to a client holding the connection
    string. The string contains an access token: treat it like a password.
    When stdout is not a terminal (e.g. sbatch), it is written to a private
    file under ~/.image-review/ instead of being printed.

    --socket (experimental) serves plain HTTP on a Unix socket instead, for a
    browser on your laptop reached through `ssh -L`; the URL, which contains
    the token, is handled the same way. With --socket, a token in
    $IMAGE_REVIEW_TOKEN (22-256 characters from A-Za-z0-9_-) is used instead of
    a fresh one, so the URL stays the same across restarts: generate it inside
    the job, e.g. export IMAGE_REVIEW_TOKEN=$(openssl rand -hex 16). It is
    ignored without --socket.
    """
    from .tunnel import parse_ssh_host, parse_via

    ctx = click.get_current_context()
    path_from_env = ctx.get_parameter_source("socket_path") is click.core.ParameterSource.ENVIRONMENT
    if path_from_env and not socket_mode:
        socket_path = None  # $IMAGE_REVIEW_SOCKET_PATH only means something with --socket
    if socket_path == "":  # e.g. --socket-path "$UNSET"; Path("") would quietly mean the current directory
        raise click.UsageError("--socket-path must not be empty.")
    socket_mode = socket_mode or socket_path is not None
    if socket_mode:
        if bind is not None or ctx.get_parameter_source("port") is not click.core.ParameterSource.DEFAULT:
            raise click.UsageError("--socket and --socket-path cannot be combined with --bind or --port.")
    else:
        if direct and ctx.get_parameter_source("direct") is click.core.ParameterSource.COMMANDLINE:
            raise click.UsageError("--direct requires --socket.")
        if ssh_host is not None:
            raise click.UsageError("--ssh-host requires --socket.")
        direct = False  # from $IMAGE_REVIEW_DIRECT, which only means something in socket mode
        if via is not None:
            if ctx.get_parameter_source("via") is click.core.ParameterSource.COMMANDLINE:
                raise click.UsageError("--via requires --socket.")
            via = None  # from $IMAGE_REVIEW_VIA, which is meant for `review`
    if direct and via is not None:
        if ctx.get_parameter_source("via") is click.core.ParameterSource.COMMANDLINE:
            raise click.UsageError("--direct and --via are mutually exclusive.")
        via = None  # from $IMAGE_REVIEW_VIA, which is meant for `review`
    if via is not None:
        try:
            via = parse_via(via)
        except ValueError as e:
            raise click.ClickException(f"Invalid --via: {e}")
    if ssh_host is not None:
        try:
            ssh_host = parse_ssh_host(ssh_host)
        except ValueError as e:
            raise click.ClickException(f"Invalid --ssh-host: {e}")
    token: Token | None = None
    if socket_mode and (raw_token := os.environ.get("IMAGE_REVIEW_TOKEN")):  # empty counts as unset
        try:
            token = parse_token(raw_token)
        except ValueError as e:  # the message never holds the value
            raise click.ClickException(f"Invalid $IMAGE_REVIEW_TOKEN: {e}")

    try:
        # Slurm stops jobs with SIGTERM (scancel, time limit): shut down like Ctrl-C so cleanup runs.
        # Installed before the lock is taken, so every exit path releases it.
        with interrupt_on(*TERMINATION_SIGNALS), contextlib.ExitStack() as stack:
            store = stack.enter_context(open_local_store(Path(work_dir)))  # holds the work dir lock
            if socket_mode:
                server, announce = _serve_socket(store, socket_path, token, via, direct, ssh_host, stack)
            else:
                server, announce = _serve_tls(store, bind, port, stack)
            announce(stack)
            server.serve_forever()
    except KeyboardInterrupt:
        pass


def _close_with(stack: contextlib.ExitStack, server: "ReviewServer", store: LocalStore) -> None:
    """Register the server's shutdown on `stack` as soon as it exists.

    Exit order (LIFO): the announced file, the socket (which, for a Unix socket, also removes the socket file),
    then the store; a handler thread may still be marking.
    """
    stack.callback(_close_store_after_marks, server, store)
    stack.callback(server.server_close)


def _serve_tls(
    store: LocalStore, bind: str | None, port: int, stack: contextlib.ExitStack
) -> tuple["ReviewServer", Callable[[contextlib.ExitStack], None]]:
    """Bind the HTTPS server; returns it and the function that announces how to connect."""
    from .server import make_server

    host = socket.getfqdn() if bind is None else bind
    try:
        server, target = make_server(store, host, port)
    except ValueError as e:  # wildcard bind or unadvertisable host; make_server closed its socket
        raise click.ClickException(str(e))
    except OSError as e:
        raise click.ClickException(f"Cannot listen on {host}:{port} (IPv4 hostnames/addresses only): {e}")
    _close_with(stack, server, store)
    return server, functools.partial(_announce, target)


def _serve_socket(
    store: LocalStore,
    socket_path: str | None,
    token: Token | None,
    via: str | None,
    direct: bool,
    ssh_host: str | None,
    stack: contextlib.ExitStack,
) -> tuple["ReviewServer", Callable[[contextlib.ExitStack], None]]:
    """Bind the Unix-socket server; returns it and the function that announces how to connect.

    `token` is the operator's parsed one, or None for a fresh one. Failures become ClickExceptions.
    The announced path is the absolute one that was bound.
    """
    from .server import default_socket_path, make_unix_server

    try:
        requested = Path(socket_path) if socket_path is not None else default_socket_path()
    except OSError as e:  # ~/.image-review is not ours, or cannot be created
        raise click.ClickException(f"Cannot use ~/.image-review: {e}")
    try:
        server, bound_token = make_unix_server(store, requested, token)
    except ValueError as e:  # bad or busy path; nothing was left behind
        raise click.ClickException(str(e))
    except OSError as e:
        raise click.ClickException(f"Cannot listen on socket {requested}: {e}")
    _close_with(stack, server, store)
    announce = functools.partial(
        _announce_socket, Path(str(server.server_address)), bound_token, token is not None, via, direct, ssh_host
    )
    return server, announce


def _announce(target: RemoteTarget, stack: contextlib.ExitStack) -> None:
    """Tell the operator how to connect: print the connection string on a terminal, else write it to a private file.

    The file's removal is registered on `stack`.
    """
    from .server import write_connection_file

    uri = target.to_uri()
    if sys.stdout.isatty():
        print("Serving review data. The connection string grants access; treat it like a password.\n")
        print(uri)
        remote_arg = f"'{uri}'"
    else:
        try:
            connection_file = write_connection_file(target)
        except OSError as e:
            raise click.ClickException(f"Cannot write connection file: {e}")
        stack.callback(connection_file.unlink, missing_ok=True)
        print("Serving review data. Stdout is not a terminal, so the connection string (an access token;")
        print(f"treat it like a password) was written to {connection_file} (mode 0600) on this node.")
        print("Home directories are usually shared with the login node, so on your laptop use:")
        remote_arg = f'"$(ssh <user>@<login-node> cat {connection_file})"'
    print("\nOn your laptop, directly:")
    print(f"  image-review review --remote {remote_arg}")
    print("or through an SSH tunnel via the login node:")
    print(f"  image-review review --remote {remote_arg} --via <user>@<login-node>")
    print("\nPress Ctrl-C to stop.", flush=True)


BROWSER_PORT = 8080  # the laptop-side port in the printed ssh command and URL; changeable if busy


def browser_url(token: str, port: int = BROWSER_PORT) -> str:
    """The URL to open on the laptop; the token rides in the fragment, which the browser never sends.

    127.0.0.1, not localhost: the forward below binds only that address, so nothing else can answer the URL.
    """
    return f"http://127.0.0.1:{port}/#{token}"


def ssh_forward_command(socket_path: Path, node: str, user: str, jump: str | None, port: int = BROWSER_PORT) -> str:
    """The `ssh -L` command that forwards 127.0.0.1:`port` on the laptop to the server's socket on `node`.

    The forward names 127.0.0.1 so ssh does not also bind ::1 (where another process could already listen);
    ControlPath=none keeps a ControlPersist master from keeping the forward alive after Ctrl-C.
    `jump` is the -J host; None means the laptop reaches `node` directly.
    """
    jump_option = "" if jump is None else f"-J {shlex.quote(jump)} "
    forward = shlex.quote(f"127.0.0.1:{port}:{socket_path}")
    target = shlex.quote(f"{user}@{node}")
    return f"ssh -N -o ExitOnForwardFailure=yes -o ControlPath=none {jump_option}-L {forward} {target}"


def _announce_socket(
    socket_path: Path,
    token: str,
    from_env: bool,
    via: str | None,
    direct: bool,
    ssh_host: str | None,
    stack: contextlib.ExitStack,
) -> None:
    """Tell the operator how to reach the socket: the ssh command (no secret), and the URL, which holds the token.

    The URL is printed on a terminal, else written to a private file whose removal is registered on `stack`.
    `from_env` says the token came from $IMAGE_REVIEW_TOKEN.
    """
    from .server import safe_name, short_host, write_private_file

    try:
        user = getpass.getuser()
    except (OSError, KeyError):  # no passwd entry (getuser raises OSError, or KeyError from pwd)
        user = "<user>"
    url = browser_url(token)
    node = socket.getfqdn() if ssh_host is None else ssh_host
    jump = None if direct else via or f"{user}@<login-node>"
    print("Serving review data over a Unix socket (experimental: browser review over SSH).")
    print("\nOn your laptop, forward a local port to the socket (leave it running):")
    print(f"  {ssh_forward_command(socket_path, node, user, jump)}")
    print(f"If port {BROWSER_PORT} is busy on your laptop, change it in -L and in the URL.")
    if from_env:
        print("Using the token from $IMAGE_REVIEW_TOKEN: the URL stays the same across restarts in this shell.")
    if sys.stdout.isatty():
        print("\nThen open this URL. It contains an access token; treat it like a password.\n")
        print(url)
    else:
        try:
            url_file = write_private_file(f"browser-{safe_name(short_host())}-{os.getpid()}.txt", url + "\n")
        except OSError as e:
            raise click.ClickException(f"Cannot write browser URL file: {e}")
        stack.callback(url_file.unlink, missing_ok=True)
        read_host = shlex.quote(f"{user}@{node}" if jump is None else jump)
        remote_path = shlex.quote(shlex.quote(str(url_file)))  # the remote shell parses it again
        print("\nStdout is not a terminal, so the URL (it contains an access token; treat it like a")
        print(f"password) was written to {url_file} (mode 0600) on this node.")
        if jump is None:
            print("On your laptop, read it from the node with:")
        else:
            print("Home directories are usually shared with the login node, so on your laptop read it with:")
        print(f"  ssh {read_host} cat {remote_path}")
        print("then open it in your browser once the ssh command above is running.")
    print("\nPress Ctrl-C to stop.", flush=True)


def _refuse_live(writer: WorkDirLocked | None, allow_live: bool) -> None:
    """Refuse to export while a writer holds the work directory, unless --allow-live (then warn)."""
    if writer is None:
        return
    if not allow_live:
        raise click.ClickException(
            f"{writer}. Verdicts may still change; stop the writer and export again, or pass --allow-live."
        )
    log.warning("exporting while a writer may be recording verdicts (--allow-live): %s", writer)


def _close_store_after_marks(server: "ReviewServer", store: LocalStore) -> None:
    with server.store_lock:  # a mark in flight finishes first; later ones get PermissionError (a 500)
        store.close()


def main():
    cli()


if __name__ == "__main__":
    main()
