# Security policy

image-review handles images that may carry burned-in Protected Health
Information (PHI), so take care with what you send when you report a problem.

## Supported versions

Only the latest release receives security fixes. That is the version on the
`main` branch (`image-review --version`). Older versions are not patched, so
upgrade before reporting if you can.

## Reporting a vulnerability

Do not open a public issue, discussion or pull request for a vulnerability.
Use GitHub private vulnerability reporting instead: open the repository's
Security tab and choose "Report a vulnerability", or go to
<https://github.com/jstorrs/image-review/security/advisories/new>.

If that is unavailable, contact the owner through
[github.com/jstorrs](https://github.com/jstorrs) without any details and ask
for a private channel.

Include:

- the version (`image-review --version` or `pip show image-review`);
- your OS and Python version;
- the mode: local review, `review --remote`, or browser review with
  `serve --socket`;
- what you observed;
- how to reproduce it.

## Never include PHI or secrets

Reproduce the problem with synthetic images. Never attach or paste:

- real images or DICOMs;
- a work directory or any file from one, such as batch JPGs, `manifest.tsv`,
  `skipped.tsv`, `preprocess.json` or `review.tsv`;
- export allowlists or reports;
- job output or logs that name source paths;
- tokens, `ir://` connection strings, `http://127.0.0.1:8080/#TOKEN` URLs, or
  the connection and URL files under `~/.image-review/`.

If you already sent something like that, say so in your report so it can be
deleted.

## What to expect

image-review is a small project maintained on a best-effort basis. You will
get an acknowledgement, and a fix is released on `main` and noted in the
changelog. There are no guaranteed response times.

## Security model

[The security model](docs/reference/security-model.md) describes what
image-review protects, against whom, and where its limits are.
