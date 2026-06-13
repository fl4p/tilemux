#!/usr/bin/env python3
"""Browser regression test for the SILENT WebGL-context-loss recovery.

Reported bug: on a dashboard reload, a tile with an actively-working claude
session ("thinking…") comes up COMPLETELY BLANK while idle tiles recover; the
per-tile ↻ reload button restores it. Root cause: under the browser's per-page
WebGL-context cap, Chrome can evict a tile's GL context with NO webglcontextlost
event — onContextLoss never demotes us, so webglAddon still looks valid but its
context is dead and every _burstHeal repaints onto it for nothing (buffer full,
screen blank). The fix detects the dead context via gl.isContextLost() (a
periodic watchdog + the post-reattach passes) and demotes to the 2D canvas, which
always paints — the same cure onContextLoss applies when the event DOES fire.

Headless Chromium uses SwiftShader with effectively no per-page cap, so real
eviction can't be provoked here. We instead force-trip the detector via the
window.__forceGlLostForTest seam (honoured by _glLost) and assert the watchdog
recovers: renderer demotes to canvas, the recovery counter increments, and the
buffer content is preserved (so no scrollback was lost in the swap).

Run:
    .venv-test/bin/python3 -m unittest test_tile_gl_recover_browser -v
"""
import os
import unittest

from tile_harness import TileHarness
from playwright.sync_api import sync_playwright


def find_frame(page, port):
    pats = ("127.0.0.1:%d/" % port, "/t/%d/" % port, ":%d/" % port)
    return next((f for f in page.frames if any(p in (f.url or "") for p in pats)), None)


BUF_LINES = """() => {
  const t = window.__claudeTerm; if (!t) return -1;
  const b = t.buffer.active; let n = 0;
  for (let i = 0; i < b.length; i++) {
    const ln = b.getLine(i);
    if (ln && ln.translateToString(true).trim() !== '') n++;
  }
  return n;
}"""


class GlRecoverTest(unittest.TestCase):
    def test_silent_context_loss_recovers_to_canvas(self):
        h = TileHarness()
        cwd = os.path.join(h.home, "glrec")
        # A couple of streaming tiles so at least one is actively painting, like a
        # working claude. (The recovery is renderer-level, so the program's state
        # only matters for realism.)
        h.add_tile("A", cwd, nlines=40, stream_count=200, stream_every=0.05)
        h.add_tile("B", cwd, nlines=40, stream_count=200, stream_every=0.05)
        h.start()
        try:
            with sync_playwright() as pw:
                br = pw.chromium.launch()
                page = br.new_page()
                page.goto(h.url)
                page.wait_for_timeout(3500)

                fr = find_frame(page, h.tiles[0]["port"])
                self.assertIsNotNone(fr, "tile frame not found")

                # Baseline: on WebGL with real content painted.
                diag0 = fr.evaluate("() => window.__tileDiag")
                self.assertEqual(diag0.get("renderer"), "webgl",
                                 "tile did not start on WebGL: %r" % diag0)
                lines0 = fr.evaluate(BUF_LINES)
                self.assertGreater(lines0, 5, "tile had no content to begin with")

                # Simulate a silent eviction (context dead, no event fired).
                fr.evaluate("() => { window.__forceGlLostForTest = true; }")

                # Watchdog runs every 3 s; give it a comfortable margin.
                fr.wait_for_function(
                    "() => window.__tileDiag && window.__tileDiag.glRecover >= 1",
                    timeout=8000,
                )

                diag1 = fr.evaluate("() => window.__tileDiag")
                self.assertGreaterEqual(diag1.get("glRecover", 0), 1,
                                        "silent GL loss was never detected: %r" % diag1)
                self.assertEqual(diag1.get("renderer"), "canvas",
                                 "did not demote to canvas after GL loss: %r" % diag1)

                # Content survived the renderer swap (no scrollback lost, not blank).
                lines1 = fr.evaluate(BUF_LINES)
                self.assertGreaterEqual(
                    lines1, 5,
                    "buffer went blank/shrank across GL recovery: %d -> %d" % (lines0, lines1))

                # Once on canvas the watchdog must NOT keep thrashing (webglAddon is
                # gone, so the guard makes it a no-op even with the flag still set).
                rec_after = diag1.get("glRecover")
                page.wait_for_timeout(3500)
                fr.evaluate("() => { window.__forceGlLostForTest = false; }")
                diag2 = fr.evaluate("() => window.__tileDiag")
                self.assertEqual(diag2.get("glRecover"), rec_after,
                                 "watchdog kept firing after demote (churn): %r" % diag2)
                br.close()
        finally:
            h.stop()


if __name__ == "__main__":
    unittest.main()
