# image-review Specification

## Purpose

`image-review` is a CLI tool for reviewing medical (DICOM) and general images
for burned-in Protected Health Information (PHI). It provides a four-step
workflow: **preprocess** raw images into normalized JPGs, **review** them
interactively in a fullscreen viewer, report **status** on review progress,
and **export** the allowlist of files that may be released.

## Requirements

- Python >= 3.12
- Every command but `review` needs only the core dependencies; `review` needs
  the `viewer` extra. `preprocess` and `codecs` are empty extras kept for
  existing install commands, and `all` equals `viewer`. The extras, what each
  command needs and every minimum version are in [Installation](../install.md#choosing-extras) and
  [Minimum dependency versions](../install.md#minimum-dependency-versions);
  `pyproject.toml` declares them. The `dev` and `docs` extras are described in
  [CONTRIBUTING.md](https://github.com/jstorrs/image-review/blob/main/CONTRIBUTING.md).
  The reasons for the minimums are on the installation page. The
  pixel-data codecs are core dependencies; if they are missing, any compressed DICOM pydicom
  cannot decode by itself or through Pillow is `failed` with `cannot decode
  <transfer syntax name>: ...` (see the DICOM preprocessing pipeline, step
  0). `pylibjpeg-libjpeg` is deliberately not used (GPL-3).

- The CLI imports heavy modules only in the command that needs them, so
  `--help`, `serve`, `status` and `export` start without them. `review`
  without the viewer extra exits 1 with
  `this command needs the viewer extra: pip install 'image-review[viewer]'`
  (`cli.requires_extra`). Only a `ModuleNotFoundError` naming one of that
  extra's own top-level modules is translated; any other import failure
  surfaces unchanged (one of image-review's modules, a missing core
  dependency, a broken install)

## Architecture

```
cli.py              Command-line entry point, argument parsing
preprocess.py       DICOM/image loading and normalization
status.py           Status, Verdict, MarkMode, Rotation, TODO_STATUSES vocabulary; GRID_ELIGIBLE, grid_status, grid_clean_refused (stdlib only)
store.py            ReviewStore Protocol, LocalStore, pure filter/summary functions
lock.py             The work directory's review.lock: acquire, release, live_writer (stdlib only)
export.py           Export rows, the allowlist split and their TSV formats: export_rows, split_allowlist, format_allowlist, format_report (stdlib only)
atomic.py           write_new_file: create a file atomically, never overwriting (stdlib only)
server.py           bearer-token server exposing a ReviewStore over plain HTTP on a Unix socket (browser review)
connection.py       Shared limits and parsers: MAX_PASS, parse_reviewer, parse_token, is_int_at_least, package_version
tunnel.py           parse_via, parse_ssh_host: the parts of the ssh command serve prints
signals.py          interrupt_on / interrupt_once: signals to KeyboardInterrupt, shared by preprocess, review, export and serve
controller.py       Review session orchestration and event loop
viewer.py           Fullscreen pygame display
layout.py           Pure grid layout: jpeg_size, fit_size, plan_grids (stdlib and rectpack only)
grid_packer.py      Review-time compositing of packed grids (pygame)
review_db.py        Persistent review state (review.tsv)
util.py             Shared utilities (surface loading)
```

All review-time data access goes through a `ReviewStore`. The controller and
grid packer never touch the work directory; `LocalStore` serves it to a local
`review`, and `image-review serve` wraps a `LocalStore` for the browser page.
`status.py`, `store.py`, `lock.py`, `export.py`, `atomic.py`, `server.py`,
`connection.py`, `tunnel.py`, `signals.py` and `layout.py` import without pygame,
PIL, numpy or skimage.

```
review (pygame, local)         serve (compute node)             browser (laptop)
  ReviewSession                  UnixReviewServer  <--ssh -L---  web/ page
    |                              |
  LocalStore                     LocalStore --> ReviewDB --> review.tsv
                                   +--> manifest.tsv, batch_NNN/*.jpg
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
data, colormap, *, draw_overlays) -> Rendered`** turns the bytes into one `(H, W, 3)` uint8 RGB
image, with the input's notes (see *Notes*); the image is then JPEG-encoded in memory; and a **writer** saves
the encoded bytes the moment they are produced and, once collisions are
known (see *Collisions*), records them in the manifest and their notes in `notes.tsv`. Each input's bytes are read
once; their SHA-256 becomes the `source_sha256` of the image rendered from
them, and the SHA-256 of each JPG's encoded
bytes its `jpeg_sha256` (see *`manifest.tsv`*).

**Parallel rendering** (`run_preprocess(..., jobs)`, CLI `--jobs`). Hashing,
rendering and encoding one input is the pure, top-level
`render_and_encode(candidate, colormap, *, draw_overlays) -> Encoded | SkippedRow`.
With `jobs` = 1 it runs in the main process and no pool exists. With `jobs` >
1 the main process submits each candidate's bytes (read during discovery) to a `ProcessPoolExecutor(jobs)` using
the `spawn` start method on every platform (workers import the package afresh:
no inherited threads, signal handlers or open archives); workers hash the
bytes and return JPEG bytes (with their notes) or `SkippedRow`s. While the pool exists,
`OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS` and `MKL_NUM_THREADS` are set to 1 in
the main process's environment (only those the user has not set; restored
afterwards), so each worker inherits single-threaded BLAS instead of a thread
per core. At most `2 × jobs` inputs are submitted and not yet consumed: the
main process holds their raw bytes, each worker one input and its decoded
arrays. Results are consumed in input
order, so the main process writes every file and row in the same order as
with `jobs` = 1 and the output (`manifest.tsv`, `skipped.tsv`, `notes.tsv`, the JPGs) is byte-identical (only `preprocess.json`'s
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
JPGs, `manifest.tsv`, `skipped.tsv`, `notes.tsv`, `preprocess.json`) in a staging directory
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

The work directory is synced before success is reported, in this order: each
batch directory once its JPGs are renamed in (`fsync_dir`), each of
`manifest.tsv`, `skipped.tsv`, `notes.tsv` and `preprocess.json` as it is
written (`os.fsync` of the file), then the staging directory, then the rename,
then the parent of the work directory (`sync_parent`). A failure before the
rename (an `OSError` from any of these syncs) fails the run through the
cleanup above, leaving no work or staging directory. The parent sync after the
rename is best effort: the work directory is complete by then, and failing
would leave one the next run refuses, so a failure is logged as a warning. The
JPGs' contents are not fsynced: a JPG lost in a power cut fails its hash check
and is visible, and a sync per JPG is slow on cluster filesystems.

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
| ZIP (by content, wherever found) | Every non-directory entry from `infolist()`, classified by its first bytes. A ZIP entry → `failed`, `unsupported: nested zip`. An entry that cannot be read (encrypted, an unknown compression method) → `failed`; so is one over the size limit, an encrypted one (`unsupported: encrypted ZIP entry (decrypt the archive first)`, checked before the method) or one compressed by a method other than STORED, DEFLATED, BZIP2 and LZMA (`unsupported: ZIP compression method <number>`, see *Size limits*). An archive that cannot be opened → one `failed` row for the archive path. So is an archive with bytes in `[0, start_dir)` that no entry covers (two ZIPs joined with `cat`, a self-extracting or damaged archive): one `failed` row for the archive path, `unsupported: ZIP holds N bytes outside its listed entries (concatenated, self-extracting or damaged archive)`, and no entry rows. An entry covers its local header (name and extra lengths read from it, not from the central directory), its data, and, with general-purpose flag bit 3, up to 24 bytes of data descriptor; a local header that cannot be read or lacks `PK\x03\x04` fails the archive too, as does a `zipfile` without `start_dir`. An archive with no file entries (empty, or directories only) → one `ignored` row for the archive path, `zip contains no files`. Office Open XML, ODF and EPUB files are ZIPs: their embedded images are reviewed, their XML parts are `ignored`, and embedded workbooks or packages are nested ZIPs (`failed`) |
| Size limits (file or ZIP entry) | A file whose `st_size`, or a ZIP entry whose `file_size` in the directory, is over `MAX_INPUT_BYTES` (1 GiB) → `failed`, `unsupported: input too large (N bytes; limit L)`, without being read. A ZIP entry is checked before it is opened, even to sniff it, so one that large fails even if it is not an image. A file is read from one descriptor, no further than `MAX_INPUT_BYTES + 1` bytes: one that has grown past the limit since `stat` fails as `input too large (more than L bytes; limit L)`. A ZIP entry compressed by a method other than STORED, DEFLATED, BZIP2 (12) and LZMA (14) → `failed`, `unsupported: ZIP compression method <number>`. Every entry is decoded in pieces of at most 64 KiB (`_zip_entry_pieces`). STORED and DEFLATED are read through `zipfile`, each read asking for no more than is left, so no more than about 4 KiB past the declared size is decoded; `ZipFile.read` would let a single call inflate up to 1 GiB (2 GiB on Python 3.14). BZIP2 and LZMA are decoded by preprocess itself from the entry's raw bytes (after `zipfile` has opened the entry, checking its local header), with `bz2.BZ2Decompressor` or a raw `lzma.LZMADecompressor` set up from the entry's 9-byte LZMA header, each call given a `max_length`, so decoding stops exactly at the declared size. The LZMA dictionary, which liblzma allocates whole up front, is cut to the bytes being decoded (at least 4 KiB), since no match reaches further back than that. `zipfile` itself bounds them only from Python 3.12.15, 3.13.16 and 3.14.8 (gh-156002): before, one read decompressed a whole chunk with no `max_length`, so a 785-byte bzip2 entry could make 1 GiB even while sniffing. preprocess does not rely on that fix, so the bound is the same on every patch level. An LZMA stream ends at its end marker or with its compressed bytes, whether or not its EOS flag (bit 1) is set; bytes after the end of a stream are ignored. An LZMA header whose properties size is not 5, or whose lc/lp/pb byte is 225 or more or gives lc + lp over 4, fails as `BadZipFile: Bad LZMA properties for file '<name>'`. An LZMA entry shorter than its 9-byte header, or an archive that changes between reads, fails as `BadZipFile: Truncated data for file '<name>'`; a compressed size that otherwise overstates the data is caught when `zipfile` opens the entry (`BadZipFile: Overlapped entries: '<name>' (possible zip bomb)`), on the Pythons whose `zipfile` has that check (early 3.12 patch releases may not: there it gives a normal read, decoding stopping at the end of the stream, or `Truncated data`); a corrupt stream fails with the decoder's error (`LZMAError: Corrupt input data`, `OSError: Invalid data stream`). The entry's size and CRC-32 are then checked for every method. A bzip2 or LZMA entry of at most 4096 bytes is read whole while sniffing, so both its CRC and its size are checked then; for a STORED or DEFLATED entry that short, `zipfile`'s sniff, which decodes its `MIN_READ_SIZE` of 4096 bytes, checks only the CRC (a DEFLATED one whose compressed bytes all come in that first read is checked even if longer). An entry whose directory understates its size fails it (`BadZipFile: Bad CRC-32 for file '<name>'`) unless the CRC was written for the stated prefix, in which case only the stated bytes are read and the rest is ignored; one holding fewer bytes than it states fails as `BadZipFile: File '<name>' is N bytes, not the M the ZIP directory states` |
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

A DICOM's `IconImageSequence` (0088,0200) is never rendered: an embedded
icon's pixels are never decoded, it adds no row to the manifest or
`skipped.tsv` (only an `icon-image` note, see *Notes*), and a broken icon item does not fail its file (a sequence
malformed enough to break reading the file still fails it, as any malformed
element does). Text burned into an icon is therefore not reviewed (see
[Security model](security-model.md)). Work directories made by an older
version hold a second row, `{image_id}#icon`, for the icon (or a `failed` row
of that id); `export` still folds these (see *Parts and folding*).

**Collisions.** Verdicts are stored per `image_id`, so an `image_id` must
name one image. The naming is not escaped (changing it would orphan existing
`review.tsv` rows), so different inputs can produce the same `image_id`: a ZIP
entry literally named `a.png#2` beside two `a.png` entries; a file literally
named `z.zip::a.png` beside `z.zip` holding `a.png`. After every input is
rendered,
the pure `colliding_ids(images)` takes the `(image_id, source_sha256,
jpeg_sha256)` of every rendered image and returns the `image_id`s that name
more than one distinct `(source_sha256, jpeg_sha256)` pair: a different image,
or the same JPG from a different source (two sources that render identically
still collide, since `export` attests one `source_sha256` per `image_id`).
Every input that rendered such an image is replaced by one `failed` row for
its own `image_id`, reason `image_id collides with another input (rename one
of them)`, with a logged warning; its JPG is deleted and never appears in the
manifest. A file literally named `a.dcm#icon` beside an `a.dcm` with an icon
is not a collision: no `#icon` id is minted. The same source reached twice
under one `image_id` (a file reached through overlapping SOURCEs, so the same
pair) is not a collision either: one verdict is right for both, and both rows
stay in the manifest (see *Key versus `image_id`*).
Only rendered images are compared: a `failed` or `ignored` row sharing an
`image_id` with an image changes nothing (`export` already reports an
`image_id` with a `failed` or `ignored` row as `NOT_REVIEWED`).

**Notes.** A note (`Note(kind, detail)`, `kind` an `export.NoteKind`,
`Literal["overlay-plane", "overlay-unused-bits", "icon-image", "alpha",
"embedded-thumbnail", "trailing-data"]`,
the DICOM kinds first and then the raster kinds, listed in that order by
`export.NOTE_KINDS`, beside the export policy that must classify each
kind) records something a rendered input holds that downstream
tools should know about or strip, written whether or not an overlay is drawn; the kinds and their details are in
[Input notes](../commands/preprocess.md#input-notes). `render` makes a DICOM's
notes after rendering it, in `NOTE_KINDS` order, decoding nothing beyond the
render's own single decode: `overlay-plane` lists the overlay groups (the same
enumeration the overlay mask uses: even groups 0x6000-0x601E with
OverlayData); `overlay-unused-bits` takes the bit positions that
`preprocess_dicom` returns with the image (see step 2 of the pipeline below)
and follows each with ` (overlay 0x60xx)` for every even group
0x6000-0x601E without OverlayData whose OverlayBitPosition (60xx,0102) is
that position (a position that does not read as one integer names no group);
and `icon-image` reads only the first IconImageSequence item's
Rows, Columns and PhotometricInterpretation (`?` when missing, each value cut
to 64 characters plus `…`), inside a
catch-all so a bad icon gives an `unreadable (<ExceptionClass>)` detail and
never fails the input; and `trailing-data` notes native (not encapsulated)
PixelData longer than `pydicom.pixels.utils.get_expected_length(dcm, "bytes")`
by more than the one pad byte allowed when that length is odd, which pydicom
renders the first frame of without a word, with the detail `N bytes of
PixelData after its frame` and the endings of a raster's (see *Trailing
data*), or `trailing data unreadable: <ExceptionClass>` if the check fails (so
export denies the file, and the input still renders). A raster's notes are `alpha` (see the alpha rule
under *Raster decoding*), `embedded-thumbnail` (see *Embedded images*
under *Raster preprocessing*) and `trailing-data` (see *Trailing data*
there): `decode_raster` returns them with the image, in that order (an
MPO's later-frame `embedded-thumbnail` notes before `trailing-data`),
`alpha` with detail `N of M pixels not opaque`, N the pixels with
alpha below 1 and M all the pixels of the images that had alpha. Notes travel with the image
through `Rendered`, `Encoded` (so from pool workers) and the staged image;
when the JPGs are placed (see *Batching*), each placed input's notes become
`notes.tsv` rows in manifest order, details cleaned like a `skipped.tsv`
reason (see *Error handling*). A failed input, including a colliding one, has
none. Notes never affect the exit status.

**Render memory budget.** Rendering holds the decoded pixels and several
float copies, so its memory is out of proportion to the input's size. An image
is estimated, from its header and before any pixel is decoded, to need
`pixels x RENDER_BYTES_PER_PIXEL[path]` bytes (a DICOM's overlay planes
added), and fails if that is over `MAX_RENDER_BYTES` (3 GiB, per worker). The
costs per pixel (80 for `dicom-gray`, 100 for `dicom-gray-wide`, 108 for
`dicom-colour`, 135 for `dicom-colour-wide`, 96 for `dicom-palette`, 44, 48, 32 and 40 for `raster-gray8`,
`-gray16`, `-rgb8` and `-rgb16`, 120 for `raster-alpha`, 44 per canvas pixel
for `raster-mpo`) are the peak resident memory of `render_and_encode`,
input included, measured on synthetic images of 4000 to 6800 pixels a side
(for DICOM, also with content in every pixel's bits above `BitsStored`, and
so their view, which stays within the same figures) and rounded up by about
a tenth; `preprocess.json` records them with the two
limits. The largest square image each path passes is 6345 pixels a side for
`dicom-gray`, 5675 for `dicom-gray-wide`, 5461 for `dicom-colour`, 4884 for
`dicom-colour-wide`, 5792 for `dicom-palette`, 8556, 8192, 10033 and 8973 for the raster gray and RGB
paths, 5181 for `raster-alpha`. The estimate trusts the header, so
compressed DICOM pixel data is then checked against it from the codestream's
headers, never by decoding (see *Compressed frame check* below). Neither the
budget nor the byte limit changes any input under them.

**Compressed frame check** (`_check_compressed_frame`, after the budget and
before `_decode_pixels`). The transfer syntax UID is read as a `UID` even when
written with another VR (a plain `str`); several values raise `unsupported:
transfer syntax UID is not a single value`. A syntax that
`pydicom.pixels.get_decoder` has no decoder for is not checked: pydicom fails
it before decoding, as `cannot decode <name>: NotImplementedError: ...`. For a
compressed one it decodes, the one frame is found as pydicom's decoder finds
it (`generate_fragmented_frames` with one frame and the Extended Offset Table
if pydicom would use it), its fragments joined; a second frame raises
`unsupported: compressed pixel data holds more than one frame`, since pydicom
decodes frames past NumberOfFrames too. Then, by transfer syntax:

- JPEG (`JPEGTransferSyntaxes`) and JPEG-LS: `_jpeg_frame_size` steps from SOI
  over marker segments by their length to the first frame header (SOF0-15
  bar DHT, JPG and DAC; DHP; SOF55), finding each marker as `_jpeg_end` does
  within a `_WalkBudget`, and reads height, width and components. A height
  of 0 (to be set by DNL, or in JPEG-LS by LSE) is refused.
- JPEG 2000 and HTJ2K (`JPEG2000TransferSyntaxes`): `_j2k_frame_size` reads
  the SIZ marker that must follow SOC, after walking a JP2 file's top-level
  boxes to `jp2c` when the data starts with the JP2 signature, and returns
  Xsiz - XOsiz, Ysiz - YOsiz and Csiz. `_check_j2k_coding` then reads
  every COD and COC, in the main header and in each tile-part header
  (`_j2k_coding_segments`: SOT to SOD, the next tile-part Psot bytes on,
  which must be SOT, EOC or the end; at most `_J2K_MAX_TILE_PARTS`, 2**20).
  Only the marker segments a Part 1 or HTJ2K header may hold are allowed:
  CAP, COD, COC, TLM, PLM, CPF, QCD, QCC, RGN, POC, PPM, CRG and COM in the
  main header, and COD, COC, PLT, QCD, QCC, RGN, POC, PPT and COM in a
  tile-part header; any other raises `unsupported: cannot read compressed
  pixel data header (unexpected JPEG 2000 marker)`. OpenJPEG skips an
  unknown marker by scanning for the next one it knows rather than by its
  length, so it could read a COD the walk stepped over, and it copies Part
  2's MCT into every tile (60 KB of it in 65025 tiles took 4.45 GB). A COD
  of more than `_J2K_MAX_LAYERS` (256) quality layers raises `unsupported:
  JPEG 2000 quality layers over 256`: OpenJPEG's packet iterator allocates
  per layer, resolution, component and precinct (65535 layers of a 2048 x
  2048 image took 1.9 GB). At 256 layers, with 16 x 16 precincts and
  code-blocks, the largest images the budget passes (6300 x 6300 gray, 5400
  x 5400 RGB, 6 or 13 resolutions) peaked no higher than with one layer.
  The most seen in real files is 6. TLM bodies may hold at most
  `_J2K_MAX_TLM_BYTES` (1 MiB) in all, else `unsupported: JPEG 2000 TLM
  markers over 1 MiB`: OpenJPEG keeps about 10 bytes per TLM byte (100 MB
  of TLM in a 1024 x 1024 file took 1.78 GB), while a real TLM holds a 2 to 6
  byte entry per tile-part; no file seen has one.
  For each resolution r the code-block is 2**(xcb + 2) x 2**(ycb + 2), each
  side capped by the precinct in band coordinates (2**PPx at r = 0,
  2**(PPx - 1) above; 2**15 without precincts); any of fewer than 2**8
  pixels raises `unsupported: JPEG 2000 code-blocks of fewer than 256
  pixels`, whichever components or tiles it applies to. OpenJPEG allocates a
  few hundred bytes per code-block, so this bounds that to a few bytes per
  pixel. A missing COD, or a marker segment or tile-part that cannot be
  parsed, is unreadable. The tile count is not checked: OpenJPEG decodes at
  most 65535, and 873 MB was measured for a 3-component file of 65025 tiles.
- RLE Lossless: `_check_rle_frame` requires the header's segment count to be
  SamplesPerPixel x (BitsAllocated // 8) and walks each segment's run
  headers as pydicom decodes them (`_rle_decoded_length`), refusing a
  segment of more than Rows x Columns bytes. pydicom itself would decode it
  whole and keep the first Rows x Columns. A row of no-ops (128) is skipped
  by one regex match and the walk stops once past Rows x Columns, so every
  other step decodes at least a byte: a segment takes at most about 2 x Rows
  x Columns Python steps, however long it is or however the segments overlap
  (offsets need not increase).

A size other than Columns x Rows raises `unsupported: compressed pixel data
is W x H, not Columns x Rows C x R`, and other components `unsupported:
compressed pixel data has N components, not SamplesPerPixel S`; RLE raises
`unsupported: RLE pixel data has N segments, not SamplesPerPixel x
BitsAllocated / 8 (M)` or `unsupported: RLE segment decodes to more than Rows
x Columns (N) bytes`. Encapsulation or a codestream header that cannot be
parsed raises `unsupported: cannot read compressed pixel data header
(<fixed text>)`, never library text or file bytes. A compressed transfer
syntax pydicom can decode but that is none of the above (there is none in
pydicom 3.0) would raise `unsupported: cannot check compressed pixel data
dimensions (<name>)`. Native pixel data is not checked; nor is a missing
Rows, Columns, SamplesPerPixel or BitsAllocated, which pydicom refuses before
decoding. The sample depth is not checked: a frame of the declared size
decodes to at most a few bytes per sample.

**DICOM preprocessing pipeline** (`preprocess_dicom`):

0. Compressed pixel data is decoded by pydicom with the codecs above. A file
   whose decode fails is `failed` with reason
   `cannot decode <transfer syntax name>: <ExceptionClass>: <first line of the message>`
   (e.g. `cannot decode JPEG Extended (Process 2 and 4): RuntimeError: Unable to
   decode as exceptions were raised by all available plugins:`); uncompressed
   files keep the plain `<ExceptionClass>: <message>` reason.
1. Only single-frame images with pixel data are rendered. Before the pixels
   are decoded, the render memory is estimated (see *Render memory budget*
   below) as Rows x Columns times the bytes per pixel of the path:
   `dicom-gray` (MONOCHROME1/2 of up to 16 bits), `dicom-gray-wide` (32 bits
   or floating point), `dicom-colour` (RGB and YBR_*), `dicom-colour-wide` (the
   same at 32 bits) or `dicom-palette`;
   with `--draw-overlays` the overlay planes are added, at OverlayRows x
   OverlayColumns times `OVERLAY_BYTES_PER_PIXEL`. Over `MAX_RENDER_BYTES`
   it raises `unsupported: image too large (C x R, about N.N GiB to render;
   limit 3.0 GiB)`; compressed pixel data must then pass the *Compressed
   frame check* above. A missing Rows or Columns is left to the decoder; frames
   are not counted, as a multi-frame image is refused first. No pixel data,
   `NumberOfFrames` above 1 (checked first, whatever the photometric
   interpretation), or a photometric interpretation other than the ones below
   raises `Unsupported` (e.g.
   `unsupported: no pixel data (Basic Text SR Storage)` naming the SOP class
   when known, `unsupported: multi-frame DICOM (3 frames)`,
   `unsupported: photometric interpretation HSV`). Multi-frame DICOMs are not
   split into per-frame items. `RGB`, `YBR_*` (pydicom's `pixel_array`
   returns RGB) and `PALETTE COLOR` (expanded with `apply_color_lut`) images
   get step 2's unused-bit masking and skip the rest of steps 2-5: samples
   are scaled to uint8 by their nominal depth alone, never by the data:
   `bits` is BitsStored (for `PALETTE COLOR`, the entry depth declared by the
   third value of the Red Palette Color Lookup Table Descriptor, not how the
   table is stored; but when that is 16 and every red, green and blue entry
   of the lookup tables themselves, never the image pixels, is at most 255,
   the entries are 8-bit values under a 16-bit descriptor and `bits` is 8,
   and if the tables cannot be read the declared depth stands) and each sample becomes `round(sample * 255 / (2**bits
   - 1))`, clipped to 0-255 (negative samples become 0, larger ones 255,
   nothing wraps). Float colour pixel data raises
   `unsupported: float colour pixel data`, since it has no bit depth to
   scale by. So 8-bit data is unchanged, and an image that uses only part of
   its range (12-bit values stored as 16 bits) looks dark, as in any
   viewer; colour is not altered. No
   windowing or colormap is applied, and only the crop of step 6 follows.
   Steps 2-5 apply in full to `MONOCHROME1`/`MONOCHROME2`
2. Extract pixel array and mask its unused bits, then convert grayscale to
   float32 (values keep their native scale) only if every value round-trips
   exactly and the min-to-max span does not overflow float32. Otherwise raise
   `unsupported: not implemented: grayscale values cannot be processed
   safely as float32`. Float pixel data (`FloatPixelData`, grayscale or
   colour) holding NaN or infinity raises
   `unsupported: non-finite pixel values (1 NaN, 0 infinite)` (counts of
   each), as min-max scaling would collapse the whole image. The array is
   decoded once, raw: `pixel_array_options(correct_unused_bits=False)` is set
   before the first `pixel_array` access, because pydicom clears the bits
   above `BitsStored` for native, deflated, RLE and JPEG data but not for
   JPEG-LS or JPEG 2000. `_split_unused_bits` (pure) then keeps each
   integer sample's low `BitsStored` bits, sign-extended when the dtype is
   signed (PixelRepresentation 1), and returns with them the content of the
   bits above: unsigned, of the sample's width, `where(content, raw ^
   masked, 0) >> BitsStored`, or None when no sample has content there;
   `_unused_bit_positions` ORs it into the positions above `BitsStored`
   where a raw sample with content there differs from the masked value. A
   sample whose bits above `BitsStored` are all zero, or (signed) the sign
   extension, has no content there: writers fill unused bits either way, and
   clearing them in a sign-extended file gives the zero form. The note can
   also come from a `BitsStored` that understates the data or from lossy
   compression overshooting it (lossy JPEG 2000 coded at a higher precision
   than `BitsStored`: masking wraps its brightest pixels). Rendering uses
   the masked array for every transfer syntax; the positions become the
   `overlay-unused-bits` note (see *Notes*). When there is content, it is
   also rendered as a view of its own to the right of the image, placed
   after the colormap of step 7 and before the JPG is encoded:
   `_unused_bits_view` (pure) reduces colour samples by their maximum across
   channels and maps 0 to black and any other value `v` to
   `UNUSED_BITS_FLOOR + (1 - UNUSED_BITS_FLOOR) * v / max` (the floor is
   0.25, so one set bit is clearly visible beside much larger values, and the
   largest is white), float32 [0, 1]; it is cropped on its own (step 6's
   `_crop`) and shown linearly as an alpha view is, and `_beside` places the
   two uint8 views side by side, top-aligned, padded and separated by
   `SIDE_BY_SIDE_GAP` pixels of `SIDE_BY_SIDE_FILL` mid-gray (128), as
   `preprocess_raster` places an alpha view. The image itself is the same as
   without the view. Lossy JPEG 2000 overshoot shows there too. The view
   shows that the bits hold something, not every pattern they hold, so
   `export` still denies the file (see *Parts and folding*). HighBit must be
   `BitsStored` - 1 (the stored bits are the low ones): `_check_high_bit`
   raises `unsupported: HighBit H with BitsStored S is not supported` before
   decoding when HighBit is present and differs, since otherwise bits below
   the stored ones, which can hold text, would be neither shown nor noted.
   Float samples, and integer samples
   whose dtype has no bits above `BitsStored` (`BitsStored` equal to
   `BitsAllocated`), are left as they are and give no positions. Colour and
   palette images are masked too, palette indices before `apply_color_lut`.
   Limit: `YBR_*` data is masked after pydicom's conversion to RGB, so for
   YBR with `BitsStored` below `BitsAllocated` unused bits in the YBR samples
   reach the conversion, and the converted RGB is masked to `BitsStored`
   bits: with 8 bits allocated and fewer stored (non-conformant; e.g. 7
   stored) RGB values of 128 and up wrap and give a spurious note
3. Correct photometric interpretation (invert MONOCHROME1 by negation)
4. Window the intensities: the robust window runs from the 1st to the 99th
   percentile of the values strictly inside the outer 0.01% of the min-max
   range (`RANGE_TRIM`, which leaves out only pixels at or very near the
   extremes, such as a hot pixel), shrunk by a 2% margin on each side. It
   maps to [0, 1] and values outside it are clipped, so text brighter (or
   darker) than the window merges with whatever else lies beyond it, such as
   a bright border. An image with a single value maps to all zeros. When no
   window comes of those values (there are none, or the percentiles meet, as
   in a two-valued image), the same percentiles and margin are taken over all
   the values instead; only when that finds none either is a plain min-max
   rescale used. Then, only with
   `draw_overlays`, overlay planes (see below) are drawn at 1.0
5. Apply CLAHE (adaptive histogram equalization, 96-pixel tiles)
   (drawn overlay pixels stay at or near the top of the range)
6. Strip uniform rows/columns (`compress_image` -- removes letterboxing). If
   stripping would leave nothing (e.g. an all-zero image), the uncropped
   image is kept
7. Apply colormap (grayscale only), save as 8-bit RGB JPG (quality 95, 4:4:4 chroma, no subsampling)

**Overlays**: by default (`draw_overlays` false; CLI `--draw-overlays`)
overlay planes are not decoded or drawn: the image is the PixelData alone and
`overlay-plane` notes list the planes. With `draw_overlays`, each overlay
plane (even group 0x6000-0x601E with OverlayData `(g,3000)`) is decoded with `overlay_array`, placed at its OverlayOrigin
(1-based row, column; default 1, 1) and clipped to the image. For grayscale
images the overlay pixels are set to 1.0 after intensity compression (step 4)
and before the first crop, so they come out near 255 (CLAHE keeps them at the
top of the range); for colour and palette images they are set to white (255)
before the crop. With the flag, a plane that cannot be decoded fails the
whole file (fail-closed: a reviewer who asked for overlays drawn never
reviews an image missing one):
`ValueError: overlay 0x6002 cannot be decoded: ...`. Overlays embedded in the
unused bits of PixelData (retired; PS3.5 §8.1.1 lets receivers assume nothing
about those bits) are never drawn onto the image, with or without the flag: those bits are masked
(step 2), shown as a view of their own beside the image, and noted as
`overlay-unused-bits`, naming any 60xx group whose OverlayBitPosition points
at a set bit.

**Raster preprocessing** (`decode_raster` + `preprocess_raster`, for PNG/JPEG/TIFF/BMP/GIF):

- Decode with Pillow and branch on the image mode, not the channel count
- Before any pixel is loaded (including by the 16-bit PNG and TIFF
  branches), the render memory is estimated from the header (see *Render
  memory budget* below): width x height times the bytes per pixel of the
  path `_raster_path` picks by mode: `raster-alpha` for the alpha modes, any
  `transparency` (so a palette with it counts as RGBA) and a 16-bit
  Pillow-truncated image with a fourth sample, `raster-rgb16` for the other
  16-bit RGB, `raster-gray16` for I;16, I and F, `raster-gray8` for 1 and L,
  and `raster-rgb8` for the rest. An MPO is one canvas of its frames side by
  side with the 4-pixel gaps (its height the tallest frame's), at
  `raster-mpo`; the frame sizes are read on a second `Image` opened from the
  same bytes, because seeking the rendering one back to frame 0 would parse
  that frame's EXIF again without the guard `Image.open` gives it. Over
  `MAX_RENDER_BYTES` raises `unsupported: image too large (W x H, about N.N
  GiB to render; limit 3.0 GiB)` (an MPO: `2 frames, W x H side by side`).
  Pillow's `DecompressionBombError` (over twice `Image.MAX_IMAGE_PIXELS`,
  179 million pixels) is the same `Unsupported`, `image too large (over N
  pixels, Pillow's decompression bomb limit)`
- Each frame is decoded to float [0, 1]. Several views of one input are
  placed side by side in a single rendered image (one manifest row, one
  `image_id`), top-aligned, separated by a 4-pixel gap
- MPO files (JPEGs carrying extra MPF images: Ultra HDR gain maps, camera
  previews) render every frame side by side at native resolution, padded and
  separated with black. Pillow 12 leaves a JPEG whose XMP declares
  `hdrgm:Version` (Ultra HDR) a plain JPEG, where Pillow 10.3 opens it as an
  MPO, so `_open_image` adopts any plain JPEG whose MPF lists more than one
  image with `MpoImageFile.adopt`, on every Pillow. The frame-size check
  for the memory budget opens the input the same way. A malformed MPF, or
  an adoption that raises, keeps the plain JPEG (opened afresh), rendered as
  its primary image with a `trailing-data` note for the extra bytes. Any other multi-frame image (animated PNG/GIF,
  multi-page TIFF) raises `unsupported: multi-frame image (N frames)`
- Modes with real alpha (`RGBA`, `LA`, `PA`, premultiplied variants, or any
  mode with a `transparency` entry such as a palette with a transparent
  index or a grayscale/RGB colour key): if alpha is fully opaque it is
  dropped, with no note. Otherwise the rendered image shows, side by side
  with a mid-gray gap, the raw colour/gray channels with alpha ignored
  (left; content hidden under transparent pixels stays visible) and alpha
  itself as grayscale, transparent 0 and opaque 1 (right; content carried
  only by alpha stays visible whatever the colour). Grayscale+alpha stays
  grayscale. The two views are normalized separately, then placed side by
  side, so the alpha view cannot flatten the colour view: the colour view as
  an image of its own (CLAHE if grayscale, none for RGB), alpha linearly
  (0 black, 1 white, no CLAHE). Any alpha below 1, including a `transparency` colour key, adds
  an `alpha` note counting those pixels. An MPO's frames are JPEGs and carry
  no alpha, but if one did, the counts of all its frames would be summed
  into one note
- Embedded images (*Embedded images*): `_embedded_image_notes(im, data)`
  reads only metadata, while the Pillow image is open and before the 16-bit
  branch below, so a 16-bit PNG or TIFF is covered too. Nothing is rendered,
  no pixel is decoded and the image is not touched: it never calls
  `getexif()` (which can decode a PNG's pixels and caches an Exif object on
  the image), and every parse uses a fresh `Image.Exif()`. The EXIF bytes
  come from every `eXIf` chunk of a PNG, found by walking its chunks up to
  IEND (`_png_exif`; readers differ on which of several counts, so each gives
  its own notes in file order), plus the
  ImageMagick "Raw profile type exif" text in `info` (decoded as Pillow does,
  `bytes.fromhex("".join(text.split("\n")[3:]))`; a bad one gives `EXIF
  unreadable: ValueError`), or from `info["exif"]` of any other format except
  TIFF (JPEG, MPO, WebP, ...). Pillow reads a text chunk after the pixels only
  on load, which is not done, so such a "Raw profile type exif" is not
  detected. IFD1 (`get_ifd(IFD1)`) holding
  JPEGInterchangeFormat 0x0201 gives `embedded-thumbnail` with detail `EXIF
  IFD1 thumbnail (N bytes)`, N the value of JPEGInterchangeFormatLength
  0x0202; one holding StripOffsets 0x0111 (and no 0x0201) gives `EXIF IFD1
  thumbnail, uncompressed (N bytes)`, N the sum of StripByteCounts 0x0117.
  N is `?` when that tag is missing or not an integer (or integers). TIFF is
  skipped for EXIF because its IFD1 would be a second page, and multi-page
  TIFFs are rejected. Each later frame of an MPO is read after its seek, from
  that frame's `info["exif"]` (Pillow 10.3 and later update it per frame), and
  its details start `frame N: ` (N from 1). A TIFF or BigTIFF with SubIFDs
  (tag 330, read from the first page with tifffile because Pillow drops a
  BigTIFF's IFD8 offsets) gives `TIFF SubIFDs (k)`, k the number of offsets.
  Whether the tag is there is decided from the first IFD's raw entry list
  (`_tiff_has_subifds`: 2-byte count and 12-byte entries, or 8-byte count and
  20-byte entries for BigTIFF; reads bounded by the file length), because
  tifffile drops a tag whose value it cannot read; a tag that is there
  without a usable value gives `TIFF SubIFDs unreadable: invalid value`.
  A failure to read gives a note, never an error: `EXIF unreadable:
  <ExceptionClass>` or `TIFF SubIFDs unreadable: <ExceptionClass>`; and
  because Pillow reports most corrupt EXIF only as a `UserWarning` and returns
  fewer tags, a warning recorded while parsing EXIF adds `EXIF unreadable:
  corrupt`. Details hold only fixed text, numbers and the exception class,
  never file text, so they are safe in a TSV. Not detected: JFIF/JFXX APP0
  thumbnails, Photoshop APP13 thumbnails, and EXIF so malformed that Pillow
  returns no tags without a warning. A plain JPEG gets no note, nor does an
  MPO without EXIF thumbnails
- Trailing data (*Trailing data*): `_trailing_data_notes(data, format,
  frame_offsets)`, called beside `_embedded_image_notes` while the Pillow
  image is open and before the 16-bit branch, gives one `trailing-data` note
  when a JPEG, MPO or PNG holds bytes that are not rendered; other formats
  are not checked. It reads only `data`, `im.format` and (for an MPO)
  Pillow's frame offsets, so no pixel is decoded and the image is not
  touched. The rule is that every byte outside the image's segment or
  chunk structure is counted: over-reporting is acceptable, a miss is not.
  Payloads inside that structure (JPEG APP and COM segments, PNG ancillary
  chunks) are not counted, even when they hold an image. The ends are found
  by pure walkers, every read bounded by the length of `data`, the position
  only moving forward and nothing allocated from a declared length:
  - `_jpeg_end(data, start=0, budget=None)`: from SOI, marker segments are
    skipped by their length. Between them, the next marker is found by a
    compiled regex search for 0xFF followed by a byte other than 0x00
    (stuffing), 0x01 (TEM), 0xD0-0xD7 (RSTn) or 0xFF (fill), so
    entropy-coded data, a progressive file's several SOS and DHT segments
    between scans, and stray bytes (which Pillow and libjpeg skip too) cost
    a C-level scan. A segment declaring a length of 0
    or 1 is passed over after its two length bytes, as Pillow and libjpeg
    do. The end is just past the first EOI; there is none (None) at the end
    of `data`, at a nested SOI, or at a segment that runs past the end.
    Each marker stepped to and each byte searched is spent from a
    `_WalkBudget`, one per input shared by all its JPEG walks (an MPO's
    frames): at most `JPEG_WALK_MAX_MARKERS` (100,000) markers and
    `JPEG_WALK_SEARCH_FACTOR` (4) times the file's size plus
    `JPEG_WALK_SEARCH_SLACK` (1 MiB) bytes searched, a search never
    reaching past what is left. Running out raises `_OverBudget`, and the
    input's note is then `not checked: too many JPEG markers or frames`
    (`TRAILING_DATA_OVER_BUDGET`), which over-reports and never hides
    data. Time and memory so stay linear in the file's size. Real files
    are far under: a 31 MB noise JPEG, progressive and restart-marker
    files, and a three-frame Pillow MPO step over 10 to 28 markers and
    search each byte once.
  - `_png_end(data)`: the chunks (`_png_chunks`, shared with `_png_exif`:
    length, type, data, CRC after the 8-byte signature) are walked to where
    Pillow stops: just past IEND's 8-byte header, which is all Pillow reads
    of it, so data that IEND declares counts (an empty IEND's CRC does not:
    the end is just past it, or the end of `data` if that CRC is cut short);
    else the start of the first chunk whose type is not four `[A-Za-z0-9_]`
    bytes or whose data and CRC run past the end; else where the walk ends.
    A PNG missing only IEND, with nothing after its last chunk, therefore
    gets no note.
  - An MPO's frames are where Pillow reads them: its private
    `_MpoImageFile__mpoffsets` (absolute, the first 0; present from Pillow
    10.3 to 12.3, set when Pillow opens or `adopt`s the MPO), read in `_mpf_offsets` inside a catch-all. Pillow deletes
    its `mpoffset` base from `info` after use, and finding the base again
    from the file could disagree with Pillow on a crafted file. If the
    offsets cannot be read, none are used, so everything after the first
    EOI is reported. `_mpo_unrendered` covers [0, first EOI) and each later
    frame's [start, EOI): repeated offsets are walked once, all from the
    input's one budget, and an offset not at `FF D8 FF` (which Pillow
    rejects) or whose EOI is not found covers nothing. Every other byte counts (`_uncovered`), between frames or after
    them. The entries' Size is not used: an end the file declares could
    cover bytes no frame decodes.

  A JPEG or MPO whose first EOI is not found gets `JPEG end not found`
  (cut-off JPEGs fail to render, so this reaches only inputs that render
  while holding bytes the decoder did not need). Otherwise, with no
  uncovered bytes, there is no note. One run at the end of the file gives
  `N bytes after the <FORMAT> end` (`JPEG`, `MPO` or `PNG`), any other
  MPO runs `N bytes between or after the MPO's frames`, N the bytes in all
  runs (`_unrendered_detail`). Then `; all zero bytes` when every counted
  byte is zero, or else, from the first `SNIFF_BYTES` of the first run, `;
  they begin with a DICOM preamble` (`_has_preamble`), `; they begin with a
  JPEG signature`, `; they begin with a PNG signature` or `; they begin with
  another image signature` (`_is_raster`); otherwise nothing. A walk over
  budget gives `not checked: too many JPEG markers or frames`. Any exception
  gives `trailing data unreadable: <ExceptionClass>` and never changes
  whether the input renders. The counted bytes are never quoted, so the
  detail is safe in a TSV. A tables-only `SOI ... EOI` prefix before the
  image makes the image itself count, which errs on the safe side
- High-bit-depth grayscale (`I;16*`, `I` and `F`, and the gray of a 16-bit
  gray+alpha PNG) is windowed exactly as DICOM grayscale is (step 4 above,
  all zeros when constant), so one extreme pixel cannot push the content
  into a single bin of CLAHE; only its alpha is divided by 65535.
  `I`/`F` samples must round-trip exactly through
  float32 and their min-to-max span must not overflow float32; otherwise the
  same unsupported reason as DICOM grayscale is raised. A mode `F` image holding
  NaN or infinity raises
  `unsupported: non-finite pixel values (1 NaN, 0 infinite)`; a `tRNS` colour
  key is matched against the native values to build the alpha mask
- 16-bit colour and alpha, which Pillow truncates to the high byte, are
  decoded at full depth instead, chosen from the header before any pixels
  are decoded. A PNG whose Pillow raw mode is `RGB;16B`, `LA;16B` or
  `RGBA;16B` (bit depth 16, colour type 2, 4 or 6) is decoded with
  imagecodecs (`png_decode`, which turns a `tRNS` colour key into alpha;
  libpng refuses a non-conformant file that Pillow would read, such as one
  with a chunk before IHDR, so it is `failed` as `cannot decode 16-bit PNG:
  <ExceptionClass>`, without libpng's message, which can be garbled). A TIFF whose BitsPerSample
  is 16 and SampleFormat unsigned, with PhotometricInterpretation RGB and at
  least 3 samples or CMYK (separated) and exactly 4, is decoded with
  tifffile (first page; planar data is interleaved). In an RGB TIFF the
  first extra sample is alpha when ExtraSamples marks it unassociated
  alpha, or when a fourth sample has no ExtraSamples tag at all (as Pillow
  reads it); associated (premultiplied) alpha is unpremultiplied first,
  `colour * 65535 // alpha` clipped to 65535, with colour 0 where alpha is
  0, as Pillow does (colour under alpha 0 in premultiplied data is zero,
  which un-premultiplying cannot recover). Colour, ink and alpha are all
  divided by 65535, their nominal depth: nothing is computed from the data,
  so no pixel (hidden content under transparent pixels, a stray bright
  pixel) changes how another is scaled, and a colour image that uses only
  part of its range looks dark. The gray of a gray+alpha PNG is not colour
  in this sense: it is windowed (see above), so hidden gray under
  transparent pixels can move its window. Then the alpha rule above applies. CMYK
  inks are converted with Pillow's formula `RGB = (1 - [C, M, Y]) * (1 - K)`.
  Gray+alpha stays grayscale. The multi-frame check runs first
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
`notes.tsv`, `preprocess.json`) still abort the run; since images are encoded in memory first, an input's content
cannot cause one.

**Batching**: Each input is rendered, encoded and written as soon as discovery yields
it (with `--jobs` > 1, at most `2 × jobs` inputs ahead), so memory does not grow with batch size. The JPG is first written to
`<staging>/.pending/` under a provisional name; once every input is rendered
and collisions are known (see *Collisions*), the JPGs of non-colliding inputs
are renamed into their batches in discovery order, the rest are deleted, and
`.pending` is removed. The n-th kept image
(0-based) goes to `batch_{n // batch_size + 1:03d}/img_{n % batch_size + 1:05d}.jpg`.

**Output**: `manifest.tsv`, `skipped.tsv` and `notes.tsv` (both always
written, even when empty) and `preprocess.json`. The summary line and the exit status are in
[Skipped inputs](../commands/preprocess.md#skipped-inputs).

### `image-review review`

The synopsis, options and defaults are in
[`image-review review`](../commands/review.md).
`--reviewer` defaults to `getpass.getuser()` at run time and is checked by
`connection.parse_reviewer` (1-64 characters, all `str.isprintable()`, not
all whitespace). `--batch` is checked by `unknown_batch_message` against the
manifest's batches once the store is open.

Opens a store (see below), initializes pygame, creates a `ReviewSession`,
runs the event loop, then shuts down pygame. pygame starts only after the
store is open.

**Store** (`cli.open_local_store`, shared by every command that opens one):
`review` opens `LocalStore(work_dir)` writable, holding the work dir lock
until the command exits; `status` opens it with `read_only=True` (no lock).
Both resolve `--work-dir` with `_existing_work_dir` (default `./review_work`;
a missing directory is a `BadParameter`, exit 2) and enter the store as a
context manager. `WorkDirLocked` is a `ClickException` (exit 1) with its
message.

### `image-review status`

```
image-review status [--check] [--work-dir DIR]
```

Opens the store read-only (no lock; see `review`). The command fetches the manifest, the current pass and that pass's
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
in [Checking from a script](../commands/status.md#checking-from-a-script).
Errors (bad store, malformed `skipped.tsv`) exit 1 or 2 as without
`--check`.

### `image-review export`

```
image-review export [--work-dir DIR] [--output FILE] [--report FILE] [--allow-live]
```

Writes the allowlist of releasable files and the optional report of the
rest. What they hold, how to use them, the exit codes and the main messages
are in [export](../commands/export.md); this section is the mechanism. Local
only: there is no server endpoint for it, so `image_id`s and
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
- `ReviewDB.decisions()` raises on a torn last line of `review.tsv` or a last mark cut short, and
  `load_skipped` and `load_notes` on a malformed `skipped.tsv` or `notes.tsv`, or a
  missing one beside a manifest that records hashes (a legacy manifest without
  them may lack both). None is overridable.
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
`export.export_rows` to the manifest entries, `ReviewDB.decisions()`,
`load_skipped` and `export.unreviewed_content` of `load_notes` (below), giving
one row per source file, which
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
make unambiguous. A row's `source_sha256` is the one the manifest
records for `X` itself; only if the manifest does not list `X` at all (an
icon without its main, or a failed `X`), the one for `X#icon`; else
empty (an `X` listed without a hash never borrows its icon's).

**Parts and folding.** Every distinct `image_id` of the manifest, and of the
`failed` rows of `skipped.tsv`, is a part. Its decision is its latest one
unless that is stale: `Decision.applies_to(hash)` is false (the decision's
`source_sha256` is not empty and differs from the manifest's for that part,
from `manifest_hashes`), in which case the part has no decision. An `X#icon`
part is judged against its own manifest hash. Then:

- It is `NOT_REVIEWED` if `skipped.tsv` has any `failed` row for it, even if
  the manifest lists it too. Repeated rows count once, with the first reason.
  A manifest `image_id` with an `ignored` row (and no `failed` one) is
  likewise `NOT_REVIEWED`, with the first `ignored` reason: the two files
  disagree, so nobody can vouch for it.
- Otherwise it is `DIRTY` if its decision is.
- Otherwise it is `NOT_REVIEWED` if `notes.tsv` lists unreviewed content for
  it, with the reason `unreviewed embedded content: <kinds>`, whether its
  decision is CLEAN or it has none. `export.unreviewed_content(notes)`
  derives that reason per `image_id`: the note kinds in
  `export.UNRENDERED_NOTE_KINDS` (`overlay-plane`, `overlay-unused-bits`,
  `icon-image`, `embedded-thumbnail`, `trailing-data`), leaving out a
  `trailing-data` note whose detail ends in
  `export.ALL_ZERO_SUFFIX` (`; all zero bytes`, which preprocess also writes),
  each kind once, in `NOTE_KINDS` order. `export.SHOWN_NOTE_KINDS`
  (`alpha`, shown as a view of its own) never block; the two sets partition
  `NOTE_KINDS`, so a new kind must be put in one. `overlay-plane` blocks
  even with `--draw-overlays`: a drawn plane is clipped to the image (one
  placed outside it is not drawn at all) and drawn at maximum brightness, so
  it can be lost against bright pixels. `overlay-unused-bits` blocks though
  preprocess shows those bits as a view of their own: the view is scaled by
  their largest value, so different bit patterns can render at nearly the
  same gray (a bit-15 box holding bit-8 text: 254 against 255), and it
  cannot vouch that all of them were seen.
  Notes for `image_id`s that are not parts are ignored. preprocess writes
  notes under the main `image_id`, and a work directory old enough to have
  an older version's DICOM `X#icon` parts has no `notes.tsv`; but a real
  file named `X#icon` gets notes of its own, and when it is folded into
  `X`'s row (equal hashes) its `NOT_REVIEWED` part makes that row
  `NOT_REVIEWED` with `icon: unreviewed embedded content: <kinds>`.
- Otherwise its status is its decision's (CLEAN; a tombstone leaves none),
  or `UNREVIEWED` without one, with the reason `export.STALE_VERDICT`
  (`verdict recorded for different content (the file changed since it was
  reviewed)`) when it had only a stale one. A stale DIRTY is no decision too,
  so the `notes.tsv` rule above sees none, and a stale verdict is never
  allowlisted.

`_fold` puts a main part's non-empty reason first in the row's `reason`; an
`UNREVIEWED` icon part with a reason adds `icon UNREVIEWED: <reason>`.

Status is not pass-aware: a DIRTY from an earlier pass that is FLAGGED in the
current one is `DIRTY` (it still contains PHI as far as anyone has said).
A part `X#icon` (`export.ICON_SUFFIX`; preprocess no longer renders icons, so
only a work directory made by an older version, or a file merely so named, has
one) is folded into the row of `X`. When there is no part `X` (e.g. a file
literally named `scan.dcm#icon` with no `scan.dcm` beside it), the row is
still `X` and the missing main part counts as `NOT_REVIEWED` (`main image
missing`, or the ignored reason if `X` was ignored, see below), so it is never
CLEAN.

An older version's DICOM icon has its file's hash, so when the manifest
records a hash for both `X` and `X#icon` and
they differ, `X#icon` is ambiguous: it may be `X`'s icon or a different file
merely named so (e.g. a ZIP entry `scan.png#icon` beside `scan.png`), and
either reading can hide a part nobody saw. Then `X#icon` is still folded into
`X`'s row, and also gets a row of its own, where `X#icon` first appears (its
own part as main, with its own hash and its own `X#icon#icon` folded in). Both
rows are at least `NOT_REVIEWED`, with `export.AMBIGUOUS_ICON` as their last
note, so neither is CLEAN and both hashes reach the "same content" denial. A
chain `X`, `X#icon`, `X#icon#icon` with three different hashes gives three
such rows.

**Ignored inputs.** Each distinct `image_id` of the `ignored` rows of
`skipped.tsv` (inputs preprocess did not take for images, e.g. a PDF) gets a
row of its own, `IGNORED`, with the first such row's reason and every other
column empty, unless an earlier row already covers it: it is a part (a
manifest or `failed` `image_id`, reported as above), or the file `X` of a
folded row: an ignored `X` with a manifest or `failed` `X#icon` but no part
`X` is that row's main part, `NOT_REVIEWED` with the ignored reason (e.g.
`DICOMDIR index`) in place of `main image missing`. If the manifest lists
`X#icon`, an ignored `X#icon` is that part (`NOT_REVIEWED`, folded into `X` as
`icon: <reason>`, and also reported on its own row if it is ambiguous, see
above); otherwise it is a source path like any other (a file may be
named so) and gets its own `IGNORED` row, not folded into `X`. An `IGNORED`
row is never CLEAN; treat its file as possibly containing PHI. Rows cover only
the listed files and ZIP entries, never a ZIP or directory as a whole. An
archive that cannot be opened or holds bytes outside its listed entries
(`failed`) or has no file entries (`ignored`,
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
image-review serve [--work-dir DIR] [--socket-path PATH] [--via DEST | --direct] [--ssh-host NODE]
```

The options, the token, the option rules and their exit codes, what `serve`
prints and how it stops are in [serve](../commands/serve.md); this section is
the mechanism. `--ssh-host` defaults to `socket.getfqdn()`,
`--socket-path` to `default_socket_path()`, `--via` is checked with
`tunnel.parse_via` and `--ssh-host` with `tunnel.parse_ssh_host`.

The [option rules](../commands/serve.md#option-rules) are `UsageError`s
raised before the work directory is opened and before anything is logged;
"on the command line" means the parameter source `COMMANDLINE`. click drops
an empty environment value, but refuses an invalid `$IMAGE_REVIEW_DIRECT`
while parsing. A missing `socket.AF_UNIX` (`cli.has_unix_sockets`) is a
`ClickException`, raised after the option rules.

Opens a writable `LocalStore` (holding the work dir lock for the server's
lifetime; `WorkDirLocked` is a `ClickException`, exit 1), calls
`server.make_unix_server` (see *Unix-socket server*) at `--socket-path` or
`default_socket_path()`, and serves until interrupted.

`$IMAGE_REVIEW_TOKEN` is read with `os.environ`, not a click option. A value goes through `connection.parse_token`; its
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
description is in [What serve prints](../commands/serve.md#what-serve-prints).
`PORT` is `BROWSER_PORT` (8080), `SOCKET`
is the absolute path that was bound, `USER` is `getpass.getuser()`, else
`<user>`. Each of `VIA`, the `-L` argument and `USER@NODE` goes through
`shlex.quote`. The URL is `browser_url(token)`. Off a TTY,
`write_private_file` writes it to
`~/.image-review/browser-{safe_name(short_host())}-{pid}.txt`
(`private_dir()` creates the directory 0700, requires it to be a directory
owned by the user, and tightens it to 0700 if looser; `write_private_file`
unlinks a stale file, then creates it `O_EXCL|O_NOFOLLOW` with mode 0600 and
removes a partly written one), and the printed `cat` path is quoted twice,
as the remote shell parses it again.

**Cleanup**: the option checks and parsing run first. Everything after
them (opening the store, binding, announcing, serving) runs under
`interrupt_once(SIGINT, *TERMINATION_SIGNALS)`, installed before the lock is
taken, so SIGTERM and SIGHUP unwind like Ctrl-C, and only the first signal
counts: its handler sets all of them to `SIG_IGN` before raising, so a second
one cannot interrupt the cleanup below. Cleanup is registered on one
`ExitStack`: `_close_with` registers the store's close
(`_close_store_after_marks`, which waits for a mark in flight under
`server.store_lock`) and then `server_close` as soon as the server is bound,
and the announced file's removal is registered as soon as it is written. A
failure at any later point therefore releases them, and they run in reverse:
the file, the socket (`server_close` also removes the socket file), then
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
local `export`; `ReviewDB` also records each verdict's `source_sha256` in
`review.tsv` (see *`review.tsv`*). Everything else identifies an image by its
`preprocessed_path`; no hash is ever sent over the wire.

The file is parsed strictly (`store.load_manifest`) when the work directory is
opened, and is never repaired. A legacy three-column manifest loads with both
hashes `None`. One `image_id` has one `source_sha256`: a row repeating an
`image_id` with a different one is a violation (equal hashes are fine; legacy rows
are not compared), so `export` vouches for one hash per file. A violation is a `ValueError` naming `<file>:<line>` (or the
byte offset, for a file that is not UTF-8), which the CLI shows as
`Cannot read work directory: ...` (exit 1).

**Integrity check.** When the manifest records a `jpeg_sha256`,
`LocalStore.image_bytes` (and so `image_bytes_many`) hashes the bytes it read
and raises `ValueError("<key> does not match its recorded hash")` on a
mismatch. Every caller already treats that as an unloadable image: the grid
packer leaves it out of every grid, the review session shows a placeholder that
takes only DIRTY (see *Unloadable Images*), and the server answers `/image`
with 404 (`loadJpeg` in the browser page returns null for any non-OK reply,
and the page shows "Cannot load image: KEY" instead of the image). This
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
It is parsed strictly by `store.load_skipped`. A missing file reads as
empty, except for `export`, which passes `required=True` when the manifest
records hashes and refuses with a `ValueError` naming the file.

It contains source paths, so it is as sensitive as `manifest.tsv`. `export`
makes each `failed` `image_id` `NOT_REVIEWED` with its `reason` (folding a
failed icon's, from a work directory made by an older version, into its
file's row), and lists each `ignored` one not otherwise
covered as `IGNORED` with its `reason` (see *Ignored inputs* under *Parts and folding*).

### `notes.tsv`

Written by `preprocess`, always (header only when there are no notes), with
the policy's file mode like the other files. Columns and encoding are in
[`notes.tsv`](work-directory.md#notestsv); how notes are made is in *Notes*.
`preprocess.json`'s `counts.notes` holds its row count per kind, and the
summary line the same counts.

It is parsed strictly by `store.load_notes` into `NoteRow(image_id, kind,
detail)`s, `kind` one of `NOTE_KINDS`; like `skipped.tsv`, `export` passes
`required=True` when the manifest records hashes, so a work directory made
before 0.4.0 (which has none) cannot be exported.

It contains source paths, so it is as sensitive as `manifest.tsv`. `export`
makes a file with unreviewed content `NOT_REVIEWED` (see *Parts and
folding*); the server never serves it (no JPG key can name it).

### `review.lock`

Present while a writer (`review` or `serve`) has the work directory open. JSON
naming the holder. Its user-facing rules are in
[`review.lock`](work-directory.md#reviewlock); the mechanism is in
*Concurrency limits*.

### `review.tsv`

An append-only log written by `ReviewDB`. Each mark action encodes its rows
into one buffer and writes it to a file opened `O_APPEND`, looping until a short
`write` has written it all, then `fsync`s. The append that creates the file
first `fsync`s the work directory, so the new name is durable before any row
is written; if that fails, the append fails with nothing written and the
empty file is reused by the retry. Appends never change earlier bytes
or the inode, with two exceptions: a torn tail (see below) is cut off before
the next append, and a file with an older header is migrated once (see below),
which replaces the file with a new inode. A crash mid-write can therefore record only some rows
of a multi-image mark. A new or empty file gets the header first and is
`fchmod`ed (if needed) to the file mode of the work directory's access policy
(0600 private, 0660 group). Lines end in `\r\n` (Python `csv`'s default, as the file has
always used); the reader accepts `\n` too.

The columns, the last-row-wins rule and the parse rules users see are in
[`review.tsv`](work-directory.md#reviewtsv). Internally, `mode` holds a
`status.MarkMode` (`single`, `grid`) or `review_db.RowMode`'s `undo`;
`tool_version` is `connection.package_version()`, resolved once per process;
in a mark row (`mark_many`), `source_sha256` is the `sources` mapping's entry
for the row's `image_id` (`""` if it has none; undo rows copy theirs, see
*Undo rows*), which `LocalStore` builds from its manifest with
`export.manifest_hashes`, so the server takes it from its own manifest and no
hash crosses the wire. `Decision.source_sha256` is a plain `str`, `""` or 64
lowercase hex characters (checked by `review_db.parse_sha256` when parsed), as
`reviewer`, `mode` and `tool_version` use `""` for "not recorded".
`Decision.applies_to(source_sha256: str | None) -> bool` is false only when
both its own hash and the given one (the manifest's now; `None` when it has
none) are non-empty and differ: a stale row (see *Pass Logic* and *Parts and
folding*).
Files written before the log format (one row per `image_id`) are already
valid logs. The file is parsed strictly (`review_db.parse_log`; `FLAGGED` is
never a valid `status`), and a bad file is a `ValueError` naming
`<file>:<line>`, shown as `Cannot read work directory: ...`, never skipped or
rewritten. `review_db.tsv_rows` reads all three TSVs (`review.tsv`,
`manifest.tsv`, `skipped.tsv`) and turns a `csv.Error` (in practice a field
longer than csv's limit of 131072 characters, which is deliberately not raised)
into that `ValueError` with the reader's line.

Two crash leftovers are tolerated. An empty file (created, but the first append
never landed) holds no decisions. An unparseable last line with no line ending
(`\r` or `\n`; a torn append, including a tail longer than 131072 characters
that csv refuses, such as a zero-filled extension) is ignored with a logged
WARNING, and the next mark truncates the file back to the end of the last
complete line before appending, so the file stays strictly parseable. The
truncate happens only if the file still has the inode and size seen when it was
loaded; otherwise the mark fails with `<file> changed since it was loaded`. A failed append (short
write then an error, or a failed `fsync`) is cut off the same way, at once if
possible, else by the next mark. A read-only store warns but never writes.

A crash can also leave only some rows of one multi-row mark, cut at a line
ending. On load, `incomplete_last_mark` takes the trailing rows equal to the
last row in `(timestamp, reviewer, mode, grid_size)`; if `grid_size` is set and
their count is not a multiple of it (so two equal marks in one timestamp stay
complete), a WARNING names the file (see [what is never logged](security-model.md#the-server-image-review-serve)) and
`decisions()` raises until the next successful append. The rows are kept. Rows
without a `grid_size` are never judged.

Every other append is checked too. `ReviewDB` keeps the file's `(st_ino,
size)` as it last loaded or migrated it, or, after an append, the inode and
the append's start offset plus the bytes it wrote (not the file's size then,
which would absorb another writer's concurrent append). If the file was
absent when loaded, only an empty file, the one its own `O_CREAT` made, will
do; if it existed, it is opened without `O_CREAT`, so one deleted or moved
since is a change too. If the file is missing or the `fstat` of the
descriptor opened for the append differs, the mark raises
`review_db.ReviewLogChanged` (a `RuntimeError`) before writing anything:
`<file> changed since it was loaded; not appending (is another review or
serve using this work directory?)`. With a torn tail pending, the truncate's
own check above stands in for it; after a failed append that check has the
file's size too, cut or not, unless even `fstat` failed (then only the
inode). The truncate and migration refusals raise `ReviewLogChanged` too. The
`ReviewDB` keeps refusing until it is loaded again.

A last line with no line ending that does parse is kept; the next mark first
writes its missing line ending (`\n` after a bare `\r`, else `\r\n`). Such a
row's last field may be cut short or empty, since it is not validated:
`timestamp` in an old five-column file, `tool_version` in a nine-column one.
Under the current header the last field is `source_sha256`, which parses only
empty or whole, and `parse_log` also refuses an unterminated last row whose
`source_sha256` is empty (a crash may have cut it at the tab before its hash),
so it takes the torn-tail path above: a row written with a hash never loads
without one. A terminated row with an empty hash is kept (it was written so).
The kept row's other fields (`status`, `pass_number` and, in a nine- or
ten-column file, `reviewer`, `mode` and `grid_size`, plus `tool_version` in a
ten-column file) are complete, since they parsed or a later field followed
them. A bad line anywhere else, or a bad last line that is terminated, is
still an error.

**Migration.** A file with one of `review_db.LEGACY_HEADERS` still loads, for
readers too (`status`, read-only stores), and is never rewritten by them: the
five-column header from before the audit columns (`image_id`, `batch`,
`status`, `pass_number`, `timestamp`), and the nine-column one 0.3.0 and 0.4.0
write (`HEADER` without `source_sha256`). A writable `LocalStore`
calls `ReviewDB.migrate()` after taking the work dir lock: it writes every
loaded row, in order, under `HEADER` with the columns the old header lacks
empty, to
a temp file `.review.tsv.*.tmp` in the work directory (`mkstemp`, then
`fchmod` to the policy's `file_mode`), `fsync`s it, and `os.replace`s it over
`review.tsv`, then `fsync`s the directory (ignoring only `EINVAL`, `ENOTSUP`/
`EOPNOTSUPP` and `EBADF`, where a filesystem cannot sync directories; every
other directory sync in this tool follows the same rule). If
writing or renaming fails, the temp file is removed and the old file is left
as it was; a `LocalStore` open then fails and releases the lock (the CLI shows
`Cannot update work directory: ...` for a `changed since it was loaded`). This happens once: the result has the new
header, so later opens leave it alone. A torn tail of the old file is not
copied (it was never loaded). Migration refuses (`changed since it was loaded`)
if the file's inode or size changed since the load. `ReviewDB` refuses to
append to an old-header file that was not migrated (`RuntimeError` naming its
column count), so the file never mixes row widths. A migrated row's
`source_sha256` is always empty, never filled from the current manifest: the
source at that path may have been replaced and preprocessed again since the
verdict, and filling it would vouch for the new content.

**Undo rows.** An undo (`ReviewDB.undo_many`, see *`ReviewStore` Protocol*)
appends, in one append, one row per `image_id` of the mark it undoes, each with
`mode` `undo`, a new `timestamp`, the undoing `reviewer`, `grid_size` = the
number of `image_id`s undone, and the current `tool_version`. Where the image
had a decision before that mark, the row restores that decision's `batch`,
`status`, `pass_number` and `source_sha256` exactly (the pass may be lower than
the undone row's: a restore is exempt from the never-decreasing pass rule; the
hash is the restored verdict's, not the manifest's now). Where it had
none, the row is a **tombstone**: `status` `UNREVIEWED`, with the undone row's
`batch`, `pass_number` and `source_sha256`. Folding the log drops an `image_id` whose last row
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
| `Key` | `NewType("Key", str)`: a manifest key, the `preprocessed_path` (see *Key versus `image_id`*). A string becomes a `Key` only where it is parsed (`load_manifest`, and the server's `parse_mark`, `parse_grids` and `/image` check), so the type checker keeps keys and source paths apart |
| `ImageId` | `NewType("ImageId", str)`: a source image id, the source path or `<zip>::<entry>` (possibly PHI; it never leaves `LocalStore`). A string becomes an `ImageId` only where it is minted or parsed: preprocess discovery (with the escaped bad-name ids derived there; preprocess mints no `#icon` id), `parse_decision`, `load_manifest`, `load_skipped`, `load_notes`, and `export_rows` (a file's id with `#icon` removed, or an ambiguous `X#icon` kept as a file of its own). Server, client and controller never see one |
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
| `mark(keys, status, pass_number, *, reviewer, mode) -> dict[str, Status]` | Record a verdict given by `reviewer` (an unauthenticated claim) in `mode` (`single` or `grid`); returns the new status of every key affected, including keys that share an `image_id` with a marked key. Raises `status.GridCleanRefused`, recording nothing, for a `grid` CLEAN that `grid_clean_refused` refuses against the keys' current statuses (a key DIRTY or FLAGGED, unless every key is DIRTY); `LocalStore` enforces this itself, so every writer holds to it. `LocalStore` also raises `KeyError` for an unknown key and `ValueError` for repeated keys or a `single` mark of other than one key, recording nothing |
| `undo(pass_number, *, reviewer) -> dict[str, Status]` | Undo the latest mark not yet undone (one `mark` call: one image or a whole grid), restoring each of its images' decision from before it (see *Undo rows*); `reviewer` is checked and recorded like `mark`'s. Returns the new status, at `pass_number`, of every key affected, as `mark` does; `{}` when there is nothing to undo |
| `current_pass() -> int` | Auto-detected pass number |
| `close()`, `__enter__`, `__exit__` | Release what the store holds (`LocalStore`: the work dir lock); idempotent. Stores are context managers that close on exit |
| `skipped() -> SkippedCounts` | Counts of `failed` and `ignored` rows in preprocess's `skipped.tsv` (frozen `SkippedCounts(failed, ignored)`); zero counts if the file is absent; `ValueError` naming `file:line` if it is malformed |

### `LocalStore(work_dir, read_only=False)`

Loads `manifest.tsv` (written once by `preprocess`, so before locking); a
writable store then takes the work dir lock (see *Concurrency limits*) and
loads a `ReviewDB(work_dir, sources=export.manifest_hashes(entries))` and
calls its `migrate()` (see *`review.tsv`*), releasing the lock if either fails. `close()` releases it. A `read_only` store takes no lock and its
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
or `ignored`) and returns only the counts (zero when the file is absent, even where `export` would refuse). `export_rows()` (not part of the
`ReviewStore` Protocol: `image_id`s stay local) returns
`export.export_rows(entries, ReviewDB.decisions(), load_skipped(...), unreviewed_content(load_notes(...)))`,
`required` set for both loaders when the manifest records hashes;
see *`image-review export`*.

### Pure functions

| Function | Description |
|----------|-------------|
| `filter_rows(rows, statuses, status_filter="unreviewed", batch=None)` | Filter rows by status and optional batch: `unreviewed` selects `TODO_STATUSES` (UNREVIEWED and FLAGGED), `clean` selects CLEAN, `all` everything. `status_filter` is a `StatusFilter` (`Literal["unreviewed", "clean", "all"]`), parsed once by the CLI's `--filter` choice |
| `summary(rows, statuses)` | A `Counter[Status]` of the rows' statuses (a missing status counts 0; `.total()` is the image count) |
| `batch_summary(rows, statuses)` | The same per batch: `dict[str, Counter[Status]]` |
| `skipped_counts(rows) -> SkippedCounts` | The failed/ignored split of `SkippedRow`s; the one derivation behind `skipped()`, preprocess's summary line and `preprocess.json` counts |
| `safe_path(work_dir, relative)` | Resolve within `work_dir`; `ValueError` if it escapes |
| `review_db.parse_sha256(text, name) -> str` | `text` if it is a SHA-256 hex digest (exactly 64 lowercase hex characters), else `ValueError` naming `name`; used by `load_manifest` and `parse_decision`, which add the `file:line` |
| `export.manifest_hashes(entries) -> dict[ImageId, str]` | The `source_sha256` per `image_id` of the manifest entries that have one; `LocalStore` passes it to `ReviewDB` as `sources`, and `export_rows` uses it |
| `export.unreviewed_content(notes) -> dict[ImageId, str]` | Per `image_id`, `unreviewed embedded content: <kinds>` for its notes of kinds in `UNRENDERED_NOTE_KINDS` (all-zero `trailing-data` left out); see *Parts and folding* |
| `export.export_rows(entries, decisions, skipped, unreviewed) -> list[ExportRow]` | The export (see *`image-review export`*) from the manifest entries, the latest decision per `image_id`, the `skipped.tsv` rows and `unreviewed_content`'s mapping: one row per source file, icons (from a work directory made by an older version) folded in; an `X#icon` whose hash differs from `X`'s also gets its own row, and both are at least `NOT_REVIEWED` |
| `export.split_allowlist(rows) -> tuple[list[AllowedRow], list[ExportRow]]` | (allowed, report) in input order, every row in exactly one: allowed only if `CLEAN`, with a `source_sha256` that no row that is not `CLEAN` shares; a denied `CLEAN` row's `reason` says why |
| `export.format_allowlist(rows: list[AllowedRow]) -> str` | The allowlist as TSV text with `ALLOWLIST_HEADER` (`AllowedRow` is a `NewType` over `ExportRow` that only `split_allowlist` makes, so passing the report's rows is a type error); `ValueError` if a field holds a control character or U+2028/U+2029, or starts with `"` |
| `export.format_report(rows) -> str` | The report as TSV text with `REPORT_HEADER`; the same `ValueError` |

`load_skipped(work_dir, *, required=False)` parses `skipped.tsv` into `SkippedRow`s (empty if
absent unless `required`, which `LocalStore.export_rows` sets for a hashed manifest and which then raises `ValueError`; `ValueError` naming `file:line` if malformed).
`load_notes(work_dir, *, required)` parses `notes.tsv` into `NoteRow`s the same way (the missing-file
error says work directories made before 0.4.0 have none). Two IO helpers serve
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

`StoreUnavailable` (`LocalStore` raises none; the handling stays for a store
that can become unreachable) while loading an
image, marking, restarting a mode, or moving to the next batch is treated as a lost connection, not an
unloadable image: autoplay and the pending auto-advance are cancelled, the
status snapshot is left unchanged (a failed mark is not applied), the viewer
shows "Lost connection to server - progress saved. Press q to quit." and the
reason is logged (ERROR). The session is then `DISCONNECTED`: `_handle_event` passes no
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
DIRTY or FLAGGED key is refused (`status.grid_clean_refused`; the session
makes no store call; the status bar shows `controller.GRID_HAS_DIRTY`, also
logged), unless every key in the grid is DIRTY, which reverses that grid's own
verdict. The messages are in
[Refused CLEAN on a grid](../commands/browser.md#refused-clean-on-a-grid).

The store applies the same rule to every grid mark, so the server's
`POST /mark` answers 409 (see *Error semantics*) and a snapshot gone stale
(e.g. another client marked a key DIRTY since it was read) cannot record such
a CLEAN either.
`LocalStore.mark` raises `GridCleanRefused`; it is not a lost
connection: the session logs and shows the same refusal (for a left-out
single, `controller.IMAGE_HAS_DIRTY`), re-reads the statuses (a `StoreUnavailable` there is a lost
connection) and repaints the item's status. Nothing is recorded, so the undo
count is unchanged and there is no auto-advance. `LocalStore.mark` checks the rule
itself, against the keys' current statuses for the pass, and raises
`GridCleanRefused` before writing; the session still refuses a grid first
(without a store call), and a left-out single cannot become FLAGGED locally
(see above).

If grid mode has no items but the status filter selected rows it left out, the
session says so instead of implying the review is done: `run()` prints, and a
mode restart shows, `_held_back_message` (text in
[Todo images](../commands/browser.md#todo-images); the terminal form is in
[`image-review review`](../commands/review.md#todo-images)). Its K counts the left-out
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

When either grid dimension is zero or negative (including a window no taller
than the status bar), grid packing is suspended. The session keeps its items
and status snapshot, stops autoplay and pending advances, and accepts only
quit, fullscreen, and mode-switch input; in particular, verdicts and
navigation cannot act on pixels that are no longer visible. It does not pack
or fetch grid images while suspended. When a positive content area returns,
the session rebuilds once, follows the current item's first key when possible,
and restarts its dwell. Startup or a mode switch into grid mode at an unusable
size defers the initial build the same way, so an empty deferred list is not
treated as a genuinely empty review.

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

1. **Load**: Fetch all image bytes with `store.image_bytes_many()` (a
   simple loop for `LocalStore`); compressed
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

### `ReviewDB(work_dir, *, sources)`

In-memory dict keyed by `image_id`, backed by `review.tsv` on disk. `sources`
(required, `Mapping[ImageId, str]`) is the manifest's `source_sha256` per
`image_id`, absent for one the manifest has no hash for; `mark_many` records it
in each row it writes.

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
| `mark_many(targets, status, pass_number, *, reviewer, mode) -> list[Change]` | Record one verdict on every `(image_id, batch)` in `targets` (same timestamp; `grid_size` = `len(targets)`; `tool_version` = `package_version()`; `source_sha256` = `sources.get(image_id, "")`); returns each row written with the decision it replaced |
| `undo_many(changes, *, reviewer)` | Append the undo rows for one `mark_many` result in one append (see *Undo rows*); an `image_id` listed twice (keys sharing it) gets one row |
| `migrate()` | Rewrite a file with one of `LEGACY_HEADERS` (five or nine columns) with `HEADER`, once, the new columns empty (see *`review.tsv`*); no-op otherwise |
| `get_status(image_id, current_pass) -> Status` | Pass-aware status (see *Pass Logic*); a stale latest row reads `UNREVIEWED` |
| `decisions() -> dict[str, Decision]` | A copy of the latest decision per `image_id` (never a tombstone), stale or not (export judges staleness itself, to explain it), for `export`; `ValueError` if `review.tsv` ended in a torn line, or its last mark was cut short, when loaded (not yet cleared by an append) |
| `current_pass(image_ids) -> int` | Auto-detect pass number |

### Pass Logic

**Stale rows.** A latest row is stale when `Decision.applies_to(sources.get(image_id))`
is false: its `source_sha256` is not empty, the manifest has a hash for the
`image_id`, and they differ (a verdict on content the file no longer has).
`get_status` and `current_pass` read through `_current(image_id)`, which gives
`None` for no row or a stale one, so a stale row counts as no row. A row with an
empty hash (recorded before the column, or migrated), and any row when
`sources` has no entry for its `image_id` (a 3-column manifest), is trusted by
`image_id`. `mark_many` and `undo_many` use the raw latest row: the recorded
pass is `max(raw pass_number, requested pass)`, and an undo restores the stale
row exactly, so the image reads `UNREVIEWED` again.

`get_status(image_id, pass)` maps the image's current row to a `Status` as the
table in [Passes](../commands/browser.md#passes) gives: no row (or a stale one)
is UNREVIEWED; a row with `pass_number >= pass`, or a CLEAN one, keeps its
verdict; an earlier-pass DIRTY is FLAGGED.

`mark_many` stores `max(existing pass_number, requested pass)` for each image,
so an image's recorded pass never decreases (its verdict and timestamp still
update). In a view at a lower pass a later-pass DIRTY image therefore reads
DIRTY, not FLAGGED, and is not grid-eligible. `ReviewStore.mark` returns
statuses as seen at the requested pass.

`current_pass(image_ids)` returns 1 if any of `image_ids` has no row, or only a
stale one.
Otherwise it returns `max(pass_number)` if any image's status at that pass is
in `TODO_STATUSES` (UNREVIEWED or FLAGGED), or `max(pass_number) + 1` if the
pass is fully complete, never more than `connection.MAX_PASS` (1000000).

## Shared limits and parsers (`connection.py`)

Stdlib only; used by the CLI, the server and the review database.

- `MAX_PASS` (1000000): the highest pass number accepted anywhere;
  `current_pass()` never answers above it.
- `parse_reviewer(value)`: a reviewer name, 1-`MAX_REVIEWER_LENGTH` (64)
  characters, all `str.isprintable()` (no tab, newline or other control
  character), not all whitespace; `ValueError` otherwise.
- `parse_token(raw) -> Token`: an operator-supplied access token, 22-256
  characters from `[A-Za-z0-9_-]`; surrounding whitespace is refused, not
  stripped. Its `ValueError` never holds the value. `Token` is a `NewType`
  over `str` for a token that passed it (or was generated).
- `is_int_at_least(value, minimum)`: a `TypeGuard` for a JSON integer (never
  a `bool`) of at least `minimum`.
- `package_version()`: the installed `image-review` version, or `"unknown"`
  when it is not installed as a package; cached for the process. It is
  recorded in `review.tsv` and `preprocess.json`.

## Server (`server.py`)

The threat model these sections implement is summarized in [the security model](security-model.md).

`make_unix_server(store, path, token=None) -> (UnixReviewServer, token)`
creates the listening server (see *Unix-socket server*); it leaves no socket
file behind when it raises. `make_loopback_server(store, port=0, token=None)
-> (LoopbackReviewServer, token)` creates the same server on loopback TCP (see
*Loopback server*).
`ReviewServer` is a `ThreadingHTTPServer` (daemon threads) holding the store,
the token, a store lock, and the sets of known keys and batches taken from the
manifest at start. It runs at most 64 connections at once, each with a 60 s
deadline per request ([Headers and connection
handling](#headers-and-connection-handling)). It, with its handler
`ReviewHandler`, also carries everything the browser page needs: the public
page files, the `Host` and `Origin` checks, the security headers, the
instance id and `POST /grids` (all described under *Unix-socket server*, and
the same on both servers). Two subclasses bind it: `LoopbackReviewServer`
binds `127.0.0.1:port` over TCP, and `UnixReviewServer` (with
`UnixReviewHandler`, which names the peer `unix` and leaves `TCP_NODELAY`
unset) adds the Unix socket
file: its bind, mode and removal. `ReviewServer` sets `SO_REUSEADDR`
(`allow_reuse_address`) everywhere but Windows, so a restart on the same port
need not wait out `TIME_WAIT`; a port another socket is listening on is
still refused. On Windows the option would let another process bind a port
already in use, so it stays off there. `SO_REUSEPORT` is never set
(`allow_reuse_port = False`). `UnixReviewServer` keeps `allow_reuse_address
= False`, which means nothing for a socket file.
`ReviewServer.host_port` is the bound TCP port (the real one when 0 was
asked for), or None on a Unix socket; the `Host` check uses it.

### Authentication

Every request must carry `Authorization: Bearer <token>`, checked with
`hmac.compare_digest` before any routing; otherwise 401 and the connection is
closed (any request body is left unread). The one exception is the four
static files of the page, served without it (see Unix-socket server). The
`Host` and `Origin` checks come first: a request failing either is 400
whether or not it carries the token.

### Endpoints

All responses are JSON unless noted. Requests are parsed into typed values at
the boundary (`parse_pass`, `parse_mark`, `parse_undo`, `parse_grids`).

| Request | Response |
|---------|----------|
| `GET /manifest` | `[{"key": str, "batch": str}, ...]` |
| `GET /image?key=K` | `image/jpeg` bytes; 404 if the key is unknown (checked against the manifest's keys before the store is asked), unreadable, or does not match its recorded `jpeg_sha256` |
| `GET /statuses?pass=N` | `{key: "CLEAN"\|"DIRTY"\|"UNREVIEWED"\|"FLAGGED", ...}` for every key; `N` an integer from 1 to `connection.MAX_PASS` (1000000) |
| `GET /current_pass` | `{"pass": N}` |
| `POST /mark` | Body `{"keys": [str, ...], "status": "CLEAN"\|"DIRTY", "pass": N, "reviewer": str, "mode": "single"\|"grid"}`; `reviewer` is checked with `connection.parse_reviewer` (1-64 printable characters, no tab, newline or other control character, not all whitespace) and recorded as the client's unauthenticated claim; other fields are ignored; `keys` must be distinct, and a `single` mark must name exactly one key (otherwise 400, nothing recorded); responds `{key: status, ...}` for every key affected (as `ReviewStore.mark`). A `grid` CLEAN that the store refuses with `GridCleanRefused` (`status.grid_clean_refused` against its current statuses for `pass`: a key DIRTY or FLAGGED, unless every key is DIRTY) is answered 409 and records nothing; `single` marks and DIRTY verdicts are never refused with 409 |
| `POST /undo` | Body `{"pass": N, "reviewer": str}`, checked as for `/mark`; other fields are ignored. Undoes the server store's latest mark (`ReviewStore.undo`); responds `{key: status, ...}` for every key affected, or `{}` when there is nothing to undo |
| `POST /grids` | Grid layouts for the browser page; see *Unix-socket server* |

Only keys appear on the wire; original `image_id`s never do.

**No API version.** The page and the server ship together: `serve` serves
the page itself, so there is no wire version to negotiate. A page that
outlives the `serve` run it was loaded from (a tab left open across a
restart) is refused with 412 by the `X-Review-Instance` check (see
*Unix-socket server*), recording nothing, until it reconnects.

### Error semantics

| Status | Cause |
|--------|-------|
| 400 | Bad request: a `Host` or `Origin` header the server does not accept (before the token check; see *Unix-socket server* and *Loopback server*); `Transfer-Encoding` present; a body on anything but `POST /mark`, `POST /undo` and `POST /grids`; a query parameter appearing more than once; `/image` without exactly one `key`; a missing or invalid `pass` (not an integer from 1 to `MAX_PASS`, 1000000) on `/statuses`; `/mark` or `/undo` with a missing, repeated or oversized (> 1 MiB) `Content-Length`, invalid JSON, non-object body, a missing or invalid `pass` or `reviewer`; `/mark` with empty or non-string `keys`, an unknown key, a status other than CLEAN/DIRTY, a `mode` other than single/grid, repeated keys, a `single` mark without exactly one key; `/grids` as described under *Unix-socket server* |
| 401 | Missing or wrong token |
| 404 | Unknown path, or HEAD/PUT/DELETE/PATCH/OPTIONS (closes the connection); other methods get the stdlib 501 before authentication; unknown or unreadable image key |
| 409 | `POST /mark`: CLEAN with `mode: "grid"` on keys holding a DIRTY or FLAGGED status in that pass, unless every key is DIRTY (`status.grid_clean_refused`); body `{"error": "grid holds a DIRTY or FLAGGED image"}` (no keys); nothing is recorded |
| 412 | An API request other than `GET /current_pass` without exactly one `X-Review-Instance` header equal to this server run's (see *Unix-socket server*); body `{"error": "the page was loaded from another serve; reconnect"}`; nothing is read, recorded or packed |
| 500 | Any unexpected store failure; only the exception class name is logged |
| 503 | `POST /grids` while another `/grids` request is being computed; retry later |

400, 401, 412, 500 and unknown-route 404 replies send `Connection: close`,
because a request body may be unread, and so does a public page file (socket
mode) requested without the token, so a tokenless connection lasts one request; an image 404, a `/mark` 409 and a
`/grids` 503 (sent after the body is read) keep the connection open.

The 409 check runs under the store lock, so no other mark can land between
it and the write. It reads `store.statuses(pass)` for the request's pass,
which builds the status of every manifest key in memory: O(manifest size)
once per grid CLEAN, small beside the synced append the mark itself does.

### Headers and connection handling

Every response, including stdlib error pages, carries `Cache-Control:
no-store` and `X-Content-Type-Options: nosniff`. The server speaks HTTP/1.1
with keep-alive.
Each request (request line, headers and body) must be read within 60 s of the
server starting to wait for its request line, so time a keep-alive connection
spends idle counts toward its next request; past that the connection is closed
with no reply and no log line. The request line and headers may total at most
64 KiB (bytes received, counted per request); a body adds its `Content-Length`
(at most 1 MiB) once the token is checked. Over that, too, the connection is
closed with no reply and no log line. A store failure that is itself a timeout
(e.g. `ETIMEDOUT` on NFS) is still a 500. Each receive and each send also has its own 60 s
timeout, so a slow image download is bounded per write, not by the request
deadline. At most 64 connections are open at once: the next one is
closed at `accept()`, before it gets a thread, and a WARNING `connection limit
(64) reached; refusing connections until one closes` is logged once each time
the server fills.

### Concurrency limits

Each writer holds its own in-memory copy of the review state and appends to
`review.tsv` from it, so two writers on one work directory would each work from
a stale view of the other's marks: a grid CLEAN decided from that view could
land after the other writer's DIRTY for the same image and, as the last row,
put it in the allowlist. Worse, a writer that loaded a torn tail truncates the
file back to that offset before its next append, which would cut away every
row the other writer appended since. A writable `LocalStore` (used by `review`
and `serve`) therefore holds `work_dir/review.lock`, described below.

Two writers can still happen: `review.lock` deleted by hand while its writer
runs, containers that share a hostname but not a pid namespace, or the
reclaim's `stat`-to-`unlink` gap (below). As a backstop, every append first
checks that `review.tsv` still has the inode and size this writer last saw
(see *`review.tsv`*), so the second writer's next mark fails with
`ReviewLogChanged` instead of landing. `review` then exits 1; `serve` answers
that mark, and every later one, with a 500 (logged as `internal error:
ReviewLogChanged`) until it is restarted. The check and the write are not
atomic, so an append by the other writer inside that window can land along
with this one, once: each writer then records the size it expects as its own
append's end, not the file's, so the next mark of each is refused. The lock
works as follows:

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
  half-written. Afterwards the directory is `fsync`ed (on each path below),
  best effort: a failure there, such as `EIO` or a directory that can be
  written but not read, is logged as a warning and the file stays. If `link` reports an error but
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
- `serve` turns SIGINT, SIGTERM and SIGHUP into `KeyboardInterrupt` before
  taking the lock (`interrupt_once`: the first one raises and the others are
  ignored until the block exits, so cleanup is not cut short), and closes the
  store last, while holding the server's store lock, so a mark in progress
  finishes first and later ones fail (500). `review` does the same for SIGINT
  and SIGHUP (lost terminal or ssh session) while the store is open; SDL
  leaves both alone, and turns SIGTERM into a quit event.

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
Two other records exist: WARNING `connection error: <ExceptionClass>` for
connection-level failures, and ERROR `internal error:
<ExceptionClass>` for 500s. On the Unix-socket server the peer is the
literal `unix` (`accept` gives no peer address there); on the loopback server
it is `127.0.0.1`.

### Unix-socket server

`make_unix_server(store, path, token=None) -> (UnixReviewServer, token)`
(a given, parsed `Token` is used as is; None generates `token_urlsafe(16)`) serves the
API as plain HTTP on a Unix domain socket, for a browser reaching it through
`ssh -L PORT:PATH node`. TLS is not used; ssh provides the transport. The path
is made absolute and must fit `sun_path` (108 bytes, 104 on macOS) and contain
no `:` or control characters (`parse_socket_path`). A stale socket file of the
user's with no listener is replaced; a live listener, another user's file or a
non-socket is refused with `ValueError` (`clear_stale_socket`). The socket is
chmodded 0600 before `listen()` (until then every connect is refused), and
`server_close` unlinks it only if it is still the same file. Before the token
check, the single `Host` header must be `localhost`, `127.0.0.1` or `[::1]`
with an optional port of 1-5 digits in 1-65535 (`is_local_host(values)`; any
port, since the forward's local port is the reviewer's choice); anything else
(including a missing or repeated header) is 400 and the connection closed, a
defence against DNS rebinding. Then the `Origin` header (`is_same_origin`,
on both servers): a request with none passes. Otherwise there must be exactly
one, and it must be the literal `http://` followed by the request's `Host`
value (which has just passed the `Host` check), the host part compared ignoring
ASCII case:
the page's own origin. Anything else (`null`, `https://`, another host,
another local port such as `http://localhost:8888` against `Host:
localhost:8080`, a path, a repeated header) is 400 and the connection closed,
the public files included. The page never fails this: browsers send no
`Origin` on its navigations, `<script>` and stylesheet loads and same-origin
GET fetches, and its own origin on its `fetch` POSTs (fetch's default mode
`cors`, so `Referrer-Policy: no-referrer` does not turn it into `null`;
`TestWebFiles` checks that the page sets no `mode` or `referrerPolicy`).
Requests without an `Origin` header are not covered by this check: another
site's `<script>`, `<img>` and navigations send none, so they can still fetch
the four public files, and get 401 on the API.

Four static files are served without the token: `GET /` (`index.html`),
`/helpers.js`, `/app.js` and `/app.css`, from `web/` in the package, read once by
`load_assets()` at startup so no request path touches the filesystem. They
hold no PHI and no secret, and the page cannot send the token before it has
loaded. Only these exact GET paths (a query string is ignored) are public;
`POST /`, `HEAD /`, `/index.html` and every other
path fall through to the token check, and the usual framing checks (no
`Transfer-Encoding`, no request body) apply first. CSRF does not apply,
because browsers never attach an `Authorization` header on their own. The
page takes the token from the URL fragment, which is never sent to the
server. Every response carries `Content-Security-Policy:
default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self';
img-src blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`
and `Referrer-Policy: no-referrer`, besides `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff`.

Each `ReviewServer` start (of either kind) draws an instance id, `token_urlsafe(16)`. It
names the server run and is not an access secret, but it is never logged.
Every response, stdlib error pages and the public files included, carries it
as `X-Review-Instance`. Every API request except `GET /current_pass` (where
the page's first load and its Reconnect start)
must carry exactly one `X-Review-Instance` request header equal to it;
otherwise the server answers 412 with `{"error": "the page was loaded from
another serve; reconnect"}`, closes the connection, and reads, records and
packs nothing (an unknown route is 412 too, not 404). The check runs after
the `Host` check and the token check, so a client without the token gets the
same 400 or 401 as before and learns nothing from a 412; the public files
need no header. Why the check exists (a tab and its token outliving a
restarted serve, with keys that repeat across work directories) is in the
[security model](security-model.md#browser-review-over-a-unix-socket),
under "A page belongs to one serve".

`POST /grids` (`ReviewHandler._grids`) lays out images into grids for
the browser client. The body, parsed by `parse_grids` into a `GridsRequest`, is `{"keys": [str, ...],
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

The packing rule: `_grids` reads each key's size from its JPEG header
(`image_size`, which uses `layout.jpeg_size`; no pixels are decoded) and
passes the sizes, keyed by their index in the request, to `layout.plan_grids`.
That sizes each image with `layout.fit_size`: with rotation allowed it tries
the grid both ways, `(W, H)` and `(H, W)`, and keeps the image at its size if
it fits either (a 1000x300 image in a 400x1200 grid stays 1000x300);
otherwise it shrinks the image, keeping its aspect ratio, by the larger of the
two orientations' scales, each side floored as `max(1, min(bound,
int(side * scale)))`. Without rotation only `(W, H)` is tried. An image is
never enlarged. `plan_grids` then adds the rects
to rectpack in request order, into as many `W` x `H` bins as it needs.
`never` packs without rotation and `always` with it; `auto` packs both ways
and keeps the rotated packing only if it needs strictly fewer bins.
`grids_payload` then builds the reply from the `GridPlan`: placements as
rectpack placed them, `rotated` from `layout.is_rotated`, and `left_out`
from the keys with no size plus `GridPlan.unpacked`.

So a batch always gets the same grids, given two preconditions on the page.
First, key order matters: rectpack's area sort is stable, so images of
equal area can swap places or grids when the order changes. The page must
send a batch's grid-eligible keys in manifest order to get the same grids
every time. Second, a batch with more grid-eligible keys than
`MAX_GRID_KEYS` cannot use `/grids` (preprocess `--batch-size` is
unbounded): the page must refuse grid mode for that batch rather than split
it, since chunks would pack differently. The server never decodes the
images, so one whose header reads but whose pixels do not decode is still
placed; the page catches it when it draws the grid (below).

The server reads each image's bytes one at a time, never holding the store
lock, and keeps each successfully read header size in memory for the life of
the server (failures are retried on the next request). `source` and
`left_out` therefore reflect each file's first successful read during this
server run: a file replaced or deleted later keeps its old size and place.
The client contract covers this: the page must check that each decoded
image's natural size equals its `source`, and treat a mismatch or any
`/image` failure as left out, never covered by a grid verdict.

One `/grids` request is computed at a time; another arriving meanwhile gets
503 with the connection kept open, meaning retry later. Packing is not
cancelled when the client disconnects, so a reloaded page may see 503 until
the earlier request finishes. Nothing about individual keys is logged.

`default_socket_path()` is `~/.image-review/serve-<short-host>-<pid>.sock`.
When that path would be `SUN_PATH_SIZE` bytes or more, the host part is replaced
by the first 8 hex characters of the SHA-256 of the short host name (not a
truncation, since node names often differ only at the end). If even that does
not fit, it is returned anyway and `parse_socket_path` raises its usual error.

### Loopback server

`make_loopback_server(store, port=0, token=None) -> (LoopbackReviewServer,
token)` (the token as for `make_unix_server`) serves the same page, handler
and API as plain HTTP on TCP, bound to `LOOPBACK_HOST` (`127.0.0.1`) only:
never `::1` or a wildcard address, so nothing listens on another interface.
`port` must be 0 (a free port; `server.host_port` gives the bound one) or
1024-65535, else `ValueError`: no privileged port, and never 80, where
browsers leave the port out of `Host` and `Origin`. A bind failure (the port
in use, say) raises `OSError`. `LoopbackReviewServer.server_bind` skips
`HTTPServer.server_bind`, whose `socket.getfqdn` is a reverse DNS lookup that
can stall startup on an offline machine; `server_name` is `127.0.0.1`. The
handler turns on `TCP_NODELAY`
(`disable_nagle_algorithm`): otherwise Nagle and delayed ACK hold each reply's
body about 40 ms behind its headers on a keep-alive connection
(`UnixReviewHandler` turns it off, as `AF_UNIX` refuses the option).

The only URL is `http://127.0.0.1:PORT/`. The `Host` rule is port-aware
here (`is_local_host(values, port)`): exactly one `Host` header,
`127.0.0.1:PORT`, with `PORT` exactly the bound port written as the decimal
number. `localhost` is refused: browsers try `::1` first for it, and this
server does not bind `[::1]:PORT`, so another local user could, and a
`http://localhost:PORT/#token` URL would hand them the token; refusing it
makes such a URL fail at once with 400 rather than work until someone takes
the port. `[::1]` is refused for the same reason, as are a missing port,
another port and any other host: 400 and the connection closed. With this
`Host` rule the `Origin` rule allows none or exactly
`http://127.0.0.1:PORT`. Everything else about requests (token, public
files, headers, instance id, limits, logging apart from the peer) is as on
the Unix-socket server.

### Browser client

`web/app.js` (plain ES2020, no build step) reviews single images and grids,
with the page's pure helpers (no DOM) in `web/helpers.js`, loaded before it.
Its keys, screens and messages as the reviewer sees them, and the review
rules they follow (passes, todo images, refused CLEAN on a grid, unloadable
images, undo, recorded verdicts), are on
[The browser page](../commands/browser.md). It obeys the CSP above:
no inline script or style, no `innerHTML`, no external URLs; all text goes
in with `textContent`. Both scripts are type-checked strictly with `tsc` (`npm run
typecheck`; see CONTRIBUTING).

`m` (rotation `auto`) and `M` (`never`) switch to grid mode and `s` back to
single mode; each switch, and `f` (see *Re-check filter*), rereads `/statuses`,
empties the stack of marked keys and rebuilds the list. It reviews one batch:
`m`/`M` stay on the current batch while it has keys to pack (`gridBatches`),
else take the first in sorted order (by code point, as Python sorts) that
has; `b` moves to the next that has a todo one (`gridTodoBatches`; the same
batches under the `todo` filter), wrapping (`nextBatch`, which goes round
once back to the current batch itself). A key is packed when `isGridRow`
accepts its status: listed by the filter (`inFilter`) and in `GRID_ELIGIBLE`
(UNREVIEWED or CLEAN), under the `todo` filter UNREVIEWED, since a grid
verdict covers every image in it and one keypress must never clear an image
already judged DIRTY (this pass) or FLAGGED (DIRTY in an earlier one). The
batch's keys to pack go to `POST /grids` in manifest order at the stage's size in device pixels,
as the *Unix-socket server* contract requires; a batch over `MAX_GRID_KEYS`,
a stage under 256 device pixels or a batch with none shows a message instead
(for none, `showEnd`: the end-of-pass screen, or `elsewhereHint`, which is
the held-back message `heldBackMessage` when no batch has todo grid items, its K
being `heldBackCount` over every batch: the todo keys the filter lists that
are not in `GRID_ELIGIBLE`, under the `todo` filter the pass's FLAGGED keys). One
`/grids` request is in flight at a time (a newer build waits for it, and a
stale reply is dropped); a 503 shows "Server busy computing grids; retrying"
and is retried every 2 s while the layout is still wanted. The last layout
alone is cached (`gridCache`), keyed by the batch, the keys sent in order,
the grid size and the rotation, so a rebuild with nothing changed (e.g. `s`
then `m` with nothing marked) reuses it, and a mark that changes which keys
are packed misses it; a failed `/grids` leaves no cached layout. It is kept
across `s`/`m`/`M`; `b` and Reconnect drop it. Items come from
`orderGridItems`: the grids shuffled, then (stably) the fullest first, then
each `left_out` key as a single item; reshuffled on every build. Each grid is drawn on a `<canvas>` at one canvas pixel per
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
`mode: "grid"`: the display mode, not the item's kind. `c`/`d` on a grid count
only while its canvas is shown with every key drawn and its dwell is over:
never while it is being drawn or redrawn, hidden by a resize repack, or
after `contextlost`. CLEAN on a grid holding a DIRTY or FLAGGED image
(`gridCleanRefused`, as the server's `status.grid_clean_refused`: unless
every image is DIRTY, which reverses the grid's own verdict) is refused
with "grid contains an image already marked DIRTY - review it in single
mode" and no request; DIRTY is always allowed. If the page's statuses were
stale and the server refuses the CLEAN instead (409), the page shows the same
message (for a left-out single that became FLAGGED, which the server refuses
as a grid mark: "image already marked DIRTY in another pass - review it in
single mode"), rereads `/statuses` into its status map, pushes nothing on the
stack of marked keys and stays on the item. A grid's status (`gridStatus`)
is DIRTY when any key is DIRTY or FLAGGED, else UNREVIEWED while any key is
UNREVIEWED, else CLEAN; under the `todo` filter an item is todo when its
status is UNREVIEWED or FLAGGED (`isTodo`, through `isItemTodo`), so a grid
holding such an image is not todo and the move after a mark skips it, while a
partly undone grid is todo again. A resize
repack empties the stack of marked keys at once (the old items are gone, and
a grid marked DIRTY is not among the new ones, so `z` then says "Nothing to
undo"); a repack due
while a `/mark` or `/undo` is in flight waits for its reply, and a mark
whose reply arrives after a resize hid the grids is not pushed on the stack
and moves nowhere. On the end (or stop) screen a resize does not repack
(`onResize` skips it there), so `z` still undoes there; leaving
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
  mode ("Single", "Grid · auto" or "Grid · never", with "· re-check: CLEAN"
  or "· re-check: all" after it under those filters), the progress and the
  item's batch and key (in grid mode "grid (N images)"). Empty, the message
  stays rendered (its live region kept) but out of the flow and unseen. The
  reviewer's own moves empty the message: Left/Right, a mark moving on, a mode
  switch (`m`, `M`, `s`, `b`, `f`). An undo, a Reconnect and startup set their own
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
  up. While one is up, `c`, `d`, `z`, Left/Right, `m`/`M`, `s`, `b`, `f`, `q`
  and `r` do nothing, every review button (Done and Reconnect included) and the
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
  filter is `todo` (it is never stored), so the list is the keys whose status
  is UNREVIEWED or FLAGGED, shuffled (Fisher-Yates); keys stay in it after
  they are marked. The bar shows the
  progress as "Pass N · K / T left" (K todo of the T listed; in grid mode
  "Pass N · BATCH (k/B) · K / T left"); see *Layout*.
- **Images**: `GET /image?key=K`. The reply must be `image/jpeg` and end in
  `FF D9` (a truncated body is refused); it becomes a blob URL in an `<img>`,
  awaited with `decode()`, and the previous blob URL is revoked. Any failure
  but a 401 or a network error shows the placeholder "Cannot load image:
  KEY", which can be marked DIRTY but never CLEAN ("cannot mark CLEAN: image
  could not be loaded", no request; a grid counts only once every key is
  drawn, so this applies to single items). A single item is fetched each
  time it is shown, so a key that loads again is shown normally.
- **Scale**: after each paint, on every window resize (which includes a
  browser zoom) and whenever a `ResizeObserver` sees the stage (which the
  `<img>` fills) change, the page computes the display scale, `min(width /
  naturalWidth, height / naturalHeight) * devicePixelRatio` with the box from
  `getBoundingClientRect()` (screen pixels per image pixel under
  `object-fit: contain`), and shows it in the bar as an integer percent,
  `floor(scale * 100 + 1e-9)` (truncated, so a scale just under 1 never reads
  100%; the epsilon keeps float error from dropping a point). Below 100% it
  reads "⚠ 46%" in a badge with the bar's colours inverted (class `low`), so
  it stands out on every tint: small burned-in text can be lost when an image
  is scaled down. A scale that leaves less than
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
  page's stack of marked keys (and, under `clean` or `all`, added to the
  re-checked set) and the reply updates the status map. The
  marked item then stays up for `MARK_FLASH_MS` (200 ms) with the
  bar in its new status (colour and word; a grid's from `gridStatus`), and the
  page is busy meanwhile: no verdict, move, undo, mode switch, `q` or `r`
  acts. Then it empties the status message (so a run of `c` and `d` keeps the
  mode and progress in view) and moves to the next todo item after the current
  one, wrapping round, or to the end screen. A stop (lost connection, 401,
  412), a resize repack or a context loss during those 200 ms drops the move
  on (a context loss redraws the marked grid instead). A mark whose items a
  resize repack dropped while it was in flight does not move and says "Marked
  STATUS: KEY" (or "grid of N images"). With none left, the end screen says
  what is next. Under `clean` or `all`, once no row the filter lists is todo
  (`anyTodo`), it is "All batches done for pass N (filter: F) - press [f] to
  change the filter", or, when the pass is done too, "All batches done for
  pass N (filter: F)." and the end-of-pass hint and Reconnect below; when the
  filter lists no manifest row at all, "No images to review for pass N
  (filter: F)" (the viewer's wording) in place of "All batches done". Under
  `todo`, only when no manifest row of the pass is todo (UNREVIEWED or
  FLAGGED) is it the end of the pass: "Pass N: nothing left to review. Stop
  serve (Ctrl-C), start the next one, then press Reconnect (r)." with the
  Reconnect button, offered while no request is in flight and the page has not
  stopped (a stop takes the hint off the stage; see *Errors*). Otherwise
  (`elsewhereHint`): in grid mode (whose list is one batch) "No todo images
  remaining - [b] next batch" while any batch has todo grid items, else the
  pass-wide "No grid items for pass N; K FLAGGED/DIRTY images need single-mode
  review - press [s]"; in single mode (whose list is the whole pass, so only
  when other clients changed statuses meanwhile) "No todo images remaining -
  press [s] to reload the list". Entering grid mode, or a repack, with nothing
  to pack shows the same screen. A 200 whose body cannot be parsed (on `/mark`
  or `/undo`) still counts, since the server has acted: the page rereads
  `/statuses` instead, and stops with "unexpected reply from the server;
  reload the page" if that fails too.
- **Navigation**: Left/Right (and buttons) step through the list without
  marking. A step past either end shows the stop screen (`showListEnd`) on
  the stage: a red octagonal stop sign (inline SVG,
  coloured by the CSS) over "End of the list", "→ first item · ← last item"
  and the list's todo count, "K todo left in this list" (grid mode: "K todo
  left in this batch - [b] next batch"), or, with none left in the list, the
  end screen's hint (e.g. "No todo images remaining - [b] next batch" or the
  held-back "No grid items for pass N; …", see *Marking*). No item
  is current there: the bar is neutral, no dwell runs, and `c`/`d` and their
  buttons do nothing; `z`, `m`/`M`, `s`, `b`, `f`, `q`, `?`/`h` act as on any
  screen, and in grid mode a resize does not repack, as on the end screen. A
  stop (lost connection, rejected token, `q`) takes the stop sign off. A
  further step goes on round: Right to the first item, Left to the last,
  whichever end it was reached from, with a fresh dwell. The end screen
  behaves the same way under Left/Right. Stepping onto the stop screen is a
  move, so it empties the status message. Once no row the filter lists is
  todo (`filterDone`; under `todo`, no todo row left in the pass), a step
  past either end shows the end screen instead of the stop sign: under
  `todo` the end-of-pass screen (with Reconnect), since the next serve is
  due; under `clean` or `all` the "All batches done … (filter: F)" screen
  (see *Marking*), which is the end-of-pass screen, "… (filter: F). Stop
  serve…" with Reconnect, only when the pass is done too. Left/Right go
  round from it too. A list with no items keeps its own screen, and the arrows do
  nothing.
- **Re-check filter**: `state.filter` (`Filter`: `todo`, `clean` or `all`)
  picks the keys the list holds (`inFilter`, as `store.filter_rows`): the
  todo ones, the CLEAN ones, or every key; grid mode packs those in
  `GRID_ELIGIBLE` (`isGridRow`). `f` takes the next (`nextFilter`: todo,
  clean, all, todo) as a mode switch does (rereads `/statuses`, empties the
  stack of marked keys, rebuilds the list; in grid mode the current batch is
  packed again while it has keys to pack) and empties `state.rechecked`, the
  keys marked under the current `clean` or `all` filter less those undone.
  That set is kept across `s`, `m`, `M`, `b` and a Reconnect that the same
  serve answers (the same `X-Review-Instance`), forgotten when `loadReview`
  sees another instance (its keys may name other images), and dropped by
  `q`. Under `clean` or `all` a key is todo while UNREVIEWED or FLAGGED or
  until re-checked (`isKeyTodo`, as the viewer's `_key_todo`), and an item
  while any of its keys is (`isItemTodo`); the progress, the move after a
  mark, the stop screen's count, `b`, the end screen and `heldBackCount`
  follow that. A grid build lands on the grid holding its landing key (a
  resize repack's or a Reconnect's), todo or not; with none (`m`, `M`, `b`
  and `f` pass none), and in single mode, the list lands on the first todo
  item (`firstTodoIndex`), or on the end screen when no item is todo; under
  `todo` every item is todo when the list is built, so that is the first
  item. The frozen controller tables in `tests/web/grid-rules.json`
  (each filter, with nothing or `k0` and `k2` re-checked) are the oracle for
  `gridKeys`, `gridBatches`, `heldBackCount` and `heldBackMessage`. While
  the filter is not `todo` the bar's mode reads "… · re-check: CLEAN" or
  "… · re-check: all" (within `#mode`, so the bar's layout is unchanged).
  Verdicts go to the current pass and the grid CLEAN rule is unchanged. The
  user-facing rules are in "Re-checking CLEAN images" on
  [The browser page](../commands/browser.md#re-checking-clean-images).
- **Undo**: `z` (and a button), also on the end and stop screens. With an
  empty stack it says "Nothing to undo" without a request. Otherwise
  `POST /undo`; `{}` empties the stack and says "Nothing to undo". The keys
  the reply names (after a resync, the entry's) leave the re-checked set. If the
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
  Shift state rather than the letter's case, so Caps Lock is safe; `f` (the
  next filter, see *Re-check filter*); and
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
  state). Pending image byte reads, image decodes and grid decodes are also
  invalidated: they cannot show an image, start a dwell, draw a grid or replace
  the stop or Reconnect message, and any URL or bitmap they produce is
  released. The image or grid already on screen stays visible at the stop;
  its dwell is cleared. A late failure cannot replace "token rejected" or
  stop a reconnected page. Reconnect stops the page first, as a loss does
  (at the end of the pass too), hides the button, says
  "Reconnecting...", takes the item off screen (its dwell with it), empties
  the stack of marked keys (the server may have restarted, or others marked
  meanwhile) and reloads what startup loads with the stored token and
  reviewer name. Startup and Reconnect read `/current_pass` first and keep
  the `X-Review-Instance` of its reply, which every later request sends (a
  reply without it is an unexpected reply); until that reload is in, every
  control is busy, so no key or button sends a request. It then rebuilds
  the current mode under the current filter, keeping its re-checked set
  when the same serve answers and forgetting it for another (see *Re-check
  filter*): single mode's list, reshuffled, from its first todo item (else
  the end screen); in grid mode the
  current batch and rotation (as `m` keeps the batch), laid out afresh by
  `/grids` and landing on the
  grid holding the previous item's first key (kept across a failed
  Reconnect, and the key a pending repack would have landed on), else the
  first todo item; with no todo item, the end screen. Every item then starts a fresh dwell. The status message says
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
  saved), starts the next `serve` on the same socket path and token
  (for the next batch or pass), and presses Reconnect in the same tab. It
  stops the page as a lost connection does, so every request in flight
  turns stale and nothing more is sent. It leaves grid mode (canvas sized to
  0, hidden), revokes the image's blob URL and clears the `<img>`, and drops
  the pass, manifest, statuses, items, marked keys, re-checked set and cached
  layout, but keeps the token and the reviewer name (in `sessionStorage` and
  in memory) and the mode, rotation and filter; the batch and the landing key are forgotten
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

## ssh command parts (`tunnel.py`)

Validates the two values `serve` puts into the ssh command it prints (see
`serve`), which the operator pastes into a shell. Both raise `ValueError` with
a message that names the rule.

**`parse_via(raw)`**: the login node for `-J` (`user@host`, `host` or a
config alias). Empty, leading `-` (option injection) and any character
outside `[A-Za-z0-9._@%:\[\]/-]` are rejected.

**`parse_ssh_host(raw)`**: the node name for `--ssh-host` (hostname, IPv4 or
bracketed IPv6), with no user part. Empty, leading `-`, any `@` (the user
name is added by `serve`) and any character outside `[A-Za-z0-9._:\[\]-]`
are rejected.

## Multi-Pass Review Workflow

The workflow (grid triage in pass 1, single-image review of FLAGGED images in
later passes, resuming, then `export`) is in
[Multi-pass workflow](../tutorials/browser-review.md#multi-pass-workflow); what
each pass shows is in [Passes](../commands/browser.md#passes).
