#!/usr/bin/env python3
"""Browser-test harness: stand up the REAL dashboard (serve.py) embedding REAL
terminal tiles — each a genuine `ttyd -I term.html … dtach -A <sock> -r winch
mock_claude.py` chain, exactly as serve.py spawns a host claude session (see
spawn_claude_session). The mock program (testdata/mock_claude.py) behaves like an
Ink TUI: unique scrollback markers + a bottom frame redrawn on SIGWINCH, with an
optional A/B/C prompt.

This is what test_tile_visibility_browser.py drives with Playwright to reproduce
and guard against: gray/blank tiles after a tab is hidden and re-shown, scrollback
loss, and content duplication.

Everything is isolated in a throwaway registry dir; nothing touches the real
~/.claude-sessions. Teardown reaps serve.py, every ttyd, and every mock (which
runs under a daemonized dtach master, so we reap it by the pid it records).
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

DIR = os.path.dirname(os.path.abspath(__file__))
SERVE_PY = os.path.join(DIR, "serve.py")
TERM_HTML = os.path.join(DIR, "term.html")
MOCK = os.path.join(DIR, "testdata", "mock_claude.py")


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_listening(port, timeout=0.2):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _which(name):
    p = shutil.which(name)
    if not p:
        raise RuntimeError("required binary not found on PATH: %s" % name)
    return p


class TileHarness:
    def __init__(self):
        self.tmp = ""
        self.ttyd = ""
        self.dtach = ""
        self.ttyds = []          # ttyd Popens
        self.pidfiles = []       # mock pidfiles to reap
        self.tiles = []          # dicts: label, port, id, cwd, sock
        self.serve_proc = None
        self.port = 0
        self.url = ""
        self.home = os.path.expanduser("~")
        self._n = 0

    # -- tile creation -----------------------------------------------------
    def add_tile(self, label, cwd, nlines=40, prompt_after=0.0, program=None, run_cwd=None,
                 stream_count=0, stream_every=0.1, bell_after=0.0):
        """Spawn one real ttyd+dtach tile and register it. Call before start()
        (registry read on boot) — serve.py also re-polls, so adding after start
        works too, but tests add up front.

        program=None runs the mock TUI (testdata/mock_claude.py). Pass a command
        list (e.g. [claude, "--dangerously-skip-permissions"]) to run the real
        program instead — same ttyd/dtach wrapping as serve.py.

        stream_count>0 makes the mock keep emitting that many MORE unique
        scrollback lines (spaced stream_every s) after boot, then go quiet —
        models claude printing output while the tile may be on a hidden tab, so
        the buffer grows/scrolls under a detached renderer (the reveal-ghost
        trigger). Default 0 = the quiet behaviour the other tests rely on."""
        if not self.tmp:
            self.tmp = tempfile.mkdtemp(prefix="tileharness-")
            self.ttyd = _which("ttyd")
            self.dtach = _which("dtach")
        if not os.path.isfile(TERM_HTML):
            raise RuntimeError("term.html missing — run build-term.sh first")
        port = free_port()
        sock = os.path.join(self.tmp, "dtach-%d.sock" % port)
        pidfile = os.path.join(self.tmp, "mockpid-%d" % port)
        env = dict(
            os.environ,
            MOCK_LABEL=label,
            MOCK_NLINES=str(nlines),
            MOCK_PROMPT_AFTER=str(prompt_after),
            MOCK_PIDFILE=pidfile,
            MOCK_STREAM_COUNT=str(stream_count),
            MOCK_STREAM_EVERY=str(stream_every),
            MOCK_BELL_AFTER=str(bell_after),
        )
        prog = program if program else [sys.executable, MOCK]
        # Mirror serve.py spawn_claude_session EXACTLY (only the program differs).
        cmd = [self.ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
               "-I", TERM_HTML, "-i", "127.0.0.1", "-p", str(port),
               self.dtach, "-A", sock, "-r", "winch",
               *prog]
        p = subprocess.Popen(
            cmd, cwd=(run_cwd or self.tmp), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.ttyds.append(p)
        self.pidfiles.append(pidfile)
        for _ in range(120):
            if port_listening(port):
                break
            if p.poll() is not None:
                raise RuntimeError("ttyd died spawning tile %r (exit %s)" % (label, p.returncode))
            time.sleep(0.05)
        else:
            raise RuntimeError("ttyd never started listening for tile %r" % label)
        sid = "host-%d" % port
        self._n += 1
        started = datetime.fromtimestamp(time.time() - 1000 + self._n, tz=timezone.utc).isoformat()
        rec = {"name": label, "port": port, "kind": "host", "cwd": cwd,
               "started": started, "sock": sock}
        with open(os.path.join(self.tmp, sid + ".json"), "w") as f:
            json.dump(rec, f)
        info = dict(rec, id=sid, label=label, pidfile=pidfile)
        self.tiles.append(info)
        return info

    def add_tiles(self, n, label_prefix, cwd, nlines=40, prompt_after=0.0):
        return [self.add_tile("%s%02d" % (label_prefix, i + 1), cwd, nlines, prompt_after)
                for i in range(n)]

    # -- server ------------------------------------------------------------
    def start(self):
        if not self.tmp:
            raise RuntimeError("add at least one tile before start()")
        self.port = free_port()
        env = dict(os.environ, CLAUDE_SESSIONS_DIR=self.tmp)
        self.serve_proc = subprocess.Popen(
            [sys.executable, SERVE_PY, str(self.port), "--no-open"],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.url = "http://127.0.0.1:%d/" % self.port
        for _ in range(160):
            if port_listening(self.port):
                break
            if self.serve_proc.poll() is not None:
                raise RuntimeError("serve.py exited early (code %s)" % self.serve_proc.returncode)
            time.sleep(0.05)
        else:
            raise RuntimeError("serve.py did not start listening on %d" % self.port)
        return self

    def tab_keys(self):
        """The distinct workdir tab keys, in insertion order — what the dashboard
        groups tiles under. A test clicks #tabs .tab[data-key=...] to switch."""
        seen = []
        for t in self.tiles:
            if t["cwd"] not in seen:
                seen.append(t["cwd"])
        return seen

    # -- teardown ----------------------------------------------------------
    def stop(self):
        # 1) serve.py
        self._term(self.serve_proc)
        # 2) every ttyd (kills its foreground dtach CLIENT)
        for p in self.ttyds:
            self._term(p)
        # 3) every mock (runs under a daemonized dtach MASTER; reap by pid). When
        #    the mock dies its dtach master notices the child exit and goes too.
        for pf in self.pidfiles:
            try:
                with open(pf) as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError):
                pass
        time.sleep(0.2)
        for p in self.ttyds + [self.serve_proc]:
            if p and p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except OSError:
                    pass
        # Catch-all: any ttyd / dtach-master / real program still referencing this
        # harness's temp dir (the dtach socket path is under self.tmp, and it's in
        # both the ttyd and dtach argv). Reaps real-claude tiles, whose daemonized
        # dtach master killpg can't reach and which write no mock pidfile.
        if self.tmp:
            try:
                subprocess.run(["pkill", "-f", self.tmp], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
        if self.tmp and os.path.isdir(self.tmp):
            shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _term(p):
        if not p or p.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except OSError:
            try:
                p.terminate()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


if __name__ == "__main__":
    # Smoke: stand up 3 tiles in two tabs and print the URL, then idle.
    h = TileHarness()
    h.add_tiles(2, "A", os.path.join(h.home, "alpha"), prompt_after=5)
    h.add_tile("B1", os.path.join(h.home, "beta"))
    h.start()
    print("dashboard:", h.url, flush=True)
    print("tabs:", h.tab_keys(), flush=True)
    try:
        time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        h.stop()
