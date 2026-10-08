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
  time before any routing (in the experimental socket mode, all but the three
  public page files; see below). The token is a password: anyone holding the
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

## Experimental: browser review over a Unix socket

`image-review serve --socket` serves a browser page instead of the pygame
client's HTTPS API. It is experimental and has weaker properties than the
default mode; use it only where you accept the points below.

- **No TLS.** ssh encrypts the path from the laptop to the node. On the node
  the socket carries plain HTTP, so anyone able to connect to it can read it.
- **The socket.** It is created 0600 in `~/.image-review/` (0700, owned by
  you). Linux enforces the socket file's own mode on connect; POSIX does not
  require `connect()` to check it and some older systems ignored it, so do
  not rely on it outside Linux. On a `--socket-path` you choose, only that
  check protects the socket. Root and your own processes can connect.
- **The page is public; the API is not.** `/`, `/app.js` and `/app.css`
  carry no PHI or secret and need no token. Every API route needs the token.
  The `Host` header must be `localhost`, `127.0.0.1` or `[::1]` (with an
  optional port), which defends against DNS rebinding. Responses carry a Content-Security-Policy and
  `Referrer-Policy: no-referrer`. The page reviews single images and
  grids; it reads the image list and statuses, asks for grid layouts,
  fetches each image and records verdicts and undos through the same API,
  with the token.
- **Grid layouts cost the server work.** In socket mode only, `POST /grids`
  lets a token holder make the server read image files (to learn each
  image's size from its header) and run the bin packer. It is bounded to at
  most 1000 known keys per request, grid sides of 256-16384 pixels, and one
  request computed at a time (others get 503), but the bound is loose: a
  realistic batch packs in seconds, while a worst-case request (1000 small
  images in a huge grid) takes tens of seconds to a few minutes. Packing is
  not cancelled when the client disconnects, so a reloaded page gets 503
  until it finishes, and meanwhile other requests are slowed, not blocked
  (an image fetch went from about 3 ms to 40-85 ms during a 28 s pack).
  Sizes read are cached for the life of the server, so a file replaced while
  the server runs keeps its old size; the page must check each decoded
  image against the size the server reported and leave a mismatch out of
  the grid verdict. Nothing about individual keys is logged.
- **The token is in the URL fragment**, which the browser never sends to the
  server. The page keeps it in per-tab `sessionStorage` and rewrites it out
  of the tab's history entry. It can still stay in browser history and
  autocomplete, and in the clipboard. By default it stops working when the
  server stops. Treat the URL like a password. When stdout is not a terminal it is written
  to `~/.image-review/browser-<host>-<pid>.txt` (0600, removed on exit), with
  the same exposure as the connection file above. Browser extensions that
  can read all sites can read the token and the images.
- **A reused token** (`$IMAGE_REVIEW_TOKEN`, socket mode only) no longer dies
  at a restart, only when the job's shell ends. `serve` writes it to disk
  only in the 0600 URL file when stdout is not a terminal (removed on exit);
  a copy of that file (e.g. in a home snapshot) stays valid for the whole
  job. It lives in that shell's environment, which you and root can read (on
  Linux, via `/proc`). Generate it inside the job (the `srun` shell or the
  batch script), not before submitting. `sbatch` copies the submit
  environment (by default `--export=ALL`), which slurmctld stores with the
  job, and with `AccountingStoreFlags=job_env` `sacct --env-vars` shows it.
  A token exported before `salloc` would outlive the job in your login shell
  and be reused by the next job. In a batch script, generate the token
  there; never paste a literal value, because Slurm stores the script (and
  with `AccountingStoreFlags=job_script`, `sacct --batch-script` shows it).
  Do not put the token on a command line (it would show in `ps` and shell
  history). `serve` accepts only 22-256 characters of `A-Za-z0-9_-` (use 128
  random bits) and never prints or logs it except in the URL.
- **The laptop side.** The printed command forwards `127.0.0.1:8080` only, and
  the URL names `127.0.0.1`: ssh given a bare `-L 8080:...` also binds `::1`
  and succeeds if either bind works, so another process already on
  `[::1]:8080` could receive the browser and read the token. If you change
  the command, keep the explicit address. Other users on the laptop can reach
  `127.0.0.1:8080` while the command runs, but still need the token.
- **Images** are held in the browser's memory as blob URLs, each revoked
  once the next item is shown. In grid mode each image is decoded to an
  `ImageBitmap`, drawn into one `<canvas>` and closed once drawn; the canvas
  holds the grid's pixels until the next grid is drawn, and is cleared and its
  buffer freed (sized to 0) when the page leaves grid mode. Both carry the
  same swap, crash-dump and screenshot caveats as the viewer, plus whatever
  the browser itself does with its memory and caches. Pressing `q` (or
  closing the tab) forgets the token and the reviewer name and frees the
  images and the canvas on the page. Responses carry
  `Cache-Control: no-store`, so they should not be written to the disk cache.
