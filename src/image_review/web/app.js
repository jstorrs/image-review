"use strict";

// Image review in the browser (experimental). The pygame client is the reference:
// see SPEC.md, "Browser client (experimental)".

const TOKEN_HASH = /^#([A-Za-z0-9_-]+)$/;
const TODO_STATUSES = new Set(["UNREVIEWED", "FLAGGED"]);
const STATUSES = new Set(["CLEAN", "DIRTY", "UNREVIEWED", "FLAGGED"]);
const MIN_DWELL_MS = 200;
const MAX_REVIEWER_LENGTH = 64;
// A grid verdict covers every image in it, so grids hold only images not yet judged DIRTY or
// FLAGGED (status.GRID_ELIGIBLE); with the todo filter that leaves UNREVIEWED.
const GRID_ELIGIBLE = new Set(["UNREVIEWED", "CLEAN"]);
const MAX_GRID_KEYS = 1000; // server.MAX_GRID_KEYS
const MIN_GRID_SIDE = 256; // server.MIN_GRID_SIDE
const MAX_GRID_SIDE = 16384; // server.MAX_GRID_SIDE
const INSTANCE_HEADER = "X-Review-Instance"; // server.INSTANCE_HEADER
const GRID_IMAGE_FETCHES = 4; // concurrent /image requests while a grid is drawn
const GRID_RETRY_MS = 2000; // between /grids attempts while the server is busy
const REPACK_DELAY_MS = 300; // resize debounce before grids are repacked

const TOKEN_REJECTED = "token rejected (server restarted?) - open the new URL";
const LOST_CONNECTION = "Lost connection — your marks so far are saved on the server";
// A 412: this page was loaded from an earlier serve (its keys may name another work directory's images).
const SERVER_CHANGED =
  "The server was restarted or now serves another work directory. Press Reconnect (r) to load it.";
// With the same socket path and token, the next serve is loaded in this tab by Reconnect.
const NEXT_SERVE = "Stop serve (Ctrl-C), start the next one, then press Reconnect (r).";
const WAITING = "Done; waiting for the next serve";
const WAITING_DETAIL = "Done. Your marks are saved. " + NEXT_SERVE;
// The end of a list with todo left elsewhere in the pass (as controller._end_message, NO_TODO_MESSAGE)
const NEXT_BATCH = "No todo images remaining - [b] next batch";
const RELOAD_SINGLE = "No todo images remaining - press [s] to reload the list";
const NOTHING_TO_UNDO = "Nothing to undo";
const RECONNECTING = "Reconnecting...";
const UNLOADABLE_CLEAN = "cannot mark CLEAN: image could not be loaded";
const NAME_NEEDED = "Enter your name (1-" + MAX_REVIEWER_LENGTH + " characters) to start reviewing";
const GRID_HAS_DIRTY = "grid contains an image already marked DIRTY - review it in single mode";
// The server's refusal for a left-out single in grid mode (sent as a grid mark) that became FLAGGED.
const IMAGE_HAS_DIRTY = "image already marked DIRTY in another pass - review it in single mode";
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

// As status.grid_status: DIRTY if any image is not grid-eligible, else
// UNREVIEWED while any is todo, else CLEAN.
function gridStatus(keys, statuses) {
  const found = new Set(keys.map((key) => statuses.get(key)));
  if (![...found].every((status) => GRID_ELIGIBLE.has(status))) {
    return "DIRTY";
  }
  return [...found].some(isTodo) ? "UNREVIEWED" : "CLEAN";
}

