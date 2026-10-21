"use strict";
// The stop sign past either end of the list, against the stub DOM. Run with `node --test tests/web/*.test.js`.
const assert = require("assert");
const test = require("node:test");
const { REPACK_MS, makePage, manifest, statuses, place, TWO_GRIDS, END_OF_PASS, plain, UNREVIEWED } = require("./page");

const tests = {};

// The stop screen is up: no item, a neutral bar, no verdict, no dwell; the arrows still move.
function assertOnStop(page, detail) {
  assert.strictEqual(page.state.index, -1);
  assert.strictEqual(page.els["list-end"].hidden, false, "the stop sign is up");
  assert.strictEqual(page.els["list-end-detail"].textContent, detail);
  assert.strictEqual(page.els.message.hidden, true, "not the end screen");
  assert.strictEqual(page.els.bar.dataset.status, "", "neutral");
  assert.strictEqual(page.els["item-status"].hidden, true);
  assert.strictEqual(page.els.where.textContent, "");
  assert.strictEqual(page.state.dwell, "none");
  for (const id of ["clean", "dirty"]) assert.strictEqual(page.els[id].disabled, true, id);
  for (const id of ["prev", "next", "undo"]) assert.strictEqual(page.els[id].disabled, false, id);
  assert.strictEqual(page.els.reconnect.hidden, true, "not the end of the pass");
}

// The page shows item `index` (an image still to load) and gives it a fresh dwell.
async function assertShows(page, index) {
  assert.strictEqual(page.state.index, index);
  assert.strictEqual(page.els["list-end"].hidden, true);
  assert.strictEqual(page.state.dwell, "none", "a fresh dwell");
  await page.image200(page.state.items[index].key, [100, 50]);
  await page.dwell();
  assert.strictEqual(page.state.dwell, "over");
}

tests["-> past the last item shows the stop sign; -> again the first, <- again the last"] = async () => {
  for (const back of ["ArrowRight", "ArrowLeft"]) {
    const page = makePage({ manifest, statuses });
    await page.boot();
    await page.dwell();
    for (const index of [1, 2]) {
      await page.key("ArrowRight");
      await assertShows(page, index);
    }
    await page.key("ArrowRight");
    assertOnStop(page, "3 todo left in this list");
    assert.deepStrictEqual(page.pending, [], "nothing fetched for the stop sign");
    await page.advance(1000);
    await page.key("c");
    await page.key("d");
    await page.click("clean");
    await page.click("dirty");
    assert.deepStrictEqual(page.posts, [], "no item: no verdict sent");
    await page.key(back);
    await assertShows(page, back === "ArrowRight" ? 0 : 2);
  }
};

tests["<- before the first item shows the stop sign; <- again the last, -> again the first"] = async () => {
  for (const back of ["ArrowLeft", "ArrowRight"]) {
    const page = makePage({ manifest, statuses });
    await page.boot();
    await page.dwell();
    await page.click("prev");
    assertOnStop(page, "3 todo left in this list");
    await page.click(back === "ArrowLeft" ? "prev" : "next");
    await assertShows(page, back === "ArrowLeft" ? 2 : 0);
  }
};

tests["grid mode: the stop sign past either end of the batch, with its todo and [b]"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, TWO_GRIDS));
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  await page.dwell();
  await page.key("ArrowRight");
  await page.image200("c", [200, 200]);
  await page.key("ArrowRight");
  assertOnStop(page, "2 todo left in this batch - [b] next batch");
  await page.key("ArrowRight");
  assert.strictEqual(page.state.index, 0);
  for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
  assert.strictEqual(page.els.grid.hidden, false);
  await page.key("ArrowLeft");
  assertOnStop(page, "2 todo left in this batch - [b] next batch");
  assert.strictEqual(page.els.grid.hidden, true, "nothing stays up under the stop sign");
  await page.key("ArrowLeft");
  assert.strictEqual(page.state.index, 1);
  await page.image200("c", [200, 200]);
  await page.dwell();
  assert.strictEqual(page.state.dwell, "over");
  await page.key("ArrowRight");
  await page.key("b");
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/statuses?pass=1"], "b works from it");
};

tests["the stop sign with no todo left in the batch says what is left elsewhere"] = async () => {
  const rows = [{ key: "a", batch: "b1" }, { key: "c", batch: "b2" }];
  const page = makePage({ manifest: rows, statuses: { a: "UNREVIEWED", c: "UNREVIEWED" }, fixedOrder: true });
  await page.boot();
  await page.enterGrid();
  await page.respond("/grids", page.json(200, { grids: [[place("a", 0)]], left_out: [] }));
  await page.image200("a", [200, 200]);
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN" }));
  assert.strictEqual(page.els.message.textContent, "No todo images remaining - [b] next batch");
  await page.key("ArrowLeft");
  await page.image200("a", [200, 200]);
  await page.key("ArrowRight");
  assertOnStop(page, "No todo images remaining - [b] next batch");
};

tests["with nothing todo left in the pass, past an end is the end-of-pass screen, not the stop sign"] = async () => {
  const page = makePage({ manifest, statuses: { a: "UNREVIEWED", b: "CLEAN", c: "CLEAN" } });
  await page.boot();
  await page.dwell();
  await page.key("c");
  await page.respond("/mark", page.json(200, { a: "CLEAN" }));
  assert.strictEqual(page.els.message.textContent, END_OF_PASS);
  await page.key("ArrowLeft");
  await page.image200("a", [100, 50]);
  for (const key of ["ArrowRight", "ArrowLeft"]) {
    await page.key(key);
    assert.strictEqual(page.state.index, -1, key);
    assert.strictEqual(page.els["list-end"].hidden, true, key);
    assert.strictEqual(page.els.message.textContent, END_OF_PASS, key);
    assert.strictEqual(page.els.message.hidden, false, key);
    assert.strictEqual(page.els.reconnect.hidden, false, key + ": Reconnect is offered");
    await page.key(key === "ArrowRight" ? "ArrowLeft" : "ArrowRight"); // back to a, round the end
    await page.image200("a", [100, 50]);
  }
};

