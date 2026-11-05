# Quick start

```bash
# Preprocess a directory of DICOMs or a ZIP archive
image-review preprocess /path/to/dicoms/ --work-dir ./review_work

# Review in your browser: pass 1 is grid triage (press m), later passes inspect the
# flagged (DIRTY) images one at a time; stop it with Ctrl-C and run it again for each pass
image-review review

# Check progress (--check: exit 1 until every image has a verdict)
image-review status

# Write the allowlist of files that may be released (path + SHA-256), and a report of the rest
image-review export --output allowlist.tsv --report report.tsv
```

The default work directory is `./review_work`. Don't create work directories
inside a git checkout (the repository's `.gitignore` excludes them as a
safety net).

## Next steps

- [Local review](tutorials/local-review.md): a walk-through of a whole review on one machine, in your browser.
- [Browser review over SSH](tutorials/browser-review.md): the recommended way to review images that stay on the cluster, from your laptop. There is nothing to install on the laptop but ssh and a browser, and nothing to keep in sync: the page is served by `serve` itself, so it always matches the server's version.
- [Installation](install.md): the dependencies, cluster installs and compressed DICOMs.
- [Commands overview](commands/index.md): logging and the options that go before the command.
- [preprocess](commands/preprocess.md), [review](commands/review.md), [status](commands/status.md), [export](commands/export.md) and [serve](commands/serve.md): one page per command.
