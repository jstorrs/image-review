# Changelog

## 0.3.0

Remote review over HTTPS, an export of the final result, an audit trail in
`review.tsv`, undo, and a much stricter `preprocess`. Read **Upgrading** first
if you share work directories with other people.

### Upgrading: breaking and behaviour changes

- **Upgrade everyone sharing a work directory together.** The first `review`
  or `serve` on an old work directory rewrites `review.tsv` in a format 0.2.0
  cannot read, and an undo (`z`) adds rows that any version before undo
  rejects.
- **`preprocess` refuses a work directory that has content.** An empty one is
  fine. Choose a new `--work-dir` or remove the old one. Output is built in a
  `.NAME.partial` staging directory and renamed into place only on success.
- **`preprocess` exits 1 if any input failed.** Failed inputs are listed in
  `skipped.tsv`. Pass `--allow-skipped` to exit 0 anyway. Previously a bad
  input aborted the run or was silently dropped.
- **Work directories are private by default.** Directories are created 0700
  and files 0600. Before, they followed your umask. Use `--access group` to
  share with the work directory's Unix group.
- **Only one writer at a time.** `review` and `serve` take `review.lock`. A
  second writer exits with an error naming the holder. `status` and `export`
  only read.
- **`--no-rotate` is gone.** Use `--rotate never`. The default is now
  `--rotate auto`, which rotates grid images only when that saves a grid. In
  0.2.0 they were rotated whenever the packer liked. `m` follows `--rotate`,
  and `M` never rotates.
- **New status FLAGGED.** An image marked DIRTY in an earlier pass and not yet
  re-reviewed now shows as FLAGGED (orange) instead of UNREVIEWED. Grids never
  include DIRTY or FLAGGED images, so a grid keypress cannot clear them.
- **A lower `--pass` no longer overwrites later decisions.** An image's
  recorded pass never decreases.
- **Verdicts need the image on screen.** `c`/`d` are ignored for 200 ms after
  an image or grid appears. Keys typed while grids are being built are
  dropped.
- **Unloadable images are no longer skipped.** They show as placeholders that
  can only be marked DIRTY. This includes a JPG whose hash no longer matches
  the manifest.
- **Gamepads use SDL's game controller mappings.** B marks CLEAN, Y marks
  DIRTY, the D-pad navigates, A continues and Start quits. Pads without a
  mapping are unsupported; `SDL_GAMECONTROLLERCONFIG` can add one.
- **Diagnostics are logged to stderr** as `time LEVEL module: message`.
  Command output stays on stdout.
- **Rendering changed.** DICOM intensity tails are compressed instead of
  clipped. JPGs are written at quality 95 with 4:4:4 chroma. Colour, palette
  and compressed DICOMs, overlays and icon images are now rendered. A work
  directory from 0.2.0 keeps its old JPGs. Preprocess again to get the new
  rendering.
- **Invalid options fail before any work starts.** This covers
  `--batch-size` and `--pass` below 1, an unknown `--colormap`, and an empty
  or unknown `--batch`.
- **Malformed `review.tsv` or `manifest.tsv` stops the tool.** It reports
  "Cannot read work directory: ..." naming the file and line. Bad rows are no
  longer skipped and then silently dropped on the next save.
- **A plain `pip install image-review` no longer installs the viewer or
  preprocess dependencies.** It installs only click and cryptography, enough
  for `serve`, `status` and `export`. Use `pip install 'image-review[all]'`
  for everything, or per machine: `[preprocess,codecs]` where you preprocess,
  `[viewer]` where you `review`. A command whose extra is missing exits 1
  with `this command needs the <extra> extra: pip install
  'image-review[<extra>]'`. Without `codecs`, `preprocess` lists compressed
  DICOMs that pydicom cannot decode by itself or through Pillow (e.g. JPEG
  Lossless, JPEG-LS) as failed.
- **Dependencies have minimum versions** (see Requirements below).
  `cryptography`, `scipy`, `python-gdcm`, `pylibjpeg` and `pylibjpeg-openjpeg`
  are new.

### CLI

- New commands:
  - `serve` serves a work directory over HTTPS, with a self-signed
    certificate and a bearer token in an `ir://` connection string.
  - `export` writes the result as TSV, with one row per source file
    (CLEAN, DIRTY, UNREVIEWED or NOT_REVIEWED). It has `--output FILE`, which
    never overwrites. It refuses while a writer holds the work directory
    unless `--allow-live` is given.
- Global `-v`/`--verbose` (debug) and `-q`/`--quiet` (warnings and errors
  only) options. They go before the command.
