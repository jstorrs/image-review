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
grid contains as many images as possible. Resizing the window in grid mode
recomputes the grids for the new size. Images are shuffled at review time to
counter attention fatigue.

The status bar is green for CLEAN, red for DIRTY, gray for UNREVIEWED and
orange for FLAGGED (marked DIRTY in an earlier pass, awaiting this pass's
verdict). The status word is also written at the left end of the bar. Grids
are built without DIRTY or FLAGGED images.

The right end of the bar shows the scale the image is displayed at, e.g. `27%`.
It is red below 100%: text in the image is smaller than in the original, so
look closely. In grid mode it is the smallest image's effective scale, counting
any shrinking done to fit the grid.

| Option | Default | Description |
|--------|---------|-------------|
| `--mode` | `single` | Display mode |
| `--pass` | auto-detected | Pass number, 1 or more |
| `--batch` | the first batch with images matching the filter | Restrict the session to this batch; `b` at the end of the list never leaves it. An empty or unknown name is rejected (exit 2) with a list of known batches |
| `--filter` | `unreviewed` | Which images to show; `unreviewed` = images still to do (UNREVIEWED and FLAGGED) |
| `--rotate` | `auto` | Rotate images 90 degrees in grids; `auto` = only when that saves a grid |
| `--reviewer` | your login name; `$IMAGE_REVIEW_REVIEWER` | Name recorded with each verdict; see [Recorded verdicts](#recorded-verdicts) |
| `--work-dir` | `./review_work` | Work directory containing preprocessed data |
| `--remote` | `$IMAGE_REVIEW_REMOTE` | Review a server started with `image-review serve` instead of a local `--work-dir`; the two are mutually exclusive |
| `--via` | `$IMAGE_REVIEW_VIA` | Reach that server through an SSH tunnel via a login node, e.g. `--via user@login.cluster`; requires `--remote` |

For `--remote` and `--via`, see
[Remote review on an HPC cluster](../tutorials/remote-review.md).

## Keys and gamepad

| Key | Action |
|-----|--------|
| `c` | Mark CLEAN |
| `d` | Mark DIRTY |
| `z` | Undo your last mark in this mode (also on the end-of-list screen) |
| `b` | Next batch (on the end-of-list screen) |
| Left / Right | Navigate |
| `n` | Jump to next todo item ("No todo images remaining" in the info bar if none) |
| `u` | Toggle todo-only navigation |
| Space | Toggle autoplay, advancing every 500 ms (any other key stops it and still does its usual job); continue from the help or end-of-list screen |
| `s` | Single mode |
| `m` | Grid mode (rotation as set by `--rotate`; default: only when it saves a grid) |
| `M` | Grid mode (no rotation) |
| `h` | Help screen |
| `w` | Select display |
| `f` | Toggle fullscreen |
| `q` / Escape | Quit |

A gamepad (Xbox-style controllers included) works too, through SDL's game
controller mappings, which give every supported pad the same Xbox-style
layout whatever its labels say. The help screen also shows the mappings.

| Button | Action |
|--------|--------|
| B (right face button) | Mark CLEAN |
| Y (top face button) | Mark DIRTY |
| D-pad left / right | Navigate |
| A (bottom face button) | Continue from the help or end-of-list screen, like Space |
| Start | Quit (on any screen) |

B, Y and the D-pad act only on the review screen, and every button stops
autoplay. A pad SDL has no mapping for is not supported (most common pads
have one); you can supply one
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
again undoes the mark before that, and so on.

- Only marks you made in this session since the last mode switch (`s`, `m`,
  `M`) can be undone; beyond them `z` says "Nothing to undo".
- The history lives in memory only and is gone when you quit. It does not
  carry over to a new batch opened with `b`.
- After a lost connection `z` does nothing; only `q` (or Start) works.
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
images; if that leaves nothing, it tells you how many images need single-mode
review. The `n` key jumps to the next todo item and `u` toggles todo-only
navigation in all filter modes.

## End of a batch

At the end of a batch, `b` moves on to the next batch without restarting.
The end screen says "End of list", or in todo-only navigation "No todo images
remaining" (or "No more todo images this way" when todo images are left in
the other direction; Left/Right wrap round to the others), with the batch's
todo count when it is not 0. The help screen shows "batch k/B", the batch's
position among all batches.

`b` re-reads the current pass (keeping `--pass` if given) and the statuses,
then opens the next batch, in sorted order, that still has todo images in the
current mode, at its first item. The search goes round once, so batches you
skipped earlier (or images skipped in this one) come back. What counts as
todo depends on `--filter`; see [Todo images](#todo-images).

- When the pass changes (the last pass ended with DIRTY images, which come
  back FLAGGED), the search starts again from the first batch, which in single
  mode means the first batch with FLAGGED images, and the info bar says "Now
  pass N".
- With `--batch`, `b` stays in that batch: it reloads it while it has todo
  images, then says "Batch NAME done for pass N".
- When nothing is left it says "All batches done for pass N", or, under the
  default `--filter unreviewed` when the pass has just ended, "Pass P
  complete - nothing to review in pass N". "(current pass is M)" is added
  when `--pass` differs from the current pass. In grid mode, when nothing is
  left, it instead counts the FLAGGED/DIRTY images grids leave out and asks
  you to press `s`, which opens the first batch holding one in single mode.
  `q` quits.

## Recorded verdicts

Every verdict is saved to `review.tsv` in the work directory, with who gave
it and how: the reviewer, the display mode, how many images the one keypress
covered and the image-review version. The columns are described in
[`review.tsv`](../reference/work-directory.md#reviewtsv).

`--reviewer NAME` (or `$IMAGE_REVIEW_REVIEWER`) sets the reviewer name, by
default your login name. It must be 1-64 printable characters, not all
spaces (no tabs or newlines), else the command exits 2. The name is an
unauthenticated claim made by the client, recorded as given; nothing
verifies it, also with `--remote`.

**Upgrade everyone sharing a work directory together.** See
[Upgrading an older file](../reference/work-directory.md#upgrading-an-older-file).