// As status.grid_clean_refused: CLEAN on a grid holding a DIRTY or FLAGGED
// image is refused, unless the whole grid is DIRTY. The server refuses it too (409).
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
  instance: null, // the serve run the review was loaded from (its /current_pass reply's INSTANCE_HEADER)
  pass: null,
  manifest: [], // {key, batch} rows in manifest order
  batches: [], // the manifest's batches, sorted
  batchOf: new Map(), // key -> batch
  statuses: new Map(), // key -> status, for the pass under review
  items: [], // review items in order: the todo list at startup, shuffled
  index: -1, // the current item, or -1 for the end screen
  reviewer: null, // a name that passed validReviewer, or null
  busy: false, // a request is in flight, or the list is loading
  dead: false, // token rejected, connection lost or done (q): nothing more is sent
  lost: false, // stopped by a lost connection: Reconnect is offered
  waiting: false, // done with this server (q): stopped and freed, Reconnect loads the next one
  atEnd: false, // the end-of-pass screen is up (no todo left in the pass): Reconnect is offered
  epoch: 0, // bumped by every stop: a request sent before it changes nothing
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
  overlay: null, // the overlay over the stage: null, "help" or "name"; while one is up nothing is judged
};

const $ = (id) => document.getElementById(id);
const ui = {
  bar: $("bar"),
  reviewer: $("reviewer"),
  nameBox: $("name-box"),
  nameOk: $("name-ok"),
  chip: $("reviewer-chip"),
  help: $("help"),
  helpButton: $("help-button"),
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
  reconnect: $("reconnect"),
  done: $("done"),
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
  state.epoch++;
  state.dead = true;
  state.busy = false;
  if (state.atEnd) {
    state.atEnd = false;
    ui.message.hidden = true; // its hint says to press Reconnect, which a stop may not offer
  }
  say(text);
  render();
  return new Stopped(text);
}

// A network failure: stop, offering Reconnect (never retried automatically).
function lose() {
  state.lost = true;
  return stop(LOST_CONNECTION);
}

// Whether a Reconnect may be tried, with a token: after a lost connection, once done with
// this server (q), or on the end-of-pass screen while idle (the next serve may be up; a
// reply still due would be dropped, though the server has acted on it).
function canReconnect() {
  const idleEnd = state.atEnd && !state.busy && !state.dead;
  return state.token !== null && (state.lost || state.waiting || idleEnd);
}

const epochOf = new WeakMap(); // response -> the epoch its request was sent in

// Thrown for a request sent before the page stopped: its reply or failure is old news.
function stale() {
  return new Stopped("stale");
}

// fetch with the token and the instance the review was loaded from. A 401 or a
// network failure stops the page, as does a 412 (another serve now answers, see
// SERVER_CHANGED); once stopped, nothing is sent until a Reconnect, and a request
// sent before then changes nothing.
async function call(path, options = {}) {
  if (state.dead) {
    throw new Stopped("stopped");
  }
  const epoch = state.epoch;
  const headers = { ...options.headers, Authorization: "Bearer " + state.token };
  if (state.instance !== null) {
    headers[INSTANCE_HEADER] = state.instance; // /current_pass needs none: a Reconnect starts there
  }
  let response;
  try {
    response = await fetch(path, { ...options, headers, cache: "no-store" });
  } catch (error) {
    throw epoch === state.epoch ? lose() : stale(); // the first stop's message stays
  }
  if (epoch !== state.epoch) {
    throw stale();
  }
  epochOf.set(response, epoch);
  if (response.status === 401) {
    state.token = null;
    writeStored("token", null);
    throw stop(TOKEN_REJECTED);
  }
  if (response.status === 412) {
    state.lost = true; // as a lost connection: Reconnect loads the serve now answering
    throw stop(SERVER_CHANGED);
  }
  return response;
}

// Read a body; a failure mid-body is a lost connection.
async function readBody(response, how) {
  const epoch = epochOf.get(response);
  let body;
  try {
    body = await response[how]();
  } catch (error) {
    if (epoch !== state.epoch) {
      throw stale();
    }
    if (error instanceof SyntaxError) {
      throw new BadReply("JSON");
    }
    throw lose();
  }
  if (epoch !== state.epoch) {
    throw stale();
  }
  return body;
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
  ui.status.title = text; // the bar is one line and cuts it short, so the stage keeps its height
}

