# image-review Specification

## Purpose

`image-review` is a CLI tool for reviewing medical (DICOM) and general images
for burned-in Protected Health Information (PHI). It provides a three-phase
workflow: **preprocess** raw images into normalized JPGs, **review** them
interactively in a fullscreen viewer, and report **status** on review progress.

## Requirements

- Python >= 3.12
- Dependencies, with the minimums declared in `pyproject.toml`, grouped by
  what needs them:
  - core (always installed; enough for `serve`, `status`, `export` and every
    `--help`): click >= 8.2, cryptography >= 41 (`serve`'s TLS certificate);
  - extra `preprocess`: matplotlib >= 3.7.3, numpy >= 1.26, pydicom >= 3.0,
    Pillow >= 10.3 except 11.x (11.x misdecodes an MPO frame whose mode
    differs from the one before), scikit-image >= 0.22, scipy >= 1.11.2,
    tqdm >= 4.60 (`tqdm.contrib.logging`);
  - extra `codecs`: the DICOM codecs python-gdcm >= 3.0.25 (JPEG
    baseline/extended/lossless, JPEG-LS, JPEG 2000, RLE; older releases decode
    a corrupt JPEG 2000 codestream without an error) and pylibjpeg >= 2.0 +
    pylibjpeg-openjpeg >= 2.0 (JPEG 2000, HTJ2K). Kept separate from
    `preprocess`: without it, pydicom decodes only what it can by itself or
    through Pillow (e.g. RLE, JPEG 2000), and any other compressed DICOM (e.g.
    JPEG Lossless, JPEG-LS) is `failed` with `cannot decode <transfer syntax
    name>: ...` (see the DICOM preprocessing pipeline, step 0);
  - extra `viewer` (`review`, local or `--remote`): pygame-ce >= 2.3.1,
    Pillow (as above), rectpack == 0.2.2 (unmaintained; pinned because grid
    packing depends on its exact behaviour);
  - extra `all` = `preprocess`, `codecs` and `viewer`; extra `dev` = `all`
    plus ruff, mypy, types-tqdm and coverage (the tests need every extra).

  Most minimums are the oldest release with a CPython 3.12
  wheel; each was checked by running the test suite on CPython 3.12. click
  8.2 is needed only by the tests (`CliRunner` with separate stderr).
  `pylibjpeg-libjpeg` is deliberately not used (GPL-3), so 12-bit JPEG
  Extended (Process 4) and JPEG-LS with 6- or 7-bit samples cannot be decoded
- The CLI imports an extra's modules only in the command that needs them
  (`cli.requires_extra`). A command whose extra is missing exits 1 with
  `this command needs the <extra> extra: pip install 'image-review[<extra>]'`;
  only a `ModuleNotFoundError` naming one of that extra's own top-level
  modules is translated, so any other import failure (one of image-review's
  modules, a broken install) surfaces unchanged
- `review --via` / `status --via` additionally need an OpenSSH client (`ssh`)
  on the client machine

## Architecture

```
cli.py              Command-line entry point, argument parsing
preprocess.py       DICOM/image loading and normalization
status.py           Status, Verdict, MarkMode, Rotation, TODO_STATUSES vocabulary (stdlib only)
store.py            ReviewStore Protocol, LocalStore, pure filter/summary functions
lock.py             The work directory's review.lock: acquire, release, live_writer (stdlib only)
export.py           Export rows, the allowlist split and their TSV formats: export_rows, split_allowlist, format_allowlist, format_report (stdlib only)
atomic.py           write_new_file: create a file atomically, never overwriting (stdlib only)
server.py           HTTPS + bearer-token server exposing a ReviewStore
connection.py       RemoteTarget: the ir:// connection string; API_VERSION, package_version, parse_reviewer
remote.py           RemoteStore: ReviewStore client with certificate pinning
tunnel.py           SSH local port-forward for --via
signals.py          interrupt_on: SIGTERM/SIGHUP to KeyboardInterrupt, shared by serve, review and the tunnel
controller.py       Review session orchestration and event loop
viewer.py           Fullscreen pygame display
grid_packer.py      Review-time bin-packing of images into grids
review_db.py        Persistent review state (review.tsv)
util.py             Shared utilities (surface loading)
```

All review-time data access goes through a `ReviewStore`. The controller and
grid packer never touch the work directory; `LocalStore` serves it directly,
and `RemoteStore` talks to an `image-review serve` process that wraps a
`LocalStore`. `status.py`, `store.py`, `lock.py`, `export.py`, `atomic.py`, `server.py`,
`connection.py`, `remote.py`, `tunnel.py` and `signals.py` import without pygame,
numpy or skimage.

```
review (pygame)             serve (compute node)
  ReviewSession                 ReviewServer (HTTPS)
    |                             |
  ReviewStore  --RemoteStore--> LocalStore --> ReviewDB --> review.tsv
    |            (TLS, pinned;      |
  LocalStore      optional ssh -L)  +--> manifest.tsv, batch_NNN/*.jpg
```

## CLI Interface

Entry point: `image-review` (mapped to `image_review.cli:main`).

### Logging

Diagnostics go through the stdlib `logging` module; each module logs to
`logging.getLogger(__name__)` under the `image_review` package logger, which the
CLI group configures for the duration of a command (one handler, no
propagation to the root logger; third-party loggers are left alone).

- **Destination and format**: stderr, one line per record:
  `%(asctime)s %(levelname)s %(name)s: %(message)s`, with `asctime` as strict
  ISO 8601 local time with its UTC offset (`2026-10-01T14:03:07+02:00`, second
  resolution; `cli.LogFormatter`). During `preprocess` the handler is swapped
  by `tqdm.contrib.logging.logging_redirect_tqdm`, so records are written with
  `tqdm.write` and appear above the progress bars instead of through them.
  The package logger name is `cli.PACKAGE_LOGGER`; `cli` logs as
  `image_review.cli` even when run as `python -m image_review.cli`.
- **Levels**: `-v/--verbose` (a group option, before the command name) logs
  DEBUG and up (currently few: the work directory opened, the server address
  connected to, the ssh tunnel command line, and each grid build's image,
  grid and left-out counts and rotation); `-q/--quiet` logs only WARNING and
  up; the default is INFO.
  The two flags together are a usage error (exit 2). ERROR: the review session
  lost its server, a server request failed with a 500. WARNING: an image that
  cannot be loaded or was left out of every grid, an input skipped by
  preprocess, a torn last line of `review.tsv`, a world-accessible work
  directory, a refused verdict, a failed server connection, `export --allow-live`
  overriding a held lock, an `export --output` or `--report` file that could not be given
  the work directory's group. INFO: the server's
  request log, and `export`'s one-line count of allowlisted and reported
  files. Nothing else is logged at INFO, so the default stays quiet
  outside `serve` and `export`.
- **Never logged by the server**: access tokens, `Authorization` headers,
  query strings, image keys, `image_id`s and source paths, request bodies, and
  the messages of exceptions raised while serving (only their class names).
  Nothing logs a token. Client-side warnings (`review`, `status`) do name image
  keys, and `preprocess` warnings name the failed source file (its
  `image_id`), on the machine where preprocess runs. Client-supplied fields in
  server records are escaped with Python's `unicode_escape`, so control
  characters cannot reach the terminal.
- **Not diagnostics**: the CLI's own output stays plain `print`/`click.echo`:
  the `preprocess` summary, `status` tables, `export`'s allowlist TSV, `serve`'s connection-string
  instructions, the review session's start and "nothing to review" lines (all
  stdout), and error messages of failed commands (`Error: ...`, stderr, exit 1
  or 2).

### `image-review preprocess`

