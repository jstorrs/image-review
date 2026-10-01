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

`cryptography` is a dependency (used by `serve` for its TLS certificate).
`review --via` and `status --via` need an OpenSSH client on the machine you
run them on (built into macOS, Linux and Windows 10+).

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
`chmod -R o-rwx <work dir>`. Only one writer should use a work directory at a
time, so a team shares one sequentially or splits a study into several work
directories.

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
                    [--filter {unreviewed,clean,all}]  [--rotate/--no-rotate]
                    [--remote CONNECTION_STRING [--via DESTINATION]]
```

Opens a fullscreen interactive session. In **grid mode**, images are
bin-packed into composite grids for fast triage. In **single mode**, images
are shown one at a time for detailed inspection.

| Key | Action |
|-----|--------|
| `c` | Mark CLEAN |
| `d` | Mark DIRTY |
| Left / Right | Navigate |
| `n` | Jump to next todo item |
| `u` | Toggle todo-only navigation |
| Space | Toggle autoplay |
| `s` | Single mode |
| `m` | Grid mode (rotation allowed) |
| `M` | Grid mode (no rotation) |
| `h` | Help screen |
| `w` | Select display |
| `f` | Toggle fullscreen |
| `q` / Escape | Quit |

Xbox-style controllers are also supported (see help screen for mappings).

`--remote` (or `$IMAGE_REVIEW_REMOTE`) reviews a server started with
`image-review serve` instead of a local `--work-dir`; the two are mutually
exclusive. `--via` (or `$IMAGE_REVIEW_VIA`) reaches that server through an SSH
tunnel via a login node, e.g. `--via user@login.cluster`; it requires
`--remote`. See [Reviewing on an HPC cluster](#reviewing-on-an-hpc-cluster).

### `image-review status`

```
image-review status [--work-dir DIR | --remote CONNECTION_STRING [--via DESTINATION]]
```

Prints overall and per-batch counts of CLEAN / DIRTY / UNREVIEWED images.
`--remote` and `--via` work as for `review`.

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

## Multi-Pass Workflow

1. **Pass 1** (grid triage): Mark grids CLEAN or DIRTY. Err toward DIRTY.
2. **Pass 2** (single review): Only DIRTY images are shown. Inspect individually.
3. **Pass 3+**: Repeat on the shrinking DIRTY pool until confident.

Sessions are resumable -- quitting saves all progress. The batch and pass
number are auto-detected when not specified.
