"use strict";
// Runs app.js (path in argv[2]) against a minimal stub DOM and checks grid mode's
// display and verdict rules end to end. Run by tests/test_socket_server.py; exits non-zero on failure.
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const APP = fs.readFileSync(process.argv[2], "utf8");
const JPEG = [0xff, 0xd8, 1, 2, 0xff, 0xd9];
const MARK_FLASH_MS = 200; // app.js's: a marked item stays up this long before the next
const REPACK_MS = 300; // app.js REPACK_DELAY_MS

// fixedOrder: Math.random stuck near 1, so shuffle keeps the order (grids then go fullest first, stably).
// reviewer: the name in sessionStorage at startup (null: none).
function makePage({ manifest, statuses, stage = [400, 300], dpr = 2, fixedOrder = false, reviewer = "Jane" }) {
  const pending = []; // fetches not yet answered
  const timers = [];
  let clock = 0;
  let frames = [];
  const draws = []; // tags of the bitmaps drawn
  const bitmaps = [];
  const grids = { inFlight: 0, most: 0, sent: [] };
  const posts = []; // {path, body, instance} of every /mark and /undo
  const revoked = []; // blob URLs revoked
  const removedAttrs = []; // [element id, attribute] of each removeAttribute
  const unhidden = []; // what the page held each time the canvas was unhidden

  function element(id) {
    const listeners = {};
    let hidden = id === "help" || id === "name-box"; // as index.html has them
    return {
      id, textContent: "", title: "", value: "", alt: "", disabled: false, dataset: {}, style: {}, width: 0, height: 0,
      naturalWidth: 100, naturalHeight: 50,
      classes: new Set(),
      classList: {
        toggle(name, on) { const el = els[id]; if (on ?? !el.classes.has(name)) el.classes.add(name); else el.classes.delete(name); },
        contains(name) { return els[id].classes.has(name); },
      },
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
      fire(type, event = {}) { (listeners[type] || []).forEach((fn) => fn(event)); },
      removeAttribute(name) { removedAttrs.push([id, name]); },
      blur() { if (ctx.document.activeElement === this) ctx.document.activeElement = null; },
      focus() { ctx.document.activeElement = this; },
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
  const ids = ["bar", "reviewer", "name-box", "name-ok", "reviewer-chip", "reviewer-name", "help", "help-button", "progress",
    "item-status", "scale", "where", "mode", "stage", "image", "grid", "placeholder", "message", "status", "clean", "dirty",
    "prev", "next", "undo", "reconnect", "done"];
  const els = Object.fromEntries(ids.map((id) => [id, element(id)]));
  const docListeners = {};
  const winListeners = {};
  const storage = new Map(reviewer === null ? [] : [["reviewer", reviewer]]);

  function fetch(path, options = {}) {
    if (path === "/grids") {
      grids.inFlight++;
      grids.most = Math.max(grids.most, grids.inFlight);
      grids.sent.push(JSON.parse(options.body));
    }
    const instance = (options.headers || {})["X-Review-Instance"] ?? null; // the instance the page sent
    if (path === "/mark" || path === "/undo") {
      posts.push({ path, body: JSON.parse(options.body), instance });
    }
    return new Promise((resolve, reject) => pending.push({ path, instance, resolve, reject }));
  }
  const ctx = {
    window: { devicePixelRatio: dpr, addEventListener: (t, fn) => (winListeners[t] ||= []).push(fn) },
    document: { activeElement: null, getElementById: (id) => els[id], addEventListener: (t, fn) => (docListeners[t] ||= []).push(fn) },
    location: { hash: "#TOKEN", pathname: "/", reload() { page.reloads++; } },
    history: { replaceState() {} },
    sessionStorage: { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: (k) => storage.delete(k) },
    fetch,
    URL: { createObjectURL: () => "blob:x", revokeObjectURL: (url) => revoked.push(url) },
    requestAnimationFrame: (fn) => frames.push(fn),
    setTimeout: (fn, ms) => { const t = { at: clock + ms, fn }; timers.push(t); return t; },
    clearTimeout: (t) => { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); },
    createImageBitmap: async (blob) => {
      await page.bitmapGate; // a test may hold decoding back
      const bitmap = { tag: blob.tag, width: blob.size[0], height: blob.size[1], closed: false, close() { this.closed = true; } };
      bitmaps.push(bitmap);
      return bitmap;
    },
  };
  vm.createContext(ctx);
  if (fixedOrder) vm.runInContext("Math.random = () => 0.99;", ctx);
  vm.runInContext(APP + "\n;globalThis.__state = state;", ctx);

  const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
  // Reply headers as the socket server sends them: the content type and the serving instance (page.instance)
  const headers = (type) => {
    const map = new Map([["content-type", type], ["x-review-instance", page.instance]]);
    return { get: (name) => map.get(name.toLowerCase()) ?? null };
  };
  const json = (status, body) => ({ status, ok: status < 300, headers: headers("application/json"), json: async () => body });
  const image = (tag, size, status = 200) => ({
    status, ok: status < 300, headers: headers("image/jpeg"),
    blob: async () => ({ tag, size, arrayBuffer: async () => Uint8Array.from(JPEG).buffer }),
  });
  const page = {
    els, draws, bitmaps, grids, posts, unhidden, pending, context, storage, revoked, removedAttrs, shift: 0, bitmapGate: null, reloads: 0,
    autoFlash: true, // respond("/mark", ok) also lets the mark's flash pass
    instance: "A", // the X-Review-Instance the stub server's replies carry: a test swaps servers by changing it
    get state() { return ctx.__state; },
    run(code) { return vm.runInContext(code, ctx); }, // calls into app.js, e.g. lose() as a failure from outside would
    document: ctx.document,
    async respond(path, reply) {
      const i = pending.findIndex((r) => r.path === path);
      assert(i >= 0, "no pending " + path + "; pending: " + pending.map((r) => r.path));
      const [request] = pending.splice(i, 1);
      if (path === "/grids") grids.inFlight--;
      request.resolve(reply);
      await settle();
      // A mark that went through keeps the marked item up for MARK_FLASH_MS before moving on:
      // let that pass, unless the test watches the flash itself (page.autoFlash = false)
      if (path === "/mark" && reply.ok && page.autoFlash) await page.advance(MARK_FLASH_MS);
    },
    async fail(path) { // the request fails as on a dropped tunnel
      const i = pending.findIndex((r) => r.path === path);
      assert(i >= 0, "no pending " + path + "; pending: " + pending.map((r) => r.path));
      const [request] = pending.splice(i, 1);
      if (path === "/grids") grids.inFlight--;
      request.reject(new TypeError("Failed to fetch"));
      await settle();
    },
    json, image,
    async image200(key, size) { await page.respond("/image?key=" + encodeURIComponent(key), image(key, size)); },
    async key(key, shiftKey = false, target = {}) {
      (docListeners.keydown || []).forEach((fn) => fn({ key, shiftKey, target, preventDefault() {} }));
      await settle();
    },
    async keyRepeat(key) {
      (docListeners.keydown || []).forEach((fn) => fn({ key, repeat: true, target: {}, preventDefault() {} }));
      await settle();
    },
    async hashchange(hash) {
      ctx.location.hash = hash;
      (winListeners.hashchange || []).forEach((fn) => fn({}));
      await settle();
    },
    async click(id) { page.els[id].fire("click"); await settle(); },
    async nameKey(key) { page.els.reviewer.fire("keydown", { key }); await settle(); }, // a key typed in the name box
    async typeName(name) { page.els.reviewer.value = name; page.els.reviewer.fire("input"); await settle(); },
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

const NEXT_SERVE = "Stop serve (Ctrl-C), start the next one, then press Reconnect (r).";
const END_OF_PASS = "Pass 1: nothing left to review. " + NEXT_SERVE;

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

tests["a 409 on grid CLEAN shows the refusal, rereads the statuses, and keeps no undo entry"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  await page.dwell();
  const index = page.state.index;
  await page.key("c"); // this page's statuses are stale: another client marked b DIRTY
  assert.strictEqual(page.marks().length, 1);
  await page.respond("/mark", page.json(409, { error: "grid holds a DIRTY or FLAGGED image" }));
  await page.respond("/statuses?pass=1", page.json(200, { ...statuses, b: "DIRTY" }));
  assert.strictEqual(page.els.status.textContent,
    "grid contains an image already marked DIRTY - review it in single mode");
  assert.strictEqual(page.state.statuses.get("b"), "DIRTY", "statuses resynced");
  assert.deepStrictEqual(plain(page.state.marked), [], "no undo entry");
  assert.strictEqual(page.state.index, index, "no advance");
  assert.strictEqual(page.state.busy, false);
  await page.key("c");
  assert.strictEqual(page.marks().length, 1, "the resynced statuses refuse CLEAN locally");
  await page.key("d");
  assert.deepStrictEqual(page.marks()[1].status, "DIRTY", "DIRTY is still allowed");
};

tests["a 409 on a left-out single's CLEAN in grid mode names the image, not a grid"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, LEFT_OUT));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("d");
  await page.respond("/mark", page.json(200, { a: "DIRTY", b: "DIRTY" }));
  await page.image200("c", [100, 50]);
  await page.dwell();
  await page.key("c"); // c became FLAGGED: another client wrote an earlier-pass DIRTY
  assert.deepStrictEqual(page.marks()[1], { keys: ["c"], status: "CLEAN", pass: 1, reviewer: "Jane", mode: "grid" });
  await page.respond("/mark", page.json(409, { error: "grid holds a DIRTY or FLAGGED image" }));
  await page.respond("/statuses?pass=1", page.json(200, { a: "DIRTY", b: "DIRTY", c: "FLAGGED" }));
  assert.strictEqual(page.els.status.textContent,
    "image already marked DIRTY in another pass - review it in single mode");
  assert.strictEqual(page.state.statuses.get("c"), "FLAGGED");
  assert.deepStrictEqual(plain(page.state.marked), [["a", "b"]], "no undo entry for the refusal");
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
  assert.strictEqual(page.els.status.textContent, "Marked CLEAN: grid of 3 images", "no move, so it is said");
  assert.strictEqual(page.els.message.textContent, "Computing grids...");
  await page.advance(300);
  assert.strictEqual(page.grids.sent.length, 1, "nothing left to pack");
  assert.strictEqual(page.els.message.textContent, END_OF_PASS);
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
  const page = makePage({ manifest, statuses, fixedOrder: true }); // [b] stays first: landing on [a] is no chance
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
  assert.strictEqual(page.els.message.textContent, END_OF_PASS);
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

// ---- Reconnect ----

const LOST = "Lost connection — your marks so far are saved on the server";

// Respond to Reconnect's three loads.
async function reload(page, current, pass = 1, rows = manifest) {
  await page.respond("/current_pass", page.json(200, { pass }));
  await page.respond("/manifest", page.json(200, rows));
  await page.respond("/statuses?pass=" + pass, page.json(200, current));
}

tests["a lost connection offers Reconnect; a rejected token does not"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  assert.strictEqual(page.els.reconnect.hidden, true);
  await page.key("ArrowRight");
  await page.fail("/image?key=" + page.state.items[1].key);
  assert.strictEqual(page.els.status.textContent, LOST);
  assert.strictEqual(page.els.reconnect.hidden, false);
  const other = makePage({ manifest, statuses });
  await other.boot();
  await other.key("ArrowRight");
  await other.respond("/image?key=" + other.state.items[1].key, other.json(401, {}));
  assert.strictEqual(other.els.status.textContent, "token rejected (server restarted?) - open the new URL");
  assert.strictEqual(other.els.reconnect.hidden, true);
  await other.key("r");
  await other.click("reconnect");
  assert.deepStrictEqual(other.pending, [], "nothing sent");
};

tests["Reconnect in single mode reloads, rebuilds, forgets the marks and waits for a fresh dwell"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.dwell();
  const first = page.state.items[0].key;
  await page.key("c");
  await page.respond("/mark", page.json(200, { [first]: "CLEAN" }));
  assert.deepStrictEqual(plain(page.state.marked), [[first]]);
  const shown = page.state.items[page.state.index].key;
  await page.fail("/image?key=" + shown);
  await page.click("reconnect");
  assert.strictEqual(page.els.status.textContent, "Reconnecting...");
  assert.strictEqual(page.els.reconnect.hidden, true);
  assert.deepStrictEqual(plain(page.state.marked), []);
  const other = ["a", "b", "c"].find((key) => key !== first && key !== shown);
  await reload(page, { [first]: "CLEAN", [shown]: "UNREVIEWED", [other]: "DIRTY" }); // another client marked
  assert.strictEqual(page.els.status.textContent, "Reconnected");
  assert.deepStrictEqual(page.state.items.map((item) => item.key), [shown], "the todo list, rebuilt");
  assert.strictEqual(page.state.statuses.get(other), "DIRTY");
  await page.image200(shown, [100, 50]);
  await page.key("c");
  await page.paint();
  await page.advance(100);
  await page.key("d");
  assert.strictEqual(page.marks().length, 1, "no verdict before a fresh dwell");
  await page.advance(100);
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo");
  assert(!page.posts.some((post) => post.path === "/undo"));
  await page.key("d");
  assert.deepStrictEqual(page.marks()[1], { keys: [shown], status: "DIRTY", pass: 1, reviewer: "Jane", mode: "single" });
  assert.strictEqual(page.els.reconnect.hidden, true);
};

tests["Reconnect in grid mode redraws, landing on the previous item's grid with a fresh dwell"] = async () => {
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
  await page.dwell();
  await page.key("d");
  await page.fail("/mark");
  assert.strictEqual(page.els.grid.hidden, false, "the grid stays up while stopped");
  assert.strictEqual(page.els.clean.disabled, true);
  await page.click("reconnect");
  assert.strictEqual(page.els.grid.hidden, true, "taken off screen at once");
  assert.strictEqual(page.state.dwell, "none");
  assert.deepStrictEqual(plain(page.state.marked), []);
  await reload(page, statuses); // another client undid c
  assert.strictEqual(page.grids.sent.length, 2, "laid out afresh");
  assert.deepStrictEqual(page.grids.sent[1], { keys: ["a", "b", "c"], width: 800, height: 600, rotation: "auto" });
  // fullest first puts [b, c] before [a]: landing on [a] is not the default
  await page.respond("/grids", page.json(200, { grids: [[place("b", 0), place("c", 100)], [place("a", 0)]], left_out: [] }));
  assert.strictEqual(page.els.grid.hidden, true, "hidden until redrawn");
  await page.image200("a", [200, 200]);
  assert.deepStrictEqual(page.unhidden.at(-1), { keys: ["a"], placements: ["a"], images: 0, dwell: "none" });
  await page.key("d");
  assert.strictEqual(page.marks().length, 2, "no verdict before a fresh dwell");
  await page.dwell();
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo");
  await page.key("d");
  assert.deepStrictEqual(page.marks()[2], { keys: ["a"], status: "DIRTY", pass: 1, reviewer: "Jane", mode: "grid" });
};

tests["Reconnect while the server is still down is lost again; one in flight at a time"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("ArrowRight");
  await page.fail("/image?key=" + page.state.items[1].key);
  await page.click("reconnect");
  await page.click("reconnect");
  await page.key("r");
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/current_pass"], "one Reconnect in flight");
  await page.fail("/current_pass");
  assert.strictEqual(page.els.status.textContent, LOST);
  assert.strictEqual(page.els.reconnect.hidden, false);
  assert.deepStrictEqual(page.pending, []);
  await page.key("r");
  await reload(page, statuses);
  assert.strictEqual(page.els.status.textContent, "Reconnected");
  assert.strictEqual(page.state.items.length, 3);
};

tests["a Reconnect answered 401 shows token rejected and no button"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("ArrowRight");
  await page.fail("/image?key=" + page.state.items[1].key);
  await page.click("reconnect");
  await page.respond("/current_pass", page.json(401, {}));
  assert.strictEqual(page.els.status.textContent, "token rejected (server restarted?) - open the new URL");
  assert.strictEqual(page.els.reconnect.hidden, true);
  await page.key("r");
  await page.advance(10000);
  assert.deepStrictEqual(page.pending, []);
};

tests["a Reconnect to a new pass uses it and says so"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("ArrowRight");
  await page.fail("/image?key=" + page.state.items[1].key);
  await page.click("reconnect");
  await reload(page, { a: "UNREVIEWED", b: "CLEAN", c: "CLEAN" }, 2);
  assert.strictEqual(page.state.pass, 2);
  assert.strictEqual(page.els.status.textContent, "Reconnected; now on pass 2");
  assert(page.els.progress.textContent.startsWith("Pass 2 · "));
  await page.image200("a", [100, 50]);
  await page.dwell();
  await page.key("c");
  assert.strictEqual(page.marks()[0].pass, 2);
};

tests["r reconnects only while the button is shown, in either case, never on repeat"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("r");
  await page.key("R");
  assert.deepStrictEqual(page.pending, []);
  await page.key("ArrowRight");
  await page.fail("/image?key=" + page.state.items[1].key);
  await page.keyRepeat("r");
  assert.deepStrictEqual(page.pending, [], "a held key acts once");
  await page.key("R");
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/current_pass"]);
};

tests["no automatic retry: timers after a loss send nothing"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  await page.dwell();
  await page.key("c");
  await page.resize(500, 300); // a repack waits for the mark's reply
  await page.fail("/mark");
  await page.advance(10000);
  await page.paint();
  await page.resize(400, 300);
  await page.advance(10000);
  page.els.grid.fire("contextrestored");
  await page.advance(10000);
  assert.deepStrictEqual(page.pending, []);
  assert.strictEqual(page.grids.sent.length, 1);
  assert.strictEqual(page.els.reconnect.hidden, false);
  await page.click("reconnect");
  await reload(page, statuses);
  assert.strictEqual(page.grids.sent.length, 2, "the Reconnect rebuilds the grids");
  // A repack from the cached layout landing on a left-out single would fetch its image
  const other = makePage({ manifest, statuses });
  await other.boot();
  await other.enterGrid();
  await other.respond("/grids", other.json(200, LEFT_OUT));
  for (const key of ["a", "b"]) await other.image200(key, [200, 200]);
  await other.key("ArrowRight");
  await other.image200("c", [100, 50]);
  await other.dwell();
  await other.key("d");
  await other.fail("/mark");
  await other.resize(500, 300);
  await other.resize(400, 300);
  await other.advance(10000);
  assert.deepStrictEqual(other.pending, []);
};

// Single mode: c on the first item, its reply, then z (/undo out) and the next image's fetch fails.
async function loseWithUndoOut(page) {
  await page.boot();
  await page.dwell();
  const first = page.state.items[0].key;
  await page.key("c");
  await page.respond("/mark", page.json(200, { [first]: "CLEAN" }));
  const shown = page.state.items[page.state.index].key;
  await page.key("z");
  await page.fail("/image?key=" + shown);
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/undo"]);
  return first;
}

tests["a reply to a request sent before the loss changes nothing, before or during Reconnect"] = async () => {
  const page = makePage({ manifest, statuses });
  const first = await loseWithUndoOut(page);
  const marked = plain(page.state.marked);
  await page.respond("/undo", page.json(200, { [first]: "UNREVIEWED" })); // while stopped
  assert.strictEqual(page.els.status.textContent, LOST);
  assert.strictEqual(page.els.reconnect.hidden, false);
  assert.strictEqual(page.state.statuses.get(first), "CLEAN");
  assert.deepStrictEqual(plain(page.state.marked), marked);
  assert.deepStrictEqual(page.pending, [], "nothing sent while stopped");
  const other = makePage({ manifest, statuses });
  const key = await loseWithUndoOut(other);
  await other.click("reconnect");
  await other.respond("/undo", other.json(200, { [key]: "UNREVIEWED" })); // during the reload
  assert.strictEqual(other.els.status.textContent, "Reconnecting...");
  assert.strictEqual(other.state.busy, true);
  assert.strictEqual(other.state.statuses.get(key), "CLEAN");
  for (const k of ["m", "s", "z", "ArrowRight", "ArrowLeft", "c", "d"]) await other.key(k);
  await other.key("M", true);
  for (const id of ["clean", "dirty", "prev", "next", "undo"]) await other.click(id);
  assert.deepStrictEqual(other.pending.map((r) => r.path), ["/current_pass"], "no key or button sends a request");
  await other.respond("/current_pass", other.json(200, { pass: 1 }));
  await other.key("m");
  assert.deepStrictEqual(other.pending.map((r) => r.path), ["/manifest"]);
};

tests["a failure of a request sent before the loss leaves a reconnected page live"] = async () => {
  const page = makePage({ manifest, statuses });
  const first = await loseWithUndoOut(page);
  await page.click("reconnect");
  await reload(page, { ...statuses, [first]: "CLEAN" });
  await page.fail("/undo");
  assert.strictEqual(page.els.status.textContent, "Reconnected");
  assert.strictEqual(page.state.dead, false);
  assert.strictEqual(page.els.reconnect.hidden, true);
  const key = page.state.items[0].key;
  await page.image200(key, [100, 50]);
  await page.dwell();
  await page.key("d");
  assert.deepStrictEqual(page.marks().at(-1).keys, [key]);
};

tests["a network failure after a 401 keeps token rejected and offers no Reconnect"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  page.els.grid.fire("contextlost");
  page.els.grid.fire("contextrestored"); // three image fetches out
  await page.respond("/image?key=a", page.json(401, {}));
  await page.fail("/image?key=b");
  assert.strictEqual(page.els.status.textContent, "token rejected (server restarted?) - open the new URL");
  assert.strictEqual(page.els.reconnect.hidden, true);
  await page.key("r");
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/image?key=c"], "nothing new sent");
};

tests["a failed Reconnect keeps the landing grid for the next"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.key("ArrowRight");
  await page.fail("/image?key=c");
  await page.click("reconnect");
  await page.fail("/current_pass");
  await page.click("reconnect");
  await reload(page, statuses);
  await page.respond("/grids", page.json(200, TWO_GRIDS)); // [a, b] first, the fuller
  await page.image200("c", [200, 200]);
  assert.deepStrictEqual(page.unhidden.at(-1).keys, ["c"]);
};

tests["a server error or bad reply during Reconnect shows it and offers Reconnect again"] = async () => {
  for (const reply of [{ status: 500, ok: false }, { status: 200, ok: true, json: async () => ({}) }]) {
    const page = makePage({ manifest, statuses });
    await page.boot();
    await page.key("ArrowRight");
    await page.fail("/image?key=" + page.state.items[1].key);
    await page.click("reconnect");
    await page.respond("/current_pass", page.json(200, { pass: 1 }));
    await page.respond("/manifest", { headers: { get: () => "application/json" }, ...reply });
    assert.strictEqual(page.els.status.textContent, reply.status === 500 ? "server error (HTTP 500)" : "unexpected reply from the server");
    assert.strictEqual(page.els.reconnect.hidden, false);
    assert.strictEqual(page.state.busy, false);
    await page.key("m");
    assert.deepStrictEqual(page.pending, [], "stopped until a Reconnect");
    await page.key("r");
    await reload(page, statuses);
    assert.strictEqual(page.els.status.textContent, "Reconnected");
    assert.strictEqual(page.state.items.length, 3);
  }
};

tests["Reconnect in grid mode keeps the batch and the rotation"] = async () => {
  const rows = manifest.concat([{ key: "d", batch: "b2" }, { key: "e", batch: "b2" }]);
  const all = { ...statuses, d: "UNREVIEWED", e: "UNREVIEWED" };
  const page = makePage({ manifest: rows, statuses: all });
  await page.boot();
  await page.enterGrid(all, true); // M: rotation never, batch b1
  await page.respond("/grids", page.json(200, PLAN));
  await page.key("b");
  await page.respond("/statuses?pass=1", page.json(200, all));
  const de = { grids: [[place("d", 0), place("e", 100)]], left_out: [] };
  await page.respond("/grids", page.json(200, de));
  await page.fail("/image?key=d");
  await page.click("reconnect");
  await reload(page, all, 1, rows);
  assert.deepStrictEqual(page.grids.sent.at(-1), { keys: ["d", "e"], width: 800, height: 600, rotation: "never" });
  assert.strictEqual(page.els.mode.textContent, "Grid · never");
};

tests["Reconnect with a repack pending lands on the grid shown at the resize"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.key("ArrowRight");
  await page.image200("c", [200, 200]);
  await page.dwell();
  await page.key("d");
  await page.resize(500, 300); // the repack waits for the mark
  await page.fail("/mark");
  await page.advance(1000);
  await page.click("reconnect");
  await reload(page, statuses);
  assert.deepStrictEqual(page.grids.sent.at(-1).width, 1000);
  await page.respond("/grids", page.json(200, TWO_GRIDS)); // [a, b] first, the fuller
  await page.image200("c", [200, 200]);
  assert.deepStrictEqual(page.unhidden.at(-1).keys, ["c"]);
};

tests["a body that fails mid-read is a lost connection, and Reconnect recovers"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("m");
  await page.respond("/statuses?pass=1", { status: 200, ok: true, headers: { get: () => "application/json" },
    json: async () => { throw new TypeError("network error"); } });
  assert.strictEqual(page.els.status.textContent, LOST);
  assert.strictEqual(page.els.reconnect.hidden, false);
  await page.key("r");
  await reload(page, statuses);
  assert.strictEqual(page.els.status.textContent, "Reconnected");
};

tests["while stopped, a /grids 503 retry is not sent"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("ArrowRight"); // an image out
  const image = page.pending[0].path;
  await page.enterGrid();
  await page.respond("/grids", page.json(503, null));
  await page.fail(image);
  assert.strictEqual(page.els.status.textContent, LOST);
  await page.advance(10000);
  assert.deepStrictEqual(page.pending, []);
  assert.strictEqual(page.grids.sent.length, 1);
};

tests["while stopped, a grid's remaining images are not fetched"] = async () => {
  const rows = ["a", "b", "c", "d", "e"].map((key) => ({ key, batch: "b1" }));
  const all = UNREVIEWED(["a", "b", "c", "d", "e"]);
  const page = makePage({ manifest: rows, statuses: all });
  await page.boot();
  await page.enterGrid(all);
  await page.respond("/grids", page.json(200, { grids: [rows.map((row, i) => place(row.key, i * 100))], left_out: [] }));
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["a", "b", "c", "d"].map((k) => "/image?key=" + k));
  await page.fail("/image?key=a");
  await page.image200("b", [200, 200]);
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/image?key=c", "/image?key=d"], "e is never asked for");
};

// A 200 JSON reply whose body settles only when told to.
function lateBody() {
  let settle;
  const body = new Promise((resolve, reject) => { settle = { resolve, reject }; });
  return { reply: { status: 200, ok: true, headers: { get: () => "application/json" }, json: () => body }, settle };
}

tests["a stale reply's status or late body changes nothing"] = async () => {
  const page = makePage({ manifest, statuses });
  await loseWithUndoOut(page);
  await page.click("reconnect");
  await page.respond("/undo", page.json(401, {})); // sent before the loss
  assert.strictEqual(page.els.status.textContent, "Reconnecting...");
  assert.strictEqual(page.state.token, "TOKEN");
  await reload(page, statuses);
  assert.strictEqual(page.els.status.textContent, "Reconnected");
  for (const outcome of ["resolve", "reject"]) {
    const other = makePage({ manifest, statuses });
    await other.boot();
    await other.dwell();
    const first = other.state.items[0].key;
    await other.key("c");
    await other.respond("/mark", other.json(200, { [first]: "CLEAN" }));
    const shown = other.state.items[other.state.index].key;
    await other.key("z");
    const late = lateBody();
    await other.respond("/undo", late.reply); // headers in, body still coming
    await other.fail("/image?key=" + shown);
    await other.click("reconnect");
    await reload(other, { ...statuses, [first]: "CLEAN" });
    const marked = plain(other.state.marked);
    if (outcome === "resolve") late.settle.resolve({ [first]: "UNREVIEWED" });
    else late.settle.reject(new TypeError("network error"));
    await other.advance(0);
    assert.strictEqual(other.els.status.textContent, "Reconnected", outcome);
    assert.strictEqual(other.state.dead, false, outcome);
    assert.strictEqual(other.state.statuses.get(first), "CLEAN", outcome);
    assert.deepStrictEqual(plain(other.state.marked), marked, outcome);
  }
};

// Single mode with the first item shown again while the second's fetch is still out, then `act`
// sends a request; the second image's fetch fails (Lost) and Reconnect is pressed.
async function loseWithRequestOut(page, act) {
  await page.boot();
  const [first, second] = page.state.items.map((item) => item.key);
  await page.key("ArrowRight");
  await page.key("ArrowLeft");
  await page.image200(first, [100, 50]);
  await page.dwell();
  await act(page);
  await page.fail("/image?key=" + second);
  await page.click("reconnect");
  return first;
}

async function assertReloadUntouched(page) {
  assert.strictEqual(page.state.busy, true);
  assert.strictEqual(page.els.status.textContent, "Reconnecting...");
  await page.key("m");
  await page.key("s");
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/current_pass"]);
  assert.deepStrictEqual(plain(page.state.marked), []);
}

tests["a /mark sent before the loss, answered during Reconnect, leaves the reload alone"] = async () => {
  const page = makePage({ manifest, statuses });
  const first = await loseWithRequestOut(page, (p) => p.key("c"));
  await page.respond("/mark", page.json(200, { [first]: "CLEAN" }));
  await assertReloadUntouched(page);
  assert.strictEqual(page.state.statuses.get(first), "UNREVIEWED");
};

tests["a mode switch sent before the loss, answered during Reconnect, leaves the reload alone"] = async () => {
  const page = makePage({ manifest, statuses });
  await loseWithRequestOut(page, (p) => p.key("m"));
  await page.respond("/statuses?pass=1", page.json(200, statuses));
  await assertReloadUntouched(page);
  assert.strictEqual(page.state.mode, "single");
};

tests["a stop at startup other than a lost connection offers no Reconnect"] = async () => {
  for (const reply of [{ status: 500, ok: false }, { status: 200, ok: true, json: async () => ({}) }]) {
    const page = makePage({ manifest, statuses });
    await page.respond("/current_pass", { headers: { get: () => "application/json" }, ...reply });
    assert.strictEqual(page.state.dead, true);
    assert.strictEqual(page.els.reconnect.hidden, true);
    await page.key("r");
    await page.click("reconnect");
    assert.deepStrictEqual(page.pending, []);
  }
};

tests["a grid image decoded after the loss starts no further fetch"] = async () => {
  const rows = ["a", "b", "c", "d", "e"].map((key) => ({ key, batch: "b1" }));
  const all = UNREVIEWED(["a", "b", "c", "d", "e"]);
  const page = makePage({ manifest: rows, statuses: all });
  await page.boot();
  await page.enterGrid(all);
  let release;
  page.bitmapGate = new Promise((resolve) => { release = resolve; });
  await page.respond("/grids", page.json(200, { grids: [rows.map((row, i) => place(row.key, i * 100))], left_out: [] }));
  await page.image200("b", [200, 200]); // fetched before the loss, still decoding
  await page.fail("/image?key=a");
  release();
  await page.advance(0);
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/image?key=c", "/image?key=d"], "e is never asked for");
};

// ---- Done with this server (q) ----

const TOKEN_REJECTED = "token rejected (server restarted?) - open the new URL";

// Everything the waiting page must have let go of and kept, that nothing is sent, and that
// only Reconnect is offered.
async function assertWaiting(page) {
  const { els, state } = page;
  assert.strictEqual(els.grid.width, 0);
  assert.strictEqual(els.grid.hidden, true);
  assert.strictEqual(els.image.hidden, true);
  assert.strictEqual(els.placeholder.hidden, true);
  assert.deepStrictEqual(page.removedAttrs.filter(([id]) => id === "image").pop(), ["image", "src"]);
  assert.strictEqual(state.objectUrl, null);
  assert.strictEqual(page.storage.get("token"), "TOKEN");
  assert.strictEqual(page.storage.get("reviewer"), "Jane");
  assert.strictEqual(state.token, "TOKEN");
  assert.strictEqual(state.reviewer, "Jane");
  assert.strictEqual(els.reviewer.value, "Jane");
  assert.deepStrictEqual([state.items.length, state.statuses.size, state.marked.length, state.manifest.length],
    [0, 0, 0, 0]);
  assert.strictEqual(state.gridCache, null);
  for (const id of ["reviewer-chip", "clean", "dirty", "prev", "next", "undo", "done"]) {
    assert.strictEqual(els[id].disabled, true, id + " disabled");
  }
  assert.strictEqual(els.reconnect.hidden, false);
  assert.strictEqual(els.message.hidden, false);
  assert.strictEqual(els.message.textContent, "Done. Your marks are saved. " + NEXT_SERVE);
  assert.strictEqual(els.status.textContent, "Done; waiting for the next serve");
  const sent = page.pending.length;
  const posted = page.posts.length;
  await page.dwell();
  await page.advance(5000);
  await page.resize(500, 400);
  for (const key of ["c", "d", "z", "m", "s", "b", "q", "ArrowRight"]) await page.key(key);
  assert.strictEqual(page.pending.length, sent, "nothing new is sent");
  assert.strictEqual(page.posts.length, posted, "nothing is posted");
  assert.strictEqual(els.status.textContent, "Done; waiting for the next serve");
}

// The next server: another work directory's batch, on pass 2.
const NEXT_ROWS = ["x", "y"].map((key) => ({ key, batch: "n1" }));
const NEXT_STATUSES = UNREVIEWED(["x", "y"]);

tests["q in single mode waits for the next server: stale replies change nothing, all freed"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.dwell();
  const shown = page.state.items[page.state.index].key;
  await page.key("c"); // /mark in flight
  await page.key("q");
  assert.deepStrictEqual(page.revoked, ["blob:x"]);
  await assertWaiting(page);
  await page.respond("/mark", page.json(200, { [shown]: "CLEAN" }));
  assert.strictEqual(page.state.statuses.size, 0);
  assert.strictEqual(page.state.marked.length, 0);
  assert.deepStrictEqual(page.pending.map((r) => r.path), []);
  assert.strictEqual(page.els.status.textContent, "Done; waiting for the next serve");
  assert.strictEqual(page.els.reconnect.hidden, false);
};

tests["q in grid mode, with grid images in flight"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, PLAN));
  await page.image200("a", [200, 200]);
  assert(page.els.grid.width > 0);
  await page.key("q");
  assert.strictEqual(page.els.grid.width, 0);
  await page.image200("b", [200, 200]); // stale
  await page.advance(5000);
  assert.deepStrictEqual(page.pending.map((r) => r.path).sort(), ["/image?key=c"], "no new fetch; c was already out");
  await page.respond("/image?key=c", page.image("c", [200, 200]));
  assert.strictEqual(page.els.grid.hidden, true);
  assert.strictEqual(page.els.grid.width, 0);
  assert(page.bitmaps.every((bitmap) => bitmap.closed), "every decoded bitmap is closed");
  await assertWaiting(page);
};

