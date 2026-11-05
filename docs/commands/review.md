# `image-review review`

```
image-review review [--work-dir DIR] [--port PORT] [--no-browser]
```

Reviews a work directory in a browser on this machine. `review` serves the
browser page on `127.0.0.1` and opens it in your browser. The page, with its
keys and screens, is described in [The browser page](browser.md).

To review data on another machine, such as a cluster, from a browser on your
laptop, use [`serve`](serve.md) instead. Use `review` on a machine only you
log in to: on a shared host, a browser on the host itself is exposed to the
other users there, so review from your laptop with `serve`; see
[Local review on loopback](../reference/security-model.md#local-review-on-loopback).

`review` needs Linux or macOS; on Windows it exits 1 ("review needs Linux or
macOS").

`review` prints a URL that grants access: **treat it like a password.**

| Option | Default | Description |
|--------|---------|-------------|
| `--work-dir` | `./review_work` | Work directory containing preprocessed data; must exist |
| `--port` | `0` | The port on `127.0.0.1` to serve on: 1024-65535, or 0 for any free one |
| `--no-browser` | off | Do not open a browser; open the URL yourself |

`review` reads no image-review environment variables: each start generates a
new token, and `$IMAGE_REVIEW_TOKEN` (see [`serve`](serve.md#token)) is
ignored. It does read `$HOME` (for `~/.image-review`), and `$DISPLAY`,
`$WAYLAND_DISPLAY`, `$SSH_CONNECTION` and Python's `$BROWSER` to choose
whether and how to open a browser (see [Opening the browser](#opening-the-browser)). The
page asks for the reviewer name itself (see [The name box](browser.md#the-name-box)).

## Exit status

These exit with status 2 before the work directory is opened, leaving
nothing behind:

- a `--work-dir` that does not exist;
- a `--port` outside 0 and 1024-65535;
- an unknown option, such as the options of the earlier pygame `review`
  (`--mode`, `--pass`, `--batch`, `--filter`, `--rotate`, `--reviewer`).

These exit with status 1 and leave nothing behind:

- Windows ("review needs Linux or macOS");
- a work directory that another writer holds (see
  [`review.lock`](../reference/work-directory.md#reviewlock)), or one with no
  preprocessed data;
- a `--port` that cannot be bound, such as one already in use ("Cannot listen
  on 127.0.0.1:PORT: …");
- a `~/.image-review` that cannot be created or is not a directory you own, or
  a redirect file that cannot be written ("Cannot write browser redirect
  file: …").

## What review prints

`TOKEN` and `PORT` stand for the real values. On a terminal:

```
Serving review data on 127.0.0.1 for browser review on this machine.

Open this URL. It contains an access token; treat it like a password.

http://127.0.0.1:PORT/#TOKEN

Press Ctrl-C to stop.
Opened it in your browser. If the browser shows an access error (a snap browser, such as
Ubuntu's Firefox, cannot read ~/.image-review), open the URL above instead.
```

The URL names `127.0.0.1`, never `localhost`; see
[Local review on loopback](../reference/security-model.md#local-review-on-loopback).

When stdout is not a terminal, the URL is not printed, so the token stays out
of logs. The output names the redirect file instead (see
[Opening the browser](#opening-the-browser)):

```
Serving review data on 127.0.0.1 for browser review on this machine.

Stdout is not a terminal, so the URL (it contains an access token; treat it like a
password) is not printed. Open this file (mode 0600) in your browser:

  /home/me/.image-review/review-laptop-12345.html

Press Ctrl-C to stop.
Opened it in your browser. If the browser shows an access error (a snap browser, such as
Ubuntu's Firefox, cannot read ~/.image-review), open the URL from `cat /home/me/.image-review/review-laptop-12345.html` instead.
```

What is said about the browser depends on it. The first line comes before
"Press Ctrl-C to stop.", the others after it, once a browser has been chosen
(which can take a few seconds) and started:

| Line | When |
|---|---|
| `No display found, so no browser was opened; open it yourself.` | Inside an ssh session (`$SSH_CONNECTION` set), or neither macOS nor a `$DISPLAY` or `$WAYLAND_DISPLAY` |
| `No graphical browser found, so none was opened; open it yourself.` | Python's `webbrowser` has no default browser, it would run in the terminal (see below), or choosing one failed |
| `Opened it in your browser. If the browser shows an access error …` | The browser was started |
| `Could not open a browser; open it yourself.` | Starting it failed |
| (none) | `--no-browser` |

## Opening the browser

Once the server is listening, `review` writes the URL into
`~/.image-review/review-<short-host>-<pid>.html`, a 0600 file in a 0700
directory you own. `<short-host>` is the host name without its domain, with
anything outside `A-Za-z0-9._-` replaced by `_`. The file redirects to the URL
and links to it, so opening the file usually opens the page.

The browser is started on the file's `file://` URI, never on the URL itself:
a browser's command line is visible to other users (on Linux, in
`/proc/*/cmdline`), and the file's URI holds no token. A sandboxed browser
may not be allowed to read the file: a snap browser, such as Firefox on
Ubuntu, cannot read hidden directories in your home such as
`~/.image-review`, and shows an access error instead. Then open the URL
itself: the printed one on a terminal, or the one in the file (`cat` it).

`review` opens a browser only when a graphical session on this machine is
likely: never inside an ssh session (`$SSH_CONNECTION` set, so `ssh -X` to a
shared node opens no browser there); otherwise on macOS, or when `$DISPLAY`
or `$WAYLAND_DISPLAY` is set. It uses Python's default browser
(`webbrowser.get()`, which `$BROWSER` can set), but never one that would run
in the terminal: a `$BROWSER` entry that `webbrowser` would run in the
foreground (one it does not recognise, unless it is written as `cmd %s &`),
or a console browser (`lynx`, `w3m`, `links`, `elinks`, `www-browser`). A
display does not rule these out, so they are refused by name and kind. The
browser is chosen and started from a separate thread once the server is
listening, so neither a slow choice nor a browser that does not return can
hold up the server; once `review` starts stopping, the thread opens and
prints nothing more. When none is opened, open the URL or the file yourself,
or review from another machine with [`serve`](serve.md).

## Stopping

Ctrl-C (SIGINT), SIGTERM and SIGHUP all stop the server. A signal that was
already ignored when `review` started stays ignored, so under `nohup` a hangup
does not stop it.

On the way out, `review` removes the redirect file, closes the port and
releases the work directory's lock once any mark in progress has been saved.
Only the first signal counts: while `review` cleans up, a further Ctrl-C or
SIGTERM does nothing, so it cannot cut the cleanup short and leave
`review.lock` behind.

The token stops working when the server stops. A tab still open on the page
shows that the connection was lost; see
[Lost connection and Reconnect](browser.md#lost-connection-and-reconnect).
Starting `review` again gives a new token, and a new port unless `--port` is
fixed, so open the new URL rather than pressing Reconnect.

A hard kill (SIGKILL, a crash) skips the cleanup and leaves behind the
redirect file, which holds the token, until you delete it or a later `review`
writes a file of the same name, and `review.lock` in the work directory, which
a later `review` or `serve` reclaims only when it can prove the process is
gone; see
[`review.lock`](../reference/work-directory.md#reviewlock).

## The work-directory lock

`review` holds the work directory's
[`review.lock`](../reference/work-directory.md#reviewlock) while it runs, so
a second `review` or `serve` on the same work directory exits 1, naming the
holder. `status` and `export` only read; `export` refuses while the lock is
held, unless given `--allow-live`.

If `review.tsv` changes while `review` runs, other than through the server
itself, the server refuses the next verdict or undo, as
[`serve` does](serve.md#another-writer-on-the-work-directory).
