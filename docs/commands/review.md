# `image-review review`

```
image-review review [--mode {single,grid}]            [--pass N]
                    [--batch BATCH_ID]                 [--work-dir DIR]
                    [--filter {unreviewed,clean,all}]  [--rotate {auto,always,never}]
                    [--reviewer NAME]
```

Opens a fullscreen interactive session. In **grid mode**, images are
bin-packed into composite grids for fast triage. In **single mode**, images
are shown one at a time for detailed inspection.

Grids are packed at review time, sized to your screen resolution, so each
grid contains as many images as possible. While they are packed the screen
says "Computing grids... i/N". Resizing the window in grid mode recomputes
the grids for the new size. Images are shuffled at review time to counter
attention fatigue; in grid mode the grids are then shown largest first.
If the window has no content area above the status bar, grid review pauses
without packing or accepting verdicts. Resize the window to resume, or press
`s` to switch to single mode.

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
| `--pass` | auto-detected | Pass number, 1 to 1000000; see [Passes](#passes) |
| `--batch` | the first batch with images matching the filter | Restrict the session to this batch; `b` at the end of the list never leaves it. An empty or unknown name is rejected (exit 2) with a list of up to five known batches |
| `--filter` | `unreviewed` | Which images to show; `unreviewed` = images still to do (UNREVIEWED and FLAGGED) |
| `--rotate` | `auto` | Rotate images 90 degrees in grids: `auto` = only when that saves a grid, `always` or `never` |
| `--reviewer` | your login name; `$IMAGE_REVIEW_REVIEWER` | Name recorded with each verdict; see [Recorded verdicts](#recorded-verdicts) |
| `--work-dir` | `./review_work` | Work directory containing preprocessed data |

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

`z` follows the browser page's rules; see [Undo](browser.md#undo). In
`review`, the marks it can undo are those made since the last mode switch
(`s`, `m`, `M`). In grid mode, confirming a different display with `w`
clears them too, as resizing the window does, and they do not carry over to
a new batch opened with `b`.

## Todo images

Under the default `--filter unreviewed`, todo images and the "No grid items
for pass N" message are as on the browser page; see
[Todo images](browser.md#todo-images). In `review`:

- With `--filter clean` or `--filter all`, every listed image is a re-check,
  so an image is todo until you have marked it in this session (whatever the
  verdict, re-confirming included), and again if it becomes UNREVIEWED or
  FLAGGED (e.g. DIRTY images come back FLAGGED in the next pass); undoing a
  mark makes it todo again. The count starts from the full list and is not
  remembered across restarts. Grid mode leaves out DIRTY and FLAGGED images
  under every filter, `all` included.
- With `--batch`, K counts that batch only.
- At the start the message is printed in the terminal, ending
  " (--mode single)" instead of " - press [s]". When there is simply nothing
  to review, the terminal says "No images to review for pass N." instead,
  with " (filter: F)" before the full stop under `--filter clean` or `all`.
- The `n` key jumps to the next todo item and `u` toggles todo-only
  navigation in all filter modes.

### Refused CLEAN on a grid

The rule and its messages are in
[Refused CLEAN on a grid](browser.md#refused-clean-on-a-grid).

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

How the pass is detected, and what each pass shows, is in
[Passes](browser.md#passes). `--pass N` reviews pass N instead of the
detected one; a verdict given with a lower `--pass` keeps the image's higher
recorded pass.

## Images that cannot be loaded

The rules are in
[Images that cannot be loaded](browser.md#images-that-cannot-be-loaded). In
`review` the placeholder is a dark screen reading:

```
Cannot load image: KEY
image could not be read or decoded
It can only be marked DIRTY
```

The error itself is only logged, as a warning naming the image. The
placeholder takes part in `n`, todo-only and autoplay like any other item.
Grid mode packs only images it can load; one it cannot is shown on its own,
after the grids.

## Another writer on the work directory

A local review refuses a verdict or an undo if `review.tsv` has changed
since the session last read or wrote it: another writer is using the work
directory despite its [`review.lock`](../reference/work-directory.md#reviewlock),
or the file was edited, replaced or deleted. Nothing is saved, the window
closes, and the command exits 1 with:

```
Not recorded: PATH changed since it was loaded; not appending (is another review or serve using this work directory?). Stop any other review or serve using this work directory, then run review again.
```

If the session was about to drop an unfinished last line of `review.tsv`
(left by an interrupted write), the message says `not truncating its
unfinished last line` instead of `not appending (...)`.

Every verdict before it is saved. Stop the other writer, then run the same
command again: it reads the file afresh, including the other writer's
verdicts.

## Recorded verdicts

What is recorded with each verdict is in
[Recorded verdicts](browser.md#recorded-verdicts). In `review` the reviewer
name comes from `--reviewer NAME` (or `$IMAGE_REVIEW_REVIEWER`), by default
your login name. An invalid name exits 2; so does a missing login name with
no `--reviewer`.
