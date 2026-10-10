# `image-review export`

```
image-review export [--work-dir DIR] [--output FILE] [--report FILE] [--allow-live]
```

Writes the study's result as an **allowlist** to stdout, or to a new `FILE`
with `--output`: one row per source file that may be released, and nothing
else. **Release a file only if both its path and its SHA-256 match a row.**
Anything not listed is denied: DIRTY, unreviewed, failed and ignored files, a
file whose bytes changed since preprocess, and ZIP containers as a whole
(their entries are listed one by one as `<zip>::<entry>`). A ZIP that cannot
be opened, or has no file entries, gets one report row under its own path.

`--report FILE` also writes every other file to a new `FILE`, with its status
and why. The report is for audit and follow-up (what is left to review, what
preprocess could not read). **Never use the report to choose what to
release**, e.g. by taking "everything not DIRTY".

| Option | Default | Description |
|--------|---------|-------------|
| `--work-dir` | `./review_work` | Work directory containing preprocessed data |
| `--output` | stdout | Write the allowlist to this new file |
| `--report` | none | Also write every file not allowlisted to this new file |
| `--allow-live` | off | Export even while a writer (`review` or `serve`) has the work directory open |

**Where to run it.** `image_id`s are source paths and may hold PHI, so
export runs where the work directory is (e.g. on the cluster). It refuses
`--remote` with a usage error (exit 2) and ignores `$IMAGE_REVIEW_REMOTE`.

## What is allowlisted

A file is allowlisted only if:

