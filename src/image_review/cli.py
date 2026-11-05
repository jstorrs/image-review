import contextlib
import datetime
import functools
import getpass
import html
import logging
import math
import os
import shlex
import signal
import socket
import sys
import threading
import webbrowser
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, get_args

import click

from .access import Access, access_of, modes, world_access_warning
from .atomic import write_new_file
from .connection import Token, parse_token
from .export import ExportRow, ExportStatus, format_allowlist, format_report, split_allowlist
from .lock import LOCK_NAME, WorkDirLocked, live_writer
from .signals import TERMINATION_SIGNALS, interrupt_on, interrupt_once
from .status import Status
from .store import LocalStore, SkippedCounts, batch_summary, skipped_counts, summary

if TYPE_CHECKING:
    from .server import ReviewServer

DEFAULT_WORK_DIR = "./review_work"

PACKAGE_LOGGER = "image_review"  # every module logs under it; the CLI configures only this tree
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

log = logging.getLogger(f"{PACKAGE_LOGGER}.cli")  # not __name__: that is "__main__" under `python -m`


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


def has_unix_sockets() -> bool:
    """Whether this system has AF_UNIX (not on Windows), which browser review's socket server needs."""
    return hasattr(socket, "AF_UNIX")


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
      2. review      — review in a browser on this machine (or `serve`, for a browser on your laptop over SSH)
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
@click.option(
    "--draw-overlays",
    is_flag=True,
    default=False,
    help="Draw DICOM overlay planes onto the review images (by default they are not drawn, only listed in notes.tsv).",
)
def preprocess(sources, batch_size, work_dir, colormap, access, allow_skipped, jobs, draw_overlays):
    """Normalize DICOM and image files to JPGs and organize them into batches.

    SOURCES are one or more ZIP files, directories, or image files to process.
    Inputs are recognized by content, not extension. Every input is listed in
    either manifest.tsv or skipped.tsv (as failed, or ignored when it is not an
    image). Exits 1 if any input failed, unless --allow-skipped is given.
    """
    from tqdm.contrib.logging import logging_redirect_tqdm

    from .preprocess import WorkDirExists, WorkerCrashed, run_preprocess

    source_paths = [Path(s).resolve() for s in sources]

    try:
        with (
            interrupt_on(*TERMINATION_SIGNALS),  # SIGTERM/SIGHUP unwind like Ctrl-C: workers stop, staging goes
            logging_redirect_tqdm(loggers=[logging.getLogger(PACKAGE_LOGGER)]),  # log lines print above the bars
        ):
            result = run_preprocess(
                source_paths,
                Path(work_dir),
                batch_size=batch_size,
                colormap=colormap,
                access=access,
                jobs=jobs,
                draw_overlays=draw_overlays,
            )
    except (WorkDirExists, WorkerCrashed) as exc:
        raise click.ClickException(str(exc)) from exc
    skipped = skipped_counts(result.skipped)
    failed = skipped.failed
    total_notes = sum(result.notes.values())
    notes = ""
    if total_notes:
        kinds = ", ".join(f"{count} {kind}" for kind, count in result.notes.items() if count)
        notes = f"; {total_notes} note{'' if total_notes == 1 else 's'} ({kinds}; see {result.notes_path})"
    click.echo(
        f"Found {result.found} inputs: wrote {result.written} images in {result.batches} batches; "
        f"{len(result.skipped)} skipped ({failed} failed, {skipped.ignored} ignored; see {result.skipped_path})"
        f"{notes}"
    )
    if access == "group":
        click.echo(_shared_with(Path(work_dir)))
    if failed and not allow_skipped:
        raise click.ClickException(
            f"{failed} input(s) failed to preprocess and will not be reviewed; see {result.skipped_path}. "
            f"{work_dir} was still written: review it as it is, or fix them and preprocess into a new "
            "--work-dir (--allow-skipped exits 0 when inputs fail)."
        )


def _loopback_port(ctx: click.Context, param: click.Parameter, value: int) -> int:
    """--port: 0 (any free port) or one of server.LOOPBACK_PORTS, checked before the work directory is opened."""
    from .server import LOOPBACK_PORTS

    if value != 0 and value not in LOOPBACK_PORTS:
        raise click.BadParameter(f"{value} must be 0 (any free port) or 1024-65535.")
    return value


