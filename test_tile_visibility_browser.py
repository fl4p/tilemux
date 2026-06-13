#!/usr/bin/env python3
"""Browser regression tests for dashboard terminal-tile visibility behaviour.

Drives the REAL dashboard + REAL ttyd/dtach tiles (see tile_harness.py) with a
headless Chromium and reproduces the bugs reported against the session-dashboard:

  • GRAY / blank tile after a dashboard tab is hidden and re-shown (the tile kept
    its WebGL context within the 12 s GL-hold window, so setRendererVisible(true)
    used to early-return without repainting — the user's "switch tab, wait 10 s,
    switch back → just grey, needs a manual reload"). Detected by screenshotting
    the tile and measuring the fraction of non-background (text) pixels.

  • SCROLLBACK LOSS / DUPLICATION across hide/show + resize cycles. Detected from
    xterm's buffer: every unique "<LABEL> SB NNNN" marker must appear exactly
    once, and the live "<LABEL> FRAME winch=" line exactly once.

  • claude asking a question (A/B/C prompt) WHILE the tab is hidden, then the tab
    is restored — the prompt must be visible and the tile must render.

Scenarios: 1 tile, 3 tiles in a tab, 20 tiles at once, and a long fuzzy walk of
random tab switches. WebGL is available headless via ANGLE/SwiftShader, so the
WebGL-hold code path (where the bug lived) is genuinely exercised.

Run:
    .venv-test/bin/python3 -m pytest test_tile_visibility_browser.py -v
    .venv-test/bin/python3 test_tile_visibility_browser.py            # unittest

Heavy tests are gated so a quick run stays fast:
    TILE_TEST_HEAVY=1   enable the 20-tile + fuzzy tests (on by default in __main__)
    TILE_TEST_HEADED=1  run headed (watch it happen)
"""
import io
import os
import random
import unittest

from PIL import Image
from playwright.sync_api import sync_playwright

from tile_harness import TileHarness

