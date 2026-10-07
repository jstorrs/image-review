# Changelog

## Unreleased

- **Experimental: browser review over SSH.** `image-review
  serve --socket` (or `--socket-path PATH`) serves plain HTTP on a Unix socket
  instead of HTTPS over TCP, for a browser on your laptop reached through
  `ssh -L`. `serve` prints the ssh command and the
  `http://127.0.0.1:8080/#TOKEN` URL (or, when stdout is not a terminal,
  writes the URL to a private file under `~/.image-review/`). `--via` (or
  `$IMAGE_REVIEW_VIA`) fills in the login node of that command; `--direct`
  (or `$IMAGE_REVIEW_DIRECT`) is for laptops that can ssh to compute nodes
  without a jump host, and omits `-J`. `--ssh-host NAME` sets the node name
  it prints, for sites where the node's own FQDN does not work from the
  laptop. The page
  reviews single images, like the viewer's single mode: enter a reviewer
  name, then `c` clean, `d` dirty, Left/Right to move, `z` to undo (this
  page's own marks, with one page per server: the undo history is shared).
  A verdict counts only once the image has been on screen for 200 ms, an
  image that cannot be loaded takes DIRTY only, and the header shows the
  display scale, in red below 100%. There is no grid mode yet. The default
  HTTPS mode and the pygame client are unchanged.
  See [SECURITY.md](SECURITY.md) for the limits.
- **Experimental:** in socket mode, `POST /grids` returns grid layouts
  computed with the viewer's own packing code, for the browser page's
  coming grid mode (which does not use it yet). Socket mode only; the
  HTTPS API and its version are unchanged.
- **Requirements:** rectpack (still pinned at 0.2.2) moved from the `viewer`
  extra to the core dependencies, with the grid layout code (`layout.py`), so
  that `serve` can later lay out browser grids with the same code as the
  viewer. rectpack has no wheel: an offline or `--only-binary` install needs
  its sdist.

## 0.3.0

Remote review over HTTPS, an allowlist export of the files that may be released, an audit trail in
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
- **`manifest.tsv`, `review.tsv` and `skipped.tsv` must be UTF-8.** 0.2.0
  wrote `manifest.tsv` and `review.tsv` in the locale's encoding. A work
  directory from 0.2.0 whose source paths were non-ASCII, under a non-UTF-8
  locale, now fails with "not valid UTF-8". Re-encode each file, keeping its
  mode and group (they hold PHI), e.g. `cp -p f f.orig && iconv -f LATIN1 -t
  UTF-8 f.orig > f && rm f.orig` (writing into the existing `f` keeps its mode
  and group; use the locale's charset in place of LATIN1). `skipped.tsv` is new in 0.3.0; it
  needs this only if you preprocessed with 0.3.0 under such a locale.
- **A plain `pip install image-review` no longer installs the viewer or
  preprocess dependencies.** It installs only click and cryptography, enough
  for `serve`, `status` and `export`. Use `pip install 'image-review[all]'`
  for everything, or per machine: `[preprocess,codecs]` where you preprocess,
  `[viewer]` where you `review`. A command whose extra is missing exits 1
  with `this command needs the <extra> extra: pip install
  'image-review[<extra>]'`. Without `codecs`, `preprocess` lists compressed
  DICOMs that pydicom cannot decode by itself or through Pillow (e.g. JPEG
  Lossless, JPEG-LS) as failed.
- **A badly named input fails even if it would have been ignored.** An
  AppleDouble file, a non-image or an empty ZIP whose name (or a directory
  above it) is not UTF-8 or holds a control character or U+2028/U+2029 is now
  `failed`, so `preprocess` exits 1 unless `--allow-skipped`. Rename it or
  remove it.
- **A 0.2.0 work directory exports an empty allowlist.** Its manifest has no
  `source_sha256`, and `export` never allowlists a file without one (its CLEAN
  files are in `--report`, marked as not allowlisted). To release files,
  preprocess again into a new work directory and redo the verdicts.
- **Dependencies have minimum versions** (see Requirements below).
  `cryptography`, `scipy`, `python-gdcm`, `pylibjpeg` and `pylibjpeg-openjpeg`
  are new.

### CLI

- New commands:
  - `serve` serves a work directory over HTTPS, with a self-signed
    certificate and a bearer token in an `ir://` connection string.
  - `export` writes an allowlist (default deny) as TSV: only the source
    files that may be released, each by path and source SHA-256, so a file is
    released only if both match. A file is listed only if it was reviewed
    CLEAN (its icon too), the manifest has its hash, and no file that is not
    CLEAN has the same recorded hash (`source_sha256`). CLEAN files of a work directory without
    hashes (made by an older version) are never allowlisted. `--report FILE`
    writes every other file, with its status (DIRTY, UNREVIEWED,
    NOT_REVIEWED, IGNORED, or CLEAN not allowlisted) and reason, for audit
    and follow-up only. The allowlist goes to stdout or `--output FILE`; the
    report is written first; neither overwrites. It refuses while a writer
    holds the work directory unless `--allow-live` is given.
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

- **`manifest.tsv` and `skipped.tsv` are always UTF-8** with `\r\n` line
  endings, whatever the locale. 0.2.0 wrote `manifest.tsv` in the locale's
  encoding; `review.tsv` was likewise locale-encoded and is now read as UTF-8.
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
wire API version **6** (`connection.API_VERSION`), and the client refuses a
server that reports a different version. Install the same image-review
version on both machines. The versions reached during development:

- **v2** added `GET /skipped` (counts only).
- **v3** added `FLAGGED` to the status vocabulary in `/statuses`. `/mark`
  responses hold only the verdict just recorded.
- **v4** dropped `batch` from the `POST /mark` body; the server takes it from
  the manifest. It added `reviewer` and `mode`.
- **v5** added `POST /undo`.
- **v6** made `GET /skipped` always send counts (`{"failed": 0, "ignored": 0}`
  when the work dir has no `skipped.tsv`) instead of `null`. The server also
  refuses a repeated query parameter with 400 instead of using the first value.

`GET /version` reports the API version and package version. A server without
it is reported as "too old to report its API version".

### Fixes

- **Left from the end screen in todo-only mode goes to the last todo item.** It
  used to skip that item when it was left unmarked.
- **`M` (shift+m) uses the Shift state of the key press itself.** It used to
  read the live keyboard, so releasing Shift before the press was handled
  (for example after a slow remote fetch) opened grid mode with the wrong
  rotation.
- **A window too short to fit an image no longer shows the previous one.** With
  the window at or below the 50px status bar, moving to another item kept the
  old item's pixels under the new item's name, and its review dwell started,
  so `c` could mark an image nobody saw. Now only the bars are painted, and the
  dwell starts once the item's pixels are on screen. Hiding the image this way
  after it appeared restarts the dwell.
- **Names that are not UTF-8 or hold a control character are failed inputs.**
  A file whose name is not valid UTF-8 used to crash `preprocess` after every
  image was rendered, losing the run; a name holding a newline, tab, other
  control character or U+2028/U+2029 was reviewed but then made `export`
  refuse the whole study. Such an input (a file, a ZIP entry, or everything
  under a directory or ZIP so named) is now a `failed` row in `skipped.tsv`
  under its escaped name: a non-UTF-8 byte or ASCII control as `\xNN`, any
  other as `\uNNNN`. Rename it and preprocess again.
- **`export` lists ignored inputs.** It used to leave out the `ignored` rows
  of `skipped.tsv`, so an input that was not an image (a PDF, a Word file, a
  DICOMDIR, ...) was never mentioned and nobody followed it up. Each is now a
  row of the report with status `IGNORED` after all other rows, never
  allowlisted. A work
  directory made by a 0.3.0 pre-release may hold an ignored name with a
  control character or U+2028/U+2029, which now makes `export` refuse;
  preprocess again into a new work directory (its verdicts must be redone).
- **A `review.lock` with an absurd pid is reported, not a traceback.** A pid
  of 2^31 or more made `review`, `serve` and `export` crash with an
  `OverflowError` instead of naming the corrupt lock. It is now refused as
  malformed like any other bad lock. A stray
  `review.lock.<host>.<boot_id>.<pid>.*` file whose pid text `int()` cannot
  read (such as `²`) or that is 2^31 or more made every writer on that machine
  fail to open the work directory until it rebooted; such files are now
  skipped.

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
