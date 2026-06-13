#!/usr/bin/env python3
"""Browser regression tests for bright (light) mode chrome colours.

Light mode only re-binds the CSS custom properties on `html.light`; any chrome
surface that hardcodes a dark hex (instead of a var) stays dark — and several
did, reading as dark slabs (some with dark-on-dark text) floating in the
otherwise-light UI. These tests pin the surfaces that were fixed:

  • empty / loading tile body + the tile's own fill + the iframe element fill
    (all defaulted to #2b2b2b — visible while a tile boots or has no live
    terminal);
  • the settings-menu font/size/line-height <select> (.font-picker, #0d1117 —
    was dark text on a near-black box);
  • the "Manage launchers…" modal cards + inputs (.lrow #0e131b / input #070a0f);
  • the embedded NOTE tile, a same-origin iframe with its OWN theme that must
    follow the dashboard (pre-paint from the shared localStorage key, then live
    via the theme postMessage broadcast).

Each assertion checks BOTH directions: light in light mode, still-dark in dark
mode — so the test also guards against a future change regressing dark mode.

Driven against the real serve.py via dashboard_fixture.Fixture (dummy-listener
sessions; no ttyd needed — the note tile is a same-origin /note/<id> page and
the rest is pure CSS/postMessage).

Run (test venv has Playwright; bare python does not):
    .venv-test/bin/python3 -m unittest test_light_mode_browser -v
    TILE_TEST_HEADED=1   run headed (watch it happen)
"""
import os
import re
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


def _lum(rgb):
    """Mean channel value of an `rgb(...)`/`rgba(...)` string. Our light fills
    (#eef1f5≈240, #fff=255) and dark fills (#2b2b2b=43, #0d1117≈14, #070a0f≈9)
    sit far either side of the 90/140 thresholds the asserts use, so a coarse
    mean is plenty to tell them apart. A fully-transparent fill (alpha 0) is
    rejected outright — these surfaces must be opaque."""
    nums = [int(n) for n in re.findall(r"\d+", rgb)]
    assert len(nums) >= 3, "unparseable colour: %r" % rgb
    if len(nums) >= 4 and rgb.startswith("rgba") and nums[3] == 0:
        return -1.0   # transparent — fails both light and dark asserts
    return (nums[0] + nums[1] + nums[2]) / 3.0


class LightModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        cls.fx.start()

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()

    def setUp(self):
        self.ctx = _browser.new_context()
        self.page = self.ctx.new_page()
        self.page.on("dialog", lambda d: d.accept())
        self.page.goto(self.fx.url, wait_until="domcontentloaded")
        self.page.wait_for_selector(".tile", timeout=10_000)

    def tearDown(self):
        self.ctx.close()

    # -- helpers -----------------------------------------------------------
    def set_theme(self, light):
        """Put the dashboard in the wanted theme via the shared localStorage key +
        reload — this exercises the real pre-paint + applyTheme load path and,
        unlike clicking the in-menu toggle, leaves no menu open to overlay the
        elements a test reads next. (The note test uses the live button path.)"""
        self.page.evaluate(
            "(l) => localStorage.setItem('claude-sessions-theme', l ? 'light' : 'dark')",
            light,
        )
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_selector(".tile", timeout=10_000)
        self.assertEqual(
            self.page.evaluate(
                "() => document.documentElement.classList.contains('light')"),
            light,
        )

    def click_theme_toggle(self):
        """Flip the theme through the real header control: the toggle lives inside
        the settings (gear) menu, which closes on an OUTSIDE click (not Escape)."""
        self.page.click("#setBtn")
        self.page.wait_for_selector(".settings-menu.open #themeBtn", timeout=5_000)
        self.page.click("#themeBtn")
        self.page.wait_for_timeout(80)
        self.page.click("header.bar h1")        # outside click → menu closes
        self.page.wait_for_timeout(40)

    def bg(self, selector):
        return self.page.evaluate(
            "(s) => { const e = document.querySelector(s);"
            " return e ? getComputedStyle(e).backgroundColor : null; }",
            selector,
        )

    def assert_light(self, rgb, what):
        self.assertIsNotNone(rgb, "%s: element not found" % what)
        self.assertGreater(_lum(rgb), 140, "%s should be light, got %s" % (what, rgb))

    def assert_dark(self, rgb, what):
        self.assertIsNotNone(rgb, "%s: element not found" % what)
        self.assertLess(_lum(rgb), 90, "%s should stay dark, got %s" % (what, rgb))

    def open_settings(self):
        self.page.click("#setBtn")
        self.page.wait_for_selector(".settings-menu.open .font-picker", timeout=5_000)

    def open_launchers(self):
        self.page.click("#newBtn")
        self.page.wait_for_selector(".new-menu.open", timeout=5_000)
        self.page.click("button[data-act='manage-launchers']")
        self.page.wait_for_selector(".modal .lrow", timeout=5_000)

    def make_note(self):
        self.page.click("#newBtn")
        self.page.wait_for_selector(".new-menu.open", timeout=5_000)
        self.page.click(".new-menu button[data-kind='note']")
        self.page.wait_for_selector(".tile[data-kind='note'] iframe", timeout=10_000)
        # wait for the same-origin note doc to actually load
        self.page.wait_for_function(
            "() => { const f = document.querySelector(\"[data-kind='note'] iframe\");"
            " return f && f.contentDocument && f.contentDocument.getElementById('note'); }",
            timeout=10_000,
        )

    def note_doc(self):
        return self.page.evaluate(
            """() => {
                const f = document.querySelector("[data-kind='note'] iframe");
                const d = f.contentDocument;
                const cs = d.defaultView.getComputedStyle(d.body);
                return { light: d.documentElement.classList.contains('light'),
                         bg: cs.backgroundColor, fg: cs.color };
            }"""
        )

    # -- tests -------------------------------------------------------------
    def test_theme_button_toggles_html_class(self):
        self.set_theme(True)
        self.assertTrue(self.page.evaluate(
            "() => document.documentElement.classList.contains('light')"))
        self.set_theme(False)
        self.assertFalse(self.page.evaluate(
            "() => document.documentElement.classList.contains('light')"))

    def test_empty_tile_surfaces_follow_theme(self):
        # The tile box, the iframe element fill, and the loading-placeholder body
        # all default to #2b2b2b — what shows while a tile boots or sits empty.
        probe = """() => {
            const t = document.createElement('div'); t.className = 'tile loading';
            const ifr = document.createElement('iframe');
            const lb = document.createElement('div'); lb.className = 'loading-body';
            t.appendChild(ifr); t.appendChild(lb);
            document.getElementById('grid').appendChild(t);
            const g = el => getComputedStyle(el).backgroundColor;
            const out = { tile: g(t), iframe: g(ifr), loadingBody: g(lb) };
            t.remove(); return out;
        }"""
        self.set_theme(True)
        light = self.page.evaluate(probe)
        self.assert_light(light["tile"], "tile box (light)")
        self.assert_light(light["iframe"], "tile iframe fill (light)")
        self.assert_light(light["loadingBody"], "loading-body (light)")

        self.set_theme(False)
        dark = self.page.evaluate(probe)
        self.assert_dark(dark["tile"], "tile box (dark)")
        self.assert_dark(dark["iframe"], "tile iframe fill (dark)")
        self.assert_dark(dark["loadingBody"], "loading-body (dark)")

    def test_settings_font_picker_follows_theme(self):
        self.set_theme(True)
        self.open_settings()
        self.assert_light(self.bg(".settings-menu .font-picker"),
                          "settings font-picker (light)")

        self.set_theme(False)
        self.open_settings()
        self.assert_dark(self.bg(".settings-menu .font-picker"),
                         "settings font-picker (dark)")

    def test_launchers_modal_surfaces_follow_theme(self):
        self.set_theme(True)
        self.open_launchers()
        self.assert_light(self.bg(".modal .lrow"), "launcher card (light)")
        self.assert_light(self.bg(".modal .lrow input"), "launcher input (light)")
        self.assert_light(self.bg(".modal .btn"), "modal button (light)")

        self.set_theme(False)
        self.open_launchers()
        self.assert_dark(self.bg(".modal .lrow"), "launcher card (dark)")
        self.assert_dark(self.bg(".modal .lrow input"), "launcher input (dark)")

    def test_note_tile_themes_with_dashboard_and_toggles_live(self):
        # Born in light mode (pre-paint from the shared localStorage theme key).
        self.set_theme(True)
        self.make_note()
        doc = self.note_doc()
        self.assertTrue(doc["light"], "note iframe didn't pick up light at mount")
        self.assert_light(doc["bg"], "note body (light)")

        # Live toggle to dark via the real button (NO reload): the dashboard
        # broadcasts the theme to every iframe; the note's postMessage listener
        # must flip it back to dark.
        self.click_theme_toggle()
        self.page.wait_for_timeout(120)
        doc = self.note_doc()
        self.assertFalse(doc["light"], "note iframe didn't follow the toggle to dark")
        self.assert_dark(doc["bg"], "note body (dark)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
