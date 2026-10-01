# image-review Specification

## Purpose

`image-review` is a CLI tool for reviewing medical (DICOM) and general images
for burned-in Protected Health Information (PHI). It provides a three-phase
workflow: **preprocess** raw images into normalized JPGs, **review** them
interactively in a fullscreen viewer, and report **status** on review progress.

## Requirements

- Python >= 3.12
- Dependencies: click, matplotlib, numpy, pydicom, Pillow, scikit-image,
  rectpack, tqdm, pygame-ce, cryptography
- `review --via` / `status --via` additionally need an OpenSSH client (`ssh`)
  on the client machine

## Architecture

```
cli.py              Command-line entry point, argument parsing
preprocess.py       DICOM/image loading and normalization
store.py            ReviewStore Protocol, LocalStore, pure filter/summary functions
server.py           HTTPS + bearer-token server exposing a ReviewStore
connection.py       RemoteTarget: the ir:// connection string
remote.py           RemoteStore: ReviewStore client with certificate pinning
tunnel.py           SSH local port-forward for --via
controller.py       Review session orchestration and event loop
viewer.py           Fullscreen pygame display
grid_packer.py      Review-time bin-packing of images into grids
review_db.py        Persistent review state (review.tsv)
util.py             Shared utilities (surface loading)
```

All review-time data access goes through a `ReviewStore`. The controller and
grid packer never touch the work directory; `LocalStore` serves it directly,
and `RemoteStore` talks to an `image-review serve` process that wraps a
`LocalStore`. `store.py`, `server.py`, `connection.py`, `remote.py` and
`tunnel.py` import without pygame, numpy or skimage.

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

### `image-review preprocess`

```
image-review preprocess SOURCE [SOURCE ...] [--batch-size N]
                                            [--work-dir DIR]
                                            [--colormap NAME]
                                            [--access {private,group}]
                                            [--allow-skipped]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `SOURCE` | (required) | One or more ZIP files, directories, or individual files |
| `--batch-size` | 300 | Maximum images per batch subdirectory |
| `--work-dir` | `./review_work` | Work directory for all output (alias: `--output-dir`); must not exist or be empty |
| `--colormap` | `inferno` | Matplotlib colormap applied to DICOM grayscale |
| `--access` | `private` | `private` (dirs 0700, files 0600) or `group` (dirs 2770, files 0660); env `IMAGE_REVIEW_ACCESS` |
| `--allow-skipped` | off | Exit 0 even if some inputs failed (they are still listed in `skipped.tsv`) |

The pipeline has three parts: **discovery** (IO) yields one `Candidate`
(`image_id`, `kind` = `dicom` or `raster`, and a `read()` returning the raw
bytes) per input without decoding anything (or a `Skipped` row for content
that is not an input or cannot be read); a pure **`render(kind, image_id,
data, colormap) -> list[Rendered]`** turns the bytes into `(H, W, 3)` uint8
RGB images; each image is then JPEG-encoded in memory; and a **writer** saves
the encoded bytes the moment they are produced and records them in the
manifest. For ZIP entries `read()` reads from the open archive, so it is only
valid until discovery moves on to the next item.

**Work directory lifecycle.** The run builds everything (batch directories,
JPGs, `manifest.tsv`, `skipped.tsv`) in a staging directory
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
`private`), and later writers follow it. `ReviewDB._save` `fchmod`s its temp
file (mkstemp creates 0600) to that policy's `file_mode` before `os.replace`,
so `review.tsv` is 0600 in a private and 0660 in a group work directory.
`review`, `serve` and local `status` call `world_access_warning`: if the work
directory or `manifest.tsv` has any other bit, they print
`warning: <path> is accessible to all users (mode NNNN); run `chmod -R o-rwx
<work dir>`` to stderr (group bits alone are silent). Existing directories are
never chmod'ed automatically. A team shares a work directory sequentially
(one writer at a time) or splits a study into several work directories.

**Source loading.** `discover(sources, exclude)` yields every input,
classified by content. `run_preprocess` passes the resolved work directory and
staging directory as `exclude`; nothing under them is ingested (resolved
paths compared), so a work dir inside a source (e.g. `preprocess .` with the
default `./review_work`) never re-ingests its own output.

| Source type | Behavior |
|-------------|----------|
| Directory | One `os.walk(followlinks=False)`, directory and file names sorted at each level, top-down (a directory's files before its subdirectories). Every file is classified; a ZIP file yields its entries. Symlinked files are read; a dangling symlink is `failed`. A symlinked directory is never entered (it could lead out of the source, e.g. `up -> ..` or a link to `/`); by its resolved target it is: inside `exclude` → skipped silently; the link's own directory or an ancestor of it (so the source root and its ancestors) → `ignored`, `symlink to an enclosing directory`; inside or equal to a directory given as a SOURCE in this run (including the one being walked) → `ignored`, `symlinked directory already included via SOURCE <path>`; anything else → `failed`, `symlinked directory not followed; pass its target <resolved path> as a SOURCE`. A non-regular file (FIFO, socket, device) is `ignored` (`not a regular file`). An unreadable directory (including the source itself) is one `failed` row for that directory's path |
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
`{zip}::{name}#2`, `#3`, ... (no entry name ends in `#N`, so these cannot
clash with a real entry). Manifest rows follow discovery order (sources in
the order given, each walked as above).

