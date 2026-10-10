# Commands

- [`image-review preprocess`](preprocess.md): Turn DICOM and image files into normalized JPG batches.
- [`image-review review`](review.md): Review the images interactively, in single or grid mode.
- [`image-review status`](status.md): Report on review progress.
- [`image-review export`](export.md): Write the allowlist of files that may be released.
- [`image-review serve`](serve.md): Serve a work directory so it can be reviewed remotely.

## Logging

Warnings and other diagnostics are logged to stderr, one line per record:
`time LEVEL module: message`. The time is ISO 8601 local time with its UTC
offset, to the second. For example:

```text
2026-10-01T14:03:07+02:00 INFO image_review.server: 10.1.2.3 GET /image 200
```

Put the option before the command, e.g. `image-review -q status`:

| Option | Shows |
|---|---|
| (default) | INFO and up |
| `-q`, `--quiet` | Only warnings and errors |
| `-v`, `--verbose` | Debug messages too (currently few: the work directory opened, the server connected to, the ssh tunnel command, grid packing counts and rotation) |

`-q` and `-v` together are a usage error (exit 2).

Errors and warnings cover:

- ERROR: the review session lost its server, or a server request failed with
  a 500.
- WARNING:
  - an image that cannot be loaded or was left out of every grid;
  - an input skipped by `preprocess`;
  - a torn last line of `review.tsv`;
  - a world-accessible work directory;
  - a refused verdict;
  - a failed server connection;
  - `export --allow-live` overriding a held lock;
  - an `export --output` or `--report` file that could not be given the work
    directory's group;
  - a deprecated remote path (`serve` over HTTPS, `review --remote` or
    `status --remote`).

Only `serve` and `export` log at INFO. `serve` logs one line per request,
with the peer address, method, path without its query string, and status.
`export` logs one line with the count of allowlisted and reported files.

During `preprocess`, log lines appear above the progress bars instead of
through them.

The server never logs tokens or image identities. The [security
model](../reference/security-model.md#the-server-image-review-serve) lists
exactly what is and is not logged.

### Output streams

Logging is separate from command output. Command output is plain text on
stdout: the `preprocess` summary, the `status` tables, `export`'s allowlist
TSV, `serve`'s connection instructions, and the review session's start
and "nothing to review" lines. Error messages of failed commands
(`Error: ...`) go to stderr, with exit 1 or 2.
