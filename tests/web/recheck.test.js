"use strict";
// Re-checking CLEAN images: the f filter, against the stub DOM. Run with `node --test tests/web/*.test.js`.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const test = require("node:test");
const { makePage, place, NEXT_PASS, plain, reload } = require("./page");

const tests = {};

const HTML = path.join(__dirname, "..", "..", "src", "image_review", "web", "index.html");

// One of each status, in one batch
const rows = ["a", "b", "c", "d"].map((key) => ({ key, batch: "b1" }));
const mixed = { a: "CLEAN", b: "UNREVIEWED", c: "DIRTY", d: "FLAGGED" };

// The current single item's image, if it is being fetched.
async function answerImage(page) {
  const item = page.state.items[page.state.index];
  if (item && item.kind === "single" && page.pending.some((r) => r.path === "/image?key=" + item.key)) {
    await page.image200(item.key, [100, 50]);
  }
}

// f, answered with `current`: the list is rebuilt under the next filter (in single mode, its first image shown).
async function pressF(page, current, pass = 1) {
  await page.key("f");
  await page.respond("/statuses?pass=" + pass, page.json(200, current));
  await answerImage(page);
}

// The current single item's key, once its image is in and its dwell is over.
async function showCurrent(page) {
  await answerImage(page);
  await page.dwell();
  return page.state.items[page.state.index].key;
}

const listed = (page) => page.state.items.map((item) => item.key).sort();

tests["f cycles todo, clean, all and back, rereading the statuses each time"] = async () => {
  const page = makePage({ manifest: rows, statuses: mixed, fixedOrder: true });
  await page.boot();
  assert.strictEqual(page.state.filter, "todo");
  assert.deepStrictEqual(listed(page), ["b", "d"]);
  assert.strictEqual(page.els.mode.textContent, "Single");
  const reads = () => page.pending.filter((r) => r.path.startsWith("/statuses")).length;
  await page.key("f");
  assert.strictEqual(reads(), 1, "f rereads the statuses");
  await page.respond("/statuses?pass=1", page.json(200, mixed));
  assert.strictEqual(page.state.filter, "clean");
  assert.deepStrictEqual(listed(page), ["a"], "clean lists only the CLEAN keys");
  assert.strictEqual(page.els.mode.textContent, "Single · re-check: CLEAN");
  await pressF(page, mixed);
  assert.strictEqual(page.state.filter, "all");
  assert.deepStrictEqual(listed(page), ["a", "b", "c", "d"]);
  assert.strictEqual(page.els.mode.textContent, "Single · re-check: all");
  assert.strictEqual(page.els.progress.textContent, "Pass 1 · 4 / 4 left", "every key is a re-check");
  await pressF(page, mixed);
  assert.strictEqual(page.state.filter, "todo");
  assert.deepStrictEqual(listed(page), ["b", "d"]);
  assert.strictEqual(page.els.mode.textContent, "Single");
};

tests["a re-checked key stops being todo; undo makes it todo again"] = async () => {
  const both = { a: "CLEAN", b: "UNREVIEWED", c: "CLEAN" };
  const page = makePage({ manifest: rows.slice(0, 3), statuses: both, fixedOrder: true });
  await page.boot();
  await pressF(page, both);
  assert.deepStrictEqual(listed(page), ["a", "c"]);
  assert.strictEqual(page.els.progress.textContent, "Pass 1 · 2 / 2 left");
  const first = await showCurrent(page);
  await page.key("c");
  await page.respond("/mark", page.json(200, { [first]: "CLEAN" })); // CLEAN again: only the re-check changes
  assert.deepStrictEqual([...page.state.rechecked], [first]);
  assert.strictEqual(page.els.progress.textContent, "Pass 1 · 1 / 2 left");
  const second = page.state.items[page.state.index].key;
  assert.notStrictEqual(second, first, "on to the next todo item");
  await showCurrent(page);
  await page.key("z");
  await page.respond("/undo", page.json(200, { [first]: "CLEAN" }));
  assert.deepStrictEqual([...page.state.rechecked], []);
  assert.strictEqual(page.els.progress.textContent, "Pass 1 · 2 / 2 left");
  assert.strictEqual(page.state.items[page.state.index].key, first, "the undone item is shown again");
  assert.strictEqual(page.els.status.textContent, "Undone: " + first + " is CLEAN");
};