**Image IDs** are fully-resolved absolute paths derived from the source:
- ZIP entry: `{absolute_zip_path}::{filename}` (also for a ZIP inside a directory)
- Directory: absolute path to each file (a symlinked file keeps its link path)
- Single file: fully-resolved absolute path

**DICOM preprocessing pipeline** (`preprocess_dicom`):

1. Only single-frame `MONOCHROME1`/`MONOCHROME2` images with pixel data are
   rendered. No pixel data, any other photometric interpretation, or a pixel
   array that is not 2-D (multi-frame) raises `Unsupported` (e.g.
   `unsupported: no pixel data (Basic Text SR Storage)` naming the SOP class
   when known, `unsupported: multi-frame DICOM (3 frames)`,
   `unsupported: photometric interpretation RGB`)
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
   the min or max) a plain min-max rescale is used
5. Apply CLAHE (adaptive histogram equalization, 96-tile grid)
6. Strip uniform rows/columns (`compress_image` -- removes letterboxing). If
   stripping would leave nothing (e.g. an all-zero image), the uncropped
   image is kept
7. Apply colormap, save as 8-bit RGB JPG

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
and other control characters replaced by spaces and object reprs such as `<_io.BytesIO object at 0x...>` replaced by `<data>` (so `skipped.tsv` is reproducible); a warning is also printed to stderr. A render that
yields no image becomes a `failed` row with reason `rendered no images`. A
source that cannot be opened at all (corrupt ZIP, unreadable directory at any
depth) becomes one `failed` row for its path. Content that is not an input
(see *Source loading*) becomes an `ignored` row, without a stderr warning.
Only `failed` rows affect the exit status. Errors writing to the work
directory (JPG files, batch directories, `manifest.tsv`, `skipped.tsv`) still
abort the run; since images are encoded in memory first, an input's content
cannot cause one.

**Batching**: Each input is rendered, encoded and written as soon as discovery yields
it, so memory does not grow with batch size. The n-th written image
(0-based) goes to `batch_{n // batch_size + 1:03d}/img_{n % batch_size + 1:05d}.jpg`.

