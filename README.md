# image-review

A command-line tool for reviewing medical (DICOM) and general images for
burned-in Protected Health Information (PHI).

The workflow has four steps:

1. **Preprocess** raw DICOM and image files into normalized JPG batches.
2. **Review** the images interactively in a fullscreen viewer, in single or
   grid mode.
3. **Status** reports on review progress.
4. **Export** writes the allowlist of files that may be released.

To review images that stay on an HPC cluster, `serve` the work directory
there and review it from your laptop; see
[Reviewing on an HPC cluster](docs/tutorials/remote-review.md).

## Installation

Requires Python >= 3.12. Install everything with `pip install '.[all]'` from a
clone, or `pip install 'image-review[all]'` from a wheel or a package index.
See [Installation](docs/install.md) for the extras, cluster installs without
root, compressed DICOMs and minimum dependency versions.

## Quick start

```bash
# Preprocess a directory of DICOMs or a ZIP archive
image-review preprocess /path/to/dicoms/ --work-dir ./review_work

# Pass 1: grid triage — quickly mark entire grids CLEAN or DIRTY
image-review review --mode grid

# Pass 2: single review — inspect only the flagged (pass-1 DIRTY) images individually
image-review review --mode single

# Check progress (--check: exit 1 until every image has a verdict)
image-review status

# Write the allowlist of files that may be released (path + SHA-256), and a report of the rest
image-review export --output allowlist.tsv --report report.tsv
```

The default work directory is `./review_work`. Don't create work directories
inside a git checkout (the repository's `.gitignore` excludes them as a
safety net).

The quick start is also on its own page: [Quick start](docs/quickstart.md).

## Commands

- [`image-review preprocess`](docs/commands/preprocess.md): Turn DICOM and image files into normalized JPG batches.
- [`image-review review`](docs/commands/review.md): Review the images interactively, in single or grid mode.
- [`image-review status`](docs/commands/status.md): Report on review progress.
- [`image-review export`](docs/commands/export.md): Write the allowlist of files that may be released.
- [`image-review serve`](docs/commands/serve.md): Serve a work directory so it can be reviewed remotely.

Logging and the options that go before the command are described in the
[commands overview](docs/commands/index.md).

## Reviewing on an HPC cluster

Preprocess and `serve` on the cluster, and review from your laptop with the
pygame viewer or, experimentally, a browser. See
[Remote review on an HPC cluster](docs/tutorials/remote-review.md) and
[Browser review over SSH](docs/tutorials/browser-review.md); the
[security model](docs/reference/security-model.md) states the threat model.

## Multi-pass workflow

1. **Pass 1** (grid triage): mark grids CLEAN or DIRTY. Err toward DIRTY.
2. **Pass 2** (single review): only images marked DIRTY in pass 1 are shown,
   as FLAGGED (orange status bar). Inspect them individually. Grid mode skips
   FLAGGED and DIRTY images, so a grid keypress cannot clear them.
3. **Pass 3+**: repeat on the shrinking DIRTY pool until confident.

Sessions are resumable: quitting saves all progress. The batch and pass
number are auto-detected when not specified. Press `b` at the end of a batch
to move on to the next one, and into the next pass once this one is done (not
with `--batch`, which keeps you in that batch).

## Contributing

Development setup, the test, lint and type-check commands, and the project's
conventions are in [CONTRIBUTING.md](CONTRIBUTING.md).
