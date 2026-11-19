"use strict";
// The bar, the help overlay and the name box, against the stub DOM. Run with `node --test tests/web/*.test.js`.
const assert = require("assert");
const test = require("node:test");
const { MARK_FLASH_MS, REPACK_MS, makePage, manifest, statuses, place, PLAN, TWO_GRIDS, showPlan, END_OF_PASS, UNREVIEWED, LOST, reload } = require("./page");

const tests = {};

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
  assert.strictEqual(page.els.status.textContent, "", "past the start is a move: the stop sign, and no message");
  assert.strictEqual(page.els["list-end"].hidden, false);
};

// A mark that went through keeps the item up, the bar in its new
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

// node:test runs these one after another and fails one whose promise is still pending when the event loop empties.
for (const [name, fn] of Object.entries(tests)) test(name, fn);