tests["q while a grid layout is requested or retrying sends nothing more"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(503, {}));
  await page.key("q");
  await page.advance(5000);
  assert.strictEqual(page.grids.sent.length, 1);
  await assertWaiting(page);
};

tests["q after a lost connection waits, still offering Reconnect"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.dwell();
  await page.key("c");
  await page.fail("/mark");
  assert.strictEqual(page.els.reconnect.hidden, false);
  await page.key("q");
  await assertWaiting(page);
};

tests["Reconnect after q loads the next server's pass, first item, fresh dwell, nothing to undo"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.dwell();
  const first = page.state.items[0].key;
  await page.key("c");
  await page.respond("/mark", page.json(200, { [first]: "CLEAN" }));
  assert.deepStrictEqual(plain(page.state.marked), [[first]]);
  await page.image200(page.state.items[page.state.index].key, [100, 50]);
  await page.key("q");
  await page.key("r");
  assert.strictEqual(page.els.status.textContent, "Reconnecting...");
  assert.strictEqual(page.els.reconnect.hidden, true);
  assert.strictEqual(page.els.message.hidden, true, "the done notice is gone");
  await page.click("reconnect");
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/current_pass"], "one Reconnect in flight");
  await reload(page, NEXT_STATUSES, 2, NEXT_ROWS);
  assert.strictEqual(page.els.status.textContent, "Reconnected; now on pass 2");
  assert(page.els.progress.textContent.startsWith("Pass 2 · "));
  assert.strictEqual(page.state.index, 0);
  const key = page.state.items[0].key;
  assert(["x", "y"].includes(key));
  assert.strictEqual(page.els.where.textContent, "n1 · " + key);
  await page.image200(key, [100, 50]);
  await page.key("c");
  await page.paint();
  await page.advance(100);
  await page.key("d");
  assert.strictEqual(page.marks().length, 1, "no verdict before a fresh dwell");
  await page.advance(100);
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo");
  assert(!page.posts.some((post) => post.path === "/undo"));
  for (const id of ["reviewer-chip", "undo", "done"]) assert.strictEqual(page.els[id].disabled, false, id);
  await page.key("c");
  assert.deepStrictEqual(page.marks()[1], { keys: [key], status: "CLEAN", pass: 2, reviewer: "Jane", mode: "single" });
  assert.strictEqual(page.els.reconnect.hidden, true);
};

