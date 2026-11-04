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
For a walk-through of a whole review on one machine, see the
[Local review](tutorials/local-review.md) tutorial.

Each command has its own page: see the [commands overview](commands/index.md), or go
straight to [preprocess](commands/preprocess.md), [review](commands/review.md),
[status](commands/status.md), [export](commands/export.md) or
[serve](commands/serve.md).

To review images that stay on an HPC cluster, see
[Browser review over SSH](tutorials/browser-review.md), the recommended
remote workflow. There is nothing to install on the laptop but ssh and a
browser, and nothing to keep in sync: the page is served by `serve` itself,
so it always matches the server's version. The deprecated
[Python client](tutorials/remote-review.md) must run the same version as the
server, and a mismatch is refused.

The files in a work directory are described in
[Work directory](reference/work-directory.md).

See the [Changelog](reference/changelog.md) for what changed in each release.

```{toctree}
:hidden:
:caption: Getting started

install.md
quickstart.md
```

```{toctree}
:hidden:
:caption: Tutorials

tutorials/local-review.md
Remote review in a browser <tutorials/browser-review.md>
Remote review, Python client (deprecated) <tutorials/remote-review.md>
```

```{toctree}
:hidden:
:caption: Commands

Overview <commands/index.md>
preprocess <commands/preprocess.md>
review <commands/review.md>
status <commands/status.md>
export <commands/export.md>
serve <commands/serve.md>
browser page <commands/browser.md>
```

```{toctree}
:hidden:
:caption: Reference

reference/work-directory.md
reference/security-model.md
Specification <reference/specification.md>
reference/changelog.md
```
