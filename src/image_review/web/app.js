"use strict";

// Image review in the browser (experimental). The pygame client is the reference:
// see SPEC.md, "Browser client (experimental)". Grid mode only displays grids so
// far: its verdicts arrive in a later version.

const TOKEN_HASH = /^#([A-Za-z0-9_-]+)$/;
const TODO_STATUSES = new Set(["UNREVIEWED", "FLAGGED"]);
const STATUSES = new Set(["CLEAN", "DIRTY", "UNREVIEWED", "FLAGGED"]);
const MIN_DWELL_MS = 200;
const MAX_REVIEWER_LENGTH = 64;
// A grid verdict covers every image in it, so grids hold only images not yet judged DIRTY or
// FLAGGED (controller.GRID_ELIGIBLE); with the todo filter that leaves UNREVIEWED.
const GRID_ELIGIBLE = new Set(["UNREVIEWED", "CLEAN"]);
const MAX_GRID_KEYS = 1000; // server.MAX_GRID_KEYS
const MIN_GRID_SIDE = 256; // server.MIN_GRID_SIDE
const MAX_GRID_SIDE = 16384; // server.MAX_GRID_SIDE
const GRID_IMAGE_FETCHES = 4; // concurrent /image requests while a grid is drawn
const GRID_RETRY_MS = 2000; // between /grids attempts while the server is busy
const REPACK_DELAY_MS = 300; // resize debounce before grids are repacked

const TOKEN_REJECTED = "token rejected (server restarted?)";
const LOST_CONNECTION = "Lost connection — your marks so far are saved on the server";
const NOTHING_TO_UNDO = "Nothing to undo";
const UNLOADABLE_CLEAN = "cannot mark CLEAN: image could not be loaded";
const NAME_NEEDED = "Enter your name (1-" + MAX_REVIEWER_LENGTH + " characters) to start reviewing";
const GRID_VERDICTS_LATER = "Grid verdicts arrive in a later version; press [s] for single mode";
const COMPUTING_GRIDS = "Computing grids...";
const SERVER_BUSY = "Server busy computing grids; retrying";
const BATCH_TOO_LARGE = "Batch too large for grid mode; use single mode [s]";
const WINDOW_TOO_SMALL = "Window too small for grid mode";

// ---- Pure helpers ----

function isTodo(status) {
  return TODO_STATUSES.has(status);
}

// The client's check; the server's parse_reviewer has the final say.
function validReviewer(name) {
  const length = [...name].length; // code points, as the server counts them
  return length >= 1 && length <= MAX_REVIEWER_LENGTH && name.trim() !== "";
}

// Fisher-Yates, returning a new array.
function shuffle(items, random = Math.random) {
  const result = items.slice();
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}

// A JPEG that starts with SOI and ends with EOI, so a truncated body is refused.
function isCompleteJpeg(bytes) {
  const n = bytes.length;
  return n >= 4 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[n - 2] === 0xff && bytes[n - 1] === 0xd9;
}

// A review item is {kind: "single", key} or {kind: "grid", placements, keys};
// the keys one verdict covers.
function itemKeys(item) {
  return item.kind === "grid" ? item.keys : [item.key];
}

// The item's status for the todo list.
function itemStatus(item, statuses) {
  return item.kind === "grid" ? gridStatus(item.keys, statuses) : statuses.get(item.key);
}

// As controller._grid_status: DIRTY if any image is not grid-eligible, else
// UNREVIEWED while any is todo, else CLEAN.
function gridStatus(keys, statuses) {
  const found = new Set(keys.map((key) => statuses.get(key)));
  if (![...found].every((status) => GRID_ELIGIBLE.has(status))) {
    return "DIRTY";
  }
  return [...found].some(isTodo) ? "UNREVIEWED" : "CLEAN";
}

// As controller._grid_clean_refused: CLEAN on a grid holding a DIRTY or FLAGGED
// image is refused, unless the whole grid is DIRTY.
function gridCleanRefused(keys, statuses) {
  const found = new Set(keys.map((key) => statuses.get(key)));
  const eligible = [...found].every((status) => GRID_ELIGIBLE.has(status));
  return !eligible && !(found.size === 1 && found.has("DIRTY"));
}

// The todo singles of a pass, shuffled: single mode's list.
function singleItems(manifest, statuses, random = Math.random) {
  return shuffle(
    manifest.filter((row) => isTodo(statuses.get(row.key))).map((row) => ({ kind: "single", key: row.key })),
    random,
  );
}

// Whether grid mode packs a key: as controller._review_rows with the todo filter,
// todo and grid-eligible, i.e. UNREVIEWED.
function isGridTodo(status) {
  return isTodo(status) && GRID_ELIGIBLE.has(status);
}

// The keys grid mode packs for `batch`, in manifest order.
function gridKeys(manifest, statuses, batch) {
  return manifest.filter((row) => row.batch === batch && isGridTodo(statuses.get(row.key))).map((row) => row.key);
}

// The batches holding any key grid mode packs.
function gridBatches(manifest, statuses) {
  return new Set(manifest.filter((row) => isGridTodo(statuses.get(row.key))).map((row) => row.batch));
}

// Todo rows of `batch` (null: every batch) that grid mode leaves out, as
// controller._held_back_count with the todo filter: the FLAGGED ones.
function heldBackCount(manifest, statuses, batch) {
  return manifest.filter((row) => {
    const status = statuses.get(row.key);
    return (batch === null || row.batch === batch) && isTodo(status) && !GRID_ELIGIBLE.has(status);
  }).length;
}

