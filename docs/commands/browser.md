# The browser page

The page that [`image-review serve`](serve.md) serves for browser review: what
you see at `http://127.0.0.1:8080/#TOKEN` once the ssh forward is running.
It is the browser's counterpart of the [`review`](review.md) viewer and
follows the viewer's rules for todo images, refused CLEAN on a grid,
unloadable images, undo and grid packing; this page links to them. For the
workflow (starting `serve`, the ssh command, moving on to the next batch),
see [Browser review over SSH](../tutorials/browser-review.md).

The page starts in single mode, showing the current pass's todo images
(UNREVIEWED and FLAGGED) one at a time, in random order. In grid mode it
shows packed grids of one batch's UNREVIEWED images; see
[Grid mode](#grid-mode).

## The name box

With no name set in the tab, the page first opens a box, "Who is
reviewing?". Type your name and press Enter (or click OK). The name is
recorded with every verdict, as `--reviewer` is for the viewer (see
[Recorded verdicts](review.md#recorded-verdicts)). It must be 1-64
characters and not all spaces; while it is not, the field is marked and OK
is disabled. Until a name is set nothing can be reviewed, and the bar says
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
left to the browser. There is no gamepad support, and none of the viewer's
`n`, `u`, Space, `w` or `f`.

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
on screen for 200 ms, so a key pressed as an image appears is ignored, as in
the viewer. After `c` or `d` the image stays up for 200 ms with the bar in
its new colour, then the next todo item appears and no message is left, so
the progress stays in view while you mark.

An image that cannot be loaded shows the one line "Cannot load image: KEY"
in its place, and follows the viewer's rule in
[Images that cannot be loaded](review.md#images-that-cannot-be-loaded). The
Clean button is disabled on it, so only `c` shows the refusal.

## Undo

`z` undoes the latest mark and shows that image or grid again, with a fresh
200 ms wait; pressing it again goes further back. The bar says what it
undid, e.g. "Undone: batch_001/img_00001.jpg is UNREVIEWED" or "Undone:
grid of 12 images". When this page has no marks left to undo, `z` says
"Nothing to undo". As in the viewer, switching mode (`m`, `M`, `s`, `b`)
forgets the marks `z` could undo (see [Undo](review.md#undo)); so do a
Reconnect and, in grid mode, a resize that repacks the grids (see
[Grid mode](#grid-mode)).

With one page per server, `z` only undoes this page's marks. The server
keeps a single undo history, so with a second tab or client it undoes the
latest mark from any of them (see "Multi-client limits" in
[the security model](../reference/security-model.md#the-server-image-review-serve)),
and the page warns "Undid another client's mark: KEY is STATUS", naming up
to three images and how many more.

## Grid mode

Grid mode works as in the viewer (see [`review`](review.md)), one batch at a
time. `m` and `M` stay on the current batch while it has UNREVIEWED images,
else take the first that has; `b` moves to the next, going round. Grids are
shown largest first.

- One verdict covers every image in the grid: look at all of them before
  pressing `c`. `d` marks them all DIRTY.
- CLEAN can be refused on a grid that already holds a DIRTY or FLAGGED
  image; review it in single mode. See
  [Refused CLEAN on a grid](review.md#refused-clean-on-a-grid).
- Grids hold only UNREVIEWED images; FLAGGED ones need single mode. An image
  that fails to load or decode leaves a black gap and follows the grids as a
  single item, as do images that did not fit a grid. These single items are
  judged one at a time.
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
  pass N" message from [Todo images](review.md#todo-images), ending " -
  press [s]".

## Lost connection and Reconnect

Your marks so far are always saved on the server. The page never retries by
itself; when it stops, it records nothing more until you press Reconnect (or
`r`).

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
