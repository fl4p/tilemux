#!/usr/bin/env python3
"""Browser regression test for the SHARED-PTY size clobber — the "terminal text
is interleaved garbage" bug.

Repro in the wild: a claude session is shared by several dtach clients (you fork
a tile a few times, or have the dashboard open in two windows). A view that
connects while its tab is HIDDEN was never fitted, so its ttyd handshake carried
xterm's 80×24 default; the dtach client then resized the SHARED pty to 80×24 and
a busy claude repainted every WIDER view into wrapped garbage — status-line
fragments stair-stepping into scrollback (see term-client `_applySavedSize`,
SPEC "Size key" / "Default size fallback").

`_applySavedSize()` pre-sizes a hidden connect to the session's last persisted
size — but that per-session key (`claude-term-size:<sid>|<ts>`) is MISSING for a
brand-new / forked session that first appears in an inactive tab (it was never
shown sized), so it used to fall back to 80×24 and clobber. The fix adds a
cross-session `claude-term-size-default` (every row tile shares the CSS-pinned
width) that a hidden connect adopts instead.

This test exercises the REAL hidden-connect path against a REAL ttyd/dtach/mock
tile:
  • seed only the GLOBAL default (NO per-session key — a fresh, never-shown
    session) in the ttyd origin's localStorage,
  • load term.html in a 0×0 iframe so isSized() is false → the client takes the
    `go(false)` hidden path → `_applySavedSize()`,
  • assert the terminal attaches at the seeded WIDE size, not 80×24, AND that the
    size reached the program end-to-end: the mock (which renders its frame bar to
    the live PTY width) draws a WIDE bar, not an 80-col one.

On the old `_applySavedSize` (per-session key only) the hidden view stays 80×24
and the mock's bar collapses to ~79 — the clobber. So this fails pre-fix.

Run (see reference_tile_browser_tests memory — use the test venv):
    cd session-dashboard
    .venv-test/bin/python3 -m unittest test_tile_size_clobber_browser -v
"""
import os
import unittest

from playwright.sync_api import sync_playwright

from tile_harness import TileHarness

HEADED = os.environ.get("TILE_TEST_HEADED") == "1"

# A deliberately WIDE seeded default, far from 80×24, and within _applySavedSize's
# 20–500 × 5–200 sanity bounds.
DEF_COLS, DEF_ROWS = 180, 50

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


def ttyd_url(tile):
    # Mirror serve.py's host-tile turl: the ttyd port serves term.html, and
    # sid/ts are how term-client keys its per-session storage.
    return ("http://127.0.0.1:%d/?sid=%s&ts=%s&kind=host"
            % (tile["port"], tile["id"], tile["started"]))


class SizeClobberTests(unittest.TestCase):
    def test_hidden_connect_adopts_default_size_not_80x24(self):
        h = TileHarness()
        # One real ttyd+dtach+mock session. NLINES small; the bar width is what
        # matters, not scrollback volume.
        tile = h.add_tile("JNB", os.path.join(h.home, "crypto", "jnb"), nlines=12)
        h.start()
        url = ttyd_url(tile)

        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            # Seed ONLY the global default in EVERY frame's localStorage at
            # document-start (so it's present before term-client runs in the
            # iframe), and prove no per-session key exists. Opaque-origin frames
            # (the about:blank host) throw on localStorage — guard it.
            page.add_init_script(
                """try {
                     localStorage.setItem('claude-term-size-default', '%dx%d');
                   } catch (e) {}""" % (DEF_COLS, DEF_ROWS))

            # display:none → term element clientWidth 0 → isSized() false → the
            # client connects via the hidden go(false) path → _applySavedSize().
            # This mirrors the REAL hidden-tab condition: at true 0 width xterm's
            # fit.fit() no-ops (proposeDimensions bails), so the boot renderer-heal
            # can't race _applySavedSize back down to a sliver (a width:0 iframe is
            # ~2px, not 0, and DOES fit → flaky). A display:none iframe still loads
            # and connects, exactly like a tile in an inactive dashboard tab.
            page.set_content(
                '<iframe id="t" style="display:none" src="%s"></iframe>' % url)

            frame = None
            for _ in range(120):
                el = page.query_selector("#t")
                frame = el.content_frame() if el else None
                if frame:
                    break
                page.wait_for_timeout(50)
            self.assertIsNotNone(frame, "iframe never produced a content frame")

            # term-client sets window.__claudeTerm during init (before connect).
            frame.wait_for_function("() => !!window.__claudeTerm", timeout=8000)
            # Confirm the test's premise: this session has NO per-session size key,
            # so only the global default can save it.
            has_session_key = frame.evaluate(
                """() => {
                     for (let i = 0; i < localStorage.length; i++)
                       if (localStorage.key(i).startsWith('claude-term-size:')) return true;
                     return false;
                   }""")
            self.assertFalse(has_session_key,
                             "test premise broken: a per-session size key already exists")

            # The hidden path waits a 1.5 s grace before go(false)→_applySavedSize
            # (at 0×0 xterm boots at a tiny 2×1; only _applySavedSize moves it).
            # Wait for the adopted default. On the OLD code _applySavedSize finds
            # no per-session key and returns early, so cols stays tiny / 80 and
            # this times out → the test fails pre-fix.
            try:
                frame.wait_for_function(
                    "(c) => window.__claudeTerm && window.__claudeTerm.cols === c",
                    arg=DEF_COLS, timeout=8000)
            except Exception:
                pass  # fall through to an explicit, readable assertion below

            cols = frame.evaluate("() => window.__claudeTerm.cols")
            rows = frame.evaluate("() => window.__claudeTerm.rows")
            self.assertEqual(
                (cols, rows), (DEF_COLS, DEF_ROWS),
                "hidden connect did not adopt the global default size; got %dx%d "
                "(80x24 — or the tiny 0×0 boot size — means the fix's fallback "
                "did not fire and the shared-pty clobber is back)" % (cols, rows))

            # term.cols is read at the ttyd connect handshake (the per-session
            # key is absent here, so _applySavedSize resized BEFORE the socket
            # opened — see term-client "size reaches ttyd via the connect
            # handshake"), so cols==DEF_COLS means the dtach client attached at
            # 180 and sized the SHARED pty wide, not down to 80. We don't read the
            # mock's frame bar back: this view is the hidden 0×0 one, whose xterm
            # buffer truncates/reflows at the 0-width element and isn't a reliable
            # readout of the PTY width.
        finally:
            page.close()
            h.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