// As controller._held_back_message (in session), or null.
function heldBackMessage(pass, held) {
  if (held === 0) {
    return null;
  }
  const images = held === 1 ? "image needs" : "images need";
  return "No grid items for pass " + pass + "; " + held + " FLAGGED/DIRTY " + images + " single-mode review" +
    " - press [s]";
}

// By code point, as Python's sorted (a plain sort compares UTF-16 code units).
function compareCodePoints(a, b) {
  const x = Array.from(a, (c) => c.codePointAt(0));
  const y = Array.from(b, (c) => c.codePointAt(0));
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    if (x[i] !== y[i]) {
      return x[i] - y[i];
    }
  }
  return x.length - y.length;
}

function sortedBatches(manifest) {
  return [...new Set(manifest.map((row) => row.batch))].sort(compareCodePoints);
}

// As controller.next_batch with wrap: the first batch after `current` (null: from
// the first) that `accepts`, going round once back to `current` itself; null if none.
function nextBatch(batches, current, accepts) {
  const start = current === null ? -1 : batches.indexOf(current);
  for (let step = 1; step <= batches.length; step++) {
    const batch = batches[(start + step) % batches.length];
    if (accepts(batch)) {
      return batch;
    }
  }
  return null;
}

const isCount = (value) => Number.isInteger(value) && value >= 1;

function parsePlacement(p, width, height) {
  if (!p || typeof p !== "object" || typeof p.key !== "string" || typeof p.rotated !== "boolean") {
    throw new BadReply("placement");
  }
  const { key, x, y, w, h, rotated, source } = p;
  if (!Number.isInteger(x) || !Number.isInteger(y) || x < 0 || y < 0 || !isCount(w) || !isCount(h)) {
    throw new BadReply("placement rect");
  }
  if (x + w > width || y + h > height) {
    throw new BadReply("placement outside the grid");
  }
  if (!Array.isArray(source) || source.length !== 2 || !source.every(isCount)) {
    throw new BadReply("placement source");
  }
  const [sw, sh] = source;
  const [fw, fh] = rotated ? [h, w] : [w, h]; // the fitted size, upright
  // layout.fit_size never enlarges and keeps the aspect ratio to within a pixel's rounding
  if (fw > sw || fh > sh || Math.abs(fw * sh - fh * sw) >= sw + sh) {
    throw new BadReply("placement size");
  }
  return { key, x, y, w, h, rotated, source: [sw, sh] };
}