@cli.command()
@click.option(
    "--work-dir",
    type=click.Path(exists=True),
    default=DEFAULT_WORK_DIR,
    show_default=True,
    help="Work directory containing preprocessed data.",
)
@click.option(
    "--port",
    type=int,
    default=0,
    show_default=True,
    callback=_loopback_port,
    help="The port on 127.0.0.1 to serve on: 1024-65535, or 0 for any free one.",
)
@click.option("--no-browser", is_flag=True, help="Do not open a browser; open the URL yourself.")
def review(work_dir, port, no_browser):
    """Review a work directory in a browser on this machine.

    This serves the browser page on 127.0.0.1 and opens it in your
    browser. The URL contains an access token: treat it like a password.
    When stdout is not a terminal, the URL is not printed: open the
    private file under ~/.image-review/ that is named instead. To review
    data on another machine, such as a cluster, use `serve`.
    Needs Linux or macOS.
    """
    if sys.platform == "win32":
        raise click.ClickException("review needs Linux or macOS")

    def start(store: LocalStore, stack: contextlib.ExitStack) -> "ReviewServer":
        return _serve_loopback(store, port, not no_browser, stack)

    _serve_until_stopped(Path(work_dir), start)


def redirect_page(url: str) -> str:
    """The redirect file's HTML: a meta refresh to `url`, and a link to it for a browser that ignores the refresh."""
    target = html.escape(url)
    return (
        "<!doctype html>\n"
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="refresh" content="0; url={target}">\n'
        "<title>image-review</title>\n"
        f'<p><a href="{target}">Open the review page</a></p>\n'
    )


def has_display(platform: str, environ: Mapping[str, str]) -> bool:
    """Whether a graphical session on this machine is plausible: never inside an ssh session ($SSH_CONNECTION, so
    `ssh -X` to a shared node opens no browser there); else macOS, or a $DISPLAY or $WAYLAND_DISPLAY.

    A display does not rule out a terminal browser as the default; `graphical_browser` refuses those.
    """
    if environ.get("SSH_CONNECTION"):
        return False
    return platform == "darwin" or bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


CONSOLE_BROWSERS = frozenset({"lynx", "w3m", "links", "elinks", "www-browser"})


def is_graphical(browser: webbrowser.BaseBrowser) -> bool:
    """False for a controller that would run in this terminal: a foreground `GenericBrowser` (as from $BROWSER),
    or a known console browser by name."""
    return type(browser) is not webbrowser.GenericBrowser and Path(browser.name).name not in CONSOLE_BROWSERS


def graphical_browser() -> webbrowser.BaseBrowser | None:
    """The default browser controller, or None if there is none or it is not graphical (see `is_graphical`)."""
    try:
        browser = webbrowser.get()
    except webbrowser.Error:
        return None
    return browser if is_graphical(browser) else None


def _serve_loopback(store: LocalStore, port: int, open_browser: bool, stack: contextlib.ExitStack) -> "ReviewServer":
    """Bind the loopback server with a fresh token, write the redirect file and announce it; returns the server.

    The server's and the file's cleanup are registered on `stack`. Failures become ClickExceptions.
    """
    from .server import make_loopback_server, safe_name, short_host, write_private_file

    try:
        server, token = make_loopback_server(store, port)  # --port is already checked
    except OSError as e:
        raise click.ClickException(f"Cannot listen on 127.0.0.1:{port}: {e}")
    _close_with(stack, server, store)
    url = browser_url(token, server.server_port)  # the bound port: --port 0 picks one
    try:
        redirect = write_private_file(f"review-{safe_name(short_host())}-{os.getpid()}.html", redirect_page(url))
    except OSError as e:
        raise click.ClickException(f"Cannot write browser redirect file: {e}")
    stack.callback(redirect.unlink, missing_ok=True)
    _announce_loopback(url, redirect, open_browser, stack)
    return server


BROWSER_THREAD = "image-review-browser"  # names the thread that opens the browser


def _announce_loopback(url: str, redirect: Path, open_browser: bool, stack: contextlib.ExitStack) -> None:
    """Print the URL on a terminal, else the redirect file's path; then, if asked, open the browser on the file.

    The browser gets the file's URI, so the token never appears in a process's command line. It is chosen and
    opened from a daemon thread, so neither a slow choice nor a browser that does not return can hold up the
    server, which is already listening. The thread's stop is registered on `stack` after the redirect file's
    removal, so it runs first: once cleanup starts, the thread opens and prints nothing.
    """
    tty = sys.stdout.isatty()
    print("Serving review data on 127.0.0.1 for browser review on this machine.")
    if tty:
        print("\nOpen this URL. It contains an access token; treat it like a password.\n")
        print(url)
    else:
        print("\nStdout is not a terminal, so the URL (it contains an access token; treat it like a")
        print("password) is not printed. Open this file (mode 0600) in your browser:\n")
        print(f"  {redirect}")
    print(flush=True)
    display = open_browser and has_display(sys.platform, os.environ)
    if open_browser and not display:
        print("No display found, so no browser was opened; open it yourself.")
    print("Press Ctrl-C to stop.", flush=True)
    if display:
        where = "the URL above" if tty else f"the URL from `cat {shlex.quote(str(redirect))}`"
        opener = _BrowserOpener(redirect, where)
        stack.callback(opener.stop)
        threading.Thread(target=opener.run, name=BROWSER_THREAD, daemon=True).start()


