#!/usr/bin/env python3
"""NON-HEADLESS regression test for the silent WebGL-context-loss blank-tile bug.

The bug: on a dashboard reload, a tile with an actively-working claude session
comes up COMPLETELY BLANK while idle ones recover; the ↻ button fixes it. Cause:
under the browser's per-page WebGL-context cap, the browser evicts a tile's GL
context — and it does so WITHOUT firing webglcontextlost, so onContextLoss never
demotes us. webglAddon still looks valid but its context is dead, every refresh
paints nothing, and the tile is blank with a full buffer.

Headless Chromium uses SwiftShader, which has effectively no per-page cap, so the
eviction can't be provoked there. THIS test runs HEADED (real ANGLE/Metal/GL
backend) where the cap is real (16 on this machine). It reproduces the eviction
for real by exhausting the cap from the top page, and does a clean A/B:

  • recovery DISABLED (window.__disableGlRecover) → an evicted tile is left on a
    dead WebGL context (real gl.isContextLost() === true) and renders BLANK — the
    bug, captured both as state AND as a near-uniform element screenshot.
  • recovery ENABLED → the watchdog detects the dead context via the REAL
    isContextLost() and demotes to the 2D canvas; the tile repaints (screenshot
    regains contrast) with its scrollback intact.

This is the piece headless could not exercise: detecting and curing a genuinely
lost GPU context. Auto-skips if a headed browser can't be launched (no display).

Run:
    .venv-test/bin/python3 -m unittest test_tile_gl_recover_headed -v
"""
import os
import unittest

from tile_harness import TileHarness
from playwright.sync_api import sync_playwright, Error as PWError


def find_frame(page, port):
    pats = ("127.0.0.1:%d/" % port, "/t/%d/" % port, ":%d/" % port)
    return next((f for f in page.frames if any(p in (f.url or "") for p in pats)), None)


# Per-tile renderer + REAL gl.isContextLost() + painted-buffer line count.
STATE = """() => {
  const t = window.__claudeTerm;
  const scr = t && t.element && t.element.querySelector('.xterm-screen');
  const cs = scr ? scr.querySelectorAll('canvas') : [];
  let lost = null;
  for (const c of cs) {
    if (c.dataset && c.dataset.frozenOverlay) continue;
    let g = null;
    try { g = c.getContext('webgl2'); } catch (e) {}
    if (!g) { try { g = c.getContext('webgl'); } catch (e) {} }
    if (g) { lost = g.isContextLost(); break; }
  }
  const b = t.buffer.active; let n = 0;
  for (let i = 0; i < b.length; i++) {
    const ln = b.getLine(i);
    if (ln && ln.translateToString(true).trim() !== '') n++;
  }
  const d = window.__tileDiag || {};
  return { renderer: d.renderer, glRecover: d.glRecover, isLost: lost, bufLines: n };
}"""

# Blow past the per-page WebGL context cap from the TOP page, forcing the browser
# to evict the OLDEST contexts — the tile iframes', created first at page load.
EXHAUST = """() => {
  window.__hog = window.__hog || [];
  for (let i = 0; i < 24; i++) {
    const c = document.createElement('canvas');
    const g = c.getContext('webgl');
    if (g) window.__hog.push(g);
  }
  return window.__hog.length;
}"""


def _png_size(png_bytes):
    """'Is this blank' metric with no image library: the COMPRESSED PNG byte size.
    A near-uniform (blank) tile compresses to a few KB; a tile full of rendered
    text is several times larger. Measured live: healthy≈13.5 KB, blank≈2.7 KB,
    recovered≈13 KB — a robust >2x margin (distinct-byte count, by contrast,
    saturates at 256 for both and can't tell them apart)."""
    return len(png_bytes)


