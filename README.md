# image-review

A command-line tool for reviewing medical (DICOM) and general images for
burned-in Protected Health Information (PHI).

The workflow has four steps:

1. **Preprocess** raw DICOM and image files into normalized JPG batches.
2. **Review** the images in a browser, in single or grid mode: on this
   machine, or from your laptop while the images stay on a cluster.
3. **Status** reports on review progress.
4. **Export** writes the allowlist of files that may be released.

The documentation is at <https://jstorrs.github.io/image-review/>.

## Installation

Requires Python >= 3.12. `pip install image-review` installs every command
from PyPI (or `pip install .` from a clone); `review` needs only a browser.
See [Installation](https://github.com/jstorrs/image-review/blob/main/docs/install.md)
for the dependencies, cluster installs without root, compressed DICOMs and minimum
dependency versions.

## Quick start

```bash
# Preprocess a directory of DICOMs or a ZIP archive
image-review preprocess /path/to/dicoms/ --work-dir ./review_work

# Review in your browser: pass 1 is grid triage (press m), later passes inspect the
# flagged (DIRTY) images one at a time; press r on the page for the next pass, Ctrl-C to stop
image-review review

# Check progress (--check: exit 1 until every image has a verdict)
image-review status

# Write the allowlist of files that may be released (path + SHA-256), and a report of the rest
image-review export --output allowlist.tsv --report report.tsv
```

The default work directory is `./review_work`. Don't create work directories
inside a git checkout (the repository's `.gitignore` excludes them as a
safety net).

The quick start is also on its own page: [Quick start](https://github.com/jstorrs/image-review/blob/main/docs/quickstart.md).

## Documentation

- [Local review](https://github.com/jstorrs/image-review/blob/main/docs/tutorials/local-review.md): a walk-through of a whole
  review on one machine, in your browser.
- [Browser review over SSH](https://github.com/jstorrs/image-review/blob/main/docs/tutorials/browser-review.md): the
  recommended way to review images that stay on the cluster, from your
  laptop. There is nothing to install on the laptop but ssh and a browser,
  and nothing to keep in sync: the page is served by `serve` itself, so it
  always matches the server's version.
- [Commands overview](https://github.com/jstorrs/image-review/blob/main/docs/commands/index.md), with one page each for
  [preprocess](https://github.com/jstorrs/image-review/blob/main/docs/commands/preprocess.md),
  [review](https://github.com/jstorrs/image-review/blob/main/docs/commands/review.md), [status](https://github.com/jstorrs/image-review/blob/main/docs/commands/status.md),
  [export](https://github.com/jstorrs/image-review/blob/main/docs/commands/export.md) and [serve](https://github.com/jstorrs/image-review/blob/main/docs/commands/serve.md).
- [Work directory](https://github.com/jstorrs/image-review/blob/main/docs/reference/work-directory.md) and the
  [specification](https://github.com/jstorrs/image-review/blob/main/docs/reference/specification.md).

## Security

Report vulnerabilities privately, as [SECURITY.md](https://github.com/jstorrs/image-review/blob/main/SECURITY.md) describes. The
[security model](https://github.com/jstorrs/image-review/blob/main/docs/reference/security-model.md) states what the tool
protects and its limits.

## Contributing

Development setup, the test, lint and type-check commands, and the project's
conventions are in [CONTRIBUTING.md](https://github.com/jstorrs/image-review/blob/main/CONTRIBUTING.md).

## Changelog

See [CHANGELOG.md](https://github.com/jstorrs/image-review/blob/main/CHANGELOG.md).
