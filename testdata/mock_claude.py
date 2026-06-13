#!/usr/bin/env python3
"""A mock claude-/Ink-like TUI for browser regression tests of the dashboard
terminal tiles. It is deliberately faithful to the two behaviours that drive the
real bugs we test for:

  1. SCROLLBACK as plain printed lines, each with a UNIQUE marker
     ("<LABEL> SB 0001 ..."). Printed exactly once and never reprinted, so a test
     can assert each marker appears exactly once in xterm's buffer — any
     duplication (reflow / double-SIGWINCH / replay-on-top) shows up as a
     repeated marker.

  2. A LIVE FRAME pinned to the bottom, redrawn on SIGWINCH using the same
     cursor-up + erase-down model Ink's log-update uses. On a reattach
     (dtach -r winch) or a resize the frame repaints; if the grid width changed
     between renders, the erase miscounts and the frame can orphan/duplicate —
     the exact "double content" hazard. The frame carries a unique marker line
     ("<LABEL> FRAME winch=<n>") so a test can assert it is present exactly once.

Optionally, after MOCK_PROMPT_AFTER seconds it appends an interactive
"choose A / B / C" question to the frame (mimicking claude asking you something
while you may be on another tab). Typing a/b/c records the answer.

Runs forever (so the dtach session stays alive). All knobs via env:
  MOCK_LABEL          unique per-tile string baked into every marker (default T)
  MOCK_NLINES         scrollback lines to print at startup (default 40)
  MOCK_PROMPT_AFTER   seconds until the A/B/C prompt appears; 0 = never (default 0)
  MOCK_COLS_HINT      unused; width is learned from the tty / SIGWINCH
"""
import os
import select
import shutil
import signal
import sys
import time

LABEL = os.environ.get("MOCK_LABEL", "T")
NLINES = int(os.environ.get("MOCK_NLINES", "40"))
PROMPT_AFTER = float(os.environ.get("MOCK_PROMPT_AFTER", "0"))
# Opt-in (default off — the quiet default preserves dtach replay for the other
# tests): after boot, stream STREAM_COUNT more unique scrollback lines spaced
# STREAM_EVERY seconds apart, then go quiet. Models real claude printing output
# while a tile may be on a hidden tab (its renderer detached), so the buffer
# grows/scrolls under a stale renderer — the reveal-ghost trigger.
STREAM_COUNT = int(os.environ.get("MOCK_STREAM_COUNT", "0"))
STREAM_EVERY = float(os.environ.get("MOCK_STREAM_EVERY", "0.1"))
# Opt-in (default off): emit a bare BEL into the output stream once, BELL_AFTER
# seconds after boot — the hook-BEL path (claude's Stop/Notification hook does
# `printf '\a'`). Lets a test ring a tile deterministically without injecting
# keystrokes (input→pty delivery isn't exercised by the harness). 0 = never.
BELL_AFTER = float(os.environ.get("MOCK_BELL_AFTER", "0"))

out = sys.stdout
_winch = 0                 # how many SIGWINCH we've seen (proves the debounce works)
_winch_pending = True      # force a first render
_prev_frame_h = 0          # rows the last frame occupied (for cursor-up erase)
_answer = None             # 'A' / 'B' / 'C' once the user picks
_prompt_shown = False


def _w(s):
    out.write(s)
    out.flush()


def term_size():
    try:
        c = shutil.get_terminal_size((80, 24))
        return c.columns, c.lines
    except Exception:
        return 80, 24


def on_winch(_signum, _frame):
    global _winch, _winch_pending
    _winch += 1
    _winch_pending = True


def frame_lines():
    """The live, redrawn-every-time bottom region. Short lines (no wrap) except
    they're padded toward the terminal width so there's real text to render
    (a blank frame can't tell 'gray tile' from 'correctly empty')."""
    cols, _ = term_size()
    width = max(20, min(cols, 200))
    bar = "─" * (width - 1)
    lines = [
        bar,
        ("%s FRAME winch=%d" % (LABEL, _winch)).ljust(width - 1),
    ]
    if _prompt_shown:
        if _answer:
            lines.append(("%s ANSWERED: %s" % (LABEL, _answer)).ljust(width - 1))
        else:
            lines.append(("%s QUESTION: choose  A / B / C" % LABEL).ljust(width - 1))
    else:
        lines.append(("%s > _" % LABEL).ljust(width - 1))
    return lines