**Output**: `manifest.tsv` and `skipped.tsv` (always written, even when
empty), plus the summary line

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
                    [--filter {unreviewed,clean,all}]  [--rotate/--no-rotate]
                    [--remote CONNECTION_STRING [--via DESTINATION]]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--mode` | `single` | `single` = one image at a time; `grid` = packed grids |
| `--pass` | auto-detected | Review pass number |
| `--batch` | all | Restrict to a named batch (e.g. `batch_001`) |
| `--filter` | `unreviewed` | Which images to show: `unreviewed`, `clean`, or `all` |
| `--rotate/--no-rotate` | `--rotate` | Allow rectpack to rotate images 90° for tighter grid packing |
| `--work-dir` | `./review_work` | Work directory from preprocessing (local review) |
| `--remote` | (none) | `ir://` connection string of an `image-review serve` process; also read from `$IMAGE_REVIEW_REMOTE` |
| `--via` | (none) | SSH destination of a login node to tunnel through; requires `--remote`; also read from `$IMAGE_REVIEW_VIA` |

Opens a store (see below), initializes pygame, creates a `ReviewSession`,
runs the event loop, then shuts down pygame. pygame starts only after the
store (and tunnel) is up, so ssh password/MFA prompts keep terminal focus.

**Store selection** (`cli.open_store`, shared by `review` and `status`):

