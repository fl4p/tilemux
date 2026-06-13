#!/usr/bin/env python3
"""Smoke test for serve.py (the localhost "Claude Sessions" dashboard).

Stdlib only: unittest + urllib.request + http.client + subprocess + socket.

Launches serve.py as a subprocess on a free port with --no-open, pointed at an
isolated temp registry dir (CLAUDE_SESSIONS_DIR) so it never reads, prunes, or
disrupts the real ~/.claude-sessions or the live dashboard on 7680. Tears the
subprocess down in tearDownModule.

Run:  python3 test_serve.py -v   or   python3 -m pytest
"""
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock as mock
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVE = os.path.join(HERE, "serve.py")

# Import serve.py in-process for unit tests of its registry logic (read_sessions
# / close_session / duplicate_session). serve.py parses sys.argv[1] as a port at
# import time, so neutralize argv first — a test-runner argv like ["-v"] would
# crash int(). main() is __main__-guarded, so importing starts no server. These
# functions read the module global serve.REGISTRY, which RegistryLogicTest
# overrides per-test to a temp dir; the real ~/.claude-sessions is never touched.
sys.path.insert(0, HERE)
_saved_argv = sys.argv
sys.argv = ["serve.py"]
try:
    import serve  # noqa: E402
finally:
    sys.argv = _saved_argv

# Module-level handles populated by setUpModule / torn down by tearDownModule.
_proc = None
_tmpdir = None
PORT = None
HOST_HDR = None  # the valid "127.0.0.1:<port>" Host header


def _free_port():
    """Grab a port the OS says is free (bind to :0, read it, release it).

    There is an inherent TOCTOU window, but serve.py sets allow_reuse_address
    and we immediately hand the port to the child, so this is fine for a test.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_for_port(port, proc, timeout=5.0):
    """Poll until a TCP connection to 127.0.0.1:port succeeds, or fail loudly."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:  # child died early
            out, err = _drain(proc)
            raise RuntimeError(
                "serve.py exited early (code %s)\nSTDOUT:\n%s\nSTDERR:\n%s"
                % (proc.returncode, out, err))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    out, err = _drain(proc)
    raise RuntimeError(
        "serve.py never started listening on %d within %.1fs\nSTDOUT:\n%s\nSTDERR:\n%s"
        % (port, timeout, out, err))


def _drain(proc):
    """Best-effort capture of whatever the child printed so far."""
    try:
        return proc.communicate(timeout=1)
    except Exception:
        return ("<unavailable>", "<unavailable>")


def setUpModule():
    global _proc, _tmpdir, PORT, HOST_HDR
    PORT = _free_port()
    if PORT == 7680:  # never collide with the live dashboard
        PORT = _free_port()
    HOST_HDR = "127.0.0.1:%d" % PORT
    _tmpdir = tempfile.mkdtemp(prefix="serve-test-registry-")

    env = dict(os.environ)
    # serve.py honors CLAUDE_SESSIONS_DIR (see REGISTRY in serve.py); point it at
    # an empty temp dir so the test cannot read/prune/touch ~/.claude-sessions.
    env["CLAUDE_SESSIONS_DIR"] = _tmpdir

    _proc = subprocess.Popen(
        [sys.executable, SERVE, str(PORT), "--no-open"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    _wait_for_port(PORT, _proc)


def tearDownModule():
    if _proc is not None:
        _proc.terminate()
        try:
            _proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _proc.kill()
            _proc.wait(timeout=3)
    if _tmpdir is not None:
        try:
            for fn in os.listdir(_tmpdir):
                os.remove(os.path.join(_tmpdir, fn))
            os.rmdir(_tmpdir)
        except OSError:
            pass


def _get(path, host=None):
    """GET via http.client so we fully control the Host header. Returns (status, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    try:
        headers = {"Host": host} if host is not None else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _post(path, host=None, csrf=None):
    """POST via http.client with full Host / X-CSRF-Token control. Returns (status, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    try:
        headers = {}
        if host is not None:
            headers["Host"] = host
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        headers["Content-Length"] = "0"
        conn.request("POST", path, body=b"", headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class ServeSmokeTest(unittest.TestCase):
    def _csrf_token(self):
        """Fetch / and extract the per-process csrf token from the meta tag."""
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        text = body.decode()
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', text)
        if m is None:
            self.fail("csrf-token meta tag not found in dashboard HTML")
        token = m.group(1)
        self.assertTrue(token)
        self.assertNotEqual(token, "__CSRF_TOKEN__", "placeholder was not substituted")
        return token

    def test_root_serves_dashboard_with_csrf(self):
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        self.assertIn(b'<meta name="csrf-token"', body)
        token = self._csrf_token()
        self.assertGreater(len(token), 10)

    def test_dashboard_persists_and_restores_active_tab(self):
        # The dashboard remembers the last-viewed tab across reloads via a
        # localStorage key. This guards the client-side wiring (the JS lives in
        # the served HTML, so there's no JS unit harness): the storage key, the
        # save-on-click, and the restore-if-still-present branch must all be
        # present. The end-to-end behavior is verified manually via the browser
        # fixture; this keeps the wiring from silently regressing.
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn("claude-sessions-active-tab", text)   # storage key
        self.assertIn("saveActiveTab", text)                # persist helper
        self.assertIn("savedTab", text)                     # restore-on-reload state

    def test_root_via_urllib(self):
        # Sanity check that a normal urllib client (Host defaults to host:port) works.
        req = urllib.request.Request("http://127.0.0.1:%d/" % PORT)
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b'<meta name="csrf-token"', resp.read())

    def test_api_sessions(self):
        status, body = _get("/api/sessions", host=HOST_HDR)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("sessions", data)
        self.assertIsInstance(data["sessions"], list)
        self.assertIn("home", data)
        self.assertIsInstance(data["home"], str)
        # Isolated temp registry is empty -> no sessions leak in.
        self.assertEqual(data["sessions"], [])

    def test_bogus_host_forbidden(self):
        status, body = _get("/", host="evil.example.com")
        self.assertEqual(status, 403)
        self.assertIn(b"forbidden", body)

    def test_api_sessions_bogus_host_forbidden(self):
        status, _ = _get("/api/sessions", host="evil.example.com")
        self.assertEqual(status, 403)

    def test_unknown_path_404(self):
        status, body = _get("/nope/not/here", host=HOST_HDR)
        self.assertEqual(status, 404)
        self.assertIn(b"not found", body)

    def test_close_without_csrf_forbidden(self):
        status, body = _post("/api/close?id=does-not-exist", host=HOST_HDR)
        self.assertEqual(status, 403)
        self.assertIn(b"bad csrf token", body)

    def test_close_wrong_csrf_forbidden(self):
        status, body = _post("/api/close?id=does-not-exist",
                             host=HOST_HDR, csrf="totally-wrong-token")
        self.assertEqual(status, 403)
        self.assertIn(b"bad csrf token", body)

    def test_proxy_requires_csrf_token(self):
        # A cross-origin page (evil.com) can still cause the browser to GET
        # /proxy with a valid Host header — Host validation alone isn't enough.
        # The CSRF token in the query gates this; cross-origin scripts can't
        # read it because same-origin policy blocks them from reading the
        # dashboard's own HTML.
        status, body = _get("/proxy?id=webview-1", host=HOST_HDR)
        self.assertEqual(status, 403)
        self.assertIn(b"bad csrf", body)

    def test_proxy_rejects_wrong_csrf(self):
        status, _ = _get("/proxy?id=webview-1&csrf=wrong", host=HOST_HDR)
        self.assertEqual(status, 403)

    def test_proxy_rejects_unknown_id(self):
        # Even with a valid CSRF, /proxy refuses unknown ids — there's no raw
        # `url=` param to abuse, so the SSRF surface is the registry alone.
        token = self._csrf_token()
        status, body = _get("/proxy?id=__no_such_webview__&csrf=" + token, host=HOST_HDR)
        self.assertEqual(status, 404)
        self.assertIn(b"no such webview", body)

    def test_proxy_rejects_non_webview_id(self):
        # Path traversal / wrong-kind ids must be refused — proxy only fetches
        # what a webview entry explicitly registered.
        token = self._csrf_token()
        status, _ = _get("/proxy?id=host-1&csrf=" + token, host=HOST_HDR)
        self.assertEqual(status, 404)
        status, _ = _get("/proxy?id=../../etc/passwd&csrf=" + token, host=HOST_HDR)
        self.assertEqual(status, 404)

    def test_close_correct_csrf_noop_for_missing_session(self):
        # Safe: close_session() opens REGISTRY/<id>.json first; a non-existent id
        # returns False immediately and touches no process. The registry is an
        # isolated temp dir anyway, so even a real-looking id can't hit anything.
        token = self._csrf_token()
        status, body = _post("/api/close?id=__definitely_not_a_real_session__",
                             host=HOST_HDR, csrf=token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": False})


class RegistryLogicTest(unittest.TestCase):
    """In-process unit tests of serve.py's registry functions, with REGISTRY
    pointed at a fresh temp dir per test (no subprocess, no real registry)."""

    def setUp(self):
        self.reg = tempfile.mkdtemp(prefix="serve-unit-reg-")
        self._saved_reg = serve.REGISTRY
        serve.REGISTRY = self.reg
        self._listeners = []
        serve._last_alive.clear()   # port-alive hysteresis is a module global

    def tearDown(self):
        serve.REGISTRY = self._saved_reg
        serve._last_alive.clear()
        for s in self._listeners:
            try:
                s.close()
            except OSError:
                pass
        for fn in os.listdir(self.reg):
            try:
                os.remove(os.path.join(self.reg, fn))
            except OSError:
                pass
        try:
            os.rmdir(self.reg)
        except OSError:
            pass

    def _listen(self):
        """Open a real listening socket; its port reads as alive (port_alive True)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        self._listeners.append(s)
        return s.getsockname()[1]

    @staticmethod
    def _dead_port():
        """A port nothing listens on (bind then close), so port_alive is False."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _write(self, sid, **fields):
        fields.setdefault("port", 1)
        path = os.path.join(self.reg, sid + ".json")
        with open(path, "w") as f:
            json.dump(fields, f)
        return path

    def test_live_session_listed(self):
        port = self._listen()
        self._write("host-1", port=port, name="x", cwd="/tmp",
                    started="2026-01-01T00:00:00")
        out = serve.read_sessions()
        self.assertEqual([s["id"] for s in out], ["host-1"])
        self.assertEqual(out[0]["port"], port)

    def test_stable_order_by_started_then_id(self):
        # Order must be (started, then id) regardless of filesystem listing order.
        # Regression: sorting by name alone tied on duplicate basenames and fell
        # back to listdir order, which varied across reloads.
        pa, pb, pc = self._listen(), self._listen(), self._listen()
        self._write("host-b", port=pa, started="2026-01-01T00:00:02")
        self._write("host-a", port=pb, started="2026-01-01T00:00:01")
        self._write("host-c", port=pc, started="2026-01-01T00:00:01")  # ties host-a; id breaks it
        out = [s["id"] for s in serve.read_sessions()]
        self.assertEqual(out, ["host-a", "host-c", "host-b"])

    def test_dead_port_stale_is_pruned(self):
        path = self._write("host-dead", port=self._dead_port(), started="x")
        old = time.time() - (serve.PRUNE_GRACE + 10)
        os.utime(path, (old, old))
        self.assertEqual(serve.read_sessions(), [])
        self.assertFalse(os.path.exists(path), "stale dead-port entry should be pruned")

    def test_dead_port_recent_is_kept_but_not_listed(self):
        # Within the grace window: not returned (port dead) but file kept, so we
        # don't race a session whose ttyd hasn't started listening yet.
        path = self._write("host-booting", port=self._dead_port(), started="x")
        self.assertEqual(serve.read_sessions(), [])
        self.assertTrue(os.path.exists(path), "recent dead-port entry should NOT be pruned")

    def test_confirmed_alive_session_survives_a_transient_dead_probe(self):
        # The mass-vanish regression: under load every port_alive() probe can time
        # out in one poll. A session CONFIRMED alive moments ago must keep being
        # listed through a failed probe (recently_alive), so it isn't dropped from
        # /api/sessions and reaped by the frontend.
        self._write("host-flap", port=self._dead_port(), started="x",
                    name="flap", cwd="/tmp")
        serve._last_alive["host-flap"] = time.time()   # probed alive just now
        out = [s["id"] for s in serve.read_sessions()]
        self.assertEqual(out, ["host-flap"],
                         "a just-alive session must ride out a transient dead probe")
        # And it is NOT mislabelled dead while riding out the blip.
        self.assertFalse(serve.read_sessions()[0].get("dead"))

    def test_alive_hysteresis_expires_and_session_falls_out(self):
        # Once the failure has persisted past ALIVE_HYSTERESIS, a dead session is
        # genuinely gone and drops from the list (so a real crash still disappears).
        self._write("host-gone", port=self._dead_port(), started="x")
        serve._last_alive["host-gone"] = time.time() - (serve.ALIVE_HYSTERESIS + 5)
        self.assertEqual(serve.read_sessions(), [],
                         "a session dead past the hysteresis window must not be listed")

    def test_never_alive_session_is_not_kept_by_hysteresis(self):
        # A booting session that was NEVER seen alive has no _last_alive record, so
        # hysteresis must not vouch for it — same as before the fix.
        self._write("host-fresh", port=self._dead_port(), started="x")
        self.assertEqual(serve.read_sessions(), [])

    def test_closed_session_is_evicted_from_hysteresis_cache(self):
        # When a session's registry file is gone (closed), its hysteresis record is
        # dropped, so the cache can't grow without bound or vouch for a reused id.
        path = self._write("host-closed", port=self._dead_port(), started="x")
        serve._last_alive["host-closed"] = time.time()
        serve.read_sessions()                 # listed via hysteresis; cache retained
        self.assertIn("host-closed", serve._last_alive)
        os.remove(path)
        serve.read_sessions()                 # file gone → cache entry pruned
        self.assertNotIn("host-closed", serve._last_alive)

    def test_port_alive_retries_on_timeout_then_succeeds(self):
        # A live ttyd whose first connect times out under load must still read as
        # alive on the retry — a single missed 0.2 s connect can't mark it dead.
        import socket as _socket
        calls = {"n": 0}
        real = serve.socket.create_connection

        def flaky(addr, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _socket.timeout("simulated load stall")
            return real(addr, timeout=timeout)

        port = self._listen()
        orig = serve.socket.create_connection
        serve.socket.create_connection = flaky
        try:
            self.assertTrue(serve.port_alive(port),
                            "port_alive must retry past a transient timeout")
            self.assertEqual(calls["n"], 2, "should have taken exactly one retry")
        finally:
            serve.socket.create_connection = orig

    def test_port_alive_does_not_retry_on_refused(self):
        # A refused connection is a definitive "nobody home" — return at once so
        # dead-port pruning stays fast (no doubled latency on every dead port).
        calls = {"n": 0}

        def refused(addr, timeout=None):
            calls["n"] += 1
            raise ConnectionRefusedError("nobody listening")

        orig = serve.socket.create_connection
        serve.socket.create_connection = refused
        try:
            self.assertFalse(serve.port_alive(self._dead_port()))
            self.assertEqual(calls["n"], 1, "refused must not be retried")
        finally:
            serve.socket.create_connection = orig

    def _fork_fixture(self, record_session_id=True):
        """A container session in a temp cwd with two conversation jsonls: the
        session's OWN one (mine, written long ago) and a sibling (other, the
        newest). When record_session_id, the registry pins `mine` as this
        session's id. Returns (cwd, proj_dir, mine_uuid, other_uuid)."""
        cwd = tempfile.mkdtemp(prefix="serve-fork-cwd-")
        self.addCleanup(shutil.rmtree, cwd, ignore_errors=True)
        proj = os.path.join(cwd, ".claude", "projects", "-workspace")
        os.makedirs(proj)
        mine = "11111111-1111-1111-1111-111111111111"
        other = "22222222-2222-2222-2222-222222222222"
        with open(os.path.join(proj, mine + ".jsonl"), "w") as f:
            f.write('{"sessionId":"%s","tag":"mine"}\n' % mine)
        with open(os.path.join(proj, other + ".jsonl"), "w") as f:
            f.write('{"sessionId":"%s","tag":"other"}\n' % other)
        # Make the SIBLING strictly newer, so _newest_jsonl would (wrongly) pick it.
        t0 = time.time()
        os.utime(os.path.join(proj, mine + ".jsonl"), (t0 - 100, t0 - 100))
        os.utime(os.path.join(proj, other + ".jsonl"), (t0, t0))
        extra = {"session_id": mine} if record_session_id else {}
        self._write("container-1", kind="container", cwd=cwd, **extra)
        return cwd, proj, mine, other

    def test_fork_uses_session_id_not_newest_sibling(self):
        # Regression: fork once resolved the conversation via _newest_jsonl(cwd),
        # so with two claude tiles in one cwd it forked whichever wrote last —
        # "forks the wrong session". With a recorded session_id it must fork the
        # CLICKED session's own jsonl even when a sibling is newer.
        cwd, proj, mine, other = self._fork_fixture(record_session_id=True)
        captured = {}

        def fake_popen(cmd, **kw):
            captured["cmd"] = list(cmd)
            return mock.Mock()

        with mock.patch("serve.subprocess.Popen", side_effect=fake_popen):
            self.assertTrue(serve.fork_session("container-1"))
        cmd = captured["cmd"]
        new_uuid = cmd[cmd.index("--resume") + 1]
        with open(os.path.join(proj, new_uuid + ".jsonl")) as f:
            body = f.read()
        self.assertIn('"tag":"mine"', body)        # forked the clicked session…
        self.assertNotIn('"tag":"other"', body)    # …not the newer sibling
        self.assertIn(new_uuid, body)              # embedded id rewritten to fork
        self.assertNotIn(mine, body)

    def test_fork_with_recorded_id_but_missing_jsonl_fails_not_sibling(self):
        # A tile whose recorded session_id has no transcript on disk (a session
        # that never spoke, or an id that went stale before hook-driven
        # tracking corrected it) must NOT fall back to the newest .jsonl in the
        # cwd — that's a SIBLING tile's conversation, the "forking the wrong
        # tiles" bug. The fork fails instead: nothing spawned, nothing copied.
        cwd, proj, mine, other = self._fork_fixture(record_session_id=True)
        self._write("container-1", kind="container", cwd=cwd,
                    session_id="99999999-9999-9999-9999-999999999999")
        files_before = sorted(os.listdir(proj))
        with mock.patch("serve.subprocess.Popen") as popen:
            self.assertFalse(serve.fork_session("container-1"))
            popen.assert_not_called()
        self.assertEqual(sorted(os.listdir(proj)), files_before)

    def test_fork_falls_back_to_newest_for_legacy_session(self):
        # Sessions registered before session_id existed have none; fork keeps the
        # old newest-in-cwd behaviour rather than failing.
        cwd, proj, mine, other = self._fork_fixture(record_session_id=False)
        captured = {}

        def fake_popen(cmd, **kw):
            captured["cmd"] = list(cmd)
            return mock.Mock()

        with mock.patch("serve.subprocess.Popen", side_effect=fake_popen):
            self.assertTrue(serve.fork_session("container-1"))
        cmd = captured["cmd"]
        new_uuid = cmd[cmd.index("--resume") + 1]
        with open(os.path.join(proj, new_uuid + ".jsonl")) as f:
            body = f.read()
        self.assertIn('"tag":"other"', body)       # newest sibling (fallback)

    def test_session_jsonl_resolves_exact_file(self):
        cwd, proj, mine, other = self._fork_fixture(record_session_id=True)
        self.assertEqual(serve._session_jsonl(cwd, True, mine),
                         os.path.join(proj, mine + ".jsonl"))
        # Unknown / missing id → None, so callers fall back to _newest_jsonl.
        self.assertIsNone(serve._session_jsonl(cwd, True, "deadbeef"))
        self.assertIsNone(serve._session_jsonl(cwd, True, ""))
        self.assertIsNone(serve._session_jsonl(cwd, True, None))

    def test_tile_jsonl_clone_with_unwritten_session_is_empty(self):
        # Regression: a freshly CLONED tile is a new empty session whose own
        # .jsonl doesn't exist yet. _tile_jsonl must NOT fall back to the newest
        # sibling (the original it was cloned from) — it must return None so the
        # chat panel shows empty until the clone writes its own transcript.
        cwd, proj, mine, other = self._fork_fixture(record_session_id=True)
        fresh = "33333333-3333-3333-3333-333333333333"   # recorded id, no file yet
        self._write("container-1", kind="container", cwd=cwd, session_id=fresh)
        self.assertIsNone(serve._tile_jsonl("container-1"),
                          "clone must not surface a sibling's transcript")

    def test_tile_jsonl_uses_exact_session_file_not_newer_sibling(self):
        # With the session's own file present, resolve THAT — never the newer
        # sibling, even though _newest_jsonl alone would pick the sibling.
        cwd, proj, mine, other = self._fork_fixture(record_session_id=True)
        self.assertEqual(serve._tile_jsonl("container-1"),
                         os.path.join(proj, mine + ".jsonl"))

    def test_tile_jsonl_legacy_no_session_id_falls_back_to_newest(self):
        # Legacy tiles (registered before session_id tracking) keep the
        # newest-in-cwd fallback as the only thing they can go on.
        cwd, proj, mine, other = self._fork_fixture(record_session_id=False)
        self.assertEqual(serve._tile_jsonl("container-1"),
                         os.path.join(proj, other + ".jsonl"))

    def test_tile_jsonl_none_for_non_claude_tiles(self):
        self._write("term-1", kind="terminal", cwd="/tmp")
        self.assertIsNone(serve._tile_jsonl("term-1"))
        self._write("web-1", kind="webview", cwd="/tmp")
        self.assertIsNone(serve._tile_jsonl("web-1"))

    def test_close_session_deletes_registry_file(self):
        # The close-reappear fix's SERVER half: close removes the registry file
        # synchronously, so a poll AFTER the close returns can't list it again
        # (the client half — render() not re-creating the tile — is covered by
        # the browser regression suite).
        path = self._write("host-9", port=self._dead_port(),
                           sock=os.path.join(self.reg, "nope.sock"))
        self.assertTrue(os.path.exists(path))
        self.assertTrue(serve.close_session("host-9"))
        self.assertFalse(os.path.exists(path), "close_session must delete the registry file")
        self.assertEqual(serve.read_sessions(), [])

    # --- note tiles (backend-less text/image scratchpads) ---
    def test_spawn_note_creates_listed_portless_entry(self):
        sid = serve.spawn_note(cwd="/tmp/proj")
        self.assertEqual(sid, "note-1")
        with open(os.path.join(self.reg, sid + ".json")) as f:
            rec = json.load(f)
        self.assertEqual(rec["kind"], "note")
        self.assertEqual(rec["name"], "Note 1")
        self.assertEqual(rec["cwd"], "/tmp/proj")
        self.assertNotIn("port", rec)
        out = serve.read_sessions()
        self.assertEqual([(s["id"], s["kind"]) for s in out], [("note-1", "note")])
        self.assertNotIn("port", out[0], "note tiles are dashboard-served — no port")

    def test_spawn_note_monotonic_ids(self):
        self.assertEqual(serve.spawn_note(), "note-1")
        self.assertEqual(serve.spawn_note(), "note-2")

    def test_note_body_roundtrip(self):
        sid = serve.spawn_note()
        self.assertTrue(serve.write_note_body(sid, "hello <b>x</b>"))
        self.assertEqual(serve.read_note_body(sid), "hello <b>x</b>")

    def test_note_body_rejects_oversize(self):
        sid = serve.spawn_note()
        self.assertFalse(serve.write_note_body(sid, "a" * (serve.NOTE_MAX_BYTES + 1)))
        self.assertEqual(serve.read_note_body(sid), "",
                         "an oversize write must not partially land")

    def test_note_body_rejects_bad_sid(self):
        for bad in ("note-abc", "../evil", "host-1", ""):
            self.assertFalse(serve.write_note_body(bad, "x"),
                             "write_note_body must reject sid %r" % bad)
            self.assertEqual(serve.read_note_body(bad), "")

    def test_note_body_not_resurrected_for_missing_note(self):
        # A late autosave arriving after the note is closed must NOT recreate a
        # sidecar (write requires the registry entry to still exist).
        self.assertFalse(serve.write_note_body("note-999", "x"))
        self.assertFalse(os.path.exists(os.path.join(self.reg, "note-999.body")))

    def test_close_note_removes_registry_and_body(self):
        sid = serve.spawn_note()
        serve.write_note_body(sid, "stuff")
        body_path = os.path.join(self.reg, sid + ".body")
        self.assertTrue(os.path.exists(body_path))
        self.assertTrue(serve.close_session(sid))
        self.assertFalse(os.path.exists(os.path.join(self.reg, sid + ".json")))
        self.assertFalse(os.path.exists(body_path),
                         "close must delete the note's body sidecar too")

    def test_close_unknown_id_returns_false(self):
        self.assertFalse(serve.close_session("nonexistent"))

    def test_close_empty_id_returns_false(self):
        self.assertFalse(serve.close_session(""))

    def test_close_path_traversal_guarded(self):
        # sid is basename()'d, so a traversal attempt resolves inside REGISTRY
        # (to a file that doesn't exist) and returns False — it can't escape.
        self.assertFalse(serve.close_session("../../../../etc/hosts"))

    def test_duplicate_validation_rejections(self):
        # Only the safe rejection paths — never the spawn path, which would
        # launch a real claude process.
        self.assertFalse(serve.duplicate_session("does-not-exist"))
        self._write("host-x", port=1, cwd="/no/such/dir/exists/here")
        self.assertFalse(serve.duplicate_session("host-x"))

    def test_port_shared_by_other_detection(self):
        self._write("host-1", port=7777)
        self._write("host-2", port=7777)   # same port — collision
        self._write("host-3", port=8888)   # distinct
        self.assertTrue(serve._port_shared_by_other("host-1", 7777))
        self.assertTrue(serve._port_shared_by_other("host-2", 7777))
        self.assertFalse(serve._port_shared_by_other("host-3", 8888))
        self.assertFalse(serve._port_shared_by_other("host-1", None))

    def test_close_shared_port_spares_bystander(self):
        # A port collision (two sessions on the same port). Closing one must NOT
        # kill by port — that would take the bystander's ttyd down too — and must
        # leave the other's registry entry intact. Guards the "closing one closes
        # others / sessions disappear" bug.
        dead = self._dead_port()
        a = self._write("host-a", port=dead, sock=os.path.join(self.reg, "a.sock"))
        b = self._write("host-b", port=dead, sock=os.path.join(self.reg, "b.sock"))
        self.assertTrue(serve.close_session("host-a"))
        self.assertFalse(os.path.exists(a), "closed session's entry should be removed")
        self.assertTrue(os.path.exists(b), "bystander sharing the port must survive")

    def test_close_unique_port_removes_own_sock(self):
        dead = self._dead_port()
        sock = os.path.join(self.reg, "uniq.sock")
        open(sock, "w").close()   # stand-in for the dtach socket file
        path = self._write("host-solo", port=dead, sock=sock)
        self.assertTrue(serve.close_session("host-solo"))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(sock), "unique-port close should remove its own sock")


class _FakeProc:
    """Minimal Popen stand-in so fork tests don't actually spawn claude."""
    pid = 12345
    def poll(self): return None
    def wait(self, *a, **kw): return 0
    def kill(self): pass


class _FakeCompleted:
    """Minimal subprocess.run() return so tests don't shell out for real."""
    returncode = 0
    stdout = ""
    stderr = ""


class NewTileKindsTest(unittest.TestCase):
    """Unit tests for the terminal + webview tile types: read/close lifecycle,
    create_webview/update_webview/duplicate_session behavior. spawn_terminal
    itself isn't unit-tested because it would launch a real ttyd."""

    def setUp(self):
        self.reg = tempfile.mkdtemp(prefix="serve-newkinds-")
        self._saved_reg = serve.REGISTRY
        serve.REGISTRY = self.reg
        self._tmpdirs = []
        self._listeners = []
        self._saved_home = None

    def tearDown(self):
        serve.REGISTRY = self._saved_reg
        for s in self._listeners:
            try:
                s.close()
            except OSError:
                pass
        for fn in os.listdir(self.reg):
            try:
                os.remove(os.path.join(self.reg, fn))
            except OSError:
                pass
        try:
            os.rmdir(self.reg)
        except OSError:
            pass
        # Tear down any staged ~/.claude/projects shadow trees fork tests built.
        import shutil
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
        if self._saved_home is not None:
            os.environ["HOME"] = self._saved_home

    def _listen_socket(self):
        """Open a real listening socket; its port reads as alive (port_alive True)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        self._listeners.append(s)
        return s.getsockname()[1]

    def _dead_port(self):
        """A port nothing listens on (bind then close), so port_alive is False."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _write_host(self, sid, port, cwd, **extra):
        e = {"name": sid, "port": port, "kind": "host", "cwd": cwd}; e.update(extra)
        with open(os.path.join(self.reg, sid + ".json"), "w") as f:
            json.dump(e, f)

    def _write_terminal(self, sid, cwd):
        with open(os.path.join(self.reg, sid + ".json"), "w") as f:
            json.dump({"name": "t", "port": 1, "kind": "terminal", "cwd": cwd}, f)

    def _stage_projects_for_host(self):
        """Create a fake cwd dir, point HOME at a temp tree, and pre-build
        ~/.claude/projects/<slug>/ so _newest_jsonl finds files placed there.
        Returns (cwd, projects_dir)."""
        home = tempfile.mkdtemp(prefix="serve-fork-home-")
        self._tmpdirs.append(home)
        cwd = tempfile.mkdtemp(prefix="serve-fork-cwd-")
        self._tmpdirs.append(cwd)
        slug = cwd.rstrip("/").replace("/", "-")
        projects = os.path.join(home, ".claude", "projects", slug)
        os.makedirs(projects, exist_ok=True)
        # Point HOME at the staged tree so os.path.expanduser('~') resolves
        # there for the duration of the test. Capture the prior value so
        # tearDown can restore it.
        if self._saved_home is None:
            self._saved_home = os.environ.get("HOME", "")
        os.environ["HOME"] = home
        return cwd, projects

    # --- webview ----------------------------------------------------------

    def test_create_webview_writes_registry_entry(self):
        sid = serve.create_webview("example.com")
        self.assertEqual(sid, "webview-1")
        assert sid is not None
        path = os.path.join(self.reg, sid + ".json")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            entry = json.load(f)
        self.assertEqual(entry["kind"], "webview")
        self.assertEqual(entry["url"], "https://example.com",
                         "bare host must be normalized to https://")
        self.assertIn("started", entry)

    def test_create_webview_preserves_existing_scheme(self):
        sid = serve.create_webview("http://foo/bar")
        assert sid is not None
        with open(os.path.join(self.reg, sid + ".json")) as f:
            self.assertEqual(json.load(f)["url"], "http://foo/bar")

    def test_create_webview_empty_url_rejected(self):
        self.assertIsNone(serve.create_webview(""))
        self.assertIsNone(serve.create_webview("   "))
        self.assertEqual(os.listdir(self.reg), [])

    def test_create_webview_stores_cwd_hint_for_tab_grouping(self):
        # Frontend passes activeTab as cwd so the new webview lands in the same
        # tab the user was viewing — this exercises the storage half.
        sid = serve.create_webview("https://x.test", cwd="/Users/me/proj")
        assert sid is not None
        with open(os.path.join(self.reg, sid + ".json")) as f:
            entry = json.load(f)
        self.assertEqual(entry["cwd"], "/Users/me/proj")
        # And it surfaces through read_sessions so the frontend's tabKeyFor()
        # can group on it.
        out = serve.read_sessions()
        self.assertEqual(out[0]["cwd"], "/Users/me/proj")

    def test_create_webview_ids_are_monotonic(self):
        a = serve.create_webview("a.test")
        b = serve.create_webview("b.test")
        c = serve.create_webview("c.test")
        self.assertEqual([a, b, c], ["webview-1", "webview-2", "webview-3"])

    def test_read_sessions_lists_webview_without_port_check(self):
        # Webviews have no port and must show up regardless of port_alive.
        sid = serve.create_webview("https://x.test")
        out = serve.read_sessions()
        self.assertEqual([s["id"] for s in out], [sid])
        self.assertEqual(out[0]["kind"], "webview")
        self.assertEqual(out[0]["url"], "https://x.test")
        self.assertNotIn("port", out[0])

    def test_close_webview_drops_only_the_file(self):
        sid = serve.create_webview("https://x.test")
        assert sid is not None
        path = os.path.join(self.reg, sid + ".json")
        self.assertTrue(serve.close_session(sid))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(serve.read_sessions(), [])

    def test_update_webview_changes_url_in_place(self):
        sid = serve.create_webview("https://a.test")
        assert sid is not None
        self.assertTrue(serve.update_webview(sid, url="b.test"))
        with open(os.path.join(self.reg, sid + ".json")) as f:
            entry = json.load(f)
        self.assertEqual(entry["url"], "https://b.test", "update must normalize URL")

    def test_update_webview_changes_name(self):
        sid = serve.create_webview("https://a.test", name="orig")
        assert sid is not None
        self.assertTrue(serve.update_webview(sid, name="renamed"))
        with open(os.path.join(self.reg, sid + ".json")) as f:
            self.assertEqual(json.load(f)["name"], "renamed")

    def test_update_webview_refuses_non_webview(self):
        # update_webview must not corrupt host/container/terminal entries even
        # if a misrouted id sneaks in.
        path = os.path.join(self.reg, "host-1.json")
        with open(path, "w") as f:
            json.dump({"kind": "host", "port": 1}, f)
        self.assertFalse(serve.update_webview("host-1", url="evil.test"))

    def test_update_webview_unknown_id(self):
        self.assertFalse(serve.update_webview("does-not-exist", url="x"))
        self.assertFalse(serve.update_webview("", url="x"))

    def test_duplicate_webview_clones_url(self):
        sid = serve.create_webview("https://orig.test", name="orig")
        self.assertTrue(serve.duplicate_session(sid))
        sids = sorted(s["id"] for s in serve.read_sessions())
        self.assertEqual(sids, ["webview-1", "webview-2"])
        for s in serve.read_sessions():
            self.assertEqual(s["url"], "https://orig.test")

    # --- terminal ---------------------------------------------------------

    # --- proxy ------------------------------------------------------------

    def test_proxy_fetch_rejects_bad_scheme(self):
        for bad in ("", "ftp://x", "javascript:alert(1)", "file:///etc/passwd", None):
            with self.assertRaises(ValueError):
                serve.proxy_fetch(bad)

    def _fake_urlopen(self, ctype, body, final_url=None):
        """Build a context-manager fake matching what urllib.request.urlopen
        returns (read/headers/geturl)."""
        class _Resp:
            def __init__(self): self.headers = {"Content-Type": ctype}
            def read(self, n): return body[:n] if n else body
            def geturl(self): return final_url or "https://upstream.test/page"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return lambda req, timeout=None: _Resp()

    def test_proxy_fetch_injects_base_into_html(self):
        # The base tag is what makes the page's relative URLs resolve against
        # the upstream origin even though we serve it from 127.0.0.1.
        html = b"<!doctype html><html><head><title>x</title></head><body>hi</body></html>"
        orig = serve._no_redirect_opener.open
        serve._no_redirect_opener.open = self._fake_urlopen("text/html", html,
            final_url="https://example.com/dir/page.html")
        try:
            code, body, ctype = serve.proxy_fetch("https://example.com/dir/page.html")
        finally:
            serve._no_redirect_opener.open = orig
        self.assertEqual(code, 200)
        self.assertTrue(ctype.startswith("text/html"))
        s = body.decode()
        self.assertIn('<base href="https://example.com/dir/page.html">', s)
        # Injection happens AFTER <head>, not before, so <title> still parses.
        self.assertLess(s.index('<base'), s.index('<title>'))

    def test_proxy_fetch_blocks_redirects(self):
        # Server-side redirect-following would let a registered URL pivot via
        # 302 → http://internal-admin.lan/secret. We use a custom opener that
        # raises on any 3xx instead of following.
        import urllib.error
        def _fake_open(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 302, "redirect blocked", {}, None)
        orig = serve._no_redirect_opener.open
        serve._no_redirect_opener.open = _fake_open
        try:
            with self.assertRaises(urllib.error.HTTPError):
                serve.proxy_fetch("https://example.com/")
        finally:
            serve._no_redirect_opener.open = orig

    def test_lookup_webview_url_constraints(self):
        # _lookup_webview_url is the SSRF-narrowing primitive: it returns a URL
        # only when the sid maps to an existing webview entry. Anything else
        # (missing, wrong kind, traversal, no-scheme URL) returns None so the
        # HTTP handler refuses with 404.
        self.assertIsNone(serve._lookup_webview_url(""))
        self.assertIsNone(serve._lookup_webview_url("nope"))
        self.assertIsNone(serve._lookup_webview_url("../../etc/passwd"))
        # wrong kind:
        with open(os.path.join(self.reg, "host-1.json"), "w") as f:
            json.dump({"kind": "host", "url": "https://x.test", "port": 1}, f)
        self.assertIsNone(serve._lookup_webview_url("host-1"))
        # malformed URL (no scheme):
        with open(os.path.join(self.reg, "webview-99.json"), "w") as f:
            json.dump({"kind": "webview", "url": "evil"}, f)
        self.assertIsNone(serve._lookup_webview_url("webview-99"))
        # good:
        sid = serve.create_webview("https://x.test")
        assert sid is not None
        self.assertEqual(serve._lookup_webview_url(sid), "https://x.test")

    def test_proxy_fetch_pass_through_non_html(self):
        # Binary / non-HTML content is served verbatim (no base tag inserted).
        png = b"\x89PNG\r\n\x1a\n\x00\x00binarydata"
        orig = serve._no_redirect_opener.open
        serve._no_redirect_opener.open = self._fake_urlopen("image/png", png)
        try:
            _, body, ctype = serve.proxy_fetch("https://x.test/a.png")
        finally:
            serve._no_redirect_opener.open = orig
        self.assertEqual(body, png)
        self.assertEqual(ctype, "image/png")

    def test_create_webview_with_proxy_flag(self):
        sid = serve.create_webview("https://x.test", proxy=True)
        assert sid is not None
        with open(os.path.join(self.reg, sid + ".json")) as f:
            self.assertTrue(json.load(f)["proxy"])
        # And surfaces through read_sessions for the frontend to pick up.
        self.assertTrue(serve.read_sessions()[0]["proxy"])

    def test_update_webview_toggles_proxy(self):
        sid = serve.create_webview("https://x.test", proxy=False)
        assert sid is not None
        self.assertTrue(serve.update_webview(sid, proxy=True))
        with open(os.path.join(self.reg, sid + ".json")) as f:
            self.assertTrue(json.load(f)["proxy"])
        self.assertTrue(serve.update_webview(sid, proxy=False))
        with open(os.path.join(self.reg, sid + ".json")) as f:
            self.assertFalse(json.load(f)["proxy"])

    # --- fork -------------------------------------------------------------

    def test_fork_rejects_non_claude_kinds(self):
        # Terminals and webviews have no conversation to fork.
        sid = serve.create_webview("https://x.test")
        self.assertFalse(serve.fork_session(sid))
        self._write_terminal("terminal-9999", cwd="/tmp")
        self.assertFalse(serve.fork_session("terminal-9999"))
        self.assertFalse(serve.fork_session(""))
        self.assertFalse(serve.fork_session("no-such-id"))

    def test_fork_rejects_missing_cwd(self):
        self._write_host("host-99", port=1, cwd="/no/such/dir/exists/here/")
        self.assertFalse(serve.fork_session("host-99"))

    def test_fork_rejects_when_no_jsonl_exists(self):
        # cwd is valid but contains no .claude/projects/<slug>/ history yet.
        d = tempfile.mkdtemp(prefix="serve-fork-empty-")
        self._write_host("host-99", port=1, cwd=d)
        try:
            self.assertFalse(serve.fork_session("host-99"))
        finally:
            os.rmdir(d)

    def test_newest_jsonl_picks_most_recently_modified(self):
        # _newest_jsonl is the file-system primitive — fork_session uses it to
        # find which conversation to copy. mtime, not lex order, wins.
        d, projects = self._stage_projects_for_host()
        a = os.path.join(projects, "aaaa.jsonl"); open(a, "w").close()
        b = os.path.join(projects, "bbbb.jsonl"); open(b, "w").close()
        os.utime(a, (1, 1))           # older
        os.utime(b, (1_000_000, 1_000_000))  # newer
        self.assertEqual(serve._newest_jsonl(d, container=False), b)

    def test_fork_copies_and_rewrites_session_id(self):
        # The real test: fork must produce a NEW jsonl with the sessionId
        # rewritten to a fresh UUID. We block the subprocess.Popen so the
        # in-process test doesn't actually try to spawn claude-unsafe.
        d, projects = self._stage_projects_for_host()
        old = "11111111-1111-1111-1111-111111111111"
        jsonl = os.path.join(projects, old + ".jsonl")
        with open(jsonl, "w") as f:
            f.write('{"type":"mode","sessionId":"' + old + '"}\n')
            f.write('{"type":"msg","sessionId":"' + old + '","text":"hi"}\n')
        self._write_host("host-99", port=1, cwd=d)
        # Stub Popen — fork_session would otherwise try to launch claude-unsafe.
        calls = []
        orig = serve.subprocess.Popen
        serve.subprocess.Popen = lambda *a, **kw: calls.append((a, kw)) or _FakeProc()
        try:
            self.assertTrue(serve.fork_session("host-99"))
        finally:
            serve.subprocess.Popen = orig
        files = sorted(os.listdir(projects))
        self.assertEqual(len(files), 2, "fork must create a second jsonl")
        new_name = [f for f in files if f != (old + ".jsonl")][0]
        new_uuid = new_name[:-len(".jsonl")]
        with open(os.path.join(projects, new_name)) as f:
            forked = f.read()
        self.assertIn(new_uuid, forked, "every sessionId reference rewritten")
        self.assertNotIn(old, forked, "no leftover references to the old UUID")
        # The spawn carries --resume <new_uuid>.
        self.assertEqual(len(calls), 1)
        args = calls[0][0][0]
        self.assertEqual(args[:2], ["zsh", "-ic"])
        self.assertIn("--resume", args[2])
        self.assertIn(new_uuid, args[2])

    # --- container terminal -----------------------------------------------

    def test_read_sessions_marks_container_terminals(self):
        # A kind=terminal entry with a container field is the "Terminal in
        # container" tile; the API surfaces a container:true flag so the
        # frontend can pick its distinct badge.
        live_port = self._listen_socket()
        with open(os.path.join(self.reg, "terminal-%d.json" % live_port), "w") as f:
            json.dump({"name": "vibe (sh)", "port": live_port, "kind": "terminal",
                       "container": "abc123def", "csock": "/tmp/dtach-cshell-1.sock",
                       "cwd": "/tmp"}, f)
        out = serve.read_sessions()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "terminal")
        self.assertTrue(out[0]["container"],
            "container field must surface as a truthy marker so the badge differentiates")

    def test_read_sessions_plain_terminal_has_no_container_flag(self):
        # Sanity check: a plain host terminal must NOT carry a container flag,
        # or every terminal would render with the wrong badge.
        live_port = self._listen_socket()
        with open(os.path.join(self.reg, "terminal-%d.json" % live_port), "w") as f:
            json.dump({"name": "t", "port": live_port, "kind": "terminal",
                       "cwd": "/tmp"}, f)
        out = serve.read_sessions()
        self.assertNotIn("container", out[0])

    def test_close_container_terminal_kills_via_podman_exec(self):
        # Container terminals share the in-container-kill path with kind=container
        # claude sessions — close_session must podman-exec pkill the csock so
        # the in-container zsh dies, not just the host ttyd.
        dead = self._dead_port()
        sock = os.path.join(self.reg, "host.sock"); open(sock, "w").close()
        path = os.path.join(self.reg, "terminal-99.json")
        with open(path, "w") as f:
            json.dump({"name": "vibe (sh)", "port": dead, "kind": "terminal",
                       "container": "abc123", "csock": "/tmp/dtach-cshell-99.sock",
                       "cwd": "/tmp"}, f)
        calls = []
        orig = serve.subprocess.run
        serve.subprocess.run = lambda *a, **kw: calls.append((a, kw)) or _FakeCompleted()
        try:
            self.assertTrue(serve.close_session("terminal-99"))
        finally:
            serve.subprocess.run = orig
        # One of the recorded calls must be the podman-exec kill targeting
        # this entry's csock.
        cmds = [a[0] for (a, _) in calls if a and isinstance(a[0], list)]
        # close_session now resolves podman via _which() (full path under launchd's
        # minimal PATH), so the argv[0] is e.g. /opt/homebrew/bin/podman — match
        # on basename rather than the literal string.
        podman_kills = [c for c in cmds
                        if c and os.path.basename(c[0]) == "podman"
                        and len(c) > 1 and c[1] == "exec"
                        and "abc123" in c]
        self.assertEqual(len(podman_kills), 1,
            "container terminal must trigger exactly one podman-exec kill")
        self.assertIn("dtach-cshell-99.sock", podman_kills[0][-1])
        self.assertFalse(os.path.exists(path), "registry file is still removed after kill")

    def test_duplicate_container_terminal_routes_to_container_spawn(self):
        # Dup on a container-terminal must invoke spawn_container_terminal,
        # not the plain spawn_terminal — otherwise the clone would land as a
        # host shell instead of in the same container.
        path = os.path.join(self.reg, "terminal-77.json")
        with open(path, "w") as f:
            json.dump({"name": "x", "port": 1, "kind": "terminal",
                       "container": "abc", "csock": "/tmp/y.sock",
                       "cwd": "/tmp"}, f)
        called = {"container": 0, "plain": 0}
        orig_c = serve.spawn_container_terminal
        orig_p = serve.spawn_terminal
        serve.spawn_container_terminal = lambda **kw: (called.__setitem__("container", called["container"] + 1) or "sid-1")
        serve.spawn_terminal = lambda **kw: (called.__setitem__("plain", called["plain"] + 1) or "sid-2")
        try:
            self.assertTrue(serve.duplicate_session("terminal-77"))
        finally:
            serve.spawn_container_terminal = orig_c
            serve.spawn_terminal = orig_p
        self.assertEqual(called["container"], 1)
        self.assertEqual(called["plain"], 0)

    def test_spawn_opencode_rejects_bad_cwd(self):
        # Non-existent cwd must bail without writing a registry. (Empty cwd
        # deliberately falls back to $HOME — same as spawn_terminal.)
        before = set(os.listdir(self.reg))
        self.assertIsNone(serve.spawn_opencode(cwd="/does/not/exist"))
        self.assertEqual(set(os.listdir(self.reg)), before)

    def test_spawn_claude_writes_kind_host(self):
        # spawn_claude self-manages ttyd+dtach+registry (mirrors spawn_opencode)
        # and registers a kind=host tile in THIS store, so it honours
        # --sessions-dir instead of delegating to the claude-unsafe zsh function
        # (which hardcodes ~/.claude-sessions). Verifies the registry shape.
        cwd = tempfile.mkdtemp(prefix="serve-claude-cwd-")
        self._tmpdirs.append(cwd)
        captured = {}
        orig_popen = serve.subprocess.Popen
        orig_release = serve._release_when_listening
        orig_which = serve._which
        def _fake(cmd, **kw):
            captured["cmd"] = list(cmd)
            captured["cwd"] = kw.get("cwd")
            return _FakeProc()
        serve.subprocess.Popen = _fake
        serve._release_when_listening = lambda port, lock: None
        serve._which = lambda name: "/fake/bin/" + name if name in ("claude", "ttyd", "dtach") else orig_which(name)
        try:
            sid = serve.spawn_claude(cwd=cwd, name="hi")
        finally:
            serve.subprocess.Popen = orig_popen
            serve._release_when_listening = orig_release
            serve._which = orig_which
        self.assertIsNotNone(sid)
        self.assertTrue(sid.startswith("host-"))
        self.assertEqual(captured["cwd"], cwd)
        with open(os.path.join(self.reg, sid + ".json")) as f:
            entry = json.load(f)
        self.assertEqual(entry["kind"], "host")
        self.assertEqual(entry["cwd"], cwd)
        self.assertEqual(entry["name"], "hi")
        self.assertIsInstance(entry["port"], int)

    def test_spawn_claude_wraps_claude_in_dtach(self):
        # Like opencode, the claude process is wrapped in `dtach -A <sock>` so it
        # survives the ttyd client tear-down on every browser reload (otherwise
        # the next reload lands in a fresh claude with no conversation). dtach
        # must be the PARENT of claude in argv.
        cwd = tempfile.mkdtemp(prefix="serve-claude-dtach-")
        self._tmpdirs.append(cwd)
        captured = {"argv": None}
        orig_popen = serve.subprocess.Popen
        orig_release = serve._release_when_listening
        orig_which = serve._which
        serve.subprocess.Popen = lambda argv, *a, **kw: captured.update(argv=list(argv)) or _FakeProc()
        serve._release_when_listening = lambda port, lock: None
        serve._which = lambda name: "/fake/bin/" + name if name in ("claude", "ttyd", "dtach") else orig_which(name)
        try:
            sid = serve.spawn_claude(cwd=cwd)
        finally:
            serve.subprocess.Popen = orig_popen
            serve._release_when_listening = orig_release
            serve._which = orig_which
        self.assertIsNotNone(sid)
        argv = captured["argv"]
        self.assertIsNotNone(argv, "Popen was never called")
        self.assertIn("/fake/bin/dtach", argv,
            "spawn_claude must invoke dtach (else reload kills claude)")
        self.assertIn("-A", argv)
        self.assertIn("-r", argv); self.assertIn("winch", argv)
        # A bare spawn_claude() is now the plain "claude" launcher — no
        # --dangerously-skip-permissions unless the launcher command includes it
        # (passed via extra=...). Skip-perms is per-launcher, not always-on.
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertLess(argv.index("/fake/bin/dtach"), argv.index("/fake/bin/claude"),
            "dtach must wrap claude, not the other way around")

    def test_spawn_claude_extra_flags_pass_through(self):
        # A launcher command like `claude --dangerously-skip-permissions --model
        # haiku` reaches spawn_claude as extra=[...]; those flags land in argv and
        # the raw command is recorded for duplicate/revive.
        cwd = tempfile.mkdtemp(prefix="serve-claude-extra-")
        self._tmpdirs.append(cwd)
        captured = {"argv": None}
        orig_popen = serve.subprocess.Popen
        orig_release = serve._release_when_listening
        orig_which = serve._which
        serve.subprocess.Popen = lambda argv, *a, **kw: captured.update(argv=list(argv)) or _FakeProc()
        serve._release_when_listening = lambda port, lock: None
        serve._which = lambda name: "/fake/bin/" + name if name in ("claude", "ttyd", "dtach") else orig_which(name)
        try:
            sid = serve.spawn_claude(cwd=cwd,
                extra=["--dangerously-skip-permissions", "--model", "haiku"],
                command="claude --dangerously-skip-permissions --model haiku")
        finally:
            serve.subprocess.Popen = orig_popen
            serve._release_when_listening = orig_release
            serve._which = orig_which
        argv = captured["argv"]
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--model", argv); self.assertIn("haiku", argv)
        self.assertIn("--session-id", argv)   # still pinned for the fork button
        entry = json.loads(open(os.path.join(serve.REGISTRY, sid + ".json")).read())
        self.assertEqual(entry["command"],
                         "claude --dangerously-skip-permissions --model haiku")

    def test_spawn_launcher_routes_and_custom_kind(self):
        # spawn_launcher routes by program name; a non-agent command becomes a
        # generic kind=custom tile that records its command for revive.
        cwd = tempfile.mkdtemp(prefix="serve-launcher-route-")
        self._tmpdirs.append(cwd)
        captured = {"argv": None}
        orig_popen = serve.subprocess.Popen
        orig_release = serve._release_when_listening
        orig_which = serve._which
        serve.subprocess.Popen = lambda argv, *a, **kw: captured.update(argv=list(argv)) or _FakeProc()
        serve._release_when_listening = lambda port, lock: None
        # ttyd/dtach are faked; the custom program ("echo") resolves to a REAL
        # path so spawn_command's "program must exist" guard is satisfied.
        serve._which = lambda name: "/fake/bin/" + name if name in ("ttyd", "dtach") else orig_which(name)
        try:
            sid = serve.spawn_launcher({"command": "echo hi", "label": "echotile"}, cwd=cwd)
        finally:
            serve.subprocess.Popen = orig_popen
            serve._release_when_listening = orig_release
            serve._which = orig_which
        self.assertIsNotNone(sid)
        self.assertTrue(sid.startswith("custom-"))
        entry = json.loads(open(os.path.join(serve.REGISTRY, sid + ".json")).read())
        self.assertEqual(entry["kind"], "custom")
        self.assertEqual(entry["command"], "echo hi")

    def test_spawn_claude_vertex_injects_env(self):
        # provider="vertex" must overlay the Vertex switches (resolved from the
        # config FILE via vertex_config()) onto the child env, including the
        # optional `env` extra dict, while the default-provider path inherits the
        # parent env (env=None). It also records provider in the registry entry.
        cwd = tempfile.mkdtemp(prefix="serve-claude-vertex-")
        self._tmpdirs.append(cwd)
        cfgfile = os.path.join(cwd, "vertex.json")
        with open(cfgfile, "w") as f:
            json.dump({"project_id": "proj-xyz", "region": "eu",
                       "model": "claude-opus-4-8[1m]",
                       "env": {"ENABLE_PROMPT_CACHING_1H": "1"}}, f)
        captured = {"env": "unset"}
        orig_popen = serve.subprocess.Popen
        orig_release = serve._release_when_listening
        orig_which = serve._which
        orig_cfgpath = serve.VERTEX_CONFIG_PATH
        # Isolate from any real env overrides so the file values are authoritative.
        saved_env = {k: os.environ.pop(k, None) for k in
                     ("ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "ANTHROPIC_MODEL")}
        serve.subprocess.Popen = lambda argv, *a, **kw: captured.update(env=kw.get("env")) or _FakeProc()
        serve._release_when_listening = lambda port, lock: None
        serve._which = lambda name: "/fake/bin/" + name if name in ("claude", "ttyd", "dtach") else orig_which(name)
        serve.VERTEX_CONFIG_PATH = cfgfile
        try:
            # Default provider: env inherited (None), no provider key in entry.
            sid_def = serve.spawn_claude(cwd=cwd)
            self.assertIsNotNone(sid_def)
            self.assertIsNone(captured["env"], "non-vertex tile must inherit parent env")
            with open(os.path.join(serve.REGISTRY, sid_def + ".json")) as f:
                self.assertNotIn("provider", json.load(f))
            # Vertex provider: env overlaid from the config file.
            sid_vtx = serve.spawn_claude(cwd=cwd, provider="vertex")
        finally:
            serve.subprocess.Popen = orig_popen
            serve._release_when_listening = orig_release
            serve._which = orig_which
            serve.VERTEX_CONFIG_PATH = orig_cfgpath
            for k, v in saved_env.items():
                if v is not None:
                    os.environ[k] = v
        self.assertIsNotNone(sid_vtx)
        env = captured["env"]
        self.assertIsInstance(env, dict, "vertex tile must get an explicit env dict")
        self.assertEqual(env.get("CLAUDE_CODE_USE_VERTEX"), "1")
        self.assertEqual(env.get("CLOUD_ML_REGION"), "eu")
        self.assertEqual(env.get("ANTHROPIC_VERTEX_PROJECT_ID"), "proj-xyz")
        self.assertEqual(env.get("ANTHROPIC_MODEL"), "claude-opus-4-8[1m]")
        self.assertEqual(env.get("ENABLE_PROMPT_CACHING_1H"), "1", "config env dict not merged")
        # PATH (a representative inherited var) must still be present — the overlay
        # copies os.environ, it doesn't replace it.
        self.assertIn("PATH", env)
        with open(os.path.join(serve.REGISTRY, sid_vtx + ".json")) as f:
            self.assertEqual(json.load(f).get("provider"), "vertex")

    def test_vertex_config_precedence_env_over_file_over_default(self):
        # vertex_config(): env var > file > built-in default, and a missing file
        # falls back cleanly. Locks the resolution contract the file feature rests
        # on so a future change can't silently ignore the file or the override.
        d = tempfile.mkdtemp(prefix="serve-vcfg-")
        self._tmpdirs.append(d)
        cfgfile = os.path.join(d, "vertex.json")
        orig_cfgpath = serve.VERTEX_CONFIG_PATH
        try:
            # (a) missing file -> built-in defaults
            serve.VERTEX_CONFIG_PATH = os.path.join(d, "does-not-exist.json")
            with mock.patch.dict(os.environ, {}, clear=False):
                for k in ("ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "ANTHROPIC_MODEL"):
                    os.environ.pop(k, None)
                cfg = serve.vertex_config()
                self.assertEqual(cfg["region"], serve.VERTEX_DEFAULTS["region"])
                self.assertEqual(cfg["project_id"], serve.VERTEX_DEFAULTS["project_id"])
            # (b) file overrides default
            with open(cfgfile, "w") as f:
                json.dump({"project_id": "from-file", "region": "us-east5"}, f)
            serve.VERTEX_CONFIG_PATH = cfgfile
            with mock.patch.dict(os.environ, {}, clear=False):
                for k in ("ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "ANTHROPIC_MODEL"):
                    os.environ.pop(k, None)
                cfg = serve.vertex_config()
                self.assertEqual(cfg["project_id"], "from-file")
                self.assertEqual(cfg["region"], "us-east5")
                # model not in file -> still the default
                self.assertEqual(cfg["model"], serve.VERTEX_DEFAULTS["model"])
            # (c) env var overrides file
            with mock.patch.dict(os.environ, {"CLOUD_ML_REGION": "eu"}, clear=False):
                cfg = serve.vertex_config()
                self.assertEqual(cfg["region"], "eu")
                self.assertEqual(cfg["project_id"], "from-file")
            # (d) malformed file -> ignored, defaults returned
            with open(cfgfile, "w") as f:
                f.write("{not valid json")
            with mock.patch.dict(os.environ, {}, clear=False):
                for k in ("ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "ANTHROPIC_MODEL"):
                    os.environ.pop(k, None)
                cfg = serve.vertex_config()
                self.assertEqual(cfg["region"], serve.VERTEX_DEFAULTS["region"])
        finally:
            serve.VERTEX_CONFIG_PATH = orig_cfgpath

    def test_spawn_claude_bails_when_binary_missing(self):
        # No `claude` on PATH → return None and write no registry entry, so the
        # menu item fails cleanly instead of spawning a broken ttyd.
        cwd = tempfile.mkdtemp(prefix="serve-claude-nobin-")
        self._tmpdirs.append(cwd)
        orig_which = serve._which
        serve._which = lambda name: None if name == "claude" else orig_which(name)
        try:
            self.assertIsNone(serve.spawn_claude(cwd=cwd))
        finally:
            serve._which = orig_which
        self.assertEqual([f for f in os.listdir(self.reg) if f.startswith("host-")], [])

    def test_spawn_claude_returns_none_on_bad_cwd(self):
        # A non-existent cwd returns None (unlike the old claude-unsafe path that
        # fell back to $HOME): the new self-managed launcher needs a real cwd to
        # bind-mount nothing but to anchor the session + its .mcp.json discovery.
        self.assertIsNone(serve.spawn_claude(cwd="/does/not/exist"))

    def test_extra_path_covers_user_local_bin(self):
        # Regression guard (contract): `claude` installs to ~/.local/bin, which is
        # NOT on launchd's minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that the
        # dashboard runs under. If ~/.local/bin drops out of _EXTRA_PATH, _which
        # can't find `claude` and every + New -> Claude spawn 500s (the
        # "+ New -> Claude does nothing" bug). Keep it covered.
        self.assertIn(os.path.expanduser("~/.local/bin"), serve._EXTRA_PATH)

    def test_spawn_binaries_resolve_under_launchd_minimal_path(self):
        # Regression guard (behavioural): the other spawn_* tests MOCK _which, so
        # they never exercise real binary resolution — which is exactly how the
        # "claude lives in ~/.local/bin, not in _EXTRA_PATH" bug slipped through.
        # This test uses the REAL _which under the production launchd-minimal PATH
        # and asserts every binary the dashboard spawns (and that is actually
        # installed on this machine) still resolves. If a binary's install dir
        # isn't covered by _EXTRA_PATH, this fails instead of shipping a dashboard
        # whose spawn buttons silently 500.
        LAUNCHD_MINIMAL = "/usr/bin:/bin:/usr/sbin:/sbin"
        binaries = ("claude", "ttyd", "dtach", "opencode")
        # Resolve real install locations using the test runner's full PATH first.
        real = {b: serve._which(b) for b in binaries}
        orig = os.environ.get("PATH")
        try:
            os.environ["PATH"] = LAUNCHD_MINIMAL
            checked = []
            for b in binaries:
                if not real[b]:
                    continue  # not installed on this machine — nothing to assert
                checked.append(b)
                self.assertIsNotNone(
                    serve._which(b),
                    "_which(%r) is None under the launchd-minimal PATH; its install "
                    "dir %r is not covered by serve._EXTRA_PATH, so dashboard spawns "
                    "of this binary will 500." % (b, os.path.dirname(real[b])))
            if not checked:
                self.skipTest("none of %s installed on this machine" % (binaries,))
        finally:
            if orig is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = orig

    def test_spawn_opencode_bails_when_binary_missing(self):
        # Force _which to fail for opencode regardless of host install state.
        orig = serve._which
        cwd = tempfile.mkdtemp(prefix="serve-oc-cwd-")
        self._tmpdirs.append(cwd)
        serve._which = lambda name: None if name == "opencode" else orig(name)
        try:
            self.assertIsNone(serve.spawn_opencode(cwd=cwd))
        finally:
            serve._which = orig
        # No registry file should be written when the binary isn't there.
        self.assertEqual([f for f in os.listdir(self.reg) if f.startswith("opencode-")], [])

    def test_spawn_opencode_writes_kind_opencode(self):
        # Make subprocess.Popen a no-op so we don't actually spawn ttyd, and
        # pretend opencode + ttyd are installed (anywhere — the wrapper only
        # cares about existence). Verifies the registry entry's shape.
        cwd = tempfile.mkdtemp(prefix="serve-oc-cwd-")
        self._tmpdirs.append(cwd)
        orig_popen = serve.subprocess.Popen
        orig_release = serve._release_when_listening
        serve.subprocess.Popen = lambda *a, **kw: _FakeProc()
        serve._release_when_listening = lambda port, lock: None
        orig_which = serve._which
        serve._which = lambda name: "/fake/bin/" + name if name in ("opencode", "ttyd") else orig_which(name)
        try:
            sid = serve.spawn_opencode(cwd=cwd, name="hi")
        finally:
            serve.subprocess.Popen = orig_popen
            serve._release_when_listening = orig_release
            serve._which = orig_which
        self.assertIsNotNone(sid)
        self.assertTrue(sid.startswith("opencode-"))
        with open(os.path.join(self.reg, sid + ".json")) as f:
            entry = json.load(f)
        self.assertEqual(entry["kind"], "opencode")
        self.assertEqual(entry["cwd"], cwd)
        self.assertEqual(entry["name"], "hi")
        self.assertIsInstance(entry["port"], int)

    def test_spawn_opencode_wraps_program_in_dtach(self):
        # User-reported "opencode sessions do not restore on reload":
        # without dtach, ttyd spawns a per-connection opencode and SIGHUPs
        # it when the websocket disconnects (every browser reload). Result:
        # next reload lands in a fresh opencode with no chat history. The
        # fix wraps opencode in `dtach -A <sock>` so the program persists
        # across ttyd reconnects — same pattern the opencode-unsafe -web
        # zsh launcher already uses.
        cwd = tempfile.mkdtemp(prefix="serve-oc-dtach-")
        self._tmpdirs.append(cwd)
        captured = {"argv": None}
        def _fake_popen(argv, *a, **kw):
            captured["argv"] = list(argv)
            return _FakeProc()
        orig_popen = serve.subprocess.Popen
        orig_release = serve._release_when_listening
        orig_which = serve._which
        serve.subprocess.Popen = _fake_popen
        serve._release_when_listening = lambda port, lock: None
        # Pretend dtach is installed alongside ttyd + opencode.
        serve._which = lambda name: "/fake/bin/" + name if name in ("opencode", "ttyd", "dtach") else orig_which(name)
        try:
            sid = serve.spawn_opencode(cwd=cwd)
        finally:
            serve.subprocess.Popen = orig_popen
            serve._release_when_listening = orig_release
            serve._which = orig_which
        self.assertIsNotNone(sid)
        argv = captured["argv"]
        self.assertIsNotNone(argv, "Popen was never called")
        assert argv is not None
        # `dtach -A <sock> -r winch opencode <cwd>` must appear after ttyd's flags.
        self.assertIn("/fake/bin/dtach", argv,
            "spawn_opencode must invoke dtach (else reload kills opencode)")
        self.assertIn("-A", argv,
            "must use `dtach -A` (auto-create / attach) so reconnects re-attach")
        self.assertIn("-r", argv); self.assertIn("winch", argv)
        # The opencode binary must come AFTER dtach in argv (i.e. dtach is the
        # parent that wraps opencode, not the other way around).
        self.assertLess(argv.index("/fake/bin/dtach"), argv.index("/fake/bin/opencode"),
            "dtach must wrap opencode (dtach first, then opencode)")
        # Registry entry must carry the sock so close_session can lsof+kill it.
        with open(os.path.join(self.reg, sid + ".json")) as f:
            entry = json.load(f)
        self.assertIn("sock", entry,
            "registry must carry the dtach sock path so close_session can clean up")
        self.assertTrue(entry["sock"].endswith(".sock"))

    def test_spawn_opencode_falls_back_when_dtach_missing(self):
        # If dtach isn't installed (uncommon — `brew install dtach` is in
        # install.sh) we must still spawn opencode rather than refusing.
        # The fallback loses reload-survival but keeps the feature usable.
        cwd = tempfile.mkdtemp(prefix="serve-oc-fallback-")
        self._tmpdirs.append(cwd)
        captured = {"argv": None}
        def _fake_popen(argv, *a, **kw):
            captured["argv"] = list(argv)
            return _FakeProc()
        orig_popen = serve.subprocess.Popen
        orig_release = serve._release_when_listening
        orig_which = serve._which
        serve.subprocess.Popen = _fake_popen
        serve._release_when_listening = lambda port, lock: None
        # dtach intentionally returns None — simulates missing-from-PATH.
        serve._which = lambda name: None if name == "dtach" else (
            "/fake/bin/" + name if name in ("opencode", "ttyd") else orig_which(name))
        try:
            sid = serve.spawn_opencode(cwd=cwd)
        finally:
            serve.subprocess.Popen = orig_popen
            serve._release_when_listening = orig_release
            serve._which = orig_which
        self.assertIsNotNone(sid, "spawn must succeed even when dtach is missing")
        argv = captured["argv"]
        assert argv is not None
        # Look for the dtach BINARY (whole argv element or basename), not the
        # substring — paths can incidentally contain "dtach" (e.g. a tempdir
        # whose name has it). Match exact basename to be safe.
        dtachy = [a for a in argv if os.path.basename(a) == "dtach"]
        self.assertEqual(dtachy, [],
            "fallback must not invoke dtach when _which returned None")
        # And the registry entry must NOT carry a sock (close_session would
        # otherwise try to remove / lsof a path that was never created).
        with open(os.path.join(self.reg, sid + ".json")) as f:
            entry = json.load(f)
        self.assertNotIn("sock", entry,
            "fallback entry must omit sock — no dtach socket was created")

    def test_duplicate_routes_opencode_to_spawn_opencode(self):
        # An existing kind=opencode entry → duplicate must call spawn_opencode,
        # not spawn_terminal (which would land as a zsh).
        port = self._listen_socket()
        cwd = tempfile.mkdtemp(prefix="serve-oc-dup-")
        self._tmpdirs.append(cwd)
        with open(os.path.join(self.reg, "opencode-%d.json" % port), "w") as f:
            json.dump({"name": "x", "port": port, "kind": "opencode",
                       "cwd": cwd, "started": "2026-05-30T00:00:00Z"}, f)
        called = {"opencode": 0, "terminal": 0}
        orig_oc = serve.spawn_opencode
        orig_t = serve.spawn_terminal
        serve.spawn_opencode = lambda **kw: called.update(opencode=called["opencode"] + 1) or "opencode-1"
        serve.spawn_terminal = lambda **kw: called.update(terminal=called["terminal"] + 1) or "terminal-1"
        try:
            self.assertTrue(serve.duplicate_session("opencode-%d" % port))
        finally:
            serve.spawn_opencode = orig_oc
            serve.spawn_terminal = orig_t
        self.assertEqual(called["opencode"], 1)
        self.assertEqual(called["terminal"], 0)

    def test_terminal_close_lifecycle(self):
        # Simulate a registered terminal entry (no actual ttyd) and verify
        # close_session removes it. Port is dead → close_session takes the
        # port-based kill path (no-op) then drops the file.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()
        path = os.path.join(self.reg, "terminal-%d.json" % dead_port)
        with open(path, "w") as f:
            json.dump({"name": "tmp", "port": dead_port, "kind": "terminal",
                       "cwd": "/tmp", "started": "2026-05-30T00:00:00Z"}, f)
        self.assertTrue(serve.close_session("terminal-%d" % dead_port))
        self.assertFalse(os.path.exists(path))


class StashTest(unittest.TestCase):
    """Coverage for the stash/unstash UI-state flag: the registry write
    persists `stashed`, read_sessions surfaces it, the backing process is
    untouched, and the /api/stash route is CSRF-gated like the other
    state-changing endpoints."""

    def setUp(self):
        self.reg = tempfile.mkdtemp(prefix="serve-stash-")
        self._saved_reg = serve.REGISTRY
        serve.REGISTRY = self.reg
        self._listeners = []

    def tearDown(self):
        serve.REGISTRY = self._saved_reg
        for s in self._listeners:
            try:
                s.close()
            except OSError:
                pass
        for fn in os.listdir(self.reg):
            try:
                os.remove(os.path.join(self.reg, fn))
            except OSError:
                pass
        try:
            os.rmdir(self.reg)
        except OSError:
            pass

    def _live_port(self):
        """Open a real listening socket so port_alive reads True for the test."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        self._listeners.append(s)
        return s.getsockname()[1]

    def _write_host(self, sid, port, **extra):
        e = {"name": sid, "port": port, "kind": "host",
             "cwd": "/tmp/x", "started": "2026-05-30T00:00:00Z"}
        e.update(extra)
        with open(os.path.join(self.reg, sid + ".json"), "w") as f:
            json.dump(e, f)
        return os.path.join(self.reg, sid + ".json")

    def _read_entry(self, sid):
        with open(os.path.join(self.reg, sid + ".json")) as f:
            return json.load(f)

    def test_stash_sets_flag_on_disk(self):
        path = self._write_host("host-1", self._live_port())
        self.assertTrue(serve.stash_session("host-1", True))
        self.assertTrue(self._read_entry("host-1").get("stashed"))
        # The file must still be there — stash is NOT a delete.
        self.assertTrue(os.path.exists(path))

    def test_unstash_clears_flag_on_disk(self):
        self._write_host("host-1", self._live_port(), stashed=True)
        self.assertTrue(serve.stash_session("host-1", False))
        # Absent rather than False, so legacy readers and the on-wire shape
        # stay clean. (read_sessions normalizes to bool either way.)
        self.assertNotIn("stashed", self._read_entry("host-1"))

    def test_stash_is_idempotent(self):
        self._write_host("host-1", self._live_port())
        self.assertTrue(serve.stash_session("host-1", True))
        self.assertTrue(serve.stash_session("host-1", True))
        self.assertTrue(self._read_entry("host-1")["stashed"])

    def test_stash_preserves_other_fields(self):
        port = self._live_port()
        self._write_host("host-1", port, cwd="/some/dir", name="hello",
                         sock="/tmp/x.sock", started="2026-05-30T01:02:03Z")
        serve.stash_session("host-1", True)
        e = self._read_entry("host-1")
        self.assertEqual(e["port"], port)
        self.assertEqual(e["cwd"], "/some/dir")
        self.assertEqual(e["name"], "hello")
        self.assertEqual(e["sock"], "/tmp/x.sock")
        self.assertEqual(e["started"], "2026-05-30T01:02:03Z")
        self.assertEqual(e["kind"], "host")

    def test_stash_unknown_id_returns_false(self):
        self.assertFalse(serve.stash_session("not-a-real-id", True))

    def test_stash_empty_id_returns_false(self):
        self.assertFalse(serve.stash_session("", True))
        self.assertFalse(serve.stash_session(None, True))

    def test_stash_path_traversal_guarded(self):
        # basename() strips the traversal — the stash either no-ops on the
        # bare filename (which doesn't exist) or, in the worst case, can't
        # escape REGISTRY. Mostly we just want it to never touch ../etc.
        self.assertFalse(serve.stash_session("../etc/passwd", True))
        self.assertFalse(serve.stash_session("../../host-1", True))

    def test_read_sessions_surfaces_stashed_true(self):
        self._write_host("host-1", self._live_port(), stashed=True)
        entries = [s for s in serve.read_sessions() if s["id"] == "host-1"]
        self.assertEqual(len(entries), 1)
        self.assertIs(entries[0]["stashed"], True)

    def test_read_sessions_surfaces_stashed_false_default(self):
        self._write_host("host-1", self._live_port())
        entries = [s for s in serve.read_sessions() if s["id"] == "host-1"]
        self.assertEqual(len(entries), 1)
        # Bool, not missing — frontend uses `s.stashed` directly without
        # checking `in s`.
        self.assertIs(entries[0]["stashed"], False)

    def test_read_sessions_webview_surfaces_stashed_field(self):
        # Webviews take the no-port branch in read_sessions; make sure the
        # flag propagates there too.
        with open(os.path.join(self.reg, "webview-1.json"), "w") as f:
            json.dump({"kind": "webview", "url": "https://example.com",
                       "stashed": True}, f)
        entries = [s for s in serve.read_sessions() if s["id"] == "webview-1"]
        self.assertEqual(len(entries), 1)
        self.assertIs(entries[0]["stashed"], True)

    def test_stash_works_on_webview(self):
        with open(os.path.join(self.reg, "webview-1.json"), "w") as f:
            json.dump({"kind": "webview", "url": "https://example.com"}, f)
        self.assertTrue(serve.stash_session("webview-1", True))
        self.assertTrue(self._read_entry("webview-1")["stashed"])
        serve.stash_session("webview-1", False)
        self.assertNotIn("stashed", self._read_entry("webview-1"))

    def test_stash_does_not_kill_process_or_drop_socket(self):
        # The whole point of stash vs close: backing process stays alive.
        # We don't spawn a real ttyd, but we can prove stash never goes near
        # subprocess/os.kill by patching them to fail loudly.
        import subprocess as _sp
        port = self._live_port()
        sock_path = os.path.join(self.reg, "fake-sock.sock")
        with open(sock_path, "w") as f:
            f.write("")
        self._write_host("host-1", port, sock=sock_path)
        orig_run, orig_kill = _sp.run, os.kill
        def boom(*a, **kw): raise AssertionError("stash must not subprocess.run")
        def killboom(*a, **kw): raise AssertionError("stash must not os.kill")
        _sp.run = boom
        os.kill = killboom
        try:
            self.assertTrue(serve.stash_session("host-1", True))
        finally:
            _sp.run = orig_run
            os.kill = orig_kill
        # Socket file and registry file both untouched.
        self.assertTrue(os.path.exists(sock_path))
        self.assertTrue(os.path.exists(os.path.join(self.reg, "host-1.json")))

    def test_close_still_works_on_stashed_session(self):
        # Stashing must not block subsequent close (e.g. closing from the
        # stash drawer's ✕ button). MUST use a dead port: a live one in the
        # test process resolves back to our own pid via lsof and close_session
        # would os.kill the test runner.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()
        self._write_host("host-1", dead_port, stashed=True)
        self.assertTrue(serve.close_session("host-1"))
        self.assertFalse(os.path.exists(os.path.join(self.reg, "host-1.json")))


class TermClientScrollbackTest(unittest.TestCase):
    """Static checks against the built term.html (and its term-client.js
    source) to prevent the restore↔persist feedback loop that was duplicating
    content and eating the MAX_LINES budget.

    Background: an earlier build wrote a literal `─── restored ───` marker +
    24 blank rows into the buffer on every restore. setInterval(persist)
    captured that, restoreScrollback re-wrote it next reload, and the cycle
    accumulated — markers stacked up, real history fell out of the top.
    """

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        # term-client.js is the source; term.html is the inlined production build.
        # Both must satisfy the contract so a rebuild can't silently regress.
        with open(os.path.join(here, "term-client.js")) as f:
            cls.js = f.read()
        with open(os.path.join(here, "term.html")) as f:
            cls.html = f.read()

    def test_restore_does_not_write_marker_into_buffer(self):
        # The function body between `function restoreScrollback` and its
        # closing brace must not contain `term.write('...─── restored ───...')`.
        # We slice the source to the function body so the regex check in the
        # SAME function (RESTORE_MARKER_RE — which legitimately mentions the
        # marker string) doesn't trigger a false positive.
        for label, src in (("js", self.js), ("html", self.html)):
            m = re.search(r"function restoreScrollback\s*\(\s*\)\s*\{", src)
            self.assertIsNotNone(m, "%s: restoreScrollback definition not found" % label)
            start = m.end()
            # Find the matching close brace by walking the source.
            depth, end = 1, len(src)
            for i in range(start, len(src)):
                c = src[i]
                if c == '{': depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0: end = i; break
            body = src[start:end]
            # Look for any `term.write(...marker...)` call.
            self.assertNotRegex(body, r"term\.write\([^)]*─── restored ───",
                "%s: restoreScrollback must not write the restore marker into "
                "the buffer (it gets persisted + re-restored, accumulating "
                "duplicates over reloads)" % label)

    def test_repaint_is_consistent_one_row_wiggle_not_iframe_bounce(self):
        # The blank-on-reattach repaint trigger lives in the client as a ONE
        # ROW wiggle scheduled after each socket open, with two hard
        # constraints (each independently regressed in the field):
        #  1. grid and pty resize TOGETHER via term.resize (a pty-only rows-1
        #     report left Ink erasing against a screen one row shorter than
        #     the real grid — duplicated in-flight output);
        #  2. it fires only once output has gone QUIET, never while a program is
        #     still streaming (a forced repaint mid-frame is the duplication
        #     hazard) — but it must NOT skip on the mere presence of the dtach
        #     attach-replay burst, or an idle reattach comes up blank until the
        #     user types (the regression an "any output since open" boolean
        #     caused). It waits for a silence window, then wiggles.
        # The dashboard's old iframe-height bounce (-48px and back, twice, on
        # load) made tiles visibly jump for ~1s — it must not come back.
        for label, src in (("js", self.js), ("html", self.html)):
            self.assertRegex(src, r"function _repaintWiggle",
                             "%s: _repaintWiggle missing" % label)
            self.assertRegex(src, r"term\.resize\(c,\s*r\s*-\s*1\)",
                             "%s: wiggle must resize grid+pty together "
                             "(term.resize), one row" % label)
            # Gated on output going QUIET (a time window since the last output),
            # not on a boolean tripped by any output since open.
            self.assertRegex(src, r"Date\.now\(\)\s*-\s*_lastOutputTs\)\s*<\s*WIGGLE_QUIET_MS",
                             "%s: wiggle must stand down only while output is "
                             "still flowing (quiet-window gate), not on any "
                             "replay burst" % label)
            self.assertNotRegex(src, r"_outputSinceOpen",
                                "%s: the 'any output since open' boolean gate "
                                "skipped the wiggle on the idle attach-replay "
                                "burst (blank-on-reattach) — must be gone" % label)
            # 3. it ONLY wiggles a genuinely blank viewport — a reattach that
            #    already replayed content must not be size-changed, or claude's
            #    bottom-anchored UI redraws across the row delta and the old
            #    copy is orphaned ("stacked input boxes / doubled status line").
            self.assertRegex(src, r"if \(!_viewportLooksBlank\(\)\) return;",
                             "%s: wiggle must stand down when content is already "
                             "on screen (bottom-UI duplication hazard)" % label)
            self.assertRegex(src, r"setTimeout\(_repaintWiggle",
                             "%s: wiggle must be scheduled after socket open" % label)
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "serve.py")) as f:
            dash = f.read()
        self.assertNotRegex(dash, r"clientHeight\s*-\s*48",
                            "dashboard must not bounce the iframe height to "
                            "force repaints (visible tile jumping)")
        self.assertIn("cmd: 'repaint'", dash,
                      "chat toggle should request the client's repaint wiggle")

    def test_terminal_forwards_first_user_gesture_to_release_scroll_pin(self):
        # The dashboard pins the tile row to its leftmost on reload and releases
        # the pin on the first genuine user gesture. Gestures over a terminal
        # are inside a cross-origin iframe and never reach the dashboard window,
        # so the client must FORWARD the first pointer/touch/key interaction —
        # else the row stays "locked to the left" until the user happens to
        # swipe horizontally (the only gesture already forwarded). Regression
        # for "after reload H-scroll locked to the left, weird glitches".
        for label, src in (("js", self.js), ("html", self.html)):
            self.assertRegex(src, r"post\(\{ *key: *'user-gesture' *\}\)",
                             "%s: client must forward a 'user-gesture' signal" % label)
            for ev in ("pointerdown", "touchstart", "keydown"):
                self.assertRegex(
                    src, r"addEventListener\('%s', *_signalUserGesture" % ev,
                    "%s: client must signal user-gesture on %s" % (label, ev))
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "serve.py")) as f:
            dash = f.read()
        self.assertRegex(dash, r"if \(d\.key === 'user-gesture'\) releasePin\(\);",
                         "dashboard must releasePin() on a forwarded user-gesture")

    def test_hidden_connect_uses_saved_size_not_default(self):
        # Regression for "log buffer garbage": a hidden (never-fitted) tile
        # connects with xterm's 80x24 DEFAULT in the ttyd handshake; the
        # spawned dtach client then resizes the SHARED session pty to 80x24,
        # and a busy claude repaints every real-size view of that session into
        # wrapped status-line fragments — which the sized view then persists,
        # baking the garbage into the snapshot. The hidden-connect path
        # (go(false)) must pre-size the terminal from the persisted size key.
        for label, src in (("js", self.js), ("html", self.html)):
            self.assertIn("claude-term-size:", src,
                          "%s: size persistence key missing" % label)
            self.assertRegex(src, r"else _applySavedSize\(\)",
                             "%s: hidden connect must apply the saved size "
                             "instead of attaching at xterm's default" % label)
            self.assertRegex(
                src, r"localStorage\.setItem\(LSSIZE,\s*term\.cols\s*\+\s*'x'\s*\+\s*term\.rows\)",
                "%s: persist must record the snapshot's render size" % label)
            self.assertRegex(src, r"k === LSSIZE",
                             "%s: _sweepOldKeys must keep the current "
                             "session's size key" % label)

    def test_restore_resets_sgr_after_writing_saved_blob(self):
        # Regression for "all text now underlined" after a reload: if
        # persist() snapshotted while claude had an attribute open (e.g.
        # underline on for the input prompt line, no matching closer in
        # the captured bytes), restoring that blob leaves the renderer's
        # current-attribute state on — and every byte ttyd's reattach
        # replay writes inherits it. Fix: append `\x1b[0m` (SGR reset) to
        # the restored payload. Cheap, paints nothing, only flips the
        # renderer's attribute state back to default.
        for label, src in (("js", self.js), ("html", self.html)):
            m = re.search(r"function _applyRestored\s*\(\s*\w+\s*\)\s*\{", src)
            self.assertIsNotNone(m, "%s: _applyRestored not found" % label)
            assert m is not None
            start = m.end(); depth, end = 1, len(src)
            for i in range(start, len(src)):
                c = src[i]
                if c == '{': depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0: end = i; break
            body = src[start:end]
            # Every term.write of the restored blob must append \x1b[0m so
            # leftover SGR state can't leak into live output. The blob is now
            # written with a completion callback — `term.write(blob + '\x1b[0m',
            # res)` (image-splice path + text-only fallback) — so allow a
            # trailing arg between the reset and the closing paren.
            self.assertRegex(body, r"term\.write\([^)]*\\x1b\[0m['\"]?[^)]*\)",
                "%s: _applyRestored must append \\x1b[0m to the saved blob — "
                "otherwise a mid-attribute snapshot leaks styling into "
                "everything written after the restore" % label)

    def test_restore_does_not_push_blank_rows_into_buffer(self):
        # Same scoping trick. The old build wrote `(rows+1) newlines` to push
        # the restored screen up — those newlines got persisted and stacked
        # too. Detect any `new Array(... rows ...).join('\n')` in the body.
        for label, src in (("js", self.js), ("html", self.html)):
            m = re.search(r"function restoreScrollback\s*\(\s*\)\s*\{", src)
            self.assertIsNotNone(m)
            start = m.end()
            depth, end = 1, len(src)
            for i in range(start, len(src)):
                c = src[i]
                if c == '{': depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0: end = i; break
            body = src[start:end]
            self.assertNotIn("term.rows", body,
                "%s: restoreScrollback must not write blank rows derived from "
                "term.rows — those land in the persisted buffer and accumulate "
                "across reloads" % label)
            self.assertNotRegex(body, r"new\s+Array\([^)]*\)\.join\(['\"]\\n['\"]\)",
                "%s: restoreScrollback must not synthesize blank-row strings" % label)

    def test_restore_marker_regex_strips_legacy_blobs(self):
        # The fix keeps a RESTORE_MARKER_RE in the script: on each load we
        # strip any legacy marker out of whatever's already in localStorage,
        # so existing (pre-fix) saved blobs self-clean on first reload.
        # The check is structural — the regex source must contain the marker
        # text — without trying to actually execute JS from Python.
        self.assertIn("RESTORE_MARKER_RE", self.js)
        self.assertIn("RESTORE_MARKER_RE", self.html)
        # Used inside the apply helper (called by restore for both v3 and
        # v2 fallback paths) so the strip pass runs before term.write.
        m = re.search(r"function _applyRestored\s*\(\s*\w+\s*\)\s*\{", self.js)
        self.assertIsNotNone(m, "expected _applyRestored helper carrying the strip pass")
        assert m is not None
        start = m.end(); depth, end = 1, len(self.js)
        for i in range(start, len(self.js)):
            c = self.js[i]
            if c == '{': depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0: end = i; break
        body = self.js[start:end]
        self.assertIn("RESTORE_MARKER_RE", body,
            "RESTORE_MARKER_RE must be applied where the loaded blob is "
            "written, so legacy markers get stripped before term.write")

    def test_persist_writes_max_lines_capped_scrollback(self):
        # The persist call sets `scrollback: MAX_LINES` so the localStorage
        # blob stays bounded. If someone drops the cap (or removes it), the
        # blob can blow past localStorage's quota and (because we swallow the
        # error) silently lose ALL scrollback. With gzip the cap was raised
        # significantly — but it MUST still exist.
        self.assertIn("MAX_LINES = 25000", self.js)
        self.assertRegex(self.js,
            r"serializer\.serialize\(\s*\{\s*scrollback:\s*MAX_LINES\s*\}\s*\)",
            "persist must cap its serialize() at MAX_LINES")

    def test_persist_only_sweeps_keys_under_this_sids_prefix(self):
        # Regression guard: an earlier build deleted ANY key starting with
        # `claude-term-scrollback:`, wiping out every OTHER session's
        # scrollback every 15 s. The current sweep must scope BOTH the legacy
        # v2 and current v3 namespaces by this session's sid-bound prefix.
        m = re.search(r"function _sweepOldKeys\s*\(\s*\w+\s*\)\s*\{", self.js)
        self.assertIsNotNone(m, "expected _sweepOldKeys helper (sid-scoped sweep)")
        assert m is not None  # narrow for the type-checker
        start = m.end(); depth, end = 1, len(self.js)
        for i in range(start, len(self.js)):
            c = self.js[i]
            if c == '{': depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0: end = i; break
        body = self.js[start:end]
        # Must scope by both sid-bound prefixes so legacy + current entries
        # for THIS sid get cleaned and no other sid's entries get touched.
        self.assertIn("LSPREFIX_V2", body,
            "sweep must check the legacy v2 prefix so older runs of THIS "
            "session get cleaned up after migrating to v3")
        self.assertIn("LSPREFIX_V3", body,
            "sweep must check the current v3 prefix so older runs of THIS "
            "session don't accumulate forever under the gzipped namespace")

    def test_terminal_scrollback_is_10000_lines(self):
        # In-memory scrollback. The persist budget (MAX_LINES) caps how much
        # of this survives a reload; the live buffer can hold more.
        self.assertIn("scrollback: 10000", self.js)
        self.assertIn("scrollback: 10000", self.html)

    def test_lskeys_are_versioned_and_keyed_by_sid_and_ts(self):
        # Two-namespace scheme: v3 (current, gzipped) and v2 (legacy,
        # uncompressed) — both prefixed `claude-term-scrollback:<vN>:<sid>|`
        # and the combined key suffixed with `<ts>` so a session restart on
        # the same port can't inherit stale scrollback from the old run.
        self.assertIn("LSPREFIX_V2 = 'claude-term-scrollback:v2:'", self.js)
        self.assertIn("LSPREFIX_V3 = 'claude-term-scrollback:v3:'", self.js)
        self.assertRegex(self.js, r"LSKEY_V2\s*=\s*LSPREFIX_V2\s*\+\s*ts")
        self.assertRegex(self.js, r"LSKEY_V3\s*=\s*LSPREFIX_V3\s*\+\s*ts")

    def test_persist_compresses_via_compressionstream(self):
        # Gzip path: serialize → gzipToString → setItem(LSKEY_V3). If
        # CompressionStream isn't available, the wrapper falls back to v2
        # uncompressed — but that path stays as a defensive fallback only.
        self.assertIn("CompressionStream", self.js)
        self.assertIn("gzipToString", self.js)
        # The fallback uncompressed write must still exist for old browsers.
        self.assertIn("LSKEY_V2", self.js)
        # The current write path goes through gzip to LSKEY_V3.
        self.assertRegex(self.js,
            r"localStorage\.setItem\(LSKEY_V3,\s*gz\)",
            "persist must write the gzipped blob to LSKEY_V3")

    def test_restore_reads_v3_first_then_falls_back_to_v2(self):
        # Loader contract: try LSKEY_V3 (gunzip), fall back to LSKEY_V2
        # (plain) if v3 is missing or corrupt. Required so existing
        # localStorage entries from before this build still restore once.
        m = re.search(r"function restoreScrollback\s*\(\s*\)\s*\{", self.js)
        self.assertIsNotNone(m)
        assert m is not None
        start = m.end(); depth, end = 1, len(self.js)
        for i in range(start, len(self.js)):
            c = self.js[i]
            if c == '{': depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0: end = i; break
        body = self.js[start:end]
        self.assertIn("LSKEY_V3", body, "restore must try LSKEY_V3 first")
        self.assertIn("LSKEY_V2", body, "restore must fall back to LSKEY_V2 for legacy blobs")
        self.assertIn("gunzipFromString", body, "restore must gunzip the v3 blob")

    def test_persist_has_quota_retry(self):
        # When setItem throws QuotaExceededError we shrink and retry instead
        # of silently dropping the entire save (the swallow-and-toss in the
        # outer try would otherwise blank a tile's history on a single
        # overflow). The retry table must hold at least one smaller line cap.
        self.assertIn("QUOTA_RETRY_LINES", self.js)
        # _quotaExceeded helper must check the canonical error shape.
        self.assertIn("QuotaExceededError", self.js)
        # Attempts must include shrinking values < MAX_LINES.
        m = re.search(r"QUOTA_RETRY_LINES\s*=\s*\[([^\]]+)\]", self.js)
        self.assertIsNotNone(m, "QUOTA_RETRY_LINES must be a literal array of fallback sizes")
        assert m is not None
        vals = [int(x.strip()) for x in m.group(1).split(",") if x.strip().isdigit()]
        self.assertTrue(all(v < 25000 for v in vals), "retry sizes must be < MAX_LINES")
        self.assertTrue(any(v <= 3000 for v in vals),
            "include at least one small retry so degraded-quota tiles still save something")

    def test_persist_is_not_bound_to_beforeunload(self):
        # beforeunload kills pending promises, so the gzip path can't reliably
        # save there. The 15 s setInterval + visibilitychange together cover
        # 99% of the close-tab cases; we accept ≤15 s of stale-on-hard-close
        # in exchange for not corrupting localStorage with half-written gzip.
        # If someone re-adds the listener naïvely, this test fires.
        self.assertNotRegex(self.js, r"addEventListener\(\s*['\"]beforeunload['\"]\s*,\s*persist",
            "do not bind persist() to beforeunload — async gzip cannot complete during unload")

    def test_pagehide_writes_sync_v2_snapshot(self):
        # User-reported "scroll buffers gone after reload": the gzip persist
        # is async (CompressionStream returns a Promise) so a quick Cmd+R
        # between 15 s ticks leaves the in-flight write killed by the browser
        # and the stored snapshot 0-15 s stale. pagehide runs sync work
        # before unload — write the uncompressed v2 blob there as a safety
        # net. v2 is bigger but uncompressed serialize is synchronous and
        # lands before the browser tears down the page.
        # Capture the whole handler — anchor on the IIFE-indented closing `  });`
        # (a non-greedy `}\s*)\s*;` stops at the first serialize({...}); call).
        m = re.search(
            r"addEventListener\(\s*['\"]pagehide['\"][\s\S]+?\n  \}\);",
            self.js)
        self.assertIsNotNone(m,
            "missing pagehide listener — scrollback older than 15 s after reload")
        assert m is not None
        body = m.group(0)
        # The write must target LSKEY_V2 (v2 is uncompressed → no async gzip).
        self.assertIn("LSKEY_V2", body,
            "pagehide save must write LSKEY_V2 (sync) not LSKEY_V3 (async gzip)")
        self.assertIn("serializer.serialize", body,
            "pagehide save must call the sync serializer directly")
        # It must drop the stale v3 (restoreScrollback prefers v3, so an older v3
        # would shadow the sync-fresh v2) — but only AFTER a verified write, so a
        # failed/empty write never leaves the tile with zero snapshots.
        self.assertIn("removeItem(LSKEY_V3)", body,
            "pagehide save must remove LSKEY_V3 so restore falls through to the "
            "sync-fresh v2")
        self.assertRegex(body, r"if\s*\(\s*wrote\s*\)\s*localStorage\.removeItem\(LSKEY_V3\)",
            "removeItem(LSKEY_V3) must be guarded by a successful v2 write — "
            "unconditionally dropping v3 after a failed/empty write loses all history")

    def test_persistence_is_gated_until_restore_attempted(self):
        # "scrollback buffer gone after reload" root cause: a hidden/never-sized
        # tile SKIPS restore (restoreThenStart → grace → go(false)), so its buffer
        # is empty. A dashboard reload fires pagehide in every iframe at once; an
        # empty tile that persisted would overwrite its own good snapshot with "".
        # Persistence must be gated behind a `canPersist` latch set only on the
        # restore-attempted path, and must never write an empty serialize over a
        # stored snapshot.
        self.assertIn("canPersist", self.js,
            "persistence must be gated by a restore-authority latch (canPersist)")
        # The latch is armed only after restoreScrollback() runs, not on the
        # skipped-restore path.
        self.assertRegex(self.js,
            r"restoreScrollback\(\)\.then\(function \(\) \{[^}]*canPersist\s*=\s*true",
            "canPersist must be set true only after restore is attempted")
        # persist() bails when not allowed.
        self.assertRegex(self.js, r"function persist\(\)\s*\{\s*if\s*\([^)]*!canPersist",
            "persist() must early-return when canPersist is false")
        # pagehide bails when not allowed.
        m = re.search(r"addEventListener\(\s*['\"]pagehide['\"][\s\S]+?\n  \}\);", self.js)
        assert m is not None
        self.assertRegex(m.group(0), r"if\s*\(\s*!canPersist\s*\)\s*return",
            "pagehide handler must early-return when canPersist is false")
        # Neither path may write an empty/falsy serialize over the stored snapshot.
        self.assertRegex(self.js, r"if\s*\(\s*!raw\s*\)\s*return",
            "an empty serialize must not be persisted (it would clobber good history)")

    def test_skipped_restore_tile_reloads_on_first_reveal(self):
        # The flip side of the canPersist gate: a hidden/never-sized tile connects
        # WITHOUT restoring (restoreThenStart grace → go(false)), so it comes up
        # with ~0 scrollback. The old code stopped there, so a tile in a non-active
        # tab showed an empty buffer on a dashboard load and the user had to ↻
        # reload every tile to get history back. The grace path must instead arm a
        # one-shot watcher that reloads the tile the first time it's shown + sized,
        # so the now-sized fresh attach restores scrollback automatically.
        for label, src in (("js", self.js), ("html", self.html)):
            # The grace timeout (go(false)) must also install the reveal watcher.
            self.assertRegex(src,
                r"go\(false\);\s*restoreOnReveal\(\)",
                "%s: the skipped-restore grace path must arm restoreOnReveal() so a "
                "later-shown tile restores its scrollback" % label)
            body = TileIconAndReloadTest._fn_body(src, "restoreOnReveal")
            self.assertIsNotNone(body, "%s: restoreOnReveal() not found" % label)
            assert body is not None
            # Only acts once the tile is actually sized (no reload while still 0×0).
            self.assertRegex(body, r"if\s*\(\s*done\s*\|\|\s*!isSized\(\)\s*\)\s*return",
                "%s: restoreOnReveal must wait until the tile is sized and fire once "
                "(guard against a reload loop on layout jitter)" % label)
            # And it reloads the tile so the fresh, sized attach restores.
            self.assertIn("location.reload()", body,
                "%s: restoreOnReveal must reload the tile so the now-sized fresh "
                "attach restores scrollback" % label)
            # Reload must be latched (done flag) so it can't loop.
            self.assertRegex(body, r"done\s*=\s*true",
                "%s: restoreOnReveal must latch (done=true) to stay one-shot" % label)

    def test_reattach_reblits_buffer_onto_renderer(self):
        # "buffer-full-but-unpainted" regression: on a dashboard reload, every
        # tile's iframe (re)acquires a WebGL context at once. Under the page's
        # GL-context cap (15+ tiles) some contexts are still warming / get
        # evicted right as the async restore + reattach repaint write the
        # history into the buffer, so those writes paint onto a not-ready
        # context and the tile shows blank — even though the BUFFER is full.
        # The boot-time _burstHeal ran on an empty buffer (before restore
        # landed) and _repaintWiggle won't fire (its blank-check sees a
        # non-empty buffer), so nothing re-blits and the tile stays blank until
        # a manual ↻. socket.onopen must schedule a renderer-only re-blit AFTER
        # the burst lands to repaint the real buffer onto the settled context.
        for label, src in (("js", self.js), ("html", self.html)):
            # onopen schedules the re-blit (and resets its per-open latch).
            self.assertIn("setTimeout(_reblitAfterReattach,", src,
                "%s: socket.onopen must schedule _reblitAfterReattach after the "
                "reattach burst lands" % label)
            self.assertRegex(src, r"_wiggleTries = 0;\s*_reblitDone = false;",
                "%s: onopen must reset _reblitDone so reconnects re-blit too" % label)
            body = TileIconAndReloadTest._fn_body(src, "_reblitAfterReattach")
            self.assertIsNotNone(body, "%s: _reblitAfterReattach() not found" % label)
            assert body is not None
            # It must re-blit via the renderer-only heal (no term.resize → no
            # SIGWINCH → no Ink reflow/dup; preserves scrollback).
            self.assertIn("_burstHeal()", body,
                "%s: _reblitAfterReattach must re-blit via _burstHeal "
                "(renderer-only repaint)" % label)
            self.assertNotIn("term.resize", body,
                "%s: the re-blit must NOT resize (a SIGWINCH would risk the Ink "
                "frame-dup the wiggle guards against)" % label)
            # It must NOT gate on _viewportLooksBlank — the whole point is a
            # NON-empty buffer that never reached the screen, which that check
            # would skip (it's correct for the wiggle, wrong here).
            self.assertNotIn("_viewportLooksBlank", body,
                "%s: the re-blit must run regardless of the buffer-blank check — "
                "a full-but-unpainted buffer is exactly what it cures" % label)
            # Latched to one heal per open.
            self.assertRegex(body, r"_reblitDone\s*=\s*true",
                "%s: _reblitAfterReattach must latch (_reblitDone=true) so the two "
                "scheduled passes collapse to a single heal" % label)

    def test_first_replay_burst_classified_to_avoid_dup(self):
        # Shell tiles have ttyd re-dump their whole scrollback as bare text on
        # reattach; restoring localStorage on top of that DUPLICATES it, and it
        # compounds every reload (200→400→600 lines — user-reported "scroll
        # buffer repeats the same text over and over"). term-client classifies
        # the first replay burst and only restores when it's a TUI repaint
        # (leading clear/home), so a shell dump is never doubled while a claude
        # tile (whose replay carries no history) still gets its scrollback back.
        self.assertIn("writeOutput(payload)", self.js,
            "cmd==0 output must route through writeOutput (the first-burst classifier)")
        self.assertIn("function _looksLikeTuiRepaint", self.js,
            "missing _looksLikeTuiRepaint — needed to tell a TUI repaint from a "
            "bare-text shell dump")
        # Restore must be gated on BOTH being allowed (sized → mayRestore) AND the
        # burst looking like a TUI repaint — never unconditionally.
        self.assertRegex(self.js,
            r"if\s*\(\s*mayRestore\s*&&\s*_looksLikeTuiRepaint\(payload\)\s*\)",
            "restore must run only for a sized tile whose first burst is a TUI "
            "repaint (else a shell dump gets duplicated on every reload)")
        # The classifier regex must recognise cursor-home/erase-display/alt-screen.
        self.assertRegex(self.js, r"TUI_LEAD_RE\s*=\s*/",
            "TUI_LEAD_RE classifier regex not found")

    def test_tui_classifier_decode_window_clears_long_preamble(self):
        # Regression: _looksLikeTuiRepaint decoded only the first 128 bytes of
        # the burst, then stripped the leading title-OSC / SGR / whitespace
        # preamble before testing TUI_LEAD_RE. A session in a deeply-nested cwd
        # emits a long title OSC (`\x1b]0;…long path…\x07`) plus SGR colour setup
        # that can exceed 128 bytes BEFORE the first clear sequence. At 128 the
        # window truncated mid-preamble, TUI_LEAD_RE missed, restore was skipped,
        # canPersist stayed false → the tile silently lost its scrollback on
        # every reload. The window must be large enough (>=512) to clear a real
        # title+SGR preamble.
        for label, src in (("js", self.js), ("html", self.html)):
            m = re.search(
                r"function _looksLikeTuiRepaint[\s\S]*?payload\.subarray\(\s*0\s*,"
                r"\s*Math\.min\(\s*payload\.length\s*,\s*(\d+)\s*\)\s*\)", src)
            self.assertIsNotNone(m,
                "%s: _looksLikeTuiRepaint decode-window slice not found" % label)
            assert m is not None
            window_bytes = int(m.group(1))
            self.assertGreaterEqual(window_bytes, 512,
                "%s: TUI-detection decode window is %d bytes — too small; a long "
                "title+SGR preamble overruns it, TUI_LEAD_RE misses, and the tile "
                "loses scrollback on reload. Use >=512." % (label, window_bytes))

    def test_reconnect_status_is_overlay_not_buffer(self):
        # Regression: reconnect/disconnect notices used to be written straight
        # into the xterm buffer (`term.write('\\r\\n…[reconnecting…]…\\r\\n')`).
        # For a normal-buffer TUI (claude/Ink) the \\r\\n pushed the line into
        # scrollback where the SerializeAddon captured it, so every network blip
        # permanently embedded a "[reconnecting…]" line in restored history,
        # accumulating on each reload. Status must go through a DOM overlay
        # (_setStatus) and never touch the buffer.
        for label, src in (("js", self.js), ("html", self.html)):
            # The overlay helper must exist and be used for both states.
            self.assertIn("function _setStatus", src,
                "%s: missing _setStatus DOM-overlay helper" % label)
            self.assertRegex(src, r"_setStatus\(\s*['\"]reconnecting",
                "%s: reconnect notice must go through _setStatus (overlay)" % label)
            self.assertRegex(src, r"_setStatus\(\s*['\"]disconnected",
                "%s: disconnect notice must go through _setStatus (overlay)" % label)
            # And the status text must NOT be written into the xterm buffer.
            self.assertNotRegex(src, r"term\.write\([^)]*reconnecting",
                "%s: '[reconnecting…]' must NOT be written into the xterm buffer "
                "— it leaks into persisted scrollback" % label)
            self.assertNotRegex(src, r"term\.write\([^)]*disconnected",
                "%s: '[disconnected]' must NOT be written into the xterm buffer "
                "— it leaks into persisted scrollback" % label)

    def test_smooth_scroll_defaults_on_with_localstorage_opt_out(self):
        # The previous behavior was `window.__smoothScroll = false;` hard-coded
        # every page load — opt-in only. A user who turned it on via devtools
        # lost the setting on every reload ("smooth scroll is gone"). Now
        # default ON, with localStorage opt-out ('claude-term-smooth-scroll'='0').
        m = re.search(
            r"function smoothScrollProto\s*\(\s*\)\s*\{([\s\S]+?)\n\s*\}\s*\)\s*\(\s*\)\s*;",
            self.js)
        self.assertIsNotNone(m, "smoothScrollProto IIFE not found")
        assert m is not None
        body = m.group(1)
        # The hard-coded false assignment must NOT linger — that would
        # always clobber the localStorage read.
        self.assertNotRegex(body, r"window\.__smoothScroll\s*=\s*false\s*;",
            "smooth scroll must not be unconditionally disabled — user-reported "
            "as 'smooth scroll is gone after every reload'")
        # The default-on read must be present.
        self.assertIn("'claude-term-smooth-scroll'", body,
            "smooth scroll must read its opt-out flag from localStorage")
        # `!== '0'` is the default-on idiom: missing key → true, '0' → false.
        self.assertRegex(body, r"!==\s*['\"]0['\"]",
            "the default must be ON (only the literal '0' disables it)")


class SaveDroppedFileTest(unittest.TestCase):
    """Unit-test the drag/paste upload saver. Verifies it writes under
    `<cwd>/.vibe-drops/` with a uuid prefix, sanitizes the basename so a
    `../../etc/passwd` filename can't escape, and returns the path the
    SHELL inside the session needs to use — which differs for container
    sessions (path inside the container = /workspace/...) vs host
    sessions (absolute host path). The path returned to the iframe is
    what gets typed into the PTY, so getting this wrong means the user's
    `cat <path>` fails."""

    def setUp(self):
        self.reg = tempfile.mkdtemp(prefix="serve-dropfile-reg-")
        self.cwd = tempfile.mkdtemp(prefix="serve-dropfile-cwd-")
        self._saved_reg = serve.REGISTRY
        serve.REGISTRY = self.reg

    def tearDown(self):
        serve.REGISTRY = self._saved_reg
        shutil.rmtree(self.reg, ignore_errors=True)
        shutil.rmtree(self.cwd, ignore_errors=True)

    def _write_session(self, sid, kind="host", container=False):
        meta = {"name": sid, "port": 7000, "kind": kind, "cwd": self.cwd,
                "started": "2026-05-31T00:00:00Z"}
        if container:
            meta["container"] = True
        with open(os.path.join(self.reg, sid + ".json"), "w") as f:
            json.dump(meta, f)

    def test_writes_under_vibe_drops_with_uid_prefix(self):
        self._write_session("host-1")
        p = serve.save_dropped_file("host-1", "foo.txt", b"hello")
        self.assertIsNotNone(p); assert p is not None
        self.assertTrue(os.path.isfile(p), "saved file missing at %r" % p)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"hello")
        # uuid-prefix prevents collisions when the same basename is dropped
        # twice in one session.
        base = os.path.basename(p)
        self.assertRegex(base, r"^[0-9a-f]{8}-foo\.txt$",
            "saved file must be uid-prefixed: got %r" % base)
        self.assertTrue(p.startswith(os.path.join(self.cwd, ".vibe-drops")),
            "saved file must live under cwd/.vibe-drops")

    def test_container_session_returns_workspace_path(self):
        # kind=container → claude-box. The container sees the session's host
        # cwd as /workspace, so the path the shell uses is /workspace/...
        # NOT the host absolute path (which doesn't exist inside the box).
        self._write_session("box-1", kind="container")
        p = serve.save_dropped_file("box-1", "foo.txt", b"hi")
        self.assertIsNotNone(p); assert p is not None
        self.assertTrue(p.startswith("/workspace/.vibe-drops/"),
            "container session must return /workspace path, got %r" % p)
        # File still lives on host inside cwd — that's where the bind-mount
        # sources from.
        host_files = os.listdir(os.path.join(self.cwd, ".vibe-drops"))
        self.assertEqual(len(host_files), 1)
        self.assertTrue(host_files[0].endswith("-foo.txt"))

    def test_terminal_in_container_returns_workspace_path(self):
        # "Terminal in container" tiles are kind=terminal + container=true.
        # They run a shell inside the same claude-box, so the path
        # translation must apply to them too — easy to miss since `kind`
        # alone says "terminal" (host kind).
        self._write_session("ct-1", kind="terminal", container=True)
        p = serve.save_dropped_file("ct-1", "foo.txt", b"hi")
        self.assertIsNotNone(p); assert p is not None
        self.assertTrue(p.startswith("/workspace/.vibe-drops/"),
            "terminal+container session must also map to /workspace path, got %r" % p)

    def test_host_session_returns_absolute_host_path(self):
        # host / plain terminal / opencode: shell runs on host, so it needs
        # the host absolute path.
        for kind in ("host", "terminal", "opencode"):
            self._write_session("h-" + kind, kind=kind)
            p = serve.save_dropped_file("h-" + kind, "f.txt", b"x")
            self.assertIsNotNone(p, "kind=%s returned None" % kind); assert p is not None
            self.assertTrue(p.startswith(self.cwd),
                "kind=%s must return host absolute path under cwd, got %r" % (kind, p))

    def test_unknown_sid_returns_none(self):
        self.assertIsNone(serve.save_dropped_file("nope", "foo.txt", b"x"),
            "unknown sid must return None, not silently save to a default")

    def test_safe_filename_handles_traversal_and_bad_chars(self):
        # `_safe_filename` is the gate that keeps a hostile name from
        # writing outside `.vibe-drops/`. Tests the spec, not the path
        # join — but the path join would still concatenate the sanitized
        # name, so verifying the sanitizer covers the security boundary.
        self.assertEqual(serve._safe_filename("../../etc/passwd"), "passwd",
            "leading slashes / .. components must be stripped (basename only)")
        self.assertEqual(serve._safe_filename("/tmp/x.txt"), "x.txt",
            "absolute path basename only")
        self.assertEqual(serve._safe_filename("a\\b\\c.txt"), "c.txt",
            "backslash path separators (Windows-style) treated as separators")
        self.assertEqual(serve._safe_filename("Bob's notes.txt"), "Bob's notes.txt",
            "ordinary printable chars including apostrophe + space preserved "
            "(quoting handled by the JS shell-quote helper)")
        self.assertEqual(serve._safe_filename(""), "file",
            "empty name falls back to 'file'")
        self.assertEqual(serve._safe_filename(None), "file",
            "None falls back to 'file'")
        self.assertEqual(serve._safe_filename("a\x00b\x01c"), "abc",
            "NUL + non-printable chars stripped")

    def test_safe_filename_path_can_not_escape_vibe_drops(self):
        # End-to-end check: try to escape with a hostile basename.
        # save_dropped_file's path-join uses the sanitized name, so the
        # file must end up INSIDE .vibe-drops regardless of what was sent.
        self._write_session("host-trav")
        p = serve.save_dropped_file("host-trav", "../../../../etc/passwd", b"oops")
        self.assertIsNotNone(p); assert p is not None
        drops = os.path.join(self.cwd, ".vibe-drops")
        # commonpath is the strict containment check we want — relpath()
        # would happily climb out via ../, but commonpath stays honest.
        self.assertEqual(os.path.commonpath([p, drops]), drops,
            "saved file escaped .vibe-drops! got %r" % p)


class StashHTTPTest(unittest.TestCase):
    """HTTP-level coverage routed through the fixture-managed serve.py
    subprocess (the same one ServeSmokeTest uses). Verifies /api/stash is
    CSRF-gated, returns ok:false for unknown ids, performs a real on-disk
    round-trip, and the dashboard HTML wires up the drawer markup."""

    def _csrf(self):
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', body.decode())
        if not m:
            self.fail("csrf-token meta tag not found")
        return m.group(1)

    def test_api_stash_requires_csrf(self):
        status, _ = _post("/api/stash?id=anything&on=1", host=HOST_HDR)
        self.assertEqual(status, 403)

    def test_api_stash_wrong_csrf_forbidden(self):
        status, _ = _post("/api/stash?id=anything&on=1", host=HOST_HDR, csrf="nope")
        self.assertEqual(status, 403)

    def test_api_stash_unknown_id_returns_ok_false(self):
        token = self._csrf()
        status, body = _post("/api/stash?id=does-not-exist&on=1",
                             host=HOST_HDR, csrf=token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ok"], False)

    def test_api_stash_round_trip_writes_disk_flag(self):
        # Write into the subprocess's registry (CLAUDE_SESSIONS_DIR points at
        # _tmpdir) so the live serve.py sees it. Use a real listener so
        # read_sessions doesn't prune the entry mid-test.
        token = self._csrf()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0)); s.listen(8)
        path = os.path.join(_tmpdir, "host-stashable.json")
        try:
            port = s.getsockname()[1]
            with open(path, "w") as f:
                json.dump({"name": "x", "port": port, "kind": "host",
                           "cwd": "/tmp", "started": "2026-05-30T00:00:00Z"}, f)
            status, body = _post("/api/stash?id=host-stashable&on=1",
                                 host=HOST_HDR, csrf=token)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])
            with open(path) as f:
                self.assertTrue(json.load(f).get("stashed"))
            status, body = _post("/api/stash?id=host-stashable&on=0",
                                 host=HOST_HDR, csrf=token)
            self.assertEqual(status, 200)
            with open(path) as f:
                self.assertNotIn("stashed", json.load(f))
        finally:
            s.close()
            try:
                os.remove(path)
            except OSError:
                pass

    def test_api_sessions_surfaces_stashed_flag(self):
        token = self._csrf()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0)); s.listen(8)
        path = os.path.join(_tmpdir, "host-listflag.json")
        try:
            port = s.getsockname()[1]
            with open(path, "w") as f:
                json.dump({"name": "x", "port": port, "kind": "host",
                           "cwd": "/tmp", "started": "2026-05-30T00:00:00Z"}, f)
            _post("/api/stash?id=host-listflag&on=1",
                  host=HOST_HDR, csrf=token)
            status, body = _get("/api/sessions", host=HOST_HDR)
            self.assertEqual(status, 200)
            entries = [e for e in json.loads(body)["sessions"]
                       if e["id"] == "host-listflag"]
            self.assertEqual(len(entries), 1)
            self.assertIs(entries[0]["stashed"], True)
        finally:
            s.close()
            try:
                os.remove(path)
            except OSError:
                pass

    def test_dashboard_html_includes_stash_drawer(self):
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        html = body.decode()
        # Drawer chrome: pill in header, dropdown menu, count badge.
        self.assertIn('id="stashWrap"', html)
        self.assertIn('id="stashBtn"', html)
        self.assertIn('id="stashMenu"', html)
        # Frontend wiring: drawer renderer + per-tile stash handler + POST URL.
        self.assertIn("renderStashDrawer", html)
        self.assertIn("doStash", html)
        self.assertIn("/api/stash", html)

    def test_dashboard_html_has_launcher_menu(self):
        # The "+ New" menu's agent section is now configurable: a dynamic
        # #launcherItems container (populated from /api/launchers) plus a
        # "Manage launchers…" action. The opencode badge CSS must still exist so
        # server-spawned and shell-launched opencode sessions render distinctly.
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        html = body.decode()
        self.assertIn('id="launcherItems"', html)
        self.assertIn('data-act="manage-launchers"', html)
        self.assertIn(".badge.opencode", html)

    def test_default_launchers_cover_common_agents(self):
        # The seeded launchers (shown on a fresh install) must include the
        # common claude / codex / opencode configs the menu used to hardcode.
        cmds = " ".join(l["command"] for l in serve.DEFAULT_LAUNCHERS)
        self.assertIn("claude", cmds)
        self.assertIn("codex", cmds)
        self.assertIn("opencode", cmds)
        # …and a skip-perms variant + the Vertex provider preset.
        self.assertTrue(any("--dangerously-skip-permissions" in l["command"]
                            for l in serve.DEFAULT_LAUNCHERS))
        self.assertTrue(any(l.get("provider") == "vertex"
                            for l in serve.DEFAULT_LAUNCHERS))


class CondenseTileTest(unittest.TestCase):
    """Condense-until-ring ("park as a card"): a tile-head button parks the tile
    as a thin spine (--cond-peek) tucked under its right neighbour; the next bell
    (markBell) springs it back to full size, animated by the .tile flex-basis +
    margin transition. The iframe is pinned to a constant width in CSS for EVERY
    row tile so the box can animate without re-firing fit→SIGWINCH. Static-source
    checks against the dashboard page (the dashboard script has no JS unit
    harness); the animation+pin invariants are also guarded end-to-end in
    test_tile_condense.mjs."""

    page = ""

    @classmethod
    def setUpClass(cls):
        status, body = _get("/", host=HOST_HDR)
        assert status == 200
        cls.page = body.decode()

    def test_head_has_condense_button(self):
        # The button must exist, carry the .cond class (shared head-button
        # styling + the CSS that keeps it visible while condensed), and sit in
        # the head's append list so every terminal-backed tile gets one.
        self.assertRegex(self.page, r"condBtn\.className\s*=\s*'cond'",
            "condense button (className 'cond') not created")
        self.assertRegex(self.page,
            r"head\.append\([^)]*condBtn[^)]*stashBtn[^)]*closeBtn\)",
            "condBtn must be appended into the tile head")

    def test_button_toggles_condensed(self):
        # Click toggles: condensed tiles can be manually expanded again.
        m = re.search(r"condBtn\.onclick[\s\S]{0,200}?setCondensed\(\s*s\.id\s*,"
                      r"\s*!el\.classList\.contains\('condensed'\)\s*\)", self.page)
        self.assertIsNotNone(m, "condBtn click must toggle via setCondensed")

    def test_css_condensed_row_mode_is_a_spine(self):
        # Row layout: the condensed BOX collapses to the --cond-peek spine and
        # tucks --cond-tuck under its right neighbour (the deck-of-cards look).
        self.assertRegex(self.page,
            r"#grid\.row\s*>\s*\.tile\.condensed\s*\{\s*"
            r"flex:\s*0 0 var\(--cond-peek\)\s*;\s*"
            r"margin-right:\s*calc\(-1 \* var\(--cond-tuck\)\)",
            "row-mode condensed tile must collapse to the --cond-peek spine and "
            "tuck by --cond-tuck")

    def test_css_iframe_pinned_for_every_row_tile(self):
        # SIGWINCH-safety: every row tile's iframe is pinned to a CONSTANT width
        # (NOT just condensed ones), so animating the box flex-basis can't drag
        # the iframe through intermediate widths and re-fire fit→SIGWINCH. The
        # selector must be the broad `#grid.row > .tile > iframe`, not gated on
        # .condensed.
        self.assertRegex(self.page,
            r"#grid\.row\s*>\s*\.tile\s*>\s*iframe\s*\{\s*width:\s*calc\(",
            "every row tile's iframe must be width-pinned, not just condensed ones")
        # ...and not the old condensed-only pin, which would let a non-condensed
        # tile's iframe follow the box at width:100% during the expand animation.
        self.assertNotRegex(self.page,
            r"#grid\.row\s*>\s*\.tile\.condensed\s*>\s*iframe\s*\{",
            "the iframe pin must not be re-gated behind .condensed")

    def test_css_deck_shadow_is_cast_by_the_covering_tile(self):
        # The stacked-deck boundary shadow is cast BY the tile that covers the
        # parked card (its right neighbour), as a leftward box-shadow on that
        # tile — tagged `.covers-card` in JS by markDeckShadows(). Two earlier
        # approaches are both wrong and must stay gone:
        #   * `.tile.condensed + .tile` — DOM-adjacency; tiles are laid out and
        #     painted by flex `order`, so the DOM-next sibling isn't the visual
        #     coverer (shadow landed on the wrong element / off-screen).
        #   * a fixed-offset `.tile.condensed::after` on the card — left an ~8px
        #     gap because the neighbour's real left edge isn't card.right-tuck.
        m = re.search(r"#grid\.row\s*>\s*\.tile\.covers-card\s*\{([^}]*)\}", self.page)
        self.assertIsNotNone(m, "deck shadow must be a `.tile.covers-card` box-shadow rule")
        assert m is not None
        self.assertRegex(m.group(1), r"box-shadow:\s*-",
            "the covering tile's shadow must project LEFT (negative x-offset) onto the card")
        # markDeckShadows tags the covering tile by walking the VISIBLE row in
        # flex order and flagging any tile whose predecessor is condensed.
        self.assertRegex(self.page, r"function markDeckShadows\(\)",
            "markDeckShadows() must exist to tag the covering tile order-independently")
        mk = re.search(r"function markDeckShadows\(\)\s*\{([\s\S]*?)\n\}", self.page)
        self.assertIsNotNone(mk, "markDeckShadows body not found")
        assert mk is not None
        self.assertRegex(mk.group(1), r"\.style\.order",
            "markDeckShadows must sort by flex `order`, not DOM order")
        self.assertRegex(mk.group(1), r"covers-card",
            "markDeckShadows must toggle the .covers-card class")
        # It must be re-run when condense state changes, so the tag stays correct.
        m2 = re.search(r"function setCondensed\([\s\S]*?\n\}", self.page)
        self.assertIsNotNone(m2, "setCondensed not found")
        assert m2 is not None
        self.assertIn("markDeckShadows()", m2.group(0),
            "setCondensed must call markDeckShadows() so the covering tile is re-tagged")
        # The fragile / buggy predecessors must be gone, or a bug is back.
        self.assertNotRegex(self.page,
            r"\.tile\.condensed\s*\+\s*\.tile\s*\{[^}]*box-shadow",
            "the order-fragile `.tile.condensed + .tile` shadow rule must not return")
        self.assertNotRegex(self.page,
            r"#grid\.row\s*>\s*\.tile\.condensed::after\s*\{[^}]*box-shadow|"
            r"#grid\.row\s*>\s*\.tile\.condensed::after\s*\{[^}]*linear-gradient",
            "the gap-prone card-anchored ::after shadow must not return")

    def test_css_width_change_is_animated(self):
        # The feature spec says animated: the base .tile rule must transition the
        # box collapse (flex-basis) AND the tuck (margin), so condense AND the
        # bell-triggered expand glide instead of snapping. (max-width is along
        # for grid mode.) This is the "condense animation is gone" guard.
        m = re.search(r"\n  \.tile\s*\{[^}]*transition:([^;}]*)", self.page)
        self.assertIsNotNone(m, ".tile rule with a transition not found")
        assert m is not None
        self.assertIn("flex-basis", m.group(1))
        self.assertIn("margin", m.group(1))

    def test_bell_expands_condensed_tile(self):
        # The whole point: a ring restores the tile to full size. markBell must
        # un-condense BEFORE flagging the bell class.
        m = re.search(r"function markBell\(id\)\s*\{([\s\S]*?)playBell\(\)", self.page)
        self.assertIsNotNone(m, "markBell not found")
        assert m is not None
        body = m.group(1)
        un = body.find("setCondensed(id, false)")
        flag = body.find("classList.add('bell')")
        self.assertGreaterEqual(un, 0, "markBell must expand a condensed tile")
        self.assertGreater(flag, un, "expand must happen before the bell flag "
                           "so the restored tile carries the ring highlight")

    def test_condensed_state_persists_and_prunes(self):
        # Survives a dashboard reload (localStorage set, re-applied on tile
        # creation) and doesn't leak ids for reaped sessions.
        self.assertIn("'claude-sessions-condensed'", self.page)
        self.assertRegex(self.page,
            r"if \(condensedIds\.has\(s\.id\)\) setCondensed\(s\.id, true, false\)",
            "persisted condensed state must be re-applied on tile creation")
        self.assertRegex(self.page,
            r"condensedIds\.delete\(id\);\s*saveCondensed\(\)",
            "reaped sessions must be pruned from the persisted set")

    def test_condense_does_not_resize_iframe_in_js(self):
        # Condensing must NOT resize the PTY. The width pin is pure CSS now (see
        # test_css_iframe_pinned_for_every_row_tile): the constant-width iframe is
        # clipped by the tile's overflow:hidden. So setCondensed must NOT touch
        # the iframe's pixel width in JS — an earlier design measured + froze it
        # with getBoundingClientRect/inline width, which raced the class toggle
        # and could freeze the already-narrow sliver. Just toggling the class
        # keeps the PTY at full width (no fit→SIGWINCH, no hard-wrapped
        # scrollback). Channel/note/webview tiles skip the parked-wheel message.
        m = re.search(r"function setCondensed\(id, on(?:, relayout)?\)\s*\{([\s\S]*?)\n\}", self.page)
        self.assertIsNotNone(m, "setCondensed not found")
        assert m is not None
        body = m.group(1)
        self.assertRegex(body, r"classList\.toggle\('condensed'",
            "setCondensed must toggle the .condensed class (CSS drives the width)")
        self.assertNotIn("getBoundingClientRect().width", body,
            "setCondensed must NOT measure/freeze the iframe width in JS — the "
            "CSS pin handles it; measuring races the class toggle")
        self.assertNotRegex(body, r"f\.style\.width\s*=",
            "setCondensed must NOT set an inline iframe width — the CSS pin "
            "(`#grid.row > .tile > iframe`) is the single source of truth")
        self.assertIn("'channel'", body)
        self.assertIn("'note'", body)


class DropfileHTTPTest(unittest.TestCase):
    """End-to-end HTTP coverage for /api/dropfile — the upload endpoint the
    iframe POSTs to from a DIFFERENT origin (its own ttyd port) so it can
    drop a file into the session's `.vibe-drops/` and get back a path the
    shell can read. Covers CSRF guard, CORS preflight (browsers send OPTIONS
    before a cross-origin POST with custom headers), the actual round-trip,
    and the local-origin-only allowlist on CORS responses."""

    def _csrf(self):
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', body.decode())
        if not m:
            self.fail("csrf-token meta tag not found")
        return m.group(1)

    def _post_raw(self, path, body, host=None, csrf=None, origin=None,
                  ctype="application/octet-stream"):
        # HOST_HDR is set in setUpModule (after class construction), so resolve
        # it at call time rather than as a default value.
        if host is None:
            host = HOST_HDR
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        try:
            headers = {"Host": host, "Content-Length": str(len(body)),
                       "Content-Type": ctype}
            if csrf is not None:
                headers["X-CSRF-Token"] = csrf
            if origin is not None:
                headers["Origin"] = origin
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def _options(self, path, host=None, origin=None):
        if host is None:
            host = HOST_HDR
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        try:
            headers = {"Host": host}
            if origin is not None:
                headers["Origin"] = origin
            conn.request("OPTIONS", path, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_dropfile_csrf_required(self):
        # Same CSRF guard as every other state-changing endpoint.
        status, _, _ = self._post_raw("/api/dropfile?sid=x&name=f", b"data")
        self.assertEqual(status, 403)

    def test_dropfile_options_preflight_returns_local_cors(self):
        # Browsers send OPTIONS before any cross-origin POST that carries a
        # custom header (X-CSRF-Token here). We must respond 204 with
        # Access-Control-Allow-Origin echoing the iframe's origin AND the
        # allowed methods + headers, or the browser cancels the actual POST.
        status, hdrs, _ = self._options("/api/dropfile",
                                         origin="http://127.0.0.1:9999")
        self.assertEqual(status, 204)
        self.assertEqual(hdrs.get("Access-Control-Allow-Origin"),
                         "http://127.0.0.1:9999")
        self.assertIn("POST", hdrs.get("Access-Control-Allow-Methods", ""))
        self.assertIn("X-CSRF-Token", hdrs.get("Access-Control-Allow-Headers", ""))

    def test_dropfile_options_ignores_non_local_origin(self):
        # Defense-in-depth: even if someone tricked the browser, a public
        # page (Origin: http://evil.com) must NOT get CORS clearance.
        status, hdrs, _ = self._options("/api/dropfile", origin="http://evil.com")
        self.assertEqual(status, 204)
        self.assertIsNone(hdrs.get("Access-Control-Allow-Origin"),
            "Non-local origins must NOT be echoed in Access-Control-Allow-Origin")

    def test_dropfile_round_trip_saves_and_returns_path(self):
        token = self._csrf()
        cwd = tempfile.mkdtemp(prefix="dropfile-cwd-")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        sid = "host-dropfile-rt"
        path_json = os.path.join(_tmpdir, sid + ".json")
        try:
            with open(path_json, "w") as f:
                json.dump({"name": "x", "port": sock.getsockname()[1],
                           "kind": "host", "cwd": cwd,
                           "started": "2026-05-31T00:00:00Z"}, f)
            payload = b"hello world\n"
            status, hdrs, body = self._post_raw(
                "/api/dropfile?sid=" + sid + "&name=greet.txt",
                payload, csrf=token, origin="http://127.0.0.1:9999")
            self.assertEqual(status, 200, body)
            self.assertEqual(hdrs.get("Access-Control-Allow-Origin"),
                             "http://127.0.0.1:9999",
                "POST response must also carry Access-Control-Allow-Origin "
                "(preflight alone isn't enough; the response itself is "
                "subject to CORS too)")
            ret = json.loads(body)
            self.assertIn("path", ret)
            self.assertTrue(ret["path"].startswith(os.path.join(cwd, ".vibe-drops")),
                "host session must return host absolute path, got %r" % ret["path"])
            with open(ret["path"], "rb") as f:
                self.assertEqual(f.read(), payload,
                    "file content must round-trip byte-for-byte")
        finally:
            sock.close()
            try:
                os.remove(path_json)
            except OSError:
                pass
            shutil.rmtree(cwd, ignore_errors=True)

    def test_dropfile_missing_sid_or_name_400(self):
        token = self._csrf()
        for q in ("/api/dropfile", "/api/dropfile?sid=x",
                  "/api/dropfile?name=f"):
            status, _, _ = self._post_raw(q, b"data", csrf=token)
            self.assertEqual(status, 400,
                "missing required params must 400, got %d for %r" % (status, q))

    def test_dropfile_empty_body_400(self):
        token = self._csrf()
        status, _, _ = self._post_raw(
            "/api/dropfile?sid=x&name=f", b"", csrf=token)
        self.assertEqual(status, 400,
            "empty body (Content-Length: 0) must 400 — there's nothing to save")

    def test_dropfile_unknown_session_404(self):
        token = self._csrf()
        status, _, _ = self._post_raw(
            "/api/dropfile?sid=does-not-exist&name=f.txt", b"x", csrf=token)
        self.assertEqual(status, 404,
            "unknown sid must 404 (session may have been closed mid-upload)")


class KeybindingsTest(unittest.TestCase):
    """Dashboard global keyboard shortcuts. Catches regressions where the
    chrome (serve.py) and the terminal client (term-client.js / term.html)
    drift apart on which keys forward + how they're handled.

    Bindings under test:
      - Ctrl+Q (no meta/alt)  → arm/close the selected tile. Replaces the
        former Ctrl+X chord, which collided with nano's "exit" — when nano
        ran inside a tile the first press got swallowed by the dashboard
        arming and never reached nano.
      - Cmd/Ctrl+E → spawn a kind=terminal tile, same path as the +New menu
        (lands in the active tab's cwd). Originally Cmd+T, but Chrome on
        macOS hard-binds Cmd+T at the window-manager level (opens a tab in
        the regular Chrome window even from a PWA) and the page never sees
        the keydown. Cmd+E is free in Chrome.
    """

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "serve.py")) as f:
            cls.serve = f.read()
        with open(os.path.join(here, "term-client.js")) as f:
            cls.js = f.read()
        with open(os.path.join(here, "term.html")) as f:
            cls.html = f.read()

    def _slice_js_function(self, header_re, src=None):
        """Find a function header in `src` (defaults to self.js) and return
        the body between its opening `{` and matching close `}`. Used a lot
        below to assert on function contents without false positives from
        unrelated code that happens to mention the same identifier."""
        if src is None:
            src = self.js
        m = re.search(header_re, src)
        self.assertIsNotNone(m, "function header not found: %r" % header_re)
        assert m is not None
        depth, end = 1, len(src)
        for i in range(m.end(), len(src)):
            c = src[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        return src[m.end():end]

    # ---- chrome (serve.py) ----

    def test_chrome_keydown_arms_on_ctrl_q_not_ctrl_x(self):
        # The document-level keydown handler must check for Ctrl+Q, not the
        # legacy Ctrl+X. The pattern is `e.ctrlKey && ... && (e.key === 'q'
        # || e.key === 'Q')`.
        self.assertRegex(self.serve,
            r"e\.ctrlKey[^;]*e\.key\s*===\s*['\"]q['\"]",
            "chrome keydown must arm on Ctrl+Q")
        # Belt-and-suspenders: no leftover Ctrl+X *handler* (a comment
        # mentioning the historical chord is fine; an active key check on
        # 'x'/'X' is not).
        self.assertNotRegex(self.serve,
            r"e\.ctrlKey[^;]*e\.key\s*===\s*['\"]x['\"]",
            "chrome must not still handle Ctrl+X (it collides with nano exit)")

    def test_chrome_keydown_spawns_terminal_on_cmd_e(self):
        # Cmd or Ctrl + E (no alt, no shift) → spawnTile('terminal'). The
        # handler accepts ctrlKey OR metaKey so Windows/Linux users (where
        # Cmd doesn't exist) still get the same shortcut. Was Cmd+T but
        # Chrome reserves that at the window-manager level on macOS.
        self.assertRegex(self.serve,
            r"\(\s*e\.metaKey\s*\|\|\s*e\.ctrlKey\s*\)[^;]*e\.key\s*===\s*['\"]e['\"]",
            "chrome keydown must spawn-terminal on Cmd/Ctrl+E")
        self.assertIn("spawnTile('terminal')", self.serve)

    def test_chrome_does_not_handle_cmd_t_anymore(self):
        # Chrome on macOS intercepts Cmd+T at the WM level — wiring a handler
        # for it is misleading (UI hint would lie about working). The handler
        # must not check for e.key === 't' / 'T' on the spawn path.
        self.assertNotRegex(self.serve,
            r"\(\s*e\.metaKey\s*\|\|\s*e\.ctrlKey\s*\)[^;]*e\.key\s*===\s*['\"]t['\"]",
            "chrome must not handle Cmd/Ctrl+T (Chrome reserves it; the UI would lie)")

    def test_chrome_routes_forwarded_keys_to_correct_action(self):
        # The postMessage receiver maps {key:'ctrl-q'}→armOrClose,
        # {key:'cmd-e'}→spawnTile, and {key:'cmd-left'|'cmd-right'}→moveTile.
        # (Was 'ctrl-x' / 'cmd-t' before.)
        self.assertIn("d.key === 'ctrl-q'", self.serve)
        self.assertIn("d.key === 'cmd-e'", self.serve)
        self.assertIn("d.key === 'cmd-left'", self.serve)
        self.assertIn("d.key === 'cmd-right'", self.serve)
        self.assertNotIn("d.key === 'ctrl-x'", self.serve)
        self.assertNotIn("d.key === 'cmd-t'", self.serve)

    def test_chrome_keydown_moves_tile_on_cmd_arrows(self):
        # Cmd/Ctrl + ArrowLeft / ArrowRight (no alt, no shift) → moveTile.
        # The chord predicate must check BOTH ArrowLeft and ArrowRight in
        # the same handler block, AND call moveTile with -1 / +1 based on
        # which arrow it was. preventDefault must run inside that block too
        # so Chrome's default history-back/forward doesn't leave the page.
        m = re.search(
            r"\(\s*e\.metaKey\s*\|\|\s*e\.ctrlKey\s*\)[^{]*"
            r"e\.key\s*===\s*['\"]ArrowLeft['\"][^{]*"
            r"e\.key\s*===\s*['\"]ArrowRight['\"][^{]*\)\s*\{",
            self.serve)
        self.assertIsNotNone(m,
            "chrome must have a Cmd/Ctrl+Arrow chord handler "
            "(checks both ArrowLeft and ArrowRight in the same predicate)")
        assert m is not None
        block = self.serve[m.end():m.end() + 400]
        self.assertIn("e.preventDefault()", block,
            "Cmd+Arrow handler must preventDefault so Chrome's history "
            "back/forward doesn't fire")
        self.assertIn("moveTile(", block,
            "Cmd+Arrow handler must call moveTile")
        # Verify direction wiring: ArrowLeft → -1, ArrowRight → +1.
        self.assertRegex(block,
            r"e\.key\s*===\s*['\"]ArrowLeft['\"][^?]*\?\s*-1\s*:\s*1",
            "ArrowLeft must map to dir=-1, ArrowRight to dir=+1")
        # And moveTile itself must exist with the expected signature.
        self.assertRegex(self.serve, r"function moveTile\s*\(\s*id\s*,\s*dir\s*\)\s*\{",
            "moveTile(id, dir) helper must be defined")

    def test_chrome_move_tile_skips_other_tabs(self):
        # moveTile must hop over orderList entries in a DIFFERENT tab when
        # searching for the swap partner — without it, Cmd+→ inside a tab
        # could swap the selected tile with an invisible neighbor and the
        # user sees no movement on screen.
        m = re.search(r"function moveTile\s*\([^)]*\)\s*\{", self.serve)
        self.assertIsNotNone(m); assert m is not None
        # Slice the function body by walking braces.
        depth, end = 1, len(self.serve)
        for i in range(m.end(), len(self.serve)):
            c = self.serve[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        body = self.serve[m.end():end]
        # Tab-skip loop check (walks until same-tab neighbor found).
        self.assertIn("dataset.tab", body,
            "moveTile must compare tiles by dataset.tab to skip foreign-tab entries")
        # Bounds check so we don't fall off the start/end of orderList.
        self.assertRegex(body, r"j\s*<\s*0\s*\|\|\s*j\s*>=\s*orderList\.length",
            "moveTile must bail when there's no neighbor in this tab")
        # Must save the new order so a reload preserves it (orderList is the
        # canonical persisted shape; a swap that doesn't save would silently
        # revert on next dashboard reload).
        self.assertIn("saveOrder()", body,
            "moveTile must persist via saveOrder() after swapping")

    def _move_tile_body(self):
        m = re.search(r"function moveTile\s*\([^)]*\)\s*\{", self.serve)
        self.assertIsNotNone(m); assert m is not None
        depth, end = 1, len(self.serve)
        for i in range(m.end(), len(self.serve)):
            c = self.serve[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        return self.serve[m.end():end]

    def test_chrome_move_tile_scrolls_to_follow(self):
        # After a Cmd+Arrow move, the tile can land off-screen in the
        # horizontal row. moveTile must scroll it into view so the move is
        # visible (user-reported: "the view should follow the tile"). It must
        # also releasePin() first — otherwise the startup scroll-pin (which
        # snaps the row back to scrollLeft 0 until the first user gesture)
        # would cancel the follow-scroll when a move happens right after a
        # reload.
        body = self._move_tile_body()
        self.assertIn("releasePin()", body,
            "moveTile must releasePin() so the startup left-pin doesn't undo "
            "the follow-scroll")
        self.assertRegex(body, r"\.scrollIntoView\(",
            "moveTile must scrollIntoView the moved tile so the row follows it")
        # 'nearest' inline keeps the scroll minimal (don't yank a visible tile);
        # smooth makes the follow read as a glide, not a jump.
        m = re.search(r"\.scrollIntoView\(\s*\{[^}]*\}", body)
        self.assertIsNotNone(m, "scrollIntoView must pass an options object")
        assert m is not None
        opts = m.group(0)
        self.assertIn("inline", opts)
        self.assertIn("'nearest'", opts)
        self.assertIn("behavior", opts)
        self.assertIn("'smooth'", opts)

    def test_chrome_row_pins_to_left_on_load(self):
        # After a (re)load the horizontal row must START on the left and stay
        # there until the user actually scrolls. A terminal iframe grabbing
        # focus on connect (or the browser restoring focus to the terminal
        # focused before the reload) auto-scrolls the row to that tile — so a
        # refresh otherwise lands on a "random" tile and a correctly-persisted
        # order LOOKS wrong. The pin reverts the focus-scroll; the first real
        # gesture lifts it. Native scroll restoration is disabled too (it would
        # re-apply a stale horizontal offset on reload).
        self.assertRegex(self.serve,
            r"history\.scrollRestoration\s*=\s*['\"]manual['\"]",
            "row-left behavior needs history.scrollRestoration='manual' so the "
            "browser can't re-apply a stale horizontal scroll offset on reload")
        # The pin flag + its release helper.
        self.assertRegex(self.serve, r"\bpinLeft\s*=\s*true",
            "must arm a pinLeft flag on load")
        self.assertRegex(self.serve,
            r"function releasePin\s*\(\s*\)\s*\{[^}]*pinLeft\s*=\s*false",
            "releasePin() must clear the pin")
        # A grid scroll handler that snaps back to 0 while pinned. (Non-greedy
        # any-char spans: the arrow `() =>` contains a ')' so a [^)] class would
        # truncate before reaching the body.)
        self.assertRegex(self.serve,
            r"grid\.addEventListener\(\s*['\"]scroll['\"][\s\S]{0,80}?pinLeft[\s\S]{0,60}?grid\.scrollLeft\s*=\s*0",
            "while pinned, a grid 'scroll' handler must force scrollLeft back to 0")
        # Genuine user gestures release the pin (so we never fight real
        # scrolling). Must wire releasePin and cover at least wheel + a
        # pointer + a keyboard gesture.
        self.assertRegex(self.serve, r"addEventListener\([^)]*releasePin",
            "user gestures must be wired to releasePin")
        for g in ("'wheel'", "'pointerdown'", "'keydown'"):
            self.assertIn(g, self.serve,
                "%s must release the startup scroll-pin" % g)

    def test_chrome_tab_switch_opens_at_left(self):
        # Switching tabs should also start at the leftmost tile of the new tab,
        # not wherever the previous tab happened to be scrolled. The tab button
        # onclick must reset grid.scrollLeft to 0.
        # Anchor on the TAB button's onclick specifically (its body sets
        # `activeTab = k`) — there are other `b.onclick = () => {` handlers
        # (e.g. the channels menu) that must not be matched here.
        m = re.search(r"b\.onclick\s*=\s*\(\s*\)\s*=>\s*\{\s*activeTab\s*=\s*k\b", self.serve)
        self.assertIsNotNone(m, "tab button onclick handler not found")
        assert m is not None
        depth, end = 1, len(self.serve)
        for i in range(m.end(), len(self.serve)):
            c = self.serve[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        body = self.serve[m.end():end]
        self.assertRegex(body, r"grid\.scrollLeft\s*=\s*0",
            "tab switch must reset grid.scrollLeft to 0 so the new tab opens "
            "at its leftmost tile")

    def test_chrome_spawn_tile_registers_pending_dup_for_placement(self):
        # spawnTile must push a pendingDups entry with the focused tile as
        # srcId, so placeNewInOrder splices the new session right after it.
        # Without this the new tile lands at the end of the row, which feels
        # backward for "Cmd+E to open another shell next to this one".
        m = re.search(r"async function spawnTile\s*\([^)]*\)\s*\{", self.serve)
        self.assertIsNotNone(m); assert m is not None
        depth, end = 1, len(self.serve)
        for i in range(m.end(), len(self.serve)):
            c = self.serve[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        body = self.serve[m.end():end]
        self.assertIn("selectedId", body,
            "spawnTile must read selectedId to pick the placement anchor")
        self.assertIn("pendingDups.push(", body,
            "spawnTile must register a pendingDups entry so placeNewInOrder "
            "splices the new tile after the focused one")
        self.assertRegex(body, r"srcId:\s*srcId",
            "pendingDups entry must carry srcId (placeNewInOrder reads it)")

    def test_chrome_help_strings_mention_new_chords(self):
        # The header hint + the armed-tile badge must show Cmd+E / Ctrl+Q,
        # not the stale Ctrl+X / Cmd+T. (UI text is the user-visible source
        # of truth for the binding — if it drifts from the handler, users
        # will press the wrong key forever.)
        self.assertIn("Ctrl+Q", self.serve)
        self.assertIn("Cmd+E", self.serve)
        # The retired chords must not appear in any user-visible string.
        # Comments referencing history are allowed (they live on lines that
        # also contain "//" or "#"); flag only bare visible occurrences.
        for stale in ("Ctrl+X", "Cmd+T"):
            for line in self.serve.splitlines():
                if stale not in line:
                    continue
                stripped = line.lstrip()
                self.assertTrue(
                    stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("*"),
                    "stale %r in user-visible string: %r" % (stale, line))

    # ---- terminal client (term-client.js + built term.html) ----

    def test_term_client_forwards_ctrl_q_and_cmd_e(self):
        # The iframe-side capture-phase keydown handler must post
        # {key:'ctrl-q'} for Ctrl+Q and {key:'cmd-e'} for Cmd+E, and must
        # NOT still post the retired {key:'ctrl-x'} / {key:'cmd-t'} (the
        # chrome no longer handles them, so xterm would receive a useless
        # preventDefault'd key for those chords).
        for label, src in (("js", self.js), ("html", self.html)):
            self.assertIn("'ctrl-q'", src, "%s: missing ctrl-q forward" % label)
            self.assertIn("'cmd-e'", src, "%s: missing cmd-e forward" % label)
            self.assertNotIn("'ctrl-x'", src,
                "%s: stale ctrl-x forward (collides with nano exit inside the tile)" % label)
            self.assertNotIn("'cmd-t'", src,
                "%s: stale cmd-t forward (Chrome reserves Cmd+T; chrome no longer handles it)" % label)

    def test_term_client_uses_capture_phase_so_xterm_doesnt_swallow_first(self):
        # The forwarder lives only when embedded (window.parent !== window)
        # and registers with the `true` capture flag so xterm's own keydown
        # listener doesn't get the key first.
        self.assertRegex(self.js,
            r"addEventListener\(\s*['\"]keydown['\"][\s\S]*?,\s*true\s*\)",
            "term-client must register the dashboard-chord forwarder in capture phase")

    def test_term_client_does_not_clobber_meta_e_when_shift_is_held(self):
        # The plain Cmd+E branch must check `!e.shiftKey` so it doesn't
        # eat the Cmd+Shift+E refresh chord (which is a strict superset of
        # its modifier predicate). Find the FIRST cmd-e branch — the one
        # without shiftKey — and assert the exclusion.
        m = re.search(
            r"\(\s*e\.metaKey\s*\|\|\s*e\.ctrlKey\s*\)[^;{}]*e\.key\s*===\s*['\"]e['\"]",
            self.js)
        self.assertIsNotNone(m, "plain cmd-e branch not found in term-client.js")
        assert m is not None
        window = self.js[max(0, m.start()-200):m.end()+50]
        self.assertIn("!e.shiftKey", window,
            "plain cmd-e handler must not fire when Shift is held — otherwise "
            "it would intercept the Cmd+Shift+E refresh chord")

    # ---- Cmd+Shift+E refresh chord ----

    def test_chrome_handles_cmd_shift_e_for_refresh(self):
        # The document keydown must check Cmd/Ctrl + Shift + E and call
        # doRefresh on the selected tile. Must come BEFORE the plain cmd-e
        # branch in source order so the shift'd chord is detected first
        # (otherwise the plain handler would intercept it). Static order
        # check: refresh-branch start offset < spawn-branch start offset.
        refresh = re.search(
            r"e\.metaKey\s*\|\|\s*e\.ctrlKey[^{}]*e\.shiftKey[^{}]*e\.key\s*===\s*['\"]E['\"]",
            self.serve)
        spawn = re.search(
            r"\(\s*e\.metaKey\s*\|\|\s*e\.ctrlKey\s*\)\s*&&\s*!e\.altKey\s*&&\s*!e\.shiftKey[^;]*e\.key\s*===\s*['\"]e['\"]",
            self.serve)
        self.assertIsNotNone(refresh, "Cmd+Shift+E refresh branch not found")
        self.assertIsNotNone(spawn, "plain Cmd+E spawn branch not found")
        assert refresh is not None and spawn is not None
        self.assertLess(refresh.start(), spawn.start(),
            "Cmd+Shift+E branch must come before plain Cmd+E branch — "
            "otherwise the plain handler swallows the shifted chord")
        self.assertIn("doRefresh", self.serve,
            "doRefresh helper missing from chrome")

    def test_chrome_refresh_posts_message_to_iframe(self):
        # doRefresh must postMessage {cmd:'refresh'} to the selected tile's
        # iframe — that's how the message arrives at the embedded
        # term-client. Origin '*' is acceptable here because the embedded
        # client validates `e.origin.indexOf('http://127.0.0.1:') === 0`
        # and the message has no sensitive payload.
        m = re.search(r"function doRefresh\s*\([^)]*\)\s*\{", self.serve)
        self.assertIsNotNone(m, "doRefresh definition not found")
        assert m is not None
        # Walk braces for the body.
        start = m.end(); depth, end = 1, len(self.serve)
        for i in range(start, len(self.serve)):
            c = self.serve[i]
            if c == '{': depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0: end = i; break
        body = self.serve[start:end]
        self.assertRegex(body, r"postMessage\(\s*\{[^}]*cmd:\s*['\"]refresh['\"]",
            "doRefresh must postMessage {cmd:'refresh'} to the iframe")

    def test_chrome_routes_forwarded_cmd_shift_e_to_refresh(self):
        # When Cmd+Shift+E is pressed inside a focused terminal iframe, the
        # client forwards {key:'cmd-shift-e', sid: …} up. The chrome must
        # route that to doRefresh on THAT sid (not selectedId — the user's
        # focus is the source-of-truth in that case).
        self.assertRegex(self.serve,
            r"d\.key\s*===\s*['\"]cmd-shift-e['\"][^;]*doRefresh\(d\.sid\)",
            "forwarded cmd-shift-e must route to doRefresh(d.sid)")

    def test_term_client_forwards_cmd_arrows_for_reorder(self):
        # The iframe-side handler must post {key:'cmd-left'} on Cmd/Ctrl+←
        # and {key:'cmd-right'} on Cmd/Ctrl+→ — only when no shift/alt is
        # held (shift+arrow is text-selection in xterm; alt+arrow is
        # readline word-jump). Both source and built HTML must match.
        for label, src in (("js", self.js), ("html", self.html)):
            # Find the Cmd/Ctrl + arrow chord block (predicate that checks
            # both ArrowLeft and ArrowRight in the same condition). Slice
            # the block body up to its closing brace.
            m = re.search(
                r"\(\s*e\.metaKey\s*\|\|\s*e\.ctrlKey\s*\)[^{]*"
                r"e\.key\s*===\s*['\"]ArrowLeft['\"][^{]*"
                r"e\.key\s*===\s*['\"]ArrowRight['\"][^{]*\)\s*\{",
                src)
            self.assertIsNotNone(m,
                "%s: Cmd/Ctrl+Arrow chord block not found (must test BOTH "
                "ArrowLeft and ArrowRight in one predicate)" % label)
            assert m is not None
            block = src[m.end():m.end() + 500]
            self.assertIn("preventDefault", block,
                "%s: arrow chord must preventDefault to block Chrome history" % label)
            self.assertIn("'cmd-left'", block,
                "%s: arrow chord block must forward 'cmd-left'" % label)
            self.assertIn("'cmd-right'", block,
                "%s: arrow chord block must forward 'cmd-right'" % label)
            # And the mapping must be ArrowLeft→cmd-left (not flipped — easy
            # off-by-one in a ternary).
            self.assertRegex(block,
                r"e\.key\s*===\s*['\"]ArrowLeft['\"][^?]*\?\s*['\"]cmd-left['\"]",
                "%s: ArrowLeft must map to 'cmd-left' (not 'cmd-right')" % label)

    def test_term_client_intercepts_cmd_shift_e_and_forwards(self):
        # Iframe-side capture-phase handler must intercept Cmd/Ctrl+Shift+E,
        # preventDefault + stopPropagation, and post {key:'cmd-shift-e'} up.
        for label, src in (("js", self.js), ("html", self.html)):
            self.assertIn("'cmd-shift-e'", src,
                "%s: missing cmd-shift-e forward" % label)
            # The handler block must include shiftKey TRUE alongside meta/ctrl.
            self.assertRegex(src,
                r"e\.shiftKey[^;{}]*e\.key\s*===\s*['\"]E['\"]",
                "%s: cmd-shift-e branch must require shiftKey" % label)

    def test_term_client_refresh_handler_heals_without_sigwinch(self):
        # The inbound {cmd:'refresh'} handler must do three things, in order:
        #   1. fit.fit() — re-sync cols/rows from the container box (fires a
        #      PTY SIGWINCH only on a REAL size change).
        #   2. _healAfterRendererSwap() — recompute the renderer's canvas/
        #      framebuffer dims from a fresh cell measurement and re-blit every
        #      row, driven straight through the render service so it does NOT
        #      fire term.onResize. This replaced the old `term.resize(c+1,r);
        #      term.resize(c,r)` trick, which fired TWO SIGWINCHes: a normal-
        #      buffer TUI (claude/Ink) re-rendered its frame twice at two widths
        #      and a miscounted frame-erase left the SAME output duplicated with
        #      mismatched wrapping (the dup b30d3cb removed from the renderer-
        #      swap path — same hazard, so the manual refresh must avoid it too).
        #   3. term.refresh(0, …) — belt-and-suspenders for the case where
        #      cols/rows were 0 before fit.fit() set them.
        for label, src in (("js", self.js), ("html", self.html)):
            m = re.search(r"d\.cmd\s*===\s*['\"]refresh['\"]", src)
            self.assertIsNotNone(m, "%s: refresh branch not found" % label)
            assert m is not None
            # Generous window — the handler has a multi-line comment.
            window = src[m.end():m.end() + 3600]
            fit_pos = window.find("fit.fit()")
            heal_pos = window.find("_healAfterRendererSwap()")
            # Match the actual call (with the `0` first-arg), not the
            # bare "term.refresh" string used in the prose comment above.
            ref_match = re.search(r"term\.refresh\(\s*0\b", window)
            ref_pos = ref_match.start() if ref_match else -1
            self.assertGreaterEqual(fit_pos, 0,
                "%s: refresh handler must call fit.fit() to re-sync cols/rows" % label)
            self.assertGreaterEqual(heal_pos, 0,
                "%s: refresh handler must call _healAfterRendererSwap() (the "
                "no-SIGWINCH re-blit that replaced the resize trick)" % label)
            self.assertGreaterEqual(ref_pos, 0,
                "%s: refresh handler must call term.refresh() as a fallback" % label)
            self.assertLess(fit_pos, heal_pos,
                "%s: fit.fit() must run BEFORE the heal" % label)
            self.assertLess(heal_pos, ref_pos,
                "%s: term.refresh() must run AFTER the heal" % label)

    def test_term_client_refresh_handler_has_no_sigwinch_resize_trick(self):
        # Regression guard: the double-resize trick `term.resize(c+1,r);
        # term.resize(c,r)` must NOT come back into the refresh handler. It
        # fires two SIGWINCHes and makes a normal-buffer TUI duplicate its
        # frame at mismatched widths — exactly what _healAfterRendererSwap()
        # was introduced to avoid. (_healAfterRendererSwap drives the renderer
        # service directly and never calls term.resize, so no resize call
        # should appear in the handler window at all.)
        for label, src in (("js", self.js), ("html", self.html)):
            m = re.search(r"d\.cmd\s*===\s*['\"]refresh['\"]", src)
            assert m is not None
            window = src[m.end():m.end() + 3600]
            self.assertNotRegex(window, r"term\.resize\(",
                "%s: refresh handler must NOT call term.resize() — the "
                "SIGWINCH resize trick causes TUI frame duplication; use "
                "_healAfterRendererSwap() instead" % label)

    # ---- bell-on-prompt (claude permission/yes-no detection) ----

    def test_term_client_has_prompt_bell_pattern(self):
        # The fallback path scans incoming output for claude's prompt
        # sigils. The regex must cover the common openers so users who
        # haven't installed the Notification hook still get a bell.
        for label, src in (("js", self.js), ("html", self.html)):
            self.assertIn("PROMPT_RE", src, "%s: missing PROMPT_RE" % label)
            # Must cover at least the two highest-recall openers.
            self.assertIn("Do you want to", src,
                "%s: PROMPT_RE must match 'Do you want to ...' prompts" % label)
            # The JS regex literal escapes its parens & forward slashes:
            # the source contains `\(y\/n\)` (each paren and slash preceded
            # by a backslash inside the regex literal). Match that literal
            # 8-char sequence.
            self.assertRegex(src,
                r"\\\(y\\/n\\\)|\\\[y\\/N\\\]|\\\[Y\\/n\\\]",
                "%s: PROMPT_RE must match the (y/n) / [y/N] / [Y/n] sigils" % label)

    def test_term_client_bell_path_dedups(self):
        # The hook and the pattern can fire on the same prompt — debounce
        # via a single `bellOnce` gate so the tile doesn't bell twice.
        for label, src in (("js", self.js), ("html", self.html)):
            self.assertIn("bellOnce", src, "%s: missing bellOnce dedup gate" % label)
            self.assertIn("BELL_DEDUP_MS", src,
                "%s: missing BELL_DEDUP_MS constant" % label)

    def test_term_client_bel_char_goes_through_bellonce(self):
        # The hook's \x07 is detected by scanBel — a SYNCHRONOUS scan of each
        # decoded websocket chunk — and must go through bellOnce so it dedups
        # against the pattern-match fallback firing on the same prompt.
        # Behavioural coverage (OSC/DCS skipping, connect mute, sigil
        # consumption) lives in test_term_bell.js.
        for label, src in (("js", self.js), ("html", self.html)):
            m = re.search(r"function scanBel\s*\([^)]*\)\s*\{", src)
            self.assertIsNotNone(m, "%s: scanBel not found" % label)
            assert m is not None
            body = src[m.end():m.end() + 1200]
            self.assertIn("bellOnce", body,
                "%s: scanBel must call bellOnce (not postBell directly) so the "
                "BEL → bell path dedups against the pattern-match fallback" % label)
            # There must be NO term.onBell handler: xterm drains its write
            # queue via setTimeout, which Chrome throttles in backgrounded
            # tabs / hidden cross-origin iframes to once a minute — bells rang
            # minutes late, and a delayed duplicate ring for the same byte
            # would escape the 600ms dedup window.
            self.assertNotRegex(src, r"term\.onBell\s*\(",
                "%s: no term.onBell handler — BEL detection is scanBel's job "
                "(synchronous, throttle-immune)" % label)

    def test_term_client_postbell_posts_bell_message(self):
        # The end of the propagation chain on the client side: bellOnce → postBell
        # must postMessage {bell:true} up to the dashboard. The full round-trip
        # (incl. alt-screen + mouse mode) is exercised by
        # ScrollbackE2ETest.test_bel_propagates_to_dashboard_under_mouse_mode.
        for label, src in (("js", self.js), ("html", self.html)):
            self.assertRegex(src, r"function postBell\(\)\s*\{\s*post\(\s*\{\s*bell:\s*true\s*\}\s*\)",
                "%s: postBell must post {bell:true} to the dashboard" % label)

    def test_term_client_pattern_runs_on_each_data_chunk(self):
        # The cmd==0 (data) branch must route through writeOutput (which
        # classifies the first replay burst for restore-vs-dump), and the
        # terminal write + maybePromptBell scan must happen for every chunk
        # via _writeAndScan. If the scan only ran on title/prefs cmds (1/2)
        # we'd never see live prompt text.
        m = re.search(r"if \(cmd === '0'\)\s*\{([^}]*)\}", self.js)
        self.assertIsNotNone(m, "ttyd cmd==0 handler not found")
        assert m is not None
        self.assertIn("writeOutput(payload)", m.group(1),
            "cmd==0 must route output through writeOutput (first-burst classifier)")
        # _writeAndScan is the single place that writes to the terminal AND
        # feeds the bell scanner — both must be present.
        w = re.search(r"function _writeAndScan\s*\([^)]*\)\s*\{([\s\S]*?)\n  \}", self.js)
        self.assertIsNotNone(w, "_writeAndScan helper not found")
        assert w is not None
        self.assertIn("term.write(payload)", w.group(1),
            "_writeAndScan must write to the terminal")
        self.assertIn("maybePromptBell", w.group(1),
            "_writeAndScan must feed maybePromptBell so the fallback can scan every chunk")

    def test_term_client_bell_scan_uses_streaming_decoder(self):
        # ttyd splits frames at arbitrary byte offsets, so claude's 3-byte
        # selection-menu arrow `❯` (U+276F) can straddle a frame boundary. A
        # stateless decode emits U+FFFD for the split halves and the `❯ N.` bell
        # pattern never matches → missed bell on multi-option prompts. The
        # output→bell scan must feed maybePromptBell from a STREAMING decoder
        # (`{stream:true}`), distinct from the stateless `dec` used for the
        # independent title/prefs frames (sharing one would splice their bytes).
        for label, src in (("js", self.js), ("html", self.html)):
            # A dedicated decoder distinct from `dec` must exist.
            self.assertRegex(src, r"\boutDec\s*=\s*new TextDecoder\(",
                "%s: missing a dedicated streaming output decoder (outDec)" % label)
            # _writeAndScan must decode the chunk with stream:true, NOT the old
            # stateless `dec.decode(payload)`.
            w = re.search(r"function _writeAndScan\s*\([^)]*\)\s*\{([\s\S]*?)\n  \}", src)
            self.assertIsNotNone(w, "%s: _writeAndScan helper not found" % label)
            assert w is not None
            body = w.group(1)
            self.assertRegex(body,
                r"maybePromptBell\(\s*outDec\.decode\(\s*payload\s*,\s*\{\s*stream:\s*true\s*\}\s*\)\s*\)",
                "%s: the bell scan must decode output with outDec.decode(payload, "
                "{stream:true}) so a `❯` split across frames still matches" % label)
            self.assertNotRegex(body, r"maybePromptBell\(\s*dec\.decode\(",
                "%s: bell scan must NOT use the stateless `dec` (drops split "
                "multi-byte glyphs to U+FFFD)" % label)
            # The streaming decoder must be reset on (re)connect so a partial
            # byte dangling at a disconnect can't corrupt the fresh stream.
            on = re.search(r"socket\.onopen\s*=\s*function[^{]*\{([\s\S]*?)\n    \}", src)
            self.assertIsNotNone(on, "%s: socket.onopen not found" % label)
            assert on is not None
            self.assertRegex(on.group(1), r"outDec\.decode\(\s*\)",
                "%s: onopen must reset outDec (outDec.decode() with no args "
                "flushes any dangling partial byte from the prior connection)" % label)

    def test_term_client_pattern_strips_ansi_before_match(self):
        # SGR sequences in claude's output would split prompt text with
        # invisible bytes. The matcher must strip CSI and OSC escapes
        # before testing the regex, or false negatives bury real prompts.
        m = re.search(r"function maybePromptBell\s*\([^)]*\)\s*\{", self.js)
        self.assertIsNotNone(m); assert m is not None
        start = m.end(); depth, end = 1, len(self.js)
        for i in range(start, len(self.js)):
            c = self.js[i]
            if c == '{': depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0: end = i; break
        body = self.js[start:end]
        # Strip pattern must remove BOTH CSI (\x1b[...letter) AND OSC
        # (\x1b]...\x07 or \x1b\).
        self.assertIn("\\x1b\\[", body,
            "maybePromptBell must strip CSI sequences before regex test")
        self.assertIn("\\x1b\\]", body,
            "maybePromptBell must strip OSC sequences before regex test")

    def test_term_client_bell_dedup_window_is_small(self):
        # The dedup window's only job is to merge the hook BEL and the PROMPT_RE
        # pattern match for ONE event (they arrive within ms). It must stay small:
        # the old 4000ms value also swallowed deliberate, distinct bells (a
        # finished turn then a quick permission prompt, or hammering `printf '\a'`
        # in a terminal tile to test) and read as "the bell only works half the
        # time". Guard against the window creeping back up.
        for label, src in (("js", self.js), ("html", self.html)):
            m = re.search(r"BELL_DEDUP_MS\s*=\s*(\d+)", src)
            self.assertIsNotNone(m, "%s: BELL_DEDUP_MS constant not found" % label)
            assert m is not None
            self.assertLessEqual(int(m.group(1)), 1000,
                "%s: BELL_DEDUP_MS must stay <= 1000ms so deliberate back-to-back "
                "bells aren't swallowed (got %s)" % (label, m.group(1)))

    # ---- drag & drop / paste: upload to /api/dropfile, type returned path ----

    def test_term_client_dragover_calls_preventdefault(self):
        # Without preventDefault on dragover, the drop event NEVER FIRES
        # and the browser opens the file in a new tab. Easiest silent
        # regression in this whole feature.
        for label, src in (("js", self.js), ("html", self.html)):
            body = self._slice_js_function(
                r"addEventListener\(\s*['\"]dragover['\"]\s*,\s*function[^{]*\{", src)
            self.assertIn("e.preventDefault()", body,
                "%s: dragover must call preventDefault, "
                "or the drop event will never fire" % label)

    def test_term_client_drop_handler_uploads_files(self):
        # The drop handler must (a) preventDefault to suppress the browser's
        # open-file default, (b) reject empty drops (no .files), (c) hand the
        # FileList to _uploadFilesAndTypePaths which POSTs to /api/dropfile.
        # We deliberately don't read uri-list anymore: Chrome doesn't
        # populate it for Finder→browser drag, so the only reliable way to
        # get the file is to upload its bytes and have the server hand us a
        # path back.
        for label, src in (("js", self.js), ("html", self.html)):
            body = self._slice_js_function(
                r"addEventListener\(\s*['\"]drop['\"]\s*,\s*function[^{]*\{", src)
            self.assertIn("e.preventDefault()", body,
                "%s: drop must preventDefault when files land" % label)
            self.assertRegex(body, r"e\.dataTransfer\.files",
                "%s: drop must read dataTransfer.files" % label)
            self.assertIn("_uploadFilesAndTypePaths(", body,
                "%s: drop must call _uploadFilesAndTypePaths (the upload-then-type pipeline)" % label)

    def test_term_client_paste_uploads_files_else_passthrough(self):
        # File paste → upload + type, same as drop. Plain text paste must
        # STILL flow through to xterm (must return WITHOUT preventDefault
        # when clipboardData.files is empty). Capture phase required so
        # xterm's textarea listener doesn't beat us to the event.
        for label, src in (("js", self.js), ("html", self.html)):
            body = self._slice_js_function(
                r"addEventListener\(\s*['\"]paste['\"]\s*,\s*function[^{]*\{", src)
            self.assertIn("clipboardData", body,
                "%s: paste must read clipboardData" % label)
            self.assertRegex(body, r"clipboardData\.files",
                "%s: paste must inspect clipboardData.files "
                "(text-only paste leaves files empty and must pass through)" % label)
            self.assertIn("_uploadFilesAndTypePaths(", body,
                "%s: paste must upload via _uploadFilesAndTypePaths when files are present" % label)
            # The early-return for non-file paste is what keeps plain text
            # paste working — without it, preventDefault would fire on every
            # paste and xterm would never see clipboard text.
            self.assertRegex(body, r"if\s*\(\s*!e\.clipboardData[^{]*!e\.clipboardData\.files",
                "%s: paste must early-return (no preventDefault) when no "
                "files are on the clipboard, so text paste still reaches xterm" % label)
        # Capture phase check: brace-walk to the closing brace, then peek
        # at the next characters for `, true)`.
        m = re.search(
            r"addEventListener\(\s*['\"]paste['\"]\s*,\s*function[^{]*\{", self.js)
        self.assertIsNotNone(m, "paste handler not found"); assert m is not None
        depth, end = 1, len(self.js)
        for i in range(m.end(), len(self.js)):
            c = self.js[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        tail = self.js[end:end + 30]
        self.assertRegex(tail, r"^\s*,\s*true\s*\)",
            "paste must use capture phase (3rd arg = true), got tail %r — "
            "otherwise xterm's textarea paste fires first" % tail)

    def test_quote_shell_arg_uses_posix_safe_escape(self):
        # POSIX-safe single-quote wrap with `'\''` escape — anything else
        # breaks paths containing single quotes (e.g. "Bob's notes.txt") or
        # opens shell-injection holes if metacharacters slip through.
        m = re.search(r"function _quoteShellArg\s*\([^)]*\)\s*\{[^}]*\}", self.js)
        self.assertIsNotNone(m, "_quoteShellArg() not found")
        assert m is not None
        body = m.group(0)
        self.assertRegex(body, r"replace\(\s*/'/g\s*,\s*['\"]'\\\\''['\"]\s*\)",
            "_quoteShellArg must escape ' as '\\'' (POSIX-safe nested quote)")
        self.assertRegex(body, r"return\s+['\"]'['\"]\s*\+",
            "_quoteShellArg must wrap in SINGLE quotes (double quotes still expand $vars)")

    def test_upload_helper_uses_csrf_and_dashboard_origin(self):
        # _uploadFile must POST to <dashboardOrigin>/api/dropfile with the
        # CSRF header and the File as the body. Without either the upload
        # 403s (csrf) or the server can't read the bytes (no body / wrong
        # encoding). Cross-origin so mode:'cors' + credentials:'omit'.
        body = self._slice_js_function(r"function _uploadFile\s*\(\s*file\s*\)\s*\{")
        self.assertIn("_csrfToken", body,
            "_uploadFile must bail when _csrfToken hasn't arrived yet")
        self.assertIn("_dashboardOrigin", body,
            "_uploadFile must use _dashboardOrigin (cross-origin POST target)")
        self.assertIn("/api/dropfile?sid=", body,
            "_uploadFile must hit /api/dropfile with sid in query")
        self.assertRegex(body, r"['\"]X-CSRF-Token['\"]\s*:\s*_csrfToken",
            "_uploadFile must send the CSRF token as X-CSRF-Token header")
        self.assertRegex(body, r"body:\s*file\b",
            "_uploadFile must put the File object in the body (browser streams it)")
        self.assertRegex(body, r"mode:\s*['\"]cors['\"]",
            "_uploadFile must use mode:'cors' since dashboard is a different origin")

    def test_csrf_handler_stores_token_and_dashboard_origin(self):
        # When the dashboard postMessages {cmd:'csrf', token}, the iframe
        # must (a) verify e.origin is http://127.0.0.1:* OR our own origin (the
        # /t/<port>/ reverse-proxy case, where dashboard and tile share an
        # origin), (b) once _dashboardOrigin is pinned, REJECT messages from any
        # other origin, (c) only then store the token AND _dashboardOrigin
        # atomically. Storing d.token without an origin check would let any
        # localhost service inject a fake token (uploads would all fail with
        # 403, not catastrophic but a real DoS vector).
        m = re.search(r"d\.cmd\s*===\s*['\"]csrf['\"]", self.js)
        self.assertIsNotNone(m, "csrf handler not found"); assert m is not None
        block = self.js[m.end():m.end() + 1200]
        self.assertIn("_csrfToken = d.token", block,
            "csrf handler must store d.token in _csrfToken")
        self.assertIn("_dashboardOrigin = e.origin", block,
            "csrf handler must capture e.origin as _dashboardOrigin")
        # Origin gating MUST appear BEFORE the token assignment. Find both
        # positions and assert order. The gate rejects unless the sender is
        # 127.0.0.1:* (direct embed) or our own origin (proxied embed).
        gate = re.search(
            r"e\.origin\.indexOf\(\s*['\"]http://127\.0\.0\.1:['\"]\s*\)\s*!==\s*0\s*"
            r"&&\s*e\.origin\s*!==\s*location\.origin\s*\)\s*\)\s*return",
            block)
        self.assertIsNotNone(gate,
            "csrf handler must early-return on origins that are neither "
            "127.0.0.1:* nor same-origin (defense-in-depth: outer router gates "
            "too, but the token write needs its own check)")
        pin = re.search(
            r"_dashboardOrigin\s*&&\s*e\.origin\s*!==\s*_dashboardOrigin\s*\)\s*return",
            block)
        self.assertIsNotNone(pin,
            "csrf handler must early-return when _dashboardOrigin is already "
            "pinned and e.origin doesn't match — prevents lateral attacks "
            "from other 127.0.0.1:<port> services")
        # Token-write must come AFTER both gates.
        assert gate is not None and pin is not None
        token_at = block.find("_csrfToken = d.token")
        self.assertGreater(token_at, gate.start(),
            "token write must come AFTER the origin-prefix gate")
        self.assertGreater(token_at, pin.start(),
            "token write must come AFTER the pinned-origin gate")

    def test_chrome_pushes_csrf_to_iframe_on_ready(self):
        # The dashboard's ready handler must postMessage cmd:'csrf' with the
        # CSRF token, alongside the input-gate + font pushes. Without this
        # the iframe never gets a token and uploads all fail.
        m = re.search(r"if\s*\(\s*d\.ready\s*\)", self.serve)
        self.assertIsNotNone(m, "ready handler not found"); assert m is not None
        win = self.serve[m.end():m.end() + 2200]
        self.assertRegex(win, r"cmd:\s*['\"]csrf['\"]",
            "dashboard ready handler must push {cmd:'csrf'} to the iframe")
        self.assertIn("token: CSRF", win,
            "dashboard ready handler must include the live CSRF token")

    def test_chrome_postmessage_pins_target_origin_for_csrf(self):
        # postMessage with '*' as targetOrigin LEAKS the message body to
        # whatever ends up loaded in the iframe at send time — for the CSRF
        # push specifically that's a token leak. The dashboard must derive
        # the target from f.src (the iframe's actual origin) via the
        # frameTargetOrigin helper, and use that string as the 2nd arg —
        # never '*' for the csrf send.
        self.assertRegex(self.serve, r"function\s+frameTargetOrigin\s*\(\s*f\s*\)",
            "frameTargetOrigin(f) helper must exist (centralizes URL(f.src).origin)")
        # The csrf push must use targetOrigin pinning, not '*'.
        win = re.search(
            r"postMessage\(\s*\{[^}]*cmd:\s*['\"]csrf['\"][^}]*\}\s*,\s*([^)]+)\)",
            self.serve)
        self.assertIsNotNone(win, "csrf postMessage call not found")
        assert win is not None
        target_arg = win.group(1).strip()
        self.assertNotEqual(target_arg, "'*'",
            "csrf postMessage must NOT use '*' as targetOrigin — that leaks "
            "the token to whoever's loaded in the iframe")
        self.assertNotEqual(target_arg, '"*"', "same — no '*' for csrf")
        # And the broader pattern: NONE of the dashboard→iframe postMessage
        # calls should use '*' anymore. Loose grep so a future regression
        # like postMessage({...}, '*') gets caught regardless of the cmd.
        for line in self.serve.splitlines():
            if "contentWindow.postMessage" in line and ", '*')" in line:
                self.fail("dashboard postMessage to iframe must pin "
                          "targetOrigin (use frameTargetOrigin), got: %r" % line)

    def test_renderer_swap_triggers_heal_repaint(self):
        # Both useWebgl() and useCanvas() must call _healAfterRendererSwap()
        # AFTER loadAddon — otherwise viewport cells written before the swap
        # never repaint (user-reported "tile goes blank when claude is
        # working", which they recovered by sending an external SIGWINCH via
        # an out-of-process resize). The heal re-blits without a PTY SIGWINCH
        # (see the helper-body assertions below); without it, a blank tile stays
        # blank until the user triggers the Cmd+Shift+E chord manually.
        for label, src in (("js", self.js), ("html", self.html)):
            for fn in ("useCanvas", "useWebgl"):
                m = re.search(r"function\s+" + fn + r"\s*\(\s*\)\s*\{", src)
                self.assertIsNotNone(m, "%s: %s() not found" % (label, fn))
                assert m is not None
                # Slice the function body by walking braces so we don't match
                # a stray _healAfterRendererSwap call from somewhere else.
                depth, end = 1, len(src)
                for i in range(m.end(), len(src)):
                    c = src[i]
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                body = src[m.end():end]
                # _burstHeal() runs _healAfterRendererSwap() several times across
                # the next frames (catches the WebGL atlas-warmup race); either
                # satisfies the "repaint after the swap" contract.
                self.assertRegex(body, r"(_healAfterRendererSwap|_burstHeal)\(\)",
                    "%s: %s() must call _healAfterRendererSwap()/_burstHeal() after "
                    "loadAddon to repaint pre-swap cells" % (label, fn))
        # The helper must re-blit WITHOUT a spurious PTY SIGWINCH: the old
        # resize(c+1,r);resize(c,r) trick fired two SIGWINCHes at two widths,
        # making a TUI (claude/Ink) re-render its frame twice at different
        # widths → the SAME message duplicated with mismatched wrapping (the
        # "dupe scroll lines" bug). It now rebuilds the glyph cache + repaints,
        # both client-only.
        body = TileIconAndReloadTest._fn_body(self.js, "_healAfterRendererSwap")
        self.assertIsNotNone(body, "_healAfterRendererSwap() definition missing")
        self.assertIn("fit.fit()", body,
            "_healAfterRendererSwap must call fit.fit() (corrects genuine size changes)")
        self.assertIn("clearTextureAtlas()", body,
            "_healAfterRendererSwap must rebuild the glyph cache (no-SIGWINCH re-blit)")
        self.assertRegex(body, r"term\.refresh\(\s*0\b",
            "_healAfterRendererSwap must call term.refresh(0, …) to repaint rows")
        self.assertNotRegex(body, r"term\.resize\(",
            "_healAfterRendererSwap must NOT call term.resize — it SIGWINCHes the "
            "PTY and makes a TUI re-render + duplicate (dupe scroll lines)")
        # clearTextureAtlas + refresh rebuild glyphs and repaint dirty rows but do
        # NOT recompute the renderer's canvas/framebuffer dimensions. A renderer
        # whose cached dims went stale while the tile was display:none (inactive
        # tab) then keeps ghosting / duplicating rows from the bottom up after the
        # tab is shown. The heal must drive the render service's dimension
        # recompute directly (handleResize) — the render half of the resize-trick,
        # but with no term.onResize → no PTY SIGWINCH (so no TUI dup).
        self.assertRegex(body, r"handleResize\(",
            "_healAfterRendererSwap must recompute render dimensions via "
            "_renderService.handleResize (no-SIGWINCH equivalent of the resize-trick) "
            "so a stale-dimension renderer stops ghosting after un-hide")
        # _burstHeal must actually invoke the heal more than once (the "reload
        # more often" intent): a single heal can lose the race with the GPU
        # glyph-atlas warmup and leave a gray tile. It must NOT call term.resize
        # either (same dup-line SIGWINCH hazard as the heal itself).
        burst = TileIconAndReloadTest._fn_body(self.js, "_burstHeal")
        self.assertIsNotNone(burst, "_burstHeal() definition missing")
        self.assertGreaterEqual(burst.count("_healAfterRendererSwap"), 2,
            "_burstHeal must run _healAfterRendererSwap multiple times so a freshly "
            "created/restored WebGL context that warms its atlas over several frames "
            "reliably repaints")
        self.assertNotRegex(burst, r"term\.resize\(",
            "_burstHeal must not term.resize (would SIGWINCH the PTY → TUI dup lines)")

    def test_webgl_context_recovery_is_wired(self):
        # WebGL contexts get lost (GPU reset, memory pressure, per-page context
        # cap eviction, tab back/foreground) and sometimes browser-restored. A
        # restored context paints nothing until something re-blits — the gray
        # tile a manual ↻ refresh otherwise fixes. The client must self-heal:
        #  - listen for webglcontextlost AND webglcontextrestored,
        #  - in the CAPTURE phase (these canvas events don't bubble, so a
        #    listener on term.element only sees them capturing down to the canvas),
        #  - preventDefault() on loss (the browser won't fire 'restored' otherwise),
        #  - heal/re-acquire on restore.
        for label, src in (("js", self.js), ("html", self.html)):
            self.assertIn("webglcontextlost", src,
                "%s: must listen for webglcontextlost to enable context restore" % label)
            self.assertIn("webglcontextrestored", src,
                "%s: must listen for webglcontextrestored to repaint a recovered GPU context" % label)
            # The restored listener heals (burst) or re-promotes to WebGL.
            m = re.search(r"webglcontextrestored['\"]\s*,\s*function\s*\(\s*\)\s*\{", src)
            self.assertIsNotNone(m, "%s: webglcontextrestored handler not found" % label)
            br = m.end()
            depth, end = 1, len(src)
            for i in range(br, len(src)):
                if src[i] == '{':
                    depth += 1
                elif src[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            rbody = src[br:end]
            self.assertRegex(rbody, r"_burstHeal\(\)|useWebgl\(\)",
                "%s: webglcontextrestored must repaint (burst-heal) or re-acquire WebGL" % label)
            # Capture phase: the registration closes `}, true)` right after the
            # handler body (non-bubbling events need capture on the ancestor).
            self.assertRegex(src[end:end + 12], r"^\s*,\s*true\s*\)",
                "%s: webglcontextrestored listener must be registered in the capture "
                "phase (the event doesn't bubble to term.element otherwise)" % label)
            # The lost handler must preventDefault so a restore can follow.
            lm = re.search(r"webglcontextlost['\"]\s*,\s*function\s*\(\s*ev\s*\)\s*\{", src)
            self.assertIsNotNone(lm, "%s: webglcontextlost handler not found" % label)
            lbr = lm.end()
            depth, lend = 1, len(src)
            for i in range(lbr, len(src)):
                if src[i] == '{':
                    depth += 1
                elif src[i] == '}':
                    depth -= 1
                    if depth == 0:
                        lend = i + 1
                        break
            self.assertIn("preventDefault", src[lbr:lend],
                "%s: webglcontextlost must preventDefault() so the browser may "
                "fire a later webglcontextrestored" % label)

    def test_term_client_repaints_when_tab_unhidden(self):
        # Inactive tabs are display:none → the tile's iframe viewport is 0×0 and
        # the GPU/canvas renderer's cached dims go stale (GL context can also be
        # reclaimed under the page context cap). On show, the first paint ghosts /
        # duplicates rows from the bottom up — user-reported "terminals duplicate
        # content on tab switch, a refresh hotkey fixes it". A tile re-shown while
        # still on WebGL does NOT swap the renderer, so setRendererVisible never
        # heals it; the resize listener is the only repaint trigger. It must detect
        # the 0→visible width transition and run the heal (fit.fit() alone no-ops
        # because cols/rows don't change across the hide).
        for label, src in (("js", self.js), ("html", self.html)):
            m = re.search(r"addEventListener\(\s*['\"]resize['\"]\s*,\s*function", src)
            self.assertIsNotNone(m, "%s: window resize listener not found" % label)
            # body of the resize handler (walk to the end of its statement block)
            br = src.find('{', m.end())
            depth, end = 1, len(src)
            for i in range(br + 1, len(src)):
                if src[i] == '{':
                    depth += 1
                elif src[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            body = src[br + 1:end]
            self.assertRegex(body, r"(_healAfterRendererSwap|_burstHeal)\(\)",
                "%s: the resize listener must heal on the 0→visible transition so a "
                "tile re-shown from a hidden tab repaints (not just fit.fit, which "
                "no-ops when cols/rows are unchanged)" % label)
            self.assertRegex(body, r"===\s*0",
                "%s: the heal must be gated on a 0→visible transition (was-hidden), "
                "not run on every resize" % label)

    def test_term_client_refresh_swaps_renderer_without_touching_socket(self):
        # Cmd+Shift+E refresh must do MORE than fit + resize-trick + repaint:
        # the "stays gray, spamming refresh doesn't help" bug is renderer-
        # state corruption (texture-atlas / GL context partial loss). A
        # renderer swap forces a fresh GPU context.
        #
        # But it must NOT close the WebSocket — ttyd's reattach replay
        # starts with a terminal-init sequence that destroys our in-memory
        # scrollback ("scrollback again gone" user-report). The earlier
        # version of this test asserted socket.close() — that was wrong,
        # the regression cost scrollback.
        m = re.search(r"d\.cmd\s*===\s*['\"]refresh['\"]", self.js)
        self.assertIsNotNone(m); assert m is not None
        from_refresh = self.js[m.end():m.end() + 4000]
        next_branch = from_refresh.find("else if (d.cmd ===")
        if next_branch > 0:
            from_refresh = from_refresh[:next_branch]
        self.assertIn("useCanvas()", from_refresh,
            "refresh must swap to canvas (then back to WebGL) to force a "
            "fresh GPU context — GL texture-atlas corruption is the main "
            "cause of 'tile stays gray, refresh doesn't help'")
        self.assertIn("useWebgl()", from_refresh,
            "refresh must restore WebGL after the swap so we keep GPU "
            "rendering when the tile is visible")
        self.assertNotRegex(from_refresh, r"socket\s*\.\s*close\(\s*\)",
            "refresh must NOT close the WebSocket — ttyd's reattach replay "
            "begins with an init sequence (RIS / \\x1bc / clear-scrollback) "
            "that destroys in-memory scrollback. The user-reported "
            "'scrollback again gone' regression came from doing this. "
            "Renderer swap alone is the safe fix for the gray-tile case.")
        self.assertRegex(from_refresh,
            r"window\.dispatchEvent\(\s*new\s+Event\(\s*['\"]resize['\"]\s*\)\s*\)",
            "refresh must also fire a synthetic window 'resize' event — kicks "
            "any external listener (xterm internals, addons, our own fit) the "
            "same way a real browser-window resize would")

    def test_term_client_refresh_does_not_clear_or_reset(self):
        # The refresh handler must NOT call term.clear() or term.reset() —
        # those wipe the viewport / scrollback. The whole point is a
        # buffer-preserving repaint.
        m = re.search(r"d\.cmd\s*===\s*['\"]refresh['\"]", self.js)
        self.assertIsNotNone(m); assert m is not None
        window = self.js[m.end():m.end() + 800]
        self.assertNotIn("term.clear", window,
            "refresh must not call term.clear() — that wipes the viewport")
        self.assertNotIn("term.reset", window,
            "refresh must not call term.reset() — that wipes scrollback")

    # ---- horizontal wheel forwarding (row-scroll over a tile iframe) ----

    def test_term_client_forwards_horizontal_wheel_to_dashboard(self):
        # User-reported "cannot h-scroll if mouse is over an opencode tile".
        # Cross-origin iframes swallow wheel events — they don't bubble to
        # the parent — so a trackpad horizontal swipe over a terminal can't
        # natively reach the dashboard's row overflow-x:auto container.
        # The custom client mirrors the horizontal delta upward as
        # {key:'wheel-x', dx:<pixels>}; the dashboard applies it to
        # grid.scrollLeft when the row mode is active.
        for label, src in (("js", self.js), ("html", self.html)):
            self.assertIn("'wheel-x'", src,
                "%s: missing wheel-x forwarder — h-scroll over a tile won't reach the dashboard" % label)
            self.assertRegex(src, r"addEventListener\(\s*['\"]wheel['\"]",
                "%s: wheel listener not registered" % label)

    def test_term_client_wheel_forwarder_runs_in_capture_phase(self):
        # Regression: side-scroll over a tile "stopped working" on mouse-mode
        # apps — the newer claude clients (full-screen UI / fixed prompt input)
        # and opencode turn mouse tracking ON, so xterm hands wheel events to the
        # app and a BUBBLE-phase listener never
        # sees the horizontal swipe (confirmed by A/B: bubble+passive scrolled 0
        # under mouse mode, capture scrolled 150). The forwarder MUST register in
        # the CAPTURE phase so it claims a horizontal swipe before xterm/the app
        # can, and be non-passive so it can preventDefault + stopPropagation.
        for label, src in (("js", self.js), ("html", self.html)):
            m = re.search(
                r"addEventListener\(\s*['\"]wheel['\"][\s\S]+?\}\s*,\s*\{([^}]*)\}\s*\)",
                src)
            self.assertIsNotNone(m, "%s: wheel listener registration not found" % label)
            assert m is not None
            opts = m.group(1)
            self.assertRegex(opts, r"capture:\s*true",
                "%s: wheel forwarder must use the CAPTURE phase so a mouse-mode "
                "app (newer claude / opencode) can't consume the swipe first" % label)
            self.assertRegex(opts, r"passive:\s*false",
                "%s: wheel forwarder must be non-passive so it can preventDefault "
                "+ stopPropagation on a horizontal swipe" % label)

    # Grab the brace-and-options-balanced body of the capture-phase wheel hook.
    _WHEEL_HOOK_RE = (
        r"addEventListener\(\s*['\"]wheel['\"]\s*,\s*function[\s\S]+?"
        r"\}\s*,\s*\{\s*capture:\s*true,\s*passive:\s*false\s*\}\s*\)")

    def _wheel_hook_body(self):
        m = re.search(self._WHEEL_HOOK_RE, self.js)
        self.assertIsNotNone(m,
            "capture-phase wheel hook not found (see "
            "test_term_client_wheel_forwarder_runs_in_capture_phase)")
        assert m is not None
        return m.group(0)

    def test_term_client_only_forwards_horizontal_component(self):
        # The horizontal branch claims only a horizontal-DOMINANT wheel (deltaX
        # bigger than deltaY) or shift+wheel (the desktop convention for "treat
        # this as horizontal"). A vertical-dominant scroll must NOT be forwarded
        # as wheel-x — it falls through to the vertical branch instead.
        body = self._wheel_hook_body()
        self.assertRegex(body, r"e\.deltaX\b", "wheel hook must read deltaX")
        self.assertRegex(body, r"e\.shiftKey\s*\?\s*e\.deltaY",
            "shift+wheel must be treated as horizontal (convention)")
        # Horizontal-dominance guard: claim only when |deltaX| > |deltaY| (or shift).
        self.assertRegex(body,
            r"Math\.abs\(\s*e\.deltaX\s*\)\s*>\s*Math\.abs\(\s*e\.deltaY\s*\)",
            "horizontal branch must claim only a horizontal-dominant wheel "
            "(|deltaX| > |deltaY|) so it doesn't steal a vertical scroll")
        # A claimed horizontal swipe is forwarded to the dashboard and swallowed
        # so the app doesn't also get it.
        self.assertIn("'wheel-x'", body,
            "horizontal swipe must post {key:'wheel-x'} to the dashboard")
        self.assertIn("preventDefault()", body,
            "a claimed wheel must preventDefault")
        self.assertIn("stopPropagation()", body,
            "a claimed wheel must stopPropagation so the mouse-mode app never sees it")

    def test_term_client_smooth_scrolls_vertical_over_mouse_mode_tile(self):
        # Regression ("we had smooth scrolling, doesn't work anymore"): the new
        # claude fixed-prompt UI is normal-screen but turns MOUSE TRACKING on, so
        # xterm forwards each wheel notch to the app instead of pixel-scrolling its
        # viewport — a trackpad scroll goes from continuous to line-quantized. When
        # the tile has scrollback to move, the vertical branch claims the wheel and
        # scrolls xterm's OWN viewport in pixels, so the sub-row transform makes it
        # glide like a non-mouse tile (works for old AND new claude / any
        # normal-screen TUI). It must NOT touch: mouse-OFF tiles (xterm already
        # scrolls smoothly), shift+wheel (goes to the app), or a viewport with
        # nothing to scroll (a true alt-screen TUI — leave the wheel to the app).
        body = self._wheel_hook_body()
        # Gate on the app actually being in mouse-tracking mode.
        self.assertRegex(body, r"areMouseEventsActive",
            "vertical branch must gate on xterm's mouse-tracking state — only then "
            "does xterm steal the wheel from its own scrollback")
        # Gate on there being scrollback to move (so an alt-screen TUI with nothing
        # above the fold is left to the app — 'make it work with the old TUI too').
        self.assertRegex(body, r"scrollHeight\s*-\s*\w+\.clientHeight\s*<=\s*1",
            "vertical branch must skip a viewport with nothing to scroll "
            "(alt-screen TUI) and leave the wheel to the app")
        # Mouse-off must bail BEFORE we ever claim a vertical wheel.
        self.assertRegex(body, r"if\s*\(\s*!\s*mouseOn\s*\)\s*return",
            "must leave vertical scrolling untouched when mouse tracking is off")
        # shift+wheel is forwarded to the app, not claimed for local scroll.
        self.assertRegex(body, r"if\s*\(\s*e\.shiftKey\s*\|\|\s*!\s*e\.deltaY\s*\)\s*return",
            "shift+wheel (and a zero-deltaY event) must be left for the app")
        # The actual scroll: move xterm's own viewport in pixels.
        self.assertRegex(body, r"\w+\.scrollTop\s*\+=\s*dy",
            "vertical branch must pixel-scroll xterm's viewport (smooth via the "
            "sub-row transform), not forward discrete wheel events to the app")

    def test_chrome_routes_wheel_x_to_row_scrollleft(self):
        # The dashboard's message handler must apply d.dx to grid.scrollLeft,
        # gated on the row mode (column-grid has no horizontal overflow, so
        # the assignment would be a silent no-op there, but the gate makes
        # the intent obvious and skips the work).
        m = re.search(
            r"d\.key\s*===\s*['\"]wheel-x['\"][^;{}]*\{([\s\S]+?)\n\s*\}",
            self.serve)
        self.assertIsNotNone(m,
            "chrome missing the wheel-x branch in the iframe message handler")
        assert m is not None
        body = m.group(1)
        self.assertRegex(body, r"grid\.classList\.contains\(\s*['\"]row['\"]\s*\)",
            "wheel-x handler must gate on .row mode to skip the column grid")
        self.assertRegex(body, r"grid\.scrollLeft\s*\+=\s*d\.dx",
            "wheel-x handler must accumulate d.dx into grid.scrollLeft")


class FontPickerTest(unittest.TestCase):
    """Font picker: header <select> swaps the terminal face across every tile.

    Contract pieces under test:
      - serve.py FONTS catalog is internally consistent (default id resolves;
        every entry's family has at least one @font-face in _FONT_FACES; every
        woff2 file present on disk).
      - The dashboard page substitutes __FONTS_JSON__/__DEFAULT_FONT_ID__ and
        builds the <select> + persists choices to localStorage + broadcasts to
        every iframe via postMessage.
      - term-client.js handles cmd:'font' by mutating term.options + re-fitting,
        and reads the same localStorage key on boot so a cold-reload doesn't
        flash through JBM.
      - Every _FONT_FACES woff2 (jbm/terminus/cozette/fira-code/charter/
        source-serif-4 × 400/700) is inlined into term.html (one @font-face rule
        each) so the picker swap is instant and offline. Georgia is the lone
        system-only entry (proprietary — falls through to the OS copy).
    """

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "serve.py")) as f:
            cls.serve = f.read()
        with open(os.path.join(here, "term-client.js")) as f:
            cls.js = f.read()
        with open(os.path.join(here, "term.html")) as f:
            cls.html = f.read()
        cls.fontdir = os.path.join(here, "fonts")

    # ---- serve.py catalog ----

    def test_default_font_id_resolves_to_a_font_entry(self):
        # DEFAULT_FONT_ID must match one of the FONTS entries' `id` — otherwise
        # the page boots without a valid initial selection and the <select>
        # falls back to its first option (potentially not what we want).
        ids = {f["id"] for f in serve.FONTS}
        self.assertIn(serve.DEFAULT_FONT_ID, ids,
            "DEFAULT_FONT_ID=%r not in FONTS ids=%r" %
            (serve.DEFAULT_FONT_ID, sorted(ids)))

    def test_fonts_catalog_has_required_shape(self):
        # Every entry must carry the keys the picker UI + term-client.js read.
        # Missing one would silently produce a broken option (no postMessage
        # field => term-client.js applyFont() bails). Sizes/weights are
        # validated to xterm-acceptable shapes.
        for f in serve.FONTS:
            for key in ("id", "label", "family", "size", "weight"):
                self.assertIn(key, f, "FONTS entry missing %r: %r" % (key, f))
            self.assertIsInstance(f["size"], int, "size must be int: %r" % f)
            self.assertGreater(f["size"], 0, "size must be positive: %r" % f)
            self.assertIn(f["weight"], ("normal", "bold"),
                "weight must be 'normal' or 'bold': %r" % f)

    def test_fonts_user_requested_entries_present(self):
        # The user asked for these labels verbatim — keep them so the picker
        # matches what they typed. If a label is renamed, update this test so
        # the change is conscious, not a typo. Note: `ter-u32n.psf` was
        # initially in the catalog but removed — at the PSF level it IS
        # `Terminus 32` regular (same glyphs, same metrics), so listing both
        # was duplicative.
        labels = {f["label"] for f in serve.FONTS}
        for required in ("solar24x32.psfu", "solar48x64.psfu",
                         "Terminus v24b", "Terminus 32"):
            self.assertIn(required, labels,
                "missing user-requested picker label %r in %r" %
                (required, sorted(labels)))
        # And the removed-duplicate label must STAY removed — re-adding it
        # would split user clicks across two rows that render identically.
        self.assertNotIn("ter-u32n.psf", labels,
            "ter-u32n.psf must not be re-added — it duplicates Terminus 32")

    # System fonts intentionally offered WITHOUT an embedded @font-face — they
    # rely on the OS copy (e.g. Georgia is proprietary, can't be embedded). Keep
    # this list tiny and obvious; anything else missing a face is a typo.
    SYSTEM_FONT_FAMILIES = {"Georgia"}

    def test_every_font_family_has_a_font_face(self):
        # Every family referenced by FONTS must have a face declared in
        # _FONT_FACES — otherwise the browser falls back through the system stack
        # and the picker swap is a visual no-op — EXCEPT explicit system fonts
        # (SYSTEM_FONT_FAMILIES), which are meant to use the OS copy.
        face_families = {family for (family, _, _) in serve._FONT_FACES}
        for f in serve.FONTS:
            if f["family"] in self.SYSTEM_FONT_FAMILIES:
                continue
            self.assertIn(f["family"], face_families,
                "FONTS entry %r references family %r with no @font-face in "
                "_FONT_FACES (declared: %r)" %
                (f["id"], f["family"], sorted(face_families)))

    def test_every_font_face_file_exists_on_disk(self):
        # _font_face_css() short-circuits to "" if ANY file is missing, which
        # would silently kill ALL inlined fonts. Catch a missing file in tests
        # so build-term.sh failures surface here too.
        for (_family, _weight, fn) in serve._FONT_FACES:
            self.assertTrue(os.path.isfile(os.path.join(self.fontdir, fn)),
                "missing font file fonts/%s — see build-term.sh FACES list" % fn)

    def test_font_face_css_emits_one_rule_per_face(self):
        # FONT_FACE_CSS must have one @font-face for every _FONT_FACES tuple.
        # Earlier the inliner hard-coded only the two JBM weights; this guards
        # the multi-family generalization.
        css = serve.FONT_FACE_CSS
        self.assertNotEqual(css, "",
            "_font_face_css() returned empty — a font file is missing")
        n = css.count("@font-face")
        self.assertEqual(n, len(serve._FONT_FACES),
            "expected %d @font-face rules, got %d in:\n%s" %
            (len(serve._FONT_FACES), n, css[:200]))
        # Each declared family appears at least once.
        for family in {f for (f, _, _) in serve._FONT_FACES}:
            self.assertIn("font-family:'%s'" % family, css,
                "FONT_FACE_CSS missing family %r" % family)

    def test_fonts_json_is_valid_and_matches_catalog(self):
        # The JSON shipped to the page must round-trip back to the Python
        # catalog — a stale FONTS_JSON would have the picker show different
        # entries than the server believes are valid.
        parsed = json.loads(serve.FONTS_JSON)
        self.assertEqual(parsed, list(serve.FONTS))

    def test_new_fonts_are_offered(self):
        ids = {f["id"] for f in serve.FONTS}
        for fid in ("fira-code", "charter", "source-serif-4", "georgia"):
            self.assertIn(fid, ids, "picker missing font id %r" % fid)

    def test_georgia_is_system_only(self):
        # Georgia is proprietary → offered WITHOUT an embedded face (uses the OS
        # copy). It must not appear in _FONT_FACES, and must be allowlisted as a
        # system font (so test_every_font_family_has_a_font_face skips it).
        face_families = {fam for (fam, _, _) in serve._FONT_FACES}
        georgia = [f for f in serve.FONTS if f["id"] == "georgia"]
        self.assertTrue(georgia, "georgia entry missing")
        self.assertEqual(georgia[0]["family"], "Georgia")
        self.assertNotIn("Georgia", face_families,
            "Georgia is proprietary and must NOT be embedded in _FONT_FACES")
        self.assertIn("Georgia", self.SYSTEM_FONT_FAMILIES)

    def test_build_faces_match_font_faces(self):
        # build-term.sh FACES (the term.html inliner) must mirror
        # serve._FONT_FACES exactly, or the dashboard chrome and the terminal
        # tiles would embed different faces and the picker would be inconsistent.
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "build-term.sh")) as f:
            build = f.read()
        m = re.search(r"FACES=\((.*?)\n\)", build, re.S)
        self.assertIsNotNone(m, "FACES array not found in build-term.sh")
        assert m is not None
        faces = re.findall(r'"([^"|]+)\|(\d+)\|([^"]+)"', m.group(1))
        build_set = {(fam, int(w), fn) for (fam, w, fn) in faces}
        self.assertEqual(build_set, set(serve._FONT_FACES),
            "build-term.sh FACES and serve._FONT_FACES drifted — keep them in lockstep")

    # ---- dashboard chrome (serve.py source) ----

    def test_page_includes_font_picker_select(self):
        self.assertIn('id="fontSel"', self.serve,
            "header must include the <select id=\"fontSel\"> font picker")

    def test_html_substitutes_fonts_json_token(self):
        # __FONTS_JSON__ and __DEFAULT_FONT_ID__ tokens must be substituted by
        # the request handler — otherwise the page ships with the literal
        # placeholder and FONTS = "__FONTS_JSON__" is a JS syntax error.
        self.assertIn('"__FONTS_JSON__"', self.serve,
            "HTML template missing __FONTS_JSON__ placeholder")
        self.assertIn('.replace("__FONTS_JSON__", FONTS_JSON)', self.serve,
            "handler must substitute __FONTS_JSON__ with FONTS_JSON")
        self.assertIn('.replace("__DEFAULT_FONT_ID__", DEFAULT_FONT_ID)', self.serve,
            "handler must substitute __DEFAULT_FONT_ID__")

    def test_font_change_persists_and_broadcasts(self):
        # On <select> change: localStorage.setItem(FONT_LSKEY, id) AND a push
        # to every iframe. The push goes through pushCurrent() →
        # broadcastFont(currentEntry()) so the size override layered on top
        # ships in the same payload. Without persistence the choice dies on
        # reload; without the broadcast it only takes effect for NEW tiles.
        m = re.search(r"sel\.addEventListener\(\s*['\"]change['\"]", self.serve)
        self.assertIsNotNone(m, "fontSel change listener not found")
        assert m is not None
        win = self.serve[m.end():m.end() + 400]
        self.assertIn("localStorage.setItem(FONT_LSKEY", win,
            "change handler must persist the choice to localStorage")
        self.assertIn("pushCurrent()", win,
            "change handler must call pushCurrent() to broadcast to every iframe")

    def test_broadcast_font_posts_to_each_iframe(self):
        # broadcastFont must iterate every tile and postMessage the
        # claude-host font command. The shape MUST be {type:'claude-host',
        # cmd:'font', font:<entry>} — term-client.js's message router gates
        # on cmd === 'font' and reads d.font.
        m = re.search(r"function broadcastFont\s*\([^)]*\)\s*\{", self.serve)
        self.assertIsNotNone(m, "broadcastFont not found")
        assert m is not None
        depth, end = 1, len(self.serve)
        for i in range(m.end(), len(self.serve)):
            c = self.serve[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        body = self.serve[m.end():end]
        self.assertIn("tiles.values()", body,
            "broadcastFont must iterate every tile")
        self.assertRegex(body,
            r"postMessage\(\s*\{\s*type:\s*['\"]claude-host['\"]\s*,\s*cmd:\s*['\"]font['\"]",
            "broadcastFont must postMessage type:'claude-host', cmd:'font'")
        self.assertIn("font: entry", body,
            "broadcastFont must include the entry as the `font` payload "
            "(term-client.js reads d.font)")

    def test_ready_handler_pushes_current_font_to_new_iframe(self):
        # When a freshly-mounted iframe announces ready, the dashboard must
        # push the current font (with size override layered on top via
        # currentEntry()) — otherwise tiles that mount after the user picked a
        # non-default font/size render in JBM-13 until the next change.
        m = re.search(r"if\s*\(\s*d\.ready\s*\)", self.serve)
        self.assertIsNotNone(m, "d.ready branch not found in message handler")
        assert m is not None
        win = self.serve[m.end():m.end() + 700]
        self.assertIn("currentEntry()", win,
            "ready handler must read currentEntry() (font + size override)")
        self.assertRegex(win, r"cmd:\s*['\"]font['\"]",
            "ready handler must postMessage cmd:'font' to the new iframe")

    # ---- term-client.js (and built term.html) ----

    def _slice_function_body(self, src, header_re):
        m = re.search(header_re, src)
        self.assertIsNotNone(m, "function header not found: %r" % header_re)
        assert m is not None
        depth, end = 1, len(src)
        for i in range(m.end(), len(src)):
            c = src[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        return src[m.end():end]

    def test_term_client_handles_font_command(self):
        # The postMessage router must dispatch cmd:'font' to applyFont(d.font).
        # Both the source and the built HTML must satisfy this — a stale
        # term.html would silently regress in production while tests still
        # pass against the source.
        for label, src in (("js", self.js), ("html", self.html)):
            self.assertRegex(src,
                r"d\.cmd\s*===\s*['\"]font['\"][^;]*applyFont\(\s*d\.font\s*\)",
                "%s: message router must dispatch cmd:'font' to applyFont(d.font)" % label)

    def test_apply_font_mutates_term_options_and_refits(self):
        # applyFont must set fontFamily / fontSize / fontWeight on term.options
        # AND call fit.fit() afterwards. Missing fit.fit() would leave cols/rows
        # mismatched against the new cell width — the PTY's view of the size
        # would lag and the visible grid would render at the OLD grid against
        # the NEW glyph metrics (visible corruption until the next resize).
        body = self._slice_function_body(self.js, r"function applyFont\s*\([^)]*\)\s*\{")
        self.assertIn("term.options.fontFamily", body,
            "applyFont must set term.options.fontFamily")
        self.assertIn("term.options.fontSize", body,
            "applyFont must set term.options.fontSize")
        self.assertIn("term.options.fontWeight", body,
            "applyFont must set term.options.fontWeight")
        self.assertIn("fit.fit()", body,
            "applyFont must call fit.fit() so cols/rows reflow to new cell metrics")
        self.assertIn("localStorage.setItem(LSFONT", body,
            "applyFont must persist the choice for cold-boot")

    def test_term_client_reads_stored_font_on_boot(self):
        # On boot, term-client.js must read the same LSFONT key the dashboard
        # mirrors into — otherwise a tile reload paints in JBM for ~1 frame
        # before the dashboard pushes the current selection.
        self.assertIn("var LSFONT = 'claude-term-font'", self.js,
            "term-client.js must declare LSFONT (the persisted font key)")
        self.assertIn("_readStoredFont", self.js,
            "term-client.js must expose _readStoredFont for boot-time lookup")
        # The Terminal constructor must read fontFamily/fontSize/fontWeight
        # from _bootFont (the stored entry or the default fallback).
        m = re.search(r"new Terminal\(\s*\{", self.js)
        self.assertIsNotNone(m); assert m is not None
        depth, end = 1, len(self.js)
        for i in range(m.end(), len(self.js)):
            c = self.js[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        ctor = self.js[m.end():end]
        self.assertIn("_bootFont.family", ctor,
            "Terminal constructor must source fontFamily from _bootFont")
        self.assertIn("_bootFont.size", ctor,
            "Terminal constructor must source fontSize from _bootFont")

    # ---- size selector ----

    def test_page_includes_size_selector(self):
        self.assertIn('id="sizeSel"', self.serve,
            "header must include the <select id=\"sizeSel\"> font-size selector")

    def test_size_constants_defined(self):
        # SIZE_LSKEY: where the chosen size is persisted across reloads.
        # SIZES:    the dropdown options. First entry MUST be '' (the "Auto"
        # sentinel — use the font catalog's natural size). The rest are int
        # CSS px values in ascending order so the dropdown reads naturally.
        self.assertRegex(self.serve,
            r"const\s+SIZE_LSKEY\s*=\s*['\"]claude-sessions-font-size['\"]",
            "SIZE_LSKEY constant missing or mis-named")
        m = re.search(r"const\s+SIZES\s*=\s*\[([^\]]+)\]", self.serve)
        self.assertIsNotNone(m, "SIZES dropdown options not found")
        assert m is not None
        items = [s.strip().strip("'\"") for s in m.group(1).split(",")]
        self.assertEqual(items[0], "",
            "first SIZES entry must be '' (the Auto sentinel)")
        nums = [int(s) for s in items[1:]]
        self.assertEqual(nums, sorted(nums),
            "SIZES values must be in ascending order: %r" % nums)
        self.assertTrue(all(8 <= n <= 64 for n in nums),
            "SIZES values out of sane terminal range (8..64 px): %r" % nums)

    def test_current_entry_layers_size_over_font(self):
        # currentEntry() is the single composer the picker, size selector,
        # AND ready-handler all push through. Without it the three paths
        # could disagree on which size ships down (e.g. picker sends catalog
        # size, size-selector sends override, ready-push uses neither).
        body = self._slice_function_body(self.serve, r"function currentEntry\s*\(\s*\)\s*\{")
        self.assertIn("getFontEntry(currentFontId())", body,
            "currentEntry must read the picked font row")
        self.assertIn("currentSizeOverride()", body,
            "currentEntry must read the size-selector override")
        # A non-zero override MUST land in the returned entry under `size`;
        # zero MUST pass through unchanged (Auto = catalog size). The entry is a
        # fresh copy of the catalog row with size (and line-height) layered on.
        self.assertRegex(body, r"Object\.assign\(\{\}\s*,\s*getFontEntry",
            "currentEntry must return a COPY of the catalog row (never mutate it)")
        self.assertRegex(body, r"if\s*\(\s*sz\s*\)\s*e\.size\s*=\s*sz",
            "currentEntry must overlay the size override on the copy when sz > 0")

    def test_size_change_persists_and_broadcasts(self):
        # sizeSel change: persist (or removeItem on Auto) AND push.
        m = re.search(r"sizeSel\.addEventListener\(\s*['\"]change['\"]", self.serve)
        self.assertIsNotNone(m, "sizeSel change listener not found")
        assert m is not None
        win = self.serve[m.end():m.end() + 500]
        self.assertIn("localStorage.removeItem(SIZE_LSKEY)", win,
            "size handler must clear the key on Auto so the font catalog "
            "size wins on next reload (vs persisting an explicit 0)")
        self.assertIn("localStorage.setItem(SIZE_LSKEY", win,
            "size handler must persist explicit sizes")
        self.assertIn("pushCurrent()", win,
            "size handler must broadcast via pushCurrent()")

    def test_current_size_override_validates_stored_value(self):
        # A corrupt SIZE_LSKEY value ('abc', '-5', '0') must read as 0 (Auto)
        # not propagate as a bogus size into broadcastFont. Otherwise a single
        # bad localStorage write could brick every tile's rendering until the
        # user manually cleared it.
        body = self._slice_function_body(self.serve, r"function currentSizeOverride\s*\(\s*\)\s*\{")
        self.assertIn("parseInt(v, 10)", body,
            "currentSizeOverride must parseInt the stored value")
        self.assertIn("n > 0", body,
            "currentSizeOverride must reject non-positive sizes (Auto sentinel)")

    # ---- term-client.js applyFont already consumes the merged entry ----

    def test_apply_font_uses_entry_size_not_hardcoded(self):
        # applyFont must take the size from entry.size (so a size-override
        # broadcast lands), not a hard-coded default. A regression here
        # would make the size dropdown a visual no-op.
        body = self._slice_function_body(self.js, r"function applyFont\s*\([^)]*\)\s*\{")
        self.assertRegex(body, r"entry\.size",
            "applyFont must read entry.size so the size selector works")
        self.assertIn("term.options.fontSize = size", body,
            "applyFont must set fontSize from the parsed entry size")

    def test_palette_contains_the_four_user_specified_colors(self):
        # The user specified exactly these four colours. If anyone reorders
        # or recolours the palette, this test should be updated consciously,
        # not silently.
        for c in ("#03AED2", "#F8DE22", "#F45B26", "#D12052"):
            self.assertRegex(self.serve,
                r"TITLE_PALETTE\s*=\s*\[[^\]]*['\"]" + c + r"['\"]",
                "TITLE_PALETTE must include %s" % c)

    def test_color_is_stable_per_sid_not_random(self):
        # tileTitleColor must hash the sid deterministically — Math.random()
        # would re-roll on every render, flickering the title on each 3 s
        # poll. The hash + modulo against TITLE_PALETTE.length keeps it
        # cheap and deterministic.
        m = re.search(r"function tileTitleColor\s*\([^)]*\)\s*\{", self.serve)
        self.assertIsNotNone(m, "tileTitleColor() not found")
        assert m is not None
        depth, end = 1, len(self.serve)
        for i in range(m.end(), len(self.serve)):
            c = self.serve[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        body = self.serve[m.end():end]
        self.assertNotIn("Math.random", body,
            "tileTitleColor must NOT use Math.random — colour must be stable per sid")
        self.assertIn("charCodeAt", body,
            "tileTitleColor must hash the sid character-by-character")
        self.assertIn("TITLE_PALETTE", body,
            "tileTitleColor must index TITLE_PALETTE")
        self.assertIn("h % TITLE_PALETTE.length", body,
            "tileTitleColor must modulo the hash against the palette length")

    def test_colour_applied_as_css_variable_on_tile(self):
        # The colour is set as `--tile-title-color` ON THE TILE — not as
        # inline `color:` on .name — so the `.tile.bell .name` rule (higher
        # specificity than `.tile .name`) keeps winning when a tile rings.
        # Setting `name.style.color = …` would let the inline value beat the
        # bell rule and the green flash would silently break. The value may be a
        # ternary (channel tiles opt into the fixed CHANNEL_TITLE_COLOR), so we
        # only require that the non-channel branch is tileTitleColor(s.id).
        self.assertRegex(self.serve,
            r"el\.style\.setProperty\(\s*['\"]--tile-title-color['\"]\s*,[^;]*tileTitleColor\(\s*s\.id\s*\)",
            "tile creation must set --tile-title-color on the .tile element "
            "(not inline `color:` on .name)")
        # And the .name rule must consume the var with var(--fg) fallback so
        # the loading-placeholder (no var set) doesn't render invisible.
        self.assertRegex(self.serve,
            r"\.tile \.name\s*\{[^}]*color:\s*var\(\s*--tile-title-color\s*,\s*var\(--fg\)\s*\)",
            ".tile .name CSS rule must read var(--tile-title-color) with var(--fg) fallback")

    def test_bell_rule_overrides_title_color(self):
        # The bell rule must still set `color: var(--host)` directly (not via
        # the variable), so its higher specificity beats the var-driven base
        # colour. If someone changed it to set the variable instead, the
        # bell would inherit the random palette colour and never flash green.
        self.assertRegex(self.serve,
            r"\.tile\.bell\s+\.name\s*\{\s*color:\s*var\(--host\)",
            ".tile.bell .name must set color directly to var(--host) so it "
            "overrides the per-tile palette colour")


    def test_term_html_inlines_all_six_woff2_faces(self):
        # term.html must inline one @font-face per (family, weight) in
        # _FONT_FACES. A swap that hits a missing face would fall back through
        # the system monospace stack — visually a no-op rather than the chosen
        # font. We check both the family declaration and the data: URI scheme.
        for (family, weight, _file) in serve._FONT_FACES:
            self.assertRegex(self.html,
                r'@font-face\{font-family:"%s";font-style:normal;font-weight:%d' %
                (re.escape(family), weight),
                "term.html missing @font-face for %s %d" % (family, weight))
        # All six are base64-inlined (no external src:url(http...) leaks).
        self.assertEqual(self.html.count("@font-face{"),
                         len(serve._FONT_FACES),
                         "expected exactly %d @font-face rules in term.html" %
                         len(serve._FONT_FACES))

    # ---- line-height control ----
    def test_line_height_picker_present_and_wired(self):
        # A header <select> sets xterm's line-height multiplier across every tile,
        # threaded through the SAME cmd:'font' broadcast as the font/size pickers
        # (currentEntry carries lineHeight), persisted, and applied by the client.
        self.assertIn('id="lineHeightSel"', self.serve,
            "header must have a line-height <select>")
        self.assertIn("LINEHEIGHT_LSKEY", self.serve,
            "line-height must persist to its own localStorage key")
        self.assertRegex(self.serve, r"function currentLineHeight\(\)",
            "missing currentLineHeight() reader")
        # currentEntry must layer lineHeight into the broadcast entry.
        body = TileIconAndReloadTest._fn_body(self.serve, "currentEntry")
        self.assertIsNotNone(body, "currentEntry() not found")
        self.assertRegex(body, r"e\.lineHeight\s*=\s*lh",
            "currentEntry must include the line-height override so it ships to tiles")

    def test_line_height_applied_and_persisted_by_client(self):
        for label, src in (("js", self.js), ("html", self.html)):
            body = TileIconAndReloadTest._fn_body(src, "applyFont")
            self.assertIsNotNone(body, "%s: applyFont() not found" % label)
            assert body is not None
            self.assertRegex(body, r"term\.options\.lineHeight\s*=\s*lineHeight",
                "%s: applyFont must set xterm's lineHeight" % label)
            self.assertRegex(body, r"lineHeight:\s*lineHeight",
                "%s: applyFont must persist lineHeight with the font (cold-boot)" % label)
            # Auto / unset → 1.0 (xterm default), never 0.
            self.assertRegex(body,
                r"entry\.lineHeight\s*>\s*0\s*\)\s*\?\s*entry\.lineHeight\s*:\s*1\.0",
                "%s: applyFont must default an unset line-height to 1.0" % label)
            # Boot constructor must seed lineHeight too (no flash on cold reload).
            self.assertRegex(src, r"lineHeight:\s*\(typeof _bootFont\.lineHeight",
                "%s: Terminal() must boot with the stored line-height" % label)


class ChannelsTest(unittest.TestCase):
    """Channels = NDJSON files at /tmp/claude-channels/<name>.ndjson written
    by the `channel` skill. Dashboard exposes them via /api/channels (list),
    /api/channel/<name> (read/append), and /channel/<name> (chatroom HTML
    that polls + posts). This class covers the in-process helpers + the
    serve.py routes through the running fixture."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="serve-channels-")
        self._saved_dir = serve.CHANNELS_DIR
        serve.CHANNELS_DIR = self.dir

    def tearDown(self):
        serve.CHANNELS_DIR = self._saved_dir
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---- in-process unit tests of the helpers ----

    def test_list_channels_returns_sorted_by_mtime(self):
        # Write three NDJSONs with explicit mtimes; list_channels() must
        # return them newest-modified-first so the menu shows the most
        # recently active conversations at the top. mtimes must stay within
        # CHANNEL_LIST_MAX_AGE_SEC of now or the freshness filter would
        # hide them — covered separately by the *_filters_stale_* test.
        now = time.time()
        names = ["old", "mid", "new"]
        for i, n in enumerate(names):
            p = os.path.join(self.dir, n + ".ndjson")
            with open(p, "w") as f:
                f.write('{"from":"a","ts":1,"text":"x"}\n' * (i + 1))
            # Spaced 60s apart, all within the last few minutes → fresh.
            mt = now - (3 - i) * 60
            os.utime(p, (mt, mt))
        result = serve.list_channels()
        self.assertEqual([c["name"] for c in result], ["new", "mid", "old"])
        # Counts must reflect actual line counts.
        self.assertEqual({c["name"]: c["count"] for c in result},
                         {"old": 1, "mid": 2, "new": 3})

    def test_list_channels_filters_stale_entries_older_than_max_age(self):
        # User-reported "channels should not be listed if no activity for 3h":
        # the dropdown was accumulating every short-lived chat ever opened.
        # Anything with mtime > CHANNEL_LIST_MAX_AGE_SEC old must be hidden
        # from the listing (the NDJSON file itself stays — /channel/<name>
        # still works if you know the name; a fresh append revives the entry).
        now = time.time()
        fresh_p = os.path.join(self.dir, "fresh.ndjson")
        stale_p = os.path.join(self.dir, "stale.ndjson")
        for p in (fresh_p, stale_p):
            with open(p, "w") as f:
                f.write('{"from":"a","ts":1,"text":"x"}\n')
        # fresh = 30 min ago (well under 3 h); stale = 4 h ago (over 3 h).
        os.utime(fresh_p, (now - 30 * 60, now - 30 * 60))
        os.utime(stale_p, (now - 4 * 3600, now - 4 * 3600))
        names = [c["name"] for c in serve.list_channels()]
        self.assertIn("fresh", names, "active channel must remain listed")
        self.assertNotIn("stale", names,
            "channel with no activity for >3h must be hidden from the listing")
        # And the threshold itself must be the documented 3h.
        self.assertEqual(serve.CHANNEL_LIST_MAX_AGE_SEC, 3 * 3600,
            "documented threshold (3h) drifted — update SPEC if intentional")

    def test_list_channels_stale_filter_uses_mtime_not_ctime(self):
        # Subtle: an append updates mtime but NOT ctime on some filesystems.
        # The filter must use mtime so an old file that just received a new
        # message stays listed. Guard structurally by reading the function
        # body — must reference st_mtime, not st_ctime.
        import inspect
        src = inspect.getsource(serve.list_channels)
        self.assertIn("st_mtime", src,
            "list_channels must filter by st_mtime — an append updates mtime")
        self.assertNotIn("st_ctime", src,
            "list_channels must NOT use st_ctime — an append doesn't bump it on every fs")

    def test_list_channels_skips_unsafe_names_and_non_ndjson(self):
        # Anything outside [A-Za-z0-9_-]+ on the filename gets ignored — a
        # malicious or stray file in /tmp/claude-channels mustn't appear in
        # the picker.
        for nm in ("ok-name.ndjson", "../sneaky.ndjson", "with space.ndjson",
                   "dot.in.middle.ndjson", "readme.txt"):
            try:
                with open(os.path.join(self.dir, nm), "w") as f:
                    f.write("")
            except OSError:
                continue
        names = [c["name"] for c in serve.list_channels()]
        self.assertEqual(names, ["ok-name"],
            "only [A-Za-z0-9_-]+ basenames must be listed, got %r" % names)

    def test_list_channels_empty_dir_returns_empty_list(self):
        # CHANNELS_DIR not existing must NOT raise — the channel skill may
        # not have run yet on a fresh box; the menu just shows empty.
        shutil.rmtree(self.dir, ignore_errors=True)
        self.assertEqual(serve.list_channels(), [])

    def test_read_channel_returns_messages_since_offset(self):
        p = os.path.join(self.dir, "ch.ndjson")
        with open(p, "w") as f:
            for i in range(5):
                f.write('{"from":"a","ts":%d,"text":"m%d"}\n' % (i, i))
        data, code = serve.read_channel("ch", 2)
        self.assertEqual(code, 200)
        self.assertEqual(data["total"], 5)
        self.assertEqual([m["text"] for m in data["messages"]], ["m2", "m3", "m4"])

    def test_read_channel_rejects_invalid_name(self):
        for bad in ("../etc/passwd", "../sneaky", "foo/bar", "with space",
                    "", None):
            data, code = serve.read_channel(bad, 0)
            self.assertEqual(code, 400,
                "invalid name %r must 400, got %d" % (bad, code))

    def test_read_channel_missing_file_returns_empty(self):
        # Reading a never-created channel must return empty + 200, not 404.
        # The chatroom UI polls from page-load before any message exists.
        data, code = serve.read_channel("brand-new", 0)
        self.assertEqual(code, 200)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["messages"], [])

    def test_read_channel_skips_invalid_json_lines(self):
        # NDJSON readers should be forgiving: a corrupt line shouldn't drop
        # the whole conversation.
        p = os.path.join(self.dir, "mixed.ndjson")
        with open(p, "w") as f:
            f.write('{"from":"a","ts":1,"text":"ok"}\n')
            f.write('not-json garbage\n')
            f.write('{"from":"b","ts":2,"text":"also ok"}\n')
        data, code = serve.read_channel("mixed", 0)
        self.assertEqual(code, 200)
        self.assertEqual([m["text"] for m in data["messages"]], ["ok", "also ok"])
        # Total counts ALL lines (including the bad one) — that's the
        # offset the iframe must pass on the next poll to stay in sync.
        self.assertEqual(data["total"], 3)

    def test_append_channel_writes_ndjson_with_canonical_shape(self):
        ok = serve.append_channel("ch", "fab", "hello world")
        self.assertTrue(ok)
        with open(os.path.join(self.dir, "ch.ndjson")) as f:
            line = f.read().strip()
        record = json.loads(line)
        # Skill protocol expects {from, ts, text}; readers (other claude
        # agents, the chatroom JS) rely on this shape.
        self.assertEqual(record["from"], "fab")
        self.assertEqual(record["text"], "hello world")
        self.assertIsInstance(record["ts"], int)

    def test_append_channel_rejects_invalid_name_or_empty_text(self):
        self.assertFalse(serve.append_channel("../bad", "fab", "x"))
        self.assertFalse(serve.append_channel("ok", "", "x"))
        self.assertFalse(serve.append_channel("ok", "fab", ""))
        self.assertFalse(serve.append_channel("ok", None, "x"))

    def test_safe_filename_pattern_blocks_traversal(self):
        # _channel_path is the gate that stops a malicious name from
        # escaping CHANNELS_DIR. None is the well-known sentinel.
        for bad in ("..", "../..", "x/y", "a.b", ""):
            self.assertIsNone(serve._channel_path(bad),
                "name %r must be rejected by _channel_path" % bad)
        # Valid names round-trip into a path INSIDE CHANNELS_DIR.
        p = serve._channel_path("good_name-1")
        assert p is not None
        self.assertEqual(os.path.commonpath([p, self.dir]), self.dir)


class ChannelsHTTPTest(unittest.TestCase):
    """HTTP-level coverage routed through the live serve.py subprocess.
    Verifies /api/channels lists, /api/channel/<name> GET reads, POST
    appends with CSRF, /channel/<name> serves the chatroom HTML, and
    /api/new?kind=channel registers a channel tile in the registry."""

    def _csrf(self):
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', body.decode())
        self.assertIsNotNone(m); assert m is not None
        return m.group(1)

    def _post_json(self, path, payload, csrf=None):
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        try:
            body = json.dumps(payload).encode()
            headers = {"Host": HOST_HDR, "Content-Type": "application/json",
                       "Content-Length": str(len(body))}
            if csrf:
                headers["X-CSRF-Token"] = csrf
            conn.request("POST", path, body=body, headers=headers)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def test_api_channel_post_requires_csrf(self):
        status, _ = self._post_json("/api/channel/test", {"from": "x", "text": "y"})
        self.assertEqual(status, 403)

    def test_channel_page_rejects_unsafe_name(self):
        for bad in ("..%2F..%2Fetc%2Fpasswd", "a.b", "with%20space"):
            status, _ = _get("/channel/" + bad, host=HOST_HDR)
            self.assertEqual(status, 400,
                "/channel/%s must 400, got %d" % (bad, status))

    def test_channel_page_returns_chatroom_html_for_safe_name(self):
        status, body = _get("/channel/test_smoke", host=HOST_HDR)
        self.assertEqual(status, 200)
        html = body.decode()
        # Must embed the channel name AND the CSRF meta (chatroom JS posts
        # back using it). And the /api/channel/ POST URL the chatroom hits.
        self.assertIn("<title>#test_smoke</title>", html)
        self.assertIn('<meta name="csrf-token"', html)
        self.assertIn("/api/channel/", html)

    def test_channel_page_prefills_who_with_default_user(self):
        # User-reported "Send button doesn't work": on first open the `who`
        # input was empty, updateSendState() required it to be non-empty,
        # and the user didn't notice the small "as ___" field in the
        # header. Fix: server injects DEFAULT_WHO (getpass.getuser()) into
        # the input's `value` so the field is non-empty by default.
        # Without this, click-Send AND Enter-to-send both silently no-op.
        status, body = _get("/channel/test_smoke", host=HOST_HDR)
        self.assertEqual(status, 200)
        html = body.decode()
        # The `who` input must have a non-empty value attribute (i.e. the
        # substitution actually fired and produced a real string).
        m = re.search(r'<input id="who"[^>]*\bvalue="([^"]*)"', html)
        self.assertIsNotNone(m, "channel page's #who input must have a value= attribute")
        assert m is not None
        self.assertTrue(m.group(1).strip(),
            "DEFAULT_WHO substitution produced an empty string — Send would stay disabled")
        # And the literal placeholder must NOT linger (would mean the
        # substitution was skipped).
        self.assertNotIn("__DEFAULT_WHO__", html,
            "__DEFAULT_WHO__ placeholder not substituted in /channel/ response")

    def test_channel_page_localstorage_override_beats_default(self):
        # The pre-fill must NOT clobber a per-channel name the user set
        # earlier — that read as "my name keeps reverting" in the old
        # code if we'd just unconditionally overwrite on every load. The
        # JS conditional is `if (saved) who.value = saved;` — guard the
        # `if (saved)` shape so a refactor doesn't accidentally drop it.
        status, body = _get("/channel/test_smoke", host=HOST_HDR)
        html = body.decode()
        self.assertRegex(html, r"const saved = localStorage\.getItem\(WHO_KEY\)",
            "channel JS must read the saved per-channel name from localStorage")
        self.assertRegex(html, r"if \(saved\)\s*who\.value = saved",
            "channel JS must only apply the saved name when it's non-empty — "
            "otherwise an empty localStorage entry would wipe the server pre-fill")

    def test_channel_page_enter_key_sends_message(self):
        # Enter (without Shift) must call send(). Shift+Enter inserts a
        # newline — the Slack/Discord/claude-code convention. The wiring
        # is one keydown listener; guard its shape.
        status, body = _get("/channel/test_smoke", host=HOST_HDR)
        html = body.decode()
        # The keydown handler must check key === 'Enter' AND !e.shiftKey,
        # then preventDefault and call send().
        m = re.search(
            r"text\.addEventListener\(\s*['\"]keydown['\"][\s\S]+?\}\s*\)",
            html)
        self.assertIsNotNone(m, "channel JS missing the textarea keydown handler")
        assert m is not None
        handler = m.group(0)
        self.assertIn("'Enter'", handler)
        self.assertIn("!e.shiftKey", handler,
            "Shift+Enter must NOT send — it inserts a newline")
        self.assertIn("preventDefault", handler,
            "Enter handler must preventDefault to stop the textarea newline")
        self.assertRegex(handler, r"send\s*\(\s*\)",
            "Enter handler must call send()")

    def test_channel_page_send_state_does_not_require_who(self):
        # Send must be enabled as soon as text is non-empty (who has a
        # server-provided default). If updateSendState() requires BOTH to
        # be non-empty AND who could ever be empty (e.g. user deletes the
        # default), Send goes dead and the user is stuck — same bug we
        # just fixed. Allow the OR pattern (both required as long as who
        # is auto-filled), but reject the case where text is checked but
        # who is the leading required check that an empty value would
        # short-circuit on.
        status, body = _get("/channel/test_smoke", host=HOST_HDR)
        html = body.decode()
        # The updateSendState body — find it and assert the textarea
        # value is the FIRST thing checked, so emptiness of `who` alone
        # never blocks send without text also being empty.
        m = re.search(
            r"function updateSendState\(\)\s*\{([\s\S]+?)\n\s*\}", html)
        self.assertIsNotNone(m, "updateSendState() not found")
        assert m is not None
        body_js = m.group(1)
        self.assertRegex(body_js, r"!text\.value\.trim\(\)",
            "updateSendState must check the textarea — it's the primary gate")

    def test_channel_page_messages_have_visible_spacing(self):
        # User asked for more breathing room between messages. The .msg
        # rule's vertical padding had been 3px (cramped); bumped to >=6px
        # and a faint separator between adjacent messages. Guard the
        # padding floor so a future style edit doesn't silently revert.
        status, body = _get("/channel/test_smoke", host=HOST_HDR)
        html = body.decode()
        m = re.search(r"\.msg\s*\{[^}]*padding:\s*(\d+)px", html)
        self.assertIsNotNone(m, ".msg padding rule not found")
        assert m is not None
        px = int(m.group(1))
        self.assertGreaterEqual(px, 6,
            ".msg vertical padding must be >= 6px so adjacent messages "
            "don't visually collide (was 3px — user-reported as cramped)")
        # And the adjacent-sibling rule that draws the separator.
        self.assertRegex(html, r"\.msg \+ \.msg\s*\{[^}]*border-top",
            ".msg + .msg must draw a separator so messages read as discrete")

    def test_channel_tiles_bypass_workdir_tab_filter(self):
        # Channel tiles aren't project-scoped — they have no cwd, so they
        # don't match any workdir tab. Without an explicit bypass in
        # applyVisibility, a channel tile spawned while activeTab is set
        # sits in the DOM with display:none and clicking a row in the
        # Channels menu LOOKS like nothing happens (user-reported as
        # "tile doesn't open when clicking on channel"). The fix:
        # applyVisibility treats kind=channel as a floating tile that
        # shows in every tab.
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        html = body.decode()
        # The applyVisibility function must check kind=channel BEFORE the
        # activeTab equality. Loose match — we want to see both the
        # dataset.kind read AND the 'channel' literal in the same logic
        # block, then a disjunction with the tab check.
        m = re.search(r"function\s+applyVisibility\s*\(\s*\)\s*\{", html)
        self.assertIsNotNone(m, "applyVisibility() not found")
        assert m is not None
        body_js = html[m.end():m.end() + 1200]
        self.assertRegex(body_js,
            r"dataset\.kind\s*===\s*['\"]channel['\"]",
            "applyVisibility must check dataset.kind === 'channel' to "
            "bypass the workdir-tab filter")
        # And the tile-creation site must SET dataset.kind so the check
        # above has something to read.
        self.assertIn("el.dataset.kind = s.kind", html,
            "tile creation must set dataset.kind = s.kind so "
            "applyVisibility's bypass check sees 'channel'")

    def test_dashboard_html_has_channels_menu(self):
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        html = body.decode()
        # Header pill + menu container + the +New button option for channels.
        self.assertIn('id="chBtn"', html)
        self.assertIn('id="chMenu"', html)
        self.assertIn('data-kind="channel"', html)
        # CSS for the channel-kind badge so tiles render in the right colour.
        self.assertIn('.badge.channel', html)

    def test_api_new_kind_channel_registers_tile(self):
        # /api/new?kind=channel&name=<nm> must create a registry entry with
        # kind=channel + channel=<nm>. Without this the Channels menu can't
        # actually open a tile.
        token = self._csrf()
        # Write a real channel file so list_channels picks it up too.
        chdir = os.environ.get("CLAUDE_CHANNELS_DIR")
        # The subprocess fixture is started without a custom CHANNELS_DIR
        # override, so files land in /tmp/claude-channels by default. We
        # can't easily inject a per-test dir into the subprocess after the
        # fact — just spawn the tile and check the registry entry; no need
        # to backstop with a real channel file.
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        try:
            headers = {"Host": HOST_HDR, "X-CSRF-Token": token,
                       "Content-Length": "0"}
            conn.request("POST", "/api/new?kind=channel&name=httptest_chan",
                         body=b"", headers=headers)
            r = conn.getresponse()
            body = r.read()
            self.assertEqual(r.status, 200, body)
            ret = json.loads(body)
            sid = ret["id"]
        finally:
            conn.close()
        # Registry entry should exist with the right shape.
        path = os.path.join(_tmpdir, sid + ".json")
        try:
            with open(path) as f:
                rec = json.load(f)
            self.assertEqual(rec["kind"], "channel")
            self.assertEqual(rec["channel"], "httptest_chan")
            self.assertEqual(rec["name"], "#httptest_chan")
            self.assertFalse(rec.get("port"),
                "channel tiles must not have a port — they're dashboard-served")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def test_api_new_kind_channel_rejects_invalid_name(self):
        token = self._csrf()
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        try:
            headers = {"Host": HOST_HDR, "X-CSRF-Token": token,
                       "Content-Length": "0"}
            conn.request("POST", "/api/new?kind=channel&name=" +
                         urllib.parse.quote("../traversal", safe=""),
                         body=b"", headers=headers)
            r = conn.getresponse()
            body = r.read()
            # spawn_channel_tile returns None → /api/new replies 500 with
            # {ok:false, id:null} (matches the existing convention for
            # failed spawns).
            self.assertEqual(r.status, 500)
            data = json.loads(body)
            self.assertFalse(data["ok"])
        finally:
            conn.close()


class NotesHTTPTest(unittest.TestCase):
    """HTTP-level coverage of the note tile routes via the live serve.py
    subprocess: /api/new?kind=note registers a port-less tile, /note/<id>
    serves the editor page, and GET/POST /api/note/<id> round-trips the body
    (CSRF-guarded, sid-validated)."""

    def _csrf(self):
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', body.decode())
        self.assertIsNotNone(m); assert m is not None
        return m.group(1)

    def _post_json(self, path, payload, csrf=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        try:
            body = raw if raw is not None else json.dumps(payload).encode()
            headers = {"Host": HOST_HDR, "Content-Type": "application/json",
                       "Content-Length": str(len(body))}
            if csrf:
                headers["X-CSRF-Token"] = csrf
            conn.request("POST", path, body=body, headers=headers)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def _new_note(self, csrf):
        status, body = _post("/api/new?kind=note&cwd=/tmp/notetest",
                             host=HOST_HDR, csrf=csrf)
        self.assertEqual(status, 200, body)
        sid = json.loads(body)["id"]
        self.addCleanup(lambda: _post("/api/close?id=" + sid, host=HOST_HDR, csrf=csrf))
        return sid

    def test_api_new_note_registers_and_lists_portless(self):
        sid = self._new_note(self._csrf())
        status, body = _get("/api/sessions", host=HOST_HDR)
        self.assertEqual(status, 200)
        recs = [s for s in json.loads(body)["sessions"] if s["id"] == sid]
        self.assertEqual(len(recs), 1, "note tile missing from /api/sessions")
        self.assertEqual(recs[0]["kind"], "note")
        self.assertFalse(recs[0].get("port"),
                         "note tiles are dashboard-served — must have no port")

    def test_note_body_roundtrip_over_http(self):
        token = self._csrf()
        sid = self._new_note(token)
        status, _ = self._post_json("/api/note/" + sid, {"html": "<b>hi</b>note"}, csrf=token)
        self.assertEqual(status, 200)
        status, body = _get("/api/note/" + sid, host=HOST_HDR)
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), "<b>hi</b>note")

    def test_api_note_body_served_non_rendering(self):
        # The body is user-authored and served on the dashboard's own origin
        # (which holds the CSRF token). It must NOT be served as text/html —
        # otherwise opening /api/note/<id> directly (or a local process writing
        # a <script> into the sidecar) is stored XSS. Require text/plain +
        # nosniff so the endpoint can never be rendered as HTML.
        token = self._csrf()
        sid = self._new_note(token)
        self._post_json("/api/note/" + sid, {"html": "<script>x()</script>"}, csrf=token)
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        try:
            conn.request("GET", "/api/note/" + sid, headers={"Host": HOST_HDR})
            r = conn.getresponse()
            r.read()
            ctype = (r.getheader("Content-Type") or "").lower()
            nosniff = (r.getheader("X-Content-Type-Options") or "").lower()
        finally:
            conn.close()
        self.assertNotIn("text/html", ctype,
                         "note body must not be served as renderable HTML")
        self.assertIn("text/plain", ctype)
        self.assertEqual(nosniff, "nosniff",
                         "note body response must set X-Content-Type-Options: nosniff")

    def test_api_note_post_requires_csrf(self):
        status, _ = self._post_json("/api/note/note-1", {"html": "x"})  # no token
        self.assertEqual(status, 403)

    def test_api_note_rejects_invalid_sid(self):
        token = self._csrf()
        for bad in ("note-abc", "notexyz", "host-1"):
            gs, _ = _get("/api/note/" + bad, host=HOST_HDR)
            self.assertEqual(gs, 400, "GET /api/note/%s must 400" % bad)
            ps, _ = self._post_json("/api/note/" + bad, {"html": "x"}, csrf=token)
            self.assertEqual(ps, 400, "POST /api/note/%s must 400" % bad)

    def test_api_note_post_rejects_bad_payload(self):
        token = self._csrf()
        sid = self._new_note(token)
        miss, _ = self._post_json("/api/note/" + sid, {"nope": 1}, csrf=token)
        self.assertEqual(miss, 400, "missing {html} must 400")
        bad, _ = self._post_json("/api/note/" + sid, None, csrf=token, raw=b"{not json")
        self.assertEqual(bad, 400, "non-JSON body must 400")

    def test_api_note_post_rejects_oversize(self):
        # The handler 413s on Content-Length over the cap before reading the
        # body. Claim an oversize length with a tiny body so the check fires fast.
        token = self._csrf()
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        try:
            headers = {"Host": HOST_HDR, "X-CSRF-Token": token,
                       "Content-Type": "application/json",
                       "Content-Length": str(serve.NOTE_MAX_BYTES + 1)}
            conn.request("POST", "/api/note/note-1", body=b"{}", headers=headers)
            self.assertEqual(conn.getresponse().status, 413)
        finally:
            conn.close()

    def test_note_page_serves_editor_html(self):
        # The page serves for any well-formed sid (body GET is empty if none).
        status, body = _get("/note/note-1", host=HOST_HDR)
        self.assertEqual(status, 200)
        html = body.decode()
        self.assertIn('contenteditable', html)
        self.assertIn('"note-1"', html, "sid must be substituted into the page")
        self.assertNotIn("__NOTE_SID__", html, "sid placeholder not substituted")
        self.assertIn('<meta name="csrf-token"', html)
        self.assertIn("/api/note/", html, "editor must POST its body back")
        self.assertIn("function sanitize", html,
                      "editor must sanitize before applying innerHTML (stored-XSS guard)")

    def test_note_page_rejects_invalid_sid(self):
        for bad in ("note-abc", "../etc/passwd", "nope"):
            status, _ = _get("/note/" + urllib.parse.quote(bad, safe=""), host=HOST_HDR)
            self.assertEqual(status, 400, "/note/%s must 400" % bad)

    def test_dashboard_has_note_tile_wiring(self):
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        html = body.decode()
        self.assertIn('data-kind="note"', html, "new-tile menu missing the Note button")
        self.assertIn('.badge.note', html, "missing note badge CSS")
        self.assertIn("'/note/'", html, "render() must build the /note/<id> iframe url")
        self.assertRegex(html, r"isNote\s*=\s*s\.kind === 'note'",
                         "render() must branch on the note kind")


class ScrollbackE2ETest(unittest.TestCase):
    """End-to-end scrollback persist+restore round-trip. Spawns a REAL ttyd
    serving the built `term.html` with a known output script, drives it via
    headless Chromium (Playwright), then asserts the saved scrollback comes
    back across a page reload.

    Why an actual browser test: the persist+restore logic lives in
    term-client.js and only runs in a real DOM with a real WebSocket and
    real localStorage. Every previous scrollback regression in this
    project (SGR-state leak; restore-loop content duplication; the recent
    socket.close() in refresh that wiped buffer via ttyd reattach's
    clear-scrollback init) would have been caught here in seconds. The
    static-source tests in TermClientScrollbackTest are necessary but not
    sufficient — they assert what the code SAYS, not what the browser DOES.

    Setup (one-time):
      cd session-dashboard
      python3 -m venv .venv-test
      .venv-test/bin/pip install playwright
      .venv-test/bin/playwright install chromium
      brew install ttyd  # if you don't have it

    Run:
      .venv-test/bin/python3 -m unittest test_serve.ScrollbackE2ETest -v

    Skipped gracefully when run with a Python that lacks playwright or when
    ttyd isn't on PATH — the suite stays green for everyone else."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("ttyd"):
            raise unittest.SkipTest(
                "ttyd not installed (brew install ttyd) — this is the test "
                "we use to spawn a real terminal backend; without it the "
                "round-trip can't be exercised end-to-end")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest(
                "playwright not importable in this Python — install into the "
                "test venv: `python3 -m venv .venv-test && "
                ".venv-test/bin/pip install playwright && "
                ".venv-test/bin/playwright install chromium`, then run this "
                "suite with `.venv-test/bin/python3 -m unittest test_serve`")
        # staticmethod wraps the import so `self._sync_playwright()` doesn't
        # try to bind `self` as the first arg — sync_playwright takes none.
        cls._sync_playwright = staticmethod(sync_playwright)

    # Two fixture kinds, mirroring the two real replay shapes term-client must
    # tell apart (see term-client.js writeOutput / _looksLikeTuiRepaint):
    #   SHELL — ttyd re-dumps the whole scrollback as bare text on reattach
    #     (no leading clear). Restore must be SKIPPED or the history duplicates.
    #   TUI   — claude/vim repaint a cleared screen on attach; the replay leads
    #     with ESC[2J/ESC[H and carries NO scrollback, so restore is the ONLY
    #     way history survives a reload.
    _SHELL_SCRIPT = ('for i in $(seq 1 60); do echo "scroll-e2e-line-$i"; done; '
                     'exec cat')
    _TUI_SCRIPT = ("printf '\\033[2J\\033[HTUI-LIVE-SCREEN\\r\\n'; exec cat")
    # TUI whose reattach repaint ERASES the scrollback — the real "scrollback
    # gone again, even ↻ reload won't bring it back" report. ttyd spawns a fresh
    # `bash -c` per connection, so this runs on every (re)attach: it leads with
    # home + clear-screen (so the client classifies it as a TUI repaint and
    # restores), then emits ESC[3J (erase scrollback), which — without the fix —
    # wipes the history restore had just written back. claude does this on
    # reattach; `exec cat` keeps the PTY open.
    _TUI_CLEAR_SCROLLBACK_SCRIPT = (
        "printf '\\033[H\\033[2J\\033[3JTUI-REATTACH-SCREEN\\r\\n'; exec cat")
    # Same, but via ESC c (RIS / full reset), which ALSO clears scrollback. The
    # fix rewrites RIS to a scrollback-preserving clear+home.
    _TUI_RIS_SCRIPT = ("printf '\\033cTUI-RIS-SCREEN\\r\\n'; exec cat")
    # Terminal that boots already in mouse-tracking mode (1000 click + 1003
    # any-motion + 1006 SGR) — the condition the newer claude clients (fixed
    # prompt input) and opencode put the terminal in, which made xterm swallow
    # the wheel from a bubble-phase forwarder. `exec cat` keeps the PTY open.
    _MOUSE_MODE_SCRIPT = ("printf '\\033[?1000h\\033[?1003h\\033[?1006h'; exec cat")
    # Enter the alternate screen + mouse tracking (the newer-claude condition),
    # then ring a BEL after 1s. Guards that bell DETECTION survives that state.
    # Ring 2s after connect — past the client's 1.5s attach-replay bell mute
    # (BELL_CONNECT_MUTE_MS), which deliberately swallows bells in the replay
    # window. This test exercises propagation of a LIVE bell, not the mute.
    _BELL_SCRIPT = ("printf '\\033[?1049h\\033[?1000h\\033[?1003h\\033[?1006h'; "
                    "sleep 2; printf '\\a'; exec cat")

    def setUp(self):
        self._procs = []

    def tearDown(self):
        for proc in self._procs:
            try:
                proc.terminate(); proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _start_ttyd(self, script):
        """Launch ttyd serving term.html running `script` (bash -c). `exec cat`
        keeps the PTY open so the WebSocket doesn't close mid-test. Returns the
        port. Bind-then-release races are unlikely enough on localhost to skip a
        lockfile dance."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        proc = subprocess.Popen(
            ["ttyd", "-p", str(port), "-I",
             os.path.join(HERE, "term.html"),
             "bash", "-c", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(proc)
        for _ in range(60):
            try:
                with socket.create_connection(("127.0.0.1", port), 0.1):
                    return port
            except OSError:
                time.sleep(0.1)
        self.fail("ttyd didn't bind on port %d within 6 s" % port)

    # Shared helpers (each test runs its own browser context to keep
    # localStorage / cookies isolated and avoid cross-test interference).

    def _iframe_url(self, port, sid="scroll-e2e", ts="2026-05-31T00:00:00Z"):
        return "http://127.0.0.1:%d/?sid=%s&ts=%s" % (port, sid, ts)

    _WAIT_FOR_TEST_LINES = """
        () => window.__claudeTerm && (() => {
            const b = window.__claudeTerm.buffer.active;
            for (let y = 0; y < b.length; y++) {
                const l = b.getLine(y);
                if (l && l.translateToString(true).includes('scroll-e2e-line-30')) return true;
            }
            return false;
        })()
    """

    _BUFFER_TO_STRING = """
        () => {
            const b = window.__claudeTerm.buffer.active;
            const out = [];
            for (let y = 0; y < b.length; y++) {
                const l = b.getLine(y);
                if (l) out.push(l.translateToString(true));
            }
            return out.join('\\n');
        }
    """

    # ---- the actual tests ----

    def test_shell_tile_does_not_duplicate_scrollback_on_reload(self):
        """Plain-shell tile: ttyd re-dumps its full scrollback as bare text on
        every (re)attach. Restoring localStorage ON TOP of that dump duplicated
        the buffer, compounding 200→400→600 lines on each reload (user-reported
        "scroll buffer repeats the same text over and over"). term-client must
        SKIP restore for such a tile.

        We seed a localStorage snapshot (with a JS-only marker ttyd never sends)
        BEFORE reloading. If restore wrongly runs, the marker reappears and/or
        the shell lines double. Assert: each line shows exactly once, and the
        marker is absent — proving restore was correctly skipped."""
        MARKER = "___JS_ONLY_RESTORE_MARKER_shell_zk9hp4q2nm___"
        SID, TS = "scroll-e2e", "2026-05-31T00:00:00Z"
        port = self._start_ttyd(self._SHELL_SCRIPT)
        with self._sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1024, "height": 600})
                page = ctx.new_page()
                page.goto(self._iframe_url(port, sid=SID, ts=TS),
                          wait_until="domcontentloaded")
                page.wait_for_function(self._WAIT_FOR_TEST_LINES, timeout=10000)
                # Seed a v2 snapshot containing the marker (and remove any v3) so
                # restore — IF it ran — would have something to bring back. v2 is
                # plain text; _applyRestored just term.write()s it.
                page.evaluate(
                    "([m, sid, ts]) => {"
                    "  localStorage.setItem('claude-term-scrollback:v2:'+sid+'|'+ts,"
                    "    '\\r\\n'+m+'\\r\\nSEEDED-HISTORY-LINE\\r\\n');"
                    "  localStorage.removeItem('claude-term-scrollback:v3:'+sid+'|'+ts);"
                    "}", [MARKER, SID, TS])
                page.reload(wait_until="domcontentloaded")
                page.wait_for_function(self._WAIT_FOR_TEST_LINES, timeout=10000)
                # Give any (erroneous) async restore a beat to land before we read.
                page.wait_for_timeout(500)
                lines = page.evaluate(self._BUFFER_TO_STRING)
                self.assertEqual(lines.count("scroll-e2e-line-30"), 1,
                    "shell scrollback duplicated on reload — restore ran on top "
                    "of ttyd's replay (got %d copies of line-30)"
                    % lines.count("scroll-e2e-line-30"))
                self.assertNotIn(MARKER, lines,
                    "seeded marker came back after reload — restore ran for a "
                    "bare-text (shell) replay; it must be skipped so ttyd's "
                    "replay isn't duplicated")
            finally:
                browser.close()

    def test_tui_tile_restores_scrollback_on_reload(self):
        """TUI tile (claude/vim): repaints a cleared screen on attach, so ttyd's
        replay carries NO scrollback — localStorage restore is the ONLY way
        history survives a reload. The full chain: xterm buffer → serialize →
        gzip → localStorage v3 → reload → gunzip → term.write.

        A JS-only marker (ttyd never sees it) proves restoration genuinely ran:
        for a TUI replay it can ONLY come back via restore. We place it well
        above the viewport (followed by filler) so the reattach repaint's
        screen-clear can't erase it from the visible rows."""
        MARKER = "___JS_ONLY_RESTORE_MARKER_tui_zk9hp4q2nm___"
        SID = "tui-e2e"
        port = self._start_ttyd(self._TUI_SCRIPT)
        with self._sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1024, "height": 600})
                page = ctx.new_page()
                page.goto(self._iframe_url(port, sid=SID), wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => window.__claudeTerm && (() => {"
                    "  const b = window.__claudeTerm.buffer.active;"
                    "  for (let y = 0; y < b.length; y++) { const l = b.getLine(y);"
                    "    if (l && l.translateToString(true).includes('TUI-LIVE-SCREEN')) return true; }"
                    "  return false; })()", timeout=10000)
                # Inject the marker, then enough filler that it sits in scrollback
                # ABOVE the viewport (the reattach repaint clears the visible
                # rows; scrollback above must survive).
                page.evaluate("""(m) => {
                    const t = window.__claudeTerm;
                    t.write('\\r\\n' + m + '\\r\\n');
                    for (let i = 0; i < 40; i++) t.write('tui-filler-' + i + '\\r\\n');
                }""", MARKER)
                # Persist (canPersist is true for a TUI tile that restored).
                page.evaluate("""() => {
                    Object.defineProperty(document, 'hidden', {
                        configurable: true, get: () => true });
                    document.dispatchEvent(new Event('visibilitychange'));
                }""")
                page.wait_for_function(
                    "() => Object.keys(localStorage).some(k => "
                    "k.indexOf('claude-term-scrollback:v3:tui-e2e|') === 0)",
                    timeout=5000)
                saved_len = page.evaluate(
                    "() => Object.entries(localStorage).find(([k]) => "
                    "k.indexOf('claude-term-scrollback:v3:tui-e2e|') === 0)[1].length")
                self.assertGreater(saved_len, 100,
                    "persisted blob looks empty (got %d bytes)" % saved_len)
                page.reload(wait_until="domcontentloaded")
                try:
                    page.wait_for_function("""(m) => {
                        if (!window.__claudeTerm) return false;
                        const b = window.__claudeTerm.buffer.active;
                        for (let y = 0; y < b.length; y++) { const l = b.getLine(y);
                            if (l && l.translateToString(true).includes(m)) return true; }
                        return false;
                    }""", arg=MARKER, timeout=5000)
                except Exception:
                    pass   # let the assertion below produce the proper failure
                lines = page.evaluate(self._BUFFER_TO_STRING)
                self.assertIn(MARKER, lines,
                    "JS-only marker missing after reload — restore didn't run for "
                    "a TUI (cleared-screen) replay. claude tiles would lose all "
                    "scrollback. Saved blob was %d bytes." % saved_len)
            finally:
                browser.close()

    def _assert_history_survives_reattach_clear(self, script, sid):
        """Shared body for the two reattach-clears-scrollback regressions. Seed a
        TUI tile, persist a marker high in scrollback, reload (= reattach, which
        re-runs `script` and so re-emits its scrollback-erase), and assert the
        restored marker survives. WITHOUT the _stripScrollbackClear fix the
        reattach repaint's ESC[3J / RIS wipes the restore and this fails."""
        MARKER = "___JS_ONLY_RESTORE_MARKER_reattach_clear_zk9hp4q2nm___"
        port = self._start_ttyd(script)
        with self._sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1024, "height": 600})
                page = ctx.new_page()
                page.goto(self._iframe_url(port, sid=sid), wait_until="domcontentloaded")
                page.wait_for_function("() => !!window.__claudeTerm", timeout=10000)
                page.wait_for_timeout(400)   # let the first repaint land
                # Marker well above the viewport, then filler so the reattach
                # repaint's screen-clear can't reach it — only an erased SCROLLBACK
                # (the bug) removes it.
                page.evaluate("""(m) => {
                    const t = window.__claudeTerm;
                    t.write('\\r\\n' + m + '\\r\\n');
                    for (let i = 0; i < 40; i++) t.write('reattach-filler-' + i + '\\r\\n');
                }""", MARKER)
                page.evaluate("""() => {
                    Object.defineProperty(document, 'hidden', {
                        configurable: true, get: () => true });
                    document.dispatchEvent(new Event('visibilitychange'));
                }""")
                page.wait_for_function(
                    "(sid) => Object.keys(localStorage).some(k => "
                    "k.indexOf('claude-term-scrollback:v3:'+sid+'|') === 0)",
                    arg=sid, timeout=5000)
                saved_len = page.evaluate(
                    "(sid) => Object.entries(localStorage).find(([k]) => "
                    "k.indexOf('claude-term-scrollback:v3:'+sid+'|') === 0)[1].length",
                    sid)
                self.assertGreater(saved_len, 100,
                    "persisted blob looks empty (got %d bytes)" % saved_len)
                # Reload == reattach: ttyd re-runs `script`, re-emitting the
                # scrollback-erase as part of the repaint.
                page.reload(wait_until="domcontentloaded")
                page.wait_for_function("() => !!window.__claudeTerm", timeout=10000)
                page.wait_for_timeout(800)
                lines = page.evaluate(self._BUFFER_TO_STRING)
                self.assertIn(MARKER, lines,
                    "restored scrollback was wiped by the reattach repaint's "
                    "ESC[3J / RIS — the 'scrollback gone again, ↻ reload won't "
                    "bring it back' bug. term-client must neutralize the "
                    "scrollback-erase in the reattach burst (saved blob was %d "
                    "bytes, so restore had data to bring back)." % saved_len)
            finally:
                browser.close()

    def test_reattach_esc3j_does_not_wipe_restored_scrollback(self):
        """Reattach repaint emitting ESC[3J (erase scrollback) must NOT erase the
        history we just restored from localStorage. This is the dynamic guard the
        old NOTE here said wasn't worth building — it is now: ttyd spawning a
        fresh `bash -c` per connection faithfully mimics claude re-emitting its
        scrollback-clear on every reattach."""
        self._assert_history_survives_reattach_clear(
            self._TUI_CLEAR_SCROLLBACK_SCRIPT, "clear3j-e2e")

    def test_reattach_ris_does_not_wipe_restored_scrollback(self):
        """Same regression via ESC c (RIS / full reset), which also clears
        scrollback. The fix rewrites RIS to a scrollback-preserving clear+home."""
        self._assert_history_survives_reattach_clear(
            self._TUI_RIS_SCRIPT, "ris-e2e")

    # NOTE: the socket.close()-in-refresh variant of scrollback loss (ttyd's
    # reattach init can carry a clear) stays guarded by a static-source check in
    # KeybindingsTest.test_term_client_refresh_swaps_renderer_without_touching_socket
    # — the renderer-swap refresh must never close the WebSocket.

    def test_horizontal_wheel_forwards_over_mouse_mode_tile(self):
        """Regression (browser-level): side-scroll over a tile "stopped working"
        on mouse-mode TUIs — the newer claude clients (fixed prompt input) and
        opencode. With mouse tracking on, xterm hands wheel events to the app, so
        a BUBBLE-phase forwarder never saw the horizontal swipe. The forwarder
        must run in the CAPTURE phase to claim a horizontal swipe before xterm.

        A/B that motivated this (run by hand against a live tile): bubble+passive
        scrolled 0 px under mouse mode; capture scrolled 150. Here we embed
        term.html (booted INTO mouse mode) in an iframe and collect the
        {key:'wheel-x'} posts the client mirrors up. A horizontal wheel must
        forward; a vertical wheel must NOT (it stays with xterm's scrollback);
        shift+wheel (a vertical delta, treated as horizontal) must forward.
        The source-level guard is
        KeybindingsTest.test_term_client_wheel_forwarder_runs_in_capture_phase."""
        port = self._start_ttyd(self._MOUSE_MODE_SCRIPT)
        iframe_url = self._iframe_url(port, sid="wheel-e2e")
        with self._sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1024, "height": 700})
                page = ctx.new_page()
                # Minimal "dashboard": an iframe holding the terminal + a collector
                # for the wheel-x posts the embedded client mirrors upward. (The
                # forwarder only fires when embedded — window.parent !== window.)
                page.set_content(
                    "<!doctype html><meta charset=utf8>"
                    "<style>html,body{margin:0;height:100%%}"
                    "#f{width:600px;height:560px;border:0}</style>"
                    "<iframe id=f src='%s'></iframe>"
                    "<script>window.__wheelx=[];addEventListener('message',function(e){"
                    "var d=e.data;if(d&&d.type==='claude-term'&&d.key==='wheel-x')"
                    "window.__wheelx.push(d.dx)})</script>" % iframe_url)
                # Wait for the embedded term-client to boot (sets window.__claudeTerm).
                fr = None
                for _ in range(60):
                    fr = next((f for f in page.frames
                               if f.url.startswith("http://127.0.0.1:%d" % port)), None)
                    if fr:
                        break
                    page.wait_for_timeout(100)
                self.assertIsNotNone(fr, "terminal iframe frame not found")
                fr.wait_for_function("() => !!window.__claudeTerm", timeout=10000)
                page.wait_for_timeout(800)

                box = page.locator("#f").bounding_box()
                cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                page.mouse.move(cx, cy)

                # Horizontal swipe over the mouse-mode tile → MUST forward.
                page.mouse.wheel(140, 0)
                page.wait_for_timeout(250)
                after_horiz = page.evaluate("() => window.__wheelx.slice()")
                self.assertTrue(after_horiz and after_horiz[-1] > 0,
                    "horizontal swipe over a mouse-mode tile did not forward "
                    "wheel-x — a bubble-phase listener gets swallowed by xterm's "
                    "mouse mode; the forwarder must capture (got %r)" % (after_horiz,))

                # Vertical wheel → must NOT forward (stays with xterm scrollback).
                n = len(after_horiz)
                page.mouse.wheel(0, 220)
                page.wait_for_timeout(250)
                after_vert = page.evaluate("() => window.__wheelx.slice()")
                self.assertEqual(len(after_vert), n,
                    "vertical wheel was forwarded as wheel-x — it must stay with "
                    "xterm's scrollback (got %r)" % (after_vert,))

                # Shift+wheel (vertical delta, treated as horizontal) → MUST forward.
                page.keyboard.down("Shift")
                page.mouse.wheel(0, 120)
                page.keyboard.up("Shift")
                page.wait_for_timeout(250)
                after_shift = page.evaluate("() => window.__wheelx.slice()")
                self.assertEqual(len(after_shift), n + 1,
                    "shift+wheel must forward as horizontal (got %r)" % (after_shift,))
            finally:
                browser.close()

    def test_bel_propagates_to_dashboard_under_mouse_mode(self):
        """A BEL that reaches a tile's terminal must propagate up to the
        dashboard as a {bell:true} postMessage — even when the terminal is in the
        newer-claude condition (alternate screen + mouse tracking). Regression
        guard for a "bell detection is broken" report.

        NB on that report: claude CAPTURES a Bash tool's stdout, so a plain
        `printf '\\a'` run from inside claude never reaches the controlling tty
        and so can't be detected — to ring from inside claude the command must
        write to the tty (`printf '\\a' > /dev/tty`), which is exactly what the
        Stop/Notification bell hook does. This test exercises the dashboard side:
        a BEL that actually reaches the terminal must be seen."""
        port = self._start_ttyd(self._BELL_SCRIPT)
        iframe_url = self._iframe_url(port, sid="bell-e2e")
        with self._sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1024, "height": 700})
                page = ctx.new_page()
                # The forwarder only fires when embedded (window.parent !== window),
                # so wrap the terminal in an iframe and collect its bell posts.
                page.set_content(
                    "<!doctype html><meta charset=utf8>"
                    "<iframe id=f style='width:600px;height:560px;border:0' src='%s'></iframe>"
                    "<script>window.__bells=[];addEventListener('message',function(e){"
                    "var d=e.data;if(d&&d.type==='claude-term'&&d.bell)window.__bells.push(1)})"
                    "</script>" % iframe_url)
                fr = None
                for _ in range(60):
                    fr = next((f for f in page.frames
                               if f.url.startswith("http://127.0.0.1:%d/?sid" % port)), None)
                    if fr:
                        break
                    page.wait_for_timeout(100)
                self.assertIsNotNone(fr, "terminal iframe frame not found")
                fr.wait_for_function("() => !!window.__claudeTerm", timeout=10000)
                # Confirm the test setup really is the claude condition. The
                # alt-screen enter (ESC[?1049h) arrives in the first output burst,
                # which the client QUEUES behind the (async) scrollback restore
                # before writing — so poll for it rather than reading the buffer
                # type the instant __claudeTerm exists (that races the flush).
                fr.wait_for_function(
                    "() => window.__claudeTerm.buffer.active.type === 'alternate'",
                    timeout=8000)
                # The bash rings ~2s after connect (past the attach-replay
                # bell mute); allow margin.
                page.wait_for_function("() => window.__bells.length > 0", timeout=8000)
                self.assertGreaterEqual(page.evaluate("() => window.__bells.length"), 1,
                    "a BEL reaching the terminal must propagate to the dashboard as "
                    "{bell:true}, even in alt-screen + mouse mode")
            finally:
                browser.close()


class TileIconAndReloadTest(unittest.TestCase):
    """Per-tile Lucide icons (kind/keyword/cached-AI resolution + /api/icon) and
    the re-attach reload button + the renderer-swap warmup mask. A mix of
    in-process unit tests (serve.resolve_icon / _ai_pick_icon) and static-source
    checks against the served page + term-client.js (there's no JS unit harness)."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "term-client.js")) as f:
            cls.JS = f.read()
        with open(os.path.join(HERE, "term.html")) as f:
            cls.HTML = f.read()

    def _csrf(self):
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', body.decode())
        self.assertIsNotNone(m)
        assert m is not None
        return m.group(1)

    @staticmethod
    def _fn_body(src, name):
        """Return the brace-balanced body of `function <name>(...) { … }`."""
        m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
        if m is None:
            return None
        depth = 1
        for i in range(m.end(), len(src)):
            if src[i] == '{':
                depth += 1
            elif src[i] == '}':
                depth -= 1
                if depth == 0:
                    return src[m.end():i + 1]
        return src[m.end():]

    # ---- server-side icon resolution ------------------------------------
    def test_icon_whitelist_nonempty_and_consistent(self):
        self.assertIn("terminal", serve.ICON_NAMES)
        self.assertGreaterEqual(len(serve.ICON_NAMES), 20)
        icons = json.loads(serve.LUCIDE_ICONS_JSON)
        self.assertEqual(set(icons), set(serve.ICON_NAMES))
        self.assertTrue(all(v.strip() for v in icons.values()),
                        "every embedded icon must have inner SVG markup")

    def test_resolve_icon_empty_and_no_key(self):
        self.assertIsNone(serve.resolve_icon("", ""))   # empty title, no call
        orig = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            # No key → _ai_pick_icon yields None; resolve_icon returns + caches None.
            self.assertIsNone(serve.resolve_icon("unknown-xyzzy-title", "/x"))
        finally:
            if orig is not None:
                os.environ["ANTHROPIC_API_KEY"] = orig

    def test_ai_pick_validates_against_whitelist(self):
        # Stub the network: _ai_pick_icon must pull a whitelisted name out of a
        # noisy reply and reject anything off-list — it can never return junk.
        import urllib.request as _u

        class _Resp:
            def __init__(self, text):
                self._b = json.dumps({"content": [{"type": "text", "text": text}]}).encode()
            def read(self, *a):
                return self._b
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        orig_open = _u.urlopen
        orig_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

        def pick(reply):
            _u.urlopen = lambda req, timeout=None: _Resp(reply)
            return serve._ai_pick_icon("x", "y")
        try:
            self.assertEqual(pick("coins"), "coins")
            self.assertEqual(pick("Icon: layout-dashboard\n"), "layout-dashboard")
            self.assertEqual(pick('"terminal".'), "terminal")
            self.assertIsNone(pick("banana"))     # off-list
            self.assertIsNone(pick(""))           # empty reply
        finally:
            _u.urlopen = orig_open
            if orig_key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = orig_key

    # ---- /api/icon endpoint --------------------------------------------
    def test_api_icon_requires_csrf(self):
        # Spends an API call → must be CSRF-gated in the query like /proxy.
        status, _ = _get("/api/icon?title=foo", host=HOST_HDR)
        self.assertEqual(status, 403)

    def test_api_icon_returns_validated_name_or_null(self):
        token = self._csrf()
        path = "/api/icon?csrf=%s&title=%s&cwd=%s" % (
            urllib.parse.quote(token), urllib.parse.quote("session-dashboard"),
            urllib.parse.quote("/tmp"))
        status, body = _get(path, host=HOST_HDR)
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertIn("icon", d)
        self.assertTrue(d["icon"] is None or d["icon"] in serve.ICON_NAMES,
                        "icon must be null or a whitelisted name, got %r" % d["icon"])

    # ---- client wiring: icons + reload + warmup mask --------------------
    def test_page_wires_icons_and_reload(self):
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        text = body.decode()
        for needle in ("function paintIcon", "const LUCIDE_ICONS = {",
                       "ICON_KEYWORDS", "function keywordIcon",
                       "function reloadTile", "reloadBtn", "/api/icon?csrf="):
            self.assertIn(needle, text, "missing client wiring: %s" % needle)
        # Re-attach must be a real iframe reload (about:blank bounce), NOT
        # contentWindow.location.reload() — the iframe is cross-origin (ttyd
        # port), so reaching into its location would throw SecurityError.
        rt = self._fn_body(text, "reloadTile")
        self.assertIsNotNone(rt, "reloadTile() not found in page")
        self.assertIn("about:blank", rt)
        self.assertNotIn("contentWindow.location.reload", rt)
        # Shift+click = "clean reload": tells the client to forget saved
        # scrollback (clears pre-existing duplicated/garbled history) before
        # reattaching. Button passes e.shiftKey; reloadTile posts clear-scrollback.
        self.assertIn("reloadTile(s.id, e.shiftKey)", text)
        self.assertIn("clear-scrollback", rt)
        # client side: handle clear-scrollback by dropping its scrollback keys
        # and barring persist so the reload can't re-save the stale buffer.
        m = re.search(r"clear-scrollback'\)\s*\{(.*?)\}", self.JS, re.S)
        self.assertIsNotNone(m, "client must handle cmd:'clear-scrollback'")
        body = m.group(1)
        self.assertIn("removeItem(LSKEY_V2)", body)
        self.assertIn("removeItem(LSKEY_V3)", body)
        self.assertIn("canPersist = false", body)

    def test_warmup_mask_freezes_before_dispose(self):
        # useWebgl must snapshot the live 2D-canvas frame BEFORE disposing it
        # (after dispose the renderer falls back to DOM and the pixels are gone).
        for label, src in (("js", self.JS), ("html", self.HTML)):
            body = self._fn_body(src, "useWebgl")
            self.assertIsNotNone(body, "%s: useWebgl not found" % label)
            fpos = body.find("_freezeFrame()")
            dpos = body.find("canvasAddon.dispose()")
            self.assertGreaterEqual(fpos, 0, "%s: useWebgl must call _freezeFrame()" % label)
            self.assertGreaterEqual(dpos, 0, "%s: useWebgl must dispose canvasAddon" % label)
            self.assertLess(fpos, dpos,
                            "%s: _freezeFrame() must run BEFORE canvasAddon.dispose()" % label)
            self.assertIn("_thawFrame()", body, "%s: useWebgl must call _thawFrame()" % label)
        # _thawFrame needs a timer backstop so the overlay can't stick if rAF
        # is starved (e.g. tab backgrounded mid-swap).
        thaw = self._fn_body(self.JS, "_thawFrame")
        self.assertIsNotNone(thaw, "_thawFrame() definition missing")
        self.assertRegex(thaw, r"setTimeout\(",
                         "_thawFrame must have a setTimeout backstop")


class InlineImageTest(unittest.TestCase):
    """Inline-image support (Sixel + iTerm2 IIP) via xterm-addon-image. Static
    checks that the addon is fetched, inlined into the build, and loaded with
    both protocols enabled. End-to-end rendering (writing an IIP/Sixel sequence
    and seeing a `canvas.xterm-image-layer` mount) is verified manually via the
    browser; there's no JS unit harness."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "term-client.js")) as f:
            cls.JS = f.read()
        with open(os.path.join(HERE, "term.html")) as f:
            cls.HTML = f.read()
        with open(os.path.join(HERE, "build-term.sh")) as f:
            cls.BUILD = f.read()

    def test_image_addon_fetched_and_inlined(self):
        self.assertIn("xterm-addon-image@0.5.0", self.BUILD,
                      "build-term.sh must fetch the image addon")
        self.assertRegex(self.BUILD, r'cat "\$TMP/image\.js"',
                         "build-term.sh must inline image.js into the bundle")
        # the addon's own code must actually be present in the built page
        self.assertIn("xterm-image-layer", self.HTML,
                      "image addon code not inlined into term.html (rebuild it)")

    def test_image_addon_loaded_with_both_protocols(self):
        for label, src in (("js", self.JS), ("html", self.HTML)):
            self.assertIn("new ImageAddon.ImageAddon(", src,
                          "%s: ImageAddon must be loaded" % label)
            self.assertIn("sixelSupport", src, "%s: Sixel must be enabled" % label)
            self.assertIn("iipSupport", src, "%s: iTerm2 IIP must be enabled" % label)


class FavoriteTabsTest(unittest.TestCase):
    """Favorite/bookmarked workdir tabs. A favorited workdir keeps its tab (and
    its "＋ New opens here" behavior) even when it has zero open tiles. Pure
    client-side feature — favorites in localStorage, the empty tab synthesized
    in the dashboard JS. Static checks against the embedded JS in serve.py;
    runtime behavior is verified in the browser."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "serve.py")) as f:
            cls.serve = f.read()

    def test_favorites_persisted_in_localstorage(self):
        # Stored per browser under a dedicated key, loaded at init and saved on
        # every toggle — same pattern as tab order / active tab.
        self.assertRegex(self.serve,
            r"localStorage\.getItem\('claude-sessions-favorites'",
            "favorites must load from localStorage('claude-sessions-favorites')")
        self.assertRegex(self.serve,
            r"localStorage\.setItem\('claude-sessions-favorites'",
            "saveFavorites must persist to localStorage('claude-sessions-favorites')")

    def test_layout_gate_shows_tabs_when_favorited(self):
        # The "tabs only with 3+ sessions" gate must also fire when any workdir
        # is favorited — otherwise an empty favorite (0 tiles) would have no tab.
        body = TileIconAndReloadTest._fn_body(self.serve, "layoutTabs")
        self.assertIsNotNone(body, "layoutTabs() not found")
        self.assertRegex(body, r"sessions\.length\s*<\s*3\s*&&\s*favorites\.length\s*===\s*0",
            "layoutTabs gate must keep tabs when favorites exist (not just 3+ sessions)")

    def test_favorites_are_grouping_anchors_and_seed_empty_tabs(self):
        body = TileIconAndReloadTest._fn_body(self.serve, "layoutTabs")
        # favorites join the tabKeyFor anchor set so sub-dir tiles group under them
        self.assertRegex(body, r"\.concat\(favorites\)",
            "layoutTabs must add favorites to the grouping anchors (cwds)")
        # and each favorite seeds curTabCount with 0 so it renders even with no tiles
        self.assertRegex(body, r"curTabCount\.set\(\s*f\s*,\s*0\s*\)",
            "layoutTabs must seed each favorite with a 0 count so empty favorites get a tab")

    def test_favorite_is_a_grouping_floor_not_just_an_anchor(self):
        # Regression: pinning a sub-dir of an active project (pin repo/sub while a
        # session runs in repo/) stranded the pinned tab permanently empty — the
        # repo/sub session, and every ＋ New spawned into the pinned tab, got
        # absorbed by the shallower repo/ tab, so the favorite could never be
        # populated ("can't start a new claude from an empty pinned tab"). A
        # favorite must act as a tab-ROOT FLOOR: tabKeyFor must never climb above
        # the deepest favorite that is an ancestor-or-equal of the cwd.
        body = TileIconAndReloadTest._fn_body(self.serve, "tabKeyFor")
        self.assertIsNotNone(body, "tabKeyFor() not found")
        # Computes the deepest favorite ancestor-or-equal of cwd as the floor.
        self.assertRegex(body, r"for\s*\(const\s+f\s+of\s+favorites\)",
            "tabKeyFor must consider favorites when grouping")
        self.assertRegex(body, r"isAncestorOrEqual\(\s*f\s*,\s*cwd\s*\)",
            "tabKeyFor must treat an ancestor-or-equal favorite as cwd's tab root")
        # And never climbs to an ancestor shallower than that floor.
        self.assertRegex(body, r"floor\s*&&\s*p\.length\s*<\s*floor\.length",
            "tabKeyFor must skip any ancestor shallower than the favorite floor "
            "(else a favorited sub-dir gets absorbed by its parent's tab)")

    def test_tab_bar_renders_toggleable_favorite_star(self):
        body = TileIconAndReloadTest._fn_body(self.serve, "buildTabBar")
        self.assertIsNotNone(body, "buildTabBar() not found")
        self.assertIn("isFavorite(k)", body, "tab must reflect favorite state")
        self.assertRegex(body, r"\.className\s*=\s*['\"]fav",
            "tab must render a .fav star affordance")
        self.assertRegex(body, r"toggleFavorite\(k\)",
            "clicking the star must toggle the favorite")
        # The star click must not also select the tab.
        self.assertRegex(body, r"e\.stopPropagation\(\);\s*toggleFavorite\(k\)",
            "star onclick must stopPropagation before toggleFavorite")

    def test_toggle_favorite_relayouts_without_a_poll(self):
        body = TileIconAndReloadTest._fn_body(self.serve, "toggleFavorite")
        self.assertIsNotNone(body, "toggleFavorite() not found")
        self.assertIn("saveFavorites()", body)
        # Re-group against the cached sessions (a poll isn't pending on a click).
        self.assertIn("layoutTabs(lastLayoutSessions)", body,
            "toggleFavorite must re-run layoutTabs with the cached sessions")

    def test_new_menu_opens_in_active_tab_workdir(self):
        # The "+ New" menu must spawn into the active tab's workdir (the favorite
        # tab's cwd), so opening a tile in an empty favorite lands in the project.
        body = TileIconAndReloadTest._fn_body(self.serve, "spawnTile")
        self.assertIsNotNone(body, "spawnTile() not found")
        self.assertRegex(body, r"if\s*\(activeTab\)\s*url\s*\+=\s*'&cwd='\s*\+\s*encodeURIComponent\(activeTab\)",
            "spawnTile must pass activeTab as the new tile's cwd")

    def test_empty_favorite_tab_shows_hint(self):
        # An active favorite tab with no tiles shows a hint instead of a blank grid.
        self.assertIn("#tab-empty", self.serve, "empty-tab hint element/CSS missing")
        self.assertRegex(self.serve, r"function\s+_setTabEmpty\b",
            "_setTabEmpty must exist to toggle the empty-tab hint")
        vis = TileIconAndReloadTest._fn_body(self.serve, "applyVisibility")
        self.assertRegex(vis, r"_setTabEmpty\(\s*!!activeTab\s*&&\s*visible\s*===\s*0",
            "applyVisibility must show the hint when the active tab has no tiles")


class TileCloseTest(unittest.TestCase):
    """Closing a tile must actually remove it — including a HIDDEN tile.

    Reproduced bug ("terminals not closing"): closing a tile that is currently
    display:none — a STASHED session killed from the header drawer's ✕, or a
    tile living in a non-active tab — left the tile pinned in the DOM and the
    `tiles` Map even though the server had already reaped the session.

    Mechanism: animateClose() folds the tile with a CSS transition and removes
    it on 'transitionend' (with a setTimeout fallback). A display:none element
    runs NO transition, so transitionend never fires; removal then hinges on the
    setTimeout alone, which a busy main thread (many live terminal iframes) can
    defer for seconds. And render()'s reconciliation skips any tile flagged
    dataset.closing ("its animation owns removal"), so while that fallback is
    delayed the dead tile just sits there. Drove it live via Playwright: a
    hidden tile sat in the DOM with dataset.closing='1' for >1.6 s after close.

    Fix: animateClose tears a non-rendered tile down immediately (nothing to
    animate off-screen), and render() reclaims a reaped tile that's flagged
    closing but not actually on screen."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "serve.py")) as f:
            cls.serve = f.read()

    def test_animate_close_tears_down_hidden_tile_immediately(self):
        body = TileIconAndReloadTest._fn_body(self.serve, "animateClose")
        self.assertIsNotNone(body, "animateClose() not found")
        # A non-rendered tile (display:none → offsetParent null) must take an
        # immediate-teardown path rather than waiting on a CSS transition.
        self.assertRegex(body, r"offsetParent\s*===\s*null",
            "animateClose must detect a non-rendered (hidden) tile via offsetParent")
        # The teardown closure must be DEFINED before that guard so the guard can
        # call it synchronously (else the early path references it before init).
        guard = re.search(r"offsetParent\s*===\s*null", body)
        finish_def = re.search(r"const\s+finish\s*=\s*\(\)\s*=>", body)
        self.assertIsNotNone(finish_def, "animateClose must define a finish() teardown closure")
        self.assertLess(finish_def.start(), guard.start(),
            "finish() must be defined before the hidden-tile guard that calls it")
        # The guard must short-circuit (call finish then return) ahead of the
        # transition wiring, so a hidden tile never depends on transitionend.
        early = body[guard.start():]
        m = re.search(r"finish\(\)\s*;\s*return\s*;", early)
        self.assertIsNotNone(m,
            "hidden-tile guard must call finish() and return before animating")
        self.assertLess(body.index("finish()", guard.start()),
                        body.index("addEventListener('transitionend'"),
            "the immediate teardown must run before the transitionend wiring")

    def test_render_reclaims_reaped_hidden_closing_tile(self):
        body = TileIconAndReloadTest._fn_body(self.serve, "render")
        self.assertIsNotNone(body, "render() not found")
        # The removal loop must let a VISIBLE tile keep folding (its animation
        # owns removal) but still reclaim a reaped tile that's flagged closing
        # yet not on screen — otherwise a dropped fallback timer pins it forever.
        self.assertRegex(body,
            r"el\.dataset\.closing\s*&&\s*el\.offsetParent\s*!==\s*null",
            "render() must only defer to the closing animation for on-screen "
            "tiles; a reaped hidden closing tile must be removed, not skipped")


class BrightModeTest(unittest.TestCase):
    """Light/dark theme toggle for the dashboard chrome. Static checks against
    the embedded dashboard JS/CSS in serve.py."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "serve.py")) as f:
            cls.serve = f.read()
        with open(os.path.join(HERE, "term-client.js")) as f:
            cls.js = f.read()

    def test_theme_toggle_button_present(self):
        self.assertIn('id="themeBtn"', self.serve, "header must have a theme toggle button")

    def test_light_palette_overrides_core_vars(self):
        # html.light must redefine the chrome's CSS variables (at least bg/fg).
        m = re.search(r"html\.light\s*\{(.*?)\}", self.serve, re.S)
        self.assertIsNotNone(m, "html.light palette block not found")
        block = m.group(1)
        for v in ("--bg:", "--fg:", "--panel:", "--accent:"):
            self.assertIn(v, block, "html.light must override %s" % v)

    def test_theme_persisted_and_applied(self):
        self.assertIn("claude-sessions-theme", self.serve, "theme must persist to localStorage")
        body = TileIconAndReloadTest._fn_body(self.serve, "applyTheme")
        self.assertIsNotNone(body, "applyTheme() not found")
        self.assertRegex(body, r"classList\.toggle\(\s*'light'",
            "applyTheme must toggle the 'light' class on the root element")


    def test_pre_paint_script_avoids_flash(self):
        # The saved theme must be applied BEFORE the dashboard's </head> (before
        # first paint) so a light-mode reload doesn't flash dark first. Scope to
        # the dashboard template (HTML = r\"\"\") — there are other sub-page
        # templates (CHANNEL_HTML/NOTE_HTML) each with their own </head>.
        m = re.search(r'\nHTML = r"""', self.serve)
        self.assertIsNotNone(m, "dashboard HTML template not found")
        assert m is not None
        head = self.serve[m.end():].split("</head>", 1)[0]
        self.assertIn("claude-sessions-theme", head,
            "a pre-paint <head> script must apply the saved theme to avoid a flash")
        self.assertRegex(head, r"classList\.add\('light'\)",
            "pre-paint script must add the 'light' class when the saved theme is light")

    def test_theme_is_broadcast_to_terminals(self):
        # Terminals are cross-origin iframes, so the dashboard must PUSH the mode
        # (they can't read our class). broadcastTheme posts cmd:'theme' to every
        # tile; the toggle calls it; and the ready handshake pushes it too.
        self.assertRegex(self.serve, r"function\s+broadcastTheme",
            "dashboard must define broadcastTheme()")
        self.assertRegex(self.serve, r"cmd:\s*'theme'",
            "dashboard must postMessage cmd:'theme' to tiles")
        self.assertIn("broadcastTheme(theme)", self.serve,
            "the theme toggle must broadcast to terminals")
        # ready handshake pushes the current theme to freshly-mounted tiles
        m = re.search(r"if\s*\(\s*d\.ready\s*\)", self.serve)
        assert m is not None
        win = self.serve[m.end():m.end() + 2200]
        self.assertRegex(win, r"cmd:\s*'theme'",
            "ready handler must push the current theme to a new tile")

    def test_term_client_applies_pushed_theme(self):
        # term-client.js handles cmd:'theme', swaps the xterm palette + page bg,
        # persists it for cold boot, and boots from the stored value (no flash).
        self.assertRegex(self.js, r"d\.cmd === 'theme'",
            "term-client must handle the cmd:'theme' message")
        body = TileIconAndReloadTest._fn_body(self.js, "applyTermTheme")
        self.assertIsNotNone(body, "applyTermTheme() not found")
        self.assertIn("term.options.theme", body,
            "applyTermTheme must swap xterm's theme")
        self.assertIn("claude-term-theme", self.js,
            "term-client must persist the theme to its own localStorage")
        # the Terminal is constructed with the BOOT theme (not a hard-coded dark)
        self.assertRegex(self.js, r"theme:\s*_xtermTheme\(_bootTheme\)",
            "Terminal must boot with the stored theme to avoid a dark flash")
        # light palette defines a foreground (full palette, not just a bg)
        light = TileIconAndReloadTest._fn_body(self.js, "_xtermTheme")
        self.assertIn("foreground", light, "light theme must set a foreground")


class BellSoundTest(unittest.TestCase):
    """The dashboard plays an audible chime when a tile rings (markBell), in
    addition to the visual tile/tab flash. Static checks against the embedded
    dashboard JS/CSS in serve.py."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "serve.py")) as f:
            cls.serve = f.read()

    def test_sound_toggle_button_present(self):
        self.assertIn('id="soundBtn"', self.serve,
            "header must have a bell-sound toggle button")

    def test_dashboard_routes_bell_message_to_markbell(self):
        # The dashboard end of the propagation chain: the iframe message handler
        # must turn an incoming {bell:true} from a tile into markBell(d.sid). If
        # this branch is dropped, a tile can ring but the dashboard never shows
        # it — the "bell detection broken" symptom.
        self.assertRegex(self.serve, r"if\s*\(\s*d\.bell\s*\)\s*markBell\(\s*d\.sid\s*\)",
            "the iframe message handler must route {bell:true} → markBell(d.sid)")

    def test_markbell_plays_sound(self):
        # The regression that prompted this: a ringing tile only flashed, which
        # is easy to miss when the dashboard isn't in front of you. markBell must
        # also fire the chime so EVERY bell is perceptible regardless of which
        # tab/tile is in view.
        body = TileIconAndReloadTest._fn_body(self.serve, "markBell")
        self.assertIsNotNone(body, "markBell() not found")
        self.assertIn("playBell()", body,
            "markBell must call playBell() so a ringing tile is audible, not just "
            "a visual flash")

    def test_playbell_synthesizes_via_webaudio(self):
        # No audio asset is shipped (would 404 / need a build step) — the chime is
        # synthesised with WebAudio. Guard that playBell stays self-contained.
        body = TileIconAndReloadTest._fn_body(self.serve, "playBell")
        self.assertIsNotNone(body, "playBell() not found")
        self.assertRegex(body, r"AudioContext",
            "playBell must synthesise via WebAudio (AudioContext)")
        self.assertRegex(body, r"createOscillator\(",
            "playBell must drive an oscillator to make the tone")
        # Must respect the mute toggle.
        self.assertRegex(body, r"if\s*\(\s*!soundOn\s*\)\s*return",
            "playBell must no-op when sound is muted")

    def test_sound_pref_persisted(self):
        self.assertIn("claude-sessions-bell-sound", self.serve,
            "bell-sound on/off preference must persist to localStorage")

    def test_dock_badge_set_on_ring_cleared_on_view(self):
        # When run as an installed Chrome app, a ring must paint a count on the
        # macOS Dock icon via the Badging API, and acknowledging it (viewing the
        # tile or its tab) must clear/recount. markBell sets it; selectTile and
        # the tab-switch handler recount. Guard all three so the badge can't get
        # stuck on (count never clears) or never appear.
        badge = TileIconAndReloadTest._fn_body(self.serve, "updateDockBadge")
        self.assertIsNotNone(badge, "updateDockBadge() not found")
        self.assertIn("setAppBadge", badge,
            "updateDockBadge must use the Badging API (navigator.setAppBadge)")
        self.assertIn("clearAppBadge", badge,
            "updateDockBadge must clear the badge when nothing is ringing")
        # Feature-detect so a plain browser tab doesn't throw.
        self.assertRegex(badge, r"'setAppBadge'\s+in\s+navigator",
            "updateDockBadge must feature-detect setAppBadge (no-op in a plain tab)")
        # markBell sets it.
        mb = TileIconAndReloadTest._fn_body(self.serve, "markBell")
        self.assertIn("updateDockBadge()", mb,
            "markBell must update the Dock badge on a ring")
        # selectTile (view a tile) recounts.
        st = TileIconAndReloadTest._fn_body(self.serve, "selectTile")
        self.assertIn("updateDockBadge()", st,
            "selectTile must recount the Dock badge when a bell is acknowledged")
        # The tab-switch handler (which bulk-clears a tab's tiles' bells) recounts.
        # Co-located with the bell-clearing loop keyed on dataset.tab === k.
        self.assertRegex(self.serve,
            r"el\.classList\.remove\('bell'\);[\s\S]{0,120}updateDockBadge\(\)",
            "switching tabs clears its tiles' bells, so it must also recount the badge")


class StashBellTest(unittest.TestCase):
    """A stashed claude session keeps a live (hidden) tile so it still receives
    its BEL, and auto-unstashes when it rings. Static checks against the
    dashboard JS in serve.py. (The full ring->unstash round-trip is verified
    manually with Playwright against a real dashboard; these guard the wiring.)"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "serve.py")) as f:
            cls.serve = f.read()

    def test_markbell_unstashes_a_ringing_stashed_tile(self):
        # The headline behavior: a stashed (hidden-but-alive) tile that rings
        # must pop back. markBell must detect dataset.stashed and unstash before
        # flashing it.
        body = TileIconAndReloadTest._fn_body(self.serve, "markBell")
        self.assertIsNotNone(body, "markBell() not found")
        self.assertRegex(body,
            r"dataset\.stashed\s*===\s*'1'\s*\)\s*doStash\(\s*id\s*,\s*false\s*\)",
            "markBell must un-stash (doStash(id,false)) a ringing stashed tile")

    def test_stash_keeps_the_tile_alive_not_torn_down(self):
        # Stashing must NOT remove/animateClose the tile (that would drop the
        # iframe and the BEL with it) — it hides via dataset.stashed and leaves
        # the iframe connected.
        body = TileIconAndReloadTest._fn_body(self.serve, "doStash")
        self.assertIsNotNone(body, "doStash() not found")
        self.assertNotIn("animateClose", body,
            "doStash must NOT animateClose/remove the tile — a stashed session "
            "has to stay alive to keep receiving its BEL")
        self.assertRegex(body, r"dataset\.stashed\s*=\s*'1'",
            "doStash must hide via dataset.stashed (keep the tile alive)")

    def test_render_keeps_stashed_tiles_alive(self):
        # Removal must be keyed on the FULL session set so a stashed tile isn't
        # torn down, and the create loop must iterate sessionsAll so a session
        # that's stashed at load still gets its (hidden) live tile.
        body = TileIconAndReloadTest._fn_body(self.serve, "render")
        self.assertIsNotNone(body, "render() not found")
        self.assertRegex(body, r"const ids = new Set\(sessionsAll\.map",
            "render removal must key on sessionsAll so stashed tiles stay alive")
        self.assertRegex(body, r"for \(const s of sessionsAll\)",
            "render create loop must iterate sessionsAll so a stashed-at-load "
            "session still gets a live hidden tile that can ring")
        self.assertRegex(body, r"dataset\.stashed = s\.stashed \? '1' : ''",
            "render must mark tiles with the server's stash flag")

    def test_stashed_tiles_hidden_and_not_counted(self):
        # applyVisibility must force-hide stashed tiles and skip them from the
        # visible count (so they don't flip row mode or the empty-tab hint).
        vis = TileIconAndReloadTest._fn_body(self.serve, "applyVisibility")
        self.assertIsNotNone(vis, "applyVisibility() not found")
        self.assertRegex(vis,
            r"dataset\.stashed\s*===\s*'1'\s*\)\s*\{\s*el\.style\.display\s*=\s*'none';\s*continue",
            "applyVisibility must hide stashed tiles and not count them")
        # "N active" must exclude stashed (liveTileCount), not raw tiles.size.
        chrome = TileIconAndReloadTest._fn_body(self.serve, "refreshGridChrome")
        self.assertIsNotNone(chrome, "refreshGridChrome() not found")
        self.assertIn("liveTileCount()", chrome,
            "the active-tile count must exclude stashed tiles (liveTileCount)")


class WebLinksTest(unittest.TestCase):
    """Clickable URLs in terminal output via xterm-addon-web-links. Static
    checks that the addon is fetched, inlined, and loaded with a click handler
    that opens http(s) URLs safely. Hover-underline + click-to-open is verified
    manually in the browser."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "term-client.js")) as f:
            cls.JS = f.read()
        with open(os.path.join(HERE, "term.html")) as f:
            cls.HTML = f.read()
        with open(os.path.join(HERE, "build-term.sh")) as f:
            cls.BUILD = f.read()

    def test_web_links_addon_fetched_and_inlined(self):
        self.assertIn("xterm-addon-web-links@0.9.0", self.BUILD,
                      "build-term.sh must fetch the web-links addon")
        self.assertRegex(self.BUILD, r'cat "\$TMP/weblinks\.js"',
                         "build-term.sh must inline weblinks.js into the bundle")
        # The addon's own code must actually be present in the built page (i.e.
        # the build was re-run after wiring it). The addon defines this global.
        self.assertIn("WebLinksAddon", self.HTML,
                      "web-links addon not inlined into term.html (rebuild it)")

    def test_web_links_loaded_with_safe_handler(self):
        for label, src in (("js", self.JS), ("html", self.HTML)):
            self.assertIn("new WebLinksAddon.WebLinksAddon(", src,
                          "%s: WebLinksAddon must be loaded" % label)
            # The activate handler must hard-gate to http/https and open with
            # noopener so the spawned tab can't reach window.opener of this
            # localhost page.
            self.assertRegex(src, r"https\?:\\?/\\?/",
                             "%s: click handler must gate to http(s) URLs" % label)
            self.assertRegex(src, r"window\.open\([^)]*noopener",
                             "%s: must window.open(..., noopener) the URL" % label)


class ImagePersistenceTest(unittest.TestCase):
    """Persist inline IIP images across reloads: capture from the byte stream
    (Marker-anchored), transcode to lossy WebP in IndexedDB, splice the
    IIP(WebP) back into the restored scrollback blob at its hard-line index. The
    full capture→persist→restore cycle is verified in the browser; these are
    static wiring checks against term-client.js + the built term.html."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "term-client.js")) as f:
            cls.JS = f.read()
        with open(os.path.join(HERE, "term.html")) as f:
            cls.HTML = f.read()

    def test_capture_is_stream_observer_not_osc_handler(self):
        # The addon registers a streaming OSC handler that a co-registered
        # function handler would starve, so capture MUST observe the byte stream
        # and pass it through — never register an OSC 1337 handler of our own.
        self.assertIn("function _scanForIIP", self.JS)
        self.assertIn("_scanForIIP(", self.JS)   # wired into _writeAndScan
        self.assertNotRegex(self.JS, r"registerOscHandler\(\s*1337",
                            "must NOT co-register an OSC 1337 handler (starves the addon)")
        # anchor tracking + eviction via a buffer Marker
        self.assertIn("registerMarker", self.JS)
        self.assertIn("onDispose", self.JS)

    def test_webp_indexeddb_storage(self):
        for label, src in (("js", self.JS), ("html", self.HTML)):
            self.assertIn("claude-term-images", src, "%s: IndexedDB name missing" % label)
            self.assertIn("image/webp", src, "%s: WebP transcode missing" % label)
            self.assertRegex(src, r"toDataURL\(\s*['\"]image/webp['\"]\s*,",
                             "%s: lossy WebP encode (quality arg) missing" % label)

    def test_restore_and_persist_are_wired(self):
        # restore splices images back in; both save paths refresh the anchors.
        applied = TileIconAndReloadTest._fn_body(self.JS, "_applyRestored")
        self.assertIsNotNone(applied)
        self.assertIn("_spliceImagesInto", applied,
                      "_applyRestored must splice persisted images into the restored blob")
        # definition + at least the persist() and pagehide call sites
        self.assertGreaterEqual(self.JS.count("_persistImageMeta()"), 3,
                                "_persistImageMeta must be called from both save paths")
        persist = TileIconAndReloadTest._fn_body(self.JS, "persist")
        self.assertIsNotNone(persist)
        self.assertIn("_persistImageMeta()", persist,
                      "persist() must refresh the image anchors")


class SearchExportTest(unittest.TestCase):
    """Header "Search" button: POST /api/chat-export runs claude-chat-export.py,
    then the client opens /chat-history/index.html (served read-only from the
    export output dir). We DON'T POST with a valid CSRF here — that would run the
    real, slow export against ~/.claude/projects — so the route behaviour is
    covered by the auth/traversal checks plus source-level wiring assertions."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "serve.py")) as f:
            cls.serve = f.read()

    # ---- wiring (source) ----
    def test_search_button_and_handler_present(self):
        self.assertIn('id="searchBtn"', self.serve, "header must have a Search button")
        # Handler must POST the export, then open the served index.
        self.assertRegex(self.serve, r"fetch\('/api/chat-export'",
            "Search button must POST /api/chat-export")
        self.assertIn("/chat-history/index.html", self.serve,
            "Search button must open the served search index")
        # Regression: the popup is pre-opened as about:blank, which has no
        # origin/authority, so a root-relative '/chat-history/…' won't resolve
        # and the window stays BLANK. The navigation target must be ABSOLUTE.
        self.assertRegex(self.serve,
            r"location\.origin\s*\+\s*BASE\s*\+\s*'/chat-history/index\.html'",
            "Search must navigate the popup to an ABSOLUTE url (location.origin + "
            "BASE + path) — a root-relative url can't resolve against about:blank "
            "and leaves the window blank; BASE carries any reverse-proxy subpath")

    def test_export_runner_uses_fixed_script_not_user_input(self):
        # The export runs a script path fixed at STARTUP — never derived from a
        # request — so there's no command injection. An optional CHAT_EXPORT_SCRIPT
        # env override (operator config, read once at import) keeps it deployable;
        # the default is a fixed, normalized repo-relative path.
        self.assertRegex(self.serve,
            r"CHAT_EXPORT_SCRIPT\s*=\s*os\.environ\.get\(\s*[\"']CHAT_EXPORT_SCRIPT[\"']\s*\)"
            r"\s*or\s*os\.path\.normpath",
            "export script path must be a startup constant (env override OR a fixed "
            "normalized default) — never request-derived")
        self.assertRegex(self.serve,
            r"subprocess\.run\(\[sys\.executable,\s*CHAT_EXPORT_SCRIPT\]",
            "export must run the fixed script with sys.executable (no shell, no user input)")
        # The referenced script ships with the repo.
        self.assertTrue(os.path.isfile(serve.CHAT_EXPORT_SCRIPT),
            "claude-chat-export.py not found at %s" % serve.CHAT_EXPORT_SCRIPT)

    def test_history_route_confines_to_output_dir(self):
        # The static server must reject anything that resolves outside the
        # export output dir (realpath confinement against base + os.sep).
        self.assertRegex(self.serve, r"def _serve_chat_history",
            "missing _serve_chat_history handler")
        self.assertRegex(self.serve, r"os\.path\.realpath",
            "history route must realpath-resolve before serving")
        self.assertRegex(self.serve,
            r"real\.startswith\(base \+ os\.sep\)",
            "history route must confine served paths to the output dir")

    # ---- behaviour (live server) ----
    def test_export_requires_csrf(self):
        # No token → 403, and it must NOT have run the export.
        status, body = _post("/api/chat-export", host=HOST_HDR, csrf=None)
        self.assertEqual(status, 403, "POST /api/chat-export must require CSRF")
        status, body = _post("/api/chat-export", host=HOST_HDR, csrf="wrong-token")
        self.assertEqual(status, 403, "a bad CSRF token must be rejected")

    def test_history_rejects_bad_host(self):
        status, body = _get("/chat-history/index.html", host="evil.example.com")
        self.assertEqual(status, 403, "history route must enforce the Host allowlist")

    def test_history_path_traversal_blocked(self):
        # Encoded dot-dot that escapes the output dir must be forbidden, never
        # leaking a file from elsewhere on disk.
        status, body = _get("/chat-history/%2e%2e/%2e%2e/serve.py", host=HOST_HDR)
        self.assertIn(status, (403, 404),
            "path traversal out of the history dir must be blocked")
        self.assertNotIn(b"def do_GET", body, "traversal must not leak serve.py")

    def test_history_missing_file_is_404(self):
        status, body = _get("/chat-history/_no_such_file_zzz.html", host=HOST_HDR)
        self.assertEqual(status, 404)


class TtydReverseProxyTest(unittest.TestCase):
    """End-to-end coverage for the /t/<port>/ reverse proxy that lets a remote
    browser (behind the nginx https vhost) reach terminal tiles. A stub stands
    in for ttyd: it echoes the request path on a plain GET and does a WebSocket
    upgrade + raw echo on /ws, so we can assert the dashboard (a) strips the
    /t/<port> prefix, (b) refuses unregistered ports, and (c) tunnels the
    upgrade + bytes both ways."""

    def _start_stub(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0)); srv.listen(8)
        port = srv.getsockname()[1]

        def serve_one(conn):
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close(); return
                data += chunk
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            request_line = head.splitlines()[0]
            path = request_line.split(" ")[1]
            if "upgrade: websocket" in head.lower():
                # Static 101 — the test client isn't a real browser, so we don't
                # bother computing Sec-WebSocket-Accept; we only need the upgrade
                # to relay through and the post-upgrade stream to be a raw pipe.
                conn.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: stub\r\n\r\n")
                while True:
                    try:
                        b = conn.recv(4096)
                    except OSError:
                        break
                    if not b:
                        break
                    conn.sendall(b)   # echo
                conn.close(); return
            body = ("PATH=" + path).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n%s"
                         % (len(body), body))
            conn.close()

        def loop():
            while not stop["v"]:
                try:
                    srv.settimeout(0.5)
                    conn, _ = srv.accept()
                except (OSError, socket.timeout):
                    continue
                threading.Thread(target=serve_one, args=(conn,), daemon=True).start()

        stop = {"v": False}
        t = threading.Thread(target=loop, daemon=True); t.start()

        def shutdown():
            stop["v"] = True
            try: srv.close()
            except OSError: pass
        return port, shutdown

    def _register(self, port, sid="proxytest"):
        path = os.path.join(_tmpdir, sid + ".json")
        with open(path, "w") as f:
            json.dump({"name": "stub", "port": port, "kind": "terminal",
                       "cwd": "/tmp", "started": "2026-06-06T00:00:00Z"}, f)
        return path

    def test_get_strips_prefix_and_relays_body(self):
        port, shutdown = self._start_stub()
        reg = self._register(port)
        try:
            status, body = _get("/t/%d/term-page?sid=x" % port, host=HOST_HDR)
            self.assertEqual(status, 200)
            # The dashboard must forward the path with /t/<port> stripped — the
            # stub sees /term-page (+query), not /t/<port>/term-page.
            self.assertIn(b"PATH=/term-page?sid=x", body)
        finally:
            shutdown()
            try: os.remove(reg)
            except OSError: pass

    def test_unregistered_port_is_refused(self):
        # A port with no live session must 404 — the proxy is not a generic
        # localhost dialer (SSRF guard).
        port, shutdown = self._start_stub()   # listening, but NOT registered
        try:
            status, _ = _get("/t/%d/" % port, host=HOST_HDR)
            self.assertEqual(status, 404)
        finally:
            shutdown()

    def test_websocket_upgrade_and_bidirectional_pump(self):
        port, shutdown = self._start_stub()
        reg = self._register(port, sid="proxyws")
        cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cli.settimeout(5)
        try:
            cli.connect(("127.0.0.1", PORT))
            cli.sendall(
                ("GET /t/%d/ws HTTP/1.1\r\nHost: %s\r\n"
                 "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                 "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                 "Sec-WebSocket-Version: 13\r\n\r\n" % (port, HOST_HDR)
                 ).encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = cli.recv(4096)
                if not chunk:
                    break
                resp += chunk
            self.assertIn(b"101", resp.split(b"\r\n", 1)[0],
                          "upgrade must relay ttyd's 101 back to the client")
            # Post-upgrade the tunnel is a raw byte pipe — send a payload and
            # expect the stub's echo to come back through the dashboard.
            cli.sendall(b"PING-THROUGH-PROXY")
            got = b""
            while b"PING-THROUGH-PROXY" not in got:
                chunk = cli.recv(4096)
                if not chunk:
                    break
                got += chunk
            self.assertIn(b"PING-THROUGH-PROXY", got)
        finally:
            cli.close()
            shutdown()
            try: os.remove(reg)
            except OSError: pass


class BasePathSubpathTest(unittest.TestCase):
    """The dashboard can be reverse-proxied under a subpath (DASHBOARD_BASE=/dash)
    so it coexists with other vhosts on one host. nginx forwards the prefix
    intact; serve.py strips it at request entry and injects it into the served
    pages as BASE. This starts a dedicated server with the env var set and checks
    both the prefixed and stripped paths route, and that BASE is injected."""

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.tmp = tempfile.mkdtemp(prefix="serve-base-test-")
        env = dict(os.environ)
        env["CLAUDE_SESSIONS_DIR"] = cls.tmp
        env["DASHBOARD_BASE"] = "/dash"
        cls.proc = subprocess.Popen(
            [sys.executable, SERVE, str(cls.port), "--no-open"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        _wait_for_port(cls.port, cls.proc)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            cls.proc.kill(); cls.proc.wait(timeout=3)
        try:
            for fn in os.listdir(cls.tmp):
                os.remove(os.path.join(cls.tmp, fn))
            os.rmdir(cls.tmp)
        except OSError:
            pass

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            # nginx forwards Host as 127.0.0.1:<port> in the deploy; mirror that so
            # the _host_ok() gate passes.
            conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % self.port})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def test_prefixed_dashboard_serves_and_injects_base(self):
        status, body = self._get("/dash/")
        self.assertEqual(status, 200)
        # BASE is injected as the JS constant the client prepends to its URLs.
        self.assertIn(b'const BASE = "/dash"', body)

    def test_prefixed_api_routes(self):
        status, body = self._get("/dash/api/sessions")
        self.assertEqual(status, 200)
        self.assertIn(b"sessions", body)

    def test_prefixed_ttyd_proxy_guard(self):
        # The /t/<port>/ proxy is reachable under the prefix and still enforces
        # the live-session guard (unregistered port → 404).
        status, _ = self._get("/dash/t/9999/")
        self.assertEqual(status, 404)

    def test_unprefixed_root_still_served(self):
        # _strip_base only removes a leading /dash; a bare request still routes
        # (harmless — nginx only ever exposes the prefixed paths publicly).
        status, _ = self._get("/api/sessions")
        self.assertEqual(status, 200)


class SettingsMenuAndRestartTest(unittest.TestCase):
    """The gear ▸ Settings menu (font/size/line-height pickers, theme + bell-sound
    toggles, Restart server) and the /api/restart re-exec endpoint."""

    def test_settings_menu_markup_and_relocated_controls(self):
        # The page must wire the gear button + dropdown and hold the relocated
        # controls (same ids as before, so their handlers still bind) plus the
        # restart button — all INSIDE the settings menu, not loose in the bar.
        status, body = _get("/", host=HOST_HDR)
        self.assertEqual(status, 200)
        text = body.decode()
        for needle in ('id="setBtn"', 'id="settingsMenu"', 'id="restartBtn"',
                       'id="fontSel"', 'id="sizeSel"', 'id="lineHeightSel"',
                       'id="themeBtn"', 'id="soundBtn"'):
            self.assertIn(needle, text, "missing settings markup: %s" % needle)
        # The relocated controls live within the settings menu container: the
        # menu div opens before each control id and the </header> closes after.
        menu_at = text.index('id="settingsMenu"')
        header_end = text.index("</header>")
        for cid in ('id="fontSel"', 'id="themeBtn"', 'id="soundBtn"', 'id="restartBtn"'):
            at = text.index(cid)
            self.assertGreater(at, menu_at, "%s must be inside the settings menu" % cid)
            self.assertLess(at, header_end, "%s must be inside the header" % cid)
        # JS wiring: toggle the menu, and the restart action POSTs /api/restart.
        self.assertIn("settingsMenu.classList.toggle('open')", text)
        self.assertIn("'/api/restart'", text)
        # The restart handler reloads the page once the server answers again.
        self.assertIn("location.reload()", text)

    def test_restart_requires_csrf(self):
        # /api/restart is state-changing → rejected without the CSRF header, and
        # crucially this path returns 403 BEFORE re-exec, so the shared test
        # server is untouched.
        status, _ = _post("/api/restart", host=HOST_HDR)
        self.assertEqual(status, 403)
        status, _ = _post("/api/restart", host=HOST_HDR, csrf="wrong")
        self.assertEqual(status, 403)

    def test_restart_reexecs_server(self):
        # End-to-end: a dedicated serve.py subprocess, hit /api/restart with a
        # valid CSRF token, and confirm it re-execs (same PID, port answers
        # again) rather than dying. Isolated on its own port so it can't disturb
        # the shared module server.
        port = _free_port()
        if port == 7680:
            port = _free_port()
        host = "127.0.0.1:%d" % port
        tmp = tempfile.mkdtemp(prefix="serve-restart-")
        env = dict(os.environ)
        env["CLAUDE_SESSIONS_DIR"] = tmp
        proc = subprocess.Popen(
            [sys.executable, SERVE, str(port), "--no-open"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            _wait_for_port(port, proc)
            pid_before = proc.pid

            def _conn():
                return http.client.HTTPConnection("127.0.0.1", port, timeout=5)

            # Pull the per-process CSRF token out of the served page.
            c = _conn()
            c.request("GET", "/", headers={"Host": host})
            page = c.getresponse().read().decode()
            c.close()
            m = re.search(r'<meta name="csrf-token" content="([^"]+)"', page)
            self.assertIsNotNone(m, "csrf-token meta tag not found")
            token = m.group(1)

            # POST /api/restart → 200 {ok:true}; server acks before re-exec.
            c = _conn()
            c.request("POST", "/api/restart",
                      headers={"Host": host, "X-CSRF-Token": token})
            resp = c.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read())["ok"], True)
            c.close()

            # The socket drops during re-exec; poll until it answers again.
            deadline = time.time() + 10
            back = False
            while time.time() < deadline:
                self.assertIsNone(proc.poll(),
                    "serve.py exited instead of re-exec'ing")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                        back = True
                        break
                except OSError:
                    time.sleep(0.1)
            self.assertTrue(back, "server did not come back up after /api/restart")
            # execv preserves the PID — proof it reloaded in place, not died.
            self.assertEqual(proc.pid, pid_before)
            # And it actually serves again.
            c = _conn()
            c.request("GET", "/api/sessions", headers={"Host": host})
            self.assertEqual(c.getresponse().status, 200)
            c.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            try:
                for fn in os.listdir(tmp):
                    os.remove(os.path.join(tmp, fn))
                os.rmdir(tmp)
            except OSError:
                pass


class DashboardUIE2ETest(unittest.TestCase):
    """End-to-end browser test of the dashboard header: the + New ▸ Claude
    (Vertex AI) item, and the gear ▸ Settings menu (font picker, theme toggle,
    bell-sound toggle, Restart server). Drives a REAL serve.py via headless
    Chromium (Playwright) so the menu open/close, localStorage persistence, and
    the /api/restart re-exec round-trip are exercised in an actual DOM — the
    parts the static-source and HTTP tests can't observe.

    Setup (one-time):
      cd session-dashboard
      python3 -m venv .venv-test
      .venv-test/bin/pip install playwright
      .venv-test/bin/playwright install chromium

    Run:
      .venv-test/bin/python3 -m unittest test_serve.DashboardUIE2ETest -v

    Skipped gracefully when run with a Python that lacks playwright."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest(
                "playwright not importable — install into .venv-test: "
                "`python3 -m venv .venv-test && .venv-test/bin/pip install "
                "playwright && .venv-test/bin/playwright install chromium`, then "
                "run `.venv-test/bin/python3 -m unittest test_serve.DashboardUIE2ETest`")
        cls._sync_playwright = staticmethod(sync_playwright)

    def setUp(self):
        self._procs = []
        self._tmpdirs = []

    def tearDown(self):
        for proc in self._procs:
            try:
                proc.terminate(); proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for d in self._tmpdirs:
            try:
                for fn in os.listdir(d):
                    os.remove(os.path.join(d, fn))
                os.rmdir(d)
            except OSError:
                pass

    def _start_dashboard(self):
        """Spawn a real serve.py on a free port with an isolated registry dir.
        Returns (port, base_url)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        if port == 7680:  # never collide with the live dashboard
            return self._start_dashboard()
        tmp = tempfile.mkdtemp(prefix="serve-e2e-reg-")
        self._tmpdirs.append(tmp)
        env = dict(os.environ)
        env["CLAUDE_SESSIONS_DIR"] = tmp
        proc = subprocess.Popen(
            [sys.executable, SERVE, str(port), "--no-open"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        self._procs.append(proc)
        for _ in range(80):
            if proc.poll() is not None:
                self.fail("serve.py exited early (code %s)" % proc.returncode)
            try:
                with socket.create_connection(("127.0.0.1", port), 0.1):
                    return port, "http://127.0.0.1:%d/" % port
            except OSError:
                time.sleep(0.1)
        self.fail("dashboard didn't bind on %d within 8 s" % port)

    def _open(self, p):
        """Launch chromium, start a dashboard, navigate, return (browser, page, port)."""
        port, url = self._start_dashboard()
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 720})
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector("#setBtn", timeout=10000)
        return browser, page, port

    def test_tile_order_survives_other_window_saves(self):
        # Regression: with TWO dashboard windows open, each holds an in-memory
        # copy of the manual tile order and writes the WHOLE list back on any
        # structural change. A reorder in window A used to be clobbered by
        # window B's next save (e.g. a new session appearing on B's 3s poll
        # appends + saves B's stale pre-reorder copy), and the next reload
        # restored the clobbered list — "tile order is not persistent across
        # dashboard reloads". Windows now adopt each other's writes via the
        # storage event (fires only in the windows that did NOT write).
        from dashboard_fixture import Fixture
        fx = Fixture()
        fx.start()
        self.addCleanup(fx.teardown)
        with self._sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1280, "height": 720})
                win_a = ctx.new_page(); win_a.goto(fx.url, wait_until="domcontentloaded")
                win_b = ctx.new_page(); win_b.goto(fx.url, wait_until="domcontentloaded")
                for pg in (win_a, win_b):
                    pg.wait_for_selector("#grid .tile", state="attached", timeout=10000)
                win_a.wait_for_timeout(1200)
                # A: swap the first two tiles of the projB tab (3 tiles, row mode).
                win_a.locator("#tabs .tab", has_text="projB").first.click()
                win_a.wait_for_timeout(300)
                win_a.locator("#grid .tile:visible").nth(0).locator(".head").click()
                win_a.wait_for_timeout(200)
                win_a.keyboard.press("Control+ArrowRight")
                win_a.wait_for_timeout(400)
                swapped = json.loads(win_a.evaluate(
                    "localStorage.getItem('claude-sessions-order')"))
                self.assertTrue(swapped, "reorder did not save an order list")
                # Put B on the same tab so the projB session it learns about is
                # visible there too (active-tab isn't cross-window synced).
                win_b.locator("#tabs .tab", has_text="projB").first.click()
                win_b.wait_for_timeout(300)
                count_js = """() => [...document.querySelectorAll('#grid .tile')]
                    .filter(el => el.style.display !== 'none').length"""
                before_a = win_a.evaluate(count_js)
                before_b = win_b.evaluate(count_js)
                # B: a new session appears → both windows pick it up on their
                # next poll. A bare "session appeared" append is NOT persisted by
                # either window (only a user reorder / authoritative dup / close
                # persists) — so the saved list stays exactly A's reorder, never
                # clobbered, and the new tile is appended in-memory (rendered) in
                # both windows.
                fx.add_session("zeta-new", os.path.join(fx.home, "projB"))
                win_b.wait_for_timeout(5000)   # > one 3 s poll in both windows
                final = json.loads(win_a.evaluate(
                    "localStorage.getItem('claude-sessions-order')"))
                self.assertEqual(
                    final, swapped,
                    "a passive new-session append must not touch the saved order "
                    "(that race is what clobbered window A's reorder)")
                # zeta is visible in BOTH windows (rendered via the in-memory
                # append) — cross-window consistency, just not persisted.
                self.assertEqual(win_a.evaluate(count_js), before_a + 1,
                                 "window A did not render the new session")
                self.assertEqual(win_b.evaluate(count_js), before_b + 1,
                                 "window B did not render the new session")
            finally:
                browser.close()

    def test_cloned_tile_lands_next_to_source_in_every_window(self):
        # Regression ("cloned tiles don't end up next to the active one, tile
        # orders scrambled"): the window that clicks ⧉ duplicate places the
        # clone right after its source (a pendingDup match) and saves. Every
        # OTHER open window also sees the new session on its poll, but with no
        # pendingDup it appended the clone at the END — and used to SAVE that.
        # The two saves raced, and the storage listener then made the placing
        # window ADOPT the end-append, so the clone jumped to the end. Now a
        # bare end-append is never persisted: only an authoritative placement /
        # reorder / close writes the order, so the placer wins and the others
        # adopt its placement.
        from dashboard_fixture import Fixture
        fx = Fixture()
        fx.start()
        self.addCleanup(fx.teardown)
        with self._sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1400, "height": 800})
                win_a = ctx.new_page(); win_a.goto(fx.url, wait_until="domcontentloaded")
                win_b = ctx.new_page(); win_b.goto(fx.url, wait_until="domcontentloaded")
                for pg in (win_a, win_b):
                    pg.wait_for_selector("#grid .tile", state="attached", timeout=10000)
                win_a.wait_for_timeout(1000)
                # Source = the projB session. Register a pendingDup in window A
                # ONLY (exactly what the ⧉ button does) so A authoritatively owns
                # the placement of the next same-cwd session.
                sessions = win_a.evaluate(
                    "async () => (await (await fetch('/api/sessions')).json()).sessions")
                src = next(s for s in sessions if s["name"] == "beta-B")
                src_id, src_cwd = src["id"], src["cwd"]
                win_a.evaluate(
                    "([sid, cwd]) => { pendingDups.push("
                    "{ srcId: sid, cwd: cwd, until: Date.now() + 30000 }); }",
                    [src_id, src_cwd])
                # The clone appears (same cwd as the source) on the next poll.
                new_id = fx.add_session("zeta-dup", src_cwd)["id"]
                win_b.wait_for_timeout(5000)   # > one 3 s poll in both windows
                # The PERSISTED order (written by the authoritative window A) puts
                # the clone immediately after its source.
                saved = json.loads(win_a.evaluate(
                    "localStorage.getItem('claude-sessions-order')"))
                self.assertEqual(saved.index(new_id), saved.index(src_id) + 1,
                                 "clone not persisted next to its source")
                # And BOTH windows' live order agree: the clone sits right after
                # the source (window B adopted A's placement, didn't clobber it).
                for pg, who in ((win_a, "A"), (win_b, "B")):
                    order = pg.evaluate("() => orderList")
                    self.assertIn(new_id, order, "window %s missing the clone" % who)
                    self.assertEqual(
                        order.index(new_id), order.index(src_id) + 1,
                        "window %s did not place the clone next to its source "
                        "(landed at index %d, source at %d)" % (
                            who, order.index(new_id), order.index(src_id)))
            finally:
                browser.close()

    def test_scroll_pin_releases_on_forwarded_terminal_gesture(self):
        # The tile row is pinned to its leftmost on reload; the pin must release
        # on the user's first interaction. A gesture inside a terminal is in a
        # cross-origin iframe and never reaches the dashboard window, so the
        # client forwards a 'user-gesture' message. Without honoring it the row
        # stays "locked to the left" (pinLeft never clears, scrollLeft snaps to
        # 0). Drive the forwarded message directly (the fixture's dummy iframes
        # don't run the real client) and assert the pin lifts + scroll sticks.
        from dashboard_fixture import Fixture
        fx = Fixture()
        fx.start()
        self.addCleanup(fx.teardown)
        with self._sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1100, "height": 760})
                page = ctx.new_page(); page.goto(fx.url, wait_until="domcontentloaded")
                page.wait_for_selector("#grid .tile", state="attached", timeout=10000)
                page.locator("#tabs .tab", has_text="projB").first.click()
                page.wait_for_timeout(400)
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("#grid .tile", state="attached", timeout=10000)
                page.wait_for_timeout(800)
                self.assertTrue(page.evaluate("() => pinLeft"),
                                "row should start pinned after reload")
                # While pinned, a scroll snaps back to 0 (the lock the user saw).
                page.evaluate("() => { document.getElementById('grid').scrollLeft = 200; }")
                page.wait_for_timeout(120)
                self.assertEqual(page.evaluate("() => document.getElementById('grid').scrollLeft"), 0,
                                 "pinned row must snap scrollLeft back to 0")
                # The client's forwarded first-interaction signal lifts the pin.
                sid = page.evaluate("() => [...tiles.keys()][0]")
                page.evaluate("""(sid) => window.dispatchEvent(new MessageEvent('message', {
                    data: {type:'claude-term', sid:sid, key:'user-gesture'},
                    origin: location.origin }))""", sid)
                self.assertFalse(page.evaluate("() => pinLeft"),
                                 "forwarded user-gesture must release the pin")
                page.evaluate("() => { document.getElementById('grid').scrollLeft = 200; }")
                page.wait_for_timeout(120)
                self.assertEqual(page.evaluate("() => document.getElementById('grid').scrollLeft"), 200,
                                 "after release, the row must scroll freely")
            finally:
                browser.close()

    def test_new_menu_has_vertex_item(self):
        # + New menu must offer "Claude (Vertex AI)" carrying provider=vertex.
        with self._sync_playwright() as p:
            browser, page, _ = self._open(p)
            try:
                page.click("#newBtn")
                vtx = page.locator('#newMenu button[data-provider="vertex"]')
                vtx.wait_for(state="visible", timeout=5000)
                self.assertIn("Vertex", vtx.inner_text())
                # The plain Claude item is still there and has no provider.
                self.assertEqual(
                    page.locator('#newMenu button[data-kind="claude"]').count(), 2)
            finally:
                browser.close()

    def test_settings_menu_opens_with_all_controls(self):
        # Gear opens the menu; the relocated controls + restart are all visible.
        with self._sync_playwright() as p:
            browser, page, _ = self._open(p)
            try:
                # Hidden until opened.
                self.assertFalse(page.locator("#settingsMenu").is_visible())
                page.click("#setBtn")
                self.assertTrue(page.locator("#settingsMenu").is_visible())
                for sel in ("#fontSel", "#sizeSel", "#lineHeightSel",
                            "#themeBtn", "#soundBtn", "#restartBtn"):
                    self.assertTrue(page.locator(sel).is_visible(),
                                    "%s not visible in settings menu" % sel)
            finally:
                browser.close()

    def test_theme_toggle_persists(self):
        with self._sync_playwright() as p:
            browser, page, _ = self._open(p)
            try:
                page.click("#setBtn")
                start_light = page.evaluate(
                    "() => document.documentElement.classList.contains('light')")
                page.click("#themeBtn")
                now_light = page.evaluate(
                    "() => document.documentElement.classList.contains('light')")
                self.assertNotEqual(start_light, now_light,
                                    "theme class did not toggle")
                stored = page.evaluate(
                    "() => localStorage.getItem('claude-sessions-theme')")
                self.assertEqual(stored, "light" if now_light else "dark")
            finally:
                browser.close()

    def test_sound_toggle_persists(self):
        with self._sync_playwright() as p:
            browser, page, _ = self._open(p)
            try:
                page.click("#setBtn")
                # Default ON (🔔, no .off class). One click mutes.
                page.click("#soundBtn")
                off = page.evaluate(
                    "() => document.getElementById('soundBtn').classList.contains('off')")
                self.assertTrue(off, "sound button did not enter muted state")
                stored = page.evaluate(
                    "() => localStorage.getItem('claude-sessions-bell-sound')")
                self.assertEqual(stored, "off")
            finally:
                browser.close()

    def test_font_change_persists(self):
        with self._sync_playwright() as p:
            browser, page, _ = self._open(p)
            try:
                page.click("#setBtn")
                vals = page.eval_on_selector_all(
                    "#fontSel option", "els => els.map(e => e.value)")
                cur = page.evaluate("() => document.getElementById('fontSel').value")
                other = next((v for v in vals if v and v != cur), None)
                self.assertIsNotNone(other, "need a second font option to switch to")
                page.select_option("#fontSel", other)
                stored = page.evaluate(
                    "() => localStorage.getItem('claude-sessions-font')")
                self.assertEqual(stored, other,
                                 "font selection not persisted to localStorage")
            finally:
                browser.close()

    def test_restart_button_reexecs_and_reloads(self):
        # Clicking Restart confirms, POSTs /api/restart, the server re-execs
        # (same PID), and the page reloads itself once the server is back.
        with self._sync_playwright() as p:
            browser, page, port = self._open(p)
            try:
                proc = self._procs[-1]
                pid_before = proc.pid
                page.on("dialog", lambda d: d.accept())  # accept the confirm()
                page.click("#setBtn")
                with page.expect_navigation(wait_until="load", timeout=25000):
                    page.click("#restartBtn")
                # Page reloaded → header is back and functional.
                page.wait_for_selector("#setBtn", timeout=10000)
                page.click("#setBtn")
                self.assertTrue(page.locator("#settingsMenu").is_visible())
                # execv keeps the PID — proof it reloaded in place, didn't die.
                self.assertIsNone(proc.poll(), "serve.py died instead of re-exec")
                self.assertEqual(proc.pid, pid_before)
            finally:
                browser.close()


@unittest.skipUnless(
    os.environ.get("RUN_VERTEX_LIVE"),
    "live Vertex inference smoke test — set RUN_VERTEX_LIVE=1 to run (needs gcloud "
    "ADC + quota; makes a real billable call)")
class VertexLiveSmokeTest(unittest.TestCase):
    """End-to-end regression guard for '+ New -> Claude (Vertex AI)' INFERENCE.

    The other Vertex test (test_spawn_claude_vertex_injects_env) only proves we
    inject the right env vars — it can't prove a completion actually comes back,
    because that depends on GCP project state (model enablement + per-model,
    per-region token quota) that is external to this repo, billable, and cannot
    run in CI. That gap is exactly why a broken-inference Vertex config could ship
    while every offline test passed.

    This test closes the gap the only way it can: by making a real minimal
    completion call against the dashboard's configured project/region/model
    (serve.VERTEX_*) and asserting HTTP 200. It is opt-in (RUN_VERTEX_LIVE=1) and
    skipped by default. A non-200 fails LOUDLY with the actual Vertex error, so a
    quota=0 (429 RESOURCE_EXHAUSTED) or not-enabled (404 NOT_FOUND) condition is
    reported as a failure rather than a silently-retrying tile. Run it after any
    change to the injected Vertex config, or to confirm a project is usable."""

    def _adc_token(self):
        try:
            out = subprocess.run(
                ["gcloud", "auth", "application-default", "print-access-token"],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            self.skipTest("gcloud unavailable for ADC token: %s" % e)
        if out.returncode != 0 or not out.stdout.strip():
            self.skipTest("no ADC token (run `gcloud auth application-default "
                          "login`): %s" % (out.stderr.strip()[:200]))
        return out.stdout.strip()

    @staticmethod
    def _vertex_host(region):
        # Mirrors Claude Code's host selection: global -> base host, multi-region
        # eu/us -> rep host, otherwise the regional host.
        if region == "global":
            return "aiplatform.googleapis.com"
        if region in ("eu", "us"):
            return "aiplatform.%s.rep.googleapis.com" % region
        return "%s-aiplatform.googleapis.com" % region

    def test_vertex_completion_returns_200(self):
        vc = serve.vertex_config()
        project = vc["project_id"]
        region = vc["region"]
        # vc["model"] may carry Claude Code's `[1m]` 1M-context flag, which is not
        # part of the Vertex model resource name — strip it for the URL.
        model = (vc["model"] or "claude-opus-4-8").split("[")[0]
        token = self._adc_token()
        url = ("https://%s/v1/projects/%s/locations/%s/publishers/anthropic/"
               "models/%s:rawPredict" % (self._vertex_host(region), project, region, model))
        body = json.dumps({
            "anthropic_version": "vertex-2023-10-16",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status, payload = resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status = e.code
            payload = e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            self.fail("could not reach Vertex (%s/%s/%s): %s" % (project, region, model, e))
        self.assertEqual(
            status, 200,
            "Vertex inference is broken for project=%s region=%s model=%s: HTTP %s\n%s\n"
            "(429 RESOURCE_EXHAUSTED = request token-per-minute quota for this model; "
            "404 NOT_FOUND = enable it in Vertex Model Garden / try another region.)"
            % (project, region, model, status, payload[:400]))
        # 200: sanity-check it's an Anthropic completion, not an empty/HTML body.
        data = json.loads(payload)
        self.assertIn(data.get("type"), ("message", None))
        self.assertTrue(data.get("content") or data.get("usage"),
                        "200 but no content/usage in Vertex response: %s" % payload[:300])


class ChatPanelHelpersTest(unittest.TestCase):
    """Unit tests for the xterm-free chat panel's server helpers."""

    def setUp(self):
        self.reg = tempfile.mkdtemp(prefix="serve-chat-reg-")
        self._saved_reg = serve.REGISTRY
        serve.REGISTRY = self.reg
        self.addCleanup(shutil.rmtree, self.reg, ignore_errors=True)
        self.addCleanup(lambda: setattr(serve, "REGISTRY", self._saved_reg))

    def _reg(self, sid, **fields):
        with open(os.path.join(self.reg, sid + ".json"), "w") as f:
            json.dump(fields, f)

    def test_is_busy_states(self):
        fresh, stale = 1, serve.STALE_BUSY_SECS + 10
        # working: user msg waiting, or assistant still in a tool loop
        self.assertTrue(serve._is_busy("user", "end_turn", fresh))
        self.assertTrue(serve._is_busy("assistant", "tool_use", fresh))
        self.assertTrue(serve._is_busy("user", "tool_use", fresh))   # tool result, claude resuming
        # idle: assistant reached an end_turn / stop
        self.assertFalse(serve._is_busy("assistant", "end_turn", fresh))
        self.assertFalse(serve._is_busy("assistant", "stop_sequence", fresh))
        self.assertFalse(serve._is_busy(None, None, fresh))
        # stale guard: a trailing user msg with no fresh writes = idle (interrupt)
        self.assertFalse(serve._is_busy("user", "end_turn", stale))
        # a stale tool RESULT (user+tool_use) claude never answered = idle too
        self.assertFalse(serve._is_busy("user", "tool_use", stale))
        # the ONE exemption: a running tool (assistant+tool_use) writes nothing
        # for minutes — stays busy even when stale
        self.assertTrue(serve._is_busy("assistant", "tool_use", stale))

    def test_context_window_by_model(self):
        self.assertEqual(serve._context_window("claude-opus-4-8"), 1000000)
        self.assertEqual(serve._context_window("claude-fable-5"), 1000000)
        self.assertEqual(serve._context_window("claude-sonnet-4-6"), serve.DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(serve._context_window(""), serve.DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(serve._context_window(None), serve.DEFAULT_CONTEXT_WINDOW)

    def test_iso_ms(self):
        self.assertEqual(serve._iso_ms("1970-01-01T00:00:01Z"), 1000)
        self.assertIsNone(serve._iso_ms(""))
        self.assertIsNone(serve._iso_ms(None))
        self.assertIsNone(serve._iso_ms("not-a-date"))

    def test_jsonl_turn_assistant_text_and_tools(self):
        o = {"type": "assistant", "uuid": "u1", "timestamp": "2026-01-01T00:00:00Z",
             "message": {"content": [
                 {"type": "text", "text": "hi"},
                 {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
             ]}}
        t = serve._jsonl_turn(o)
        self.assertEqual(t["role"], "assistant")
        self.assertEqual(t["text"], "hi")
        self.assertEqual([x["name"] for x in t["tools"]], ["Bash"])
        self.assertEqual(t["uuid"], "u1")

    def test_jsonl_turn_skips_sidechain_and_empty(self):
        self.assertIsNone(serve._jsonl_turn(
            {"type": "assistant", "isSidechain": True,
             "message": {"content": [{"type": "text", "text": "x"}]}}))
        # a user turn that's only a tool_result → no text, no tools → skipped
        self.assertIsNone(serve._jsonl_turn(
            {"type": "user",
             "message": {"content": [{"type": "tool_result", "content": "x"}]}}))
        self.assertIsNone(serve._jsonl_turn({"type": "summary", "message": {}}))

    def test_tool_brief_extracts_salient_input(self):
        self.assertEqual(serve._tool_brief({"name": "Bash", "input": {"command": "git status"}}),
                         {"name": "Bash", "detail": "git status"})
        self.assertEqual(serve._tool_brief({"name": "Read", "input": {"file_path": "/a/b.py"}})["detail"],
                         "/a/b.py")
        self.assertEqual(serve._tool_brief({"name": "Grep", "input": {"pattern": "foo", "path": "src"}})["detail"],
                         "foo in src")   # whitespace collapsed
        self.assertEqual(serve._tool_brief({"name": "Bash", "input": {"command": "a\n\nb   c"}})["detail"],
                         "a b c")
        long = serve._tool_brief({"name": "Bash", "input": {"command": "x" * 500}})["detail"]
        self.assertTrue(long.endswith("…") and len(long) <= 221)

    def test_tool_brief_edit_diffs(self):
        e = serve._tool_brief({"name": "Edit",
                               "input": {"file_path": "/a.py", "old_string": "x", "new_string": "y"}})
        self.assertEqual(e["detail"], "/a.py")
        self.assertEqual(e["diff"], [{"old": "x", "new": "y"}])
        w = serve._tool_brief({"name": "Write", "input": {"file_path": "/b", "content": "hello"}})
        self.assertEqual(w["diff"], [{"old": "", "new": "hello"}])
        me = serve._tool_brief({"name": "MultiEdit", "input": {"file_path": "/c", "edits": [
            {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"}]}})
        self.assertEqual(len(me["diff"]), 2)
        self.assertEqual(me["diff"][1], {"old": "c", "new": "d"})

    def test_tool_brief_todos(self):
        t = serve._tool_brief({"name": "TodoWrite", "input": {"todos": [
            {"content": "do x", "status": "completed"},
            {"activeForm": "Doing y", "status": "in_progress"}]}})
        self.assertEqual(t["todos"], [{"content": "do x", "status": "completed"},
                                      {"content": "Doing y", "status": "in_progress"}])
        self.assertEqual(serve._tool_brief({"name": "TodoWrite", "input": {}})["todos"], [])

    def test_tool_brief_ask_user_question(self):
        b = {"name": "AskUserQuestion", "input": {"questions": [{
            "question": "How to run it?", "header": "Run", "multiSelect": False,
            "options": [{"label": "Now", "description": "right away"},
                        {"label": "Later", "description": "hold off"}]}]}}
        q = serve._tool_brief(b)
        self.assertEqual(q["name"], "AskUserQuestion")
        self.assertEqual(len(q["questions"]), 1)
        self.assertEqual(q["questions"][0]["question"], "How to run it?")
        self.assertEqual(q["questions"][0]["header"], "Run")
        self.assertFalse(q["questions"][0]["multi"])
        self.assertEqual([o["label"] for o in q["questions"][0]["options"]], ["Now", "Later"])
        self.assertEqual(q["questions"][0]["options"][0]["description"], "right away")
        # Malformed input degrades gracefully (no questions key / wrong types).
        self.assertEqual(serve._tool_brief({"name": "AskUserQuestion", "input": {}})["questions"], [])
        self.assertEqual(serve._tool_brief(
            {"name": "AskUserQuestion", "input": {"questions": "nope"}})["questions"], [])

    def test_answer_brief_parses_selection(self):
        s = ('Your questions have been answered: "How to run it?"="Now". '
             "You can now continue with these answers in mind.")
        self.assertEqual(serve._answer_brief(s), [{"q": "How to run it?", "a": "Now"}])
        # multiple Q=A pairs
        s2 = 'Your questions have been answered: "Q1"="A1", "Q2"="A2". You can now continue.'
        self.assertEqual(serve._answer_brief(s2),
                         [{"q": "Q1", "a": "A1"}, {"q": "Q2", "a": "A2"}])
        # list-of-blocks content form
        self.assertEqual(serve._answer_brief(
            [{"type": "text", "text": 'Your questions have been answered: "x"="y".'}]),
            [{"q": "x", "a": "y"}])
        # non-answer tool_result text → nothing surfaced
        self.assertEqual(serve._answer_brief("file contents here"), [])
        self.assertEqual(serve._answer_brief(None), [])

    def test_jsonl_turn_surfaces_question(self):
        o = {"type": "assistant", "uuid": "q1", "message": {"content": [
            {"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": [
                {"question": "Pick?", "header": "P", "options": [{"label": "A", "description": "d"}]}]}}]}}
        t = serve._jsonl_turn(o)
        self.assertEqual(t["tools"][0]["questions"][0]["question"], "Pick?")

    def test_jsonl_turn_surfaces_answer_from_tool_result(self):
        # A user turn that's ONLY an AskUserQuestion answer is no longer dropped —
        # it carries the chosen answer so the chat can show it.
        o = {"type": "user", "uuid": "a1", "message": {"content": [
            {"type": "tool_result",
             "content": 'Your questions have been answered: "Pick?"="A". You can now continue.'}]}}
        t = serve._jsonl_turn(o)
        self.assertIsNotNone(t)
        self.assertEqual(t["answers"], [{"q": "Pick?", "a": "A"}])
        # a plain (non-answer) tool_result user turn is still dropped
        self.assertIsNone(serve._jsonl_turn(
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "some file output"}]}}))

    def test_read_taskfile_scoping(self):
        base = tempfile.mkdtemp(prefix="claude-tasktest-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        d = os.path.join(base, "sess", "tasks")
        os.makedirs(d)
        good = os.path.join(d, "job.output")
        with open(good, "w") as f:
            f.write("hello")
        self.assertEqual(serve._read_taskfile(good), b"hello")     # scoped + allowed
        self.assertIsNone(serve._read_taskfile("/etc/passwd"))     # outside tmp roots
        self.assertIsNone(serve._read_taskfile(os.path.join(d, "job.json")))  # wrong ext
        notasks = os.path.join(base, "x.output")
        with open(notasks, "w") as f:
            f.write("x")
        self.assertIsNone(serve._read_taskfile(notasks))           # no /tasks/ segment
        self.assertIsNone(serve._read_taskfile("relative/x.output"))  # not absolute
        self.assertIsNone(serve._read_taskfile(
            os.path.join(d, "..", "..", "..", "..", "etc", "passwd")))  # traversal escapes tmp

    def test_complete_paths_lists_and_blocks_traversal(self):
        cwd = tempfile.mkdtemp(prefix="serve-complete-")
        self.addCleanup(shutil.rmtree, cwd, ignore_errors=True)
        os.makedirs(os.path.join(cwd, "src"))
        open(os.path.join(cwd, "README.md"), "w").close()
        open(os.path.join(cwd, "package.json"), "w").close()
        open(os.path.join(cwd, ".hidden"), "w").close()
        self._reg("host-1", kind="host", cwd=cwd, session_id="s")
        res = serve._complete_paths("host-1", "")
        names = [i["name"] for i in res["items"]]
        self.assertEqual(names[0], "src")               # dirs sort first
        self.assertTrue(res["items"][0]["dir"])
        self.assertIn("README.md", names)
        self.assertNotIn(".hidden", names)              # hidden filtered by default
        self.assertEqual([i["name"] for i in serve._complete_paths("host-1", "pack")["items"]],
                         ["package.json"])               # prefix filter
        self.assertIn(".hidden", [i["name"] for i in
                                  serve._complete_paths("host-1", ".")["items"]])  # shown on '.'
        self.assertEqual(serve._complete_paths("host-1", "../../../etc/pas")["items"], [])  # traversal
        self.assertEqual(serve._complete_paths("host-1", "src/")["dir"], "src/")  # nested prefix
        self.assertEqual(serve._complete_paths("nope", ""), {"items": [], "dir": ""})

    def test_chat_send_no_socket_is_graceful(self):
        self._reg("host-1", kind="host", cwd="/tmp")   # no 'sock' field
        ok, err = serve.chat_send("host-1", "hi")
        self.assertFalse(ok)
        self.assertIsNotNone(err)
        ok, err = serve.chat_send("missing", "hi")
        self.assertFalse(ok)
        self.assertEqual(err, "unknown session")


class ResurrectTest(unittest.TestCase):
    """Unit tests for resurrect_sessions / _revive_entry — the reboot-recovery
    path that respawns dead tiles from the surviving registry. All process
    launches are mocked (no real ttyd/claude/opencode is started); _which is
    mocked so the tests don't depend on what's installed on this machine."""

    def setUp(self):
        self.reg = tempfile.mkdtemp(prefix="serve-resurrect-reg-")
        self.addCleanup(shutil.rmtree, self.reg, ignore_errors=True)
        self._saved_reg = serve.REGISTRY
        serve.REGISTRY = self.reg
        self.addCleanup(setattr, serve, "REGISTRY", self._saved_reg)
        self.cwd = tempfile.mkdtemp(prefix="serve-resurrect-cwd-")
        self.addCleanup(shutil.rmtree, self.cwd, ignore_errors=True)
        # Fake HOME so _projects_dir (host tiles resolve the conversation
        # .jsonl under ~/.claude/projects/<cwd-slug>/) stays inside the test.
        self.home = tempfile.mkdtemp(prefix="serve-resurrect-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env_patch = mock.patch.dict(os.environ, {"HOME": self.home})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        # Capture every spawn instead of launching anything.
        self.spawns = []

        def fake_popen(cmd, **kw):
            self.spawns.append((list(cmd), kw))
            return mock.Mock()

        self._tools = {"ttyd": "/fake/ttyd", "dtach": "/fake/dtach",
                       "claude": "/fake/claude", "opencode": "/fake/opencode"}
        for target, side in (("serve.subprocess.Popen", fake_popen),
                             ("serve._which", self._tools.get)):
            p = mock.patch(target, side_effect=side)
            p.start()
            self.addCleanup(p.stop)
        # Drop the port lock synchronously — the real version waits up to 6 s
        # in a background thread for a ttyd that will never listen here.
        p = mock.patch("serve._release_when_listening",
                       side_effect=lambda port, lock: os.remove(lock))
        p.start()
        self.addCleanup(p.stop)
        self._listeners = []
        self.addCleanup(lambda: [s.close() for s in self._listeners])

    def _listen(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        self._listeners.append(s)
        return s.getsockname()[1]

    @staticmethod
    def _dead_port():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _write(self, sid, **fields):
        path = os.path.join(self.reg, sid + ".json")
        with open(path, "w") as f:
            json.dump(fields, f)
        return path

    def _entry(self, sid):
        with open(os.path.join(self.reg, sid + ".json")) as f:
            return json.load(f)

    def _write_host_jsonl(self, session_id):
        proj = os.path.join(self.home, ".claude", "projects",
                            self.cwd.rstrip("/").replace("/", "-"))
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, session_id + ".jsonl")
        with open(path, "w") as f:
            f.write('{"sessionId":"%s"}\n' % session_id)
        return path

    def test_dead_host_tile_resumes_its_session(self):
        # The core OOM-reboot case: registry entry survives, processes don't.
        # The revived claude must --resume the recorded session id, on the
        # same port, under the same sid/started (so scrollback still matches).
        uuid = "11111111-1111-1111-1111-111111111111"
        self._write_host_jsonl(uuid)
        port = self._dead_port()
        self._write("host-1", kind="host", port=port, cwd=self.cwd,
                    name="x", started="2026-01-01T00:00:00Z", session_id=uuid,
                    sock="/old/dtach-claude-%d.sock" % port)
        self.assertEqual(serve.resurrect_sessions(), 1)
        self.assertEqual(len(self.spawns), 1)
        cmd, kw = self.spawns[0]
        self.assertEqual(cmd[-3:], ["--dangerously-skip-permissions", "--resume", uuid])
        self.assertIn("/fake/dtach", cmd)
        self.assertIn(str(port), cmd)          # old port reclaimed (it's free)
        self.assertEqual(kw.get("cwd"), self.cwd)
        s = self._entry("host-1")
        self.assertEqual(s["port"], port)
        self.assertEqual(s["started"], "2026-01-01T00:00:00Z")   # identity kept
        self.assertEqual(s["session_id"], uuid)
        self.assertEqual(s["sock"],
                         os.path.join(self.reg, "dtach-claude-%d.sock" % port))

    def test_host_tile_without_transcript_restarts_under_same_id(self):
        # A session that never wrote its .jsonl can't be resumed — claude would
        # error out. Restart it fresh under the same pinned --session-id.
        uuid = "22222222-2222-2222-2222-222222222222"
        self._write("host-1", kind="host", port=self._dead_port(), cwd=self.cwd,
                    started="x", session_id=uuid)
        self.assertEqual(serve.resurrect_sessions(), 1)
        cmd, _ = self.spawns[0]
        self.assertEqual(cmd[-2:], ["--session-id", uuid])
        self.assertNotIn("--resume", cmd)

    def test_legacy_host_without_session_id_resumes_newest(self):
        # Entries from before session_id tracking: fall back to the cwd's
        # newest transcript (same fallback fork_session uses) and record it.
        uuid = "33333333-3333-3333-3333-333333333333"
        self._write_host_jsonl(uuid)
        self._write("host-1", kind="host", port=self._dead_port(), cwd=self.cwd,
                    started="x")
        self.assertEqual(serve.resurrect_sessions(), 1)
        cmd, _ = self.spawns[0]
        self.assertEqual(cmd[-2:], ["--resume", uuid])
        self.assertEqual(self._entry("host-1")["session_id"], uuid)

    def test_vertex_host_tile_keeps_vertex_routing(self):
        self._write("host-1", kind="host", port=self._dead_port(), cwd=self.cwd,
                    started="x", session_id="44444444-4444-4444-4444-444444444444",
                    provider="vertex")
        self.assertEqual(serve.resurrect_sessions(), 1)
        _, kw = self.spawns[0]
        self.assertEqual(kw.get("env", {}).get("CLAUDE_CODE_USE_VERTEX"), "1")
        self.assertEqual(self._entry("host-1")["provider"], "vertex")

    def test_dead_opencode_tile_continues_last_session(self):
        port = self._dead_port()
        self._write("opencode-1", kind="opencode", port=port, cwd=self.cwd,
                    started="x")
        self.assertEqual(serve.resurrect_sessions(), 1)
        cmd, _ = self.spawns[0]
        self.assertEqual(cmd[-3:], ["/fake/opencode", self.cwd, "--continue"])
        self.assertIn("/fake/dtach", cmd)      # reload-survival wrapper kept

    def test_dead_terminal_tile_respawns_fresh_shell(self):
        self._write("terminal-1", kind="terminal", port=self._dead_port(),
                    cwd=self.cwd, started="x")
        self.assertEqual(serve.resurrect_sessions(), 1)
        cmd, _ = self.spawns[0]
        self.assertEqual(cmd[-1], "-i")        # plain interactive shell…
        self.assertNotIn("/fake/dtach", cmd)   # …bare, matching spawn_terminal

    def test_container_claude_relaunches_via_launcher(self):
        # claude-box owns container bring-up; the launcher self-registers a
        # NEW tile, so the stale entry must be removed (not left to linger —
        # a stashed one would otherwise sit there dead forever).
        uuid = "55555555-5555-5555-5555-555555555555"
        launcher = os.path.join(self.cwd, "claude-box")
        with open(launcher, "w") as f:
            f.write("#!/bin/sh\n")
        path = self._write("container-1", kind="container", port=self._dead_port(),
                           cwd=self.cwd, started="x", session_id=uuid,
                           launcher=launcher)
        self.assertEqual(serve.resurrect_sessions(), 1)
        cmd, _ = self.spawns[0]
        self.assertEqual(cmd, [launcher, "-web", "--detach", "--resume", uuid])
        self.assertFalse(os.path.exists(path))

    def test_container_shell_is_left_to_prune(self):
        # In-container shells aren't revived (their container is gone after a
        # reboot); the entry keeps its old mtime and prunes as before.
        path = self._write("terminal-1", kind="terminal", port=self._dead_port(),
                           cwd=self.cwd, started="x", container="abc123",
                           csock="/tmp/dtach-cshell-1.sock")
        self.assertEqual(serve.resurrect_sessions(), 0)
        self.assertEqual(self.spawns, [])
        self.assertTrue(os.path.exists(path))

    def test_alive_tile_is_left_alone(self):
        # Dashboard-only restart (launchd KeepAlive): ttyd survived, so the
        # entry must not be touched — respawning would double the session.
        self._write("host-1", kind="host", port=self._listen(), cwd=self.cwd,
                    started="x", session_id="66666666-6666-6666-6666-666666666666")
        self.assertEqual(serve.resurrect_sessions(), 0)
        self.assertEqual(self.spawns, [])

    def test_portless_kinds_are_skipped(self):
        for sid, fields in (("webview-1", {"kind": "webview", "url": "http://x"}),
                            ("channel-1", {"kind": "channel", "channel": "c"}),
                            ("note-1", {"kind": "note"})):
            self._write(sid, started="x", **fields)
        self.assertEqual(serve.resurrect_sessions(), 0)
        self.assertEqual(self.spawns, [])
        self.assertEqual(len(os.listdir(self.reg)), 3)   # all kept untouched

    def test_missing_cwd_is_not_revived(self):
        self._write("host-1", kind="host", port=self._dead_port(),
                    cwd=os.path.join(self.cwd, "gone"), started="x",
                    session_id="77777777-7777-7777-7777-777777777777")
        self.assertEqual(serve.resurrect_sessions(), 0)
        self.assertEqual(self.spawns, [])

    def test_stashed_tile_is_revived_stashed(self):
        # Stashed = hidden but running; after a reboot it should come back
        # running (and still hidden) rather than sit there dead forever.
        self._write("host-1", kind="host", port=self._dead_port(), cwd=self.cwd,
                    started="x", stashed=True,
                    session_id="88888888-8888-8888-8888-888888888888")
        self.assertEqual(serve.resurrect_sessions(), 1)
        self.assertTrue(self._entry("host-1")["stashed"])


class HookEventLogicTest(unittest.TestCase):
    """In-process tests of the hook-event state machine (serve.hook_event):
    forwarded Claude Code hook events drive the chat panel's busy/idle and
    pending-permission state."""

    def setUp(self):
        serve.HOOK_STATE.clear()

    def test_user_prompt_marks_busy(self):
        self.assertTrue(serve.hook_event(
            {"session_id": "s1", "hook_event_name": "UserPromptSubmit"}))
        self.assertEqual(serve.HOOK_STATE["s1"]["phase"], "busy")

    def test_stop_marks_idle_and_clears_perm(self):
        serve.hook_event({"session_id": "s1", "hook_event_name": "Notification",
                          "message": "Claude needs your permission to use Bash"})
        self.assertIsNotNone(serve.HOOK_STATE["s1"]["perm"])
        serve.hook_event({"session_id": "s1", "hook_event_name": "Stop"})
        self.assertEqual(serve.HOOK_STATE["s1"]["phase"], "idle")
        self.assertIsNone(serve.HOOK_STATE["s1"]["perm"])

    def test_permission_notification_records_tool(self):
        serve.hook_event({"session_id": "s1", "hook_event_name": "Notification",
                          "message": "Claude needs your permission to use Bash"})
        self.assertEqual(serve.HOOK_STATE["s1"]["perm"]["tool"], "Bash")

    def test_pretooluse_clears_pending_perm(self):
        # PreToolUse firing means the prompt was approved — the card must drop.
        serve.hook_event({"session_id": "s1", "hook_event_name": "Notification",
                          "message": "Claude needs your permission to use Bash"})
        serve.hook_event({"session_id": "s1", "hook_event_name": "PreToolUse",
                          "tool_name": "Bash"})
        self.assertIsNone(serve.HOOK_STATE["s1"]["perm"])
        self.assertEqual(serve.HOOK_STATE["s1"]["phase"], "busy")

    def test_waiting_for_input_notification_marks_idle(self):
        serve.hook_event({"session_id": "s1", "hook_event_name": "Notification",
                          "message": "Claude is waiting for your input"})
        self.assertEqual(serve.HOOK_STATE["s1"]["phase"], "idle")

    def test_other_notifications_ignored(self):
        self.assertFalse(serve.hook_event(
            {"session_id": "s1", "hook_event_name": "Notification",
             "message": "some other notification"}))

    def test_rejects_missing_session_or_unknown_event(self):
        self.assertFalse(serve.hook_event({"hook_event_name": "Stop"}))
        self.assertFalse(serve.hook_event(
            {"session_id": "s1", "hook_event_name": "Bogus"}))
        self.assertFalse(serve.hook_event("not a dict"))
        self.assertFalse(serve.hook_event(None))

    def test_snapshot_is_a_detached_copy(self):
        serve.hook_event({"session_id": "s1", "hook_event_name": "Stop"})
        snap = serve._hook_snapshot("s1")
        snap["phase"] = "mutated"
        self.assertEqual(serve.HOOK_STATE["s1"]["phase"], "idle")
        self.assertIsNone(serve._hook_snapshot("missing"))
        self.assertIsNone(serve._hook_snapshot(None))

    def test_user_prompt_captures_text_for_echo(self):
        # The first-turn echo reads the prompt text the UserPromptSubmit hook
        # carries (before claude writes it to the .jsonl).
        self.assertTrue(serve.hook_event({
            "session_id": "s1", "hook_event_name": "UserPromptSubmit",
            "prompt": "hello there"}))
        self.assertEqual(serve.HOOK_STATE["s1"]["prompt"]["text"], "hello there")
        self.assertIn("prompt", serve._hook_snapshot("s1"))

    def test_blank_prompt_is_not_captured(self):
        serve.hook_event({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
                          "prompt": "   "})
        self.assertIsNone(serve.HOOK_STATE["s1"].get("prompt"))

    def test_prompt_text_is_capped(self):
        serve.hook_event({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
                          "prompt": "x" * 20000})
        self.assertEqual(len(serve.HOOK_STATE["s1"]["prompt"]["text"]), 8000)

    def test_transcript_path_captured_from_any_event(self):
        serve.hook_event({"session_id": "s1", "hook_event_name": "Stop",
                          "transcript_path": "/home/u/.claude/projects/p/s1.jsonl"})
        self.assertEqual(serve.HOOK_STATE["s1"]["transcript"],
                         "/home/u/.claude/projects/p/s1.jsonl")


class LiveSessionIdTrackingTest(unittest.TestCase):
    """Hook ppid → tile resolution that keeps a tile's registry session_id
    pointing at its LIVE conversation. /clear (and relaunching claude inside a
    tile) mints a new session id underneath the tile; the stale recorded id
    made fork copy a sibling's transcript ("forking the wrong tiles"), the
    chat panel tail the wrong file, and resurrection resume the pre-/clear
    conversation. dashboard-notify.sh forwards its $PPID; the server walks
    the process ancestry to the dtach master whose -A <sock> argv names a
    registered tile, then rewrites that tile's session_id."""

    def setUp(self):
        self.reg = tempfile.mkdtemp(prefix="serve-hookpid-reg-")
        self.addCleanup(shutil.rmtree, self.reg, ignore_errors=True)
        self._saved = serve.REGISTRY
        serve.REGISTRY = self.reg
        self.addCleanup(setattr, serve, "REGISTRY", self._saved)
        serve.HOOK_STATE.clear()
        serve._PID_TILE_CACHE.clear()

    def _write(self, sid, **fields):
        with open(os.path.join(self.reg, sid + ".json"), "w") as f:
            json.dump(fields, f)

    def _read(self, sid):
        with open(os.path.join(self.reg, sid + ".json")) as f:
            return json.load(f)

    def test_tile_for_pid_walks_to_dtach_master(self):
        # hook script (300) <- claude (200) <- dtach master (100): the walk
        # must find the master and match its socket to the registry entry.
        sock = os.path.join(self.reg, "dtach-claude-7681.sock")
        self._write("host-7681", kind="host", port=7681, sock=sock)
        table = {300: (200, "/bin/sh -c dashboard-notify.sh"),
                 200: (100, "claude --dangerously-skip-permissions"),
                 100: (1, "dtach -A %s -r winch claude" % sock)}
        with mock.patch("serve._ps_table", return_value=table):
            self.assertEqual(serve._tile_for_pid(300), "host-7681")
        # Cached under the QUERIED pid: the next event from the same claude
        # must resolve without another ps snapshot.
        with mock.patch("serve._ps_table", side_effect=AssertionError):
            self.assertEqual(serve._tile_for_pid(300), "host-7681")

    def test_tile_for_pid_unresolvable(self):
        sock = os.path.join(self.reg, "dtach-claude-7681.sock")
        self._write("host-7681", kind="host", port=7681, sock=sock)
        with mock.patch("serve._ps_table", return_value={300: (1, "sh")}):
            self.assertIsNone(serve._tile_for_pid(300))
        self.assertIsNone(serve._tile_for_pid("bogus"))
        self.assertIsNone(serve._tile_for_pid(None))
        self.assertIsNone(serve._tile_for_pid(-4))

    def test_hook_event_updates_drifted_session_id(self):
        self._write("host-7681", kind="host", port=7681,
                    sock="/x/dtach.sock", session_id="old-id")
        with mock.patch("serve._tile_for_pid", return_value="host-7681"):
            self.assertTrue(serve.hook_event(
                {"session_id": "new-id", "hook_event_name": "UserPromptSubmit"},
                ppid="42"))
        self.assertEqual(self._read("host-7681")["session_id"], "new-id")

    def test_hook_event_without_ppid_touches_nothing(self):
        # Older installs of dashboard-notify.sh don't send ?ppid — the event
        # must still drive HOOK_STATE without any registry writes.
        self._write("host-7681", kind="host", port=7681, session_id="old-id")
        with mock.patch("serve._tile_for_pid", side_effect=AssertionError):
            self.assertTrue(serve.hook_event(
                {"session_id": "new-id", "hook_event_name": "UserPromptSubmit"}))
        self.assertEqual(self._read("host-7681")["session_id"], "old-id")

    def test_record_live_session_id_idempotent(self):
        self._write("host-7681", kind="host", session_id="same")
        self.assertFalse(serve._record_live_session_id("host-7681", "same"))
        self.assertTrue(serve._record_live_session_id("host-7681", "new"))
        self.assertEqual(self._read("host-7681")["session_id"], "new")
        self.assertFalse(serve._record_live_session_id("missing-tile", "x"))


class HookTranscriptPathTest(unittest.TestCase):
    """_hook_transcript_path validates a hook-reported transcript path before the
    chat tailer streams it, and _tile_jsonl falls back to it when reconstruction
    misses."""

    def setUp(self):
        serve.HOOK_STATE.clear()
        self.tmp = tempfile.mkdtemp(prefix="serve-test-transcript-")
        self.reg = tempfile.mkdtemp(prefix="serve-test-transcript-reg-")
        self._saved_reg = serve.REGISTRY
        serve.REGISTRY = self.reg

    def tearDown(self):
        serve.REGISTRY = self._saved_reg
        serve.HOOK_STATE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.reg, ignore_errors=True)

    def _make_jsonl(self, session_id, slug="-some-proj"):
        proj = os.path.join(self.tmp, ".claude", "projects", slug)
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, session_id + ".jsonl")
        with open(path, "w") as f:
            f.write("{}\n")
        return path

    def _set_transcript(self, session_id, path):
        serve.HOOK_STATE[session_id] = {"phase": None, "ts": 0, "perm": None,
                                        "transcript": path}

    def test_accepts_valid_path(self):
        sid = "aaaaaaaa-1111-2222-3333-444444444444"
        p = self._make_jsonl(sid)
        self._set_transcript(sid, p)
        self.assertEqual(serve._hook_transcript_path(sid), os.path.realpath(p))

    def test_rejects_basename_not_matching_session(self):
        sid = "aaaaaaaa-1111-2222-3333-444444444444"
        p = self._make_jsonl("some-other-id")     # wrong filename
        self._set_transcript(sid, p)
        self.assertIsNone(serve._hook_transcript_path(sid))

    def test_rejects_path_outside_claude_projects(self):
        sid = "aaaaaaaa-1111-2222-3333-444444444444"
        stray = os.path.join(self.tmp, sid + ".jsonl")   # not under .claude/projects
        with open(stray, "w") as f:
            f.write("{}\n")
        self._set_transcript(sid, stray)
        self.assertIsNone(serve._hook_transcript_path(sid))

    def test_rejects_nonexistent_and_relative_and_missing(self):
        sid = "aaaaaaaa-1111-2222-3333-444444444444"
        self._set_transcript(sid, os.path.join(
            self.tmp, ".claude", "projects", "p", sid + ".jsonl"))  # never created
        self.assertIsNone(serve._hook_transcript_path(sid))
        self._set_transcript(sid, "relative/.claude/projects/p/%s.jsonl" % sid)
        self.assertIsNone(serve._hook_transcript_path(sid))
        self.assertIsNone(serve._hook_transcript_path("no-such-session"))
        self.assertIsNone(serve._hook_transcript_path(None))

    def test_tile_jsonl_falls_back_to_hook_path(self):
        # Registry says cwd=/nope/cwd (its reconstructed projects dir doesn't
        # exist), but the hook reported the real transcript → tailer uses it.
        sid = "bbbbbbbb-1111-2222-3333-444444444444"
        real = self._make_jsonl(sid)
        with open(os.path.join(self.reg, "host-1.json"), "w") as f:
            json.dump({"kind": "host", "cwd": "/nope/cwd", "session_id": sid}, f)
        self.assertIsNone(serve._tile_jsonl("host-1"))   # before the hook fires
        self._set_transcript(sid, real)
        self.assertEqual(serve._tile_jsonl("host-1"), os.path.realpath(real))


class ChatKeyLogicTest(unittest.TestCase):
    """chat_key injects only allowlisted keystrokes into known tiles."""

    def setUp(self):
        self.reg = tempfile.mkdtemp(prefix="serve-test-chatkey-")
        self._saved_reg = serve.REGISTRY
        serve.REGISTRY = self.reg

    def tearDown(self):
        serve.REGISTRY = self._saved_reg
        shutil.rmtree(self.reg, ignore_errors=True)

    def test_unknown_key_rejected(self):
        ok, err = serve.chat_key("any", "rm -rf /")
        self.assertFalse(ok)
        self.assertEqual(err, "unsupported key")
        ok, _ = serve.chat_key("any", "")
        self.assertFalse(ok)

    def test_unknown_session_rejected(self):
        ok, err = serve.chat_key("nope", "1")
        self.assertFalse(ok)
        self.assertEqual(err, "unknown session")

    def test_tile_without_sock_rejected(self):
        with open(os.path.join(self.reg, "host-9.json"), "w") as f:
            json.dump({"kind": "host", "cwd": "/tmp"}, f)
        ok, err = serve.chat_key("host-9", "enter")
        self.assertFalse(ok)
        self.assertIn("no input socket", err)


def _post_raw(path, headers=None, body=b""):
    """POST with arbitrary headers/body against the live test server."""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    try:
        h = {"Host": HOST_HDR, "Content-Length": str(len(body))}
        h.update(headers or {})
        conn.request("POST", path, body=body, headers=h)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class HookEndpointTest(unittest.TestCase):
    """POST /api/hook-event on the live test server: authenticated by the
    registry-dir token file (written at startup), not the page CSRF token."""

    def _token(self):
        with open(os.path.join(_tmpdir, ".hook-token")) as f:
            return f.read().strip()

    def test_hook_files_written_at_startup(self):
        self.assertTrue(self._token())
        with open(os.path.join(_tmpdir, ".hook-port")) as f:
            self.assertEqual(f.read().strip(), str(PORT))

    def test_missing_or_wrong_token_forbidden(self):
        status, _ = _post_raw("/api/hook-event", body=b"{}")
        self.assertEqual(status, 403)
        status, _ = _post_raw("/api/hook-event",
                              headers={"X-Hook-Token": "wrong"}, body=b"{}")
        self.assertEqual(status, 403)

    def test_valid_token_accepts_event(self):
        body = json.dumps({"session_id": "live-test",
                           "hook_event_name": "Stop"}).encode()
        status, resp = _post_raw("/api/hook-event",
                                 headers={"X-Hook-Token": self._token()}, body=body)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(resp)["ok"])

    def test_valid_token_bad_payload_is_ok_false(self):
        status, resp = _post_raw("/api/hook-event",
                                 headers={"X-Hook-Token": self._token()},
                                 body=b"not json")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(resp)["ok"])


class ChatKeyEndpointTest(unittest.TestCase):
    """POST /api/chat-key is CSRF-guarded like the other browser POSTs."""

    def _csrf(self):
        _, body = _get("/", host=HOST_HDR)
        return re.search(r'<meta name="csrf-token" content="([^"]+)"',
                         body.decode()).group(1)

    def test_requires_csrf(self):
        status, _ = _post_raw("/api/chat-key?id=x&key=1")
        self.assertEqual(status, 403)

    def test_unknown_session_rejected(self):
        status, resp = _post_raw("/api/chat-key?id=nope&key=1",
                                 headers={"X-CSRF-Token": self._csrf()})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(resp)["ok"])


if __name__ == "__main__":
    unittest.main()
