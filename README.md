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

Compressed DICOMs (JPEG, JPEG Lossless, JPEG-LS, JPEG 2000, HTJ2K, RLE) are
decoded with `python-gdcm`, `pylibjpeg` and `pylibjpeg-openjpeg`, installed
with the package. Wheels exist for CPython 3.12 and 3.13 on Linux (x86_64 and
aarch64), macOS (Intel and Apple silicon) and Windows (x86_64); on other
platforms `python-gdcm` has no wheel and installation may fail.
12-bit JPEG Extended files cannot be decoded (the only decoder is
GPL-licensed and is not used) and are listed in `skipped.tsv` as
`cannot decode JPEG Extended (Process 2 and 4): ...`.

`cryptography` is a dependency (used by `serve` for its TLS certificate).
`review --via` and `status --via` need an OpenSSH client on the machine you
run them on (built into macOS, Linux and Windows 10+).

## Quick Start

```bash
# Preprocess a directory of DICOMs or a ZIP archive
image-review preprocess /path/to/dicoms/ --work-dir ./review_work

# Pass 1: grid triage — quickly mark entire grids CLEAN or DIRTY
image-review review --mode grid

# Pass 2: single review — inspect only the flagged (pass-1 DIRTY) images individually
image-review review --mode single

# Check progress (--check: exit 1 until every image has a verdict)
image-review status

# Write the result: which source files are CLEAN and which DIRTY
image-review export --output result.tsv
```

The default work directory is `./review_work`; don't create work directories inside a git checkout (the repo's `.gitignore` excludes them as a safety net).

Warnings and other diagnostics are logged to stderr as `time LEVEL module: message`.
Put `-q`/`--quiet` before the command to show only warnings and errors, or
`-v`/`--verbose` to enable debug messages (currently few: the work directory or
server opened, the ssh tunnel command, grid packing results), e.g.
`image-review -q status`. The default shows INFO and up; only `serve` logs at
INFO (one line per request: peer address, method, path without its query
string, and status). The server never logs tokens, query strings, image keys,
source paths or exception messages. Warnings from `review` and `status` do name
image keys (e.g. an image that cannot be loaded), and `preprocess` warnings
name the source files that failed, on the machine where preprocess runs.

## Commands

### `image-review preprocess`

```
image-review preprocess SOURCE [SOURCE ...] [--batch-size N]
                                            [--work-dir DIR]
                                            [--colormap NAME]
                                            [--access {private,group}]
                                            [--allow-skipped]
```

Accepts ZIP files, directories (searched recursively, including ZIP files
inside them), or individual files. Inputs are recognized by content, not by
extension: DICOM (including extensionless files like `IM0001` and DICOM
without the 128-byte preamble), PNG, JPEG, TIFF, BMP, GIF, WebP, JPEG 2000 and
PNM. Symlinked directories are never entered: a link to an enclosing
directory or into another SOURCE is ignored, and any other is failed with its
target, so pass that target as a SOURCE if you want it. The work directory is
never read as input. DICOM images are
normalized with adaptive histogram equalization to enhance local contrast
and a configurable colormap; single-frame colour and palette DICOMs are shown
as they are. DICOM overlay planes are drawn at maximum brightness, and an
embedded icon image becomes an extra manifest row whose image id ends in
`#icon`. Non-DICOM images are converted to RGB, with the same
contrast enhancement applied to grayscale. Transparent images are shown as
the composite over mid-gray beside the raw channels with alpha ignored, and
MPO JPEGs (HDR gain maps, previews) show all their frames side by side.
Output is organized into batch subdirectories with a `manifest.tsv` index.

