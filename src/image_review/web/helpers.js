"use strict";

// The page's pure helpers: no DOM, no state. Loaded before app.js, which uses them.

/** @type {Set<string | undefined>} */
const TODO_STATUSES = new Set(["UNREVIEWED", "FLAGGED"]);
const STATUSES = new Set(["CLEAN", "DIRTY", "UNREVIEWED", "FLAGGED"]);
const MAX_REVIEWER_LENGTH = 64;
// A grid verdict covers every image in it, so grids hold only images not yet judged DIRTY or
// FLAGGED (status.GRID_ELIGIBLE); with the todo filter that leaves UNREVIEWED.
/** @type {Set<string | undefined>} */
const GRID_ELIGIBLE = new Set(["UNREVIEWED", "CLEAN"]);
const MAX_PASS = 1000000; // connection.MAX_PASS

// ---- Shapes ----

/** @typedef {"CLEAN" | "DIRTY" | "UNREVIEWED" | "FLAGGED"} Status */
/** @typedef {Map<string, string>} StatusMap */ // values are Status once the server reply is trusted; app.js still holds them as strings
/** @typedef {{key: string, batch: string}} ManifestRow */
/** @typedef {{key: string, x: number, y: number, w: number, h: number, rotated: boolean, source: [number, number]}} Placement */
/** @typedef {{kind: "single", key: string}} SingleItem */
/** @typedef {{kind: "grid", placements: Placement[], keys: string[]}} GridItem */
/** @typedef {SingleItem | GridItem} Item */
/** @typedef {{grids: Placement[][], leftOut: string[], width: number, height: number}} GridPlan */

// ---- Pure helpers ----

/** @param {string | undefined} status @returns {boolean} */
function isTodo(status) {
  return TODO_STATUSES.has(status);
}

// The client's check; the server's parse_reviewer has the final say.
/** @param {string} name @returns {boolean} */
function validReviewer(name) {
  const length = [...name].length; // code points, as the server counts them
  return length >= 1 && length <= MAX_REVIEWER_LENGTH && name.trim() !== "";
}

// Fisher-Yates, returning a new array.
/** @template T @param {T[]} items @param {() => number} [random] @returns {T[]} */
function shuffle(items, random = Math.random) {
  const result = items.slice();
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}

// A JPEG that starts with SOI and ends with EOI, so a truncated body is refused.
/** @param {Uint8Array} bytes @returns {boolean} */
function isCompleteJpeg(bytes) {
  const n = bytes.length;
  return n >= 4 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[n - 2] === 0xff && bytes[n - 1] === 0xd9;
}

// A review item is {kind: "single", key} or {kind: "grid", placements, keys};
// the keys one verdict covers.
/** @param {Item} item @returns {string[]} */
function itemKeys(item) {
  return item.kind === "grid" ? item.keys : [item.key];
}

// The item's status for the todo list.
/** @param {Item} item @param {StatusMap} statuses @returns {string | undefined} */
function itemStatus(item, statuses) {
  return item.kind === "grid" ? gridStatus(item.keys, statuses) : statuses.get(item.key);
}

// As status.grid_status: DIRTY if any image is not grid-eligible, else
// UNREVIEWED while any is todo, else CLEAN.
/** @param {string[]} keys @param {StatusMap} statuses @returns {Status} */
function gridStatus(keys, statuses) {
  const found = new Set(keys.map((key) => statuses.get(key)));
  if (![...found].every((status) => GRID_ELIGIBLE.has(status))) {
    return "DIRTY";
  }
  return [...found].some(isTodo) ? "UNREVIEWED" : "CLEAN";
}

// As status.grid_clean_refused: CLEAN on a grid holding a DIRTY or FLAGGED
// image is refused, unless the whole grid is DIRTY. The server refuses it too (409).
/** @param {string[]} keys @param {StatusMap} statuses @returns {boolean} */
function gridCleanRefused(keys, statuses) {
  const found = new Set(keys.map((key) => statuses.get(key)));
  const eligible = [...found].every((status) => GRID_ELIGIBLE.has(status));
  return !eligible && !(found.size === 1 && found.has("DIRTY"));
}