function overlaps(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

// A /grids reply for `sentKeys` packed into width x height grids, checked whole:
// every sent key exactly once across the grids and left_out, nothing else, and
// integer rects inside the grid that do not overlap.
function parseGridPlan(reply, sentKeys, width, height) {
  if (!isCount(width) || !isCount(height)) {
    throw new BadReply("grid size");
  }
  if (!reply || !Array.isArray(reply.grids) || !Array.isArray(reply.left_out)) {
    throw new BadReply("grids");
  }
  const sent = new Set(sentKeys);
  const seen = new Set();
  const claim = (key) => {
    if (typeof key !== "string" || !sent.has(key) || seen.has(key)) {
      throw new BadReply("grid key");
    }
    seen.add(key);
  };
  const grids = reply.grids.map((grid) => {
    if (!Array.isArray(grid) || grid.length === 0) {
      throw new BadReply("grid");
    }
    const placements = grid.map((p) => parsePlacement(p, width, height));
    placements.forEach((p, i) => {
      claim(p.key);
      if (placements.slice(0, i).some((q) => overlaps(p, q))) {
        throw new BadReply("overlapping placements");
      }
    });
    return placements;
  });
  const leftOut = reply.left_out.map((key) => {
    claim(key);
    return key;
  });
  if (seen.size !== sent.size) {
    throw new BadReply("grid keys missing");
  }
  return { grids, leftOut, width, height };
}

function gridItem(placements) {
  return { kind: "grid", placements, keys: placements.map((p) => p.key) };
}

// As controller._grid_items: the grids shuffled, then (stably) the fullest first,
// then each left-out key as a single item.
function orderGridItems(plan, random = Math.random) {
  const grids = shuffle(plan.grids.map(gridItem), random);
  grids.sort((a, b) => b.keys.length - a.keys.length);
  return grids.concat(plan.leftOut.map((key) => ({ kind: "single", key })));
}

// `items` with `failed` keys taken out of grid item `index` (dropped if it is left
// empty) and appended as single items, unless already listed as one: an image
// that cannot be drawn is never covered by a grid verdict.
function demoteKeys(items, index, failed) {
  const gone = new Set(failed);
  const kept = items[index].placements.filter((p) => !gone.has(p.key));
  const result = items.slice();
  if (kept.length > 0) {
    result[index] = gridItem(kept);
  } else {
    result.splice(index, 1);
  }
  const singles = new Set(result.filter((item) => item.kind === "single").map((item) => item.key));
  return result.concat(failed.filter((key) => !singles.has(key)).map((key) => ({ kind: "single", key })));
}

// The canvas matrix [a, b, c, d, e, f] that draws a source image (at its natural
// size, from 0,0) into placement p. A rotated image turns clockwise, as pygame's
// rotate(-90) in grid_packer: its top edge lands on the rect's right edge.
function placementTransform(p) {
  const [sw, sh] = p.source;
  if (p.rotated) {
    return [0, p.h / sw, -p.w / sh, 0, p.x + p.w, p.y];
  }
  return [p.w / sw, 0, 0, p.h / sh, p.x, p.y];
}

// The smallest fitted / source ratio among the placements (1 at most), as
// GridSpec.min_scale: the grid's own shrink, before the display scale.
function minScale(placements) {
  return placements.reduce((scale, p) => {
    const [fw, fh] = p.rotated ? [p.h, p.w] : [p.w, p.h];
    return Math.min(scale, fw / p.source[0], fh / p.source[1]);
  }, 1);
}

// The index of the next item whose status is todo, searching forward from `from`
// and wrapping round (including `from` itself last); -1 if none.
function nextTodoIndex(items, statuses, from) {
  for (let step = 1; step <= items.length; step++) {
    const i = (from + step) % items.length;
    if (isTodo(itemStatus(items[i], statuses))) {
      return i;
    }
  }
  return -1;
}

function countTodo(items, statuses) {
  return items.filter((item) => isTodo(itemStatus(item, statuses))).length;
}

// "k1 is S1, k2 is S2, k3 is S3 and N more"
function describeStatuses(statuses) {
  const entries = [...statuses];
  const shown = entries.slice(0, 3).map(([key, status]) => key + " is " + status);
  const more = entries.length > 3 ? " and " + (entries.length - 3) + " more" : "";
  return shown.join(", ") + more;
}

class BadReply extends Error {}

function parsePass(reply) {
  const value = reply && reply.pass;
  if (!Number.isInteger(value) || value < 1) {
    throw new BadReply("pass");
  }
  return value;
}

function parseManifest(reply) {
  if (!Array.isArray(reply)) {
    throw new BadReply("manifest");
  }
  return reply.map((row) => {
    if (!row || typeof row.key !== "string" || typeof row.batch !== "string") {
      throw new BadReply("manifest row");
    }
    return { key: row.key, batch: row.batch };
  });
}

function parseStatusMap(reply) {
  if (!reply || typeof reply !== "object" || Array.isArray(reply)) {
    throw new BadReply("statuses");
  }
  const result = new Map();
  for (const [key, status] of Object.entries(reply)) {
    if (!STATUSES.has(status)) {
      throw new BadReply("status");
    }
    result.set(key, status);
  }
  return result;
}

// ---- Token and reviewer name (sessionStorage, guarded: storage may be blocked) ----

function readStored(name) {
  try {
    return sessionStorage.getItem(name);
  } catch (error) {
    return null;
  }
}

function writeStored(name, value) {
  try {
    if (value === null) {
      sessionStorage.removeItem(name);
    } else {
      sessionStorage.setItem(name, value);
    }
  } catch (error) {
    // storage blocked: the value lives for this page load only
  }
}

// The token arrives in the URL fragment (never sent to the server). Keep it in
// sessionStorage (falling back to memory if storage is blocked) and rewrite this
// tab's history entry without it; the original URL may remain in browser history.
function takeToken() {
  const match = TOKEN_HASH.exec(location.hash);
  let token = match ? match[1] : null;
  if (token) {
    writeStored("token", token);
  } else {
    token = readStored("token");
  }
  if (match) {
    history.replaceState(null, "", location.pathname);
  }
  return token;
}

// ---- State ----

const state = {
  token: takeToken(),
  pass: null,
  manifest: [], // {key, batch} rows in manifest order
  batches: [], // the manifest's batches, sorted
  batchOf: new Map(), // key -> batch
  statuses: new Map(), // key -> status, for the pass under review
  items: [], // review items in order: the todo list at startup, shuffled
  index: -1, // the current item, or -1 for the end screen
  reviewer: null, // a name that passed validReviewer, or null
  busy: false, // a request is in flight, or the list is loading
  dead: false, // token rejected or connection lost: nothing more is sent
  mode: "single",
  rotation: "auto", // grid mode's rotation policy: "auto" or "never"
  batch: null, // grid mode's batch, or null when no batch has grid items
  gridSize: null, // {width, height} in device pixels the grids are packed for, once measured
  gridCache: null, // {key, plan}: the last /grids layout, as controller.GridCacheKey
  gridsRequest: null, // the /grids request in flight, if any: never two at once
  buildSeq: 0, // bumped by every grid build or abandon, so a stale build is dropped
  repackTimer: null, // the pending resize repack, if any
  landKey: null, // the key whose item a grid build lands on
  sourceScale: 1, // the shown grid's own shrink (minScale)
  marked: [], // key arrays this page marked, less those undone: what z may undo
  loaded: false, // the current item's image is on screen (else a placeholder)
  scale: null, // screen pixels per image pixel, while an image is shown
  dwell: "none", // "none", "running" (painted, under MIN_DWELL_MS) or "over"
  dwellSeq: 0, // bumped by every dwell start or reset, so a stale timer is dropped
  showSeq: 0, // bumped by every showItem, so a stale load is dropped
  objectUrl: null, // the blob URL the <img> shows
};

const $ = (id) => document.getElementById(id);
const ui = {
  reviewer: $("reviewer"),
  progress: $("progress"),
  itemStatus: $("item-status"),
  scale: $("scale"),
  where: $("where"),
  mode: $("mode"),
  stage: $("stage"),
  image: $("image"),
  grid: $("grid"),
  placeholder: $("placeholder"),
  message: $("message"),
  status: $("status"),
  clean: $("clean"),
  dirty: $("dirty"),
  prev: $("prev"),
  next: $("next"),
  undo: $("undo"),
};

// ---- Requests ----

// Thrown once the page has stopped (token rejected, connection lost); callers just unwind.
class Stopped extends Error {}

class HttpError extends Error {
  constructor(status) {
    super("HTTP " + status);
    this.status = status;
  }
}

function stop(text) {
  state.dead = true;
  state.busy = false;
  say(text);
  render();
  return new Stopped(text);
}

// fetch with the token. A 401 or a network failure stops the page.
async function call(path, options = {}) {
  const headers = { ...options.headers, Authorization: "Bearer " + state.token };
  let response;
  try {
    response = await fetch(path, { ...options, headers, cache: "no-store" });
  } catch (error) {
    throw stop(LOST_CONNECTION);
  }
  if (response.status === 401) {
    state.token = null;
    writeStored("token", null);
    throw stop(TOKEN_REJECTED);
  }
  return response;
}

// Read a body; a failure mid-body is a lost connection.
async function readBody(response, how) {
  try {
    return await response[how]();
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new BadReply("JSON");
    }
    throw stop(LOST_CONNECTION);
  }
}