tests["z on the stop sign undoes and shows the item; s, m and q work from it"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.dwell();
  const first = page.state.items[0].key;
  await page.key("c");
  await page.respond("/mark", page.json(200, { [first]: "CLEAN" }));
  await page.key("ArrowRight");
  await page.key("ArrowRight");
  assertOnStop(page, "2 todo left in this list");
  await page.key("z");
  await page.respond("/undo", page.json(200, { [first]: "UNREVIEWED" }));
  assert.strictEqual(page.els.status.textContent, "Undone: " + first + " is UNREVIEWED");
  assert.strictEqual(page.els["list-end"].hidden, true);
  assert.strictEqual(page.state.index, 0);
  for (const key of ["s", "m"]) {
    const other = makePage({ manifest, statuses });
    await other.boot();
    await other.key("ArrowLeft");
    await other.key(key);
    assert.deepStrictEqual(other.pending.map((r) => r.path), ["/statuses?pass=1"], key);
  }
  const lost = makePage({ manifest, statuses });
  await lost.boot();
  await lost.key("ArrowLeft");
  lost.run("lose()");
  assert.strictEqual(lost.els["list-end"].hidden, true, "a stop takes the stop sign's arrow hint off the stage");
  assert.strictEqual(lost.els.reconnect.hidden, false);
  const other = makePage({ manifest, statuses });
  await other.boot();
  await other.key("ArrowLeft");
  await other.key("q");
  assert.strictEqual(other.state.waiting, true);
  assert.strictEqual(other.els["list-end"].hidden, true, "q clears the stage");
};

tests["onto the stop sign: a move, so it clears the message; a held arrow and an overlay do nothing"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  await page.dwell();
  await page.key("z");
  assert.strictEqual(page.els.status.textContent, "Nothing to undo");
  await page.keyRepeat("ArrowLeft");
  assert.strictEqual(page.state.index, 0, "a held arrow is ignored");
  await page.key("h");
  await page.key("ArrowLeft");
  assert.strictEqual(page.state.index, 0, "the help blocks it");
  await page.key("Escape");
  await page.key("ArrowLeft");
  assertOnStop(page, "3 todo left in this list");
  assert.strictEqual(page.els.status.textContent, "");
  assert(!page.els.status.textContent.includes("of list"), "no Start/End of list message");
  await page.keyRepeat("ArrowLeft");
  await page.keyRepeat("ArrowRight");
  assertOnStop(page, "3 todo left in this list");
  await page.key("h");
  await page.key("ArrowRight");
  assert.strictEqual(page.state.index, -1, "the help blocks it");
  await page.key("Escape");
  assertOnStop(page, "3 todo left in this list");
  await page.key("ArrowRight");
  await assertShows(page, 0);
};

tests["grid mode: a resize on the stop sign does not repack; leaving it does, keeping or landing as the end screen"] = async () => {
  for (const leave of ["z", "ArrowRight"]) {
    const page = makePage({ manifest, statuses });
    await page.boot();
    await page.enterGrid();
    await page.respond("/grids", page.json(200, TWO_GRIDS)); // [a, b], then [c]
    for (const key of ["a", "b"]) await page.image200(key, [200, 200]);
    await page.dwell();
    await page.key("c");
    await page.respond("/mark", page.json(200, { a: "CLEAN", b: "CLEAN" }));
    await page.image200("c", [200, 200]);
    await page.key("ArrowRight");
    assertOnStop(page, "1 todo left in this batch - [b] next batch");
    await page.resize(500, 300);
    await page.advance(REPACK_MS * 3);
    assert.deepStrictEqual(page.pending, [], "no /grids or /statuses: no repack");
    assert.strictEqual(page.grids.sent.length, 1);
    assertOnStop(page, "1 todo left in this batch - [b] next batch");
    assert.deepStrictEqual(page.state.items.map((item) => item.keys), [["a", "b"], ["c"]], "items kept");
    assert.deepStrictEqual(plain(page.state.marked), [["a", "b"]], "the undo stack kept");
    await page.key(leave);
    if (leave === "z") {
      assert.deepStrictEqual(page.posts.map((post) => post.path), ["/mark", "/undo"]);
      await page.respond("/undo", page.json(200, UNREVIEWED(["a", "b"])));
      assert.strictEqual(page.els.status.textContent, "Undone: grid of 2 images");
    }
    await page.advance(REPACK_MS);
    assert.strictEqual(page.grids.sent.length, 2, leave + ": leaving at the new size repacks");
    assert.deepStrictEqual(page.grids.sent[1].keys, leave === "z" ? ["a", "b", "c"] : ["c"]);
    assert.deepStrictEqual(page.grids.sent[1].width, 1000);
    const plan = leave === "z" ? TWO_GRIDS : { grids: [[place("c", 0)]], left_out: [] };
    await page.respond("/grids", page.json(200, plan));
    const landed = leave === "z" ? ["a", "b"] : ["c"]; // the undone grid, or the first
    assert.deepStrictEqual(page.state.items[page.state.index].keys, landed);
    for (const key of landed) await page.image200(key, [200, 200]);
    assert.strictEqual(page.els.grid.hidden, false);
  }
};

// node:test runs these one after another and fails one whose promise is still pending when the event loop empties.
for (const [name, fn] of Object.entries(tests)) test(name, fn);
