#!/usr/bin/env python3
"""Browser regression test for the CONDENSED-TILE FREEZE renderer optimization.

A parked ("condensed") card is shown as a static snapshot and must hold NO WebGL
context. The bug this guards against: a deck of condensed cards scrolling into the
viewport used to fire one WebGL dispose+recreate (gateSwaps++) PER card — the
dashboard's IntersectionObserver sends {cmd:'input', enabled:true} for each
clipped spine, and setRendererVisible(true) promoted every one back onto the GPU.
N cards entering view at once = N synchronous GL recreations + burst-heals = the
"lag when many condensed tiles enter the screen" the user reported.

The fix (term-client.js):
  • renderer-wanted = inputEnabled && !condensed — a parked card never re-acquires
    WebGL, not even when scrolled into view.
  • on park: swap GL->canvas, snapshot the frame as a static overlay, drop GL.
  • on un-park (or a bell, which the dashboard auto-expands): drop the overlay and
    restore WebGL if the tile is input-visible.

Asserted via term-client's __tileDiag: `renderer` ('webgl'|'canvas') and
`condFrozen`. WebGL is available headless via ANGLE/SwiftShader, so the GL path is
genuinely exercised.

Run:
    .venv-test/bin/python3 test_tile_condense_freeze_browser.py
"""
import os
import unittest

from playwright.sync_api import sync_playwright

from tile_harness import TileHarness

HEADED = os.environ.get("TILE_TEST_HEADED") == "1"

_play = None
_browser = None


def setUpModule():
    global _play, _browser
    _play = sync_playwright().start()
    _browser = _play.chromium.launch(headless=not HEADED)


def tearDownModule():
    if _browser:
        _browser.close()
    if _play:
        _play.stop()


def frame_for_port(page, port):
    for el in page.query_selector_all("iframe"):
        src = el.get_attribute("src") or ""
        if ("sid=host-%d" % port) in src or (":%d/" % port) in src:
            cf = el.content_frame()
            if cf:
                return cf
    raise AssertionError("no iframe for port %d" % port)


def wait_ready(frame, timeout=12000):
    frame.wait_for_function("() => window.__claudeTerm && window.__tileDiag", timeout=timeout)


def wait_for_text(frame, needle, timeout=12000):
    frame.wait_for_function(
        "(s) => (window.__claudeTerm && window.__claudeTerm.buffer.active "
        "&& Array.from({length: window.__claudeTerm.buffer.active.length}, (_, i) => "
        "window.__claudeTerm.buffer.active.getLine(i)).map(l => l ? l.translateToString() : '')"
        ".join('\\n').includes(s))",
        arg=needle, timeout=timeout)


def diag(frame):
    return frame.evaluate("() => window.__tileDiag || null")


def post(page, port, msg):
    """Post a {claude-host,...} message to the tile (same origin path the
    dashboard uses — passes term-client's origin check)."""
    page.evaluate(
        """([port, msg]) => {
          for (const f of document.querySelectorAll('iframe')) {
            if ((f.getAttribute('src')||'').includes('sid=host-'+port)) {
              f.contentWindow.postMessage(Object.assign({type:'claude-host'}, msg), '*');
            }
          }
        }""", [port, msg])


def wait_renderer(frame, want, timeout=6000):
    frame.wait_for_function("(w) => (window.__tileDiag||{}).renderer === w",
                            arg=want, timeout=timeout)