HEADED = os.environ.get("TILE_TEST_HEADED") == "1"
HEAVY = os.environ.get("TILE_TEST_HEAVY", "1") != "0"
BG = (43, 43, 43)            # #2b2b2b terminal background
HIDE_SECONDS = 10            # stay inside the 12 s GL-hold window (the bug path)
RENDER_FLOOR = 0.012         # min text-pixel fraction for "this tile rendered text"

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
def text_ratio(png_bytes):
    """Fraction of pixels that differ from the terminal background — i.e. how
    much rendered text/box is on screen. A correctly rendered tile is well above
    RENDER_FLOOR; a gray/blank tile collapses toward ~0."""
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = im.size
    px = im.load()
    total = w * h
    if total == 0:
        return 0.0
    step = max(1, total // 50000)   # subsample large shots for speed
    seen = 0
    nonbg = 0
    for idx in range(0, total, step):
        x, y = idx % w, idx // w
        r, g, b = px[x, y]
        seen += 1
        if abs(r - BG[0]) + abs(g - BG[1]) + abs(b - BG[2]) > 45:
            nonbg += 1
    return nonbg / max(1, seen)


def frame_for_port(page, port):
    """The content frame of the tile iframe serving <port> (its src carries
    sid=host-<port>). Works even for a hidden-tab tile (still loaded)."""
    for el in page.query_selector_all("iframe"):
        src = el.get_attribute("src") or ""
        if ("sid=host-%d" % port) in src or (":%d/" % port) in src:
            cf = el.content_frame()
            if cf:
                return cf
    raise AssertionError("no iframe for port %d" % port)


def wait_ready(frame, timeout=12000):
    frame.wait_for_function(
        "() => window.__claudeTerm && window.__claudeTerm.buffer && window.__claudeTerm.buffer.active",
        timeout=timeout)


def buffer_text(frame):
    return frame.evaluate("""() => {
      const t = window.__claudeTerm, b = t.buffer.active; let s = '';
      for (let i = 0; i < b.length; i++) { const ln = b.getLine(i); if (ln) s += ln.translateToString(true) + '\\n'; }
      return s;
    }""")


def wait_for_text(frame, needle, timeout=12000):
    frame.wait_for_function(
        """(needle) => {
          const t = window.__claudeTerm; if (!t) return false;
          const b = t.buffer.active;
          for (let i = 0; i < b.length; i++) { const ln = b.getLine(i); if (ln && ln.translateToString(true).includes(needle)) return true; }
          return false;
        }""", arg=needle, timeout=timeout)


def count_lines_containing(text, needle):
    return sum(1 for ln in text.splitlines() if needle in ln)


def switch_tab(page, key):
    page.click('#tabs .tab[data-key="%s"]' % key)
    page.wait_for_timeout(250)


def screenshot_tile(frame):
    # Scrolls the element into view if needed, then captures just the terminal.
    return frame.locator("#term").screenshot()


def iframe_el(page, port):
    for el in page.query_selector_all("iframe"):
        if ("sid=host-%d" % port) in (el.get_attribute("src") or ""):
            return el
    return None


def tile_clip(page, port):
    """The on-screen rectangle of this tile's iframe, clipped to the viewport, or
    None if it isn't meaningfully visible. Used to screenshot a tile WITHOUT
    Playwright's per-element scroll-into-view (which waits for 'stable' and times
    out on the animating horizontal-row layout)."""
    el = iframe_el(page, port)
    if not el:
        return None
    box = el.bounding_box()
    if not box:
        return None
    vw = page.viewport_size["width"]
    vh = page.viewport_size["height"]
    x0, y0 = max(box["x"], 0), max(box["y"], 0)
    x1 = min(box["x"] + box["width"], vw)
    y1 = min(box["y"] + box["height"], vh)
    if x1 - x0 < 60 or y1 - y0 < 60:
        return None
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def render_ratio_in_view(page, port):
    clip = tile_clip(page, port)
    if not clip:
        return None
    return text_ratio(page.screenshot(clip=clip))


def grid_scroll(page, dx):
    page.evaluate("(dx) => { const g = document.getElementById('grid'); if (g) g.scrollLeft += dx; }", dx)


def grid_scroll_home(page):
    page.evaluate("() => { const g = document.getElementById('grid'); if (g) g.scrollLeft = 0; }")


# --------------------------------------------------------------------------- #
# assertions
# --------------------------------------------------------------------------- #
class TileAsserts(unittest.TestCase):
    def assert_scrollback_intact(self, frame, label, nlines):
        """Every unique SB marker present exactly once, and exactly one live
        FRAME line — i.e. no loss and no duplication."""
        txt = buffer_text(frame)
        # presence: first and last markers must be there (nothing dropped)
        self.assertIn("%s SB %04d" % (label, 1), txt, "%s: first scrollback line missing" % label)
        self.assertIn("%s SB %04d" % (label, nlines), txt, "%s: last scrollback line missing" % label)
        # no duplication: each marker exactly once
        dupes = []
        for i in range(1, nlines + 1):
            c = count_lines_containing(txt, "%s SB %04d " % (label, i))
            if c != 1:
                dupes.append((i, c))
        self.assertEqual(dupes, [], "%s: duplicated/missing scrollback markers %r" % (label, dupes))
        fcount = count_lines_containing(txt, "%s FRAME winch=" % label)
        self.assertEqual(fcount, 1, "%s: expected exactly one live FRAME line, got %d" % (label, fcount))

    def assert_no_dup(self, frame, label):
        """No marker appears more than once and at most one live FRAME line. This
        is the duplication guard (the "double lines" bug). Unlike
        assert_scrollback_intact it does NOT require presence — fair for a tile
        whose scrollback legitimately can't be recovered (a fresh hidden-at-boot
        TUI tile that never persisted a snapshot)."""
        txt = buffer_text(frame)
        import re as _re
        seen = {}
        for m in _re.finditer(r"%s SB (\d{4})" % _re.escape(label), txt):
            seen[m.group(1)] = seen.get(m.group(1), 0) + 1
        dupes = sorted((k, v) for k, v in seen.items() if v > 1)
        self.assertEqual(dupes, [], "%s: duplicated scrollback markers %r" % (label, dupes))
        fcount = txt.count("%s FRAME winch=" % label)
        self.assertLessEqual(fcount, 1, "%s: %d live FRAME lines (duplicated frame)" % (label, fcount))

    def sweep_capture(self, page, tiles):
        """Scroll the horizontal row from the left, returning {port: text_ratio}
        for every tile as it comes into view. Raises if a tile never becomes
        visible. (A tile's content can legitimately be sparse — a hidden-at-boot
        TUI tile that lost its scrollback shows only its few-line frame — so a
        fixed pixel floor can't tell 'sparse' from 'gray'; callers compare
        against a per-tile baseline instead.)"""
        grid_scroll_home(page)
        page.wait_for_timeout(400)
        ratios = {}
        for _ in range(len(tiles) + 5):
            for t in tiles:
                if t["port"] in ratios:
                    continue
                r = render_ratio_in_view(page, t["port"])
                if r is not None:
                    ratios[t["port"]] = r
            if len(ratios) == len(tiles):
                break
            grid_scroll(page, 700)
            page.wait_for_timeout(450)
        missing = [t["label"] for t in tiles if t["port"] not in ratios]
        self.assertEqual(missing, [], "tiles never scrolled into view to check: %r" % missing)
        return ratios

    def assert_not_gray(self, tiles, before, after):
        """A tile is gray if its rendered text pixels COLLAPSED relative to its own
        earlier baseline (a gray renderer paints ~the background; correctly-shown
        content — even sparse — keeps roughly its own ratio). Relative so a sparse
        frame-only tile isn't mistaken for gray."""
        gray = []
        for t in tiles:
            b = before.get(t["port"], 0.0)
            a = after.get(t["port"], 0.0)
            if a < max(0.0008, 0.4 * b):
                gray.append((t["label"], round(b, 4), round(a, 4)))
        self.assertEqual(gray, [], "gray/blank after cycle (label, before, after): %r" % gray)

    def assert_rendered(self, frame, label, floor=RENDER_FLOOR, baseline=None):
        """The tile actually painted text (not a gray rectangle)."""
        ratio = text_ratio(screenshot_tile(frame))
        if baseline is not None:
            self.assertGreaterEqual(
                ratio, 0.4 * baseline,
                "%s: looks gray after re-show — text pixels collapsed %.4f → %.4f" % (label, baseline, ratio))
        self.assertGreater(
            ratio, floor,
            "%s: tile is gray/blank after re-show (text-pixel ratio %.4f ≤ %.4f)" % (label, ratio, floor))
        return ratio


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
class SingleTileTests(TileAsserts):
    def test_prompt_while_hidden_then_restore(self):
        """claude is told to ask A/B/C in 5 s; switch away, wait 10 s (prompt
        fires while hidden), switch back → tile must render and show the prompt,
        with scrollback intact."""
        h = TileHarness()
        alpha = os.path.join(h.home, "alpha")
        beta = os.path.join(h.home, "beta")
        a = h.add_tile("A1", alpha, nlines=40, prompt_after=5)
        h.add_tiles(2, "B", beta)            # filler → ≥3 sessions → real tab bar
        h.start()
        page = _browser.new_page(viewport={"width": 1500, "height": 950})
        try:
            page.goto(h.url)
            page.wait_for_selector('#tabs .tab[data-key="%s"]' % alpha, timeout=8000)
            fa = frame_for_port(page, a["port"])
            wait_ready(fa)
            wait_for_text(fa, "A1 SB 0040")
            baseline = self.assert_rendered(fa, "A1")   # healthy while active

            switch_tab(page, beta)                       # hide alpha BEFORE t=5
            page.wait_for_timeout(HIDE_SECONDS * 1000)   # prompt fires at t≈5 while hidden
            switch_tab(page, alpha)                       # restore
            page.wait_for_timeout(800)

            wait_for_text(fa, "A1 QUESTION: choose")     # the question made it through
            self.assert_scrollback_intact(fa, "A1", 40)
            self.assert_rendered(fa, "A1", baseline=baseline)
        finally:
            page.close()
            h.stop()


class ThreeTileTests(TileAsserts):
    def test_three_tiles_hide_show(self):
        """3 tiles in one tab, one of them asking A/B/C while hidden. Switch away,
        wait, switch back → all three render, none gray, scrollback intact."""
        h = TileHarness()
        alpha = os.path.join(h.home, "alpha")
        beta = os.path.join(h.home, "beta")
        tiles = [
            h.add_tile("A1", alpha, nlines=40, prompt_after=5),
            h.add_tile("A2", alpha, nlines=40),
            h.add_tile("A3", alpha, nlines=40),
        ]
        h.add_tile("B1", beta)
        h.start()
        page = _browser.new_page(viewport={"width": 1700, "height": 1000})
        try:
            page.goto(h.url)
            page.wait_for_selector('#tabs .tab[data-key="%s"]' % alpha, timeout=8000)
            switch_tab(page, alpha)
            base = {}
            for t in tiles:
                f = frame_for_port(page, t["port"])
                wait_ready(f)
                wait_for_text(f, "%s SB 0040" % t["label"])
                base[t["label"]] = self.assert_rendered(f, t["label"])

            switch_tab(page, beta)
            page.wait_for_timeout(HIDE_SECONDS * 1000)
            switch_tab(page, alpha)
            page.wait_for_timeout(900)

            for t in tiles:
                f = frame_for_port(page, t["port"])
                self.assert_scrollback_intact(f, t["label"], 40)
                self.assert_rendered(f, t["label"], baseline=base[t["label"]])
            # the hidden-fired prompt survived on A1
            wait_for_text(frame_for_port(page, tiles[0]["port"]), "A1 QUESTION: choose")
        finally:
            page.close()
            h.stop()


class ManyTileTests(TileAsserts):
    @unittest.skipUnless(HEAVY, "set TILE_TEST_HEAVY=1 for the 20-tile stress test")
    def test_twenty_tiles_hide_show(self):
        """20 tiles at once in a single tab. Switch away, wait, switch back →
        every tile's scrollback is intact (no dup/loss). Then bring each into
        view and assert it rendered (not gray)."""
        h = TileHarness()
        alpha = os.path.join(h.home, "alpha")
        beta = os.path.join(h.home, "beta")
        tiles = h.add_tiles(20, "T", alpha, nlines=30)
        h.add_tile("B1", beta)
        h.start()
        page = _browser.new_page(viewport={"width": 1900, "height": 1100})
        try:
            page.goto(h.url)
            page.wait_for_selector('#tabs .tab[data-key="%s"]' % alpha, timeout=10000)
            switch_tab(page, alpha)
            for t in tiles:
                f = frame_for_port(page, t["port"])
                wait_ready(f, timeout=30000)
                wait_for_text(f, "%s SB 0030" % t["label"], timeout=30000)
            switch_tab(page, beta)
            page.wait_for_timeout(HIDE_SECONDS * 1000)
            switch_tab(page, alpha)
            page.wait_for_timeout(1200)

            # All 20 were active at boot → never reloaded → scrollback stays in the
            # xterm buffer across the hide/show; assert it's intact AND not dup'd.
            for t in tiles:
                f = frame_for_port(page, t["port"])
                self.assert_scrollback_intact(f, t["label"], 30)
            # And none came back gray: these tiles carry 30 dense scrollback lines,
            # so a correct render is well clear of the background; a gray/blank
            # renderer collapses to ~0. (Absolute floor — fair here because the
            # content is dense; the fuzzy test uses a relative baseline for its
            # legitimately-sparse frame-only tiles.)
            after = self.sweep_capture(page, tiles)
            gray = [(t["label"], round(after[t["port"]], 4)) for t in tiles if after[t["port"]] < 0.004]
            self.assertEqual(gray, [], "gray/blank tiles after the cycle: %r" % gray)
        finally:
            page.close()
            h.stop()


class FuzzyTests(TileAsserts):
    @unittest.skipUnless(HEAVY, "set TILE_TEST_HEAVY=1 for the long fuzzy test")
    def test_fuzzy_tab_walk(self):
        """A long, complicated walk: several tabs with several tiles each, some
        asking questions on staggered timers, and a random sequence of tab
        switches with random dwell times — mocking a user flipping around while
        claude works. At the end EVERY tile must be intact and render cleanly."""
        rng = random.Random(20260613)
        h = TileHarness()
        groups = {
            "g_alpha": os.path.join(h.home, "alpha"),
            "g_beta": os.path.join(h.home, "beta"),
            "g_gamma": os.path.join(h.home, "gamma"),
            "g_delta": os.path.join(h.home, "delta"),
        }
        all_tiles = []
        for gi, cwd in enumerate(groups.values()):
            n = [3, 2, 4, 2][gi]
            for j in range(n):
                pa = 4 + (j * 3) if j == 0 else 0     # first tile in each group asks a question
                # nlines=15 fits the 80×24 a hidden-at-boot tile connects at, so its
                # scrollback survives the reveal-triggered reload (dtach only replays
                # the visible screen; anything scrolled off a 24-row PTY is gone).
                all_tiles.append(h.add_tile("G%dT%d" % (gi, j), cwd, nlines=15, prompt_after=pa))
        h.start()
        page = _browser.new_page(viewport={"width": 1800, "height": 1050})
        try:
            page.goto(h.url)
            page.wait_for_selector("#tabs .tab", timeout=10000)
            keys = list(groups.values())
            # Make sure each tile connected & printed its scrollback at least once,
            # and capture a per-group render baseline (each tile shown at least once).
            baseline = {}
            for k in keys:
                switch_tab(page, k)
                page.wait_for_timeout(500)
                group = [x for x in all_tiles if x["cwd"] == k]
                for t in group:
                    wait_ready(frame_for_port(page, t["port"]))
                baseline.update(self.sweep_capture(page, group))

            # Random walk: ~14 switches, dwell 2–9 s (all < 12 s GL-hold).
            cur = None
            for _ in range(14):
                k = rng.choice([x for x in keys if x != cur])
                cur = k
                switch_tab(page, k)
                page.wait_for_timeout(rng.randint(2000, 9000))

            # Settle, then verify every tile across every tab:
            #   • no tile went gray vs its own baseline (assert_not_gray), and
            #   • no tile duplicated content (assert_no_dup).
            # Scrollback PRESENCE is asserted only for the first tab's tiles: they
            # were active at boot so they never reload and keep their buffer. Tiles
            # in tabs hidden at boot reload on first reveal, and a fresh TUI tile
            # with no prior localStorage snapshot legitimately can't recover its
            # scrollback on reattach (dtach -r winch redraws via the program, which
            # never reprints scrollback) — so we don't demand presence there, only
            # that nothing got duplicated.
            first_tab = keys[0]
            for k in keys:
                switch_tab(page, k)
                page.wait_for_timeout(500)
                group = [x for x in all_tiles if x["cwd"] == k]
                after = self.sweep_capture(page, group)
                self.assert_not_gray(group, baseline, after)
                for t in group:
                    f = frame_for_port(page, t["port"])
                    self.assert_no_dup(f, t["label"])
                    if k == first_tab:
                        self.assert_scrollback_intact(f, t["label"], 15)
        finally:
            page.close()
            h.stop()


class ResizeBurstTests(TileAsserts):
    def test_resize_burst_no_dup(self):
        """A screen-unlock / display-wake fires a burst of resize events at
        transient widths. The debounced fit must collapse that to one settled
        resize so the Ink frame is not re-rendered at two widths and duplicated."""
        h = TileHarness()
        alpha = os.path.join(h.home, "alpha")
        beta = os.path.join(h.home, "beta")
        a = h.add_tile("A1", alpha, nlines=40)
        h.add_tiles(2, "B", beta)
        h.start()
        page = _browser.new_page(viewport={"width": 1500, "height": 950})
        try:
            page.goto(h.url)
            page.wait_for_selector('#tabs .tab[data-key="%s"]' % alpha, timeout=8000)
            fa = frame_for_port(page, a["port"])
            wait_ready(fa)
            wait_for_text(fa, "A1 SB 0040")

            # Burst of differing viewport widths in quick succession, then settle.
            for w in (1490, 1300, 1480, 1280, 1500, 1290, 1495):
                page.set_viewport_size({"width": w, "height": 950})
                page.wait_for_timeout(40)
            page.set_viewport_size({"width": 1500, "height": 950})
            page.wait_for_timeout(1500)

            self.assert_scrollback_intact(fa, "A1", 40)
            self.assert_rendered(fa, "A1")
        finally:
            page.close()
            h.stop()


def post_gate(page, port, enabled):
    """Drive the dashboard's visibility gate to the tile directly (the same
    {cmd:'input'} message applyVisibility's IntersectionObserver sends). Posting
    from the dashboard page means the origin check in term-client passes."""
    page.evaluate(
        """([port, enabled]) => {
          for (const f of document.querySelectorAll('iframe')) {
            if ((f.getAttribute('src')||'').includes('sid=host-'+port)) {
              f.contentWindow.postMessage({type:'claude-host', cmd:'input', enabled}, '*');
            }
          }
        }""", [port, enabled])


def diag(frame):
    return frame.evaluate("() => window.__tileDiag || null")


class DiagTests(TileAsserts):
    """Deterministic tests for the exact fixes, using term-client's diagnostic
    counters. These don't depend on the renderer's environment-specific repaint
    timing (headless Chromium happens to fire the iframe resize that ALSO heals,
    which would mask the gray bug) — they assert the fix's mechanism directly."""

    def test_visibility_gate_repaints_on_reshow(self):
        """Hide→show via the dashboard's visibility gate (NOT a display change, so
        the iframe-resize heal can't fire) must still repaint the tile. This is
        the gray-tile fix: setRendererVisible(true) heals when re-shown within the
        12 s GL-hold window instead of early-returning."""
        h = TileHarness()
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=20)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0020")
            f.wait_for_function("() => window.__tileDiag", timeout=5000)
            before = diag(f)["gateHeals"]
            post_gate(page, a["port"], False)            # tile hidden
            page.wait_for_timeout(1000)                  # stays inside the 12 s GL-hold
            post_gate(page, a["port"], True)             # re-shown
            page.wait_for_timeout(400)
            after = diag(f)["gateHeals"]
            self.assertGreaterEqual(
                after - before, 1,
                "re-show via the visibility gate did not repaint (gray-tile bug): "
                "gateHeals %d → %d" % (before, after))
        finally:
            page.close()
            h.stop()

    def test_reshow_recreates_renderer_to_clear_ghost(self):
        """Hide→show via the visibility gate must RECREATE the WebGL renderer
        (gateSwaps++), not merely _burstHeal. A render-only heal can't reset a GL
        context that went stale while hidden (Chrome reclaims hidden tiles'
        contexts under the per-page cap), leaving a composited GHOST — content
        duplicated at an offset on screen while the buffer stays clean. Only a
        full dispose+recreate clears it (the same path the manual ↻ / Cmd+Shift+E
        uses, which the user confirmed fixes it every time). We can't reproduce
        the GPU-specific ghost under headless SwiftShader, so we assert the FIX
        MECHANISM (the recreate fires) plus that the recreate itself leaves the
        tile correctly rendered and NOT duplicated. Reverting the gate heal to
        _burstHeal drops gateSwaps to 0 and fails the first assert."""
        h = TileHarness()
        # Stream output after boot so the buffer keeps growing while the tile is
        # hidden — the real-world reveal-ghost trigger (claude printing on another
        # tab). ~40 lines over the hidden window.
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=30,
                       stream_count=60, stream_every=0.08)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0030")
            f.wait_for_function("() => window.__tileDiag", timeout=5000)
            before = diag(f)["gateSwaps"]
            post_gate(page, a["port"], False)            # hide (stay in 12 s GL-hold)
            page.wait_for_timeout(2500)                  # buffer streams/scrolls while hidden
            post_gate(page, a["port"], True)             # re-show → must recreate renderer
            page.wait_for_timeout(900)                   # swap + warmup heal settle
            after = diag(f)["gateSwaps"]
            self.assertGreaterEqual(
                after - before, 1,
                "re-show did not recreate the renderer (gateSwaps %d → %d): the "
                "render-only heal can't clear a stale-context ghost." % (before, after))
            # the recreate must leave the tile rendered and not duplicated
            self.assert_no_dup(f, "A1")
            self.assert_rendered(f, "A1")
        finally:
            page.close()
            h.stop()

    def test_boot_hidden_heals_on_first_show(self):
        """On page load, a tile may start hidden (display:none, e.g. a background
        tab on page launch). useWebgl() initializes while hidden with stale (0×0)
        dimensions. When the dashboard first shows it via the visibility gate, it
        must trigger a heal. _hiddenSinceShown=true on boot ensures this happens
        even on the first show. Regression test for "gray content on reload" bug.

        Simulates: boot hidden, then immediately show (stay in 12 s GL window).
        With the bug (_hiddenSinceShown=false on boot): the show early-returns
        without healing, tile stays gray. With the fix: heal fires."""
        h = TileHarness()
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=20)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0020")
            f.wait_for_function("() => window.__tileDiag", timeout=5000)
            # Simulate the boot-hidden scenario: immediately hide and show via the
            # visibility gate, staying inside the 12 s GL-hold window (where the bug
            # would have the tile stay gray). The first show must trigger healing.
            before = diag(f)["gateHeals"]
            post_gate(page, a["port"], False)            # hide (boot hidden → stays hidden)
            page.wait_for_timeout(100)                   # minimal wait, stay in GL window
            post_gate(page, a["port"], True)             # first show from boot-hidden state
            page.wait_for_timeout(400)
            after = diag(f)["gateHeals"]
            self.assertGreaterEqual(
                after - before, 1,
                "boot-hidden tile did not heal on first show (gray-on-reload bug): "
                "gateHeals %d → %d; _hiddenSinceShown must default to true" % (before, after))
        finally:
            page.close()
            h.stop()

    def test_reattach_reblit_runs_both_passes(self):
        """The PRIMARY gray-on-reload fix. After a (re)attach, term-client schedules
        two re-blit passes (700 ms + 1400 ms) to repaint the buffer onto a renderer
        that may have been cold/contended when the restored content landed. The old
        code latched on the FIRST pass (_reblitDone=true), so the SECOND never ran —
        and on a heavy reload where the async-gzip restore lands AFTER 700 ms, that
        sole heal repainted an EMPTY buffer and the tile stayed gray until a manual
        ↻ / keystroke. Both passes must heal now.

        Counter-based (not pixel-based) on purpose: headless Chromium incidentally
        fires the iframe-resize heal that would mask the gray pixels, so only the
        re-blit COUNTER reliably distinguishes fixed (≥2) from buggy (exactly 1)."""
        h = TileHarness()
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=20)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0020")
            f.wait_for_function("() => window.__tileDiag", timeout=5000)
            # Both timer passes fire 700 ms & 1400 ms after socket.onopen; wait past
            # the second, then assert BOTH ran. reblitPasses isolates the timer
            # passes from the restore-completion heal (which also bumps `reblits`),
            # so it's exactly 2 when fixed and exactly 1 under the old latch bug.
            f.wait_for_function("() => window.__tileDiag.reblitPasses >= 2", timeout=6000)
            self.assertEqual(
                diag(f)["reblitPasses"], 2,
                "post-reattach re-blit ran %d timer pass(es), expected 2 — the second "
                "pass was suppressed (the gray-on-reload latch bug)" % diag(f)["reblitPasses"])
        finally:
            page.close()
            h.stop()

    def test_heal_burst_coalesces_its_fit(self):
        """_burstHeal runs _healAfterRendererSwap THREE times (0/80/220 ms). Each
        used to call fit.fit() directly → on a still-settling layout (a flicker /
        renderer-swap relayout) that's 3 SIGWINCHes at 3 widths, so claude (Ink)
        re-renders 3 frames woven together — the char-by-char "double content"
        garble seen while idle. The fix routes every heal-fit through the shared
        debounce, so a whole heal burst yields ONE fit, never three (and never the
        zero of the old direct-fit.fit path, which is how this test tells them
        apart: the debounced `fits` counter only moves when the heal funnels
        through _scheduleFit)."""
        h = TileHarness()
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=20)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0020")
            f.wait_for_function("() => window.__tileDiag", timeout=5000)
            page.wait_for_timeout(2200)              # past the 700/1400 ms reblit passes
            before = diag(f)["fits"]
            post_gate(page, a["port"], False)
            page.wait_for_timeout(100)
            post_gate(page, a["port"], True)         # → _burstHeal → 3× heal → 3× _scheduleFit
            page.wait_for_timeout(900)               # cover the 220 ms burst + 180 ms debounce
            delta = diag(f)["fits"] - before
            self.assertGreaterEqual(
                delta, 1,
                "heal did not funnel its fit through the debounce (direct fit.fit → 0 "
                "debounced fits): delta %d" % delta)
            self.assertLessEqual(
                delta, 2,
                "heal burst did not coalesce — %d debounced fits (3-SIGWINCH interleave "
                "risk)" % delta)
        finally:
            page.close()
            h.stop()

    def test_reblit_repaints_across_reload(self):
        """The reported user action: RELOAD the dashboard. The fresh page's tile
        reattaches, and the post-reattach re-blit must AGAIN run both passes — so a
        late-landing buffer is repainted on a reload, not just on first boot.

        Counter-based, not pixel-based: this mock relies on dtach replay rather
        than the localStorage-restore branch (its output starts with plain
        scrollback, never a clear/home, so it never matches _looksLikeTuiRepaint),
        so its post-reload buffer is legitimately sparse — a pixel check couldn't
        tell that from gray. The re-blit COUNTER is what proves the fix holds."""
        h = TileHarness()
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=20)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0020")
            page.wait_for_timeout(400)

            page.reload()
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            f.wait_for_function("() => window.__tileDiag", timeout=5000)
            # Both re-blit timer passes fire on the reattach of the reloaded page.
            f.wait_for_function("() => window.__tileDiag.reblitPasses >= 2", timeout=6000)
            self.assertEqual(
                diag(f)["reblitPasses"], 2,
                "after reload only %d re-blit timer pass ran, expected 2 — the second "
                "was suppressed (gray-on-reload latch bug)" % diag(f)["reblitPasses"])
        finally:
            page.close()
            h.stop()

    def test_resize_burst_coalesced_to_one_fit(self):
        """A burst of resize events at transient widths must collapse to a single
        settled fit (the _scheduleFit debounce), so the PTY isn't SIGWINCHed at
        several widths in a row (the double-content hazard). We end the burst at
        the starting width: debounced → one fit at the original size → ~0 PTY
        resizes; un-debounced → a fit per event → several."""
        h = TileHarness()
        a = h.add_tile("A1", os.path.join(h.home, "alpha"), nlines=20)
        h.start()
        page = _browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            page.goto(h.url)
            f = frame_for_port(page, a["port"])
            wait_ready(f)
            wait_for_text(f, "A1 SB 0020")
            page.wait_for_timeout(800)                   # let the initial fit settle
            # Fire a tight burst of resize events INSIDE the frame (the browser
            # coalesces rapid viewport changes, so drive the listener directly).
            # Debounced → one fit for the whole burst; un-debounced → one per event.
            before = diag(f)["fits"]
            f.evaluate("() => { for (let i = 0; i < 8; i++) window.dispatchEvent(new Event('resize')); }")
            page.wait_for_timeout(500)                   # let the debounce settle
            after = diag(f)["fits"]
            # The 8-event burst must COALESCE to ~one fit (un-debounced → 8). Allow a
            # little slack (≤3): an unrelated background heal — e.g. the gate-driven
            # renderer recreate — can land its own _scheduleFit in this window. The
            # point is 8 events ≠ 8 fits, not an exact count.
            self.assertLessEqual(
                after - before, 3,
                "resize burst not coalesced — %d fits for 8 rapid resize events "
                "(SIGWINCH storm → double-content hazard)" % (after - before))
            self.assert_scrollback_intact(f, "A1", 20)
        finally:
            page.close()
            h.stop()


