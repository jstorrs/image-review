"use strict";
// Runs app.js (path in argv[2]) against a minimal stub DOM and checks grid mode's
// display and verdict rules end to end. Run by tests/test_socket_server.py; exits non-zero on failure.
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const APP = fs.readFileSync(process.argv[2], "utf8");
const JPEG = [0xff, 0xd8, 1, 2, 0xff, 0xd9];

function makePage({ manifest, statuses, stage = [400, 300], dpr = 2 }) {
  const pending = []; // fetches not yet answered
  const timers = [];
  let clock = 0;
  let frames = [];
  const draws = []; // tags of the bitmaps drawn
  const bitmaps = [];
  const grids = { inFlight: 0, most: 0, sent: [] };
  const posts = []; // {path, body} of every /mark and /undo
  const unhidden = []; // what the page held each time the canvas was unhidden

  function element(id) {
    const listeners = {};
    let hidden = false;
    return {
      id, textContent: "", title: "", value: "", alt: "", disabled: false, dataset: {}, style: {}, width: 0, height: 0,
      naturalWidth: 100, naturalHeight: 50,
      classList: { toggle() {} },
      get hidden() { return hidden; },
      set hidden(value) {
        if (id === "grid" && hidden && !value) {
          const item = page.state.items[page.state.index];
          unhidden.push({ keys: [...item.keys], placements: item.placements.map((p) => p.key),
            images: pending.filter((r) => r.path.startsWith("/image")).length, dwell: page.state.dwell });
        }
        hidden = value;
      },
      addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
      fire(type) { (listeners[type] || []).forEach((fn) => fn({})); },
      removeAttribute() {},
      blur() {},
      decode: async () => {},
      getContext: () => context,
      getBoundingClientRect() {
        if (id === "grid") { // centred in the stage at its CSS size
          const w = parseFloat(this.style.width) || 0, h = parseFloat(this.style.height) || 0;
          const left = (stage[0] - w) / 2 + page.shift, top = (stage[1] - h) / 2; // shift: off-centre by that much
          return { left, top, width: w, height: h, right: left + w, bottom: top + h };
        }
        return { left: 0, top: 0, width: stage[0], height: stage[1], right: stage[0], bottom: stage[1] };
      },
    };
  }
  const context = {
    lost: false, // what isContextLost() says
    failOn: null, // a bitmap tag whose drawImage throws
    setTransform() {}, fillRect() {},
    isContextLost() { return this.lost; },
    drawImage(bitmap) {
      assert(!bitmap.closed, "a closed bitmap is never drawn");
      if (bitmap.tag === this.failOn) throw new Error("drawImage failed");
      draws.push(bitmap.tag);
    },
  };
  const ids = ["reviewer", "progress", "item-status", "scale", "where", "mode", "stage", "image", "grid", "placeholder",
    "message", "status", "clean", "dirty", "prev", "next", "undo"];
  const els = Object.fromEntries(ids.map((id) => [id, element(id)]));
  const docListeners = {};
  const winListeners = {};
  const storage = new Map([["reviewer", "Jane"]]);

  function fetch(path, options = {}) {
    if (path === "/grids") {
      grids.inFlight++;
      grids.most = Math.max(grids.most, grids.inFlight);
      grids.sent.push(JSON.parse(options.body));
    }
    if (path === "/mark" || path === "/undo") {
      posts.push({ path, body: JSON.parse(options.body) });
    }
    return new Promise((resolve) => pending.push({ path, resolve }));
  }
  const ctx = {
    window: { devicePixelRatio: dpr, addEventListener: (t, fn) => (winListeners[t] ||= []).push(fn) },
    document: { activeElement: null, getElementById: (id) => els[id], addEventListener: (t, fn) => (docListeners[t] ||= []).push(fn) },
    location: { hash: "#TOKEN", pathname: "/", reload() {} },
    history: { replaceState() {} },
    sessionStorage: { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: (k) => storage.delete(k) },
    fetch,
    URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    requestAnimationFrame: (fn) => frames.push(fn),
    setTimeout: (fn, ms) => { const t = { at: clock + ms, fn }; timers.push(t); return t; },
    clearTimeout: (t) => { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); },
    createImageBitmap: async (blob) => {
      const bitmap = { tag: blob.tag, width: blob.size[0], height: blob.size[1], closed: false, close() { this.closed = true; } };
      bitmaps.push(bitmap);
      return bitmap;
    },
  };
  vm.createContext(ctx);
  vm.runInContext(APP + "\n;globalThis.__state = state;", ctx);

  const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
  const json = (status, body) => ({ status, ok: status < 300, headers: { get: () => "application/json" }, json: async () => body });
  const image = (tag, size, status = 200) => ({
    status, ok: status < 300, headers: { get: () => "image/jpeg" },
    blob: async () => ({ tag, size, arrayBuffer: async () => Uint8Array.from(JPEG).buffer }),
  });
  const page = {
    els, draws, bitmaps, grids, posts, unhidden, pending, context, shift: 0,
    get state() { return ctx.__state; },
    async respond(path, reply) {
      const i = pending.findIndex((r) => r.path === path);
      assert(i >= 0, "no pending " + path + "; pending: " + pending.map((r) => r.path));
      const [request] = pending.splice(i, 1);
      if (path === "/grids") grids.inFlight--;
      request.resolve(reply);
      await settle();
    },
    json, image,
    async image200(key, size) { await page.respond("/image?key=" + encodeURIComponent(key), image(key, size)); },
    async key(key, shiftKey = false) {
      (docListeners.keydown || []).forEach((fn) => fn({ key, shiftKey, target: {}, preventDefault() {} }));
      await settle();
    },
    async click(id) { page.els[id].fire("click"); await settle(); },
    async dwell() { await page.paint(); await page.advance(200); },
    marks() { return posts.filter((post) => post.path === "/mark").map((post) => post.body); },
    async frame() { const f = frames; frames = []; f.forEach((fn) => fn()); await settle(); },
    async paint() { await page.frame(); await page.frame(); },
    async advance(ms) {
      clock += ms;
      for (const t of timers.filter((t) => t.at <= clock)) { timers.splice(timers.indexOf(t), 1); t.fn(); }
      await settle();
    },
    async resize(width, height) { stage = [width, height]; (winListeners.resize || []).forEach((fn) => fn()); await settle(); },
    async boot() {
      await settle();
      await page.respond("/current_pass", json(200, { pass: 1 }));
      await page.respond("/manifest", json(200, manifest));
      await page.respond("/statuses?pass=1", json(200, statuses));
      const first = page.state.items[0];
      if (first) await page.respond("/image?key=" + encodeURIComponent(first.key), image(first.key, [100, 50]));
    },
    async enterGrid(current = statuses, shift = false) {
      await page.key(shift ? "M" : "m", shift);
      await page.respond("/statuses?pass=1", json(200, current));
    },
  };
  return page;
}

