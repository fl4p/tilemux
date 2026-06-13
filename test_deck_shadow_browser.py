#!/usr/bin/env python3
"""End-to-end browser regression test for the stacked-card DECK SHADOW.

Drives the REAL dashboard (serve.py) with REAL ttyd+dtach+mock tiles under
Playwright Chromium (see tile_harness.py / reference_tile_browser_tests), parks a
tile as a card, screenshots it, and MEASURES where the shadow actually lands.

Two bugs this guards against, both found by eye and confirmed with the browser:

  1. "shadow on the wrong tile" — a condensed card tucks UNDER its right
     neighbour, so the shadow must be cast BY that covering neighbour onto the
     card below. An earlier ::after on the card itself (promoted above the
     neighbour by z-index) painted the shadow ONTO the covering tile instead.
     Guard: the covering tile (the visual right neighbour, picked by flex order)
     carries the box-shadow; the condensed card does NOT; and the covering
     tile's interior is not itself darkened.

  2. "gap between shadow and tile" — anchoring the shadow to the card's geometry
     at a fixed `right:--cond-tuck` offset missed the neighbour's real left edge
     by ~8px (flex math predicted 746, the neighbour rendered at 754). Guard:
     the darkest pixel column sits flush at the covering tile's left edge (±2px).

We measure with a per-column MEDIAN luminance over a vertical band of the row,
which rejects the sparse bright text glyphs and exposes the full-height shadow as
a dip below the ~43/#2b2b2b background.
"""
import io
import os
import time
import unittest

from PIL import Image
from playwright.sync_api import sync_playwright

from tile_harness import TileHarness

HEADED = os.environ.get("TILE_TEST_HEADED") == "1"
BG_LUM = 43            # #2b2b2b terminal background luminance
DPR = 2                # device_scale_factor — CSS px * DPR = screenshot px

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