The work directory must not already exist (an empty directory is fine);
`preprocess` refuses to write into one that has content, so verdicts can never
be attached to a replaced image. Choose a new `--work-dir` or remove the old
one. Output is built in a staging directory next to it
(`.NAME.partial`, with the access policy's directory mode) and renamed into place only on success, so an
interrupted run leaves no work directory behind. If a crash leaves
`.NAME.partial` behind, the next run says so; remove it and re-run.

**Access control.** A work directory holds PHI (images with burned-in text,
source paths, verdicts), so it is never world-readable. `--access` (or
`$IMAGE_REVIEW_ACCESS`) picks who else may use it:

| `--access` | Directories | Files | Who |
|------------|-------------|-------|-----|
| `private` (default) | 0700 | 0600 | the owner only |
| `group` | 2770 (setgid) | 0660 | the work directory's Unix group |

Every directory and file `preprocess` creates follows the policy, and so does
`review.tsv` when verdicts are saved (it recovers the policy from the work
directory's own mode, so there is nothing to repeat). The tool only sets mode
bits; it never runs `chgrp`. The work directory's group is whatever the
filesystem assigns: the parent directory's group if the parent is setgid,
otherwise your current primary group; a pre-created empty work directory's
own group and mode are not kept (it is replaced by the staging directory).
On clusters where everyone's primary group is site-wide (e.g. `users`), create
the work directory under the study's group-owned setgid project directory, or
run `sg <group> -c 'image-review preprocess ... --access group'` (or
`newgrp <group>` first). With `--access group`, `preprocess` prints which Unix
group got access (`Shared with Unix group 'study' (gid N)`). POSIX default ACLs
on the parent can add named user/group entries (check with `getfacl`; the tool
does not manage ACLs), but files never get "other" bits.
`review`, `serve` and `status` print a warning if the work directory or its
`manifest.tsv` is accessible to other users (e.g. one made by an older
version); they never change an existing directory's mode: run
`chmod -R o-rwx <work dir>`. Only one writer (`review` or `serve`) can use a
work directory at a time: it holds `review.lock` there, and a second writer
exits with an error naming who holds it (`status` is read-only and always
works). A team shares a work directory sequentially or splits a study into
several work directories.

Every input ends up in exactly one of `manifest.tsv` (rendered) or
`skipped.tsv`, with kind `failed` (e.g. a corrupt file, a `.jpg`/`.png`/...
or `.zip` whose content is not one, a `.tar.gz` or other non-ZIP archive, an
`unsupported:` multi-frame DICOM, an unreadable subdirectory, a
symlinked directory outside the sources, a file named on the command line
that is not an image) or `ignored` (not an image: unrecognized content, macOS
AppleDouble files, a DICOMDIR index, an empty ZIP). The run finishes with a summary line (`Found N inputs:
wrote K images in B batches; S skipped (F failed, I ignored; see
.../skipped.tsv)`) and exits 1 if any input failed, unless `--allow-skipped`
is given. Check `skipped.tsv` before reviewing: those images will not be
shown.

### `image-review review`

```
image-review review [--mode {single,grid}]            [--pass N]
                    [--batch BATCH_ID]                 [--work-dir DIR]
                    [--filter {unreviewed,clean,all}]  [--rotate {auto,always,never}]
                    [--reviewer NAME]
                    [--remote CONNECTION_STRING [--via DESTINATION]]
```

Opens a fullscreen interactive session. In **grid mode**, images are
bin-packed into composite grids for fast triage. In **single mode**, images
are shown one at a time for detailed inspection.

| Key | Action |
|-----|--------|
| `c` | Mark CLEAN |
| `d` | Mark DIRTY |
| `z` | Undo your last mark in this mode (also on the end-of-list screen) |
| `b` | Next batch (on the end-of-list screen) |
| Left / Right | Navigate |
| `n` | Jump to next todo item ("No todo images remaining" in the info bar if none) |
| `u` | Toggle todo-only navigation |
| Space | Toggle autoplay (any other key stops it) |
| `s` | Single mode |
| `m` | Grid mode (rotation as set by `--rotate`; default: only when it saves a grid) |
| `M` | Grid mode (no rotation) |
| `h` | Help screen |
| `w` | Select display |
| `f` | Toggle fullscreen |
| `q` / Escape | Quit |

A gamepad works too, through SDL's game controller mappings, which give every
supported pad the same Xbox-style layout whatever its labels say:

| Button | Action |
|--------|--------|
| B (right face button) | Mark CLEAN |
| Y (top face button) | Mark DIRTY |
| D-pad left / right | Navigate |
| A (bottom face button) | Continue from the help or end-of-list screen, like Space |
| Start | Quit (on any screen) |

B, Y and the D-pad act only on the review screen, and every button stops
autoplay. A pad SDL has no mapping for is not supported; you can supply one
in SDL's mapping format through the `SDL_GAMECONTROLLERCONFIG` environment
variable (one mapping per line).

`c` and `d` (and B and Y) are ignored for 200 ms after an image or grid appears, so a verdict
only applies to an item you have seen.

`z` undoes your most recent mark, a single image or a whole grid, and returns
to that item so you can mark it again (after the same 200 ms). Pressing it again
undoes the mark before that, and so on. Only marks you made in this session
since the last mode switch (`s`, `m`, `M`) can be undone; beyond them `z` says
"Nothing to undo". The history lives in memory only and is gone when you quit.
After a lost connection `z` does nothing; only `q` (or Start) works. An undo appends rows to `review.tsv`
(`mode` `undo`) that restore each image's previous verdict and pass, or, for an
image that had none, mark it `UNREVIEWED` again (a tombstone).

At the end of a batch, `b` moves on to the next batch without restarting. The
end screen says "End of list", or in todo-only navigation "No todo images
remaining" (or "No more todo images this way" when todo images are left in
the other direction; Left/Right wrap round to the others), with the batch's
todo count when it is not 0. `b`
re-reads the current pass (keeping `--pass` if given) and the statuses, then
opens the next batch, in sorted order, that still has todo images in the
current mode, at its first item. The search goes round once, so batches you
skipped earlier (or images skipped in this one) come back. Under
`--filter clean` or `all` an image is todo until you have marked it in this
session, and again if it becomes UNREVIEWED or FLAGGED. When the pass changes
(the last pass ended with DIRTY images) the search starts again from the first
batch, and the info bar says "Now pass N". With `--batch`, `b` stays in that
batch: it reloads it while it has todo images, then says "Batch NAME done for
pass N". When nothing is left it says "All batches done for pass N", or, under
the default `--filter unreviewed` when the pass has just ended, "Pass P
complete - nothing to review in pass N"; "(current pass is M)" is added when
`--pass` differs from the current pass. In grid mode it instead counts the
FLAGGED/DIRTY images grids leave out and asks you to press `s`, which opens
the first batch holding one in single mode. `q` quits. The undo history does
not carry over to the new batch.

The status bar is green for CLEAN, red for DIRTY, gray for UNREVIEWED and
orange for FLAGGED (marked DIRTY in an earlier pass, awaiting this pass's
verdict). The status word is also written at the left end of the bar. Grids are built without DIRTY or FLAGGED images.

`--pass` must be 1 or more. `--batch` defaults to the first batch with images
matching the filter; an empty or unknown batch name is rejected (exit 2) with a
list of known batches. It restricts the session to that batch: `b` at the end
of the list never leaves it.

Every verdict is saved to `review.tsv` in the work directory with who gave it
and how: the columns are `image_id`, `batch`, `status`, `pass_number`,
`timestamp`, `reviewer`, `mode` (`single`, `grid`, or `undo` for a row written
by `z`), `grid_size` (how many images the one keypress covered) and
`tool_version`. `status` is `CLEAN` or `DIRTY`, or `UNREVIEWED` in an undo row
that returns an image to never-reviewed. `--reviewer NAME` (or
`$IMAGE_REVIEW_REVIEWER`) sets the reviewer name, by default your login name;
it must be 1-64 printable characters, not all spaces (no tabs or newlines),
else the command exits 2. The name is an unauthenticated claim made by the client, recorded as
given; nothing verifies it, also with `--remote`. A `review.tsv` from an older
version (five columns) is upgraded in place the first time `review` or `serve`
opens it, with the new columns left empty for its existing rows. Older
image-review versions cannot read the upgraded file, so everyone sharing a
work directory should upgrade together. Likewise, versions before wire API v5
(before undo) reject a `review.tsv` that holds undo rows, so upgrade everyone
sharing a work directory before anyone presses `z`.

Xbox-style controllers are also supported (see help screen for mappings).

`--remote` (or `$IMAGE_REVIEW_REMOTE`) reviews a server started with
`image-review serve` instead of a local `--work-dir`; the two are mutually
exclusive. `--via` (or `$IMAGE_REVIEW_VIA`) reaches that server through an SSH
tunnel via a login node, e.g. `--via user@login.cluster`; it requires
`--remote`. See [Reviewing on an HPC cluster](#reviewing-on-an-hpc-cluster).

### `image-review status`

```
image-review status [--check] [--work-dir DIR | --remote CONNECTION_STRING [--via DESTINATION]]
```

Prints overall and per-batch counts of CLEAN / DIRTY / UNREVIEWED / FLAGGED
images. FLAGGED means marked DIRTY in an earlier pass and not yet re-reviewed
in the current one, so after pass 1 completes its DIRTY images show as FLAGGED:

```
Overall: 6 images (pass 2)
  CLEAN:           4
  DIRTY:           0
  UNREVIEWED:      0
  FLAGGED:         2
```

If preprocess skipped any inputs it also prints
`Skipped during preprocess: F failed, I ignored (see skipped.tsv in the work dir)`;
failed inputs were never shown, so they are not part of the counts above.
`--remote` and `--via` work as for `review`.

With `--check` the report is printed as usual and the exit status says whether
the review is finished, i.e. every image has a verdict: 1 if any image is
UNREVIEWED or any input `failed` to preprocess; 0 otherwise (ignored inputs do
not count). FLAGGED images have a DIRTY verdict from an earlier pass, so they
count as decided: re-review passes are optional. They still show in the report,
so a second pass remains available. Without `--check`, `status` exits 0.

### `image-review export`

```
image-review export [--work-dir DIR] [--output FILE] [--allow-live]
```

Writes the study's result to stdout, or to a new `FILE` with `--output`: one
row per source file, which is CLEAN or DIRTY. The format is tab-separated
UTF-8 with LF line endings and a header row, with no quoting. Export refuses
(exit 1, naming the `image_id`) if any field (`image_id`, `reviewer`, `reason`,
...) holds a control character (tab, CR, LF and the rest of C0, DEL, C1 such as
U+0085), U+2028 or U+2029, or that starts with `"`, since readers could
split or merge rows there. A `"` anywhere else is written as is.

| Column | Description |
|--------|-------------|
| `image_id` | The source file's path, as in `manifest.tsv` / `skipped.tsv`; a file inside a ZIP is `<zip>::<entry>`, one row per entry |
| `status` | `CLEAN`, `DIRTY`, `UNREVIEWED` (no verdict yet) or `NOT_REVIEWED` (preprocess could not render it, or its icon) |
| `pass_number`, `timestamp`, `reviewer` | From the latest verdict on the file's main image; empty without one. After an undo they are the undo's time and reviewer. `reviewer` is the reviewer's unverified claim |
| `reason` | Why the row is not simply the main image's verdict: preprocess's error for a `NOT_REVIEWED` file, `icon DIRTY` / `icon UNREVIEWED` / `icon: <error>` for its icon, `main image missing` (an icon whose file is not in the manifest; the row is then at best `NOT_REVIEWED`); otherwise empty |

- A DICOM's embedded icon (`<path>#icon` in the manifest) is folded into its
  file's row: the row is CLEAN only if the image and its icon both are, else
  DIRTY if either is, else NOT_REVIEWED, else UNREVIEWED.
- Inputs that failed to preprocess (the `failed` rows of `skipped.tsv`) are
  `NOT_REVIEWED`: nobody has looked at them, so treat them as possibly
  containing PHI. This applies even if the manifest also lists them. `ignored`
  inputs (not images) are left out.
- A FLAGGED image (DIRTY in an earlier pass, not yet re-reviewed) is `DIRTY`.
- Rows follow the manifest's order, then `skipped.tsv`'s, one per file.
- Export covers only the files and entries it lists. It never vouches for a
  ZIP or directory as a whole, because ignored members (a DICOMDIR, a PDF, ...)
  are not listed.
- `reviewer` values can start with `=`, `+`, `-` or `@`. Open the file as text
  (e.g. import it as text columns), not by double-clicking it into a
  spreadsheet that would read them as formulas.

`image_id`s are source paths and may hold PHI, so export runs where the work
directory is (e.g. on the cluster); it refuses `--remote` and ignores
`$IMAGE_REVIEW_REMOTE`. It writes nothing in the work directory, and it
refuses (exit 1) while a writer (`review` or `serve`) has the work directory
open, since verdicts may still change. `--allow-live` exports anyway, with a
warning. It always refuses a `review.tsv` whose last line was cut short by an
interrupted write; the next verdict recorded with `review` drops that line.
`--output` never overwrites an existing file. It creates the file in one step
(written to a hidden `.FILE.<random>.tmp` beside it, then linked into place)
with the work directory's file mode, and for a group work directory its group
too (0660). If the group cannot be set, the file is made 0600 with a warning.
The hidden file is removed on every exit except a hard kill (`kill -9`, a node
crash), which can leave it behind: delete it, as it holds source paths. A
writer that opens the work directory while export reads it also makes export
refuse, unless `--allow-live`.

### `image-review serve`

```
image-review serve [--work-dir DIR] [--bind HOST] [--port N]
```

Serves a work directory over HTTPS (self-signed certificate, bearer token) so
a remote client can review it without copying the images. Prints a connection
string (`ir://...`) that grants access: treat it like a password. When stdout
is not a terminal (e.g. `sbatch`), the string is written to
`~/.image-review/connection-<host>-<port>.txt` (mode 0600) instead, and the
file is removed when the server stops.

| Option | Default | Description |
|--------|---------|-------------|
| `--work-dir` | `./review_work` | Work directory containing preprocessed data |
| `--bind` | this machine's FQDN | Hostname or IPv4 address to bind and advertise (wildcard addresses are refused) |
| `--port` | 0 | Port to listen on (0 picks a free port) |

Each start generates a new token and certificate.

## Reviewing on an HPC cluster

Review images where they are, without copying them off the cluster. The
server runs on a compute node and the viewer on your laptop.

```bash
# On the cluster: get an interactive compute node and serve the work dir
salloc ...                      # your site's usual options
srun --pty bash                 # shell on the allocated node (if salloc leaves you on the login node)
image-review serve --work-dir ./review_work
# or in one step: srun --pty image-review serve --work-dir ./review_work

# On your laptop: paste the command `serve` printed, or
image-review review --remote 'ir://...'

# If the laptop can only reach the login node:
image-review review --remote 'ir://...' --via user@login-node
```

Original files, DICOM headers and source paths stay on the cluster; only the
preprocessed JPGs (and their batch/file names and review statuses) travel, over TLS, and are held in the viewer's memory. See
[TUTORIAL.md](TUTORIAL.md#reviewing-on-an-hpc-cluster) for the full workflow,
batch jobs, and [security model and
limitations](TUTORIAL.md#security-model-and-limitations).

If `--remote` reports "server speaks API vN, this client vM" (or "server is too old to report its API version"), install the same image-review version on both machines.

## Multi-Pass Workflow

1. **Pass 1** (grid triage): Mark grids CLEAN or DIRTY. Err toward DIRTY.
2. **Pass 2** (single review): Only images marked DIRTY in pass 1 are shown,
   as FLAGGED (orange status bar). Inspect individually. Grid mode skips
   FLAGGED and DIRTY images, so a grid keypress cannot clear them.
3. **Pass 3+**: Repeat on the shrinking DIRTY pool until confident.

Sessions are resumable -- quitting saves all progress. The batch and pass
number are auto-detected when not specified. Press `b` at the end of a batch to
move on to the next one, and into the next pass once this one is done (not
with `--batch`, which keeps you in that batch).

## Running the tests

From the repository root:

```
python -m unittest discover
```

Lint and type-check (install the tools with `pip install -e ".[dev]"`):

```
ruff check src tests
mypy --python-executable "$(which python)"
```

`mypy` reads its settings from `pyproject.toml`; `--python-executable` points it at the environment that has the
project's dependencies installed.

CI (`.github/workflows/ci.yml`) runs the same checks, plus `ruff format --check src tests`, on Linux and macOS
with Python 3.12 and 3.13. To keep the one-off reformat out of `git blame`:

```
git config blame.ignoreRevsFile .git-blame-ignore-revs
```