tests["marks post the current pass and mode, and f rereads that pass"] = async () => {
  const page = makePage({ manifest: rows, statuses: mixed, fixedOrder: true });
  await page.respond("/current_pass", page.json(200, { pass: 4 }));
  await page.respond("/manifest", page.json(200, rows));
  await page.respond("/statuses?pass=4", page.json(200, mixed));
  await page.image200(page.state.items[0].key, [100, 50]);
  await pressF(page, mixed, 4);
  await showCurrent(page);
  await page.key("d");
  assert.deepStrictEqual(page.marks(), [{ keys: ["a"], status: "DIRTY", pass: 4, reviewer: "Jane", mode: "single" }]);
};

tests["switching the filter clears the undo stack and the re-checked keys; s, m and b keep the re-checked"] = async () => {
  const page = makePage({ manifest: rows, statuses: mixed, fixedOrder: true });
  await page.boot();
  await pressF(page, mixed);
  await showCurrent(page);
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN" }));
  assert.deepStrictEqual(plain(page.state.marked), [["a"]]);
  assert.deepStrictEqual([...page.state.rechecked], ["a"]);
  await page.key("s");
  await page.respond("/statuses?pass=1", page.json(200, mixed));
  assert.deepStrictEqual(plain(page.state.marked), [], "a mode switch forgets the undo stack");
  assert.deepStrictEqual([...page.state.rechecked], ["a"], "but not the re-checked keys");
  await page.enterGrid(mixed);
  await page.respond("/grids", page.json(200, { grids: [[place("a", 0)]], left_out: [] }));
  assert.deepStrictEqual([...page.state.rechecked], ["a"]);
  await page.key("b");
  await page.respond("/statuses?pass=1", page.json(200, mixed));
  assert.deepStrictEqual([...page.state.rechecked], ["a"]);
  await pressF(page, mixed);
  assert.strictEqual(page.state.filter, "all");
  assert.deepStrictEqual(plain(page.state.marked), []);
  assert.deepStrictEqual([...page.state.rechecked], [], "a new filter starts with nothing re-checked");
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo");
  assert(!page.posts.some((post) => post.path === "/undo"));
};

tests["grid mode under all packs CLEAN and UNREVIEWED and holds DIRTY and FLAGGED back, with the message"] = async () => {
  const page = makePage({ manifest: rows, statuses: mixed, fixedOrder: true });
  await page.boot();
  await pressF(page, mixed);
  await pressF(page, mixed);
  await page.enterGrid(mixed);
  assert.deepStrictEqual(page.grids.sent.map((body) => body.keys), [["a", "b"]]);
  assert.strictEqual(page.els.mode.textContent, "Grid · auto · re-check: all");
  await page.respond("/grids", page.json(200, { grids: [[place("a", 0), place("b", 100)]], left_out: [] }));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("c");
  assert.deepStrictEqual(page.marks().at(-1), { keys: ["a", "b"], status: "CLEAN", pass: 1, reviewer: "Jane", mode: "grid" });
  await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN" }));
  assert.deepStrictEqual([...page.state.rechecked].sort(), ["a", "b"]);
  assert.strictEqual(page.state.index, -1, "the grid was the batch's only todo item");
  // c (DIRTY, not yet re-checked) and d (FLAGGED) are left for single mode
  assert.strictEqual(page.els.message.textContent,
    "No grid items for pass 1; 2 FLAGGED/DIRTY images need single-mode review - press [s]");
  assert.strictEqual(page.els.reconnect.hidden, true);
};

