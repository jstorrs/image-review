# image-review

A command-line tool for reviewing medical (DICOM) and general images for
burned-in Protected Health Information (PHI).

The workflow has four steps:

1. **Preprocess** raw DICOM and image files into normalized JPG batches.
2. **Review** the images interactively in a fullscreen viewer, in single or
   grid mode.
3. **Status** reports on review progress.
4. **Export** writes the allowlist of files that may be released.

To get started, see [Installation](install.md) and the [Quick start](quickstart.md).

Each command has its own page: see the [commands overview](commands/index.md), or go
straight to [preprocess](commands/preprocess.md), [review](commands/review.md),
[status](commands/status.md), [export](commands/export.md) or
[serve](commands/serve.md).

To review images that stay on an HPC cluster, see the tutorials
[Remote review on an HPC cluster](tutorials/remote-review.md) and the
experimental [Browser review over SSH](tutorials/browser-review.md).

See the [Changelog](reference/changelog.md) for what changed in each release.