tests["Reconnect after q in grid mode lays out the next server's batch, landing on its first grid"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  await page.key("q");
  await page.key("r");
  await reload(page, NEXT_STATUSES, 2, NEXT_ROWS);
  assert.strictEqual(page.els.status.textContent, "Reconnected; now on pass 2");
  assert.deepStrictEqual(page.grids.sent.at(-1), { keys: ["x", "y"], width: 800, height: 600, rotation: "auto" });
  await page.respond("/grids", page.json(200, { grids: [[place("x", 0)], [place("y", 0)]], left_out: [] }));
  assert.strictEqual(page.state.index, 0, "the old grid's key is absent: the first");
  await page.image200(page.state.items[0].keys[0], [200, 200]);
  assert.strictEqual(page.els.grid.hidden, false);
  assert(page.els.progress.textContent.startsWith("Pass 2 · n1 (1/1) · "));
};

tests["a Reconnect after q answered 401 shows token rejected and no button"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("q");
  await page.click("reconnect");
  await page.respond("/current_pass", page.json(401, {}));
  assert.strictEqual(page.els.status.textContent, TOKEN_REJECTED);
  assert.strictEqual(page.els.reconnect.hidden, true);
  assert.strictEqual(page.storage.has("token"), false);
  await page.key("r");
  await page.click("reconnect");
  assert.deepStrictEqual(page.pending, []);
};

