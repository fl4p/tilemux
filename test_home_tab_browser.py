#!/usr/bin/env python3
"""Browser regression tests for the dashboard's Home ("needs attention") tab.

Drives the REAL dashboard (serve.py) with a headless Chromium, backed by the
lightweight `dashboard_fixture.Fixture` (six dummy-listener sessions grouped into
projA / projB / soloproj tabs — no ttyd/dtach needed, since the Home tab is pure
client-side tab/bell logic). A tile "needs attention" when it rings the bell; the
real signal is a postMessage from term-client.js, which we reproduce verbatim:

    window.postMessage({type:'claude-term', sid, bell:true}, location.origin)

The dashboard's own message handler consumes that and calls markBell(sid), so we
exercise the production code path, not a test shim.

What's asserted:
  • Home tab exists, pinned first, count 0 at rest.
  • A ring glows Home + its workdir tab; opening Home gathers the ringing tiles
    from across workdir tabs.
  • New rings land on the RIGHT (ring-arrival order) and slide in (home-enter).
  • Snapshot triage: answering a tile keeps it shown until you leave+reopen Home.
  • Per-card discard button (hidden off-Home) drops a card until its next ring,
    which re-inserts it on the right.
  • Empty-state hint; ＋ New from Home doesn't leak the ::home:: sentinel cwd;
    leaving Home restores the manual tile order.

Run (test venv has Playwright; bare python does not):
    .venv-test/bin/python3 -m unittest test_home_tab_browser -v
    TILE_TEST_HEADED=1   run headed (watch it happen)
"""
import os
import unittest

from playwright.sync_api import sync_playwright

from dashboard_fixture import Fixture

HEADED = os.environ.get("TILE_TEST_HEADED") == "1"

PROJA = "/projA"          # tab-key suffixes (joined to the fixture's home dir)
PROJB = "/projB"
SOLO = "/soloproj"

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