class CondenseFreezeTests(unittest.TestCase):
    def test_park_drops_gl_and_freezes_then_restores(self):
        h = TileHarness()
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=20)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0020")
            # boots on WebGL
            wait_renderer(f, "webgl")

            # --- park it: GL must drop to canvas, and a static overlay appears ---
            post(page, a["port"], {"cmd": "condensed", "on": True})
            wait_renderer(f, "canvas")
            f.wait_for_function("() => (window.__tileDiag||{}).condFrozen === true",
                                timeout=4000)

            # --- the lag fix: a parked card scrolling into view (enabled:true)
            #     must NOT recreate the WebGL context (no gateSwaps, stays canvas).
            before = diag(f)["gateSwaps"]
            post(page, a["port"], {"cmd": "input", "enabled": False})   # scrolled off
            page.wait_for_timeout(150)
            post(page, a["port"], {"cmd": "input", "enabled": True})    # scrolled back in
            page.wait_for_timeout(400)
            d = diag(f)
            self.assertEqual(d["renderer"], "canvas",
                             "parked card re-acquired a renderer other than canvas on "
                             "scroll-into-view: %r" % d["renderer"])
            self.assertEqual(d["gateSwaps"], before,
                             "parked card recreated its WebGL context on scroll-into-view "
                             "(gateSwaps %d -> %d) — the parked-deck lag is back"
                             % (before, d["gateSwaps"]))
            self.assertTrue(d["condFrozen"], "parked card lost its frozen overlay")

            # --- un-park: overlay clears and WebGL is restored (tile is input-visible) ---
            post(page, a["port"], {"cmd": "condensed", "on": False})
            wait_renderer(f, "webgl")
            self.assertFalse(diag(f)["condFrozen"],
                             "frozen overlay survived un-parking")
        finally:
            page.close()
            h.stop()

    def test_hover_peek_drops_overlay_without_touching_renderer(self):
        """Hovering a parked card drops the frozen overlay to reveal the live
        canvas underneath, and re-freezes on leave — WITHOUT changing the renderer
        (no WebGL re-acquire) and WITHOUT un-condensing."""
        h = TileHarness()
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=20)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0020")
            wait_renderer(f, "webgl")
            post(page, a["port"], {"cmd": "condensed", "on": True})
            wait_renderer(f, "canvas")
            f.wait_for_function("() => (window.__tileDiag||{}).condFrozen === true",
                                timeout=4000)
            # hover in → overlay gone, live canvas shows; still parked, still canvas
            post(page, a["port"], {"cmd": "peek", "on": True})
            f.wait_for_function("() => (window.__tileDiag||{}).condFrozen === false",
                                timeout=3000)
            d = diag(f)
            self.assertEqual(d["renderer"], "canvas",
                             "peek changed the renderer — it must never re-acquire GL")
            self.assertTrue(d["condensed"], "peek un-condensed the card (should only drop the overlay)")
            # hover out → re-frozen on the current frame
            post(page, a["port"], {"cmd": "peek", "on": False})
            f.wait_for_function("() => (window.__tileDiag||{}).condFrozen === true",
                                timeout=3000)
        finally:
            page.close()
            h.stop()

    def test_inview_card_refreshes_every_2s_and_pauses_offscreen(self):
        """A parked card IN VIEW re-snapshots its preview every 2s; scrolled
        off-screen (inputEnabled false) the refresh pauses so it costs nothing."""
        h = TileHarness()
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=20)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0020")
            wait_renderer(f, "webgl")
            post(page, a["port"], {"cmd": "condensed", "on": True})
            wait_renderer(f, "canvas")
            f.wait_for_function("() => (window.__tileDiag||{}).condFrozen === true",
                                timeout=4000)
            before = diag(f)["condRefreshes"]
            page.wait_for_timeout(4500)          # > 2 ticks while in view
            mid = diag(f)["condRefreshes"]
            self.assertGreaterEqual(
                mid - before, 1,
                "in-view parked card never refreshed its snapshot (%d -> %d)" % (before, mid))
            # scroll off / hidden tab → inputEnabled false → refresh must pause
            post(page, a["port"], {"cmd": "input", "enabled": False})
            page.wait_for_timeout(300)
            off = diag(f)["condRefreshes"]
            page.wait_for_timeout(4500)
            after = diag(f)["condRefreshes"]
            self.assertEqual(
                after, off,
                "off-screen parked card kept refreshing (%d -> %d) — should pause" % (off, after))
        finally:
            page.close()
            h.stop()

    def test_bell_escapes_a_frozen_card(self):
        """The key safety property: freezing must NOT swallow the bell. The bell is
        detected in the write-queue parse (scanBel), not the renderer, so a frozen
        card must still post its bell up to the dashboard. We park the card (frozen,
        on canvas), record bell messages on the dashboard window, then drive a real
        BEL through the LIVE data path (the mock rings on '!') and assert it lands."""
        h = TileHarness()
        # The mock rings a bare BEL ~7s after boot — landing LIVE, well after the
        # client connects and clears its 1.5s connect-mute, so it's a genuine ring.
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=20, bell_after=7.0)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            # Record any bell the dashboard receives (the custom client posts
            # {type:'claude-term', bell:true} to window.parent).
            page.evaluate(
                "() => { window.__bellSeen = false; window.addEventListener('message', (e) => {"
                "  if (e.data && e.data.type === 'claude-term' && e.data.bell) window.__bellSeen = true; }); }")
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0020")
            wait_renderer(f, "webgl")
            # Park it (client-side, so the freeze reliably succeeds at full size)
            # BEFORE the ring fires — the tile is frozen on canvas when it rings.
            post(page, a["port"], {"cmd": "condensed", "on": True})
            wait_renderer(f, "canvas")
            f.wait_for_function("() => (window.__tileDiag||{}).condFrozen === true", timeout=4000)
            # The bell must escape the frozen tile and reach the dashboard.
            page.wait_for_function("() => window.__bellSeen === true", timeout=12000)
            # The tile is STILL frozen on canvas — the bell rang with no renderer
            # involvement at all (scanBel runs in the websocket onmessage parse).
            self.assertEqual(diag(f)["renderer"], "canvas",
                             "tile left canvas during the bell — bell should be renderer-independent")
            self.assertTrue(diag(f)["condFrozen"], "tile un-froze on its own during the bell")
        finally:
            page.close()
            h.stop()

    def test_parked_card_stays_glued_to_bottom(self):
        """A parked card snaps to the bottom and FOLLOWS new output, so the preview
        always shows the latest lines even if the terminal was scrolled up first."""
        h = TileHarness()
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=80,
                       stream_count=200, stream_every=0.1)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0080")
            wait_renderer(f, "webgl")
            # scroll up off the bottom (output won't auto-follow while scrolled up)
            f.evaluate("() => window.__claudeTerm.scrollToTop()")
            m = f.evaluate("() => ({b: __claudeTerm.buffer.active.baseY, "
                           "v: __claudeTerm.buffer.active.viewportY})")
            self.assertLess(m["v"], m["b"], "precondition: terminal must be scrolled up off the bottom")
            # park it → must snap to the bottom
            post(page, a["port"], {"cmd": "condensed", "on": True})
            wait_renderer(f, "canvas")
            f.wait_for_function(
                "() => __claudeTerm.buffer.active.viewportY === __claudeTerm.buffer.active.baseY",
                timeout=3000)
            base1 = f.evaluate("() => __claudeTerm.buffer.active.baseY")
            # output keeps streaming → baseY grows, the viewport must follow it down
            page.wait_for_timeout(1600)
            m2 = f.evaluate("() => ({b: __claudeTerm.buffer.active.baseY, "
                            "v: __claudeTerm.buffer.active.viewportY})")
            self.assertGreater(m2["b"], base1, "no new output streamed while parked (test setup)")
            self.assertEqual(m2["v"], m2["b"], "parked card did not follow new output to the bottom")
        finally:
            page.close()
            h.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