// The todo singles of a pass, shuffled: single mode's list.
/** @param {ManifestRow[]} manifest @param {StatusMap} statuses @param {() => number} [random] @returns {Item[]} */
function singleItems(manifest, statuses, random = Math.random) {
  return shuffle(
    manifest.filter((row) => isTodo(statuses.get(row.key))).map((row) => ({ kind: "single", key: row.key })),
    random,
  );
}

// Whether grid mode packs a key: as controller._review_rows with the todo filter,
// todo and grid-eligible, i.e. UNREVIEWED.
/** @param {string | undefined} status @returns {boolean} */
function isGridTodo(status) {
  return isTodo(status) && GRID_ELIGIBLE.has(status);
}

// The keys grid mode packs for `batch`, in manifest order.
/** @param {ManifestRow[]} manifest @param {StatusMap} statuses @param {string} batch @returns {string[]} */
function gridKeys(manifest, statuses, batch) {
  return manifest.filter((row) => row.batch === batch && isGridTodo(statuses.get(row.key))).map((row) => row.key);
}

// The batches holding any key grid mode packs.
/** @param {ManifestRow[]} manifest @param {StatusMap} statuses @returns {Set<string>} */
function gridBatches(manifest, statuses) {
  return new Set(manifest.filter((row) => isGridTodo(statuses.get(row.key))).map((row) => row.batch));
}

// Todo rows of `batch` (null: every batch) that grid mode leaves out, as
// controller._held_back_count with the todo filter: the FLAGGED ones.
/** @param {ManifestRow[]} manifest @param {StatusMap} statuses @param {string | null} batch @returns {number} */
function heldBackCount(manifest, statuses, batch) {
  return manifest.filter((row) => {
    const status = statuses.get(row.key);
    return (batch === null || row.batch === batch) && isTodo(status) && !GRID_ELIGIBLE.has(status);
  }).length;
}

// As controller._held_back_message (in session), or null.
/** @param {number | null} pass @param {number} held @returns {string | null} */
function heldBackMessage(pass, held) {
  if (held === 0) {
    return null;
  }
  const images = held === 1 ? "image needs" : "images need";
  return "No grid items for pass " + pass + "; " + held + " FLAGGED/DIRTY " + images + " single-mode review" +
    " - press [s]";
}

// By code point, as Python's sorted (a plain sort compares UTF-16 code units).
/** @param {string} a @param {string} b @returns {number} */
function compareCodePoints(a, b) {
  // codePointAt(0) is defined: each element is a non-empty code point
  const x = Array.from(a, (c) => /** @type {number} */ (c.codePointAt(0)));
  const y = Array.from(b, (c) => /** @type {number} */ (c.codePointAt(0)));
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    if (x[i] !== y[i]) {
      return x[i] - y[i];
    }
  }
  return x.length - y.length;
}

/** @param {ManifestRow[]} manifest @returns {string[]} */
function sortedBatches(manifest) {
  return [...new Set(manifest.map((row) => row.batch))].sort(compareCodePoints);
}

// As controller.next_batch with wrap: the first batch after `current` (null: from
// the first) that `accepts`, going round once back to `current` itself; null if none.
/** @param {string[]} batches @param {string | null} current @param {(batch: string) => boolean} accepts @returns {string | null} */
function nextBatch(batches, current, accepts) {
  const start = current === null ? -1 : batches.indexOf(current);
  for (let step = 1; step <= batches.length; step++) {
    const batch = batches[(start + step) % batches.length];
    if (accepts(batch)) {
      return batch;
    }
  }
  return null;
}

/** @param {number} value @returns {boolean} */
const isCount = (value) => Number.isInteger(value) && value >= 1;

