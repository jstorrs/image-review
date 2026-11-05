# Security model

image-review handles images that may carry burned-in Protected Health
Information (PHI). This page is the one place that states what the tool
protects, against whom, and where its protection ends. The commands are
described in [the command reference](../commands/index.md) and the file
formats in [the specification](specification.md). The practical HPC steps are in
[Browser review over SSH](../tutorials/browser-review.md).

## What is protected, and against whom

The aim is that PHI stays on the cluster, and that the people who can reach it
are the people you chose.

- **Original files, DICOM headers, source paths and `review.tsv`** stay where
  `preprocess` ran. What travels to a reviewer's machine is the preprocessed
  JPGs, their batch and file names (`batch_001/img_00001.jpg`), review
  statuses and pass numbers, and the reviewer name the page sends.
- **The threats considered** are other users on shared cluster nodes (and,
  for a local `review`, on the same machine) and network observers or
  impostors between the laptop and the cluster.
- **Not considered:** a malicious cluster administrator or root user, a
  compromised laptop or cluster account, and the reviewer themselves (who sees
  the images by design). Anyone who can read the work directory as your Unix
  user, or as a member of its group under `--access group`, can read all of it.

The tool does not certify that a file is free of PHI. It records the verdicts
of human reviewers.

## The server (`image-review serve`)