const manifest = ["a", "b", "c"].map((key) => ({ key, batch: "b1" }));
const statuses = { a: "UNREVIEWED", b: "UNREVIEWED", c: "UNREVIEWED" };
const place = (key, x) => ({ key, x, y: 0, w: 100, h: 100, rotated: false, source: [200, 200] });
const PLAN = { grids: [[place("a", 0), place("b", 100), place("c", 200)]], left_out: [] };
const TWO_GRIDS = { grids: [[place("a", 0), place("b", 100)], [place("c", 0)]], left_out: [] };
const tests = {};

// m, then PLAN's grid drawn and shown (not yet painted).
async function showPlan(page) {
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, PLAN));
  for (const key of ["a", "b", "c"]) await page.image200(key, [200, 200]);
}

tests["grid shown only once every placement settled; failures out of the item first"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  assert.deepStrictEqual(page.grids.sent[0], { keys: ["a", "b", "c"], width: 800, height: 600, rotation: "auto" });
  await page.respond("/grids", page.json(200, PLAN));
  await page.image200("a", [200, 200]);
  await page.image200("b", [201, 200]); // decoded size is not `source`
  assert.strictEqual(page.els.grid.hidden, true, "hidden while an image is outstanding");
  await page.paint();
  assert.strictEqual(page.state.dwell, "none", "no dwell before the grid is shown");
  await page.respond("/image?key=c", page.image("c", [200, 200], 404));
  assert.deepStrictEqual(page.unhidden, [{ keys: ["a"], placements: ["a"], images: 0, dwell: "none" }]);
  assert.deepStrictEqual(page.draws, ["a"]);
  assert(page.bitmaps.every((bitmap) => bitmap.closed));
  assert.deepStrictEqual(page.state.items.map((item) => item.kind + ":" + (item.keys || [item.key]).join()),
    ["grid:a", "single:b", "single:c"]);
  await page.paint();
  assert.strictEqual(page.state.dwell, "running");
  await page.advance(200);
  assert.strictEqual(page.state.dwell, "over");
};

