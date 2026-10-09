# `image-review serve`

```
image-review serve [--work-dir DIR] [--bind HOST] [--port N]
image-review serve [--work-dir DIR] (--socket | --socket-path PATH) [--via USER@LOGIN | --direct] [--ssh-host NODE]   # experimental
```

Serves a work directory over HTTPS (self-signed certificate, bearer token) so
a remote client can review it without copying the images.

`serve` prints a connection string (`ir://...`) that grants access: **treat
it like a password.** When stdout is not a terminal (e.g. `sbatch`), the
string is written to `~/.image-review/connection-<host>-<port>.txt` (mode
0600) instead, and the file is removed when the server stops.

| Option | Default | Description |
|--------|---------|-------------|
| `--work-dir` | `./review_work` | Work directory containing preprocessed data |
| `--bind` | this machine's FQDN | Hostname or IPv4 address to bind and advertise (wildcard addresses are refused) |
| `--port` | 0 | Port to listen on (0 picks a free port) |
| `--socket` | off | Experimental: serve plain HTTP on a Unix socket for browser review over SSH, instead of HTTPS over TCP |
| `--socket-path` | `~/.image-review/serve-<host>-<pid>.sock`; `$IMAGE_REVIEW_SOCKET_PATH` | Experimental: the socket path; implies `--socket` on the command line, but the environment variable is used only with `--socket` |
| `--via` | `$IMAGE_REVIEW_VIA` | With `--socket`: the login node to put in the printed ssh command |
| `--direct` | off; `$IMAGE_REVIEW_DIRECT` | With `--socket`: your laptop can ssh to compute nodes without a jump host; the printed ssh command omits `-J` |
| `--ssh-host` | this machine's FQDN | With `--socket`: the node name to put in the printed ssh command, for when the node's own FQDN does not resolve from your laptop |

Each start generates a new token (and, over HTTPS, a new certificate). With
`--socket`, a token in `$IMAGE_REVIEW_TOKEN` (22-256 characters from
`A-Za-z0-9_-`) is used instead, so the URL survives restarts; it is ignored
without `--socket`.

Option rules:

- `--socket` and `--socket-path` cannot be combined with `--bind` or
  `--port`, and `--socket-path` must not be empty.
- `$IMAGE_REVIEW_SOCKET_PATH` is used only with `--socket`; without it,
  `serve` stays HTTPS over TCP and ignores the variable (an empty value
  counts as unset).
- `--via` on the command line requires `--socket` (an `$IMAGE_REVIEW_VIA` in
  the environment is ignored without it).
- `--direct` follows the same rule (with `$IMAGE_REVIEW_DIRECT`) and cannot
  be combined with `--via` on the command line.
- `--ssh-host` requires `--socket`, takes a host name only (no `user@`), and
  has no environment variable.
