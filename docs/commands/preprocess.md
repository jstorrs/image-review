# `image-review preprocess`

```
image-review preprocess SOURCE [SOURCE ...] [--batch-size N]
                                            [--work-dir DIR]
                                            [--colormap NAME]
                                            [--access {private,group}]
                                            [--allow-skipped]
                                            [--jobs N]
```

Renders each image input to a JPG, in batches, in a new work directory.
Inputs it cannot render are listed in `skipped.tsv` (see
[Skipped inputs](#skipped-inputs)).

| Option | Default | Description |
|--------|---------|-------------|
| `--batch-size` | 300 | Images per batch (1 or more) |
| `--work-dir` (alias `--output-dir`) | `./review_work` | Work directory to create |
| `--colormap` | `inferno` | Matplotlib colormap for rendering |
| `--access` | `private`; `$IMAGE_REVIEW_ACCESS` | Who may use the work directory; see [Access control](#access-control) |
| `--allow-skipped` | off | Exit 0 even if some inputs failed to preprocess |
| `--jobs` | `$SLURM_CPUS_PER_TASK`, else the usable CPUs | Worker processes; see [Parallel rendering](#parallel-rendering) |

**Inputs.** Each SOURCE is a ZIP file, a directory (searched recursively,
including ZIP files inside it) or an individual file. Inputs are recognized by
content, not by extension: DICOM (including extensionless files like `IM0001`
and DICOM without the 128-byte preamble), PNG, JPEG, TIFF, BMP, GIF, WebP,
JPEG 2000 and PNM. Symlinked directories are never entered: a link to an
enclosing directory or into another SOURCE is ignored, and any other is
failed with its target, so pass that target as a SOURCE if you want it. The
work directory is never read as input.

**Rendering.**

- DICOM images are normalized with adaptive histogram equalization, to
  enhance local contrast, and a configurable colormap. Single-frame color and
  palette DICOMs are shown as they are.
- DICOM overlay planes are drawn at maximum brightness, and an embedded icon
  image becomes an extra manifest row whose image id ends in `#icon`.
- Non-DICOM images are converted to RGB, with the same contrast enhancement
  applied to grayscale.
- Transparent images are shown as the composite over mid-gray beside the raw
  channels with alpha ignored.
- MPO JPEGs (HDR gain maps, previews) show all their frames side by side.

**Output.** Images are written to batch subdirectories with a `manifest.tsv`
index, which records for each JPG the SHA-256 of its source file (or ZIP
entry) and of the JPG itself. Viewing an image checks it against that hash: a
JPG changed or cut short after preprocessing is shown as an unloadable
placeholder (DIRTY only), never as an image that could be marked CLEAN.

`preprocess.json` beside the manifest records the tool and library versions,
the resolved SOURCES, the rendering parameters and the input counts. Like the
manifest, it holds source paths and stays in the work directory; the server
never sends it.

**A new work directory every time.** The work directory must not already
exist (an empty directory is fine). `preprocess` refuses to write into one
that has content, so verdicts can never be attached to a replaced image:
choose a new `--work-dir` or remove the old one.

Output is built in a staging directory next to it (`.NAME.partial`, with the
access policy's directory mode) and renamed into place only on success, so an
interrupted run leaves no work directory behind. If a crash leaves
`.NAME.partial` behind, the next run says so; remove it and re-run.

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
  (`Shared with Unix group 'study' (gid N)`).
- POSIX default ACLs on the parent can add named user/group entries (check
  with `getfacl`; the tool does not manage ACLs), but files never get "other"
  bits.

`review`, `serve` and `status` print a warning if the work directory or its
`manifest.tsv` is accessible to other users (e.g. one made by an older
version). They never change an existing directory's mode: run
`chmod -R o-rwx <work dir>`.

Only one writer (`review` or `serve`) can use a work directory at a time: it
holds `review.lock` there, and a second writer exits with an error naming who
holds it (`status` is read-only and always works). A team shares a work
directory sequentially or splits a study into several work directories.

## Skipped inputs

Every input ends up in exactly one of `manifest.tsv` (rendered) or
`skipped.tsv`, with one of two kinds:

- `failed`, e.g.:
  - a corrupt file;
  - a `.jpg`/`.png`/... or `.zip` whose content is not one;
  - a `.tar.gz` or other non-ZIP archive;
  - an `unsupported:` multi-frame DICOM;
  - an unreadable subdirectory;
  - a symlinked directory outside the sources;
  - a file named on the command line that is not an image;
  - an input whose image id collides with another's;
  - a file or ZIP entry whose name, or a directory above it, is not UTF-8 or
    holds a control character such as a newline or U+2028/U+2029, even one
    that would otherwise be ignored. It is listed under an escaped name like
    `a\x0ab.png` or `a\u2028b.png`: rename it.
- `ignored` (not an image): unrecognized content, macOS AppleDouble files, a
  DICOMDIR index, an empty ZIP.

The run finishes with a summary line
(`Found N inputs: wrote K images in B batches; S skipped (F failed, I ignored; see .../skipped.tsv)`)
and exits 1 if any input failed, unless `--allow-skipped` is given. Check
`skipped.tsv` before reviewing: those images will not be shown.

## Parallel rendering

`--jobs N` renders inputs in N worker processes; `--jobs 1` renders in the
main process. The default is `$SLURM_CPUS_PER_TASK` when set, else the CPUs
the process may use, capped by the smallest cgroup v2 CPU quota of the
process's cgroup and its ancestors (such as a login node's per-user
`CPUQuota=` or a container's limit).

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
