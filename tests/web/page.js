"use strict";
// The stub-DOM page and the fixtures the browser-page test files share (not itself a test file).
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SCRIPTS = ["helpers.js", "app.js"].map((name) => path.join(__dirname, "..", "..", "src", "image_review", "web", name));

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
  const createdUrls = [];
  let nextUrl = 0;
  const removedAttrs = []; // [element id, attribute] of each removeAttribute
  const unhidden = []; // what the page held each time the canvas was unhidden

  function element(id) {
    const listeners = {};
    let hidden = id === "help" || id === "name-box" || id === "list-end"; // as index.html has them
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
    "prev", "next", "undo", "reconnect", "done", "list-end", "list-end-detail"];
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
    URL: {
      createObjectURL: () => { const url = "blob:x" + ++nextUrl; createdUrls.push(url); return url; },
      revokeObjectURL: (url) => revoked.push(url),
    },
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
  // Both scripts in one context, as the page runs them: app.js sees helpers.js's top-level names.
  // Named, so V8 coverage attributes lines to the files.
  for (const script of SCRIPTS) vm.runInContext(fs.readFileSync(script, "utf8"), ctx, { filename: script });
  vm.runInContext("globalThis.__state = state;", ctx);

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
    els, draws, bitmaps, grids, posts, unhidden, pending, context, storage, revoked, createdUrls, removedAttrs,
    shift: 0, bitmapGate: null, reloads: 0,
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

// m, then PLAN's grid drawn and shown (not yet painted).
async function showPlan(page) {
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, PLAN));
  for (const key of ["a", "b", "c"]) await page.image200(key, [200, 200]);
}

const NEXT_SERVE = "Stop serve (Ctrl-C), start the next one, then press Reconnect (r).";
const END_OF_PASS = "Pass 1: nothing left to review. " + NEXT_SERVE;

const plain = (value) => JSON.parse(JSON.stringify(value)); // across the vm realm
const UNREVIEWED = (keys) => Object.fromEntries(keys.map((key) => [key, "UNREVIEWED"]));
const LEFT_OUT = { grids: [[place("a", 0), place("b", 100)]], left_out: ["c"] };

const LOST = "Lost connection — your marks so far are saved on the server";

// Respond to Reconnect's three loads.
async function reload(page, current, pass = 1, rows = manifest) {
  await page.respond("/current_pass", page.json(200, { pass }));
  await page.respond("/manifest", page.json(200, rows));
  await page.respond("/statuses?pass=" + pass, page.json(200, current));
}

module.exports = {
  JPEG,
  MARK_FLASH_MS,
  REPACK_MS,
  makePage,
  manifest,
  statuses,
  place,
  PLAN,
  TWO_GRIDS,
  showPlan,
  NEXT_SERVE,
  END_OF_PASS,
  plain,
  UNREVIEWED,
  LEFT_OUT,
  LOST,
  reload,
};
