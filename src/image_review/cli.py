import ipaddress
import signal
import socket
import sys
from pathlib import Path

import click


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


@cli.command()
@click.argument("sources", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--batch-size", type=int, default=300, show_default=True, help="Images per batch.")
@click.option("--work-dir", "--output-dir", type=click.Path(), default="./review_work", show_default=True, help="Work directory for output.")
@click.option("--colormap", type=str, default="inferno", show_default=True, help="Matplotlib colormap for rendering.")
def preprocess(sources, batch_size, work_dir, colormap):
    """Normalize DICOM and image files to JPGs and organize them into batches.

    SOURCES are one or more ZIP files, directories, or image files to process.
    """
    from .preprocess import run_preprocess

    source_paths = [Path(s).resolve() for s in sources]
    run_preprocess(source_paths, Path(work_dir), batch_size=batch_size, colormap=colormap)


@cli.command()
@click.option("--mode", type=click.Choice(["single", "grid"]), default="single", show_default=True, help="Review display mode.")
@click.option("--pass", "pass_number", type=int, default=None, help="Pass number (auto-detected if omitted).")
@click.option("--batch", type=str, default=None, help="Restrict to a specific batch.")
@click.option("--filter", "status_filter", type=click.Choice(["unreviewed", "clean", "all"]), default="unreviewed", show_default=True, help="Which images to show.")
@click.option("--rotate/--no-rotate", default=True, show_default=True, help="Allow rectpack to rotate images for tighter grid packing.")
@click.option("--work-dir", type=click.Path(exists=True), default="./review_work", show_default=True, help="Work directory containing preprocessed data.")
def review(mode, pass_number, batch, status_filter, rotate, work_dir):
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
    from .store import LocalStore

    pg.init()
    try:
        try:
            session = ReviewSession(
                store=LocalStore(Path(work_dir)),
                mode=mode,
                pass_number=pass_number,
                batch=batch,
                status_filter=status_filter,
                allow_rotation=rotate,
            )
        except FileNotFoundError:
            pg.quit()
            raise click.ClickException("No preprocessed data found. Run `image-review preprocess` first.")
        session.run()
    finally:
        pg.quit()


@cli.command()
@click.option("--work-dir", type=click.Path(exists=True), default="./review_work", show_default=True, help="Work directory containing preprocessed data.")
def status(work_dir):
    """Report overall and per-batch review progress (CLEAN / DIRTY / UNREVIEWED counts)."""
    from .store import LocalStore, batch_summary, summary

    try:
        store = LocalStore(Path(work_dir))
    except FileNotFoundError:
        raise click.ClickException("No preprocessed data found. Run `image-review preprocess` first.")

    manifest = store.manifest()
    current = store.current_pass()
    statuses = store.statuses(current)

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
