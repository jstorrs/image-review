# Installation

Requires Python >= 3.12.

```bash
pip install .                 # every command
```

That installs from a source checkout. From a wheel or package index, use
`pip install image-review`; from a clone, `uv sync` installs the same as
`pip install .`. `review` needs only a browser on the same machine.

## Choosing extras

| Install | Commands | Dependencies |
|---|---|---|
| `pip install .` | every command: `preprocess`, `review`, `serve`, `status`, `export`, `--help` | click, rectpack; pydicom, numpy, scikit-image, scipy, matplotlib, Pillow, imagecodecs, tifffile, tqdm; python-gdcm, pylibjpeg, pylibjpeg-openjpeg |
| `pip install '.[viewer]'` | the same; no command uses pygame-ce any more | pygame-ce |

`[all]` is the same as `[viewer]`, and `[preprocess]` and `[codecs]` are still
accepted but add nothing, since their dependencies are installed by default.

For [browser review](tutorials/browser-review.md) on a cluster, use a plain
install where you preprocess and serve; the laptop needs only ssh and a
browser.

Every install brings several hundred MB of native wheels. On Linux the
imagecodecs wheels need glibc 2.28 or later (manylinux_2_28), and python-gdcm
2.27 or later; on an older Linux, such as CentOS/RHEL 7, installation may fail.

## Cluster install without root

Use a virtual environment in your own
space; no administrator rights are needed. Get Python 3.12 or later, either
from your site's module system (`module load python/3.12` is only an example;
module names vary by site) or from your own interpreter or `uv`, then:

```bash
module load python/3.12                  # or skip if python3.12 is already on PATH
python3.12 -m venv ~/venvs/image-review  # or: uv venv --seed --python 3.12 ~/venvs/image-review
source ~/venvs/image-review/bin/activate
pip install 'image-review @ git+https://github.com/jstorrs/image-review@<branch-or-tag>'
# or, from a clone of the repository:
pip install -e .
```

If the repository is private, the https URL needs credentials (or use an
`ssh://git@github.com/jstorrs/image-review` URL with a key on the cluster).
Activate the same environment in your `sbatch` scripts.

## Compressed DICOMs

JPEG, JPEG Lossless, JPEG-LS, JPEG 2000, HTJ2K and RLE
DICOMs are decoded with `python-gdcm`, `pylibjpeg` and `pylibjpeg-openjpeg`
(they come with every install).

- Wheels exist for CPython 3.12 to 3.14 (python-gdcm 3.2.6 and
  pylibjpeg-openjpeg 2.6.0 have cp314 wheels) on Linux (x86_64 and aarch64), macOS
  (Intel and Apple silicon) and Windows (x86_64). On other platforms
  `python-gdcm` has no wheel and installing image-review may fail.
- If they are missing, `preprocess` still runs, but only what pydicom decodes
  by itself or through Pillow (e.g. RLE, JPEG 2000) renders. Other compressed
  DICOMs (e.g. JPEG Lossless, JPEG-LS) are listed in `skipped.tsv` as failed,
  `cannot decode <transfer syntax>: ...`.
- 12-bit JPEG Extended files cannot be decoded even with the codecs (the only
  decoder is GPL-licensed and is not used). They are listed in `skipped.tsv`
  as `cannot decode JPEG Extended (Process 2 and 4): ...`.
- JPEG-LS files with 6- or 7-bit samples cannot be decoded either, for the
  same reason. They are listed in `skipped.tsv` as `cannot decode <transfer
  syntax>: ...`.

## Minimum dependency versions

These are declared in `pyproject.toml` and checked by running the test suite
on CPython 3.12. Most are the oldest release with a CPython 3.12 wheel. numpy
2.1 is what imagecodecs 2026.5.10 and later require, and matplotlib,
scikit-image, scipy, tifffile and pylibjpeg-openjpeg are the oldest releases
that work with it. click 8.3.0
is needed only by the tests (`CliRunner` with separate stderr, and values
from environment variables validated); 8.2.2 would do but is yanked on PyPI,
so 8.3.0, the next release, is the minimum. pydicom 3.0.1 is the first release
that decodes float pixel data without BitsStored; and python-gdcm 3.0.25 is the first release that reports a
corrupt JPEG 2000 codestream instead of decoding it without an error. The
minimums are: click >= 8.3.0, imagecodecs >= 2024.6.1, matplotlib >= 3.8.4,
numpy >= 2.1, pydicom >= 3.0.1, Pillow >= 10.3 except 11.x (which misdecodes
an MPO frame whose mode differs from the one before), scikit-image >= 0.23.1,
scipy >= 1.13, tifffile >= 2024.1.30, tqdm >= 4.60, pygame-ce >= 2.3.1,
python-gdcm >= 3.0.25, pylibjpeg >= 2.0,
pylibjpeg-openjpeg >= 2.3, and rectpack pinned at 0.2.2 (unmaintained; grid
packing depends on its exact behavior).

imagecodecs decodes 16-bit colour PNGs and tifffile 16-bit RGB and CMYK
TIFFs (with imagecodecs for LZW, deflate and other compressions), which
Pillow truncates to their high byte. imagecodecs 2026.10.10 has abi3 wheels for Linux
(x86_64 and aarch64, manylinux_2_28), macOS (arm64 and x86_64) and Windows.

rectpack is published only as a source distribution: a default `pip install`
builds it, but an offline or `--only-binary :all:` install needs its sdist or
a wheel you built beforehand. See [CHANGELOG.md](reference/changelog.md) for
what changed between releases.
