# Quick start

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

## Next steps

- [Installation](install.md): extras, cluster installs and compressed DICOMs.
- [Commands overview](commands/index.md): logging and the options that go before the command.
- [preprocess](commands/preprocess.md), [review](commands/review.md), [status](commands/status.md), [export](commands/export.md) and [serve](commands/serve.md): one page per command.
