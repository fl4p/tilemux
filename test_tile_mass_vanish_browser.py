"""Regression: a single degraded /api/sessions poll must not reap every tile.

The failure it guards against: with ~20 live tiles, every port_alive() probe in
read_sessions is a 0.2 s connect run serially; under a CPU spike (e.g. a
concurrent heavy test run) they can all time out in ONE poll, so /api/sessions
comes back empty though the sessions are alive. The frontend used to take that as
authoritative and tore down every tile at once — "all terminals showed
disconnecting and were gone". render() now requires a mass disappearance to
repeat across two consecutive polls before believing it (_isSuspectMassDrop +
_suspectDrops), so a one-cycle blip is ridden out.

Driven against the REAL dashboard via the lightweight dashboard_fixture (six fake
sessions). We intercept /api/sessions with Playwright and feed an empty body to
model the degraded poll.
"""
import json
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


class MassVanishGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        cls.fx.start()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self._empty = {"on": False}   # flip True to make /api/sessions return []
        self.ctx = _browser.new_context(viewport={"width": 1280, "height": 860})
        self.page = self.ctx.new_page()
        self.page.on("dialog", lambda d: d.accept())
        # Intercept the session poll. When armed, fulfil with an EMPTY list (the
        # degraded-poll case); otherwise let it hit the fixture as normal.
        self.page.route("**/api/sessions*", self._route_sessions)
        self.page.goto(self.fx.url, wait_until="domcontentloaded")
        self.page.wait_for_selector(".tile", timeout=10_000)
        self.page.wait_for_timeout(350)

    def tearDown(self):
        self.ctx.close()

    def _route_sessions(self, route):
        if self._empty["on"]:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"sessions": [], "home": "/tmp"}))
        else:
            route.continue_()

    def test_one_empty_poll_does_not_reap_all_tiles(self):
        base = self.page.evaluate("() => tiles.size")
        self.assertGreaterEqual(base, 3, "fixture should provide >=3 live tiles")
        skips0 = self.page.evaluate("() => window.__dashDiag.suspectSkips")

        # First degraded poll: the guard rides it out — every tile survives.
        self._empty["on"] = True
        self.page.evaluate("() => poll()")
        self.assertEqual(self.page.evaluate("() => tiles.size"), base,
                         "a single empty poll must NOT reap live tiles")
        self.assertEqual(
            self.page.evaluate("() => window.__dashDiag.suspectSkips"), skips0 + 1,
            "the suspect poll should have been skipped, not applied")

        # Second consecutive empty poll: now believed — tiles are reaped.
        self.page.evaluate("() => poll()")
        self.assertEqual(self.page.evaluate("() => tiles.size"), 0,
                         "two consecutive empty polls are accepted as a real change")

        # Recovery: a normal poll re-adds the tiles and clears the suspect state.
        self._empty["on"] = False
        self.page.evaluate("() => poll()")
        self.page.wait_for_timeout(200)
        self.assertEqual(self.page.evaluate("() => tiles.size"), base,
                         "sessions come back once the poll recovers")

    def test_a_single_legit_close_is_not_treated_as_suspect(self):
        # Dropping ONE tile from the poll (a normal close) is below the mass-drop
        # threshold, so it applies immediately — the guard only blocks a wholesale
        # vanish, never an ordinary close.
        ids = self.page.evaluate(
            "() => [...tiles.keys()]")
        base = len(ids)
        self.assertGreaterEqual(base, 3)
        drop = ids[0]
        # Serve the fixture list minus one session.
        kept = [s for s in self.fx.sessions if s["id"] != drop]
        self.page.route(
            "**/api/sessions*",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"sessions": kept, "home": "/tmp"})),
        )
        skips0 = self.page.evaluate("() => window.__dashDiag.suspectSkips")
        self.page.evaluate("() => poll()")
        self.page.wait_for_timeout(150)
        self.assertEqual(self.page.evaluate("() => tiles.size"), base - 1,
                         "a single drop should apply on the first poll")
        self.assertEqual(
            self.page.evaluate("() => window.__dashDiag.suspectSkips"), skips0,
            "a single close must not register as a suspect mass drop")


if __name__ == "__main__":
    unittest.main()