```
image-review preprocess SOURCE [SOURCE ...] [--batch-size N]
                                            [--work-dir DIR]
                                            [--colormap NAME]
                                            [--access {private,group}]
                                            [--allow-skipped]
                                            [--jobs N]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `SOURCE` | (required) | One or more ZIP files, directories, or individual files |
| `--batch-size` | 300 | Maximum images per batch subdirectory (integer >= 1) |
| `--work-dir` | `./review_work` | Work directory for all output (alias: `--output-dir`); must not exist or be empty |
| `--colormap` | `inferno` | Matplotlib colormap applied to DICOM grayscale (unknown names exit 2 before any output) |
| `--access` | `private` | `private` (dirs 0700, files 0600) or `group` (dirs 2770, files 0660); env `IMAGE_REVIEW_ACCESS` |
| `--allow-skipped` | off | Exit 0 even if some inputs failed (they are still listed in `skipped.tsv`) |
| `--jobs` | `$SLURM_CPUS_PER_TASK`, else the usable CPUs | Worker processes rendering in parallel (integer >= 1; 0 exits 2 before any output). Resolved when the command runs from the usable CPUs (`len(os.sched_getaffinity(0))` where available, else `os.cpu_count()`, else 1): `$SLURM_CPUS_PER_TASK` capped at the usable CPUs if it is a whole number >= 1 (ASCII or other decimal digits only), else the usable CPUs capped at the smallest cgroup v2 CPU quota `ceil(quota / period)` among the `cpu.max` files of the process's own cgroup (the `0::<path>` line of `/proc/self/cgroup`, under `/sys/fs/cgroup`) and each ancestor up to the mount root, e.g. a login node's per-user `CPUQuota=` on `user-UID.slice`, or a container's quota at its namespace root (`0::/`). `max`, a missing or unreadable file, or an unreadable `/proc/self/cgroup` means no cap at that level; a path outside the cgroup namespace (`..`) reads only the mount root. cgroup v1 quotas are not read |

The pipeline has three parts: **discovery** (IO) yields one `Candidate`
(`image_id`, `kind` = `dicom` or `raster`, and its raw
bytes, read during discovery) per input without decoding anything (or a `SkippedRow` for content
that is not an input or cannot be read); a pure **`render(kind, image_id,
data, colormap) -> tuple[Rendered, ...]`** (never empty: the main image first)
turns the bytes into `(H, W, 3)` uint8 RGB images; each image is then JPEG-encoded in memory; and a **writer** saves
the encoded bytes the moment they are produced and, once collisions are
known (see *Collisions*), records them in the manifest. Each input's bytes are read
once; their SHA-256 becomes the `source_sha256` of every image rendered from
them (a DICOM and its icon share it), and the SHA-256 of each JPG's encoded
bytes its `jpeg_sha256` (see *`manifest.tsv`*).

**Parallel rendering** (`run_preprocess(..., jobs)`, CLI `--jobs`). Hashing,
rendering and encoding one input is the pure, top-level
`render_and_encode(candidate, colormap) -> list[Encoded | SkippedRow]`.
With `jobs` = 1 it runs in the main process and no pool exists. With `jobs` >
1 the main process submits each candidate's bytes (read during discovery) to a `ProcessPoolExecutor(jobs)` using
the `spawn` start method on every platform (workers import the package afresh:
no inherited threads, signal handlers or open archives); workers hash the
bytes and return JPEG bytes or `SkippedRow`s. While the pool exists,
`OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS` and `MKL_NUM_THREADS` are set to 1 in
the main process's environment (only those the user has not set; restored
afterwards), so each worker inherits single-threaded BLAS instead of a thread
per core. At most `2 × jobs` inputs are submitted and not yet consumed: the
main process holds their raw bytes, each worker one input and its decoded
arrays. Results are consumed in input
order, so the main process writes every file and row in the same order as
with `jobs` = 1 and the output is byte-identical (only `preprocess.json`'s
`created` and `parameters.jobs` differ). Workers ignore SIGINT from the start
(SIGINT is blocked while they are spawned and the initializer ignores, then
unblocks it), so Ctrl-C reaches only the main process. The CLI runs preprocess
under `interrupt_on(SIGTERM, SIGHUP)`. On any exception in the main process
(Ctrl-C, SIGTERM, a dead worker, a write error) the workers are killed and
joined, the main process closes its copy of the pool's result-pipe write end
(so a manager thread reading a result cut off mid-send sees EOF instead of
waiting forever), and the pool is shut down with queued inputs cancelled,
all before the staging directory is removed. A worker that dies fails the run
with `WorkerCrashed` (CLI: exit 1, naming how many inputs were in flight and
the first, and suggesting `--jobs 1`) rather than retrying in the main
process, where the same input could kill the whole run without a message.
The death is seen either as `BrokenProcessPool` or, when the worker died
mid-send and the pool cannot notice, by the main process checking every
second while it waits that no worker has exited (without
`max_tasks_per_child`, workers exit only at shutdown).

**Work directory lifecycle.** The run builds everything (batch directories,
JPGs, `manifest.tsv`, `skipped.tsv`, `preprocess.json`) in a staging directory
`<parent>/.<name>.partial`, created with the access policy's directory mode
(not `exist_ok`), and renames it to the work directory only on success; the
final directory keeps that mode. If the work directory already exists and is not an empty directory
(or is a file) the run is refused with `WorkDirExists` (a `ValueError`; the CLI
exits 1 asking for a new `--work-dir` or removal of the old one); an existing
empty directory is replaced by the rename. If the staging directory already
exists, a previous run did not finish (a crash or kill -9) and the run is
refused naming it; remove it and re-run. On any error or interrupt
(including `KeyboardInterrupt`) the staging directory is removed and the work
directory is never created. There is no `--force`. Existing work directories
are never modified, so a running `serve` is not affected.

**Access policy** (`access.py`, stdlib only). `run_preprocess(..., access)`
takes `Access = Literal["private", "group"]` (CLI `--access`, env
`IMAGE_REVIEW_ACCESS`, default `private`). `modes(access)` returns frozen
`Modes(dir_mode, file_mode, umask)`: private is (0700, 0600, 0077); group is
(2770, 0660, 007), setgid so new entries inherit the directory's group.
Nothing is ever created with an "other" bit. The run holds `os.umask(umask)` for
its whole duration (restored in `finally`, including on error), creates the
staging dir and every batch dir and then `chmod`s them to `dir_mode`
explicitly (mkdir's mode is masked by the umask and does not set setgid), and
creates every file with `os.open(O_CREAT|O_EXCL, file_mode)` (a POSIX default
ACL on the parent makes Linux ignore the umask, so the mode is passed
explicitly; the umask stays as a second guard). Missing parents of the work dir
are created before the umask is set. If the staging chmod fails the staging dir
is removed. The policy is only mode bits: no chgrp, no ACL handling (default
ACLs on the parent can add named user/group entries; check `getfacl`; files
never get "other" bits). With `--access group` the CLI prints
`Shared with Unix group '<name>' (gid N)` from the work dir's gid. A
pre-created empty work dir's group and mode are not kept (it is replaced by the
staging dir). It is not stored: `access_of(st_mode)` recovers it from an existing
work directory (`group` if the group bits are rwx, else
`private`), and later writers follow it. `ReviewDB` opens `review.tsv` with
`os.open(O_APPEND | O_CREAT, file_mode)` and, when the file is new or empty and
its mode differs from that policy's `file_mode` (the umask may have narrowed
it), `fchmod`s it, so a new `review.tsv` is 0600 in a private and 0660 in a
group work directory. An existing, non-empty file keeps whatever mode it has,
except that migrating an old-header file (see *`review.tsv`*) writes the new
file with the policy's `file_mode`.
`review`, `serve` and local `status` call `world_access_warning`: if the work
directory or `manifest.tsv` has any other bit, they log a WARNING
`<path> is accessible to all users (mode NNNN); run `chmod -R o-rwx
<work dir>`` (group bits alone are silent). Existing directories are
never chmod'ed automatically. A team shares a work directory sequentially
(one writer at a time, enforced by `review.lock`; see *Concurrency limits*) or
splits a study into several work directories.

**Source loading.** `discover(sources, exclude)` yields every input,
classified by content. `run_preprocess` passes the resolved work directory and
staging directory as `exclude`; nothing under them is ingested (resolved
paths compared), so a work dir inside a source (e.g. `preprocess .` with the
default `./review_work`) never re-ingests its own output.

Every image_id is minted in discovery and checked once as `discover` yields
it: an input whose name (any part of its id: the file path, or a ZIP's path
and entry name) is not valid UTF-8 (a byte the file system name decodes only
as a surrogate) or holds a control character (category `Cc`, including tab,
CR, LF and U+0085) or U+2028/U+2029 becomes a `failed` row, whatever it would
otherwise have been (even `ignored`), with reason `name is not UTF-8 or holds a
control character or line separator; rename it`. Its image_id is the escaped
name: a non-UTF-8 byte (0x80-0xFF) or an ASCII control (below 0x80) as `\xNN`,
any other such character (C1 controls, U+2028/U+2029) as `\uNNNN`, so a raw
byte and a character never share a spelling; every other character, including
`\`, is kept, so ids of other inputs are unchanged. Such an id cannot be told from a real name spelling
the same escape; if the two meet, `export` reports that id `NOT_REVIEWED`
(a `failed` row wins), never `CLEAN`. So `manifest.tsv` and `skipped.tsv` always hold valid UTF-8
that `export` accepts, and the run never stops on a name.

| Source type | Behavior |
|-------------|----------|
| Directory | One `os.walk(followlinks=False)`, directory and file names sorted at each level, top-down (a directory's files before its subdirectories). Every file is classified; a ZIP file yields its entries. Symlinked files are read; a dangling symlink is `failed`. A symlinked directory is never entered (it could lead out of the source, e.g. `up -> ..` or a link to `/`); by its resolved target it is: inside `exclude` → skipped silently; the link's own directory or an ancestor of it (so the source root and its ancestors) → `ignored`, `symlink to an enclosing directory`; inside or equal to a directory given as a SOURCE in this run (including the one being walked) → `ignored`, `symlinked directory already included via SOURCE <path>`; anything else → `failed`, `symlinked directory not followed; pass its target <resolved path> as a SOURCE` (both paths escaped like an image_id, see above). A non-regular file (FIFO, socket, device) is `ignored` (`not a regular file`). An unreadable directory (including the source itself) is one `failed` row for that directory's path |
| ZIP (by content, wherever found) | Every non-directory entry from `infolist()`, classified by its first bytes. A ZIP entry → `failed`, `unsupported: nested zip`. An entry that cannot be read (encrypted, unknown compression) → `failed`. An archive that cannot be opened → one `failed` row for the archive path. An archive with no file entries (empty, or directories only) → one `ignored` row for the archive path, `zip contains no files`. Office Open XML, ODF and EPUB files are ZIPs: their embedded images are reviewed, their XML parts are `ignored`, and embedded workbooks or packages are nested ZIPs (`failed`) |
| Single file | Classified like a file in a directory, except that a file that would be `ignored` is `failed` with the same reason (it was named explicitly, e.g. `notes.txt`, a FIFO); AppleDouble stays `ignored`. Rows from inside a named ZIP are unchanged |

`classify(name, head)` is pure over the name and the first 132 bytes (the
whole file is read only when the input is rendered):

| Content | Result |
|---------|--------|
| `DICM` at offset 128 | `dicom` |
| `PK\x03\x04` or `PK\x05\x06` (empty archive) | ZIP (its entries) |
| gzip (`\x1f\x8b`), bzip2 (`BZh` + digit), xz, zstd, 7z or rar signature | `failed`: `unsupported: <format> archive` |
| PNG, JPEG, TIFF, BigTIFF, GIF, WebP (`RIFF` + `WEBP` at 8), JPEG 2000 (JP2 box or J2K codestream), PNM (`P1`-`P6` + whitespace), BMP (`BM` + a DIB header size of 12, 40, 52, 56, 64, 108 or 124 at offset 14), or HEIF/AVIF (`ftyp` at 4 with brand `heic`, `heix`, `hevc`, `hevx`, `heim`, `heis`, `mif1`, `msf1`, `avif` or `avis`) signature | `raster` (Pillow decides) |
| AppleDouble signature (`\x00\x05\x16\x07`), or none of the above and the name starts with `._` or has a `__MACOSX` component | `ignored`: `AppleDouble metadata (macOS resource fork)` |
| A bare DICOM dataset: little-endian group `0002` or `0008` at bytes 0-1, then an explicit VR at bytes 4-5 or an even implicit length of at most 256 at bytes 4-7 (names such as `IM0003`, `I.001`, UID-named files) | `dicom` |
| None of the above, name ends in `.dcm`, `.dicom` or `.ima` (case-insensitive) | `dicom` (the reader decides) |
| None of the above, name ends in an image suffix (`.jpg .jpeg .png .tif .tiff .bmp .gif .webp .jp2 .j2k .jpx .pbm .pgm .ppm .pnm .heic .heif .avif`, case-insensitive) | `raster`: Pillow decides, so a damaged image is `failed` with Pillow's error, never dropped |
| None of the above, name ends in `.gz .tgz .tar .7z .rar .bz2 .xz .zst` (tar's `ustar` is at offset 257, beyond the sniffed bytes) | `failed`: `unsupported: <format> archive` |
| None of the above, name ends in `.zip` | `failed`: `unrecognized content for a .zip file` (a login page or truncated download) |
| Anything else | `ignored`: `not an image (unrecognized content)` |

DICOM is read with `dcmread(force=True)`. Without `DICM` at offset 128 the
parsed dataset must have a SOP Class UID or pixel data (an ACR-NEMA image may
have only pixel data), else it is `InvalidDicomError: not DICOM (no preamble,
no SOP Class UID and no pixel data)` when its first element is in group
`0002`/`0008`, or `not DICOM (no preamble and no recognizable DICOM
elements)` otherwise; so a mislabelled `.dcm` never fails as `unsupported: no
pixel data`. The SOP class name in `unsupported: no pixel data (<name>)` is
given only for a single UID value. For a bare dataset the transfer
syntax is set from the encoding pydicom detected (uncompressed by definition).
A DICOM without pixel data whose SOP class is DICOMDIR
(`1.2.840.10008.1.3.10`) is `ignored`: `DICOMDIR index`.
ZIP entries are read by their `ZipInfo`, so entries that share a name are
distinct inputs: the first keeps `{zip}::{name}`, later ones get
`{zip}::{name}#2`, `#3`, ... These can clash with a real entry literally
named `{name}#2` (see *Collisions*). Manifest rows follow discovery order
(sources in the order given, each walked as above).

**Image IDs** are fully-resolved absolute paths derived from the source:
- ZIP entry: `{absolute_zip_path}::{filename}` (also for a ZIP inside a directory)
- Directory: absolute path to each file (a symlinked file keeps its link path)
- Single file: fully-resolved absolute path

A DICOM whose `IconImageSequence` (0088,0200) has an item also yields a second
output row, `{image_id}#icon`, for the embedded thumbnail (item 0, rendered
like a single-frame DICOM image of its own: grayscale through the colormap,
colour as is). The main image keeps its `image_id`. If the icon cannot be
rendered the main image is still written and `{image_id}#icon` is a `failed`
row in `skipped.tsv`. If the main image fails the file is `failed` and has no
icon row.

**Collisions.** Verdicts are stored per `image_id`, so an `image_id` must
name one image. The naming is not escaped (changing it would orphan existing
`review.tsv` rows), so different inputs can produce the same `image_id`: a ZIP
entry literally named `a.png#2` beside two `a.png` entries; a file literally
named `z.zip::a.png` beside `z.zip` holding `a.png`; a file literally named
`a.dcm#icon` beside an `a.dcm` with an icon. After every input is rendered,
the pure `colliding_ids(images)` takes the `(image_id, source_sha256,
jpeg_sha256)` of every rendered image and returns the `image_id`s that name
more than one distinct `(source_sha256, jpeg_sha256)` pair: a different image,
or the same JPG from a different source (two sources that render identically
still collide, since `export` attests one `source_sha256` per `image_id`). Every input that rendered any such image (main or icon) is replaced by
one `failed` row for its own `image_id` (so a DICOM whose main image and icon
both collide is listed once, and a DICOM whose icon collides loses its main
image too), reason `image_id collides with another input (rename one of
them)`, with a logged warning; its JPGs are deleted and never appear in the
manifest. `{path}` and `{path}#icon` from one DICOM are distinct ids, never a
collision. The same source reached twice under one `image_id` (a file reached
through overlapping SOURCEs, so the same pair) is not a collision either: one verdict is right
for both, and both rows stay in the manifest (see *Key versus `image_id`*).
Only rendered images are compared: a `failed` or `ignored` row sharing an
`image_id` with an image changes nothing (`export` already reports an
`image_id` with a `failed` or `ignored` row as `NOT_REVIEWED`).

**DICOM preprocessing pipeline** (`preprocess_dicom`):

0. Compressed pixel data is decoded by pydicom with the codecs above. A file
   whose decode fails is `failed` with reason
   `cannot decode <transfer syntax name>: <ExceptionClass>: <first line of the message>`
   (e.g. `cannot decode JPEG Extended (Process 2 and 4): RuntimeError: Unable to
   decode as exceptions were raised by all available plugins:`); uncompressed
   files keep the plain `<ExceptionClass>: <message>` reason.