tests["a stale bitmap is closed and never drawn"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  const [first, second] = page.state.items.map((item) => item.keys);
  await page.key("ArrowRight");
  for (const key of first) await page.image200(key, [200, 200]);
  assert.deepStrictEqual(page.draws, []);
  assert.strictEqual(page.bitmaps.length, first.length);
  assert(page.bitmaps.every((bitmap) => bitmap.closed));
  for (const key of second) await page.image200(key, [200, 200]);
  assert.deepStrictEqual(page.draws, second);
};

tests["a resize mid-draw draws nothing and starts no dwell"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, PLAN));
  await page.image200("a", [200, 200]);
  await page.resize(500, 300);
  for (const key of ["b", "c"]) await page.image200(key, [200, 200]);
  await page.paint();
  await page.advance(250); // past the dwell and the repack debounce
  assert.deepStrictEqual(page.draws, ["a"]);
  assert.strictEqual(page.els.grid.hidden, true);
  assert.strictEqual(page.state.dwell, "none");
  assert.deepStrictEqual(page.unhidden, []);
  await page.advance(50);
  assert.deepStrictEqual(page.grids.sent[1], { keys: ["a", "b", "c"], width: 1000, height: 600, rotation: "auto" });
};

tests["one /grids in flight; the 503 retry stops on s"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.enterGrid(statuses, true); // M while the first request is out
  assert.strictEqual(page.grids.sent.length, 1, "the second waits");
  await page.respond("/grids", page.json(200, PLAN)); // stale: dropped
  assert.strictEqual(page.grids.sent.length, 2);
  assert.strictEqual(page.grids.sent[1].rotation, "never");
  await page.respond("/grids", page.json(503, null));
  await page.advance(2000);
  assert.strictEqual(page.grids.sent.length, 3, "retried after 2 s");
  await page.respond("/grids", page.json(503, null));
  await page.key("s");
  await page.respond("/statuses?pass=1", page.json(200, statuses));
  await page.advance(10000);
  assert.strictEqual(page.grids.sent.length, 3, "no retry once single mode is on");
  assert.strictEqual(page.grids.most, 1);
  assert.strictEqual(page.state.mode, "single");
};

tests["the cached layout is used until eligibility changes"] = async () => {
  const before = { ...statuses, a: "FLAGGED" };
  const page = makePage({ manifest, statuses: before });
  await page.boot();
  await page.enterGrid(before);
  const plan = { grids: [[place("b", 0), place("c", 100)]], left_out: [] };
  await page.respond("/grids", page.json(200, plan));
  await page.enterGrid(before);
  assert.strictEqual(page.grids.sent.length, 1, "same keys, size and rotation: cached");
  await page.enterGrid({ ...statuses, b: "FLAGGED" }); // as many keys, but not the same ones
  assert.strictEqual(page.grids.sent.length, 2);
  assert.deepStrictEqual(page.grids.sent[1].keys, ["a", "c"]);
};