`serve` serves browser review on a Unix socket, and `review` serves the same
page on 127.0.0.1; this section applies to both. What is particular to each
is in [Browser review over a Unix socket](#browser-review-over-a-unix-socket)
and [Local review on loopback](#local-review-on-loopback).

- **Token.** Every request but those for the four public page files (see
  below) must carry the bearer token, compared in constant time before any
  routing. The token is a password: anyone holding the URL can view the
  images and record verdicts while the server runs. Do not paste it into
  chat or tickets. Unless it comes from `$IMAGE_REVIEW_TOKEN` (`serve`
  only), a new token is generated at each start, so an old URL stops working.
- **Logging.** One INFO line per request: peer address, method, path without
  its query string, and status. Access tokens, `Authorization` headers, query
  strings, image keys, `image_id`s, source paths, request bodies, and the
  messages of exceptions raised while serving (only their class names) are
  never logged in the request line or the error records. No log record
  contains the token. Client-supplied
  fields in server records are escaped, so control characters cannot reach the
  terminal. Other records are not so restricted: startup and work-directory
  messages (for example a `review.tsv` torn-line warning or a failure opening
  the work directory) can name file paths and parse errors. `preprocess`
  warnings name the failed source file (its `image_id`), on the machine where it runs. Levels
  and format are in [Commands](../commands/index.md#logging).
- **Caching.** Every response carries `Cache-Control: no-store` (and
  `X-Content-Type-Options: nosniff`).
- **`image_id`s never leave the server.** The manifest's `image_id` is the
  source path and may hold identifiers. The wire carries only preprocessed
  paths (keys), statuses, grid layouts and JPG bytes.
- **Reviewer identity is a claim.** The `reviewer` name in each mark is sent by
  the client and recorded as given (1-64 printable characters). The server
  does not authenticate it: whoever holds the token can record verdicts under
  any name. Treat the `reviewer` column as unverified.
- **Single writer.** `serve` and a local `review` each hold the work
  directory's lock, so a second writer exits with an error naming the holder;
  `status` and `export` only read. A lock the tool cannot prove stale is left
  for you to delete by hand
  ([`review.lock`](work-directory.md#reviewlock)).
- **Multi-client limits.** The server assumes one reviewer per server. Its
  undo stack is a single one, shared by every client, so `z` in one client
  undoes the latest mark from any of them. Each client works from its own
  snapshot of the statuses and may not see another client's marks until it
  refetches; in `review.tsv` the last row for an image wins. Serving several
  clients at once is out of scope.
- **Connection limits.** The server holds at most 64 connections at once and
  closes the next at accept, before it gets a thread. Each request must be
  read whole within 60 s, however slowly its bytes arrive, and its request
  line and headers may total at most 64 KiB; only after the token check may a
  body (at most 1 MiB) follow. A connection without the token gets one
  request: every reply to it closes the connection. Anyone who can reach the
  server without the token (see [The laptop side](#browser-review-over-a-unix-socket))
  can still fill the 64 slots and keep the reviewer out. Each connection lasts
  at most one request deadline (60 s), but by reconnecting as slots free they
  can keep the reviewer out for as long as they keep at it. What they can no
  longer do is exhaust the serve user's processes on the node (each
  connection used to start a thread, counted against `RLIMIT_NPROC`, with no
  cap) or its memory: a tokenless request can make the server read at most
  64 KiB, 4 MiB across all 64 slots plus the parser's copies (each connection
  used to be able to make it buffer about 6.5 MB of headers, over 50 MB while
  parsing them).

## Browser review over a Unix socket

This is what `image-review serve` does, and the way to review remotely. It
serves a browser page on a Unix socket on the compute node, reached through
`ssh -L`. There is no TLS on the node, and the token is in the URL you open.
The points below are its limits.

- **No TLS.** ssh encrypts the path from the laptop to the node. On the node
  the socket carries plain HTTP, so anyone able to connect to it can read it.
- **The socket.** It is created 0600 in `~/.image-review/` (0700, owned by
  you). Linux enforces the socket file's own mode on connect; POSIX does not
  require `connect()` to check it and some older systems ignored it, so do
  not rely on it outside Linux. On a `--socket-path` you choose, only that
  check protects the socket. Root and your own processes can connect.
- **The page is public; the API is not.** `/`, `/helpers.js`, `/app.js` and
  `/app.css` carry no PHI or secret and need no token. Every API route needs the token.
  The `Host` header must be `localhost`, `127.0.0.1` or `[::1]` (with an
  optional port), which defends against DNS rebinding. Responses carry a Content-Security-Policy and
  `Referrer-Policy: no-referrer`. The page reviews single images and
  grids; it reads the image list and statuses, asks for grid layouts,
  fetches each image and records verdicts and undos through the same API,
  with the token.
- **Requests that name another origin are refused.** A request carrying an
  `Origin` header passes only if it has exactly one, and it is `http://`
  followed by the request's own `Host` value (the host part ignoring case), after that
  `Host` has passed its check: the page's own origin. Anything else, `null`,
  `https://`, another site or another local port (another web app on this
  machine) included, gets 400 before the token is checked, the page files
  included. The page itself always passes. Requests with no `Origin` header
  are not covered: another site's `<script>`, `<img>` and navigations send
  none, so they can still fetch the four public files, and get 401 on the
  API. The token is what protects the API, as a browser never attaches it
  on its own.
- **Grid layouts cost the server work.** `POST /grids` lets a token holder make the server read image files (to learn each
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
  server stops. Treat the URL like a password. When stdout is not a terminal
  it goes to `~/.image-review/browser-<short-host>-<pid>.txt`, a 0600 file in
  a 0700 directory you own, so other users cannot read it
  ([What serve prints](../commands/serve.md#what-serve-prints)). The
  remaining exposure is root on any node that mounts the home directory,
  backups or snapshots of it, and any process running as you. A hard kill
  leaves the file behind ([Stopping](../commands/serve.md#stopping)).
  Browser extensions that can read all sites can read the token and the
  images.
- **A reused token** (`$IMAGE_REVIEW_TOKEN`) no longer dies
  at a restart, only when the job's shell ends. `serve` writes it to disk
  only in the 0600 URL file when stdout is not a terminal (removed on exit);
  a copy of that file (e.g. in a home snapshot) stays valid for the whole
  job. It lives in that shell's environment, which you and root can read (on
  Linux, via `/proc`). Generated before submitting, it would leak: `sbatch`
  copies the submit environment (by default `--export=ALL`), which slurmctld
  stores with the job, and with `AccountingStoreFlags=job_env`
  `sacct --env-vars` shows it. A token exported before `salloc` would outlive
  the job in your login shell and be reused by the next job. A literal value
  in a batch script leaks too, because Slurm stores the script (and with
  `AccountingStoreFlags=job_script`, `sacct --batch-script` shows it), and
  one on a command line shows in `ps` and shell history. `serve` shows it
  only in the URL, never in an error or log. Use 128 random bits;
  [Token](../commands/serve.md#token) has the rules and where to generate it.
- **A page belongs to one serve.** With a fixed socket path and
  `$IMAGE_REVIEW_TOKEN`, the ssh forward and the token outlive a serve
  restarted on the next work directory, batch or pass, and keys
  (`batch_NNN/img_NNNNN.jpg`) repeat across work directories. So a tab left
  showing an image while serve is restarted elsewhere could otherwise send
  its verdict for that key to the new server, recording CLEAN for an image
  nobody saw there (every key of a grid). Each serve start therefore draws a
  random instance id, sent on every reply; the page keeps the one it loaded
  from and sends it on every request, and the server refuses (412) any API
  request but `/current_pass` that does not carry this
  server's id (none, a repeated one, or another), reading, recording and
  packing nothing, so the tab's reads cannot mix two servers either. The
  page then stops and asks for Reconnect, which loads the new server from
  scratch (see
  [Lost connection and Reconnect](../commands/browser.md#lost-connection-and-reconnect)).
  The id is not a secret (the token is the access control), is checked only
  after the token, and is never logged.
- **The laptop side.** The printed command forwards `127.0.0.1:8080` only, and
  the URL names `127.0.0.1`: ssh given a bare `-L 8080:...` also binds `::1`
  and succeeds if either bind works, so another process already on
  `[::1]:8080` could receive the browser and read the token. If you change
  the command, keep the explicit address. Other users on the laptop can reach
  `127.0.0.1:8080` while the command runs, but still need the token.
  Without it they can still fill the server's 64 connection slots through
  the forward (see [Connection limits](#the-server-image-review-serve)) and
  keep the browser out: each tokenless connection lasts one request, the
  public page files included, and at most 60 s, but by reconnecting they can
  keep the slots full for as long as they keep at it. They can no longer
  exhaust your processes or memory on the node.
  That holds only while the forward is up. If it ends (the laptop slept, the
  network dropped) and another local user binds `127.0.0.1:8080`, pressing
  Reconnect sends them the token, in the `Authorization` header of its first
  request. Reloading the tab does worse: their page runs at the same origin,
  so its script can read the token from `sessionStorage`, and the token then
  opens the real server for as long as it lives. The printed command carries
  `-o ExitOnForwardFailure=yes`, so an ssh restarted on a taken port fails
  loudly instead of running without a forward. Check that ssh is running (and
  has not failed) before you press Reconnect or reload, and on a shared laptop
  close the tab when the tunnel drops. See
  [Lost connection and Reconnect](../commands/browser.md#lost-connection-and-reconnect).
- **Images** are held in the browser's memory as blob URLs, each revoked
  once the next item is shown. In grid mode each image is decoded to an
  `ImageBitmap`, drawn into one `<canvas>` and closed once drawn; the canvas
  holds the grid's pixels until the next grid is drawn, and is cleared and its
  buffer freed (sized to 0) when the page leaves grid mode. Memory is not a
  guarantee: swap, crash dumps and core files can write it to disk, and
  screenshots, screen recording and screen sharing capture what is shown,
  as can whatever the browser itself does with its memory and caches. Pressing `q` frees
  the images and the canvas on the page but keeps the token and the
  reviewer name, so Reconnect can load the next server. They stay in the
  tab's `sessionStorage` until the tab is closed; the page has no button to
  forget them (a 401 deletes the token, and a new URL pasted into the tab
  replaces it). Responses carry
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

## Local review on loopback

This is what `image-review review` does: it serves the browser page on
`127.0.0.1` on this machine, over plain TCP. Everything in
[The server](#the-server-image-review-serve) applies.

- **The token's path.** `review` writes the URL into
  `~/.image-review/review-<short-host>-<pid>.html`, a 0600 file in a 0700
  directory you own, and starts the browser on that file's `file://` URI, so
  the token never appears on a command line (which other users can read, on
  Linux in `/proc/*/cmdline`). The file redirects to the URL, whose fragment
  holds the token; the browser never sends a fragment. The page moves it
  into per-tab `sessionStorage`, rewrites it out of the tab's history entry
  and sends it as a bearer `Authorization` header on every API request. On a
  terminal `review` also prints the URL; otherwise it prints only the file's
  path, so the token stays out of logs. The file is removed when `review`
  stops; a hard kill leaves it behind
  ([Stopping](../commands/review.md#stopping)). As with `serve`, the URL can
  stay in browser history, and browser extensions that can read all sites
  can read the token and the images.
- **127.0.0.1, never `localhost`.** The server binds `127.0.0.1` only, and
  the URL names it. Browsers try `::1` first for `localhost`, and the server
  does not bind `[::1]:PORT`, so another local user could listen there and
  receive the token with the browser's first request.
- **Host and Origin.** The `Host` header must be exactly `127.0.0.1:PORT`,
  with this server's port; `localhost`, `[::1]`, another port or no port get
  400 before the token is checked. A request with an `Origin` header passes
  only if it is `http://127.0.0.1:PORT`, the page's own origin, as on the
  socket (see
  [Requests that name another origin are refused](#browser-review-over-a-unix-socket)).
- **Who else can connect.** A loopback port is open to every user on the
  machine it is on. With `review` that is the machine holding the data; with
  `serve`, reached from your laptop, the only TCP port is the ssh forward on
  the laptop, and the socket on the node is 0600.

  | | `review` | `serve`, browser on your laptop |
  |---|---|---|
  | Who can connect | any user on this machine, though only the token opens the API | users of your laptop, through the forward; on the node, only you and root |
  | Denial of service | any user on this machine can fill the 64 connection slots and keep your browser out while `review` runs | users of your laptop can, through the forward (see [The laptop side](#browser-review-over-a-unix-socket)) |
  | After the server stops | another user on this machine can listen on the freed port; a tab still open sends them its (now dead) token on Reconnect, and a reload runs their page at the same origin, reading anything else in that tab's `sessionStorage` | the same on your laptop if the forward ends and another laptop user takes its port |

  With `review` the token dies with the server, so the after-stop case leaks a
  token that no longer opens anything, plus the reviewer name. Close the tab
  when you stop `review`, and open the new URL each time it starts.
- **Use `review` on a machine only you log in to** (your own laptop or
  workstation). **On a shared host** (a login or compute node, or any machine
  other people log in to), **review from a browser on another machine, your
  laptop, with `serve`.** A browser on the shared host itself is exposed to
  the other users there either way: a local TCP forward to `serve`'s socket
  on that host is as open to them as `review`'s port.

## Local disk

A work directory holds PHI in several forms, and is never meant to be
world-readable.

- **Access policy.** By default the tool creates work-directory files for
  the owner only. With `--access group` it creates them readable and writable
  by the work directory's Unix group (0660 files, 2770 directories). No file
  is ever created with "other" bits
  ([Access control](../commands/preprocess.md#access-control)).
  The tool only sets mode bits: it never runs `chgrp` and never manages ACLs,
  so the group is whatever the filesystem assigns, and POSIX default ACLs
  inherited from the parent directory can grant more access than owner or
  group (check with `getfacl`).
- **Warnings.** `review`, `serve`, `status` and `export` warn when a local
  work directory or its `manifest.tsv` is accessible to all users, but never
  change an existing directory's mode: you run
  `chmod -R o-rwx <work dir>`.
- **Files that hold PHI:**
  - the batch JPGs, which show whatever text was burned in;
  - `manifest.tsv`, `skipped.tsv`, `notes.tsv` and `preprocess.json`, which
    hold source paths (and `skipped.tsv` reasons, which also contain paths);
  - `review.tsv`, which holds source paths as `image_id`s plus reviewer names,
    timestamps and each verdict's `source_sha256` (derived from file content,
    like the manifest's);
  - job output and stderr logs: `preprocess` logs the source paths that
    failed, and under `sbatch` these land
    in `slurm-*.out` (by default beside where you submitted). Keep job output
    inside the work directory or another private location with
    `#SBATCH --output`;
  - export files (the allowlist and the report), whose `image_id` column is
    the source path and whose `source_sha256` is derived from file content.
- **Hidden temporary files** hold the same data as the files they stand in
  for. A hard kill (`kill -9`, a node crash) can leave them behind; delete
  any you find
  ([Leftover temporary files](work-directory.md#leftover-temporary-files)).
- **git.** The repository's `.gitignore` excludes the usual work-directory
  names as a safety net, but do not create work directories inside a git
  checkout, and never commit one.
- **Backups and shared home directories.** The tool does not control where
  your site's backups or snapshots copy a work directory, or who can read a
  home directory shared between login and compute nodes (relevant to
  `~/.image-review/`). Put work directories where your site's data policy
  allows PHI.
- **Export files** (`--output`, `--report`) get the work directory's access,
  never overwrite an existing file, and a failed write leaves at most a
  report, which releases nothing
  ([Refusals and output files](../commands/export.md#refusals-and-output-files)).
  `export` reads only a work directory on its own machine, so source paths
  never travel.

## Integrity

- **JPG hash check.** `manifest.tsv` records the SHA-256 of each JPG. Viewing
  an image checks it, and the server answers 404 for a mismatch. A JPG changed
  or cut short after preprocessing is shown as an unloadable placeholder that
  can be marked DIRTY but never CLEAN. This rule is enforced by the browser
  page, not the server: `/mark` accepts CLEAN for any known key, so a
  different client, or a hand-written request carrying the token, could mark
  such an image CLEAN. This is a current limit. (Work directories from older versions
  have no hashes in the manifest and are not checked.)
- **Audit log.** `review.tsv` is append-only, except that the tool drops a
  last line cut short by an interrupted write, and rewrites an old-format
  (five- or nine-column) file once on upgrade. Each verdict appends a row,
  synced to disk, with `reviewer`, `mode` (`single`, `grid` or `undo`),
  `grid_size`, `tool_version` and the source file's `source_sha256` from the
  manifest beside the status, pass and timestamp. An
  undo appends restoring rows rather than deleting any. The log is plain text
  and is not signed: anyone who can write the work directory can edit it.
- **Verdicts are tied to content.** A verdict counts only while its
  `source_sha256` matches the manifest's for that `image_id`. If the file at a
  path is replaced and preprocessed again, or `review.tsv` is copied into a
  work directory rebuilt from other files, the old verdict counts as none: the
  image comes back for review, and `export` reports it `UNREVIEWED` and never
  allowlists it on that verdict
  ([`review.tsv`](work-directory.md#reviewtsv)). This is a current limit:
  rows with an empty `source_sha256` (recorded before the column existed, or
  carried over by an upgrade) and any rows read against a manifest without
  hashes cannot be checked and are still trusted by path alone. The hash is
  as good as the manifest: anyone who can write the work directory can edit
  both files.
- **Export refusals.** `export` refuses while a writer holds the work
  directory (unless `--allow-live`, which exports anyway with a warning),
  refuses a `review.tsv` whose last line, or last grid mark, was cut short,
  refuses a malformed `skipped.tsv` or `notes.tsv` or (beside a manifest with hashes) a missing
  one, so a partial copy cannot drop failed inputs or unreviewed content from
  the decision, and refuses any field
  holding a control character, U+2028/U+2029, or a leading `"`, so a reader
  cannot be made to split or merge rows
  ([Refusals and output files](../commands/export.md#refusals-and-output-files)).
  Preprocess records an input whose name is not UTF-8 or holds a control
  character or U+2028/U+2029 as a `failed` row (never rendered, so never
  reviewed) under an escaped id, and cleans such characters out of reasons,
  so a work dir made by this version never trips this refusal through an
  image_id or reason (a reviewer name starting with `"`, or a work dir from
  an earlier version, still can).
  `reviewer` values can start with `=`, `+`, `-` or `@`: open the result as
  text, not by double-clicking it into a spreadsheet.
- **Allowlist (default deny).** `export`'s main output lists only the files
  that may be released, each by path and source SHA-256; release a file only
  if both match, and deny everything not listed
  ([What is allowlisted](../commands/export.md#what-is-allowlisted)). A file
  is listed only if it was reviewed CLEAN (and, in a work directory made by
  an older version, its `#icon` row too), `notes.tsv` lists no pixel content
  of it that its review image does not show, the manifest has its hash, and no
  file that is not CLEAN has the same hash (identical bytes cannot be both
  clean and dirty). So these
  are never listed: inputs that failed to preprocess, inputs that were not
  images, files whose name preprocess recorded under an escaped id (which
  never matches the real name), a file whose name ends in `#icon` with no
  file of the name without it, or beside one with a different hash (then
  neither is listed: it cannot be told apart from an older version's DICOM
  icon), CLEAN
  files of a work directory without hashes, a file changed since
  preprocess (its hash no longer matches), and a ZIP or directory as a whole
  (only its entries are listed). Nor is a file holding an embedded icon or
  thumbnail, overlay planes, pixel bits above BitsStored, or trailing bytes
  that are not all zero, none of which the review image reliably shows: it
  is `NOT_REVIEWED`. This holds for overlay planes even with `preprocess
  --draw-overlays`, since a drawn plane is clipped to the image and can be
  lost against bright pixels. Stripping such content changes the hash, so
  the stripped file must be preprocessed into a new work directory and
  reviewed there. Everything else goes to the optional `--report`, for
  audit and follow-up; it must never be used to choose what to release
  (e.g. "everything not DIRTY"). Only what preprocess detects is
  noted: content it does not look for (see [Input
  notes](../commands/preprocess.md#input-notes), *Not detected*) can still
  ship unreviewed in an allowlisted file.
- **DIRTY-only placeholders.** The page shows an image that cannot be
  fetched, read or verified as a placeholder that accepts DIRTY but refuses
  CLEAN, so an image nobody could see is not cleared by the page (see the
  limit above).
- **Grid CLEAN over DIRTY or FLAGGED images.** One grid verdict covers every
  image in the grid, so the page refuses CLEAN on a grid holding an image
  already DIRTY (this pass) or FLAGGED (DIRTY in an earlier pass), unless
  every image in it is DIRTY (reversing that grid's own verdict). The server
  refuses it too: `/mark` checks the store's current statuses under its lock
  and answers 409, recording nothing, so a client bug, or statuses gone stale
  because another client marked an image DIRTY meanwhile, cannot clear such
  an image through a grid. An image whose only DIRTY verdict was given on
  content its file no longer has (replaced and preprocessed again) is
  UNREVIEWED, not DIRTY, so a grid can clear it: that verdict was never about
  the content now shown (see *Verdicts are tied to content* above). This does
  not lift the limit above: a `single` mark, from any client holding the
  token, can still mark any known image CLEAN.

## Reporting a vulnerability

Do not report a vulnerability in a public issue. Follow the
[security policy](https://github.com/jstorrs/image-review/security/policy).