class _BrowserOpener:
    """Choose a graphical browser and open the redirect file in it, saying how it went, until stopped.

    Run in its own thread: choosing can take seconds (webbrowser runs xdg-settings and the like). Opening and
    each print happen under `_lock` after checking `_stopped`, so none happens once `stop` has returned.
    """

    STOP_WAIT = 5.0  # seconds stop waits for an open or print in progress; it never blocks cleanup for longer

    def __init__(self, redirect: Path, where: str):
        self._redirect = redirect
        self._where = where  # where else to find the URL
        self._lock = threading.Lock()
        self._stopped = threading.Event()

    def stop(self) -> None:
        acquired = self._lock.acquire(timeout=self.STOP_WAIT)
        self._stopped.set()
        if acquired:
            self._lock.release()

    def _say(self, *lines: str) -> None:
        with self._lock:
            if not self._stopped.is_set():
                print("\n".join(lines), flush=True)

    def run(self) -> None:
        try:
            browser = graphical_browser()
        except Exception as e:  # noqa: BLE001 - whatever webbrowser raises, the server keeps running
            log.debug("choosing a browser failed: %s", type(e).__name__)
            browser = None
        if browser is None:
            self._say("No graphical browser found, so none was opened; open it yourself.")
            return
        with self._lock:
            if self._stopped.is_set():
                return
            try:
                opened = browser.open(self._redirect.as_uri())
            except Exception as e:  # noqa: BLE001 - e.g. the browser's command vanished
                log.debug("opening the browser failed: %s", type(e).__name__)
                opened = False
        if opened:
            self._say(
                "Opened it in your browser. If the browser shows an access error (a snap browser, such as",
                f"Ubuntu's Firefox, cannot read ~/.image-review), open {self._where} instead.",
            )
        else:
            self._say("Could not open a browser; open it yourself.")


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
@click.option(
    "--check",
    is_flag=True,
    default=False,
    help="Exit 1 if any image is UNREVIEWED (has no verdict) or any input failed to preprocess; else 0. "
    "FLAGGED images have a DIRTY verdict, so they count as decided.",
)
def status(work_dir, check):
    """Report overall and per-batch review progress (CLEAN / DIRTY / UNREVIEWED / FLAGGED counts).

    With --check, the exit status also says whether the review is finished: every image has a verdict.
    """
    with open_local_store(_existing_work_dir(work_dir), read_only=True) as store:
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
def export(work_dir, output, report, allow_live):
    """Write the allowlist of releasable files as TSV: source_sha256, image_id, pass_number, timestamp, reviewer.

    Default deny: release a file only if both its path (image_id; a ZIP entry is `<zip>::<entry>`) and its SHA-256
    match a row; anything not listed is denied. A file is listed only if it was reviewed CLEAN (and, in a work
    directory made by an older version, its `#icon` row too), the manifest has its hash, and no file that is not
    CLEAN has the same recorded hash. --report writes the rest as TSV (image_id, status, pass_number, timestamp,
    reviewer, reason, source_sha256): DIRTY (a FLAGGED image is DIRTY), UNREVIEWED, NOT_REVIEWED when preprocess
    failed on it or notes.tsv lists content its review image does not show (an icon, thumbnail, overlay or trailing
    data), IGNORED when preprocess did not take it for an image, and CLEAN ones not allowlisted, with why.
    """
    if report is not None and output is not None and os.path.realpath(report) == os.path.realpath(output):
        raise click.UsageError("--report and --output name the same file.")
    path = _existing_work_dir(work_dir)
    with interrupt_on(*TERMINATION_SIGNALS):  # a SIGTERM/SIGHUP unwinds like Ctrl-C, removing a half-written file
        writer = live_writer(path)
        _refuse_live(writer, allow_live)
        with open_local_store(path, read_only=True) as store:
            # A torn or cut-short review.tsv, a malformed or (beside a hashed manifest) missing skipped.tsv or
            # notes.tsv, or an unsafe field in either text: both are built even without --report, so one unsafe
            # field anywhere refuses the whole export.
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
    "--socket-path",
    envvar="IMAGE_REVIEW_SOCKET_PATH",
    default=None,
    help="The socket to serve on [default: ~/.image-review/serve-HOST-PID.sock]; also read from $IMAGE_REVIEW_SOCKET_PATH.",
)
@click.option(
    "--via",
    envvar="IMAGE_REVIEW_VIA",
    default=None,
    help="The login node (e.g. user@login.cluster) to put in the printed ssh command (also read from $IMAGE_REVIEW_VIA).",
)
@click.option(
    "--direct",
    envvar="IMAGE_REVIEW_DIRECT",
    is_flag=True,
    help="Your laptop reaches compute nodes without a jump host; the printed ssh command omits -J (also read from $IMAGE_REVIEW_DIRECT).",
)
@click.option(
    "--ssh-host",
    default=None,
    help="The node name to put in the printed ssh command, if its own FQDN does not work from your laptop [default: this machine's FQDN].",
)
def serve(work_dir, socket_path, via, direct, ssh_host):
    """Serve a work directory for browser review over SSH.

    This serves plain HTTP on a Unix socket, for a browser on your
    laptop reached through `ssh -L`. Images never leave this machine except
    to a client holding the URL, which contains an access token: treat it
    like a password. When stdout is not a terminal (e.g. sbatch), the URL is
    written to a private file under ~/.image-review/ instead of being
    printed. A token in $IMAGE_REVIEW_TOKEN (22-256 characters from
    A-Za-z0-9_-) is used instead of a fresh one, so the URL stays the same
    across restarts: generate it inside the job, e.g.
    export IMAGE_REVIEW_TOKEN=$(openssl rand -hex 16).
    """
    from .tunnel import parse_ssh_host, parse_via

    ctx = click.get_current_context()

    def given(name: str) -> bool:
        return ctx.get_parameter_source(name) is click.core.ParameterSource.COMMANDLINE

    if socket_path == "":  # e.g. --socket-path "$UNSET"; Path("") would quietly mean the current directory
        raise click.UsageError("--socket-path must not be empty.")
    if direct and via is not None:
        if given("via"):
            raise click.UsageError("--direct and --via are mutually exclusive.")
        via = None  # --direct, from either source, drops a $IMAGE_REVIEW_VIA
    if not has_unix_sockets():
        raise click.ClickException("this system has no Unix sockets; browser review needs one")
    if via is not None:
        try:
            via = parse_via(via)
        except ValueError as e:
            source = "--via" if given("via") else "$IMAGE_REVIEW_VIA"
            raise click.ClickException(f"Invalid {source}: {e}")
    if ssh_host is not None:
        try:
            ssh_host = parse_ssh_host(ssh_host)
        except ValueError as e:
            raise click.ClickException(f"Invalid --ssh-host: {e}")
    token: Token | None = None
    if raw_token := os.environ.get("IMAGE_REVIEW_TOKEN"):  # empty counts as unset
        try:
            token = parse_token(raw_token)
        except ValueError as e:  # the message never holds the value
            raise click.ClickException(f"Invalid $IMAGE_REVIEW_TOKEN: {e}")

    def start(store: LocalStore, stack: contextlib.ExitStack) -> "ReviewServer":
        server, announce = _serve_socket(store, socket_path, token, via, direct, ssh_host, stack)
        announce(stack)
        return server

    _serve_until_stopped(Path(work_dir), start)