tests["leaving grid mode frees the canvas"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, PLAN));
  for (const key of ["a", "b", "c"]) await page.image200(key, [200, 200]);
  assert.strictEqual(page.els.grid.width, 800);
  assert.strictEqual(page.els.grid.hidden, false);
  await page.key("s");
  await page.respond("/statuses?pass=1", page.json(200, statuses));
  assert.strictEqual(page.els.grid.width, 0);
  assert.strictEqual(page.els.grid.hidden, true);
};

// A test stuck on a promise that never settles ends node early: exit code 1 and
// no "N/N passed" line, which the Python test looks for.
process.exitCode = 1;
for (const dwell of ["running", "over"]) {
  tests["contextlost with the dwell " + dwell + " takes the grid off screen"] = async () => {
    const page = makePage({ manifest, statuses });
    await showPlan(page);
    await page.paint();
    await page.advance(dwell === "over" ? 200 : 100);
    assert.strictEqual(page.state.dwell, dwell);
    page.els.grid.fire("contextlost");
    assert.strictEqual(page.els.grid.hidden, true);
    assert.strictEqual(page.state.dwell, "none");
    assert.strictEqual(page.state.loaded, false);
    await page.paint();
    await page.advance(1000);
    assert.strictEqual(page.state.dwell, "none", "no dwell while the context is lost");
  };
}

tests["contextrestored redraws every placement; the dwell starts after the unhide"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  page.els.grid.fire("contextlost");
  page.els.grid.fire("contextrestored");
  await page.paint();
  await page.advance(1000);
  assert.strictEqual(page.state.dwell, "none", "nothing on screen until the redraw ends");
  for (const key of ["a", "b", "c"]) await page.image200(key, [200, 200]);
  assert.deepStrictEqual(page.draws, ["a", "b", "c", "a", "b", "c"]);
  assert.strictEqual(page.unhidden.length, 2);
  assert(page.unhidden.every((u) => u.dwell === "none" && u.keys.length === 3));
  assert.strictEqual(page.state.dwell, "none");
  await page.paint();
  assert.strictEqual(page.state.dwell, "running");
};

tests["contextlost mid-draw shows nothing until the redraw"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, PLAN));
  await page.image200("a", [200, 200]);
  page.els.grid.fire("contextlost");
  for (const key of ["b", "c"]) await page.image200(key, [200, 200]);
  await page.paint();
  assert.deepStrictEqual(page.unhidden, []);
  assert.strictEqual(page.els.grid.hidden, true);
  page.els.grid.fire("contextrestored");
  for (const key of ["a", "b", "c"]) await page.image200(key, [200, 200]);
  assert.strictEqual(page.unhidden.length, 1);
  assert.deepStrictEqual(page.unhidden[0].keys, ["a", "b", "c"]);
};

tests["a context still lost when the draw ends moves every key to a single item"] = async () => {
  const page = makePage({ manifest, statuses });
  page.context.lost = true;
  await showPlan(page);
  assert.deepStrictEqual(page.unhidden, []);
  assert.strictEqual(page.els.grid.hidden, true);
  assert(page.state.items.every((item) => item.kind === "single"));
  assert.deepStrictEqual(page.state.items.map((item) => item.key).sort(), ["a", "b", "c"]);
};

tests["a drawImage that throws demotes its key and closes the bitmap"] = async () => {
  const page = makePage({ manifest, statuses });
  page.context.failOn = "b";
  await showPlan(page);
  assert.deepStrictEqual(page.unhidden.map((u) => u.keys), [["a", "c"]]);
  assert(page.bitmaps.every((bitmap) => bitmap.closed));
  assert.deepStrictEqual(page.state.items.map((item) => item.keys || item.key), [["a", "c"], "b"]);
};