1. Only single-frame images with pixel data are rendered. No pixel data,
   `NumberOfFrames` above 1 (checked first, whatever the photometric
   interpretation), or a photometric interpretation other than the ones below
   raises `Unsupported` (e.g.
   `unsupported: no pixel data (Basic Text SR Storage)` naming the SOP class
   when known, `unsupported: multi-frame DICOM (3 frames)`,
   `unsupported: photometric interpretation HSV`). Multi-frame DICOMs are not
   split into per-frame items. `RGB`, `YBR_*` (pydicom's `pixel_array`
   returns RGB) and `PALETTE COLOR` (expanded with `apply_color_lut`) images
   skip steps 2-5: samples are scaled to uint8 (by `BitsStored`, or the
   palette's entry bit depth, when above 8 bits), no windowing or colormap is
   applied, and only the crop of step 6 follows. Steps 2-5 apply to
   `MONOCHROME1`/`MONOCHROME2`
2. Extract pixel array, convert to float32 (values keep their native scale)
3. Correct photometric interpretation (invert MONOCHROME1 by negation)
4. Compress the intensity tails: the robust core range (1st-99th percentile
   of the values strictly inside the outer 1% of the min-max range, then
   shrunk by a 2% margin) maps to [`TAIL_FRACTION`, 1 - `TAIL_FRACTION`]
   (0.10-0.90), and the values below/above it are interpolated linearly into
   the remaining tails [0, 0.10] and [0.90, 1] rather than clipped, so
   extremes such as burned-in text at the maximum stay distinguishable from
   a bright core. An image with a single value maps to all zeros; when no
   robust core exists (no inner values, or the core is empty or touches
   the min or max) a plain min-max rescale is used. Then overlay planes
   (see below) are drawn at 1.0
5. Apply CLAHE (adaptive histogram equalization, 96-pixel tiles)
   (overlay pixels stay at or near the top of the range)
6. Strip uniform rows/columns (`compress_image` -- removes letterboxing). If
   stripping would leave nothing (e.g. an all-zero image), the uncropped
   image is kept
7. Apply colormap (grayscale only), save as 8-bit RGB JPG (quality 95, 4:4:4 chroma, no subsampling)

**Overlays**: each overlay plane (even group 0x6000-0x601E with OverlayData
`(g,3000)`) is decoded with `overlay_array`, placed at its OverlayOrigin
(1-based row, column; default 1, 1) and clipped to the image. For grayscale
images the overlay pixels are set to 1.0 after intensity compression (step 4)
and before the first crop, so they come out near 255 (CLAHE keeps them at the
top of the range); for colour and palette images they are set to white (255)
before the crop. A plane that cannot be decoded fails the whole file
(fail-closed, so a reviewer never sees an image with undrawn annotations):
`ValueError: overlay 0x6002 cannot be decoded: ...`. Overlays embedded in the
unused bits of PixelData are not drawn.

**Raster preprocessing** (`decode_raster` + `preprocess_raster`, for PNG/JPEG/TIFF/BMP/GIF):
- Decode with Pillow and branch on the image mode, not the channel count
- Each frame is decoded to float [0, 1]. Several views of one input are
  placed side by side in a single rendered image (one manifest row, one
  `image_id`), top-aligned, separated by a 4-pixel gap
- MPO files (JPEGs carrying extra MPF images: HDR gain maps, camera
  previews) render every frame side by side at native resolution, padded and
  separated with black. Any other multi-frame image (animated PNG/GIF,
  multi-page TIFF) raises `unsupported: multi-frame image (N frames)`
- Modes with real alpha (`RGBA`, `LA`, `PA`, premultiplied variants, or any
  mode with a `transparency` entry such as a palette with a transparent
  index or a grayscale/RGB colour key): if alpha is fully opaque it is
  dropped. Otherwise the rendered image shows, side by side with a mid-gray
  gap, the composite over mid-gray (left; content carried only by alpha stays
  visible) and the raw colour/gray channels with alpha ignored (right;
  content hidden under transparent pixels stays visible). Grayscale+alpha
  stays grayscale
- High-bit-depth grayscale (`I;16*` scaled by 65535, `I`/`F` min-max scaled)
  keeps its full range; a `tRNS` colour key is matched against the native
  values to build the alpha mask
- Grayscale modes (`1`, `L`, `I;16*`, `I`, `F`) stay grayscale; every other
  mode (`RGB`, `CMYK`, `YCbCr`, `P`, ...) is converted to RGB by Pillow (so
  CMYK black text stays black)
- Grayscale: apply CLAHE, convert to 8-bit, and stack to 3 channels
- Colour: convert to 8-bit RGB

**Error handling**: Every input is accounted for exactly once, in either
`manifest.tsv` or `skipped.tsv`. Any exception while reading, decoding,
rendering or JPEG-encoding one input (e.g. an image wider than libjpeg's
65500-pixel limit) becomes a `failed` row in `skipped.tsv` with reason
`<ExceptionClass>: <message>` (or the `unsupported: ...` message), with tabs
and runs of other control characters, U+2028/U+2029 and non-UTF-8 bytes (surrogates) replaced by one space, and object reprs such as `<_io.BytesIO object at 0x...>` replaced by `<data>` (so `skipped.tsv` is reproducible); a warning is also logged. A
source that cannot be opened at all (corrupt ZIP, unreadable directory at any
depth) becomes one `failed` row for its path. An input whose name is not
UTF-8 or holds a control character or line separator becomes a `failed` row
under its escaped name (see *Source loading*). Content that is not an input
(see *Source loading*) becomes an `ignored` row, without a logged warning.
Only `failed` rows affect the exit status. Errors writing to the work
directory (JPG files, batch directories, `manifest.tsv`, `skipped.tsv`,
`preprocess.json`) still abort the run; since images are encoded in memory first, an input's content
cannot cause one.

**Batching**: Each input is rendered, encoded and written as soon as discovery yields
it (with `--jobs` > 1, at most `2 × jobs` inputs ahead), so memory does not grow with batch size. The JPG is first written to
`<staging>/.pending/` under a provisional name; once every input is rendered
and collisions are known (see *Collisions*), the JPGs of non-colliding inputs
are renamed into their batches in discovery order, the rest are deleted, and
`.pending` is removed. The n-th kept image
(0-based) goes to `batch_{n // batch_size + 1:03d}/img_{n % batch_size + 1:05d}.jpg`.

**Output**: `manifest.tsv`, `skipped.tsv` (always written, even when
empty) and `preprocess.json`, plus the summary line

```
Found N inputs: wrote K images in B batches; S skipped (F failed, I ignored; see WORK_DIR/skipped.tsv)
```

N counts every discovered item (rendered, failed and ignored).

If any input failed and `--allow-skipped` was not given, the command exits 1
after writing everything.

### `image-review review`

```
image-review review [--mode {single,grid}]            [--pass N]
                    [--batch BATCH_ID]                 [--work-dir DIR]
                    [--filter {unreviewed,clean,all}]  [--rotate {auto,always,never}]
                    [--reviewer NAME]
                    [--remote CONNECTION_STRING [--via DESTINATION]]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--mode` | `single` | `single` = one image at a time; `grid` = packed grids |
| `--pass` | auto-detected | Review pass number (integer >= 1) |
| `--batch` | first batch with images matching the filter | Restrict to a named batch (e.g. `batch_001`), also for `b` at the end of the list (see *Next Batch*); an empty or unknown name exits 2 listing up to 5 known batches |
| `--filter` | `unreviewed` | Which images to show: `unreviewed` (images still to do: UNREVIEWED and FLAGGED), `clean`, or `all` |
| `--rotate` | `auto` | Rotating images 90° in grids: `auto` = only when that needs fewer grids, `always`, or `never` |
| `--reviewer` | `getpass.getuser()` at run time | Name recorded in the `reviewer` column of every verdict; also read from `$IMAGE_REVIEW_REVIEWER`. Checked by `connection.parse_reviewer` (1-64 characters, all `str.isprintable()`, so no tab, newline or other control character, and not all whitespace); a bad value, or no login name to default to, exits 2. It is the client's unauthenticated claim, recorded as given |
| `--work-dir` | `./review_work` | Work directory from preprocessing (local review) |
| `--remote` | (none) | `ir://` connection string of an `image-review serve` process; also read from `$IMAGE_REVIEW_REMOTE` |
| `--via` | (none) | SSH destination of a login node to tunnel through; requires `--remote`; also read from `$IMAGE_REVIEW_VIA` |

Opens a store (see below), initializes pygame, creates a `ReviewSession`,
runs the event loop, then shuts down pygame. pygame starts only after the
store (and tunnel) is up, so ssh password/MFA prompts keep terminal focus.

**Store selection** (`cli.open_store`, shared by `review` and `status`):

- Without `--remote`: `LocalStore(work_dir)` (writable for `review`, holding
  the work dir lock until the command exits; `read_only=True` for `status`),
  entered as a context manager like `RemoteStore`. `WorkDirLocked` is a
  `ClickException` (exit 1) with its message. `--via` without `--remote` is a
  usage error unless it came only from `$IMAGE_REVIEW_VIA`.
- With `--remote`: `--work-dir` is a usage error (mutually exclusive; if
  `--remote` came from the environment the message says to unset
  `IMAGE_REVIEW_REMOTE`). The string is parsed with `RemoteTarget.parse` and
  `--via` validated with `parse_via`; either failure is a `ClickException`
  naming the option. With `--via`, `ssh_tunnel` is entered first and the
  `RemoteStore` connects to `127.0.0.1:<local port>` instead of the advertised
  host. The store is closed before the tunnel.
- Startup failures become `ClickException`s with distinct messages:
  `TunnelError` (its message); `FingerprintMismatch` (certificate does not
  match, aborted before credentials were sent; with `--via` also notes another
  local process may have taken the forwarded port); `RemoteError` with status
  401 (token rejected), any other status (`returned HTTP N`), or no status
  (`Cannot reach server`, with a hint about the login node's reachability when
  `--via` is used and the cause was an OS error).
- Local behaviour and messages are unchanged.

### `image-review status`

```
image-review status [--check] [--work-dir DIR | --remote CONNECTION_STRING [--via DESTINATION]]
```

Same store selection as `review`, but a local store is opened read-only (no
lock), so `status` works while a writer holds the work directory. Fetches the manifest, the current pass and
that pass's statuses from the store, then prints overall and per-batch counts
of each `Status` (pass-aware, computed by the pure `store.summary` /
`store.batch_summary`), plus the current pass number. The overall block has
one line each for CLEAN, DIRTY, UNREVIEWED and FLAGGED; the per-batch table
(printed when there is more than one batch) has columns `Batch Total Clean
Dirty Unrev Flag`. After a completed pass, images marked DIRTY in it show as
FLAGGED (not DIRTY) because the current pass is then the next one. Then
it calls `store.skipped()`; if preprocess skipped anything it prints
`Skipped during preprocess: F failed, I ignored (see skipped.tsv in the work dir)`
so that a fully reviewed manifest is not mistaken for a complete input set.
Nothing extra is printed when `skipped.tsv` is absent (work dirs from older
versions) or has no rows. A malformed `skipped.tsv` (`ValueError`) is a
`ClickException` (exit 1). Does not start pygame.
`status` cannot count unloadable images (see *Unloadable Images*): they are
only discovered when `review` tries to show them, and until marked DIRTY they
count as UNREVIEWED.

**Exit status**: 0, unless `--check` is given: then, after printing the same
report, it exits 1 if any image is UNREVIEWED or `skipped.tsv` has any
`failed` row (`ignored` rows do not count), else 0: a study is finished when
every image has a verdict. FLAGGED is a DIRTY verdict from an earlier pass, so
it counts as decided; re-review passes are optional, and FLAGGED images still
show in the report, so a second pass remains available. It works the same over
`--remote` (the counts come from `store.skipped()`). Errors
(bad store, malformed `skipped.tsv`) exit 1 or 2 as without `--check`.

### `image-review export`

```
image-review export [--work-dir DIR] [--output FILE] [--report FILE] [--allow-live]
```

Writes the study's result as an **allowlist** with default deny: the source
files that may be released, each keyed by its path (`image_id`) and its
`source_sha256`. A file is released only if both match a row; anything not
listed (DIRTY, unreviewed, failed, ignored, a ZIP as a whole, a file whose
bytes changed) is denied. Every other file goes to an optional **report**
(`--report FILE`), for audit and follow-up; it must never be used to choose
what to release (e.g. "everything not DIRTY"). Local
only: the rows name `image_id`s (source paths, possibly PHI), which never leave
the machine holding the work directory, so export runs there (e.g. on the
cluster). `--remote` is declared only to be refused with a usage error (exit 2)
saying so; it has no envvar, so `$IMAGE_REVIEW_REMOTE` is ignored and does not
get in the way of `--work-dir`. There is no server endpoint for it. The whole
command runs under `interrupt_on(*TERMINATION_SIGNALS)`, so SIGTERM/SIGHUP
unwind like Ctrl-C and remove a half-written output.

`--report` and `--output` naming the same file (compared after
`os.path.realpath`) is a usage error (exit 2), checked before anything is read.

**Refusals** (`ClickException`, exit 1, nothing written):
- `lock.live_writer(work_dir)` finds `review.lock` held by a writer. The lock
  is held unless it names a process of this machine and boot that no longer
  exists (`is_stale`); an unreadable lock counts as held. The message is
  `WorkDirLocked`'s. `--allow-live` overrides this one refusal only, and then
  logs a WARNING (stderr). The check is made once, at the start.
- A writer that took the lock while export read the work directory: when
  there was none at the start, `live_writer` is checked again after
  `export_rows()`, with the same `--allow-live` override.
- `review.tsv` ends in a torn line (`ReviewDB.decisions()` raises). This is not
  overridable. The torn line is dropped by a writer's next append, so the
  message says to record a verdict with `review` (or through `serve`) and to
  re-check the last image reviewed.
- A malformed `skipped.tsv`.
- A field (any column of the allowlist or the report, even without
  `--report`, so one unsafe field anywhere refuses the whole export)
  containing a control character (Unicode category
  `Cc`: C0 incl. tab, CR and LF, DEL, and C1 incl. U+0085) or U+2028/U+2029,
  which covers every character `str.splitlines()` breaks at, or starting
  with `"`, which CSV-aware readers (Python's `csv`, pandas, spreadsheet
  import) take as the start of a quoted field (`format_allowlist`'s or
  `format_report`'s `ValueError`, naming the `image_id` with `repr` and the
  column).
- `--output FILE` or `--report FILE` already exists (`os.path.lexists`, so a
  dangling symlink counts), checked before either is written, so no new report
  is left beside a stale allowlist. `write_new_file`'s `O_EXCL` still refuses
  one created after the check.

Then it opens a read-only `LocalStore` (no lock; nothing is written in the
work directory) and calls its `export_rows()`. That applies the pure
`export.export_rows` to the manifest entries, `ReviewDB.decisions()` and
`load_skipped`, giving one row per source file (below), which
`export.split_allowlist` splits into the allowlist and the report.

**Split** (`export.split_allowlist(rows) -> (allowed, report)`): each list
keeps the rows' order, and every row lands in exactly one. A row is allowed
only if all of these hold:
- its status is `CLEAN` (so every part of the file, icon included, is CLEAN);
- it has a `source_sha256` (a manifest from before the hash columns has none);
- no row that is not `CLEAN` has the same `source_sha256`: identical bytes
  cannot be both clean and dirty, so all copies are denied.

Every other row goes to the report unchanged, except that a denied `CLEAN`
row stays `CLEAN` and gets `not allowlisted: no source_sha256 (work directory
from an older version)` or `not allowlisted: same content as a file that is
not CLEAN` added to its `reason` (after any existing note, `; `-joined). So
CLEAN files of a work directory without hashes are never allowlisted.

**Formats** (`export.format_allowlist`, `export.format_report`): UTF-8 text,
a header line, then one line per row. Fields are joined with tabs and every
line ends in LF. There is no quoting or escaping: fields are written exactly
as stored (a `"` not at the start included), which the refusals above make
unambiguous. The allowlist's header is
`source_sha256 image_id pass_number timestamp reviewer` (`ALLOWLIST_HEADER`);
the report's is
`image_id status pass_number timestamp reviewer reason source_sha256`
(`REPORT_HEADER`). The columns mean the same in both (the allowlist's rows are
all `CLEAN`, with an empty `reason`):

| Column | Description |
|--------|-------------|
| `image_id` | The source file: a path, or `<zip>::<entry>` for a ZIP entry (each entry is its own row; a ZIP that opened and has entries is not a file of the export, but one that cannot be opened or has no file entries gets one row under its own path, from its `skipped.tsv` row). A DICOM's icon is never a row of its own (an `ignored` input whose path ends in `#icon` and that is not a part is a file, see *Ignored inputs*) |
| `status` | The worst status of the file's parts (below): `DIRTY`, then `NOT_REVIEWED`, then `UNREVIEWED`, then `CLEAN`; or `IGNORED` for an `ignored` input with no parts (below), which nobody looked at |
| `pass_number`, `timestamp`, `reviewer` | From the latest decision on the main part (`X` itself), as recorded; empty when it has none (`UNREVIEWED`, `NOT_REVIEWED`, `IGNORED`, or no main part). After an undo they come from the undo row (its time and the undoing reviewer; the restored pass). `reviewer` is the client's unauthenticated claim, possibly empty in old rows; it is not sanitized, so it may begin with `=`, `+`, `-` or `@`, and the file must be read as text, not as spreadsheet formulas |
| `reason` | `; `-joined notes, in this order: the main part's skip reason if it is `NOT_REVIEWED`; `main image missing` if there is no main part and `X` was not ignored (it then counts as a `NOT_REVIEWED` part, so the row is never CLEAN; an ignored `X` gives its ignored reason instead); `icon: <skip reason>` if the icon is `NOT_REVIEWED`, else `icon <STATUS>` if the icon is not `CLEAN`. For an `IGNORED` row, its `ignored` reason. For a `CLEAN` row denied by the split, then its `not allowlisted: ...` note. Empty otherwise (always, in the allowlist) |
| `source_sha256` | The SHA-256 of the source file (or ZIP entry) recorded in the manifest for `X` or `X#icon` (they share it); a file is released only if its own SHA-256 matches its allowlist row. Empty (so never allowlisted) when the manifest has none: a file that never rendered (only in `skipped.tsv`, so always for `IGNORED`), or a manifest from before the hash columns. It is derived from PHI content and, like `image_id`, appears only in these local files, never over the wire |

**Parts and folding.** Every distinct `image_id` of the manifest, and of the
`failed` rows of `skipped.tsv`, is a part:
- It is `NOT_REVIEWED` if `skipped.tsv` has any `failed` row for it, even if
  the manifest lists it too. Repeated rows count once, with the first reason.
  A manifest `image_id` with an `ignored` row (and no `failed` one) is
  likewise `NOT_REVIEWED`, with the first `ignored` reason: the two files
  disagree, so nobody can vouch for it.
- Otherwise its status is its latest decision's (CLEAN or DIRTY; a tombstone
  leaves none), or `UNREVIEWED` without one.

