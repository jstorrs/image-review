# Security

image-review handles images that may carry burned-in Protected Health
Information (PHI). This page is the one place that states what the tool
protects, against whom, and where its protection ends. The commands and file
formats are described in [README.md](README.md) and [SPEC.md](SPEC.md); the
practical HPC steps are in [TUTORIAL.md](TUTORIAL.md#reviewing-on-an-hpc-cluster).

## What is protected, and against whom

The aim is that PHI stays on the cluster, and that the people who can reach it
are the people you chose.

- **Original files, DICOM headers, source paths and `review.tsv`** stay where
  `preprocess` ran. What travels to a reviewer's machine is the preprocessed
  JPGs, their batch and file names (`batch_001/img_00001.jpg`), review
  statuses and pass numbers, the reviewer name the client sends, skip counts
  and the server's version string.
- **The threats considered** are other users on shared cluster nodes and
  network observers or impostors between the laptop and the cluster.
- **Not considered:** a malicious cluster administrator or root user, a
  compromised laptop or cluster account, and the reviewer themselves (who sees
  the images by design). Anyone who can read the work directory as your Unix
  user, or as a member of its group under `--access group`, can read all of it.

The tool does not certify that a file is free of PHI. It records the verdicts
of human reviewers.

## The server (`image-review serve`)

- **Transport.** HTTPS with a self-signed certificate generated at every
  start, TLS 1.2 or later. The certificate's SHA-256 fingerprint is part of
  the connection string, and the client checks it on every connection before
  it sends the token, so a wrong or replaced server is rejected. Nothing is
  reused across runs.
- **Token.** Every request must carry the bearer token, compared in constant
  time before any routing. The token is a password: anyone holding the
  connection string (`ir://...`) can view the images and record verdicts
  while the server runs. Do not paste it into chat or tickets. A new token
  and certificate are generated at each start, so an old string stops working.
- **Delivery of the connection string.** On a terminal it is printed. When
  stdout is not a terminal (for example `sbatch`) it is written instead to
  `~/.image-review/connection-<host>-<port>.txt`: the directory must be owned
  by you and is tightened to 0700, the file is created 0600, and the file is
  removed when the server stops. A hard kill can leave it behind; it is
  replaced by the next server on the same host and port, and you can delete it
  by hand. Other users cannot read it. The remaining exposure is root on any
  node that mounts the home directory, backups or snapshots of it, and any
  process running as you.
- **Binding.** Wildcard addresses (`0.0.0.0`, `::`, empty) are refused; the
  server binds and advertises one named host (default: this machine's FQDN).
- **Logging.** One INFO line per request: peer address, method, path without
  its query string, and status. Tokens, keys, source paths, query strings and
  exception messages are never logged in that per-request line. Other records
  are not so restricted: startup and work-directory messages (for example a
  `review.tsv` torn-line warning or a failure opening the work directory) can
  name file paths and parse errors.
- **Caching.** Every response carries `Cache-Control: no-store` (and
  `X-Content-Type-Options: nosniff`).
- **`image_id`s never leave the server.** The manifest's `image_id` is the
  source path and may hold identifiers. The wire carries only preprocessed
  paths (keys), skip counts, statuses and JPG bytes. `/skipped` sends counts,
  never reasons or paths.
- **Reviewer identity is a claim.** The `reviewer` name in each mark is sent by
  the client and recorded as given (1-64 printable characters). The server
  does not authenticate it: whoever holds the token can record verdicts under
  any name. Treat the `reviewer` column as unverified.
- **Single writer.** `serve` and a local `review` each hold `review.lock` in
  the work directory, so a second writer exits with an error naming the holder.
  `status` and `export` only read. A lock is reclaimed automatically only when
  the tool can verify its process is gone on the same machine since its last
  boot; otherwise it is left for you to delete by hand.
- **Multi-client limits.** The server assumes one reviewer per server. Its
  undo stack is a single one, shared by every client, so `z` in one client
  undoes the latest mark from any of them. Each client works from its own
  snapshot of the statuses and may not see another client's marks until it
  refetches; in `review.tsv` the last row for an image wins. Serving several
  clients at once is out of scope.
- **`--via`.** `review --via` and `status --via` run `ssh` to forward a local
  port to the compute node. ssh protects the hop to the login node; TLS with
  the pinned certificate covers the whole path to the compute node, and the
  tunnel adds no trust of its own.

## The client (`image-review review --remote`)

The viewer holds the preprocessed images in memory only. The tool makes no
deliberate attempt to write them to disk. Several things are outside its
control, so do not treat "RAM only" as a guarantee:

- **swap**: the operating system may page memory to disk;
- **crash dumps and core files**;
- **screenshots, screen recording and screen sharing**, which capture what is
  displayed;
- a compromised or shared laptop.

Keep the connection string out of `ps` by putting it in
`IMAGE_REVIEW_REMOTE` rather than on the command line. To keep it out of shell
history too, do not type it: use
`export IMAGE_REVIEW_REMOTE="$(ssh user@login-node cat <file>)"` (see the
tutorial), or start the line with a space and set `HISTCONTROL=ignorespace`.

## Local disk

A work directory holds PHI in several forms, and is never meant to be
world-readable.

- **Access policy** (`--access`, or `$IMAGE_REVIEW_ACCESS`):
  `private` (default) creates directories 0700 and files 0600;
  `group` creates directories 2770 (setgid) and files 0660 for the work
  directory's Unix group. No file is ever created with "other" bits. The tool
  only sets mode bits: it never runs `chgrp` and never manages ACLs, so the
  group is whatever the filesystem assigns (see the README for how to get the
  right one). POSIX default ACLs inherited from the parent directory can grant
  more access than owner or group: check with `getfacl`. Later writers recover the policy from the work directory's own
  mode.
- **Warnings.** `review`, `serve`, `status` and `export` warn if the work directory or
  `manifest.tsv` is accessible to other users (for example one made by an old
  version). They never change an existing directory's mode; run
  `chmod -R o-rwx <work dir>`.
- **Files that hold PHI:**
  - the batch JPGs, which show whatever text was burned in;
  - `manifest.tsv`, `skipped.tsv` and `preprocess.json`, which hold source
    paths (and `skipped.tsv` reasons, which also contain paths);
  - `review.tsv`, which holds source paths as `image_id`s plus reviewer names
    and timestamps;
  - job output and stderr logs: `preprocess` logs the source paths that
    failed and `review` logs keys it cannot load, and under `sbatch` these land
    in `slurm-*.out` (by default beside where you submitted). Keep job output
    inside the work directory or another private location with
    `#SBATCH --output`;
  - export files, whose `image_id` column is the source path and whose
    `source_sha256` is derived from file content.
- **Hidden temporary files.** Each is removed on every exit except a hard
  kill (`kill -9`, a node crash), which can leave it behind. Delete any you
  find, since they hold the same data:
  - `.NAME.partial`: `preprocess`'s staging directory next to the work
    directory (the next run tells you about it);
  - `.<file>.<random>.tmp`: the temporary file `export --output` writes beside
    its target;
  - `.review.tsv.*.tmp`: left by an interrupted `review.tsv` upgrade, inside
    the work directory; the tool ignores it.
- **git.** The repository's `.gitignore` excludes the usual work-directory
  names as a safety net, but do not create work directories inside a git
  checkout, and never commit one.
- **Backups and shared home directories.** The tool does not control where
  your site's backups or snapshots copy a work directory, or who can read a
  home directory shared between login and compute nodes (relevant to
  `~/.image-review/`). Put work directories where your site's data policy
  allows PHI.
- **Export files** follow the work directory's file mode and, for a group work
  directory, its group (0600 if the group cannot be set, with a warning).
  `export` refuses `--remote` so that source paths never travel.

## Integrity

- **JPG hash check.** `manifest.tsv` records the SHA-256 of each JPG. Viewing
  an image checks it, and the server answers 404 for a mismatch. A JPG changed
  or cut short after preprocessing is shown as an unloadable placeholder that
  can be marked DIRTY but never CLEAN. This rule is enforced by the bundled
  viewer, not the server: `/mark` accepts CLEAN for any known key, so a
  different client, or a hand-written request carrying the token, could mark
  such an image CLEAN. This is a current limit. (Work directories from older versions
  have no hashes in the manifest and are not checked.)
- **Audit log.** `review.tsv` is append-only, except that the tool drops a
  last line cut short by an interrupted write, and rewrites an old-format
  (five-column) file once on upgrade. Each verdict appends a row,
  synced to disk, with `reviewer`, `mode` (`single`, `grid` or `undo`),
  `grid_size` and `tool_version` beside the status, pass and timestamp. An
  undo appends restoring rows rather than deleting any. The log is plain text
  and is not signed: anyone who can write the work directory can edit it.
- **Export refusals.** `export` refuses while a writer holds the work
  directory (unless `--allow-live`, which warns), refuses a `review.tsv` whose
  last line was cut short, and refuses any field holding a control character,
  U+2028/U+2029, or starting with `"`, so a reader cannot be made to split or
  merge rows. Preprocess records an input whose name is not UTF-8 or holds
  such a character as a `failed` row under an escaped id and cleans such
  characters out of reasons, so a work dir made by this version never trips
  this refusal through an image_id or reason (a reviewer name starting with `"`,
  or a work dir from an earlier version, still can). `export` never overwrites
  an existing file. `reviewer` values can start
  with `=`, `+`, `-` or `@`: open the result as text, not by double-clicking it
  into a spreadsheet.
- **Worst-of rule.** A file's exported status is the worst of its parts (the
  image and its embedded icon): `DIRTY`, then `NOT_REVIEWED`, then
  `UNREVIEWED`, then `CLEAN`. It is `CLEAN` only if every part is. Inputs that
  failed to preprocess are `NOT_REVIEWED`, and inputs that were not images
  (a PDF, a Word file, ...) are `IGNORED`: nobody has looked at them, so treat
  them as possibly containing PHI. A FLAGGED image (DIRTY in an earlier pass,
  not yet re-reviewed) exports as `DIRTY`.
- **DIRTY-only placeholders.** The viewer shows an image that cannot be
  fetched, read or verified as a placeholder that accepts DIRTY but refuses
  CLEAN, so an image nobody could see is not cleared by the viewer (see the
  limit above).
- **What export does not vouch for.** It covers only the files and ZIP
  entries it lists, never a ZIP or directory as a whole. Ignored inputs (a
  DICOMDIR, a PDF, ...) are listed as `IGNORED`, never `CLEAN`: treat them as
  possibly containing PHI.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Report it privately
through GitHub private vulnerability reporting:
<https://github.com/jstorrs/image-review/security/advisories/new> (the
repository owner must enable it in the repository settings; if the link
reports it is unavailable, contact the owner through
[github.com/jstorrs](https://github.com/jstorrs) without details). Include the
version (`image-review --version` or `pip show image-review`), what you
observed and how to reproduce it. Do not include real PHI in a report.
