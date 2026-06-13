#!/usr/bin/env node
// Tests for the prompt-detection bell in term-client.js.
//
// The bell is what the dashboard surfaces as "tile wants attention / went
// idle": the tile client posts {bell:true} and the dashboard flashes + chimes.
// Two trigger paths — a synchronous scan for the Notification hook's bare BEL
// byte (scanBel), and a regex over a rolling window for claude's prompt sigils
// (maybePromptBell) — share one dedup/mute gate (bellOnce). These tests pin the
// fixes for the delayed/false-bell reports:
//   • a sigil that rang is CONSUMED, so later output can't re-ring it
//   • a (re)connect mutes the attach replay (stale prompts / replayed BELs)
//   • BEL is detected in the websocket chunk, not via xterm's throttleable
//     parser — and OSC/DCS-embedded \x07 (titles, inline images) stays silent
//
// We load the REAL bell section out of term-client.js (marker-sliced) and run
// it with an injected clock + postBell spy, so this exercises shipped code.
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, 'term-client.js'), 'utf8');
const START = '// --- prompt-detection bell ---';
const END = '// --- end prompt-detection bell ---';
const s = src.indexOf(START), e = src.indexOf(END);
assert.ok(s >= 0, 'start marker missing in term-client.js');
assert.ok(e > s, 'end marker missing in term-client.js');
const section = src.slice(s, e);

// Build an isolated bell instance with a controllable clock and a ring spy.
function mkBell() {
  const rings = [];
  let now = 0;
  const factory = new Function('postBell', 'performance', section +
    '\nreturn { maybePromptBell: maybePromptBell, bellOnce: bellOnce,' +
    ' scanBel: scanBel, bellOnConnect: bellOnConnect };');
  const api = factory(() => rings.push(now), { now: () => now });
  return {
    rings,
    feed: (text) => api.maybePromptBell(text),
    connect: () => api.bellOnConnect(),
    at: (t, fn) => { now = t; fn(); },
    set: (t) => { now = t; },
  };
}
// Most tests want a tile past its connect-mute: connected at t=0, acting at t≥2000.
function mkLive() {
  const b = mkBell();
  b.at(0, b.connect);
  b.set(2000);
  return b;
}

let pass = 0;
function t(name, fn) { fn(); pass++; console.log('  ok -', name); }

// --- hook-BEL path (scanBel) -------------------------------------------------

t('bare BEL rings', () => {
  const b = mkLive();
  b.feed('\x07');
  assert.strictEqual(b.rings.length, 1);
});

t('OSC title terminator BEL stays silent', () => {
  const b = mkLive();
  b.feed('\x1b]0;dtach claude\x07');
  assert.strictEqual(b.rings.length, 0);
});

t('OSC spanning websocket frames stays silent, next bare BEL rings', () => {
  const b = mkLive();
  b.feed('\x1b]1337;File=inline=1:');   // IIP image opener — payload continues…
  b.feed('aGVsbG8gd29ybGQ=\x07');       // …and ends with the BEL terminator
  assert.strictEqual(b.rings.length, 0);
  b.set(3000);
  b.feed('\x07');                        // genuine hook bell after the image
  assert.strictEqual(b.rings.length, 1);
});

t('ESC split across frames still enters OSC mode', () => {
  const b = mkLive();
  b.feed('\x1b');
  b.feed(']0;title\x07');
  assert.strictEqual(b.rings.length, 0);
});

t('BEL inside a DCS payload stays silent until ST', () => {
  const b = mkLive();
  b.feed('\x1bPqdata\x07moredata\x1b\\');   // \x07 is payload here, not a ring
  assert.strictEqual(b.rings.length, 0);
  b.set(3000);
  b.feed('\x07');
  assert.strictEqual(b.rings.length, 1);
});

t('CAN aborts a string mode so a later bare BEL rings', () => {
  const b = mkLive();
  b.feed('\x1b]0;never-terminated\x18');    // CAN abort
  b.feed('\x07');
  assert.strictEqual(b.rings.length, 1);
});

