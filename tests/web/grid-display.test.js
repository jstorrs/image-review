"use strict";
// Grid mode display: placements, items, dwell and the grid repack, against the stub DOM. Run with `node --test tests/web/*.test.js`.
const assert = require("assert");
const test = require("node:test");
const { makePage, manifest, statuses, place, PLAN, TWO_GRIDS, showPlan, UNREVIEWED } = require("./page");

const tests = {};

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

tests["a batch over MAX_GRID_KEYS shows the too-large notice and sends no grid request"] = async () => {
  const rows = Array.from({ length: 1001 }, (_, i) => ({ key: "k" + i, batch: "b1" }));
  const all = Object.fromEntries(rows.map((row) => [row.key, "UNREVIEWED"]));
  const page = makePage({ manifest: rows, statuses: all });
  await page.boot();
  await page.enterGrid(all);
  assert.strictEqual(page.els.message.textContent, "Batch too large for grid mode; use single mode [s]");
  assert.strictEqual(page.els.message.hidden, false);
  assert.strictEqual(page.grids.sent.length, 0);
};

for (const [name, stage] of [["narrow", [100, 300]], ["short", [400, 100]]]) {
  tests["a " + name + " stage shows the window-too-small notice and sends no grid request"] = async () => {
    const page = makePage({ manifest, statuses, stage, dpr: 1 });
    await page.boot();
    await page.enterGrid();
    assert.strictEqual(page.els.message.textContent, "Window too small for grid mode");
    assert.strictEqual(page.els.message.hidden, false);
    assert.strictEqual(page.grids.sent.length, 0);
  };
}

// node:test runs these one after another and fails one whose promise is still pending when the event loop empties.
for (const [name, fn] of Object.entries(tests)) test(name, fn);
