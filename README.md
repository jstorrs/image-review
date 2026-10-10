# image-review

A command-line tool for reviewing medical (DICOM) and general images for
burned-in Protected Health Information (PHI).

The workflow has four steps:

1. **Preprocess** raw DICOM and image files into normalized JPG batches.
2. **Review** the images interactively in a fullscreen viewer, in single or
   grid mode.
3. **Status** reports on review progress.
4. **Export** writes the allowlist of files that may be released.

The documentation is at <https://jstorrs.github.io/image-review/>.

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

## Documentation

- [Local review](docs/tutorials/local-review.md): a walk-through of a whole
  review on one machine.
- [Browser review over SSH](docs/tutorials/browser-review.md): the
  recommended way to review images that stay on the cluster, from your
  laptop. There is nothing to install on the laptop but ssh and a browser,
  and nothing to keep in sync: the page is served by `serve` itself, so it
  always matches the server's version. The deprecated
  [Python client](docs/tutorials/remote-review.md) must run the same version
  as the server, and a mismatch is refused.
- [Commands overview](docs/commands/index.md), with one page each for
  [preprocess](docs/commands/preprocess.md),
  [review](docs/commands/review.md), [status](docs/commands/status.md),
  [export](docs/commands/export.md) and [serve](docs/commands/serve.md).
- [Work directory](docs/reference/work-directory.md) and the
  [specification](docs/reference/specification.md).

## Security

Report vulnerabilities privately, as [SECURITY.md](SECURITY.md) describes. The
[security model](docs/reference/security-model.md) states what the tool
protects and its limits.

## Contributing

Development setup, the test, lint and type-check commands, and the project's
conventions are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
