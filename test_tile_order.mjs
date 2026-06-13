#!/usr/bin/env node
// Regression tests for the dashboard tile-ordering helper placeNewInOrder()
// (defined in the embedded client JS inside serve.py).
//
// Bugs this guards against (both reported together, one root cause):
//   1. "When cloning a tile the new tile appears at the very right end of the
//      list, not next to the current tile."
//   2. "Order of tiles is not preserved across reload/tab-switch."
//
// A clone only lands next to its source while the in-memory `pendingDups`
// placeholder window is alive (~20 s) AND in the very window that clicked
// duplicate. If the clone boots slower than that window, or the page is
// reloaded mid-boot, or another window learns about the session first, the old
// code dropped it at the FAR RIGHT of the whole row (push at end) — and because
// that placement was server-(started,id)-derived and never persisted, it
// reverted to far-right on every reload. The fix slots an un-matched session in
// right after the LAST tile that shares its cwd, which is deterministic across
// windows and reloads, so a clone stays next to its source's group.
//
// We extract the REAL placeNewInOrder() source from serve.py and run it against
// a stubbed environment (orderList / pendingDups / tiles / Date.now), so this
// exercises the shipped algorithm, not a reimplementation.
import fs from 'fs';
import path from 'path';
import url from 'url';
import assert from 'assert';

const __dirname = path.dirname(url.fileURLToPath(import.meta.url));

// Pull out a top-level `function name(...) { ... }` by brace-matching.
function extractFn(src, name) {
  const s = src.indexOf('function ' + name + '(');
  assert.ok(s >= 0, 'could not find ' + name + ' in serve.py');
  let i = src.indexOf('{', s), depth = 0, end = -1;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  return src.slice(s, end);
}

const serveSrc = fs.readFileSync(path.join(__dirname, 'serve.py'), 'utf8');
const fnSrc = extractFn(serveSrc, 'placeNewInOrder');

// Build a callable that runs the extracted function with injected globals.
// orderList is mutated in place (splice/push); pendingDups is reassigned
// internally (filter) but we only assert on orderList + the boolean return.
const run = new Function(
  's', '_orderList', '_pendingDups', '_tiles', '_now',
  `let orderList = _orderList;
   let pendingDups = _pendingDups;
   const tiles = _tiles;
   const Date = { now: () => _now };
   ${fnSrc}
   const ret = placeNewInOrder(s);
   return { ret, orderList };`
);

// tiles is a Map<id, {dataset:{cwd}}>; build one from a {id: cwd} object.
function tilesFrom(map) {
  const m = new Map();
  for (const [id, cwd] of Object.entries(map)) m.set(id, { dataset: { cwd } });
  return m;
}

let passed = 0;
function test(name, fn) {
  fn();
  passed++;
  console.log('  ok - ' + name);
}

const FUTURE = 9e15; // pendingDup `until` far in the future
const NOW = 1000;

// 1. The core fix: an un-matched clone slots in after the LAST tile sharing its
//    cwd, NOT at the far right. Source group is /projB = {beta, vls}; clone has
//    cwd /projB, so it lands right after vls (the last /projB tile).
test('un-matched clone lands after its cwd-group, not at the end', () => {
  const tiles = tilesFrom({
    a: '/projA', beta: '/projB', vls: '/projB', solo: '/solo',
  });
  const { ret, orderList } = run(
    { id: 'clone', cwd: '/projB' },
    ['a', 'beta', 'vls', 'solo'], [], tiles, NOW);
  assert.strictEqual(ret, false);
  assert.deepStrictEqual(orderList, ['a', 'beta', 'vls', 'clone', 'solo']);
});

// 2. A genuinely new session in a cwd nothing else shares appends at the end
//    (no group to join) — unchanged behaviour.
test('new session in a fresh cwd appends at the end', () => {
  const tiles = tilesFrom({ a: '/projA', b: '/projB' });
  const { ret, orderList } = run(
    { id: 'x', cwd: '/elsewhere' },
    ['a', 'b'], [], tiles, NOW);
  assert.strictEqual(ret, false);
  assert.deepStrictEqual(orderList, ['a', 'b', 'x']);
});

// 3. An empty cwd (notes/channels) is NOT grouped — it appends at the end even
//    when other empty-cwd tiles exist, so blank-cwd tiles don't clump oddly.
test('empty-cwd session is not grouped, appends at the end', () => {
  const tiles = tilesFrom({ note1: '', a: '/projA', note2: '' });
  const { ret, orderList } = run(
    { id: 'note3', cwd: '' },
    ['note1', 'a', 'note2'], [], tiles, NOW);
  assert.strictEqual(ret, false);
  assert.deepStrictEqual(orderList, ['note1', 'a', 'note2', 'note3']);
});

// 4. Regression for the in-window happy path: a live pendingDup whose marker is
//    still in the order swaps the real id into the placeholder's exact slot.
test('live pendingDup with marker swaps real id into the placeholder slot', () => {
  const tiles = tilesFrom({ a: '/projA', beta: '/projB' });
  const pd = [{ cwd: '/projB', srcId: 'beta', marker: '__m', until: FUTURE }];
  const { ret, orderList } = run(
    { id: 'clone', cwd: '/projB' },
    ['a', 'beta', '__m', 'solo'], pd, tiles, NOW);
  assert.strictEqual(ret, true);
  assert.deepStrictEqual(orderList, ['a', 'beta', 'clone', 'solo']);
});

