# Changelog

## Unreleased

### Upgrading: breaking and behaviour changes

- **Grayscale rendering changed.** DICOM grayscale windowing is restored to the original method, replacing 0.4.0's
  tail compression (and the 1% range trim that preceded it): the values outside the outer 0.01% of the min-max range
  are set aside, the 1st and 99th percentiles of the rest, shrunk by 2%, are the window, and values outside it are
  clipped. Bright text on a bright border, both above the window, merges again. 16-bit and 32-bit grayscale image
  files (modes `I;16`, `I` and `F`, and the gray of a 16-bit gray+alpha PNG) are windowed the same way; before, they
  were divided by 65535 or min-max scaled, so one bright pixel could hide all the text. `parameters.tail_fraction`
  is no longer written to `preprocess.json`, and `parameters.range_trim` is new. Preprocess again to get the new
  rendering.
- **DICOM pixel bits above BitsStored are now shown, and a HighBit other
  than BitsStored - 1 is refused.** A DICOM whose unused high bits hold
  content renders with an extra grayscale view of those bits to the right of
  the image (the image itself is unchanged), so the reviewer sees there is
  hidden content; preprocess again to get it. `export` still denies such a
  file (`overlay-unused-bits`): to release it, strip the bits and preprocess
  the stripped file again. A DICOM whose HighBit is not BitsStored - 1 is
  now `failed` (`unsupported: HighBit H with BitsStored S is not
  supported`), since bits below the stored ones would go unseen and
  unnoted.
- **`export` no longer allowlists a file holding content its review image
  does not show.** A file with an `overlay-plane`, `overlay-unused-bits`,
  `icon-image`, `embedded-thumbnail` or `trailing-data` note in `notes.tsv`
  (unless its trailing bytes are all zero) is now `NOT_REVIEWED` with the
  reason `unreviewed embedded content: <kinds>`, even if it was reviewed
  CLEAN; DIRTY stays DIRTY, and `alpha` never denies a file. This holds for
  overlay planes even with `preprocess --draw-overlays`: a drawn plane is
  clipped to the image and can be lost against bright pixels; and for
  unused pixel bits even though they are now shown as a view (see above).
  Such a file used to be allowlisted under the hash of its unstripped bytes, and
  stripping it, as the documentation advised, changed the hash so it matched
  no row. To release one, strip it, preprocess the stripped file into a new
  work directory and review it there. Re-exporting an existing work
  directory can therefore deny files it allowlisted before.
- **A DICOM whose uncompressed PixelData runs past its frame now gets a `trailing-data` note**, so `export` denies it
  unless the extra bytes are all zero (pydicom rendered the frame and ignored the rest). Preprocess again to get it.
- **`export` now reads `notes.tsv`, and refuses a work directory without it**
  (when `manifest.tsv` records hashes), or with a malformed one (exit 1,
  naming the file and line). Work directories made by 0.3.0 have none and
  can no longer be exported: preprocess the sources again into a new work
  directory and review them there.
- `export` now refuses a work directory whose `skipped.tsv` is missing (when `manifest.tsv` records hashes), instead of
  reporting as if no input had failed or been ignored.
- A `manifest.tsv` listing one `image_id` with two different `source_sha256` values is now rejected when the work
  directory is opened, instead of `export` silently using the first.
- **Wire API v9: client and server must be upgraded together.** `/statuses`,
  `POST /mark` and `POST /undo` now answer 400 for a pass above one million.
  A v9 client refuses a server of any other version (0.4.0 speaks v8), and
  older clients refuse a v9 server.
- A `review.tsv` already holding a `pass_number` above one million is now
  refused when the work directory is opened; edit it by hand to remove or
  correct those rows.

### Fixes