tests["q after a rejected token frees the page and offers no Reconnect"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("ArrowRight");
  await page.respond("/image?key=" + page.state.items[1].key, page.json(401, {}));
  await page.key("q");
  assert.strictEqual(page.state.items.length, 0);
  assert.strictEqual(page.els.status.textContent, TOKEN_REJECTED);
  assert.strictEqual(page.els.reconnect.hidden, true);
  await page.key("r");
  assert.deepStrictEqual(page.pending, []);
};

tests["q typed into the reviewer field is a letter; Q with Caps Lock waits; a held q acts once"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("q", false, page.els.reviewer);
  assert.strictEqual(page.state.waiting, false);
  await page.keyRepeat("q");
  assert.strictEqual(page.state.waiting, false, "a repeat alone does nothing");
  await page.key("Q");
  assert.strictEqual(page.state.waiting, true);
  const revoked = page.revoked.length;
  const epoch = page.state.epoch;
  await page.key("q");
  await page.keyRepeat("q");
  assert.strictEqual(page.revoked.length, revoked, "again does nothing");
  assert.strictEqual(page.state.epoch, epoch);
};

tests["the Done button does the same, and a new token in the URL still reloads"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  assert.strictEqual(page.els.done.disabled, false);
  await page.click("done");
  await assertWaiting(page);
  await page.click("done");
  assert.strictEqual(page.state.waiting, true);
  await page.hashchange("#NEWTOKEN");
  assert.strictEqual(page.reloads, 1, "a new token pasted in still reloads");
};

