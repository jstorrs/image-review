# image-review Specification

## Purpose

`image-review` is a CLI tool for reviewing medical (DICOM) and general images
for burned-in Protected Health Information (PHI). It provides a three-phase
workflow: **preprocess** raw images into normalized JPGs, **review** them
interactively in a fullscreen viewer, and report **status** on review progress.

## Requirements

- Python >= 3.12
- Dependencies: click, matplotlib, numpy, pydicom, Pillow, scikit-image,
  rectpack, tqdm, pygame-ce, cryptography, and the DICOM codecs python-gdcm
  (JPEG baseline/extended/lossless, JPEG-LS, JPEG 2000, RLE) and pylibjpeg +
  pylibjpeg-openjpeg (JPEG 2000, HTJ2K). `pylibjpeg-libjpeg` is deliberately
  not used (GPL-3), so 12-bit JPEG Extended (Process 4) and JPEG-LS with 6- or
  7-bit samples cannot be decoded
- `review --via` / `status --via` additionally need an OpenSSH client (`ssh`)
  on the client machine

## Architecture

```
cli.py              Command-line entry point, argument parsing
preprocess.py       DICOM/image loading and normalization
store.py            ReviewStore Protocol, LocalStore, pure filter/summary functions
server.py           HTTPS + bearer-token server exposing a ReviewStore
connection.py       RemoteTarget: the ir:// connection string; API_VERSION, package_version, parse_reviewer
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
| `--batch-size` | 300 | Maximum images per batch subdirectory (integer >= 1) |
| `--work-dir` | `./review_work` | Work directory for all output (alias: `--output-dir`); must not exist or be empty |
| `--colormap` | `inferno` | Matplotlib colormap applied to DICOM grayscale (unknown names exit 2 before any output) |
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
`private`), and later writers follow it. `ReviewDB` opens `review.tsv` with
`os.open(O_APPEND | O_CREAT, file_mode)` and, when the file is new or empty and
its mode differs from that policy's `file_mode` (the umask may have narrowed
it), `fchmod`s it, so a new `review.tsv` is 0600 in a private and 0660 in a
group work directory. An existing, non-empty file keeps whatever mode it has,
except that migrating an old-header file (see *`review.tsv`*) writes the new
file with the policy's `file_mode`.
`review`, `serve` and local `status` call `world_access_warning`: if the work
directory or `manifest.tsv` has any other bit, they print
`warning: <path> is accessible to all users (mode NNNN); run `chmod -R o-rwx
<work dir>`` to stderr (group bits alone are silent). Existing directories are
never chmod'ed automatically. A team shares a work directory sequentially
(one writer at a time, enforced by `review.lock`; see *Concurrency limits*) or
splits a study into several work directories.

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

A DICOM whose `IconImageSequence` (0088,0200) has an item also yields a second
output row, `{image_id}#icon`, for the embedded thumbnail (item 0, rendered
like a single-frame DICOM image of its own: grayscale through the colormap,
colour as is). The main image keeps its `image_id`. If the icon cannot be
rendered the main image is still written and `{image_id}#icon` is a `failed`
row in `skipped.tsv`. If the main image fails the file is `failed` and has no
icon row.

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
                    [--reviewer NAME]
                    [--remote CONNECTION_STRING [--via DESTINATION]]
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--mode` | `single` | `single` = one image at a time; `grid` = packed grids |
| `--pass` | auto-detected | Review pass number (integer >= 1) |
| `--batch` | first batch with images matching the filter | Restrict to a named batch (e.g. `batch_001`); an empty or unknown name exits 2 listing up to 5 known batches |
| `--filter` | `unreviewed` | Which images to show: `unreviewed` (images still to do: UNREVIEWED and FLAGGED), `clean`, or `all` |
| `--rotate/--no-rotate` | `--rotate` | Allow rectpack to rotate images 90° for tighter grid packing |
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
image-review status [--work-dir DIR | --remote CONNECTION_STRING [--via DESTINATION]]
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
ignored (e.g. under `nohup`) is left ignored. On exit the socket is closed,
the store closed (releasing the work dir lock) and the connection file removed.

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
| `image_id` | Unique string identifier (fully-resolved absolute path; `{path}#icon` for a DICOM's embedded icon image) |

`image_id` (source paths, which may carry patient identifiers) is used only
inside `LocalStore`/`ReviewDB`. Everything else identifies an image by its
`preprocessed_path`.

