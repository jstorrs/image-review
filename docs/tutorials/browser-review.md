# Browser review over SSH

Review images where they are, without copying them off the cluster, in a
browser on your laptop. This is the recommended way to review remotely. There
is nothing to install on the laptop but ssh and a browser, and nothing to keep
in sync: the page is served by `serve` itself, so it always matches the
server's version. (The deprecated
[Python client](remote-review.md) must run the same version as the server,
and a mismatch is refused.)

`image-review serve` runs on a compute node and listens on a Unix socket, and
your laptop forwards a local port to it with ssh. The traffic is plain HTTP
inside the ssh tunnel, with no TLS on the node. The page reviews single
images or, in grid mode, packed grids of a batch's images.
**Read the browser review section of
[the security model](../reference/security-model.md#browser-review-over-a-unix-socket)
first**: it lists the differences from the HTTPS mode (a URL that
holds the token) and the questions to ask your HPC administrator if
forwarding does not work.

Install `[preprocess,codecs]` where you preprocess and core alone where you
only `serve`; see [Installation](../install.md) and
[Cluster install without root](../install.md#cluster-install-without-root).

## 1. Preprocess on the cluster

Run `preprocess` as a batch or interactive job, asking Slurm for several
cores: `--jobs` follows `$SLURM_CPUS_PER_TASK`, so rendering uses every core
you were given.

```bash
srun --cpus-per-task=8 --mem=16G image-review preprocess /data/scans.zip --work-dir /scratch/me/review_work
```

In an `sbatch` script, use `#SBATCH --cpus-per-task=8`. See
[Parallel rendering](../commands/preprocess.md#parallel-rendering) for how
`--jobs` is chosen otherwise.

## 2. Review in the browser

1. On the cluster, get a shell on a compute node and start the server, naming
   your login node:

   ```bash
   salloc ...                       # your site's usual options
   srun --pty bash                  # or your site's interactive command
   image-review serve --work-dir /scratch/me/review_work --via me@login-node
   ```

   On many Slurm sites `salloc` leaves you on the login node, hence the
   `srun --pty bash`. If your laptop can ssh to compute nodes directly
   (`ssh me@<node>` works without a jump host), use `--direct` instead of
   `--via`; the printed command then has no `-J`. If the node name it prints
   does not resolve from your laptop, add `--ssh-host NAME` (a host name, no
   `user@`) with the name that does.

2. It prints an ssh command and, on a terminal, a URL. On your laptop, paste
   the ssh command and leave it running (password or MFA prompts appear
   there):

   ```
   ssh -N -o ExitOnForwardFailure=yes -o ControlPath=none -J me@login-node -L 127.0.0.1:8080:/home/me/.image-review/serve-node042-12345.sock me@node042.cluster.example
   ```

3. Open the URL in your browser: `http://127.0.0.1:8080/#TOKEN`. The token is
   a password; do not paste the URL into chat or tickets.

4. The page first asks "Who is reviewing?": type your name and press
   Enter. It is recorded with every verdict, as `--reviewer` is for the
   viewer. The page then shows the current pass's todo images one at a
   time. Press `?` for the list of keys: `c` marks an image CLEAN, `d`
   DIRTY, and `m` switches to grid mode, where one verdict covers a whole
   grid. [The browser page](../commands/browser.md) describes the keys, the
   bar, grid mode and every screen and message.

   Your marks are saved on the server as you make them. If the tunnel
   drops, the page stops and offers Reconnect: run the same ssh command
   again, then press Reconnect (or `r`). If the server was restarted, the
   old tunnel points at a socket that is gone (the default path includes
   the server's process id): stop the old ssh command (it holds port 8080,
   so the new one would fail), run the new command the server printed, and
   open its new URL (pasting it into the same tab works). See
   [Lost connection and Reconnect](../commands/browser.md#lost-connection-and-reconnect)
   for what the page says in each case.

   **Tip: keep the tunnel across restarts.** ssh connects to the socket
   only when the browser opens a connection, so a forward to a fixed path
   keeps working when the server is restarted on that path. Give each job
   its own path instead of the default. Set it once in the job's environment
   (see the [option rules](../commands/serve.md#option-rules) for when
   `serve` uses it):

   ```bash
   # Once per job:
   export IMAGE_REVIEW_SOCKET_PATH=~/.image-review/ir-$SLURM_JOB_ID.sock

   # Each start (after a restart, re-run only this line):
   image-review serve --work-dir /scratch/me/review_work --direct
   ```

   Leave the ssh command running. While the server is stopped the page says
   "Lost connection". With a fixed socket path and `$IMAGE_REVIEW_TOKEN`
   (below), a restart needs only Reconnect; after a restart with a fresh
   token (none exported) the page
   [rejects the token](../commands/browser.md#lost-connection-and-reconnect),
   and you paste the new URL into the same tab. Use a per-job name such as
   `$SLURM_JOB_ID`, not one shared between jobs: on a home directory shared
   between nodes, a second job with the same path would take over the first
   one's socket.

   To keep the URL too, give the job one token and export it as
   `$IMAGE_REVIEW_TOKEN`; `serve` then reuses it on every start in
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

   Both commands give 32 characters, which meet the
   [token rules](../commands/serve.md#token). Never put the token on a
   command line. After a restart, press Reconnect (or `r`) on the page: the
   forward and the URL are unchanged.

**Moving on to the next batch or pass.** With the fixed socket path and
`$IMAGE_REVIEW_TOKEN` exported in the job's shell, one tab and one ssh
command last the whole job:

1. Finish the pass (the page says the pass has nothing left to review, not
   just the end of a batch; see
   [End of a pass or batch](../commands/browser.md#end-of-a-pass-or-batch))
   or press `q` (or click Done; see [Done](../commands/browser.md#done)).
   Your marks are already saved, and the tab keeps the token and your name.
   `q` does not stop the server.
2. Stop the server with Ctrl-C on the node.
3. Start the next `image-review serve ...` in the same shell (same
   `$IMAGE_REVIEW_SOCKET_PATH` and `$IMAGE_REVIEW_TOKEN`), for the next batch
   or pass.
4. Press Reconnect (or `r`) on the page. It loads whatever the new server
   serves.

If you skip step 1 or 4 and keep reviewing in the old tab, nothing goes to
the wrong images: the new server refuses anything from a page loaded from
the old one, and the page stops and asks for Reconnect (see
[Lost connection and Reconnect](../commands/browser.md#lost-connection-and-reconnect)).
If Reconnect cannot reach the new server yet, press it again once it is up.

When you are finished for the day, stop the server with Ctrl-C, then the
ssh command, and close the tab: closing it is what forgets the token.

For how `--via`, `--direct` and `--ssh-host` shape the printed command, see
[Socket mode](../commands/serve.md#socket-mode) on the serve page.

**Batch mode.** Under `sbatch` stdout is not a terminal, so the URL is
written to a private file instead
([what serve prints](../commands/serve.md#socket-mode)). The job output gives
the ssh command and the file's path; fetch the URL from your laptop (use the
absolute path, not `~`):

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
- "work directory is in use": another session holds the work directory, and
  the message names it (user, node, pid, start time) and the lock file.
  Finish or stop that session first. A lock left on another node, for
  example by a `serve` job that was killed, is not cleared automatically;
  [`review.lock`](../reference/work-directory.md#reviewlock) says when it is
  safe to delete it by hand:

  ```bash
  rm /scratch/me/review_work/review.lock
  ```

- "Cannot read work directory": `review.tsv` or `manifest.tsv` is malformed,
  for example after a hand edit. The message names the file and line; fix or
  remove that line and run again. See
  [`review.tsv`](../reference/work-directory.md#reviewtsv) and
  [`manifest.tsv`](../reference/work-directory.md#manifesttsv).
- Prefer the default socket path. On a shared home directory, a second server
  on another node given the same explicit `--socket-path` takes the first's
  socket over and the first becomes unreachable. If you must choose a path,
  include `$SLURM_JOB_ID` in it.
