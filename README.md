# image-review

A command-line tool for reviewing medical (DICOM) and general images for
burned-in Protected Health Information (PHI).

The workflow has four steps:

1. **Preprocess** raw DICOM and image files into normalized JPG batches.
2. **Review** the images interactively in a fullscreen viewer, in single or
   grid mode.
3. **Status** reports on review progress.
4. **Export** writes the allowlist of files that may be released.

To review images that stay on an HPC cluster, `serve` the work directory
there and review it from your laptop; see
[Reviewing on an HPC cluster](#reviewing-on-an-hpc-cluster).

## Installation

Requires Python >= 3.12.

```bash
pip install '.[all]'          # everything: preprocess, codecs and the viewer
```

That installs from a source checkout. From a wheel or a package index the same
extras apply, e.g. `pip install 'image-review[all]'`.

The dependencies are split into extras, so each machine installs only what its
commands need:

| Install | Commands | Adds |
|---|---|---|
| `pip install .` (core) | `serve`, `status`, `export`, `--help` | click, cryptography, rectpack |
| `pip install '.[viewer]'` | `review` (local or `--remote`) | pygame-ce, Pillow |
| `pip install '.[preprocess,codecs]'` | `preprocess` | pydicom, numpy, scikit-image, scipy, matplotlib, Pillow, tqdm; python-gdcm, pylibjpeg, pylibjpeg-openjpeg |

On a cluster with a laptop viewer (see
[Reviewing on an HPC cluster](#reviewing-on-an-hpc-cluster)), install
`[preprocess,codecs]` where you preprocess, core alone where you only `serve`,
and `[viewer]` on the laptop (`pip install '.[viewer]'`). A command whose
extra is missing exits 1 with
`this command needs the <extra> extra: pip install 'image-review[<extra>]'`.

`cryptography` is a core dependency (`serve` uses it for its TLS
certificate). `review --via` and `status --via` need an OpenSSH client on the
machine you run them on (built into macOS, Linux and Windows 10+).

**Cluster install without root.** Use a virtual environment in your own
space; no administrator rights are needed. Get Python 3.12 or later, either
from your site's module system (`module load python/3.12` is only an example;
module names vary by site) or from your own interpreter or `uv`, then:

```bash
module load python/3.12                  # or skip if python3.12 is already on PATH
python3.12 -m venv ~/venvs/image-review  # or: uv venv --seed --python 3.12 ~/venvs/image-review
source ~/venvs/image-review/bin/activate
pip install 'image-review[preprocess,codecs] @ git+https://github.com/jstorrs/image-review@<branch-or-tag>'
# or, from a clone of the repository:
pip install -e '.[preprocess,codecs]'
```

If the repository is private, the https URL needs credentials (or use an
`ssh://git@github.com/jstorrs/image-review` URL with a key on the cluster).
Activate the same environment in your `sbatch` scripts. Use the same
image-review version on the laptop and on the cluster.

**Compressed DICOMs.** JPEG, JPEG Lossless, JPEG-LS, JPEG 2000, HTJ2K and RLE
DICOMs are decoded with `python-gdcm`, `pylibjpeg` and `pylibjpeg-openjpeg`
(the `codecs` extra). Install the codecs wherever you preprocess DICOMs.

- Wheels exist for CPython 3.12 and 3.13 on Linux (x86_64 and aarch64), macOS
  (Intel and Apple silicon) and Windows (x86_64). On other platforms
  `python-gdcm` has no wheel and installation may fail.
- Without the codecs, `preprocess` still runs, but only what pydicom decodes
  by itself or through Pillow (e.g. RLE, JPEG 2000) renders. Other compressed
  DICOMs (e.g. JPEG Lossless, JPEG-LS) are listed in `skipped.tsv` as failed,
  `cannot decode <transfer syntax>: ...`.
- 12-bit JPEG Extended files cannot be decoded even with the codecs (the only
  decoder is GPL-licensed and is not used). They are listed in `skipped.tsv`
  as `cannot decode JPEG Extended (Process 2 and 4): ...`.

**Minimum dependency versions** (declared in `pyproject.toml` and checked by
running the test suite on CPython 3.12): click >= 8.2, matplotlib >= 3.7.3,
numpy >= 1.26, pydicom >= 3.0, Pillow >= 10.3 except 11.x (which misdecodes
multi-frame MPO JPEGs), scikit-image >= 0.22, scipy >= 1.11.2, tqdm >= 4.60,
pygame-ce >= 2.3.1, cryptography >= 41, python-gdcm >= 3.0.25,
pylibjpeg >= 2.0, pylibjpeg-openjpeg >= 2.0, and rectpack pinned at 0.2.2
(unmaintained; grid packing depends on its exact behavior).

rectpack is published only as a source distribution: a default `pip install`
builds it, but an offline or `--only-binary :all:` install needs its sdist or
a wheel you built beforehand. See [CHANGELOG.md](CHANGELOG.md) for what
changed between releases.

## Quick start

```bash
# Preprocess a directory of DICOMs or a ZIP archive
image-review preprocess /path/to/dicoms/ --work-dir ./review_work

# Pass 1: grid triage — quickly mark entire grids CLEAN or DIRTY
image-review review --mode grid

# Pass 2: single review — inspect only the flagged (pass-1 DIRTY) images individually
image-review review --mode single

# Check progress (--check: exit 1 until every image has a verdict)
image-review status

# Write the allowlist of files that may be released (path + SHA-256), and a report of the rest
image-review export --output allowlist.tsv --report report.tsv
```

The default work directory is `./review_work`. Don't create work directories
inside a git checkout (the repository's `.gitignore` excludes them as a
safety net).

## Commands

- [`image-review preprocess`](docs/commands/preprocess.md): Turn DICOM and image files into normalized JPG batches.
- [`image-review review`](docs/commands/review.md): Review the images interactively, in single or grid mode.
- [`image-review status`](docs/commands/status.md): Report on review progress.
- [`image-review export`](docs/commands/export.md): Write the allowlist of files that may be released.
- [`image-review serve`](docs/commands/serve.md): Serve a work directory so it can be reviewed remotely.

Logging and the options that go before the command are described in the
[commands overview](docs/commands/index.md).

## Reviewing on an HPC cluster

Review images where they are, without copying them off the cluster. The
server runs on a compute node and the viewer on your laptop. Install
`[preprocess,codecs]` on the cluster (core alone is enough for a node that
only runs `serve`, `status` and `export`) and `[viewer]` on the laptop; see
[Installation](#installation).

```bash
# On the cluster: get an interactive compute node and serve the work directory
salloc ...                      # your site's usual options
srun --pty bash                 # shell on the allocated node (if salloc leaves you on the login node)
image-review serve --work-dir ./review_work
# or in one step: srun --pty image-review serve --work-dir ./review_work

# On your laptop: paste the command `serve` printed, or
image-review review --remote 'ir://...'

# If the laptop can only reach the login node:
image-review review --remote 'ir://...' --via user@login-node
```

Original files, DICOM headers and source paths stay on the cluster. Only the
preprocessed JPGs (and their batch/file names and review statuses) travel,
over TLS with a pinned certificate, and are held in the viewer's memory.
**The connection string is a password.** [SECURITY.md](SECURITY.md) states
the threat model and its limits (swap, screenshots, shared nodes and home
directories, multiple clients). See
[TUTORIAL.md](TUTORIAL.md#reviewing-on-an-hpc-cluster) for the full workflow
and batch jobs.

**Use the same version on both machines.** This release speaks wire API v7
(the server refuses a grid CLEAN over a DIRTY or FLAGGED image), so upgrade
the cluster and the laptop together. If `--remote` reports "server speaks API
vN, this client vM" (or "server is too old to report its API version"),
install the same image-review version on both machines.

### Browser review over SSH (experimental)

An experimental alternative to the pygame viewer: a browser on your laptop,
with nothing installed there but `ssh`. The traffic is plain HTTP inside the
ssh tunnel, with no TLS on the node. **Read the experimental section of
[SECURITY.md](SECURITY.md) first**, and see the
[tutorial](TUTORIAL.md#browser-review-over-ssh-experimental) for the steps
and troubleshooting.

On the node:

```bash
image-review serve --work-dir ./review_work --socket --via you@login-node
```

- If your laptop can reach compute nodes directly (`ssh you@node` works from
  it), add `--direct` instead of `--via`: the printed command then has no
  `-J`.
- If the node's own name does not work from your laptop, add
  `--ssh-host NAME` with the name that does.

`serve` prints an `ssh -N ... -L 127.0.0.1:8080:/path/to.sock you@node`
command and an `http://127.0.0.1:8080/#TOKEN` URL (**the token is a
password**). Run the ssh command on your laptop, then open the URL. Under
`sbatch` the URL is written to `~/.image-review/browser-<host>-<pid>.txt`
instead.

**Using the page.** The page starts with single images: enter a reviewer
name, then press `c` clean, `d` dirty, Left/Right to move and `z` to undo
(`?` lists every key). One bar at the bottom, colored by the image's status,
holds the buttons and messages.

- `m` (or `M`, no rotation) switches to grid mode for one batch at a time,
  where one verdict covers the whole grid. CLEAN is refused if any image in
  the grid is already DIRTY or FLAGGED (unless every image in it is DIRTY,
  which reverses that grid's own verdict).
- `b` moves to the next batch and `s` returns to single mode.
- With one page per server, `z` undoes only that page's marks; the server's
  undo history is shared by every client.

**Restarts.** To keep the ssh forward across server restarts, give each job a
fixed socket path (or pass `--socket-path`). Each start makes a new token, so
paste the new URL after a restart, unless you also export
`IMAGE_REVIEW_TOKEN` inside the job: `serve --socket` then reuses it and the
URL stays the same (see the tutorial).

```bash
# Once per job (re-running the token line makes a new token):
export IMAGE_REVIEW_SOCKET_PATH=~/.image-review/ir-$SLURM_JOB_ID.sock
export IMAGE_REVIEW_TOKEN=$(openssl rand -hex 16)   # optional: keeps the URL

# Each start (after a restart, re-run only this line):
image-review serve --socket --direct                # or --via you@login-node
```

After a "Lost connection" the page offers a Reconnect button (or `r`), never
retrying by itself. With the fixed path and token, a server restart needs
nothing more, and `q` (done) followed by Ctrl-C, the next `serve` in the same
shell and Reconnect moves the same tab on to the next batch or pass.

## Multi-pass workflow

1. **Pass 1** (grid triage): mark grids CLEAN or DIRTY. Err toward DIRTY.
2. **Pass 2** (single review): only images marked DIRTY in pass 1 are shown,
   as FLAGGED (orange status bar). Inspect them individually. Grid mode skips
   FLAGGED and DIRTY images, so a grid keypress cannot clear them.
3. **Pass 3+**: repeat on the shrinking DIRTY pool until confident.

Sessions are resumable: quitting saves all progress. The batch and pass
number are auto-detected when not specified. Press `b` at the end of a batch
to move on to the next one, and into the next pass once this one is done (not
with `--batch`, which keeps you in that batch).

## Contributing

Development setup, the test, lint and type-check commands, and the project's
conventions are in [CONTRIBUTING.md](CONTRIBUTING.md).
