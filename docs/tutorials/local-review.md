# Local review

A walk-through of a whole review on one machine: **preprocess**, **review**,
**status** and **export**. Each command's options, keys and file formats are
on its own page; this tutorial links to them.

On one machine you need the `preprocess`, `codecs` and `viewer` extras:
`pip install 'image-review[all]'` (see [Installation](../install.md)).

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

Open an interactive fullscreen viewer to classify images.

```bash
image-review review [options]
```

The options are in [`image-review review`](../commands/review.md).

### Review modes

**Single mode** shows one preprocessed image at a time, filling the screen.
Best for careful inspection of flagged images.

```bash
image-review review --mode single
```

**Grid mode** packs images into grid canvases at review time, sized to your
screen resolution. Each grid contains many images. Best for rapid first-pass
scanning -- you can review hundreds of images per minute. By default, images
may be rotated 90° when that needs fewer grids (`--rotate auto`, the default);
use `--rotate always` or `--rotate never` to force it either way, or press `M`
during review to switch to grid mode without rotation.

```bash
image-review review --mode grid
```

### Controls

Press `c` to mark the image or grid on screen CLEAN and `d` to mark it DIRTY,
Left/Right to move, `z` to undo your last mark and `q` (or Escape) to quit;
progress is saved automatically. The status bar shows each image's status:
green for CLEAN, red for DIRTY, gray for UNREVIEWED and orange for FLAGGED
(marked DIRTY in an earlier pass; needs a verdict in this one). Every key,
the gamepad buttons, undo and the end-of-batch screen are described in
[Keys and gamepad](../commands/review.md#keys-and-gamepad).

### Multi-pass workflow

The intended workflow uses repeated passes to build confidence:

1. **Pass 1** (grid triage): mark grids CLEAN or DIRTY. Err toward DIRTY.

   ```bash
   image-review review --mode grid
   ```

   Mark grids as CLEAN if every image in the grid looks clean. Mark DIRTY if
   any image is suspicious. When you mark a grid, all constituent images get
   that status. This is the fast pass.

2. **Pass 2** (single review): only images marked DIRTY in pass 1 are shown,
   as FLAGGED (orange status bar). Inspect them individually.

   ```bash
   image-review review --mode single --pass 2
   ```

   Mark obviously clean ones as CLEAN, mark the rest DIRTY again. Grid mode
   skips FLAGGED and DIRTY images, so a grid keypress cannot clear them.
   Review flagged images in single mode.

3. **Pass 3+**: repeat on the shrinking DIRTY pool until confident.

   ```bash
   image-review review --mode single
   ```

   Each subsequent pass shows only the images still DIRTY from the previous
   pass (FLAGGED in the new pass).

The batch and pass number are auto-detected when not specified. Press `b` at
the end of a batch to move on to the next one, and into the next pass once
this one is done (not with `--batch`, which keeps you in that batch); see
[End of a batch](../commands/review.md#end-of-a-batch).

### Resuming a session

Sessions are resumable: if you quit mid-session (`q`/`Esc`), your progress is
saved immediately. Re-running the same command picks up where you left off --
already-reviewed images are skipped.

```bash
# Quit partway through pass 1
image-review review --mode grid
# ... review some, press q ...

# Resume -- only unreviewed images are shown
image-review review --mode grid
```

### Reviewing a specific batch

To focus on a single batch:

```bash
image-review review --mode single --batch batch_003
```

`--batch` restricts the whole session to that batch: pressing `b` at the end
of the list reloads it while it has todo images, and never moves on to another.

### Filtering by status

By default, only images still to do are shown (UNREVIEWED, plus FLAGGED in
pass 2 and later). Use `--filter` to change this:

```bash
# Re-examine images previously marked CLEAN (e.g., to re-mark as DIRTY)
image-review review --filter clean

# Show all images regardless of status
image-review review --filter all
```

What counts as todo under each filter is described in
[Todo images](../commands/review.md#todo-images).

## Step 3: Status

Check review progress at any time:

```bash
image-review status [--work-dir ./review_work]
image-review status --remote 'ir://...' [--via user@login-node]
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
ignored files, a file whose bytes have changed, and a ZIP container as a whole
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

# Pass 1: rapid grid triage
image-review review --mode grid --work-dir ./phi_review

# Check progress
image-review status --work-dir ./phi_review

# Pass 2: single-image review of the images flagged DIRTY in pass 1
image-review review --mode single --work-dir ./phi_review

# Pass 3: final review of stubborn cases
image-review review --mode single --work-dir ./phi_review

# Final status
image-review status --work-dir ./phi_review

# The allowlist of releasable files, and a report of the rest
image-review export --work-dir ./phi_review --output ./phi_review_allowlist.tsv --report ./phi_review_report.tsv
```

## Tips

- **Grid mode first**: Grids are packed at review time to fit your screen,
  so each grid contains as many images as possible. Marking a grid CLEAN
  clears all of them at once. Reserve single mode for the DIRTY remainder
  (grid mode skips DIRTY and FLAGGED images).
- **Autoplay**: Press `Space` to start auto-advancing through images at
  500ms intervals. Press any key to stop (the key still does its usual job). Useful for a quick visual scan.
- **Gamepad**: A game controller makes long review sessions more
  comfortable: B marks CLEAN, Y marks DIRTY and the D-pad navigates.
- **Batch size**: Larger batches mean fewer but denser grids. The default
  (300) works well for typical DICOM series. Reduce for very large images.
- **Colormap**: `inferno` (default) provides good contrast for medical
  images. Try `gray` for a more traditional radiological look, or `viridis`
  for general-purpose use.
