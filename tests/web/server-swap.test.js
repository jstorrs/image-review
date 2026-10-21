"use strict";
// Another serve on the same socket path and token, against the stub DOM. Run with `node --test tests/web/*.test.js`.
const assert = require("assert");
const test = require("node:test");
const { makePage, manifest, statuses, PLAN, showPlan, plain, UNREVIEWED } = require("./page");

const tests = {};

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

tests["a /current_pass reply with a pass outside 1..1000000 is an unexpected reply"] = async () => {
  for (const pass of [0, 1000001, 1.5, "1"]) {
    const page = makePage({ manifest, statuses });
    await page.respond("/current_pass", page.json(200, { pass }));
    assert.strictEqual(page.els.status.textContent, "unexpected reply from the server", String(pass));
    assert.strictEqual(page.state.dead, true, String(pass));
    assert.deepStrictEqual(page.pending, [], String(pass));
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

// node:test runs these one after another and fails one whose promise is still pending when the event loop empties.
for (const [name, fn] of Object.entries(tests)) test(name, fn);
