# Work directory

All state lives in the work directory (default `./review_work`), which
`preprocess` creates.

```
review_work/
  manifest.tsv        # Master list of images
  skipped.tsv         # Inputs that produced no image
  preprocess.json     # How it was made
  review.tsv          # Verdicts (created later during review)
  review.lock         # Present while review or serve has it open
  batch_001/
    img_00001.jpg      # Individual preprocessed images
    img_00002.jpg
    ...
  batch_002/
    ...
```

| File | Format | Description |
|------|--------|-------------|
| [`manifest.tsv`](#manifesttsv) | TSV | Master image list, written by `preprocess` |
| [`skipped.tsv`](#skippedtsv) | TSV | Inputs that produced no image, written by `preprocess` |
| [`preprocess.json`](#preprocessjson) | JSON | How the work directory was made |
| [`review.tsv`](#reviewtsv) | TSV | Review decisions, appended by `review` and `serve` |
| [`review.lock`](#reviewlock) | JSON | Present while a writer (`review` or `serve`) has the work directory open; names the holder |
| `batch_NNN/img_NNNNN.jpg` | JPG | Preprocessed individual images, numbered in order within each batch |

The TSV files are tab-separated UTF-8 with `\r\n` line endings, and
`review.tsv` also accepts `\n`. Every file except the JPGs holds source
paths, which may carry patient identifiers: treat them as PHI (see
[Local disk](security-model.md#local-disk)). How the tool reads and writes
them internally is in the specification's
[Data Files](specification.md#data-files).

## `manifest.tsv`

One row per image, in the order the inputs were found.

| Column | Description |
|--------|-------------|
| `batch` | Batch subdirectory name (e.g. `batch_001`) |
| `preprocessed_path` | Path of the JPG within the work directory; unique |
| `image_id` | The image's source (see below) |
| `source_sha256` | SHA-256 (64 lowercase hex characters) of the source file's bytes, or of the ZIP entry's bytes. One per source: a DICOM and its icon share it |
| `jpeg_sha256` | SHA-256 (64 lowercase hex characters) of the JPG as written |

An `image_id` is an absolute path:

- a file named as a SOURCE: its fully-resolved path, so a symlink named on
  the command line is recorded under its target's path;
- a file found inside a directory: the resolved directory SOURCE's path plus
  the names below it, so a symlinked file there keeps its link path;
- a ZIP entry: `<zip path>::<entry name>`; when several entries share a name,
  the later ones get `#2`, `#3`, ...;
- a DICOM's embedded icon image: `<path>#icon`, a second row beside the main
  image;
- a name that is not UTF-8 or holds a control character or line separator
  never reaches the manifest: it is a `failed` row in
  [`skipped.tsv`](#skippedtsv), under an escaped name.

The same `image_id` can appear on more than one row when one file was reached
through overlapping SOURCES; one verdict covers all of them.

Work directories from older versions have only the first three columns
(`batch`, `preprocessed_path`, `image_id`). They still load; their images are
not hash-checked.

**Strict parsing.** `review`, `status`, `serve` and `export` read the file
when they open the work directory, and stop (exit 1) with

```
Cannot read work directory: <file>:<line>: <problem>
```

if the header is not exactly one of the two above, a row has a missing or
empty field, a hash is not 64 lowercase hex characters, or a
`preprocessed_path` repeats. A file that is not valid UTF-8 is named with a
byte offset instead of a line (`<file>: not valid UTF-8 at byte N`). The tool
never repairs the file: fix or remove the line it names.

**Hash check.** Each JPG is checked against its `jpeg_sha256` whenever it is
loaded. A JPG changed or cut short after preprocessing is shown as an
unloadable placeholder, which can only be marked DIRTY, never as an image
that could be marked CLEAN.

## `skipped.tsv`

One row per input that produced no image. It is always written, with only
its header when nothing was skipped. Every input ends up in exactly one of
`manifest.tsv` or `skipped.tsv`.

| Column | Description |
|--------|-------------|
| `image_id` | The input, in the same form as in `manifest.tsv`: `<path>#icon` for an icon that failed to render, the source path if a whole source could not be opened, or, for a name that is not UTF-8 or holds a control character or line separator, the escaped name (`\xNN` for a non-UTF-8 byte or ASCII control, `\uNNNN` for any other character) |
| `kind` | `failed` (an input that was not rendered; `preprocess` exits 1 unless `--allow-skipped`) or `ignored` (not an image: unrecognized content, AppleDouble, DICOMDIR, a ZIP without files, a symlink to an enclosing directory or to a directory inside a SOURCE, a non-regular file inside a directory) |
| `reason` | `<ExceptionClass>: <message>`, `unsupported: ...` for inputs this tool does not render, or the `ignored` reason |

What each kind covers, with example reasons, is in
[Skipped inputs](../commands/preprocess.md#skipped-inputs). `status` counts
the rows and `export` reports them. Both read the file strictly and stop with
an error naming the file and line if it is malformed.

## `preprocess.json`

One JSON object recording how the work directory was made:

| Key | Description |
|-----|-------------|
| `tool_version` | The image-review version (`"unknown"` if not installed) |
| `created` | UTC time the run finished, ISO 8601 (`YYYY-MM-DDTHH:MM:SSZ`) |
| `sources` | The SOURCES as resolved absolute paths, in the order given, escaped like an `image_id` |
| `parameters` | `batch_size`, `colormap`, `clahe_kernel_size`, `outlier_percentile`, `intensity_margin`, `tail_fraction`, `jpeg_quality`, `jpeg_subsampling`, `access`, `jobs` (recorded only: the output does not depend on it) |
| `libraries` | Versions of `pydicom`, `numpy`, `scikit-image`, `Pillow`, `matplotlib`, plus `gdcm`, `pylibjpeg`, `openjpeg` when installed |
| `counts` | `inputs` (N of the summary line), `written`, `skipped_failed`, `skipped_ignored` |

It holds source paths, so it is as sensitive as `manifest.tsv`. The tool
never reads it back, and the server never sends it.

## `review.tsv`

Every verdict is appended to `review.tsv`, with who gave it and how. The file
is created by the first verdict.

| Column | Description |
|--------|-------------|
| `image_id` | As in `manifest.tsv` |
| `batch` | Batch the image belongs to |
| `status` | `CLEAN` or `DIRTY`; `UNREVIEWED` only in an undo row (see [Undo rows](#undo-rows)) |
| `pass_number` | Pass (an integer, at least 1) in which the verdict was given; in an undo row, the restored verdict's pass, or for a tombstone the undone mark's pass |
| `timestamp` | ISO 8601 UTC time |
| `reviewer` | Who gave the verdict: `review --reviewer`, by default the login name. An unverified claim made by the client, recorded as given |
| `mode` | `single` or `grid`: the display mode the verdict was given in; `undo` for a row written by `z` |
| `grid_size` | How many images the one keypress covered (1 in single mode); in an undo row, how many images the undo covered |
| `tool_version` | The image-review version that wrote the row (`unknown` if not installed) |

`reviewer`, `mode`, `grid_size` and `tool_version` are empty in rows carried
over from an older file (see [Upgrading an older file](#upgrading-an-older-file)).

**Last row wins.** The file is an append-only log: every rating action
appends a line per image and syncs it to disk, and when an image appears more
than once its last line wins. Only images that have been marked appear; an
image with no row, or whose last row is an undo's `UNREVIEWED`, is
UNREVIEWED. FLAGGED (marked DIRTY in an earlier pass; see
[review](../commands/review.md)) is worked out when the file is read and is
never written.

**Strict parsing.** A malformed file stops the tool with the same
`Cannot read work directory: <file>:<line>: <problem>` message as
`manifest.tsv`, instead of being skipped or rewritten, so a hand edit cannot
silently lose decisions (a file that is not UTF-8 is named with a byte
offset instead of a line). The header must be exactly these nine columns (or
the old first five), and every row has all nine (or five) fields, with:

- a non-empty `image_id`;
- a `status` of `CLEAN` or `DIRTY`, or `UNREVIEWED` with `mode` `undo`;
- a `pass_number` of at least 1;
- a `mode` of `single`, `grid`, `undo` or empty;
- a `grid_size` that is empty or at least 1.

The other columns are free text and may be empty.

**A line cut short by a crash.** It is safe to kill the process at any time.
An empty file holds no decisions. A last line cut short by a crash is skipped
with a warning and dropped on the next rating, unless all of its fields made
it to disk: then it is kept, and only its last field (`tool_version`, or
`timestamp` in an old five-column file) may be cut short or empty. A crash
in the middle of a grid mark can record only some of its images. A bad line
anywhere else is still an error. `export` refuses a file whose last line was
cut short; see [Refusals](../commands/export.md#refusals-and-output-files).

### Undo rows

An undo (`z`) appends one row for each image of the mark it undoes, with
`mode` `undo`, the time of the undo and the undoing reviewer. Where the image
had a verdict before that mark, the row restores that verdict's `batch`,
`status` and `pass_number` exactly, so the pass can go down. Where it had
none, the row is a **tombstone**: `status` `UNREVIEWED`, keeping the undone
row's `batch` and `pass_number`, which makes the image read as never reviewed
until it is marked again.

### Upgrading an older file

**Upgrade everyone sharing a work directory together.** A `review.tsv` from
an older version has only the first five columns. It is upgraded in place the
first time `review` or `serve` opens it, with the new columns left empty for
its existing rows. `status` and `export` read it as is, without upgrading it.

- Older image-review versions cannot read the upgraded file.
- Versions before wire API v5 (before undo) reject a `review.tsv` that holds
  undo rows, so upgrade everyone sharing a work directory before anyone
  presses `z`.

If the upgrade is interrupted, a `.review.tsv.*.tmp` file may be left in the
work directory; see [Leftover temporary files](#leftover-temporary-files).

## `review.lock`

Only one writer (`review` or `serve`) can use a work directory at a time. It
holds `review.lock` while the work directory is open and removes it when it
exits. `status` takes no lock and always works. `export` takes none either,
but refuses while a writer holds the lock unless given `--allow-live`.

The lock is a JSON object naming the holder: `host`, `boot_id` (the
machine's boot id, `""` where unavailable), `user`, `pid` and `started` (UTC,
ISO 8601). It has the work directory's file mode, so teammates in a group
work directory can read who holds it.

A second writer exits 1 with:

```
work directory is in use by USER on HOST (pid PID) since STARTED; lock file PATH. If that process is gone, delete PATH by hand
```

A lock file that cannot be read or is malformed counts as held, and the
message names it with the same advice.

**Reclaiming.** A lock is cleared automatically only when the tool can verify
that its process is gone: same host name, same machine boot, and no process
with that pid. A lock left on another node (for example by a `serve` job that
was killed), one from before a reboot, or one whose process id has since been
reused is not cleared. Nor is a lock without a boot id: one written on a
system that has none (such as macOS, which has no
`/proc/sys/kernel/random/boot_id`) or by an older version.

**Deleting it by hand.** Delete `review.lock` only when you are sure its
process is gone: check `squeue`, or `ps -p PID` on that node, and compare the
start time.
[Browser review over SSH](../tutorials/browser-review.md#troubleshooting)
shows the command.

## Leftover temporary files

Each of these is normally removed when the command exits, but an abrupt kill
(e.g. `kill -9`, a node crash) can leave it behind. They hold the same data
as the files they stand in for, source paths included, so delete any you
find. Apart from `.NAME.partial`, the tool ignores them:

- `.NAME.partial`, next to the work directory: `preprocess`'s staging
  directory. The next run into that work directory refuses and names it.
- `.review.tsv.*.tmp`, inside the work directory: an interrupted
  [upgrade](#upgrading-an-older-file) of `review.tsv`.
- `review.lock.<host>.<boot id>.<pid>.<random>`, inside the work directory: a
  writer killed while taking the lock. The next writer on the same machine
  removes those whose process is gone, unless the machine has no boot id;
  others are safe to delete by hand.
- `.FILE.<random>.tmp`, beside an `export --output` or `--report` file: an
  interrupted export.