The file is parsed strictly when the work directory is opened: the header must
be exactly the three columns, every row must have three non-empty fields, and
`preprocessed_path` must be unique (the same `image_id` may repeat). Any
violation stops `review`, `status`, and `serve` with `Cannot read work
directory: <file>:<line>: <problem>` (exit 1); the file is never repaired.

### `skipped.tsv`

Written by `preprocess`, always (header only when nothing was skipped).
Tab-separated, one row per input that produced no image.

| Column | Description |
|--------|-------------|
| `image_id` | Source identifier, in the same form as `manifest.tsv` (`{path}#icon` for an icon image that failed to render; or the source path, if a whole source could not be opened) |
| `kind` | `failed` (an input that was not rendered; makes the CLI exit 1 unless `--allow-skipped`) or `ignored` (not an input: unrecognized content, AppleDouble, DICOMDIR, a ZIP without files, a symlink to an enclosing directory or to a directory inside a SOURCE, a non-regular file inside a directory) |
| `reason` | `<ExceptionClass>: <message>`, `unsupported: ...` for inputs this tool does not render, or the `ignored` reason |

It contains source paths, so it is as sensitive as `manifest.tsv`.

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
| `mode` | `single` or `grid` (`review_db.MarkMode`): the display mode the verdict was given in; `undo` for a row written by an undo (`review_db.RowMode`). Empty in migrated rows |
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
(`\r` or `\n`; a torn append) is ignored with a `WARNING:` on stderr, and the
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

| Name | Description |
|------|-------------|
| `Status` | `Literal["CLEAN", "DIRTY", "UNREVIEWED", "FLAGGED"]`: an image's status in a given pass (see *Pass Logic*) |
| `Verdict` | `Literal["CLEAN", "DIRTY"]`: what a mark may record |
| `TODO_STATUSES` | `frozenset({"UNREVIEWED", "FLAGGED"})`: statuses that still need a verdict in the current pass |
| `StatusFilter` | `Literal["unreviewed", "clean", "all"]`: the `review --filter` vocabulary, parsed by the CLI's choice and taken by `filter_rows` and `ReviewSession` |
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
| `mark(keys, status, pass_number, *, reviewer, mode) -> dict[str, Status]` | Record a verdict given by `reviewer` (an unauthenticated claim) in `mode` (`single` or `grid`); returns the new status of every key affected, including keys that share an `image_id` with a marked key |
| `undo(pass_number, *, reviewer) -> dict[str, Status]` | Undo the latest mark not yet undone (one `mark` call: one image or a whole grid), restoring each of its images' decision from before it (see *Undo rows*); `reviewer` is checked and recorded like `mark`'s. Returns the new status, at `pass_number`, of every key affected, as `mark` does; `{}` when there is nothing to undo |
| `current_pass() -> int` | Auto-detected pass number |
| `close()`, `__enter__`, `__exit__` | Release what the store holds (`RemoteStore`: pool and connections; `LocalStore`: the work dir lock); idempotent. Stores are context managers that close on exit |
| `skipped() -> SkippedCounts \| None` | Counts of `failed` and `ignored` rows in preprocess's `skipped.tsv` (frozen `SkippedCounts(failed, ignored)`); `None` if the file is absent; `ValueError` naming `file:line` if it is malformed |

### `LocalStore(work_dir, read_only=False)`

Loads `manifest.tsv` (written once by `preprocess`, so before locking); a
writable store then takes the work dir lock (see *Concurrency limits*) and
loads a `ReviewDB` and calls its `migrate()` (see *`review.tsv`*), releasing the lock if either fails. `close()` releases it. A `read_only` store takes no lock and its
`mark` and `undo` raise `PermissionError` (as they do after `close()`). `image_bytes` reads the file via
`safe_path`; `image_bytes_many` loops over it, catching `KeyError`,
`ValueError` (path escape) and `OSError`. `mark` translates each key to its
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
`skipped.tsv` strictly (header `image_id`, `kind`, `reason`; `kind` is `failed`
or `ignored`) and returns only the counts.

### Pure functions

| Function | Description |
|----------|-------------|
| `filter_rows(rows, statuses, status_filter="unreviewed", batch=None)` | Filter rows by status and optional batch: `unreviewed` selects `TODO_STATUSES` (UNREVIEWED and FLAGGED), `clean` selects CLEAN, `all` everything. `status_filter` is a `StatusFilter` (`Literal["unreviewed", "clean", "all"]`), parsed once by the CLI's `--filter` choice |
| `summary(rows, statuses)` | Count of each `Status` (CLEAN/DIRTY/UNREVIEWED/FLAGGED) plus `total` |
| `batch_summary(rows, statuses)` | The same per batch |
| `safe_path(work_dir, relative)` | Resolve within `work_dir`; `ValueError` if it escapes |