tests["grid mode under all with only DIRTY and FLAGGED left says so at once"] = async () => {
  const held = { a: "DIRTY", b: "FLAGGED" };
  const page = makePage({ manifest: rows.slice(0, 2), statuses: held, fixedOrder: true });
  await page.boot();
  await page.enterGrid(held);
  assert.strictEqual(page.els.message.textContent,
    "No grid items for pass 1; 1 FLAGGED/DIRTY image needs single-mode review - press [s]", "todo: the FLAGGED one");
  await pressF(page, held); // clean: none listed, and the pass is not done
  assert.strictEqual(page.els.message.textContent,
    "No images to review for pass 1 (filter: clean) - press [f] to change the filter", "the filter lists none");
  assert.strictEqual(page.els.reconnect.hidden, true);
  await pressF(page, held);
  assert.strictEqual(page.els.message.textContent,
    "No grid items for pass 1; 2 FLAGGED/DIRTY images need single-mode review - press [s]");
  assert.strictEqual(page.grids.sent.length, 0, "nothing to pack");
};

tests["with every key re-checked under all the pass is done: All batches done, and Reconnect"] = async () => {
  const done = { a: "CLEAN", b: "DIRTY" };
  const page = makePage({ manifest: rows.slice(0, 2), statuses: done, fixedOrder: true });
  await page.boot();
  await pressF(page, done);
  await pressF(page, done);
  for (const key of ["a", "b"]) {
    assert.strictEqual(await showCurrent(page), key);
    await page.key("c");
    await page.respond("/mark", page.json(200, { [key]: "CLEAN" }));
  }
  assert.strictEqual(page.state.index, -1);
  assert.strictEqual(page.els.message.textContent, "All batches done for pass 1 (filter: all). " + NEXT_PASS);
  assert.strictEqual(page.els.reconnect.hidden, false);
};

tests["Reconnect keeps the filter and the re-checked keys; q forgets the re-checked keys"] = async () => {
  const both = { a: "CLEAN", b: "UNREVIEWED", c: "CLEAN" };
  const page = makePage({ manifest: rows.slice(0, 3), statuses: both, fixedOrder: true });
  await page.boot();
  await pressF(page, both);
  await showCurrent(page);
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN" }));
  await page.fail("/image?key=c"); // the next item's image: a lost connection
  assert.strictEqual(page.els.reconnect.hidden, false);
  await page.key("r");
  await reload(page, both, 1, rows.slice(0, 3));
  assert.strictEqual(page.state.filter, "clean");
  assert.deepStrictEqual([...page.state.rechecked], ["a"]);
  assert.deepStrictEqual(listed(page), ["a", "c"]);
  assert.strictEqual(page.els.progress.textContent, "Pass 1 · 1 / 2 left");
  assert.strictEqual(page.els.mode.textContent, "Single · re-check: CLEAN");
  await page.key("q");
  await page.key("r");
  await reload(page, both, 1, rows.slice(0, 3));
  assert.strictEqual(page.state.filter, "clean", "kept, as the mode is");
  assert.deepStrictEqual([...page.state.rechecked], [], "the next server's keys may match these");
};

// Under clean, the current single item marked CLEAN (re-checked), with its reply.
async function recheckCurrent(page) {
  const key = await showCurrent(page);
  await page.key("c");
  await page.respond("/mark", page.json(200, { [key]: "CLEAN" }));
  return key;
}

const CLEAN_AC = { a: "CLEAN", b: "UNREVIEWED", c: "CLEAN" };

tests["the mode keeps its whole text in its title"] = async () => {
  const page = makePage({ manifest: rows, statuses: mixed, fixedOrder: true });
  await page.boot();
  await pressF(page, mixed);
  assert.strictEqual(page.els.mode.title, "Single · re-check: CLEAN");
};

