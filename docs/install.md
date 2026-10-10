# Installation

Requires Python >= 3.12.

```bash
pip install '.[all]'          # everything: preprocess, codecs and the viewer
```

That installs from a source checkout. From a wheel or a package index the same
extras apply, e.g. `pip install 'image-review[all]'`.

## Choosing extras

The dependencies are split into extras, so each machine installs only what its
commands need:

| Install | Commands | Adds |
|---|---|---|
| `pip install .` (core) | `serve`, `status`, `export`, `--help` | click, cryptography, rectpack |
| `pip install '.[viewer]'` | `review` (local or `--remote`) | pygame-ce, Pillow |
| `pip install '.[preprocess,codecs]'` | `preprocess` | pydicom, numpy, scikit-image, scipy, matplotlib, Pillow, tqdm; python-gdcm, pylibjpeg, pylibjpeg-openjpeg |

On a cluster with a laptop viewer (see
[Remote review on an HPC cluster](tutorials/remote-review.md)), install
`[preprocess,codecs]` where you preprocess, core alone where you only `serve`,
and `[viewer]` on the laptop (`pip install '.[viewer]'`). A command whose
extra is missing exits 1 with
`this command needs the <extra> extra: pip install 'image-review[<extra>]'`.

`cryptography` is a core dependency (`serve` uses it for its TLS
certificate). `review --via` and `status --via` need an OpenSSH client on the
machine you run them on (built into macOS, Linux and Windows 10+).

## Cluster install without root

Use a virtual environment in your own
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

## Compressed DICOMs

JPEG, JPEG Lossless, JPEG-LS, JPEG 2000, HTJ2K and RLE
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

## Minimum dependency versions

These are declared in `pyproject.toml` and checked by running the test suite
on CPython 3.12: click >= 8.2, matplotlib >= 3.7.3,
numpy >= 1.26, pydicom >= 3.0, Pillow >= 10.3 except 11.x (which misdecodes
an MPO frame whose mode differs from the one before), scikit-image >= 0.22,
scipy >= 1.11.2, tqdm >= 4.60, pygame-ce >= 2.3.1, cryptography >= 41, python-gdcm >= 3.0.25,
pylibjpeg >= 2.0, pylibjpeg-openjpeg >= 2.0, and rectpack pinned at 0.2.2
(unmaintained; grid packing depends on its exact behavior).

rectpack is published only as a source distribution: a default `pip install`
builds it, but an offline or `--only-binary :all:` install needs its sdist or
a wheel you built beforehand. See [CHANGELOG.md](reference/changelog.md) for
what changed between releases.