- A ZIP holding bytes outside its listed entries (two ZIPs joined with `cat`, a self-extracting or damaged archive)
  now fails as a whole: one `failed` row under the ZIP's path (`unsupported: ZIP holds N bytes outside its listed
  entries ...`) and no entry rows. Before, only the entries in its central directory were found, so the others were
  reviewed by nobody and the report did not mention them. Split such an archive and preprocess the parts.
- DICOM palette images whose 8-bit lookup table entries sit under a 16-bit descriptor no longer render near
  black (text at 255 on 0 became 0 and 1 and looked clean). When every red, green and blue entry is at most 255,
  they are scaled as 8-bit; tables with larger entries, and 8-bit descriptors, are unchanged. Preprocess again.
- A new `review.tsv`, `review.lock` or `export --output` file is now made durable with an `fsync` of its directory.
  For the lock and export files this is best effort (a failure is logged as a warning); creating `review.tsv` fails
  before writing any row if it cannot be synced.
- A second signal while `serve` (Ctrl-C, SIGTERM, SIGHUP) or `review` (Ctrl-C, SIGHUP; SDL turns SIGTERM into a quit
  event) is cleaning up no longer leaves `review.lock` behind. Before, it
  raised inside the lock release, or while waiting for a mark's fsync, so the store was never closed; on a
  cluster the next job usually lands on another node, which cannot reclaim the stale lock. Only the first signal
  now counts; later ones are ignored until the process exits.
- `serve` now holds at most 64 connections at once, gives each request 60 s to arrive in full (request line,
  headers and body, counted from when the server starts waiting for it), and reads at most 64 KiB of request line
  and headers per request (a body only after the token check). A connection without the token gets one request, the
  public page files included. Before, the 60 s timeout applied to each receive, so a client sending a byte every 59 s
  was never cut off; every connection got its own thread with no cap, and could make the server buffer about 6.5 MB
  of headers: anyone able to connect, without the token, could exhaust the serve user's processes (`RLIMIT_NPROC`)
  and memory on the node. Such a client can still fill the 64 slots and, by reconnecting, keep the reviewer out, but
  each connection now lasts at most one request and 60 s, and holds about 64 KiB.
- A pass number is now bounded at one million (`review --pass`, the serve API and `review.tsv`). Before, a client
  holding the token could record a pass with thousands of digits, after which `/current_pass` answered 500 on every
  later serve until `review.tsv` was edited by hand. `current_pass` now stops at one million.
- Grid review now pauses when resizing leaves no display area above the status bar, then rebuilds when space returns.
- Grayscale inputs that would lose pixel values or overflow their intensity
  range when converted to float32 now fail with an explicit unsupported
  reason instead of rendering a potentially misleading image.
- Stopping browser review now invalidates pending image reads and decodes, so
  late image work cannot replace the stop or Reconnect state or start a dwell.
- `export` now refuses while a crash-cut mark is the last thing in
  `review.tsv` (a grid mark with fewer rows on disk than its `grid_size`).
  Before, it allowlisted the images whose rows never landed, though the
  reviewer had judged their grid DIRTY. The rows that landed are kept. As
  with a torn last line, the next verdict recorded clears the refusal, so
  re-check the last grid reviewed before the interruption first, then export
  again.

## 0.4.0

Browser review over SSH, now `serve`'s default and the recommended way to
review remotely, with a grid mode; the Python remote client is deprecated;
wire API v8 (upgrade client and server together); and the documentation site.
Read **Upgrading** first.

### Upgrading: breaking and behaviour changes

- **`serve` now serves browser review by default.** Plain `image-review
  serve` now serves plain HTTP on a Unix socket and prints an ssh command
  and a URL, not an `ir://` connection string; a batch script running plain
  `serve` no longer writes `~/.image-review/connection-*.txt`. Add `--https`
  (or `--bind` or `--port`, which imply it) for the deprecated HTTPS server
  that `review --remote` needs; it logs a deprecation WARNING.
  `$IMAGE_REVIEW_SOCKET_PATH`, `$IMAGE_REVIEW_TOKEN`, `$IMAGE_REVIEW_VIA` and
  `$IMAGE_REVIEW_DIRECT` now take effect on plain `serve`, so a stale export
  changes what it does, and an invalid `$IMAGE_REVIEW_TOKEN` or
  `$IMAGE_REVIEW_VIA` now stops it (exit 1). `--via`, `--direct`,
  `--ssh-host` and `--socket-path` on the command line exit 2 in HTTPS mode.
  `serve --socket` from pre-release builds is gone (exit 2, no such option):
  drop it; plain `serve` does the same.
- **Deprecated: the Python remote client.** `review --remote`, `status
  --remote` (with `$IMAGE_REVIEW_REMOTE` and `--via`) and `serve --https`,
  `--bind` or `--port` each log one WARNING and keep working; they will be
  removed in a future release. With browser review there is no client to
  install or keep in sync: the server serves the page itself, so it always
  matches. The Python client must run the same version as the server, and a
  mismatch is refused. The laptop needs only ssh and a browser.
- **Wire API v8: client and server must be upgraded together.** `POST /mark`
  now answers 400 and records nothing for a `mode: "single"` body that does
  not name exactly one key, and for repeated keys in either mode (a
  multi-key "single" CLEAN would otherwise skip the grid rule below; both
  clients already send one key in single mode and never repeat a key). It
  also refuses CLEAN with `mode: "grid"` when any of its images is DIRTY or
  FLAGGED in that pass, unless every one is DIRTY, answering 409 and
  recording nothing. Both clients already refused it, but only against their
  own copy of the statuses, which can be stale (another client may have
  marked an image DIRTY meanwhile). The viewer and the browser page show
  "grid contains an image already marked DIRTY - review it in single mode"
  and re-read the statuses; the viewer does not treat the 409 as a lost
  connection. A v8 client refuses a server of any other version (0.3.0
  speaks v6), and older clients refuse a v8 server.
- **Requirements:** rectpack (still pinned at 0.2.2) moved from the `viewer`
  extra to the core dependencies, with the grid layout code (`layout.py`), so
  that `serve` lays out browser grids with the same code as the viewer.
  rectpack has no wheel: an offline or `--only-binary` install needs its
  sdist.
