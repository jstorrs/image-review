"use strict";

// Image review in the browser. The rules are in docs/reference/specification.md,
// "Browser client", and docs/commands/browser.md.

const TOKEN_HASH = /^#([A-Za-z0-9_-]+)$/;
const MIN_DWELL_MS = 200;
const MAX_GRID_KEYS = 1000; // server.MAX_GRID_KEYS
const MIN_GRID_SIDE = 256; // server.MIN_GRID_SIDE
const MAX_GRID_SIDE = 16384; // server.MAX_GRID_SIDE
const INSTANCE_HEADER = "X-Review-Instance"; // server.INSTANCE_HEADER
const GRID_IMAGE_FETCHES = 4; // concurrent /image requests while a grid is drawn
const GRID_RETRY_MS = 2000; // between /grids attempts while the server is busy
const REPACK_DELAY_MS = 300; // resize debounce before grids are repacked
const MARK_FLASH_MS = 200; // a marked item stays up this long, the bar in its new status, before the next

const TOKEN_REJECTED = "token rejected (server restarted?) - open the new URL";
const LOST_CONNECTION = "Lost connection — your marks so far are saved on the server";
// A 412: this page was loaded from an earlier server run (its keys may name another work directory's images).
const SERVER_CHANGED = "Server restarted or changed work directory - press Reconnect (r)";
// With the same address and token, the next server is loaded in this tab by Reconnect.
const NEXT_SERVER = "Stop the server (Ctrl-C) and start the next one; then press Reconnect (r) for serve, or open review's new URL.";
const WAITING = "Done; waiting for the next server";
const WAITING_DETAIL = "Done. Your marks are saved. " + NEXT_SERVER;
// The end of a list with todo left elsewhere in the pass (see "End of a pass or batch" in docs/commands/browser.md)
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

// ---- Token and reviewer name (sessionStorage, guarded: storage may be blocked) ----

/** @param {string} name @returns {string | null} */
function readStored(name) {
  try {
    return sessionStorage.getItem(name);
  } catch (error) {
    return null;
  }
}

/** @param {string} name @param {string | null} value */
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

/**
 * @typedef {Object} State
 * @property {string | null} token
 * @property {string | null} instance
 * @property {number | null} pass
 * @property {ManifestRow[]} manifest
 * @property {string[]} batches
 * @property {Map<string, string>} batchOf
 * @property {StatusMap} statuses
 * @property {Item[]} items
 * @property {number} index
 * @property {string | null} reviewer
 * @property {boolean} busy
 * @property {boolean} dead
 * @property {boolean} lost
 * @property {boolean} waiting
 * @property {boolean} atEnd
 * @property {number} epoch
 * @property {"single" | "grid"} mode
 * @property {"auto" | "never"} rotation
 * @property {string | null} batch
 * @property {{width: number, height: number} | null} gridSize
 * @property {{key: string, plan: GridPlan} | null} gridCache
 * @property {Promise<unknown> | null} gridsRequest
 * @property {number} buildSeq
 * @property {ReturnType<typeof setTimeout> | null} repackTimer
 * @property {string | null} landKey
 * @property {number} sourceScale
 * @property {string[][]} marked
 * @property {Filter} filter
 * @property {Set<string>} rechecked
 * @property {boolean} loaded
 * @property {number | null} scale
 * @property {"none" | "running" | "over"} dwell
 * @property {number} dwellSeq
 * @property {number} showSeq
 * @property {string | null} objectUrl
 * @property {null | "help" | "name"} overlay
 * @property {boolean} flashing
 */