- **Stale-socket races.** A server treats a socket whose connect is refused
  as stale and unlinks it. Two servers started at once on one explicit
  `--socket-path` can therefore unlink each other's socket, and on macOS a
  live server with a full backlog can look stale. On a home directory shared
  between nodes it is worse than a race: a connect to a live socket from
  another node is always refused, so a second server on another node with the
  same explicit path always replaces the first, which becomes unreachable.
  Use the default path (host and process id in the name) or a per-job one,
  for example containing `$SLURM_JOB_ID`.

**Questions for your HPC administrator**, if it does not work:
- Can you `ssh` to a compute node where you have a job (`pam_slurm_adopt`)?
- Is `AllowStreamLocalForwarding` enabled on compute-node sshd (the default;
  `DisableForwarding` must not be set)?
- Is `AllowTcpForwarding` enabled on the login node (needed for `-J`)?
- Do Unix sockets work on the shared home filesystem (NFS, GPFS, Lustre)?
- With `job_container/tmpfs`, does an adopted ssh session see the job's
  private `/tmp`?

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
  - export files (the allowlist and the report), whose `image_id` column is
    the source path and whose `source_sha256` is derived from file content.
- **Hidden temporary files.** Each is removed on every exit except a hard
  kill (`kill -9`, a node crash), which can leave it behind. Delete any you
  find, since they hold the same data:
  - `.NAME.partial`: `preprocess`'s staging directory next to the work
    directory (the next run tells you about it);
  - `.<file>.<random>.tmp`: the temporary file `export --output` or
    `--report` writes beside its target;
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
- **Export files** (`--output`, `--report`) follow the work directory's file
  mode and, for a group work directory, its group (0600 if the group cannot be
  set, with a warning). Neither overwrites an existing file, and they may not
  name the same file. Both are built and checked before either is written,
  and the report is written first, so a failed second write leaves only a
  report, which releases nothing. `export` refuses `--remote`
  so that source paths never travel.

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
  or a work dir from an earlier version, still can). The check covers the
  report's rows too, even without `--report`. `export` never overwrites
  an existing file. `reviewer` values can start
  with `=`, `+`, `-` or `@`: open the result as text, not by double-clicking it
  into a spreadsheet.
- **Allowlist (default deny).** `export`'s main output lists only the files
  that may be released, each by path and source SHA-256; release a file only
  if both match, and deny everything not listed. A file is listed only if its
  status is `CLEAN`, the manifest has its hash, and no file that is not
  `CLEAN` has the same hash (identical bytes cannot be both clean and dirty).
  A file's status is the worst of its parts (the image and its embedded icon):
  `DIRTY`, then `NOT_REVIEWED`, then `UNREVIEWED`, then `CLEAN`; it is `CLEAN`
  only if every part is. A FLAGGED image (DIRTY in an earlier pass, not yet
  re-reviewed) is `DIRTY`. So these are never listed: inputs that failed to
  preprocess (`NOT_REVIEWED`), inputs that were not images (a DICOMDIR, a PDF,
  ...: `IGNORED`), files whose name preprocess recorded under an escaped id
  (which never matches the real name), CLEAN files of a work directory
  without hashes, a file changed since preprocess (its hash no longer
  matches), and a ZIP or directory as a whole (only its entries are listed).
  Everything else goes to the optional `--report`, for audit and follow-up;
  it must never be used to choose what to release (e.g. "everything not
  DIRTY").
- **DIRTY-only placeholders.** The viewer shows an image that cannot be
  fetched, read or verified as a placeholder that accepts DIRTY but refuses
  CLEAN, so an image nobody could see is not cleared by the viewer (see the
  limit above).
- **Grid CLEAN over DIRTY or FLAGGED images.** One grid verdict covers every
  image in the grid, so both clients refuse CLEAN on a grid holding an image
  already DIRTY (this pass) or FLAGGED (DIRTY in an earlier pass), unless
  every image in it is DIRTY (reversing that grid's own verdict). The server
  refuses it too: `/mark` checks the store's current statuses under its lock
  and answers 409, recording nothing, so a client bug, or statuses gone stale
  because another client marked an image DIRTY meanwhile, cannot clear such
  an image through a grid. This does not lift the limit above: a `single`
  mark, from any client holding the token, can still mark any known image
  CLEAN.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Report it privately
through GitHub private vulnerability reporting:
<https://github.com/jstorrs/image-review/security/advisories/new> (the
repository owner must enable it in the repository settings; if the link
reports it is unavailable, contact the owner through
[github.com/jstorrs](https://github.com/jstorrs) without details). Include the
version (`image-review --version` or `pip show image-review`), what you
observed and how to reproduce it. Do not include real PHI in a report.