tests["Reconnect to another server forgets the re-checked keys"] = async () => {
  const page = makePage({ manifest: rows.slice(0, 3), statuses: CLEAN_AC, fixedOrder: true });
  await page.boot();
  await pressF(page, CLEAN_AC);
  await recheckCurrent(page);
  assert.deepStrictEqual([...page.state.rechecked], ["a"]);
  await page.fail("/image?key=c");
  page.instance = "B"; // the server restarted, perhaps on another work directory with the same key names
  await page.key("r");
  await reload(page, CLEAN_AC, 1, rows.slice(0, 3));
  assert.strictEqual(page.state.filter, "clean");
  assert.deepStrictEqual([...page.state.rechecked], [], "another server's a may be another image");
  assert.strictEqual(page.els.progress.textContent, "Pass 1 · 2 / 2 left");
};

tests["s after a partial re-check lands on a todo item"] = async () => {
  const page = makePage({ manifest: rows.slice(0, 3), statuses: CLEAN_AC, fixedOrder: true });
  await page.boot();
  await pressF(page, CLEAN_AC);
  assert.strictEqual(await recheckCurrent(page), "a");
  await page.key("s");
  await page.respond("/statuses?pass=1", page.json(200, CLEAN_AC));
  assert.deepStrictEqual(listed(page), ["a", "c"]);
  assert.strictEqual(page.state.items[page.state.index].key, "c", "not a, already re-checked");
};

tests["Reconnect after a complete re-check shows the end screen, not a re-checked item"] = async () => {
  const page = makePage({ manifest: rows.slice(0, 3), statuses: CLEAN_AC, fixedOrder: true });
  await page.boot();
  await pressF(page, CLEAN_AC);
  await recheckCurrent(page);
  await recheckCurrent(page);
  assert.strictEqual(page.state.index, -1);
  page.run("lose()");
  await page.key("r");
  await reload(page, CLEAN_AC, 1, rows.slice(0, 3));
  assert.strictEqual(page.state.index, -1);
  assert(!page.pending.some((r) => r.path.startsWith("/image")), "no image is shown");
  assert.strictEqual(page.els.message.textContent, "All batches done for pass 1 (filter: clean) - press [f] to change the filter");
};

tests["m on a fully re-checked batch shows the end screen"] = async () => {
  const page = makePage({ manifest: rows.slice(0, 3), statuses: CLEAN_AC, fixedOrder: true });
  await page.boot();
  await pressF(page, CLEAN_AC);
  await recheckCurrent(page);
  await recheckCurrent(page);
  await page.enterGrid(CLEAN_AC);
  assert.deepStrictEqual(page.grids.sent.map((body) => body.keys), [["a", "c"]]);
  await page.respond("/grids", page.json(200, { grids: [[place("a", 0), place("c", 100)]], left_out: [] }));
  assert.strictEqual(page.state.index, -1);
  assert(!page.pending.some((r) => r.path.startsWith("/image")), "the re-checked grid is not drawn");
  assert.strictEqual(page.els.message.textContent, "All batches done for pass 1 (filter: clean) - press [f] to change the filter");
};

tests["b under clean skips a fully re-checked batch"] = async () => {
  const three = [{ key: "a", batch: "b1" }, { key: "c", batch: "b2" }, { key: "e", batch: "b3" }];
  const clean = { a: "CLEAN", c: "CLEAN", e: "CLEAN" };
  const page = makePage({ manifest: three, statuses: clean, fixedOrder: true });
  await page.boot();
  await pressF(page, clean);
  await showCurrent(page);
  await page.key("ArrowRight"); // c, the whole of b2
  assert.strictEqual(await recheckCurrent(page), "c");
  await page.enterGrid(clean);
  assert.strictEqual(page.state.batch, "b1");
  await page.respond("/grids", page.json(200, { grids: [[place("a", 0)]], left_out: [] }));
  await page.key("b");
  await page.respond("/statuses?pass=1", page.json(200, clean));
  assert.strictEqual(page.state.batch, "b3", "b2 has nothing left to re-check");
  assert.deepStrictEqual(page.grids.sent.at(-1).keys, ["e"]);
};

