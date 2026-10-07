"use strict";

// Single-image review in the browser (experimental). The pygame client's single
// mode is the reference: see SPEC.md, "Browser client (experimental)".

const TOKEN_HASH = /^#([A-Za-z0-9_-]+)$/;
const TODO_STATUSES = new Set(["UNREVIEWED", "FLAGGED"]);
const STATUSES = new Set(["CLEAN", "DIRTY", "UNREVIEWED", "FLAGGED"]);
const MIN_DWELL_MS = 200;
const MAX_REVIEWER_LENGTH = 64;

const TOKEN_REJECTED = "token rejected (server restarted?)";
const LOST_CONNECTION = "Lost connection — your marks so far are saved on the server";
const NOTHING_TO_UNDO = "Nothing to undo";
const UNLOADABLE_CLEAN = "cannot mark CLEAN: image could not be loaded";
const NAME_NEEDED = "Enter your name (1-" + MAX_REVIEWER_LENGTH + " characters) to start reviewing";

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

// The index of the next item whose status is todo, searching forward from `from`
// and wrapping round (including `from` itself last); -1 if none.
function nextTodoIndex(items, statuses, from) {
  for (let step = 1; step <= items.length; step++) {
    const i = (from + step) % items.length;
    if (isTodo(statuses.get(items[i]))) {
      return i;
    }
  }
  return -1;
}

function countTodo(items, statuses) {
  return items.filter((key) => isTodo(statuses.get(key))).length;
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
  batchOf: new Map(), // key -> batch
  statuses: new Map(), // key -> status, for the pass under review
  items: [], // keys in review order: the todo list at startup, shuffled
  index: -1, // the current item, or -1 for the end screen
  reviewer: null, // a name that passed validReviewer, or null
  busy: false, // a request is in flight, or the list is loading
  dead: false, // token rejected or connection lost: nothing more is sent
  marked: [], // keys this page marked, less those undone: what z may undo
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
  image: $("image"),
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

// The image as a blob URL, or null if it cannot be shown (a placeholder then).
async function loadImage(key) {
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
  return isCompleteJpeg(bytes) ? URL.createObjectURL(blob) : null;
}

// ---- Display ----

function say(text) {
  ui.status.textContent = text;
}

function currentKey() {
  return state.index >= 0 ? state.items[state.index] : null;
}

function canAct() {
  return state.reviewer !== null && !state.busy && !state.dead && state.pass !== null;
}

function canJudge() {
  return canAct() && currentKey() !== null && state.dwell === "over";
}

function render() {
  const key = currentKey();
  if (state.pass !== null) {
    const left = countTodo(state.items, state.statuses);
    ui.progress.textContent = "Pass " + state.pass + " · " + left + " / " + state.items.length + " remaining";
  }
  ui.where.textContent = key === null ? "" : state.batchOf.get(key) + " · " + key;
  const status = key === null ? "" : state.statuses.get(key) || "";
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
  ui.clean.disabled = !canJudge() || !state.loaded;
  ui.dirty.disabled = !canJudge();
  ui.prev.disabled = !canAct() || state.items.length === 0;
  ui.next.disabled = !canAct() || state.items.length === 0;
  ui.undo.disabled = !canAct();
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

function refreshScale() {
  state.scale = state.loaded && !ui.image.hidden ? displayScale() : null;
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
  if (!state.loaded || ui.image.hidden) {
    return;
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

function showEnd() {
  ui.message.textContent = "Pass " + state.pass + ": nothing left to review";
  ui.message.hidden = false;
}

// Show item `index` (or the end screen for -1). The dwell starts once the image
// (or placeholder) has been decoded and painted.
async function showItem(index) {
  const seq = ++state.showSeq;
  state.index = index;
  state.loaded = false;
  state.scale = null;
  clearDwell();
  ui.image.hidden = true; // never leave the previous image up under a new item
  ui.placeholder.hidden = true;
  ui.message.hidden = true;
  render();
  if (index < 0) {
    setObjectUrl(null);
    showEnd();
    return;
  }
  const key = state.items[index];
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
  if (!canJudge()) {
    return; // too soon after the item appeared, or busy: ignored, as in the pygame client
  }
  if (verdict === "CLEAN" && !state.loaded) {
    say(UNLOADABLE_CLEAN);
    return;
  }
  const key = currentKey();
  state.busy = true;
  render();
  try {
    const response = await postJson("/mark", {
      keys: [key],
      status: verdict,
      pass: state.pass,
      reviewer: state.reviewer,
      mode: "single",
    });
    if (!response.ok) {
      say(response.status === 400 ? "invalid reviewer name" : "mark failed (HTTP " + response.status + ")");
      return;
    }
    state.marked.push(key); // recorded on the server, whatever the body holds
    const { changed } = await replyStatuses(response);
    applyStatuses(changed);
    say("Marked " + verdict + ": " + key);
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
    if (resynced || changed.has(expected)) {
      state.marked.pop();
      index = state.items.indexOf(expected);
      say("Undone: " + expected + " is " + state.statuses.get(expected));
    } else {
      say("Undid another client's mark: " + describeStatuses(changed));
      index = state.items.findIndex((k) => changed.has(k));
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
    new ResizeObserver(onResize).observe(ui.image); // layout changes such as the footer wrapping
  }
  window.addEventListener("hashchange", onHashChange);
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
    state.batchOf = new Map(manifest.map((row) => [row.key, row.batch]));
    state.items = shuffle(manifest.filter((row) => isTodo(statuses.get(row.key))).map((row) => row.key));
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
