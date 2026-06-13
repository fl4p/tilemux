#!/usr/bin/env python3
"""Browser regression tests for two tile-layout behaviours:

  1. CONDENSE SHRINKS THE BOX in BOTH layouts. The condense-shrink (flex-basis +
     the SIGWINCH-safe iframe-width pin) only exists in ROW mode. A ≤2-tile tab is
     normally GRID mode, where condensing used to restyle only the head and leave
     the box full-width ("condense does nothing"). The fix: a parked card forces
     row mode (`anyCond` in applyVisibility), so the box actually shrinks to the
     --cond-peek spine. This guards both the grid-mode (forces row) and row-mode
     (already row) cases, and that expanding the last card drops back to grid.

  2. A NEW TILE LANDS NEXT TO THE MOST-VISIBLE TILE, ON-SCREEN — not appended off
     the right edge of the scroll row (where its open animation is invisible).
     spawnTile anchors placement on mostVisibleTileId() when nothing is selected;
     the reveal then scrolls it fully into view by LAYOUT geometry (transform-
     immune, so the entrance unfold doesn't fool it).

Driven against the REAL dashboard via the lightweight dashboard_fixture (six
dummy-listener sessions: projA=2 tiles → grid, projB=3 tiles → row). No ttyd
needed — condense width + placement are pure client-side layout.

Run (test venv has Playwright):
    .venv-test/bin/python3 -m unittest test_tile_layout_browser -v
    TILE_TEST_HEADED=1   run headed (watch it happen)
"""
import os
import unittest

from playwright.sync_api import sync_playwright

from dashboard_fixture import Fixture

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


class TileLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        cls.fx.start()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.ctx = _browser.new_context(viewport={"width": 1280, "height": 860})
        self.page = self.ctx.new_page()
        self.page.on("dialog", lambda d: d.accept())
        self.page.goto(self.fx.url, wait_until="domcontentloaded")
        self.page.wait_for_selector(".tile", timeout=10_000)
        self.page.wait_for_timeout(350)

    def tearDown(self):
        self.ctx.close()

    # -- helpers -----------------------------------------------------------
    def goto_tab(self, label):
        self.page.evaluate(
            """(label) => { const b=[...document.querySelectorAll('#tabs .tab')]
                 .find(t => (t.textContent||'').includes(label)); if (b) b.click(); }""",
            label,
        )
        self.page.wait_for_timeout(300)

    def first_visible(self):
        """{id,width,row} for the first shown, non-loading tile in the grid."""
        return self.page.evaluate(
            """() => {
              const g = document.getElementById('grid');
              const t = [...g.querySelectorAll('.tile:not(.loading)')].find(e => e.style.display !== 'none');
              const id = [...tiles].find(([i,e]) => e === t)?.[0];
              return { id, width: t.getBoundingClientRect().width, row: g.classList.contains('row'),
                       cond: t.classList.contains('condensed') };
            }"""
        )

    def click_cond_first(self):
        self.page.evaluate(
            """() => {
              const g = document.getElementById('grid');
              const t = [...g.querySelectorAll('.tile:not(.loading)')].find(e => e.style.display !== 'none');
              t.querySelector('button.cond').click();
            }"""
        )
        self.page.wait_for_timeout(450)  # past the FLIP / flex-basis glide

    # -- condense shrink ---------------------------------------------------
    def test_condense_shrinks_in_grid_tab_by_forcing_row(self):
        # projA = 2 tiles → grid mode. Condensing must flip to row and shrink the
        # box to the spine (the regression: it only changed the head before).
        self.goto_tab("projA")
        before = self.first_visible()
        self.assertFalse(before["row"], "projA (2 tiles) should start in grid mode")
        self.click_cond_first()
        after = self.first_visible()
        self.assertTrue(after["row"], "condensing must force row mode so the box can shrink")
        self.assertTrue(after["cond"], "tile should be marked condensed")
        self.assertLess(after["width"], before["width"] - 100,
                        "condensed box must visibly shrink, not just restyle the head "
                        "(was %.0f -> %.0f)" % (before["width"], after["width"]))

    def test_condense_shrinks_in_row_tab(self):
        # projB = 3 tiles → already row mode. Condense shrinks to the spine.
        self.goto_tab("projB")
        before = self.first_visible()
        self.assertTrue(before["row"], "projB (3 tiles) should be row mode")
        self.click_cond_first()
        after = self.first_visible()
        self.assertLess(after["width"], before["width"] - 100,
                        "condensed box must shrink in row mode (was %.0f -> %.0f)"
                        % (before["width"], after["width"]))

    def test_expand_restores_width_and_drops_back_to_grid(self):
        # Grid tab: condense (→ row + shrink), then expand → width back, grid back.
        self.goto_tab("projA")
        before = self.first_visible()
        self.click_cond_first()
        mid = self.first_visible()
        self.assertLess(mid["width"], before["width"] - 100)
        self.click_cond_first()  # expand
        after = self.first_visible()
        self.assertFalse(after["row"], "expanding the last card drops back to grid mode")
        self.assertGreater(after["width"], mid["width"] + 100,
                           "expanded box must return to full width")

    # -- new-tile placement ------------------------------------------------
    def test_new_tile_lands_next_to_most_visible_on_screen(self):
        # projB row, scrolled to the left, nothing selected. A new session must
        # land adjacent to the most-centered tile AND be scrolled fully into view,
        # not appended off the right edge.
        self.goto_tab("projB")
        tabkey = self.page.evaluate("() => activeTab")
        self.page.evaluate(
            """() => { if (typeof selectedId!=='undefined') selectedId=null;
                 if (typeof releasePin==='function') releasePin();
                 document.getElementById('grid').scrollLeft = 0; }"""
        )
        self.page.wait_for_timeout(80)
        anchor = self.page.evaluate("(t)=>mostVisibleTileId(t)", tabkey)
        self.assertIsNotNone(anchor, "should find a most-visible tile to anchor on")
        # Register the pendingDup exactly as spawnTile would for an unselected spawn.
        self.page.evaluate(
            """(a)=>{ pendingDups.push({srcId:a, cwd: activeTab||'', until: Date.now()+90000}); }""",
            anchor,
        )
        self.fx.add_session("placed-next-to-me", os.path.join(self.fx.home, "projB"), "host")
        self.page.evaluate("() => poll()")
        self.page.wait_for_timeout(500)
        info = self.page.evaluate(
            """(anchor) => {
              const cand = [...tiles].find(([id,el]) => {
                const n = el.querySelector('.name'); return n && /placed-next-to-me/.test(n.textContent); });
              if (!cand) return { found:false };
              const [id, el] = cand;
              const r = el.getBoundingClientRect(), vw = window.innerWidth;
              return { found:true,
                       adjacent: orderList.indexOf(id) === orderList.indexOf(anchor) + 1,
                       fullyOnScreen: r.left >= -1 && r.right <= vw + 1,
                       left: Math.round(r.left), right: Math.round(r.right), vw };
            }""",
            anchor,
        )
        self.assertTrue(info["found"], "the new session's tile should exist")
        self.assertTrue(info["adjacent"],
                        "new tile must land immediately after its anchor in the order")
        self.assertTrue(info["fullyOnScreen"],
                        "new tile must be scrolled fully into view (left=%(left)s right=%(right)s vw=%(vw)s)" % info)

    # -- reduced-motion setting -------------------------------------------
    def test_reduced_motion_setting_snaps_instead_of_animating(self):
        # Toggling Reduced motion ON (gear menu) must add body.reduce-motion AND
        # make a freshly-opened tile SNAP — no flip transform / inline transition /
        # dataset.flip token (the entrance is gated on the `reducedMotion` flag).
        self.goto_tab("projB")
        self.page.click("#setBtn")            # open the gear menu
        self.page.click("#motionBtn")          # turn Reduced motion ON
        self.page.wait_for_timeout(60)
        self.assertTrue(self.page.evaluate("() => document.body.classList.contains('reduce-motion')"),
                        "Reduced motion must add body.reduce-motion")
        tabkey = self.page.evaluate("() => activeTab")
        self.page.evaluate(
            """() => { if (typeof selectedId!=='undefined') selectedId=null;
                 if (typeof releasePin==='function') releasePin(); }"""
        )
        anchor = self.page.evaluate("(t)=>mostVisibleTileId(t)", tabkey)
        self.page.evaluate(
            "(a)=>{ pendingDups.push({srcId:a, cwd: activeTab||'', until: Date.now()+90000}); }", anchor)
        self.fx.add_session("rm-snap-tile", os.path.join(self.fx.home, "projB"), "host")
        self.page.evaluate("() => poll()")
        self.page.wait_for_timeout(45)        # an animation would still be mid-flight here
        info = self.page.evaluate(
            """() => {
              const c = [...tiles].find(([id,el]) => { const n=el.querySelector('.name');
                return n && /rm-snap-tile/.test(n.textContent); });
              if (!c) return { found:false };
              const el = c[1], cs = getComputedStyle(el);
              return { found:true, transform: cs.transform, inlineTransition: el.style.transition,
                       flip: el.dataset.flip || null };
            }"""
        )
        self.assertTrue(info["found"], "the new tile should exist")
        self.assertIn(info["transform"], ("none", "matrix(1, 0, 0, 1, 0, 0)"),
                      "reduced motion: new tile must not carry a flip transform (got %s)" % info["transform"])
        self.assertFalse(info["flip"], "reduced motion: no flip animation token should be set")

    def test_reduced_motion_persists_in_localstorage(self):
        self.page.click("#setBtn")
        self.page.click("#motionBtn")
        self.page.wait_for_timeout(40)
        self.assertEqual(
            self.page.evaluate("() => localStorage.getItem('claude-sessions-reduced-motion')"), "on")
        self.page.click("#motionBtn")          # toggle back off
        self.page.wait_for_timeout(40)
        self.assertEqual(
            self.page.evaluate("() => localStorage.getItem('claude-sessions-reduced-motion')"), "off")
        self.assertFalse(self.page.evaluate("() => document.body.classList.contains('reduce-motion')"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
