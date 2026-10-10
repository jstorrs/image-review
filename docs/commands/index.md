# Commands

- [`image-review preprocess`](preprocess.md): Turn DICOM and image files into normalized JPG batches.
- [`image-review review`](review.md): Review the images interactively, in single or grid mode.
- [`image-review status`](status.md): Report on review progress.
- [`image-review export`](export.md): Write the allowlist of files that may be released.
- [`image-review serve`](serve.md): Serve a work directory so it can be reviewed remotely.

## Logging

Warnings and other diagnostics are logged to stderr as
`time LEVEL module: message`. Put the option before the command, e.g.
`image-review -q status`:

| Option | Shows |
|---|---|
| (default) | INFO and up |
| `-q`, `--quiet` | Only warnings and errors |
| `-v`, `--verbose` | Debug messages too (currently few: the work directory or server opened, the ssh tunnel command, grid packing results) |

Only `serve` and `export` log at INFO. `serve` logs one line per request,
with the peer address, method, path without its query string, and status.
`export` logs one line with the count of allowlisted and reported files. The
server's request line and error records never contain tokens, query strings,
image keys, source paths or exception messages (only exception class names).
Its startup and work-directory messages can name file paths and parse errors.
Warnings from `review` and `status` do name image keys (e.g. an image that
cannot be loaded), and `preprocess` warnings name the source files that
failed, on the machine where `preprocess` runs.