- Without `--remote`: `LocalStore(work_dir)`. `--via` without `--remote` is a
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
image-review status [--work-dir DIR | --remote CONNECTION_STRING [--via DESTINATION]]
```

Same store selection as `review`. Fetches the manifest, the current pass and
that pass's statuses from the store, then prints overall and per-batch counts
of CLEAN / DIRTY / UNREVIEWED images (pass-aware, computed by the pure
`store.summary` / `store.batch_summary`), plus the current pass number. Does
not start pygame.

### `image-review serve`

```
image-review serve [--work-dir DIR] [--bind HOST] [--port N]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--work-dir` | `./review_work` | Work directory from preprocessing; must exist |
| `--bind` | `socket.getfqdn()` | Hostname or IPv4 address to bind and advertise |
| `--port` | 0 | Port to listen on; 0 picks a free port |

Opens a `LocalStore`, calls `server.make_server`, and serves until interrupted.
Wildcard binds (`0.0.0.0`, `::`, empty) are refused, as is any address the
server ends up bound to that is unspecified, or whose connection string would
not parse (`RemoteTarget.parse` round trip). Only IPv4 hostnames/addresses are
supported.

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
ignored (e.g. under `nohup`) is left ignored. On exit the socket is closed and
the connection file removed.

## Data Files

All state lives in the work directory. `preprocess` creates it atomically
(see *Work directory lifecycle*); while a run is in progress its output is in
`.NAME.partial` beside it.

### `manifest.tsv`

Written by `preprocess`. Tab-separated, one row per image.

| Column | Description |
|--------|-------------|
| `batch` | Batch subdirectory name (e.g. `batch_001`) |
| `preprocessed_path` | Relative path to the JPG within the work directory |
| `image_id` | Unique string identifier (fully-resolved absolute path) |

`image_id` (source paths, which may carry patient identifiers) is used only
inside `LocalStore`/`ReviewDB`. Everything else identifies an image by its
`preprocessed_path`.

### `skipped.tsv`

Written by `preprocess`, always (header only when nothing was skipped).
Tab-separated, one row per input that produced no image.

| Column | Description |
|--------|-------------|
| `image_id` | Source identifier, in the same form as `manifest.tsv` (or the source path, if a whole source could not be opened) |
| `kind` | `failed` (an input that was not rendered; makes the CLI exit 1 unless `--allow-skipped`) or `ignored` (not an input: unrecognized content, AppleDouble, DICOMDIR, a ZIP without files, a symlink to an enclosing directory or to a directory inside a SOURCE, a non-regular file inside a directory) |
| `reason` | `<ExceptionClass>: <message>`, `unsupported: ...` for inputs this tool does not render, or the `ignored` reason |

It contains source paths, so it is as sensitive as `manifest.tsv`.

### `review.tsv`

Written atomically by `ReviewDB` after every mark action (temp file + `os.replace`), with the file mode of the work directory's access policy (0600 private, 0660 group).

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

## Review Store (`store.py`)

### Types

| Name | Description |
|------|-------------|
| `Status` | `Literal["CLEAN", "DIRTY", "UNREVIEWED"]` |
| `Verdict` | `Literal["CLEAN", "DIRTY"]`: what a mark may record |
| `ManifestRow` | Frozen dataclass: `key` (the `preprocessed_path`) and `batch` |
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
| `image_bytes_many(keys) -> dict[str, bytes]` | Bytes for the keys that loaded; missing or unloadable keys are omitted with a stderr warning |
| `statuses(pass_number) -> dict[str, Status]` | Pass-aware status of every manifest key |
| `mark(keys, batch, status, pass_number) -> dict[str, Status]` | Record a verdict; returns the new status of every key affected, including keys that share an `image_id` with a marked key |
| `current_pass() -> int` | Auto-detected pass number |

### `LocalStore(work_dir)`

Loads `manifest.tsv` and a `ReviewDB`. `image_bytes` reads the file via
`safe_path`; `image_bytes_many` loops over it, catching `KeyError`,
`ValueError` (path escape) and `OSError`. `mark` translates keys to
`image_id`s and calls `ReviewDB.mark_many`. `statuses` and `current_pass`
delegate to `ReviewDB.get_status` / `current_pass`.

### Pure functions

| Function | Description |
|----------|-------------|
| `filter_rows(rows, statuses, status_filter="unreviewed", batch=None)` | Filter rows by `unreviewed`, `clean` or `all` and optional batch; `ValueError` on an invalid filter |
| `summary(rows, statuses)` | Totals of CLEAN/DIRTY/UNREVIEWED/total |
| `batch_summary(rows, statuses)` | The same per batch |
| `safe_path(work_dir, relative)` | Resolve within `work_dir`; `ValueError` if it escapes |

`ReviewDB.images_by_status`, `summary` and `batch_summary` are no longer used
by the application; they remain as oracles for tests of the functions above.

## Review Session (`controller.py`)

### Initialization

`ReviewSession(store, mode, pass_number, batch, status_filter, allow_rotation)`:

1. Fetch `store.manifest()` (a list of `ManifestRow`)
2. Determine the pass: `store.current_pass()` if not specified. The store's
   auto-detection (`ReviewDB.current_pass`) is:
   - Pass 1 if any image has never been reviewed
   - Otherwise stays on max(pass_number) if it has unfinished work,
     or advances to max(pass_number) + 1
3. Fetch the **status snapshot** `store.statuses(pass)` (key -> status)
4. Auto-select batch if not specified: pick the first batch (sorted
   alphabetically) that has rows matching the status filter
5. Determine review items based on mode

### Status Snapshot

All status decisions (filtering, item status, todo counts, grid status) read a
local dict, not the store. It is refreshed from `store.statuses()` at init and
on every mode restart, and updated after each mark from the returned
`mark()` result (which includes other keys sharing an `image_id`). Marking
therefore costs one store call and no re-fetch.

### Store Failures

`StoreUnavailable` (raised by `RemoteStore` as `RemoteError`) while loading an
image, marking, or restarting a mode is treated as a lost connection, not an
unloadable image: autoplay and the pending auto-advance are cancelled, the
status snapshot is left unchanged (a failed mark is not applied), the viewer
shows "Lost connection to server - progress saved. Press q to quit." and the
reason is printed to stderr. A failed mode restart clears the item list. Any
other exception while loading a single-mode image logs a warning and skips the
image.

### Single Mode

- `filter_rows()` over the manifest and snapshot for the current
  pass/batch/filter
- Shuffle the resulting rows
- Each item is a `ManifestRow`; the image is fetched with
  `store.image_bytes(key)` and decoded with `load_surface(bytes)` on display

### Grid Mode

- Display a "Computing grids..." message while packing
- Read screen dimensions, subtract the 50px status bar height
- `filter_rows()` for the current pass/batch/filter
- Pass the rows and the store to `pack_into_grids()` with the screen dimensions
- Convert the returned `GridSpec` list into item dicts with `surface`,
  `keys`, and `batch` keys
- Shuffle the grid items, then sort by image count (largest grids first)

When a grid is marked CLEAN or DIRTY, `store.mark()` is called with all its
`keys`, and every key in the result is written into the snapshot.

### Grid Status Derivation

A grid's aggregate status is derived from the snapshot statuses of its keys:
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
| `w` key | Select display |
| `f` key | Toggle fullscreen |
| `n` key | Jump to next todo item |
| `u` key | Toggle todo-only navigation |
| `s` key | Switch to single mode |
| `m` key | Switch to grid mode (rotation allowed) |
| `M` key (shift+m) | Switch to grid mode (no rotation) |
| `h` key | Show help/splash screen |
| `q` / Escape / Button 7 | Quit |
| Window resize | Refit current image |
| Joystick added/removed | Hot-plug handling |

After marking, the viewer auto-advances to the next item after 200ms.
Navigation stops at list boundaries with an "End of list" message.

Marking, navigation, `n`, `Space`, and mode switches cancel autoplay. The display
only redraws when a dirty flag is set, to minimize CPU usage.

## Grid Packer (`grid_packer.py`)

### `GridSpec` Dataclass

| Field | Type | Description |
|-------|------|-------------|
| `surface` | `pg.Surface` | Composited grid image, ready for display |
| `keys` | `list[str]` | Keys (preprocessed paths) of all images packed into this grid |
| `batch` | `str` | Batch of the first packed image |

### `pack_into_grids(items, store, grid_w, grid_h, *, allow_rotation=True) -> list[GridSpec]`

`items` is a list of `ManifestRow`.

1. **Load**: Fetch all image bytes with `store.image_bytes_many()` (an
   8-worker pool for `RemoteStore`, a simple loop for `LocalStore`), decode each
   with `util.load_surface(bytes)` and read dimensions from
   `surface.get_size()`. This avoids fetching each image twice. Images that are
   missing or fail to decode are skipped with a stderr warning.
2. **Pack**: Create a `rectpack` packer with `rotation=allow_rotation` and
   `(grid_w, grid_h)` bins (unlimited bin count). Add each image as a rect.
3. **Composite**: For each bin, create a black `pg.Surface(grid_w, grid_h)`.
   Blit each pre-loaded surface at the packed position. If rectpack rotated
   the rect (packed size differs from original), apply
   `pg.transform.rotate(-90)` before blitting.
4. **Overflow**: Any image too large to fit in any bin becomes a single-image
   `GridSpec` with its original surface.

`util.load_surface(buf: bytes) -> pg.Surface` decodes JPG bytes with
scikit-image (grayscale and RGBA are converted to RGB).

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
| `show_splash(lines, footer)` | Render centered splash/help overlay |
| `show_message(text)` | Render centered text message (e.g. loading indicator) |

## Review Database (`review_db.py`)

### `ReviewDB`

In-memory dict keyed by `image_id`, backed by `review.tsv` on disk.

Used only through `LocalStore`, which is the one place that maps keys to
`image_id`s.

**Persistence**: Every mutation (`mark`, `mark_many`) writes the full state
atomically via `tempfile.mkstemp` + `os.replace`. Safe to kill the process
at any point.

### Key Methods

| Method | Description |
|--------|-------------|
| `mark(image_id, batch, status, pass_number)` | Record a single review decision |
| `mark_many(image_ids, batch, status, pass_number)` | Record decisions for multiple images (same timestamp) |
| `get_status(image_id, current_pass) -> str` | Returns pass-aware status or `"UNREVIEWED"` if absent |
| `images_by_status(manifest, pass_number, status_filter?, batch?) -> list[dict]` | Filter manifest dicts by status (test oracle; the application uses `store.filter_rows`) |
| `current_pass(manifest) -> int` | Auto-detect pass number |
| `summary(manifest, pass_number) -> dict` | Pass-aware count of CLEAN/DIRTY/UNREVIEWED/total (test oracle; the application uses `store.summary`) |
| `batch_summary(manifest, pass_number) -> dict` | Pass-aware per-batch status counts (test oracle; the application uses `store.batch_summary`) |

### Pass Logic

| Pass | Shows |
|------|-------|
| 1 | All UNREVIEWED images |
| N > 1 | Images marked DIRTY in a prior pass (treated as UNREVIEWED for the current pass) plus any still-UNREVIEWED images |

`current_pass` returns 1 if any image has never been reviewed. Otherwise it
returns `max(pass_number)` if that pass still has UNREVIEWED work remaining,
or `max(pass_number) + 1` if the pass is fully complete.

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

`make_server(store, host, port) -> (ReviewServer, RemoteTarget)` generates a
token, a certificate, the TLS context and the listening server.
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
the boundary (`parse_pass`, `parse_mark`).

| Request | Response |
|---------|----------|
| `GET /manifest` | `[{"key": str, "batch": str}, ...]` |
| `GET /image?key=K` | `image/jpeg` bytes; 404 if the key is unknown or unreadable |
| `GET /statuses?pass=N` | `{key: "CLEAN"\|"DIRTY"\|"UNREVIEWED", ...}` for every key; `N` integer >= 1 |
| `GET /current_pass` | `{"pass": N}` |
| `POST /mark` | Body `{"keys": [str, ...], "batch": str, "status": "CLEAN"\|"DIRTY", "pass": N}`; responds `{key: status, ...}` for every key affected (as `ReviewStore.mark`) |

Only keys appear on the wire; original `image_id`s never do.

### Error semantics

| Status | Cause |
|--------|-------|
| 400 | Bad request: `Transfer-Encoding` present; a body on anything but `POST /mark`; `/image` without exactly one `key`; invalid `pass`; `/mark` with a missing, repeated or oversized (> 1 MiB) `Content-Length`, invalid JSON, non-object body, empty or non-string `keys`, an unknown key or batch, a status other than CLEAN/DIRTY |
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

The server assumes one reviewer and is the only writer of `review.tsv`:
running `serve` and a local `review` (or two servers) on the same work
directory can lose marks, since each process holds its own in-memory copy of
the review state.

### Locking

Manifest, statuses, current-pass and mark calls run under a single store lock
(`ReviewDB` is not thread-safe). `/image` does not take it (read-only file
access), and the lock is never held while writing to the network.

### Logging policy

stderr only, one line per request: `METHOD path status`. The query string is
dropped, control characters are escaped, and stdlib `log_message` output
(which can echo request lines) is suppressed. Tokens, keys in queries and
exception messages are not logged. Two other lines exist: `connection error:
<ExceptionClass>` for failed handshakes and other connection-level failures,
and `internal error: <ExceptionClass>` for 500s.

## Remote Store (`remote.py`)

`RemoteStore(target, connect_host=None, connect_port=None)` implements
`ReviewStore` over HTTPS. `connect_host`/`connect_port` override the address
(used to connect to the local end of an ssh tunnel) while the pin and token
still come from `target`. It is a context manager; `close()` shuts down the
pool and closes every connection. Images are returned as bytes and never
cached on disk.

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
Any other `OSError` or `HTTPException` raises `RemoteError` immediately.

**Error mapping**: `RemoteError(StoreUnavailable)` carries `status`, the HTTP
status when the server answered, else `None`. A non-200 reply raises
`RemoteError` with that status, except `image_bytes`, where 404 raises
`KeyError(key)` (an unloadable image, not an outage). Responses are parsed
strictly (`parse_manifest`, `parse_statuses`, `parse_pass`); malformed
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
2. **Pass 2 (single review)**: `--mode single`. Only DIRTY images from
   pass 1 are shown. Inspect individually.
3. **Pass 3+**: Repeat single-mode review on the shrinking DIRTY pool
   until confident.

Sessions are resumable: quitting mid-session saves all progress (with a
remote store, progress is saved on the server at every mark). Re-running
the same command shows only remaining unreviewed (pass 1) or dirty (pass 2+)
images.