#!/usr/bin/env python3
"""Browser regression tests for tile-ORDER PERSISTENCE across a page reload.

Reported bug: "the tiles don't keep their positions during page reloads."

`test_tile_order.mjs` already guards placeNewInOrder() — the helper that slots a
freshly-appeared session into the row — but it runs the helper in ISOLATION with
stubbed globals. Nothing exercised the actual end-to-end round-trip that the user
sees: drag-reorder -> saveOrder() -> localStorage['claude-sessions-order'] ->
RELOAD -> orderList re-read -> applyOrder() repaints the same CSS `order`. That
gap is exactly where a "positions not kept across reload" regression would hide,
so these tests drive the REAL dashboard (serve.py) with REAL ttyd/dtach tiles
(see tile_harness.py) under headless Chromium and assert the visible left-to-right
order is identical before and after `page.reload()`.

The "position" of a tile is its flex `order` (applyOrder sets el.style.order on
every tile; DOM nodes are never moved so the iframes don't reload). We read the
on-screen order by sorting the visible tiles by that value and mapping each back
to its session id via the iframe src (sid=host-<port>) — the same handle the
visibility tests use.

Scenarios:
  • single tab (one cwd, 3+ tiles → horizontal row): reorder, reload.
  • multi tab (two cwds): reorder inside the active tab, reload — the other tab's
    interleaved ids in the global order must not scramble the active tab.
  • reorder in a NON-default tab, reload — the saved active tab restores and its
    manual order with it.
  • a poll fires AFTER the reorder (every 3 s) before the reload — the periodic
    render must not clobber the just-saved order.
  • two windows share localStorage: a reorder in window A is adopted by window B
    via the `storage` listener, and survives a reload of BOTH (the documented
    multi-window clobber that first produced "order not persistent across
    reloads"; see serve.py storage-listener notes).
  • a brand-new session appears (like a dup landing on a later poll) after a
    reorder — the reordered tiles keep their relative order across the reload.

Run (see reference_tile_browser_tests memory — use the test venv, NOT bare python):
    cd session-dashboard
    .venv-test/bin/python3 -m unittest test_tile_order_reload_browser -v
    TILE_TEST_HEADED=1  to watch it happen.

No build-term.sh rerun is needed: the ordering logic lives in serve.py (served
directly), not in term-client.js/term.html.
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


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
# Visible left-to-right order: the non-loading, non-stashed, displayed tiles,
# sorted by their flex `order`, each mapped back to its session id via the iframe
# src (sid=host-<port>). This is what the user actually sees as "tile positions".
_JS_VISUAL_ORDER = r"""
() => {
  const tiles = [...document.querySelectorAll('#grid > .tile')]
    .filter(el => !el.classList.contains('loading')
                  && el.dataset.stashed !== '1'
                  && el.style.display !== 'none');
  const out = [];
  for (const el of tiles) {
    const f = el.querySelector('iframe');
    const src = f ? (f.getAttribute('src') || '') : '';
    const m = src.match(/[?&]sid=([^&]+)/);
    out.push({ id: m ? decodeURIComponent(m[1]) : '?', order: parseInt(el.style.order, 10) });
  }
  // NaN order (un-applied) sorts last but stays input-stable, so a missing
  // applyOrder shows up as a scrambled result rather than a silent pass.
  out.sort((a, b) => (a.order || 0) - (b.order || 0));
  return out.map(o => o.id);
}
"""


def wait_tiles(page, n, timeout=12000):
    """Wait until at least n tile elements exist in the grid (visible or not),
    then give applyOrder()/applyVisibility() a beat to run. We can't wait on
    selector visibility: tiles in a non-active tab are display:none."""
    page.wait_for_function(
        "(n) => document.querySelectorAll('#grid > .tile').length >= n",
        arg=n, timeout=timeout)
    page.wait_for_timeout(450)


def visual_order(page):
    return page.evaluate(_JS_VISUAL_ORDER)


def saved_order(page):
    return page.evaluate("() => localStorage.getItem('claude-sessions-order')")


def reorder_first_to_last(page, order):
    """Drive the SHIPPED reorder() (what a title-bar drag's drop handler calls):
    move the first visible tile to just after the last visible one."""
    page.evaluate("(o) => reorder(o[0], o[o.length - 1], true)", order)
    page.wait_for_timeout(250)


def switch_tab(page, key):
    page.click('#tabs .tab[data-key="%s"]' % key)
    page.wait_for_timeout(300)


def reload_settle(page, n):
    page.reload()
    wait_tiles(page, n)


class OrderReloadTests(unittest.TestCase):
    def test_tile_missing_from_orderlist_sorts_last_not_first(self):
        """THE root-cause guard for 'I drag a tile to last, it always comes back
        first'. applyOrder() set el.style.order = orderList.indexOf(id); for a
        live tile NOT in orderList that is -1, and CSS sorts order:-1 BEFORE every
        order>=0 tile — so the tile slammed to the FAR LEFT and stuck there. A
        tile can fall out of orderList outside the render path (the storage
        listener swaps in another window's list, a session reappears with a new
        id, a stale/older-version saved list omits it).

        We force the exact condition — drop a live tile's id from orderList, then
        call the SHIPPED applyOrder() — and assert the tile sorts to the END
        (a real, largest order), never to -1/first. Fails on the old code
        (order === '-1'); passes once applyOrder adopts unknown live tiles at the
        end and folds them back into orderList."""
        h = TileHarness()
        cwd = os.path.join(h.home, "alpha")
        for i in range(4):
            h.add_tile("T%d" % (i + 1), cwd)
        h.start()
        page = _browser.new_page(viewport={"width": 1800, "height": 1000})
        try:
            page.goto(h.url)
            wait_tiles(page, 4)
            order = visual_order(page)
            victim = order[-1]            # currently last; we'll evict it from the list

            res = page.evaluate(
                """(victim) => {
                  // evict the victim id from orderList, then run the real applyOrder()
                  for (let i = orderList.length - 1; i >= 0; i--)
                    if (orderList[i] === victim) orderList.splice(i, 1);
                  applyOrder();
                  const el = [...document.querySelectorAll('#grid > .tile')].find(e => {
                    const f = e.querySelector('iframe');
                    return f && (f.getAttribute('src') || '').includes('sid=' + victim);
                  });
                  return {
                    order: el ? el.style.order : null,
                    // did applyOrder self-heal the id back into the list?
                    healed: orderList.includes(victim),
                    listLen: orderList.length,
                  };
                }""", victim)

            self.assertIsNotNone(res["order"], "victim tile element vanished")
            self.assertNotEqual(
                res["order"], "-1",
                "tile missing from orderList got style.order=-1 → it renders FIRST "
                "(the 'comes back first' bug). It must sort to the end instead.")
            self.assertGreaterEqual(int(res["order"]), 0, "negative order sorts before the row")
            self.assertTrue(res["healed"], "applyOrder did not fold the unknown id back into orderList")

            # And visibly: after applyOrder the victim is LAST, not first.
            after = visual_order(page)
            self.assertEqual(
                after[-1], victim,
                "evicted tile did not land last after applyOrder.\n  got order: %r" % after)
            self.assertNotEqual(after[0], victim, "evicted tile slammed to FIRST (the bug)")
        finally:
            page.close()
            h.stop()

    def test_single_tab_reorder_survives_reload(self):
        """Three tiles in one cwd (a horizontal row). Reorder, reload: the
        on-screen order is byte-identical to what was set."""
        h = TileHarness()
        cwd = os.path.join(h.home, "alpha")
        for i in range(4):
            h.add_tile("T%d" % (i + 1), cwd)
        h.start()
        page = _browser.new_page(viewport={"width": 1800, "height": 1000})
        try:
            page.goto(h.url)
            wait_tiles(page, 4)
            before = visual_order(page)
            self.assertEqual(len(before), 4, "expected 4 visible tiles, got %r" % before)

            reorder_first_to_last(page, before)
            reordered = visual_order(page)
            self.assertNotEqual(reordered, before, "reorder() did not change the order")
            self.assertEqual(reordered, before[1:] + before[:1])
            # the reorder must have been persisted (the whole point)
            self.assertIsNotNone(saved_order(page), "reorder did not write localStorage")

            reload_settle(page, 4)
            after = visual_order(page)
            self.assertEqual(
                after, reordered,
                "tile order was NOT kept across reload.\n  set:  %r\n  got:  %r"
                % (reordered, after))
        finally:
            page.close()
            h.stop()

    def test_multi_tab_reorder_in_active_tab_survives_reload(self):
        """Two cwds interleaved in the GLOBAL order list. Reordering inside the
        active tab must survive reload without the other tab's ids scrambling
        the active tab's row."""
        h = TileHarness()
        a = os.path.join(h.home, "alpha")
        b = os.path.join(h.home, "beta")
        h.add_tile("A1", a); h.add_tile("B1", b); h.add_tile("A2", a)
        h.add_tile("B2", b); h.add_tile("A3", a); h.add_tile("B3", b)
        h.start()
        page = _browser.new_page(viewport={"width": 1900, "height": 1000})
        try:
            page.goto(h.url)
            wait_tiles(page, 6)
            switch_tab(page, a)
            before = visual_order(page)
            self.assertEqual(len(before), 3, "alpha tab should show its 3 tiles, got %r" % before)

            reorder_first_to_last(page, before)
            reordered = visual_order(page)
            self.assertEqual(reordered, before[1:] + before[:1])

            reload_settle(page, 6)
            switch_tab(page, a)
            after = visual_order(page)
            self.assertEqual(
                after, reordered,
                "active-tab tile order not kept across reload.\n  set: %r\n  got: %r"
                % (reordered, after))
        finally:
            page.close()
            h.stop()

    def test_reorder_in_non_default_tab_survives_reload(self):
        """Reorder while the SECOND tab is active. The active tab is persisted
        (claude-sessions-active-tab); on reload it restores and brings its manual
        order with it."""
        h = TileHarness()
        a = os.path.join(h.home, "alpha")
        b = os.path.join(h.home, "beta")
        h.add_tile("A1", a); h.add_tile("A2", a); h.add_tile("A3", a)
        h.add_tile("B1", b); h.add_tile("B2", b); h.add_tile("B3", b)
        h.start()
        page = _browser.new_page(viewport={"width": 1900, "height": 1000})
        try:
            page.goto(h.url)
            wait_tiles(page, 6)
            switch_tab(page, b)
            before = visual_order(page)
            self.assertEqual(len(before), 3, "beta tab should show its 3 tiles, got %r" % before)

            reorder_first_to_last(page, before)
            reordered = visual_order(page)
            self.assertEqual(reordered, before[1:] + before[:1])

            reload_settle(page, 6)
            # active tab should restore to beta; if it doesn't, make it explicit
            switch_tab(page, b)
            after = visual_order(page)
            self.assertEqual(
                after, reordered,
                "non-default tab order not kept across reload.\n  set: %r\n  got: %r"
                % (reordered, after))
        finally:
            page.close()
            h.stop()

    def test_order_survives_a_poll_then_reload(self):
        """The 3 s setInterval poll fires AFTER the reorder, before the reload.
        A render() that recomputes/relayouts must not clobber the saved order."""
        h = TileHarness()
        cwd = os.path.join(h.home, "alpha")
        for i in range(4):
            h.add_tile("T%d" % (i + 1), cwd)
        h.start()
        page = _browser.new_page(viewport={"width": 1800, "height": 1000})
        try:
            page.goto(h.url)
            wait_tiles(page, 4)
            before = visual_order(page)
            reorder_first_to_last(page, before)
            reordered = visual_order(page)
            self.assertEqual(reordered, before[1:] + before[:1])

            page.wait_for_timeout(3600)            # let a periodic poll() fire
            self.assertEqual(visual_order(page), reordered, "a poll clobbered the live order")

            reload_settle(page, 4)
            after = visual_order(page)
            self.assertEqual(
                after, reordered,
                "order not kept across poll+reload.\n  set: %r\n  got: %r"
                % (reordered, after))
        finally:
            page.close()
            h.stop()

    def test_new_session_after_reorder_keeps_reordered_relative_order(self):
        """A brand-new session appears (like a dup landing on a later poll) after
        a reorder. The reordered tiles must keep their relative order across the
        reload — the newcomer slots in without scrambling them."""
        h = TileHarness()
        cwd = os.path.join(h.home, "alpha")
        for i in range(4):
            h.add_tile("T%d" % (i + 1), cwd)
        h.start()
        page = _browser.new_page(viewport={"width": 1800, "height": 1000})
        try:
            page.goto(h.url)
            wait_tiles(page, 4)
            before = visual_order(page)
            reorder_first_to_last(page, before)
            reordered = visual_order(page)            # the manual order of the 4

            h.add_tile("T5", cwd)                     # new registry entry → appears on a poll
            page.wait_for_timeout(3800)
            wait_tiles(page, 5)

            reload_settle(page, 5)
            after = visual_order(page)
            rel = [x for x in after if x in reordered]
            self.assertEqual(
                rel, reordered,
                "original tiles' relative order not kept after a newcomer + reload.\n"
                "  set: %r\n  got: %r" % (reordered, rel))
        finally:
            page.close()
            h.stop()

    def test_two_windows_reorder_adopted_and_survives_reload(self):
        """Two dashboard windows share localStorage. A reorder in window A is
        adopted by window B via the `storage` listener; after BOTH reload, both
        show A's order. This is the documented multi-window clobber that first
        surfaced as 'tile order is not persistent across reloads'."""
        h = TileHarness()
        cwd = os.path.join(h.home, "alpha")
        for i in range(4):
            h.add_tile("T%d" % (i + 1), cwd)
        h.start()
        ctx = _browser.new_context(viewport={"width": 1700, "height": 950})
        try:
            A = ctx.new_page(); A.goto(h.url); wait_tiles(A, 4)
            B = ctx.new_page(); B.goto(h.url); wait_tiles(B, 4)

            before = visual_order(A)
            reorder_first_to_last(A, before)
            a_re = visual_order(A)
            self.assertEqual(a_re, before[1:] + before[:1])

            # B should ADOPT A's write via the storage event (it didn't write it).
            B.wait_for_timeout(800)
            self.assertEqual(
                visual_order(B), a_re,
                "window B did not adopt window A's reorder via the storage listener")

            # Let B poll/render a few times — its stale copy must not clobber.
            B.wait_for_timeout(3600)

            reload_settle(B, 4)
            self.assertEqual(visual_order(B), a_re, "B lost the order across reload")
            reload_settle(A, 4)
            self.assertEqual(visual_order(A), a_re, "A lost the order across reload")
        finally:
            ctx.close()
            h.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