- it was reviewed CLEAN (a DICOM's icon too);
- the manifest has its SHA-256; and
- no file that is not CLEAN has the same recorded hash (`source_sha256`;
  identical bytes cannot be both clean and dirty, so all copies are denied).

A CLEAN file that is not allowlisted appears in the report, still CLEAN,
with one of these notes in `reason`:

- `not allowlisted: no source_sha256 (work directory from an older version)`:
  the manifest has no hash for it. So CLEAN files of a work directory made by
  an older version are never allowlisted.
- `not allowlisted: same content as a file that is not CLEAN`.

## File format

Both files are tab-separated UTF-8 with LF line endings and a header row,
with no quoting.

- Allowlist columns: `source_sha256`, `image_id`, `pass_number`,
  `timestamp`, `reviewer`.
- Report columns: `image_id`, `status`, `pass_number`, `timestamp`,
  `reviewer`, `reason`, `source_sha256`.

`allowlist.tsv`:

```
source_sha256  image_id                      pass_number  timestamp                         reviewer
3f1c...e09a    /data/site_a.zip::001.dcm     1            2026-03-02T10:14:07.512408+00:00  alice
```

`report.tsv`, everything else:

```
image_id                      status        pass_number  timestamp                         reviewer  reason                    source_sha256
/data/site_a.zip::002.dcm     DIRTY         2            2026-03-03T09:01:44.020731+00:00  bob                                 a27b...51c4
/data/site_a.zip::003.dcm     DIRTY         1            2026-03-02T10:15:30.101266+00:00  alice     icon DIRTY                0d9e...7f30
/data/site_a.zip::004.dcm     UNREVIEWED                                                                                       c6b2...18de
/data/site_b/broken.dcm       NOT_REVIEWED                                                           unsupported: multi-frame DICOM (3 frames)
/data/site_b/referral.pdf     IGNORED                                                                not an image (unrecognized content)
```

(Hashes shortened here; each is 64 hex characters. Columns are aligned here
for reading.)

| Column | Description |
|--------|-------------|
| `image_id` | The source file's path, as in `manifest.tsv` / `skipped.tsv`; a file inside a ZIP is `<zip>::<entry>`, one row per entry |
| `source_sha256` | SHA-256 of the source file (or ZIP entry), from the manifest (a DICOM and its icon share it); compare it with the file's own before releasing it. In the report, empty for a file that never rendered (so always for `IGNORED`) or a work directory from an older version. It is derived from the file's content, so treat it like the `image_id` |
| `status` | (report) `DIRTY`, `UNREVIEWED` (no verdict yet), `NOT_REVIEWED` (preprocess could not render it, or its icon), `IGNORED` (preprocess did not take it for an image, so nobody looked at it), or `CLEAN` for a CLEAN file that was not allowlisted |
| `pass_number`, `timestamp`, `reviewer` | From the latest verdict on the file's main image; empty without one. Also empty when the main image is `NOT_REVIEWED` (preprocess failed on it or ignored it), even if `review.tsv` has a verdict for it, and when there is no main image. After an [undo](../reference/work-directory.md#undo-rows) they are the undo's time and reviewer, with the pass it restored. `reviewer` is the reviewer's unverified claim, and may be empty in rows from an older version |
| `reason` | (report) Why the row is not simply the main image's verdict, as `; `-separated notes in this order: preprocess's error or ignored reason for a `NOT_REVIEWED` main image; `main image missing` for an icon whose file is neither in the manifest nor a failed input (the row is then at best `NOT_REVIEWED`), or that file's reason if preprocess ignored it; `icon: <error>` for an icon preprocess could not render, else `icon DIRTY` / `icon UNREVIEWED`; preprocess's reason for an `IGNORED` file; and last, `not allowlisted: ...` for a CLEAN file that was not allowlisted. Otherwise empty |

- A DICOM's embedded icon (`<path>#icon` in the manifest) is folded into its
  file's row: the row is CLEAN only if the image and its icon both are, else
  DIRTY if either is, else NOT_REVIEWED, else UNREVIEWED.
- Inputs that failed to preprocess (the `failed` rows of `skipped.tsv`) are
  `NOT_REVIEWED`: nobody has looked at them, so treat them as possibly
  containing PHI. This applies even if the manifest also lists them.
- Inputs preprocess did not take for images (the `ignored` rows of
  `skipped.tsv`: a PDF, a text file, a DICOMDIR, ...) are `IGNORED`, one row
  each, after all other rows. Nobody has looked at them, so follow them up.
  An ignored input the manifest also lists is `NOT_REVIEWED` instead.
- A FLAGGED image (DIRTY in an earlier pass, not yet re-reviewed) is `DIRTY`.
- Rows follow the manifest's order, then `skipped.tsv`'s (failed, then
  ignored), one per file. Verdicts in `review.tsv` for an `image_id` in
  neither file are left out.
- `reviewer` values can start with `=`, `+`, `-` or `@`. Open the files as
  text (e.g. import them as text columns), not by double-clicking them into a
  spreadsheet that would read them as formulas.
- Export [logs](index.md#logging) one INFO line to stderr: how many files are
  allowlisted and how many of each status are in the report, CLEAN last.
  For example:

  ```text
  2026-10-01T14:03:07+02:00 INFO image_review.cli: 12 files allowlisted; 5 in the report (2 DIRTY, 1 UNREVIEWED, 1 NOT_REVIEWED, 1 IGNORED, 0 CLEAN not allowlisted)
  ```

Fields are never quoted, so export refuses a field that readers could split
or merge rows at; see [Refusals](#refusals-and-output-files). A `"` that does
not start a field is written as is.

## Refusals and output files

- It writes nothing in the work directory.
- It refuses `--remote` with a usage error (exit 2); see *Where to run it*
  above:

  ```text
  Error: export does not work with --remote: image_ids stay on the server. Run export on the machine (cluster) holding the work directory, with --work-dir.
  ```

- It refuses (exit 1) while a writer (`review` or `serve`) holds the work
  directory's [lock](../reference/work-directory.md#reviewlock), since
  verdicts may still change. It checks before reading and, if no writer was
  there, again after, so it also refuses if a writer opens the work
  directory while export reads it:

  ```text
  Error: work directory is in use by USER on HOST (pid PID) since STARTED; lock file PATH. If that process is gone, delete PATH by hand. Verdicts may still change; stop the writer and export again, or pass --allow-live.
  ```

  For a lock that cannot be read or is malformed, the part before
  "Verdicts" instead says the lock file is unreadable or corrupt, with the
  same advice.

  `--allow-live` overrides only this refusal: it exports anyway, with a
  warning.
- It always refuses (exit 1) a `review.tsv` whose last line was cut short by
  an interrupted write; the next verdict recorded with `review` (or through
  `serve`) drops that line. Re-check the last image you reviewed before the
  crash.
- It refuses (exit 1) a malformed `skipped.tsv`.
- It refuses (exit 1, naming the `image_id` and the column, writing nothing)
  if any field of either file (`image_id`, `reviewer`, `reason`, ...), even
  without `--report`, holds a control character (tab, CR, LF and the rest of
  C0, DEL, C1 such as U+0085), U+2028 or U+2029, or starts with `"`, since
  readers could split or merge rows there.
- `--output` and `--report` may not name the same file: that is a usage
  error (exit 2), found before anything is read:

  ```text
  Error: --report and --output name the same file.
  ```

- `--output` and `--report` never overwrite an existing file (exit 1):

  ```text
  Error: FILE already exists; not overwriting it.
  ```

  A dangling symlink counts as an existing file. Both are checked before
  either file is written, so no new report is left beside an old allowlist.
- The report is written first, so a failed allowlist write leaves only a
  report, which releases nothing.
- Each file is created in one step (written to a hidden `.FILE.<random>.tmp`
  beside it, then linked into place) with the work directory's file mode, and
  for a group work directory its group too (0660). If the group cannot be
  set, the file is made 0600 with a warning.
- Ctrl-C, SIGTERM and SIGHUP remove the hidden file. Only a hard kill
  (`kill -9`, a node crash) can leave it behind; see
  [Leftover temporary files](../reference/work-directory.md#leftover-temporary-files).