- **Requirements: the `preprocess` extra now needs numpy 2.1 or later** and
  adds imagecodecs (>= 2024.6.1) and tifffile (>= 2024.1.30), which decode
  16-bit colour PNGs and TIFFs; imagecodecs 2026.5.10 and later need numpy
  2.1, the minimum tested with these floors. Raised to work with numpy 2.1:
  matplotlib >= 3.8.4, scikit-image >= 0.23.1, scipy >= 1.13 and, in the `codecs`
  extra, pylibjpeg-openjpeg >= 2.3. Also raised, because the test suite
  fails with the old minimums: pydicom >= 3.0.1 and click >= 8.2.2. An
  environment pinned to numpy 1.x must upgrade before installing.
- **DICOM icon images are no longer rendered or reviewed.** preprocess used
  to add each DICOM's embedded icon (IconImageSequence) as a second image,
  `<path>#icon`, and list an icon it could not render as its own `failed`
  row. A new work directory has neither; nobody sees the icon, so remove
  icons before releasing files; notes.tsv lists each DICOM that has one
  (`icon-image`). Work directories made by an older version
  keep their icon rows and verdicts, and `export` still folds them into
  their file's row. A file whose name ends in `#icon` beside the file without
  it is no longer a preprocess collision, but `export` cannot vouch for it:
  rename it before preprocessing.
- **DICOM pixel bits above BitsStored are masked for every transfer
  syntax.** JPEG-LS and JPEG 2000 images used to be rendered with whatever
  their unused high bits held (pydicom clears them only for the other
  syntaxes), so a retired overlay stored there was drawn into the image; now
  every syntax renders only the stored bits (signed data sign-extended), and
  an input whose unused bits hold anything other than zeros or sign
  extension gets an `overlay-unused-bits` note
  (see `notes.tsv` below). The bits are not reviewed and stay in the file:
  clear them before releasing files.
- **Overlay planes are no longer drawn on the review image.** Work
  directories made from now on show the DICOM pixel data alone; overlays
  still in the file are listed in `notes.tsv` and must be stripped before
  release. An overlay that cannot be decoded no longer fails the
  input. Pass `preprocess --draw-overlays` for the old output (overlays at
  maximum brightness, and an undecodable one fails its input).
  `preprocess.json` records `parameters.draw_overlays`. `notes.tsv` lists
  every overlay (`overlay-plane`, `overlay-unused-bits`), drawn or not. The flag does not draw overlays stored in the unused pixel bits,
  which are always masked.

### CLI

- **Browser review over SSH.** `image-review serve` serves plain HTTP on a Unix
  socket, for a browser on your laptop reached through `ssh -L`. `serve` prints
  the ssh command and the `http://127.0.0.1:8080/#TOKEN` URL (or, when stdout is
  not a terminal, writes the URL to a private file under `~/.image-review/`).
  `--via` (or `$IMAGE_REVIEW_VIA`) fills in the login node of that command;
  `--direct` (or `$IMAGE_REVIEW_DIRECT`) is for laptops that can ssh to compute
  nodes without a jump host, and omits `-J`. `--ssh-host NAME` sets the node
  name it prints, for sites where the node's own FQDN does not work from the
  laptop. `--socket-path` (or `$IMAGE_REVIEW_SOCKET_PATH`) sets the socket path.
  `$IMAGE_REVIEW_TOKEN` (22-256 characters from `A-Za-z0-9_-`, generated inside
  the job, e.g. with `openssl rand -hex 16`) is reused as the token in socket
  mode, so the URL survives restarts; an invalid value is refused without being
  printed. The page starts with single images, like the viewer's single mode:
  enter a reviewer name, then `c` clean, `d` dirty (the item stays up 200 ms in its new
  status, then the next), Left/Right to move (past either end a stop sign
  with the list's todo count, then round to the other end, as the viewer's
  "End of list"), `z` to undo (this page's own marks, with one page per
  server: the undo history is shared).
  A verdict counts only once the image has been on screen for 200 ms, an image
  that cannot be loaded takes DIRTY only, and one bar at the bottom holds the
  buttons, the status and the progress, with the messages in its middle
  (a second row in a narrow window), its colour the item's status (grey
  UNREVIEWED, green CLEAN, red DIRTY, amber FLAGGED), with the display scale
  as a "⚠ 46%" badge below 100%. The name is asked for in a box at startup
  and changed from the bar; `?` (or `h`) shows every key. Nothing can be
  marked while the help or the name box is open, and a verdict waits 200 ms
  again once it closes. `m` (rotation `auto`)
  or `M` (`never`) switches to grid mode, one batch at a time (`b` for the
  next, `s` back to single mode): the batch's UNREVIEWED images are packed by
  the server into grids sized to the window, and one `c` or `d` marks every
  image in the grid, recorded with mode `grid`. CLEAN is refused if any image
  in the grid is already DIRTY or FLAGGED, unless every image in it is DIRTY
  (which reverses that grid's own verdict); an image that fails to load
  follows the grids as a single item; resizing the window repacks the grids
  and clears undo; a batch over 1000 images needs single mode. After a lost
  connection the page shows a Reconnect button (or `r`; nothing is retried
  automatically), which reloads the statuses, rebuilds the current mode with a
  fresh 200 ms wait and forgets what `z` could undo, so with a fixed socket
  path and `$IMAGE_REVIEW_TOKEN` a server restart needs only Reconnect; a
  rejected token says to open the new URL. `q` (or the Done button) means done
  with this server: nothing more is sent and the images are freed, but the tab
  keeps the token and the name, so after Ctrl-C and the next `serve` in the
  same shell, Reconnect (also offered once nothing in the pass is left to
  review; a batch's end in grid mode says `b` or `s` instead, as the viewer
  does) loads the next batch or pass in the same tab, from its start; closing
  the tab forgets the token. A tab still open on an earlier serve cannot
  record a verdict, or undo one, on the next serve's images (keys repeat
  across work directories): each serve start has its own instance id, which
  the page sends with every request, and the server refuses a request naming
  another (412) and records nothing; the page then says the server was
  restarted or now serves another work directory and offers Reconnect. The
  socket routes and the page needed no API version bump and do not change the
  HTTPS API (API v8, above, is for the `/mark` refusals). See the security
  model in the documentation for the limits.