- New `preprocess` options:
  - `--access {private,group}`, also read from `$IMAGE_REVIEW_ACCESS`;
  - `--allow-skipped`;
  - `--jobs N` for parallel rendering. It defaults to
    `$SLURM_CPUS_PER_TASK` or the usable CPUs, and the output is identical
    for any N.
- New `review` options:
  - `--reviewer NAME`, also read from `$IMAGE_REVIEW_REVIEWER`. It defaults
    to your login name.
  - `--rotate {auto,always,never}` replaces `--rotate/--no-rotate`.
  - `--remote ir://...` and `--via user@login-node`, also read from
    `$IMAGE_REVIEW_REMOTE` and `$IMAGE_REVIEW_VIA`. They let you review a
    `serve` instance, optionally through an SSH tunnel.
- New `status` options: `--remote`/`--via`, and `--check`. With `--check`,
  `status` exits 1 while any image is UNREVIEWED or any input failed to
  preprocess.
- New review keys:
  - `z` undoes your last mark (an image or a whole grid);
  - `b` on the end-of-list screen opens the next batch with work left.
- The review screen shows the display scale and rebuilds grids on resize.

### On-disk format

- **`skipped.tsv`** (new). Every input that was not rendered, with kind
  `failed` (corrupt, unsupported, unreadable, or its image id collides with
  another input's) or `ignored` (not an image). `status` reports the counts.
- **`review.tsv`**:
  - **Append-only.** Each mark appends rows; the last row per image wins. A
    torn last line from a crash is tolerated and cut off before the next
    append.
  - **Four audit columns** follow the original five: `reviewer`, `mode`
    (`single`, `grid` or `undo`), `grid_size` and `tool_version`. A
    five-column file is migrated once, the first time `review` or `serve`
    opens it, with the new columns empty for existing rows. 0.2.0 cannot
    read a migrated file.
  - **Undo rows.** `z` appends rows with mode `undo`. Each restores the
    previous verdict, or sets status `UNREVIEWED` for an image that had none.
    Versions before undo reject such a file.
- **`manifest.tsv`** gains `source_sha256` (the source file or ZIP entry) and
  `jpeg_sha256` (the JPG as written). A JPG that no longer matches is
  unloadable. Old three-column manifests still load, without the check.
- **`preprocess.json`** (new). It records how the work directory was made:
  tool and library versions, resolved sources, parameters and input counts.
  It holds source paths and is never served.
- **`review.lock`** (new). It is held by the one writer (`review` or `serve`)
  and records host, user, pid, start time and boot id. A stale lock is
  reclaimed automatically only when its process is provably gone on the same
  host. Otherwise, delete it by hand.
- **Access modes.** `private` gives directories 0700 and files 0600. `group`
  gives directories 2770 (setgid) and files 0660. Nothing is world-readable,
  and `review.tsv` follows the work directory's mode. `review`, `serve` and
  `status` warn about an existing work directory that other users can read,
  but never change its mode.

### Wire API (remote review)

`serve` and `review`/`status --remote` are new in this release. They speak
wire API version **5** (`connection.API_VERSION`), and the client refuses a
server that reports a different version. Install the same image-review
version on both machines. The versions reached during development:

- **v2** added `GET /skipped` (counts only).
- **v3** added `FLAGGED` to the status vocabulary in `/statuses`. `/mark`
  responses hold only the verdict just recorded.
- **v4** dropped `batch` from the `POST /mark` body; the server takes it from
  the manifest. It added `reviewer` and `mode`.
- **v5** added `POST /undo`.

`GET /version` reports the API version and package version. A server without
it is reported as "too old to report its API version".

### Requirements

Python >= 3.12. Minimum dependency versions, checked by running the test suite
on CPython 3.12 from wheels, by extra:

- core: click >= 8.2, cryptography >= 41
- `preprocess`:
  - matplotlib >= 3.7.3
  - numpy >= 1.26
  - pydicom >= 3.0
  - Pillow >= 10.3, excluding 11.x. Pillow 11 misdecodes a multi-frame MPO.
  - scikit-image >= 0.22
  - scipy >= 1.11.2
  - tqdm >= 4.60
- `codecs`: python-gdcm >= 3.0.25, pylibjpeg >= 2.0, pylibjpeg-openjpeg >= 2.0
- `viewer`: pygame-ce >= 2.3.1, Pillow (as above), rectpack == 0.2.2
- `all`: `preprocess`, `codecs` and `viewer`; `dev`: `all` plus the lint,
  type-check and coverage tools
