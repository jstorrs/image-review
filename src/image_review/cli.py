import contextlib
import ipaddress
import signal
import socket
import sys
from collections.abc import Iterator
from pathlib import Path

import click

from .access import world_access_warning
from .store import ReviewStore

DEFAULT_WORK_DIR = "./review_work"


def work_dir_option(f):
    return click.option("--work-dir", type=click.Path(exists=True), default=None, help=f"Work directory containing preprocessed data [default: {DEFAULT_WORK_DIR}].")(f)


def remote_option(f):
    return click.option("--remote", envvar="IMAGE_REVIEW_REMOTE", default=None, help="Review a server started with `image-review serve` (ir:// connection string; also read from $IMAGE_REVIEW_REMOTE).")(f)


def via_option(f):
    return click.option("--via", envvar="IMAGE_REVIEW_VIA", default=None, help="With --remote: reach the server through an SSH tunnel via this login node, e.g. user@login.cluster (also read from $IMAGE_REVIEW_VIA).")(f)


def warn_if_world_accessible(work_dir: Path) -> None:
    warning = world_access_warning(work_dir)
    if warning is not None:
        click.echo(warning, err=True)


@contextlib.contextmanager
def open_store(work_dir: str | None, remote: str | None, via: str | None = None) -> Iterator[ReviewStore]:
    """Open the local or remote store, translating startup failures into ClickExceptions."""
    if remote is None:
        if via is not None and click.get_current_context().get_parameter_source("via") is not click.core.ParameterSource.ENVIRONMENT:
            raise click.UsageError("--via requires --remote.")
        raw = work_dir if work_dir is not None else DEFAULT_WORK_DIR
        path = Path(raw)
        if not path.exists():
            raise click.BadParameter(f"Path '{raw}' does not exist.", param_hint="'--work-dir'")
        from .store import LocalStore

        try:
            store = LocalStore(path)
        except FileNotFoundError:
            raise click.ClickException("No preprocessed data found. Run `image-review preprocess` first.")
        warn_if_world_accessible(path)
        yield store
        return

    if work_dir is not None:
        if click.get_current_context().get_parameter_source("remote") is click.core.ParameterSource.ENVIRONMENT:
            raise click.UsageError("IMAGE_REVIEW_REMOTE is set; unset it to use --work-dir.")
        raise click.UsageError("--remote and --work-dir are mutually exclusive.")
    from .connection import RemoteTarget
    from .remote import ApiMismatch, FingerprintMismatch, RemoteError, RemoteStore
    from .tunnel import TunnelError

    try:
        target = RemoteTarget.parse(remote)
    except ValueError as e:
        raise click.ClickException(f"Invalid --remote connection string: {e}")
    if via is not None:
        from .tunnel import parse_via

        try:
            via = parse_via(via)
        except ValueError as e:
            raise click.ClickException(f"Invalid --via: {e}")
    where = f"{target.host}:{target.port}" + (f" (via {via})" if via else "")
    try:
        with contextlib.ExitStack() as stack:
            if via is None:
                store = RemoteStore(target)
            else:
                from .tunnel import ssh_tunnel

                local_port = stack.enter_context(ssh_tunnel(via, target.host, target.port))
                store = RemoteStore(target, connect_host="127.0.0.1", connect_port=local_port)
            stack.enter_context(store)  # closed before the tunnel
            store.check_api()
            yield store
    except TunnelError as e:
        raise click.ClickException(str(e))
    except ApiMismatch as e:
        raise click.ClickException(str(e))
    except FingerprintMismatch:
        raise click.ClickException(
            f"The certificate presented by {where} does NOT match the connection string. "
            "The connection was aborted before any credentials were sent. "
            "Do not continue unless you know why the server's identity changed."
            + (" With --via, this can also happen if another local process grabbed the forwarded port; retry." if via else "")
        )
    except RemoteError as e:
        if e.status == 401:
            raise click.ClickException(f"Server at {where} rejected the access token (connection string from a different or restarted server?)")
        if e.status is not None:
            raise click.ClickException(f"Server at {where} returned HTTP {e.status}")
        transport_failure = isinstance(e.__cause__, OSError)
        hint = " (the login node may not be able to reach the server; see the ssh output above)" if via and transport_failure else ""
        raise click.ClickException(f"Cannot reach server at {where}: {e}{hint}")


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
@click.version_option()
def cli():
    """Review DICOM / medical images for burned-in PHI.

    Workflow:

    \b
      1. preprocess  — convert source DICOMs/images to JPG batches
      2. review      — interactively classify images as CLEAN or DIRTY
      3. status      — check review progress and counts
    """


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