- **`notes.tsv`: inputs written with something downstream tools should know about or strip.**
  `preprocess` now always writes `notes.tsv` (`image_id`, `kind`, `detail`;
  header only when empty) beside `skipped.tsv`, one row per note on a
  written input: `overlay-plane` for a DICOM with overlay planes (detail
  `group 0x6000` or `groups 0x6000, 0x6002`; overlays are not drawn by default,
  see Upgrading), `overlay-unused-bits` for a DICOM whose pixel data holds
  something above BitsStored other than zeros or sign extension (detail `bit 15`,
  `bits 12, 15`, or `bit 15 (overlay 0x6000)` when a 60xx group's
  OverlayBitPosition points at it), `icon-image` for a DICOM with an
  embedded icon (detail such as `IconImageSequence: 64x64 MONOCHROME2`),
  `alpha` for a raster with pixels that are not fully opaque (detail
  `120 of 2400 pixels not opaque`), and `embedded-thumbnail` for a raster with
  an EXIF IFD1 thumbnail (detail `EXIF IFD1 thumbnail (4096 bytes)`) or
  TIFF SubIFDs (`TIFF SubIFDs (2)`), or whose EXIF cannot be read (`EXIF
  unreadable: corrupt`); the thumbnails are not rendered. `trailing-data` is
  for a JPEG or PNG holding bytes that are not rendered: after the image
  (detail `2048 bytes after the JPEG end`), between or after an MPO's frames
  (`N bytes between or after the MPO's frames`), either followed by `; all
  zero bytes` or `; they begin with a JPEG signature` (or `a PNG
  signature`, `a DICOM preamble`, `another image signature`); `JPEG end not
  found` for a JPEG that renders without a findable EOI; `not checked: too
  many JPEG markers or frames` for one too costly to walk; or `trailing data
  unreadable: <ExceptionClass>`. Other formats are not checked. Notes
  never fail an input or change the exit status. When there are any, the
  summary line ends with
  `; N notes (K overlay-plane, J icon-image; see .../notes.tsv)`, and
  `preprocess.json` gains `counts.notes`, the count of every kind. The file
  holds source paths: treat it like `manifest.tsv`.
- **Help texts follow `serve`.** The Workflow list in `image-review --help`
  now includes `serve`, and `status --help` describes the deprecated
  `--remote` as reporting on a server, not reviewing it.

### Wire API (remote review)

- In socket mode, `POST /grids` returns grid layouts
  computed with the viewer's own packing code, for the browser page's grid
  mode. Socket mode only; it does not change the HTTPS API and needed no API
  version bump.

### Fixes