function currentItem() {
  return state.index >= 0 ? state.items[state.index] : null;
}

// No action while an overlay covers the stage: nothing is judged on an item the reviewer cannot see.
function canAct() {
  return state.reviewer !== null && !state.busy && !state.dead && state.pass !== null && state.overlay === null;
}

// The request(s) begun in epoch `epoch` are over: clear busy, unless the page
// stopped meanwhile (busy is then a Reconnect's, or already clear).
function endBusy(epoch) {
  if (epoch === state.epoch) {
    state.busy = false;
    render();
  }
}

// A grid counts only while its canvas is up with every key it covers drawn.
function canJudge() {
  const item = currentItem();
  if (!canAct() || item === null || state.dwell !== "over") {
    return false;
  }
  return item.kind !== "grid" || (state.loaded && !ui.grid.hidden);
}

function gridMode() {
  return state.mode === "grid";
}

function render() {
  const item = currentItem();
  if (state.pass !== null) {
    const left = countTodo(state.items, state.statuses);
    const remaining = left + " / " + state.items.length + " left";
    const batch =
      gridMode() && state.batch !== null
        ? state.batch + " (" + (state.batches.indexOf(state.batch) + 1) + "/" + state.batches.length + ") · "
        : "";
    ui.progress.textContent = "Pass " + state.pass + " · " + batch + remaining;
    ui.progress.title = ui.progress.textContent;
  }
  ui.mode.textContent = gridMode() ? "Grid · " + state.rotation : "Single";
  if (item === null) {
    ui.where.textContent = "";
  } else if (item.kind === "grid") {
    ui.where.textContent = "grid (" + item.keys.length + " images)";
  } else {
    ui.where.textContent = state.batchOf.get(item.key) + " · " + item.key;
  }
  ui.where.title = ui.where.textContent;
  // The bar takes the item's status colour; neutral on the end screen and once stopped
  const status = item === null || state.dead ? "" : itemStatus(item, state.statuses) || "";
  ui.bar.dataset.status = status;
  ui.itemStatus.textContent = status;
  ui.itemStatus.hidden = status === "";
  ui.scale.hidden = state.scale === null;
  if (state.scale !== null) {
    // Truncated, so just under 1 never reads 100%; the epsilon keeps float error from dropping a point
    const percent = Math.floor(state.scale * 100 + 1e-9) + "%";
    ui.scale.textContent = state.scale < 1 ? "⚠ " + percent : percent;
    ui.scale.classList.toggle("low", state.scale < 1);
  }
  ui.chip.textContent = (state.reviewer === null ? "Name" : state.reviewer) + " ✎";
  ui.chip.title = state.reviewer === null ? "Set your name" : "Reviewer: " + state.reviewer + " (click to change)";
  const nameValid = validReviewer(ui.reviewer.value);
  ui.reviewer.classList.toggle("invalid", !nameValid);
  ui.nameOk.disabled = !nameValid;

  const covered = state.overlay !== null;
  ui.chip.disabled = state.dead || covered;
  ui.helpButton.disabled = state.overlay === "name";
  ui.done.disabled = state.waiting || covered;
  ui.clean.disabled = !canJudge() || !state.loaded;
  ui.dirty.disabled = !canJudge();
  ui.prev.disabled = !canAct() || state.items.length === 0;
  ui.next.disabled = !canAct() || state.items.length === 0;
  ui.undo.disabled = !canAct();
  ui.reconnect.hidden = !canReconnect();
  ui.reconnect.disabled = covered;
  ui.done.hidden = !ui.reconnect.hidden; // they share a place in the bar: Reconnect takes it while offered
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
  if (seq !== state.showSeq || state.dwell !== "none" || state.overlay !== null) {
    return; // (under an overlay: closing it starts the dwell)
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

// In grid mode a resize repacks, except on the end screen: as the controller,
// which repacks only while reviewing, the end screen keeps z, and leaving it
// (z or an arrow) repacks first.
function onResize() {
  const endScreen = state.index < 0 && state.items.length > 0 && state.repackTimer === null;
  if (gridMode() && !endScreen && (state.repackTimer !== null || gridSizeChanged())) {
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

// The end of the list. Only with no todo row left in the pass is it the end of the pass,
// where the next serve is due and Reconnect is offered. Grid mode's list is one batch, so
// there, as the controller, b moves on while a batch has grid items and s reviews what
// grids leave out; single mode's list is the whole pass, unless others marked meanwhile.
function showEnd() {
  const todo = state.manifest.filter((row) => isTodo(state.statuses.get(row.key)));
  if (todo.length === 0) {
    state.atEnd = true;
    showNotice("Pass " + state.pass + ": nothing left to review. " + NEXT_SERVE);
  } else if (!gridMode()) {
    showNotice(RELOAD_SINGLE);
  } else if (gridBatches(state.manifest, state.statuses).size > 0) {
    showNotice(NEXT_BATCH);
  } else {
    showNotice(heldBackMessage(state.pass, heldBackCount(state.manifest, state.statuses, null)));
  }
  render();
}

// Hide everything on the stage, so nothing stays up under a new item.
function clearStage() {
  state.atEnd = false;
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

// "k1" for a single item, "grid of N images" for a grid.
function describeKeys(keys) {
  return keys.length === 1 ? keys[0] : "grid of " + keys.length + " images";
}

// One verdict on the current item: every key it covers (a grid's drawn keys),
// sent with the display mode, so a left-out single in grid mode is sent as "grid".
async function mark(verdict) {
  if (!canJudge()) {
    return; // too soon after the item appeared, or busy: ignored, as in the pygame client
  }
  const item = currentItem();
  if (verdict === "CLEAN" && item.kind === "grid" && gridCleanRefused(item.keys, state.statuses)) {
    say(GRID_HAS_DIRTY);
    return;
  }
  if (verdict === "CLEAN" && !state.loaded) {
    say(UNLOADABLE_CLEAN);
    return;
  }
  const keys = itemKeys(item);
  const build = state.buildSeq; // a resize repack meanwhile drops the items, and with them z
  const epoch = state.epoch;
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
    if (response.status === 409) {
      // The server refused a grid-mode CLEAN this page allowed (its statuses were stale, e.g. another
      // client marked a key DIRTY): nothing was recorded, so no undo entry; reread every status.
      say(item.kind === "grid" ? GRID_HAS_DIRTY : IMAGE_HAS_DIRTY);
      applyStatuses(parseStatusMap(await getJson("/statuses?pass=" + state.pass)));
      return;
    }
    if (!response.ok) {
      say(response.status === 400 ? "invalid reviewer name" : "mark failed (HTTP " + response.status + ")");
      return;
    }
    const current = build === state.buildSeq;
    if (current) {
      state.marked.push(keys); // recorded on the server, whatever the body holds
    }
    const { changed } = await replyStatuses(response);
    applyStatuses(changed);
    say("Marked " + verdict + ": " + describeKeys(keys));
    state.busy = false;
    if (current && build === state.buildSeq) {
      showItem(nextTodoIndex(state.items, state.statuses, state.index)).catch(reportError);
    }
  } finally {
    endBusy(epoch);
  }
}

// The server keeps one undo history for every client, so with another page or
// client marking on the same server, z can undo their latest mark, not ours.
async function undo() {
  if (!canAct()) {
    return;
  }
  if (state.marked.length === 0) {
    say(NOTHING_TO_UNDO); // this page has made no mark it could show again
    return;
  }
  const expected = state.marked[state.marked.length - 1]; // a repack meanwhile empties the stack
  const epoch = state.epoch;
  state.busy = true;
  render();
  try {
    const response = await postJson("/undo", { pass: state.pass, reviewer: state.reviewer });
    if (!response.ok) {
      say(response.status === 400 ? "invalid reviewer name" : "undo failed (HTTP " + response.status + ")");
      return;
    }
    const { changed, resynced } = await replyStatuses(response);
    if (changed.size === 0) {
      state.marked = []; // the server's history is gone (e.g. it restarted)
      say(NOTHING_TO_UNDO);
      return;
    }
    applyStatuses(changed);
    let index;
    const own = resynced || expected.some((key) => changed.has(key));
    if (own) {
      state.marked.pop();
      const covered = new Set(expected); // show again the item this page marked
      index = state.items.findIndex((item) => itemKeys(item).some((key) => covered.has(key)));
      const now = expected.length === 1 ? " is " + state.statuses.get(expected[0]) : "";
      say("Undone: " + describeKeys(expected) + now);
    } else {
      say("Undid another client's mark: " + describeStatuses(changed));
      index = state.items.findIndex((item) => itemKeys(item).some((key) => changed.has(key)));
    }
    if (index < 0) {
      if (own && state.repackTimer !== null) {
        state.landKey = expected[0]; // the pending repack lands on the undone keys
      }
      return; // nothing in this page's list to show again (e.g. a repack is pending)
    }
    state.busy = false;
    showOrRepack(index); // a fresh dwell before any verdict counts
  } finally {
    endBusy(epoch);
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
  showOrRepack(index);
}

// Show item `index`; in grid mode after a resize on the end screen, repack first
// and land on its grid (the repack also empties the stack of marked keys).
function showOrRepack(index) {
  if (gridMode() && gridSizeChanged()) {
    state.landKey = itemKeys(state.items[index])[0];
    scheduleRepack();
    return;
  }
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
    showEnd(); // the pass's end, or what is left elsewhere in it
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
  state.marked = []; // as controller._rebuild_grids_for_resize: the old items are gone
  showNotice(COMPUTING_GRIDS);
  render();
  const repack = () => {
    if (state.dead) {
      state.repackTimer = null; // a Reconnect rebuilds, landing on state.landKey
      return;
    }
    if (state.busy) {
      state.repackTimer = setTimeout(repack, REPACK_DELAY_MS); // pack with the statuses a reply brings
      return;
    }
    state.repackTimer = null;
    buildGrids(state.landKey).catch(reportError);
  };
  state.repackTimer = setTimeout(repack, REPACK_DELAY_MS);
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
  const epoch = state.epoch;
  state.busy = true;
  render();
  let statuses;
  try {
    statuses = parseStatusMap(await getJson("/statuses?pass=" + state.pass));
  } finally {
    endBusy(epoch);
  }
  state.statuses = statuses;
  state.marked = [];
  say(""); // (with no name the name box is up, and blocks every switch)
  await rebuild(mode, rotation, nextBatchToo, null);
}

// Rebuild the items for `mode` from state.statuses (see restart); a grid build
// lands on the grid holding `landKey`, else the first.
async function rebuild(mode, rotation, nextBatchToo, landKey) {
  if (mode === "single") {
    abandonItems();
    leaveGrid();
    state.mode = "single";
    state.items = singleItems(state.manifest, state.statuses);
    await showItem(state.items.length > 0 ? 0 : -1);
    return;
  }
  const withGrids = gridBatches(state.manifest, state.statuses);
  const accepts = (batch) => withGrids.has(batch);
  if (nextBatchToo) {
    state.gridCache = null;
    state.batch = nextBatch(state.batches, state.batch, accepts);
  } else if (!accepts(state.batch)) {
    state.batch = nextBatch(state.batches, null, accepts);
  }
  state.mode = "grid";
  state.rotation = rotation;
  await buildGrids(landKey);
}

// What start() loads: the current pass, the manifest and the pass's statuses. The
// instance named by the /current_pass reply is sent on every request after it.
async function loadReview() {
  const response = await call("/current_pass");
  if (!response.ok) {
    throw new HttpError(response.status);
  }
  const instance = response.headers.get(INSTANCE_HEADER);
  const pass = parsePass(await readBody(response, "json"));
  if (!instance) {
    throw new BadReply("instance");
  }
  state.instance = instance;
  const manifest = parseManifest(await getJson("/manifest"));
  const statuses = parseStatusMap(await getJson("/statuses?pass=" + pass));
  return { pass, manifest, statuses };
}

function takeReview({ pass, manifest, statuses }) {
  state.pass = pass;
  state.statuses = statuses;
  state.manifest = manifest;
  state.batches = sortedBatches(manifest);
  state.batchOf = new Map(manifest.map((row) => [row.key, row.batch]));
}

// After a lost connection, after q, or on the end-of-pass screen, on the reviewer's
// request only: reload what start() loads and rebuild the current mode's items with a
// fresh dwell. The server may have restarted (even on another pass or work directory)
// or others marked meanwhile, so this page's marks are forgotten (z cannot undo what
// the page cannot vouch for) and the grids are laid out afresh. Busy from the click
// until the reload is in, so no key or button sends a request meanwhile; anything sent
// before is stale (see call).
async function reconnect() {
  if (!canReconnect() || state.overlay !== null) {
    return; // not offered, or a Reconnect is already in flight
  }
  const item = currentItem(); // null while a repack is pending, or after a failed Reconnect or q
  if (item !== null) {
    state.landKey = itemKeys(item)[0]; // kept for a retry should this one fail
  }
  state.epoch++; // on the end screen, as a stop does: an image or layout fetch still out changes nothing
  state.lost = false;
  state.waiting = false;
  state.dead = false;
  state.busy = true;
  abandonItems(); // nothing from before the loss stays on screen, nor its dwell
  state.marked = [];
  say(RECONNECTING);
  render();
  let review;
  try {
    review = await loadReview();
  } catch (error) {
    if (!(error instanceof Stopped)) {
      state.lost = true; // a server error or bad reply: the reviewer may try again
      stop("");
      reportError(error);
    }
    return; // (a lost connection or rejected token has stopped the page already)
  }
  state.busy = false;
  const before = state.pass;
  takeReview(review);
  state.gridCache = null;
  const moved = state.pass !== before; // after q the old pass is forgotten: always say which
  say(moved ? "Reconnected; now on pass " + state.pass : "Reconnected"); // (a name is set: the name box blocks r)
  await rebuild(state.mode, state.rotation, false, state.landKey);
}

// Done with this server (q): the reviewer stops it (marks are saved as made) and starts
// the next on the same socket path and token. Stops the page as a lost connection does,
// so every request in flight turns stale and nothing more is sent, and frees the images
// and the review, but keeps the token and the reviewer name for Reconnect.
function waitForNextServer() {
  if (state.waiting || state.overlay !== null) {
    return;
  }
  abandonItems(); // also clears the stage
  leaveGrid();
  setObjectUrl(null); // revokes the blob URL and clears the <img>
  ui.image.alt = "";
  state.pass = null;
  state.manifest = [];
  state.batches = [];
  state.batchOf = new Map();
  state.statuses = new Map();
  state.marked = [];
  state.gridCache = null;
  state.batch = null; // the next server starts at its first batch and item: its keys may match these
  state.landKey = null;
  ui.progress.textContent = "";
  ui.progress.title = "";
  if (state.token === null) {
    render(); // token rejected or none: already stopped, and the page says to open the new URL
    return;
  }
  state.lost = false;
  state.waiting = true;
  stop(WAITING);
  showNotice(WAITING_DETAIL);
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
const doReconnect = run(reconnect);

// ---- Overlays: the name box and the help, over the stage ----

// Open overlay `which` ("help" or "name"): the dwell is cleared, so nothing is
// judged on an item it covered, and every action waits for it to close.
function openOverlay(which) {
  state.overlay = which;
  ui.help.hidden = which !== "help";
  ui.nameBox.hidden = which !== "name";
  clearDwell();
  render();
}

// Close the overlay and start a fresh dwell for the item under it (none ran while it was up).
function closeOverlay() {
  state.overlay = null;
  ui.help.hidden = true;
  ui.nameBox.hidden = true;
  if (document.activeElement === ui.reviewer) {
    ui.reviewer.blur(); // later keys act on the page
  }
  render();
  if (currentItem() !== null) {
    startDwell(state.showSeq).catch(reportError);
  }
}

function toggleHelp() {
  if (state.overlay === "help") {
    closeOverlay();
  } else if (state.overlay === null) {
    openOverlay("help");
  }
}

function openNameBox() {
  if (state.overlay !== null) {
    return;
  }
  if (state.reviewer !== null) {
    ui.reviewer.value = state.reviewer;
  }
  openOverlay("name");
  ui.reviewer.focus();
}

// The name typed is taken only when valid (else the box stays up).
function acceptName() {
  const name = ui.reviewer.value;
  if (state.overlay !== "name" || !validReviewer(name)) {
    return;
  }
  state.reviewer = name;
  writeStored("reviewer", name);
  if (ui.status.textContent === NAME_NEEDED) {
    say("");
  }
  closeOverlay();
}

// Escape keeps the name there was; with none yet, the box stays up.
function cancelName() {
  if (state.overlay !== "name" || state.reviewer === null) {
    return;
  }
  ui.reviewer.value = state.reviewer;
  closeOverlay();
}

// ---- Event wiring ----

function onReviewerKey(event) {
  if (event.isComposing) {
    return; // an input method's Enter or Escape
  }
  if (event.key === "Enter") {
    acceptName();
  } else if (event.key === "Escape") {
    cancelName();
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
  if (state.overlay === "name") {
    // The name box is up and the review waits for it. Focus went elsewhere (a click):
    // Enter still accepts, and any other key goes back to the field (it lands there).
    if (key === "Escape") {
      cancelName();
    } else if (key === "Enter") {
      acceptName();
    } else {
      ui.reviewer.focus();
    }
    return;
  }
  if (key === "?" || key === "h") {
    toggleHelp();
    return;
  }
  if (state.overlay === "help") {
    if (key === "Escape") {
      closeOverlay();
    }
    return;
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
  } else if (key === "r" && canReconnect()) {
    doReconnect(); // only while the Reconnect button is shown
  } else if (key === "q") {
    waitForNextServer(); // in any state
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
  ui.reviewer.addEventListener("input", render); // marks the name invalid, and OK with it
  ui.reviewer.addEventListener("keydown", onReviewerKey);
  ui.nameOk.addEventListener("mousedown", (event) => event.preventDefault()); // focus stays in the name
  ui.nameOk.addEventListener("click", acceptName);
  const buttons = [
    [ui.clean, () => doMark("CLEAN")],
    [ui.dirty, () => doMark("DIRTY")],
    [ui.prev, () => navigate(-1)],
    [ui.next, () => navigate(1)],
    [ui.undo, doUndo],
    [ui.reconnect, doReconnect],
    [ui.done, waitForNextServer],
    [ui.chip, openNameBox],
    [ui.helpButton, toggleHelp],
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
    new ResizeObserver(onResize).observe(ui.stage); // any layout change (the bar itself never resizes it)
  }
  window.addEventListener("hashchange", onHashChange);
  ui.grid.addEventListener("contextlost", onContextLost);
  ui.grid.addEventListener("contextrestored", onContextRestored);
}

async function start() {
  wire();
  if (state.reviewer === null) {
    openNameBox(); // every review control waits for a name
  }
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
    takeReview(await loadReview());
    state.items = singleItems(state.manifest, state.statuses);
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
