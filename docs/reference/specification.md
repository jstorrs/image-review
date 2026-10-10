# image-review Specification

## Purpose

`image-review` is a CLI tool for reviewing medical (DICOM) and general images
for burned-in Protected Health Information (PHI). It provides a four-step
workflow: **preprocess** raw images into normalized JPGs, **review** them
interactively in a fullscreen viewer, report **status** on review progress,
and **export** the allowlist of files that may be released.

## Requirements

- Python >= 3.12
- Dependencies are grouped into extras (core, `preprocess`, `codecs`,
  `viewer`, `all`). The extras, what each command needs and every minimum
  version are in [Installation](../install.md#choosing-extras) and
  [Minimum dependency versions](../install.md#minimum-dependency-versions);
  `pyproject.toml` declares them. The `dev` and `docs` extras are described in
  [CONTRIBUTING.md](https://github.com/jstorrs/image-review/blob/main/CONTRIBUTING.md).
  The reasons for the minimums are on the installation page. `codecs`
  is separate from `preprocess`: without it, any compressed DICOM pydicom
  cannot decode by itself or through Pillow is `failed` with `cannot decode
  <transfer syntax name>: ...` (see the DICOM preprocessing pipeline, step
  0). `pylibjpeg-libjpeg` is deliberately not used (GPL-3).

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
status.py           Status, Verdict, MarkMode, Rotation, TODO_STATUSES vocabulary; GRID_ELIGIBLE, grid_status, grid_clean_refused (stdlib only)
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
layout.py           Pure grid layout: jpeg_size, fit_size, plan_grids (stdlib and rectpack only)
grid_packer.py      Review-time compositing of packed grids (pygame)
review_db.py        Persistent review state (review.tsv)
util.py             Shared utilities (surface loading)
```

All review-time data access goes through a `ReviewStore`. The controller and
grid packer never touch the work directory; `LocalStore` serves it directly,
and `RemoteStore` talks to an `image-review serve` process that wraps a
`LocalStore`. `status.py`, `store.py`, `lock.py`, `export.py`, `atomic.py`, `server.py`,
`connection.py`, `remote.py`, `tunnel.py`, `signals.py` and `layout.py` import without pygame,
PIL, numpy or skimage.

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
  `%(asctime)s %(levelname)s %(name)s: %(message)s`; `cli.LogFormatter`
  renders `asctime`. The format with an example, the levels (`-v`, `-q`, and
  the usage error for both together), what is logged at each level, and the
  split between stdout and stderr are in
  [Commands](../commands/index.md#logging). During `preprocess` the handler is
  swapped by `tqdm.contrib.logging.logging_redirect_tqdm`, so records are
  written with `tqdm.write`. The package logger name is `cli.PACKAGE_LOGGER`;
  `cli` logs as `image_review.cli` even when run as `python -m
  image_review.cli`.
- **Never logged**: the
  [security model](security-model.md#the-server-image-review-serve) lists what
  the server never logs. Client-supplied fields in server records are escaped with
  Python's `unicode_escape`, so control characters cannot reach the terminal.
- **Not diagnostics**: the CLI's own output stays plain `print`/`click.echo`,
  not logging. Which output goes where is in
  [Commands](../commands/index.md#output-streams).

### `image-review preprocess`

The synopsis, options, defaults and usage errors are in
[`image-review preprocess`](../commands/preprocess.md).

The `--jobs` default is resolved when the command runs (`cli.default_jobs`)
from the usable CPUs (`len(os.sched_getaffinity(0))` where available, else
`os.cpu_count()`, else 1): `$SLURM_CPUS_PER_TASK` capped at the usable CPUs
if it is a whole number >= 1 (ASCII or other decimal digits only), else the
usable CPUs capped at the smallest cgroup v2 CPU quota `ceil(quota / period)`
among the `cpu.max` files of the process's own cgroup (the `0::<path>` line
of `/proc/self/cgroup`, under `/sys/fs/cgroup`) and each ancestor up to the
mount root, e.g. a login node's per-user `CPUQuota=` on `user-UID.slice`, or
a container's quota at its namespace root (`0::/`). `max`, a missing or
unreadable file, or an unreadable `/proc/self/cgroup` means no cap at that
level; a path outside the cgroup namespace (`..`) reads only the mount root.
cgroup v1 quotas are not read.

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
are never modified (what that means for users is in
[`image-review preprocess`](../commands/preprocess.md)).

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
is removed. The policy is only mode bits; what that means for the group, ACLs
and a pre-created work directory, and the line `--access group` prints, are in
[Access control](../commands/preprocess.md#access-control). The policy is not
stored: `access_of(st_mode)` recovers it from an existing
work directory (`group` if the group bits are rwx, else
`private`), and later writers follow it. `ReviewDB` opens `review.tsv` with
`os.open(O_APPEND | O_CREAT, file_mode)` and, when the file is new or empty and
its mode differs from that policy's `file_mode` (the umask may have narrowed
it), `fchmod`s it, so a new `review.tsv` is 0600 in a private and 0660 in a
group work directory. An existing, non-empty file keeps whatever mode it has,
except that migrating an old-header file (see *`review.tsv`*) writes the new
file with the policy's `file_mode`.
`review`, `serve`, `export` and local `status` call `world_access_warning`
when they open the work directory: if it or `manifest.tsv` has any other bit,
they log its message as a WARNING (the text is in
[Access control](../commands/preprocess.md#access-control)). Nothing chmods an
existing directory.

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

**Image IDs** are absolute paths derived from the source, in the forms listed
under [`manifest.tsv`](work-directory.md#manifesttsv). The CLI resolves each
SOURCE (`Path.resolve()`, symlinks included) before `discover`; paths below a
directory SOURCE are joined from walked names and never resolved, so a
symlinked file there keeps its link path (a symlinked directory below a
SOURCE is never entered, so no file is reached through one).

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
rendering or JPEG-encoding one input (e.g. an image wider or taller than libjpeg's
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
empty) and `preprocess.json`. The summary line and the exit status are in
[Skipped inputs](../commands/preprocess.md#skipped-inputs).

### `image-review review`

The synopsis, options and defaults are in
[`image-review review`](../commands/review.md); the option rules and startup
error messages are in
[Connecting to a server](../commands/review.md#connecting-to-a-server).
`--reviewer` defaults to `getpass.getuser()` at run time and is checked by
`connection.parse_reviewer` (1-64 characters, all `str.isprintable()`, not
all whitespace). `--batch` is checked by `unknown_batch_message` against the
manifest's batches once the store is open.

Opens a store (see below), initializes pygame, creates a `ReviewSession`,
runs the event loop, then shuts down pygame. pygame starts only after the
store (and tunnel) is up, so ssh password/MFA prompts keep terminal focus.

**Store selection** (`cli.open_store`, shared by `review` and `status`):

- Without `--remote`: `LocalStore(work_dir)` (writable for `review`, holding
  the work dir lock until the command exits; `read_only=True` for `status`),
  entered as a context manager like `RemoteStore`. `WorkDirLocked` is a
  `ClickException` (exit 1) with its message. A `--via` whose parameter
  source is not the environment is a `UsageError`.
- With `--remote`: `--work-dir` is a `UsageError`, worded by the `--remote`
  parameter source. The string is parsed with `RemoteTarget.parse` and
  `--via` validated with `parse_via`; either failure is a `ClickException`
  naming the option. `_remote_store` enters `ssh_tunnel` first when `--via`
  is given, and the `RemoteStore` connects to `127.0.0.1:<local port>`
  instead of the advertised host; it calls `check_api()` before yielding. The
  store is closed before the tunnel.
- Startup failures: `_remote_failure` turns `TunnelError` and `ApiMismatch`
  (their own messages), `FingerprintMismatch`, and `RemoteError` with status
  401, any other status, or no status into `ClickException`s. The login-node
  hint is added only with `--via` when the `RemoteError`'s cause is an
  `OSError`.

### `image-review status`

```
image-review status [--check] [--work-dir DIR | --remote CONNECTION_STRING [--via DESTINATION]]
```

Same store selection as `review`, but a local store is opened read-only (no
lock). The command fetches the manifest, the current pass and that pass's
statuses from the store and prints the report. The counts come from the pure
`store.summary` and `store.batch_summary`, which are pass-aware. Then it calls
`store.skipped()`; a `ValueError` from a malformed `skipped.tsv` becomes a
`ClickException` (exit 1). Does not start pygame.

What the report shows, the FLAGGED status, the Skipped line and what happens
when `skipped.tsv` is missing or malformed are in
[status](../commands/status.md). `status` cannot count unloadable images (see
*Unloadable Images*): they count as UNREVIEWED until marked DIRTY.

**Exit status**: 0, unless `--check` is given. Then it exits 1 if any image is
UNREVIEWED or `store.skipped()` reports a `failed` row, else 0; the meaning is
in [Checking from a script](../commands/status.md#checking-from-a-script). The
skipped counts come from `store.skipped()`, so it works the same over
`--remote`. Errors (bad store, malformed `skipped.tsv`) exit 1 or 2 as without
`--check`.

### `image-review export`

```
image-review export [--work-dir DIR] [--output FILE] [--report FILE] [--allow-live]
```

Writes the allowlist of releasable files and the optional report of the
rest. What they hold, how to use them, the exit codes and the main messages
are in [export](../commands/export.md); this section is the mechanism. Local only:
`--remote` is declared (hidden) only to be refused with a `UsageError`; it has
no envvar, so `$IMAGE_REVIEW_REMOTE` is ignored and does not get in the way
of `--work-dir`. There is no server endpoint for it, so `image_id`s and
`source_sha256`s never go over the wire. The whole command runs under
`interrupt_on(*TERMINATION_SIGNALS)`, so SIGTERM/SIGHUP unwind like Ctrl-C and
remove a half-written output.

Whether `--report` and `--output` name the same file is checked after
`os.path.realpath`, before anything is read.

**Refusals** (`ClickException`, nothing written; the list is in
[Refusals and output files](../commands/export.md#refusals-and-output-files)):

- `lock.live_writer(work_dir)` returns the `WorkDirLocked` of a held
  `review.lock`: held unless it names a process of this machine and boot that
  no longer exists (`is_stale`); an unreadable lock counts as held. It is
  checked at the start and, when there was no writer then, again after
  `export_rows()`. The message is `WorkDirLocked`'s, then export's advice
  (both quoted on the export page).
  `--allow-live` turns either refusal into a WARNING.
- `ReviewDB.decisions()` raises on a torn last line of `review.tsv`, and
  `load_skipped` on a malformed `skipped.tsv`. Neither is overridable.
- `format_allowlist`'s or `format_report`'s `ValueError`, naming the
  `image_id` with `repr` and the column, for a field holding a control
  character (Unicode category `Cc`) or U+2028/U+2029, which covers every
  character `str.splitlines()` breaks at, or starting with `"`, which
  CSV-aware readers (Python's `csv`, pandas, spreadsheet import) take as the
  start of a quoted field. Both texts are built even without `--report`, so
  one unsafe field anywhere refuses the whole export.
- `os.path.lexists` finds `--output FILE` or `--report FILE`, before either is
  written. `write_new_file`'s `O_EXCL` still refuses one created after the
  check.

Then it opens a read-only `LocalStore` (no lock; nothing is written in the
work directory) and calls its `export_rows()`. That applies the pure
`export.export_rows` to the manifest entries, `ReviewDB.decisions()` and
`load_skipped`, giving one row per source file (below), which
`export.split_allowlist` splits into the allowlist and the report.

**Split** (`export.split_allowlist(rows) -> (allowed, report)`) implements
[What is allowlisted](../commands/export.md#what-is-allowlisted). Each list
keeps the rows' order, and every row lands in exactly one. The allowed rows
are typed `AllowedRow`, the only rows `format_allowlist` accepts. The
`source_sha256`s of the rows that are not `CLEAN` are collected first, so a
denied `CLEAN` row may come before the row that denies it. Its
`not allowlisted: ...` note is `; `-joined after any existing `reason`.

**Formats** (`export.format_allowlist`, `export.format_report`): the columns
are in [File format](../commands/export.md#file-format). The headers are
`ALLOWLIST_HEADER` and `REPORT_HEADER`. Fields are joined with tabs, every
line ends in LF, and there is no quoting or escaping, which the refusals above
make unambiguous. A row's `source_sha256` is the first one the manifest
records for `X` or `X#icon`, else empty.

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
report (with `--report`) is written first, then the allowlist. Without
`--output` the allowlist's bytes go to stdout's binary stream. Then the INFO
line shown in [File format](../commands/export.md#file-format) goes to stderr,
one count per `ExportStatus` (taken from the `Literal`, so none is left out),
CLEAN last. `--report FILE` and `--output FILE` are each written by
`atomic.write_new_file` (the same helper as `review.lock`; see *Concurrency
limits*):

1. A unique hidden sibling (`.FILE.<random>.tmp`) is created with
   `O_CREAT|O_EXCL`: mode 0600 when a group is to be set, else the file mode
   at once (the umask may strip bits). For a group work directory it is given
   the work directory's group (`fchown(fd, -1, gid)`), so it is right even
   outside the setgid work directory. Only then is it `fchmod`ed to the work
   directory's policy file mode (exactly, whatever the umask), and only then
   written and fsynced, so it is never readable by a group it does not belong
   to. If `fchown` fails, a WARNING is logged and the file is `fchmod`ed to
   0600 instead of the policy mode.
2. The sibling is hard-linked to `FILE`, so `FILE` appears complete or not at
   all. If `FILE` already exists, export refuses (exit 1) and never
   overwrites it.
3. Where hard links are not supported (`EPERM`, `ENOTSUP`, `ENOSYS`), `FILE`
   is created directly with `O_EXCL`, the same way as the sibling in step 1.
   A failed `link` after which `os.path.samefile(sibling, FILE)`
   holds (NFS: the link was made but the reply was lost) counts as made.

One `try`/`finally` around creation through the link removes the sibling on
every exit, including Ctrl-C and SIGTERM/SIGHUP; a hard kill (SIGKILL, a node
crash) can leave it behind, holding source paths, to be deleted by hand.

### `image-review serve`

```
image-review serve [--work-dir DIR] [--bind HOST] [--port N]
image-review serve [--work-dir DIR] (--socket | --socket-path PATH) [--via DEST | --direct]
                    [--ssh-host NODE]
```

The options, the token, the option rules and their exit codes, what `serve`
prints and how it stops are in [serve](../commands/serve.md); this section is
the mechanism. `--bind` and `--ssh-host` default to `socket.getfqdn()`,
`--socket-path` to `default_socket_path()`, `--via` is checked with
`tunnel.parse_via` and `--ssh-host` with `tunnel.parse_ssh_host`.

The [option rules](../commands/serve.md#option-rules) are `UsageError`s
raised before the work directory is opened. "`--port` was given" means any
parameter source but the default. A value that comes only from the
environment (`$IMAGE_REVIEW_VIA`, `$IMAGE_REVIEW_DIRECT`,
`$IMAGE_REVIEW_SOCKET_PATH`) is dropped, not refused, where the rules say it
is ignored; click drops an empty environment value.

Opens a writable `LocalStore` (holding the work dir lock for the server's
lifetime; `WorkDirLocked` is a `ClickException`, exit 1), calls
`server.make_server`, and serves until interrupted.
`make_server` itself refuses wildcard binds (`0.0.0.0`, `::`, empty), any
address the server ends up bound to that is unspecified, and a host whose
connection string would not parse (`RemoteTarget.parse` round trip), raising
`ValueError` after closing its socket; the command reports these as
`ClickException`s.

**Connection string delivery**: on a TTY the string is printed. Otherwise
`server.write_connection_file` writes it to
`~/.image-review/connection-<host>-<port>.txt` (host through `safe_name`;
`private_dir()` creates the directory 0700, requires it to be a directory
owned by the user, and tightens it to 0700 if looser; `write_private_file`
unlinks a stale file, then creates it `O_EXCL|O_NOFOLLOW` with mode 0600 and
removes a partly written one).

**Socket mode (experimental)**: `--socket` or `--socket-path` calls
`server.make_unix_server` (see *Unix-socket server*) at `--socket-path` or
`default_socket_path()` instead of `make_server`.

`$IMAGE_REVIEW_TOKEN` is read with `os.environ`, not a click option, and only
in socket mode. A value goes through `connection.parse_token`; its
`ValueError` never holds the value. It is passed to
`make_unix_server(store, path, token)` (None generates `token_urlsafe(16)`).
`--via`, `--ssh-host` and the token are parsed before the work directory is
opened. An invalid `--via` or `--ssh-host`, a `ValueError` from `make_unix_server`
(bad or busy path), and an `OSError` from binding or from `~/.image-review`
all become `ClickException`s with nothing left behind.

The printed ssh command is `ssh_forward_command`:
`ssh -N -o ExitOnForwardFailure=yes -o ControlPath=none -J VIA -L
127.0.0.1:PORT:SOCKET USER@NODE`. `VIA` is the `jump` argument: `--via`,
else `USER@<login-node>`; with `--direct` it is `None` and `-J VIA` is
omitted. `NODE` is `--ssh-host`, else `socket.getfqdn()`. The user-facing
description is in [Socket mode](../commands/serve.md#socket-mode).
`PORT` is `BROWSER_PORT` (8080), `SOCKET`
is the absolute path that was bound, `USER` is `getpass.getuser()`, else
`<user>`. Each of `VIA`, the `-L` argument and `USER@NODE` goes through
`shlex.quote`. The URL is `browser_url(token)`. Off a TTY,
`write_private_file` writes it to
`browser-{safe_name(short_host())}-{pid}.txt`, and the printed `cat` path is
quoted twice, as the remote shell parses it again.

**Cleanup**: the option checks and parsing run first. Everything after
them (opening the store, binding, announcing, serving) runs under
`interrupt_on(*TERMINATION_SIGNALS)`, installed before the lock is taken, so
SIGTERM and SIGHUP unwind like Ctrl-C. Cleanup is registered on one
`ExitStack`: `_close_with` registers the store's close
(`_close_store_after_marks`, which waits for a mark in flight under
`server.store_lock`) and then `server_close` as soon as the server is bound,
and the announced file's removal is registered as soon as it is written. A
failure at any later point therefore releases them, and they run in reverse:
the file, the socket (`server_close` also removes a Unix socket file), then
the store.

## Data Files

All state lives in the work directory. `preprocess` creates it atomically
(see *Work directory lifecycle*); while a run is in progress its output is in
`.NAME.partial` beside it.

### `manifest.tsv`

Written by `preprocess`. Columns, image-id forms, encoding and the parse
rules users see are in [`manifest.tsv`](work-directory.md#manifesttsv).

`image_id` (source paths, which may carry patient identifiers) and
`source_sha256` (derived from PHI content; a digest of a small input can be
recovered by guessing) are used only inside `LocalStore`/`ReviewDB` and in the
local `export`. Everything else identifies an image by its
`preprocessed_path`; no hash is ever sent over the wire.

The file is parsed strictly (`store.load_manifest`) when the work directory is
opened, and is never repaired. A legacy three-column manifest loads with both
hashes `None`. A violation is a `ValueError` naming `<file>:<line>` (or the
byte offset, for a file that is not UTF-8), which the CLI shows as
`Cannot read work directory: ...` (exit 1).

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

Written by `preprocess` (`provenance`), in the staging directory with the
policy's file mode like the other files. Its keys are in
[`preprocess.json`](work-directory.md#preprocessjson); `sources` are escaped
like an image_id (see *Source loading*).

It holds source paths, so it is as sensitive as `manifest.tsv`. Nothing reads
it back and the server never serves it: `/image` serves only keys listed in
the manifest (the key map is checked before `safe_path`), and no JPG key can
name it.

### `skipped.tsv`

Written by `preprocess`, always (header only when nothing was skipped).
Columns and encoding are in [`skipped.tsv`](work-directory.md#skippedtsv);
the escaping of bad names and the `ignored` reasons are in *Source loading*.
It is parsed strictly by `store.load_skipped` (a missing file reads as
empty).

It contains source paths, so it is as sensitive as `manifest.tsv`. `export`
makes each `failed` `image_id` `NOT_REVIEWED` with its `reason` (folding an
icon's into its file's row), and lists each `ignored` one not otherwise
covered as `IGNORED` with its `reason` (see *Ignored inputs* under *Parts and folding*).

### `review.lock`

Present while a writer (`review` or `serve`) has the work directory open. JSON
naming the holder. Its user-facing rules are in
[`review.lock`](work-directory.md#reviewlock); the mechanism is in
*Concurrency limits*.

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

The columns, the last-row-wins rule and the parse rules users see are in
[`review.tsv`](work-directory.md#reviewtsv). Internally, `mode` holds a
`status.MarkMode` (`single`, `grid`) or `review_db.RowMode`'s `undo`;
`tool_version` is `connection.package_version()`, resolved once per process.
Files written before the log format (one row per `image_id`) are already
valid logs. The file is parsed strictly (`review_db.parse_log`; `FLAGGED` is
never a valid `status`), and a bad file is a `ValueError` naming
`<file>:<line>`, shown as `Cannot read work directory: ...`, never skipped or
rewritten.

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

Compatibility (older versions cannot read a migrated file) is in
[Upgrading an older file](work-directory.md#upgrading-an-older-file).
`FLAGGED` is derived when reading (see *Pass Logic*) and is never written.

### `batch_NNN/img_NNNNN.jpg`

Preprocessed individual image files. Numbered sequentially within each batch.

## Review Store (`store.py`)

### Types

`Key`, `ImageId`, `Status`, `Verdict` and `TODO_STATUSES` (with `MarkMode`
and `Rotation`) are defined in `status.py`, with the grid rules both clients
and the server apply: `GRID_ELIGIBLE`, `grid_status(snapshot, keys)` and
`grid_clean_refused(snapshot, keys)` (see *Grid Status Derivation*), and
`GridCleanRefused`, the exception a store raises when it refuses a grid CLEAN
by that rule (not a `StoreUnavailable`).

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
2. Determine the pass: `store.current_pass()` if not specified (the rule is
   in *Pass Logic*)
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
shows the lost-connection screen of
[Lost connection](../commands/review.md#lost-connection) and the reason is
logged (ERROR). The session is then `DISCONNECTED`: `_handle_event` passes no
key to a screen handler, and only quitting acts, so nothing reaches the store
again. A failed mode restart or next-batch move clears the item list. Any
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

- Display the packing message (see [`image-review review`](../commands/review.md))
  while packing, with its count updated about every 25 images (via
  `pack_into_grids`'s `on_progress`; repainted and `pg.event.pump()`ed, which
  does not consume key events)
- Read screen dimensions, subtract the 50px status bar height
- A window resize rebuilds the grids (see *Resize rebuild* below)
- `filter_rows()` for the current pass/batch/filter, then keep only rows whose
  status is in `status.GRID_ELIGIBLE` (UNREVIEWED or CLEAN), in every
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
  not the grid rules, with one exception: it is marked with mode `grid`, so
  over a server its CLEAN follows the grid rule once it is FLAGGED (the 409
  below). That needs another client writing an earlier-pass DIRTY; locally it
  cannot arise, as the work directory has a single writer and a session's own
  marks never make a key DIRTY in another pass. No grid ever holds a key whose
  pixels it does not show

When a grid is marked CLEAN or DIRTY, `store.mark()` is called with all its
`keys`, and every key in the result is written into the snapshot.

A grid can still come to hold a DIRTY key mid-session, when a key in it shares
an `image_id` with an image marked DIRTY elsewhere. CLEAN on a grid holding any
DIRTY or FLAGGED key is refused (`status.grid_clean_refused`; no store call;
the status bar shows `controller.GRID_HAS_DIRTY`, also logged), unless every
key in the grid is DIRTY, which reverses that grid's own verdict. The
messages are in
[Refused CLEAN on a grid](../commands/review.md#refused-clean-on-a-grid).

The server applies the same rule to every `POST /mark` with mode `grid` (409,
see *Error semantics*), so a snapshot gone stale (e.g. another client marked
a key DIRTY since it was read) cannot record such a CLEAN either.
`RemoteStore.mark` raises `GridCleanRefused` for the 409; it is not a lost
connection: the session logs and shows the same refusal (for a left-out
single, `controller.IMAGE_HAS_DIRTY`), re-reads the statuses (a `StoreUnavailable` there is a lost
connection) and repaints the item's status. Nothing is recorded, so the undo
count is unchanged and there is no auto-advance. `LocalStore` does not check the rule:
the session refuses a grid before calling it, and a left-out single cannot
become FLAGGED locally (see above).

If grid mode has no items but the status filter selected rows it left out, the
session says so instead of implying the review is done: `run()` prints, and a
mode restart shows, `_held_back_message` (text in
[Todo images](../commands/review.md#todo-images)). Its K counts the left-out
rows that are todo as in *Todo*, over the selected batch, or all batches when
none is selected.

**Resize rebuild.** A `WINDOWRESIZED` event refits the current item. In grid
mode, each `refresh_if_needed` (only while reviewing) compares the size held in
the grid cache key (the size the grids were last packed for) with the current
grid size, and repacks when they differ: one rebuild however many resize events
a batch holds, none for a resize back to the packed size before the tick, and
a size change that arrives without a `WINDOWRESIZED` is caught too. The repack
stops autoplay and a pending advance, shows the packing message, and rebuilds
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
the end-of-list state with the held-back message or the end-of-list message,
as a mode restart does. If the store is unavailable during the repack
the session goes to the lost-connection screen, which is not repainted over.
Single mode only rescales. A restart (mode switch, `b`, display change) packs
at the current size, so it leaves nothing to repack.

### Unloadable Images

An image whose bytes are missing (`KeyError`, e.g. a 404 from the server),
do not match the manifest's `jpeg_sha256` (`ValueError`), or cannot be read or
decoded is not an outage (see *Store Failures*). A warning
naming the key is logged, the key is added to the session's
`_unloadable` set, and the item is shown as a placeholder built by
`controller._placeholder` with `viewer.placeholder_surface`: the key and a
short reason, `KeyError` giving the "fetched" reason and any other failure
the "read or decoded" one (text in
[Images that cannot be loaded](../commands/review.md#images-that-cannot-be-loaded));
the error itself goes only to the log. Navigation, `n`, todo-only and autoplay treat it
like any other item (autoplay does not stop at it), so the cursor always points
at a real item. A `SingleItem` (every single-mode item, and the item
for an image left out of the grids in grid mode) is loaded each time it is shown, so the
key joins `_unloadable` before any verdict can count, and a key that loads
again leaves it. Placeholders are never built up front.

A placeholder can be marked DIRTY but never CLEAN: CLEAN on an item holding
any unloadable key is refused (no store call; the status bar shows
`controller.UNLOADABLE_CLEAN`, also logged), checked before the grid rule.
DIRTY is recorded normally, so batch auto-selection moves on and the pass can
finish.

### Grid Status Derivation

A grid's aggregate status (`status.grid_status`) is derived from the snapshot
statuses of its keys:

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

The session runs a pygame event loop. The keys and gamepad buttons, the
display-select and help screens, the end-of-list screens and their messages
are in [Keys and gamepad](../commands/review.md#keys-and-gamepad) and
[End of a batch](../commands/review.md#end-of-a-batch). Internally:

- `_handle_event` quits on `q`/Escape, the Start button or `pg.QUIT` in every
  state. In any other state than `DISCONNECTED` it then offers a key to
  `_handle_mode_key` (`s`, `m`, `M`), and otherwise to the current
  `UIState`'s handler: `REVIEWING`, `SPLASH` (the help screen),
  `DISPLAY_SELECT` (opened by `w`) or `END_MESSAGE`. `DISCONNECTED` takes no
  other input (see *Store Failures*).
- `M` is `pg.K_m` with `KMOD_SHIFT` in the key event's `mod`, not the live
  keyboard state.
- `1`-`9` (`pg.K_1`-`pg.K_9`) call `viewer.switch_display` on the
  display-select screen only. Confirming there restarts grid mode only when
  the display index differs from the one before `w`.
- Gamepad buttons go through `_handle_button`: in `REVIEWING`, B, Y and the
  D-pad map to `c`, `d`, Left and Right (`REVIEW_BUTTON_KEYS`) after stopping
  autoplay; in every other state A is passed on as Space.
- `WINDOWRESIZED` refits the current image; in grid mode the grids are
  rebuilt on the next tick (see *Resize rebuild*).

Gamepads go through SDL's GameController API (`pygame._sdl2.controller`,
initialised when the session starts), so buttons are numbered by SDL's
standard layout, with Xbox-style positions: A bottom, B right, Y top. On
`CONTROLLERDEVICEADDED` the session opens a `Controller(event.device_index)`
and keeps it in a dict keyed by its joystick instance id; on
`CONTROLLERDEVICEREMOVED` it drops the one with `event.instance_id`. Both
pass the dict's size to `viewer.set_joystick_count`. SDL also
sends the raw `JOY*` events for these pads, but none are handled, so each pad
is counted once and a pad's raw button indices never act. A pad with no SDL
mapping gets no `CONTROLLER*` events and is not counted.

After a mark, an `ADVANCE_EVENT` timer (200 ms) advances to the next item.
Navigation past either end shows `_end_message` (or, in todo-only
navigation, `_no_todo_message`) in the `END_MESSAGE` state.

On the review screen every key except Space cancels autoplay (and still does
its normal action); every gamepad button cancels it. Left/Right, the D-pad,
`n`, mode switches, `h` and `w` cancel a
pending post-mark advance, and so does any change of the current item: the
advance belongs to the item that was marked, so an `ADVANCE_EVENT` already
queued when it was cancelled is ignored (`_advance_pending`). The autoplay and
post-mark advance timers act only while an item is being reviewed, never behind
the help, display-select or message screens.

`run()` alternates two halves: `handle_events(events) -> bool` applies one
`pg.event.get()` batch (False means quit; events after a quit are dropped) and
`refresh_if_needed()` repaints. The display only redraws when a dirty flag is
set, to minimize CPU usage, and only while reviewing: the help,
display-select and message screens are painted once
by the viewer and stay until the state changes. When there are no items (an
empty mode, or no batch left), `_handle_end_key` ignores navigation, so the
message stays up.

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

`z` (on the review screen, in single and grid mode, and on the end-of-list
and other message screens, but not the lost connection screen) first cancels
autoplay and a pending post-mark advance. The session counts its own
successful marks since the current mode started (`_undoable`: reset by every
mode restart, +1 per successful mark, -1 per successful undo). At 0, `z` shows
`controller.NOTHING_TO_UNDO` without calling the store, so it only ever undoes a mark made
on one of this mode's items. Otherwise it calls `store.undo(pass,
reviewer=...)`; a `StoreUnavailable` is a lost connection, as for a mark, and a
`{}` result (the store's history is gone) resets the count and shows the same
message. The returned statuses update the snapshot and the todo count. The cursor
moves to the first item, in this mode's item order, holding any returned key,
the review screen is shown for it, and its dwell starts again, so `c`/`d` count
only once the restored item has been on screen for `MIN_DWELL_MS`. Items are
not rebuilt: grid membership is fixed when the mode starts, and an undone
grid's keys are still in it. Only another client's mark on a shared server (out
of scope, see *Concurrency limits*) can leave no item holding a returned key;
the status bar then names up to three of its keys with their new status. `z`
is not dwell-gated: it only undoes a mark on an item this mode showed, and
shows that item again before any verdict counts. Key repeat stays off
(pygame's default; `pg.key.set_repeat` is never called). What the reviewer
sees is in [Undo](../commands/review.md#undo).

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
current mode as for a mode restart (`_undoable` reset), from the snapshot `b`
just fetched, starting at the first item; after a pass change the info bar
announces the new pass. When nothing is found the session drops its items
(they may belong to an ended pass) and resets `_undoable`. If the mode holds
back todo rows of the status filter (grid mode: FLAGGED and DIRTY; counted
over all batches, or the `--batch` one), it shows `_held_back_message` and
moves the session's batch to the first batch holding one (the `--batch` one
itself when given), so `s` opens it. Otherwise it shows `_all_done_message`,
which chooses, in this order: the pass-complete message (under `--filter
unreviewed` when the pass has just advanced), the batch-done message (with
`--batch`), or the all-batches-done message, and appends the current pass
when an explicit `--pass` differs from `current_pass()`. The messages are in
[End of a batch](../commands/review.md#end-of-a-batch). A `StoreUnavailable`
from any of these calls is a lost connection (see *Store Failures*).

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
   JPGs are small. Read each image's dimensions from the JPEG header up to its
   start of scan with `layout.jpeg_size(bytes)`, which decodes no pixels and
   raises `ValueError` on an unreadable or truncated header. Images that are
   missing (the store warns) or whose header cannot be read (warned here) are
   left out of the packing.
2. **Pack**: Each image is packed at `fit_size(w, h, grid_w, grid_h,
   rotate)`: its own size if it fits the bin upright (or rotated, when
   `rotate`), otherwise shrunk, keeping its aspect ratio, only as far
   as the better allowed orientation requires, so nothing is larger than the
   bin. `rotation` is `"always"` (`rotate` true), `"never"` (false) or `"auto"`.
   The plan comes from `layout.plan_grids(sizes, grid_w, grid_h, rotation)`, a
   pure function returning a `GridPlan` (whether rotation was used, the placed
   rects per bin in bin order, and the ids left unpacked), so `serve` can
   compute the same layout. It creates a `rectpack` packer with
   `rotation=rotate` and `(grid_w, grid_h)` bins (unlimited bin count), and
   adds each image as a rect of its fit size, in input order. Under `"auto"` the headers are packed twice, without and
   with rotation (each at its own fit sizes; no pixels are decoded), and the
   rotated packing is kept only if it needs strictly fewer bins. Only the
   chosen packing is composited.
3. **Composite**: One bin at a time: create a black `pg.Surface(grid_w,
   grid_h)`, decode each of the bin's images with `util.load_surface(bytes)`,
   `pg.transform.smoothscale` it to its fit size, and blit it at the packed
   position, after `pg.transform.rotate(-90)` if rectpack rotated the rect
   (`layout.is_rotated`: packed size differs from the fit size). The bin's decoded surfaces are
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

**Status bar**: A rectangle spanning the full width at the bottom, coloured
by `STATUS_COLORS[status]`. `refresh()` draws the status word, then the help,
todo-only and gamepad indicators, at the left; the info text centred; and the
scale percent and the item's name at the right. What each part shows is in
[`image-review review`](../commands/review.md).

**Scale indicator**: `resize()` applies the pure `fit_image`, which returns the
scaled size, offset and factor (displayed size / source size) as one `Fit`, or
`None` when no pixel would show. It never caps the factor, so an image smaller
than the content area is enlarged and shows more than 100%. The status bar
shows it as an integer percent (`scale_percent`, truncated, so a scale just
under 1.0 never reads "100%"). Any scale under 1.0 draws the percent in
`SCALE_WARNING_COLOR`, because small text such as burned-in PHI can be lost
when an image is scaled down; at or above 1.0 it uses the normal font colour.
In grid mode the percent is the smallest image's effective scale: `GridSpec.min_scale` (the smallest `fit_size` / header-size
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

`get_status(image_id, pass)` maps the image's last row to a `Status` as the
table in [Passes](../commands/review.md#passes) gives: no row is UNREVIEWED; a
row with `pass_number >= pass`, or a CLEAN one, keeps its verdict; an
earlier-pass DIRTY is FLAGGED.

`mark_many` stores `max(existing pass_number, requested pass)` for each image,
so an image's recorded pass never decreases (its verdict and timestamp still
update). In a view at a lower pass a later-pass DIRTY image therefore reads
DIRTY, not FLAGGED, and is not grid-eligible. `ReviewStore.mark` returns
statuses as seen at the requested pass.

`current_pass(image_ids)` returns 1 if any of `image_ids` has no row.
Otherwise it returns `max(pass_number)` if any image's status at that pass is
in `TODO_STATUSES` (UNREVIEWED or FLAGGED), or `max(pass_number) + 1` if the
pass is fully complete.

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

The threat model these sections implement is summarized in [the security model](security-model.md).

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
closed (any request body is left unread). The one exception is socket mode,
which serves three static files without it (see Unix-socket server).

### Endpoints

All responses are JSON unless noted. Requests are parsed into typed values at
the boundary (`parse_pass`, `parse_mark`, `parse_undo`, `parse_grids`).

| Request | Response |
|---------|----------|
| `GET /version` | `{"api": N, "version": str}`: the wire API version (`connection.API_VERSION`) and the installed `image-review` package version (`"unknown"` if not installed) |
| `GET /manifest` | `[{"key": str, "batch": str}, ...]` |
| `GET /image?key=K` | `image/jpeg` bytes; 404 if the key is unknown (checked against the manifest's keys before the store is asked), unreadable, or does not match its recorded `jpeg_sha256` |
| `GET /statuses?pass=N` | `{key: "CLEAN"\|"DIRTY"\|"UNREVIEWED"\|"FLAGGED", ...}` for every key; `N` integer >= 1 |
| `GET /current_pass` | `{"pass": N}` |
| `GET /skipped` | `{"failed": N, "ignored": M}` (counts of the `kind` column of the work dir's `skipped.tsv`; both 0 if it has none). Only counts are sent, never `image_id`s or reasons (which contain source paths) |
| `POST /mark` | Body `{"keys": [str, ...], "status": "CLEAN"\|"DIRTY", "pass": N, "reviewer": str, "mode": "single"\|"grid"}`; `reviewer` is checked with `connection.parse_reviewer` (1-64 printable characters, no tab, newline or other control character, not all whitespace) and recorded as the client's unauthenticated claim; other fields are ignored; responds `{key: status, ...}` for every key affected (as `ReviewStore.mark`). A `grid` CLEAN that `status.grid_clean_refused` refuses against the store's current statuses for `pass` (a key DIRTY or FLAGGED, unless every key is DIRTY) is answered 409 and records nothing; `single` marks and DIRTY verdicts are never refused |
| `POST /undo` | Body `{"pass": N, "reviewer": str}`, checked as for `/mark`; other fields are ignored. Undoes the server store's latest mark (`ReviewStore.undo`); responds `{key: status, ...}` for every key affected, or `{}` when there is nothing to undo |
| `POST /grids` | Socket mode only (404 over TLS): grid layouts for the browser client; see *Unix-socket server* |

Only keys and skip counts appear on the wire; original `image_id`s never do.

**API version rule.** `connection.API_VERSION` (an integer, currently 7; v2
added `GET /skipped`; v3 added `FLAGGED` to the `Status` vocabulary, which
`/statuses` responses may contain; `/mark` responses hold only the verdict
just recorded; v4 dropped `batch` from the `/mark` body, the server taking
each key's batch from the manifest, and added `reviewer` and `mode`; v5 added
`POST /undo`; v6 made `GET /skipped` always send counts, never `null`, and
refused repeated query parameters with 400; v7 made `/mark` refuse a grid
CLEAN over a DIRTY or FLAGGED key with 409) is shared by client and server.
Any change to request or response shapes, or to the `Status` vocabulary, must
bump it. Client and server are installed
separately, so skew is expected and must fail clearly rather than as a
malformed reply or a 404.

### Error semantics

| Status | Cause |
|--------|-------|
| 400 | Bad request: `Transfer-Encoding` present; a body on anything but `POST /mark` and `POST /undo` (and, in socket mode, `POST /grids`); a query parameter appearing more than once; `/image` without exactly one `key`; a missing or invalid `pass` on `/statuses`; `/mark` or `/undo` with a missing, repeated or oversized (> 1 MiB) `Content-Length`, invalid JSON, non-object body, a missing or invalid `pass` or `reviewer`; `/mark` with empty or non-string `keys`, an unknown key, a status other than CLEAN/DIRTY, a `mode` other than single/grid; `/grids` as described under *Unix-socket server* |
| 401 | Missing or wrong token |
| 404 | Unknown path, or HEAD/PUT/DELETE/PATCH/OPTIONS (closes the connection); other methods get the stdlib 501 before authentication; unknown or unreadable image key |
| 409 | `POST /mark`: CLEAN with `mode: "grid"` on keys holding a DIRTY or FLAGGED status in that pass, unless every key is DIRTY (`status.grid_clean_refused`); body `{"error": "grid holds a DIRTY or FLAGGED image"}` (no keys); nothing is recorded |
| 412 | Socket mode only: an API request other than `GET /version` and `GET /current_pass` without exactly one `X-Review-Instance` header equal to this server run's (see *Unix-socket server*); body `{"error": "the page was loaded from another serve; reconnect"}`; nothing is read, recorded or packed |
| 500 | Any unexpected store failure; only the exception class name is logged |
| 503 | Socket mode: `POST /grids` while another `/grids` request is being computed; retry later |

400, 401, 412, 500 and unknown-route 404 replies send `Connection: close`,
because a request body may be unread; an image 404, a `/mark` 409 and a
`/grids` 503 (sent after the body is read) keep the connection open.

The 409 check runs under the store lock, so no other mark can land between
it and the write. It reads `store.statuses(pass)` for the request's pass,
which builds the status of every manifest key in memory: O(manifest size)
once per grid CLEAN, small beside the synced append the mark itself does.

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
(`ReviewDB` is not thread-safe). `/image` and `/grids` do not take it
(read-only file access; packing is pure), and the lock is never held while
writing to the network.

### Logging policy

Logger `image_review.server` (see *Logging* under *CLI Interface*). One INFO
record per response: `peer-IP METHOD path status`; the formatter's timestamp
supplies the time, so a line reads `2026-10-01T14:03:07+02:00 INFO
image_review.server: 10.1.2.3 GET /image 200`. The query string is dropped,
control characters in the peer, method and path are escaped (`_escape`, at the
call site), and stdlib `log_message` output (which can echo request lines) is
suppressed. What is never logged is listed in the
[security model](security-model.md#the-server-image-review-serve).
Two other records exist: WARNING `connection error: <ExceptionClass>` for failed
handshakes and other connection-level failures, and ERROR `internal error:
<ExceptionClass>` for 500s. On the Unix-socket server the peer is the
literal `unix` (`accept` gives no peer address there).

### Unix-socket server (experimental)

`make_unix_server(store, path, token=None) -> (UnixReviewServer, token)`
(a given, parsed `Token` is used as is; None generates `token_urlsafe(16)`) serves the same
API as plain HTTP on a Unix domain socket, for a browser reaching it through
`ssh -L PORT:PATH node`. TLS is not used; ssh provides the transport. The path
is made absolute and must fit `sun_path` (108 bytes, 104 on macOS) and contain
no `:` or control characters (`parse_socket_path`). A stale socket file of the
user's with no listener is replaced; a live listener, another user's file or a
non-socket is refused with `ValueError` (`clear_stale_socket`). The socket is
chmodded 0600 before `listen()` (until then every connect is refused), and
`server_close` unlinks it only if it is still the same file. Before the token
check, the single `Host` header must be `localhost`, `127.0.0.1` or `[::1]`
with an optional port of 1-5 digits in 1-65535; anything else (including a
missing or repeated header) is 400 and the connection closed, a defence
against DNS rebinding.

Three static files are served without the token: `GET /` (`index.html`),
`/app.js` and `/app.css`, from `web/` in the package, read once by
`load_assets()` at startup so no request path touches the filesystem. They
hold no PHI and no secret, and the page cannot send the token before it has
loaded. Only these exact GET paths (a query string is ignored) are public,
and only in socket mode; `POST /`, `HEAD /`, `/index.html` and every other
path fall through to the token check, and the usual framing checks (no
`Transfer-Encoding`, no request body) apply first. CSRF does not apply,
because browsers never attach an `Authorization` header on their own. The
page takes the token from the URL fragment, which is never sent to the
server. Every socket-mode response carries `Content-Security-Policy:
default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self';
img-src blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
and `Referrer-Policy: no-referrer`, besides `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff`. TLS mode has no public routes and sends
none of the new headers. The public routes needed no `API_VERSION` bump and
do not change the HTTPS API: they exist only in socket mode, which the
versioned pygame client cannot reach, and the page is served by the same
server it calls. (v7 is for the grid CLEAN refusal on `/mark`.)

Each `UnixReviewServer` start draws an instance id, `token_urlsafe(16)`. It
names the server run and is not an access secret, but it is never logged.
Every socket-mode response, stdlib error pages and the public files included,
carries it as `X-Review-Instance`. Every API request except `GET /version`
and `GET /current_pass` (where the page's first load and its Reconnect start)
must carry exactly one `X-Review-Instance` request header equal to it;
otherwise the server answers 412 with `{"error": "the page was loaded from
another serve; reconnect"}`, closes the connection, and reads, records and
packs nothing (an unknown route is 412 too, not 404). The check runs after
the `Host` check and the token check, so a client without the token gets the
same 400 or 401 as before and learns nothing from a 412; the public files
need no header. Why the check exists (a tab and its token outliving a
restarted serve, with keys that repeat across work directories) is in the
[security model](security-model.md#experimental-browser-review-over-a-unix-socket),
under "A page belongs to one serve". TLS mode sends no such header and
requires none; it needed no `API_VERSION` bump, since only the page served
by this server calls it.

`POST /grids` (socket mode only; over TLS it is an unknown path, 404, and a
body on it is refused with 400 as on any bodyless route) lays out images into
grids for the browser client with the code the pygame client uses
(`layout.plan_grids`), so a grid verdict covers the same images in both. The
body, parsed by `parse_grids` into a `GridsRequest`, is `{"keys": [str, ...],
"width": W, "height": H, "rotation": "auto"|"always"|"never"}`: `keys` a
non-empty list of 1 to `MAX_GRID_KEYS` (1000) distinct manifest keys; `W` and
`H` JSON integers (not booleans) from `MIN_GRID_SIDE` (256) to `MAX_GRID_SIDE`
(16384); other fields are ignored. Anything else is 400. The response is

    {"grids": [[{"key": K, "x": X, "y": Y, "w": W, "h": H,
                 "rotated": bool, "source": [SW, SH]}, ...], ...],
     "left_out": [K, ...]}

with grids in bin order and placements in the packer's order; `source` is the
image's size from its JPEG header, `w`/`h` its packed size (shrunk to fit a
grid, and swapped when `rotated`, as `layout.is_rotated`). `left_out` lists,
in request order, keys whose bytes cannot be read (missing file, `jpeg_sha256`
mismatch), whose header `layout.jpeg_size` refuses, or that the packer left
unplaced. Identical requests give identical responses.

Parity with the pygame client holds under two preconditions. First, key order
matters: rects go to rectpack in request order and its area sort is stable,
so images of equal area can swap places or grids when the order changes. The
page must send a batch's grid-eligible keys in manifest order, as the pygame
controller does (`_grid_items`), to get the same grids. Second, a batch with
more grid-eligible keys than `MAX_GRID_KEYS` cannot use `/grids` (preprocess
`--batch-size` is unbounded): the page must refuse grid mode for that batch
rather than split it, since chunks would pack differently. Given both, the
layout matches `grid_packer.pack_into_grids` exactly for images that decode;
the pygame client also leaves out images that fail to decode, which the
server does not try.

The server reads each image's bytes one at a time, never holding the store
lock, and keeps each successfully read header size in memory for the life of
the server (failures are retried on the next request). `source` and
`left_out` therefore reflect each file's first successful read during this
server run: a file replaced or deleted later keeps its old size and place.
The client contract covers this: the page must check that each decoded
image's natural size equals its `source`, and treat a mismatch or any
`/image` failure as left out, never covered by a grid verdict (as
`grid_packer._composite_bin` checks the decoded size).

One `/grids` request is computed at a time; another arriving meanwhile gets
503 with the connection kept open, meaning retry later. Packing is not
cancelled when the client disconnects, so a reloaded page may see 503 until
the earlier request finishes. Nothing about individual keys is logged.
`/grids` needed no `API_VERSION` bump for the same reason as the public
routes: it exists only in socket mode, whose page is served by the server it
calls.

`default_socket_path()` is `~/.image-review/serve-<short-host>-<pid>.sock`.
When that path would be `SUN_PATH_SIZE` bytes or more, the host part is replaced
by the first 8 hex characters of the SHA-256 of the short host name (not a
truncation, since node names often differ only at the end). If even that does
not fit, it is returned anyway and `parse_socket_path` raises its usual error.

### Browser client (experimental)

`web/app.js` (plain ES2020, no build step) reviews single images and grids,
following the pygame client (see *Single Mode*, *Grid Mode*, *Unloadable
Images*, *Undo*). It obeys the CSP above: no inline script or style, no
`innerHTML`, no external URLs; all text goes in with `textContent`.

`m` (rotation `auto`) and `M` (`never`) switch to grid mode and `s` back to
single mode; each switch rereads `/statuses`, empties the stack of marked keys
and rebuilds the list. It reviews one batch: `m`/`M` stay on the current batch
while it has UNREVIEWED keys, else take the first in sorted order (by code
point, as Python sorts) that has; `b` moves to the next that has, wrapping.
The batch's UNREVIEWED keys go to `POST /grids` in manifest order at the
stage's size in device pixels, as the *Unix-socket server* contract requires;
a batch over `MAX_GRID_KEYS`, a stage under 256 device pixels or a batch with
none (the controller's held-back message) shows a message instead. One
`/grids` request is in flight at a time (a newer build waits for it, and a
stale reply is dropped); a 503 shows "Server busy computing grids; retrying"
and is retried every 2 s while the layout is still wanted. The last layout is
cached, keyed as `controller.GridCacheKey` plus the batch, and kept across
`s`/`m`/`M`; `b` drops it. Items follow `controller._grid_items`, reshuffled
on every build. Each grid is drawn on a `<canvas>` at one canvas pixel per
device pixel from `ImageBitmap`s (four `/image` requests at a time, no blob
URLs); an image that fails to load, or whose decoded size is not its `source`,
leaves its rectangle black and becomes a single item after the grids. The
canvas is shown only once every image is settled. If the canvas loses its
context the grid is taken off screen (dwell cleared) and drawn again when the
context is restored; a grid whose drawing ends while the context is lost has
all its keys moved to single items. The scale shown is the grid's smallest
image scale times the canvas's display scale (taken as exactly 1 when the
canvas box is within half a device pixel of the canvas size), and the dwell
starts as in single mode once the whole canvas is painted inside the stage. A
resize to a new device size hides the grid at once and repacks 300 ms after
the last resize event, landing on the grid holding the previous item's first
key. The bar's height depends only on the window's width and the overlays
float over the stage (see *Layout*), so a message, a button shown or hidden,
the help or the name box never changes the stage size and so never repacks.

A grid verdict covers exactly the grid's drawn keys (any demoted to single
items are not in it) in one `POST /mark` with `mode: "grid"`; every item
shown in grid mode, including a left-out or demoted single, is marked with
`mode: "grid"`, as `controller._mark` sends its mode. `c`/`d` on a grid count
only while its canvas is shown with every key drawn and its dwell is over:
never while it is being drawn or redrawn, hidden by a resize repack, or
after `contextlost`. CLEAN on a grid holding a DIRTY or FLAGGED image (as
`status.grid_clean_refused`: unless every image is DIRTY) is refused
with "grid contains an image already marked DIRTY - review it in single
mode" and no request; DIRTY is always allowed. If the page's statuses were
stale and the server refuses the CLEAN instead (409), the page shows the same
message (for a left-out single that became FLAGGED, which the server refuses
as a grid mark: "image already marked DIRTY in another pass - review it in
single mode"), rereads `/statuses` into its status map, pushes nothing on the
stack of marked keys and stays on the item. A grid holding such an image
is not todo (its status is DIRTY, as `status.grid_status`), so the
move after a mark skips it. A resize repack empties the stack of marked keys
at once, as `controller._rebuild_grids_for_resize` resets its undo count
(the old items are gone, so `z` then says "Nothing to undo"); a repack due
while a `/mark` or `/undo` is in flight waits for its reply, and a mark
whose reply arrives after a resize hid the grids is not pushed on the stack
and moves nowhere. On the end (or stop) screen a resize does not repack (the
controller repacks only while reviewing), so `z` still undoes there; leaving
that screen by `z` or Left/Right at a new size repacks first, landing on
the grid holding the item that would have been shown, and that repack
empties the rest of the stack.

- **Layout**: the stage fills the window above one bar at the bottom, one row
  (`--bar-height`) of fixed height. The bar's font is a fixed 15px and its
  sizes are in `em` of it; its breakpoints are in CSS pixels, so what fits at
  a width fits whatever the browser's default font size (an `em` breakpoint
  would follow that default while the text would not), and browser zoom scales
  text and breakpoints alike. At or below a breakpoint of 1408 CSS pixels of
  window width the bar has a second, shorter row (`--centre-height`, text
  only) for the centre group; the breakpoint depends on the window's width
  alone, so the bar's height never changes with its text or state. Nothing
  wraps; long text is cut short with an ellipsis and its `title` holds it
  whole. Narrow windows (960 CSS pixels or less) drop the key hints from the
  button labels and tighten the spacing; the narrowest (640 CSS pixels or
  less) also shorten Clean, Dirty and Undo to "C", "D" and "↶" (their `title`
  and `aria-label` keep the names). The row has three groups. Left: ← and →
  (Previous, Next), Clean (c), Dirty (d), Undo (z), the item's status word
  (UNREVIEWED, CLEAN, DIRTY or FLAGGED; a grid's is `gridStatus`) and the
  scale badge. Right, pinned to the right end: the reviewer chip ("NAME ✎"),
  "?" (help) and, last, Done (q), whose place Reconnect (r) takes while it is
  offered (the place is as wide as the wider of the two, so neither moves
  anything). Centre, centred in the space between (or in its own row): the
  status message (`#status`, `role="status"`) while it is not empty, else the
  mode ("Single", "Grid · auto" or "Grid · never"), the progress and the
  item's batch and key (in grid mode "grid (N images)"). Empty, the message
  stays rendered (its live region kept) but out of the flow and unseen. The
  reviewer's own moves empty the message: Left/Right, a mark moving on, a mode
  switch (`m`, `M`, `s`, `b`). An undo, a Reconnect and startup set their own
  message as they move ("Undone …", "Undid another client's mark …",
  "Reconnected …", the name prompt). A show the reviewer did not start (a
  resize repack, a redraw after a context loss) leaves the message as it is,
  so a refusal or error lasts until the reviewer's next move, and a stopped
  page's message until Reconnect. Above the breakpoint the centre has room for
  the longest fixed message with the widest left and right groups. A row too
  narrow for all of it gives way in this order: the centre first (it has only
  the space left over; of its parts the batch and key are cut short first),
  then the reviewer's name, down to its "✎"; the left group is clipped at its
  right end only as a last resort, which never happens at 520 CSS pixels or
  more, so the scale badge, like Done or Reconnect, always stays in view
  there. The bar's whole background is the item's status colour, from the
  `data-status` attribute on `#bar`: neutral grey for UNREVIEWED, and also on
  the end and stop screens, with no item, and once the page has stopped (lost
  connection, rejected token, Done); green CLEAN, red DIRTY, amber FLAGGED.
  The colours are CSS custom properties with light and dark variants
  (`prefers-color-scheme`; the stage stays dark in both), and every tint has
  its own text and button colours, at least 4.5:1 (WCAG AA) against it; where
  a verdict button's colour would match the tint (Clean on light CLEAN, Dirty
  on light DIRTY) it takes a darker shade. Disabled buttons are drawn at 55%
  opacity. The status word is always written too, so colour is never the only
  cue.
- **Overlays**: the help and the name box float over the stage (absolutely
  positioned, out of its layout) and never change its size; at most one is
  up. While one is up, `c`, `d`, `z`, Left/Right, `m`/`M`, `s`, `b`, `q` and
  `r` do nothing, every review button (Done and Reconnect included) and the
  reviewer chip are disabled, and no dwell runs: opening one clears the
  dwell, and closing it starts a fresh one for the item under it (painted,
  then `MIN_DWELL_MS`), so nothing is judged on an image that was covered.
- **Help**: the "?" button, or `?` or `h` (either case; not on key repeat),
  opens a list of every key, what the bar's colours and the scale badge
  mean, and the end-of-pass workflow; Escape, `?`, `h` or the button
  closes it.
- **Token**: taken from the fragment into `sessionStorage` (memory if
  storage is blocked) and sent as `Authorization: Bearer` on every API call.
  Pasting a new URL into the tab changes only the fragment, so on a
  `hashchange` to a token fragment the page reloads to take it.
- **Reviewer**: kept in `sessionStorage`. With no valid name stored, the page
  opens a centred name box over the stage at startup ("Who is reviewing?",
  the name field and OK), and every review control is disabled until a name
  is set. The page accepts 1-64 code points, not all spaces (an invalid name
  is marked and OK disabled); the server's `parse_reviewer` decides, and a
  400 from `/mark` or `/undo` shows "invalid reviewer name". Enter (or OK)
  takes a valid name and stores it; Escape closes the box keeping the
  current name, only when there is one. The bar then shows it as a chip
  ("Jane ✎"); clicking the chip opens the box again to change it (disabled
  once the page has stopped). If focus leaves the field while the box is up
  (a click elsewhere), Enter still accepts and any other key puts focus
  back in the field. The name field is the page's only focusable control
  (the help's scrolling box has `tabindex="-1"`).
- **Startup**: `GET /current_pass`, `/manifest`, `/statuses?pass=N`. The
  list is the keys whose status is UNREVIEWED or FLAGGED, shuffled
  (Fisher-Yates); keys stay in it after they are marked. The bar shows the
  progress as "Pass N · K / T left" (K todo of the T listed; in grid mode
  "Pass N · BATCH (k/B) · K / T left"); see *Layout*.
- **Images**: `GET /image?key=K`. The reply must be `image/jpeg` and end in
  `FF D9` (a truncated body is refused); it becomes a blob URL in an `<img>`,
  awaited with `decode()`, and the previous blob URL is revoked. Any failure
  but a 401 or a network error shows the placeholder "Cannot load image:
  KEY", which can be marked DIRTY but never CLEAN ("cannot mark CLEAN: image
  could not be loaded", no request).
- **Scale**: after each paint, on every window resize (which includes a
  browser zoom) and whenever a `ResizeObserver` sees the stage (which the
  `<img>` fills) change, the page computes the display scale, `min(width /
  naturalWidth, height / naturalHeight) * devicePixelRatio` with the box from
  `getBoundingClientRect()` (screen pixels per image pixel under
  `object-fit: contain`), and shows it in the bar as an integer percent,
  `floor(scale * 100 + 1e-9)` as in the viewer. Below 100% it reads "⚠ 46%"
  in a badge with the bar's colours inverted (class `low`), so it stands out
  on every tint, as the viewer's scale indicator warns: small burned-in text
  can be lost when an image is scaled down. A scale that leaves less than
  one pixel counts as 0.
- **Dwell**: `c`/`d` and their buttons act only once the image or
  placeholder has been decoded, painted (two animation frames) and on screen
  for `MIN_DWELL_MS` (200 ms); earlier ones are ignored. The dwell is a
  per-item state (`none`, `running`, `over`) set to `over` by a timer started
  after the paint, so no clock comparison can leave it short. It restarts on
  every item change, including after an undo, and when an overlay closes
  (none runs while one is up; see *Overlays*). An image whose scale is 0 (a
  window too small to show it) starts no dwell, and a resize to 0 clears it;
  a resize that shows it again starts a new one.
- **Marking**: one `POST /mark` with the item's keys and `mode` the display
  mode (`single`, or `grid` for every item in grid mode), controls disabled
  while it is in flight, never retried. On a 200 the keys are pushed on the
  page's stack of marked keys and the reply updates the status map. As in the
  viewer, the marked item then stays up for `MARK_FLASH_MS` (200 ms) with the
  bar in its new status (colour and word; a grid's from `gridStatus`), and the
  page is busy meanwhile: no verdict, move, undo, mode switch, `q` or `r`
  acts. Then it empties the status message (so a run of `c` and `d` keeps the
  mode and progress in view) and moves to the next todo item after the current
  one, wrapping round, or to the end screen. A stop (lost connection, 401,
  412), a resize repack or a context loss during those 200 ms drops the move
  on (a context loss redraws the marked grid instead). A mark whose items a
  resize repack dropped while it was in flight does not move and says "Marked
  STATUS: KEY" (or "grid of N images"). With none left, the end screen says
  what is next. Only when no manifest row of the pass is todo (UNREVIEWED or
  FLAGGED) is it the end of the pass: "Pass N: nothing left to review. Stop
  serve (Ctrl-C), start the next one, then press Reconnect (r)." with the
  Reconnect button, offered while no request is in flight and the page has not
  stopped (a stop takes the hint off the stage; see *Errors*). Otherwise, as
  the controller: in grid mode (whose list is one batch) "No todo images
  remaining - [b] next batch" while any batch has grid items, else the
  pass-wide "No grid items for pass N; K FLAGGED/DIRTY images need single-mode
  review - press [s]"; in single mode (whose list is the whole pass, so only
  when other clients changed statuses meanwhile) "No todo images remaining -
  press [s] to reload the list". Entering grid mode, or a repack, with nothing
  to pack shows the same screen. A 200 whose body cannot be parsed (on `/mark`
  or `/undo`) still counts, since the server has acted: the page rereads
  `/statuses` instead, and stops with "unexpected reply from the server;
  reload the page" if that fails too.
- **Navigation**: Left/Right (and buttons) step through the list without
  marking. As the viewer's "End of list" message, a step past either end
  shows the stop screen on the stage: a red octagonal stop sign (inline SVG,
  coloured by the CSS) over "End of the list", "→ first item · ← last item"
  and the list's todo count, "K todo left in this list" (grid mode: "K todo
  left in this batch - [b] next batch"), or, with none left in the list, the
  end screen's hint (e.g. "No todo images remaining - [b] next batch" or the
  held-back "No grid items for pass N; …", see *Marking*). No item
  is current there: the bar is neutral, no dwell runs, and `c`/`d` and their
  buttons do nothing; `z`, `m`/`M`, `s`, `b`, `q`, `?`/`h` act as on any
  screen, and in grid mode a resize does not repack, as on the end screen. A
  stop (lost connection, rejected token, `q`) takes the stop sign off. A
  further step goes on round: Right to the first item, Left to the last,
  whichever end it was reached from, with a fresh dwell. The end screen
  behaves the same way under Left/Right. Stepping onto the stop screen is a
  move, so it empties the status message. With no todo row left in the pass,
  a step past either end shows the end-of-pass screen (with Reconnect)
  instead of the stop sign, since the next serve is due; Left/Right go round
  from it too. A list with no items keeps its own screen, and the arrows do
  nothing.
- **Undo**: `z` (and a button), also on the end and stop screens. With an
  empty stack it says "Nothing to undo" without a request. Otherwise
  `POST /undo`; `{}` empties the stack and says "Nothing to undo". If the
  reply holds any key of the entry on top of the stack, it is popped and the
  item holding those keys is shown again with a fresh dwell (a grid is drawn
  again from its images). If a repack is pending when the reply arrives, the
  repack lands on the grid holding the undone keys instead. The server keeps
  one undo history for every client (see *Concurrency limits*), so with
  another tab or client marking too the reply can name other keys: the page
  then leaves the stack alone, says "Undid another client's mark: KEY is
  STATUS" (up to three keys), and shows the first listed item holding a
  returned key, if any. So `z` is limited to this page's marks only while the
  page is the server's one client. A Reconnect empties the stack (see
  *Errors*).
- **Keys**: `c`, `d`, `z`, `s` (single mode), `b` (next batch, grid mode
  only), `r` (Reconnect, only while its button is shown) and `q` (done with
  this server, in any state), in either case, so Caps Lock does not matter;
  `m` (grid, rotation `auto`) and `M` (`never`), told apart by the event's
  Shift state rather than the letter's case, so Caps Lock is safe; and
  Left/Right; `?` or `h` toggles the help and Escape closes the help or
  the name box (see *Overlays*, which also lists what an overlay blocks).
  Ignored while the name field has focus, with Ctrl/Alt/Meta, and on key
  repeat (a held key acts once). The buttons, Reconnect, the chip and the
  name box's OK included, never take focus (`tabindex="-1"`, and
  `mousedown` is cancelled), so Enter or Space cannot click one, held, past
  the repeat guard; a click on one also takes focus out of the name field,
  so later keys act on the page.
- **Errors**: a 401 clears the stored token, shows "token rejected (server
  restarted?) - open the new URL" and disables everything; a 412 (another
  server run answers: see *Unix-socket server*) stops the page as a lost
  connection does, with "Server restarted or changed work directory -
  press Reconnect (r)": the verdict, undo or read it answered changes
  nothing, and Reconnect loads the new server as
  below; a network failure
  shows "Lost connection — your marks so far are saved on the server", does
  the same and shows a Reconnect button in Done's place (see *Layout*;
  showing it never moves or resizes anything). The button is
  also shown after `q` (see *Done*) and at the end of the pass (see
  *Marking*), while the page holds a token. Once stopped
  the page sends nothing (a pending repack or `/grids` retry is dropped)
  until the reviewer presses Reconnect (or `r`); it is never retried
  automatically. The reply, body or failure of a request sent before the
  page stopped changes nothing (no statuses, message, item, stack or busy
  state), so a later failure never replaces "token rejected" and never
  stops a reconnected page. Reconnect stops the page first, as a loss does
  (at the end of the pass too), hides the button, says
  "Reconnecting...", takes the item off screen (its dwell with it), empties
  the stack of marked keys (the server may have restarted, or others marked
  meanwhile) and reloads what startup loads with the stored token and
  reviewer name. Startup and Reconnect read `/current_pass` first and keep
  the `X-Review-Instance` of its reply, which every later request sends (a
  reply without it is an unexpected reply); until that reload is in, every
  control is busy, so no key or button sends a request. It then rebuilds
  the current mode: single mode's todo list, reshuffled; in grid mode the
  current batch and rotation (as `m` keeps the batch), laid out afresh by
  `/grids` and landing on the
  grid holding the previous item's first key (kept across a failed
  Reconnect, and the key a pending repack would have landed on), else the
  first. Every item then starts a fresh dwell. The status message says
  "Reconnected", or "Reconnected; now on pass N" if the current pass
  changed or was forgotten by `q` (the new pass is reviewed); after `q` the
  server may serve another work directory or batch, so grid mode starts
  afresh at the first batch with grid items and its first grid. A network
  failure during Reconnect shows "Lost connection" and the button again, a 401
  the token-rejected message, and another error a short message with the
  button; clicks while one is in flight are ignored. Other HTTP errors show a
  short message with the status and leave the controls usable (at startup the
  page stops instead, offering Reconnect only after a network failure);
  nothing is retried automatically but a `/grids` 503 (see grid mode above).
- **Done** (`q` or the "Done (q)" button, in any state but while an
  overlay is up) means done with
  this server: the reviewer stops it with Ctrl-C (the marks are already
  saved), starts the next `serve --socket` on the same socket path and token
  (for the next batch or pass), and presses Reconnect in the same tab. It
  stops the page as a lost connection does, so every request in flight
  turns stale and nothing more is sent. It leaves grid mode (canvas sized to
  0, hidden), revokes the image's blob URL and clears the `<img>`, and drops
  the pass, manifest, statuses, items, marked keys and cached layout, but
  keeps the token and the reviewer name (in `sessionStorage` and in memory)
  and the mode and rotation; the batch and the landing key are forgotten
  too, since another work directory's keys and batch names usually match
  these. Every control is disabled, the Done
  button and the reviewer chip included, but Reconnect is shown; the
  status message says "Done; waiting for the next serve" and the stage "Done.
  Your marks are saved. Stop serve (Ctrl-C), start the next one, then press
  Reconnect (r)." Reconnect then loads whatever the server serves (see
  *Errors*); a 401 shows the token-rejected message. There is no way to
  forget the token on the page: closing the tab does (`sessionStorage` is
  per tab). After a rejected token (or with none) `q` only frees the images
  and the review, and the page keeps its message.

## Remote Store (`remote.py`)

`RemoteStore(target)` implements `ReviewStore` over HTTPS, connecting to
`target.host:target.port`. With `--via`, the CLI passes a copy of the target
with host `127.0.0.1` and the forwarded local port (the local end of the ssh
tunnel), keeping the pin and token. It is a context manager; `close()` shuts down the
pool and closes every connection. Images are returned as bytes and never
cached on disk.

**Startup check.** `RemoteStore.check_api()` GETs `/version` (parsed by
`parse_version`; a malformed reply raises `RemoteError`) and raises
`ApiMismatch` (a `RemoteError`) if the server answers 404 or reports a
different `api`; the messages are in
[Connecting to a server](../commands/review.md#connecting-to-a-server).
`cli._remote_store` calls it after entering the store and
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
`KeyError(key)` (an unloadable image, not an outage), and `mark`, where 409
raises `status.GridCleanRefused` (a refused grid CLEAN, not an outage).
Responses are parsed strictly (`parse_manifest`, `parse_statuses`,
`parse_pass`, `parse_skipped`); malformed payloads raise `RemoteError`.

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

The workflow (grid triage in pass 1, single-image review of FLAGGED images in
later passes, resuming, then `export`) is in
[Multi-pass workflow](../tutorials/local-review.md#multi-pass-workflow); what
each pass shows is in [Passes](../commands/review.md#passes).