class HomeTabTests(unittest.TestCase):
    # One dashboard for the whole class; each test gets a fresh browser CONTEXT
    # (isolated localStorage + a fresh page load → no bell/tab carryover).
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        cls.fx.start()
        cls.home = cls.fx.home

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.ctx = _browser.new_context()
        self.page = self.ctx.new_page()
        # The dashboard arms a beforeunload guard while tiles are open; auto-accept
        # so reloads/navigations in a test don't hang on the dialog.
        self.page.on("dialog", lambda d: d.accept())
        self.page.goto(self.fx.url, wait_until="domcontentloaded")
        # Tabs need 3+ sessions; the fixture has six, so the bar (and Home) appear
        # on the first poll(), which fires immediately on load.
        self.page.wait_for_selector("#tabs .tab.home", timeout=10_000)

    def tearDown(self):
        self.ctx.close()

    # -- helpers -----------------------------------------------------------
    def tab_key(self, suffix):
        return self.home + suffix

    def sid_in(self, tab_suffix):
        """A real session id whose tile lives in the given workdir tab."""
        key = self.tab_key(tab_suffix)
        sid = self.page.evaluate(
            "(k) => { for (const [id, el] of tiles) if (el.dataset.tab === k) return id; return null; }",
            key,
        )
        self.assertIsNotNone(sid, "no tile found in tab " + key)
        return sid

    def ring(self, sid):
        """Reproduce term-client's bell postMessage and let the handler run."""
        self.page.evaluate(
            "(sid) => window.postMessage({type:'claude-term', sid, bell:true}, location.origin)",
            sid,
        )
        self.page.wait_for_timeout(60)

    def click_home(self):
        self.page.click("#tabs .tab.home")
        self.page.wait_for_timeout(40)

    def click_tab(self, suffix):
        self.page.click('#tabs .tab[data-key="%s"]' % self.tab_key(suffix))
        self.page.wait_for_timeout(40)

    def home_count(self):
        return self.page.eval_on_selector("#tabs .tab.home .n", "el => el.textContent")

    def visible_ids(self):
        return self.page.evaluate(
            "() => [...tiles].filter(([id,el]) => el.style.display !== 'none').map(([id]) => id)"
        )

    # -- tests -------------------------------------------------------------
    def test_home_tab_present_and_idle(self):
        info = self.page.evaluate(
            """() => {
                const tabs = [...document.querySelectorAll('#tabs .tab')];
                const home = tabs[0];
                return {
                    firstIsHome: home.dataset.key === '::home::',
                    glow: home.classList.contains('bell'),
                    count: home.querySelector('.n').textContent,
                    countHidden: home.querySelector('.n').style.display === 'none',
                };
            }"""
        )
        self.assertTrue(info["firstIsHome"], "Home tab is not pinned first")
        self.assertFalse(info["glow"], "Home glows with nothing ringing")
        self.assertTrue(info["countHidden"], "idle Home shows a count badge")

    def test_ring_glows_home_and_origin_tab(self):
        self.ring(self.sid_in(PROJB))
        info = self.page.evaluate(
            """(bkey) => {
                const home = document.querySelector('#tabs .tab.home');
                const b = document.querySelector('#tabs .tab[data-key="'+bkey+'"]');
                return { homeGlow: home.classList.contains('bell'),
                         homeCount: home.querySelector('.n').textContent,
                         originGlow: b.classList.contains('bell') };
            }""",
            self.tab_key(PROJB),
        )
        self.assertTrue(info["homeGlow"])
        self.assertEqual(info["homeCount"], "1")
        self.assertTrue(info["originGlow"], "the ringing tile's own tab should glow too")

    def test_home_gathers_ringing_tiles_across_tabs(self):
        b = self.sid_in(PROJB)
        s = self.sid_in(SOLO)
        self.ring(b)
        self.ring(s)
        self.click_home()
        self.assertCountEqual(self.visible_ids(), [b, s],
                              "Home should show exactly the two ringing tiles")
        self.assertEqual(
            self.page.evaluate("() => document.querySelector('#tabs .tab.active').dataset.key"),
            "::home::",
        )

    def test_new_rings_land_on_the_right_and_animate(self):
        self.click_home()                      # open Home empty
        first = self.sid_in(PROJB)
        second = self.sid_in(PROJA)
        self.ring(first)                       # lands left (order 0)
        self.ring(second)                      # lands right (order 1)
        info = self.page.evaluate(
            """(ids) => {
                const [a, b] = ids;
                return {
                    orderA: parseInt(tiles.get(a).style.order, 10),
                    orderB: parseInt(tiles.get(b).style.order, 10),
                    // second ring still mid-animation when we sampled it
                    enterB: tiles.get(b).classList.contains('home-enter'),
                    bodyHomeActive: document.body.classList.contains('home-active'),
                };
            }""",
            [first, second],
        )
        self.assertLess(info["orderA"], info["orderB"],
                        "a later ring must sit to the right of an earlier one")
        self.assertTrue(info["enterB"], "a fresh ring should slide in (home-enter)")
        self.assertTrue(info["bodyHomeActive"])

    def test_answering_keeps_card_until_reopen(self):
        b = self.sid_in(PROJB)
        s = self.sid_in(SOLO)
        self.ring(b)
        self.ring(s)
        self.click_home()
        # Answering a tile (focus → selectTile clears its bell) must NOT yank it
        # out from under the cursor while Home stays open.
        self.page.evaluate("(id) => selectTile(id)", b)
        self.page.wait_for_timeout(30)
        self.assertIn(b, self.visible_ids(), "answered card vanished mid-triage")
        self.assertEqual(self.home_count(), "1", "count should drop as bells clear")
        # Leave + reopen → the answered card is gone, the still-ringing one stays.
        self.click_tab(PROJB)
        self.click_home()
        self.assertCountEqual(self.visible_ids(), [s])

    def test_discard_button(self):
        b = self.sid_in(PROJB)
        s = self.sid_in(SOLO)
        self.ring(b)
        self.ring(s)
        # discard button is hidden until Home is the active tab
        self.assertEqual(
            self.page.evaluate(
                "(id) => getComputedStyle(tiles.get(id).querySelector('.home-discard')).display", b),
            "none",
        )
        self.click_home()
        self.assertNotEqual(
            self.page.evaluate(
                "(id) => getComputedStyle(tiles.get(id).querySelector('.home-discard')).display", b),
            "none",
            "discard button should be visible on Home",
        )
        # Discard b → it drops out and its bell clears; s remains.
        self.page.evaluate("(id) => tiles.get(id).querySelector('.home-discard').click()", b)
        self.page.wait_for_timeout(40)
        info = self.page.evaluate(
            """(id) => ({ inShown: homeShown.has(id),
                          bell: tiles.get(id).classList.contains('bell') })""",
            b,
        )
        self.assertFalse(info["inShown"])
        self.assertFalse(info["bell"])
        self.assertCountEqual(self.visible_ids(), [s])
        self.assertEqual(self.home_count(), "1")
        # Re-ring the discarded tile → it comes back on the RIGHT (highest order).
        self.ring(b)
        orders = self.page.evaluate(
            "() => [...homeShown].map(id => parseInt(tiles.get(id).style.order, 10))"
        )
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(
            self.page.evaluate("(id) => parseInt(tiles.get(id).style.order,10) === Math.max(...[...homeShown].map(x=>parseInt(tiles.get(x).style.order,10)))", b),
            True,
            "a re-rung card should reappear in the rightmost slot",
        )
        self.assertEqual(self.home_count(), "2")

    def test_empty_state_hint(self):
        self.click_home()                      # nothing ringing yet
        info = self.page.evaluate(
            """() => {
                const e = document.getElementById('tab-empty');
                return { shown: !!e && e.style.display !== 'none', text: e && e.textContent };
            }"""
        )
        self.assertTrue(info["shown"], "empty Home should show a hint")
        self.assertIn("needs attention", info["text"])

    def test_new_from_home_omits_sentinel_cwd(self):
        self.click_home()
        captured = self.page.evaluate(
            """async () => {
                let url = null;
                const orig = window.fetch;
                window.fetch = (u, o) => {
                    if (String(u).includes('/api/new')) { url = String(u); window.fetch = orig;
                        return Promise.resolve(new Response('{}')); }
                    return orig(u, o);
                };
                await spawnTile('host');
                window.fetch = orig;
                return url;
            }"""
        )
        self.assertIsNotNone(captured)
        self.assertNotIn("cwd=", captured,
                         "＋ New from Home must not pass the ::home:: sentinel as a cwd")

    def test_leaving_home_restores_manual_order(self):
        self.ring(self.sid_in(PROJB))
        self.click_home()
        self.click_tab(PROJB)
        info = self.page.evaluate(
            """() => ({
                bodyHomeActive: document.body.classList.contains('home-active'),
                ordersMatchManual: [...tiles].every(([id,el]) =>
                    String(orderList.indexOf(id)) === el.style.order),
            })"""
        )
        self.assertFalse(info["bodyHomeActive"])
        self.assertTrue(info["ordersMatchManual"],
                        "manual tile order should be restored when leaving Home")


if __name__ == "__main__":
    unittest.main(verbosity=2)