tests["an undo whose reply cannot be read takes the entry's keys, and only those, out of the re-checked"] = async () => {
  const page = makePage({ manifest: rows.slice(0, 3), statuses: CLEAN_AC, fixedOrder: true });
  await page.boot();
  await pressF(page, CLEAN_AC);
  await recheckCurrent(page);
  await recheckCurrent(page);
  assert.deepStrictEqual([...page.state.rechecked], ["a", "c"]);
  await page.key("z");
  await page.respond("/undo", page.json(200, "not a status map")); // the server acted; its reply is unusable
  await page.respond("/statuses?pass=1", page.json(200, CLEAN_AC)); // resynced
  assert.deepStrictEqual([...page.state.rechecked], ["a"], "c's mark was undone; a's stays");
};

tests["a refused or failed mark under clean re-checks nothing"] = async () => {
  const page = makePage({ manifest: rows.slice(0, 3), statuses: CLEAN_AC, fixedOrder: true });
  await page.boot();
  await pressF(page, CLEAN_AC);
  await page.enterGrid(CLEAN_AC);
  await page.respond("/grids", page.json(200, { grids: [[place("a", 0), place("c", 100)]], left_out: [] }));
  for (const key of ["a", "c"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(409, {}));
  await page.respond("/statuses?pass=1", page.json(200, CLEAN_AC));
  assert.deepStrictEqual([...page.state.rechecked], [], "a 409 records nothing");
  await page.key("d");
  await page.respond("/mark", page.json(500, {}));
  assert.strictEqual(page.els.status.textContent, "mark failed (HTTP 500)");
  assert.deepStrictEqual([...page.state.rechecked], [], "nor does a 500");
};

tests["past the end once the clean re-check is done: the end screen, not the stop sign"] = async () => {
  const page = makePage({ manifest: rows.slice(0, 3), statuses: CLEAN_AC, fixedOrder: true });
  await page.boot();
  await pressF(page, CLEAN_AC);
  await recheckCurrent(page);
  await recheckCurrent(page);
  await page.key("ArrowLeft"); // round to the last item
  await showCurrent(page);
  await page.key("ArrowRight"); // past the end
  assert.strictEqual(page.els["list-end"].hidden, true);
  assert.strictEqual(page.els.message.textContent, "All batches done for pass 1 (filter: clean) - press [f] to change the filter");
  assert.strictEqual(page.els.reconnect.hidden, true, "b is still UNREVIEWED: the pass is not done");
};

tests["a grid undo under all makes every key of it todo again"] = async () => {
  const two = rows.slice(0, 2);
  const clean = { a: "CLEAN", b: "CLEAN" };
  const page = makePage({ manifest: two, statuses: clean, fixedOrder: true });
  await page.boot();
  await pressF(page, clean);
  await pressF(page, clean);
  await page.enterGrid(clean);
  await page.respond("/grids", page.json(200, { grids: [[place("a", 0), place("b", 100)]], left_out: [] }));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, clean));
  assert.deepStrictEqual([...page.state.rechecked].sort(), ["a", "b"]);
  await page.key("z");
  await page.respond("/undo", page.json(200, clean));
  assert.deepStrictEqual([...page.state.rechecked], []);
};

tests["a grid holding a re-checked key and an unchecked one is todo"] = async () => {
  const two = rows.slice(0, 2);
  const clean = { a: "CLEAN", b: "CLEAN" };
  const page = makePage({ manifest: two, statuses: clean, fixedOrder: true });
  await page.boot();
  await pressF(page, clean);
  await pressF(page, clean);
  assert.strictEqual(await recheckCurrent(page), "a"); // in single mode
  await page.enterGrid(clean);
  await page.respond("/grids", page.json(200, { grids: [[place("a", 0), place("b", 100)]], left_out: [] }));
  assert.strictEqual(page.state.index, 0, "the grid is shown: b is still to re-check");
  assert.match(page.els.progress.textContent, / 1 \/ 1 left$/);
};