tests["the end-of-pass screen says what next and offers Reconnect, only while idle"] = async () => {
  const page = makePage({ manifest: [{ key: "a", batch: "b1" }], statuses: { a: "UNREVIEWED" } });
  await page.boot();
  assert.strictEqual(page.els.reconnect.hidden, true);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN" }));
  assert.strictEqual(page.state.index, -1);
  assert.strictEqual(page.els.message.textContent, END_OF_PASS);
  assert.strictEqual(page.els.reconnect.hidden, false);
  assert.strictEqual(page.els.done.disabled, false);
  await page.key("z"); // /undo in flight: its reply must not be dropped, so no Reconnect meanwhile
  assert.strictEqual(page.els.reconnect.hidden, true);
  await page.key("r");
  await page.click("reconnect");
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/undo"]);
  await page.respond("/undo", page.json(200, { a: "UNREVIEWED" }));
  assert.strictEqual(page.state.index, 0, "the undone image is shown again");
  assert.strictEqual(page.els.reconnect.hidden, true, "not the end of the pass any more");
  await page.image200("a", [100, 50]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN" }));
  assert.strictEqual(page.els.reconnect.hidden, false);
  await page.key("r");
  assert.strictEqual(page.els.status.textContent, "Reconnecting...");
  assert.deepStrictEqual(plain(page.state.marked), []);
  await reload(page, NEXT_STATUSES, 2, NEXT_ROWS);
  assert.strictEqual(page.els.status.textContent, "Reconnected; now on pass 2");
  assert.strictEqual(page.state.statuses.get("a"), undefined);
  assert.strictEqual(page.state.index, 0);
  assert.strictEqual(page.els.reconnect.hidden, true);
  await page.image200(page.state.items[0].key, [100, 50]);
  await page.dwell();
  await page.key("d");
  assert.strictEqual(page.marks().at(-1).pass, 2);
};

// The end screen, then z whose reply cannot be read and whose resync answers `reply`.
async function stopOnEndScreen(page, reply) {
  await page.boot();
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN" }));
  assert.strictEqual(page.els.reconnect.hidden, false);
  await page.key("z");
  await page.respond("/undo", page.json(200, [1, 2]));
  await page.respond("/statuses?pass=1", reply);
}

tests["a stop on the end screen hides Reconnect: no token after a 401, or a stop that is not a loss"] = async () => {
  const one = { manifest: [{ key: "a", batch: "b1" }], statuses: { a: "UNREVIEWED" } };
  const page = makePage(one);
  await stopOnEndScreen(page, page.json(401, {}));
  assert.strictEqual(page.els.status.textContent, TOKEN_REJECTED);
  assert.strictEqual(page.state.token, null);
  assert.strictEqual(page.els.reconnect.hidden, true);
  assert.strictEqual(page.els.message.hidden, true);
  const other = makePage(one);
  await stopOnEndScreen(other, other.json(500, {}));
  assert.strictEqual(other.els.status.textContent, "unexpected reply from the server; reload the page");
  assert.strictEqual(other.els.reconnect.hidden, true);
  assert.strictEqual(other.els.message.hidden, true, "no stage hint to press Reconnect");
  await other.key("r");
  assert.deepStrictEqual(other.pending, []);
};

const ROWS2 = [{ key: "a", batch: "b1" }, { key: "b", batch: "b1" }, { key: "c", batch: "b2" }, { key: "d", batch: "b2" }];
const AB = { grids: [[place("a", 0), place("b", 100)]], left_out: [] };

// Grid mode on b1 of ROWS2 (or `rows`), its [a, b] grid marked CLEAN; `during` runs while the /mark is out.
async function markBatchOne(page, all, during = async () => {}) {
  await page.boot();
  await page.enterGrid(all);
  await page.respond("/grids", page.json(200, AB));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("c");
  await during();
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN" }));
}

tests["grid mode: a batch done while another has grid items says b, not the end of the pass"] = async () => {
  const all = UNREVIEWED(["a", "b", "c", "d"]);
  const page = makePage({ manifest: ROWS2, statuses: all });
  await markBatchOne(page, all);
  assert.strictEqual(page.state.index, -1);
  assert.strictEqual(page.els.message.textContent, "No todo images remaining - [b] next batch");
  assert.strictEqual(page.els.reconnect.hidden, true);
  await page.key("r");
  assert.deepStrictEqual(page.pending, []);
  await page.key("b");
  await page.respond("/statuses?pass=1", page.json(200, { ...all, a: "CLEAN", b: "CLEAN" }));
  assert.deepStrictEqual(page.grids.sent.at(-1).keys, ["c", "d"]);
};

tests["grid mode: a repack after a batch is done says b too"] = async () => {
  const all = UNREVIEWED(["a", "b", "c", "d"]);
  const page = makePage({ manifest: ROWS2, statuses: all });
  await markBatchOne(page, all, () => page.resize(500, 400)); // the repack waits for the mark
  await page.advance(300);
  assert.strictEqual(page.grids.sent.length, 1, "nothing left in b1 to pack");
  assert.strictEqual(page.els.message.textContent, "No todo images remaining - [b] next batch");
  assert.strictEqual(page.els.reconnect.hidden, true);
};

tests["grid mode: grids done while a FLAGGED image is left says s, not the end of the pass"] = async () => {
  const all = { a: "UNREVIEWED", b: "UNREVIEWED", c: "FLAGGED" };
  const page = makePage({ manifest, statuses: all });
  await markBatchOne(page, all);
  const held = "No grid items for pass 1; 1 FLAGGED/DIRTY image needs single-mode review - press [s]";
  assert.strictEqual(page.els.message.textContent, held);
  assert.strictEqual(page.els.reconnect.hidden, true);
  // Entering grid mode with only FLAGGED images left: buildGrids has no keys to pack
  const other = makePage({ manifest: ROWS2, statuses: { a: "CLEAN", b: "FLAGGED", c: "FLAGGED", d: "DIRTY" } });
  await other.boot();
  await other.enterGrid({ a: "CLEAN", b: "FLAGGED", c: "FLAGGED", d: "DIRTY" });
  assert.strictEqual(other.grids.sent.length, 0);
  assert.strictEqual(other.els.message.textContent,
    "No grid items for pass 1; 2 FLAGGED/DIRTY images need single-mode review - press [s]");
  assert.strictEqual(other.els.reconnect.hidden, true);
};

tests["grid mode: the last batch done is the end of the pass"] = async () => {
  const all = { a: "UNREVIEWED", b: "UNREVIEWED", c: "CLEAN", d: "DIRTY" };
  const page = makePage({ manifest: ROWS2, statuses: all });
  await markBatchOne(page, all);
  assert.strictEqual(page.els.message.textContent, END_OF_PASS);
  assert.strictEqual(page.els.reconnect.hidden, false);
};

tests["single mode: a list done with todo marked back meanwhile says s"] = async () => {
  const page = makePage({ manifest: [{ key: "a", batch: "b1" }, { key: "x", batch: "b1" }],
    statuses: { a: "UNREVIEWED", x: "CLEAN" } });
  await page.boot();
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN", x: "UNREVIEWED" })); // x changed by another client
  assert.strictEqual(page.els.message.textContent, "No todo images remaining - press [s] to reload the list");
  assert.strictEqual(page.els.reconnect.hidden, true);
};

tests["Reconnect after q starts at the first batch and grid, even on the same server"] = async () => {
  const rows = ROWS2.concat([{ key: "e", batch: "b2" }]);
  const all = UNREVIEWED(["a", "b", "c", "d", "e"]);
  const page = makePage({ manifest: rows, statuses: all, fixedOrder: true });
  await page.boot();
  await page.enterGrid(all);
  await page.respond("/grids", page.json(200, AB));
  await page.key("b");
  await page.respond("/statuses?pass=1", page.json(200, all));
  const cde = { grids: [[place("c", 0), place("d", 100)], [place("e", 0)]], left_out: [] };
  await page.respond("/grids", page.json(200, cde));
  await page.key("ArrowRight"); // on [e]
  assert.deepStrictEqual(page.state.items[page.state.index].keys, ["e"]);
  await page.key("q");
  await page.key("r");
  await reload(page, all, 1, rows); // the same keys: another work directory, or this one again
  assert.deepStrictEqual(page.grids.sent.at(-1).keys, ["a", "b"], "the first batch with grid items");
  await page.respond("/grids", page.json(200, AB));
  assert.strictEqual(page.state.index, 0);
  assert.strictEqual(page.state.batch, "b1");
  // q while a repack would land on [c]: Reconnect still starts at the first grid
  const other = makePage({ manifest, statuses, fixedOrder: true });
  await other.boot();
  await other.enterGrid();
  await other.respond("/grids", other.json(200, TWO_GRIDS));
  await other.key("ArrowRight"); // on [c]
  await other.resize(500, 300);
  await other.key("q");
  await other.key("r");
  await reload(other, statuses);
  await other.respond("/grids", other.json(200, TWO_GRIDS));
  assert.deepStrictEqual(other.state.items[other.state.index].keys, ["a", "b"]);
};

tests["Reconnect on the end screen of a server still up reloads the same pass"] = async () => {
  const page = makePage({ manifest, statuses: { a: "CLEAN", b: "DIRTY", c: "CLEAN" } });
  await page.boot(); // nothing to review: the end screen at once
  assert.strictEqual(page.els.message.textContent, END_OF_PASS);
  assert.strictEqual(page.els.reconnect.hidden, false);
  await page.click("reconnect");
  await reload(page, { a: "CLEAN", b: "DIRTY", c: "CLEAN" });
  assert.strictEqual(page.els.status.textContent, "Reconnected");
  assert.strictEqual(page.els.message.textContent, END_OF_PASS);
  assert.strictEqual(page.els.reconnect.hidden, false);
};

// ---- Another serve on the same socket path and token ----

const SERVER_CHANGED = "Server restarted or changed work directory - press Reconnect (r)";

// The page refused by a 412 from server B: stopped, Reconnect offered, nothing applied or sent since.
async function assertRefused(page, before) {
  assert.strictEqual(page.els.status.textContent, SERVER_CHANGED);
  assert.strictEqual(page.state.dead, true);
  assert.strictEqual(page.els.reconnect.hidden, false);
  assert.deepStrictEqual(plain([...page.state.statuses]), before, "nothing applied");
  assert.deepStrictEqual(page.pending, [], "nothing more sent");
  const posts = page.posts.length;
  await page.key("c");
  await page.key("z");
  assert.strictEqual(page.posts.length, posts, "no verdict or undo while stopped");
}

// Reconnect to server B (page.instance): its /current_pass names B, sent on every request after it.
async function reconnectToB(page, current, rows = manifest) {
  await page.click("reconnect");
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/current_pass"]);
  await page.respond("/current_pass", page.json(200, { pass: 1 }));
  assert.strictEqual(page.state.instance, "B");
  assert.deepStrictEqual(page.pending.map((r) => [r.path, r.instance]), [["/manifest", "B"]]);
  await page.respond("/manifest", page.json(200, rows));
  assert.deepStrictEqual(page.pending.map((r) => [r.path, r.instance]), [["/statuses?pass=1", "B"]]);
  await page.respond("/statuses?pass=1", page.json(200, current));
  assert.strictEqual(page.els.status.textContent, "Reconnected");
  assert.deepStrictEqual(plain(page.state.marked), [], "undo forgets what A recorded");
}

tests["every request after /current_pass carries the instance it named"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  assert.strictEqual(page.state.instance, "A");
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { [page.posts[0].body.keys[0]]: "CLEAN" }));
  assert.deepStrictEqual(page.pending.map((r) => r.instance), ["A"], "the next image");
  assert.strictEqual(page.posts[0].instance, "A");
};

