# image-review

CLI tool for reviewing medical (DICOM) and general images for burned-in
Protected Health Information (PHI).

Provides a three-phase workflow:

1. **Preprocess** raw DICOM/image files into normalized JPG batches
2. **Review** images interactively in a fullscreen viewer (single or grid mode)
3. **Status** reporting on review progress

## Installation

Requires Python >= 3.12.

```bash
pip install .
```

## Quick Start

```bash
# Preprocess a directory of DICOMs or a ZIP archive
image-review preprocess /path/to/dicoms/ --work-dir ./review_work

# Pass 1: grid triage — quickly mark entire grids CLEAN or DIRTY
image-review review --mode grid

# Pass 2: single review — inspect only the DIRTY images individually
image-review review --mode single

# Check progress
image-review status
```

## Commands

### `image-review preprocess`

```
image-review preprocess SOURCE [SOURCE ...] [--batch-size N]
                                            [--work-dir DIR]
                                            [--colormap NAME]
```

Accepts ZIP files, directories, or individual image files. DICOM images are
normalized with adaptive histogram equalization to enhance local contrast
and a configurable colormap. Non-DICOM images are passed through (with
the same contrast enhancement applied to grayscale). Output is organized into
batch subdirectories with a `manifest.tsv` index.

### `image-review review`

```
image-review review [--mode {single,grid}] [--pass N]
                    [--batch BATCH_ID]     [--work-dir DIR]
```

Opens a fullscreen interactive session. In **grid mode**, images are
bin-packed into composite grids for fast triage. In **single mode**, images
are shown one at a time for detailed inspection.

| Key | Action |
|-----|--------|
| `c` | Mark CLEAN |
| `d` | Mark DIRTY |
| Left / Right | Navigate |
| Space | Toggle autoplay |
| `m` | Switch mode (single/grid) |
| `h` | Help screen |
| `w` | Toggle fullscreen |
| `q` / Escape | Quit |

Xbox-style controllers are also supported (see help screen for mappings).

### `image-review status`

```
image-review status [--work-dir DIR]
```

Prints overall and per-batch counts of CLEAN / DIRTY / UNREVIEWED images.

## Multi-Pass Workflow

1. **Pass 1** (grid triage): Mark grids CLEAN or DIRTY. Err toward DIRTY.
2. **Pass 2** (single review): Only DIRTY images are shown. Inspect individually.
3. **Pass 3+**: Repeat on the shrinking DIRTY pool until confident.

Sessions are resumable -- quitting saves all progress. The batch and pass
number are auto-detected when not specified.
