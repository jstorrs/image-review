"use strict";
// Reconnect and lost-connection handling, against the stub DOM. Run with `node --test tests/web/*.test.js`.
const assert = require("assert");
const test = require("node:test");
const { JPEG, makePage, manifest, statuses, place, PLAN, TWO_GRIDS, showPlan, plain, UNREVIEWED, LEFT_OUT, LOST, reload } = require("./page");

const tests = {};

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
  assert.strictEqual(page.els.status.textContent, LOST);
  const stoppedMessage = page.els.message.textContent;
  const shownBeforeRelease = page.els.grid.hidden;
  release();
  await page.advance(0);
  assert.deepStrictEqual(page.pending.map((r) => r.path), ["/image?key=c", "/image?key=d"], "e is never asked for");
  assert.strictEqual(page.els.status.textContent, LOST);
  assert.strictEqual(page.els.message.textContent, stoppedMessage, "stale decode does not replace the stop message");
  assert.strictEqual(page.els.grid.hidden, shownBeforeRelease, "stale decode does not show the grid");
  assert.strictEqual(page.bitmaps.length, 1);
  assert(page.bitmaps[0].closed, "bitmap created after the stop is disposed");
  assert.deepStrictEqual(page.draws, [], "stale bitmap is not drawn");
};

tests["stopping invalidates pending paint and running dwell while preserving the displayed image"] = async () => {
  const page = makePage({ manifest, statuses });
  await page.boot();
  const shownUrl = page.state.objectUrl;
  await page.paint();
  assert.strictEqual(page.state.dwell, "running");
  page.run("lose()");
  assert.strictEqual(page.state.dwell, "none");
  assert.strictEqual(page.els.image.hidden, false);
  assert.strictEqual(page.state.objectUrl, shownUrl, "the displayed image stays in place");
  await page.advance(200);
  assert.strictEqual(page.state.dwell, "none", "the old timer cannot end a dwell after stop");

  const pendingPaint = makePage({ manifest, statuses });
  await pendingPaint.boot();
  pendingPaint.run("lose()");
  await pendingPaint.paint();
  await pendingPaint.advance(200);
  assert.strictEqual(pendingPaint.state.dwell, "none", "a paint already queued before stop cannot start dwell");

  const resized = makePage({ manifest, statuses });
  await resized.boot();
  resized.run("lose()");
  await resized.resize(400, 300);
  await resized.paint();
  await resized.advance(200);
  assert.strictEqual(resized.state.dwell, "none", "resize cannot restart dwell after stop");

  const resizedGrid = makePage({ manifest, statuses });
  await showPlan(resizedGrid);
  resizedGrid.run("lose()");
  const gridItems = resizedGrid.state.items;
  const gridMessage = resizedGrid.els.message.textContent;
  await resizedGrid.resize(300, 250);
  await resizedGrid.paint();
  await resizedGrid.advance(200);
  assert.strictEqual(resizedGrid.state.dwell, "none", "grid resize cannot restart dwell after stop");
  assert.strictEqual(resizedGrid.els.grid.hidden, false, "a stopped grid stays displayed through resize");
  assert.strictEqual(resizedGrid.state.items, gridItems, "a stopped resize does not rebuild its items");
  assert.strictEqual(resizedGrid.els.message.textContent, gridMessage, "a stopped resize preserves its notice");
  assert.deepStrictEqual(resizedGrid.pending, [], "a stopped resize sends no layout or image request");
};