tests["after a server swap a single-mode mark is refused (412) until Reconnect loads the new server"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.dwell();
  const shown = page.state.items[page.state.index].key;
  const before = plain([...page.state.statuses]);
  page.instance = "B"; // serve restarted on another work dir: same path, token and keys
  await page.key("c");
  assert.deepStrictEqual(page.posts.at(-1), { path: "/mark", instance: "A",
    body: { keys: [shown], status: "CLEAN", pass: 1, reviewer: "Jane", mode: "single" } });
  await page.respond("/mark", page.json(412, { error: "the page was loaded from another serve; reconnect" }));
  await assertRefused(page, before);
  await reconnectToB(page, { a: "UNREVIEWED", b: "DIRTY", c: "UNREVIEWED" });
  const next = page.state.items[page.state.index].key;
  assert.deepStrictEqual(page.pending.map((r) => [r.path, r.instance]), [["/image?key=" + next, "B"]]);
  await page.image200(next, [100, 50]);
  await page.key("c");
  assert.strictEqual(page.marks().length, 1, "no verdict before a fresh dwell");
  await page.dwell();
  await page.key("c");
  assert.deepStrictEqual(page.posts.at(-1), { path: "/mark", instance: "B",
    body: { keys: [next], status: "CLEAN", pass: 1, reviewer: "Jane", mode: "single" } });
};

tests["after a server swap z is refused (412) and Reconnect leaves nothing to undo"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.dwell();
  const first = page.state.items[0].key;
  await page.key("c");
  await page.respond("/mark", page.json(200, { [first]: "CLEAN" })); // recorded on A
  assert.deepStrictEqual(plain(page.state.marked), [[first]]);
  const before = plain([...page.state.statuses]);
  page.instance = "B";
  await page.key("z");
  assert.deepStrictEqual(page.posts.at(-1), { path: "/undo", instance: "A", body: { pass: 1, reviewer: "Jane" } });
  const image = page.pending.find((r) => r.path.startsWith("/image"));
  await page.respond("/undo", page.json(412, {}));
  await page.respond(image.path, page.image(image.path, [100, 50])); // stale once stopped: changes nothing
  await assertRefused(page, before);
  await reconnectToB(page, statuses);
  await page.image200(page.state.items[page.state.index].key, [100, 50]);
  await page.dwell();
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo");
  assert.strictEqual(page.posts.filter((post) => post.path === "/undo").length, 1);
};

tests["after a server swap a grid mark is refused (412) for every key; Reconnect lays out B's grids"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  await page.dwell();
  const before = plain([...page.state.statuses]);
  page.instance = "B";
  await page.key("c");
  assert.deepStrictEqual(page.posts.at(-1), { path: "/mark", instance: "A",
    body: { keys: ["a", "b", "c"], status: "CLEAN", pass: 1, reviewer: "Jane", mode: "grid" } });
  await page.respond("/mark", page.json(412, {}));
  await assertRefused(page, before);
  assert.strictEqual(page.state.mode, "grid");
  await reconnectToB(page, statuses);
  assert.deepStrictEqual(page.pending.map((r) => [r.path, r.instance]), [["/grids", "B"]]);
  await page.respond("/grids", page.json(200, PLAN));
  for (const key of ["a", "b", "c"]) {
    assert.strictEqual(page.pending.find((r) => r.path === "/image?key=" + key).instance, "B");
    await page.image200(key, [200, 200]);
  }
  await page.key("c");
  assert.strictEqual(page.marks().length, 1, "no verdict before a fresh dwell");
  await page.dwell();
  await page.key("c");
  assert.deepStrictEqual(page.posts.at(-1), { path: "/mark", instance: "B",
    body: { keys: ["a", "b", "c"], status: "CLEAN", pass: 1, reviewer: "Jane", mode: "grid" } });
};

tests["a 412 on a read stops the page the same way"] = async () => {
  for (const path of ["/image", "/statuses", "/grids"]) {
    const page = makePage({ manifest, statuses });
    await page.boot();
    if (path === "/image") {
      await page.key("ArrowRight");
      await page.respond("/image?key=" + page.state.items[1].key, page.json(412, {}));
    } else {
      await page.key("m");
      if (path === "/statuses") {
        await page.respond("/statuses?pass=1", page.json(412, {}));
      } else {
        await page.respond("/statuses?pass=1", page.json(200, statuses));
        await page.respond("/grids", page.json(412, {}));
      }
    }
    assert.strictEqual(page.els.status.textContent, SERVER_CHANGED, path);
    assert.strictEqual(page.els.reconnect.hidden, false, path);
    assert.deepStrictEqual(page.pending, [], path);
  }
};

tests["a /current_pass reply naming no instance is an unexpected reply"] = async () => {
  const page = makePage({ manifest, statuses });
  page.instance = null;
  await page.respond("/current_pass", page.json(200, { pass: 1 }));
  assert.strictEqual(page.els.status.textContent, "unexpected reply from the server");
  assert.strictEqual(page.state.dead, true);
  assert.deepStrictEqual(page.pending, []);
  // On Reconnect: offered again, and the old instance is not kept as B's
  const other = makePage({ manifest, statuses });
  await other.boot();
  await other.key("ArrowRight");
  await other.fail("/image?key=" + other.state.items[1].key);
  other.instance = null;
  await other.click("reconnect");
  await other.respond("/current_pass", other.json(200, { pass: 1 }));
  assert.strictEqual(other.els.status.textContent, "unexpected reply from the server");
  assert.strictEqual(other.els.reconnect.hidden, false);
  assert.deepStrictEqual(other.pending, []);
};


tests["a stale 412 (sent before a Reconnect) leaves the reconnected page alone"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("ArrowRight");
  const old = page.pending.find((r) => r.path.startsWith("/image"));
  assert.strictEqual(old.instance, "A");
  await page.key("q");
  page.instance = "B";
  await page.click("reconnect");
  await page.respond("/current_pass", page.json(200, { pass: 1 }));
  await page.respond("/manifest", page.json(200, manifest));
  await page.respond("/statuses?pass=1", page.json(200, statuses));
  assert.strictEqual(page.state.instance, "B");
  const status = page.els.status.textContent;
  await page.respond(old.path, page.json(412, {})); // B refusing A's request: old news
  assert.strictEqual(page.state.dead, false);
  assert.strictEqual(page.els.status.textContent, status);
  assert.strictEqual(page.els.reconnect.hidden, true);
};

// ---- The bar, the help and the name box ----

const NAME_NEEDED = "Enter your name (1-64 characters) to start reviewing";

tests["the bar's colour follows the item's status, neutral on the end screen, stopped or waiting"] = async () => {
  const page = makePage({ manifest, statuses: { a: "FLAGGED", b: "UNREVIEWED", c: "CLEAN" }, fixedOrder: true });
  const { bar, "item-status": word } = page.els;
  await page.boot();
  assert.strictEqual(bar.dataset.status, "FLAGGED");
  assert.strictEqual(word.textContent, "FLAGGED", "the status is written too, not only coloured");
  assert.strictEqual(word.hidden, false);
  await page.dwell();
  await page.key("d");
  await page.respond("/mark", page.json(200, { a: "DIRTY" }));
  assert.strictEqual(bar.dataset.status, "UNREVIEWED");
  await page.image200("b", [100, 50]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { b: "CLEAN" }));
  assert.strictEqual(page.state.index, -1);
  assert.strictEqual(bar.dataset.status, "", "the end screen is neutral");
  assert.strictEqual(word.hidden, true);
  await page.key("ArrowLeft");
  assert.strictEqual(bar.dataset.status, "CLEAN");
  await page.image200("b", [100, 50]);
  await page.key("ArrowLeft");
  assert.strictEqual(bar.dataset.status, "DIRTY");
  await page.fail("/image?key=a");
  assert.strictEqual(page.state.lost, true);
  assert.strictEqual(bar.dataset.status, "", "stopped: neutral");
  await page.key("q");
  assert.strictEqual(page.state.waiting, true);
  assert.strictEqual(bar.dataset.status, "", "waiting: neutral");

  const grid = makePage({ manifest, statuses });
  await showPlan(grid);
  assert.strictEqual(grid.els.bar.dataset.status, "UNREVIEWED");
  grid.state.statuses.set("b", "DIRTY");
  grid.els.reviewer.fire("input"); // renders
  assert.strictEqual(grid.els.bar.dataset.status, "DIRTY", "a grid takes gridStatus");
  assert.strictEqual(grid.els["item-status"].textContent, "DIRTY");
};

