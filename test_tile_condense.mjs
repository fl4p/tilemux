#!/usr/bin/env node
// Regression test for the tile condense/expand ANIMATION (the "park as a card"
// glide).
//
// Bug this guards against: "the tile condense animation is gone." A redesign of
// condense (genuinely-narrow card box via `flex-basis:var(--cond-peek)` + a
// small `--cond-tuck` margin, instead of the old full-width box + huge negative
// margin) dropped the `transition` line off the base `.tile` rule entirely, so
// condensing/expanding SNAPPED with no animation at all.
//
// The fix has two halves that only work TOGETHER, so the test asserts both:
//
//   1. The base `.tile` rule animates `flex-basis` (the box shrinking to a
//      spine) and `margin` (the tuck under the right neighbour). Without this,
//      condense snaps — the reported regression.
//
//   2. EVERY row tile's iframe is pinned to a constant width
//      (`#grid.row > .tile > iframe`), not just condensed ones. This is the
//      safety invariant that makes (1) acceptable: the iframe no longer follows
//      the box at width:100%, so animating the box's flex-basis can't drag the
//      iframe through intermediate widths and re-fire fit→SIGWINCH (which would
//      reflow the PTY and hard-wrap scrollback). A naive "just re-add the
//      transition" fix that left the pin gated on `.condensed` would bring the
//      animation back but reintroduce the SIGWINCH storm on expand — so this
//      half must hold too.
//
// We assert against the REAL CSS embedded in serve.py (the shipped dashboard
// stylesheet), so the test fails the moment either half is removed again.
import fs from 'fs';
import path from 'path';
import url from 'url';
import assert from 'assert';

const __dirname = path.dirname(url.fileURLToPath(import.meta.url));
const serveSrc = fs.readFileSync(path.join(__dirname, 'serve.py'), 'utf8');

// Pull the main dashboard stylesheet: the <style>…</style> block that carries
// the condense rules (serve.py has several embedded page templates, so we pick
// by content, not position). Strip CSS comments so brace/keyword matching isn't
// fooled by prose in the (heavily) commented source.
function dashboardCss(src) {
  const re = /<style>([\s\S]*?)<\/style>/g;
  let m;
  while ((m = re.exec(src))) {
    if (m[1].includes('.tile.condensed')) {
      return m[1].replace(/\/\*[\s\S]*?\*\//g, '');   // drop /* … */ comments
    }
  }
  throw new assert.AssertionError({ message: 'could not find the dashboard <style> block (none contained `.tile.condensed`)' });
}

const css = dashboardCss(serveSrc);

// --- (1) the base `.tile` rule must animate the condense glide --------------
// Match the BASE rule `  .tile { … }` only — not `.tile.condensed`,
// `.tile.closing`, `.tile > .head`, `.tile iframe`, etc. `\.tile \{` requires
// `.tile` immediately followed by `{`, which the qualified selectors don't have.
const tileRule = css.match(/\n {2}\.tile \{([^}]*)\}/);
assert.ok(tileRule, 'could not find the base `.tile { … }` rule in the dashboard CSS');

const tileDecls = tileRule[1];
const transitionDecl = tileDecls.match(/transition\s*:\s*([^;]+)/);
assert.ok(
  transitionDecl,
  'the base `.tile` rule has NO `transition` — condense/expand snaps with no ' +
  'animation (the reported "tile condense animation is gone" regression).'
);

const transition = transitionDecl[1];
assert.ok(
  /flex-basis/.test(transition),
  '`.tile` transition does not animate `flex-basis`, so the card box snapping ' +
  'from full width to a spine is not animated. Got: ' + JSON.stringify(transition)
);
assert.ok(
  /\bmargin\b/.test(transition),
  '`.tile` transition does not animate `margin`, so the tuck under the right ' +
  'neighbour is not animated. Got: ' + JSON.stringify(transition)
);

// --- (2) the row-mode iframe pin must cover EVERY tile, not just condensed ---
// This is what makes animating flex-basis safe (constant iframe width → no
// fit→SIGWINCH). Must be the broad `#grid.row > .tile > iframe`, and crucially
// NOT gated behind `.condensed`.
const broadPin = /#grid\.row\s*>\s*\.tile\s*>\s*iframe\s*\{[^}]*width\s*:/m.test(css);
assert.ok(
  broadPin,
  'no `#grid.row > .tile > iframe { width: … }` rule — the iframe is not pinned ' +
  'for non-condensed row tiles, so animating the box flex-basis on expand would ' +
  'drag the width:100% iframe through intermediate widths and re-fire ' +
  'fit→SIGWINCH (reflowing the PTY, hard-wrapping scrollback).'
);

// And the pin must apply to the channel/note half-width tiles too, otherwise
// those would re-fit on condense/expand.
const broadPinKinds = /#grid\.row\s*>\s*\.tile\[data-kind="channel"\]\s*>\s*iframe/.test(css);
assert.ok(
  broadPinKinds,
  'the row-mode iframe pin does not cover channel/note tiles ' +
  '(`#grid.row > .tile[data-kind="channel"] > iframe`).'
);

// --- (3) sanity: condensing actually shrinks the box, so there IS something to
// animate. The condensed box collapses to the --cond-peek spine via flex-basis.
const condensedRule = css.match(/#grid\.row\s*>\s*\.tile\.condensed\s*\{([^}]*)\}/);
assert.ok(condensedRule, 'could not find the `#grid.row > .tile.condensed` rule');
assert.ok(
  /flex\s*:\s*0\s+0\s+var\(--cond-peek\)/.test(condensedRule[1]),
  'condensed tiles no longer collapse their box to the --cond-peek spine; ' +
  'the flex-basis animation would have nothing to animate. Got: ' +
  JSON.stringify(condensedRule[1])
);

console.log('PASS test_tile_condense.mjs — condense animation present and SIGWINCH-safe');
