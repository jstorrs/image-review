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

`.github/workflows/docs.yml` runs `mkdocs build --strict` on every push and
pull request, and deploys the site to GitHub Pages from `main` only.

## Documentation

The documentation site is built with MkDocs Material from `mkdocs.yml` and
`docs/`. Install its extra, preview it, and check it builds:

```
pip install -e '.[docs]'
mkdocs serve
mkdocs build --strict
```

`--strict` turns warnings, including broken links and a list or code block
glued to the paragraph before it, into errors. Set `NO_MKDOCS_2_WARNING=1` to
silence Material's MkDocs 2.0 notice.

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
- **Docs travel with the change.** A change to the CLI or to a file format
  updates [README.md](README.md), [TUTORIAL.md](TUTORIAL.md) and
  [SPEC.md](SPEC.md) in the same commit. A change to the security behaviour
  also updates [SECURITY.md](SECURITY.md).
- **Wire changes bump `API_VERSION`.** Any change to the server's request or
  response shapes, or to the `Status` vocabulary, bumps `API_VERSION` in
  `src/image_review/connection.py`, so a client and server of different
  versions fail with a clear message. See the API version rule in SPEC.md.
- **Changelog.** Add user-visible changes to [CHANGELOG.md](CHANGELOG.md),
  and put anything that needs action from people sharing work directories
  under its *Upgrading* heading.

## Security issues

Do not report vulnerabilities in a public issue; see
[SECURITY.md](SECURITY.md#reporting-a-vulnerability).
