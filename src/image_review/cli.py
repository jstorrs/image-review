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
@click.option("--work-dir", type=click.Path(exists=True), default="./review_work", show_default=True, help="Work directory containing preprocessed data.")
def review(mode, pass_number, batch, status_filter, work_dir):
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

    pg.init()
    try:
        session = ReviewSession(
            work_dir=Path(work_dir),
            mode=mode,
            pass_number=pass_number,
            batch=batch,
            status_filter=status_filter,
        )
        session.run()
    finally:
        pg.quit()


@cli.command()
@click.option("--work-dir", type=click.Path(exists=True), default="./review_work", show_default=True, help="Work directory containing preprocessed data.")
def status(work_dir):
    """Report overall and per-batch review progress (CLEAN / DIRTY / UNREVIEWED counts)."""
    from .controller import load_manifest
    from .review_db import ReviewDB

    work_dir = Path(work_dir)
    manifest = load_manifest(work_dir)

    db = ReviewDB(work_dir)

    current = db.current_pass(manifest)

    # Overall summary
    counts = db.summary(manifest, current)
    print(f"\nOverall: {counts['total']} images (pass {current})")
    print(f"  CLEAN:      {counts['CLEAN']:>6}")
    print(f"  DIRTY:      {counts['DIRTY']:>6}")
    print(f"  UNREVIEWED: {counts['UNREVIEWED']:>6}")

    # Per-batch summary
    batch_counts = db.batch_summary(manifest, current)
    if len(batch_counts) > 1:
        print(f"\n{'Batch':<15} {'Total':>6} {'Clean':>6} {'Dirty':>6} {'Unrev':>6}")
        print("-" * 45)
        for batch_id in sorted(batch_counts):
            bc = batch_counts[batch_id]
            print(f"{batch_id:<15} {bc['total']:>6} {bc['CLEAN']:>6} {bc['DIRTY']:>6} {bc['UNREVIEWED']:>6}")

    print(f"\nCurrent pass: {current}")


def main():
    cli()


if __name__ == "__main__":
    main()