tests["no dwell while the canvas box is not inside the stage"] = async () => {
  const page = makePage({ manifest, statuses });
  page.shift = 1;
  await showPlan(page);
  await page.paint();
  await page.advance(1000);
  assert.strictEqual(page.state.dwell, "none");
  assert.strictEqual(page.state.scale, 0);
};

tests["a stale dwell start is dropped"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  const [first, second] = page.state.items.map((item) => item.keys);
  for (const key of first) await page.image200(key, [200, 200]);
  await page.frame(); // the first grid's dwell start is one frame from running
  await page.key("ArrowRight");
  for (const key of second) await page.image200(key, [200, 200]);
  await page.frame(); // ends the stale wait; the second grid has had one frame
  assert.strictEqual(page.state.dwell, "none", "two frames of the new grid first");
  await page.frame();
  assert.strictEqual(page.state.dwell, "running");
};

// ---- Verdicts ----

const plain = (value) => JSON.parse(JSON.stringify(value)); // across the vm realm
const UNREVIEWED = (keys) => Object.fromEntries(keys.map((key) => [key, "UNREVIEWED"]));
const LEFT_OUT = { grids: [[place("a", 0), place("b", 100)]], left_out: ["c"] };

for (const verdict of ["CLEAN", "DIRTY"]) {
  tests["grid " + verdict + " sends exactly the drawn keys, mode grid; a demoted single follows as grid"] = async () => {
    const page = makePage({ manifest, statuses });
    await page.boot();
    await page.enterGrid();
    await page.respond("/grids", page.json(200, PLAN));
    await page.image200("a", [200, 200]);
    await page.respond("/image?key=b", page.image("b", [200, 200], 404)); // demoted
    await page.image200("c", [200, 200]);
    await page.dwell();
    await page.key(verdict[0].toLowerCase());
    assert.deepStrictEqual(page.marks(), [{ keys: ["a", "c"], status: verdict, pass: 1, reviewer: "Jane", mode: "grid" }]);
    await page.respond("/mark", page.json(200, { a: verdict, c: verdict }));
    assert.deepStrictEqual(plain(page.state.marked), [["a", "c"]]);
    assert.strictEqual(page.state.items[page.state.index].key, "b", "on to the next todo item");
    await page.image200("b", [100, 50]);
    await page.dwell();
    await page.key("d");
    assert.deepStrictEqual(page.marks()[1], { keys: ["b"], status: "DIRTY", pass: 1, reviewer: "Jane", mode: "grid" });
  };
}

tests["a left-out single in grid mode marks with mode grid"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, LEFT_OUT));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.click("clean");
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN" }));
  await page.image200("c", [100, 50]);
  await page.dwell();
  await page.key("c");
  assert.deepStrictEqual(page.marks().map((m) => [m.keys, m.mode]), [[["a", "b"], "grid"], [["c"], "grid"]]);
};

tests["single mode sends mode single"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.dwell();
  const key = page.state.items[0].key;
  await page.key("c");
  assert.deepStrictEqual(page.marks(), [{ keys: [key], status: "CLEAN", pass: 1, reviewer: "Jane", mode: "single" }]);
};

for (const other of ["DIRTY", "FLAGGED"]) {
  tests["grid CLEAN refused, with no request, once a key is " + other] = async () => {
    const page = makePage({ manifest, statuses });
    await page.boot();
    await page.enterGrid();
    await page.respond("/grids", page.json(200, TWO_GRIDS)); // [a, b] first (fullest), then [c]
    for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
    await page.key("ArrowRight");
    await page.image200("c", [200, 200]);
    await page.dwell();
    await page.key("d");
    await page.respond("/mark", page.json(200, { c: "DIRTY", a: other })); // a shares c's image_id
    assert.strictEqual(page.state.index, -1, "[a, b] is no longer todo, so skipped");
    await page.key("ArrowLeft");
    await page.key("ArrowLeft");
    for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
    await page.dwell();
    await page.key("c");
    await page.click("clean");
    assert.strictEqual(page.els.status.textContent,
      "grid contains an image already marked DIRTY - review it in single mode");
    assert.strictEqual(page.marks().length, 1, "no request for the refused CLEAN");
    await page.key("d");
    assert.deepStrictEqual(page.marks()[1].keys, ["a", "b"], "DIRTY is still allowed");
  };
}

