# image-review Tutorial

A CLI tool for reviewing DICOM (and other) images for burned-in PHI.
The workflow has three phases: **preprocess**, **review**, and **status**.

## Installation

```bash
pip install -e .
```

This installs the `image-review` command and the codecs that decode compressed
DICOMs (`python-gdcm`, `pylibjpeg`, `pylibjpeg-openjpeg`; wheels for CPython
3.12/3.13 on Linux x86_64/aarch64, macOS and Windows). A DICOM that still
cannot be decoded (e.g. 12-bit JPEG Extended) is listed in `skipped.tsv` as
`cannot decode <transfer syntax>: ...`. `--via` (HPC tunnelling, below)
also needs an OpenSSH client on the machine you run it on (built into macOS,
Linux and Windows 10+).

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
  manifest.tsv        # Master list: batch, preprocessed_path, image_id
  skipped.tsv         # Inputs that produced no image: image_id, kind, reason
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
| `--rotate/--no-rotate` | `--rotate` | Allow rectpack to rotate images for tighter grid packing |
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
may be rotated 90° for tighter packing; use `--no-rotate` to disable this,
or press `M` during review to switch to grid mode without rotation.

```bash
image-review review --mode grid
```

### Controls

The viewer accepts keyboard and gamepad input:

| Action | Key | Gamepad |
|--------|-----|---------|
| Mark CLEAN | `c` | Button 1 |
| Mark DIRTY | `d` | Button 3 |
| Undo your last mark in this mode | `z` | -- |
| Next batch (end-of-list screen) | `b` | -- |
| Next image | `Right` | Hat right |
| Previous image | `Left` | Hat left |
| Next todo item | `n` | -- |
| Toggle todo-only navigation | `u` | -- |
| Autoplay (auto-advance) | `Space` | -- |
| Single mode | `s` | -- |
| Grid mode (rotation allowed) | `m` | -- |
| Grid mode (no rotation) | `M` | -- |
| Select display | `w` | -- |
| Toggle fullscreen | `f` | -- |
| Help screen | `h` | -- |
| Quit (saves automatically) | `q` or `Esc` | Button 7 |

**Status bar** at the bottom of the screen:
- **Green** = CLEAN
- **Red** = DIRTY
- **Gray** = UNREVIEWED
- **Orange** = FLAGGED (marked DIRTY in an earlier pass; needs a verdict in this one)

The right end of the bar shows the scale the image is displayed at, e.g. `27%`.
It is red below 100%: text in the image is smaller than in the original, so
look closely. In grid mode it is the smallest image's effective scale, counting
any shrinking done to fit the grid. Resizing the window in grid mode recomputes the grids for the new
size.

After marking an image, the viewer auto-advances to the next image after a
short delay (200ms). Images are shuffled at review time to counter attention
fatigue. A verdict (`c`/`d` or gamepad Button 1/3) only counts once the image
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
left in the other direction), which also shows how many todo images the batch
has left, if any, press `b` to move on to the next batch without restarting
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

## Reviewing on an HPC Cluster

If the images live on a cluster, you can review them from your laptop
without copying them off. `image-review serve` runs on a compute node and
serves the preprocessed work directory over HTTPS; `image-review review
--remote` on your laptop is the viewer.

### 1. Preprocess on the cluster

Unchanged. Run it as a batch or interactive job:

```bash
image-review preprocess /data/scans.zip --work-dir /scratch/me/review_work
```

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

### Security Model and Limitations

**What stays on the cluster:** the original files, DICOM headers, source
paths and `image_id`s, and `review.tsv`. The client only sees preprocessed
paths like `batch_001/img_00001.jpg`.

**What travels:** the preprocessed JPGs, over TLS, held only in the client's
memory. The tool makes no deliberate attempt to persist them, but swap, crash
dumps, and screenshots or screen recording are outside its control.

**Authentication and encryption:** the server uses a self-signed certificate
generated at start-up. Its SHA-256 fingerprint is part of the connection
string and the client checks it on every connection before sending the token,
so a wrong or replaced server is rejected. Requests also need the bearer
token. With `--via`, ssh protects laptop to login node and TLS covers the
whole path to the compute node.

**Limitations:**
- Anyone holding the connection string can view the images and record review
  marks while the server runs. Do not paste it into chat or tickets.
- One writer per work directory: `serve` or a local `review` holds
  `review.lock` in the work directory while it runs, and a second one exits
  with an error (see below). `status` still works. Use one reviewer per server.

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
status other than `CLEAN`/`DIRTY`, or a non-numeric pass). The message names
the file and line. Fix or remove that line and run again; the tool never
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
```

## Work Directory Files

All state lives in the work directory (default `./review_work`):

| File | Format | Description |
|------|--------|-------------|
| `manifest.tsv` | TSV | Master image list (batch, preprocessed_path, image_id) |
| `skipped.tsv` | TSV | Inputs that produced no image (image_id, kind, reason) |
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
  500ms intervals. Press any key to stop. Useful for a quick visual scan.
- **Gamepad**: A game controller makes long review sessions more
  comfortable. Map CLEAN/DIRTY to face buttons and navigate with the d-pad.
- **Batch size**: Larger batches mean fewer but denser grids. The default
  (300) works well for typical DICOM series. Reduce for very large images.
- **Colormap**: `inferno` (default) provides good contrast for medical
  images. Try `gray` for a more traditional radiological look, or `viridis`
  for general-purpose use.