/** @type {State} */
const state = {
  token: takeToken(),
  instance: null, // the server run the review was loaded from (its /current_pass reply's INSTANCE_HEADER)
  pass: null,
  manifest: [], // {key, batch} rows in manifest order
  batches: [], // the manifest's batches, sorted
  batchOf: new Map(), // key -> batch
  statuses: new Map(), // key -> status, for the pass under review
  items: [], // review items in order: the todo list at startup, shuffled
  index: -1, // the current item, or -1 for the end screen (or the stop sign past either end of the list)
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
  gridCache: null, // {key, plan}: the last /grids layout, keyed by batch, keys, grid size and rotation
  gridsRequest: null, // the /grids request in flight, if any: never two at once
  buildSeq: 0, // bumped by every grid build or abandon, so a stale build is dropped
  repackTimer: null, // the pending resize repack, if any
  landKey: null, // the key whose item a grid build lands on
  sourceScale: 1, // the shown grid's own shrink (minScale)
  marked: [], // key arrays this page marked, less those undone: what z may undo
  filter: "todo", // which keys the list holds (f cycles it); never stored, so every load starts at todo
  rechecked: new Set(), // keys marked under the current clean or all filter, less those undone: done there
  loaded: false, // the current item's image is on screen (else a placeholder)
  scale: null, // screen pixels per image pixel, while an image is shown
  dwell: "none", // "none", "running" (painted, under MIN_DWELL_MS) or "over"
  dwellSeq: 0, // bumped by every dwell start or reset, so a stale timer is dropped
  showSeq: 0, // bumped by every showItem, so a stale load is dropped
  objectUrl: null, // the blob URL the <img> shows
  overlay: null, // the overlay over the stage: null, "help" or "name"; while one is up nothing is judged
  flashing: false, // a marked item is up in its new status before the move on: busy, and q waits too
};

/** @param {string} id @returns {HTMLElement} */
function $(id) {
  const element = document.getElementById(id);
  if (element === null) {
    throw new Error("missing #" + id);
  }
  return element;
}
const ui = {
  bar: $("bar"),
  reviewer: /** @type {HTMLInputElement} */ ($("reviewer")),
  nameBox: $("name-box"),
  nameOk: /** @type {HTMLButtonElement} */ ($("name-ok")),
  chip: /** @type {HTMLButtonElement} */ ($("reviewer-chip")),
  reviewerName: $("reviewer-name"), // the chip's name, before its fixed "✎": cut short first when the bar is narrow
  help: $("help"),
  helpButton: /** @type {HTMLButtonElement} */ ($("help-button")),
  progress: $("progress"),
  itemStatus: $("item-status"),
  scale: $("scale"),
  where: $("where"),
  mode: $("mode"),
  stage: $("stage"),
  image: /** @type {HTMLImageElement} */ ($("image")),
  grid: /** @type {HTMLCanvasElement} */ ($("grid")),
  placeholder: $("placeholder"),
  message: $("message"),
  listEnd: $("list-end"), // the stop sign past either end of the list
  listEndDetail: $("list-end-detail"),
  status: $("status"),
  clean: /** @type {HTMLButtonElement} */ ($("clean")),
  dirty: /** @type {HTMLButtonElement} */ ($("dirty")),
  prev: /** @type {HTMLButtonElement} */ ($("prev")),
  next: /** @type {HTMLButtonElement} */ ($("next")),
  undo: /** @type {HTMLButtonElement} */ ($("undo")),
  reconnect: /** @type {HTMLButtonElement} */ ($("reconnect")),
  done: /** @type {HTMLButtonElement} */ ($("done")),
};

// ---- Requests ----

// Thrown once the page has stopped (token rejected, connection lost); callers just unwind.
class Stopped extends Error {}

class HttpError extends Error {
  /** @param {number} status */
  constructor(status) {
    super("HTTP " + status);
    this.status = status;
  }
}

/** @param {string} text @returns {Stopped} */
function stop(text) {
  state.epoch++;
  state.showSeq++; // pending image reads, decodes and grid draws no longer apply
  clearDwell(); // invalidate both a running timer and a dwell waiting for paint
  state.dead = true;
  state.busy = false;
  if (state.atEnd) {
    state.atEnd = false;
    ui.message.hidden = true; // its hint says to press Reconnect, which a stop may not offer
  }
  ui.listEnd.hidden = true; // its arrows no longer move
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
// this server (q), or on the end-of-pass screen while idle (the next server may be up; a
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
// network failure stops the page, as does a 412 (another server now answers, see
// SERVER_CHANGED); once stopped, nothing is sent until a Reconnect, and a request
// sent before then changes nothing.
/** @param {string} path @param {Omit<RequestInit, "headers"> & {headers?: Record<string, string>}} [options] (callers pass plain-object headers) @returns {Promise<Response>} */
async function call(path, options = {}) {
  if (state.dead) {
    throw new Stopped("stopped");
  }
  const epoch = state.epoch;
  /** @type {Record<string, string>} */
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
    state.lost = true; // as a lost connection: Reconnect loads the server now answering
    throw stop(SERVER_CHANGED);
  }
  return response;
}

