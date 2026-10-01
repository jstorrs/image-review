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
| `--pass` | auto | Pass number (auto-detected if omitted) |
| `--batch` | all | Restrict review to a specific batch (e.g., `batch_001`) |
| `--filter` | `unreviewed` | Which images to show: `unreviewed`, `clean`, or `all` |
| `--rotate/--no-rotate` | `--rotate` | Allow rectpack to rotate images for tighter grid packing |
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
image-review status --remote 'ir://...' [--via user@login-node]
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
viewer shows "Lost connection to server - progress saved"; press `q`, then
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
| `skipped.tsv` | TSV | Inputs that produced no image (image_id, kind, reason) |
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