/** @param {any} p an unparsed server reply @param {number} width @param {number} height @returns {Placement} */
function parsePlacement(p, width, height) {
  if (!p || typeof p !== "object" || typeof p.key !== "string" || typeof p.rotated !== "boolean") {
    throw new BadReply("placement");
  }
  const { key, x, y, w, h, rotated, source } = p;
  if (!Number.isInteger(x) || !Number.isInteger(y) || x < 0 || y < 0 || !isCount(w) || !isCount(h)) {
    throw new BadReply("placement rect");
  }
  if (x + w > width || y + h > height) {
    throw new BadReply("placement outside the grid");
  }
  if (!Array.isArray(source) || source.length !== 2 || !source.every(isCount)) {
    throw new BadReply("placement source");
  }
  const [sw, sh] = source;
  const [fw, fh] = rotated ? [h, w] : [w, h]; // the fitted size, upright
  // layout.fit_size never enlarges and keeps the aspect ratio to within a pixel's rounding
  if (fw > sw || fh > sh || Math.abs(fw * sh - fh * sw) >= sw + sh) {
    throw new BadReply("placement size");
  }
  return { key, x, y, w, h, rotated, source: [sw, sh] };
}

/** @param {Placement} a @param {Placement} b @returns {boolean} */
function overlaps(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

// A /grids reply for `sentKeys` packed into width x height grids, checked whole:
// every sent key exactly once across the grids and left_out, nothing else, and
// integer rects inside the grid that do not overlap.
/** @param {any} reply an unparsed server reply @param {string[]} sentKeys @param {number} width @param {number} height @returns {GridPlan} */
function parseGridPlan(reply, sentKeys, width, height) {
  if (!isCount(width) || !isCount(height)) {
    throw new BadReply("grid size");
  }
  if (!reply || !Array.isArray(reply.grids) || !Array.isArray(reply.left_out)) {
    throw new BadReply("grids");
  }
  const sent = new Set(sentKeys);
  const seen = new Set();
  /** @param {unknown} key */
  const claim = (key) => {
    if (typeof key !== "string" || !sent.has(key) || seen.has(key)) {
      throw new BadReply("grid key");
    }
    seen.add(key);
  };
  const grids = reply.grids.map((/** @type {unknown} */ grid) => {
    if (!Array.isArray(grid) || grid.length === 0) {
      throw new BadReply("grid");
    }
    const placements = grid.map((p) => parsePlacement(p, width, height));
    placements.forEach((p, i) => {
      claim(p.key);
      if (placements.slice(0, i).some((q) => overlaps(p, q))) {
        throw new BadReply("overlapping placements");
      }
    });
    return placements;
  });
  // claim() checks each is a string
  const leftOut = reply.left_out.map((/** @type {string} */ key) => {
    claim(key);
    return key;
  });
  if (seen.size !== sent.size) {
    throw new BadReply("grid keys missing");
  }
  return { grids, leftOut, width, height };
}

/** @param {Placement[]} placements @returns {GridItem} */
function gridItem(placements) {
  return { kind: "grid", placements, keys: placements.map((p) => p.key) };
}

// As controller._grid_items: the grids shuffled, then (stably) the fullest first,
// then each left-out key as a single item.
/** @param {GridPlan} plan @param {() => number} [random] @returns {Item[]} */
function orderGridItems(plan, random = Math.random) {
  const grids = shuffle(plan.grids.map(gridItem), random);
  grids.sort((a, b) => b.keys.length - a.keys.length);
  // Typed as Item[] so the single items may be appended
  return /** @type {Item[]} */ (grids).concat(plan.leftOut.map((key) => ({ kind: "single", key })));
}

// `items` with `failed` keys taken out of grid item `index` (dropped if it is left
// empty) and appended as single items, unless already listed as one: an image
// that cannot be drawn is never covered by a grid verdict.
/** @param {Item[]} items @param {number} index @param {string[]} failed @returns {Item[]} */
function demoteKeys(items, index, failed) {
  const gone = new Set(failed);
  const grid = /** @type {GridItem} */ (items[index]); // callers pass the index of a grid item
  const kept = grid.placements.filter((p) => !gone.has(p.key));
  const result = items.slice();
  if (kept.length > 0) {
    result[index] = gridItem(kept);
  } else {
    result.splice(index, 1);
  }
  const singles = new Set(result.filter((item) => item.kind === "single").map((item) => item.key));
  return result.concat(failed.filter((key) => !singles.has(key)).map((key) => ({ kind: "single", key })));
}

// The canvas matrix [a, b, c, d, e, f] that draws a source image (at its natural
// size, from 0,0) into placement p. A rotated image turns clockwise, as pygame's
// rotate(-90) in grid_packer: its top edge lands on the rect's right edge.
/** @param {Placement} p @returns {number[]} */
function placementTransform(p) {
  const [sw, sh] = p.source;
  if (p.rotated) {
    return [0, p.h / sw, -p.w / sh, 0, p.x + p.w, p.y];
  }
  return [p.w / sw, 0, 0, p.h / sh, p.x, p.y];
}

// The smallest fitted / source ratio among the placements (1 at most), as
// GridSpec.min_scale: the grid's own shrink, before the display scale.
/** @param {Placement[]} placements @returns {number} */
function minScale(placements) {
  return placements.reduce((scale, p) => {
    const [fw, fh] = p.rotated ? [p.h, p.w] : [p.w, p.h];
    return Math.min(scale, fw / p.source[0], fh / p.source[1]);
  }, 1);
}

// The index of the next item whose status is todo, searching forward from `from`
// and wrapping round (including `from` itself last); -1 if none.
/** @param {Item[]} items @param {StatusMap} statuses @param {number} from @returns {number} */
function nextTodoIndex(items, statuses, from) {
  for (let step = 1; step <= items.length; step++) {
    const i = (from + step) % items.length;
    if (isTodo(itemStatus(items[i], statuses))) {
      return i;
    }
  }
  return -1;
}

/** @param {Item[]} items @param {StatusMap} statuses @returns {number} */
function countTodo(items, statuses) {
  return items.filter((item) => isTodo(itemStatus(item, statuses))).length;
}

// "k1 is S1, k2 is S2, k3 is S3 and N more"
/** @param {StatusMap} statuses @returns {string} */
function describeStatuses(statuses) {
  const entries = [...statuses];
  const shown = entries.slice(0, 3).map(([key, status]) => key + " is " + status);
  const more = entries.length > 3 ? " and " + (entries.length - 3) + " more" : "";
  return shown.join(", ") + more;
}

class BadReply extends Error {}

/** @param {any} reply an unparsed server reply @returns {number} */
function parsePass(reply) {
  const value = reply && reply.pass;
  if (!Number.isInteger(value) || value < 1 || value > MAX_PASS) {
    throw new BadReply("pass");
  }
  return value;
}

/** @param {any} reply an unparsed server reply @returns {ManifestRow[]} */
function parseManifest(reply) {
  if (!Array.isArray(reply)) {
    throw new BadReply("manifest");
  }
  return reply.map((row) => {
    if (!row || typeof row.key !== "string" || typeof row.batch !== "string") {
      throw new BadReply("manifest row");
    }
    return { key: row.key, batch: row.batch };
  });
}

/** @param {any} reply an unparsed server reply @returns {StatusMap} */
function parseStatusMap(reply) {
  if (!reply || typeof reply !== "object" || Array.isArray(reply)) {
    throw new BadReply("statuses");
  }
  const result = new Map();
  for (const [key, status] of Object.entries(reply)) {
    if (!STATUSES.has(status)) {
      throw new BadReply("status");
    }
    result.set(key, status);
  }
  return result;
}
