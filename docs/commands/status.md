# `image-review status`

```
image-review status [--check] [--work-dir DIR | --remote CONNECTION_STRING [--via DESTINATION]]
```

Prints overall and per-batch counts of CLEAN / DIRTY / UNREVIEWED / FLAGGED
images. FLAGGED means marked DIRTY in an earlier pass and not yet re-reviewed
in the current one, so after pass 1 completes its DIRTY images show as
FLAGGED:

```
Overall: 6 images (pass 2)
  CLEAN:           4
  DIRTY:           0
  UNREVIEWED:      0
  FLAGGED:         2
```

| Option | Default | Description |
|--------|---------|-------------|
| `--check` | off | Set the exit status by whether the review is finished (see below) |
| `--work-dir` | `./review_work` | Work directory containing preprocessed data |
| `--remote` | `$IMAGE_REVIEW_REMOTE` | As for `review` |
| `--via` | `$IMAGE_REVIEW_VIA` | As for `review` |

If `preprocess` skipped any inputs, `status` also prints
`Skipped during preprocess: F failed, I ignored (see skipped.tsv in the work dir)`.
Failed inputs were never shown, so they are not part of the counts above.

With `--check`, the report is printed as usual and the exit status says
whether the review is finished, i.e. every image has a verdict: 1 if any
image is UNREVIEWED or any input `failed` to preprocess; 0 otherwise (ignored
inputs do not count). FLAGGED images have a DIRTY verdict from an earlier
pass, so they count as decided: re-review passes are optional. They still
show in the report, so a second pass remains available. Without `--check`,
`status` exits 0.