class RealClaudeTests(TileAsserts):
    """End-to-end against the REAL `claude` binary (not the mock). Opt-in — it
    needs a working claude install + auth + network and claude's output is
    non-deterministic, so it's NOT part of the default regression suite. It is a
    genuine "let claude ask you something, switch tabs, come back" smoke test.

        TILE_TEST_REAL_CLAUDE=1 .venv-test/bin/python3 \\
            -m unittest test_tile_visibility_browser.RealClaudeTests -v
    """

    @unittest.skipUnless(os.environ.get("TILE_TEST_REAL_CLAUDE") == "1",
                         "set TILE_TEST_REAL_CLAUDE=1 (needs real claude + auth)")
    def test_real_claude_survives_tab_cycle(self):
        import shutil as _sh
        claude = _sh.which("claude")
        self.assertIsNotNone(claude, "claude binary not on PATH")
        h = TileHarness()
        alpha = os.path.join(h.home, "alpha")
        beta = os.path.join(h.home, "beta")
        a = h.add_tile("CLAUDE", alpha,
                       program=[claude, "--dangerously-skip-permissions"],
                       run_cwd=None)
        h.add_tiles(2, "B", beta)        # filler → ≥3 sessions → real tab bar
        h.start()
        page = _browser.new_page(viewport={"width": 1500, "height": 950})
        marker = "ZZMARKER42ZZ"
        try:
            page.goto(h.url)
            page.wait_for_selector('#tabs .tab[data-key="%s"]' % alpha, timeout=8000)
            switch_tab(page, alpha)
            f = frame_for_port(page, a["port"])
            wait_ready(f, timeout=20000)
            # Wait for claude's TUI to actually paint something substantial.
            f.wait_for_function(
                """() => {
                  const t = window.__claudeTerm; if (!t) return false;
                  const b = t.buffer.active; let nonblank = 0;
                  for (let i = 0; i < b.length; i++) { const ln = b.getLine(i); if (ln && ln.translateToString(true).trim()) nonblank++; }
                  return nonblank >= 3;
                }""", timeout=45000)
            page.wait_for_timeout(1500)

            # claude Code shows a folder-trust gate on a fresh dir (--dangerously-
            # skip-permissions skips TOOL perms, not this). Accept it ("Enter to
            # confirm" → "Yes, I trust this folder") so we reach the real prompt.
            if "trust this folder" in buffer_text(f) or "safety check" in buffer_text(f):
                f.locator(".xterm-helper-textarea").focus()
                page.keyboard.press("Enter")
                f.wait_for_function(
                    "() => !window.__claudeTerm.buffer.active.getLine ? false : "
                    "!Array.from({length: window.__claudeTerm.buffer.active.length}, (_,i) => "
                    "window.__claudeTerm.buffer.active.getLine(i)).some(l => l && l.translateToString(true).includes('trust this folder'))",
                    timeout=15000)
                page.wait_for_timeout(2000)

            # Ask claude to ask US something AND emit a unique marker we can anchor on.
            f.locator(".xterm-helper-textarea").focus()
            page.keyboard.type(
                "Reply with exactly this line and nothing else: %s — then ask me to choose A, B, or C." % marker,
                delay=8)
            page.wait_for_timeout(300)
            page.keyboard.press("Enter")

            # Wait for claude's reply (the marker) to land in the buffer.
            wait_for_text(f, marker, timeout=90000)
            page.wait_for_timeout(1500)
            baseline = self.assert_rendered(f, "CLAUDE")
            before_txt = buffer_text(f)
            self.assertIn(marker, before_txt)

            # Now the real scenario: leave to another tab, wait, come back.
            switch_tab(page, beta)
            page.wait_for_timeout(HIDE_SECONDS * 1000)
            switch_tab(page, alpha)
            page.wait_for_timeout(1000)

            # The tile must NOT be gray and must NOT have lost/duplicated its content.
            self.assert_rendered(f, "CLAUDE", baseline=baseline)
            after_txt = buffer_text(f)
            self.assertIn(marker, after_txt, "claude tile lost its content after the tab cycle")
            self.assertLessEqual(
                after_txt.count(marker), 2,
                "claude tile duplicated content after the tab cycle (marker x%d)" % after_txt.count(marker))
        finally:
            page.close()
            h.stop()