async function getJson(path) {
  const response = await call(path);
  if (!response.ok) {
    throw new HttpError(response.status);
  }
  return readBody(response, "json");
}

function postJson(path, body) {
  return call(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// The image as a complete JPEG blob, or null if it cannot be shown.
async function loadJpeg(key) {
  const response = await call("/image?key=" + encodeURIComponent(key));
  if (!response.ok) {
    return null;
  }
  const type = (response.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase();
  if (type !== "image/jpeg") {
    return null;
  }
  const blob = await readBody(response, "blob");
  const bytes = new Uint8Array(await blob.arrayBuffer());
  return isCompleteJpeg(bytes) ? blob : null;
}

// The image as a blob URL, or null if it cannot be shown (a placeholder then).
async function loadImage(key) {
  const blob = await loadJpeg(key);
  return blob === null ? null : URL.createObjectURL(blob);
}

// The image decoded for a grid, or null if it cannot be drawn.
async function loadBitmap(key) {
  const blob = await loadJpeg(key);
  if (blob === null) {
    return null;
  }
  try {
    return await createImageBitmap(blob, { imageOrientation: "from-image" });
  } catch (error) {
    return null;
  }
}

// One /grids request: the reply body, or null when the server is busy (503).
async function fetchGrids(body) {
  const response = await postJson("/grids", body);
  if (response.status === 503) {
    return null;
  }
  if (!response.ok) {
    throw new HttpError(response.status);
  }
  return readBody(response, "json");
}

// The layout for `body`, retrying while the server is busy; null once build `seq`
// is stale. A request still in flight is waited out first.
async function requestGrids(body, seq) {
  for (;;) {
    while (state.gridsRequest !== null) {
      await state.gridsRequest.catch(() => {});
    }
    if (seq !== state.buildSeq || state.dead) {
      return null;
    }
    const request = fetchGrids(body);
    state.gridsRequest = request;
    let reply;
    try {
      reply = await request;
    } finally {
      state.gridsRequest = null;
    }
    if (seq !== state.buildSeq) {
      return null;
    }
    if (reply !== null) {
      return parseGridPlan(reply, body.keys, body.width, body.height);
    }
    showNotice(SERVER_BUSY);
    await new Promise((resolve) => setTimeout(resolve, GRID_RETRY_MS));
  }
}

// ---- Display ----

function say(text) {
  ui.status.textContent = text;
  ui.status.title = text; // the line is cut short to one line, so the stage keeps its height
}

function currentItem() {
  return state.index >= 0 ? state.items[state.index] : null;
}

function canAct() {
  return state.reviewer !== null && !state.busy && !state.dead && state.pass !== null;
}

function canJudge() {
  return canAct() && currentItem() !== null && state.dwell === "over";
}

function gridMode() {
  return state.mode === "grid";
}

function render() {
  const item = currentItem();
  if (state.pass !== null) {
    const left = countTodo(state.items, state.statuses);
    const remaining = left + " / " + state.items.length + " remaining";
    const batch =
      gridMode() && state.batch !== null
        ? state.batch + " (" + (state.batches.indexOf(state.batch) + 1) + "/" + state.batches.length + ") · "
        : "";
    ui.progress.textContent = "Pass " + state.pass + " · " + batch + remaining;
  }
  ui.mode.textContent = gridMode() ? "Grid (" + state.rotation + ")" : "Single";
  if (item === null) {
    ui.where.textContent = "";
  } else if (item.kind === "grid") {
    ui.where.textContent = "grid (" + item.keys.length + " images)";
  } else {
    ui.where.textContent = state.batchOf.get(item.key) + " · " + item.key;
  }
  const status = item === null ? "" : itemStatus(item, state.statuses) || "";
  ui.itemStatus.textContent = status;
  ui.itemStatus.dataset.status = status;
  ui.itemStatus.hidden = status === "";
  ui.scale.hidden = state.scale === null;
  if (state.scale !== null) {
    // Truncated, so just under 1 never reads 100%; the epsilon keeps float error from dropping a point
    ui.scale.textContent = Math.floor(state.scale * 100 + 1e-9) + "%";
    ui.scale.classList.toggle("low", state.scale < 1);
  }

  ui.reviewer.disabled = state.dead;
  ui.clean.disabled = gridMode() || !canJudge() || !state.loaded;
  ui.dirty.disabled = gridMode() || !canJudge();
  ui.prev.disabled = !canAct() || state.items.length === 0;
  ui.next.disabled = !canAct() || state.items.length === 0;
  ui.undo.disabled = gridMode() || !canAct();
}

function nextPaint() {
  return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}

// Screen pixels per image pixel as drawn (object-fit: contain), or 0 when the
// image would not show a whole pixel. Below 1, small burned-in text can be lost.
function displayScale() {
  const { naturalWidth, naturalHeight } = ui.image;
  if (!naturalWidth || !naturalHeight) {
    return 0;
  }
  const box = ui.image.getBoundingClientRect(); // the element's box; contain fits the image inside it
  const fit = Math.min(box.width / naturalWidth, box.height / naturalHeight);
  if (naturalWidth * fit < 1 || naturalHeight * fit < 1) {
    return 0;
  }
  return fit * window.devicePixelRatio;
}

// Screen pixels per source pixel for the grid shown: its own shrink times the
// canvas's display scale; 0 unless the whole canvas lies inside the stage.
function gridDisplayScale() {
  const box = ui.grid.getBoundingClientRect();
  const stage = ui.stage.getBoundingClientRect();
  const slack = 0.01; // CSS pixels, for float error in width / devicePixelRatio
  const inside =
    box.left >= stage.left - slack &&
    box.top >= stage.top - slack &&
    box.right <= stage.right + slack &&
    box.bottom <= stage.bottom + slack;
  if (!inside || ui.grid.width === 0 || box.width === 0) {
    return 0;
  }
  const shown = box.width * window.devicePixelRatio; // device pixels; layout rounding leaves it a hair off
  const ratio = Math.abs(shown - ui.grid.width) < 0.5 ? 1 : shown / ui.grid.width;
  return state.sourceScale * ratio;
}

function refreshScale() {
  if (!state.loaded) {
    state.scale = null;
  } else if (!ui.grid.hidden) {
    state.scale = gridDisplayScale();
  } else {
    state.scale = ui.image.hidden ? null : displayScale();
  }
}

function itemVisible() {
  return !ui.placeholder.hidden || (state.loaded && state.scale > 0);
}

function clearDwell() {
  state.dwellSeq++; // a pending dwell timer no longer applies
  state.dwell = "none";
}

// Start the current item's dwell once it has been painted. The timer ends it:
// c/d count only after MIN_DWELL_MS with the item on screen.
async function startDwell(seq) {
  await nextPaint();
  if (seq !== state.showSeq || state.dwell !== "none") {
    return;
  }
  refreshScale();
  if (!itemVisible()) {
    render(); // too small a window to show the image: no dwell until a resize shows it
    return;
  }
  state.dwell = "running";
  const dwell = ++state.dwellSeq;
  setTimeout(() => {
    if (dwell === state.dwellSeq) {
      state.dwell = "over";
      render();
    }
  }, MIN_DWELL_MS);
  render();
}

function onResize() {
  if (gridMode() && (state.repackTimer !== null || gridSizeChanged())) {
    scheduleRepack();
    return;
  }
  if (!state.loaded) {
    return;
  }
  if (!ui.grid.hidden) {
    sizeCanvas(); // a zoom can change devicePixelRatio and keep the device size
  }
  refreshScale();
  if (!itemVisible()) {
    clearDwell();
  } else if (state.dwell === "none") {
    startDwell(state.showSeq).catch(reportError);
  }
  render();
}

function setObjectUrl(url) {
  if (url === null) {
    ui.image.removeAttribute("src");
  }
  if (state.objectUrl !== null && state.objectUrl !== url) {
    URL.revokeObjectURL(state.objectUrl);
  }
  state.objectUrl = url;
}

function showNotice(text) {
  ui.message.textContent = text;
  ui.message.hidden = false;
}

function nothingLeft() {
  return "Pass " + state.pass + ": nothing left to review";
}

function showEnd() {
  showNotice(nothingLeft());
}

// Hide everything on the stage, so nothing stays up under a new item.
function clearStage() {
  ui.image.hidden = true;
  ui.grid.hidden = true;
  ui.placeholder.hidden = true;
  ui.message.hidden = true;
}

// The stage's size in device pixels, capped at the largest grid the server packs.
function stageDeviceSize() {
  const box = ui.stage.getBoundingClientRect();
  const ratio = window.devicePixelRatio;
  return {
    width: Math.min(MAX_GRID_SIDE, Math.floor(box.width * ratio)),
    height: Math.min(MAX_GRID_SIDE, Math.floor(box.height * ratio)),
  };
}

function gridSizeChanged() {
  if (state.gridSize === null) {
    return false;
  }
  const now = stageDeviceSize();
  return now.width !== state.gridSize.width || now.height !== state.gridSize.height;
}

// One device pixel per canvas pixel: the canvas's CSS size is its pixel size over
// devicePixelRatio (set through the CSSOM, which the CSP allows).
function sizeCanvas() {
  ui.grid.style.width = ui.grid.width / window.devicePixelRatio + "px";
  ui.grid.style.height = ui.grid.height / window.devicePixelRatio + "px";
}

// Run `work` on each of `values`, at most `limit` at a time.
async function eachLimited(values, limit, work) {
  let next = 0;
  const worker = async () => {
    while (next < values.length) {
      await work(values[next++]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, values.length) }, worker));
}

// Draw `bitmap` (null: it could not be loaded) into placement p and close it;
// false if it was not drawn, wrong-sized or failing.
function drawPlacement(context, p, bitmap) {
  if (bitmap === null) {
    return false;
  }
  try {
    if (bitmap.width !== p.source[0] || bitmap.height !== p.source[1]) {
      return false;
    }
    context.setTransform(...placementTransform(p));
    context.drawImage(bitmap, 0, 0);
    return true;
  } catch (error) {
    return false;
  } finally {
    context.setTransform(1, 0, 0, 1, 0, 0);
    bitmap.close();
  }
}

// The canvas lost its pixels (e.g. a GPU reset): take the grid off screen at once,
// with no dwell, and draw it again once the context is back.
function onContextLost() {
  const item = currentItem();
  if (item === null || item.kind !== "grid") {
    return; // the canvas is not in use
  }
  state.showSeq++; // a draw under way is stale
  clearDwell();
  state.loaded = false;
  state.scale = null;
  ui.grid.hidden = true;
  showNotice("Graphics reset; redrawing... (press [s] for single mode)");
  render();
}

function onContextRestored() {
  const item = currentItem();
  if (item !== null && item.kind === "grid") {
    showItem(state.index).catch(reportError);
  }
}

// Draw grid item `index` on the canvas, then show it. An image that fails to load
// or decode, or whose decoded size is not its `source`, leaves its rect black and
// becomes a single item.
async function showGrid(index, seq) {
  const item = state.items[index];
  const canvas = ui.grid;
  canvas.width = state.gridSize.width; // resizing clears the canvas
  canvas.height = state.gridSize.height;
  const context = canvas.getContext("2d");
  context.fillStyle = "#000";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.imageSmoothingEnabled = true;
  context.imageSmoothingQuality = "high";
  const failed = [];
  let done = 0;
  showNotice("Loading grid 0/" + item.placements.length);
  await eachLimited(item.placements, GRID_IMAGE_FETCHES, async (p) => {
    if (seq !== state.showSeq || state.dead) {
      return;
    }
    const bitmap = await loadBitmap(p.key);
    if (seq !== state.showSeq) {
      if (bitmap !== null) {
        bitmap.close();
      }
      return;
    }
    if (!drawPlacement(context, p, bitmap)) {
      failed.push(p.key);
    }
    showNotice("Loading grid " + ++done + "/" + item.placements.length);
  });
  if (seq !== state.showSeq || state.dead) {
    return; // (a stopped page skips the rest, so not every rect is accounted for)
  }
  if (typeof context.isContextLost === "function" && context.isContextLost()) {
    failed.splice(0, failed.length, ...item.keys); // nothing drawn survives a lost context
  }
  if (failed.length > 0) {
    state.items = demoteKeys(state.items, index, failed);
    if (failed.length === item.keys.length) {
      await showItem(Math.min(index, state.items.length - 1)); // nothing drawn: show what holds its place now
      return;
    }
  }
  showGridDrawn(state.items[index]); // less any failed keys, whose rects stay black
  render();
  await startDwell(seq);
}

function showGridDrawn(item) {
  state.sourceScale = minScale(item.placements);
  sizeCanvas();
  ui.message.hidden = true;
  ui.grid.hidden = false;
  state.loaded = true;
}

// Show item `index` (or the end screen for -1). The dwell starts once the image
// (or placeholder) has been decoded and painted.
async function showItem(index) {
  const seq = ++state.showSeq;
  state.index = index;
  state.loaded = false;
  state.scale = null;
  clearDwell();
  clearStage(); // never leave the previous image up under a new item
  render();
  if (index < 0) {
    setObjectUrl(null);
    showEnd();
    return;
  }
  if (state.items[index].kind === "grid") {
    setObjectUrl(null);
    await showGrid(index, seq);
    return;
  }
  const key = itemKeys(state.items[index])[0];
  let url = await loadImage(key);
  if (seq !== state.showSeq) {
    if (url !== null) {
      URL.revokeObjectURL(url);
    }
    return;
  }
  if (url !== null) {
    ui.image.src = url;
    try {
      await ui.image.decode();
    } catch (error) {
      if (seq === state.showSeq) {
        ui.image.removeAttribute("src");
      }
      URL.revokeObjectURL(url);
      url = null;
    }
    if (seq !== state.showSeq) {
      if (url !== null) {
        URL.revokeObjectURL(url);
      }
      return;
    }
  }
  setObjectUrl(url);
  if (url !== null) {
    state.loaded = true;
    ui.image.alt = key;
    ui.image.hidden = false;
  } else {
    ui.placeholder.textContent = "Cannot load image: " + key;
    ui.placeholder.hidden = false;
  }
  render();
  await startDwell(seq);
}

// ---- Actions ----

function applyStatuses(changed) {
  for (const [key, status] of changed) {
    state.statuses.set(key, status);
  }
}

// The statuses in a 200 reply to /mark or /undo. The server has acted either
// way, so a body that cannot be used is replaced by rereading every status
// (resynced); if that fails too the page stops.
async function replyStatuses(response) {
  try {
    return { changed: parseStatusMap(await readBody(response, "json")), resynced: false };
  } catch (error) {
    if (!(error instanceof BadReply)) {
      throw error;
    }
  }
  try {
    return { changed: parseStatusMap(await getJson("/statuses?pass=" + state.pass)), resynced: true };
  } catch (error) {
    if (error instanceof Stopped) {
      throw error;
    }
    throw stop("unexpected reply from the server; reload the page");
  }
}

async function mark(verdict) {
  if (gridMode()) {
    say(GRID_VERDICTS_LATER);
    return;
  }
  if (!canJudge()) {
    return; // too soon after the item appeared, or busy: ignored, as in the pygame client
  }
  if (verdict === "CLEAN" && !state.loaded) {
    say(UNLOADABLE_CLEAN);
    return;
  }
  const keys = itemKeys(currentItem());
  state.busy = true;
  render();
  try {
    const response = await postJson("/mark", {
      keys,
      status: verdict,
      pass: state.pass,
      reviewer: state.reviewer,
      mode: state.mode,
    });
    if (!response.ok) {
      say(response.status === 400 ? "invalid reviewer name" : "mark failed (HTTP " + response.status + ")");
      return;
    }
    state.marked.push(keys); // recorded on the server, whatever the body holds
    const { changed } = await replyStatuses(response);
    applyStatuses(changed);
    say("Marked " + verdict + ": " + keys[0]);
    state.busy = false;
    showItem(nextTodoIndex(state.items, state.statuses, state.index)).catch(reportError);
  } finally {
    state.busy = false;
    render();
  }
}

// The server keeps one undo history for every client, so with another page or
// client marking on the same server, z can undo their latest mark, not ours.
async function undo() {
  if (gridMode()) {
    say(GRID_VERDICTS_LATER);
    return;
  }
  if (!canAct()) {
    return;
  }
  if (state.marked.length === 0) {
    say(NOTHING_TO_UNDO); // this page has made no mark it could show again
    return;
  }
  state.busy = true;
  render();
  try {
    const response = await postJson("/undo", { pass: state.pass, reviewer: state.reviewer });
    if (!response.ok) {
      say(response.status === 400 ? "invalid reviewer name" : "undo failed (HTTP " + response.status + ")");
      return;
    }
    const expected = state.marked[state.marked.length - 1];
    const { changed, resynced } = await replyStatuses(response);
    if (changed.size === 0) {
      state.marked = []; // the server's history is gone (e.g. it restarted)
      say(NOTHING_TO_UNDO);
      return;
    }
    applyStatuses(changed);
    let index;
    if (resynced || expected.some((key) => changed.has(key))) {
      state.marked.pop();
      const covered = new Set(expected); // show again the item this page marked
      index = state.items.findIndex((item) => itemKeys(item).some((key) => covered.has(key)));
      say("Undone: " + expected[0] + " is " + state.statuses.get(expected[0]));
    } else {
      say("Undid another client's mark: " + describeStatuses(changed));
      index = state.items.findIndex((item) => itemKeys(item).some((key) => changed.has(key)));
    }
    if (index < 0) {
      return; // nothing in this page's list to show again
    }
    state.busy = false;
    showItem(index).catch(reportError); // a fresh dwell before any verdict counts
  } finally {
    state.busy = false;
    render();
  }
}

function navigate(step) {
  if (!canAct() || state.items.length === 0) {
    return;
  }
  let index;
  if (state.index < 0) {
    index = step > 0 ? 0 : state.items.length - 1;
  } else {
    index = state.index + step;
    if (index < 0 || index >= state.items.length) {
      say(step > 0 ? "End of list" : "Start of list");
      return;
    }
  }
  say("");
  showItem(index).catch(reportError);
}

// Drop the grid build (and any pending repack) and the item shown: a stale build,
// load or dwell then does nothing.
function abandonItems() {
  clearTimeout(state.repackTimer);
  state.repackTimer = null;
  state.buildSeq++;
  state.showSeq++;
  clearDwell();
  state.items = [];
  state.index = -1;
  state.loaded = false;
  state.scale = null;
  clearStage();
}

function leaveGrid() {
  state.gridSize = null;
  ui.grid.width = 0; // frees the canvas buffer
  ui.grid.height = 0;
  ui.grid.hidden = true;
}

// Build grid mode's items for state.batch and show the one holding `landKey`
// (else the first). Messages instead when there is nothing to pack, the batch is
// over MAX_GRID_KEYS (split, it would pack unlike pygame) or the window is too small.
async function buildGrids(landKey) {
  abandonItems();
  const seq = state.buildSeq;
  state.landKey = landKey;
  state.gridSize = null;
  render();
  const keys = state.batch === null ? [] : gridKeys(state.manifest, state.statuses, state.batch);
  if (keys.length === 0) {
    const held = heldBackCount(state.manifest, state.statuses, state.batch); // null: every batch
    showNotice(heldBackMessage(state.pass, held) || nothingLeft());
    return;
  }
  if (keys.length > MAX_GRID_KEYS) {
    showNotice(BATCH_TOO_LARGE);
    return;
  }
  const size = stageDeviceSize();
  state.gridSize = size; // a resize from here repacks
  if (size.width < MIN_GRID_SIDE || size.height < MIN_GRID_SIDE) {
    showNotice(WINDOW_TOO_SMALL);
    return;
  }
  const cacheKey = JSON.stringify([state.batch, keys, size.width, size.height, state.rotation]);
  let plan = state.gridCache !== null && state.gridCache.key === cacheKey ? state.gridCache.plan : null;
  if (plan === null) {
    state.gridCache = null; // at most one layout, and none if this one fails
    showNotice(COMPUTING_GRIDS);
    const body = { keys, width: size.width, height: size.height, rotation: state.rotation };
    try {
      plan = await requestGrids(body, seq);
    } catch (error) {
      if (seq !== state.buildSeq) {
        return; // superseded: its error is old news
      }
      if (!(error instanceof Stopped)) {
        showNotice("Cannot compute grids; press [m] to retry or [s] for single mode");
      }
      throw error;
    }
    if (plan === null) {
      return; // superseded
    }
    state.gridCache = { key: cacheKey, plan };
  }
  state.items = orderGridItems(plan); // reshuffled on every build
  const index = state.items.findIndex((item) => itemKeys(item).includes(landKey));
  await showItem(Math.max(index, 0));
}

// A resize away from the size the grids were packed for: hide them at once (no
// dwell runs on a stale layout) and repack once the resizing stops.
function scheduleRepack() {
  const item = currentItem(); // null once a repack is pending or building: keep its key
  if (item !== null) {
    state.landKey = itemKeys(item)[0];
  }
  abandonItems(); // also cancels the pending repack
  showNotice(COMPUTING_GRIDS);
  render();
  state.repackTimer = setTimeout(() => {
    state.repackTimer = null;
    state.marked = []; // as controller._rebuild_grids_for_resize: the old items are gone
    buildGrids(state.landKey).catch(reportError);
  }, REPACK_DELAY_MS);
}

function canSwitch() {
  return !state.busy && !state.dead && state.pass !== null;
}

// As controller._restart_in_mode: reread the statuses, forget this page's marks
// and rebuild the items for `mode`. Grid mode stays on its batch while it has grid
// items (else takes the first that has); with `nextBatchToo` it moves to the next
// one with any, wrapping, and drops the cached layout.
async function restart(mode, rotation, nextBatchToo = false) {
  if (!canSwitch()) {
    return;
  }
  state.busy = true;
  render();
  let statuses;
  try {
    statuses = parseStatusMap(await getJson("/statuses?pass=" + state.pass));
  } finally {
    state.busy = false;
  }
  state.statuses = statuses;
  state.marked = [];
  say(state.reviewer === null ? NAME_NEEDED : "");
  if (mode === "single") {
    abandonItems();
    leaveGrid();
    state.mode = "single";
    state.items = singleItems(state.manifest, statuses);
    await showItem(state.items.length > 0 ? 0 : -1);
    return;
  }
  const withGrids = gridBatches(state.manifest, statuses);
  const accepts = (batch) => withGrids.has(batch);
  if (nextBatchToo) {
    state.gridCache = null;
    state.batch = nextBatch(state.batches, state.batch, accepts);
  } else if (!accepts(state.batch)) {
    state.batch = nextBatch(state.batches, null, accepts);
  }
  state.mode = "grid";
  state.rotation = rotation;
  await buildGrids(null);
}

function reportError(error) {
  if (error instanceof Stopped) {
    return; // the page already says why
  }
  if (error instanceof BadReply) {
    say("unexpected reply from the server");
  } else if (error instanceof HttpError) {
    say("server error (" + error.message + ")");
  } else {
    say("error: " + error);
  }
  render();
}

function run(action) {
  return (...args) => {
    action(...args).catch(reportError);
  };
}

const doMark = run(mark);
const doUndo = run(undo);
const doRestart = run(restart);

// ---- Event wiring ----

function onReviewerInput() {
  const name = ui.reviewer.value;
  if (validReviewer(name)) {
    state.reviewer = name;
    writeStored("reviewer", name);
    if (ui.status.textContent === NAME_NEEDED) {
      say("");
    }
  } else {
    state.reviewer = null;
    say(NAME_NEEDED);
  }
  render();
}

function onReviewerKey(event) {
  if (event.key === "Enter" || event.key === "Escape") {
    ui.reviewer.blur();
  }
}

function onKey(event) {
  if (event.target === ui.reviewer || event.altKey || event.ctrlKey || event.metaKey) {
    return; // typing a name, or a browser shortcut
  }
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key; // Caps Lock
  const arrow = key === "ArrowRight" || key === "ArrowLeft";
  if (arrow) {
    event.preventDefault();
  }
  if (event.repeat) {
    return; // a held key acts once: no queue of verdicts, undos or image fetches
  }
  if (arrow) {
    navigate(key === "ArrowRight" ? 1 : -1);
  } else if (key === "c") {
    doMark("CLEAN");
  } else if (key === "d") {
    doMark("DIRTY");
  } else if (key === "z") {
    doUndo();
  } else if (key === "m") {
    doRestart("grid", event.shiftKey ? "never" : "auto"); // Shift, not the letter's case: Caps Lock
  } else if (key === "s") {
    doRestart("single", state.rotation);
  } else if (key === "b" && gridMode()) {
    doRestart("grid", state.rotation, true);
  }
}

// A new token pasted into this tab changes only the fragment: reload to take it.
function onHashChange() {
  if (TOKEN_HASH.test(location.hash)) {
    location.reload();
  }
}

function wire() {
  ui.reviewer.value = readStored("reviewer") || "";
  if (validReviewer(ui.reviewer.value)) {
    state.reviewer = ui.reviewer.value;
  }
  ui.reviewer.addEventListener("input", onReviewerInput);
  ui.reviewer.addEventListener("keydown", onReviewerKey);
  const buttons = [
    [ui.clean, () => doMark("CLEAN")],
    [ui.dirty, () => doMark("DIRTY")],
    [ui.prev, () => navigate(-1)],
    [ui.next, () => navigate(1)],
    [ui.undo, doUndo],
  ];
  for (const [button, action] of buttons) {
    // Never focused (tabindex -1 too), so Enter or Space cannot click one, held, past the repeat guard
    button.addEventListener("mousedown", (event) => {
      event.preventDefault();
      if (document.activeElement === ui.reviewer) {
        ui.reviewer.blur(); // else later keys would be typed into the name (blur fires no input event)
      }
    });
    button.addEventListener("click", action);
  }
  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", onResize); // also catches browser zoom (a devicePixelRatio change)
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(onResize).observe(ui.stage); // layout changes such as the footer wrapping
  }
  window.addEventListener("hashchange", onHashChange);
  ui.grid.addEventListener("contextlost", onContextLost);
  ui.grid.addEventListener("contextrestored", onContextRestored);
}

async function start() {
  wire();
  if (!state.token) {
    state.dead = true;
    say("No token. Open the URL printed by `image-review serve --socket`.");
    render();
    return;
  }
  state.busy = true;
  render();
  say("loading...");
  try {
    const pass = parsePass(await getJson("/current_pass"));
    const manifest = parseManifest(await getJson("/manifest"));
    const statuses = parseStatusMap(await getJson("/statuses?pass=" + pass));
    state.pass = pass;
    state.statuses = statuses;
    state.manifest = manifest;
    state.batches = sortedBatches(manifest);
    state.batchOf = new Map(manifest.map((row) => [row.key, row.batch]));
    state.items = singleItems(manifest, statuses);
  } catch (error) {
    state.dead = true; // nothing to review without the list
    reportError(error);
    return;
  }
  state.busy = false;
  say(state.reviewer === null ? NAME_NEEDED : "");
  await showItem(state.items.length > 0 ? 0 : -1);
}

start().catch(reportError);
