# image-review Specification

## Purpose

`image-review` is a CLI tool for reviewing medical (DICOM) and general images
for burned-in Protected Health Information (PHI). It provides a three-phase
workflow: **preprocess** raw images into normalized JPGs, **review** them
interactively in a fullscreen viewer, and report **status** on review progress.

## Requirements

- Python >= 3.12
- Dependencies: matplotlib, numpy, pydicom, scikit-image, rectpack, tqdm,
  pygame-ce

## Architecture

```
cli.py              Command-line entry point, argument parsing
preprocess.py       DICOM/image loading and normalization
controller.py       Review session orchestration and event loop
viewer.py           Fullscreen pygame display
grid_packer.py      Review-time bin-packing of images into grids
review_db.py        Persistent review state (review.tsv)
util.py             Shared utilities (surface loading)
```

## CLI Interface

Entry point: `image-review` (mapped to `image_review.cli:main`).

### `image-review preprocess`

```
image-review preprocess SOURCE [SOURCE ...] [--batch-size N]
                                            [--work-dir DIR]
                                            [--colormap NAME]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `SOURCE` | (required) | One or more ZIP files, directories, or image files |
| `--batch-size` | 300 | Maximum images per batch subdirectory |
| `--work-dir` | `./review_work` | Work directory for all output (alias: `--output-dir`) |
| `--colormap` | `inferno` | Matplotlib colormap applied to DICOM grayscale |

**Source loading** dispatches by type:

| Source type | Behavior |
|-------------|----------|
| `.zip` | Scans for `*.dcm`, `*.jpg`, `*.jpeg`, `*.png` entries |
| Directory | Globs `**/*.dcm`, then `**/*.jpg`, `**/*.jpeg`, `**/*.png` |
| Single file | Reads as DICOM (`.dcm`) or passes through (other extensions) |

**Image IDs** are fully-resolved absolute paths derived from the source:
- ZIP: `{absolute_zip_path}::{filename}`
- Directory: fully-resolved absolute path to each file
- Single file: fully-resolved absolute path

**DICOM preprocessing pipeline** (`preprocess_dicom`):

1. Extract pixel array, convert to float32
2. Correct photometric interpretation (invert MONOCHROME1)
3. Clip to robust intensity range (1st-99th percentile with 2% margin)
4. Apply CLAHE (adaptive histogram equalization, 96-tile grid)
5. Strip uniform rows/columns (`compress_image` -- removes letterboxing)
6. Apply colormap, save as 8-bit RGB JPG

**Non-DICOM preprocessing** (`preprocess_non_dicom` / `preprocess_non_dicom_bytes`):
- Read image from path or in-memory bytes (the bytes variant is used for
  non-DICOM files inside ZIP archives); if grayscale, apply CLAHE and
  convert to 8-bit
- Color images are saved as-is

**Error handling**: If a single file fails to load or preprocess (corrupt
DICOM, unreadable image, etc.), a warning is logged to stderr and the file
is skipped. This prevents one bad file from aborting the entire batch.

**Batching**: Sources are streamed through a generator. Each batch of up to
`batch_size` images is written to a `batch_NNN/` subdirectory. This bounds
peak memory usage regardless of dataset size.

**Output**: A single `manifest.tsv` file.

### `image-review review`

```
image-review review [--mode {single,grid}]            [--pass N]
                    [--batch BATCH_ID]                 [--work-dir DIR]
                    [--filter {unreviewed,clean,all}]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--mode` | `single` | `single` = one image at a time; `grid` = packed grids |
| `--pass` | auto-detected | Review pass number |
| `--batch` | all | Restrict to a named batch (e.g. `batch_001`) |
| `--filter` | `unreviewed` | Which images to show: `unreviewed`, `clean`, or `all` |
| `--work-dir` | `./review_work` | Work directory from preprocessing |

Initializes pygame, creates a `ReviewSession`, runs the event loop, then
shuts down pygame.

### `image-review status`

```
image-review status [--work-dir DIR]
```

Prints overall and per-batch counts of CLEAN / DIRTY / UNREVIEWED images
(pass-aware), plus the current pass number.

## Data Files

All state lives in the work directory.

### `manifest.tsv`

Written by `preprocess`. Tab-separated, one row per image.

| Column | Description |
|--------|-------------|
| `batch` | Batch subdirectory name (e.g. `batch_001`) |
| `preprocessed_path` | Relative path to the JPG within the work directory |
| `image_id` | Unique string identifier (fully-resolved absolute path) |

### `review.tsv`

Written atomically by `ReviewDB` after every mark action (temp file + `os.replace`).

| Column | Description |
|--------|-------------|
| `image_id` | Matches `manifest.tsv` |
| `batch` | Batch the image belongs to |
| `status` | `CLEAN`, `DIRTY`, or `UNREVIEWED` |
| `pass_number` | Integer pass in which this decision was made |
| `timestamp` | ISO 8601 UTC timestamp |

Only images that have been explicitly marked appear in `review.tsv`. An image
absent from `review.tsv` is implicitly `UNREVIEWED`.

### `batch_NNN/img_NNNNN.jpg`

Preprocessed individual image files. Numbered sequentially within each batch.

## Review Session (`controller.py`)

### Initialization

1. Load `manifest.tsv` into a list of dicts
2. Open `ReviewDB` (loads existing `review.tsv` if present)
3. Auto-detect pass number if not specified:
   - Pass 1 if any image has never been reviewed
   - Otherwise stays on max(pass_number) if it has unfinished work,
     or advances to max(pass_number) + 1
4. Auto-select batch if not specified: pick the first batch (sorted
   alphabetically) that has images matching the status filter
5. Determine review items based on mode

### Single Mode

- Query `ReviewDB.images_by_status()` for the current pass/batch/filter
- Shuffle the resulting manifest rows
- Each item is a manifest dict; surfaces are loaded from disk on display

### Grid Mode

- Display a "Computing grids..." message while packing
- Read screen dimensions, subtract the 50px status bar height
- Query `ReviewDB.images_by_status()` for the current pass/batch/filter
- Pass the review items to `pack_into_grids()` with the screen dimensions
- Convert the returned `GridSpec` list into item dicts with `surface`,
  `image_ids`, and `batch` keys
- Shuffle the grid items, then sort by image count (largest grids first)

When a grid is marked CLEAN or DIRTY, all constituent `image_ids` receive
that status via `ReviewDB.mark_many()`.

### Grid Status Derivation

A grid's aggregate status is derived from its member images:
- All CLEAN -> CLEAN
- Any UNREVIEWED -> UNREVIEWED
- Otherwise -> DIRTY

### Event Loop

The session runs a pygame event loop processing:

| Event | Action |
|-------|--------|
| `c` key / Button 1 | Mark current item CLEAN |
| `d` key / Button 3 | Mark current item DIRTY |
| Right arrow / Hat right | Next item |
| Left arrow / Hat left | Previous item |
| Space | Toggle autoplay (500ms auto-advance) |
| `w` key | Toggle fullscreen |
| `n` key | Jump to next todo item |
| `u` key | Toggle todo-only navigation |
| `m` key | Switch between single/grid mode |
| `h` key | Show help/splash screen |
| `q` / Escape / Button 7 | Quit |
| Window resize | Refit current image |
| Joystick added/removed | Hot-plug handling |

After marking, the viewer auto-advances to the next item after 200ms.
Navigation stops at list boundaries with an "End of list" message.

Marking, navigation, `n`, `Space`, and `m` cancel autoplay. The display
only redraws when a dirty flag is set, to minimize CPU usage.

## Grid Packer (`grid_packer.py`)

### `GridSpec` Dataclass

| Field | Type | Description |
|-------|------|-------------|
| `surface` | `pg.Surface` | Composited grid image, ready for display |
| `image_ids` | `list[str]` | IDs of all images packed into this grid |
| `batch` | `str` | Batch of the first packed image |

### `pack_into_grids(items, work_dir, grid_w, grid_h) -> list[GridSpec]`

1. **Load**: Load all image surfaces upfront via `util.load_surface` and read
   dimensions from `surface.get_size()`. This avoids opening each file twice.
2. **Pack**: Create a `rectpack` packer with `(grid_w, grid_h)` bins
   (unlimited bin count). Add each image as a rect.
3. **Composite**: For each bin, create a black `pg.Surface(grid_w, grid_h)`.
   Blit each pre-loaded surface at the packed position. If rectpack rotated
   the rect (packed size differs from original), apply
   `pg.transform.rotate(-90)` before blitting.
4. **Overflow**: Any image too large to fit in any bin becomes a single-image
   `GridSpec` with its original surface.

## Image Viewer (`viewer.py`)

### `ImageViewer`

Opens a fullscreen, resizable pygame window with hidden cursor.

**Layout**: Image content fills the screen above a 50px status bar at the
bottom edge.

**Scaling**: Images are aspect-ratio-scaled to fit the available content area
(`screen_height - 50px` by `screen_width`), centered both horizontally and
vertically within the content area. Uses `pg.transform.smoothscale`.

**Status bar**: A colored rectangle spanning the full width at the bottom.
Color encodes review status (green=CLEAN, red=DIRTY, gray=UNREVIEWED).
The image name is rendered right-aligned, position info is centered.

**Font**: DejaVu Sans 36pt bold, dark gray (`Color(64,64,64)`). Bundled in
the `fonts/` subdirectory for cross-platform consistency. The help screen
uses DejaVu Sans Mono 24pt.

### Interface

| Method | Description |
|--------|-------------|
| `set_image(surface, name, status, info)` | Set new image; triggers resize/scale |
| `set_status(status)` | Update status bar color without changing image |
| `resize()` | Recalculate scaling for current screen size |
| `refresh()` | Render frame: background, status bar, text, scaled image |
| `show_splash(lines, footer, mode)` | Render centered splash/help overlay |
| `show_message(text)` | Render centered text message (e.g. loading indicator) |

## Review Database (`review_db.py`)

### `ReviewDB`

In-memory dict keyed by `image_id`, backed by `review.tsv` on disk.

**Persistence**: Every mutation (`mark`, `mark_many`) writes the full state
atomically via `tempfile.mkstemp` + `os.replace`. Safe to kill the process
at any point.

### Key Methods

| Method | Description |
|--------|-------------|
| `mark(image_id, batch, status, pass_number)` | Record a single review decision |
| `mark_many(image_ids, batch, status, pass_number)` | Record decisions for multiple images (same timestamp) |
| `get_status(image_id, current_pass) -> str` | Returns pass-aware status or `"UNREVIEWED"` if absent |
| `images_by_status(manifest, pass_number, status_filter?, batch?) -> list[dict]` | Filter manifest by status: `"unreviewed"`, `"clean"`, or `"all"` |
| `current_pass(manifest) -> int` | Auto-detect pass number |
| `summary(manifest, pass_number) -> dict` | Pass-aware count of CLEAN/DIRTY/UNREVIEWED/total |
| `batch_summary(manifest, pass_number) -> dict` | Pass-aware per-batch status counts |

### Pass Logic

| Pass | Shows |
|------|-------|
| 1 | All UNREVIEWED images |
| N > 1 | Images marked DIRTY in a prior pass (treated as UNREVIEWED for the current pass) plus any still-UNREVIEWED images |

`current_pass` returns 1 if any image has never been reviewed. Otherwise it
returns `max(pass_number)` if that pass still has UNREVIEWED work remaining,
or `max(pass_number) + 1` if the pass is fully complete.

## Multi-Pass Review Workflow

1. **Pass 1 (grid triage)**: `--mode grid`. Mark grids CLEAN or DIRTY.
   Each grid mark applies to all constituent images. Err toward DIRTY.
2. **Pass 2 (single review)**: `--mode single`. Only DIRTY images from
   pass 1 are shown. Inspect individually.
3. **Pass 3+**: Repeat single-mode review on the shrinking DIRTY pool
   until confident.

Sessions are resumable: quitting mid-session saves all progress. Re-running
the same command shows only remaining unreviewed (pass 1) or dirty (pass 2+)
images.