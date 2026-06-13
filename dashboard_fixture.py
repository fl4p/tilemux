#!/usr/bin/env python3
"""Isolated test fixture for the Claude Sessions dashboard (serve.py).

Spins up serve.py against a throwaway registry dir on a free port (NEVER 7680),
with a set of fake "sessions" each backed by its own dummy-listener subprocess
(so each fake session has its own pid on its own port — closing one must not
kill the others). Designed to exercise tile grouping into workdir tabs and the
horizontal-row layout, so a browser harness can drive the client-side logic.

Usage:
    from dashboard_fixture import Fixture
    fx = Fixture()
    fx.start()
    print(fx.url)   # http://127.0.0.1:<PORT>/
    ...
    fx.teardown()

Or run standalone:
    python3 dashboard_fixture.py        # starts, prints state, waits for Ctrl+C
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone


def free_port():
    """Bind :0, read the assigned port, release it. (Small race window before
    something re-grabs it, but fine for a local fixture.)"""
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


# A dummy listener: binds a port and accepts+drops every connection, so
# serve.py's port_alive() (a bare TCP connect) always sees it as a live
# session. Its OWN pid is what close_session SIGTERMs.
#
# It MUST accept() in a loop: port_alive never sends data and never closes
# cleanly, so if the listener only listen()ed and slept, the accept queue
# (backlog) would fill after a handful of probes and further connects would be
# refused — making the "session" flap to dead. Accepting and immediately
# closing keeps the queue drained.
LISTENER_SRC = (
    "import socket,sys;"
    "p=int(sys.argv[1]);"
    "s=socket.socket();"
    "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
    "s.bind(('127.0.0.1',p));"
    "s.listen(64);"
    "\nwhile True:\n"
    "    try:\n"
    "        c,_=s.accept(); c.close()\n"
    "    except OSError:\n"
    "        break\n"
)


class Fixture:
    def __init__(self):
        self.port: int = 0
        self.tmp: str = ""
        self.serve_proc: "subprocess.Popen | None" = None
        self.listeners = []          # list of (port, Popen)
        self.sessions = []           # list of registry dicts (as written)
        self.url: str = ""
        self.home = os.path.expanduser("~")
        self.serve_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve.py")

    def _iso(self, offset_seconds):
        # Distinct, ordered timestamps so the (started, id) sort is well-defined.
        t = time.time() + offset_seconds
        return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()

    def _spawn_listener(self, port):
        # start_new_session detaches the child from the controlling terminal so
        # a SIGHUP to the launching shell's process group can't reap it.
        p = subprocess.Popen(
            [sys.executable, "-c", LISTENER_SRC, str(port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.listeners.append((port, p))
        # Wait until it is actually listening.
        for _ in range(50):
            if port_listening(port):
                return True
            if p.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    def _write_session(self, name, cwd, kind, started):
        port = free_port()
        if not self._spawn_listener(port):
            raise RuntimeError("dummy listener failed to start on %d" % port)
        sid = "%s-%d" % (kind, port)
        rec = {
            "name": name,
            "port": port,
            "kind": kind,
            "cwd": cwd,
            "started": started,
        }
        with open(os.path.join(self.tmp, sid + ".json"), "w") as f:
            json.dump(rec, f)
        rec = dict(rec, id=sid)
        self.sessions.append(rec)
        return rec

    def start(self):
        if 7680 in (self.port,):
            raise RuntimeError("refusing to use port 7680")
        self.port = free_port()
        if self.port == 7680:
            self.port = free_port()
        self.tmp = tempfile.mkdtemp(prefix="dash-fixture-")
        h = self.home

        # ~6 sessions grouped to exercise tabs + row layout + the home boundary:
        #   projA      -> tab "projA" (2 sessions: projA + projA/sub joins it)
        #   projB      -> tab "projB" (3 sessions: projB + projB/x + projB/y -> row)
        #   solo       -> its own tab (1 session, under a different dir)
        # projA and projB are BOTH directly under home, so grouping must NOT
        # merge them (the tabKeyFor home-boundary guard).
        projA = os.path.join(h, "projA")
        projB = os.path.join(h, "projB")
        solo = os.path.join(h, "soloproj")
        long_name = "VERY-LONG-SESSION-TITLE-" + ("x" * 120) + "-END"

        # Distinct started timestamps so order is deterministic (oldest first).
        self._write_session("alpha-A", projA, "host", self._iso(-600))
        self._write_session("alpha-A-sub", os.path.join(projA, "sub"), "host", self._iso(-500))
        self._write_session("beta-B", projB, "host", self._iso(-400))
        self._write_session("beta-B-x", os.path.join(projB, "x"), "container", self._iso(-300))
        self._write_session(long_name, os.path.join(projB, "y"), "host", self._iso(-200))
        self._write_session("solo-one", solo, "container", self._iso(-100))

        env = dict(os.environ, CLAUDE_SESSIONS_DIR=self.tmp)
        self.serve_proc = subprocess.Popen(
            [sys.executable, self.serve_py, str(self.port), "--no-open"],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.url = "http://127.0.0.1:%d/" % self.port
        for _ in range(100):
            if port_listening(self.port):
                break
            if self.serve_proc.poll() is not None:
                raise RuntimeError("serve.py exited early (code %s)" % self.serve_proc.returncode)
            time.sleep(0.05)
        else:
            raise RuntimeError("serve.py did not start listening on %d" % self.port)
        return self

    def add_session(self, name, cwd, kind="host", started=None):
        """Spawn a new dummy-listener-backed session at runtime (simulating the
        duplicate button's spawn): pick a started timestamp if none is given,
        then reuse _write_session to bind a port, launch the listener, and drop
        a registry json into self.tmp. serve.py picks it up on the next poll.
        Requires start() to have been called (self.tmp must exist)."""
        if not self.tmp:
            raise RuntimeError("add_session() called before start()")
        if started is None:
            # Newest-so-far: one second past the latest existing session, so the
            # (started, id) sort lands it at the end and the order is well-defined.
            started = self._iso(1)
        return self._write_session(name, cwd, kind, started)

    def expected_order(self):
        """ids sorted by (started, id) — what /api/sessions should return."""
        return [s["id"] for s in sorted(self.sessions, key=lambda x: (x["started"], x["id"]))]

    def teardown(self):
        if self.serve_proc and self.serve_proc.poll() is None:
            self.serve_proc.terminate()
            try:
                self.serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.serve_proc.kill()
        for _, p in self.listeners:
            if p.poll() is None:
                p.terminate()
        for _, p in self.listeners:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        if self.tmp and os.path.isdir(self.tmp):
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    # Optional path to dump connection info as JSON (so a driver can read it
    # regardless of stdout buffering). Default keeps a copy in the temp dir too.
    info_path = sys.argv[1] if len(sys.argv) > 1 else None
    fx = Fixture()
    fx.start()
    assert fx.serve_proc is not None  # set by start(); narrows for the prints below
    info = {
        "url": fx.url,
        "port": fx.port,
        "registry": fx.tmp,
        "home": fx.home,
        "serve_pid": fx.serve_proc.pid,
        "expected_order": fx.expected_order(),
        "listener_pids": [p.pid for _, p in fx.listeners],
        "sessions": fx.sessions,
    }
    if info_path:
        with open(info_path, "w") as f:
            json.dump(info, f, indent=2)
    for line in ("URL:         " + fx.url,
                 "registry:    " + fx.tmp,
                 "home:        " + fx.home,
                 "serve pid:   " + str(fx.serve_proc.pid),
                 "expected id order: " + repr(fx.expected_order())):
        print(line, flush=True)
    print("sessions:", flush=True)
    for s in fx.sessions:
        print("  %-16s port=%-6d kind=%-9s cwd=%s" % (s["id"], s["port"], s["kind"], s["cwd"]), flush=True)
    print("\nReady. Ctrl+C / SIGTERM to tear down.", flush=True)

    import signal as _signal
    import urllib.request

    def _bye(*_):
        raise KeyboardInterrupt
    _signal.signal(_signal.SIGTERM, _bye)
    _signal.signal(_signal.SIGINT, _bye)

    def _api_count():
        try:
            req = urllib.request.Request(
                fx.url + "api/sessions", headers={"Host": "127.0.0.1:%d" % fx.port})
            with urllib.request.urlopen(req, timeout=2) as r:
                return len(json.load(r)["sessions"])
        except Exception as e:  # noqa: BLE001
            return "ERR:%s" % e

    try:
        i = 0
        while True:
            # Self-check from inside the fixture's own namespace once a second,
            # so the driver can read the live API count from this log.
            print("selfcheck t=%ds api_count=%s" % (i, _api_count()), flush=True)
            time.sleep(1)
            i += 1
    except KeyboardInterrupt:
        pass
    finally:
        fx.teardown()
        print("torn down.", flush=True)