tests["stale image bytes cannot affect a stopped or reconnected display"] = async () => {
  for (const rejectBytes of [false, true]) {
    for (const reconnect of [false, true]) {
      const page = makePage({ manifest, statuses });
      await page.boot();
      await page.key("ArrowRight");
      const stalePath = page.pending.find((request) => request.path.startsWith("/image?")).path;
      const key = decodeURIComponent(stalePath.slice("/image?key=".length));
      let release;
      let entered;
      const started = new Promise((resolve) => { entered = resolve; });
      const gate = new Promise((resolve, reject) => { release = rejectBytes ? () => reject(new Error("late bytes failure")) : resolve; });
      const reply = page.image(key, [100, 50]);
      reply.blob = async () => ({ tag: key, size: [100, 50], arrayBuffer: async () => {
        entered();
        await gate;
        return Uint8Array.from(JPEG).buffer;
      } });
      await page.respond("/image?key=" + key, reply);
      await started;
      page.run("lose()");
      assert.strictEqual(page.els.status.textContent, LOST);
      let expected = {
        message: page.els.message.textContent,
        hidden: page.els.image.hidden,
        loaded: page.state.loaded,
        objectUrl: page.state.objectUrl,
        dwell: page.state.dwell,
      };
      if (reconnect) {
        await page.click("reconnect");
        await reload(page, statuses);
        const reloadedKey = page.state.items[page.state.index].key;
        await page.image200(reloadedKey, [100, 50]);
        await page.paint();
        expected = {
          message: page.els.message.textContent,
          hidden: page.els.image.hidden,
          loaded: page.state.loaded,
          objectUrl: page.state.objectUrl,
          dwell: page.state.dwell,
        };
        assert.strictEqual(page.els.status.textContent, "Reconnected");
      }
      const revokedCount = page.revoked.length;
      release();
      await page.advance(0);
      assert.strictEqual(page.els.status.textContent, reconnect ? "Reconnected" : LOST);
      assert.deepStrictEqual({
        message: page.els.message.textContent,
        hidden: page.els.image.hidden,
        loaded: page.state.loaded,
        objectUrl: page.state.objectUrl,
        dwell: page.state.dwell,
      }, expected, "late byte completion or rejection leaves display state unchanged");
      assert.strictEqual(page.revoked.length, revokedCount, "stale bytes create no URL to revoke");
    }
  }
};

tests["a decoded image released while stopped or after Reconnect is revoked without changing the display"] = async () => {
  for (const rejectDecode of [false, true]) {
    for (const reconnect of [false, true]) {
      const page = makePage({ manifest, statuses });
      await page.boot();
      await page.key("ArrowRight");
      const stalePath = page.pending.find((request) => request.path.startsWith("/image?")).path;
      const key = decodeURIComponent(stalePath.slice("/image?key=".length));
      let release;
      let entered;
      const started = new Promise((resolve) => { entered = resolve; });
      const gate = new Promise((resolve, reject) => {
        release = rejectDecode ? () => reject(new Error("late decode failure")) : resolve;
      });
      let decodes = 0;
      page.els.image.decode = async () => {
        if (++decodes === 1) {
          entered();
          await gate;
        }
      };
      await page.image200(key, [100, 50]);
      await started;
      const staleUrl = page.createdUrls.at(-1);
      page.run("lose()");
      assert.strictEqual(page.els.status.textContent, LOST);
      if (reconnect) {
        await page.click("reconnect");
        await reload(page, statuses);
        const reloadedKey = page.state.items[page.state.index].key;
        await page.image200(reloadedKey, [100, 50]);
        await page.paint();
      }
      const expected = {
        message: page.els.message.textContent,
        hidden: page.els.image.hidden,
        src: page.els.image.src,
        loaded: page.state.loaded,
        objectUrl: page.state.objectUrl,
        dwell: page.state.dwell,
      };
      const currentUrl = page.state.objectUrl;
      const removeCount = page.removedAttrs.filter(([id, attr]) => id === "image" && attr === "src").length;
      release();
      await page.advance(0);
      assert(page.revoked.includes(staleUrl), "the stale decoded URL is revoked");
      assert.deepStrictEqual({
        message: page.els.message.textContent,
        hidden: page.els.image.hidden,
        src: page.els.image.src,
        loaded: page.state.loaded,
        objectUrl: page.state.objectUrl,
        dwell: page.state.dwell,
      }, expected, "late decode leaves display state unchanged");
      assert.strictEqual(page.removedAttrs.filter(([id, attr]) => id === "image" && attr === "src").length,
        removeCount, "stale decode failure does not remove the current image source");
      assert.strictEqual(page.els.status.textContent, reconnect ? "Reconnected" : LOST);
      if (currentUrl !== null) {
        assert(!page.revoked.includes(currentUrl), "the current image URL remains live");
      }
    }
  }
};

// node:test runs these one after another and fails one whose promise is still pending when the event loop empties.
for (const [name, fn] of Object.entries(tests)) test(name, fn);
