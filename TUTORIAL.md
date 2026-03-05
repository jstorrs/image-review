# image-review Tutorial

A CLI tool for reviewing DICOM (and other) images for burned-in PHI.
The workflow has three phases: **preprocess**, **review**, and **status**.

## Installation

```bash
pip install -e .
```

This installs the `image-review` command.

## Quick Start

```bash
# 1. Preprocess your images
image-review preprocess /path/to/dicoms.zip

# 2. Review them interactively
image-review review

# 3. Check your progress
image-review status
```

## Step 1: Preprocess

Preprocessing converts raw DICOM files into optimized JPGs. This is the slow
step -- run it once, then review is fast.

```bash
image-review preprocess SOURCE [SOURCE ...] [options]
```

**Sources** can be:
- ZIP files containing `.dcm`, `.jpg`, `.jpeg`, or `.png` files
- Directories containing `.dcm`, `.jpg`, or `.png` files
- Individual image files

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--batch-size` | 300 | Number of images per batch |
| `--work-dir` | `./review_work` | Where to write preprocessed output |
| `--colormap` | `inferno` | Matplotlib colormap for DICOM rendering |

**Examples:**

```bash
# Single ZIP file
image-review preprocess scans.zip

# Multiple sources
image-review preprocess batch1.zip batch2.zip /path/to/loose_dicoms/

# Smaller batches, different colormap
image-review preprocess scans.zip --batch-size 100 --colormap viridis

# Custom output directory
image-review preprocess scans.zip --work-dir /data/review_session_1
```

**What it produces:**

```
review_work/
  manifest.tsv        # Master list: batch, preprocessed_path, image_id
  review.tsv          # (created later during review)
  batch_001/
    img_00001.jpg      # Individual preprocessed images
    img_00002.jpg
    ...
  batch_002/
    ...
```

The DICOM preprocessing pipeline:
1. Converts to float32, corrects photometric interpretation
2. Removes quantile outliers (1st/99th percentile)
3. Applies adaptive histogram equalization (96 tiles)
4. Strips uniform rows/columns (letterboxing removal)
5. Applies colormap and saves as JPG

Non-DICOM images (JPG/PNG) get adaptive histogram equalization if grayscale, then are saved directly.

## Step 2: Review

Open an interactive fullscreen viewer to classify images.

```bash
image-review review [options]
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--mode` | `single` | `single` (one image at a time) or `grid` (packed grids) |
| `--pass` | auto | Pass number (auto-detected if omitted) |
| `--batch` | all | Restrict review to a specific batch (e.g., `batch_001`) |
| `--filter` | `unreviewed` | Which images to show: `unreviewed`, `clean`, or `all` |
| `--work-dir` | `./review_work` | Work directory from preprocessing |

### Review Modes

**Single mode** shows one preprocessed image at a time, filling the screen.
Best for careful inspection of flagged images.

```bash
image-review review --mode single
```

**Grid mode** packs images into grid canvases at review time, sized to your
screen resolution. Each grid contains many images. Best for rapid first-pass
scanning -- you can review hundreds of images per minute.

```bash
image-review review --mode grid
```

### Controls

The viewer accepts keyboard and gamepad input:

| Action | Key | Gamepad |
|--------|-----|---------|
| Mark CLEAN | `c` | Button 1 |
| Mark DIRTY | `d` | Button 3 |
| Next image | `Right` | Hat right |
| Previous image | `Left` | Hat left |
| Next todo item | `n` | -- |
| Toggle todo-only navigation | `u` | -- |
| Autoplay (auto-advance) | `Space` | -- |
| Switch mode (single/grid) | `m` | -- |
| Toggle fullscreen | `w` | -- |
| Help screen | `h` | -- |
| Quit (saves automatically) | `q` or `Esc` | Button 7 |

**Status bar** at the bottom of the screen:
- **Green** = CLEAN
- **Red** = DIRTY
- **Gray** = UNREVIEWED

After marking an image, the viewer auto-advances to the next image after a
short delay (200ms). Images are shuffled at review time to counter attention
fatigue.

### Multi-Pass Workflow

The intended workflow uses repeated passes to build confidence:

**Pass 1** -- Start with grid mode for rapid triage:
```bash
image-review review --mode grid
```
Mark grids as CLEAN if every image in the grid looks clean. Mark DIRTY if
any image is suspicious. When you mark a grid, all constituent images get
that status. This is the fast pass -- err on the side of marking DIRTY.

**Pass 2** -- Switch to single mode for the remaining DIRTY images:
```bash
image-review review --mode single --pass 2
```
Only images marked DIRTY in pass 1 are shown. Review each one individually.
Mark obviously clean ones as CLEAN, leave the rest DIRTY.

**Pass 3+** -- Repeat until confident:
```bash
image-review review --mode single
```
Each subsequent pass shows only the remaining DIRTY images. The pool
shrinks with each pass.

If you omit `--pass`, the tool auto-detects the next pass number.

### Resuming a Session

If you quit mid-session (`q`/`Esc`), your progress is saved immediately.
Re-running the same command picks up where you left off -- already-reviewed
images are skipped.

```bash
# Quit partway through pass 1
image-review review --mode grid
# ... review some, press q ...