@cli.command()
@click.argument("sources", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--batch-size", type=click.IntRange(min=1), default=300, show_default=True, help="Images per batch.")
@click.option("--work-dir", "--output-dir", type=click.Path(), default="./review_work", show_default=True, help="Work directory for output.")
@click.option("--colormap", type=str, callback=_known_colormap, default="inferno", show_default=True, help="Matplotlib colormap for rendering.")
@click.option(
    "--access",
    type=click.Choice(["private", "group"]),
    envvar="IMAGE_REVIEW_ACCESS",
    default="private",
    show_default=True,
    help="Who can use the work directory: private = owner only (dirs 0700, files 0600); group = readable/writable by the work dir's Unix group (dirs 2770, files 0660). Never world-readable. Also read from $IMAGE_REVIEW_ACCESS.",
)
@click.option("--allow-skipped", is_flag=True, default=False, help="Exit 0 even if some inputs failed to preprocess (they are listed in skipped.tsv).")
def preprocess(sources, batch_size, work_dir, colormap, access, allow_skipped):
    """Normalize DICOM and image files to JPGs and organize them into batches.

    SOURCES are one or more ZIP files, directories, or image files to process.
    Inputs are recognized by content, not extension. Every input is listed in
    either manifest.tsv or skipped.tsv (as failed, or ignored when it is not an
    image). Exits 1 if any input failed, unless --allow-skipped is given.
    """
    from .preprocess import WorkDirExists, run_preprocess

    source_paths = [Path(s).resolve() for s in sources]
    try:
        result = run_preprocess(source_paths, Path(work_dir), batch_size=batch_size, colormap=colormap, access=access)
    except WorkDirExists as exc:
        raise click.ClickException(str(exc)) from exc
    failed = sum(1 for s in result.skipped if s.kind == "failed")
    click.echo(
        f"Found {result.found} inputs: wrote {result.written} images in {result.batches} batches; "
        f"{len(result.skipped)} skipped ({failed} failed, {len(result.skipped) - failed} ignored; see {result.skipped_path})"
    )
    if access == "group":
        click.echo(_shared_with(Path(work_dir)))
    if failed and not allow_skipped:
        raise click.ClickException(
            f"{failed} input(s) failed to preprocess and will not be reviewed; see {result.skipped_path}. "
            "Re-run with --allow-skipped to accept this."
        )


@cli.command()
@click.option("--mode", type=click.Choice(["single", "grid"]), default="single", show_default=True, help="Review display mode.")
@click.option("--pass", "pass_number", type=int, default=None, help="Pass number (auto-detected if omitted).")
@click.option("--batch", type=str, default=None, help="Restrict to a specific batch.")
@click.option("--filter", "status_filter", type=click.Choice(["unreviewed", "clean", "all"]), default="unreviewed", show_default=True, help="Which images to show.")
@click.option("--rotate/--no-rotate", default=True, show_default=True, help="Allow rectpack to rotate images for tighter grid packing.")
@work_dir_option
@remote_option
@via_option
def review(mode, pass_number, batch, status_filter, rotate, work_dir, remote, via):
    """Open an interactive review session for classifying images.

    \b
    Keyboard controls:
      c / d       — mark image CLEAN / DIRTY
      Arrow keys  — navigate between images
      Space       — toggle autoplay
      q           — quit
    """
    import pygame as pg

    from .controller import ReviewSession

    with open_store(work_dir, remote, via) as store:
        # after the store: SDL must not steal terminal focus during ssh password/MFA prompts
        pg.init()
        try:
            session = ReviewSession(
                store=store,
                mode=mode,
                pass_number=pass_number,
                batch=batch,
                status_filter=status_filter,
                allow_rotation=rotate,
            )
            session.run()
        finally:
            pg.quit()


@cli.command()
@work_dir_option
@remote_option
@via_option
def status(work_dir, remote, via):
    """Report overall and per-batch review progress (CLEAN / DIRTY / UNREVIEWED counts)."""
    from .store import batch_summary, summary

    with open_store(work_dir, remote, via) as store:
        manifest = store.manifest()
        current = store.current_pass()
        statuses = store.statuses(current)
        try:
            skipped = store.skipped()
        except ValueError as e:
            raise click.ClickException(str(e)) from e

    # Overall summary
    counts = summary(manifest, statuses)
    print(f"\nOverall: {counts['total']} images (pass {current})")
    print(f"  CLEAN:      {counts['CLEAN']:>6}")
    print(f"  DIRTY:      {counts['DIRTY']:>6}")
    print(f"  UNREVIEWED: {counts['UNREVIEWED']:>6}")

    # Per-batch summary
    batch_counts = batch_summary(manifest, statuses)
    if len(batch_counts) > 1:
        print(f"\n{'Batch':<15} {'Total':>6} {'Clean':>6} {'Dirty':>6} {'Unrev':>6}")
        print("-" * 45)
        for batch_id in sorted(batch_counts):
            bc = batch_counts[batch_id]
            print(f"{batch_id:<15} {bc['total']:>6} {bc['CLEAN']:>6} {bc['DIRTY']:>6} {bc['UNREVIEWED']:>6}")

    print(f"\nCurrent pass: {current}")

    if skipped is not None and skipped.any:
        print(f"Skipped during preprocess: {skipped.failed} failed, {skipped.ignored} ignored (see skipped.tsv in the work dir)")


@cli.command()
@click.option("--work-dir", type=click.Path(exists=True), default="./review_work", show_default=True, help="Work directory containing preprocessed data.")
@click.option("--bind", default=None, help="Hostname/IPv4 address to bind and advertise [default: this machine's FQDN].")
@click.option("--port", type=click.IntRange(0, 65535), default=0, help="Port to listen on (0 picks a free port).")
def serve(work_dir, bind, port):
    """Serve a work directory over HTTPS so a remote client can review it.

    Images never leave this machine except to a client holding the connection
    string. The string contains an access token: treat it like a password.
    When stdout is not a terminal (e.g. sbatch), it is written to a private
    file under ~/.image-review/ instead of being printed.
    """
    from .connection import RemoteTarget
    from .server import make_server, write_connection_file
    from .store import LocalStore

    if bind is not None and bind.strip() in ("", "0.0.0.0", "::"):
        raise click.ClickException("Refusing to bind a wildcard address; pass this node's hostname with --bind.")
    try:
        store = LocalStore(Path(work_dir))
    except FileNotFoundError:
        raise click.ClickException("No preprocessed data found. Run `image-review preprocess` first.")
    warn_if_world_accessible(Path(work_dir))

    host = bind or socket.getfqdn()
    try:
        server, target = make_server(store, host, port)
    except OSError as e:
        raise click.ClickException(f"Cannot listen on {host}:{port} (IPv4 hostnames/addresses only): {e}")

    connection_file = None
    previous_handlers = {}
    try:
        if ipaddress.ip_address(server.server_address[0]).is_unspecified:
            raise click.ClickException("Refusing to bind a wildcard address; pass this node's hostname with --bind.")
        uri = target.to_uri()
        try:
            RemoteTarget.parse(uri)
        except ValueError as e:
            raise click.ClickException(f"--bind {host!r} cannot be advertised to clients ({e}); use a hostname or dotted IPv4 address.")
        # Slurm stops jobs with SIGTERM (scancel, time limit): shut down like Ctrl-C so cleanup runs
        for sig in (signal.SIGTERM, signal.SIGHUP):
            if signal.getsignal(sig) is signal.SIG_IGN:  # e.g. nohup
                continue
            previous_handlers[sig] = signal.signal(sig, _raise_interrupt)
        if sys.stdout.isatty():
            print("Serving review data. The connection string grants access; treat it like a password.\n")
            print(uri)
            remote_arg = f"'{uri}'"
        else:
            try:
                connection_file = write_connection_file(target)
            except OSError as e:
                raise click.ClickException(f"Cannot write connection file: {e}")
            print("Serving review data. Stdout is not a terminal, so the connection string (an access token;")
            print(f"treat it like a password) was written to {connection_file} (mode 0600) on this node.")
            print("Home directories are usually shared with the login node, so on your laptop use:")
            remote_arg = f'"$(ssh <user>@<login-node> cat {connection_file})"'
        print("\nOn your laptop, directly:")
        print(f"  image-review review --remote {remote_arg}")
        print("or through an SSH tunnel via the login node:")
        print(f"  image-review review --remote {remote_arg} --via <user>@<login-node>")
        print("\nPress Ctrl-C to stop.", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        server.server_close()
        if connection_file is not None:
            connection_file.unlink(missing_ok=True)


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    cli()


if __name__ == "__main__":
    main()