def render():
    """Redraw the bottom frame in place, Ink/log-update style: move up over the
    previous frame, erase to end of screen, reprint."""
    global _prev_frame_h
    lines = frame_lines()
    buf = []
    if _prev_frame_h:
        buf.append("\x1b[%dA" % _prev_frame_h)   # cursor up over the old frame
    buf.append("\r\x1b[0J")                        # col 0 + erase to end of screen
    buf.append("\r\n".join(lines))
    _w("".join(buf))
    _prev_frame_h = len(lines) - 1                 # rows BELOW the first (cursor sits on last)


def main():
    global _winch_pending, _prompt_shown, _prev_frame_h
    signal.signal(signal.SIGWINCH, on_winch)
    # Record our pid so the harness can reap us (we run under a dtach master that
    # daemonizes away from the launching process group, so killpg can't reach us).
    pidfile = os.environ.get("MOCK_PIDFILE")
    if pidfile:
        try:
            with open(pidfile, "w") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass

    # Scrollback: unique, once. Wide enough to render as real text.
    for i in range(1, NLINES + 1):
        _w("%s SB %04d %s\r\n" % (LABEL, i, "." * 40))

    started = time.time()
    streamed = 0
    next_stream = started + STREAM_EVERY
    _bell_fired = False
    while True:
        # Pop the prompt once its delay elapses.
        if PROMPT_AFTER > 0 and not _prompt_shown and (time.time() - started) >= PROMPT_AFTER:
            _prompt_shown = True
            _winch_pending = True

        # Ring once (hook-BEL path) after BELL_AFTER seconds — a bare BEL in the
        # output stream, no redraw, so it tests the bell path in isolation.
        if BELL_AFTER > 0 and not _bell_fired and (time.time() - started) >= BELL_AFTER:
            _bell_fired = True
            _w("\a")

        # Opt-in streaming: emit more unique scrollback over time, then go quiet.
        # Each new line scrolls the buffer; done while a tab is hidden it grows the
        # buffer under a detached renderer (the reveal-ghost trigger). We erase the
        # live frame, print the new SB line, then force a frame redraw — exactly
        # how Ink prints a log line above its pinned UI.
        if streamed < STREAM_COUNT and time.time() >= next_stream:
            streamed += 1
            next_stream = time.time() + STREAM_EVERY
            if _prev_frame_h:
                _w("\x1b[%dA\r\x1b[0J" % _prev_frame_h)   # erase the pinned frame
                _prev_frame_h = 0
            _w("%s ST %04d %s\r\n" % (LABEL, streamed, "." * 40))
            _winch_pending = True

        # Redraw ONLY on a real event (SIGWINCH / prompt / answer) — never on a
        # timer. A quiet idle program (like claude waiting) keeps its initial
        # scrollback at the front of dtach's recent-output ring buffer, so a fresh
        # reattach replays it. A chatty per-second redraw would roll the scrollback
        # out of that buffer and a reattaching client would lose it.
        if _winch_pending:
            _winch_pending = False
            render()

        r, _, _ = select.select([sys.stdin], [], [], 0.25)
        if r:
            try:
                data = os.read(sys.stdin.fileno(), 1024)
            except OSError:
                data = b""
            if not data:
                # EOF: the dtach master / PTY closed (harness teardown). Exit
                # instead of busy-spinning on a dead stdin.
                break
            for ch in data.decode("utf-8", "ignore").strip().upper():
                if ch == "!":
                    _w("\a")          # emit BEL on demand → dashboard rings this tile
                                      # (used by the Home-tab width test to put it on Home)
                elif ch in ("A", "B", "C") and _prompt_shown:
                    globals()["_answer"] = ch
                    _winch_pending = True


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