tests["the scale badge stands out below 100% only"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.dwell();
  assert.strictEqual(page.els.scale.textContent, "800%");
  assert.strictEqual(page.els.scale.classList.contains("low"), false);
  const grid = makePage({ manifest, statuses });
  await showPlan(grid);
  await grid.dwell();
  assert.strictEqual(grid.els.scale.textContent, "⚠ 50%");
  assert.strictEqual(grid.els.scale.classList.contains("low"), true);
};

tests["help opens with ?, h, H and its button, and closes with Escape or the same key"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  const { help } = page.els;
  const opens = [["?", "?"], ["h", "Escape"], ["H", "h"], ["?", "H"], ["h", "?"]];
  for (const [open, close] of opens) {
    await page.key(open);
    assert.strictEqual(help.hidden, false, open + " opens");
    assert.strictEqual(page.state.overlay, "help");
    await page.keyRepeat(close);
    assert.strictEqual(help.hidden, false, "a held key acts once");
    await page.key(close);
    assert.strictEqual(help.hidden, true, close + " closes");
    assert.strictEqual(page.state.overlay, null);
  }
  await page.click("help-button");
  assert.strictEqual(help.hidden, false);
  await page.click("help-button");
  assert.strictEqual(help.hidden, true);
  await page.keyRepeat("h");
  assert.strictEqual(help.hidden, true, "a repeat does not open it");
  await page.key("h", false, page.els.reviewer);
  assert.strictEqual(help.hidden, true, "h typed into the name is a letter");
};

// Every review key and button, none of which may act while an overlay is up.
async function tryEverything(page) {
  for (const key of ["c", "d", "z", "ArrowRight", "ArrowLeft", "m", "s", "b", "q", "r"]) await page.key(key);
  await page.key("M", true);
  for (const id of ["clean", "dirty", "undo", "prev", "next", "done", "reconnect"]) await page.click(id);
}

for (const overlay of ["help", "name"]) {
  tests["while the " + overlay + " overlay is up nothing acts; closing it restarts the dwell"] = async () => {
    const page = makePage({ manifest, statuses });
    await page.boot();
    await page.dwell();
    assert.strictEqual(page.els.clean.disabled, false);
    const index = page.state.index;
    await page.click(overlay === "help" ? "help-button" : "reviewer-chip");
    assert.strictEqual(page.state.overlay, overlay);
    assert.strictEqual(page.state.dwell, "none", "the dwell is cleared at once");
    for (const id of ["clean", "dirty", "undo", "prev", "next", "done", "reviewer-chip"]) {
      assert.strictEqual(page.els[id].disabled, true, id + " disabled");
    }
    assert.strictEqual(page.els["help-button"].disabled, overlay === "name");
    await tryEverything(page);
    await page.dwell();
    await page.resize(500, 400); // a resize under the overlay starts no dwell
    await page.dwell();
    assert.strictEqual(page.state.dwell, "none");
    assert.deepStrictEqual(page.pending, [], "nothing sent");
    assert.deepStrictEqual(page.posts, []);
    assert.deepStrictEqual([page.state.index, page.state.mode, page.state.waiting], [index, "single", false]);
    if (overlay === "help") {
      await page.key("Escape");
    } else {
      await page.nameKey("Escape");
      assert.strictEqual(page.state.reviewer, "Jane");
    }
    assert.strictEqual(page.state.overlay, null);
    await page.key("c");
    assert.deepStrictEqual(page.posts, [], "no verdict before a fresh dwell");
    await page.paint();
    assert.strictEqual(page.state.dwell, "running");
    await page.key("d");
    assert.deepStrictEqual(page.posts, [], "nor while it runs");
    await page.advance(200);
    await page.key("d");
    assert.deepStrictEqual(page.marks().map((m) => m.status), ["DIRTY"]);
  };
}

tests["r and Reconnect wait while an overlay is up"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("ArrowRight");
  await page.fail(page.pending[0].path);
  assert.strictEqual(page.els.reconnect.hidden, false);
  assert.strictEqual(page.els.done.hidden, true, "Reconnect takes Done's place");
  await page.key("h");
  assert.strictEqual(page.els.reconnect.disabled, true);
  await page.key("r");
  await page.click("reconnect");
  assert.deepStrictEqual(page.pending, []);
  await page.key("h");
  await page.key("r");
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/current_pass"]);
};

tests["startup without a name shows the name box, and nothing is judged until a valid one is set"] = async () => {
  const page = makePage({ manifest, statuses, reviewer: null });
  await page.boot();
  const { reviewer } = page.els;
  assert.strictEqual(page.els["name-box"].hidden, false);
  assert.strictEqual(page.state.overlay, "name");
  assert.strictEqual(page.state.reviewer, null);
  assert.strictEqual(page.els.status.textContent, NAME_NEEDED);
  assert.strictEqual(page.els["reviewer-name"].textContent, "Name");
  await page.dwell();
  assert.strictEqual(page.state.dwell, "none");
  await tryEverything(page);
  await page.key("Escape");
  await page.nameKey("Escape");
  assert.strictEqual(page.state.overlay, "name", "no name to go back to: Escape keeps the box up");
  for (const name of ["", "   ", "x".repeat(65)]) {
    await page.typeName(name);
    assert.strictEqual(page.els["name-ok"].disabled, true, JSON.stringify(name));
    assert.strictEqual(reviewer.classList.contains("invalid"), true);
    await page.nameKey("Enter");
    await page.click("name-ok");
    assert.strictEqual(page.state.overlay, "name");
  }
  assert.strictEqual(page.state.reviewer, null);
  assert.strictEqual(page.storage.has("reviewer"), false);
  await page.typeName("Ann");
  assert.strictEqual(page.els["name-ok"].disabled, false);
  assert.strictEqual(reviewer.classList.contains("invalid"), false);
  await page.nameKey("Enter");
  assert.strictEqual(page.state.overlay, null);
  assert.strictEqual(page.els["name-box"].hidden, true);
  assert.strictEqual(page.state.reviewer, "Ann");
  assert.strictEqual(page.storage.get("reviewer"), "Ann");
  assert.strictEqual(page.els.status.textContent, "");
  assert.strictEqual(page.els["reviewer-name"].textContent, "Ann");
  assert.deepStrictEqual(page.posts, []);
  await page.key("c");
  assert.deepStrictEqual(page.posts, [], "a fresh dwell first");
  await page.dwell();
  await page.key("c");
  assert.deepStrictEqual(page.marks().map((m) => m.reviewer), ["Ann"]);
};

tests["the name box keeps the keys: focus lost goes back to the field, an IME Enter waits, accepting leaves it"] = async () => {
  const page = makePage({ manifest, statuses, reviewer: null });
  await page.boot();
  const doc = () => page.document;
  assert.strictEqual(doc().activeElement, page.els.reviewer, "the box opens with the field focused");
  page.els.reviewer.blur(); // a click elsewhere
  assert.strictEqual(doc().activeElement, null);
  await page.key("x");
  assert.strictEqual(doc().activeElement, page.els.reviewer, "a key typed elsewhere goes back to the field");
  assert.strictEqual(page.state.overlay, "name");
  await page.typeName("Ann");
  page.els.reviewer.fire("keydown", { key: "Enter", isComposing: true });
  assert.strictEqual(page.state.overlay, "name", "an input method's Enter does not accept");
  assert.strictEqual(page.state.reviewer, null);
  page.els.reviewer.blur();
  await page.key("Enter"); // focus elsewhere: Enter still accepts
  assert.strictEqual(page.state.overlay, null);
  assert.strictEqual(page.state.reviewer, "Ann");
  await page.click("reviewer-chip");
  assert.strictEqual(doc().activeElement, page.els.reviewer);
  await page.typeName("Bob");
  await page.nameKey("Enter");
  assert.strictEqual(page.state.reviewer, "Bob");
  assert.notStrictEqual(doc().activeElement, page.els.reviewer, "accepting takes focus out of the field");
};

tests["the reviewer chip reopens the name box; Escape keeps the old name, OK takes the new one"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  assert.strictEqual(page.state.overlay, null, "a stored name needs no box");
  assert.strictEqual(page.els["reviewer-name"].textContent, "Jane");
  await page.click("reviewer-chip");
  assert.strictEqual(page.els["name-box"].hidden, false);
  assert.strictEqual(page.els.reviewer.value, "Jane");
  assert.strictEqual(page.state.overlay, "name");
  await page.typeName("Bob");
  await page.nameKey("Escape");
  assert.strictEqual(page.state.overlay, null);
  assert.strictEqual(page.state.reviewer, "Jane");
  assert.strictEqual(page.els.reviewer.value, "Jane");
  assert.strictEqual(page.storage.get("reviewer"), "Jane");
  await page.click("reviewer-chip");
  await page.typeName(" ");
  await page.key("Escape"); // focus elsewhere: the page's Escape cancels too
  assert.strictEqual(page.state.overlay, null);
  assert.strictEqual(page.state.reviewer, "Jane");
  await page.click("reviewer-chip");
  await page.typeName("Bob");
  await page.click("name-ok");
  assert.strictEqual(page.state.overlay, null);
  assert.strictEqual(page.state.reviewer, "Bob");
  assert.strictEqual(page.storage.get("reviewer"), "Bob");
  assert.strictEqual(page.els["reviewer-name"].textContent, "Bob");
  await page.dwell();
  await page.key("d");
  assert.deepStrictEqual(page.marks().map((m) => m.reviewer), ["Bob"]);
};

