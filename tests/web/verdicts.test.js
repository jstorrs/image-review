"use strict";
// Marking and undoing: verdict rules in grid and single mode, against the stub DOM. Run with `node --test tests/web/*.test.js`.
const assert = require("assert");
const test = require("node:test");
const { makePage, manifest, statuses, place, PLAN, TWO_GRIDS, showPlan, END_OF_PASS, plain, UNREVIEWED, LEFT_OUT } = require("./page");

const tests = {};

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

// A grid marked CLEAN, then z with the server answering /undo as given.
for (const [label, status, body, message] of [
  ["a 400", 400, { error: "x" }, "invalid reviewer name"],
  ["a 500", 500, { error: "x" }, "undo failed (HTTP 500)"],
  ["an empty reply", 200, {}, "Nothing to undo"],
]) {
  tests[label + " to /undo says " + message] = async () => {
    const page = makePage({ manifest, statuses });
    await showPlan(page);
    await page.dwell();
    await page.key("c");
    await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN", c: "CLEAN" }));
    await page.key("z");
    await page.respond("/undo", page.json(status, body));
    assert.strictEqual(page.els.status.textContent, message);
    assert.strictEqual(page.state.busy, false, "the page is free again");
  };
}

// node:test runs these one after another and fails one whose promise is still pending when the event loop empties.
for (const [name, fn] of Object.entries(tests)) test(name, fn);
