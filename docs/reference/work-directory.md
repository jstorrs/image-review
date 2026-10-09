# Work directory

All state lives in the work directory (default `./review_work`), which
`preprocess` creates.

```
review_work/
  manifest.tsv        # Master list: batch, preprocessed_path, image_id, source_sha256, jpeg_sha256
  skipped.tsv         # Inputs that produced no image: image_id, kind, reason
  preprocess.json     # How it was made: versions, sources, parameters, counts
  review.tsv          # (created later during review)
  batch_001/
    img_00001.jpg      # Individual preprocessed images
    img_00002.jpg
    ...
  batch_002/
    ...
```

| File | Format | Description |
|------|--------|-------------|
| `manifest.tsv` | TSV | Master image list (batch, preprocessed_path, image_id, source_sha256, jpeg_sha256). Work dirs from older versions have only the first three columns and still load |
| `skipped.tsv` | TSV | Inputs that produced no image (image_id, kind, reason) |
| `preprocess.json` | JSON | How the work directory was made: tool and library versions, time (UTC), resolved sources, parameters (batch size, colormap, contrast settings, JPEG quality, access, jobs), and counts. Holds source paths; never served |
| `review.tsv` | TSV | Review decisions (image_id, batch, status, pass_number, timestamp, reviewer, mode, grid_size, tool_version) |
| `review.lock` | JSON | Present while a writer (`review` or `serve`) has the work directory open; names the holder |
| `batch_NNN/img_NNNNN.jpg` | JPG | Preprocessed individual images |

The exact formats are in the specification's [Data Files](specification.md#data-files).

## `review.tsv`

`review.tsv` is an append-only log: every rating action appends a line per
image and syncs it to disk, and when an image appears more than once its last
line wins. It is safe to kill the process at any time. A last line cut short by
a crash is skipped with a warning and dropped on the next rating, unless all
of its fields made it to disk: then it is kept, and only its last field
(`tool_version`, or `timestamp` in an old five-column file) may be cut short
or empty.

Each line also records who rated and how; the columns, the lines an undo
(`z`) appends and the upgrade of an older file are described in
[Recorded verdicts](../commands/review.md#recorded-verdicts). If the upgrade
is interrupted, a `.review.tsv.*.tmp` file may be left in the
work directory: the tool ignores it, but it contains source paths, so delete
it.
