# `image-review review`

```
image-review review [--mode {single,grid}]            [--pass N]
                    [--batch BATCH_ID]                 [--work-dir DIR]
                    [--filter {unreviewed,clean,all}]  [--rotate {auto,always,never}]
                    [--reviewer NAME]
                    [--remote CONNECTION_STRING [--via DESTINATION]]
```

Opens a fullscreen interactive session. In **grid mode**, images are
bin-packed into composite grids for fast triage. In **single mode**, images
are shown one at a time for detailed inspection.

Grids are packed at review time, sized to your screen resolution, so each
grid contains as many images as possible. While they are packed the screen
says "Computing grids... i/N". Resizing the window in grid mode recomputes
the grids for the new size. Images are shuffled at review time to counter
attention fatigue; in grid mode the grids are then shown largest first.

The status bar is green for CLEAN, red for DIRTY, gray for UNREVIEWED and
orange for FLAGGED (marked DIRTY in an earlier pass, awaiting this pass's
verdict). The status word is also written at the left end of the bar. Grids
are built without DIRTY or FLAGGED images.

Next to the status word the bar shows `(h)elp`, `all` or `todo-only` (see
[Todo images](#todo-images)) and the gamepads: "no gamepad", "gamepad
connected" or "N gamepads". The centre shows your position and the todo count,
e.g. `12 / 300 (41 todo)`, and messages such as refusals. The
right end shows the image name (or "grid (N images)") and the scale the
image is displayed at, e.g. `27%`. The scale is red below 100%: text in the
image is smaller than in the original, so look closely. In grid mode it is
the smallest image's effective scale, counting any shrinking done to fit the
grid.

| Option | Default | Description |
|--------|---------|-------------|
| `--mode` | `single` | Display mode |
| `--pass` | auto-detected | Pass number, 1 or more; see [Passes](#passes) |
| `--batch` | the first batch with images matching the filter | Restrict the session to this batch; `b` at the end of the list never leaves it. An empty or unknown name is rejected (exit 2) with a list of up to five known batches |
| `--filter` | `unreviewed` | Which images to show; `unreviewed` = images still to do (UNREVIEWED and FLAGGED) |
| `--rotate` | `auto` | Rotate images 90 degrees in grids: `auto` = only when that saves a grid, `always` or `never` |
| `--reviewer` | your login name; `$IMAGE_REVIEW_REVIEWER` | Name recorded with each verdict; see [Recorded verdicts](#recorded-verdicts) |
| `--work-dir` | `./review_work` | Work directory containing preprocessed data |
| `--remote` | `$IMAGE_REVIEW_REMOTE` | Review a server started with `image-review serve --https` instead of a local `--work-dir`; see [Connecting to a server](#connecting-to-a-server) |
| `--via` | `$IMAGE_REVIEW_VIA` | Reach that server through an SSH tunnel via a login node, e.g. `--via user@login.cluster`; requires `--remote` |

## Keys and gamepad

| Key | Action |
|-----|--------|
| `c` | Mark CLEAN |
| `d` | Mark DIRTY |
| `z` | Undo your last mark in this mode (also on the end-of-list screen) |
| `b` | Next batch (on the end-of-list screen) |
| Left / Right | Navigate |
| `n` | Jump to next todo item ("No todo images remaining" in the info bar if none, staying on the current item) |
| `u` | Toggle todo-only navigation |
| Space | Toggle autoplay, advancing every 500 ms (any other key stops it and still does its usual job); continue from the help, display-select or end-of-list screen |
| `s` | Single mode |
| `m` | Grid mode (rotation as set by `--rotate`; default: only when it saves a grid) |
| `M` | Grid mode (no rotation) |
| `h` | Help screen |
| `w` | Select display |
| `f` | Toggle fullscreen |
| `q` / Escape | Quit |

`w` opens the display-select screen. There `1`-`9` switch to that display,
and Space, `h` or the gamepad's A confirm; in grid mode the grids are
rebuilt if the display changed. `f`, `s`, `m`, `M` and `q`/Escape work there
too. The digits do nothing on the ordinary help screen, which Space, `h` or
A leave and where `f` also works.

A gamepad (Xbox-style controllers included) works too, through SDL's game
controller mappings, which give every supported pad the same Xbox-style
layout whatever its labels say. The help screen also shows the mappings, and
the status bar counts the connected pads. Pads can be plugged in or removed
during a session.

| Button | Action |
|--------|--------|
| B (right face button) | Mark CLEAN |
| Y (top face button) | Mark DIRTY |
| D-pad left / right | Navigate |
| A (bottom face button) | Continue from the help, display-select or end-of-list screen, like Space |
| Start | Quit (on any screen) |

B, Y and the D-pad act only on the review screen, and every button stops
autoplay. There is no button for `b` or `z`: on the end-of-list screen a pad
can only continue (A) or quit (Start). A pad SDL has no mapping for is not
supported (most common pads have one); you can supply one
in SDL's mapping format through the `SDL_GAMECONTROLLERCONFIG` environment
variable (one mapping per line; tools such as SDL's `controllermap` produce
one).

`c` and `d` (and B and Y) are ignored for 200 ms after an image or grid
appears, so a verdict only applies to an item you have seen: press it again
once you have looked. After a mark, the viewer auto-advances to the next item
after a short delay (200 ms). Every key and button pressed while grids are
being computed is dropped, including `q`/Escape and the arrows.

## Undo

`z` undoes your most recent mark, a single image or a whole grid, and returns
to that item so you can mark it again (after the same 200 ms). Pressing it
again undoes the mark before that, and so on. Keys do not repeat, so holding
`z` down undoes one mark.

- Only marks you made in this session since the last mode switch (`s`, `m`,
  `M`) can be undone; beyond them `z` says "Nothing to undo".
- In grid mode, resizing the window, or confirming a different display with
  `w`, clears the history too: the grids are rebuilt, and a grid marked DIRTY
  is no longer among them.
- The history lives in memory only and is gone when you quit. It does not
  carry over to a new batch opened with `b`.
- After a lost connection `z` does nothing; see [Lost connection](#lost-connection).
- An undo appends rows to `review.tsv` that restore each image's previous
  verdict and pass, or mark it UNREVIEWED again; see
  [Undo rows](../reference/work-directory.md#undo-rows).

## Todo images

With the default `--filter unreviewed`, "todo" means UNREVIEWED or FLAGGED.
With `--filter clean` or `--filter all`, every listed image is a re-check, so
an image is todo until you have marked it in this session (whatever the
verdict, re-confirming included), and again if it becomes UNREVIEWED or
FLAGGED (e.g. DIRTY images come back FLAGGED in the next pass); undoing a mark
makes it todo again. The count starts from the full list and is not
remembered across restarts.

In grid mode every filter, `all` included, leaves out DIRTY and FLAGGED
images, so a batch whose only todo images are FLAGGED is not picked for grid
mode. If that leaves nothing to show, it says how many images need
single-mode review instead of implying the review is done. In the terminal,
at the start:

```
No grid items for pass N; K FLAGGED/DIRTY image(s) need(s) single-mode review (--mode single)
```

It says "images need", or "image needs" when K is 1. On screen the message
ends " - press [s]" instead of " (--mode single)", and `s` switches to single
mode to review them. When there is simply nothing to review, the terminal
says "No images to review for pass N." instead, with " (filter: F)" before
the full stop under `--filter clean` or `all`.

The `n` key jumps to the next todo item and `u` toggles todo-only navigation
in all filter modes.

### Refused CLEAN on a grid

A grid can still come to hold a DIRTY or FLAGGED image mid-session, for
example when one of its images shares a source file with an image marked
DIRTY elsewhere. CLEAN on such a grid is refused, unless every image in it is
DIRTY (that reverses the grid's own verdict), and the status bar says "grid contains an
image already marked DIRTY - review it in single mode"; DIRTY is still
accepted. Over a server, the server applies the same rule, so a CLEAN is
refused even when another client marked an image DIRTY since your viewer last
looked. An image that grid mode left out of the grids then says "image
already marked DIRTY in another pass - review it in single mode". A refused
CLEAN records nothing, does not advance and leaves nothing to undo.

## End of a batch

At the end of a batch, `b` moves on to the next batch without restarting.
The end screen says "End of list - K todo left - [b] next batch", with the
batch's todo count K ("K todo left" is left out when K is 0). In todo-only
navigation it says "No todo images remaining - [b] next batch", or "No more
todo images this way - K todo left - [Left/Right] wrap - [b] next batch" when
todo images are left in the other direction. On that screen Right or Space
and Left wrap round to the first or last item (the first or last todo item in
todo-only navigation), `s`, `m` and `M` switch mode, `z` undoes, `b` moves on
and `q`/Escape quits; `n` does nothing there. A mode switch that finds
nothing to show says "No items for single mode" (or grid mode). The help
screen shows "batch k/B", the batch's position among all batches.

`b` re-reads the current pass (keeping `--pass` if given) and the statuses,
then opens the next batch, in sorted order, that still has todo images in the
current mode, at its first item. The search goes round once, so batches you
skipped earlier (or images skipped in this one) come back. What counts as
todo depends on `--filter`; see [Todo images](#todo-images). With nothing
left to show, only `q`/Escape, `s`, `m`, `M`, `z` and `b` act, and `z` says
"Nothing to undo".

- When the pass changes (the last pass ended with DIRTY images, which come
  back FLAGGED), the search starts again from the first batch, which in single
  mode means the first batch with FLAGGED images, and the info bar says "Now
  pass N".
- With `--batch`, `b` stays in that batch: it reloads it while it has todo
  images, then says "Batch NAME done for pass N".
- When nothing is left it says "All batches done for pass N", or, under the
  default `--filter unreviewed` when the pass has just ended, "Pass P
  complete - nothing to review in pass N" ("... in NAME for pass N" with
  `--batch`). "(current pass is M)" is added when `--pass` differs from the
  current pass. In grid mode, when the only todo images left are ones
  grids leave out, it shows the "No grid items for pass N" message
  from [Todo images](#todo-images) instead, and `s` opens the first batch
  holding one in single mode.

## Passes

Without `--pass`, the pass is detected from `review.tsv`:

- pass 1 while any image has never been reviewed;
- otherwise the highest recorded pass while it still has todo images
  (UNREVIEWED or FLAGGED);
- otherwise the next pass.

An image's status in a pass comes from its latest verdict:

| Latest verdict | Status in this pass |
|----------------|---------------------|
| none | UNREVIEWED |
| from this pass or a later one | that verdict, CLEAN or DIRTY |
| CLEAN, from an earlier pass | CLEAN |
| DIRTY, from an earlier pass | FLAGGED |

So under the default `--filter unreviewed` each pass shows:

| Pass | Shows |
|------|-------|
| 1 | All UNREVIEWED images |
| N > 1 | FLAGGED images (marked DIRTY in an earlier pass) in single mode, plus any still-UNREVIEWED images |

An image's recorded pass never goes down: a verdict given with a lower
`--pass` updates the verdict but keeps the higher pass. So a lower `--pass`,
or a manifest that gains an image (for example by a hand edit, which sends
the detected pass back to 1), cannot hide or overwrite later-pass verdicts.
In such a view an image marked DIRTY in a later pass reads DIRTY, not
FLAGGED.

The workflow built on passes is in
[Multi-pass workflow](../tutorials/local-review.md#multi-pass-workflow).

## Images that cannot be loaded

An image whose JPEG is missing, does not match its recorded hash, or cannot
be read or decoded is shown as a dark placeholder instead:

```
Cannot load image: KEY
image could not be fetched
It can only be marked DIRTY
```

Over a server, a JPEG that is missing, altered or unreadable on the server
is answered with HTTP 404, and the second line says "image could not be
fetched". "image could not be read or decoded" is for bytes that arrived but
do not decode, and for every failure in a local review, a missing or altered
file included. The error itself is only logged, as a warning naming the
image.

The placeholder takes part in navigation, `n`, todo-only and autoplay like
any other item. It can be marked DIRTY but never CLEAN: CLEAN is refused and
the status bar says "cannot mark CLEAN: image could not be loaded". DIRTY is
recorded normally, so the image stops being todo and the pass can finish. In
later passes it comes back FLAGGED and, if it still cannot be loaded, again as
a placeholder that only takes DIRTY. The image is tried again each time it is
shown, so one that loads again is shown normally.

Grid mode packs only images it can load; one it cannot is shown on its own,
after the grids.

## Connecting to a server

`--remote` and `--work-dir` are mutually exclusive (exit 2). When
`--remote` came from `$IMAGE_REVIEW_REMOTE`, the message says
"IMAGE_REVIEW_REMOTE is set; unset it to use --work-dir." `--via` on the
command line without `--remote` is also an error (exit 2), but a
`$IMAGE_REVIEW_VIA` left in the environment is ignored by a local review.

With `--via`, the ssh tunnel comes up first, and its password or MFA prompts
appear in the terminal before the viewer window opens. The review then
starts only after these checks pass; each failure exits 1 with its message:

- A connection string or `--via` value that cannot be parsed:
  "Invalid --remote connection string: …" or "Invalid --via: …".
- The server's certificate does not match the connection string: "The
  certificate presented by HOST:PORT does NOT match the connection string.
  The connection was aborted before any credentials were sent. Do not
  continue unless you know why the server's identity changed." With `--via`
  it adds that another local process may have grabbed the forwarded port,
  and to retry.
- "Server at HOST:PORT rejected the access token (connection string from a
  different or restarted server?)".
- "Server at HOST:PORT returned HTTP N" for any other error reply.
- "Cannot reach server at HOST:PORT: …". With `--via`, when the cause was a
  network error, it adds "(the login node may not be able to reach the
  server; see the ssh output above)".
- Different image-review versions: "server speaks API vS, this client vC"
  or "server is too old to report its API version", each ending "install the
  same image-review version on both machines".
- An ssh tunnel that fails to come up gives the tunnel's own message.

`HOST:PORT` is followed by "(via DESTINATION)" when `--via` is used. For
setting up the server, the tunnel and the environment variables, see
[Remote review on an HPC cluster](../tutorials/remote-review.md).

## Lost connection

With `--remote`, if the server cannot be reached or answers with an HTTP
error such as a 500 or a 401, the viewer shows:

```
Lost connection to server - progress saved. Press q to quit.
```

This can happen while loading an image, marking, undoing, switching mode,
rebuilding the grids after a resize, moving to the next batch, or re-reading
the statuses after a refused CLEAN. The viewer also logs the reason.
Two errors are not a lost connection: a missing image is shown as a
[placeholder](#images-that-cannot-be-loaded), and a
[refused CLEAN](#refused-clean-on-a-grid) shows its own message.
Every mark up to the last one is saved on the server; a mark that failed is
not. Autoplay stops, and only `q`, Escape, the gamepad's Start and closing
the window do anything: no navigation, mode switch or `z`. Quit, then run the
same command again while the server is still running.

## Recorded verdicts

Every verdict is saved to `review.tsv` in the work directory, with who gave
it and how: the reviewer, the display mode, how many images the one keypress
covered and the image-review version. The columns are described in
[`review.tsv`](../reference/work-directory.md#reviewtsv).

`--reviewer NAME` (or `$IMAGE_REVIEW_REVIEWER`) sets the reviewer name, by
default your login name. It must be 1-64 printable characters, not all
spaces (no tabs or newlines), else the command exits 2; so does a missing
login name with no `--reviewer`. The name is an
unauthenticated claim made by the client, recorded as given; nothing
verifies it, also with `--remote`.

**Upgrade everyone sharing a work directory together.** See
[Upgrading an older file](../reference/work-directory.md#upgrading-an-older-file).
