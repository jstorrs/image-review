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

The browser-page tests run `web/helpers.js` and `web/app.js` under `node` and skip when it is not on
`PATH`. Set `IMAGE_REVIEW_REQUIRE_NODE=1` to make them fail instead; CI sets it
and installs node 24, so a missing node cannot pass silently. It is test-only
and the application never reads it.

## Coverage

```
coverage run -m unittest discover && coverage report
```

Run it with the same `SDL_*` variables and with `node` on `PATH`, as above.
The report includes branch coverage, and `fail_under` in `pyproject.toml` sets
the floor; CI enforces it in the Ubuntu / Python 3.13 job only.

The number is in-process only: the preprocess worker processes and the
subprocess CLI runs that the tests spawn are not measured.

## CI

`.github/workflows/ci.yml` runs on pushes to `main`, on pull requests and on
manual dispatch: lint, format check, type check and tests on Linux
(`ubuntu-latest`) and macOS (`macos-latest`) with Python 3.12, 3.13 and 3.14.
A push to a branch without a pull request does not run CI; open a pull request,
or start the workflow from the Actions tab (`workflow_dispatch`).

The `minimums` job runs the tests on Python 3.12 against the lowest versions that
`pyproject.toml` allows for the direct dependencies, installed with
`uv pip install --resolution lowest-direct` and wheels only (rectpack has only
an sdist, so it is built from source). It runs no lint, format or type checks.
If it fails, a floor in `pyproject.toml` is too low: raise it, and note the new
requirement under *Upgrading* in the changelog. It installs with uv, not pip, so
it does not use the pip cache.

`.github/workflows/docs.yml` has the same triggers. It runs `sphinx-build -W`
and deploys the site to GitHub Pages from `main` only; pull requests just prove
the site builds.

Both workflows cancel an in-progress run for the same ref when a newer one
starts (never on `main`), cache pip downloads keyed on `pyproject.toml`, and
set a job timeout: 30 minutes for tests (both test jobs), 15 for the docs build,
10 for deploy.

Every `uses:` is pinned to a full commit SHA with a `# vX.Y.Z` comment.
Dependabot (`.github/dependabot.yml`) opens a weekly pull request for newer
action releases; check the release notes and keep the comment in step with the
SHA. It watches `github-actions` only. Do not enable the `pip` ecosystem: the
dependency floors in `pyproject.toml` are verified minimums, and version-update
pull requests would raise them.

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

### Which page owns what

State a fact once, on its owner page, and link to it from everywhere else.
Tutorials tell the story and summarise; they never restate a table or a rule
list. The specification holds internals and links to the owner pages for the
rest; together they are enough to rebuild the tool.

| Fact | Owner |
|---|---|
| A command's options, defaults, environment variables, exit codes, messages and output | `docs/commands/<command>.md` |
| Global options, log levels and the log format | `docs/commands/index.md` |
| Viewer keys, gamepad, screens, passes and todo images | `docs/commands/review.md` |
| The browser page's keys, screens and messages | `docs/commands/browser.md` |
| Work-directory files: columns, encoding, parsing, `review.lock`, leftover files | `docs/reference/work-directory.md` |
| Skip kinds and example reasons | `docs/commands/preprocess.md` |
| Note kinds and example details | `docs/commands/preprocess.md` |
| Allowlist and report format, export refusals | `docs/commands/export.md` |
| Extras, minimum versions and codec limits | `docs/install.md` |
| The `dev` and `docs` extras, tooling | this file |
| Security claims and limits, what is never logged | `docs/reference/security-model.md` |
| Workflows | `docs/tutorials/` |
| Algorithms, invariants, the wire API, function contracts | `docs/reference/specification.md` |
| The quick-start block | `docs/quickstart.md`, mirrored word for word in the README |

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
- **Docs travel with the change.** Each user-facing fact has one owner page
  (see [Which page owns what](#which-page-owns-what)). A change updates the
  owner of every fact it changes, in the same commit. Update the
  specification when internals change (algorithms, invariants, the wire API,
  function contracts), and other pages only where their summary or link
  becomes wrong. Before committing, grep `docs/` and the README for the old
  wording.
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
