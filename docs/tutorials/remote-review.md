# Remote review on an HPC cluster

Review images where they are, without copying them off the cluster. The
server runs on a compute node and the viewer on your laptop:
`image-review serve` serves the preprocessed work directory over HTTPS, and
`image-review review --remote` is the viewer.

Install `[preprocess,codecs]` on the cluster (core alone is enough for a node
that only runs `serve`, `status` and `export`) and `[viewer]` on the laptop;
see [Installation](../install.md) and
[Cluster install without root](../install.md#cluster-install-without-root).
Use the same image-review version on both.

Original files, DICOM headers and source paths stay on the cluster. Only the
preprocessed JPGs (and their batch/file names and review statuses) travel,
over TLS with a pinned certificate, and are held in the viewer's memory.

To review in a browser instead, with nothing installed on the laptop but
`ssh`, see the experimental [Browser review over SSH](browser-review.md).

## 1. Preprocess on the cluster

Unchanged. Run it as a batch or interactive job, asking Slurm for several
cores: `--jobs` defaults to `$SLURM_CPUS_PER_TASK`, so rendering uses every
core you were given:

```bash
srun --cpus-per-task=8 --mem=16G image-review preprocess /data/scans.zip --work-dir /scratch/me/review_work
```

In an `sbatch` script, use `#SBATCH --cpus-per-task=8`. Without
`--cpus-per-task`, `$SLURM_CPUS_PER_TASK` is unset and `--jobs` falls back to
the CPUs the job may use (its CPU affinity, capped by the smallest cgroup v2 CPU quota of its cgroup and its ancestors). `scancel` (or the time limit) stops the workers and
leaves no work directory.

## 2. Serve from an interactive session

```bash
salloc ...                       # your site's usual options
srun --pty bash                  # or your site's interactive command
image-review serve --work-dir /scratch/me/review_work
```

On many Slurm sites `salloc` leaves you on the login node, so first get a
shell on the allocated node (as above), or run `srun --pty image-review serve
--work-dir ...` directly. `--pty` keeps stdout a terminal so the string is
printed; plain `srun` without `--pty` takes the connection-file path described
under batch mode below.

The server binds the node's hostname by default (`--bind` to override;
wildcard addresses are refused) and picks a free port (`--port` to choose
one). It prints the connection string plus ready-to-paste client commands:

```
ir://node042.cluster.example:41733/?token=...&fp=sha256:...
```

Treat the string like a password. Each server start generates a new token and
certificate, so a string from an earlier run no longer works.

## 3. Connect from your laptop

**Direct**, if compute nodes are reachable from your network:

```bash
image-review review --remote 'ir://node042.cluster.example:41733/?token=...'
```

**Through the login node**, if you can only reach that:

```bash
image-review review --remote 'ir://...' --via user@login-node
```

With `--via` the client runs `ssh` for you to forward a local port to the
compute node. Password or MFA prompts appear in your terminal, and the tunnel
closes when the client exits.

To keep the token out of your shell history, put it in the environment:

```bash
export IMAGE_REVIEW_REMOTE='ir://...'
export IMAGE_REVIEW_VIA=user@login-node     # optional
image-review review --mode grid
image-review status
```

The viewer behaves as it does locally (same flags, keys, passes and
resumption); `--work-dir` cannot be combined with `--remote`. `status
--remote` works too.

## 4. Batch mode (`sbatch`)

When stdout is not a terminal, `serve` does not print the string. It writes it
to `~/.image-review/connection-<host>-<port>.txt` (mode 0600) and removes the
file when the server stops (Ctrl-C, `scancel`, or the time limit). The path is
written to the job's output file.

```bash
#!/bin/bash
#SBATCH --job-name=image-review
#SBATCH --time=04:00:00
#SBATCH --output=image-review-%j.out

source /path/to/venv/bin/activate   # or your site's module load
image-review serve --work-dir /scratch/me/review_work
```

Then, on your laptop, copy the absolute path from the job output (do not use
`~`: your laptop's shell would expand it locally) and keep the string out of
`ps` and shell history by putting it in the environment:

```bash
export IMAGE_REVIEW_REMOTE="$(ssh user@login-node cat /home/me/.image-review/connection-node042.cluster.example-41733.txt)"
image-review review
```

This assumes your home directory is shared between the login and compute nodes.

## 5. Stopping and reconnecting

Stop the server with Ctrl-C, `scancel`, or by letting the allocation end.
Progress is saved on the server at every mark. If the connection drops, the
viewer shows "Lost connection to server - progress saved" and accepts only
quit: `q`, `Esc`, the gamepad's Start button, or closing the window. Quit, then
reconnect with the same string while the server is still running.

## Security

The connection string is a password: anyone holding it can view the images and
record verdicts while the server runs, so do not paste it into chat or tickets.
Use one reviewer per server, and remember that a work directory and any export
hold source paths. The [security model](../reference/security-model.md) is the full threat model
(what stays on the cluster, what travels, what the viewer cannot control, and
the local-disk and integrity rules), including its limits: swap, screenshots,
shared nodes and home directories, and multiple clients.

## Troubleshooting

**Troubleshooting `--via`:**

- The server logs one `connection error: SSLEOFError` line per client start.
  This is the client's readiness probe and is harmless.
- `channel N: open failed` from ssh means the login node cannot reach
  `NODE:PORT`.
- `--via` always authenticates afresh (ControlMaster sharing is disabled so no
  forward is left behind), so expect an MFA prompt each time.
- If your `ssh_config` has `LocalForward` lines for the login node (e.g. for
  Jupyter), `--via` can fail with "Address already in use". Use a separate
  `Host` alias without `LocalForward`.
- If ssh backgrounds itself (`ForkAfterAuthentication`), remove that option.

**Troubleshooting "work directory is in use":** the message names who holds
the work directory (user, node, pid, start time) and the lock file
(`review.lock` in the work directory). Finish or stop that session first. A
lock is cleared automatically only when the tool can verify that its process is
gone on the same machine since its last boot. A lock left on another node (for
example a `serve` job that was killed), or one whose process id has since been
reused, is not. If you are sure that process is gone (check `squeue`, or
`ps -p PID` on that node, and compare the start time), delete the lock file by
hand and run again:

```bash
rm /scratch/me/review_work/review.lock
```

**Troubleshooting "Cannot read work directory":** `review.tsv` or
`manifest.tsv` is malformed (for example a hand edit left a short row, a
status other than `CLEAN`/`DIRTY`, a non-numeric pass, or a hash that is not
64 lowercase hex characters). The message names the file and line. Fix or remove that line and run again; the tool never
repairs or drops rows on its own.

**Troubleshooting versions:** "server speaks API vN, this client vM" (or
"server is too old to report its API version") means the laptop and the
cluster have different image-review versions; install the same image-review
version on both machines. This release speaks wire API v7 (the server refuses
a grid CLEAN over a DIRTY or FLAGGED image), so upgrade the cluster and the
laptop together.
