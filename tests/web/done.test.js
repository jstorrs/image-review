"use strict";
// Done with this server (q) and the next server's batch, against the stub DOM. Run with `node --test tests/web/*.test.js`.
const assert = require("assert");
const test = require("node:test");
const { makePage, manifest, statuses, place, PLAN, TWO_GRIDS, showPlan, NEXT_SERVE, END_OF_PASS, plain, UNREVIEWED, reload } = require("./page");

const tests = {};

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
  assert.deepStrictEqual(page.revoked, ["blob:x1"]);
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

// node:test runs these one after another and fails one whose promise is still pending when the event loop empties.
for (const [name, fn] of Object.entries(tests)) test(name, fn);