tests["a grid of only DIRTY images may be marked CLEAN again"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  await page.dwell();
  await page.key("d");
  await page.respond("/mark", page.json(200, { a: "DIRTY", b: "DIRTY", c: "DIRTY" }));
  await page.key("ArrowLeft");
  for (const key of ["a", "b", "c"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("c");
  assert.deepStrictEqual(page.marks().map((m) => m.status), ["DIRTY", "CLEAN"]);
};

tests["no grid verdict before the dwell is over or while the canvas is hidden"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, PLAN));
  await page.image200("a", [200, 200]);
  await page.paint();
  await page.advance(1000);
  await page.key("c"); // still drawing: hidden
  await page.click("dirty");
  for (const key of ["b", "c"]) await page.image200(key, [200, 200]);
  await page.paint();
  await page.advance(100); // dwell running
  await page.key("c");
  await page.key("d");
  assert.strictEqual(page.state.dwell, "running");
  assert.strictEqual(page.els.clean.disabled, true);
  assert.strictEqual(page.els.dirty.disabled, true);
  assert.deepStrictEqual(page.marks(), []);
  await page.advance(100);
  assert.strictEqual(page.els.clean.disabled, false);
  assert.strictEqual(page.els.dirty.disabled, false);
};

tests["no grid verdict during a resize repack"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  await page.dwell();
  await page.resize(500, 300);
  await page.key("c");
  await page.advance(1000); // the repack is computing
  await page.key("d");
  assert.deepStrictEqual(page.marks(), []);
  await page.respond("/grids", page.json(200, PLAN));
  for (const key of ["a", "b", "c"]) await page.image200(key, [200, 200]);
  await page.key("c"); // shown, but not yet for the dwell
  assert.deepStrictEqual(page.marks(), []);
  await page.dwell();
  await page.key("c");
  assert.strictEqual(page.marks().length, 1);
};

tests["no grid verdict after contextlost"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  await page.dwell();
  page.els.grid.fire("contextlost");
  await page.key("c");
  await page.click("dirty");
  assert.deepStrictEqual(page.marks(), []);
};

tests["a double c, or a click and a key, sends one grid mark"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  await page.dwell();
  await page.key("c");
  await page.key("c");
  await page.click("clean");
  await page.key("d");
  assert.strictEqual(page.marks().length, 1);
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN", c: "CLEAN" }));
  await page.key("c"); // the end screen
  assert.strictEqual(page.marks().length, 1);
};

tests["z redraws the marked grid with a fresh dwell and pops the stack"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN" }));
  await page.image200("c", [200, 200]);
  await page.dwell();
  assert.deepStrictEqual(plain(page.state.marked), [["a", "b"]]);
  await page.key("z");
  assert.deepStrictEqual(page.posts.map((post) => post.path), ["/mark", "/undo"]);
  await page.respond("/undo", page.json(200, UNREVIEWED(["a", "b"])));
  assert.deepStrictEqual(plain(page.state.marked), []);
  assert.strictEqual(page.els.grid.hidden, true, "redrawn before it shows");
  await page.key("c");
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  assert.deepStrictEqual(page.unhidden.map((u) => u.keys), [["a", "b"], ["c"], ["a", "b"]]);
  assert.strictEqual(page.state.dwell, "none");
  await page.key("c");
  await page.paint();
  await page.advance(100);
  await page.key("c");
  assert.strictEqual(page.marks().length, 1, "no verdict until the fresh dwell is over");
  await page.advance(100);
  await page.key("c");
  assert.deepStrictEqual(page.marks()[1].keys, ["a", "b"]);
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN" }));
  assert.deepStrictEqual(plain(page.state.marked), [["a", "b"]]);
};

