# Browser review over SSH (experimental)

An experimental alternative to the pygame viewer: a browser on your laptop,
with nothing installed there but `ssh`. The traffic is plain HTTP inside the
ssh tunnel, with no TLS on the node. The server listens on a Unix socket on
the compute node and your laptop forwards a local port to it. The page reviews
single images or, in grid mode, packed grids of a batch's images.
**Read the experimental section of
[the security model](../reference/security-model.md#experimental-browser-review-over-a-unix-socket)
first**: it lists the differences from the HTTPS mode (a URL that
holds the token) and the questions to ask your HPC administrator if
forwarding does not work.

For the HTTPS mode with the pygame viewer, and the Slurm basics used below,
see [Remote review on an HPC cluster](remote-review.md).

1. On the cluster, get a shell on a compute node (`salloc`, then `srun --pty
   bash`) and start the server, naming your login node:

   ```bash
   image-review serve --work-dir /scratch/me/review_work --socket --via me@login-node
   ```

   If your laptop can ssh to compute nodes directly (`ssh me@<node>` works
   without a jump host), use `--direct` instead of `--via`; the printed
   command then has no `-J`. If the node name it prints does not resolve from
   your laptop, add `--ssh-host NAME` (a host name, no `user@`) with the name
   that does.

2. It prints an ssh command and, on a terminal, a URL. On your laptop, paste
   the ssh command and leave it running (password or MFA prompts appear
   there):

   ```
   ssh -N -o ExitOnForwardFailure=yes -o ControlPath=none -J me@login-node -L 127.0.0.1:8080:/home/me/.image-review/serve-node042-12345.sock me@node042.cluster.example
   ```

3. Open the URL in your browser: `http://127.0.0.1:8080/#TOKEN`. The token is
   a password; do not paste the URL into chat or tickets.

4. The page first asks "Who is reviewing?": type your name (1-64
   characters; it is recorded with every verdict, as `--reviewer` is for the
   viewer) and press Enter. The tab remembers it; the bar at the bottom
   shows it as "Jane ✎", and clicking that lets you change it (Escape keeps
   the old name). The page starts in single mode, showing the current pass's
   UNREVIEWED and FLAGGED images one at a time, in random order. Press `?`
   (or `h`, or the "?" button) for the list of keys:

   | Key | Button | Action |
   |-----|--------|--------|
   | `c` | Clean | Mark the image CLEAN and move on |
   | `d` | Dirty | Mark the image DIRTY and move on |
   | Right / Left | Next / Previous | Move through the list without marking |
   | `z` | Undo | Undo the latest mark and show that image (or grid) again |
   | `m` | -- | Grid mode for the current batch (rotation `auto`) |
   | `M` | -- | Grid mode without rotating images (rotation `never`) |
   | `s` | -- | Back to single mode |
   | `b` | -- | Grid mode: the next batch with images to review |
   | `r` | Reconnect | After "Lost connection", after `q` or at the end of a pass: load the review |
   | `q` | Done | Done with this server: the page waits for the next one |
   | `?` or `h` | ? | Show or hide the help (Escape also closes it) |

   Everything is in one bar at the bottom of the page: on the left the review
   buttons, the status word and the scale; on the right your name, "?" and
   Done (or Reconnect); in the middle a message (what was undone, refusals,
   errors) when there is one, otherwise the mode, the progress and the image's
   batch and key. Your own moves (arrows, marking, switching mode) clear the
   message; an undo or Reconnect says what it did. After `c` or `d` the image
   stays up for a moment (200 ms) with the bar in its new colour, then the
   next one appears and no message is left, so the progress stays in view
   while you mark. In a window about 1400 pixels wide or less the middle part
   gets a row of its own under the buttons. The bar's colour is the current
   item's status, also written in it: grey UNREVIEWED, green CLEAN, red DIRTY,
   amber FLAGGED (grey too on the end and stop-sign screens and once the
   page has stopped).
   Text too long for its place is cut short; hover over it to read it whole.
   While the help or the name box is open nothing can be marked, and once it
   closes the page waits the 200 ms again before a verdict counts.

   Right past the last item (or Left before the first) shows a stop sign,
   "End of the list", with how many todo items the list (in grid mode, the
   batch) still has; press the arrow again to go round to the other end:
   Right goes to the first item and Left to the last. There is no image on
   the stop sign, so `c` and `d` do nothing; `z`, `m`, `s`, `b` and `q` work
   as anywhere. Once nothing in the pass is left to review, the arrows past
   an end show the "nothing left to review" screen instead.

   A verdict counts only once the image has been on screen for 200 ms, so a
   key pressed as an image appears is ignored. An image that cannot be
   loaded shows "Cannot load image: KEY" and can be marked DIRTY but never
   CLEAN. The bar shows the display scale as a percent; below 100% it stands
   out as a badge such as "⚠ 46%": the image is shrunk to fit and small
   burned-in text can be lost, so enlarge the window or go full screen
   (browser zoom does not help: it makes the page's text larger and the
   image's share smaller).
   `z` says "Nothing to undo" once this page has no marks left to undo.
   With one page per server it only
   undoes this page's marks; the server keeps a single undo history, so with
   a second tab or client it undoes the latest mark from any of them (see
   "Multi-client limits" in [the security model](../reference/security-model.md#the-server-image-review-serve)), and the page warns
   "Undid another client's mark". When the whole pass is done the page says
   "Pass N: nothing left to review" and offers Reconnect for the next
   server. In grid mode the list is one batch, so at its end the page says
   "No todo images remaining - [b] next batch" while another batch has
   grids, or asks for `s` while FLAGGED images remain.

   Grid mode works as in the viewer (see the [review](../commands/review.md) page), one batch at a
   time:

   - One verdict covers every image in the grid: look at all of them before
     pressing `c`. `d` marks them all DIRTY.
   - CLEAN is refused ("grid contains an image already marked DIRTY") if any
     image in the grid is already DIRTY or FLAGGED, unless every image in it
     is DIRTY (which reverses that grid's own verdict); review it in single
     mode.
   - Grids hold only UNREVIEWED images; FLAGGED ones need single mode. An
     image that fails to load or decode leaves a black gap and follows the
     grids as a single item, as do images that did not fit a grid. These
     single items are judged one at a time.
   - Resizing the window repacks the grids for the new size and clears undo:
     `z` then says "Nothing to undo". On the "nothing left to review" screen
     `z` still works after a resize; the grids are repacked when you leave
     that screen.
   - A batch of more than 1000 images is too large for the browser's grid
     mode; review it in single mode (`s`).

   Your marks so far are always saved on the server. If the tunnel drops,
   the page says "Lost connection" and shows a Reconnect button: run the
   same ssh command again, then press Reconnect (or `r`). The page never
   retries by itself. Reconnect reloads the statuses and rebuilds the list
   in the mode you were in (grid mode lands on the grid you were on), forgets
   which marks `z` could undo ("Nothing to undo" until you mark again) and
   waits the 200 ms again before a verdict counts. If the server was
   restarted, the old tunnel points at a socket that is gone (the default
   path includes the server's process id), so the page also says "Lost
   connection": stop the old ssh command (it holds port 8080, so the new one
   would fail), run the new command the server printed, and open its new URL
   (pasting it into the same tab works). The page says "token rejected -
   open the new URL" when a restarted server reuses the same socket path
   with a new token.

   **Tip: keep the tunnel across restarts.** ssh connects to the socket
   only when the browser opens a connection, so a forward to a fixed path
   keeps working when the server is restarted on that path. Give each job
   its own path instead of the default. Set it once in the job's environment
   (`serve` uses `$IMAGE_REVIEW_SOCKET_PATH` only with `--socket`):

   ```bash
   # Once per job:
   export IMAGE_REVIEW_SOCKET_PATH=~/.image-review/ir-$SLURM_JOB_ID.sock

   # Each start (after a restart, re-run only this line):
   image-review serve --work-dir /scratch/me/review_work --socket --direct
   ```

   (`--socket-path PATH` on the command line does the same and overrides the
   variable.)

   Leave the ssh command running. While the server is stopped the page says
   "Lost connection". With a fixed socket path and `$IMAGE_REVIEW_TOKEN`
   (below), a restart needs only Reconnect; after a restart with a fresh
   token (none exported) Reconnect says "token rejected", and you paste the
   new URL into the same tab. Use a per-job name such as `$SLURM_JOB_ID`,
   not one shared between jobs: on a home directory shared between nodes, a
   second job with the same path would take over the first one's socket.
   (Not yet tested on a cluster.)

   To keep the URL too, give the job one token and export it as
   `$IMAGE_REVIEW_TOKEN`; `serve --socket` then reuses it on every start in
   that shell. Generate it inside the job, in the `srun` shell or in the batch
   script, never before `sbatch` or `salloc`: `sbatch` copies your
   environment into Slurm's records of the job (which administrators can
   read), and a token exported before `salloc` would be reused by your next
   job. Never paste a literal value into a batch script either; Slurm stores
   the script. Either of these works; after a restart, re-run only the `serve` line:

   ```bash
   # Once per job (re-running a token line makes a new token and a new URL):
   export IMAGE_REVIEW_TOKEN=$(openssl rand -hex 16)
   # or:
   export IMAGE_REVIEW_TOKEN=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')
   ```

   It must be 22-256 characters from letters, digits, `_` and `-` (the two
   commands above give 32); `serve` refuses anything else without printing
   it, and ignores the variable without `--socket`. Never put the token on a
   command line. After a restart, press Reconnect (or `r`) on the page: the
   forward and the URL are unchanged.

**Moving on to the next batch or pass.** With the fixed socket path and
`$IMAGE_REVIEW_TOKEN` exported in the job's shell, one tab and one ssh
command last the whole job:

1. Finish the pass (the page says "Pass N: nothing left to review", not
   just the end of a batch) or press `q` (or click Done). `q` stops the
   page and frees the images; your marks are already saved, and the tab
   keeps the token and your name. It does not stop the server.
2. Stop the server with Ctrl-C on the node.
3. Start the next `image-review serve --socket ...` in the same shell (same
   `$IMAGE_REVIEW_SOCKET_PATH` and `$IMAGE_REVIEW_TOKEN`), for the next batch
   or pass.
4. Press Reconnect (or `r`) on the page. It loads whatever the new server
   serves, from its first item (in grid mode, the first batch with grids),
   and says "Reconnected; now on pass N".

If you skip step 1 or 4 and keep reviewing in the old tab, nothing goes to
the wrong images: the new server refuses anything from a page loaded from
the old one (its keys may name the new work directory's images), so the
page records nothing, stops and says "Server restarted or changed work
directory - press Reconnect (r)" (or "Lost connection", if the new server is
not up yet). Press Reconnect, and judge
the images it then shows.

If Reconnect says "Lost connection", the new server is not up yet: press it
again. "token rejected" means the new server has another token: open its URL
in the tab. When you are finished for the day, stop the server with Ctrl-C,
then the ssh command, and close the tab: closing it is what forgets the
token.

Each start has a new token unless `$IMAGE_REVIEW_TOKEN` is set. `--via` only
fills in the `-J` part of the printed command; without it the command shows
`<user>@<login-node>` for you to fill in. `--direct` (or
`$IMAGE_REVIEW_DIRECT=1`) leaves `-J` out; it cannot be combined with `--via`
on the command line.

**Batch mode.** Under `sbatch` stdout is not a terminal, so the URL is
written to `~/.image-review/browser-<host>-<pid>.txt` (mode 0600) and removed
when the server stops. The job output gives the ssh command and the file's
path; fetch the URL from your laptop (use the absolute path, not `~`):

```bash
ssh me@login-node cat /home/me/.image-review/browser-node042-12345.txt
```

This assumes your home directory is shared with the login node. Run the ssh
command from the job output first, then open the URL. With `--direct` the
job output gives `ssh me@node cat ...` instead, since you reach the node
itself. Under `sbatch` the node name is only known when the job runs, so set
it there if the default does not work from your laptop, e.g. `--ssh-host
"$(hostname -f)"` or a site-specific name.

## Troubleshooting

- `channel N: open failed: connect failed` from ssh: the node's sshd would
  not forward to the socket (a forwarding policy), or the socket on your home
  filesystem is not usable (some network filesystems do not support Unix
  sockets). Try a node-local path:
  `serve --socket-path "$(mktemp -d /tmp/ir.XXXXXX)/ir.sock"`, and use the
  command it prints. That socket exists only on that node.
- `channel 0: open failed: connect failed: Name or service not known`
  followed by `stdio forwarding failed`, with `-J`: the login node cannot
  resolve the compute node's name. If `ssh you@<node>` works from your
  laptop, rerun the server with `--direct` and use the command it prints.
- The printed node name does not resolve from your laptop: rerun with
  `--ssh-host` set to the name that works, and use the command it prints.
- Windows: the built-in OpenSSH client works (a direct forward to the socket
  was tested from Windows). If `-J` fails with `CreateProcessW failed
  error:2` or `posix_spawn: No such file or directory`, replace `-J
  you@login` with `-o ProxyCommand="C:\Windows\System32\OpenSSH\ssh.exe -W
  %h:%p you@login"`. In PowerShell the single quotes the command may print
  are fine; cmd.exe does not treat single quotes as quoting, which only
  matters if a printed piece was quoted (for example a path with spaces).
- `Permission denied (publickey,hostbased)`: `-J` makes your laptop
  authenticate to the compute node itself, through the login node, so the
  laptop's key must be accepted there (in the cluster's `authorized_keys`),
  not only on the login node.
- `bind [127.0.0.1]:8080: Address already in use` or "Could not request local
  forwarding": port 8080 is busy on your laptop. Change the number in
  `-L 127.0.0.1:8080:...` and in the URL.
- If your `ssh_config` enables `ControlPersist` for these hosts, a background
  master can keep a forward alive after Ctrl-C. The printed command sets
  `ControlPath=none` to avoid that; if you edit it, keep that option.
- "Socket path is N bytes; the limit is ...": use a shorter `--socket-path`.
- "Another server is listening on ...": pick a different `--socket-path`.
- Prefer the default socket path. On a shared home directory, a second server
  on another node given the same explicit `--socket-path` takes the first's
  socket over and the first becomes unreachable. If you must choose a path,
  include `$SLURM_JOB_ID` in it.