- preprocess now refuses inputs too large to render within a memory budget,
  before reading or decoding their pixels, as ordinary `failed` rows. One
  huge input could get preprocess killed by the system for lack of memory,
  with no message and a `.partial` directory left behind. A file or ZIP entry over
  1 GiB fails as `unsupported: input too large (N bytes; limit L)` without
  being read, and a ZIP entry over 1 GiB fails even if it is not an image.
  An image estimated to need more than 3 GiB to render (its pixels times a
  per-pixel cost measured for how that kind of image is rendered, for
  instance about 80 bytes for a grayscale DICOM and 32 for an RGB photo;
  `--jobs` workers each may use that much) fails as `unsupported: image too
  large (7000 x 6000, about 3.2 GiB to render; limit 3.0 GiB)`. A 50-megapixel
  RGB photo and a 4096 x 5120 16-bit grayscale DICOM render. ZIP entries
  compressed with a method other than stored or deflate (bzip2, LZMA) now fail
  as `unsupported: ZIP compression method bzip2`, since `zipfile` does not
  bound the memory used to decompress them. The limits and costs are recorded
  in `preprocess.json`; there is no option to change them yet. Since the
  estimate trusts Rows and Columns, a compressed DICOM is first checked
  against them from its codestream's own headers, without decoding: a JPEG,
  JPEG-LS, JPEG 2000 or HTJ2K frame must be Columns x Rows with
  SamplesPerPixel components (else `unsupported: compressed pixel data is
  12000 x 12000, not Columns x Rows 16 x 16`), JPEG 2000 and HTJ2K
  code-blocks must hold at least 256 pixels (else `unsupported: JPEG 2000
  code-blocks of fewer than 256 pixels`; a 738-byte 4096 x 4096 file of 1 x 1
  code-blocks needed 9.3 GB to decode), and have at most 256 quality layers,
  at most 1 MiB of TLM markers (else `unsupported: JPEG 2000 TLM markers over
  1 MiB`; decoders keep about 10 bytes per TLM byte) and only the marker
  segments of a JPEG 2000 Part 1 or HTJ2K header (else
  `unsupported: cannot read compressed pixel data header (unexpected JPEG
  2000 marker)`: decoders skip an unknown marker differently from its
  length, and Part 2's MCT is copied into every tile), an RLE frame must have
  SamplesPerPixel x BitsAllocated / 8 segments, none decoding to more than
  Rows x Columns bytes, and the pixel data must hold one frame, as pydicom
  decodes any past NumberOfFrames too. A 1 KB JPEG 2000 file declaring 16 x
  16 used to decode 144 million pixels before failing, and an 8 MB RLE file
  decoded 512 MB and rendered its first 256 bytes; both now fail before
  decoding. A header that cannot be read fails as `unsupported: cannot read
  compressed pixel data header (...)`. A transfer syntax pydicom has no
  decoder for (MPEG, HEVC, retired JPEG processes) still fails as `cannot
  decode <name>: ...`. Not checked: the number of JPEG 2000 tiles, which
  OpenJPEG caps at 65535 (873 MB was measured for a 3-component file of
  65025 tiles).
- Content carried only by a transparent image's alpha channel is now
  visible. preprocess showed the image composited over mid-gray beside the
  raw colour, so alpha-only text was invisible when the colour was
  mid-gray; it now shows the colour with alpha ignored beside the alpha
  channel itself (transparent black, opaque white, shown linearly). The
  views are processed separately, so the alpha view cannot flatten faint
  grayscale content. A grayscale image with alpha stays grayscale, and fully
  opaque alpha is still dropped. Such images get an `alpha` note in
  `notes.tsv`.
- 16-bit colour PNGs (RGB, RGBA and gray+alpha) and 16-bit RGB and CMYK
  TIFFs are now decoded at full depth, with imagecodecs and tifffile,
  instead of through Pillow, which keeps only the high byte. Colour is not
  adjusted: it is shown at its stored depth (divided by 65535), so it comes
  out within one level of the high-byte rendering, and an image that uses
  only part of its range looks dark. What changes is the rest: a 16-bit
  gray+alpha PNG is rendered as grayscale (with adaptive histogram
  equalization), not as RGB; associated (premultiplied) alpha in a TIFF is
  un-premultiplied; and CMYK inks and the alpha view are computed at full
  depth. A non-conformant 16-bit colour PNG that libpng refuses (for example
  with a chunk before IHDR) is now `failed` instead of rendered from its
  high byte. Other rasters, including 8-bit images and 16-bit grayscale
  PNGs, render exactly as before. `preprocess.json` records the imagecodecs
  and tifffile versions.
- Colour DICOM samples that exceed their declared depth now clip to white
  instead of wrapping around, at 8 bits or fewer (for example 16-bit palette
  entries under an 8-bit lookup table descriptor). Colour is still scaled by
  its declared depth alone and is not adjusted. A colour DICOM with float
  pixel data is now a `failed` input (`unsupported: float colour pixel
  data`).
- A float DICOM or mode `F` TIFF with a NaN or infinite pixel value is now a
  `failed` input (`unsupported: non-finite pixel values (1 NaN, 0
  infinite)`). Before, the one value made the min-max scaling collapse the
  rendering to a single colour, hiding burned-in text.
- `export` no longer gives a file the hash of a different file named like
  its DICOM icon: a ZIP entry `scan.png#icon` listed before `scan.png` made
  `scan.png`'s row carry the wrong hash, so it could be allowlisted although
  another file with its bytes was DIRTY. When the manifest records different
  hashes for `X` and `X#icon`, both now get a row of their own, each with its
  own hash, and both are at least `NOT_REVIEWED` (`ambiguous #icon: ...`;
  rename the file ending in `#icon`). So re-exporting an existing work
  directory may deny a file that an earlier export allowlisted.
- When some inputs fail, `preprocess` no longer tells you to "Re-run with
  --allow-skipped": the work directory has already been written, so that
  re-run is refused. It now says to review the work directory as it is, or to
  fix the inputs and preprocess into a new `--work-dir`.
- `serve` in socket mode without `--socket-path` no longer fails on a
  machine with a long host name: when the default socket path would be too
  long, an 8-character hash of the host name replaces the host in the file
  name.
- A `review.tsv` ending in a very long unfinished line (for example the
  zero-filled tail some file systems leave after a crash) no longer stops
  `status`, `review` and `export` with a traceback: it is treated as a torn
  last line like any other. A field longer than 131072 characters
  anywhere else in `review.tsv`, `manifest.tsv` or `skipped.tsv` is now
  reported as a parse error naming the file and line.
- Two writers on one work directory (possible, for example, after
  `review.lock` was deleted by hand while its `review` or `serve` still ran)
  could let a stale grid CLEAN override the other writer's DIRTY, putting the
  image in the export allowlist. A writer now refuses to record a verdict or
  an undo when `review.tsv` has changed since it last read or wrote it:
  `review` exits 1 with "Not recorded: ... changed since it was loaded", and
  `serve` answers that request and later ones with an error until restarted.
  Nothing is written.
- On Pillow 12, preprocess rendered only the primary image of an Ultra HDR
  JPEG (the default on recent Android cameras): Pillow 12 does not open it as
  an MPO, so its HDR gain map was never shown for review. Every image the
  MPF lists is now rendered side by side on every supported Pillow, as it
  already was on Pillow 10.3; an Ultra HDR file's gain map no longer gets a
  `trailing-data` note. A malformed MPF still renders the primary image.

### Documentation

- **Documentation moved to a site built from `docs/`**, at
  <https://jstorrs.github.io/image-review/>. `TUTORIAL.md` is now
  `docs/tutorials/` (local, remote and browser review) plus the command pages,
  `SPEC.md` is now `docs/reference/specification.md`, and the README's command
  reference is under `docs/commands/`. `SECURITY.md` is now the
  vulnerability-reporting policy; the threat model is
  `docs/reference/security-model.md`.
- **The security model now qualifies "still need the token" for a dropped
  tunnel.** If the ssh forward ends and another local user binds the port,
  Reconnect sends them the token, and a reload runs their page, which can read
  it from `sessionStorage`. Confirm ssh is running before Reconnect or a
  reload, and close the tab on a shared laptop.

## 0.3.0

Remote review over HTTPS, an allowlist export of the files that may be released, an audit trail in
`review.tsv`, undo, and a much stricter `preprocess`. Read **Upgrading** first
if you share work directories with other people.

### Upgrading: breaking and behaviour changes

- **Upgrade everyone sharing a work directory together.** The first `review`
  or `serve` on an old work directory rewrites `review.tsv` in a format 0.2.0
  cannot read, and an undo (`z`) adds rows that any version before undo
  rejects.
- **`preprocess` refuses a work directory that has content.** An empty one is
  fine. Choose a new `--work-dir` or remove the old one. Output is built in a
  `.NAME.partial` staging directory and renamed into place only on success.
- **`preprocess` exits 1 if any input failed.** Failed inputs are listed in
  `skipped.tsv`. Pass `--allow-skipped` to exit 0 anyway. Previously a bad
  input aborted the run or was silently dropped.
- **Work directories are private by default.** Directories are created 0700
  and files 0600. Before, they followed your umask. Use `--access group` to
  share with the work directory's Unix group.
- **Only one writer at a time.** `review` and `serve` take `review.lock`. A
  second writer exits with an error naming the holder. `status` and `export`
  only read.
- **`--no-rotate` is gone.** Use `--rotate never`. The default is now
  `--rotate auto`, which rotates grid images only when that saves a grid. In
  0.2.0 they were rotated whenever the packer liked. `m` follows `--rotate`,
  and `M` never rotates.
- **New status FLAGGED.** An image marked DIRTY in an earlier pass and not yet
  re-reviewed now shows as FLAGGED (orange) instead of UNREVIEWED. Grids never
  include DIRTY or FLAGGED images, so a grid keypress cannot clear them.
- **A lower `--pass` no longer overwrites later decisions.** An image's
  recorded pass never decreases.
- **Verdicts need the image on screen.** `c`/`d` are ignored for 200 ms after
  an image or grid appears. Keys typed while grids are being built are
  dropped.
- **Unloadable images are no longer skipped.** They show as placeholders that
  can only be marked DIRTY. This includes a JPG whose hash no longer matches
  the manifest.
- **Gamepads use SDL's game controller mappings.** B marks CLEAN, Y marks
  DIRTY, the D-pad navigates, A continues and Start quits. Pads without a
  mapping are unsupported; `SDL_GAMECONTROLLERCONFIG` can add one.
- **Diagnostics are logged to stderr** as `time LEVEL module: message`.
  Command output stays on stdout.
- **Rendering changed.** DICOM intensity tails are compressed instead of
  clipped. JPGs are written at quality 95 with 4:4:4 chroma. Colour, palette
  and compressed DICOMs, overlays and icon images are now rendered. A work
  directory from 0.2.0 keeps its old JPGs. Preprocess again to get the new
  rendering.
- **Invalid options fail before any work starts.** This covers
  `--batch-size` and `--pass` below 1, an unknown `--colormap`, and an empty
  or unknown `--batch`.
- **Malformed `review.tsv` or `manifest.tsv` stops the tool.** It reports
  "Cannot read work directory: ..." naming the file and line. Bad rows are no
  longer skipped and then silently dropped on the next save.
- **`manifest.tsv`, `review.tsv` and `skipped.tsv` must be UTF-8.** 0.2.0
  wrote `manifest.tsv` and `review.tsv` in the locale's encoding. A work
  directory from 0.2.0 whose source paths were non-ASCII, under a non-UTF-8
  locale, now fails with "not valid UTF-8". Re-encode each file, keeping its
  mode and group (they hold PHI), e.g. `cp -p f f.orig && iconv -f LATIN1 -t
  UTF-8 f.orig > f && rm f.orig` (writing into the existing `f` keeps its mode
  and group; use the locale's charset in place of LATIN1). `skipped.tsv` is new in 0.3.0; it
  needs this only if you preprocessed with 0.3.0 under such a locale.
- **A plain `pip install image-review` no longer installs the viewer or
  preprocess dependencies.** It installs only click and cryptography, enough
  for `serve`, `status` and `export`. Use `pip install 'image-review[all]'`
  for everything, or per machine: `[preprocess,codecs]` where you preprocess,
  `[viewer]` where you `review`. A command whose extra is missing exits 1
  with `this command needs the <extra> extra: pip install
  'image-review[<extra>]'`. Without `codecs`, `preprocess` lists compressed
  DICOMs that pydicom cannot decode by itself or through Pillow (e.g. JPEG
  Lossless, JPEG-LS) as failed.
- **A badly named input fails even if it would have been ignored.** An
  AppleDouble file, a non-image or an empty ZIP whose name (or a directory
  above it) is not UTF-8 or holds a control character or U+2028/U+2029 is now
  `failed`, so `preprocess` exits 1 unless `--allow-skipped`. Rename it or
  remove it.
- **A 0.2.0 work directory exports an empty allowlist.** Its manifest has no
  `source_sha256`, and `export` never allowlists a file without one (its CLEAN
  files are in `--report`, marked as not allowlisted). To release files,
  preprocess again into a new work directory and redo the verdicts.
- **Dependencies have minimum versions** (see Requirements below).
  `cryptography`, `scipy`, `python-gdcm`, `pylibjpeg` and `pylibjpeg-openjpeg`
  are new.

### CLI

- New commands:
  - `serve` serves a work directory over HTTPS, with a self-signed
    certificate and a bearer token in an `ir://` connection string.
  - `export` writes an allowlist (default deny) as TSV: only the source
    files that may be released, each by path and source SHA-256, so a file is
    released only if both match. A file is listed only if it was reviewed
    CLEAN (its icon too), the manifest has its hash, and no file that is not
    CLEAN has the same recorded hash (`source_sha256`). CLEAN files of a work directory without
    hashes (made by an older version) are never allowlisted. `--report FILE`
    writes every other file, with its status (DIRTY, UNREVIEWED,
    NOT_REVIEWED, IGNORED, or CLEAN not allowlisted) and reason, for audit
    and follow-up only. The allowlist goes to stdout or `--output FILE`; the
    report is written first; neither overwrites. It refuses while a writer
    holds the work directory unless `--allow-live` is given.
- Global `-v`/`--verbose` (debug) and `-q`/`--quiet` (warnings and errors
  only) options. They go before the command.
- New `preprocess` options:
  - `--access {private,group}`, also read from `$IMAGE_REVIEW_ACCESS`;
  - `--allow-skipped`;
  - `--jobs N` for parallel rendering. It defaults to
    `$SLURM_CPUS_PER_TASK` or the usable CPUs, and the output is identical
    for any N.
- New `review` options:
  - `--reviewer NAME`, also read from `$IMAGE_REVIEW_REVIEWER`. It defaults
    to your login name.
  - `--rotate {auto,always,never}` replaces `--rotate/--no-rotate`.
  - `--remote ir://...` and `--via user@login-node`, also read from
    `$IMAGE_REVIEW_REMOTE` and `$IMAGE_REVIEW_VIA`. They let you review a
    `serve` instance, optionally through an SSH tunnel.
- New `status` options: `--remote`/`--via`, and `--check`. With `--check`,
  `status` exits 1 while any image is UNREVIEWED or any input failed to
  preprocess.
- New review keys:
  - `z` undoes your last mark (an image or a whole grid);
  - `b` on the end-of-list screen opens the next batch with work left.
- The review screen shows the display scale and rebuilds grids on resize.

### On-disk format

- **`manifest.tsv` and `skipped.tsv` are always UTF-8** with `\r\n` line
  endings, whatever the locale. 0.2.0 wrote `manifest.tsv` in the locale's
  encoding; `review.tsv` was likewise locale-encoded and is now read as UTF-8.
- **`skipped.tsv`** (new). Every input that was not rendered, with kind
  `failed` (corrupt, unsupported, unreadable, or its image id collides with
  another input's) or `ignored` (not an image). `status` reports the counts.
- **`review.tsv`**:
  - **Append-only.** Each mark appends rows; the last row per image wins. A
    torn last line from a crash is tolerated and cut off before the next
    append.
  - **Four audit columns** follow the original five: `reviewer`, `mode`
    (`single`, `grid` or `undo`), `grid_size` and `tool_version`. A
    five-column file is migrated once, the first time `review` or `serve`
    opens it, with the new columns empty for existing rows. 0.2.0 cannot
    read a migrated file.
  - **Undo rows.** `z` appends rows with mode `undo`. Each restores the
    previous verdict, or sets status `UNREVIEWED` for an image that had none.
    Versions before undo reject such a file.
- **`manifest.tsv`** gains `source_sha256` (the source file or ZIP entry) and
  `jpeg_sha256` (the JPG as written). A JPG that no longer matches is
  unloadable. Old three-column manifests still load, without the check.
- **`preprocess.json`** (new). It records how the work directory was made:
  tool and library versions, resolved sources, parameters and input counts.
  It holds source paths and is never served.
- **`review.lock`** (new). It is held by the one writer (`review` or `serve`)
  and records host, user, pid, start time and boot id. A stale lock is
  reclaimed automatically only when its process is provably gone on the same
  host. Otherwise, delete it by hand.
- **Access modes.** `private` gives directories 0700 and files 0600. `group`
  gives directories 2770 (setgid) and files 0660. Nothing is world-readable,
  and `review.tsv` follows the work directory's mode. `review`, `serve` and
  `status` warn about an existing work directory that other users can read,
  but never change its mode.

### Wire API (remote review)

`serve` and `review`/`status --remote` are new in this release. They speak
wire API version **6** (`connection.API_VERSION`), and the client refuses a
server that reports a different version. Install the same image-review
version on both machines. The versions reached during development:

- **v2** added `GET /skipped` (counts only).
- **v3** added `FLAGGED` to the status vocabulary in `/statuses`. `/mark`
  responses hold only the verdict just recorded.
- **v4** dropped `batch` from the `POST /mark` body; the server takes it from
  the manifest. It added `reviewer` and `mode`.
- **v5** added `POST /undo`.
- **v6** made `GET /skipped` always send counts (`{"failed": 0, "ignored": 0}`
  when the work dir has no `skipped.tsv`) instead of `null`. The server also
  refuses a repeated query parameter with 400 instead of using the first value.

`GET /version` reports the API version and package version. A server without
it is reported as "too old to report its API version".

### Fixes

- **Left from the end screen in todo-only mode goes to the last todo item.** It
  used to skip that item when it was left unmarked.
- **`M` (shift+m) uses the Shift state of the key press itself.** It used to
  read the live keyboard, so releasing Shift before the press was handled
  (for example after a slow remote fetch) opened grid mode with the wrong
  rotation.
- **A window too short to fit an image no longer shows the previous one.** With
  the window at or below the 50px status bar, moving to another item kept the
  old item's pixels under the new item's name, and its review dwell started,
  so `c` could mark an image nobody saw. Now only the bars are painted, and the
  dwell starts once the item's pixels are on screen. Hiding the image this way
  after it appeared restarts the dwell.
- **Names that are not UTF-8 or hold a control character are failed inputs.**
  A file whose name is not valid UTF-8 used to crash `preprocess` after every
  image was rendered, losing the run; a name holding a newline, tab, other
  control character or U+2028/U+2029 was reviewed but then made `export`
  refuse the whole study. Such an input (a file, a ZIP entry, or everything
  under a directory or ZIP so named) is now a `failed` row in `skipped.tsv`
  under its escaped name: a non-UTF-8 byte or ASCII control as `\xNN`, any
  other as `\uNNNN`. Rename it and preprocess again.
- **`export` lists ignored inputs.** It used to leave out the `ignored` rows
  of `skipped.tsv`, so an input that was not an image (a PDF, a Word file, a
  DICOMDIR, ...) was never mentioned and nobody followed it up. Each is now a
  row of the report with status `IGNORED` after all other rows, never
  allowlisted. A work
  directory made by a 0.3.0 pre-release may hold an ignored name with a
  control character or U+2028/U+2029, which now makes `export` refuse;
  preprocess again into a new work directory (its verdicts must be redone).
- **A `review.lock` with an absurd pid is reported, not a traceback.** A pid
  of 2^31 or more made `review`, `serve` and `export` crash with an
  `OverflowError` instead of naming the corrupt lock. It is now refused as
  malformed like any other bad lock. A stray
  `review.lock.<host>.<boot_id>.<pid>.*` file whose pid text `int()` cannot
  read (such as `²`) or that is 2^31 or more made every writer on that machine
  fail to open the work directory until it rebooted; such files are now
  skipped.

### Requirements

Python >= 3.12. Minimum dependency versions, checked by running the test suite
on CPython 3.12 from wheels, by extra:

- core: click >= 8.2, cryptography >= 41
- `preprocess`:
  - matplotlib >= 3.7.3
  - numpy >= 1.26
  - pydicom >= 3.0
  - Pillow >= 10.3, excluding 11.x. Pillow 11 misdecodes a multi-frame MPO.
  - scikit-image >= 0.22
  - scipy >= 1.11.2
  - tqdm >= 4.60
- `codecs`: python-gdcm >= 3.0.25, pylibjpeg >= 2.0, pylibjpeg-openjpeg >= 2.0
- `viewer`: pygame-ce >= 2.3.1, Pillow (as above), rectpack == 0.2.2
- `all`: `preprocess`, `codecs` and `viewer`; `dev`: `all` plus the lint,
  type-check and coverage tools