## Review Session (`controller.py`)

### Initialization

`ReviewSession(store, reviewer, mode, pass_number, batch, status_filter, allow_rotation)`:
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

Every item is a `ReviewItem`, a frozen dataclass: `keys` (a tuple of manifest
keys), `label` (shown in the status bar: the key, or
"grid (N images)"), `surface` (a grid's composited surface, or `None` for a
single image, loaded when displayed) and `grid` (a grid's status and CLEAN
refusal follow the grid rules below, even for a one-image overflow grid; an
unloadable image's item in grid mode is a single image, not a grid).

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
reason is printed to stderr. The session is then `DISCONNECTED`: only `q`/Esc
and closing the window do anything (no navigation, mode switch or `z` reaches
the store again). A failed mode restart clears the item list. Any
other failure to load an image is an unloadable image (see *Unloadable Images*).

### Single Mode

- `filter_rows()` over the manifest and snapshot for the current
  pass/batch/filter
- Shuffle the resulting rows
- Each row becomes a one-key `ReviewItem` with no surface; the image is fetched with
  `store.image_bytes(key)` and decoded with `load_surface(bytes)` on display. An
  image that cannot be fetched or decoded is shown as a placeholder in its place
  (see *Unloadable Images*); the cursor stays on it

### Grid Mode

- Display a "Computing grids..." message while packing
- Read screen dimensions, subtract the 50px status bar height
- `filter_rows()` for the current pass/batch/filter, then keep only rows whose
  status is in `controller.GRID_ELIGIBLE` (UNREVIEWED or CLEAN), in every
  filter including `all`. A grid mark applies to all its images, so one
  keypress must never clear an image already judged DIRTY (this pass) or
  FLAGGED (DIRTY in another pass); those are reviewed in single mode
- Pass the rows and the store to `pack_into_grids()` with the screen dimensions
- Convert each returned `GridSpec` into a `ReviewItem` with its `surface`
  and `keys`
- Shuffle the grid items, then sort by image count (largest grids first)
- Append one item per unloadable key `pack_into_grids()` reported:
  `ReviewItem(keys=(key,), label=key, surface=None, grid=False)`. Like a
  single-mode item it is loaded (retried) when shown, so its placeholder is
  drawn only then, and it follows the single-image status rules, not the grid
  rules. No grid ever holds an unloadable key

When a grid is marked CLEAN or DIRTY, `store.mark()` is called with all its
`keys`, and every key in the result is written into the snapshot.

A grid can still come to hold a DIRTY key mid-session, when a key in it shares
an `image_id` with an image marked DIRTY elsewhere. CLEAN on a grid holding any
DIRTY or FLAGGED key is refused (no store call; the status bar shows "grid
contains an image already marked DIRTY - review it in single mode", also
printed to stderr), unless every key in the grid is DIRTY, which reverses that
grid's own verdict.

If grid mode has no items but the status filter selected rows it left out, the
session says so instead of implying the review is done: `run()` prints, and a
mode restart shows, "No grid items for pass N; K FLAGGED/DIRTY image(s) need(s)
single-mode review (--mode single)" (K counted over the selected batch, or all
batches when none is selected).

### Unloadable Images

