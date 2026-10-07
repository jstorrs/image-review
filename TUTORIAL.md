# image-review Tutorial

A CLI tool for reviewing DICOM (and other) images for burned-in PHI.
The workflow has three phases: **preprocess**, **review**, and **status**.

## Installation

```bash
pip install -e '.[all]'
```

This installs the `image-review` command with every optional part: the
`preprocess` extra, the `codecs` extra that decodes compressed DICOMs
(`python-gdcm`, `pylibjpeg`, `pylibjpeg-openjpeg`; wheels for CPython
3.12/3.13 on Linux x86_64/aarch64, macOS and Windows) and the `viewer` extra
for `review`. From a wheel or package index, `pip install 'image-review[all]'`.

Each machine can instead install only what its commands need:

| Install | For |
|---|---|
| `pip install -e .` (core) | `serve`, `status`, `export` (e.g. a cluster node that only serves) |
| `pip install -e '.[preprocess,codecs]'` | `preprocess` (on the cluster) |
| `pip install -e '.[viewer]'` | `review`, including `review --remote` (on your laptop) |

On a cluster without root, install into a virtual environment; see
[Cluster install without root](README.md#installation) in the README.

A command whose extra is missing stops with
`this command needs the <extra> extra: pip install 'image-review[<extra>]'`.
`preprocess` without the `codecs` extra still runs, but compressed DICOMs that
pydicom cannot decode by itself or through Pillow (e.g. JPEG Lossless, JPEG-LS)
are listed in `skipped.tsv` as failed; install the codecs wherever you
preprocess DICOMs.

A DICOM that still cannot be decoded (e.g. 12-bit JPEG Extended) is listed in
`skipped.tsv` as `cannot decode <transfer syntax>: ...`. `--via` (HPC
tunnelling, below) also needs an OpenSSH client on the machine you run it on
(built into macOS, Linux and Windows 10+).

## Quick Start

```bash
# 1. Preprocess your images
image-review preprocess /path/to/dicoms.zip

# 2. Review them interactively
image-review review

# 3. Check your progress
image-review status

# 4. Export the allowlist of files that may be released (and a report of the rest)
image-review export --output allowlist.tsv --report report.tsv
```

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

Inputs are recognized by their content, not their file name, so upper-case
extensions (`B.DCM`, `e.JPG`), `.jpeg`, `.dicom` and `.ima` files, and
extensionless DICOM files such as `IM0001` (common in DICOM exports) are all
picked up. DICOM (with or without the standard 128-byte preamble), PNG,
JPEG, TIFF, BMP, GIF, WebP, JPEG 2000 and PNM are recognized. Word, Excel,
PowerPoint and EPUB files are ZIPs, so images embedded in them are reviewed
too. A ZIP inside a ZIP is not opened, and other archives (`.tar.gz`, `.7z`,
`.rar`, ...) are not supported: extract them first (both are recorded as
failed). Symlinked files are read, but symlinked directories are never
entered, so a link such as `up -> ..` cannot pull in other patients' studies:
a link to an enclosing directory, or into a directory you passed as a SOURCE,
is ignored; any other is recorded as failed with its target, so pass that
target as another SOURCE if you want it reviewed.
The work directory and its `.NAME.partial` staging directory are never read
as input, so `image-review preprocess .` with the default `./review_work` is
safe.

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--batch-size` | 300 | Number of images per batch |
| `--work-dir` | `./review_work` | Where to write preprocessed output; must not exist or be empty |
| `--colormap` | `inferno` | Matplotlib colormap for DICOM rendering |
| `--access` | `private` | `private`: owner only (dirs 0700, files 0600); `group`: the work dir's Unix group too (dirs 2770, files 0660). Never world-readable. Env: `IMAGE_REVIEW_ACCESS` |
| `--allow-skipped` | off | Exit 0 even if some inputs failed (they are still listed in `skipped.tsv`) |
| `--jobs` | `$SLURM_CPUS_PER_TASK`, else the usable CPUs, capped by the cgroup v2 CPU quotas (e.g. a login node's per-user limit, a container's limit) | Worker processes rendering in parallel; `1` renders in the main process. The output is the same for any value |

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

# Render in 8 worker processes (or 1 for the main process only)
image-review preprocess scans.zip --jobs 8
```

The main process holds the raw bytes of up to 2 × N inputs waiting for or
being rendered, and each worker holds one input and its decoded arrays, so if
very large DICOMs run the machine out of memory, lower `--jobs`. A worker that dies fails the
whole run (no work directory is created); re-run with `--jobs 1` to see which
input is the problem.

**Sharing with a team:** the work directory holds PHI, so by default only you
can read it. If several accounts share a study Unix group, use
`--access group`: directories become 2770 (setgid) and files 0660, and new
files inherit the directory's group. The tool never changes groups itself, so
create the work directory under the study's group-owned setgid project
directory, or run `sg <group> -c 'image-review preprocess ... --access group'`.
The work directory's group comes from the parent if it is setgid, else your
current primary group (on clusters where that is site-wide, e.g. `users`, use
one of the two ways above); a pre-created empty work directory's group and mode
are not kept. After the run, `preprocess` prints `Shared with Unix group
'study' (gid N)`. A parent directory's POSIX default ACL can add named
user/group entries (check `getfacl`), but files never get "other" bits. `review`, `serve` and `status` warn if a work directory is
accessible to all users and never change it; fix it with
`chmod -R o-rwx <work dir>`. Only one person should review a work directory at
a time; take turns, or split the study into several work directories.

**Re-running:** `preprocess` never writes into a work directory that already
has content: choose a new `--work-dir` or remove the old one. (Reusing one
would let your earlier CLEAN/DIRTY marks attach to different images.) Output is
built in a hidden staging directory next to it (`.review_work.partial`) and
renamed into place only when the run finishes, so Ctrl-C leaves nothing
behind. If the machine crashed mid-run and `.review_work.partial` is left over,
the next run tells you to remove it; do so and re-run.

**What it produces:**

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

The DICOM preprocessing pipeline for grayscale (MONOCHROME1/2) images; single-frame
colour (RGB, YBR) and palette DICOMs are only converted to 8-bit RGB and
cropped, with no windowing or colormap (overlay planes are drawn in white):
1. Converts to float32, corrects photometric interpretation
2. Compresses intensity outliers (beyond the 1st/99th percentile) into the
   ends of the range instead of clipping them, so bright burned-in text
   stays visible. DICOM overlay planes (annotations stored outside the pixel
   data) are then drawn at maximum brightness
3. Applies adaptive histogram equalization (CLAHE with 96-pixel tiles)
4. Strips uniform rows/columns (letterboxing removal)
5. Applies colormap and saves as JPG

A DICOM with an embedded icon image (a thumbnail stored in the file) gets a
second row in `manifest.tsv` with the image id ending in `#icon`, so any
text burned into the thumbnail is reviewed too. If the icon cannot be
rendered, the main image is still written and `<path>#icon` is listed as
`failed` in `skipped.tsv`. A DICOM whose overlay cannot be decoded is
`failed` as a whole.

Non-DICOM images (JPG/PNG) are decoded by mode (CMYK and palette images are
converted to RGB; 16-bit grayscale keeps its full range), get adaptive
histogram equalization if grayscale, and are saved as RGB JPGs. Some inputs
are shown as several views side by side in one image (still one item to
review):

- **Transparent images**: left is the image composited over mid-gray (so
  anything drawn only in the alpha channel is visible), right is the raw
  image with transparency ignored (so anything hidden under transparent
  pixels is visible). Check both halves.
- **MPO JPEGs** (HDR gain maps, camera previews embedded in a JPEG): every
  embedded image, left to right.

Animated PNGs and other multi-frame rasters are listed in `skipped.tsv` as
unsupported.

Every file the run finds is listed in `manifest.tsv` or `skipped.tsv`. The
`kind` column of `skipped.tsv` is either:

- `failed`: an input that should have been rendered but was not (a corrupt
  file, a file named like an image or a ZIP such as `.jpg` or `.zip` whose
  content is not one, an unsupported DICOM, an unreadable subdirectory, a ZIP
  inside a ZIP, a `.tar.gz` or other non-ZIP archive, a symlinked directory
  outside your sources, or any file you named on the command line that is
  not an image, such as `notes.txt`). These make `preprocess` exit 1.
  An input whose image id would name a different image from another input's
  (a file literally called `scan.dcm#icon` beside a `scan.dcm` with an icon,
  a file called `site.zip::a.png` beside `site.zip`, or a ZIP entry called
  `a.png#2` beside two `a.png` entries) is also `failed`, every one of them,
  with `image_id collides with another input (rename one of them)`: they
  would otherwise share one verdict.
- `ignored`: a file that is not an image (`not an image (unrecognized
  content)`, e.g. a text file; macOS `._name` AppleDouble files and
  `__MACOSX/` entries; a DICOMDIR index; a ZIP with no files), or a symlink
  to a directory that is already being read. These do not
  affect the exit status, but glance at them in case something you expected
  to review is among them.

Only single-frame DICOMs (grayscale, colour or palette) are rendered for now.
Multi-frame DICOMs, DICOM objects without pixel data (structured
reports, encapsulated PDFs), and any file that cannot be read or decoded do
not stop the run: each is recorded in `skipped.tsv` as `failed` with a reason
(for example `unsupported: multi-frame DICOM (3 frames)` or `BadZipFile: File
is not a zip file`). The run ends with a summary such as:

```
Found 1200 inputs: wrote 1195 images in 4 batches; 5 skipped (3 failed, 2 ignored; see review_work/skipped.tsv)
```

If anything failed, `preprocess` exits with status 1 so scripts notice.
Inspect `skipped.tsv` -- those inputs will **not** be reviewed -- and either
fix them or re-run with `--allow-skipped` to accept the result.
`skipped.tsv` contains source paths, so treat it as carefully as
`manifest.tsv`.

## Step 2: Review

Open an interactive fullscreen viewer to classify images.

```bash
image-review review [options]
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--mode` | `single` | `single` (one image at a time) or `grid` (packed grids) |
| `--pass` | auto | Pass number, 1 or more (auto-detected if omitted) |
| `--batch` | first batch with images matching the filter | Restrict review to a specific batch (e.g., `batch_001`), also for `b` at the end of the list; an unknown or empty name is rejected with the list of known batches |
| `--filter` | `unreviewed` | Which images to show: `unreviewed` (images still to do: UNREVIEWED and FLAGGED), `clean`, or `all` |
| `--rotate` | `auto` | Rotate images 90° in grids: `auto` = only when that needs fewer grids, `always`, or `never` |
| `--reviewer` | your login name | Name recorded with each verdict in `review.tsv`, 1-64 printable characters, not all spaces (also `$IMAGE_REVIEW_REVIEWER`). An unauthenticated claim: it is recorded as given, not verified |
| `--work-dir` | `./review_work` | Work directory from preprocessing |
| `--remote` | -- | Review a server started with `image-review serve` instead (also `$IMAGE_REVIEW_REMOTE`); see [Reviewing on an HPC Cluster](#reviewing-on-an-hpc-cluster) |
| `--via` | -- | With `--remote`: reach the server through an SSH tunnel via this login node (also `$IMAGE_REVIEW_VIA`) |

### Review Modes

**Single mode** shows one preprocessed image at a time, filling the screen.
Best for careful inspection of flagged images.

```bash
image-review review --mode single
```

**Grid mode** packs images into grid canvases at review time, sized to your
screen resolution. Each grid contains many images. Best for rapid first-pass
scanning -- you can review hundreds of images per minute. By default, images
may be rotated 90° when that needs fewer grids (`--rotate auto`, the default);
use `--rotate always` or `--rotate never` to force it either way (this replaces
the old `--no-rotate`), or press `M` during review to switch to grid mode
without rotation.

```bash
image-review review --mode grid
```

### Controls

The viewer accepts keyboard and gamepad input. Gamepad buttons are named by
SDL's standard (Xbox-style) layout: A is the bottom face button, B the right,
Y the top, whatever the pad itself prints on them:

| Action | Key | Gamepad |
|--------|-----|---------|
| Mark CLEAN | `c` | B |
| Mark DIRTY | `d` | Y |
| Undo your last mark in this mode | `z` | -- |
| Next batch (end-of-list screen) | `b` | -- |
| Next image | `Right` | D-pad right |
| Previous image | `Left` | D-pad left |
| Next todo item | `n` | -- |
| Toggle todo-only navigation | `u` | -- |
| Autoplay (auto-advance) | `Space` | -- |
| Single mode | `s` | -- |
| Grid mode (`--rotate` policy) | `m` | -- |
| Grid mode (no rotation) | `M` | -- |
| Select display | `w` | -- |
| Toggle fullscreen | `f` | -- |
| Help screen | `h` | -- |
| Continue (help or end-of-list screen) | `Space` | A |
| Quit (saves automatically) | `q` or `Esc` | Start |

B, Y and the D-pad act only while reviewing; Start quits from any screen.
Every gamepad button stops autoplay. Only pads SDL has a game controller
mapping for are supported (most common pads are). For another pad, set
`SDL_GAMECONTROLLERCONFIG` to a mapping line in SDL's format (tools such as
SDL's `controllermap` produce one) before starting `image-review`.

**Status bar** at the bottom of the screen:
- **Green** = CLEAN
- **Red** = DIRTY
- **Gray** = UNREVIEWED
- **Orange** = FLAGGED (marked DIRTY in an earlier pass; needs a verdict in this one)

The status word (CLEAN, DIRTY, ...) is also written at the left end of the bar.

The right end of the bar shows the scale the image is displayed at, e.g. `27%`.
It is red below 100%: text in the image is smaller than in the original, so
look closely. In grid mode it is the smallest image's effective scale, counting
any shrinking done to fit the grid. Resizing the window in grid mode recomputes the grids for the new
size.

After marking an image, the viewer auto-advances to the next image after a
short delay (200ms). Images are shuffled at review time to counter attention
fatigue. A verdict (`c`/`d` or gamepad B/Y) only counts once the image
or grid has been on screen for 200ms: one pressed sooner is ignored, so press
it again once you have looked. Every key and button pressed while grids are
being computed is dropped, including `q`/`Esc` and the arrows.

Pressed the wrong key? `z` undoes your most recent mark, whether it was one
image or a whole grid, and takes you back to that item to mark it again (the
200ms wait applies again). Press `z` repeatedly to step further back. It also
works on the "End of list" screen, so you can undo the last mark of a batch.
`z` only undoes marks you made in this session since the last mode switch
(`s`, `m` or `M`); past those it says "Nothing to undo". The history is kept in
memory and is gone once the program exits. After "Lost connection to server",
`z` and the other keys do nothing; press `q`.

Finished a batch? On the "End of list" screen (in todo-only navigation, "No
todo images remaining", or "No more todo images this way" when todo images are
left in the other direction; Left/Right wrap round to the others), which also
shows how many todo images the batch has left, if any, press `b` to move on to the next batch without restarting
the program. The viewer re-reads the current pass (keeping it if you gave
`--pass`) and the statuses, then opens the next batch, in sorted order, that
still has todo images in the current mode, starting at its first item. The
help screen shows "batch k/B", the batch's position among all batches. `b`
goes round once, so batches you skipped earlier come back; with
`--filter clean` or `--filter all` an image counts as todo until you have
marked it in this session, and again if it becomes UNREVIEWED or FLAGGED (for
example, DIRTY images come back FLAGGED in the next pass). When the pass
changes (the pass ended with DIRTY images) it starts again from the first
batch, which in single mode means the first batch with FLAGGED images, and the
info bar says "Now pass N". When nothing is left you see "All batches done for
pass N", or, with the default `--filter unreviewed`, "Pass P complete -
nothing to review in pass N" when the pass has just ended. In grid mode, which
leaves FLAGGED and DIRTY images out, it instead tells you how many need
single-mode review: press `s` to review them. Press `q` to quit. If you started with `--batch`, `b` stays
in that batch: it reloads it while images are still todo, then says "Batch
NAME done for pass N". `z` cannot undo marks made in the previous batch.

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
Only images marked DIRTY in pass 1 are shown; in pass 2 they are FLAGGED
(orange) until you give them a verdict. Review each one individually. Mark
obviously clean ones as CLEAN, mark the rest DIRTY again.

Grid mode skips FLAGGED images (and any image already marked DIRTY), so a
single grid keypress can never clear an image that was flagged. Review
flagged images in single mode.

**Pass 3+** -- Repeat until confident:
```bash
image-review review --mode single
```
Each subsequent pass shows only the images still DIRTY from the previous
pass (FLAGGED in the new pass). The pool shrinks with each pass.

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

`--batch` restricts the whole session to that batch: pressing `b` at the end
of the list reloads it while it has todo images, and never moves on to another.

### Filtering by Status

By default, only images still to do are shown (UNREVIEWED, plus FLAGGED in
pass 2 and later). Use `--filter` to change this:

```bash
# Re-examine images previously marked CLEAN (e.g., to re-mark as DIRTY)
image-review review --filter clean

# Show all images regardless of status
image-review review --filter all
```

With the default `--filter unreviewed`, "todo" means UNREVIEWED or FLAGGED. With `--filter clean` or
`--filter all`, every listed image is a re-check, so "todo" means not yet marked in this session
(whatever the verdict, re-confirming included); undoing a mark makes the image todo again. The count
starts from the full list and is not remembered across restarts. In grid mode every filter, `all` included, leaves out
DIRTY and FLAGGED images; if that leaves nothing, it tells you how many
images need single-mode review. The `n` key jumps to the next todo item and `u` toggles todo-only
navigation in all filter modes.

## Step 3: Status

Check review progress at any time:

```bash
image-review status [--work-dir ./review_work]
image-review status --remote 'ir://...' [--via user@login-node]
```

**Example output:**

```
Overall: 40320 images (pass 2)
  CLEAN:       38100
  DIRTY:         820
  UNREVIEWED:      0
  FLAGGED:      1400

Batch            Total  Clean  Dirty  Unrev   Flag
----------------------------------------------------
batch_001          300    290      8      0      2
batch_002          300    285     12      0      3
batch_003          300    280     15      0      5
...

Current pass: 2

Skipped during preprocess: 3 failed, 12 ignored (see skipped.tsv in the work dir)
```

The last line appears only when preprocess skipped something. "Failed" inputs
could not be rendered, so they were never shown for review and are not in the
counts above; check `skipped.tsv` in the work directory for which ones and
why. "Ignored" inputs were not images (for example stray text files).

### Checking from a script

`--check` keeps the same report and sets the exit status: 1 while any image
is UNREVIEWED (has no verdict yet) or any input failed to preprocess; 0 once
every image has a verdict. It works with `--remote` too.

```bash
if image-review status --check --work-dir ./review_work > /dev/null; then
    echo "review finished"
fi
```

FLAGGED images already have a verdict (DIRTY, from an earlier pass), so they
do not keep `--check` at 1: re-review passes are optional. They still show in
the report as FLAGGED, so you can run a second pass on them whenever you want.

## Step 4: Export

The deliverable of a study is the list of source files that may be released.
`export` writes it as an **allowlist**: only the files reviewed CLEAN, each
with its path and SHA-256. Everything else is denied by default.

```bash
image-review export --work-dir ./review_work --output allowlist.tsv --report report.tsv
image-review export --work-dir ./review_work --output allowlist.tsv   # the allowlist alone, without a report
```

`allowlist.tsv`:

```
source_sha256  image_id                      pass_number  timestamp                         reviewer
3f1c...e09a    /data/site_a.zip::001.dcm     1            2026-03-02T10:14:07.512+00:00     alice
```

`report.tsv`, everything else:

```
image_id                      status        pass_number  timestamp                         reviewer  reason                    source_sha256
/data/site_a.zip::002.dcm     DIRTY         2            2026-03-03T09:01:44.020+00:00     bob                                 a27b...51c4
/data/site_a.zip::003.dcm     DIRTY         1            2026-03-02T10:15:30.101+00:00     alice     icon DIRTY                0d9e...7f30
/data/site_a.zip::004.dcm     UNREVIEWED                                                                                   c6b2...18de
/data/site_b/broken.dcm       NOT_REVIEWED                                                           cannot decode pixel data
/data/site_b/referral.pdf     IGNORED                                                                not an image (unrecognized content)
```

(Hashes shortened here; each is 64 hex characters.)

(Columns are aligned here for reading. Both files are tab-separated UTF-8
with LF line endings and no quoting. Export refuses, writing nothing, if any
field of either file holds a control character (tab, CR, LF and the rest of
C0, DEL, C1 such as U+0085), U+2028 or U+2029, or starts with `"`: such a
value could not be read back as one cell. That holds even without
`--report`.)

**Release a file only if both its path and its SHA-256 match a row of the
allowlist.** Anything not listed is denied: DIRTY, unreviewed, failed and
ignored files, a file whose bytes have changed, and a ZIP container as a whole
(its entries are listed one by one, as `<zip>::<entry>`). The report is for
audit and follow-up, e.g. what is left to review; never use it to choose what
to release (for example "everything that is not DIRTY").

- A file is allowlisted only if its status is CLEAN, the manifest has its
  SHA-256, and no file that is not CLEAN has the same recorded hash
  (`source_sha256`; identical bytes
  cannot be both clean and dirty, so all copies are denied). A CLEAN file
  that was not allowlisted is in the report as CLEAN, with `not allowlisted:
  ...` in `reason`. In a work directory made by a version before the hash
  columns, no file has a hash, so nothing is allowlisted.
- `status` (report) is the file's latest verdict. An image that was DIRTY in
  an earlier pass and not yet re-reviewed (FLAGGED) is reported as `DIRTY`.
  `UNREVIEWED` files have no verdict yet.
- A DICOM with an embedded icon was reviewed as two images. It is CLEAN only
  if both were marked CLEAN. Otherwise it takes the worse status (DIRTY,
  then NOT_REVIEWED, then UNREVIEWED), and `reason` names the icon's state
  (`icon DIRTY`, or `icon: <error>` if the icon could not be rendered). An
  icon whose file is not in the manifest is reported under that file, as
  NOT_REVIEWED at best (`main image missing`).
- `NOT_REVIEWED` rows are inputs that failed to preprocess (the `failed`
  rows of `skipped.tsv`, with its `reason`). Nobody has looked at them, so
  treat them as possibly containing PHI.
- `IGNORED` rows are inputs that were not images (the `ignored` rows of
  `skipped.tsv`, e.g. a PDF or a Word file), listed after all other rows.
  Nobody has looked at them either, so follow them up.
- `pass_number`, `timestamp` and `reviewer` come from the latest verdict on
  the file's main image and are empty without one. After an undo (`z`) they
  are the undo's time and reviewer.
- Rows follow the manifest's order, then `skipped.tsv`'s (failed, then
  ignored).
- `source_sha256` is the SHA-256 of the source file (or ZIP entry), from the
  manifest: compute each file's own SHA-256 and compare before releasing it.
  It is derived from the file's content: treat it as carefully as the
  `image_id`.
- `reviewer` is whatever name each reviewer gave, unverified, and can start
  with `=`, `+`, `-` or `@` (one starting with `"` makes export refuse). Import the files into a spreadsheet as text
  columns rather than opening them directly, so no value is taken as a formula.
- Export logs one line to stderr with how many files are allowlisted and how
  many of each status are in the report.

`image_id`s are the original source paths, which can contain PHI. So export
runs where the work directory is: on the cluster, not over `--remote`. It
refuses `--remote` and ignores `$IMAGE_REVIEW_REMOTE`.

Export refuses while a `review` or `serve` has the work directory open, since
verdicts may still change. Stop it first, or pass `--allow-live` to export
anyway (with a warning). If a crash cut the last line of `review.tsv` short,
export refuses until the next verdict is recorded with `review`, which drops
that line. Re-check the last image you reviewed before the crash.

`--output` and `--report` each create a new file with the work directory's
permissions: 0600 private, or 0660 and the work directory's group. They refuse
to overwrite an existing file (or to name the same file), and a file appears
only once it is complete. The report is written first, so if writing the
allowlist fails, only a report exists, which releases nothing. Each file is
first written to a hidden `.allowlist.tsv.<random>.tmp` beside it, which is removed on
every exit except a hard kill (`kill -9`, a node crash); delete such a
leftover, as it holds source paths.

## Reviewing on an HPC Cluster

If the images live on a cluster, you can review them from your laptop
without copying them off. `image-review serve` runs on a compute node and
serves the preprocessed work directory over HTTPS; `image-review review
--remote` on your laptop is the viewer.

Install `[preprocess,codecs]` on the cluster (core alone is enough where you
only run `serve`, `status` and `export`) and `[viewer]` on your laptop; see
[Installation](#installation). Use the same image-review version on both.

### 1. Preprocess on the cluster

Unchanged. Run it as a batch or interactive job, asking Slurm for several
cores: `--jobs` defaults to `$SLURM_CPUS_PER_TASK`, so rendering uses every
core you were given:

```bash
srun --cpus-per-task=8 --mem=16G image-review preprocess /data/scans.zip --work-dir /scratch/me/review_work
```

In an `sbatch` script, use `#SBATCH --cpus-per-task=8`. Without
`--cpus-per-task`, `$SLURM_CPUS_PER_TASK` is unset and `--jobs` falls back to
the CPUs the job may use (its CPU affinity, capped by the smallest cgroup v2 CPU quota of its cgroup and its ancestors). `scancel` (or the time limit) stops the workers and
leaves no work directory.

### 2. Serve from an interactive session

```bash
salloc ...                       # your site's usual options
srun --pty bash                  # or your site's interactive command
image-review serve --work-dir /scratch/me/review_work
```

On many Slurm sites `salloc` leaves you on the login node, so first get a
shell on the allocated node (as above), or run `srun --pty image-review serve
--work-dir ...` directly. `--pty` keeps stdout a terminal so the string is
printed; plain `srun` without `--pty` takes the connection-file path described
under batch mode below.

The server binds the node's hostname by default (`--bind` to override;
wildcard addresses are refused) and picks a free port (`--port` to choose
one). It prints the connection string plus ready-to-paste client commands:

```
ir://node042.cluster.example:41733/?token=...&fp=sha256:...
```

Treat the string like a password. Each server start generates a new token and
certificate, so a string from an earlier run no longer works.

### 3. Connect from your laptop

**Direct**, if compute nodes are reachable from your network:

```bash
image-review review --remote 'ir://node042.cluster.example:41733/?token=...'
```

**Through the login node**, if you can only reach that:

```bash
image-review review --remote 'ir://...' --via user@login-node
```

With `--via` the client runs `ssh` for you to forward a local port to the
compute node. Password or MFA prompts appear in your terminal, and the tunnel
closes when the client exits.

To keep the token out of your shell history, put it in the environment:

```bash
export IMAGE_REVIEW_REMOTE='ir://...'
export IMAGE_REVIEW_VIA=user@login-node     # optional
image-review review --mode grid
image-review status
```

The viewer behaves as it does locally (same flags, keys, passes and
resumption); `--work-dir` cannot be combined with `--remote`. `status
--remote` works too.

### 4. Batch mode (`sbatch`)

When stdout is not a terminal, `serve` does not print the string. It writes it
to `~/.image-review/connection-<host>-<port>.txt` (mode 0600) and removes the
file when the server stops (Ctrl-C, `scancel`, or the time limit). The path is
written to the job's output file.

```bash
#!/bin/bash
#SBATCH --job-name=image-review
#SBATCH --time=04:00:00
#SBATCH --output=image-review-%j.out

source /path/to/venv/bin/activate   # or your site's module load
image-review serve --work-dir /scratch/me/review_work
```

Then, on your laptop, copy the absolute path from the job output (do not use
`~`: your laptop's shell would expand it locally) and keep the string out of
`ps` and shell history by putting it in the environment:

```bash
export IMAGE_REVIEW_REMOTE="$(ssh user@login-node cat /home/me/.image-review/connection-node042.cluster.example-41733.txt)"
image-review review
```

This assumes your home directory is shared between the login and compute nodes.

### 5. Stopping and reconnecting

Stop the server with Ctrl-C, `scancel`, or by letting the allocation end.
Progress is saved on the server at every mark. If the connection drops, the
viewer shows "Lost connection to server - progress saved" and ignores every
key but `q`/`Esc`; press `q`, then
reconnect with the same string while the server is still running.

### Browser review over SSH (experimental)

An experimental alternative to the pygame viewer: a browser on your laptop,
with only `ssh` installed there. The server listens on a Unix socket on the
compute node and your laptop forwards a local port to it. The page reviews
single images (there is no grid mode yet).
[SECURITY.md](SECURITY.md#experimental-browser-review-over-a-unix-socket)
lists the differences from the HTTPS mode (plain HTTP on the node, a URL that
holds the token) and the questions to ask your HPC administrator if
forwarding does not work.

1. On the cluster, get a shell on a compute node (`salloc`, then `srun --pty
   bash`) and start the server, naming your login node:

   ```bash
   image-review serve --work-dir /scratch/me/review_work --socket --via me@login-node
   ```

   If your laptop can ssh to compute nodes directly (`ssh me@<node>` works
   without a jump host), use `--direct` instead of `--via`; the printed
   command then has no `-J`. If the node name it prints does not resolve from
   your laptop, add `--ssh-host NAME` (a host name, no `user@`) with the name
   that does.

2. It prints an ssh command and, on a terminal, a URL. On your laptop, paste
   the ssh command and leave it running (password or MFA prompts appear
   there):

   ```
   ssh -N -o ExitOnForwardFailure=yes -o ControlPath=none -J me@login-node -L 127.0.0.1:8080:/home/me/.image-review/serve-node042-12345.sock me@node042.cluster.example
   ```

3. Open the URL in your browser: `http://127.0.0.1:8080/#TOKEN`. The token is
   a password; do not paste the URL into chat or tickets.

4. Type your name in the Reviewer box (1-64 characters; it is recorded with
   every verdict, as `--reviewer` is for the viewer), then click on the image
   or press Enter so the keys reach the page. The page shows the current
   pass's UNREVIEWED and FLAGGED images one at a time, in random order:

   | Key | Button | Action |
   |-----|--------|--------|
   | `c` | Clean | Mark the image CLEAN and move on |
   | `d` | Dirty | Mark the image DIRTY and move on |
   | Right / Left | Next / Previous | Move through the list without marking |
   | `z` | Undo | Undo the latest mark and show that image again |

   A verdict counts only once the image has been on screen for 200 ms, so a
   key pressed as an image appears is ignored. An image that cannot be
   loaded shows "Cannot load image: KEY" and can be marked DIRTY but never
   CLEAN. The header shows the display scale as a percent; below 100%
   (shown in red) the image is shrunk to fit and small burned-in text can be
   lost, so enlarge the window or go full screen (browser zoom does not
   help: it makes the page's text larger and the image's share smaller).
   `z` says "Nothing to undo" once this page has no marks left to undo.
   With one page per server it only
   undoes this page's marks; the server keeps a single undo history, so with
   a second tab or client it undoes the latest mark from any of them (see
   "Multi-client limits" in [SECURITY.md](SECURITY.md)), and the page warns
   "Undid another client's mark". When the list is done the page says
   "Pass N: nothing left to review".

   Your marks so far are always saved on the server. If the tunnel drops,
   the page says "Lost connection": run the same ssh command again and
   reload the page. If the server was restarted, the old tunnel points at a
   socket that is gone (the default path includes the server's process id),
   so the page also says "Lost connection": stop the old ssh command (it
   holds port 8080, so the new one would fail), run the new command the
   server printed, and open its new URL (pasting it into the same tab
   works). The page says "token rejected" only when a restarted server
   reuses the same `--socket-path`.

Stop the server with Ctrl-C, then the ssh command. Each start has a new
token. `--via` only fills in the `-J` part of the printed command; without
it the command shows `<user>@<login-node>` for you to fill in. `--direct`
(or `$IMAGE_REVIEW_DIRECT=1`) leaves `-J` out; it cannot be combined with
`--via` on the command line.

**Batch mode.** Under `sbatch` stdout is not a terminal, so the URL is
written to `~/.image-review/browser-<host>-<pid>.txt` (mode 0600) and removed
when the server stops. The job output gives the ssh command and the file's
path; fetch the URL from your laptop (use the absolute path, not `~`):

```bash
ssh me@login-node cat /home/me/.image-review/browser-node042-12345.txt
```

This assumes your home directory is shared with the login node. Run the ssh
command from the job output first, then open the URL. With `--direct` the
job output gives `ssh me@node cat ...` instead, since you reach the node
itself. Under `sbatch` the node name is only known when the job runs, so set
it there if the default does not work from your laptop, e.g. `--ssh-host
"$(hostname -f)"` or a site-specific name.

**Troubleshooting:**
- `channel N: open failed: connect failed` from ssh: the node's sshd would
  not forward to the socket (a forwarding policy), or the socket on your home
  filesystem is not usable (some network filesystems do not support Unix
  sockets). Try a node-local path:
  `serve --socket-path "$(mktemp -d /tmp/ir.XXXXXX)/ir.sock"`, and use the
  command it prints. That socket exists only on that node.
- `channel 0: open failed: connect failed: Name or service not known`
  followed by `stdio forwarding failed`, with `-J`: the login node cannot
  resolve the compute node's name. If `ssh you@<node>` works from your
  laptop, rerun the server with `--direct` and use the command it prints.
- The printed node name does not resolve from your laptop: rerun with
  `--ssh-host` set to the name that works, and use the command it prints.
- Windows: the built-in OpenSSH client works (a direct forward to the socket
  was tested from Windows). If `-J` fails with `CreateProcessW failed
  error:2` or `posix_spawn: No such file or directory`, replace `-J
  you@login` with `-o ProxyCommand="C:\Windows\System32\OpenSSH\ssh.exe -W
  %h:%p you@login"`. In PowerShell the single quotes the command may print
  are fine; cmd.exe does not treat single quotes as quoting, which only
  matters if a printed piece was quoted (for example a path with spaces).
- `Permission denied (publickey,hostbased)`: `-J` makes your laptop
  authenticate to the compute node itself, through the login node, so the
  laptop's key must be accepted there (in the cluster's `authorized_keys`),
  not only on the login node.
- `bind [127.0.0.1]:8080: Address already in use` or "Could not request local
  forwarding": port 8080 is busy on your laptop. Change the number in
  `-L 127.0.0.1:8080:...` and in the URL.
- If your `ssh_config` enables `ControlPersist` for these hosts, a background
  master can keep a forward alive after Ctrl-C. The printed command sets
  `ControlPath=none` to avoid that; if you edit it, keep that option.
- "Socket path is N bytes; the limit is ...": use a shorter `--socket-path`.
- "Another server is listening on ...": pick a different `--socket-path`.
- Prefer the default socket path. On a shared home directory, a second server
  on another node given the same explicit `--socket-path` takes the first's
  socket over and the first becomes unreachable. If you must choose a path,
  include `$SLURM_JOB_ID` in it.

### Security

The connection string is a password: anyone holding it can view the images and
record verdicts while the server runs, so do not paste it into chat or tickets.
Use one reviewer per server, and remember that a work directory and any export
hold source paths. [SECURITY.md](SECURITY.md) is the full threat model
(what stays on the cluster, what travels, what the viewer cannot control, and
the local-disk and integrity rules).

**Troubleshooting `--via`:**
- The server logs one `connection error: SSLEOFError` line per client start.
  This is the client's readiness probe and is harmless.
- `channel N: open failed` from ssh means the login node cannot reach
  `NODE:PORT`.
- `--via` always authenticates afresh (ControlMaster sharing is disabled so no
  forward is left behind), so expect an MFA prompt each time.
- If your `ssh_config` has `LocalForward` lines for the login node (e.g. for
  Jupyter), `--via` can fail with "Address already in use". Use a separate
  `Host` alias without `LocalForward`.
- If ssh backgrounds itself (`ForkAfterAuthentication`), remove that option.

**Troubleshooting "work directory is in use":** the message names who holds
the work directory (user, node, pid, start time) and the lock file
(`review.lock` in the work directory). Finish or stop that session first. A
lock is cleared automatically only when the tool can verify that its process is
gone on the same machine since its last boot. A lock left on another node (for
example a `serve` job that was killed), or one whose process id has since been
reused, is not. If you are sure that process is gone (check `squeue`, or
`ps -p PID` on that node, and compare the start time), delete the lock file by
hand and run again:

```bash
rm /scratch/me/review_work/review.lock
```

**Troubleshooting "Cannot read work directory":** `review.tsv` or
`manifest.tsv` is malformed (for example a hand edit left a short row, a
status other than `CLEAN`/`DIRTY`, a non-numeric pass, or a hash that is not
64 lowercase hex characters). The message names the file and line. Fix or remove that line and run again; the tool never
repairs or drops rows on its own.

**Troubleshooting versions:** "server speaks API vN, this client vM" (or
"server is too old to report its API version") means the laptop and the
cluster have different image-review versions; install the same version on
both machines.

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

# Pass 2: single-image review of the images flagged DIRTY in pass 1
image-review review --mode single --work-dir ./phi_review

# Pass 3: final review of stubborn cases
image-review review --mode single --work-dir ./phi_review

# Final status
image-review status --work-dir ./phi_review

# The allowlist of releasable files, and a report of the rest
image-review export --work-dir ./phi_review --output ./phi_review_allowlist.tsv --report ./phi_review_report.tsv
```

## Work Directory Files

All state lives in the work directory (default `./review_work`):

| File | Format | Description |
|------|--------|-------------|
| `manifest.tsv` | TSV | Master image list (batch, preprocessed_path, image_id, source_sha256, jpeg_sha256). Work dirs from older versions have only the first three columns and still load |
| `skipped.tsv` | TSV | Inputs that produced no image (image_id, kind, reason) |
| `preprocess.json` | JSON | How the work directory was made: tool and library versions, time (UTC), resolved sources, parameters (batch size, colormap, contrast settings, JPEG quality, access, jobs), and counts. Holds source paths; never served |
| `review.tsv` | TSV | Review decisions (image_id, batch, status, pass_number, timestamp, reviewer, mode, grid_size, tool_version) |
| `batch_NNN/img_NNNNN.jpg` | JPG | Preprocessed individual images |

`review.tsv` is an append-only log: every rating action appends a line per
image and syncs it to disk, and when an image appears more than once its last
line wins. It is safe to kill the process at any time. A last line cut short by
a crash is skipped with a warning and dropped on the next rating, unless all
of its fields made it to disk: then it is kept, and only its last field
(`tool_version`, or `timestamp` in an old five-column file) may be cut short
or empty.

Each line also records who rated and how: `reviewer` (the `--reviewer` name,
the client's own unverified claim), `mode` (`single` or `grid`), `grid_size`
(how many images one keypress rated; 1 in single mode) and `tool_version` (the
image-review version that wrote it). An undo (`z`) appends lines with `mode`
`undo`: each restores an image's previous status and pass number exactly, or,
for an image that had no earlier rating, has status `UNREVIEWED`, which makes
the image count as never reviewed again. A `review.tsv` written by an older
version has only the first five columns; `review` or `serve` upgrades it once,
leaving the new columns empty for the old lines, while `status` reads it as is.
Older image-review versions cannot read the upgraded file, so upgrade everyone
sharing a work directory together. Versions before wire API v5 (before undo)
also reject a `review.tsv` holding undo lines, so upgrade everyone before
anyone uses `z`. If the upgrade is interrupted, a
`.review.tsv.*.tmp` file may be left in the work directory: the tool ignores
it, but it contains source paths, so delete it.

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