Status is not pass-aware: a DIRTY from an earlier pass that is FLAGGED in the
current one is `DIRTY` (it still contains PHI as far as anyone has said).
A part `X#icon` (`export.ICON_SUFFIX`) is
folded into the row of `X`. When there is no part `X` (e.g. a file literally
named `scan.dcm#icon` with no `scan.dcm` beside it), the row is still `X` and
the missing main part counts as `NOT_REVIEWED` (`main image missing`, or the
ignored reason if `X` was ignored, see below), so it is never CLEAN.

**Ignored inputs.** Each distinct `image_id` of the `ignored` rows of
`skipped.tsv` (inputs preprocess did not take for images, e.g. a PDF) gets a
row of its own, `IGNORED`, with the first such row's reason and every other
column empty, unless an earlier row already covers it: it is a part (a
manifest or `failed` `image_id`, reported as above), or the file `X` of a
folded row: an ignored `X` with a manifest or `failed` `X#icon` but no part
`X` is that row's main part, `NOT_REVIEWED` with the ignored reason (e.g.
`DICOMDIR index`) in place of `main image missing`. If the manifest lists
`X#icon`, an ignored `X#icon` is that part (`NOT_REVIEWED`, folded into `X` as
`icon: <reason>`); otherwise it is a source path like any other (a file may be
named so) and gets its own `IGNORED` row, not folded into `X`. An `IGNORED`
row is never CLEAN; treat its file as possibly containing PHI. Rows cover only
the listed files and ZIP entries, never a ZIP or directory as a whole. An
archive that cannot be opened (`failed`) or has no file entries (`ignored`,
`zip contains no files`) has one row under its own path.

Rows come in order of first appearance of their file: manifest order, then
`skipped.tsv` order of `failed` rows, then `skipped.tsv` order of `IGNORED`
rows. Decisions for `image_id`s in neither file are left out.

**Output.** Both texts are built and checked before anything is written. The
report (with `--report`) is written first, then the allowlist, so a failed
second write leaves only a report, which releases nothing. Without `--output`
the allowlist's bytes go to stdout's binary stream. Then one INFO line goes to
stderr: `N files allowlisted; M in the report (d DIRTY, u UNREVIEWED, n
NOT_REVIEWED, i IGNORED, c CLEAN not allowlisted)`, one count per
`ExportStatus` (taken from the `Literal`, so none is left out), CLEAN last. `--report FILE` and
`--output FILE` are each written by `atomic.write_new_file` (the same helper as
`review.lock`; see *Concurrency limits*):
1. A unique hidden sibling (`.FILE.<random>.tmp`) is created with
   `O_CREAT|O_EXCL` and mode 0600. For a group work directory it is then given
   the work directory's group (`fchown(fd, -1, gid)`), so it is right even
   outside the setgid work directory; if that fails, it stays 0600 and a
   WARNING is logged. Only then is it `fchmod`ed to the work directory's
   policy file mode (exactly, whatever the umask), and only then written and
   fsynced, so it is never readable by a group it does not belong to.
2. The sibling is hard-linked to `FILE`, so `FILE` appears complete or not at
   all. If `FILE` already exists, export refuses (exit 1) and never
   overwrites it.
3. Where hard links are not supported (`EPERM`, `ENOTSUP`, `ENOSYS`), `FILE`
   is created directly with `O_EXCL`, the same way (0600, group, mode, then
   the text). A failed `link` after which `os.path.samefile(sibling, FILE)`
   holds (NFS: the link was made but the reply was lost) counts as made.

One `try`/`finally` around creation through the link removes the sibling on
every exit, including Ctrl-C and SIGTERM/SIGHUP; a hard kill (SIGKILL, a node
crash) can leave it behind, holding source paths, to be deleted by hand.

### `image-review serve`

```
image-review serve [--work-dir DIR] [--bind HOST] [--port N]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--work-dir` | `./review_work` | Work directory from preprocessing; must exist |
| `--bind` | `socket.getfqdn()` | Hostname or IPv4 address to bind and advertise |
| `--port` | 0 | Port to listen on; 0 picks a free port |

Opens a writable `LocalStore` (holding the work dir lock for the server's
lifetime; `WorkDirLocked` is a `ClickException`, exit 1), calls
`server.make_server`, and serves until interrupted.
`make_server` itself refuses wildcard binds (`0.0.0.0`, `::`, empty), any
address the server ends up bound to that is unspecified, and a host whose
connection string would not parse (`RemoteTarget.parse` round trip), raising
`ValueError` after closing its socket; the command reports these as
`ClickException`s. Only IPv4 hostnames/addresses are supported.

**Connection string delivery**: on a TTY, the string and ready-to-paste client
commands (direct and `--via`) are printed. Otherwise (e.g. `sbatch`)
`server.write_connection_file` writes it to
`~/.image-review/connection-<host>-<port>.txt` (host sanitized to
`[A-Za-z0-9._-]`; directory created 0700, must be a directory owned by the
user, tightened to 0700 if looser; file created `O_EXCL|O_NOFOLLOW` with mode
0600, replacing a stale file), and the printed output gives the path and an
`ssh ... cat` command for the client.

**Shutdown**: SIGINT, SIGTERM and SIGHUP all stop the server (SIGTERM is what
Slurm sends on `scancel` and at the time limit); a signal that was already
ignored (e.g. under `nohup`) is left ignored. On exit the socket is closed,
the store closed (releasing the work dir lock) and the connection file removed.

## Data Files

All state lives in the work directory. `preprocess` creates it atomically
(see *Work directory lifecycle*); while a run is in progress its output is in
`.NAME.partial` beside it.

### `manifest.tsv`

Written by `preprocess`. Tab-separated UTF-8 with `\r\n` line endings, one row per image. A file that is not valid UTF-8 stops the tool.