An image whose bytes are missing (`KeyError`, e.g. a 404 from the server) or
cannot be read or decoded is not an outage (see *Store Failures*). A warning
naming the key is printed to stderr, the key is added to the session's
`_unloadable` set, and the item is shown as a placeholder from
`viewer.placeholder_surface`: a dark surface reading `Cannot load image: <key>`
with a short reason ("image could not be fetched" or "image could not be read
or decoded"; the error itself goes only to stderr). Navigation, `n`, todo-only and autoplay treat it
like any other item (autoplay does not stop at it), so the cursor always points
at a real item. An item with no surface (every single-mode item, and an
unloadable image's item in grid mode) is loaded each time it is shown, so the
key joins `_unloadable` before any verdict can count, and a key that loads
again leaves it. Placeholders are never built up front.

A placeholder can be marked DIRTY but never CLEAN: CLEAN on an item holding
any unloadable key is refused (no store call; the status bar shows "cannot mark
CLEAN: image could not be loaded", also printed to stderr). DIRTY is recorded
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
its keys is not in the session's marked set. A successful mark adds the item's keys; a successful
undo removes the keys it restored. The set is in memory only. Any unmarked key keeps a grid todo,
so a partly undone grid comes back.

### Event Loop

The session runs a pygame event loop processing:

| Event | Action |
|-------|--------|
| `c` key / Button 1 | Mark current item CLEAN |
| `d` key / Button 3 | Mark current item DIRTY |
| `z` key | Undo this session's latest mark since the mode started (see *Undo*); also on the end-of-list screen |
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

Marking, Left / hat left, `n`, `Space` (while playing), mode switches, the help
screen (`h`) and display select (`w`) cancel autoplay; Right / hat right keeps
it running. Left/Right, the hat, `n`, mode switches, `h` and `w` cancel a
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
"Lost connection to server ...") are painted once by the viewer and stay until
the state changes. When there are no items (an empty mode), the navigation keys
leave the message up; only `q`/Esc, `s`, `m`, `M` and `z` act. On the lost
connection screen only `q`/Esc act (see *Store Failures*).

**Verdicts need a seen item.** A verdict (`c`/`d`, Button 1/3) applies only to
an item that has been painted and on screen for `MIN_DWELL_MS` (200 ms);
otherwise it is ignored (it still stops autoplay). The session records the tick
of the first paint of the current item in `refresh_if_needed`, compares it
with the clock read once at the start of the event batch (so slow events earlier
in the batch, like a resize, do not count toward the dwell), and clears it
whenever the current item or screen changes (a new item, a mode restart, the
help or display-select screen); repainting the same item (resize, a mark) keeps
it. So a verdict queued in the same event batch as a mode switch, an autoplay
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

## Grid Packer (`grid_packer.py`)

### `GridSpec` Dataclass

| Field | Type | Description |
|-------|------|-------------|
| `surface` | `pg.Surface` | Composited grid image, ready for display |
| `keys` | `list[str]` | Keys (preprocessed paths) of all images packed into this grid |

### `pack_into_grids(items, store, grid_w, grid_h, *, allow_rotation=True) -> tuple[list[GridSpec], dict[str, str]]`

`items` is a list of `ManifestRow`. Returns the grids and the unloadable
keys, in input order; no grid holds an unloadable key. What to do with
unloadable keys is left to the caller.

1. **Load**: Fetch all image bytes with `store.image_bytes_many()` (an
   8-worker pool for `RemoteStore`, a simple loop for `LocalStore`), decode each
   with `util.load_surface(bytes)` and read dimensions from
   `surface.get_size()`. This avoids fetching each image twice. Images that are
   missing (the store warns) or fail to decode (warned here) are left out of
   the packing and reported as unloadable.
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
Color encodes review status (green=CLEAN, red=DIRTY, gray=UNREVIEWED,
orange=FLAGGED).
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

The module-level `placeholder_surface(text) -> pg.Surface` renders `text`
(one line per newline) centred on a dark 1280x720 surface (wider or taller if
the text needs it), in the status bar font, for an image that could not be
loaded. The font file is read once and cached.

## Review Database (`review_db.py`)

### `ReviewDB`

In-memory dict keyed by `image_id`, backed by `review.tsv` on disk.

Used only through `LocalStore`, which is the one place that maps keys to
`image_id`s.

**Persistence**: Every mutation (`mark`, `mark_many`, `undo_many`) appends its rows to
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
| `mark(image_id, batch, status, pass_number, *, reviewer, mode)` | Record a single review decision |
| `mark_many(targets, status, pass_number, *, reviewer, mode) -> list[Change]` | Record one verdict on every `(image_id, batch)` in `targets` (same timestamp; `grid_size` = `len(targets)`; `tool_version` = `package_version()`); returns each row written with the decision it replaced |
| `undo_many(changes, *, reviewer)` | Append the undo rows for one `mark_many` result in one append (see *Undo rows*); an `image_id` listed twice (keys sharing it) gets one row |
| `migrate()` | Rewrite an old five-column file with the current header, once (see *`review.tsv`*); no-op otherwise |
| `get_status(image_id, current_pass) -> Status` | Pass-aware status (see *Pass Logic*) |
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
the boundary (`parse_pass`, `parse_mark`, `parse_undo`).

| Request | Response |
|---------|----------|
| `GET /version` | `{"api": N, "version": str}`: the wire API version (`connection.API_VERSION`) and the installed `image-review` package version (`"unknown"` if not installed) |
| `GET /manifest` | `[{"key": str, "batch": str}, ...]` |
| `GET /image?key=K` | `image/jpeg` bytes; 404 if the key is unknown or unreadable |
| `GET /statuses?pass=N` | `{key: "CLEAN"\|"DIRTY"\|"UNREVIEWED"\|"FLAGGED", ...}` for every key; `N` integer >= 1 |
| `GET /current_pass` | `{"pass": N}` |
| `GET /skipped` | `{"failed": N, "ignored": M}` (counts of the `kind` column of the work dir's `skipped.tsv`), or `null` if the work dir has no `skipped.tsv`. Only counts are sent, never `image_id`s or reasons (which contain source paths) |
| `POST /mark` | Body `{"keys": [str, ...], "status": "CLEAN"\|"DIRTY", "pass": N, "reviewer": str, "mode": "single"\|"grid"}`; `reviewer` is checked with `connection.parse_reviewer` (1-64 printable characters, no tab, newline or other control character, not all whitespace) and recorded as the client's unauthenticated claim; other fields are ignored; responds `{key: status, ...}` for every key affected (as `ReviewStore.mark`) |
| `POST /undo` | Body `{"pass": N, "reviewer": str}`, checked as for `/mark`; other fields are ignored. Undoes the server store's latest mark (`ReviewStore.undo`); responds `{key: status, ...}` for every key affected, or `{}` when there is nothing to undo |

Only keys and skip counts appear on the wire; original `image_id`s never do.

**API version rule.** `connection.API_VERSION` (an integer, currently 5; v2 added `GET /skipped`;
v3 added `FLAGGED` to the `Status` vocabulary, which `/statuses` responses
may contain; `/mark` responses hold only the verdict just recorded; v4 dropped
`batch` from the `/mark` body, the server taking each key's batch from the
manifest, and added `reviewer` and `mode`; v5 added `POST /undo`) is
shared by client and server. Any change to request or response shapes, or to
the `Status` vocabulary, must bump it. Client and server are installed
separately, so skew is expected and must fail clearly rather than as a
malformed reply or a 404.

### Error semantics

| Status | Cause |
|--------|-------|
| 400 | Bad request: `Transfer-Encoding` present; a body on anything but `POST /mark` and `POST /undo`; `/image` without exactly one `key`; invalid `pass`; `/mark` or `/undo` with a missing, repeated or oversized (> 1 MiB) `Content-Length`, invalid JSON, non-object body, a missing or invalid `pass` or `reviewer`; `/mark` with empty or non-string `keys`, an unknown key, a status other than CLEAN/DIRTY, a `mode` other than single/grid |
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
  uid if there is none), pid, and UTC ISO start time. The full record is written
  to a unique sibling `review.lock.<host>.<boot_id or ->.<pid>.<random>`
  (created `O_EXCL`, `fchmod`ed to the work dir policy's file mode: 0600
  private, 0660 group, so teammates can read who holds it; then `fsync`ed),
  which is hard-linked to `review.lock` and then removed. A lock is therefore
  never seen empty or half-written. If `link` reports an error but the
  sibling's link count is 2 (a lost NFS reply), the lock was acquired. Where
  hard links are unsupported (`link` fails with `EPERM`, `ENOTSUP`/`EOPNOTSUPP`
  or `ENOSYS`: vfat/exFAT, SMB, many FUSE mounts), `review.lock` is created
  directly with `O_CREAT | O_EXCL` and the record written and `fsync`ed; a
  reader that finds the lock empty re-reads it for up to 1 s before treating it
  as corrupt. A process killed mid-acquire can leave a sibling behind; each
  acquire removes, best effort, siblings named with this host and `boot_id`
  whose pid no longer exists. Others are harmless and can be deleted by hand.
  `flock` is not used: it is unreliable on Lustre/GPFS/NFS.
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

**Startup check.** `RemoteStore.check_api()` GETs `/version` (parsed by
`parse_version`; a malformed reply raises `RemoteError`) and raises
`ApiMismatch` (a `RemoteError`) if the server answers 404 ("server is too old
to report its API version") or reports a different `api` ("server speaks API
vS, this client vC"); both messages end "install the same image-review version
on both machines". `cli.open_store` calls it after entering the store and
before yielding, and turns `ApiMismatch` into a `ClickException` (exit 1).

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
A retried `/mark` whose first attempt was applied (its reply lost) is applied
twice; the store's undo stack then holds two entries for it, so the first `z`
restores the identical verdict and nothing visibly changes. `POST /undo` is not
idempotent, so it is never retried: it is sent once on a fresh connection (the
thread's connection is closed first, so the request reconnects and re-checks
the pin), and any transport error raises `RemoteError`.
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