for (const [label, plan] of [
  ["everything re-checked", { grids: [[place("a", 0), place("c", 100)]], left_out: [] }],
  ["part re-checked", { grids: [[place("a", 0)], [place("c", 0)]], left_out: [] }],
]) {
  tests["a resize repack under clean stays on the re-checked grid shown, " + label] = async () => {
    const page = makePage({ manifest: rows.slice(0, 3), statuses: CLEAN_AC, fixedOrder: true });
    await page.boot();
    await pressF(page, CLEAN_AC);
    await page.enterGrid(CLEAN_AC);
    await page.respond("/grids", page.json(200, plan));
    const draw = async () => {
      for (const key of page.state.items[page.state.index].keys) await page.image200(key, [200, 200]);
      await page.dwell();
    };
    await draw();
    await page.key("c");
    await page.respond("/mark", page.json(200, { a: "CLEAN", c: "CLEAN" }));
    assert(page.state.rechecked.has("a"));
    await page.key(plan.grids.length === 1 ? "ArrowRight" : "ArrowLeft"); // back to the re-checked grid [a, ...]
    await draw();
    assert(page.state.items[page.state.index].keys.includes("a"));
    await page.resize(500, 300);
    await page.advance(300);
    await page.respond("/grids", page.json(200, plan));
    assert(page.state.index >= 0, "not the end screen");
    assert(page.state.items[page.state.index].keys.includes("a"), "the grid that was on screen");
  };
}

tests["a Reconnect retried after a failed one to another server still forgets the re-checked keys"] = async () => {
  const page = makePage({ manifest: rows.slice(0, 3), statuses: CLEAN_AC, fixedOrder: true });
  await page.boot();
  await pressF(page, CLEAN_AC);
  await recheckCurrent(page);
  await page.fail("/image?key=c");
  page.instance = "B";
  await page.key("r");
  await page.respond("/current_pass", page.json(200, { pass: 1 }));
  await page.fail("/manifest");
  assert.strictEqual(page.els.reconnect.hidden, false);
  await page.key("r");
  await reload(page, CLEAN_AC, 1, rows.slice(0, 3));
  assert.deepStrictEqual([...page.state.rechecked], []);
  assert.strictEqual(page.els.progress.textContent, "Pass 1 · 2 / 2 left");
};

tests["the pass-done end messages under clean"] = async () => {
  const some = { a: "CLEAN", b: "DIRTY" };
  const page = makePage({ manifest: rows.slice(0, 2), statuses: some, fixedOrder: true });
  await page.boot();
  await pressF(page, some);
  await recheckCurrent(page);
  assert.strictEqual(page.els.message.textContent, "All batches done for pass 1 (filter: clean). " + NEXT_PASS);
  assert.strictEqual(page.els.reconnect.hidden, false);
  const none = { a: "DIRTY", b: "DIRTY" };
  const other = makePage({ manifest: rows.slice(0, 2), statuses: none, fixedOrder: true });
  await other.boot();
  await pressF(other, none);
  assert.strictEqual(other.els.message.textContent, "No images to review for pass 1 (filter: clean). " + NEXT_PASS);
  assert.strictEqual(other.els.reconnect.hidden, false);
};

tests["the help lists f"] = () => {
  const html = fs.readFileSync(HTML, "utf8");
  const help = html.slice(html.indexOf('<div id="help"'), html.indexOf("</dl>"));
  assert.match(help, /<dt>f<\/dt><dd>Re-check: [^<]*CLEAN[^<]*<\/dd>/);
};

// node:test runs these one after another and fails one whose promise is still pending when the event loop empties.
for (const [name, fn] of Object.entries(tests)) test(name, fn);
