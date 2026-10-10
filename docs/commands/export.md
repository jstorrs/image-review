# `image-review export`

```
image-review export [--work-dir DIR] [--output FILE] [--report FILE] [--allow-live]
```

Writes the study's result as an **allowlist** to stdout, or to a new `FILE`
with `--output`: one row per source file that may be released, and nothing
else. **Release a file only if both its path and its SHA-256 match a row.**
Anything not listed is denied: DIRTY, unreviewed, failed and ignored files, a
file whose bytes changed since preprocess, and ZIP containers as a whole
(their entries are listed one by one as `<zip>::<entry>`).

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
`--remote` and ignores `$IMAGE_REVIEW_REMOTE`.

## What is allowlisted

A file is allowlisted only if:

- it was reviewed CLEAN (a DICOM's icon too);
- the manifest has its SHA-256; and
- no file that is not CLEAN has the same recorded hash (`source_sha256`;
  identical bytes cannot be both clean and dirty, so all copies are denied).

CLEAN files of a work directory made by an older version, whose manifest has
no hashes, are never allowlisted: they appear in the report as CLEAN with
`not allowlisted: ...` in `reason`.

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
| `source_sha256` | SHA-256 of the source file (or ZIP entry), from the manifest; compare it with the file's own before releasing it. In the report, empty for a file that never rendered or a work directory from an older version. It is derived from the file's content, so treat it like the `image_id` |
| `status` | (report) `DIRTY`, `UNREVIEWED` (no verdict yet), `NOT_REVIEWED` (preprocess could not render it, or its icon), `IGNORED` (preprocess did not take it for an image, so nobody looked at it), or `CLEAN` for a CLEAN file that was not allowlisted |
| `pass_number`, `timestamp`, `reviewer` | From the latest verdict on the file's main image; empty without one. After an undo they are the undo's time and reviewer. `reviewer` is the reviewer's unverified claim |
| `reason` | (report) Why the row is not simply the main image's verdict: preprocess's error for a `NOT_REVIEWED` file, `icon DIRTY` / `icon UNREVIEWED` / `icon: <error>` for its icon, `main image missing` (an icon whose file is not in the manifest; the row is then at best `NOT_REVIEWED`), preprocess's reason for an `IGNORED` file, `not allowlisted: ...` for a CLEAN file that was not allowlisted; otherwise empty |

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
  ignored), one per file.
- `reviewer` values can start with `=`, `+`, `-` or `@`. Open the files as
  text (e.g. import them as text columns), not by double-clicking them into a
  spreadsheet that would read them as formulas.
- Export logs one line to stderr: how many files are allowlisted and how many
  of each status are in the report.

Export refuses (exit 1, naming the `image_id`, writing nothing) if any field
of either file (`image_id`, `reviewer`, `reason`, ...), even without
`--report`, holds a control character (tab, CR, LF and the rest of C0, DEL,
C1 such as U+0085), U+2028 or U+2029, or starts with `"`, since readers could
split or merge rows there. A `"` anywhere else is written as is.

## Refusals and output files

- It writes nothing in the work directory.
- It refuses (exit 1) while a writer (`review` or `serve`) has the work
  directory open, since verdicts may still change, and also if a writer opens
  the work directory while export reads it. `--allow-live` exports anyway,
  with a warning.
- It always refuses a `review.tsv` whose last line was cut short by an
  interrupted write; the next verdict recorded with `review` drops that line.
  Re-check the last image you reviewed before the crash.
- `--output` and `--report` never overwrite an existing file, and may not
  name the same file.
- The report is written first, so a failed allowlist write leaves only a
  report, which releases nothing.
- Each file is created in one step (written to a hidden `.FILE.<random>.tmp`
  beside it, then linked into place) with the work directory's file mode, and
  for a group work directory its group too (0660). If the group cannot be
  set, the file is made 0600 with a warning.
- The hidden file is removed on every exit except a hard kill (`kill -9`, a
  node crash), which can leave it behind: delete it, as it holds source
  paths.