| Column | Description |
|--------|-------------|
| `batch` | Batch subdirectory name (e.g. `batch_001`) |
| `preprocessed_path` | Relative path to the JPG within the work directory |
| `image_id` | Unique string identifier (fully-resolved absolute path; `{path}#icon` for a DICOM's embedded icon image) |
| `source_sha256` | SHA-256 (64 lowercase hex characters) of the source file's bytes, or of the ZIP entry's bytes for `<zip>::<entry>`. One per source: `X` and `X#icon` share it |
| `jpeg_sha256` | SHA-256 (64 lowercase hex characters) of the JPG's bytes as written |

`image_id` (source paths, which may carry patient identifiers) and
`source_sha256` (derived from PHI content; a digest of a small input can be
recovered by guessing) are used only inside `LocalStore`/`ReviewDB` and in the
local `export`. Everything else identifies an image by its
`preprocessed_path`; no hash is ever sent over the wire.

The file is parsed strictly (`store.load_manifest`) when the work directory is
opened: the header must be exactly the five columns above, or the three
columns `batch`, `preprocessed_path`, `image_id` of work dirs from older
versions (which load with both hashes `None`); every row must have as many
non-empty fields as the header, each hash must be exactly 64 lowercase hex
characters, and `preprocessed_path` must be unique (the same `image_id` may
repeat). Any violation stops `review`, `status`, and `serve` with `Cannot read
work directory: <file>:<line>: <problem>` (exit 1); the file is never repaired.

**Integrity check.** When the manifest records a `jpeg_sha256`,
`LocalStore.image_bytes` (and so `image_bytes_many`) hashes the bytes it read
and raises `ValueError("<key> does not match its recorded hash")` on a
mismatch. Every caller already treats that as an unloadable image: the grid
packer leaves it out of every grid, the review session shows a placeholder that
takes only DIRTY (see *Unloadable Images*), and the server answers `/image`
with 404 (which the client raises as `KeyError`, again a placeholder). This
catches what Pillow's strict decode cannot: a JPG whose scan data was cut short
but which still ends in an EOI marker decodes without error, with grey rows,
and could otherwise be marked CLEAN with its missing rows never seen. The cost
is one SHA-256 per image load (about 9 ms for a 0.9 MB JPG on the reference
machine, against about 74 ms to decode it). Old manifests without hashes are
not checked.

### `preprocess.json`

Written by `preprocess`, in the staging directory with the policy's file mode
like the other files. One JSON object recording how the work directory was made:

| Key | Description |
|-----|-------------|
| `tool_version` | The image-review package version (`"unknown"` if not installed) |
| `created` | UTC time the run finished, ISO 8601 (`YYYY-MM-DDTHH:MM:SSZ`) |
| `sources` | The SOURCES, as resolved absolute paths, in the order given; a path that is not UTF-8 or holds a control character or line separator is escaped like an image_id (see *Source loading*) |
| `parameters` | `batch_size`, `colormap`, `clahe_kernel_size`, `outlier_percentile`, `intensity_margin`, `tail_fraction`, `jpeg_quality`, `jpeg_subsampling`, `access`, `jobs` (recorded only: the output does not depend on it) |
| `libraries` | Versions of `pydicom`, `numpy`, `scikit-image`, `Pillow`, `matplotlib`, plus `gdcm`, `pylibjpeg`, `openjpeg` when importable |
| `counts` | `inputs` (N of the summary line), `written`, `skipped_failed`, `skipped_ignored` |

It holds source paths, so it is as sensitive as `manifest.tsv`. Nothing reads
it back and the server never serves it: `/image` serves only keys listed in
the manifest (the key map is checked before `safe_path`), and no JPG key can
name it.

### `skipped.tsv`

Written by `preprocess`, always (header only when nothing was skipped).
Tab-separated UTF-8 with `\r\n` line endings, one row per input that produced no image. A file that is not valid UTF-8 stops the tool.

| Column | Description |
|--------|-------------|
| `image_id` | Source identifier, in the same form as `manifest.tsv` (`{path}#icon` for an icon image that failed to render; or the source path, if a whole source could not be opened; or, for a name that is not UTF-8 or holds a control character or line separator, the name escaped as `\xNN` for a non-UTF-8 byte or ASCII control and `\uNNNN` for any other, see *Source loading*) |
| `kind` | `failed` (an input that was not rendered; makes the CLI exit 1 unless `--allow-skipped`) or `ignored` (not an input: unrecognized content, AppleDouble, DICOMDIR, a ZIP without files, a symlink to an enclosing directory or to a directory inside a SOURCE, a non-regular file inside a directory) |
| `reason` | `<ExceptionClass>: <message>`, `unsupported: ...` for inputs this tool does not render, or the `ignored` reason |

It contains source paths, so it is as sensitive as `manifest.tsv`. `export`
makes each `failed` `image_id` `NOT_REVIEWED` with its `reason` (folding an
icon's into its file's row), and lists each `ignored` one not otherwise
covered as `IGNORED` with its `reason` (see *Ignored inputs* under *Parts and folding*).

### `review.lock`

Present while a writer (`review` or `serve`) has the work directory open. JSON
naming the holder; see *Concurrency limits*.

### `review.tsv`

An append-only log written by `ReviewDB`. Each mark action encodes its rows
into one buffer and writes it to a file opened `O_APPEND`, looping until a short
`write` has written it all, then `fsync`s. Appends never change earlier bytes
or the inode, with two exceptions: a torn tail (see below) is cut off before
the next append, and an old five-column file is migrated once (see below),
which replaces the file with a new inode. A crash mid-write can therefore record only some rows
of a multi-image mark. A new or empty file gets the header first and is
`fchmod`ed (if needed) to the file mode of the work directory's access policy
(0600 private, 0660 group). Lines end in `\r\n` (Python `csv`'s default, as the file has
always used); the reader accepts `\n` too.

| Column | Description |
|--------|-------------|
| `image_id` | Matches `manifest.tsv` |
| `batch` | Batch the image belongs to |
| `status` | `CLEAN` or `DIRTY`; or `UNREVIEWED` in a tombstone (only with `mode` `undo`; see *Undo rows*) |
| `pass_number` | Integer pass (at least 1) in which this decision was made (an undo row: the restored decision's pass) |
| `timestamp` | ISO 8601 UTC timestamp |
| `reviewer` | Who gave the verdict, as the client claims it (`review --reviewer`, default the login name); unauthenticated, recorded as given. Empty in migrated rows (and, in an old five-column file, absent) |
| `mode` | `single` or `grid` (`status.MarkMode`): the display mode the verdict was given in; `undo` for a row written by an undo (`review_db.RowMode`). Empty in migrated rows |
| `grid_size` | Integer >= 1: how many keys the one verdict covered (1 in single mode); in an undo row, how many `image_id`s the undo covered. Empty in migrated rows |
| `tool_version` | `image-review` package version of the writing process (`connection.package_version()`, resolved once per process; `unknown` if not installed). Empty in migrated rows, and possibly cut short or empty in a kept torn last row (see below) |

An `image_id` may repeat; its last row wins. Files written before the log format
(one row per `image_id`) are already valid logs. The file is parsed strictly:
it must be UTF-8, the header must be exactly these nine columns or the old first
five, every row must have as many fields as the header, a `status` of `CLEAN` or
`DIRTY` (or `UNREVIEWED` with `mode` `undo`; `FLAGGED` never), an integer `pass_number` of at least 1, a non-empty `image_id`, a
`mode` of `single`, `grid`, `undo` or empty, and a `grid_size` that is empty or an
integer of at least 1 (`reviewer` and `tool_version` are free text, possibly
empty). A bad file stops the
tool with `Cannot read work directory: <file>:<line>: <problem>` instead of
being skipped or rewritten, so a hand edit cannot silently lose decisions.

Two crash leftovers are tolerated. An empty file (created, but the first append
never landed) holds no decisions. An unparseable last line with no line ending
(`\r` or `\n`; a torn append) is ignored with a logged WARNING, and the
next mark truncates the file back to the end of the last complete line before
appending, so the file stays strictly parseable. The truncate happens only if
the file still has the inode and size seen when it was loaded; otherwise the
mark fails with `<file> changed since it was loaded`. A failed append (short
write then an error, or a failed `fsync`) is cut off the same way, at once if
possible, else by the next mark. A read-only store warns but never writes.

A last line with no line ending that does parse is kept; the next mark first
writes its missing line ending (`\n` after a bare `\r`, else `\r\n`). Such a
row's last field may be cut short or empty, since it is not validated:
`timestamp` in an old five-column file, `tool_version` in a current one. Its
other fields (`status`, `pass_number` and, in a current file, `reviewer`,
`mode` and `grid_size`) are complete, since they parsed. A bad line anywhere else, or a bad last line that
is terminated, is still an error.

**Migration.** A file with the old five-column header (`image_id`, `batch`,
`status`, `pass_number`, `timestamp`) still loads, for readers too (`status`,
read-only stores), and is never rewritten by them. A writable `LocalStore`
calls `ReviewDB.migrate()` after taking the work dir lock: it writes every
loaded row, in order, under the new header with the four new columns empty, to
a temp file `.review.tsv.*.tmp` in the work directory (`mkstemp`, then
`fchmod` to the policy's `file_mode`), `fsync`s it, and `os.replace`s it over
`review.tsv`, then `fsync`s the directory (ignoring only `EINVAL`, `ENOTSUP`/
`EOPNOTSUPP` and `EBADF`, where a filesystem cannot sync directories). If
writing or renaming fails, the temp file is removed and the old file is left
as it was; a `LocalStore` open then fails and releases the lock (the CLI shows
`Cannot update work directory: ...` for a `changed since it was loaded`). This happens once: the result has the new
header, so later opens leave it alone. A torn tail of the old file is not
copied (it was never loaded). Migration refuses (`changed since it was loaded`)
if the file's inode or size changed since the load. `ReviewDB` refuses to
append to an old-header file that was not migrated, so the file never mixes
five- and nine-field rows.

**Undo rows.** An undo (`ReviewDB.undo_many`, see *`ReviewStore` Protocol*)
appends, in one append, one row per `image_id` of the mark it undoes, each with
`mode` `undo`, a new `timestamp`, the undoing `reviewer`, `grid_size` = the
number of `image_id`s undone, and the current `tool_version`. Where the image
had a decision before that mark, the row restores that decision's `batch`,
`status` and `pass_number` exactly (the pass may be lower than the undone
row's: a restore is exempt from the never-decreasing pass rule). Where it had
none, the row is a **tombstone**: `status` `UNREVIEWED`, with the undone row's
`batch` and `pass_number`. Folding the log drops an `image_id` whose last row
is a tombstone, so it reads as never reviewed (`UNREVIEWED`, and unseen by
`current_pass`) until it is marked again. image-review versions before undo
(wire API v5) reject a file holding undo rows as malformed.

Compatibility: image-review versions before this format cannot read a migrated
(nine-column) `review.tsv`; teammates sharing a group work directory must all
upgrade before any of them opens it with `review` or `serve`.

Only images that have been explicitly marked appear in `review.tsv`. An image
absent from `review.tsv`, or whose last row is a tombstone, is implicitly `UNREVIEWED`. `FLAGGED` is derived when
reading (see *Pass Logic*) and is never written.

### `batch_NNN/img_NNNNN.jpg`

Preprocessed individual image files. Numbered sequentially within each batch.

## Review Store (`store.py`)

### Types

`Key`, `ImageId`, `Status`, `Verdict` and `TODO_STATUSES` (with `MarkMode` and `Rotation`) are defined in `status.py`.

| Name | Description |
|------|-------------|
| `Key` | `NewType("Key", str)`: a manifest key, the `preprocessed_path` (see *Key versus `image_id`*). A string becomes a `Key` only where it is parsed (`load_manifest`, the client's `parse_manifest` and `parse_statuses`, the server's `parse_mark` and `/image` check), so the type checker keeps keys and source paths apart |
| `ImageId` | `NewType("ImageId", str)`: a source image id, the source path or `<zip>::<entry>` (possibly PHI; it never leaves `LocalStore`). A string becomes an `ImageId` only where it is minted or parsed: preprocess discovery (with the `#icon` and escaped bad-name ids derived there), `parse_decision`, `load_manifest`, `load_skipped`, and `export_rows` (a file's id with `#icon` removed). Server, client and controller never see one |
| `Status` | `Literal["CLEAN", "DIRTY", "UNREVIEWED", "FLAGGED"]`: an image's status in a given pass (see *Pass Logic*) |
| `Verdict` | `Literal["CLEAN", "DIRTY"]`: what a mark may record |
| `TODO_STATUSES` | `frozenset({"UNREVIEWED", "FLAGGED"})`: statuses that still need a verdict in the current pass |
| `StatusFilter` | `Literal["unreviewed", "clean", "all"]`: the `review --filter` vocabulary, parsed by the CLI's choice and taken by `filter_rows` and `ReviewSession` |
| `ManifestRow` | Frozen dataclass: `key` (the `preprocessed_path`) and `batch` |
| `SkippedRow` | Frozen dataclass: one `skipped.tsv` row, `image_id`, `kind` (`SkipKind`, `Literal["failed", "ignored"]`) and `reason` |
| `ManifestEntry` | Frozen dataclass: one `manifest.tsv` row, `batch`, `key`, `image_id`, and `source_sha256` and `jpeg_sha256` (`str \| None`; `None` in a 3-column manifest). Local only |
| `ExportRow` | (`export.py`) Frozen dataclass: one `export` row (a source file), `image_id`, `status` (`ExportStatus`, `Literal["CLEAN", "DIRTY", "UNREVIEWED", "NOT_REVIEWED", "IGNORED"]`), `pass_number` (`int \| None`), `timestamp`, `reviewer`, `reason`, `source_sha256` (`""` when unknown) |
| `StoreUnavailable` | Exception: the store cannot be reached (as opposed to a bad key or image) |

### Key versus `image_id`

Clients identify an image by its **key**, the `preprocessed_path`
(`batch_001/img_00001.jpg`). The manifest's `image_id` is the original source
path, which may carry identifiers, so it never leaves `LocalStore`: the server
never sends it and the client never needs it. The mapping key -> `image_id` is
private to `LocalStore`. Several keys may share one `image_id` (the same source
image preprocessed more than once); they share a review status.

### `ReviewStore` Protocol

| Method | Description |
|--------|-------------|
| `manifest() -> list[ManifestRow]` | All rows, in manifest order |
| `image_bytes(key) -> bytes` | JPG bytes; `KeyError` for an unknown key |
| `image_bytes_many(keys) -> dict[str, bytes]` | Bytes for the keys that loaded; missing or unloadable keys are omitted with a logged warning |
| `statuses(pass_number) -> dict[str, Status]` | Pass-aware status of every manifest key |
| `mark(keys, status, pass_number, *, reviewer, mode) -> dict[str, Status]` | Record a verdict given by `reviewer` (an unauthenticated claim) in `mode` (`single` or `grid`); returns the new status of every key affected, including keys that share an `image_id` with a marked key |
| `undo(pass_number, *, reviewer) -> dict[str, Status]` | Undo the latest mark not yet undone (one `mark` call: one image or a whole grid), restoring each of its images' decision from before it (see *Undo rows*); `reviewer` is checked and recorded like `mark`'s. Returns the new status, at `pass_number`, of every key affected, as `mark` does; `{}` when there is nothing to undo |
| `current_pass() -> int` | Auto-detected pass number |
| `close()`, `__enter__`, `__exit__` | Release what the store holds (`RemoteStore`: pool and connections; `LocalStore`: the work dir lock); idempotent. Stores are context managers that close on exit |
| `skipped() -> SkippedCounts` | Counts of `failed` and `ignored` rows in preprocess's `skipped.tsv` (frozen `SkippedCounts(failed, ignored)`); zero counts if the file is absent; `ValueError` naming `file:line` if it is malformed |

### `LocalStore(work_dir, read_only=False)`

Loads `manifest.tsv` (written once by `preprocess`, so before locking); a
writable store then takes the work dir lock (see *Concurrency limits*) and
loads a `ReviewDB` and calls its `migrate()` (see *`review.tsv`*), releasing the lock if either fails. `close()` releases it. A `read_only` store takes no lock and its
`mark` and `undo` raise `PermissionError` (as they do after `close()`). `image_bytes` reads the file via
`safe_path` and checks it against its `jpeg_sha256` when the manifest has one
(see *Integrity check*); `image_bytes_many` loops over it, catching `KeyError`,
`ValueError` (path escape, hash mismatch) and `OSError`. `mark` translates each key to its
`image_id` and its own manifest batch (a grid's keys need not share one) and
calls `ReviewDB.mark_many`, so `grid_size` is the number of keys. After a
successful `mark_many`, the rows it wrote and each image's decision from just
before them (`review_db.Change(written, previous)`, `previous` `None` for an
image with none) are pushed as one entry on the store's **undo stack**; `undo`
writes the top entry's restore rows with `ReviewDB.undo_many` and pops it only
once they are written (a failed undo can be retried). The stack is unbounded
(one small entry per mark of the session). It is in memory
only: it starts empty and is lost when the store is closed or the process ends,
so only marks made since the store was opened can be undone. `statuses` and `current_pass`
delegate to `ReviewDB.get_status` / `current_pass`. `skipped` parses
`skipped.tsv` strictly with `load_skipped` (header `image_id`, `kind`, `reason`; `kind` is `failed`
or `ignored`) and returns only the counts (zero when the file is absent). `export_rows()` (not part of the
`ReviewStore` Protocol: `image_id`s stay local) returns
`export.export_rows(entries, ReviewDB.decisions(), load_skipped(...))`;
see *`image-review export`*.

### Pure functions

| Function | Description |
|----------|-------------|
| `filter_rows(rows, statuses, status_filter="unreviewed", batch=None)` | Filter rows by status and optional batch: `unreviewed` selects `TODO_STATUSES` (UNREVIEWED and FLAGGED), `clean` selects CLEAN, `all` everything. `status_filter` is a `StatusFilter` (`Literal["unreviewed", "clean", "all"]`), parsed once by the CLI's `--filter` choice |
| `summary(rows, statuses)` | A `Counter[Status]` of the rows' statuses (a missing status counts 0; `.total()` is the image count) |
| `batch_summary(rows, statuses)` | The same per batch: `dict[str, Counter[Status]]` |
| `skipped_counts(rows) -> SkippedCounts` | The failed/ignored split of `SkippedRow`s; the one derivation behind `skipped()`, preprocess's summary line and `preprocess.json` counts |
| `safe_path(work_dir, relative)` | Resolve within `work_dir`; `ValueError` if it escapes |
| `export.export_rows(entries, decisions, skipped) -> list[ExportRow]` | The export (see *`image-review export`*) from the manifest entries, the latest decision per `image_id` and the `skipped.tsv` rows: one row per source file, icons folded in |
| `export.split_allowlist(rows) -> tuple[list[AllowedRow], list[ExportRow]]` | (allowed, report) in input order, every row in exactly one: allowed only if `CLEAN`, with a `source_sha256` that no row that is not `CLEAN` shares; a denied `CLEAN` row's `reason` says why |
| `export.format_allowlist(rows: list[AllowedRow]) -> str` | The allowlist as TSV text with `ALLOWLIST_HEADER` (`AllowedRow` is a `NewType` over `ExportRow` that only `split_allowlist` makes, so passing the report's rows is a type error); `ValueError` if a field holds a control character or U+2028/U+2029, or starts with `"` |
| `export.format_report(rows) -> str` | The report as TSV text with `REPORT_HEADER`; the same `ValueError` |

`load_skipped(work_dir)` parses `skipped.tsv` into `SkippedRow`s (empty if
absent; `ValueError` naming `file:line` if malformed). Two IO helpers serve
`export` (see *`image-review export`*): `atomic.write_new_file(path, file_mode,
group, text)` creates the `--output` and `--report` files, and `lock.live_writer(work_dir) ->
WorkDirLocked | None` checks the work dir lock.

## Review Session (`controller.py`)

### Initialization

`ReviewSession(store, reviewer, mode, pass_number, batch, status_filter, rotation)`:
`reviewer` comes from `review --reviewer` (already checked) and is passed, with
the session's current display mode, to every `store.mark()`, and to every `store.undo()`.

1. Fetch `store.manifest()` (a list of `ManifestRow`)
2. Determine the pass: `store.current_pass()` if not specified. The store's
   auto-detection (`ReviewDB.current_pass`) is:
   - Pass 1 if any image has never been reviewed
   - Otherwise stays on max(pass_number) if it has unfinished work,
     or advances to max(pass_number) + 1
3. Fetch the **status snapshot** `store.statuses(pass)` (key -> status)
4. Auto-select batch if not specified: pick the first batch (sorted
   alphabetically) that has rows the mode may show (the status filter's rows;
   in grid mode, minus DIRTY and FLAGGED, see *Grid Mode*). A batch whose only
   todo images are FLAGGED is therefore not auto-selected for grid mode
5. Determine review items based on mode

The session remembers whether the pass (`--pass`) and the batch (`--batch`)
were given: `b` (see *Next Batch*) adopts the current pass only when the pass
was not given, and never leaves a given batch.

Every item is a `ReviewItem`, the union of two frozen dataclasses, so a grid
without its surface, or a single image with several keys, cannot be built:

- `SingleItem(key)`: one image, loaded when displayed. Its `keys` is `(key,)`
  and its `label` (shown in the status bar) is the key.
- `GridItem(keys, surface, source_scale)`: a grid with its composited
  surface and its smallest image scale. Its `label` is "grid (N images)". A
  grid's status and CLEAN refusal follow the grid rules below, even for a
  one-image grid; the item for an image left out of the grids in grid mode is
  a `SingleItem`, not a grid.

### Status Snapshot

All status decisions (filtering, item status, todo counts, grid status) read a
local dict, not the store. It is refreshed from `store.statuses()` at init and
on every mode restart and next-batch move, and updated after each mark from the returned
`mark()` result (which includes other keys sharing an `image_id`). Marking
therefore costs one store call and no re-fetch.

### Store Failures

`StoreUnavailable` (raised by `RemoteStore` as `RemoteError`) while loading an
image, marking, restarting a mode, or moving to the next batch is treated as a lost connection, not an
unloadable image: autoplay and the pending auto-advance are cancelled, the
status snapshot is left unchanged (a failed mark is not applied), the viewer
shows "Lost connection to server - progress saved. Press q to quit." and the
reason is logged (ERROR). The session is then `DISCONNECTED`: only `q`/Esc, the
gamepad's Start and closing the window do anything (no navigation, mode switch or `z` reaches
the store again). A failed mode restart or next-batch move clears the item list. Any
other failure to load an image is an unloadable image (see *Unloadable Images*).

### Single Mode

- `filter_rows()` over the manifest and snapshot for the current
  pass/batch/filter
- Shuffle the resulting rows
- Each row becomes a `SingleItem`; the image is fetched with
  `store.image_bytes(key)` and decoded with `load_surface(bytes)` on display. An
  image that cannot be fetched or decoded is shown as a placeholder in its place
  (see *Unloadable Images*); the cursor stays on it

### Grid Mode

- Display a "Computing grids..." message while packing, updated to
  "Computing grids... i/N" about every 25 images (via `pack_into_grids`'s
  `on_progress`; repainted and `pg.event.pump()`ed, which does not consume key events)
- Read screen dimensions, subtract the 50px status bar height
- A window resize rebuilds the grids (see *Resize rebuild* below)
- `filter_rows()` for the current pass/batch/filter, then keep only rows whose
  status is in `controller.GRID_ELIGIBLE` (UNREVIEWED or CLEAN), in every
  filter including `all`. A grid mark applies to all its images, so one
  keypress must never clear an image already judged DIRTY (this pass) or
  FLAGGED (DIRTY in another pass); those are reviewed in single mode
- Pass the keys of those rows (the cache key's keys) and the store to
  `pack_into_grids()` with the screen dimensions.
  The session caches the last result (only one), keyed by the review rows'
  keys in order, the grid size and rotation policy; a rebuild with the same
  key (e.g. `s` then `m` with nothing marked) reuses it instead of packing
  again. A mark that changes which rows are eligible (e.g. one now DIRTY)
  changes the key, so a cached grid never holds a key the snapshot excludes.
  A failed pack leaves no cached result. The cache is kept across single mode
  within a batch (so `m`, `s`, `m` packs once) and dropped by `b`, so a
  previous batch's canvases are not kept alive. The items below are rebuilt
  from the cached result on every build, including a fresh shuffle
- Convert each returned `GridSpec` into a `GridItem` with its `keys`,
  `surface` and `min_scale` (as `source_scale`)
- Shuffle the grid items, then sort by image count (largest grids first)
- Append one item per key `pack_into_grids()` left out of the grids
  (unloadable, or, should it ever happen, left unpacked):
  `SingleItem(key)`. Like a
  single-mode item it is loaded when shown, so a placeholder is drawn only
  then and only if loading fails, and it follows the single-image status rules,
  not the grid rules. No grid ever holds a key whose pixels it does not show

When a grid is marked CLEAN or DIRTY, `store.mark()` is called with all its
`keys`, and every key in the result is written into the snapshot.

A grid can still come to hold a DIRTY key mid-session, when a key in it shares
an `image_id` with an image marked DIRTY elsewhere. CLEAN on a grid holding any
DIRTY or FLAGGED key is refused (no store call; the status bar shows "grid
contains an image already marked DIRTY - review it in single mode", also
logged), unless every key in the grid is DIRTY, which reverses that
grid's own verdict.

If grid mode has no items but the status filter selected rows it left out, the
session says so instead of implying the review is done: `run()` prints, and a
mode restart shows, "No grid items for pass N; K FLAGGED/DIRTY image(s) need(s)
single-mode review", ending " (--mode single)" in the terminal and " - press
[s]" on screen (K counts the left-out rows that are todo as in *Todo*, over the
selected batch, or all batches when none is selected).

**Resize rebuild.** A `WINDOWRESIZED` event refits the current item. In grid
mode, each `refresh_if_needed` (only while reviewing) compares the size held in
the grid cache key (the size the grids were last packed for) with the current
grid size, and repacks when they differ: one rebuild however many resize events
a batch holds, none for a resize back to the packed size before the tick, and
a size change that arrives without a `WINDOWRESIZED` is caught too. The repack
stops autoplay and a pending advance, shows "Computing grids...", and rebuilds
the items (the changed size misses the cache, and the new cache key records the
size it was packed for, so the next tick repacks only if the window changed
meanwhile). Queued
`KEYDOWN` and `CONTROLLERBUTTONDOWN` events are discarded, as on a mode restart. The repack uses the current statuses, so under the
default filter it drops grids already marked CLEAN as well as DIRTY or FLAGGED
ones. The cursor goes to the grid that holds the first key of the item that was
current, or to the first item if none does (that grid was marked, or the new
packing left the key out of the grids). The dwell restarts, so a verdict never
lands on a re-composited grid the reviewer has not seen. Undo history is
cleared, as on a mode switch: the repack drops grids marked DIRTY, so `z` could
no longer show what it undoes. If nothing is left to show, the session goes to
the end-of-list state with the held-back message or "End of list - [b] next
batch", as a mode restart does. If the store is unavailable during the repack
the session goes to the lost-connection screen, which is not repainted over.
Single mode only rescales. A restart (mode switch, `b`, display change) packs
at the current size, so it leaves nothing to repack.

### Unloadable Images

An image whose bytes are missing (`KeyError`, e.g. a 404 from the server),
do not match the manifest's `jpeg_sha256` (`ValueError`), or cannot be read or
decoded is not an outage (see *Store Failures*). A warning
naming the key is logged, the key is added to the session's
`_unloadable` set, and the item is shown as a placeholder from
`viewer.placeholder_surface`: a dark surface reading `Cannot load image: <key>`
with a short reason ("image could not be fetched" or "image could not be read
or decoded"; the error itself goes only to the log). Navigation, `n`, todo-only and autoplay treat it
like any other item (autoplay does not stop at it), so the cursor always points
at a real item. A `SingleItem` (every single-mode item, and the item
for an image left out of the grids in grid mode) is loaded each time it is shown, so the
key joins `_unloadable` before any verdict can count, and a key that loads
again leaves it. Placeholders are never built up front.

A placeholder can be marked DIRTY but never CLEAN: CLEAN on an item holding
any unloadable key is refused (no store call; the status bar shows "cannot mark
CLEAN: image could not be loaded", also logged). DIRTY is recorded
normally, so the image stops being todo, batch auto-selection moves on and the
pass can finish; in later passes it is FLAGGED and, if still unloadable, again
a placeholder that only takes DIRTY.

### Grid Status Derivation

A grid's aggregate status is derived from the snapshot statuses of its keys:
- Any key not in `GRID_ELIGIBLE` (DIRTY or FLAGGED) -> DIRTY, so such a grid
  is neither shown nor counted as todo
- Otherwise any UNREVIEWED -> UNREVIEWED
- Otherwise (all CLEAN) -> CLEAN

### Todo

With `--filter unreviewed`, an item is todo if its status is in `TODO_STATUSES` (UNREVIEWED or
FLAGGED); in single mode a FLAGGED image is therefore todo, and grids are never built with one.
With `--filter clean` or `all`, every loaded item is a re-check: it is todo while at least one of
its keys is todo, i.e. not in the session's marked set or with a status in `TODO_STATUSES` (a key
marked earlier that is FLAGGED in a new pass, or restored to UNREVIEWED, is todo again). A
successful mark adds the item's keys; a successful undo removes the keys it restored. The set is in
memory only and is kept across a pass change (see *Next Batch*). Any todo key keeps a grid todo,
so a partly undone grid comes back.

### Event Loop

The session runs a pygame event loop processing:

| Event | Action |
|-------|--------|
| `c` key / B button | Mark current item CLEAN |
| `d` key / Y button | Mark current item DIRTY |
| `z` key | Undo this session's latest mark since the mode started (see *Undo*); also on the end-of-list screen |
| `b` key | On the end-of-list screen only: move on to the next batch (see *Next Batch*) |
| Right arrow / D-pad right | Next item |
| Left arrow / D-pad left | Previous item |
| Space | Toggle autoplay (500ms auto-advance) |
| `w` key | Select display (see below) |
| `f` key | Toggle fullscreen |
| `n` key | Jump to next todo item |
| `u` key | Toggle todo-only navigation |
| `s` key | Switch to single mode |
| `m` key | Switch to grid mode (the `--rotate` policy, default `auto`) |
| `M` key (shift+m) | Switch to grid mode (`never` rotate); Shift is read from the key event, not the live keyboard |
| `h` key | Show help/splash screen |
| A button | On the splash/help, display-select and end-of-list screens: continue, as Space |
| `q` / Escape / Start button | Quit (Start in every state, including `DISCONNECTED`) |
| Window resize | Refit current image; in grid mode also rebuild the grids (see *Resize rebuild*) |
| Controller added/removed | Hot-plug handling: open or drop the `Controller`; the status bar shows the count |

The display-select screen (`UIState.DISPLAY_SELECT`, opened by `w`) takes `1`-`9` to
switch display, Space/`h`/A to confirm (in grid mode, rebuilding the grids if the
display changed), `f` to toggle fullscreen, `s`/`m`/`M` to switch mode, and `q`/Escape to quit; the
digits do nothing on the ordinary help screen.

Gamepads go through SDL's GameController API (`pygame._sdl2.controller`,
initialised when the session starts), so buttons are numbered by SDL's
standard layout, with Xbox-style positions: A bottom, B right, Y top. On
`CONTROLLERDEVICEADDED` the session opens a `Controller(event.device_index)`
and keeps it in a dict keyed by its joystick instance id; on
`CONTROLLERDEVICEREMOVED` it drops the one with `event.instance_id`. SDL also
sends the raw `JOY*` events for these pads, but none are handled, so each pad
is counted once and a pad's raw button indices never act. A pad with no SDL
mapping is not supported: it gets no `CONTROLLER*` events and is not counted. A
mapping can be added through the `SDL_GAMECONTROLLERCONFIG` environment
variable. B, Y and the D-pad act only while reviewing; A acts only on the
splash/help, display-select and end-of-list screens; Start quits in every state.

After marking, the viewer auto-advances to the next item after 200ms.
Navigation stops at list boundaries with an "End of list - K todo left - [b]
next batch" message, where K is the batch's todo count; the "K todo left" part
is left out when K is 0. In todo-only navigation the message is "No todo
images remaining - [b] next batch" when K is 0, and "No more todo images this
way - K todo left - [Left/Right] wrap - [b] next batch" when todo items remain
in the other direction. On that screen Right/Space and Left wrap round to the
first or last item (in todo-only navigation, the first or last todo item), `s`,
`m` and `M` switch mode, `z` undoes, `b` moves on to the next batch and
`q`/Esc quits; `n` does not act there. Pressing `n` on the review screen with no
todo items left shows "No todo images remaining" in the info bar and stays on
the current item.

The status bar shows the item's status as a word (CLEAN, DIRTY, UNREVIEWED or
FLAGGED) at its left edge as well as in the bar colour. The splash and help
screen also handle `f` (toggle fullscreen).

On the review screen every key except Space cancels autoplay (and still does
its normal action, so Right steps once and stops); Space toggles it. Every
gamepad button cancels it (there is no gamepad autoplay toggle), and still does
its normal action. Left/Right, the D-pad, `n`, mode switches, `h` and `w` cancel a
pending post-mark advance, and so does any change of the current item: the
advance belongs to the item that was marked, so an `ADVANCE_EVENT` already
queued when it was cancelled is ignored (`_advance_pending`). The autoplay and
post-mark advance timers act only while an item is being reviewed, never behind
the help, display-select or message screens.

`run()` alternates two halves: `handle_events(events) -> bool` applies one
`pg.event.get()` batch (False means quit; events after a quit are dropped) and
`refresh_if_needed()` repaints. The display only redraws when a dirty flag is
set, to minimize CPU usage, and only while reviewing: the splash, help,
display-select and message screens ("End of list", "No items for grid mode",
"All batches done for pass N", "Lost connection to server ...") are painted once
by the viewer and stay until the state changes. When there are no items (an
empty mode, or no batch left), the navigation keys leave the message up; only
`q`/Esc, `s`, `m`, `M`, `z` and `b` act. On the lost
connection screen only `q`/Esc and Start act (see *Store Failures*).

**Verdicts need a seen item.** A verdict (`c`/`d`, B/Y) applies only to
an item that has been painted and on screen for `MIN_DWELL_MS` (200 ms);
otherwise it is ignored (it still stops autoplay). The session records the tick
of the first paint that shows the current item's pixels in `refresh_if_needed`
(the viewer's `image_shown`; a window too short to fit an image, or an image
that scales to zero size, paints only the bars and background and starts no
dwell), compares it
with the clock read once at the start of the event batch (so slow events earlier
in the batch, like a resize, do not count toward the dwell), and clears it
whenever the current item or screen changes (a new item, a mode restart, the
help or display-select screen) or a repaint shows no pixels of it; repainting
the same item with its pixels (resize, a mark) keeps it. So a verdict queued in the same event batch as a mode switch, an autoplay
advance or a post-mark advance, or typed within 200 ms of the new item
appearing, never judges an item the reviewer has not seen. All keyboard and
gamepad button events queued during a blocking step (a grid build or mode
restart) are also discarded with `pg.event.clear`, including `q`/Esc and the
arrows. `_mark` itself is not gated.

### Undo

`z` (on the review screen, in single and grid mode, and on the "End of list"
and other message screens, but not the lost connection screen) first cancels
autoplay and a pending post-mark advance. The session counts its own
successful marks since the current mode started (`_undoable`: reset by every
mode restart, +1 per successful mark, -1 per successful undo). At 0, `z` says
"Nothing to undo" without calling the store, so it only ever undoes a mark made
on one of this mode's items. Otherwise it calls `store.undo(pass,
reviewer=...)`; a `StoreUnavailable` is a lost connection, as for a mark, and a
`{}` result (the store's history is gone) resets the count and says "Nothing to
undo". The returned statuses update the snapshot and the todo count. The cursor
moves to the first item, in this mode's item order, holding any returned key,
the review screen is shown for it, and its dwell starts again, so `c`/`d` count
only once the restored item has been on screen for `MIN_DWELL_MS`. Items are
not rebuilt: grid membership is fixed when the mode starts, and an undone
grid's keys are still in it. Only another client's mark on a shared server (out
of scope, see *Concurrency limits*) can leave no item holding a returned key;
the status bar then names up to three of its keys with their new status. `z`
is not dwell-gated: it only undoes a mark on an item this mode showed, and
shows that item again before any verdict counts. Key repeat stays off
(pygame's default), so a held `z` undoes one mark.

### Next Batch

`b` on the end-of-list screen (not the lost connection screen) moves the
session on to another batch without restarting. It cancels autoplay and a
pending post-mark advance, reads `store.current_pass()` (adopted as the
session's pass unless `--pass` was given) and re-fetches the status snapshot.
The batch is then chosen by the pure function
`next_batch(batches, current, has_rows, *, wrap)`, which returns the first of
the sorted `batches` after `current` (from the first when `current` is `None`)
that `has_rows` accepts, or `None`; with `wrap` the search goes round once,
through the batches before `current` and finally `current` itself. `b` calls it
with:

- `batches`: the manifest's batches sorted alphabetically, or only the
  `--batch` one when it was given, so `b` never leaves a restricted batch (it
  reloads it while it has todo).
- `current`: the session's batch, or `None` when the pass changed (the search
  starts again from the first batch).
- `has_rows(batch)`: the batch has todo rows the mode may show, i.e. rows of
  the status filter (minus DIRTY and FLAGGED in grid mode, as in
  auto-selection) that are todo as in *Todo*: any such row under
  `--filter unreviewed`; under `clean`/`all`, a row whose key is not in the
  session's marked set or whose status is UNREVIEWED or FLAGGED. The set is
  kept across a pass change: under clean/all a re-check of a finished pass is
  itself recorded in a new pass, and `current_pass()` then advances; a key it
  holds that the new pass shows as FLAGGED is still todo by its status.
- `wrap=True` under every filter, so todo left behind (e.g. skipped images or
  batches) is found.

A found batch becomes the session's batch and its items are rebuilt in the
current mode as for a mode restart ("Computing grids..." in grid mode,
`_undoable` reset), from the snapshot `b` just fetched, starting at the first
item. After a pass change the info bar says "Now pass N". When nothing is found the session drops its items (they may belong to an
ended pass), resets `_undoable` (so `z` says "Nothing to undo") and shows:

- when the mode holds back todo rows of the status filter (grid mode:
  FLAGGED and DIRTY; counted over all batches, or the `--batch` one): "No grid
  items for pass N; K FLAGGED/DIRTY image(s) need(s) single-mode review -
  press [s]", with the session's batch moved to the first batch holding one
  (the `--batch` one itself when given), so `s` opens it;
- otherwise, under `--filter unreviewed` when the pass has just advanced:
  "Pass P complete - nothing to review in pass N" (with `--batch`, "... in
  NAME for pass N");
- otherwise, with `--batch`: "Batch NAME done for pass N";
- otherwise "All batches done for pass N".

The last three get " (current pass is M)" appended when an explicit `--pass`
differs from `current_pass()`. `q`/Esc quit. A `StoreUnavailable` from any of
these calls is a lost connection (see *Store Failures*). The
splash/help info line shows the batch's position, "batch k/B", among all the
manifest's batches. There is no gamepad binding for `b`: on the end-of-list screen the gamepad
only continues (A, as Space) or quits (Start).

## Grid Packer (`grid_packer.py`)

### `GridSpec` Dataclass (frozen)

| Field | Type | Description |
|-------|------|-------------|
| `surface` | `pg.Surface` | Composited grid image, ready for display |
| `keys` | `tuple[str, ...]` | Keys (preprocessed paths) of the images drawn in this grid |
| `min_scale` | `float` | Smallest `fit_size` / header-size ratio among the images drawn (1.0 if none was shrunk) |

### `pack_into_grids(keys, store, grid_w, grid_h, *, rotation="auto", on_progress=None) -> tuple[list[GridSpec], list[Key]]`

`keys` is a `Sequence[Key]`, the images to pack. Returns the grids and the keys
left out of every grid (missing, unreadable header, failed decode, or left unpacked), in input order. A
grid holds only keys whose pixels it shows. What to do with the left-out keys is
left to the caller. At most one bin's decoded images are alive at a time.

1. **Load**: Fetch all image bytes with `store.image_bytes_many()` (an
   8-worker pool for `RemoteStore`, a simple loop for `LocalStore`); compressed
   JPGs are small. Read each image's dimensions from its header with
   `PIL.Image.open(BytesIO(bytes)).size`, which decodes no pixels. Images that
   are missing (the store warns) or whose header cannot be read (warned here)
   are left out of the packing.
2. **Pack**: Each image is packed at `fit_size(w, h, grid_w, grid_h,
   rotate)`: its own size if it fits the bin upright (or rotated, when
   `rotate`), otherwise shrunk, keeping its aspect ratio, only as far
   as the better allowed orientation requires, so nothing is larger than the
   bin. `rotation` is `"always"` (`rotate` true), `"never"` (false) or `"auto"`.
   Create a `rectpack` packer with `rotation=rotate` and
   `(grid_w, grid_h)` bins (unlimited bin count), and add each image as a rect
   of its fit size. Under `"auto"` the headers are packed twice, without and
   with rotation (each at its own fit sizes; no pixels are decoded), and the
   rotated packing is kept only if it needs strictly fewer bins. Only the
   chosen packing is composited.
3. **Composite**: One bin at a time: create a black `pg.Surface(grid_w,
   grid_h)`, decode each of the bin's images with `util.load_surface(bytes)`,
   `pg.transform.smoothscale` it to its fit size, and blit it at the packed
   position, after `pg.transform.rotate(-90)` if rectpack rotated the rect
   (packed size differs from the fit size). The bin's decoded surfaces are
   released before the next bin. An image that fails to decode here (a valid
   header over a truncated body, a decoded size differing from the header's, or
   a final size differing from its packed rectangle) is warned, its rectangle stays black, its key is left out of the grid's
   `keys` and it is reported as left out. A bin left with no keys is dropped.
4. **Overflow**: `fit_size` makes every image fit a bin, so the packer should
   leave none out. Any it does is warned and reported as left out, so the
   caller shows it as a single image loaded on display.

`on_progress(i, n)`, if given, is called as each of the n images is handled
(left out at load, composited or failed, or left unpacked), ending with `(n, n)`.

`util.load_surface(buf: bytes) -> pg.Surface` decodes JPG bytes with
Pillow (grayscale is converted to RGB). Truncated or undecodable input raises
(Pillow is strict; `pg.image.load` would silently grey-fill missing rows).

## Image Viewer (`viewer.py`)

### `ImageViewer`

Opens a fullscreen, resizable pygame window with hidden cursor.

**Layout**: Image content fills the screen above a 50px status bar at the
bottom edge.

**Scaling**: Images are aspect-ratio-scaled to fit the available content area
(`screen_height - 50px` by `screen_width`), centered both horizontally and
vertically within the content area. Uses `pg.transform.smoothscale`.

**Status bar**: A colored rectangle spanning the full width at the bottom.
Color encodes review status (green=CLEAN, red=DIRTY, gray=UNREVIEWED,
orange=FLAGGED).
The image name is rendered right-aligned, position info is centered.

**Scale indicator**: `resize()` applies the pure `fit_image`, which returns the
scaled size, offset and factor (displayed size / source size) as one `Fit`, or
`None` when no pixel would show. It never caps the factor, so an image smaller
than the content area is enlarged and shows more than 100%. The status bar
shows it as an integer percent (truncated, so a scale just under 1.0 never
reads "100%") at the right edge, with the image name to its left. Below 100% (any scale under 1.0)
the percent is drawn in red (`SCALE_WARNING_COLOR`), because small text such as
burned-in PHI can be lost when an image is scaled down; at or above 100% it uses
the normal font colour. In grid mode the percent is the smallest image's
effective scale: `GridSpec.min_scale` (the smallest `fit_size` / header-size
ratio among the images drawn, 1.0 if none was shrunk) is carried by
`GridItem.source_scale` to `set_image(..., source_scale=1.0)`, and the
percent is that times the canvas's display scale. A `SingleItem` is
shown with `source_scale` 1.0.

**Font**: DejaVu Sans 36pt bold, dark gray (`Color(64,64,64)`). Bundled in
the `fonts/` subdirectory for cross-platform consistency. The help screen
uses DejaVu Sans Mono 24pt.

### Interface

| Method | Description |
|--------|-------------|
| `set_image(surface, name, status, info, source_scale=1.0)` | Set new image; triggers resize/scale |
| `set_status(status)` | Update status bar color without changing image |
| `set_info(info)`, `set_todo_only(enabled)`, `set_joystick_count(count)` | Update the centered text, the todo-only and the gamepad indicators |
| `resize()` | Apply `fit_image` for the current screen size: scaled image, offset and scale percent are replaced together |
| `image_shown -> bool` | Property: whether `refresh()` paints visible image pixels. False when none is set, or it has no visible pixels (the window is too short to fit it, or it scales to zero size); `resize` replaces the scaled image with the new fit (or none), so the frame never shows one image's pixels under another's name |
| `refresh_if_dirty() -> bool` | `refresh()` if the frame is dirty, clear the flag, return True; else False. Every method above, plus `switch_display` (through `resize`) and a new viewer, marks the frame dirty; `show_message` and `show_splash` do not |
| `refresh()` | Render frame: background, status bar, text, scaled image |
| `show_splash(lines, footer)` | Render centered splash/help overlay |
| `show_message(text)` | Render centered text message (e.g. loading indicator) |

The module-level `placeholder_surface(text) -> pg.Surface` renders `text`
(one line per newline) centred on a dark 1280x720 surface (wider or taller if
the text needs it), in the status bar font, for an image that could not be
loaded. The font file is read once and cached.

## Review Database (`review_db.py`)

### `ReviewDB`

In-memory dict keyed by `image_id`, backed by `review.tsv` on disk.

Used only through `LocalStore`, which is the one place that maps keys to
`image_id`s.

**Persistence**: Every mutation (`mark_many`, `undo_many`) appends its rows to
`review.tsv` as one buffer (written in a loop that handles short writes),
`fsync`s, and only then updates the in-memory dict; a failed append leaves the
dict unchanged and its partial bytes are cut off. Loading folds the log (last row per `image_id` wins; a tombstone removes the `image_id`). Safe to kill the
process at any point: a torn append is ignored on load and dropped by the next mark (see
*`review.tsv`*). A kill during migration leaves `review.tsv` either old or
migrated, but may leave a `.review.tsv.*.tmp` behind; it is ignored by the
tool and harmless to it, but it holds `image_id`s (source paths), so delete it
by hand.

### Key Methods

| Method | Description |
|--------|-------------|
| `mark_many(targets, status, pass_number, *, reviewer, mode) -> list[Change]` | Record one verdict on every `(image_id, batch)` in `targets` (same timestamp; `grid_size` = `len(targets)`; `tool_version` = `package_version()`); returns each row written with the decision it replaced |
| `undo_many(changes, *, reviewer)` | Append the undo rows for one `mark_many` result in one append (see *Undo rows*); an `image_id` listed twice (keys sharing it) gets one row |
| `migrate()` | Rewrite an old five-column file with the current header, once (see *`review.tsv`*); no-op otherwise |
| `get_status(image_id, current_pass) -> Status` | Pass-aware status (see *Pass Logic*) |
| `decisions() -> dict[str, Decision]` | A copy of the latest decision per `image_id` (never a tombstone), for `export`; `ValueError` if `review.tsv` ended in a torn line when loaded (not yet dropped by an append) |
| `current_pass(image_ids) -> int` | Auto-detect pass number |

### Pass Logic

`get_status(image_id, pass)` maps the image's (last) row to a `Status`:

| Row | Status |
|-----|--------|
| none | UNREVIEWED |
| from this pass or a later one | its verdict (CLEAN or DIRTY), as recorded |
| from an earlier pass, CLEAN | CLEAN |
| from an earlier pass, DIRTY | FLAGGED |

`mark_many` stores `max(existing pass_number, requested pass)` for each image,
so an image's recorded pass never decreases (its verdict and timestamp still
update). A lower `--pass`, or a manifest grown by one image (which sends
`current_pass` back to 1), therefore cannot hide or overwrite later-pass
decisions: in such a view a later-pass DIRTY image reads DIRTY, not FLAGGED,
and is not grid-eligible. `ReviewStore.mark` returns statuses as seen at the
requested pass.

| Pass | Shows (default `unreviewed` filter) |
|------|-------|
| 1 | All UNREVIEWED images |
| N > 1 | FLAGGED images (marked DIRTY in an earlier pass) in single mode, plus any still-UNREVIEWED images; grid mode skips FLAGGED images |

`current_pass` returns 1 if any image has never been reviewed. Otherwise it
returns `max(pass_number)` if that pass still has work in `TODO_STATUSES`
(UNREVIEWED or FLAGGED), or `max(pass_number) + 1` if the pass is fully
complete.

## Connection String (`connection.py`)

`RemoteTarget(host, port, token, fingerprint)`, a frozen dataclass; `token` is
excluded from `repr`.

Format: `ir://HOST:PORT/?token=TOKEN&fp=sha256:FINGERPRINT`, where `HOST` is a
hostname, dotted IPv4 address or bracketed IPv6 address; `TOKEN` is
`[A-Za-z0-9_-]+` (`secrets.token_urlsafe(16)`); `FINGERPRINT` is 64 lowercase
hex characters, the SHA-256 of the server certificate in DER form
(`cert_fingerprint`).

`RemoteTarget.parse` is strict and raises `ValueError` with a specific message
for: a scheme other than `ir`; user info; a path other than `/` or empty, or a
fragment; no host; an invalid IPv4, IPv6 (zone ids rejected) or host name; a
missing or invalid port (1-65535); a query that is not exactly one `token=` and
one `fp=`; a malformed token or fingerprint. `to_uri()` is the inverse.

## Server (`server.py`)

The threat model these sections implement is summarized in [SECURITY.md](SECURITY.md).

`make_server(store, host, port) -> (ReviewServer, RemoteTarget)` generates a
token, a certificate, the TLS context and the listening server. It enforces
the no-wildcard and advertisable-host checks (see `serve`) and leaves no
listener behind when it raises.
`ReviewServer` is a `ThreadingHTTPServer` (daemon threads) holding the store,
the token, a store lock, and the sets of known keys and batches taken from the
manifest at start.

### TLS and certificate lifecycle

A new self-signed EC P-256 certificate (CN = host, truncated to 64 characters;
valid from 5 minutes ago for 30 days) is generated at every start, along with
a new token. The key is written to a private temporary directory (0700, key
file 0600) only long enough for `load_cert_chain`, then the directory is
removed. TLS >= 1.2. The certificate fingerprint goes into the connection
string. Nothing is reused across runs.

### Authentication

Every request must carry `Authorization: Bearer <token>`, checked with
`hmac.compare_digest` before any routing; otherwise 401 and the connection is
closed (any request body is left unread).

### Endpoints

All responses are JSON unless noted. Requests are parsed into typed values at
the boundary (`parse_pass`, `parse_mark`, `parse_undo`).

| Request | Response |
|---------|----------|
| `GET /version` | `{"api": N, "version": str}`: the wire API version (`connection.API_VERSION`) and the installed `image-review` package version (`"unknown"` if not installed) |
| `GET /manifest` | `[{"key": str, "batch": str}, ...]` |
| `GET /image?key=K` | `image/jpeg` bytes; 404 if the key is unknown (checked against the manifest's keys before the store is asked), unreadable, or does not match its recorded `jpeg_sha256` |
| `GET /statuses?pass=N` | `{key: "CLEAN"\|"DIRTY"\|"UNREVIEWED"\|"FLAGGED", ...}` for every key; `N` integer >= 1 |
| `GET /current_pass` | `{"pass": N}` |
| `GET /skipped` | `{"failed": N, "ignored": M}` (counts of the `kind` column of the work dir's `skipped.tsv`; both 0 if it has none). Only counts are sent, never `image_id`s or reasons (which contain source paths) |
| `POST /mark` | Body `{"keys": [str, ...], "status": "CLEAN"\|"DIRTY", "pass": N, "reviewer": str, "mode": "single"\|"grid"}`; `reviewer` is checked with `connection.parse_reviewer` (1-64 printable characters, no tab, newline or other control character, not all whitespace) and recorded as the client's unauthenticated claim; other fields are ignored; responds `{key: status, ...}` for every key affected (as `ReviewStore.mark`) |
| `POST /undo` | Body `{"pass": N, "reviewer": str}`, checked as for `/mark`; other fields are ignored. Undoes the server store's latest mark (`ReviewStore.undo`); responds `{key: status, ...}` for every key affected, or `{}` when there is nothing to undo |

Only keys and skip counts appear on the wire; original `image_id`s never do.

**API version rule.** `connection.API_VERSION` (an integer, currently 6; v2 added `GET /skipped`;
v3 added `FLAGGED` to the `Status` vocabulary, which `/statuses` responses
may contain; `/mark` responses hold only the verdict just recorded; v4 dropped
`batch` from the `/mark` body, the server taking each key's batch from the
manifest, and added `reviewer` and `mode`; v5 added `POST /undo`; v6 made
`GET /skipped` always send counts, never `null`, and refused repeated query
parameters with 400) is shared by client and server. Any change to request or response shapes, or to
the `Status` vocabulary, must bump it. Client and server are installed
separately, so skew is expected and must fail clearly rather than as a
malformed reply or a 404.

### Error semantics

| Status | Cause |
|--------|-------|
| 400 | Bad request: `Transfer-Encoding` present; a body on anything but `POST /mark` and `POST /undo`; a query parameter appearing more than once; `/image` without exactly one `key`; a missing or invalid `pass` on `/statuses`; `/mark` or `/undo` with a missing, repeated or oversized (> 1 MiB) `Content-Length`, invalid JSON, non-object body, a missing or invalid `pass` or `reviewer`; `/mark` with empty or non-string `keys`, an unknown key, a status other than CLEAN/DIRTY, a `mode` other than single/grid |
| 401 | Missing or wrong token |
| 404 | Unknown path, or HEAD/PUT/DELETE/PATCH/OPTIONS (closes the connection); other methods get the stdlib 501 before authentication; unknown or unreadable image key |
| 500 | Any unexpected store failure; only the exception class name is logged |

400, 401, 500 and unknown-route 404 replies send `Connection: close`, because
a request body may be unread; an image 404 keeps the connection open.

### Headers and connection handling

Every response, including stdlib error pages, carries `Cache-Control:
no-store` and `X-Content-Type-Options: nosniff`. The server speaks HTTP/1.1
with keep-alive; Nagle is disabled (headers and body are separate writes) and
idle connections time out after 60 s. The TLS handshake is deferred to the
handler thread (so a stalled client cannot block `accept()`) and gets a 10 s
deadline before authentication; a failed handshake is logged as
`connection error: <ExceptionClass>` and the connection closed. The client's
readiness probe through `--via` (a TCP connect and close) produces one such
`SSLEOFError` line per client start.

### Concurrency limits

Each writer holds its own in-memory copy of the review state and appends to
`review.tsv` from it, so two writers on one work directory would each work from
a stale view of the other's marks. Worse, a writer that loaded a torn tail
truncates the file back to that offset before its next append, which would cut
away every row the other writer appended since; the inode-and-size check before
truncating (see *`review.tsv`*) makes that mark fail instead. A writable `LocalStore` (used by `review` and `serve`)
therefore holds `work_dir/review.lock`:

- Contents are JSON `{"host", "boot_id", "user", "pid", "started"}`: hostname,
  `/proc/sys/kernel/random/boot_id` (`""` where unavailable), login name (the
  uid if there is none), pid (an integer from 1 to 2^31-1, what `os.kill`
  accepts; any other value makes the lock malformed), and UTC ISO start time.
  The full record is written to a unique sibling
  `review.lock.<host>.<boot_id or ->.<pid>.<random>`
  (created `O_EXCL` with, and `fchmod`ed to, the work dir policy's file mode:
  0600 private, 0660 group, so teammates can read who holds it; then
  `fsync`ed), which is hard-linked to `review.lock` and then removed (the same
  helper as `export --output`). A lock is therefore never seen empty or
  half-written. If `link` reports an error but
  `os.path.samefile(sibling, review.lock)` holds (a lost NFS reply), the lock
  was acquired. Where hard links are unsupported (`link` fails with `EPERM`,
  `ENOTSUP`/`EOPNOTSUPP` or `ENOSYS`: vfat/exFAT, SMB, many FUSE mounts),
  `review.lock` is created directly the same way (`O_CREAT | O_EXCL`, mode,
  record, `fsync`); a reader that finds the lock empty re-reads it for up to
  1 s before treating it as corrupt. A process killed mid-acquire can leave a
  sibling behind; each acquire removes, best effort, siblings named with this
  host and `boot_id` whose pid no longer exists (a name whose pid is not a
  decimal number from 1 to 2^31-1 is left alone). Others are harmless and can
  be deleted by hand. `flock` is not used: it is unreliable on Lustre/GPFS/NFS.
- A lock is reclaimed automatically only when its process is verifiably gone
  on this machine: same hostname and same non-empty `boot_id` (so neither a
  different machine with the same hostname nor a lock from before a reboot or
  from an older version qualifies), and `os.kill(pid, 0)` raises
  `ProcessLookupError` (`PermissionError` means another user's process
  exists; `os.kill` is only called on POSIX, as signal 0 is `CTRL_C_EVENT` on
  Windows). Containers share the host's `boot_id`, so one that also shares the
  hostname but has its own pid namespace is not told apart. The lock is then
  removed only if `review.lock` is still the same file (`(st_dev, st_ino)` from
  the `fstat` of the descriptor it was read from). That check relies on the
  descriptor staying open from the read through the staleness check, the
  `stat` and the `unlink`: an open inode cannot be freed, so its number cannot
  be reused by a lock another reclaimer has just re-created (NFS
  silly-renames an open file instead). Only the gap between that `stat` and
  the `unlink` remains. Creation is retried once.
- Otherwise `WorkDirLocked` is raised: `work directory is in use by USER on
  HOST (pid PID) since STARTED; lock file PATH. If that process is gone, delete
  PATH by hand` (the start time helps judge, e.g. after a pid was reused). An
  unreadable or malformed lock file counts as held; the message names it and
  gives the same advice.
- `close()` removes the file only if it still names this process (host,
  `boot_id` and pid) and is still the file it read (held open the same way);
  it is idempotent.
- `serve` turns SIGTERM and SIGHUP into `KeyboardInterrupt` before taking the
  lock, and closes the store last, while holding the server's store lock, so a
  mark in progress finishes first and later ones fail (500). `review` turns
  SIGHUP (lost terminal or ssh session) into `KeyboardInterrupt` while the store
  is open.

The server additionally assumes one reviewer per server. Its undo stack is
one, in the server's store, shared by every client: if several clients use one
server, `z` in one undoes the latest mark from any of them. Serving several
clients at once is out of scope.

### Locking

Manifest, statuses, current-pass, mark and undo calls run under a single store lock
(`ReviewDB` is not thread-safe). `/image` does not take it (read-only file
access), and the lock is never held while writing to the network.

### Logging policy

Logger `image_review.server` (see *Logging* under *CLI Interface*). One INFO
record per response: `peer-IP METHOD path status`; the formatter's timestamp
supplies the time, so a line reads `2026-10-01T14:03:07+02:00 INFO
image_review.server: 10.1.2.3 GET /image 200`. The query string is dropped,
control characters in the peer, method and path are escaped (`_escape`, at the
call site), and stdlib `log_message` output (which can echo request lines) is
suppressed. Tokens, keys in queries and exception messages are not logged. Two
other records exist: WARNING `connection error: <ExceptionClass>` for failed
handshakes and other connection-level failures, and ERROR `internal error:
<ExceptionClass>` for 500s.

## Remote Store (`remote.py`)

`RemoteStore(target)` implements `ReviewStore` over HTTPS, connecting to
`target.host:target.port`. With `--via`, the CLI passes a copy of the target
with host `127.0.0.1` and the forwarded local port (the local end of the ssh
tunnel), keeping the pin and token. It is a context manager; `close()` shuts down the
pool and closes every connection. Images are returned as bytes and never
cached on disk.

**Startup check.** `RemoteStore.check_api()` GETs `/version` (parsed by
`parse_version`; a malformed reply raises `RemoteError`) and raises
`ApiMismatch` (a `RemoteError`) if the server answers 404 ("server is too old
to report its API version") or reports a different `api` ("server speaks API
vS, this client vC"); both messages end "install the same image-review version
on both machines". `cli._remote_store` calls it after entering the store and
before yielding, and `cli.open_store` turns `ApiMismatch` into a
`ClickException` (exit 1).

**Certificate pinning**: `PinnedHTTPSConnection` disables CA and host name
verification and instead compares the peer certificate's SHA-256 fingerprint
to the pinned one (`hmac.compare_digest`) inside `connect()`. Because
`http.client` reconnects transparently, every connection and every automatic
reconnect is checked before any request (and so the token) is sent.
Mismatch raises `FingerprintMismatch` and is never retried. TLS >= 1.2.

**Connections and retry**: one persistent connection per thread. If a request
fails with a stale-connection error (connection reset/remote disconnect,
broken pipe, SSL EOF/zero return, `CannotSendRequest`, `ResponseNotReady`),
the connection is closed and the request retried once on a fresh one; a
second failure raises `RemoteError("connection lost: ...")`. Timeout is 30 s.
`POST /mark` and `POST /undo` are not idempotent (a replayed mark writes a
second row and pushes a second undo entry; a replayed undo undoes a second
mark), so they are never retried: each is sent once on a fresh connection (the
thread's connection is closed first, so the request reconnects and re-checks
the pin), and any transport error raises `RemoteError`; the reviewer re-marks.
Any other `OSError` or `HTTPException` raises `RemoteError` immediately.

**Error mapping**: `RemoteError(StoreUnavailable)` carries `status`, the HTTP
status when the server answered, else `None`. A non-200 reply raises
`RemoteError` with that status, except `image_bytes`, where 404 raises
`KeyError(key)` (an unloadable image, not an outage). Responses are parsed
strictly (`parse_manifest`, `parse_statuses`, `parse_pass`, `parse_skipped`); malformed
payloads raise `RemoteError`.

**`image_bytes_many`**: fetches distinct keys concurrently on an 8-worker
pool; a `KeyError` (404) is warned about and omitted, like `LocalStore`; any
`RemoteError` cancels the remaining fetches and propagates.

## SSH Tunnel (`tunnel.py`)

For clients that can reach only a login node. `ssh_tunnel(via, host, port,
*, ready_timeout=120)` is a context manager that yields a free local port.
The tunnel carries the already-pinned TLS connection end to end and adds no
trust of its own.

**argv**:

```
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -o ControlPath=none -L 127.0.0.1:LOCAL:HOST:PORT -- VIA
```

stdout goes to `/dev/null`; stdin and stderr are inherited so password/MFA
prompts and ssh errors reach the terminal. `ControlPath=none` keeps the
forward out of a shared ControlMaster so none is left behind after exit (this
also means every `--via` authenticates afresh). `LOCAL` is a free port found
by binding 127.0.0.1:0 (a small race window: if another process takes it, the
pin makes the connection fail rather than leak credentials).

**Readiness**: polls every 0.1 s until a TCP connect to `127.0.0.1:LOCAL`
succeeds. `TunnelError` if ssh exits first (status 0 gets a hint about
`ForkAfterAuthentication`/`ControlPersist`; other statuses point at the ssh
output), if `ready_timeout` elapses, or if `ssh` is missing or cannot be run.
The probe connection is accepted and dropped by the server, which logs one
`SSLEOFError`.

**Teardown**: on every exit path (normal, exception, interrupt) ssh is sent
SIGTERM, waited on for 5 s, then killed. While the tunnel is up (main thread
only) SIGTERM and SIGHUP are converted to `KeyboardInterrupt` so this runs; a
signal that was ignored at entry is left ignored (`nohup`) and previous
handlers are restored on exit.

**`parse_via(raw)`**: validates the destination (`user@host`, `host` or a
config alias). Empty, leading `-` (option injection) and any character outside
`[A-Za-z0-9._@%:\[\]/-]` are rejected with `ValueError`; the destination is
also passed after `--`.

## Multi-Pass Review Workflow

1. **Pass 1 (grid triage)**: `--mode grid`. Mark grids CLEAN or DIRTY.
   Each grid mark applies to all constituent images. Err toward DIRTY.
2. **Pass 2 (single review)**: `--mode single`. Only images marked DIRTY in
   pass 1 (now FLAGGED) are shown. Inspect individually. Grid mode skips
   FLAGGED images, so they cannot be cleared by a grid keypress.
3. **Pass 3+**: Repeat single-mode review on the shrinking DIRTY pool
   until confident.

Sessions are resumable: quitting mid-session saves all progress (with a
remote store, progress is saved on the server at every mark). Re-running
the same command shows only remaining unreviewed (pass 1) or flagged (pass 2+)
images.

When done, `export` (on the machine holding the work directory) writes the
allowlist: only the source files reviewed CLEAN, by path and SHA-256; anything
not listed must not be released. `--report` lists the rest: images still DIRTY
(or FLAGGED) as `DIRTY`, inputs that failed to preprocess as `NOT_REVIEWED`,
and inputs that were not images as `IGNORED`.
