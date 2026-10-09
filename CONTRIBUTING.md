# Contributing

## Development install

Python 3.12 or later. From a clone, in a virtual environment:

```
pip install -e '.[dev]'
```

`dev` pulls in every extra (the tests exercise every command) plus the lint,
type-check and coverage tools.

## Tests, lint, format and type check

```
python -m unittest discover
ruff check src tests
ruff format --check src tests     # `ruff format src tests` to apply
mypy --python-executable "$(which python)"
```

`mypy` reads its settings from `pyproject.toml`; `--python-executable` points
it at the environment that has the project's dependencies installed.

CI sets `SDL_VIDEODRIVER=dummy` and `SDL_AUDIODRIVER=dummy` so pygame needs no
display or sound device. Set them too when running the tests on a headless
machine.

## CI

`.github/workflows/ci.yml` runs on every push and pull request: lint, format
check, type check and tests on Linux (`ubuntu-latest`) and macOS
(`macos-latest`) with Python 3.12 and 3.13.

`.github/workflows/docs.yml` runs `sphinx-build -W` on every push and
pull request, and deploys the site to GitHub Pages from `main` only.

## Documentation

The documentation site is built with Sphinx, the MyST Markdown parser and the
Furo theme, from `docs/conf.py` and `docs/`. Install its extra, preview it, and
check it builds:

```
pip install -e '.[docs]'
sphinx-build -b html docs site
sphinx-build -E -W --keep-going -b html docs site
```

Open `site/index.html` to read the preview. `-W` turns warnings, including
broken links and pages missing from a toctree, into errors. `-E` rereads every
page; without it a rebuild skips unchanged pages and repeats none of their
warnings.

When writing a page:

- Add a new page to a `toctree` in `docs/index.md`; the strict build fails
  on a page that no toctree lists.
- Indent anything nested in a list item to the item's content column (three
  spaces under `1.`, two under `-`). Less is a new paragraph, as on GitHub.
- Link between pages with relative `.md` paths. Link from `docs/` to root
  files (SECURITY.md, CONTRIBUTING.md) with absolute
  `https://github.com/jstorrs/image-review/blob/main/...` URLs, since
  relative links out of `docs/` fail the strict build.
- CHANGELOG.md is also rendered on the site (`docs/reference/changelog.md`
  includes it), so it may contain no relative links.
- Use no MyST directives or roles other than the toctrees in `docs/index.md`
  and the changelog include: `docs/` is read on GitHub too.
- Link to a heading with GitHub's slug (`remote-review.md#4-batch-mode-sbatch`);
  MyST resolves it, and the strict build fails on a slug that matches no
  heading.

## `git blame`

The one-off `ruff format` reformat is listed in `.git-blame-ignore-revs`. To
keep it out of `git blame`:

```
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

## Conventions

- **Commits.** An imperative subject line ("Add export refusals", not "Added"
  or "Adds"), then a body in prose that says why the change is needed and what
  it does.
- **Docs travel with the change.** A change to the CLI or a file format
  updates, in the same commit, its page under `docs/commands/` (the single
  source for options, keys, rules and formats),
  `docs/reference/work-directory.md` if it changes a work-directory file, any
  tutorial under `docs/tutorials/` or `docs/quickstart.md` (and the README
  quick start it mirrors) that shows it, and
  `docs/reference/specification.md`. A security behaviour change also updates
  `docs/reference/security-model.md`.
- **Wire changes bump `API_VERSION`.** Any change to the server's request or
  response shapes, or to the `Status` vocabulary, bumps `API_VERSION` in
  `src/image_review/connection.py`, so a client and server of different
  versions fail with a clear message. See the API version rule in
  [the specification](docs/reference/specification.md#endpoints).
- **Changelog.** Add user-visible changes to [CHANGELOG.md](CHANGELOG.md),
  and put anything that needs action from people sharing work directories
  under its *Upgrading* heading.

## Security issues

Do not report vulnerabilities in a public issue; see
[SECURITY.md](SECURITY.md).
