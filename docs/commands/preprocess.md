# `image-review preprocess`

```
image-review preprocess SOURCE [SOURCE ...] [--batch-size N]
                                            [--work-dir DIR]
                                            [--colormap NAME]
                                            [--access {private,group}]
                                            [--allow-skipped]
                                            [--jobs N]
                                            [--draw-overlays]
```

Renders each image input to a JPG, in batches, in a new work directory.
Inputs it cannot render are listed in `skipped.tsv` (see
[Skipped inputs](#skipped-inputs)); rendered inputs that hold something
downstream tools should strip are listed in `notes.tsv` (see
[Input notes](#input-notes)).

| Option | Default | Description |
|--------|---------|-------------|
| `--batch-size` | 300 | Images per batch (1 or more) |
| `--work-dir` (alias `--output-dir`) | `./review_work` | Work directory to create |
| `--colormap` | `inferno` | Matplotlib colormap for DICOM grayscale images |
| `--access` | `private`; `$IMAGE_REVIEW_ACCESS` | Who may use the work directory; see [Access control](#access-control) |
| `--allow-skipped` | off | Exit 0 even if some inputs failed to preprocess (they are still listed in `skipped.tsv`) |
| `--jobs` | `$SLURM_CPUS_PER_TASK`, else the usable CPUs | Worker processes (1 or more); see [Parallel rendering](#parallel-rendering) |
| `--draw-overlays` | off | Draw DICOM overlay planes onto the review images at maximum brightness; by default they are not drawn, only listed in `notes.tsv` (see [Rendering pipeline](#rendering-pipeline)) |

An unknown `--colormap` name, or `--jobs 0`, is a usage error: the command
exits 2 before reading any input or creating the work directory.

**Inputs.** Each SOURCE is a ZIP file, a directory (searched recursively,
including ZIP files inside it) or an individual file. Inputs are recognized by
content, not by extension: DICOM (including extensionless files like `IM0001`,
common in DICOM exports, and DICOM without the 128-byte preamble), PNG, JPEG,
TIFF, BMP, GIF, WebP, JPEG 2000 and PNM. So upper-case extensions (`B.DCM`,
`e.JPG`), `.jpeg`, `.dicom` and `.ima` files are all picked up too.

HEIF and AVIF files (`.heic`, `.heif`, `.avif`) are recognized too, but they
render only if the installed Pillow can decode them: Pillow 12 reads AVIF,
older releases may not, and HEIC needs a Pillow plugin that image-review does
not load. Otherwise the file is `failed` with Pillow's error, so it is never
silently dropped.

Word, Excel, PowerPoint and EPUB files are ZIPs, so images embedded in them
are reviewed too. A ZIP inside a ZIP is not opened, and other archives
(`.tar.gz`, `.7z`, `.rar`, ...) are not supported: extract them first (both
are recorded as failed).

Symlinked files are read, but symlinked directories are never entered, so a
link such as `up -> ..` cannot pull in other patients' studies: a link to an
enclosing directory or into another SOURCE is ignored, and any other is
failed with its target, so pass that target as a SOURCE if you want it. The
work directory and its `.NAME.partial` staging directory are never read as
input, so `image-review preprocess .` with the default `./review_work` is
safe.

Each SOURCE is resolved first, symlinks included, so a symlink named on the
command line is recorded under its target's path. A symlinked file found
inside a directory keeps its link path.

Inputs are taken in a fixed order, which is the order of `manifest.tsv`:
SOURCES in the order given; within a directory, names sorted by character
code at each level (so upper case before lower case), with a directory's
files before its subdirectories; within a ZIP, entries in the order they are
stored. A ZIP holding several entries with the same name gives each its own
image id (see [`manifest.tsv`](../reference/work-directory.md#manifesttsv)).

**Rendering.** DICOM images are normalized with adaptive histogram
equalization, to enhance local contrast, and a configurable colormap; the
steps, and how other images are shown, are in
[Rendering pipeline](#rendering-pipeline).

**Output.** Images are written to batch subdirectories with a `manifest.tsv`
index, which records the SHA-256 of each JPG and its source, so a JPG changed
after preprocessing is caught when viewed (see
[`manifest.tsv`](../reference/work-directory.md#manifesttsv)).

`preprocess.json` beside the manifest records how the work directory was
made; its keys are in
[`preprocess.json`](../reference/work-directory.md#preprocessjson).

**A new work directory every time.** The work directory must not already
exist (an empty directory is fine). `preprocess` refuses to write into one
that has content, so verdicts can never be attached to a replaced image:
choose a new `--work-dir` or remove the old one. An existing work directory
is never modified, so a `serve` running on it is not affected.

Output is built in a staging directory next to it (`.NAME.partial`, with the
access policy's directory mode) and renamed into place only on success, so an
interrupted run leaves no work directory behind. If a crash leaves
`.NAME.partial` behind, the next run says so; remove it and re-run.

## Examples

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

## Access control

A work directory holds PHI (images with burned-in text, source paths,
verdicts), so it is never world-readable. `--access` (or
`$IMAGE_REVIEW_ACCESS`) picks who else may use it:

| `--access` | Directories | Files | Who |
|------------|-------------|-------|-----|
| `private` (default) | 0700 | 0600 | the owner only |
| `group` | 2770 (setgid) | 0660 | the work directory's Unix group |

Every directory and file `preprocess` creates follows the policy, and so does
`review.tsv` when verdicts are saved (it recovers the policy from the work
directory's own mode, so there is nothing to repeat).

The tool only sets mode bits; it never runs `chgrp`. The work directory's
group is whatever the filesystem assigns: the parent directory's group if the
parent is setgid, otherwise your current primary group. A pre-created empty
work directory's own group and mode are not kept (it is replaced by the
staging directory).

- On clusters where everyone's primary group is site-wide (e.g. `users`),
  create the work directory under the study's group-owned setgid project
  directory, or run
  `sg <group> -c 'image-review preprocess ... --access group'` (or
  `newgrp <group>` first).
- With `--access group`, `preprocess` prints which Unix group got access
  after the summary line (`Shared with Unix group 'study' (gid N)`, or just
  the gid when the group has no name).
- POSIX default ACLs on the parent can add named user/group entries (check
  with `getfacl`; the tool does not manage ACLs), but files never get "other"
  bits.

`review`, `serve`, `export` and `status` on a local work directory log a
warning if the work directory or its `manifest.tsv` is accessible to all
users (e.g. one made by an older version):

```
<path> is accessible to all users (mode NNNN); run `chmod -R o-rwx <work dir>`
```

Group bits alone do not warn. The commands never change an existing directory's mode:
run the `chmod` yourself.

A work directory takes one writer at a time (see
[`review.lock`](../reference/work-directory.md#reviewlock)). A team
shares a work directory sequentially or splits a study into several work
directories.

## Skipped inputs

Every input ends up in exactly one of `manifest.tsv` (rendered) or
`skipped.tsv`, with one of two kinds:

- `failed`, e.g.:
  - a corrupt file;
  - a `.jpg`/`.png`/... or `.zip` whose content is not one;
  - a `.tar.gz` or other non-ZIP archive;
  - a ZIP inside a ZIP;
  - an `unsupported:` multi-frame DICOM, or a DICOM object without pixel
    data (structured reports, encapsulated PDFs);
  - an input whose float pixel data holds NaN or infinity (a float DICOM or a
    mode `F` TIFF), with `unsupported: non-finite pixel values (1 NaN, 0
    infinite)`: scaling by the minimum and maximum would wipe out the
    contrast, so nothing readable would be left to review;
  - an unreadable subdirectory;
  - a rendered image wider or taller than 65500 pixels, libjpeg's limit,
    with an `OSError` from the JPEG writer (with Pillow 12.3,
    `OSError: broken data stream when writing image file`);
  - a symlinked directory outside the sources;
  - a file named on the command line that is not an image, such as
    `notes.txt`;
  - an input whose image id would name a different image from another
    input's (a file called `site.zip::a.png` beside `site.zip`, or a ZIP
    entry called `a.png#2` beside two `a.png` entries): every one of them, with
    `image_id collides with another input (rename one of them)`, as they
    would otherwise share one verdict. A file whose name ends in `#icon` is
    not a collision, but rename it before preprocessing anyway: `export`
    cannot tell it apart from a DICOM's icon in a work directory made by an
    older version, so it cannot vouch for it (see
    [What is allowlisted](export.md#what-is-allowlisted));
  - a file or ZIP entry whose name, or a directory above it, is not UTF-8 or
    holds a control character such as a newline or U+2028/U+2029, even one
    that would otherwise be ignored. It is listed under an escaped name like
    `a\x0ab.png` or `a\u2028b.png`: rename it.
- `ignored` (not an image): unrecognized content, macOS AppleDouble files
  (`._name`) and `__MACOSX/` entries, a DICOMDIR index, an empty ZIP, a
  symlink to a directory that is already being read. These do not affect the
  exit status, but glance at them in case something you expected to review
  is among them.

Each row has a reason, for example `unsupported: multi-frame DICOM (3 frames)`
or `BadZipFile: File is not a zip file`. `skipped.tsv` contains source paths,
so treat it as carefully as `manifest.tsv`.

The run finishes with a summary line
(`Found N inputs: wrote K images in B batches; S skipped (F failed, I ignored; see .../skipped.tsv)`),
such as:

```
Found 1200 inputs: wrote 1195 images in 4 batches; 5 skipped (3 failed, 2 ignored; see review_work/skipped.tsv)
```

N counts every input found: rendered, failed and ignored. When any rendered
input has [notes](#input-notes), the line goes on with their count by kind
(kinds with none are left out):

```
...; 5 skipped (3 failed, 2 ignored; see review_work/skipped.tsv); 3 notes (2 overlay-plane, 1 icon-image; see review_work/notes.tsv)
```

It exits 1 if any input failed, unless `--allow-skipped` is given, so
scripts notice. Check `skipped.tsv` before reviewing: those images will not
be shown. Either fix them and preprocess again into a new work directory, or
accept the result: the work directory has been written and can be reviewed as
it is (pass `--allow-skipped` up front when a script should treat failed
inputs as success).

## Input notes

`notes.tsv` lists written inputs that hold something that downstream tools
are expected to strip, whether or not the review image shows it. A note never fails an input and never changes the exit
status: the image is in `manifest.tsv` and is reviewed as usual. Only
inputs that are written get notes; a failed or ignored input has none. An
input can have several notes, one row each. The file's columns are in
[`notes.tsv`](../reference/work-directory.md#notestsv); like `skipped.tsv` it
holds source paths.

| Kind | Meaning | Example detail |
|------|---------|----------------|
| `overlay-plane` | A DICOM with one or more overlay planes (groups 6000-601E with OverlayData) | `group 0x6000`, or `groups 0x6000, 0x6002` |
| `overlay-unused-bits` | A DICOM whose pixel data holds something above BitsStored other than zeros or sign extension (e.g. a retired overlay) | `bit 15`, `bits 12, 15`, or `bit 15 (overlay 0x6000)` |
| `icon-image` | A DICOM with an embedded icon image (IconImageSequence (0088,0200)) | `IconImageSequence: 64x64 MONOCHROME2` |

- **`overlay-plane`**: the review image is the pixel data alone, so overlay
  planes are not reviewed (unless `--draw-overlays` draws them; see
  [Rendering pipeline](#rendering-pipeline)): strip them before release.
  The note needs only that OverlayData is present, so an overlay that cannot
  be decoded is noted too.
- **`overlay-unused-bits`**: the bits of each pixel above BitsStored are
  masked before rendering (signed data is sign-extended from its stored
  bits), whatever the transfer syntax, so they are not reviewed. A pixel
  whose bits above BitsStored are all zero, or (signed data) repeat its
  sign, holds nothing there; for the other pixels the detail lists the bit
  positions that differ from the masked value, ascending. A position named by
  OverlayBitPosition (60xx,0102) in a group without OverlayData is followed
  by ` (overlay 0x60xx)`. Besides a retired overlay stored in those bits,
  the note can mean a BitsStored that understates the data, or lossy
  compression overshooting it (e.g. lossy JPEG 2000 coded at a higher
  precision than BitsStored, whose brightest pixels then wrap round to dark
  when masked): check the image against the note. The bits stay in the
  pixel data as stored, so they ship in the released bytes unless they are
  stripped (cleared) before release.
- **`icon-image`**: icons are not rendered, so text burned into them is not
  reviewed: remove them before releasing files. The detail gives the first
  icon's rows, columns and photometric interpretation, with `?` for one that
  is missing (`IconImageSequence: ?x? ?`) and each value cut to 64 characters
  plus `…`, and `, N items` when the sequence holds more than one icon. An icon sequence that cannot be read is noted as
  `IconImageSequence: unreadable (ValueError)` (the error's class), and the
  input still renders. An empty sequence gets no note.

## Rendering pipeline

The DICOM preprocessing pipeline for grayscale (MONOCHROME1/2) images;
single-frame colour (RGB, YBR) and palette DICOMs are only converted to 8-bit
RGB and cropped, with no windowing or colormap:

1. Converts to float32, corrects photometric interpretation
2. Compresses intensity outliers (beyond the 1st/99th percentile) into the
   ends of the range instead of clipping them, so bright burned-in text
   stays visible. With `--draw-overlays`, DICOM overlay planes (annotations
   stored outside the pixel data) are then drawn at maximum brightness (white
   on colour and palette images)
3. Applies adaptive histogram equalization (CLAHE with 96-pixel tiles)
4. Strips uniform rows/columns (letterboxing removal)
5. Applies colormap and saves as JPG

A DICOM's embedded icon image (a thumbnail stored in the file, in
IconImageSequence (0088,0200)) is not rendered, so text burned into it is not
reviewed: remove icons before releasing files. `notes.tsv` lists each DICOM
that has one (see [Input notes](#input-notes)). Only the pixel data is
reviewed: overlay planes are not drawn by default, so the review image shows
only the pixel data, and `notes.tsv` lists each DICOM that has
overlay planes, which still ship in the file (strip them before release). With `--draw-overlays` they are
drawn onto the image, and a DICOM whose overlay cannot be decoded is `failed`
as a whole, naming its group; without the flag they are never decoded.
Overlays stored in the unused bits of the pixel data (bits above
BitsStored), rather than in overlay planes, are masked for every transfer
syntax, so they are never drawn, with or without the flag, and are noted as
`overlay-unused-bits`. Only
single-frame DICOMs (grayscale, colour or palette) are rendered for now.

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

Every rendered image, DICOM or not, is saved as a JPG with quality 95 and no
chroma subsampling, so small text keeps its edges.

## Parallel rendering

`--jobs N` renders inputs in N worker processes; `--jobs 1` renders in the
main process. The default is `$SLURM_CPUS_PER_TASK` when it is a whole
number of 1 or more, capped at the CPUs the process may use. Otherwise it is
the CPUs the process may use, capped by the smallest cgroup v2 CPU quota of
the process's cgroup and its ancestors (such as a login node's per-user
`CPUQuota=` or a container's limit). cgroup v1 quotas are not read.

- The output is byte-for-byte the same for any N; `preprocess.json` records
  the value.
- Memory grows with N: the main process holds the raw bytes of up to 2 × N
  inputs and each worker one input and its decoded arrays, so lower `--jobs`
  for very large DICOMs.
- Workers use one BLAS/OpenMP thread each unless you set `OMP_NUM_THREADS`
  and the like yourself.
- On a Slurm cluster, request cores with `--cpus-per-task` (e.g.
  `srun --cpus-per-task=8 image-review preprocess ...`) and `--jobs` follows.
- If a worker dies (e.g. out of memory), the run fails and leaves no work
  directory; re-run with `--jobs 1` to find the input. Ctrl-C or `scancel`
  stops the workers and removes the staging directory.