// --- prompt-sigil path (maybePromptBell) --------------------------------------

t('permission prompt text rings', () => {
  const b = mkLive();
  b.feed('Do you want to proceed?');
  assert.strictEqual(b.rings.length, 1);
});

t('colourised selection menu rings (CSI stripped before match)', () => {
  const b = mkLive();
  b.feed('\x1b[36m❯\x1b[0m 1. Yes');
  assert.strictEqual(b.rings.length, 1);
});

t('a rung sigil is consumed — later output cannot re-ring it', () => {
  const b = mkLive();
  b.feed('Do you want to proceed?');
  assert.strictEqual(b.rings.length, 1);
  // Post-answer output flows; the old sigil would still sit inside the 2 KB
  // window and re-matched on every chunk > 600ms apart before the fix.
  b.set(3000); b.feed('tool output line 1\r\n');
  b.set(4000); b.feed('tool output line 2\r\n');
  b.set(9000); b.feed('tool output line 3\r\n');
  assert.strictEqual(b.rings.length, 1);
});

t('a freshly re-drawn prompt rings again', () => {
  const b = mkLive();
  b.feed('Do you want to proceed?');
  b.set(4000);
  b.feed('Do you want to proceed?');   // a NEW prompt re-draws its sigil
  assert.strictEqual(b.rings.length, 2);
});

// --- dedup window --------------------------------------------------------------

t('hook BEL + pattern for the same prompt collapse into one ring', () => {
  const b = mkLive();
  b.feed('Do you want to proceed? (y/n)\x07');   // both paths fire in one chunk
  assert.strictEqual(b.rings.length, 1);
  b.set(2100);
  b.feed('\x07');                                 // hook echo 100ms later — deduped
  assert.strictEqual(b.rings.length, 1);
});

t('distinct bells more than 600ms apart both ring', () => {
  const b = mkLive();
  b.feed('\x07');
  b.set(2700);
  b.feed('\x07');
  assert.strictEqual(b.rings.length, 2);
});

// --- connect mute (attach replay) ----------------------------------------------

t('replayed prompt text inside the mute window stays silent', () => {
  const b = mkBell();
  b.at(0, b.connect);
  b.at(100, () => b.feed('Do you want to proceed?\r\n❯ 1. Yes'));   // reattach repaint
  assert.strictEqual(b.rings.length, 0);
});

t('replayed BEL inside the mute window stays silent', () => {
  const b = mkBell();
  b.at(0, b.connect);
  b.at(100, () => b.feed('old shell history\x07'));   // ttyd bare-text re-dump
  assert.strictEqual(b.rings.length, 0);
});

t('a muted replay sigil cannot resurface after the mute expires', () => {
  const b = mkBell();
  b.at(0, b.connect);
  b.at(100, () => b.feed('Do you want to proceed?'));   // consumed silently
  b.at(2000, () => b.feed('plain post-replay output'));  // must NOT ring on the leftover
  assert.strictEqual(b.rings.length, 0);
});

t('a genuinely new prompt right after the mute window rings', () => {
  const b = mkBell();
  b.at(0, b.connect);
  b.at(1600, () => b.feed('Do you want to proceed?'));
  assert.strictEqual(b.rings.length, 1);
});

t('reconnect resets a half-open string mode', () => {
  const b = mkLive();
  b.feed('\x1b]0;osc left dangling by the dying socket');   // never terminated
  b.at(10000, b.connect);                                   // reconnect
  b.at(12000, () => b.feed('\x07'));                        // must ring, not be eaten as OSC payload
  assert.strictEqual(b.rings.length, 1);
});

t('reconnect mute applies to the new connection time, not page load', () => {
  const b = mkLive();
  b.at(60000, b.connect);                                   // reconnect at t=60s
  b.at(60100, () => b.feed('\x07'));                        // replay echo — muted
  assert.strictEqual(b.rings.length, 0);
  b.at(61600, () => b.feed('\x07'));                        // live bell after the window
  assert.strictEqual(b.rings.length, 1);
});

console.log(`\n${pass} tests passed`);