class GlRecoverHeadedTest(unittest.TestCase):
    def _launch(self, pw):
        try:
            return pw.chromium.launch(headless=False)
        except PWError as e:
            raise unittest.SkipTest("no headed browser available: %s" % e)

    def test_real_context_eviction_blanks_then_recovers(self):
        h = TileHarness()
        cwd = os.path.join(h.home, "glheaded")
        # A few actively-streaming tiles, like working claude sessions.
        for i in range(3):
            h.add_tile("T%d" % i, cwd, nlines=40, stream_count=400, stream_every=0.05)
        h.start()
        try:
            with sync_playwright() as pw:
                br = self._launch(pw)
                page = br.new_page()
                page.goto(h.url)
                page.wait_for_timeout(3500)

                frames = {t["label"]: find_frame(page, t["port"]) for t in h.tiles}
                self.assertTrue(all(frames.values()), "missing tile frames")
                for label, fr in frames.items():
                    st = fr.evaluate(STATE)
                    self.assertEqual(st["renderer"], "webgl",
                                     "%s did not start on WebGL: %r" % (label, st))
                    self.assertGreater(st["bufLines"], 5, "%s had no content" % label)

                # Healthy baseline screenshot (tile painting normally on WebGL).
                t0 = h.tiles[0]["label"]
                healthy_size = _png_size(frames[t0].frame_element().screenshot())

                # ---- Phase 1: recovery OFF → reproduce the bug (real dead ctx) ----
                # __forceSilentGlLoss models the bug's defining condition: the
                # browser drops the context with no usable loss event, so the
                # event-driven demote never runs. __disableGlRecover then turns off
                # OUR poll-based cure too — nothing recovers, the bug stands.
                for fr in frames.values():
                    fr.evaluate("() => { window.__forceSilentGlLoss = true; "
                                "window.__disableGlRecover = true; }")
                made = page.evaluate(EXHAUST)
                self.assertGreaterEqual(made, 16, "could not allocate enough GL contexts")
                page.wait_for_timeout(4500)  # > watchdog interval; it tries but is disabled

                broken = []
                for label, fr in frames.items():
                    st = fr.evaluate(STATE)
                    if st["renderer"] == "webgl" and st["isLost"] is True:
                        broken.append((label, fr, st))
                self.assertTrue(
                    broken,
                    "expected at least one tile left on a real dead WebGL context "
                    "with recovery disabled — none were; states: %r"
                    % {l: f.evaluate(STATE) for l, f in frames.items()})

                # The broken tile is BLANK on screen (real lost GPU context): its
                # composited frame compresses to a fraction of the healthy frame.
                # Use t0, which we baselined, so the comparison is apples-to-apples.
                self.assertIn(t0, [b[0] for b in broken],
                              "baselined tile %s was not among the broken tiles" % t0)
                fr = frames[t0]
                el = fr.frame_element()
                blank_size = _png_size(el.screenshot())
                self.assertLess(
                    blank_size, healthy_size * 0.5,
                    "%s should have gone blank on a dead GL context but didn't "
                    "(healthy=%d, blank=%d)" % (t0, healthy_size, blank_size))

                # ---- Phase 2: recovery ON → watchdog cures it ----
                fr.evaluate("() => { window.__disableGlRecover = false; }")
                fr.wait_for_function(
                    "() => window.__tileDiag && window.__tileDiag.glRecover >= 1",
                    timeout=8000,
                )
                st2 = fr.evaluate(STATE)
                self.assertGreaterEqual(st2["glRecover"], 1,
                                        "%s: silent loss never recovered: %r" % (t0, st2))
                self.assertEqual(st2["renderer"], "canvas",
                                 "%s: did not demote to canvas: %r" % (t0, st2))
                self.assertGreaterEqual(st2["bufLines"], 5,
                                        "%s: scrollback lost across recovery: %r" % (t0, st2))

                page.wait_for_timeout(600)  # let the canvas paint
                fixed_size = _png_size(el.screenshot())

                # The recovered tile repaints — its frame is back to roughly the
                # healthy size, far above the blank frame.
                self.assertGreater(
                    fixed_size, blank_size * 2,
                    "%s did not visibly repaint after recovery "
                    "(blank=%d, fixed=%d, healthy=%d)"
                    % (t0, blank_size, fixed_size, healthy_size))
                br.close()
        finally:
            h.stop()


if __name__ == "__main__":
    unittest.main()