class HomeWidthTests(TileAsserts):
    """WIDTH-CONSISTENCY INVARIANT: a session's PTY width (term.cols) must not
    change with its tab's LAYOUT. If it does, a tile that moves between layouts —
    e.g. a tile from a 3+ workdir tab (row mode) ringing onto Home — gets
    SIGWINCH'd to a new width and claude reflows its frame, the user-reported
    "tile popped up on Home too wide / doubled; opening it in a new tab fixes it".

    NOTE / SCOPE: this guards the row-vs-non-row WORKDIR tabs, which today already
    measure identical (≈898px / 124 cols at 2000px wide) — so it's a passing guard
    against a FUTURE regression that makes layouts diverge. It does NOT yet
    reproduce the Home-SPECIFIC width difference the user hit (the row/non-row
    widths match, so Home must change the width via its own layout/animation path,
    e.g. home-enter or the covers-card stacking). That Home-specific reproduction
    needs the Home layout internals and is owned by the Home-tab work; see the
    `sht`-channel handoff. Keep this invariant green; add a Home-path case there.
    """

    def test_row_and_nonrow_tabs_give_same_pty_width(self):
        h = TileHarness()
        alpha = os.path.join(h.home, "alpha")    # 3 tiles → row mode
        beta = os.path.join(h.home, "beta")      # 1 tile  → non-row mode
        a = h.add_tiles(3, "A", alpha, nlines=20)[0]
        b = h.add_tile("B1", beta, nlines=20)
        h.start()
        # Wide viewport so the row-mode 900px cap bites and the uncapped non-row
        # tile is genuinely wider (→ different term.cols).
        page = _browser.new_page(viewport={"width": 2000, "height": 1000})
        try:
            page.goto(h.url)
            page.wait_for_selector('#tabs .tab[data-key="%s"]' % alpha, timeout=8000)
            switch_tab(page, alpha)              # 3 tiles visible → row mode
            fa = frame_for_port(page, a["port"])
            wait_ready(fa)
            wait_for_text(fa, "A01 SB 0020")
            page.wait_for_timeout(700)           # let the fit settle
            cols_row = fa.evaluate("() => window.__claudeTerm.cols")

            switch_tab(page, beta)               # 1 tile visible → non-row mode
            # B1 is hidden at boot → it reload-on-reveals (and a fresh TUI tile
            # loses its scrollback); we only need its term.cols, so let the reload
            # settle, re-fetch the frame, and read the width — no SB text needed.
            page.wait_for_timeout(2500)
            fb = frame_for_port(page, b["port"])
            wait_ready(fb)
            page.wait_for_timeout(700)
            cols_nonrow = fb.evaluate("() => window.__claudeTerm.cols")

            self.assertEqual(
                cols_row, cols_nonrow,
                "row-mode tile is %d cols but a non-row tile is %d cols at the same "
                "viewport → a tile moving between layouts (e.g. ringing onto Home) gets "
                "SIGWINCH'd and claude garbles. Pin the non-row/Home tile iframe to the "
                "row-mode constant width." % (cols_row, cols_nonrow))
        finally:
            page.close()
            h.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