// 5. Reload path: a pendingDup whose marker is gone (page reloaded, placeholder
//    DOM lost) still places the clone right after its source id.
test('pendingDup with a missing marker places after the source id', () => {
  const tiles = tilesFrom({ a: '/projA', beta: '/projB', vls: '/projB' });
  const pd = [{ cwd: '/projB', srcId: 'beta', marker: '__gone', until: FUTURE }];
  const { ret, orderList } = run(
    { id: 'clone', cwd: '/projB' },
    ['a', 'beta', 'vls'], pd, tiles, NOW);
  assert.strictEqual(ret, true);
  assert.deepStrictEqual(orderList, ['a', 'beta', 'clone', 'vls']);
});

// 6. The slow-boot bug itself: an EXPIRED pendingDup must NOT strand the clone
//    at the far right — it falls through to deterministic cwd-group placement.
test('expired pendingDup falls through to cwd-group placement (not far right)', () => {
  const tiles = tilesFrom({ a: '/projA', beta: '/projB', vls: '/projB' });
  const pd = [{ cwd: '/projB', srcId: 'beta', marker: '__old', until: NOW - 1 }];
  const { ret, orderList } = run(
    { id: 'clone', cwd: '/projB' },
    ['a', 'beta', 'vls'], pd, tiles, NOW);
  assert.strictEqual(ret, false);
  assert.deepStrictEqual(orderList, ['a', 'beta', 'vls', 'clone']);
});

// 7. Determinism across reloads: placing the same clone into the same
//    (saved order + tiles) twice yields the identical row both times — this is
//    what keeps the order stable across reloads without persisting it.
test('placement is deterministic (stable across reloads)', () => {
  const tiles = tilesFrom({ a: '/projA', beta: '/projB', vls: '/projB' });
  const r1 = run({ id: 'clone', cwd: '/projB' }, ['a', 'beta', 'vls'], [], tiles, NOW);
  const r2 = run({ id: 'clone', cwd: '/projB' }, ['a', 'beta', 'vls'], [], tiles, NOW);
  assert.deepStrictEqual(r1.orderList, r2.orderList);
  assert.deepStrictEqual(r1.orderList, ['a', 'beta', 'vls', 'clone']);
});

// 8. cwd normalization: the spawned session's recorded cwd has a trailing
//    slash the source's didn't. It must still match its placeholder (marker
//    swap), not drift to the right of its group.
test('trailing-slash cwd still matches its placeholder via normalization', () => {
  const tiles = tilesFrom({ a: '/projA', beta: '/projB' });
  const pd = [{ cwd: '/projB', srcId: 'beta', marker: '__m', until: FUTURE }];
  const { ret, orderList } = run(
    { id: 'clone', cwd: '/projB/' },          // trailing slash on the clone
    ['a', 'beta', '__m', 'tail'], pd, tiles, NOW);
  assert.strictEqual(ret, true);
  assert.deepStrictEqual(orderList, ['a', 'beta', 'clone', 'tail']);
});

// 9. Sole-placeholder claim: the spawned session's cwd doesn't match at all
//    (e.g. a symlink-resolved $PWD or a container WORKSPACE path), but it's the
//    only dup in flight — so it claims that placeholder rather than drifting.
test('sole in-flight placeholder is claimed even when cwd does not match', () => {
  const tiles = tilesFrom({ a: '/projA', beta: '/realpath/projB' });
  const pd = [{ cwd: '/projB', srcId: 'beta', marker: '__m', until: FUTURE }];
  const { ret, orderList } = run(
    { id: 'clone', cwd: '/realpath/projB' },  // resolved path != source's /projB
    ['a', 'beta', '__m', 'tail'], pd, tiles, NOW);
  assert.strictEqual(ret, true);
  assert.deepStrictEqual(orderList, ['a', 'beta', 'clone', 'tail']);
});

// 10. With TWO dups in flight and no cwd match, do NOT blindly claim one — the
//     sole-placeholder shortcut only applies when exactly one is pending. An
//     unmatched session falls through to deterministic cwd-group placement.
test('two pending dups + no cwd match does not mis-claim a placeholder', () => {
  const tiles = tilesFrom({ a: '/projA', beta: '/projB', solo: '/solo' });
  const pd = [
    { cwd: '/projA', srcId: 'a', marker: '__m1', until: FUTURE },
    { cwd: '/projB', srcId: 'beta', marker: '__m2', until: FUTURE },
  ];
  const { ret, orderList } = run(
    { id: 'x', cwd: '/elsewhere' },
    ['a', '__m1', 'beta', '__m2', 'solo'], pd, tiles, NOW);
  assert.strictEqual(ret, false);
  // /elsewhere has no cwd-mate among real tiles → appends at the end; crucially
  // it did NOT steal __m1 or __m2.
  assert.deepStrictEqual(orderList, ['a', '__m1', 'beta', '__m2', 'solo', 'x']);
});

console.log(`\nplaceNewInOrder: ${passed} tests passed`);