tests["z after another client's mark warns and keeps the stack"] = async () => {
  const page = makePage({ manifest: manifest.concat([{ key: "x", batch: "b2" }]), statuses: { ...statuses, x: "CLEAN" } });
  await showPlan(page);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN", c: "CLEAN" }));
  await page.key("z");
  await page.respond("/undo", page.json(200, { x: "UNREVIEWED" }));
  assert(page.els.status.textContent.startsWith("Undid another client's mark: x is UNREVIEWED"));
  assert.deepStrictEqual(plain(page.state.marked), [["a", "b", "c"]]);
};

tests["a resize repack clears the undo stack"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN" }));
  await page.resize(500, 300);
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo", "at once, not only after the debounce");
  await page.advance(300);
  assert.deepStrictEqual(page.grids.sent[1].keys, ["c"]);
  await page.respond("/grids", page.json(200, { grids: [[place("c", 0)]], left_out: [] }));
  await page.image200("c", [200, 200]);
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo");
  assert(!page.posts.some((post) => post.path === "/undo"));
};

tests["a resize during a grid mark: no undo entry, and the repack waits for the reply"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  await page.dwell();
  await page.key("c");
  await page.resize(500, 300);
  await page.advance(1000);
  assert.strictEqual(page.grids.sent.length, 1, "no repack while the mark is out");
  const shown = page.state.showSeq;
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN", c: "CLEAN" }));
  assert.deepStrictEqual(plain(page.state.marked), []);
  assert.strictEqual(page.state.showSeq, shown, "no stale advance");
  assert.strictEqual(page.els.message.textContent, "Computing grids...");
  await page.advance(300);
  assert.strictEqual(page.grids.sent.length, 1, "nothing left to pack");
  assert.strictEqual(page.els.message.textContent, "Pass 1: nothing left to review");
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo");
};

tests["single mode: a FLAGGED image takes CLEAN; a placeholder takes DIRTY only"] = async () => {
  const page = makePage({ manifest: [{ key: "a", batch: "b1" }], statuses: { a: "FLAGGED" } });
  await page.boot();
  await page.dwell();
  await page.key("c");
  assert.deepStrictEqual(page.marks().map((m) => [m.keys, m.status]), [[["a"], "CLEAN"]]);
  const other = makePage({ manifest, statuses });
  await other.boot();
  await other.key("ArrowRight");
  const key = other.state.items[1].key;
  await other.respond("/image?key=" + key, other.image(key, [100, 50], 404));
  await other.dwell();
  assert.strictEqual(other.els.clean.disabled, true);
  assert.strictEqual(other.els.dirty.disabled, false);
  await other.key("c");
  assert.strictEqual(other.els.status.textContent, "cannot mark CLEAN: image could not be loaded");
  assert.deepStrictEqual(other.marks(), []);
};

tests["canJudge needs the grid canvas shown and loaded, not only the dwell"] = async () => {
  for (const spoil of [(page) => { page.els.grid.hidden = true; }, (page) => { page.state.loaded = false; }]) {
    const page = makePage({ manifest, statuses });
    await showPlan(page);
    await page.dwell();
    assert.strictEqual(page.state.dwell, "over");
    spoil(page);
    page.els.reviewer.fire("input"); // renders
    assert.strictEqual(page.els.clean.disabled, true);
    assert.strictEqual(page.els.dirty.disabled, true);
    await page.key("c");
    await page.key("d");
    await page.click("dirty");
    assert.deepStrictEqual(page.marks(), []);
  }
};

