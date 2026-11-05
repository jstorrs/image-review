# Local review

A walk-through of a whole review on one machine: **preprocess**, **review**
in your browser, **status** and **export**. Each command's options, keys and
file formats are on its own page; this tutorial links to them.

On one machine you need only a plain install and a browser (see
[Installation](../install.md)). To review images that stay on a cluster from
your laptop, see [Browser review over SSH](browser-review.md) instead.

## Step 1: Preprocess

Preprocessing converts raw DICOM files into optimized JPGs. This is the slow
step -- run it once, then review is fast.

```bash
image-review preprocess SOURCE [SOURCE ...] [options]
```

**Sources** can be:

- Directories, searched recursively (including ZIP files inside them)
- ZIP files
- Individual DICOM, image or ZIP files

Inputs are recognized by their content, not their file name. The inputs it
recognizes, the options and [examples](../commands/preprocess.md#examples)
are in [`image-review preprocess`](../commands/preprocess.md).

`preprocess` writes a new work directory (default `./review_work`); its files
are described in [Work directory](../reference/work-directory.md). If anything
failed, `preprocess` exits with status 1 so scripts notice. Inspect
`skipped.tsv` -- those inputs will **not** be reviewed; see
[Skipped inputs](../commands/preprocess.md#skipped-inputs).

## Step 2: Review

Start the review server and open the page:

```bash
image-review review --work-dir ./review_work
```

`review` serves the browser page on `127.0.0.1` and opens it in your browser.
It prints the URL, which holds an access token: treat it like a password.
Leave `review` running while you review, and stop it with Ctrl-C. If the
browser shows an access error (as Ubuntu's snap Firefox does), open the
printed URL instead.

- On a machine without a display, `review` opens nothing: open the printed
  URL in a browser on that machine yourself.
- Over ssh, stop `review` and use [`serve`](../commands/serve.md) from your
  laptop instead. The URL is on the remote machine's loopback, and the server
  refuses requests through an ssh forward from another port, whose `Host`
  names your laptop's port.

The options, the output and why the browser is opened through a file are in
[`image-review review`](../commands/review.md).

Use `review` on a machine only you log in to. On a shared host, review from a
browser on your laptop with `serve` instead: a browser on the shared host is
exposed to its other users either way. See
[Local review on loopback](../reference/security-model.md#local-review-on-loopback).

### The page

The page first asks "Who is reviewing?": type your name and press Enter. It
is recorded with every verdict. The page then shows the current pass's todo
images one at a time. `c` marks an image CLEAN, `d` DIRTY, Left/Right move,
`z` undoes your latest mark and `?` lists every key. `m` switches to grid
mode, where one verdict covers a whole grid, `s` back to single mode, and `b`
moves to the next batch in grid mode. Your marks are saved as you make them.
[The browser page](../commands/browser.md) describes the keys, the bar, grid
mode and every screen and message.

### Multi-pass workflow

The intended workflow, grid triage in pass 1 and then single-image review of
the images marked DIRTY, pass after pass, is in
[Multi-pass workflow](browser-review.md#multi-pass-workflow). The page
reviews the current pass (see [Passes](../commands/browser.md#passes)). When
it says the pass has nothing left to review, press Reconnect (`r`): the
page loads the next pass from the same `review`, with no restart.

To re-check images already marked CLEAN, press `f` on the page; see
[Re-checking CLEAN images](../commands/browser.md#re-checking-clean-images).

### Resuming

Your marks are saved as you make them, so you can stop at any point. Running
`review` again shows only what is still to review.

## Step 3: Status

Check review progress at any time:

```bash
image-review status [--work-dir ./review_work]
```

[`image-review status`](../commands/status.md) shows an example report. To
check from a script whether every image has a verdict, use `--check`; see
[Checking from a script](../commands/status.md#checking-from-a-script).

## Step 4: Export

The deliverable of a study is the list of source files that may be released.
`export` writes it as an **allowlist**: only the files reviewed CLEAN, each
with its path and SHA-256. Everything else is denied by default.

```bash
image-review export --work-dir ./review_work --output allowlist.tsv --report report.tsv
image-review export --work-dir ./review_work --output allowlist.tsv   # the allowlist alone, without a report
```

**Release a file only if both its path and its SHA-256 match a row of the
allowlist.** Anything not listed is denied: DIRTY, unreviewed, failed and
ignored files, a file holding content its review image does not show (an
embedded icon or thumbnail, say), a file whose bytes have changed, and a ZIP container as a whole
(its entries are listed one by one, as `<zip>::<entry>`). The report is for
audit and follow-up, e.g. what is left to review; never use it to choose what
to release (for example "everything that is not DIRTY").

Sample files, the columns and when export refuses are in
[`image-review export`](../commands/export.md).

## Full workflow example

```bash
# Preprocess a large dataset
image-review preprocess \
  /data/site_a.zip \
  /data/site_b.zip \
  /data/site_c/ \
  --batch-size 500 \
  --work-dir ./phi_review

# Check initial state -- everything should be UNREVIEWED
image-review status --work-dir ./phi_review

# Pass 1: grid triage in the browser (press m); press r once the page says the pass is done,
# for pass 2, 3, ...: single-image review of the images flagged DIRTY in the pass before.
# Ctrl-C once you are finished.
image-review review --work-dir ./phi_review

# Final status
image-review status --work-dir ./phi_review

# The allowlist of releasable files, and a report of the rest
image-review export --work-dir ./phi_review --output ./phi_review_allowlist.tsv --report ./phi_review_report.tsv
```

## Tips

- **Grid mode first**: Grids are packed to fit the page's image area, so each
  grid contains as many images as possible. Marking a grid CLEAN clears all
  of them at once. Reserve single mode for the DIRTY remainder (grid mode
  skips DIRTY and FLAGGED images).
- **Batch size**: Larger batches mean fewer but denser grids. Reduce
  `--batch-size` for very large images.
- **Colormap**: Try `--colormap gray` for a more traditional radiological
  look, or `viridis` for general-purpose use. Both options are in
  [`image-review preprocess`](../commands/preprocess.md).