def _med_lum_column(px, x, y0, y1):
    """Median luminance of screenshot column x over device-px rows [y0, y1)."""
    vals = sorted((px[x, y][0] + px[x, y][1] + px[x, y][2]) // 3
                  for y in range(y0, y1, DPR))
    return vals[len(vals) // 2] if vals else 255


class DeckShadowTests(unittest.TestCase):
    def setUp(self):
        self.h = TileHarness()
        self.h.add_tiles(4, "T", os.path.join(self.h.home, "alpha"))
        self.h.start()
        self.page = _browser.new_page(
            viewport={"width": 1400, "height": 900}, device_scale_factor=DPR)
        self.page.goto(self.h.url)
        self.page.wait_for_selector("#grid.row > .tile", timeout=15000)
        time.sleep(2.0)   # let tiles paint scrollback

    def tearDown(self):
        try:
            self.page.close()
        finally:
            self.h.stop()

    # -- helpers -----------------------------------------------------------
    def _tiles_by_order(self):
        """[{i, condensed, coversCard, boxShadow, left, right, order}] sorted by
        flex order — i.e. left-to-right as the user sees them."""
        rows = self.page.evaluate(
            """() => [...document.querySelectorAll('#grid.row > .tile')].map((el,i)=>{
                 const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
                 return { i, condensed: el.classList.contains('condensed'),
                          coversCard: el.classList.contains('covers-card'),
                          boxShadow: cs.boxShadow, order: parseInt(cs.order,10)||0,
                          left: Math.round(r.left), right: Math.round(r.right) }; })""")
        return sorted(rows, key=lambda r: r["order"])

    def _condense_selected(self, tile_index_in_order):
        ordered = self._tiles_by_order()
        target_dom_i = ordered[tile_index_in_order]["i"]
        tile = self.page.locator("#grid.row > .tile").nth(target_dom_i)
        tile.locator(".head").click()
        time.sleep(0.3)
        self.page.keyboard.press("Control+x")
        time.sleep(1.2)   # condense glide + neighbour tagging

    def _assert_shadow_flush(self, msg_prefix=""):
        ordered = self._tiles_by_order()
        ci = next(i for i, r in enumerate(ordered) if r["condensed"])
        cond = ordered[ci]
        self.assertLess(ci + 1, len(ordered),
                        "test needs a tile to the RIGHT of the condensed one")
        cover = ordered[ci + 1]

        # (1) the covering tile (visual right neighbour) carries the shadow;
        #     the condensed card itself does not.
        self.assertTrue(cover["coversCard"],
                        msg_prefix + "covering right neighbour is not tagged .covers-card")
        self.assertNotEqual(cover["boxShadow"], "none",
                            msg_prefix + "covering tile has no box-shadow")
        self.assertFalse(cond["coversCard"],
                         msg_prefix + "condensed card wrongly tagged .covers-card")
        self.assertEqual(cond["boxShadow"], "none",
                         msg_prefix + "shadow is on the condensed card, not the covering tile")

        # (2) pixel truth: darkest column flush at the covering tile's left edge.
        png = self.page.screenshot()
        im = Image.open(io.BytesIO(png)).convert("RGB")
        px = im.load()
        edge = cover["left"]                       # css px
        y0, y1 = DPR * 200, DPR * 720              # card content band (device px)
        lo, hi = DPR * (edge - 26), DPR * edge     # exposed-spine window up to the edge
        lums = {x: _med_lum_column(px, x, y0, y1) for x in range(lo, hi)}
        darkest_x = min(lums, key=lums.get)
        darkest_lum = lums[darkest_x]

        # flush: darkest within 2 css px of the covering edge (guards the 8px gap)
        self.assertLessEqual(
            abs(darkest_x / DPR - edge), 2,
            msg_prefix + "shadow not flush at covering edge: darkest at %dcss, edge at %dcss "
            "(gap = %d) — the fixed-offset gap bug." % (
                darkest_x / DPR, edge, darkest_x / DPR - edge))
        # the shadow is actually visible (meaningfully darker than background)
        self.assertLessEqual(
            darkest_lum, BG_LUM - 8,
            msg_prefix + "no visible shadow at the boundary (darkest lum %d vs bg %d)" % (
                darkest_lum, BG_LUM))
        # it fades LEFT: a column ~16px left of the edge is lighter than at the edge
        far = _med_lum_column(px, DPR * (edge - 16), y0, y1)
        self.assertGreater(
            far, darkest_lum + 4,
            msg_prefix + "shadow does not fade left (lum %d at edge-16 vs %d at edge)" % (
                far, darkest_lum))
        return im

    # -- tests -------------------------------------------------------------
    def test_shadow_on_covering_tile_flush_at_edge(self):
        """Park a middle card; shadow is cast by its right neighbour, flush."""
        self._condense_selected(1)
        self._assert_shadow_flush()

    def test_shadow_tracks_visual_neighbour_after_reorder(self):
        """Move a tile so flex order diverges from DOM order, THEN condense — the
        shadow must follow the VISUAL right neighbour, not the DOM-next sibling
        (the trap that a `.condensed + .tile` selector would fall into)."""
        # shuffle: select the first tile and move it right twice (Cmd/Ctrl+→)
        first = self.page.locator("#grid.row > .tile").first
        first.locator(".head").click()
        time.sleep(0.2)
        self.page.keyboard.press("Control+ArrowRight")
        time.sleep(0.3)
        self.page.keyboard.press("Control+ArrowRight")
        time.sleep(0.3)
        # sanity: DOM order no longer equals flex order
        ordered = self._tiles_by_order()
        dom_seq = [r["i"] for r in ordered]
        self.assertNotEqual(dom_seq, sorted(dom_seq),
                            "reorder did not diverge DOM order from flex order")
        self._condense_selected(1)
        self._assert_shadow_flush("after reorder: ")

    def test_last_card_has_no_covering_shadow(self):
        """A condensed card that is the rightmost tile has nothing covering it —
        no tile should be tagged .covers-card."""
        self._condense_selected(len(self._tiles_by_order()) - 1)
        ordered = self._tiles_by_order()
        self.assertTrue(ordered[-1]["condensed"], "rightmost tile is not condensed")
        self.assertFalse(any(r["coversCard"] for r in ordered),
                         "a tile is tagged .covers-card though the only condensed "
                         "card is the rightmost (nothing covers it)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