def _serve_until_stopped(work_dir: Path, start: Callable[[LocalStore, contextlib.ExitStack], "ReviewServer"]) -> None:
    """Open the work directory (taking its lock), start a server on it with `start`, and serve until stopped.

    `start` binds and announces the server and registers its cleanup on the stack (see `_close_with`).
    Ctrl-C, SIGTERM and SIGHUP stop it; every exit path closes the server and releases the lock.
    """
    try:
        # Slurm stops jobs with SIGTERM (scancel, time limit): shut down like Ctrl-C so cleanup runs.
        # Installed before the lock is taken, so every exit path releases it. Only the first signal counts: a second
        # one during cleanup would cut short the lock release or the wait for a mark in flight.
        with interrupt_once(signal.SIGINT, *TERMINATION_SIGNALS), contextlib.ExitStack() as stack:
            store = stack.enter_context(open_local_store(work_dir))  # holds the work dir lock
            start(store, stack).serve_forever()
    except KeyboardInterrupt:
        pass


def _close_with(stack: contextlib.ExitStack, server: "ReviewServer", store: LocalStore) -> None:
    """Register the server's shutdown on `stack` as soon as it exists.

    Exit order (LIFO): the announced file, the socket (which also removes the socket file),
    then the store; a handler thread may still be marking.
    """
    stack.callback(_close_store_after_marks, server, store)
    stack.callback(server.server_close)


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


BROWSER_PORT = 8080  # the laptop-side port in the printed ssh command and URL; changeable if busy


def browser_url(token: str, port: int = BROWSER_PORT) -> str:
    """The URL to open in the browser; the token rides in the fragment, which the browser never sends.

    127.0.0.1, not localhost: `review`'s server and `serve`'s ssh forward bind only that address, so nothing
    listening on ::1 can answer the URL.
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
    print("Serving review data over a Unix socket for browser review over SSH.")
    print("\nOn your laptop, forward a local port to the socket (leave it running):")
    print(f"\n  {ssh_forward_command(socket_path, node, user, jump)}\n")  # set apart, to copy cleanly
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
