"use strict";
// helpers.js's pure helpers, checked in a bare vm context with no DOM. Run with `node --test tests/web/*.test.js`.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const test = require("node:test");
const vm = require("vm");
const { plain } = require("./page");

const HELPERS = path.join(__dirname, "..", "..", "src", "image_review", "web", "helpers.js");
const m = vm.createContext({});
vm.runInContext(fs.readFileSync(HELPERS, "utf8"), m, { filename: HELPERS });

test("script helpers", () => {
  assert(m.validReviewer("a") && m.validReviewer("\u{1F600}".repeat(64)));
  assert(!m.validReviewer("") && !m.validReviewer("   ") && !m.validReviewer("x".repeat(65)));
  assert.deepStrictEqual(plain(m.shuffle([1, 2, 3, 4], () => 0.99)), [1, 2, 3, 4]);
  assert.deepStrictEqual(plain(m.shuffle([3, 1, 2])).sort(), [1, 2, 3]);
  assert(m.isCompleteJpeg(new Uint8Array([0xff, 0xd8, 0, 0xff, 0xd9])));
  assert(!m.isCompleteJpeg(new Uint8Array([0xff, 0xd8, 0, 0xff])));
  assert(!m.isCompleteJpeg(new Uint8Array([0xff, 0xd8, 0, 0xff, 0])));
  assert(!m.isCompleteJpeg(new Uint8Array([0, 0xd8, 0, 0xff, 0xd9])));
  const st = new Map([["a", "CLEAN"], ["b", "FLAGGED"], ["c", "UNREVIEWED"]]);
  const single = (key) => ({ kind: "single", key });
  const items = ["a", "b", "c"].map(single);
  assert.deepStrictEqual(plain(m.itemKeys(single("a"))), ["a"]);
  assert.strictEqual(m.itemStatus(single("b"), st), "FLAGGED");
  assert.strictEqual(m.itemStatus(single("zz"), st), undefined);
  assert.strictEqual(m.nextTodoIndex(items, st, 1), 2);
  assert.strictEqual(m.nextTodoIndex(items, st, 2), 1);
  assert.strictEqual(m.nextTodoIndex([single("a")], st, 0), -1);
  assert.strictEqual(m.countTodo(items, st), 2);
  assert.strictEqual(m.describeStatuses(new Map([["a", "CLEAN"]])), "a is CLEAN");
  assert.throws(() => m.parseStatusMap({ a: "BOGUS" }));
  // Grid items: their keys, and a status by the grid rules
  const grid = { kind: "grid", placements: [], keys: ["a", "c"] };
  assert.deepStrictEqual(plain(m.itemKeys(grid)), ["a", "c"]);
  assert.strictEqual(m.itemStatus(grid, st), "UNREVIEWED");
  assert.strictEqual(m.itemStatus({ kind: "grid", placements: [], keys: ["a", "b"] }, st), "DIRTY");
  assert.strictEqual(m.countTodo([grid, single("a")], st), 1);
});

test("parse grid plan", () => {
  const place = (key, x, y, w, h, more = {}) => ({ key, x, y, w, h, rotated: false, source: [w, h], ...more });
  const good = () => ({
    grids: [[place("a", 0, 0, 100, 50), place("b", 100, 0, 50, 100, { rotated: true, source: [100, 50] })]],
    left_out: ["c"],
  });
  const sent = ["a", "b", "c"];
  const plan = m.parseGridPlan(good(), sent, 300, 200);
  assert.deepStrictEqual(plain(plan.leftOut), ["c"]);
  assert.deepStrictEqual(plain(plan.grids[0].map((p) => p.key)), ["a", "b"]);
  assert.strictEqual(plan.width, 300);
  // Shrunk to fit, with rounding: 1001x500 at 400 wide is 400x199
  m.parseGridPlan({ grids: [[place("a", 0, 0, 400, 199, { source: [1001, 500] })]], left_out: [] }, ["a"], 400, 400);
  const bad = (mutate, keys = sent, w = 300, h = 200) => {
    const reply = good();
    mutate(reply);
    assert.throws(() => m.parseGridPlan(reply, keys, w, h), (e) => e.constructor.name === "BadReply");
  };
  bad((r) => { r.grids[0][1].x = 50; }); // overlaps a
  bad((r) => { r.grids[0][1].x = 260; }); // past the right edge
  bad((r) => { r.grids[0][1].y = 101; }); // past the bottom
  bad((r) => { r.grids[0][0].x = -1; });
  bad((r) => { r.left_out.push("a"); }); // a twice
  bad((r) => { r.grids.push([place("a", 0, 100, 100, 50)]); }); // a twice, across grids
  bad((r) => { r.left_out = []; }); // c missing
  bad((r) => { r.left_out.push("z"); }); // never sent
  bad((r) => { r.grids[0][0].key = "z"; });
  bad((r) => { r.grids[0][0].w = 100.5; });
  bad((r) => { r.grids[0][0].y = "0"; });
  bad((r) => { r.grids[0][0].w = 0; });
  bad((r) => { r.grids[0][0].rotated = 1; });
  bad((r) => { r.grids[0][0].source = [100]; });
  bad((r) => { r.grids[0][0].source = [100, 0]; });
  bad((r) => { r.grids[0][0].source = [100, 50.5]; });
  bad((r) => { r.grids[0][0].source = [50, 25]; }); // enlarged
  bad((r) => { r.grids[0][0].source = [100, 100]; }); // aspect differs
  bad((r) => { r.grids[0][1].rotated = false; }); // the rect is not the source's fit upright
  bad((r) => { r.grids.push([]); });
  bad((r) => { r.grids = {}; });
  bad((r) => { delete r.left_out; });
  bad(() => {}, sent, 0, 200);
  bad(() => {}, sent, 300.5, 200);
  assert.throws(() => m.parseGridPlan(null, sent, 300, 200));
});