// Read a body; a failure mid-body is a lost connection.
// any: a parsed JSON reply goes straight to the helpers.js parsers (which take any); "blob" gives a Blob.
/** @param {Response} response @param {"json" | "blob"} how @returns {Promise<any>} */
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

/** @param {string} path @returns {Promise<any>} a reply body for the helpers.js parsers */
async function getJson(path) {
  const response = await call(path);
  if (!response.ok) {
    throw new HttpError(response.status);
  }
  return readBody(response, "json");
}

/** @param {string} path @param {unknown} body @returns {Promise<Response>} */
function postJson(path, body) {
  return call(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// The image as a complete JPEG blob, or null if it cannot be shown.
/** @param {string} key @returns {Promise<Blob | null>} */
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
  const epoch = epochOf.get(response);
  let buffer;
  try {
    buffer = await blob.arrayBuffer();
  } catch (error) {
    if (epoch !== state.epoch) {
      throw stale();
    }
    throw error;
  }
  if (epoch !== state.epoch) {
    throw stale();
  }
  const bytes = new Uint8Array(buffer);
  return isCompleteJpeg(bytes) ? blob : null;
}

// The image as a blob URL, or null if it cannot be shown (a placeholder then).
/** @param {string} key @returns {Promise<string | null>} */
async function loadImage(key) {
  const blob = await loadJpeg(key);
  return blob === null ? null : URL.createObjectURL(blob);
}

// The image decoded for a grid, or null if it cannot be drawn.
/** @param {string} key @returns {Promise<ImageBitmap | null>} */
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
/** @param {{keys: string[], width: number, height: number, [field: string]: unknown}} body @returns {Promise<any>} a reply body for parseGridPlan, or null */
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
/** @param {{keys: string[], width: number, height: number, [field: string]: unknown}} body @param {number} seq @returns {Promise<GridPlan | null>} */
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

// The message takes the centre of the bar in place of the mode, progress and place.
// The reviewer's own moves (arrows, a mark moving on, a mode switch) clear it first;
// a show the reviewer did not start (a repack, a redraw) leaves it.
/** @param {string} text */
function say(text) {
  ui.status.textContent = text;
  ui.status.title = text; // the bar is one line and cuts it short, so the stage keeps its height
}

// The first todo item of the list (isItemTodo), or -1 for none: where a rebuild lands.
// Under todo every item is todo when the list is built, so it is the first.
function firstTodoIndex() {
  return nextTodoIndex(state.items, state.statuses, state.items.length - 1, state.filter, state.rechecked);
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
/** @param {number} epoch */
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
    const left = countTodo(state.items, state.statuses, state.filter, state.rechecked);
    const remaining = left + " / " + state.items.length + " left";
    const batch =
      gridMode() && state.batch !== null
        ? state.batch + " (" + (state.batches.indexOf(state.batch) + 1) + "/" + state.batches.length + ") · "
        : "";
    ui.progress.textContent = "Pass " + state.pass + " · " + batch + remaining;
    ui.progress.title = ui.progress.textContent;
  }
  const recheck = state.filter === "todo" ? "" : " · re-check: " + (state.filter === "clean" ? "CLEAN" : "all");
  ui.mode.textContent = (gridMode() ? "Grid · " + state.rotation : "Single") + recheck;
  ui.mode.title = ui.mode.textContent;
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
  ui.reviewerName.textContent = state.reviewer === null ? "Name" : state.reviewer;
  ui.chip.title = state.reviewer === null ? "Set your name" : "Reviewer: " + state.reviewer + " (click to change)";
  const nameValid = validReviewer(ui.reviewer.value);
  ui.reviewer.classList.toggle("invalid", !nameValid);
  ui.nameOk.disabled = !nameValid;

  const covered = state.overlay !== null;
  ui.chip.disabled = state.dead || covered;
  ui.helpButton.disabled = state.overlay === "name";
  ui.done.disabled = state.waiting || covered || state.flashing;
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
  return !ui.placeholder.hidden || (state.loaded && state.scale !== null && state.scale > 0);
}

function clearDwell() {
  state.dwellSeq++; // a pending dwell timer no longer applies
  state.dwell = "none";
}