// The status message takes the bar's centre, so a move to another item clears it
// (mode and progress show again), but not one set by the action that made the move.
tests["a run of c and d keeps the progress in view: a mark that moves on says nothing"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.dwell();
  const [first, second, third] = page.state.items.map((item) => item.key);
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo");
  await page.key("c");
  await page.respond("/mark", page.json(200, { [first]: "CLEAN" }));
  assert.strictEqual(page.state.index, 1);
  assert.strictEqual(page.els.status.textContent, "", "the move clears the refusal, and the mark adds nothing");
  assert(page.els.progress.textContent.startsWith("Pass 1 · "));
  await page.image200(second, [100, 50]);
  await page.dwell();
  await page.key("d");
  await page.respond("/mark", page.json(200, { [second]: "DIRTY" }));
  assert.strictEqual(page.state.index, 2);
  assert.strictEqual(page.els.status.textContent, "");
  await page.image200(third, [100, 50]);
  assert.strictEqual(page.els.status.textContent, "");
};

tests["a refusal lasts through a repack and a redraw; the reviewer's next move clears it"] = async () => {
  const page = makePage({ manifest, statuses });
  await showPlan(page);
  await page.dwell();
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo");
  page.context.lost = true;
  page.els.grid.fire("contextlost");
  page.context.lost = false;
  page.els.grid.fire("contextrestored");
  for (const key of ["a", "b", "c"]) await page.image200(key, [200, 200]);
  assert.strictEqual(page.els.grid.hidden, false, "redrawn");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo", "a redraw is not the reviewer's move");
  await page.resize(500, 300);
  await page.advance(300);
  await page.respond("/grids", page.json(200, PLAN));
  for (const key of ["a", "b", "c"]) await page.image200(key, [200, 200]);
  assert.strictEqual(page.els.grid.hidden, false, "repacked");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo", "nor is a repack");
  await page.key("s");
  await page.respond("/statuses?pass=1", page.json(200, statuses));
  assert.strictEqual(page.els.status.textContent, "", "a mode switch is");
};

for (const [status, message] of [[409, "grid contains an image already marked DIRTY - review it in single mode"], [500, "mark failed (HTTP 500)"]]) {
  tests["a " + status + " on a mark with a resize meanwhile: the repacked grid keeps the message"] = async () => {
    const page = makePage({ manifest, statuses });
    await showPlan(page);
    await page.dwell();
    await page.key("c");
    await page.resize(500, 300);
    await page.advance(1000);
    await page.respond("/mark", page.json(status, { error: "x" }));
    if (status === 409) await page.respond("/statuses?pass=1", page.json(200, statuses));
    assert.strictEqual(page.els.status.textContent, message);
    await page.advance(300);
    await page.respond("/grids", page.json(200, PLAN));
    for (const key of ["a", "b", "c"]) await page.image200(key, [200, 200]);
    assert.strictEqual(page.els.grid.hidden, false, "the repacked grid is up");
    assert.strictEqual(page.els.status.textContent, message);
  };
}

tests["another client's undone mark, with no move, is cleared by the next mark's move"] = async () => {
  const rows = manifest.concat([{ key: "x", batch: "b1" }]);
  const page = makePage({ manifest: rows, statuses: { ...statuses, x: "CLEAN" }, fixedOrder: true });
  await page.boot();
  await page.dwell();
  const [first, second, third] = page.state.items.map((item) => item.key);
  await page.key("c");
  await page.respond("/mark", page.json(200, { [first]: "CLEAN" }));
  await page.image200(second, [100, 50]);
  await page.dwell();
  await page.key("z");
  await page.respond("/undo", page.json(200, { x: "UNREVIEWED" })); // x is not in this page's list: no move
  assert.strictEqual(page.els.status.textContent, "Undid another client's mark: x is UNREVIEWED");
  assert.strictEqual(page.state.index, 1);
  await page.key("d");
  await page.respond("/mark", page.json(200, { [second]: "DIRTY" }));
  assert.strictEqual(page.state.index, 2);
  assert.strictEqual(page.els.status.textContent, "");
  await page.image200(third, [100, 50]);
  assert.strictEqual(page.els.status.textContent, "");
};

tests["an undo's message survives the move it makes, a grid demoted on the way included"] = async () => {
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
  await page.key("z");
  await page.respond("/undo", page.json(200, UNREVIEWED(["a", "b"])));
  assert.strictEqual(page.els.status.textContent, "Undone: grid of 2 images");
  for (const key of ["a", "b"]) await page.respond("/image?key=" + key, page.image(key, [200, 200], 404));
  assert.deepStrictEqual(page.state.items.map((item) => item.kind), ["grid", "single", "single"], "nothing drawn: demoted");
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/image?key=c"], "what holds its place now is shown");
  assert.strictEqual(page.els.status.textContent, "Undone: grid of 2 images", "the same move's second show keeps it");
};

tests["a stop's message persists until Reconnect; Reconnect's survives its own show"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.key("ArrowRight");
  await page.fail("/image?key=" + page.state.items[1].key);
  await page.advance(10000);
  await page.key("ArrowRight");
  assert.strictEqual(page.els.status.textContent, LOST);
  await page.click("reconnect");
  await reload(page, { a: "UNREVIEWED", b: "CLEAN", c: "CLEAN" }, 2);
  await page.image200("a", [100, 50]);
  await page.dwell();
  assert.strictEqual(page.els.status.textContent, "Reconnected; now on pass 2");
  await page.key("ArrowLeft");
  assert.strictEqual(page.els.status.textContent, "Start of list", "a refusal to move, not a move");
};

// As the pygame client: a mark that went through keeps the item up, the bar in its new
// status, for MARK_FLASH_MS, then moves on; nothing else acts meanwhile.
for (const [key, verdict] of [["c", "CLEAN"], ["d", "DIRTY"]]) {
  tests[key + " shows the marked status for " + MARK_FLASH_MS + " ms, then moves on; nothing acts meanwhile"] = async () => {
    const page = makePage({ manifest, statuses });
    page.autoFlash = false;
    await page.boot();
    await page.dwell();
    const [first, second] = page.state.items.map((item) => item.key);
    await page.key(key);
    await page.respond("/mark", page.json(200, { [first]: verdict }));
    assert.strictEqual(page.state.index, 0, "still on the marked item");
    assert.strictEqual(page.els.bar.dataset.status, verdict);
    assert.strictEqual(page.els["item-status"].textContent, verdict);
    for (const id of ["clean", "dirty", "undo", "prev", "next", "done"]) assert.strictEqual(page.els[id].disabled, true, id);
    for (const k of ["c", "d", "z", "ArrowRight", "ArrowLeft", "m", "s", "b", "q", "r"]) await page.key(k);
    for (const id of ["clean", "dirty", "undo", "next", "done", "reconnect"]) await page.click(id);
    assert.deepStrictEqual(page.posts.map((post) => post.path), ["/mark"], "no second verdict, no undo");
    assert.deepStrictEqual(page.pending, [], "no switch, no move, nothing sent");
    assert.strictEqual(page.state.waiting, false, "q waits too");
    await page.advance(MARK_FLASH_MS - 1);
    assert.strictEqual(page.state.index, 0);
    await page.advance(1);
    assert.strictEqual(page.state.index, 1, "moved on");
    assert.deepStrictEqual(page.pending.map((r) => r.path), ["/image?key=" + second]);
    assert.strictEqual(page.els.bar.dataset.status, "UNREVIEWED");
    assert.strictEqual(page.state.busy, false);
    await page.image200(second, [100, 50]);
    await page.dwell();
    await page.key("z");
    await page.respond("/undo", page.json(200, { [first]: "UNREVIEWED" }));
    assert.strictEqual(page.state.index, 0, "undo after the move works as before");
  };
}

tests["a grid's flash shows the grid's new status; the last grid moves on to the end screen"] = async () => {
  const page = makePage({ manifest, statuses });
  page.autoFlash = false;
  await showPlan(page);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN", c: "CLEAN" }));
  assert.strictEqual(page.els.bar.dataset.status, "CLEAN");
  assert.strictEqual(page.els.grid.hidden, false, "the marked grid stays up");
  await page.advance(MARK_FLASH_MS);
  assert.strictEqual(page.state.index, -1);
  assert.strictEqual(page.els.message.textContent, END_OF_PASS);
};

tests["a stop during the flash: no move, then or later"] = async () => {
  const page = makePage({ manifest, statuses });
  page.autoFlash = false;
  await page.boot();
  await page.dwell();
  const first = page.state.items[0].key;
  await page.key("c");
  await page.respond("/mark", page.json(200, { [first]: "CLEAN" }));
  page.run("lose()");
  await page.advance(1000);
  assert.strictEqual(page.state.index, 0);
  assert.deepStrictEqual(page.pending, []);
  assert.strictEqual(page.els.status.textContent, LOST);
  assert.strictEqual(page.els.reconnect.hidden, false);
};

tests["a resize during a grid's flash: no move; the repack lays out what is left"] = async () => {
  const page = makePage({ manifest, statuses });
  page.autoFlash = false;
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN" }));
  await page.resize(500, 300);
  await page.advance(MARK_FLASH_MS);
  assert.deepStrictEqual(page.pending, [], "the stale move on shows nothing");
  assert.strictEqual(page.state.busy, false);
  await page.advance(REPACK_MS);
  assert.deepStrictEqual(page.grids.sent[1].keys, ["c"]);
  await page.respond("/grids", page.json(200, { grids: [[place("c", 0)]], left_out: [] }));
  await page.image200("c", [200, 200]);
  assert.strictEqual(page.els.grid.hidden, false);
  assert.deepStrictEqual(page.unhidden.map((u) => u.keys), [["a", "b"], ["c"]]);
};

tests["a context loss during a grid's flash: no move; the redraw shows the marked grid"] = async () => {
  const page = makePage({ manifest, statuses });
  page.autoFlash = false;
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN" }));
  page.context.lost = true;
  page.els.grid.fire("contextlost");
  await page.advance(MARK_FLASH_MS);
  assert.deepStrictEqual(page.pending, []);
  page.context.lost = false;
  page.els.grid.fire("contextrestored");
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  assert.deepStrictEqual(page.state.items[page.state.index].keys, ["a", "b"]);
  assert.strictEqual(page.els.bar.dataset.status, "CLEAN");
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
