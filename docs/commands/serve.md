# `image-review serve`

```
image-review serve [--work-dir DIR] [--socket-path PATH] [--via USER@LOGIN | --direct] [--ssh-host NODE]
image-review serve [--work-dir DIR] --https [--bind HOST] [--port N]   # deprecated; --bind or --port implies --https
```

Serves a work directory for browser review over SSH: plain HTTP on a Unix
socket, which a browser on your laptop reaches through `ssh -L`. There is
nothing to install on the laptop but ssh and a browser, and nothing to keep
in sync: the page is served by `serve` itself, so it always matches the
server's version.

HTTPS mode is deprecated. `--https`, `--bind` or `--port` serves HTTPS over
TCP (self-signed certificate, bearer token) for the Python client,
`review --remote`, which must run the same image-review version as the
server. It logs a warning and keeps working until a future release removes
it.

For the workflow, see the tutorials
[Browser review over SSH](../tutorials/browser-review.md) and
[Remote review with the Python client (deprecated)](../tutorials/remote-review.md).

`serve` prints a URL, or in HTTPS mode a connection string (`ir://...`),
that grants access: **treat it like a password.** When stdout is not a
terminal (e.g. `sbatch`), it is written to a private file instead; see
[What serve prints](#what-serve-prints).

| Option | Default | Description |
|--------|---------|-------------|
| `--work-dir` | `./review_work` | Work directory containing preprocessed data; must exist |
| `--socket-path` | `~/.image-review/serve-<host>-<pid>.sock` (an 8-character hash replaces a host name too long for the socket path); `$IMAGE_REVIEW_SOCKET_PATH` | Socket mode; not with `--https`: the socket path |
| `--via` | `$IMAGE_REVIEW_VIA` | Socket mode; not with `--https`: the login node to put in the printed ssh command |
| `--direct` | off; `$IMAGE_REVIEW_DIRECT` (`1`, `true`, `yes` or `on` sets it) | Socket mode; not with `--https`: your laptop can ssh to compute nodes without a jump host; the printed ssh command omits `-J` |
| `--ssh-host` | this machine's FQDN | Socket mode; not with `--https`: the node name to put in the printed ssh command, for when the node's own FQDN does not resolve from your laptop |
| `--https` | off | Deprecated: serve HTTPS over TCP for the `review --remote` client, instead of browser review |
| `--bind` | this machine's FQDN | HTTPS mode, deprecated; implies `--https`: hostname or IPv4 address to bind and advertise (wildcard addresses are refused) |
| `--port` | 0 | HTTPS mode, deprecated; implies `--https`: port to listen on (0 picks a free port) |

`<host>` in the default socket path is the short host name, without its
domain.

## Token

Each start generates a new token (and, over HTTPS, a new certificate). In
socket mode, a token in `$IMAGE_REVIEW_TOKEN` is used instead, so the URL
survives restarts. It must be 22-256 characters from `A-Za-z0-9_-`, with no
surrounding whitespace (it is refused, not stripped). An invalid one stops
`serve` with an error that names the variable and the rule but never the
value. An empty value counts as unset. In HTTPS mode the variable is ignored
and not checked.

Generate the token inside the job, in the `srun` shell or in the batch
script, never before `sbatch` or `salloc`. Never write the literal value on
a command line or into a batch script.
[Browser review over SSH](../tutorials/browser-review.md) shows how.

## Option rules

`serve` runs in HTTPS mode when `--https`, `--bind` or `--port` is on the
command line. Any `--port` counts, even `--port 0`. Otherwise it runs in
socket mode.

These are checked before the work directory is opened. Breaking one exits
with status 2, before anything is created or logged:

- `--socket-path`, `--via`, `--direct` and `--ssh-host` on the command line
  cannot be used in HTTPS mode ("--via cannot be used in HTTPS mode (--https,
  --bind or --port).").
- `--socket-path` must not be empty.
- `--direct`, from the command line or `$IMAGE_REVIEW_DIRECT`, cannot be
  combined with `--via` on the command line ("--direct and --via are mutually
  exclusive."). With `--direct`, an `$IMAGE_REVIEW_VIA` in the environment is
  ignored.
- `$IMAGE_REVIEW_DIRECT` turns `--direct` on with `1`, `true`, `yes`, `on`,
  `t` or `y`, and leaves it off with `0`, `false`, `no`, `off`, `f`, `n` or
  an empty value (any case). Any other value is refused in either mode.
- `--ssh-host` has no environment variable.

The environment variables apply only in socket mode. A `--socket-path` on
the command line overrides `$IMAGE_REVIEW_SOCKET_PATH`, and an empty
`$IMAGE_REVIEW_SOCKET_PATH` counts as unset, so the default path is used.

| Variable | Socket mode | HTTPS mode |
|---|---|---|
| `$IMAGE_REVIEW_SOCKET_PATH` | the socket path | ignored |
| `$IMAGE_REVIEW_VIA` | as `--via` | ignored |
| `$IMAGE_REVIEW_DIRECT` | as `--direct` | ignored once checked |
| `$IMAGE_REVIEW_TOKEN` | the token; see [Token](#token) | ignored, not checked |

A `--work-dir` that does not exist also exits with status 2.

These exit with status 1 instead, and leave nothing behind. The first four
are also checked before the work directory is opened:

- a system without Unix sockets, such as Windows, in socket mode ("this
  system has no Unix sockets; browser review needs one (serve --https is
  deprecated)");
- an invalid `--via` ("Invalid --via: …", or "Invalid $IMAGE_REVIEW_VIA: …"
  when it came from the environment): it must not be empty or start with `-`, and may contain only letters, digits and `. _ @ % : [ ] / -`;
- an invalid `--ssh-host` ("Invalid --ssh-host: …"): a host name only, with
  no `user@`; it must not be empty or start with `-`, and may contain only
  letters, digits and `. _ : [ ] -`;
- an invalid `$IMAGE_REVIEW_TOKEN` ("Invalid $IMAGE_REVIEW_TOKEN: …");
- a socket path that cannot be used: one with a `:` or a control character,
  one too long for a Unix socket (107 bytes, 103 on macOS; the message
  suggests a shorter path such as `/tmp/ir.sock`), something there that is
  not a socket, or a socket owned by another user or with a live server
  behind it. A socket left by a dead server is removed;
- a `~/.image-review` that cannot be created or is not a directory you own,
  a connection or URL file that cannot be written, or a bind that fails
  ("Cannot listen on …");
- a wildcard `--bind` (`0.0.0.0`, `::` or empty);
- a `--bind` that binds but cannot go into a connection string ("--bind 'X'
  cannot be advertised to clients (…); use a hostname or dotted IPv4
  address.");
- a work directory that another writer holds (see [`review.lock`](../reference/work-directory.md#reviewlock)).

Only IPv4 host names and addresses are supported for `--bind`.

In HTTPS mode, once the status-2 rules pass and before the work directory
is opened, `serve` logs one WARNING (see [Logging](index.md#logging)):

```text
serve over HTTPS (--https, --bind or --port) is deprecated and will be removed in a future release, along with review --remote and status --remote: that client must run the same image-review version as the server. Plain `image-review serve` serves browser review instead; its page comes from the server itself, so it always matches. See https://jstorrs.github.io/image-review/tutorials/browser-review.html
```

## What serve prints

The token appears only in the connection string or URL below; the server
never logs it (see the
[security model](../reference/security-model.md#the-server-image-review-serve)).
In the examples, `TOKEN` and `FINGERPRINT` stand for the real values.

### Socket mode

`serve` prints a notice, then the ssh command to run on your laptop. The
command holds no secret:

```
Serving review data over a Unix socket for browser review over SSH.

On your laptop, forward a local port to the socket (leave it running):

  ssh -N -o ExitOnForwardFailure=yes -o ControlPath=none -J me@login-node -L 127.0.0.1:8080:/home/me/.image-review/serve-node042-12345.sock me@node042.cluster.example

If port 8080 is busy on your laptop, change it in -L and in the URL.
```

The command's parts:

- `-J` is followed by `--via`. Without it, `-J` gets your user name at a
  placeholder, such as `'me@<login-node>'`: replace `<login-node>`. The
  user name is `<user>` too if it cannot be found. With `--direct` there is
  no `-J`.
- The `-L` argument forwards `127.0.0.1:8080` on your laptop to the absolute
  path of the socket. It names 127.0.0.1 so ssh does not also bind `::1`.
- The destination is your user name on the node (`<user>` if it cannot be
  found) at `--ssh-host`, or else the node's FQDN.
- `ExitOnForwardFailure=yes` makes ssh exit if the forward fails, and
  `ControlPath=none` stops a shared connection from keeping the forward alive
  after Ctrl-C.
- Each part is quoted for the shell only when it needs it.

When the token came from `$IMAGE_REVIEW_TOKEN`, the next line is:

```
Using the token from $IMAGE_REVIEW_TOKEN: the URL stays the same across restarts in this shell.
```

On a terminal, the URL follows:

```
Then open this URL. It contains an access token; treat it like a password.

http://127.0.0.1:8080/#TOKEN

Press Ctrl-C to stop.
```

When stdout is not a terminal, the URL is written to
`~/.image-review/browser-<short-host>-<pid>.txt` (mode 0600) instead, with
anything in `<short-host>` outside `A-Za-z0-9._-` replaced by `_`, and the
output says how to read it:

```
Stdout is not a terminal, so the URL (it contains an access token; treat it like a
password) was written to /home/me/.image-review/browser-node042-12345.txt (mode 0600) on this node.
Home directories are usually shared with the login node, so on your laptop read it with:
  ssh me@login-node cat /home/me/.image-review/browser-node042-12345.txt
then open it in your browser once the ssh command above is running.

Press Ctrl-C to stop.
```

The `ssh` host is the same one as `-J`, so without `--via` it also has
`<login-node>` to replace. With `--direct`, the output says "On
your laptop, read it from the node with:" and the host is the node itself,
as in the forward command.

### HTTPS mode

Deprecated; `serve` first logs the WARNING under [Option rules](#option-rules).
On a terminal, it then prints the connection string and ready-to-paste
client commands:

```
Serving review data. The connection string grants access; treat it like a password.

ir://node042.cluster.example:41733/?token=TOKEN&fp=sha256:FINGERPRINT

On your laptop, directly:
  image-review review --remote 'ir://node042.cluster.example:41733/?token=TOKEN&fp=sha256:FINGERPRINT'
or through an SSH tunnel via the login node:
  image-review review --remote 'ir://node042.cluster.example:41733/?token=TOKEN&fp=sha256:FINGERPRINT' --via <user>@<login-node>

Press Ctrl-C to stop.
```

When stdout is not a terminal (e.g. `sbatch`), the string is written to
`~/.image-review/connection-<host>-<port>.txt` (mode 0600), where `<host>`
is the advertised host, sanitized the same way.
A file of the same name left by an earlier server is replaced.
The output gives the path and commands that read it over ssh:

```
Serving review data. Stdout is not a terminal, so the connection string (an access token;
treat it like a password) was written to /home/me/.image-review/connection-node042.cluster.example-41733.txt (mode 0600) on this node.
Home directories are usually shared with the login node, so on your laptop use:

On your laptop, directly:
  image-review review --remote "$(ssh <user>@<login-node> cat /home/me/.image-review/connection-node042.cluster.example-41733.txt)"
or through an SSH tunnel via the login node:
  image-review review --remote "$(ssh <user>@<login-node> cat /home/me/.image-review/connection-node042.cluster.example-41733.txt)" --via <user>@<login-node>

Press Ctrl-C to stop.
```

## Stopping

Ctrl-C (SIGINT), SIGTERM and SIGHUP all stop the server. SIGTERM is what
Slurm sends on `scancel` and at the time limit; SIGHUP comes from a closed
terminal or a dropped ssh session. A signal that was already ignored when
`serve` started stays ignored, so under `nohup` a hangup does not stop it.

On the way out, `serve` removes the connection file or URL file, closes the
socket (removing the socket file in socket mode) and releases the work
directory's lock once any mark in progress has been saved.

A hard kill (SIGKILL, a node crash) skips all of this and leaves behind:

- the connection file or URL file, which holds the token. It stays in
  `~/.image-review` until you delete it or a later server writes a file of
  the same name;
- in socket mode, the socket file. A later server on the same path removes
  it;
- `review.lock` in the work directory. It is not cleared automatically when
  the server ran on another node or before a reboot, so the next `review` or
  `serve` exits 1 saying the work directory is in use. See
  [`review.lock`](../reference/work-directory.md#reviewlock) for when to
  delete it by hand.