// Start the current item's dwell once it has been painted. The timer ends it:
// c/d count only after MIN_DWELL_MS with the item on screen.
/** @param {number} seq */
async function startDwell(seq) {
  await nextPaint();
  if (seq !== state.showSeq || state.dead || state.dwell !== "none" || state.overlay !== null) {
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

// In grid mode a resize repacks, except on the end (or stop) screen: there it
// does not, so the end screen keeps z, and leaving it (z or an arrow) repacks
// first.
function onResize() {
  const endScreen = state.index < 0 && state.items.length > 0 && state.repackTimer === null;
  if (!state.dead && gridMode() && !endScreen && (state.repackTimer !== null || gridSizeChanged())) {
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

/** @param {string | null} url */
function setObjectUrl(url) {
  if (url === null) {
    ui.image.removeAttribute("src");
  }
  if (state.objectUrl !== null && state.objectUrl !== url) {
    URL.revokeObjectURL(state.objectUrl);
  }
  state.objectUrl = url;
}

/** @param {string | null} text null clears the text (a notice with nothing to say) */
function showNotice(text) {
  ui.message.textContent = text;
  ui.message.hidden = false;
}

function passDone() {
  return !anyTodo(state.manifest, state.statuses);
}

// Whether the filter's review is done: under todo, the pass's (see anyTodo).
function filterDone() {
  return !anyTodo(state.manifest, state.statuses, state.filter, state.rechecked);
}

// Where the todo left in the pass, none of it in the list, is reviewed. Grid mode's list
// is one batch, so there b moves on while a batch has todo grid items and
// s reviews what grids leave out; single mode's list is the whole pass, unless others
// marked meanwhile.
/** @returns {string | null} */
function elsewhereHint() {
  if (!gridMode()) {
    return RELOAD_SINGLE;
  }
  if (gridTodoBatches(state.manifest, state.statuses, state.filter, state.rechecked).size > 0) {
    return NEXT_BATCH;
  }
  return heldBackMessage(
    state.pass,
    heldBackCount(state.manifest, state.statuses, null, state.filter, state.rechecked),
  );
}

// The end of the list, with no todo left in it. Only with no todo row left in the pass is
// it the end of the pass, where the next server is due and Reconnect is offered. Under clean
// or all, with every key re-checked, the re-check is done ("All batches done", or "No
// images to review" when the filter lists none, as the viewer said); the pass may be done too.
function showEnd() {
  if (!filterDone()) {
    showNotice(elsewhereHint());
  } else if (state.filter === "todo") {
    state.atEnd = true;
    showNotice("Pass " + state.pass + ": nothing left to review. " + NEXT_SERVER);
  } else {
    state.atEnd = passDone();
    const listed = state.manifest.some((row) => inFilter(state.statuses.get(row.key), state.filter));
    const done = (listed ? "All batches done" : "No images to review") + " for pass " + state.pass;
    const filter = done + " (filter: " + state.filter + ")";
    showNotice(filter + (state.atEnd ? ". " + NEXT_SERVER : " - press [f] to change the filter"));
  }
  render();
}

// An arrow past either end of the list: a stop sign, from which an arrow goes on
// round: -> the first item, <- the last. With nothing left under the filter it is the
// end screen instead (under todo the end of the pass, as Reconnect is due then).
function showListEnd() {
  if (filterDone()) {
    showEnd();
    return;
  }
  const left = countTodo(state.items, state.statuses, state.filter, state.rechecked);
  const where = gridMode() ? " in this batch - [b] next batch" : " in this list";
  ui.listEndDetail.textContent = left > 0 ? left + " todo left" + where : elsewhereHint();
  ui.listEnd.hidden = false;
  render();
}

// Hide everything on the stage, so nothing stays up under a new item.
function clearStage() {
  state.atEnd = false;
  ui.image.hidden = true;
  ui.grid.hidden = true;
  ui.placeholder.hidden = true;
  ui.message.hidden = true;
  ui.listEnd.hidden = true;
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
/** @template T @param {T[]} values @param {number} limit @param {(value: T) => Promise<void>} work */
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
/** @param {CanvasRenderingContext2D} context @param {Placement} p @param {ImageBitmap | null} bitmap @returns {boolean} */
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
/** @param {number} index @param {GridItem} item @param {number} seq */
async function showGrid(index, item, seq) {
  // Grid items exist only after buildGrids has measured gridSize (leaveGrid clears it
  // only once abandonItems has dropped them).
  const size = /** @type {{width: number, height: number}} */ (state.gridSize);
  const canvas = ui.grid;
  canvas.width = size.width; // resizing clears the canvas
  canvas.height = size.height;
  // The canvas only ever gets a 2d context, so getContext("2d") never returns null.
  const context = /** @type {CanvasRenderingContext2D} */ (canvas.getContext("2d"));
  context.fillStyle = "#000";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.imageSmoothingEnabled = true;
  context.imageSmoothingQuality = "high";
  /** @type {string[]} */
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
  // demoteKeys leaves a grid item at `index` when only some keys failed (all failed returned above)
  showGridDrawn(/** @type {GridItem} */ (state.items[index])); // less any failed keys, whose rects stay black
  render();
  await startDwell(seq);
}

/** @param {GridItem} item */
function showGridDrawn(item) {
  state.sourceScale = minScale(item.placements);
  sizeCanvas();
  ui.message.hidden = true;
  ui.grid.hidden = false;
  state.loaded = true;
}

// Show item `index` (or for -1 no item: the end screen, or what `end` shows). The
// dwell starts once the image (or placeholder) has been decoded and painted.
/** @param {number} index @param {() => void} [end] */
async function showItem(index, end = showEnd) {
  const seq = ++state.showSeq;
  state.index = index;
  state.loaded = false;
  state.scale = null;
  clearDwell();
  clearStage(); // never leave the previous image up under a new item
  render();
  if (index < 0) {
    setObjectUrl(null);
    end();
    return;
  }
  const item = state.items[index];
  if (item.kind === "grid") {
    setObjectUrl(null);
    await showGrid(index, item, seq);
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

/** @param {StatusMap} changed */
function applyStatuses(changed) {
  for (const [key, status] of changed) {
    state.statuses.set(key, status);
  }
}

// The statuses in a 200 reply to /mark or /undo. The server has acted either
// way, so a body that cannot be used is replaced by rereading every status
// (resynced); if that fails too the page stops.
/** @param {Response} response @returns {Promise<{changed: StatusMap, resynced: boolean}>} */
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
/** @param {string[]} keys @returns {string} */
function describeKeys(keys) {
  return keys.length === 1 ? keys[0] : "grid of " + keys.length + " images";
}

// One verdict on the current item: every key it covers (a grid's drawn keys),
// sent with the display mode, so a left-out single in grid mode is sent as "grid".
/** @param {"CLEAN" | "DIRTY"} verdict */
async function mark(verdict) {
  const item = currentItem();
  if (item === null || !canJudge()) {
    return; // too soon after the item appeared, or busy: ignored
  }
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
    if (state.filter !== "todo") {
      keys.forEach((key) => state.rechecked.add(key));
    }
    const { changed } = await replyStatuses(response);
    applyStatuses(changed);
    if (!current || build !== state.buildSeq) {
      say("Marked " + verdict + ": " + describeKeys(keys)); // a repack dropped the items meanwhile: no move
      return;
    }
    // The marked item stays up, the bar in its new status, for
    // MARK_FLASH_MS, still busy (nothing else is judged, moved or sent), then the next
    // item (or the end screen). A stop (epoch), a repack (buildSeq) or a context loss or
    // other show (showSeq) meanwhile makes the move on stale, and it is dropped.
    const seq = state.showSeq;
    state.flashing = true;
    render();
    try {
      await new Promise((resolve) => setTimeout(resolve, MARK_FLASH_MS));
    } finally {
      state.flashing = false;
    }
    if (epoch !== state.epoch || build !== state.buildSeq || seq !== state.showSeq) {
      render(); // the flash is over: redraw the controls it disabled (Done)
      return;
    }
    state.busy = false;
    // No message, so the mode and progress stay in view through a run of c and d;
    // and none left from before (a refusal, say) now that the reviewer moves on
    say("");
    showItem(nextTodoIndex(state.items, state.statuses, state.index, state.filter, state.rechecked)).catch(reportError);
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
    for (const key of resynced ? expected : changed.keys()) {
      state.rechecked.delete(key); // todo again under clean or all
    }
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

/** @param {number} step */
function navigate(step) {
  if (!canAct() || state.items.length === 0) {
    return;
  }
  say("");
  if (state.index < 0) {
    showOrRepack(step > 0 ? 0 : state.items.length - 1); // round from the end or stop screen
    return;
  }
  const index = state.index + step;
  if (index < 0 || index >= state.items.length) {
    showItem(-1, showListEnd).catch(reportError);
    return;
  }
  showOrRepack(index);
}

// Show item `index`; in grid mode after a resize on the end screen, repack first
// and land on its grid (the repack also empties the stack of marked keys).
/** @param {number} index */
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
  if (state.repackTimer !== null) {
    clearTimeout(state.repackTimer);
    state.repackTimer = null;
  }
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
// over MAX_GRID_KEYS (split, its chunks would pack differently) or the window is too small.
/** @param {string | null} landKey */
async function buildGrids(landKey) {
  abandonItems();
  const seq = state.buildSeq;
  state.landKey = landKey;
  state.gridSize = null;
  render();
  const keys = state.batch === null ? [] : gridKeys(state.manifest, state.statuses, state.batch, state.filter);
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
  // The grid holding landKey (a repack or Reconnect stays where the reviewer was), else the
  // first todo item, else (all re-checked) the end screen
  const index = state.items.findIndex((item) => landKey !== null && itemKeys(item).includes(landKey));
  await showItem(index >= 0 ? index : firstTodoIndex());
}

// A resize away from the size the grids were packed for: hide them at once (no
// dwell runs on a stale layout) and repack once the resizing stops.
function scheduleRepack() {
  const item = currentItem(); // null once a repack is pending or building: keep its key
  if (item !== null) {
    state.landKey = itemKeys(item)[0];
  }
  abandonItems(); // also cancels the pending repack
  state.marked = []; // the old items are gone, so z could not show what it undoes
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

// A mode or filter switch (m, M, s, b, f): reread the statuses, forget this page's marks
// and rebuild the items for `mode` under `filter` (a new filter starts with nothing
// re-checked). Grid mode stays on its batch while it has grid items (else takes the
// first that has); with `nextBatchToo` it moves to the next one with todo grid items,
// wrapping, and drops the cached layout.
/** @param {"single" | "grid"} mode @param {"auto" | "never"} rotation @param {boolean} [nextBatchToo] @param {Filter} [filter] */
async function restart(mode, rotation, nextBatchToo = false, filter = state.filter) {
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
  if (filter !== state.filter) {
    state.filter = filter;
    state.rechecked = new Set();
  }
  say(""); // (with no name the name box is up, and blocks every switch)
  await rebuild(mode, rotation, nextBatchToo, null);
}

// Rebuild the items for `mode` from state.statuses (see restart); a grid build
// lands on the grid holding `landKey`, else the first.
/** @param {"single" | "grid"} mode @param {"auto" | "never"} rotation @param {boolean} nextBatchToo @param {string | null} landKey */
async function rebuild(mode, rotation, nextBatchToo, landKey) {
  if (mode === "single") {
    abandonItems();
    leaveGrid();
    state.mode = "single";
    state.items = singleItems(state.manifest, state.statuses, state.filter);
    await showItem(firstTodoIndex());
    return;
  }
  const withGrids = gridBatches(state.manifest, state.statuses, state.filter);
  /** @param {string | null} batch */
  const accepts = (batch) => batch !== null && withGrids.has(batch);
  if (nextBatchToo) {
    state.gridCache = null;
    const withTodo = gridTodoBatches(state.manifest, state.statuses, state.filter, state.rechecked);
    state.batch = nextBatch(state.batches, state.batch, (batch) => withTodo.has(batch));
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
  if (instance !== state.instance) {
    state.rechecked = new Set(); // another server: its keys may name other images (kept for the same one)
  }
  state.instance = instance;
  const manifest = parseManifest(await getJson("/manifest"));
  const statuses = parseStatusMap(await getJson("/statuses?pass=" + pass));
  return { pass, manifest, statuses };
}

/** @param {{pass: number, manifest: ManifestRow[], statuses: StatusMap}} review */
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
  if (state.waiting || state.overlay !== null || state.flashing) {
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
  state.rechecked = new Set(); // as the batch: the next server's keys may match these (the filter is kept, as the mode)
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

/** @param {unknown} error */
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

/** @template {unknown[]} A @param {(...args: A) => Promise<void>} action @returns {(...args: A) => void} */
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
/** @param {"help" | "name"} which */
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

/** @param {KeyboardEvent} event */
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

/** @param {KeyboardEvent} event */
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
  } else if (key === "f") {
    doRestart(state.mode, state.rotation, false, nextFilter(state.filter));
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
  /** @type {[HTMLButtonElement, () => void][]} */
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
    say("No token. Open the URL that image-review printed.");
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