tests["z redraws the second grid when its mark is undone"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.key("ArrowRight");
  await page.image200("c", [200, 200]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { c: "CLEAN" }));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]); // wrapped round to [a, b]
  await page.key("z");
  await page.respond("/undo", page.json(200, { c: "UNREVIEWED" }));
  await page.image200("c", [200, 200]);
  assert.deepStrictEqual(page.unhidden.map((u) => u.keys), [["a", "b"], ["c"], ["a", "b"], ["c"]]);
};

// [c] then [a, b] marked, ending on the end screen.
async function markAllToEnd(page) {
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.key("ArrowRight");
  await page.image200("c", [200, 200]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { c: "CLEAN" }));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN" }));
  assert.strictEqual(page.state.index, -1);
}

tests["a resize on the end screen keeps z; the undo repacks and lands on the undone grid"] = async () => {
  const page = makePage({ manifest, statuses });
  await markAllToEnd(page);
  await page.resize(500, 300);
  await page.advance(1000);
  assert.strictEqual(page.grids.sent.length, 1, "no repack on the end screen");
  assert.deepStrictEqual(plain(page.state.marked), [["c"], ["a", "b"]]);
  await page.key("z");
  await page.respond("/undo", page.json(200, { a: "UNREVIEWED", b: "UNREVIEWED" }));
  assert.strictEqual(page.els.status.textContent, "Undone: grid of 2 images");
  assert.deepStrictEqual(plain(page.state.marked), [], "the repack empties the rest of the stack");
  await page.advance(300);
  assert.deepStrictEqual(page.grids.sent[1], { keys: ["a", "b"], width: 1000, height: 600, rotation: "auto" });
  await page.respond("/grids", page.json(200, { grids: [[place("b", 0)], [place("a", 0)]], left_out: [] }));
  await page.image200("a", [200, 200]);
  assert.deepStrictEqual(page.unhidden.at(-1).keys, ["a"], "lands on the grid holding the undone keys");
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo");
  assert.strictEqual(page.posts.filter((post) => post.path === "/undo").length, 1);
};

tests["leaving the end screen after a resize repacks first"] = async () => {
  const page = makePage({ manifest, statuses });
  await markAllToEnd(page);
  await page.resize(500, 300);
  await page.key("ArrowRight");
  assert(!page.pending.some((r) => r.path.startsWith("/image")), "no grid drawn at the old size");
  await page.advance(300);
  assert.strictEqual(page.grids.sent.length, 1, "nothing left to pack");
  assert.strictEqual(page.els.message.textContent, "Pass 1: nothing left to review");
};

tests["a resize during /undo: the repack waits, then lands on the undone grid"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN" }));
  await page.image200("c", [200, 200]);
  await page.key("z");
  await page.resize(500, 300);
  await page.advance(1000);
  assert.strictEqual(page.grids.sent.length, 1, "no repack while the undo is out");
  await page.respond("/undo", page.json(200, { a: "UNREVIEWED", b: "UNREVIEWED" }));
  assert.strictEqual(page.els.status.textContent, "Undone: grid of 2 images");
  assert.deepStrictEqual(plain(page.state.marked), []);
  await page.advance(300);
  assert.deepStrictEqual(page.grids.sent[1].keys, ["a", "b", "c"]);
  const plan = { grids: [[place("c", 0), place("b", 100)], [place("a", 0)]], left_out: [] };
  await page.respond("/grids", page.json(200, plan));
  await page.image200("a", [200, 200]);
  assert.deepStrictEqual(page.unhidden.at(-1).keys, ["a"], "the undone keys, not the grid shown at the resize");
};

(async () => {
  const names = Object.keys(tests);
  let passed = 0;
  for (const name of names) {
    try {
      await tests[name]();
      passed++;
    } catch (error) {
      console.error("FAIL: " + name + "\n" + (error.stack || error));
    }
  }
  console.log(passed + "/" + names.length + " passed");
  process.exitCode = passed === names.length ? 0 : 1;
})();