# Resume -- only unreviewed images are shown
image-review review --mode grid
```

### Reviewing a Specific Batch

To focus on a single batch:
```bash
image-review review --mode single --batch batch_003
```

### Filtering by Status

By default, only unreviewed images are shown. Use `--filter` to change this:

```bash
# Re-examine images previously marked CLEAN (e.g., to re-mark as DIRTY)
image-review review --filter clean

# Show all images regardless of status
image-review review --filter all
```

With `--filter clean`, the "todo" counter tracks how many CLEAN images remain
(haven't been re-marked yet). With `--filter all`, "todo" tracks unreviewed
images. The `n` key jumps to the next todo item and `u` toggles todo-only
navigation in all filter modes.

## Step 3: Status

Check review progress at any time:

```bash
image-review status [--work-dir ./review_work]
```

**Example output:**

```
Overall: 40320 images (pass 2)
  CLEAN:       38100
  DIRTY:         820
  UNREVIEWED:   1400

Batch            Total  Clean  Dirty  Unrev
---------------------------------------------
batch_001          300    290      8      2
batch_002          300    285     12      3
batch_003          300    280     15      5
...

Current pass: 2
```

## Full Workflow Example

```bash
# Preprocess a large dataset
image-review preprocess \
  /data/site_a.zip \
  /data/site_b.zip \
  /data/site_c/ \
  --batch-size 500 \
  --output-dir ./phi_review

# Check initial state -- everything should be UNREVIEWED
image-review status --work-dir ./phi_review

# Pass 1: rapid grid triage
image-review review --mode grid --work-dir ./phi_review

# Check progress
image-review status --work-dir ./phi_review

# Pass 2: single-image review of remaining DIRTY
image-review review --mode single --work-dir ./phi_review

# Pass 3: final review of stubborn cases
image-review review --mode single --work-dir ./phi_review

# Final status
image-review status --work-dir ./phi_review
```

## Work Directory Files

All state lives in the work directory (default `./review_work`):

| File | Format | Description |
|------|--------|-------------|
| `manifest.tsv` | TSV | Master image list (batch, preprocessed_path, image_id) |
| `review.tsv` | TSV | Review decisions (image_id, batch, status, pass, timestamp) |
| `batch_NNN/img_NNNNN.jpg` | JPG | Preprocessed individual images |

`review.tsv` is written atomically (temp file + rename) after every rating
action, so it is safe to kill the process at any time without data loss.

## Tips

- **Grid mode first**: Grids are packed at review time to fit your screen,
  so each grid contains as many images as possible. Marking a grid CLEAN
  clears all of them at once. Reserve single mode for the DIRTY remainder.
- **Autoplay**: Press `Space` to start auto-advancing through images at
  500ms intervals. Press any key to stop. Useful for a quick visual scan.
- **Gamepad**: A game controller makes long review sessions more
  comfortable. Map CLEAN/DIRTY to face buttons and navigate with the d-pad.
- **Batch size**: Larger batches mean fewer but denser grids. The default
  (300) works well for typical DICOM series. Reduce for very large images.
- **Colormap**: `inferno` (default) provides good contrast for medical
  images. Try `gray` for a more traditional radiological look, or `viridis`
  for general-purpose use.
