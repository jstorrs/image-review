# The browser page

The page that [`image-review serve`](serve.md) serves for browser review: what
you see at `http://127.0.0.1:8080/#TOKEN` once the ssh forward is running.
For the workflow (starting `serve`, the ssh command, moving on to the next
batch or pass), see [Browser review over SSH](../tutorials/browser-review.md).

The page reviews the current pass (see [Passes](#passes)). It starts in
single mode, showing the pass's [todo images](#todo-images) (UNREVIEWED and
FLAGGED) one at a time, in random order. In grid mode it shows packed grids
of one batch's UNREVIEWED images; see [Grid mode](#grid-mode).

## The name box

With no name set in the tab, the page first opens a box, "Who is
reviewing?". Type your name and press Enter (or click OK). The name is
recorded with every verdict (see [Recorded verdicts](#recorded-verdicts)).
It must be 1-64 characters and not all spaces; while it is not, the field is
marked and OK is disabled. Until a name is set nothing can be reviewed, and the bar says
"Enter your name (1-64 characters) to start reviewing". The server has the
final say: a name it refuses gives "invalid reviewer name" on the next mark
or undo.

The tab remembers the name (closing the tab forgets it). The bar shows it as
"Jane ✎"; click that to change it. Escape closes the box and keeps the old
name, but only when there is one. The name cannot be changed once the page
has stopped (see [Lost connection and Reconnect](#lost-connection-and-reconnect)
and [Done](#done)).

## Keys

Press `?` (or `h`, or click the "?" button) for the list of keys.

| Key | Button | Action |
|-----|--------|--------|
| `c` | Clean | Mark the image (or every image in the grid) CLEAN and move on |
| `d` | Dirty | Mark it DIRTY and move on |
| Right / Left | → / ← | Next / previous item, without marking |
| `z` | Undo | Undo the latest mark and show that image (or grid) again; see [Undo](#undo) |
| `m` | -- | Grid mode for the current batch (rotation `auto`) |
| `M` | -- | Grid mode without rotating images (rotation `never`) |
| `s` | -- | Back to single mode (rereads the list) |
| `b` | -- | Grid mode only: the next batch with images to review |
| `r` | Reconnect | Only while Reconnect is shown: after a lost connection or a server change, after `q` or at the end of a pass; see [Lost connection and Reconnect](#lost-connection-and-reconnect) |
| `q` | Done | Done with this server: the page waits for the next one; see [Done](#done) |
| `?` or `h` | ? | Show or hide the help (Escape also closes it) |

`m`, `M`, `s` and `b` reread the current pass's statuses from the server
before they rebuild the list. Letters work with Caps Lock on; `M` is Shift
and `m`. A held key acts once, and keys pressed with Ctrl, Alt or Cmd are
left to the browser. There is no gamepad support, and none of the
[`review`](review.md#keys-and-gamepad) viewer's `n`, `u`, Space, `w` or `f`.

Where the keys differ from the viewer's:

- Escape only closes the help or the name box; it does not quit.
- `b` acts anywhere in grid mode, not only at the end of the list, and
  rereads only the statuses of the same pass, not which pass is current.
- While the grids are being computed, `q` (Done) and `s`, `m`, `M` and `b`
  still act; the viewer drops every key then.

While the help or the name box is open nothing can be marked or moved, and
the review buttons, Done and Reconnect included, are disabled. Once it
closes the page waits the [200 ms](#marking) again before a verdict counts,
so nothing is judged on an image that was covered.

## The bar

Everything is in one bar at the bottom of the page:

- **Left:** the ← and → buttons, Clean, Dirty and Undo, the item's status
  word and the [scale](#the-scale).
- **Right:** your name, "?" and Done (or Reconnect in its place).
- **Middle:** a message (what was undone, refusals, errors) when there is
  one; otherwise the mode ("Single", "Grid · auto" or "Grid · never"), the
  progress (pass, in grid mode the batch, and how many are left) and the
  item: "batch · key" for an image, "grid (N images)" for a grid.

Your own moves (arrows, marking, switching mode) clear the message; an undo
or Reconnect says what it did. A refusal or error stays until your next
move.

In a window 1408 pixels wide or less the middle part gets a row of its own
under the buttons. At 960 pixels or less the buttons drop their key hints,
and at 640 or less Clean, Dirty and Undo shorten to "C", "D" and "↶". These
widths are CSS pixels, so browser zoom and the system's display scaling
change where they fall. Text too long for its place is cut short; hover over
it to read it whole.

The bar's colour is the current item's status, also written in it: grey
UNREVIEWED, green CLEAN, red DIRTY, amber FLAGGED. It is grey too on the
stop sign and end screens and once the page has stopped. The colours follow
the browser's light or dark setting; the image area stays dark in both.

### The scale

The bar shows the display scale as a percent; in grid mode it is the
smallest image's. Below 100% it stands out as a badge such as "⚠ 46%": the
image is shrunk to fit and small burned-in text can be lost, so enlarge the
window or go full screen. Browser zoom does not help: it makes the page's
text larger and the image's share smaller.

## Marking

A verdict counts only once the image (in grid mode, the whole grid) has been
on screen for 200 ms, so a key pressed as an image appears is ignored: press
it again once you have looked. After `c` or `d` the image stays up for
200 ms with the bar in its new colour, then the next todo item appears and
no message is left, so the progress stays in view while you mark.

## Passes

The page reviews the current pass, read from the server when the page loads
and on every [Reconnect](#lost-connection-and-reconnect); there is no way to
pick another. `m`, `M`, `s` and `b` reread the statuses of the same pass,
not which pass is current. The current pass is worked out from `review.tsv`:

- pass 1 while any image has never been reviewed;
- otherwise the highest recorded pass while it still has todo images
  (UNREVIEWED or FLAGGED);
- otherwise the next pass, but never above 1000000.

An image's status in a pass comes from its latest verdict:

| Latest verdict | Status in this pass |
|----------------|---------------------|
| none | UNREVIEWED |
| given on other content (the file changed since) | UNREVIEWED |
| from this pass or a later one | that verdict, CLEAN or DIRTY |
| CLEAN, from an earlier pass | CLEAN |
| DIRTY, from an earlier pass | FLAGGED |

A verdict counts only for the content it was given on: if an image's file was
replaced and preprocessed again since its verdict, the image is offered for
review again, and until then it counts as never reviewed (so the pass is 1).
Verdicts recorded before `review.tsv` held a hash are taken as given (see
[`review.tsv`](../reference/work-directory.md#reviewtsv)).

So each pass shows:

| Pass | Shows |
|------|-------|
| 1 | All UNREVIEWED images |
| N > 1 | FLAGGED images (marked DIRTY in an earlier pass) in single mode, plus any still-UNREVIEWED images |

An image's recorded pass never goes down: a verdict given in a lower pass
updates the verdict but keeps the higher pass. So a review in a lower pass,
for example after a manifest gains an image (by a hand edit, which sends the
current pass back to 1), cannot hide or overwrite later-pass verdicts. In
such a view an image marked DIRTY in a later pass reads DIRTY, not FLAGGED.

The workflow built on passes is in
[Multi-pass workflow](../tutorials/browser-review.md#multi-pass-workflow).

## Todo images

"Todo" means UNREVIEWED or FLAGGED. Single mode lists every todo image of
the pass; grid mode, the UNREVIEWED images of one batch. Marked images stay
in the list: Left and Right still reach them, a mark moves on to the next
todo item after the current one, going round, and the bar counts the todo
items left, e.g. "Pass 1 · 41 / 300 left".

Grid mode leaves out DIRTY and FLAGGED images: one verdict covers a whole
grid, so a keypress must never clear an image already judged DIRTY. So a
batch whose only todo images are FLAGGED is not picked for grid mode. An
image whose DIRTY verdict was given on content its file no longer has
(replaced and preprocessed again) is UNREVIEWED, not DIRTY, so it can be
shown, and cleared, in a grid. When no batch has an image for grid mode but
FLAGGED images are left, grid mode says how many images need single-mode
review instead of implying the review is done:

```
No grid items for pass N; K FLAGGED/DIRTY images need single-mode review - press [s]
```

K counts the FLAGGED images of the whole pass. It says "image needs" when K
is 1. `s` switches to single mode to review them.

## Images that cannot be loaded

An image whose JPEG is missing, does not match its recorded hash, or cannot
be read or decoded (the server's reply is not a complete JPEG, or the browser
cannot decode it) is shown as the one line "Cannot load image: KEY" in its
place.

It takes part in navigation like any other item. It can be marked DIRTY but
never CLEAN: the Clean button is disabled on it, and `c` is refused with
"cannot mark CLEAN: image could not be loaded", sending nothing. DIRTY is
recorded normally, so the image stops being todo and the pass can finish. In
later passes it comes back FLAGGED and, if it still cannot be loaded, again
as a line that only takes DIRTY. The image is fetched again each time it is
shown, so one that loads again is shown normally.

In grid mode an image the server cannot read is left out of the grids. One
the page cannot load or decode, or whose decoded size is not the size the
server packed it at, is taken out of its grid: the rest of the grid is shown
with a black gap in its place, or, when no image of the grid could be drawn,
the grid is dropped and the item now in its place is shown. Either way the
image follows the grids as a single item, judged on its own and never
covered by a grid verdict.

## Undo

`z` undoes your latest mark, a single image or a whole grid, and shows that
image or grid again so you can mark it again, after a fresh 200 ms wait.
Pressing it again undoes the mark before that, and so on; a held key acts
once, so holding `z` down undoes one mark. It works on the end and stop
screens too. The bar says what it undid, e.g. "Undone:
batch_001/img_00001.jpg is UNREVIEWED" or "Undone: grid of 12 images".

- Only marks made on this page since the last mode switch (`m`, `M`, `s`,
  `b`) can be undone; beyond them `z` says "Nothing to undo".
- A Reconnect forgets them too, and so, in grid mode, does a resize that
  repacks the grids (see [Grid mode](#grid-mode)): a grid marked DIRTY is no
  longer among the new ones.
- The page keeps them in memory only: `q` (Done), reloading or closing the
  tab forgets them. If the server has no mark left to undo (it was
  restarted), `z` says "Nothing to undo" and forgets the rest.
- An undo appends rows to `review.tsv` that restore each image's previous
  verdict and pass, or mark it UNREVIEWED again; see
  [Undo rows](../reference/work-directory.md#undo-rows).

With one page per server, `z` only undoes this page's marks. The server
keeps a single undo history, so with a second tab or client it undoes the
latest mark from any of them (see "Multi-client limits" in
[the security model](../reference/security-model.md#the-server-image-review-serve)),
and the page warns "Undid another client's mark: KEY is STATUS", naming up
to three images and how many more.

## Grid mode

Grid mode reviews one batch at a time. `m` and `M` stay on the current batch
while it has UNREVIEWED images, else take the first, in sorted order, that
has; `b` moves to the next, going round. The batch's UNREVIEWED images are
packed into grids the size of the image area in device pixels, at most 16384
on a side. `m` allows rotating images 90 degrees when that needs fewer
grids; `M` never rotates. The grids are shuffled, then shown largest first.

- One verdict covers every image in the grid: look at all of them before
  pressing `c`. `d` marks them all DIRTY.
- CLEAN can be refused on a grid that already holds a DIRTY or FLAGGED
  image; review it in single mode. See
  [Refused CLEAN on a grid](#refused-clean-on-a-grid).
- Grids hold only UNREVIEWED images; FLAGGED ones need single mode (see
  [Todo images](#todo-images)). An image that cannot be loaded (see
  [Images that cannot be loaded](#images-that-cannot-be-loaded)) follows the
  grids as a single item, as do images that did not fit a grid. These single
  items are judged one at a time.
- While the server packs the grids the page says "Computing grids...", then
  "Loading grid i/N" while it fetches the images; the grid is shown once
  every image is in. If the server is busy with another layout it says
  "Server busy computing grids; retrying" and tries again every 2 s. A
  failure says "Cannot compute grids; press [m] to retry or [s] for single
  mode".
- Resizing the window hides the grids and, once the resizing stops, repacks
  them for the new size, landing on the grid that holds the image you were
  on. A repack clears [undo](#undo). On the stop sign and the end screens a
  resize does not repack, so `z` still works there; the grids are repacked
  when you leave that screen.
- A batch with more than 1000 UNREVIEWED images is too large for the
  browser's grid mode: the page says "Batch too large for grid mode; use
  single mode [s]". A window too small to pack says "Window too small for
  grid mode".
- If the browser resets its graphics, the page says "Graphics reset;
  redrawing... (press [s] for single mode)" and draws the grid again.

### Refused CLEAN on a grid

A grid can still come to hold a DIRTY or FLAGGED image while you review, for
example when one of its images shares a source file with an image marked
DIRTY elsewhere, or another client marked one. CLEAN on such a grid is
refused, unless every image in it is DIRTY (that reverses the grid's own
verdict), and the bar says "grid contains an image already marked DIRTY -
review it in single mode"; DIRTY is still accepted. The page refuses from
the statuses it holds and sends nothing; if they were out of date, the
server refuses the CLEAN instead, and the page shows the same message and
rereads the statuses. An image shown on its own after the grids is refused
only by the server, once another client's mark has made it FLAGGED, with
"image already marked DIRTY in another pass - review it in single mode". A
refused CLEAN records nothing, does not move on and leaves nothing to undo.

## End of the list

Right past the last item (or Left before the first) shows a stop sign, "End
of the list", with how many todo items the list (in grid mode, the batch)
still has: "N todo left in this list", or in grid mode "N todo left in this
batch - [b] next batch". With none left in the list it shows the hint from
[End of a pass or batch](#end-of-a-pass-or-batch) instead. Press the arrow
again to go round to the other end: Right goes to the first item and Left
to the last.

There is no image on the stop sign, so `c` and `d` do nothing; `z`, `m`,
`M`, `s`, `b` and `q` work as anywhere. Once nothing in the pass is left to
review, the arrows past an end show the
[end-of-pass screen](#end-of-a-pass-or-batch) instead.

## End of a pass or batch

When the whole pass is done the page shows the end-of-pass screen, "Pass
N: nothing left to review. Stop serve (Ctrl-C), start the next one, then
press Reconnect (r).", and offers Reconnect for the next server.

When the list is done but the pass is not, an end screen says what is left:

- In single mode, which happens only when another client's marks changed
  the pass meanwhile: "No todo images remaining - press [s] to reload the
  list".
- In grid mode, where the list is one batch, while another batch has grids:
  "No todo images remaining - [b] next batch".
- In grid mode, when only FLAGGED images are left: the "No grid items for
  pass N" message from [Todo images](#todo-images).

## Lost connection and Reconnect

Your marks so far are always saved on the server. The page never retries by
itself; when it stops, it records nothing more until you press Reconnect (or
`r`).

Before you press Reconnect after a dropped tunnel, make sure ssh is running
again: see "The laptop side" in
[the security model](../reference/security-model.md#browser-review-over-a-unix-socket).

- **"Lost connection — your marks so far are saved on the server"**: the
  tunnel dropped or the server is not answering, for example because it was
  stopped or restarted. Reconnect is offered. If Reconnect gives the same
  message, the server is still not reachable; press it again once it is.
- **"Server restarted or changed work directory - press Reconnect (r)"**:
  the page was loaded from an earlier server, and a new one now answers on
  the same socket with the same token. Its keys may name another work
  directory's images, so the new server refuses everything from the page
  and nothing goes to the wrong images. Press Reconnect, and judge the
  images it then shows. See "A page belongs to one serve" in
  [the security model](../reference/security-model.md#browser-review-over-a-unix-socket).
- **"token rejected (server restarted?) - open the new URL"**: the server
  has another token, typically a restarted server without a fixed
  `$IMAGE_REVIEW_TOKEN`. Reconnect is not offered: open the server's new URL
  (pasting it into the same tab works; the page reloads to take it).

Reconnect says "Reconnecting...", then reloads the pass, the image list and the
statuses, and rebuilds the list in the mode you were in (grid mode lands on the
grid you were on). It says "Reconnected", or "Reconnected; now on pass N" when
the pass changed or after `q`. It forgets which marks `z` could [undo](#undo)
and waits the 200 ms again before a verdict counts. After `q` it loads whatever
the new server serves, from its first item (in grid mode, the first batch with
grids).

## Other errors

- **"No token. Open the URL printed by `image-review serve`."**: the page
  was opened without its token. The page takes the token out of the
  address bar and keeps it only in that tab, so a bookmark of the bare
  address, or the address copied into a new tab, shows this. Open the full
  URL that `serve` printed.
- **"unexpected reply from the server; reload the page"**: after a mark or
  undo the server's reply could not be read, nor the statuses reread. The
  page stops; reload it.
- **"unexpected reply from the server"** or **"server error (HTTP N)"**: a
  request failed or its reply could not be read. After a failed Reconnect,
  Reconnect stays offered.
- **"mark failed (HTTP N)"** and **"undo failed (HTTP N)"**: the server
  refused a mark or undo; nothing moves on.
- **"Marked CLEAN: KEY"** (or DIRTY, or "grid of N images"): the mark was
  recorded, but a grid repack replaced the list meanwhile, so the page does
  not move on.

## Done

`q` (or the Done button) means done with this server. The page stops: it
sends nothing more, frees the images and says "Done; waiting for the next
serve", with "Done. Your marks are saved. Stop serve (Ctrl-C), start the
next one, then press Reconnect (r)." in the image area. The tab keeps the
token and your name, so after the next `serve` starts on the same socket
path and token, Reconnect (or `r`) loads it. `q` does not stop the server.

## Recorded verdicts

Every verdict is saved to `review.tsv` in the work directory as it is made,
with who gave it and how: the reviewer, the display mode (`grid` for every
item in grid mode, an image shown on its own after the grids included), how
many images the one keypress covered and the image-review version of
`serve`. The columns are described in
[`review.tsv`](../reference/work-directory.md#reviewtsv).

The reviewer is the name from [the name box](#the-name-box). The server
takes 1-64 printable characters, not all spaces (no tabs or newlines). The
name is an unauthenticated claim, recorded as given; nothing verifies it.

**Upgrade everyone sharing a work directory together.** See
[Upgrading an older file](../reference/work-directory.md#upgrading-an-older-file).