test("order and demote grid items", () => {
  const place = (key) => ({ key, x: 0, y: 0, w: 1, h: 1, rotated: false, source: [1, 1] });
  const plan = { grids: [["a"], ["b", "c"], ["d"], ["e", "f", "g"]].map((g) => g.map(place)), leftOut: ["x", "y"] };
  // random() = 0 always swaps with the first: [g0..g3] -> [g1, g2, g3, g0]
  const items = m.orderGridItems(plan, () => 0);
  assert.deepStrictEqual(plain(items.map((item) => [item.kind, item.keys || item.key])), [
    ["grid", ["e", "f", "g"]], ["grid", ["b", "c"]], ["grid", ["d"]], ["grid", ["a"]], ["single", "x"], ["single", "y"],
  ]);
  assert.deepStrictEqual(plain(items[0].placements.map((p) => p.key)), ["e", "f", "g"]);
  // A failed key leaves the grid as a single item; an emptied grid is dropped; no key is listed twice
  const less = m.demoteKeys(items, 0, ["f"]);
  assert.deepStrictEqual(plain(less.map((item) => item.keys || item.key)), [["e", "g"], ["b", "c"], ["d"], ["a"], "x", "y", "f"]);
  assert.deepStrictEqual(plain(less[0].placements.map((p) => p.key)), ["e", "g"]);
  assert.deepStrictEqual(plain(items[0].keys), ["e", "f", "g"]); // the input is left alone
  const gone = m.demoteKeys(items, 2, ["d"]);
  assert.deepStrictEqual(plain(gone.map((item) => item.keys || item.key)), [["e", "f", "g"], ["b", "c"], ["a"], "x", "y", "d"]);
  const listed = m.demoteKeys([...items, { kind: "single", key: "a" }], 3, ["a"]);
  assert.deepStrictEqual(plain(listed.map((item) => item.keys || item.key)), [["e", "f", "g"], ["b", "c"], ["d"], "x", "y", "a"]);
});

test("placement transform and min scale", () => {
  const apply = ([a, b, c, d, e, f], [u, v]) => [a * u + c * v + e, b * u + d * v + f];
  const corners = (p) => {
    const [sw, sh] = p.source;
    const t = m.placementTransform(p);
    return [[0, 0], [sw, 0], [sw, sh], [0, sh]].map((uv) => apply(t, uv));
  };
  // Upright: source corners to the rect's, in order
  const upright = { key: "a", x: 10, y: 20, w: 50, h: 25, rotated: false, source: [200, 100] };
  assert.deepStrictEqual(plain(corners(upright)), [[10, 20], [60, 20], [60, 45], [10, 45]]);
  // Rotated 90 degrees clockwise: top-left to top-right, top-right to bottom-right, ...
  const turned = { key: "b", x: 10, y: 20, w: 25, h: 50, rotated: true, source: [200, 100] };
  assert.deepStrictEqual(plain(corners(turned)), [[35, 20], [35, 70], [10, 70], [10, 20]]);
  assert.strictEqual(m.minScale([]), 1);
  assert.strictEqual(m.minScale([{ ...upright, w: 200, h: 100 }]), 1);
  assert.strictEqual(m.minScale([upright, { ...turned, w: 40, h: 80 }]), 0.25);
  assert.strictEqual(m.minScale([{ ...turned, w: 40, h: 80 }]), 0.4);
});
