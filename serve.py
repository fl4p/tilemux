#!/usr/bin/env python3
"""Claude Sessions dashboard.

Shows every active agent/terminal session side-by-side as embedded ttyd web TTYs:
  - host  : a claude/codex/opencode (or custom) session run directly on the host
  - container : a session running inside a container, exposed via a host-side ttyd

Sessions self-register by dropping a small JSON file in ~/.claude-sessions/:
  {"name": "...", "port": 7681, "kind": "host"|"container", "cwd": "...",
   "started": "2026-05-29T..."}

This server reads that registry, prunes dead entries (whose ttyd port is no
longer listening), serves /api/sessions, and serves a dashboard page that embeds
each live session in an iframe arranged in a responsive grid. Binds localhost
only. Run: python3 serve.py [port]   (dashboard port defaults to 7680).
"""
import base64
import datetime
import getpass
import http.server
import json
import mimetypes
import os
import re
import socket
import secrets
import shlex
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


def _consume_flag(name):
    """Pull `--name VALUE` or `--name=VALUE` out of sys.argv; return VALUE or None.

    Consumed before REGISTRY/PORT/CHANNELS_DIR are computed below so the flags can
    override them. Removing the flag from sys.argv keeps the positional
    `serve.py [port]` form and the `--no-open` check working unchanged.
    """
    for i, a in enumerate(sys.argv[1:], 1):
        if a == name and i + 1 < len(sys.argv):
            val = sys.argv[i + 1]
            del sys.argv[i:i + 2]
            return val
        if a.startswith(name + "="):
            del sys.argv[i]
            return a.split("=", 1)[1]
    return None


# --sessions-dir / --channels-dir relocate the two stores; --port sets the bind
# port. --sessions-dir is EXPORTED into the environment (not merely used for
# REGISTRY) so sessions this dashboard spawns — +New terminals, fork, duplicate,
# and the claude launchers it execs — inherit CLAUDE_SESSIONS_DIR and
# self-register into the SAME store. That lets one command stand up a fully
# isolated dashboard "universe": separate server + separate session store, in
# sync. Flags are optional; the positional `serve.py [port]` form still works.
_flag_sessions = _consume_flag("--sessions-dir")
if _flag_sessions:
    os.environ["CLAUDE_SESSIONS_DIR"] = os.path.expanduser(_flag_sessions)
_flag_channels = _consume_flag("--channels-dir")
if _flag_channels:
    os.environ["CLAUDE_CHANNELS_DIR"] = os.path.expanduser(_flag_channels)
_flag_port = _consume_flag("--port")
if _flag_port:
    sys.argv.insert(1, _flag_port)  # let the positional PORT logic below pick it up

REGISTRY = os.environ.get("CLAUDE_SESSIONS_DIR") or os.path.expanduser("~/.claude-sessions")
# Default display name in the chatroom's "as" field. The chatroom JS used to
# require the user to type a name before the Send button enabled — easy to miss
# (the input is small and lives in the header), and it read as "Send is broken".
# Pre-fill with the OS username so first-time use just works; user can still
# overwrite it. Falls back to "anon" if even getpass can't resolve a name.
try:
    DEFAULT_WHO = getpass.getuser() or "anon"
except Exception:
    DEFAULT_WHO = "anon"
# Channels live as one append-only NDJSON file per channel under this dir; the
# `channel` skill writes here, and the dashboard exposes them as chatroom tiles
# (see /api/channels + /channel/<name>). Override for tests via env.
CHANNELS_DIR = os.environ.get("CLAUDE_CHANNELS_DIR") or "/tmp/claude-channels"
# Channel names appear in URLs and on the filesystem — restrict to a small
# safe charset to defuse path traversal / shell injection across both surfaces.
# `_` and `-` are allowed because the existing channels (infl, parquet-review)
# already use them; `.` is NOT allowed because of `..` traversal.
CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Channels with no append in the last CHANNEL_LIST_MAX_AGE_SEC don't show up in
# the Channels dropdown — the menu would otherwise accumulate every short-lived
# chat ever opened and become useless. The NDJSON file itself is left alone;
# /channel/<name> still serves the chatroom HTML for stale channels (you just
# have to know the name to reach them), and a new append resurfaces the entry.
CHANNEL_LIST_MAX_AGE_SEC = 3 * 3600
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7680
# Public URL prefix when reverse-proxied under a subpath (e.g. DASHBOARD_BASE=/dash
# behind nginx at https://host/dash/). nginx forwards the prefix through; we strip
# it at request entry (_strip_base) so all internal routing stays prefix-free, and
# inject it into the served pages (__BASE__) so the client prefixes the URLs it
# builds. Empty (the default) means served at the origin root — no-op everywhere.
BASE_PATH = (os.environ.get("DASHBOARD_BASE") or "").rstrip("/")
if BASE_PATH and not BASE_PATH.startswith("/"):
    BASE_PATH = "/" + BASE_PATH
# Don't prune a just-registered session whose ttyd is still booting.
PRUNE_GRACE = 30  # seconds
# Hysteresis for the port-alive probe. A session CONFIRMED alive at least once
# keeps being listed for this long after its last successful probe, even while
# the probe currently fails — so a transient miss (a load spike timing out the
# 0.2 s connect) can't drop a live session from /api/sessions and make the
# frontend reap its tile. Sessions never yet seen alive (still booting) are
# unaffected: they wait for a real listen, as before. Genuinely dead sessions
# fall out once the failure persists past this window.
ALIVE_HYSTERESIS = 20  # seconds
# sid -> wall-clock time of the last successful port_alive(). Mutated from
# multiple request threads; plain-dict get/set/pop are atomic under the GIL and
# the values are only timestamps, so a benign race is acceptable (no lock).
_last_alive = {}
# Per-file ceiling for drag/paste uploads (POST /api/dropfile). Files above
# this size are rejected with 413 — keeps a runaway drop from filling the
# host disk via a single request. 100 MB covers typical screenshots, logs,
# CSVs, small data dumps; anything bigger probably wants `cp` or a mount.
MAX_DROP_BYTES = 100 * 1024 * 1024

# Google Vertex AI provider for dashboard-spawned `claude` tiles. When a tile is
# spawned with provider=vertex (the "Claude (Vertex AI)" item in the + New menu),
# vertex_config() values are injected into the claude child's environment so it
# talks to Claude on Vertex instead of the default Anthropic API. gcloud
# Application Default Credentials (~/.config/gcloud/application_default_credentials
# .json) are inherited by the child automatically — no service-account key file
# needed; run `gcloud auth application-default login` once on this host.
#
# Settings live in a JSON config file (VERTEX_CONFIG_PATH) so they can be changed
# without editing code; vertex_config() is read per spawn, so edits take effect on
# the next + New -> Claude (Vertex AI) with no restart. The file is plain JSON:
#   {"project_id": "...", "region": "eu", "model": "claude-opus-4-8[1m]",
#    "env": {"ENABLE_PROMPT_CACHING_1H": "1"}}
# `model` may carry Claude Code's `[1m]` suffix to enable the 1M context window.
# `env` is an optional dict of extra env vars passed through to the tile (e.g.
# per-model VERTEX_REGION_* overrides). Per field, precedence is: explicit env var
# (ANTHROPIC_VERTEX_PROJECT_ID / CLOUD_ML_REGION / ANTHROPIC_MODEL) > file > the
# built-in default below.
VERTEX_CONFIG_PATH = os.environ.get("VERTEX_CONFIG") or os.path.expanduser(
    "~/.config/session-dashboard/vertex.json")
VERTEX_DEFAULTS = {
    "project_id": "",
    "region": "eu",
    "model": "claude-opus-4-8[1m]",
    "env": {},
}


def vertex_config():
    """Resolve the Vertex provider config for a tile spawn. Reads the JSON file at
    VERTEX_CONFIG_PATH ({project_id, region, model, env}) when present, lets the
    matching explicit env vars win over the file, and the file win over
    VERTEX_DEFAULTS. Always returns a dict with project_id/region/model/env; a
    missing or malformed file is ignored (falls back to env/defaults)."""
    cfg = dict(VERTEX_DEFAULTS)
    cfg["env"] = dict(VERTEX_DEFAULTS["env"])
    try:
        with open(VERTEX_CONFIG_PATH) as f:
            filecfg = json.load(f)
        if isinstance(filecfg, dict):
            for k in ("project_id", "region", "model"):
                if filecfg.get(k) is not None:
                    cfg[k] = filecfg[k]
            if isinstance(filecfg.get("env"), dict):
                cfg["env"].update({str(k): str(v) for k, v in filecfg["env"].items()})
    except (OSError, ValueError):
        pass
    if os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"):
        cfg["project_id"] = os.environ["ANTHROPIC_VERTEX_PROJECT_ID"]
    if os.environ.get("CLOUD_ML_REGION"):
        cfg["region"] = os.environ["CLOUD_ML_REGION"]
    if os.environ.get("ANTHROPIC_MODEL"):
        cfg["model"] = os.environ["ANTHROPIC_MODEL"]
    return cfg


def _vertex_child_env():
    """Child environment for a provider=vertex claude tile: the parent env plus
    the Vertex routing switches. Factored out of spawn_claude so resurrection
    (reviving tiles after a reboot) applies the same per-session routing."""
    vc = vertex_config()
    env = {**os.environ,
           "CLAUDE_CODE_USE_VERTEX": "1",
           "CLOUD_ML_REGION": vc["region"],
           "ANTHROPIC_VERTEX_PROJECT_ID": vc["project_id"]}
    if vc["model"]:
        env["ANTHROPIC_MODEL"] = vc["model"]
    env.update(vc["env"])  # optional extra knobs from the config file
    return env


# Configurable launchers for the "+ New" menu. Each preset is a raw command line
# (e.g. `claude --dangerously-skip-permissions`, `codex`, `opencode`) the user can
# edit, plus optional per-launcher env vars so a preset can point at a custom
# endpoint provider (Google Vertex, an Anthropic-compatible proxy via
# ANTHROPIC_BASE_URL, an OpenAI-compatible base via OPENAI_BASE_URL, ...). The
# command's program name (claude/codex/opencode) selects the smart spawn path so
# those tiles keep fork/resume/chat + correct badges; anything else runs as a
# generic kind=custom tile. Stored as plain JSON so it survives restarts and is
# editable from the dashboard's "Manage launchers…" modal.
LAUNCHERS_CONFIG_PATH = os.environ.get("LAUNCHERS_CONFIG") or os.path.expanduser(
    "~/.config/session-dashboard/launchers.json")
DEFAULT_LAUNCHERS = [
    {"id": "claude", "label": "Claude", "command": "claude"},
    {"id": "claude-skip", "label": "Claude (skip-perms)",
     "command": "claude --dangerously-skip-permissions"},
    {"id": "claude-haiku", "label": "Claude (Haiku)",
     "command": "claude --model haiku"},
    {"id": "claude-vertex", "label": "Claude (Vertex AI)",
     "command": "claude", "provider": "vertex"},
    {"id": "codex", "label": "Codex (ChatGPT)", "command": "codex"},
    {"id": "codex-bypass", "label": "Codex (bypass sandbox)",
     "command": "codex --dangerously-bypass-approvals-and-sandbox"},
    {"id": "opencode", "label": "opencode", "command": "opencode"},
]
# Command used to relaunch a legacy kind=host tile that has no recorded launcher
# command — i.e. a session started OUTSIDE the "+ New" menu (e.g. an external
# wrapper that self-registers a web tile). Modern tiles spawned from "+ New"
# carry their own command and ignore this. Empty by default: with no recorded
# command and no override here, host relaunch/fork simply bails. Set it to your
# external web launcher (e.g. "mywrapper -web") to re-enable that path.
HOST_LAUNCH_CMD = os.environ.get("HOST_LAUNCH_CMD", "")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _slug(text):
    """A safe id slug from a label: lowercase, non-alnum -> '-', trimmed."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "launcher"


def _clean_launcher(raw, seen_ids):
    """Validate/normalise one launcher dict from disk or an API write. Returns a
    sanitised dict or None if it has no usable label+command. Env keys must look
    like shell identifiers; values are coerced to str and length-capped. Ids are
    de-duplicated against seen_ids (mutated)."""
    if not isinstance(raw, dict):
        return None
    label = (raw.get("label") or "").strip()
    command = (raw.get("command") or "").strip()
    if not label or not command:
        return None
    try:
        if not shlex.split(command):
            return None
    except ValueError:
        return None  # unbalanced quotes etc.
    lid = _slug(str(raw.get("id") or label))
    base, n = lid, 2
    while lid in seen_ids:
        lid = "%s-%d" % (base, n)
        n += 1
    seen_ids.add(lid)
    item = {"id": lid, "label": label[:80], "command": command[:2000]}
    if raw.get("provider") == "vertex":
        item["provider"] = "vertex"
    env = raw.get("env")
    if isinstance(env, dict):
        clean_env = {}
        for k, v in env.items():
            if isinstance(k, str) and _ENV_KEY_RE.match(k) and v is not None:
                clean_env[k] = str(v)[:4000]
        if clean_env:
            item["env"] = clean_env
    if isinstance(raw.get("icon"), str) and raw["icon"]:
        item["icon"] = raw["icon"][:40]
    return item


def load_launchers():
    """The configured launcher presets. Reads LAUNCHERS_CONFIG_PATH; a missing or
    malformed file falls back to (and is NOT auto-written as) DEFAULT_LAUNCHERS, so
    a first-run dashboard shows the common claude/codex/opencode configs."""
    try:
        with open(LAUNCHERS_CONFIG_PATH) as f:
            data = json.load(f)
        raw = data.get("launchers") if isinstance(data, dict) else None
        if isinstance(raw, list):
            seen, out = set(), []
            for r in raw:
                c = _clean_launcher(r, seen)
                if c:
                    out.append(c)
            return out if out else [dict(l) for l in DEFAULT_LAUNCHERS]
    except (OSError, ValueError):
        pass
    return [dict(l) for l in DEFAULT_LAUNCHERS]


def save_launchers(raw_list):
    """Validate and persist the launcher list (atomic write). Returns the cleaned
    list actually written, or None on I/O failure."""
    if not isinstance(raw_list, list):
        return None
    seen, clean = set(), []
    for r in raw_list[:200]:
        c = _clean_launcher(r, seen)
        if c:
            clean.append(c)
    try:
        os.makedirs(os.path.dirname(LAUNCHERS_CONFIG_PATH), exist_ok=True)
        tmp = LAUNCHERS_CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"launchers": clean}, f, indent=2)
        os.replace(tmp, LAUNCHERS_CONFIG_PATH)
    except OSError:
        return None
    return clean


def _manages_session(args):
    """True if the user's launcher flags already pick a claude session (so we must
    NOT inject our own --session-id, which would conflict / be ignored)."""
    return any(a in ("--session-id", "--resume", "-r", "--continue", "-c")
               for a in (args or []))


def _launcher_extra(s):
    """The flags after the program name from a tile's stored launcher command
    (empty for legacy tiles with no recorded command)."""
    cmdstr = s.get("command")
    if not cmdstr:
        return []
    try:
        return shlex.split(cmdstr)[1:]
    except ValueError:
        return []


def _strip_session_flags(extra):
    """Drop any session-selecting flags (and the value they consume) from a
    claude flag list — used on revive, where WE re-attach --resume/--session-id."""
    out, skip = [], False
    for a in extra:
        if skip:
            skip = False
            continue
        if a in ("--session-id", "--resume", "-r"):
            skip = True  # also swallow the id that follows
            continue
        if a in ("--continue", "-c"):
            continue
        out.append(a)
    return out


def _launcher_child_env(provider=None, env=None):
    """Child environment for a launcher tile, or None to inherit unchanged.
    provider=='vertex' applies the dynamic Vertex routing (gcloud ADC + vertex.json,
    same as the legacy Vertex item); an explicit `env` dict then overlays on top so
    a preset can point claude/codex at any custom endpoint provider."""
    if not provider and not env:
        return None
    base = _vertex_child_env() if provider == "vertex" else dict(os.environ)
    if env:
        for k, v in env.items():
            if isinstance(k, str) and _ENV_KEY_RE.match(k) and v is not None:
                base[k] = str(v)
    return base

# Accept requests only when the browser sent one of these Host headers. A
# DNS-rebinding page (evil.example -> 127.0.0.1) carries Host: evil.example,
# so this rejects it before it can read /api/sessions or hit /api/close.
ALLOWED_HOSTS = frozenset({"127.0.0.1:%d" % PORT, "localhost:%d" % PORT})
# Per-startup secret embedded in the page and required as a header on the
# state-changing POST. A cross-origin attacker can't read the page to learn it
# (and Host validation above blocks the DNS-rebinding way of becoming
# same-origin), so this defeats CSRF against /api/close.
CSRF_TOKEN = secrets.token_urlsafe(32)


def port_alive(port, attempts=2):
    """True if something is accepting on 127.0.0.1:port.

    Retries on TIMEOUT only (not on connection-refused). Every read_sessions()
    poll probes every session's port serially, each a 0.2 s connect; under a CPU
    spike (e.g. ~20 live tiles + a concurrent heavy test run) a *live* ttyd's
    connect can miss the 0.2 s deadline. A single missed probe must not read as
    "dead": that drops the session from /api/sessions, and the frontend then reaps
    its tile — and when every probe misses at once, EVERY tile vanishes though the
    sessions are alive (the "all terminals showed disconnecting and were gone"
    report). A second attempt rides out a transient stall. A *refused* connection
    is a definitive "nobody home", so we return at once and keep dead-port pruning
    fast."""
    for i in range(attempts):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except socket.timeout:
            continue                    # transient under load — try once more
        except OSError:
            return False                # refused / unreachable — truly not listening
    return False                        # every attempt timed out


def _live_ttyd_ports():
    """Set of ttyd ports belonging to live registered sessions. Used by the
    /t/<port>/ reverse proxy to constrain which localhost ports it will dial —
    so the proxy can't be turned into a generic localhost port scanner. Backed
    by read_sessions(), which already prunes entries whose ttyd has died."""
    return {s["port"] for s in read_sessions()
            if isinstance(s.get("port"), int)}


def read_sessions():
    out = []
    if not os.path.isdir(REGISTRY):
        return out
    for fn in sorted(os.listdir(REGISTRY)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(REGISTRY, fn)
        try:
            with open(path) as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        kind = s.get("kind", "host")
        # Webview tiles have no backing process — they're pure URL holders the
        # dashboard renders into an iframe. Skip the port-alive prune; they're
        # only removed explicitly (close button) or by editing the JSON.
        if kind == "webview":
            out.append({
                "id": fn[:-5],
                "name": s.get("name") or s.get("url") or fn[:-5],
                "kind": "webview",
                "url": s.get("url", ""),
                "proxy": bool(s.get("proxy")),
                "cwd": s.get("cwd", ""),
                "started": s.get("started", ""),
                "stashed": bool(s.get("stashed")),
            })
            continue
        # Channel tiles also have no backing process — they're a chatroom UI
        # served from the dashboard itself at /channel/<name>. Same prune
        # exemption as webviews; close button is the only way to remove them.
        if kind == "channel":
            out.append({
                "id": fn[:-5],
                "name": s.get("name") or ("#" + s.get("channel", "?")),
                "kind": "channel",
                "channel": s.get("channel", ""),
                "cwd": "",
                "started": s.get("started", ""),
                "stashed": bool(s.get("stashed")),
            })
            continue
        # Note tiles are backend-less scratchpads (text + pasted images) served
        # from the dashboard at /note/<id>; the body lives in a sidecar file
        # (note-<id>.body). `cwd` is only a tab-grouping hint. Same prune
        # exemption as webviews/channels — only the close button removes them.
        if kind == "note":
            out.append({
                "id": fn[:-5],
                "name": s.get("name") or fn[:-5],
                "kind": "note",
                "cwd": s.get("cwd", ""),
                "started": s.get("started", ""),
                "stashed": bool(s.get("stashed")),
            })
            continue
        try:
            port = int(s["port"])
        except (KeyError, TypeError, ValueError):
            continue
        sid = fn[:-5]
        alive = port_alive(port)
        now = time.time()
        if alive:
            _last_alive[sid] = now
        # A session confirmed alive within ALIVE_HYSTERESIS is treated as still
        # alive even when this probe failed — it almost certainly just missed the
        # 0.2 s connect under load. Without this, a load spike that times out every
        # probe at once empties the list and the frontend reaps every live tile. A
        # never-seen-alive (booting) session has no record, so it still waits for a
        # real listen.
        recently_alive = (not alive and sid in _last_alive
                          and now - _last_alive[sid] < ALIVE_HYSTERESIS)
        if alive or recently_alive or s.get("stashed"):
            entry = {
                "id": sid,
                "name": s.get("name") or sid,
                "port": port,
                "kind": kind,
                "cwd": s.get("cwd", ""),
                "started": s.get("started", ""),
                "stashed": bool(s.get("stashed")),
            }
            # Flag dead only when the probe failed AND hysteresis didn't vouch for
            # it — i.e. a stashed entry whose ttyd is genuinely gone. A live tile
            # riding out a transient miss (recently_alive) is NOT marked dead.
            if not alive and not recently_alive:
                entry["dead"] = True
            # "Terminal in container" tiles register as kind=terminal but with
            # a container field — the frontend uses this to pick a distinct
            # badge so the user can tell host shell vs container shell apart.
            if s.get("container"):
                entry["container"] = True
            out.append(entry)
        else:
            _last_alive.pop(sid, None)
            # Prune only once the entry is stale, so we don't race a session
            # whose ttyd hasn't started listening yet.
            try:
                if time.time() - os.path.getmtime(path) > PRUNE_GRACE:
                    os.remove(path)
            except OSError:
                pass
    # Drop hysteresis records for sessions whose registry file is gone (closed),
    # so the cache can't grow without bound or vouch for a later-reused id.
    if _last_alive:
        for k in list(_last_alive):
            if not os.path.exists(os.path.join(REGISTRY, k + ".json")):
                _last_alive.pop(k, None)
    # Stable, deterministic order across reloads: creation time, then the unique
    # id (filename) as a tiebreak. (Sorting by name alone tied on duplicate
    # basenames and fell back to filesystem listing order, which varies.)
    out.sort(key=lambda x: (x.get("started", ""), x["id"]))
    return out


def _pids(args):
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=2).stdout
        return [int(x) for x in out.split()]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _port_shared_by_other(sid, port):
    """True if a registry entry other than <sid> also claims <port>. A
    port-allocation race can let two sessions share a port (and the ttyd
    listening on it); closing one by port would then kill the other, so the
    caller skips the port/socket-based kill when this is true."""
    if port is None:
        return False
    me = sid + ".json"
    try:
        names = os.listdir(REGISTRY)
    except OSError:
        return False
    for fn in names:
        if not fn.endswith(".json") or fn == me:
            continue
        try:
            with open(os.path.join(REGISTRY, fn)) as f:
                if int(json.load(f).get("port")) == port:
                    return True
        except (OSError, ValueError, TypeError):
            continue
    return False


def stash_session(sid, on):
    """Flip the `stashed` field in <sid>.json without touching the backing
    process. Stash = "hide the tile, keep the container/ttyd running"; restore
    is the inverse. Webviews and live sessions are treated identically — the
    flag is just persisted UI state the frontend reads to decide what to render
    as a tile vs. what to list in the stash drawer.

    Returns True on a successful write, False if the entry doesn't exist or the
    write fails. Callers don't need to check the previous value — flipping the
    flag is idempotent."""
    sid = os.path.basename(sid or "")  # guard against path traversal
    if not sid:
        return False
    path = os.path.join(REGISTRY, sid + ".json")
    try:
        with open(path) as f:
            entry = json.load(f)
    except (OSError, ValueError):
        return False
    if on:
        entry["stashed"] = True
    else:
        entry.pop("stashed", None)
    try:
        with open(path, "w") as f:
            json.dump(entry, f)
    except OSError:
        return False
    return True


def close_session(sid):
    """Terminate a registered session: kill its ttyd + dtach (the claude
    process) and drop its registry file. Returns True if the entry existed."""
    sid = os.path.basename(sid or "")  # guard against path traversal
    if not sid:
        return False
    path = os.path.join(REGISTRY, sid + ".json")
    try:
        with open(path) as f:
            s = json.load(f)
    except (OSError, ValueError):
        return False
    # Webviews + channel + note tiles are pure registry entries (no backing
    # process) — just drop the file and exit; nothing to kill. Notes also have
    # a sidecar body file to remove.
    if s.get("kind") in ("webview", "channel", "note"):
        if s.get("kind") == "note":
            try:
                os.remove(_note_body_path(sid))
            except OSError:
                pass
        try:
            os.remove(path)
        except OSError:
            pass
        return True
    try:
        port = int(s["port"])
    except (KeyError, TypeError, ValueError):
        port = None
    sock = s.get("sock")
    # If another session shares this port (a port-collision bug slipped through),
    # killing by port or by the port-named socket would take the bystander down
    # too — so skip those kills and just drop this entry's registry file.
    shared = _port_shared_by_other(sid, port)
    if not shared:
        pids = []
        if port is not None:
            pids += _pids(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"])
        if sock:
            pids += _pids(["lsof", "-t", sock])
        for pid in set(pids):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    # In-container sessions (kind=container claude, or kind=terminal with a
    # container field — the "Terminal in container" tile) run behind a dtach
    # master inside the container, not on a host socket. Killing the host
    # ttyd above only drops the web bridge; we also end the in-container
    # process via podman exec (best-effort). The kill targets THIS session's
    # own csock, so it's safe even if the host port was shared.
    if s.get("container") and s.get("csock"):
        csock = shlex.quote(s["csock"])
        podman = _which("podman")
        if podman:
            try:
                subprocess.run(
                    [podman, "exec", s["container"], "sh", "-c",
                     "pkill -f %s 2>/dev/null; rm -f %s" % (csock, csock)],
                    capture_output=True, timeout=6)
            except (OSError, subprocess.SubprocessError):
                pass
    # Always remove the registry file; remove the host socket only when it isn't
    # shared with a colliding session (which may still need it).
    for p in ([path] if shared else [sock, path]):
        try:
            if p:
                os.remove(p)
        except OSError:
            pass
    return True


def _projects_dir(cwd, container):
    """Path to the directory holding a tile's claude conversation .jsonl files.

    Host: ~/.claude/projects/<slug>/ where <slug> is the cwd with '/'
    replaced by '-' (so `/Users/x/proj` → `-Users-x-proj`).

    Container: <host-cwd>/.claude/projects/-workspace/ — the container sees cwd
    as /workspace (slug `-workspace`), and its /home/node/.claude/projects is
    bind-mounted to <host-cwd>/.claude/projects, so the host path resolves the
    same file the in-container claude writes.
    """
    if container:
        return os.path.join(cwd, ".claude", "projects", "-workspace")
    slug = cwd.rstrip("/").replace("/", "-")
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", slug)


def _session_jsonl(cwd, container, session_id):
    """Path to a SPECIFIC session's .jsonl, or None if `session_id` is falsy or
    the file doesn't exist. claude names each conversation file after its
    session UUID, so a recorded session_id maps deterministically to its file —
    unlike _newest_jsonl, which can't tell two sessions in one cwd apart."""
    if not session_id:
        return None
    # session_id comes from our own registry, but it's used as a filename below,
    # so keep it to a bare UUID-ish basename as defence in depth.
    session_id = os.path.basename(str(session_id))
    cand = os.path.join(_projects_dir(cwd, container), session_id + ".jsonl")
    return cand if os.path.isfile(cand) else None


def _newest_jsonl(cwd, container):
    """Locate the most recent claude conversation .jsonl for a tile's cwd.

    Fallback used only when a session's exact id is unknown (legacy sessions
    registered before session_id was recorded): with several sessions sharing a
    cwd this can pick the wrong one, so prefer _session_jsonl when possible.
    """
    projects = _projects_dir(cwd, container)
    try:
        jsonls = [os.path.join(projects, fn) for fn in os.listdir(projects)
                  if fn.endswith(".jsonl")]
    except OSError:
        return None
    if not jsonls:
        return None
    return max(jsonls, key=os.path.getmtime)


def _registry_record(sid):
    """Raw registry JSON for a tile id, or None. Unlike read_sessions() this
    keeps every field (session_id, container, ...) — the chat tailer needs them
    to resolve the backing .jsonl."""
    if not sid:
        return None
    sid = os.path.basename(str(sid))  # used as a filename below — keep it bare
    try:
        with open(os.path.join(REGISTRY, sid + ".json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _tile_jsonl(sid):
    """Resolve the claude conversation .jsonl backing a tile id, or None when the
    tile isn't a claude session (terminal/webview/note) or hasn't written its
    transcript yet.

    When the registry records a session_id (the common case — our launchers pin
    one via `--session-id`, so it equals claude's own .jsonl filename), use ONLY
    that exact file. Crucially, do NOT fall back to _newest_jsonl here: a freshly
    duplicated tile is an empty new session whose file doesn't exist yet, and the
    newest .jsonl in its cwd is a *sibling's* transcript — that fallback is what
    made a clone's chat panel show the original tile's conversation. Returning
    None until the new session writes its own file shows it correctly as empty.

    The _newest_jsonl fallback is kept only for legacy tiles that predate
    session_id tracking and have nothing better to go on."""
    s = _registry_record(sid)
    if not s:
        return None
    kind = s.get("kind", "host")
    container = (kind == "container") or bool(s.get("container"))
    # host/container claude tiles have a transcript; a kind=terminal tile only
    # does when it's the shell of a the container launcher container (container flag set).
    if kind not in ("host", "container") and not container:
        return None
    cwd = s.get("cwd")
    if not cwd:
        return None
    if s.get("session_id"):
        # Reconstructed path first (the common, fully-offline case); fall back to
        # the hook-reported transcript_path when reconstruction misses — e.g. a
        # cwd whose slug doesn't round-trip, or a transcript claude put somewhere
        # unexpected. The hook path is validated against this exact session_id.
        return (_session_jsonl(cwd, container, s.get("session_id"))
                or _hook_transcript_path(s.get("session_id")))
    return _newest_jsonl(cwd, container)


def _hook_transcript_path(session_id):
    """The transcript file claude reported via hooks for `session_id`, but only
    if it passes a tight safety check: an existing absolute real path named
    `<session_id>.jsonl` inside a `.claude/projects/` tree. This endpoint streams
    the file to the browser, so an unvalidated, hook-supplied path would be an
    arbitrary-file-read; tying the basename to the session id keeps it to the
    one file the reconstruction would have produced anyway."""
    if not session_id:
        return None
    snap = _hook_snapshot(session_id)
    tp = snap.get("transcript") if snap else None
    if not tp or not isinstance(tp, str) or not os.path.isabs(tp):
        return None
    rp = os.path.realpath(tp)
    if os.path.basename(rp) != os.path.basename(str(session_id)) + ".jsonl":
        return None
    if os.sep + ".claude" + os.sep + "projects" + os.sep not in rp:
        return None
    return rp if os.path.isfile(rp) else None


def _tool_brief(b):
    """A {name, detail} summary of a tool_use block for the chat panel — the
    name alone ('Bash') is uninformative, so surface the salient input: the
    command, file, query, etc. Detail is whitespace-collapsed and capped."""
    name = b.get("name", "tool")
    inp = b.get("input")
    if not isinstance(inp, dict):
        return {"name": name, "detail": ""}
    # File edits carry their before/after so the panel can show a clickable diff.
    if name in ("Edit", "MultiEdit", "Write"):
        if name == "Write":
            edits = [{"old_string": "", "new_string": inp.get("content")}]
        elif name == "MultiEdit":
            ed = inp.get("edits")
            edits = ed if isinstance(ed, list) else []
        else:
            edits = [{"old_string": inp.get("old_string"), "new_string": inp.get("new_string")}]
        diff = [{"old": (e.get("old_string") or "")[:6000],
                 "new": (e.get("new_string") or "")[:6000]}
                for e in edits if isinstance(e, dict)]
        return {"name": name, "detail": inp.get("file_path") or "", "diff": diff}
    # Todo lists render as a checklist with status boxes.
    if name == "TodoWrite":
        td = inp.get("todos")
        todos = td if isinstance(td, list) else []
        return {"name": name, "detail": "",
                "todos": [{"content": t.get("content") or t.get("activeForm") or "",
                           "status": t.get("status") or ""}
                          for t in todos if isinstance(t, dict)]}
    # Interactive questions render as a card with the prompt + selectable options,
    # so the chat shows what claude asked (it's a tool_use, not prose, so it would
    # otherwise be invisible in the panel). The chosen answer arrives later as a
    # tool_result and is surfaced separately (see _jsonl_turn).
    if name == "AskUserQuestion":
        qs = inp.get("questions")
        qs = qs if isinstance(qs, list) else []
        out = []
        for q in qs:
            if not isinstance(q, dict):
                continue
            opts = q.get("options")
            opts = opts if isinstance(opts, list) else []
            out.append({
                "question": q.get("question") or "",
                "header": q.get("header") or "",
                "multi": bool(q.get("multiSelect")),
                "options": [{"label": o.get("label") or "",
                             "description": o.get("description") or ""}
                            for o in opts if isinstance(o, dict)],
            })
        return {"name": name, "detail": "", "questions": out}
    if name == "Bash":
        detail = inp.get("command") or inp.get("description") or ""
    elif name in ("Read", "Write", "Edit", "MultiEdit"):
        detail = inp.get("file_path") or ""
    elif name == "NotebookEdit":
        detail = inp.get("notebook_path") or ""
    elif name == "Grep":
        detail = inp.get("pattern") or ""
        if inp.get("path"):
            detail += "  in " + inp["path"]
    elif name == "Glob":
        detail = inp.get("pattern") or ""
    elif name in ("Task", "Agent"):
        detail = inp.get("description") or inp.get("prompt") or ""
    elif name == "WebFetch":
        detail = inp.get("url") or ""
    elif name == "WebSearch":
        detail = inp.get("query") or ""
    elif name == "TodoWrite":
        detail = ""
    else:
        # Unknown tool: show the first short, salient string-ish field we find.
        detail = ""
        for k in ("description", "command", "query", "url", "path",
                  "pattern", "file_path", "prompt"):
            v = inp.get(k)
            if isinstance(v, str) and v:
                detail = v
                break
    detail = " ".join(str(detail).split())  # collapse newlines/runs of spaces
    if len(detail) > 220:
        detail = detail[:220] + "…"
    return {"name": name, "detail": detail}


# Context-window sizes for the chat panel's usage meter, by model-id substring.
# The window isn't recorded per-message — claude strips the `[1m]` beta marker
# before the API call, so the transcript only ever shows e.g. "claude-opus-4-8"
# even when the session runs the 1M context. So map the models that run at 1M in
# this deployment; everything else uses DEFAULT_CONTEXT_WINDOW. Override the
# default (and effectively the 1M list, by setting it to 1000000) via the
# CHAT_CONTEXT_WINDOW env var.
DEFAULT_CONTEXT_WINDOW = int(os.environ.get("CHAT_CONTEXT_WINDOW") or 200000)
_CONTEXT_WINDOWS_1M = ("opus-4-8", "fable-5")


def _context_window(model):
    m = (model or "").lower()
    if any(n in m for n in _CONTEXT_WINDOWS_1M):
        return 1000000
    return DEFAULT_CONTEXT_WINDOW


_IDLE_STOPS = frozenset({"end_turn", "stop_sequence", "max_tokens"})
# If "busy" but the transcript hasn't been touched for this long, treat it as
# idle — an interrupt / error / slash-command can leave a trailing user message
# (or a partial assistant) with no end_turn, which would otherwise pin the
# "working…" indicator forever.
STALE_BUSY_SECS = 45


def _is_busy(last_role, last_stop, file_idle_secs, stale_secs=STALE_BUSY_SECS):
    """Whether claude is working, from the transcript's last real message: busy
    from a user message until the assistant reaches an idle stop_reason.

    Staleness guard against a stuck "working…": if nothing's been written for
    `stale_secs` it's an idle session after an interrupt/error. The ONE exemption
    is a genuinely-running tool — `assistant` + `tool_use` means the tool call was
    written and we're waiting on its result, which can take minutes with no
    writes. (A `user` + `tool_use` shape is the tool RESULT already back with
    claude not answering — that's stuck, not running, so it is NOT exempt.)"""
    busy = (last_role == "user") or \
           (last_role == "assistant" and last_stop not in _IDLE_STOPS)
    running_tool = (last_role == "assistant" and last_stop == "tool_use")
    if busy and not running_tool and file_idle_secs > stale_secs:
        busy = False
    return busy


# --- Claude Code hook events -------------------------------------------------
# A user-level hook (hooks/dashboard-notify.sh) forwards every hook event's
# stdin JSON to POST /api/hook-event. That gives the chat panel ground truth the
# transcript alone can't provide: Stop = the turn is definitely over (no more
# stale-guessing), Notification = a permission prompt is pending (these never
# appear in the .jsonl at all), Pre/PostToolUse = a tool actually started or
# finished. Keyed by claude's own session_id (the registry maps tiles to it).
# Advisory only: sessions without the hook installed (containers, sessions
# started before it) simply fall back to the transcript heuristics.
HOOK_STATE = {}                      # session_id -> {phase, ts, perm}
_HOOK_LOCK = threading.Lock()
_PERM_MSG_RE = re.compile(r"permission to use (\S+)", re.I)


def _hook_token(create=False):
    """Shared secret hooks use to authenticate to /api/hook-event. Lives in the
    registry dir (0600) where the hook script can read it but a web page can't —
    the browser CSRF token is unusable for hooks (they run outside the page)."""
    path = os.path.join(REGISTRY, ".hook-token")
    try:
        with open(path) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    if not create:
        return None
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


def hook_event(o, ppid=None):
    """Apply one forwarded hook event to HOOK_STATE. True if it was understood
    (known event with a session_id) and recorded.

    `ppid` is the hook script's parent pid (a descendant of the tile's claude;
    see dashboard-notify.sh). When it resolves to a registered host tile whose
    recorded session_id differs from the event's, the registry entry is updated
    in place — this is how the dashboard learns that a tile's conversation
    moved to a new id (/clear, claude relaunched inside the tile), keeping
    fork / chat panel / reboot-resurrection pointed at the LIVE conversation
    instead of the one the tile was spawned with."""
    if not isinstance(o, dict):
        return False
    sid = o.get("session_id")
    ev = o.get("hook_event_name")
    if not sid or not isinstance(sid, str) or not ev:
        return False
    if ppid:
        tile = _tile_for_pid(ppid)
        if tile:
            _record_live_session_id(tile, sid)
    now = time.time()
    with _HOOK_LOCK:
        st = HOOK_STATE.setdefault(sid, {"phase": None, "ts": 0, "perm": None})
        # Every event carries the exact transcript file claude is writing — the
        # ground-truth path, no cwd→slug reconstruction. Stash it (validated at
        # read time) so the chat tailer can prefer it.
        tp = o.get("transcript_path")
        if isinstance(tp, str) and tp:
            st["transcript"] = tp
        if ev in ("Stop", "SessionEnd"):
            st.update(phase="idle", ts=now, perm=None)
        elif ev == "UserPromptSubmit":
            # The prompt text arrives here BEFORE claude writes it to the .jsonl,
            # so the chat panel can echo the user's turn instantly (especially
            # the first one, when no transcript file exists yet).
            p = o.get("prompt")
            if isinstance(p, str) and p.strip():
                st["prompt"] = {"text": p[:8000], "ts": now}
            st.update(phase="busy", ts=now, perm=None)
        elif ev in ("PreToolUse", "PostToolUse"):
            # PreToolUse also means any pending permission prompt was approved.
            st.update(phase="busy", ts=now, perm=None)
        elif ev == "Notification":
            msg = str(o.get("message") or "")
            m = _PERM_MSG_RE.search(msg)
            if m or "permission" in msg.lower():
                st.update(ts=now, perm={"tool": m.group(1) if m else "",
                                        "message": msg[:300], "ts": now})
            elif "waiting for your input" in msg.lower():
                st.update(phase="idle", ts=now)
            else:
                return False
        else:
            return False
        # Bound memory: a long-lived server sees many sessions come and go.
        if len(HOOK_STATE) > 512:
            for k in sorted(HOOK_STATE, key=lambda k: HOOK_STATE[k]["ts"])[:64]:
                HOOK_STATE.pop(k, None)
    return True


def _hook_snapshot(session_id):
    """Thread-safe copy of a session's hook state, or None."""
    if not session_id:
        return None
    with _HOOK_LOCK:
        st = HOOK_STATE.get(session_id)
        if not st:
            return None
        return dict(st, perm=dict(st["perm"]) if st.get("perm") else None,
                    prompt=dict(st["prompt"]) if st.get("prompt") else None)


# A tile's registry session_id is pinned at spawn, but the conversation can
# move to a NEW id underneath it (/clear mints one; so does relaunching claude
# inside the tile after an exit). A stale id breaks everything keyed on it:
# fork copies the wrong (or no) transcript, the chat panel tails the wrong
# file, and reboot resurrection resumes the pre-/clear conversation. The hook
# script runs as a descendant of the tile's claude, so it can tell us where it
# lives: it forwards its $PPID, and we walk that pid's ancestry to the dtach
# master whose `-A <sock>` argv names a registered tile socket. Cached per pid
# — a claude process keeps its pid across /clear, so one walk per process.
_PID_TILE_CACHE = {}                 # pid -> sid (host tiles only)


def _ps_table():
    """{pid: (ppid, command)} snapshot of every process, or {} on failure."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="],
                             capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for ln in out.splitlines():
        parts = ln.split(None, 2)
        if len(parts) >= 2:
            try:
                table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
            except ValueError:
                continue
    return table


def _tile_for_pid(pid):
    """Resolve a hook's reported pid to the host tile (registry sid) whose
    dtach master is among its ancestors, or None. Container tiles can't be
    resolved this way (their hooks run inside the container and can't reach
    this server anyway)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    cached = _PID_TILE_CACHE.get(pid)
    if cached:
        return cached
    socks = {}
    try:
        for fn in os.listdir(REGISTRY):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(REGISTRY, fn)) as f:
                    s = json.load(f)
            except (OSError, ValueError):
                continue
            if s.get("kind", "host") == "host" and s.get("sock"):
                socks[s["sock"]] = fn[:-5]
    except OSError:
        return None
    if not socks:
        return None
    table = _ps_table()
    qpid, seen = pid, 0
    while pid in table and seen < 12:
        ppid, cmd = table[pid]
        if "dtach" in cmd:
            for sock, sid in socks.items():
                if sock in cmd:
                    if len(_PID_TILE_CACHE) > 256:
                        _PID_TILE_CACHE.clear()
                    _PID_TILE_CACHE[qpid] = sid
                    return sid
        pid = ppid
        seen += 1
    return None


def _tile_for_session_id(session_id):
    """Resolve a claude session UUID to the registry sid that records it, or
    None. Fallback for /api/agent-close when process-ancestry resolution fails
    (the agent passes the session_id it knows about itself)."""
    if not session_id or not isinstance(session_id, str):
        return None
    try:
        for fn in os.listdir(REGISTRY):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(REGISTRY, fn)) as f:
                    s = json.load(f)
            except (OSError, ValueError):
                continue
            if s.get("session_id") == session_id:
                return fn[:-5]
    except OSError:
        return None
    return None


def _tile_for_name(name):
    """Resolve a human tile NAME to (sid, entry), for agent-to-agent messaging.
    Matching: an exact case-insensitive name match wins; otherwise a UNIQUE
    case-insensitive substring match. Returns:
      (sid, entry)      a single unambiguous match
      ("", candidates)  zero or multiple matches — `candidates` is a list of
                        {id, name, cwd, kind} dicts (empty when nothing matched)
                        so the caller can disambiguate."""
    if not name or not isinstance(name, str):
        return ("", [])
    want = name.strip().lower()
    entries = []
    try:
        for fn in os.listdir(REGISTRY):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(REGISTRY, fn)) as f:
                    s = json.load(f)
            except (OSError, ValueError):
                continue
            entries.append((fn[:-5], s))
    except OSError:
        return ("", [])
    matches = [(sid, s) for sid, s in entries
               if str(s.get("name", "")).strip().lower() == want]
    if not matches:
        matches = [(sid, s) for sid, s in entries
                   if want in str(s.get("name", "")).strip().lower()]
    if len(matches) == 1:
        return matches[0]
    cands = [{"id": sid, "name": s.get("name"), "cwd": s.get("cwd"),
              "kind": s.get("kind", "host")} for sid, s in matches]
    return ("", cands)


def _session_title(jsonl_path, maxlen=80):
    """A short human title for a claude conversation: its `summary` entry if it
    has one, else its first real user message — collapsed to one line and
    truncated. None if the file is missing/empty/unreadable. Reads only the
    first handful of entries (the title material lives at the top)."""
    if not jsonl_path or not os.path.isfile(jsonl_path):
        return None
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(o, dict):
                    continue
                if o.get("type") == "summary" and o.get("summary"):
                    return " ".join(str(o["summary"]).split())[:maxlen]
                if o.get("type") != "user":
                    continue
                content = (o.get("message") or {}).get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            text = b.get("text") or ""
                            break
                text = " ".join(text.split())
                # Skip slash-command wrappers, caveats, tool-result echoes — none
                # of which read as a conversation title.
                if not text or text.startswith(("<", "Caveat:", "[Request")):
                    continue
                return text[:maxlen]
    except OSError:
        return None
    return None


def _conversation_cwd(jsonl_path):
    """The cwd a claude conversation ran in, read from the transcript (claude
    records `cwd` on its entries). None if not found."""
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for _ in range(10):
                line = f.readline()
                if not line:
                    break
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if isinstance(o, dict) and o.get("cwd"):
                    return o["cwd"]
    except OSError:
        pass
    return None


def _all_conversations(limit=300):
    """Scan ~/.claude/projects transcripts and return the `limit` most-recently
    modified as {session_id, cwd, title, mtime}, newest first. Used by the
    restore-session search (keyword-match a past conversation by title)."""
    root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    files = []
    try:
        for slug in os.listdir(root):
            d = os.path.join(root, slug)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if not fn.endswith(".jsonl"):
                    continue
                p = os.path.join(d, fn)
                try:
                    files.append((os.path.getmtime(p), p, slug, fn[:-len(".jsonl")]))
                except OSError:
                    pass
    except OSError:
        return []
    files.sort(reverse=True)
    out = []
    for mtime, p, slug, sid in files[:limit]:
        cwd = _conversation_cwd(p) or ("/" + slug.lstrip("-").replace("-", "/"))
        out.append({"session_id": sid, "cwd": cwd,
                    "title": _session_title(p) or "(untitled)", "mtime": mtime})
    return out


def _record_live_session_id(sid, session_id):
    """Persist a tile's CURRENT claude session id into its registry entry when
    it drifted from the recorded one. Best-effort read-modify-write; the
    registry is the only store, so fork / chat panel / resurrection all pick
    the new id up on their next read."""
    path = os.path.join(REGISTRY, sid + ".json")
    try:
        with open(path) as f:
            s = json.load(f)
        if s.get("session_id") == session_id:
            return False
        s["session_id"] = session_id
        with open(path, "w") as f:
            json.dump(s, f)
        return True
    except (OSError, ValueError):
        return False


def _iso_ms(s):
    """Parse an ISO-8601 timestamp to epoch milliseconds, or None."""
    if not s:
        return None
    try:
        return int(datetime.datetime.fromisoformat(
            str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _jsonl_turn(o):
    """Convert one transcript record into a compact chat turn, or None to skip.
    Mirrors extract_chat_claude's text extraction but also surfaces tool calls
    as a lightweight marker, so the panel shows activity — not just prose."""
    t = o.get("type")
    if t not in ("user", "assistant") or o.get("isSidechain"):
        return None
    msg = o.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    text_parts, tools, answers = [], [], []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                text_parts.append(b.get("text", "") or "")
            elif bt == "tool_use":
                tools.append(_tool_brief(b))
            elif bt == "tool_result":
                # The only tool_result we surface is an AskUserQuestion answer, so
                # the chat shows what the user picked. Everything else (file reads,
                # command output, …) stays dropped to keep the panel clean.
                answers.extend(_answer_brief(b.get("content")))
            # thinking blocks are dropped to keep the panel clean
    text = "\n\n".join(p for p in text_parts if p and p.strip()).strip()
    if not text and not tools and not answers:
        return None  # e.g. a user turn that's only a tool_result echo
    return {
        "role": t,
        "text": text,
        "tools": tools,
        "answers": answers,
        "ts": o.get("timestamp") or "",
        "uuid": o.get("uuid") or "",
    }


# AskUserQuestion's answer comes back as a tool_result whose text reads
# `Your questions have been answered: "<q>"="<a>"[, "<q2>"="<a2>"]. …`. Parse it
# into {q, a} pairs so the chat can render "✓ <q> → <a>" under the question card.
_ANSWER_MARKER = "Your questions have been answered:"
_ANSWER_PAIR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"\s*=\s*"((?:[^"\\]|\\.)*)"')


def _answer_brief(rc):
    if isinstance(rc, list):
        rtxt = " ".join(x.get("text", "") for x in rc
                        if isinstance(x, dict) and x.get("type") == "text")
    elif isinstance(rc, str):
        rtxt = rc
    else:
        return []
    if _ANSWER_MARKER not in rtxt:
        return []
    return [{"q": q.strip(), "a": a.strip()}
            for q, a in _ANSWER_PAIR_RE.findall(rtxt)]


def _read_taskfile(path):
    """Read a background-task output/log file for the chat panel's notification
    cards. Tightly scoped on purpose — absolute real path, under a temp dir, in a
    claude task tree, with a known-safe extension — so this read-only endpoint
    can't be turned into an arbitrary-file slurp. Returns bytes, or None."""
    if not path or not os.path.isabs(path):
        return None
    rp = os.path.realpath(path)
    roots = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")
    if not any(rp.startswith(r) for r in roots):
        return None
    if "claude" not in rp or "/tasks/" not in rp:
        return None
    if not rp.endswith((".output", ".log", ".txt")):
        return None
    if not os.path.isfile(rp):
        return None
    try:
        with open(rp, "rb") as f:
            return f.read(2_000_000)  # cap at 2 MB — these are short task logs
    except OSError:
        return None


def _dtach_push(sock_path, data):
    """Inject bytes into a dtach session's PTY via MSG_PUSH packets — exactly
    what a dtach client does when you type. dtach's wire packet is
    [type=0][len][8-byte buf] = 10 bytes, carrying <=8 data bytes each, so the
    payload is chunked. Returns True on success."""
    if not sock_path or not data:
        return False
    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(sock_path)
        for i in range(0, len(data), 8):
            chunk = data[i:i + 8]
            s.sendall(bytes([0, len(chunk)]) + chunk + b"\x00" * (8 - len(chunk)))
        time.sleep(0.05)  # let the master drain the pushes before we close
        return True
    except OSError:
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def chat_send(sid, text):
    """Send a typed message into a claude tile's session from the chat panel.
    Works for host dtach tiles (the registry records their `sock`); container /
    terminal / webview tiles have no host-side socket. Returns (ok, error)."""
    s = _registry_record(sid)
    if not s:
        return False, "unknown session"
    sock = s.get("sock")
    if not sock:
        return False, "this tile has no input socket (container or non-claude)"
    # Drop ESC and other control bytes so a message can't smuggle terminal
    # escapes (e.g. break out of the bracketed-paste wrapper below); keep
    # printable text plus newlines and tabs.
    text = "".join(c for c in (text or "")
                   if c in "\n\t" or (ord(c) >= 0x20 and ord(c) != 0x7f))
    if not text.strip():
        return False, "empty message"
    # A single line is just typed + Enter. A multi-line message is wrapped in
    # bracketed paste so claude takes it as ONE input block (it won't submit on
    # each newline — it detects the paste); a trailing CR then submits.
    if "\n" in text:
        payload = ("\x1b[200~" + text + "\x1b[201~\r").encode("utf-8", "replace")
    else:
        payload = (text + "\r").encode("utf-8", "replace")
    if _dtach_push(sock, payload):
        return True, None
    return False, "could not reach the session socket"


# Key names the chat panel may inject (clickable AskUserQuestion options and
# permission-prompt buttons drive claude's own TUI selector). Deliberately
# tiny: digits pick numbered options, enter submits, esc cancels/denies,
# arrows + space navigate multi-selects.
CHAT_KEYS = {"enter": "\r", "esc": "\x1b", "space": " ", "tab": "\t",
             "up": "\x1b[A", "down": "\x1b[B"}
CHAT_KEYS.update({str(d): str(d) for d in range(1, 10)})


def chat_key(sid, key):
    """Inject one named keystroke into a claude tile's PTY. Allowlist only —
    this is how the chat panel answers select questions / permission prompts."""
    seq = CHAT_KEYS.get(key or "")
    if not seq:
        return False, "unsupported key"
    s = _registry_record(sid)
    if not s:
        return False, "unknown session"
    sock = s.get("sock")
    if not sock:
        return False, "this tile has no input socket (container or non-claude)"
    if _dtach_push(sock, seq.encode()):
        return True, None
    return False, "could not reach the session socket"


def _complete_paths(sid, q):
    """File/dir completions under a session's cwd for the composer's @-mentions
    (claude's @ file picker). Scoped to cwd — no escaping via '..'. Returns
    {items: [{name, dir}], dir: <already-typed directory prefix>}."""
    s = _registry_record(sid)
    cwd = s.get("cwd") if s else None
    if not cwd or not os.path.isdir(cwd):
        return {"items": [], "dir": ""}
    q = q or ""
    dpart, prefix = q.rsplit("/", 1) if "/" in q else ("", q)
    root = os.path.realpath(cwd)
    base = os.path.realpath(os.path.join(cwd, dpart))
    if base != root and not base.startswith(root + os.sep):   # stay inside cwd
        return {"items": [], "dir": ""}
    try:
        names = os.listdir(base)
    except OSError:
        return {"items": [], "dir": (dpart + "/") if dpart else ""}
    show_hidden = prefix.startswith(".")
    pl = prefix.lower()
    items = []
    for nm in names:
        if nm.startswith(".") and not show_hidden:
            continue
        if pl and not nm.lower().startswith(pl):
            continue
        items.append({"name": nm, "dir": os.path.isdir(os.path.join(base, nm))})
    items.sort(key=lambda it: (not it["dir"], it["name"].lower()))
    return {"items": items[:30], "dir": (dpart + "/") if dpart else ""}


def fork_session(sid):
    """Fork a claude tile: copy its most recent conversation .jsonl, rewrite
    the embedded sessionId to a fresh UUID, then spawn a new tile that resumes
    from the copy. The original and the fork share the conversation up to
    "now" and then diverge independently — useful for "try a different
    direction without losing the current one."

    Terminals and webviews have no claude conversation, so fork is a no-op
    for them (returns False).

    Race note: copying a live jsonl while the source claude is mid-write
    captures whatever bytes are flushed at that instant. Trailing partial
    lines are unlikely (claude flushes per message), but a torn-write fork
    just means the fork's last message may be missing — the source is
    untouched.
    """
    import uuid as _uuid
    sid = os.path.basename(sid or "")
    if not sid:
        return False
    try:
        with open(os.path.join(REGISTRY, sid + ".json")) as f:
            s = json.load(f)
    except (OSError, ValueError):
        return False
    kind = s.get("kind")
    if kind not in ("host", "container"):
        return False
    cwd = s.get("cwd")
    if not cwd or not os.path.isdir(cwd):
        return False
    # Fork THIS session's conversation, not just whatever was written last in
    # this cwd: when several claude tiles share a cwd, _newest_jsonl can resolve
    # to a sibling session (the "forks the wrong session" bug). The registry
    # records each session's own UUID (session_id == jsonl filename) — use it.
    # When that id is recorded but its .jsonl doesn't exist (a session that
    # never spoke, or a stale id from before hook-driven tracking), fail the
    # fork rather than fall back to newest-in-cwd: the newest .jsonl is a
    # SIBLING tile's transcript, and forking it is the "forks the wrong tile"
    # bug all over again (same trap _tile_jsonl already avoids for the chat
    # panel). Newest-in-cwd survives only for legacy entries with no id at all.
    container = (kind == "container")
    if s.get("session_id"):
        src = _session_jsonl(cwd, container, s.get("session_id"))
    else:
        src = _newest_jsonl(cwd, container)
    if not src:
        return False
    old_uuid = os.path.basename(src)[: -len(".jsonl")]
    new_uuid = str(_uuid.uuid4())
    dst = os.path.join(os.path.dirname(src), new_uuid + ".jsonl")
    try:
        with open(src, "rb") as f:
            data = f.read()
        # Swap every reference to the original session ID so the fork is a
        # standalone session (sessionId fields, any embedded resume refs, etc.)
        # claude looks up sessions by filename; an internally-mismatched
        # sessionId may confuse the resume path.
        data = data.replace(old_uuid.encode(), new_uuid.encode())
        with open(dst, "wb") as f:
            f.write(data)
    except OSError:
        return False
    if kind == "container":
        # Container tiles relaunch via the launcher recorded when they were
        # spawned; without one we can't bring the container up, so bail.
        launcher = s.get("launcher")
        if not launcher:
            return False
        cmd = [launcher, "-web", "--detach", "--resume", new_uuid]
    else:
        # Host tiles relaunch via their recorded launcher command, or the
        # configured HOST_LAUNCH_CMD for legacy tiles with no recorded command.
        base = s.get("command") or HOST_LAUNCH_CMD
        if not base:
            return False
        cmd = ["zsh", "-ic",
               base + " --detach --resume " + shlex.quote(new_uuid)]
    try:
        subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return False
    return True


def _safe_filename(name):
    """Take a basename-only, printable-only version of `name`. Used to sanitize
    the filename of a drag/paste-uploaded file before joining it to a path on
    disk — strips `/`, `\\`, and any non-printable / NUL chars, falls back to
    `'file'` if nothing's left. The path-join in `save_dropped_file` happens
    AFTER this, so even a malicious `'../../etc/passwd'` from a compromised
    iframe would land as a literal `..-..-etc-passwd` style basename inside
    `.vibe-drops/`."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c != "\x00")
    name = name.strip()
    return name or "file"


def save_dropped_file(sid, name, data):
    """Save a drag/paste-uploaded file under `<session-cwd>/.vibe-drops/`
    with a uuid-prefixed safe name. Returns the path the session's running
    shell should USE to read the file:
      - container sessions: `/workspace/.vibe-drops/<uid>-<name>` (container
        sees the host cwd as /workspace via the bind-mount, so files written
        on the host appear there immediately),
      - host sessions (host / terminal / opencode): the host absolute path.
    Returns None if the sid is unknown, cwd is missing/unwritable, or the
    write fails. The `.vibe-drops/` dir is created on demand."""
    sid = os.path.basename(sid or "")
    if not sid:
        return None
    try:
        with open(os.path.join(REGISTRY, sid + ".json")) as f:
            s = json.load(f)
    except (OSError, ValueError):
        return None
    cwd = s.get("cwd")
    if not cwd or not os.path.isdir(cwd):
        return None
    safe = _safe_filename(name)
    uid = secrets.token_hex(4)
    drop_dir = os.path.join(cwd, ".vibe-drops")
    final = os.path.join(drop_dir, uid + "-" + safe)
    try:
        os.makedirs(drop_dir, exist_ok=True)
        with open(final, "wb") as f:
            f.write(data)
    except OSError:
        return None
    kind = s.get("kind")
    # the container launcher binds the session's cwd as /workspace inside the container,
    # so a container shell must reference the file by its CONTAINER path.
    # Two kinds use the container: top-level `the container launcher -web` (kind=container)
    # and the "Terminal in container" tiles (kind=terminal + container=true).
    in_container = kind == "container" or (kind == "terminal" and s.get("container"))
    if in_container:
        return "/workspace/.vibe-drops/" + uid + "-" + safe
    return final


def duplicate_session(sid):
    """Spawn another session of the same kind in the same cwd, detached so it
    just appears as a new tile. Returns True if a launch was started."""
    sid = os.path.basename(sid or "")
    if not sid:
        return False
    try:
        with open(os.path.join(REGISTRY, sid + ".json")) as f:
            s = json.load(f)
    except (OSError, ValueError):
        return False
    kind = s.get("kind")
    if kind == "webview":
        # No subprocess — just clone the registry entry with a fresh id.
        return bool(create_webview(s.get("url", ""), s.get("name")))
    if kind == "terminal":
        # If this terminal lives inside a the container launcher container, dup another
        # of the same kind; otherwise a plain host terminal in the same cwd.
        if s.get("container"):
            return bool(spawn_container_terminal(cwd=s.get("cwd") or None, name=s.get("name")))
        return bool(spawn_terminal(cwd=s.get("cwd") or None, name=s.get("name")))
    # Launcher-spawned tiles (host/codex/opencode/custom) carry their raw command
    # — re-run it through spawn_launcher so the dup keeps the same flags, env
    # overlay (custom endpoint) and provider as the original.
    if s.get("command"):
        preset = {"command": s["command"], "label": s.get("name")}
        if s.get("env"):
            preset["env"] = s["env"]
        if s.get("provider"):
            preset["provider"] = s["provider"]
        if s.get("icon"):
            preset["icon"] = s["icon"]
        return bool(spawn_launcher(preset, cwd=s.get("cwd") or None, name=s.get("name")))
    if kind == "opencode":
        return bool(spawn_opencode(cwd=s.get("cwd") or None, name=s.get("name")))
    if kind == "codex":
        return bool(spawn_codex(cwd=s.get("cwd") or None, name=s.get("name")))
    cwd = s.get("cwd")
    if not cwd or not os.path.isdir(cwd):
        return False
    if kind == "container":
        launcher = s.get("launcher")
        if not launcher:
            return False
        cmd = [launcher, "-web", "--detach"]
    else:
        # Legacy host tile with no recorded command: relaunch via the configured
        # external web launcher (loaded through an interactive zsh so a shell
        # function works); bail if none is configured. cwd comes from Popen.
        base = s.get("command") or HOST_LAUNCH_CMD
        if not base:
            return False
        cmd = ["zsh", "-ic", base + " --detach"]
    try:
        subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# New tile types: plain terminal (ttyd + zsh) and webview (URL holder).
# ---------------------------------------------------------------------------

def _alloc_port(start=7681):
    """Find a free TCP port, claim it via the same noclobber per-port lock the
    launchers use, and return (port, lockfile). Caller releases the lock once
    its ttyd is listening."""
    port = start
    while port < 65000:
        if port_alive(port):
            port += 1
            continue
        lock = os.path.join(REGISTRY, ".port-%d.lock" % port)
        try:
            mtime = os.path.getmtime(lock)
            if time.time() - mtime > 60:
                os.remove(lock)
        except OSError:
            pass
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return port, lock
        except OSError:
            port += 1
    raise RuntimeError("no free port")


def _release_when_listening(port, lock):
    """Drop the per-port lock once ttyd is actually listening, so a colliding
    launch can't grab the port. Background thread; never blocks the caller."""
    def wait():
        for _ in range(60):
            if port_alive(port):
                break
            time.sleep(0.1)
        try:
            os.remove(lock)
        except OSError:
            pass
    threading.Thread(target=wait, daemon=True).start()


# Common brew install prefixes, used as a fallback when our PATH is the minimal
# launchd one (no ~/.zshrc, no `eval $(brew shellenv)`) and `which ttyd` finds
# nothing.
# `claude` installs to ~/.local/bin, which is NOT on launchd's minimal PATH
# (/usr/bin:/bin:/usr/sbin:/sbin). Without it here, a dashboard started by
# launchd can't resolve `claude` and every + New ▸ Claude spawn fails.
_EXTRA_PATH = (os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin",
               "/usr/local/bin", "/usr/local/sbin")


def _which(name):
    """shutil.which, but also searches common Homebrew bin dirs even when our
    inherited PATH (launchd's minimal /usr/bin:/bin:/usr/sbin:/sbin) doesn't
    include them."""
    import shutil
    found = shutil.which(name)
    if found:
        return found
    for d in _EXTRA_PATH:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _channel_path(name):
    """Resolve a channel name to its NDJSON file path, or None if the name
    fails the safe-charset check (defuses traversal). Caller checks for None."""
    if not name or not CHANNEL_NAME_RE.match(name):
        return None
    return os.path.join(CHANNELS_DIR, name + ".ndjson")


def list_channels():
    """Enumerate `CHANNELS_DIR/*.ndjson`, returning per-channel metadata
    sorted newest-modified first. Used by the dashboard's Channels menu.
    Each entry: {name, modified (unix-ts float), count (line count, cheap
    proxy for message count)}. Returns [] if the dir doesn't exist.

    **Hides stale channels**: entries whose mtime is older than
    `CHANNEL_LIST_MAX_AGE_SEC` (default 3 h) are filtered out. Any
    `append_channel` write touches the mtime, so an active conversation
    stays visible forever; abandoned ones drop off automatically without
    deleting the underlying NDJSON. The /channel/<name> page still works
    for a stale channel — the listing filter is the only gate."""
    out = []
    if not os.path.isdir(CHANNELS_DIR):
        return out
    cutoff = time.time() - CHANNEL_LIST_MAX_AGE_SEC
    for fn in os.listdir(CHANNELS_DIR):
        if not fn.endswith(".ndjson"):
            continue
        nm = fn[:-7]
        if not CHANNEL_NAME_RE.match(nm):
            continue   # ignore files with unsafe names
        path = os.path.join(CHANNELS_DIR, fn)
        try:
            st = os.stat(path)
        except OSError:
            continue
        # Skip the line-count walk for stale entries — they're not going
        # into the response either way.
        if st.st_mtime < cutoff:
            continue
        count = 0
        if st.st_size > 0:
            try:
                with open(path, "rb") as f:
                    # Cheap line count without loading the whole file.
                    # Cap at 100k lines so the menu stays responsive even
                    # if someone leaves a channel running forever.
                    for line in f:
                        count += 1
                        if count >= 100000:
                            break
            except OSError:
                count = 0
        out.append({"name": nm, "modified": st.st_mtime, "count": count})
    out.sort(key=lambda c: c["modified"], reverse=True)
    return out


def read_channel(name, since=0):
    """Return messages from `since` (0-indexed line number, exclusive) to
    the end of the channel's NDJSON file. Each message is a parsed dict
    with at least {from, ts, text}. Lines that fail to parse as JSON are
    skipped silently (preserves forward compatibility with future fields).
    Returns ({"messages": [...], "total": N}, status_code)."""
    path = _channel_path(name)
    if path is None:
        return {"error": "invalid name"}, 400
    if not os.path.isfile(path):
        return {"messages": [], "total": 0}, 200
    try:
        since = max(0, int(since or 0))
    except (TypeError, ValueError):
        since = 0
    msgs = []
    total = 0
    try:
        with open(path) as f:
            for i, line in enumerate(f):
                total = i + 1
                if i < since:
                    continue
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    msgs.append(json.loads(line))
                except ValueError:
                    pass
    except OSError as e:
        return {"error": "read failed: %s" % e}, 500
    return {"messages": msgs, "total": total}, 200


def append_channel(name, sender, text):
    """Append one NDJSON message to the channel file. The skill's protocol
    uses {"from", "ts", "text"} so we mirror that exactly — tests and
    skill-driven readers both expect that shape. Creates the channel dir
    + file if missing. Atomic for typical message sizes (single write())."""
    path = _channel_path(name)
    if path is None:
        return False
    if not isinstance(sender, str) or not isinstance(text, str):
        return False
    if not sender.strip() or not text:
        return False
    try:
        os.makedirs(CHANNELS_DIR, exist_ok=True)
        rec = {"from": sender, "ts": int(time.time()), "text": text}
        # Force ensure_ascii=False so non-ASCII messages survive unmangled.
        # Newline-terminated so the file stays valid NDJSON.
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        return False
    return True


def spawn_channel_tile(name):
    """Register a channel tile in the session registry so it shows up
    alongside terminal / claude tiles. Returns the new session id, or
    None if the channel name is invalid. The tile's iframe loads
    `/channel/<name>` (served by this dashboard, same-origin), so no
    backend process / ttyd is involved — the tile is a thin chatroom
    UI talking to /api/channel/<name> for read+append."""
    if not CHANNEL_NAME_RE.match(name or ""):
        return None
    sid = "channel-%s-%d" % (name, int(time.time() * 1000))
    entry = {
        "id": sid,
        "name": "#" + name,            # tile title — `#` hints chatroom
        "channel": name,               # consumed by the dashboard's render path
        "kind": "channel",
        "cwd": "",                     # no working dir for a chatroom
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        os.makedirs(REGISTRY, exist_ok=True)
        with open(os.path.join(REGISTRY, sid + ".json"), "w") as f:
            json.dump(entry, f)
    except OSError:
        return None
    return sid


def _mcp_project_dir():
    """Nearest ancestor of this file that holds a .mcp.json, so a dashboard-spawned
    claude loads the same MCP servers (notably image-mcp's show_image). $HOME if none."""
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(d, ".mcp.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.expanduser("~")


def spawn_claude(cwd=None, name=None, provider=None, extra=None, command=None, env=None):
    """Spawn a ttyd serving a host `claude` session in `cwd` and register it as a
    kind=host tile in THIS dashboard's store. Self-manages ttyd + dtach + term.html
    + registry (mirrors spawn_opencode) rather than delegating to the the host launcher
    zsh function — so it honours --sessions-dir (the host launcher hardcodes
    ~/.claude-sessions) and survives reloads. cwd defaults to the nearest .mcp.json
    project so the spawned claude has the same MCP servers (show_image).

    `extra` is the list of user-configured flags after `claude` (from a launcher
    preset's command line, e.g. ["--dangerously-skip-permissions", "--model",
    "haiku"]). `command` is the raw launcher string, recorded so duplicate/revive
    can reproduce the tile. provider=="vertex" and/or `env` route this one session
    to a custom endpoint (Vertex / an Anthropic-compatible proxy) via the child's
    environment — a per-session choice, so other tiles are unaffected.

    A fresh --session-id is pinned (so the fork button can fork THIS conversation)
    unless the user's own flags already manage the session (--resume/--continue/
    --session-id). Returns the sid, or None on failure."""
    cwd = cwd or _mcp_project_dir()
    if not os.path.isdir(cwd):
        return None
    ttyd = _which("ttyd")
    claude = _which("claude")
    if not ttyd or not claude:
        return None
    dtach = _which("dtach")
    try:
        port, lock = _alloc_port()
    except RuntimeError:
        return None
    term_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "term.html")
    idx = ["-I", term_html] if os.path.isfile(term_html) else []
    sock = os.path.join(REGISTRY, "dtach-claude-%d.sock" % port) if dtach else None
    extra = list(extra or [])
    # Pin a known session id so the fork button can later fork THIS exact
    # conversation (claude names the .jsonl after this uuid). Without it, fork
    # falls back to newest-jsonl-in-cwd and may grab a sibling session. Skip the
    # pin when the launcher command already drives the session itself.
    import uuid as _uuid
    if _manages_session(extra):
        session_id = None
        claude_cmd = [claude, *extra]
    else:
        session_id = str(_uuid.uuid4())
        claude_cmd = [claude, *extra, "--session-id", session_id]
    if dtach:
        cmd = [ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
               *idx, "-i", "127.0.0.1", "-p", str(port),
               dtach, "-A", sock, "-r", "winch", *claude_cmd]
    else:
        cmd = [ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
               *idx, "-i", "127.0.0.1", "-p", str(port), *claude_cmd]
    # provider/env: copy the parent env and overlay the routing so only this child
    # is affected. None (the default) means inherit, which is what a plain tile wants.
    child_env = _launcher_child_env(provider, env)
    log = "/tmp/ttyd-claude-%d.log" % port
    try:
        with open(log, "ab") as lf:
            subprocess.Popen(cmd, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=lf, start_new_session=True)
    except OSError:
        try:
            os.remove(lock)
        except OSError:
            pass
        return None
    _release_when_listening(port, lock)
    sid = "host-%d" % port
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {"name": name or os.path.basename(cwd) or "claude",
             "port": port, "kind": "host", "cwd": cwd, "started": started}
    if session_id:
        entry["session_id"] = session_id
    if provider:
        entry["provider"] = provider
    if env:
        entry["env"] = dict(env)
    if command:
        entry["command"] = command
    if sock:
        entry["sock"] = sock
    try:
        with open(os.path.join(REGISTRY, sid + ".json"), "w") as f:
            json.dump(entry, f)
    except OSError:
        return None
    return sid


def spawn_terminal(cwd=None, name=None):
    """Spawn a ttyd serving an interactive zsh and register it as a terminal
    tile. Returns the registry sid on success, None on failure."""
    cwd = cwd or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        return None
    ttyd = _which("ttyd")
    if not ttyd:
        return None
    try:
        port, lock = _alloc_port()
    except RuntimeError:
        return None
    term_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "term.html")
    idx = ["-I", term_html] if os.path.isfile(term_html) else []
    cmd = [ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
           *idx, "-i", "127.0.0.1", "-p", str(port),
           os.environ.get("SHELL", "/bin/zsh"), "-i"]
    log = "/tmp/ttyd-terminal-%d.log" % port
    try:
        with open(log, "ab") as lf:
            subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=lf, start_new_session=True)
    except OSError:
        try:
            os.remove(lock)
        except OSError:
            pass
        return None
    _release_when_listening(port, lock)
    sid = "terminal-%d" % port
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {"name": name or os.path.basename(cwd) or "terminal",
             "port": port, "kind": "terminal", "cwd": cwd, "started": started}
    try:
        with open(os.path.join(REGISTRY, sid + ".json"), "w") as f:
            json.dump(entry, f)
    except OSError:
        return None
    return sid


def spawn_opencode(cwd=None, name=None, extra=None, command=None, env=None):
    """Spawn a ttyd serving `opencode` (interactive TUI) in `cwd` and register
    it as an opencode tile. Mirrors spawn_terminal — same ttyd flags, same
    custom client, same registry shape — but kind=opencode so the frontend
    badges it differently and so close_session/duplicate route correctly.

    The opencode process is wrapped in `dtach -A <sock>` so it SURVIVES the
    ttyd client tear-down that happens on every browser reload. Without
    dtach, ttyd would SIGHUP the per-connection opencode child on disconnect
    and the next reload would land in a freshly-spawned opencode with no
    chat history — user-reported as "opencode sessions do not restore on
    reload". The opencode-unsafe -web zsh launcher already uses this same
    pattern; this brings the dashboard-spawned path in line.

    `extra`/`command`/`env` carry the launcher preset's flags, raw command (for
    duplicate/revive) and optional custom-endpoint env overlay.

    Returns the registry sid on success, None on failure (cwd missing, ttyd
    or opencode not installed, port exhausted). Falls back to a direct exec
    (no dtach, no reload-survival) only if dtach is missing on $PATH."""
    cwd = cwd or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        return None
    ttyd = _which("ttyd")
    if not ttyd:
        return None
    opencode = _which("opencode")
    if not opencode:
        return None
    dtach = _which("dtach")
    try:
        port, lock = _alloc_port()
    except RuntimeError:
        return None
    term_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "term.html")
    idx = ["-I", term_html] if os.path.isfile(term_html) else []
    # One dtach socket per port — independent opencode per session.
    # `-r winch` makes dtach relay window-resize so opencode's TUI tracks
    # the actual browser viewport instead of clinging to the size at first
    # attach. Path lives in the registry dir alongside the json (so it's
    # easy to inspect / clean up; close_session looks at s["sock"]).
    sock = os.path.join(REGISTRY, "dtach-opencode-%d.sock" % port) if dtach else None
    # opencode's default subcommand starts the TUI; pass the cwd explicitly via
    # the [project] positional so it doesn't matter what ttyd's cwd ends up as.
    opencode_cmd = [opencode, *(extra or []), cwd]
    if dtach:
        cmd = [ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
               *idx, "-i", "127.0.0.1", "-p", str(port),
               dtach, "-A", sock, "-r", "winch", *opencode_cmd]
    else:
        cmd = [ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
               *idx, "-i", "127.0.0.1", "-p", str(port), *opencode_cmd]
    child_env = _launcher_child_env(None, env)
    log = "/tmp/ttyd-opencode-%d.log" % port
    try:
        with open(log, "ab") as lf:
            subprocess.Popen(cmd, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=lf, start_new_session=True)
    except OSError:
        try:
            os.remove(lock)
        except OSError:
            pass
        return None
    _release_when_listening(port, lock)
    sid = "opencode-%d" % port
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {"name": name or os.path.basename(cwd) or "opencode",
             "port": port, "kind": "opencode", "cwd": cwd, "started": started}
    if env:
        entry["env"] = dict(env)
    if command:
        entry["command"] = command
    if sock:
        # close_session uses this to lsof-then-kill the dtach master so the
        # ✕ button actually ends the underlying opencode process (otherwise
        # killing ttyd would leave the dtach server + opencode running and
        # the next +New opencode in the same cwd would attach to it).
        entry["sock"] = sock
    try:
        with open(os.path.join(REGISTRY, sid + ".json"), "w") as f:
            json.dump(entry, f)
    except OSError:
        return None
    return sid


def spawn_codex(cwd=None, name=None, extra=None, command=None, env=None):
    """Spawn a ttyd serving the `codex` TUI (OpenAI Codex / ChatGPT) in `cwd`
    and register it as a kind=codex tile. Mirrors spawn_opencode — same ttyd
    flags, same dtach reload-survival wrapper, same registry shape — but
    kind=codex so the frontend badges/icons it as a ChatGPT agent and so
    close/duplicate route correctly.

    codex runs with the launcher preset's `extra` flags (e.g.
    --dangerously-bypass-approvals-and-sandbox) plus `-C <cwd>` to pin its
    working root (added only if the command didn't set its own -C/--cd).
    `command` is the raw launcher string; `env` overlays a custom endpoint
    (e.g. OPENAI_BASE_URL/OPENAI_API_KEY) onto the child.

    Returns the registry sid on success, None on failure (cwd missing, ttyd or
    codex not installed, port exhausted). Falls back to a direct exec (no
    dtach, no reload-survival) only if dtach is missing on $PATH."""
    cwd = cwd or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        return None
    ttyd = _which("ttyd")
    if not ttyd:
        return None
    codex = _which("codex")
    if not codex:
        return None
    dtach = _which("dtach")
    try:
        port, lock = _alloc_port()
    except RuntimeError:
        return None
    term_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "term.html")
    idx = ["-I", term_html] if os.path.isfile(term_html) else []
    sock = os.path.join(REGISTRY, "dtach-codex-%d.sock" % port) if dtach else None
    extra = list(extra or [])
    codex_cmd = [codex, *extra]
    if "-C" not in extra and "--cd" not in extra:
        codex_cmd += ["-C", cwd]
    if dtach:
        cmd = [ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
               *idx, "-i", "127.0.0.1", "-p", str(port),
               dtach, "-A", sock, "-r", "winch", *codex_cmd]
    else:
        cmd = [ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
               *idx, "-i", "127.0.0.1", "-p", str(port), *codex_cmd]
    child_env = _launcher_child_env(None, env)
    log = "/tmp/ttyd-codex-%d.log" % port
    try:
        with open(log, "ab") as lf:
            subprocess.Popen(cmd, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=lf, start_new_session=True)
    except OSError:
        try:
            os.remove(lock)
        except OSError:
            pass
        return None
    _release_when_listening(port, lock)
    sid = "codex-%d" % port
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {"name": name or os.path.basename(cwd) or "codex",
             "port": port, "kind": "codex", "cwd": cwd, "started": started}
    if env:
        entry["env"] = dict(env)
    if command:
        entry["command"] = command
    if sock:
        entry["sock"] = sock
    try:
        with open(os.path.join(REGISTRY, sid + ".json"), "w") as f:
            json.dump(entry, f)
    except OSError:
        return None
    return sid


def spawn_command(argv, cwd=None, name=None, command=None, env=None, icon=None):
    """Spawn a ttyd running an arbitrary argv (a launcher whose program isn't
    claude/codex/opencode) and register it as a kind=custom tile. Same ttyd +
    dtach reload-survival wrapper as the agent spawns, but with no resume/fork
    semantics — a generic command tile, revived by re-running the command.

    `command` is the raw launcher string (stored for duplicate/revive), `env`
    an optional custom-endpoint overlay, `icon` an optional Lucide glyph name.
    Returns the sid, or None on failure (empty argv, missing program/ttyd,
    bad cwd, port exhausted)."""
    if not argv:
        return None
    cwd = cwd or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        return None
    ttyd = _which("ttyd")
    if not ttyd:
        return None
    prog = _which(argv[0]) or argv[0]
    if not (os.path.isabs(prog) and os.path.exists(prog)):
        return None  # unknown program — don't spawn a tile that instantly dies
    dtach = _which("dtach")
    try:
        port, lock = _alloc_port()
    except RuntimeError:
        return None
    term_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "term.html")
    idx = ["-I", term_html] if os.path.isfile(term_html) else []
    sock = os.path.join(REGISTRY, "dtach-custom-%d.sock" % port) if dtach else None
    run = [prog, *argv[1:]]
    if dtach:
        cmd = [ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
               *idx, "-i", "127.0.0.1", "-p", str(port),
               dtach, "-A", sock, "-r", "winch", *run]
    else:
        cmd = [ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
               *idx, "-i", "127.0.0.1", "-p", str(port), *run]
    child_env = _launcher_child_env(None, env)
    log = "/tmp/ttyd-custom-%d.log" % port
    try:
        with open(log, "ab") as lf:
            subprocess.Popen(cmd, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=lf, start_new_session=True)
    except OSError:
        try:
            os.remove(lock)
        except OSError:
            pass
        return None
    _release_when_listening(port, lock)
    sid = "custom-%d" % port
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {"name": name or os.path.basename(argv[0]) or "custom",
             "port": port, "kind": "custom", "cwd": cwd, "started": started}
    if command:
        entry["command"] = command
    if env:
        entry["env"] = dict(env)
    if icon:
        entry["icon"] = icon
    if sock:
        entry["sock"] = sock
    try:
        with open(os.path.join(REGISTRY, sid + ".json"), "w") as f:
            json.dump(entry, f)
    except OSError:
        return None
    return sid


def spawn_launcher(preset, cwd=None, name=None):
    """Spawn a configured launcher preset. `preset` is a dict {command, label,
    provider?, env?, icon?} (or a raw command string for convenience). The
    command's program name routes to the smart spawn path so claude/codex/
    opencode tiles keep their resume/fork/chat + badges; any other program runs
    as a generic kind=custom tile. Returns the sid, or None on failure."""
    if isinstance(preset, str):
        preset = {"command": preset}
    command = (preset.get("command") or "").strip()
    if not command:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    name = name or preset.get("label")
    env = preset.get("env") if isinstance(preset.get("env"), dict) else None
    provider = preset.get("provider") if preset.get("provider") == "vertex" else None
    prog = os.path.basename(argv[0])
    extra = argv[1:]
    if prog == "claude":
        return spawn_claude(cwd=cwd, name=name, provider=provider,
                            extra=extra, command=command, env=env)
    if prog == "codex":
        return spawn_codex(cwd=cwd, name=name, extra=extra, command=command, env=env)
    if prog == "opencode":
        return spawn_opencode(cwd=cwd, name=name, extra=extra, command=command, env=env)
    return spawn_command(argv, cwd=cwd, name=name, command=command, env=env,
                         icon=preset.get("icon"))


def spawn_container_terminal(cwd=None, name=None):
    """Spawn a ttyd that drops into zsh INSIDE the the container launcher podman container
    for `cwd`. Mirrors the the container launcher -web pattern: ttyd wraps `podman exec
    -it <ctr> dtach -A <csock> -r winch /bin/zsh -i` so the in-container shell
    is reachable via the dashboard tile.

    Requires an existing (running) the container launcher container for this cwd — we
    don't replicate the full `devcontainer up` bring-up here (it lives in
    the container launcher and does Keychain import, hash-based auto-rebuild, etc.).
    User can launch `the container launcher -web` first, then add this terminal alongside
    their claude session in the same container.

    Returns sid on success, None otherwise."""
    cwd = (cwd or "").rstrip("/")
    if not cwd or not os.path.isdir(cwd):
        return None
    # Under launchd our PATH is /usr/bin:/bin:/usr/sbin:/sbin (no Homebrew),
    # so bare `podman` would silently fail. Resolve up front via _which.
    podman = _which("podman")
    if not podman:
        return None
    # Pick the NEWEST running container for this cwd. Two the container launcher launches
    # from different paths that resolve to the same workspace (e.g. via
    # symlink, or rerunning after the .devcontainer changed without removing
    # the old one) leave both labelled with the same local_folder. The old one
    # may predate Dockerfile changes (e.g. before `dtach` was added) — picking
    # it would land us in a shell-less container and confuse the user. Newest
    # is what `the container launcher -web` will have just (re)started.
    try:
        out = subprocess.run(
            [podman, "ps", "--format", "{{.CreatedAt}}\t{{.ID}}",
             "--filter", "label=devcontainer.local_folder=" + cwd,
             "--filter", "status=running"],
            capture_output=True, text=True, timeout=4).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    # CreatedAt sorts lexicographically (RFC3339-ish), so descending = newest first.
    rows = sorted((ln for ln in out.splitlines() if "\t" in ln), reverse=True)
    if not rows:
        return None
    container = rows[0].split("\t", 1)[1].strip()
    if not container:
        return None
    # Best-effort sanity check: the in-container shell wrapper needs `dtach`.
    # If the image is stale (pre-dtach Dockerfile), bail early with a clear
    # signal rather than spawning a ttyd that will fail on every connect with
    # the OCI "executable file `dtach` not found" error.
    try:
        probe = subprocess.run(
            [podman, "exec", container, "sh", "-c", "command -v dtach"],
            capture_output=True, text=True, timeout=4)
        if probe.returncode != 0 or not probe.stdout.strip():
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    ttyd = _which("ttyd")
    if not ttyd:
        return None
    try:
        port, lock = _alloc_port()
    except RuntimeError:
        return None
    term_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "term.html")
    idx = ["-I", term_html] if os.path.isfile(term_html) else []
    # csock lives on a container-local path (not the bind mount — unix sockets
    # on virtiofs are unreliable). Per-port so close_session can target THIS
    # tile's zsh without taking down a colliding session's claude.
    csock = "/tmp/dtach-cshell-%d.sock" % port
    # Pass the full podman path: ttyd inherits our (minimal) PATH and would
    # otherwise hit the same "command not found" wall as we did above.
    cmd = [ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
           *idx, "-i", "127.0.0.1", "-p", str(port),
           podman, "exec", "-it", container,
           "dtach", "-A", csock, "-r", "winch", "/bin/zsh", "-i"]
    log = "/tmp/ttyd-cshell-%d.log" % port
    try:
        with open(log, "ab") as lf:
            subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=lf, start_new_session=True)
    except OSError:
        try:
            os.remove(lock)
        except OSError:
            pass
        return None
    _release_when_listening(port, lock)
    sid = "terminal-%d" % port
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # kind="terminal" + container/csock fields. close_session keys on the
    # container field (regardless of kind) for the podman-exec kill; the
    # frontend uses the container field to pick the distinct badge.
    entry = {"name": name or ((os.path.basename(cwd) or "container") + " (sh)"),
             "port": port, "kind": "terminal", "container": container,
             "csock": csock, "cwd": cwd, "started": started}
    try:
        with open(os.path.join(REGISTRY, sid + ".json"), "w") as f:
            json.dump(entry, f)
    except OSError:
        return None
    return sid


# ---------------------------------------------------------------------------
# Reboot resurrection: respawn dead tiles from the surviving registry.
# ---------------------------------------------------------------------------

def _reclaim_port(want):
    """Claim a tile's previous port if it's still free (the normal case right
    after a reboot — nothing else has bound it yet) so the tile keeps its
    /t/<port>/ URL and dtach socket path. Uses the same noclobber per-port
    lock as _alloc_port; if the port is taken or locked, falls back to
    scanning for a fresh one. Returns (port, lock); raises RuntimeError only
    when _alloc_port exhausts the range."""
    try:
        want = int(want)
    except (TypeError, ValueError):
        want = 0
    if want > 0 and not port_alive(want):
        lock = os.path.join(REGISTRY, ".port-%d.lock" % want)
        try:
            if time.time() - os.path.getmtime(lock) > 60:
                os.remove(lock)
        except OSError:
            pass
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return want, lock
        except OSError:
            pass
    return _alloc_port()


def _revive_entry(sid, s):
    """Respawn the backing process for one dead registry entry, keeping its
    registry identity — sid, name, cwd, started, stashed — so the dashboard
    shows the same tile in the same order and term-client's localStorage
    scrollback snapshot (keyed on sid|started) still applies. What "respawn"
    means depends on the tile kind:

      host       claude --resume <session_id>: the conversation .jsonl under
                 ~/.claude/projects survived the reboot, so the tile comes
                 back mid-conversation. A session that never wrote its
                 transcript can't be resumed — restart it fresh under the
                 same pinned --session-id instead. Legacy entries without a
                 recorded session_id resume the cwd's newest transcript (the
                 same fallback fork_session uses) and record its id.
      opencode   opencode <cwd> --continue: opencode mints its own session
                 ids that we never learn, so "this project's most recent
                 session" is the best anchor available. Several opencode
                 tiles sharing a cwd all come back on that same newest
                 session — a known limit, better than coming back empty.
      terminal   a fresh interactive shell in the recorded cwd (a shell has
                 no resumable state). In-container shells are NOT revived:
                 their container is stopped after a reboot and bring-up
                 belongs to the container launcher (see kind=container).
      container  relaunched detached via the recorded the container launcher launcher
                 with --resume <session_id>; the launcher owns the container
                 bring-up (podman machine start, devcontainer up, ...) and
                 self-registers a NEW tile, so the stale entry is removed
                 here rather than left to confuse the dashboard.

    A stale dtach socket file from before the reboot is harmless: dtach -A
    detects the dead master (ECONNREFUSED), removes the socket, and creates
    a fresh session. Returns True if a process was (re)launched."""
    kind = s.get("kind", "host")
    cwd = s.get("cwd") or ""
    # session_id is our own write, but it lands in an argv below — keep it a
    # bare basename as defence in depth (same treatment as _session_jsonl).
    session_id = os.path.basename(str(s.get("session_id") or "")) or None
    if not cwd or not os.path.isdir(cwd):
        return False
    if kind == "container":
        launcher = s.get("launcher")
        if not launcher or not os.path.isfile(launcher):
            return False
        cmd = [launcher, "-web", "--detach"]
        if session_id:
            cmd += ["--resume", session_id]
        try:
            subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            return False
        try:
            os.remove(os.path.join(REGISTRY, sid + ".json"))
        except OSError:
            pass
        return True
    if kind == "terminal" and s.get("container"):
        return False
    if kind not in ("host", "opencode", "codex", "custom", "terminal"):
        return False
    ttyd = _which("ttyd")
    if not ttyd:
        return False
    dtach = _which("dtach")
    # Re-apply the tile's launcher flags (skip-perms, model, bypass, ...) and
    # custom-endpoint env across the restart, from what spawn_* recorded.
    extra = _launcher_extra(s)
    child_env = _launcher_child_env(s.get("provider"), s.get("env"))
    if kind == "host":
        claude = _which("claude")
        if not claude:
            return False
        if not session_id:
            src = _newest_jsonl(cwd, False)
            if src:
                session_id = os.path.basename(src)[: -len(".jsonl")]
        # A launcher-spawned tile replays its exact flags; a legacy tile (no
        # recorded command — e.g. spawned by `the host launcher -web`, which always
        # uses --dangerously-skip-permissions) defaults to skip-perms so it comes
        # back in the same mode it had before the reboot.
        if s.get("command"):
            flags = _strip_session_flags(extra)  # WE manage --resume/--session-id below
        else:
            flags = ["--dangerously-skip-permissions"]
        if session_id and _session_jsonl(cwd, False, session_id):
            tail = [claude, *flags, "--resume", session_id]
        else:
            import uuid as _uuid
            session_id = session_id or str(_uuid.uuid4())
            tail = [claude, *flags, "--session-id", session_id]
        s["session_id"] = session_id
        proc_tag = "claude"
    elif kind == "opencode":
        opencode = _which("opencode")
        if not opencode:
            return False
        tail = [opencode, *extra, cwd, "--continue"]
        proc_tag = "opencode"
    elif kind == "codex":
        codex = _which("codex")
        if not codex:
            return False
        # codex mints its own session ids that we never learn, so "resume the
        # most recent session" (resume --last) is the best anchor available —
        # the same limitation opencode's --continue has. The session transcript
        # survived the reboot; only the dtach master died.
        cflags = list(extra)
        if "-C" not in cflags and "--cd" not in cflags:
            cflags += ["-C", cwd]
        tail = [codex, *cflags, "resume", "--last"]
        proc_tag = "codex"
    elif kind == "custom":
        # Generic launcher tile: no resume semantics — just re-run the command.
        try:
            argv = shlex.split(s.get("command") or "")
        except ValueError:
            argv = []
        if not argv:
            return False
        prog = _which(argv[0]) or argv[0]
        tail = [prog, *argv[1:]]
        proc_tag = "custom"
    else:
        tail = [os.environ.get("SHELL", "/bin/zsh"), "-i"]
        proc_tag = "terminal"
    try:
        port, lock = _reclaim_port(s.get("port"))
    except RuntimeError:
        return False
    term_html = os.path.join(os.path.dirname(os.path.abspath(__file__)), "term.html")
    idx = ["-I", term_html] if os.path.isfile(term_html) else []
    base = [ttyd, "-W", "-t", "scrollback=5000", "-t", "disableLeaveAlert=true",
            *idx, "-i", "127.0.0.1", "-p", str(port)]
    sock = None
    # Terminals run bare (matches spawn_terminal); host/opencode keep their
    # dtach wrapper so the revived session again survives browser reloads.
    if kind != "terminal" and dtach:
        sock = os.path.join(REGISTRY, "dtach-%s-%d.sock" % (proc_tag, port))
        cmd = base + [dtach, "-A", sock, "-r", "winch"] + tail
    else:
        cmd = base + tail
    log = "/tmp/ttyd-%s-%d.log" % (proc_tag, port)
    try:
        with open(log, "ab") as lf:
            subprocess.Popen(cmd, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=lf, start_new_session=True)
    except OSError:
        try:
            os.remove(lock)
        except OSError:
            pass
        return False
    _release_when_listening(port, lock)
    s["port"] = port
    if sock:
        s["sock"] = sock
    else:
        s.pop("sock", None)
    try:
        with open(os.path.join(REGISTRY, sid + ".json"), "w") as f:
            json.dump(s, f)
    except OSError:
        return False
    return True


def resurrect_sessions():
    """Bring tiles back after their backing processes died with the host (the
    OOM reboots): for every registry entry whose ttyd port is no longer
    listening, respawn its process via _revive_entry. Entries whose port is
    alive (a dashboard-only restart — launchd KeepAlive, manual rerun) are
    left untouched, so this is idempotent and safe to run on every startup.

    Must run BEFORE the HTTP server answers requests: the surviving registry
    files are long past PRUNE_GRACE, so the first read_sessions() would prune
    them all. Reviving rewrites each entry, refreshing its mtime and thereby
    restarting the grace window while the new ttyd boots. Entries that can't
    be revived (e.g. in-container shells whose container is gone) keep their
    old mtime and prune exactly as before. Returns the number of tiles
    revived; one broken entry never blocks the dashboard from starting."""
    revived = 0
    if not os.path.isdir(REGISTRY):
        return 0
    for fn in sorted(os.listdir(REGISTRY)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(REGISTRY, fn)
        try:
            with open(path) as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(s, dict):
            continue
        kind = s.get("kind", "host")
        if kind in ("webview", "channel", "note"):
            continue  # no backing process — these survive reboots already
        try:
            port = int(s["port"])
        except (KeyError, TypeError, ValueError):
            continue
        if port_alive(port):
            continue
        sid = fn[:-5]
        try:
            if _revive_entry(sid, s):
                revived += 1
                print("resurrected %s (%s) in %s" % (sid, kind, s.get("cwd", "?")))
        except Exception as e:  # noqa: BLE001 — one bad entry must not block boot
            print("resurrect %s failed: %s" % (sid, e))
    return revived


def _normalize_url(url):
    """Add a scheme if missing. Default to http:// for local/LAN hosts (bare
    IPs, `localhost`, single-label names, *.local) because services there are
    almost always plaintext — and we want ws:// to work inside the iframe, which
    would otherwise be blocked as mixed content under an https:// page. Public
    hostnames still default to https://."""
    url = (url or "").strip()
    if not url:
        return ""
    # data: URLs (inline image/html for agent-spawned webviews) and explicit
    # schemes pass through untouched — only scheme-less hostnames get one added.
    if url.startswith("data:"):
        return url
    if "://" in url or url.startswith("//"):
        return url
    host = url.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    bare_host = host.split(":", 1)[0]
    is_ipv4 = bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", bare_host))
    is_local = (bare_host == "localhost" or bare_host.endswith(".local")
                or bare_host.endswith(".localhost") or "." not in bare_host)
    scheme = "http" if (is_ipv4 or is_local) else "https"
    return scheme + "://" + url


# Cap proxied response size: enough for any sensible config-tool HTML, far short
# of what could OOM a localhost server.
PROXY_MAX_BYTES = 5 * 1024 * 1024
PROXY_TIMEOUT = 8.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Raise on any 3xx so a registered URL can't silently pivot via Location:
    headers (an SSRF amplifier — esp. when the registered URL goes through an
    attacker-controlled redirector)."""
    def http_error_301(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, "redirect blocked", headers, fp)
    http_error_302 = http_error_303 = http_error_307 = http_error_308 = http_error_301


_no_redirect_opener = urllib.request.build_opener(_NoRedirect())


def proxy_fetch(url):
    """Fetch a URL and rewrap it so an iframe over http://127.0.0.1:7680 can
    host an https:// page that needs to open ws:// to a LAN service (default
    browser mixed-content rules block ws:// from https:// pages).

    Returns (status, body, content_type) or raises on failure. For HTML we inject
    `<base href="<url>">` so the page's relative URLs still resolve against
    upstream; everything else is passed through verbatim. Redirects are blocked
    (raise) so a registered URL can't pivot via Location.

    Caller must serve the body with `Content-Security-Policy: sandbox …` (no
    `allow-same-origin`) to prevent the proxied page from reading the
    dashboard's own endpoints (CSRF token, /api/*). We DON'T block private-IP
    targets — reaching LAN services is the entire point of this proxy."""
    if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("bad url")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (claude-sessions-proxy)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    with _no_redirect_opener.open(req, timeout=PROXY_TIMEOUT) as resp:
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        body = resp.read(PROXY_MAX_BYTES + 1)
        if len(body) > PROXY_MAX_BYTES:
            raise ValueError("response too large")
        final_url = resp.geturl() or url
    if "html" in ctype.lower():
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover
            text = body.decode("latin-1", errors="replace")
        tag = '<base href="%s">' % final_url.replace('"', "%22")
        m = re.search(r"<head[^>]*>", text, flags=re.IGNORECASE)
        if m:
            text = text[:m.end()] + tag + text[m.end():]
        else:
            text = tag + text
        body = text.encode("utf-8")
        ctype = "text/html; charset=utf-8"
    return 200, body, ctype


def _lookup_webview_url(sid):
    """Resolve a webview sid → its registered URL, or None if the entry is
    missing / wrong kind / malformed. Used by /proxy to constrain fetches to
    URLs the user explicitly registered — closes the SSRF surface that an
    `&url=` query param would open against the user's LAN."""
    sid = os.path.basename(sid or "")
    if not sid:
        return None
    path = os.path.join(REGISTRY, sid + ".json")
    try:
        with open(path) as f:
            entry = json.load(f)
    except (OSError, ValueError):
        return None
    if entry.get("kind") != "webview":
        return None
    url = entry.get("url")
    if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
        return None
    return url


def create_webview(url, name=None, cwd=None, proxy=False):
    """Register a webview tile (no backing process). Returns the sid.

    `cwd` is purely a tab-grouping hint: the dashboard groups tiles into tabs by
    workdir, so passing the active tab's cwd here makes the new webview land in
    the same tab the user was viewing when they created it. It's never used as
    a filesystem path."""
    url = _normalize_url(url)
    if not url:
        return None
    # Unique sid via a monotonically-increasing index; survives dashboard
    # restarts because we scan existing files first.
    n = 0
    try:
        for fn in os.listdir(REGISTRY):
            if fn.startswith("webview-") and fn.endswith(".json"):
                try:
                    n = max(n, int(fn[len("webview-"):-len(".json")]))
                except ValueError:
                    pass
    except OSError:
        pass
    sid = "webview-%d" % (n + 1)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {"name": name or url, "kind": "webview", "url": url,
             "cwd": cwd or "", "proxy": bool(proxy), "started": started}
    try:
        with open(os.path.join(REGISTRY, sid + ".json"), "w") as f:
            json.dump(entry, f)
    except OSError:
        return None
    return sid


def update_webview(sid, url=None, name=None, proxy=None):
    """Update a webview tile's stored URL, name, and/or proxy flag in place."""
    sid = os.path.basename(sid or "")
    if not sid:
        return False
    path = os.path.join(REGISTRY, sid + ".json")
    try:
        with open(path) as f:
            entry = json.load(f)
    except (OSError, ValueError):
        return False
    if entry.get("kind") != "webview":
        return False
    if url is not None:
        normalized = _normalize_url(url)
        if not normalized:
            return False
        entry["url"] = normalized
    if name is not None:
        entry["name"] = name.strip() or entry.get("name") or entry.get("url", "")
    if proxy is not None:
        entry["proxy"] = bool(proxy)
    try:
        with open(path, "w") as f:
            json.dump(entry, f)
    except OSError:
        return False
    return True


# --- note tiles: backend-less text/image scratchpads -----------------------
# A note is a registry entry (note-<n>.json) plus a sidecar body file
# (note-<n>.body) holding the contenteditable HTML the user typed/pasted. The
# dashboard serves the editor at /note/<id> (same origin) and reads/writes the
# body via GET/POST /api/note/<id>. No process, no ttyd — like webview/channel.
NOTE_SID_RE = re.compile(r"^note-\d+$")
# Body cap. Pasted images are inlined as base64 data URLs, which are bulky, so
# this is generous — but bounded so a runaway paste can't fill the disk.
NOTE_MAX_BYTES = 8 * 1024 * 1024


def _note_body_path(sid):
    """Sidecar path holding a note's HTML body. `sid` is basename-guarded."""
    return os.path.join(REGISTRY, os.path.basename(sid or "") + ".body")


def read_note_body(sid):
    """Return a note's saved HTML body, or '' if none/unreadable."""
    if not NOTE_SID_RE.match(os.path.basename(sid or "")):
        return ""
    try:
        with open(_note_body_path(sid), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def write_note_body(sid, body):
    """Persist a note's HTML body. Returns False on bad sid / oversize / IO
    error. Written atomically (tmp + rename) so a crash mid-write can't leave a
    truncated note."""
    sid = os.path.basename(sid or "")
    if not NOTE_SID_RE.match(sid):
        return False
    if not os.path.exists(os.path.join(REGISTRY, sid + ".json")):
        return False   # don't resurrect a closed note's body
    body = body or ""
    if len(body.encode("utf-8")) > NOTE_MAX_BYTES:
        return False
    dst = _note_body_path(sid)
    # Safety net against the empty-overwrite race: if we're about to replace a
    # non-empty body with an empty one, stash the current content in a .bak
    # sidecar first so it stays recoverable. A deliberate "clear the note" still
    # works — the empty body is written — it just leaves one undo copy behind.
    if not body.strip():
        try:
            if os.path.getsize(dst) > 0:
                with open(dst, encoding="utf-8") as f:
                    prev = f.read()
                with open(dst + ".bak", "w", encoding="utf-8") as f:
                    f.write(prev)
        except OSError:
            pass
    tmp = dst + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, dst)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True


def spawn_note(name=None, cwd=None):
    """Register a note tile (no backing process). Returns the sid. `cwd` is only
    a tab-grouping hint (where the user was when they created it), never a real
    path. Mirrors create_webview's monotonic-index id scheme."""
    n = 0
    try:
        for fn in os.listdir(REGISTRY):
            if fn.startswith("note-") and fn.endswith(".json"):
                try:
                    n = max(n, int(fn[len("note-"):-len(".json")]))
                except ValueError:
                    pass
    except OSError:
        pass
    sid = "note-%d" % (n + 1)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {"name": (name or "").strip() or ("Note %d" % (n + 1)),
             "kind": "note", "cwd": cwd or "", "started": started}
    try:
        with open(os.path.join(REGISTRY, sid + ".json"), "w") as f:
            json.dump(entry, f)
    except OSError:
        return None
    return sid


# Catalog of font faces inlined into the dashboard page AND term.html. Each
# (family, weight, file) tuple becomes one @font-face rule. Same six files are
# inlined by build-term.sh into term.html so the picker can swap fonts in any
# tile without a network round-trip. The catalog is the contract — adding a new
# font means: drop woff2 in fonts/, add it here, add it to build-term.sh's
# FACES, and (if exposing it in the picker) add it to FONTS below.
_FONT_FACES = (
    ("JetBrains Mono",    400, "jbm-400.woff2"),
    ("JetBrains Mono",    700, "jbm-700.woff2"),
    ("Terminus",          400, "terminus-400.woff2"),
    ("Terminus",          700, "terminus-700.woff2"),
    ("Cozette",           400, "cozette-400.woff2"),
    ("Cozette",           700, "cozette-700.woff2"),
    ("Fira Code",         400, "fira-code-400.woff2"),
    ("Fira Code",         700, "fira-code-700.woff2"),
    ("Bitstream Charter", 400, "charter-400.woff2"),
    ("Bitstream Charter", 700, "charter-700.woff2"),
    ("Source Serif 4",    400, "source-serif-4-400.woff2"),
    ("Source Serif 4",    700, "source-serif-4-700.woff2"),
    # NOTE: Georgia is intentionally NOT here — it's a proprietary system font
    # (can't embed), so its picker entry below falls through to the OS copy.
)

# Picker entries shown in the dashboard header. `id` is the localStorage key +
# postMessage payload identifier (stable across renames of `label`). `family`
# must match a `font-family` from _FONT_FACES (or fall through to the system
# stack if the woff2 is missing). `size` is xterm's fontSize (CSS px); `weight`
# is its fontWeight ('normal'|'bold' — xterm accepts strings or numbers).
#
# PSFs aren't web-renderable, so each PSF-named entry maps to the closest
# maintained TTF: Terminus TTF for the Terminus entries, Cozette for
# solar24x32.psfu (no maintained web mirror exists). The PSF name `ter-u32n`
# IS Terminus 32 regular — same glyphs, same metrics — so we don't list it
# separately; "Terminus 32" covers both names.
# NOTE on proportional faces: xterm.js lays every cell on a FIXED advance grid,
# so proportional fonts (Bitstream Charter, Source Serif 4, Georgia) don't align
# columns — they're offered for reading claude's prose, not for column-aligned
# TUIs. Fira Code is monospace (with ligatures) and aligns fine.
FONTS = (
    {"id": "jbm",            "label": "JetBrains Mono",   "family": "JetBrains Mono",   "size": 13, "weight": "normal"},
    {"id": "fira-code",      "label": "Fira Code",        "family": "Fira Code",        "size": 13, "weight": "normal"},
    {"id": "charter",        "label": "Bitstream Charter","family": "Bitstream Charter","size": 15, "weight": "normal"},
    {"id": "source-serif-4", "label": "Source Serif 4",   "family": "Source Serif 4",   "size": 15, "weight": "normal"},
    {"id": "georgia",        "label": "Georgia",          "family": "Georgia",          "size": 15, "weight": "normal"},
    {"id": "solar24x32",  "label": "solar24x32.psfu", "family": "Cozette",        "size": 13, "weight": "normal"},
    # "solar48x64" — bigger Solar variant; same Cozette face at ~2× the natural
    # 6x13 cell grid (26 CSS px ≈ 13 × 2). The label keeps the PSF dim-pair
    # naming so it pattern-matches the smaller one in the dropdown.
    {"id": "solar48x64",  "label": "solar48x64.psfu", "family": "Cozette",        "size": 26, "weight": "normal"},
    {"id": "terminus-24b","label": "Terminus v24b",   "family": "Terminus",       "size": 12, "weight": "bold"},
    {"id": "terminus-32", "label": "Terminus 32",     "family": "Terminus",       "size": 16, "weight": "normal"},
)
DEFAULT_FONT_ID = "jbm"


def _font_face_css():
    """Build @font-face rules from the committed woff2 files (fonts/, shared
    with term.html — see build-term.sh), base64-inlined so the dashboard
    renders in the right face without a separate request or a local install.
    Returns "" if any file is missing (rare — the build script guards this),
    leaving the font-family chain to fall back to the system monospace stack."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    out = []
    for family, weight, fn in _FONT_FACES:
        try:
            with open(os.path.join(base, fn), "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            return ""
        out.append(
            "@font-face{font-family:'%s';font-style:normal;"
            "font-weight:%d;font-display:swap;"
            "src:url(data:font/woff2;base64,%s) format('woff2');}" % (family, weight, b64)
        )
    return "\n".join(out)


FONT_FACE_CSS = _font_face_css()
FONTS_JSON = json.dumps(list(FONTS))

# Curated Lucide icon subset (MIT) for the per-tile glyphs. Value = inner SVG
# markup for each name; the client renders it in a 24x24 viewBox stroked with
# the tile's hash-derived hue. Kept inline (like the fonts above) so the server
# stays a single file. This same set is the whitelist for the AI fallback in
# resolve_icon() — the model can only ever return one of these names, so it
# physically cannot emit junk.
LUCIDE_ICONS_JSON = r'''{"layout-dashboard":"<rect width=\"7\" height=\"9\" x=\"3\" y=\"3\" rx=\"1\" /> <rect width=\"7\" height=\"5\" x=\"14\" y=\"3\" rx=\"1\" /> <rect width=\"7\" height=\"9\" x=\"14\" y=\"12\" rx=\"1\" /> <rect width=\"7\" height=\"5\" x=\"3\" y=\"16\" rx=\"1\" />","coins":"<path d=\"M13.744 17.736a6 6 0 1 1-7.48-7.48\" /> <path d=\"M15 6h1v4\" /> <path d=\"m6.134 14.768.866-.5 2 3.464\" /> <circle cx=\"16\" cy=\"8\" r=\"6\" />","notebook-text":"<path d=\"M2 6h4\" /> <path d=\"M2 10h4\" /> <path d=\"M2 14h4\" /> <path d=\"M2 18h4\" /> <rect width=\"16\" height=\"20\" x=\"4\" y=\"2\" rx=\"2\" /> <path d=\"M9.5 8h5\" /> <path d=\"M9.5 12H16\" /> <path d=\"M9.5 16H14\" />","package":"<path d=\"M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z\" /> <path d=\"M12 22V12\" /> <polyline points=\"3.29 7 12 12 20.71 7\" /> <path d=\"m7.5 4.27 9 5.15\" />","package-check":"<path d=\"M12 22V12\" /> <path d=\"m16 17 2 2 4-4\" /> <path d=\"M21 11.127V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.729l7 4a2 2 0 0 0 2 .001l1.32-.753\" /> <path d=\"M3.29 7 12 12l8.71-5\" /> <path d=\"m7.5 4.27 8.997 5.148\" />","flask-conical":"<path d=\"M14 2v6a2 2 0 0 0 .245.96l5.51 10.08A2 2 0 0 1 18 22H6a2 2 0 0 1-1.755-2.96l5.51-10.08A2 2 0 0 0 10 8V2\" /> <path d=\"M6.453 15h11.094\" /> <path d=\"M8.5 2h7\" />","hammer":"<path d=\"m15 12-9.373 9.373a1 1 0 0 1-3.001-3L12 9\" /> <path d=\"m18 15 4-4\" /> <path d=\"m21.5 11.5-1.914-1.914A2 2 0 0 1 19 8.172v-.344a2 2 0 0 0-.586-1.414l-1.657-1.657A6 6 0 0 0 12.516 3H9l1.243 1.243A6 6 0 0 1 12 8.485V10l2 2h1.172a2 2 0 0 1 1.414.586L18.5 14.5\" />","cpu":"<path d=\"M12 20v2\" /> <path d=\"M12 2v2\" /> <path d=\"M17 20v2\" /> <path d=\"M17 2v2\" /> <path d=\"M2 12h2\" /> <path d=\"M2 17h2\" /> <path d=\"M2 7h2\" /> <path d=\"M20 12h2\" /> <path d=\"M20 17h2\" /> <path d=\"M20 7h2\" /> <path d=\"M7 20v2\" /> <path d=\"M7 2v2\" /> <rect x=\"4\" y=\"4\" width=\"16\" height=\"16\" rx=\"2\" /> <rect x=\"8\" y=\"8\" width=\"8\" height=\"8\" rx=\"1\" />","bot":"<path d=\"M12 8V4H8\" /> <rect width=\"16\" height=\"12\" x=\"4\" y=\"8\" rx=\"2\" /> <path d=\"M2 14h2\" /> <path d=\"M20 14h2\" /> <path d=\"M15 13v2\" /> <path d=\"M9 13v2\" />","sparkles":"<path d=\"M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .962 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.962 0z\" /> <path d=\"M20 3v4\" /> <path d=\"M22 5h-4\" /> <path d=\"M4 17v2\" /> <path d=\"M5 18H3\" />","box":"<path d=\"M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z\" /> <path d=\"m3.3 7 8.7 5 8.7-5\" /> <path d=\"M12 22V12\" />","message-square":"<path d=\"M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z\" />","git-branch":"<path d=\"M15 6a9 9 0 0 0-9 9V3\" /> <circle cx=\"18\" cy=\"6\" r=\"3\" /> <circle cx=\"6\" cy=\"18\" r=\"3\" />","database":"<ellipse cx=\"12\" cy=\"5\" rx=\"9\" ry=\"3\" /> <path d=\"M3 5V19A9 3 0 0 0 21 19V5\" /> <path d=\"M3 12A9 3 0 0 0 21 12\" />","globe":"<circle cx=\"12\" cy=\"12\" r=\"10\" /> <path d=\"M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20\" /> <path d=\"M2 12h20\" />","server":"<rect width=\"20\" height=\"8\" x=\"2\" y=\"2\" rx=\"2\" ry=\"2\" /> <rect width=\"20\" height=\"8\" x=\"2\" y=\"14\" rx=\"2\" ry=\"2\" /> <line x1=\"6\" x2=\"6.01\" y1=\"6\" y2=\"6\" /> <line x1=\"6\" x2=\"6.01\" y1=\"18\" y2=\"18\" />","terminal":"<path d=\"M12 19h8\" /> <path d=\"m4 17 6-6-6-6\" />","candlestick-chart":"<path d=\"M9 5v4\" /> <rect width=\"4\" height=\"6\" x=\"7\" y=\"9\" rx=\"1\" /> <path d=\"M9 15v2\" /> <path d=\"M17 3v2\" /> <rect width=\"4\" height=\"8\" x=\"15\" y=\"5\" rx=\"1\" /> <path d=\"M17 13v3\" /> <path d=\"M3 3v16a2 2 0 0 0 2 2h16\" />","code":"<path d=\"m16 18 6-6-6-6\" /> <path d=\"m8 6-6 6 6 6\" />","hash":"<line x1=\"4\" x2=\"20\" y1=\"9\" y2=\"9\" /> <line x1=\"4\" x2=\"20\" y1=\"15\" y2=\"15\" /> <line x1=\"10\" x2=\"8\" y1=\"3\" y2=\"21\" /> <line x1=\"16\" x2=\"14\" y1=\"3\" y2=\"21\" />","folder":"<path d=\"M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z\" />","rocket":"<path d=\"M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5\" /> <path d=\"M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09\" /> <path d=\"M9 12a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.4 22.4 0 0 1-4 2z\" /> <path d=\"M9 12H4s.55-3.03 2-4c1.62-1.08 5 .05 5 .05\" />","bug":"<path d=\"M12 20v-9\" /> <path d=\"M14 7a4 4 0 0 1 4 4v3a6 6 0 0 1-12 0v-3a4 4 0 0 1 4-4z\" /> <path d=\"M14.12 3.88 16 2\" /> <path d=\"M21 21a4 4 0 0 0-3.81-4\" /> <path d=\"M21 5a4 4 0 0 1-3.55 3.97\" /> <path d=\"M22 13h-4\" /> <path d=\"M3 21a4 4 0 0 1 3.81-4\" /> <path d=\"M3 5a4 4 0 0 0 3.55 3.97\" /> <path d=\"M6 13H2\" /> <path d=\"m8 2 1.88 1.88\" /> <path d=\"M9 7.13V6a3 3 0 1 1 6 0v1.13\" />","wrench":"<path d=\"M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z\" />","network":"<rect x=\"16\" y=\"16\" width=\"6\" height=\"6\" rx=\"1\" /> <rect x=\"2\" y=\"16\" width=\"6\" height=\"6\" rx=\"1\" /> <rect x=\"9\" y=\"2\" width=\"6\" height=\"6\" rx=\"1\" /> <path d=\"M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3\" /> <path d=\"M12 12V8\" />","file-code":"<path d=\"M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z\" /> <path d=\"M14 2v5a1 1 0 0 0 1 1h5\" /> <path d=\"M10 12.5 8 15l2 2.5\" /> <path d=\"m14 12.5 2 2.5-2 2.5\" />","zap":"<path d=\"M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z\" />","container":"<path d=\"M22 7.7c0-.6-.4-1.2-.8-1.5l-6.3-3.9a1.72 1.72 0 0 0-1.7 0l-10.3 6c-.5.2-.9.8-.9 1.4v6.6c0 .5.4 1.2.8 1.5l6.3 3.9a1.72 1.72 0 0 0 1.7 0l10.3-6c.5-.3.9-1 .9-1.5Z\" /> <path d=\"M10 21.9V14L2.1 9.1\" /> <path d=\"m10 14 11.9-6.9\" /> <path d=\"M14 19.8v-8.1\" /> <path d=\"M18 17.5V9.4\" />"}'''
ICON_NAMES = frozenset(json.loads(LUCIDE_ICONS_JSON))

# --- tile icon resolution ------------------------------------------------
# The client resolves most icons itself (a kind map + a keyword regex, both
# instant and offline). Only titles that match nothing fall through to here,
# where an Anthropic call picks the best-fitting name from ICON_NAMES. Results
# are memoised to disk so each distinct title costs at most ONE API call ever,
# and a missing key / network error just yields None (client keeps its
# keyword/terminal fallback).
ICON_AI_TIMEOUT = 8  # seconds; the dashboard already showed a fallback glyph
# Not a *.json name on purpose: REGISTRY is scanned for session "<sid>.json"
# files (see read_sessions), so the cache must not look like one.
_ICON_CACHE_PATH = os.path.join(REGISTRY, ".icon-cache")
_icon_lock = threading.Lock()
_icon_cache = None  # lazy {normalized_title: name|None}


def _load_icon_cache():
    global _icon_cache
    if _icon_cache is None:
        try:
            with open(_ICON_CACHE_PATH) as f:
                data = json.load(f)
            _icon_cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _icon_cache = {}
    return _icon_cache


def _save_icon_cache():
    try:
        os.makedirs(REGISTRY, exist_ok=True)
        tmp = _ICON_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_icon_cache, f)
        os.replace(tmp, _ICON_CACHE_PATH)  # atomic; never leave a half-written cache
    except OSError:
        pass


def _ai_pick_icon(title, cwd):
    """Ask Anthropic for the single best icon NAME from ICON_NAMES for this
    session. Returns a validated name or None (no key / error / off-list)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (
        "Pick the single most fitting icon for a terminal / coding session.\n"
        "Title: %s\nWorking dir: %s\n\n"
        "Reply with ONLY one name from this list, nothing else:\n%s"
        % (title[:200], (cwd or "")[:200], ", ".join(sorted(ICON_NAMES)))
    )
    body = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=ICON_AI_TIMEOUT) as resp:
            data = json.load(resp)
        text = "".join(b.get("text", "") for b in data.get("content", []))
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    # Tokenise on non-name chars and return the first whitelisted name. More
    # robust than a bare strip — survives a stray prefix like "Icon: coins" or
    # surrounding quotes/prose — and still can't yield anything off-list.
    for tok in re.split(r"[^a-z0-9-]+", text.strip().lower()):
        if tok in ICON_NAMES:
            return tok
    return None


def resolve_icon(title, cwd):
    """Title -> validated icon name or None, memoised on disk."""
    norm = (title or "").strip().lower()
    if not norm:
        return None
    cache = _load_icon_cache()
    with _icon_lock:
        if norm in cache:
            return cache[norm]
    name = _ai_pick_icon(title or "", cwd or "")  # network call OUTSIDE the lock
    with _icon_lock:
        cache[norm] = name
        _save_icon_cache()
    return name


# Self-contained chatroom page served at /channel/<name>. Embedded as an
# iframe inside the dashboard's channel tile (same origin as the dashboard,
# so no CSRF/CORS dance — POST to /api/channel/<name> with the same token).
# Four placeholders get substituted at request time: __CHANNEL_NAME__,
# __CSRF_TOKEN__, __FONT_FACE__, __DEFAULT_WHO__. The poll interval (1500 ms)
# is the same as the dashboard's tile poll — fast enough to feel live
# without hammering.
CHANNEL_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="__CSRF_TOKEN__">
<title>#__CHANNEL_NAME__</title>
<style>
__FONT_FACE__
:root { --bg:#0b0e14; --panel:#11151c; --border:#222a35; --fg:#c9d1d9;
        --muted:#6b7685; --accent:#7aa2f7; --me:#7ee787; }
* { box-sizing: border-box; }
html, body { margin:0; height:100%; background:var(--bg); color:var(--fg);
  font:13px/1.45 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  display:flex; flex-direction:column; overflow:hidden; }
header { padding:6px 10px; border-bottom:1px solid var(--border);
  background:var(--panel); display:flex; align-items:center; gap:10px;
  font-size:12px; color:var(--muted); flex:0 0 auto; }
/* Channel name in gold — matches the title-bar icon + name in the dashboard
   tile (CHANNEL_TITLE_COLOR) and the `channel` badge, so the chatroom reads as
   one coherent colour everywhere it appears. */
header .nm { color:#f0b54e; font-weight:600; }
header .who { margin-left:auto; }
header .who input { background:#0d1117; color:var(--fg); border:1px solid var(--border);
  border-radius:4px; padding:2px 6px; font:inherit; width:120px; outline:none; }
header .who input:focus { border-color:var(--accent); }
#log { flex:1 1 auto; overflow-y:auto; padding:8px 10px; }
.msg { padding:8px 0; word-wrap:break-word; white-space:pre-wrap; }
.msg + .msg { border-top:1px solid #1a1f28; }
.msg .from { color:var(--accent); font-weight:600; margin-right:6px; }
.msg.me .from { color:var(--me); }
.msg .ts { color:var(--muted); font-size:10px; margin-left:8px;
  font-variant-numeric:tabular-nums; }
form { display:flex; gap:6px; padding:6px 8px; border-top:1px solid var(--border);
  background:var(--panel); flex:0 0 auto; }
form textarea { flex:1; resize:none; background:#0d1117; color:var(--fg);
  border:1px solid var(--border); border-radius:5px; padding:5px 8px;
  font:inherit; outline:none; min-height:28px; max-height:120px; }
form textarea:focus { border-color:var(--accent); }
form button { background:var(--accent); color:#0b0e14; border:0; border-radius:5px;
  padding:0 14px; font:inherit; font-weight:600; cursor:pointer; }
form button:disabled { opacity:.4; cursor:default; }
.status { color:var(--muted); font-size:11px; padding:4px 10px; }
/* Dark scrollbar */
#log { scrollbar-width:thin; scrollbar-color:#2a3340 transparent; }
#log::-webkit-scrollbar { width:8px; }
#log::-webkit-scrollbar-thumb { background:#2a3340; border-radius:6px;
  border:2px solid var(--bg); }
</style>
</head><body>
<header>
  <span class="nm">#__CHANNEL_NAME__</span>
  <span id="count" class="muted">0 messages</span>
  <span class="who">as <input id="who" type="text" spellcheck="false" autocomplete="off" value="__DEFAULT_WHO__"></span>
</header>
<div id="log"></div>
<form id="form">
  <textarea id="text" placeholder="message (Enter to send, Shift+Enter for newline)" rows="1"></textarea>
  <button type="submit" id="send" disabled>Send</button>
</form>
<script>
const CHANNEL = "__CHANNEL_NAME__";
const CSRF = document.querySelector('meta[name=csrf-token]').content;
// Reverse-proxy subpath prefix (e.g. '/dash'); see the dashboard page for why.
// This chatroom is loaded as a tile under the same prefix, so prepend it to the
// /api/channel fetches via the same fetch wrapper.
const BASE = "__BASE__";
(function () {
  const _f = window.fetch.bind(window);
  window.fetch = function (input, init) {
    if (typeof input === 'string' && input.charAt(0) === '/' && input.charAt(1) !== '/')
      input = BASE + input;
    return _f(input, init);
  };
})();
const log = document.getElementById('log');
const countEl = document.getElementById('count');
const form = document.getElementById('form');
const text = document.getElementById('text');
const sendBtn = document.getElementById('send');
const who = document.getElementById('who');

// Remember the user's chosen display name across reloads of THIS channel.
// Falls back to the server-injected OS username, so the Send button isn't
// gated behind "user notices the small 'as' field and types a name".
const WHO_KEY = 'channel-who:' + CHANNEL;
try {
  const saved = localStorage.getItem(WHO_KEY);
  if (saved) who.value = saved;       // keep the per-channel override if set
} catch (e) {}
who.addEventListener('input', () => {
  try { localStorage.setItem(WHO_KEY, who.value); } catch (e) {}
  updateSendState();
});

function updateSendState() {
  // Only the message text is strictly required — `who` defaults to the OS
  // username server-side, so this is normally non-empty too.
  sendBtn.disabled = !text.value.trim() || !who.value.trim();
}
text.addEventListener('input', updateSendState);

let total = 0;        // last-seen total line count returned by /api/channel
let pollTimer = null;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 5);   // HH:MM
}
function appendMessage(m) {
  const el = document.createElement('div');
  el.className = 'msg' + (m.from === who.value.trim() ? ' me' : '');
  el.innerHTML = '<span class="from">' + escapeHtml(m.from) + '</span>' +
                 escapeHtml(m.text) +
                 '<span class="ts">' + escapeHtml(fmtTime(m.ts)) + '</span>';
  log.appendChild(el);
}

async function poll() {
  try {
    const r = await fetch('/api/channel/' + encodeURIComponent(CHANNEL) +
                          '?since=' + total,
                          { headers: { 'Accept': 'application/json' } });
    if (!r.ok) return;
    const d = await r.json();
    if (Array.isArray(d.messages) && d.messages.length) {
      const wasAtBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
      for (const m of d.messages) appendMessage(m);
      if (wasAtBottom) log.scrollTop = log.scrollHeight;
    }
    if (typeof d.total === 'number') {
      total = d.total;
      countEl.textContent = total + (total === 1 ? ' message' : ' messages');
    }
  } catch (e) {}
}

async function send() {
  const t = text.value.trim();
  const me = who.value.trim();
  if (!t || !me) return;
  sendBtn.disabled = true;
  try {
    const r = await fetch('/api/channel/' + encodeURIComponent(CHANNEL), {
      method: 'POST',
      headers: { 'X-CSRF-Token': CSRF, 'Content-Type': 'application/json' },
      body: JSON.stringify({ from: me, text: t }),
    });
    if (r.ok) {
      text.value = '';
      text.style.height = 'auto';
      poll();   // pick our own message up immediately so the UI doesn't lag the network
    }
  } catch (e) {}
  updateSendState();
  text.focus();
}

form.addEventListener('submit', e => { e.preventDefault(); send(); });
text.addEventListener('keydown', e => {
  // Enter sends, Shift+Enter inserts a newline. Same convention as Slack /
  // Discord / claude code's TUI prompt.
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
// Auto-grow the textarea up to its max-height.
text.addEventListener('input', () => {
  text.style.height = 'auto';
  text.style.height = Math.min(120, text.scrollHeight) + 'px';
});

poll();
pollTimer = setInterval(poll, 1500);
text.focus();
</script>
</body></html>
"""


# Self-contained note page served at /note/<id>. Embedded as an iframe by the
# dashboard (same origin), so its POST back to /api/note/<id> carries the CSRF
# token with no CORS dance. A contenteditable surface holds text + pasted
# images (inlined as base64 data URLs); the body autosaves (debounced) and the
# first line is posted up as the live tile title.
NOTE_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="__CSRF_TOKEN__">
<title>note</title>
<style>
__FONT_FACE__
:root { --bg:#1c1e26; --fg:#d6d9e0; --muted:#6b7685; --accent:#f79ac0;
        --border:#2a2d38; }
/* Light mode mirrors the dashboard: the host toggles a `light` class on <html>
   (pre-paint from the shared localStorage key, then live via a postMessage
   listener), flipping the note surface from dark to light. */
html.light { --bg:#ffffff; --fg:#1c2430; --muted:#5b6573; --accent:#2f6df6;
        --border:#d4d9e1; }
html.light #note { scrollbar-color:#c3c9d2 transparent; }
html.light #note::-webkit-scrollbar-thumb { background:#c3c9d2; }
* { box-sizing:border-box; }
html, body { margin:0; height:100%; background:var(--bg); color:var(--fg);
  font:14px/1.55 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  display:flex; flex-direction:column; overflow:hidden; }
#note { flex:1 1 auto; overflow-y:auto; padding:12px 14px; outline:none;
  white-space:pre-wrap; word-wrap:break-word; }
#note:empty::before { content:attr(data-placeholder); color:var(--muted); }
#note img { max-width:100%; height:auto; border-radius:6px; margin:4px 0;
  display:block; }
#note a { color:var(--accent); }
#status { flex:0 0 auto; font-size:10px; color:var(--muted); padding:3px 10px;
  border-top:1px solid var(--border); display:flex; gap:6px; align-items:center; }
#status .dot { width:6px; height:6px; border-radius:50%; background:var(--muted);
  flex:0 0 auto; }
#status.saved .dot { background:#7ee787; }
#status.saving .dot { background:#f7c97a; }
#status.error .dot { background:#f78a8a; }
#note { scrollbar-width:thin; scrollbar-color:#2a3340 transparent; }
#note::-webkit-scrollbar { width:8px; }
#note::-webkit-scrollbar-thumb { background:#2a3340; border-radius:6px;
  border:2px solid var(--bg); }
</style>
<script>/* apply saved theme before first paint → no flash (same-origin: shares the dashboard's localStorage) */try{if(localStorage.getItem('claude-sessions-theme')==='light')document.documentElement.classList.add('light')}catch(e){}</script>
</head><body>
<div id="note" contenteditable="true" spellcheck="true"
     data-placeholder="Type a note… (paste images too)"></div>
<div id="status"><span class="dot"></span><span id="stext">loading…</span></div>
<script>
(function () {
  'use strict';
  var SID = "__NOTE_SID__";
  var CSRF = document.querySelector('meta[name=csrf-token]').content;
  // Reverse-proxy subpath prefix (e.g. '/dash'); prepend it to the /api/note
  // fetches via the same fetch wrapper the dashboard uses.
  var BASE = "__BASE__";
  (function () {
    var _f = window.fetch.bind(window);
    window.fetch = function (input, init) {
      if (typeof input === 'string' && input.charAt(0) === '/' && input.charAt(1) !== '/')
        input = BASE + input;
      return _f(input, init);
    };
  })();
  var note = document.getElementById('note');
  var statusEl = document.getElementById('status');
  var stext = document.getElementById('stext');
  function setStatus(cls, txt) { statusEl.className = cls; stext.textContent = txt; }

  // Live light/dark toggle: the dashboard broadcasts the theme to every iframe
  // (including this same-origin note) on the header button. Pre-paint already
  // applied the stored theme; this keeps us in sync when the user flips it.
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || d.type !== 'claude-host' || d.cmd !== 'theme') return;
    document.documentElement.classList.toggle('light', d.theme === 'light');
  });

  // Allowlist sanitizer (no external deps). The body is the user's own content,
  // but it's persisted and re-applied via innerHTML on every load — so a
  // drag-dropped `<img onerror>` (or any stray markup) must never round-trip.
  // We parse into a <template>, whose content is INERT (images don't load,
  // scripts don't run), keep only a safe subset of tags, drop all attributes
  // except an `alt` and a `data:image/` `src` on <img>, and unwrap the rest to
  // their text. Applied on BOTH save (storage is always clean) and load.
  var ALLOWED = { DIV:1, P:1, BR:1, IMG:1, B:1, I:1, U:1, STRONG:1, EM:1, SPAN:1 };
  function sanitize(html) {
    var tpl = document.createElement('template');
    tpl.innerHTML = html || '';
    (function walk(node) {
      var kids = Array.prototype.slice.call(node.childNodes);
      for (var i = 0; i < kids.length; i++) {
        var n = kids[i];
        if (n.nodeType === 3) continue;                       // text — keep
        if (n.nodeType !== 1) { n.remove(); continue; }        // comment/etc — drop
        if (!ALLOWED[n.tagName]) {                             // unknown — unwrap to text
          n.replaceWith(document.createTextNode(n.textContent || ''));
          continue;
        }
        var attrs = Array.prototype.slice.call(n.attributes);
        for (var j = 0; j < attrs.length; j++) {
          var a = attrs[j];
          var keep = n.tagName === 'IMG' &&
            ((a.name === 'src' && /^data:image\//i.test(a.value)) || a.name === 'alt');
          if (!keep) n.removeAttribute(a.name);
        }
        if (n.tagName === 'IMG' && !/^data:image\//i.test(n.getAttribute('src') || '')) {
          n.remove(); continue;                                // img without a safe data URL
        }
        walk(n);
      }
    })(tpl.content);
    return tpl.innerHTML;
  }

  // Live tile title: first non-empty line, posted up to the dashboard. The
  // parent validates e.origin is 127.0.0.1, so '*' targetOrigin is fine here
  // (same pattern as the terminal client).
  function postTitle() {
    try {
      if (!(window.parent && window.parent !== window)) return;
      var first = (note.innerText || '').split('\n')
        .map(function (s) { return s.trim(); }).filter(Boolean)[0] || 'Note';
      window.parent.postMessage(
        { type: 'claude-term', sid: SID, title: first.slice(0, 80) }, '*');
    } catch (e) {}
  }

  // Debounced autosave. lastSaved guards against re-POSTing an unchanged body;
  // `dirty` coalesces edits that land mid-flight into one trailing save.
  var saveTimer = null, lastSaved = null, saving = false, dirty = false;
  // `loaded` latch: the body is fetched async on startup. Until it arrives the
  // editor is empty and lastSaved is null, so a pagehide / visibilitychange
  // firing in that window would autosave an EMPTY body over the saved content
  // (the race that zeroed real notes when an OOM reload re-mounted the iframes).
  // Block all saves until the initial load attempt has resolved.
  var loaded = false;
  function doSave(keepalive) {
    if (!loaded) return;                   // never persist before first load
    var html = sanitize(note.innerHTML);   // store only the safe subset
    if (html === lastSaved) { setStatus('saved', 'saved'); return; }
    if (saving) { dirty = true; return; }
    saving = true; dirty = false; setStatus('saving', 'saving…');
    fetch('/api/note/' + encodeURIComponent(SID), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
      body: JSON.stringify({ html: html }),
      keepalive: !!keepalive   // survives unload (note: ~64 KB cap when set)
    }).then(function (r) { return r.ok; }, function () { return false; })
      .then(function (ok) {
        saving = false;
        if (ok) { lastSaved = html; setStatus('saved', 'saved'); }
        else setStatus('error', 'save failed');
        if (dirty || !ok) scheduleSave();   // flush coalesced edits / retry
      });
  }
  function scheduleSave() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { saveTimer = null; doSave(false); }, 600);
  }

  note.addEventListener('input', function () { postTitle(); scheduleSave(); });
  note.addEventListener('blur', function () {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; } doSave(false);
  });
  window.addEventListener('pagehide', function () { doSave(true); });
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) doSave(true);
  });

  // Paste: accept plain text + images only — never arbitrary pasted HTML (so
  // the stored body can't carry scripts/markup back into innerHTML on reload).
  note.addEventListener('paste', function (e) {
    var dt = e.clipboardData; if (!dt) return;
    var imgs = [];
    for (var i = 0; i < dt.items.length; i++) {
      var it = dt.items[i];
      if (it.kind === 'file' && it.type.indexOf('image/') === 0) {
        var f = it.getAsFile(); if (f) imgs.push(f);
      }
    }
    if (imgs.length) {
      e.preventDefault();
      imgs.forEach(function (file) {
        var rd = new FileReader();
        rd.onload = function () {
          // data URL we build ourselves — safe to insertHTML.
          document.execCommand('insertHTML', false,
            '<img src="' + rd.result + '" alt="pasted image">');
          postTitle(); scheduleSave();
        };
        rd.readAsDataURL(file);
      });
      return;
    }
    var text = dt.getData('text/plain');
    if (text != null) {
      e.preventDefault();
      document.execCommand('insertText', false, text);
    }
  });

  // Drop: same policy as paste — image files inline as data URLs, everything
  // else as plain text. preventDefault blocks the browser's default rich-HTML
  // drop (which would inject arbitrary markup into the contenteditable).
  note.addEventListener('drop', function (e) {
    var dt = e.dataTransfer; if (!dt) return;
    e.preventDefault();
    var handled = false;
    if (dt.files && dt.files.length) {
      for (var i = 0; i < dt.files.length; i++) {
        var file = dt.files[i];
        if (file.type.indexOf('image/') !== 0) continue;
        handled = true;
        (function (f) {
          var rd = new FileReader();
          rd.onload = function () {
            document.execCommand('insertHTML', false,
              '<img src="' + rd.result + '" alt="dropped image">');
            postTitle(); scheduleSave();
          };
          rd.readAsDataURL(f);
        })(file);
      }
    }
    if (!handled) {
      var text = dt.getData('text/plain');
      if (text) { document.execCommand('insertText', false, text); postTitle(); scheduleSave(); }
    }
  });

  // Load the saved body. Sanitized again on the way in (belt-and-suspenders:
  // the file is already clean, but this neutralizes any hand-edited body too).
  fetch('/api/note/' + encodeURIComponent(SID), { cache: 'no-store' })
    .then(function (r) { return r.ok ? r.text() : ''; }, function () { return ''; })
    .then(function (html) {
      note.innerHTML = sanitize(html || '');
      lastSaved = note.innerHTML;
      loaded = true;   // arm autosave only now that the body is in the DOM
      setStatus('saved', html ? 'saved' : 'empty');
      postTitle();
      try { note.focus(); } catch (e) {}
    });
})();
</script>
</body></html>
"""


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="__CSRF_TOKEN__">
<title>Claude Sessions</title>
<!-- Empty inline icon so the browser doesn't auto-request /favicon.ico — under a
     reverse-proxy subpath that resolves at the ORIGIN root, outside the app, and
     would 404/502 against whatever else lives there (e.g. a noisy console error). -->
<link rel="icon" href="data:,">
<style>
__FONT_FACE__
  :root {
    --bg:#0b0e14; --panel:#11151c; --border:#222a35; --fg:#c9d1d9;
    --muted:#6b7685; --accent:#7aa2f7; --host:#7ee787; --container:#79c0ff;
    /* parked (condensed) card: --cond-peek is the spine width, --cond-tuck is
       how far each card slides under its right neighbour (the overlap). */
    --cond-peek:124px;
    --cond-tuck:30px;
    /* trailing gap past the last tile in a row — a clear "stop here" */
    --row-end-gap:96px;
  }
  /* Bright (light) mode — toggled via the header ☀/🌙 button, persisted in
     localStorage('claude-sessions-theme') and applied to <html> so the override
     cascades to the whole chrome. Terminal TILE contents keep their own dark
     background (an xterm theme pushed separately), so light mode reads as a
     light dashboard around dark terminals. */
  html.light {
    --bg:#f5f6f8; --panel:#ffffff; --border:#d4d9e1; --fg:#1c2430;
    --muted:#5b6573; --accent:#2f6df6; --host:#1a7f37; --container:#0969da;
  }
  html.light, html.light * { scrollbar-color:#c3c9d2 transparent; }
  html.light ::-webkit-scrollbar-thumb { background:#c3c9d2; border-color:var(--bg); }
  html.light ::-webkit-scrollbar-thumb:hover { background:#aeb6c2; }
  /* Light mode: chrome controls/surfaces that hardcode a dark fill must flip to
     light ones — otherwise selects, inputs, menu rows and buttons render as dark
     boxes (some with dark-on-dark text) floating in the otherwise-light UI. */
  html.light .font-picker,
  html.light .modal .btn,
  html.light button.stash-pill { background:#eef1f5; }
  html.light .new-menu button:hover,
  html.light .stash-menu .row:hover { background:#e9edf3; }
  html.light .stash-menu .row { border-bottom-color:var(--border); }
  html.light .lrow { background:#eef1f5; }
  html.light .lrow input, html.light .lrow textarea { background:#ffffff; }
  html.light .tile.webview .urlbar { background:#eef1f5; }
  html.light .tile.webview .urlbar input { background:#ffffff; }
  /* Tile box, loading placeholder and the iframe's own fill default to a dark
     #2b2b2b — visible while a tile boots (or when it has no live terminal yet),
     reading as a dark slab in the light UI. A connected terminal paints its own
     dark background over the iframe, so only the loading/empty state changes. */
  html.light .tile,
  html.light .tile iframe,
  html.light .tile.loading > .loading-body { background:#eef1f5; }
  * { box-sizing: border-box; scrollbar-width: thin; scrollbar-color: #2a3340 transparent; }
  ::-webkit-scrollbar { width:8px; height:8px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background:#2a3340; border-radius:6px;
    border:2px solid var(--bg); }
  ::-webkit-scrollbar-thumb:hover { background:#3a4656; }
  html, body { margin:0; height:100%; overflow:hidden; background:var(--bg); color:var(--fg);
    /* Stop a horizontal trackpad scroll from being read as a back/forward swipe. */
    overscroll-behavior-x:none;
    font:13px/1.4 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace; }
  header.bar { display:flex; align-items:center; gap:10px; padding:8px 14px;
    border-bottom:1px solid var(--border); background:var(--panel); height:42px; }
  header.bar h1 { font-size:14px; margin:0; font-weight:600; letter-spacing:.3px; }
  .muted { color:var(--muted); }
  .spacer { flex:1; }
  .dot { width:8px; height:8px; border-radius:50%; background:var(--host);
    box-shadow:0 0 7px var(--host); }
  #tabs { display:flex; align-items:stretch; gap:4px; padding:0 8px;
    height:36px; background:var(--panel); border-bottom:1px solid var(--border);
    overflow-x:auto; overflow-y:hidden; overscroll-behavior-x:none; white-space:nowrap; }
  #tabs[hidden] { display:none; }
  #tabs .tab { display:flex; align-items:center; gap:6px; padding:0 12px;
    border:0; border-bottom:2px solid transparent; background:none; cursor:grab;
    color:var(--muted); font:inherit; height:36px; }
  #tabs .tab:hover { color:var(--fg); }
  #tabs .tab.active { color:var(--fg); border-bottom-color:var(--accent); }
  #tabs .tab.dragging { opacity:.5; cursor:grabbing; }
  #tabs .tab.drop-before { box-shadow:inset 2px 0 0 var(--accent); }
  #tabs .tab.drop-after { box-shadow:inset -2px 0 0 var(--accent); }
  #tabs .tab.bell { color:var(--host); }
  #tabs .tab.bell .n { background:var(--host); color:#000; box-shadow:0 0 6px var(--host); }
  #tabs .tab .n { font-size:10px; padding:0 6px; border-radius:9px;
    background:#0006; color:var(--muted); }
  #tabs .tab.active .n { color:var(--accent); }
  /* The count bullet and the pin share one fixed-width slot, so hovering never
     resizes the tab. The pin sits ABSOLUTELY over the slot, revealed on tab
     hover, and the number is hidden while it shows (the pin covers it). Faint =
     not pinned (click to pin), solid = pinned. An "empty" tab is a pinned
     workdir with no live tiles. */
  #tabs .tab .cntslot { position:relative; display:inline-flex; align-items:center;
    justify-content:center; min-width:16px; }
  #tabs .tab .cntslot .fav { position:absolute; inset:0; display:flex; align-items:center;
    justify-content:center; font-size:10px; line-height:1; cursor:pointer; opacity:0;
    pointer-events:none; transition:opacity .1s; }
  #tabs .tab:hover .cntslot .n { visibility:hidden; }            /* pin covers the number */
  #tabs .tab:hover .cntslot .fav { opacity:.55; pointer-events:auto; }
  #tabs .tab:hover .cntslot .fav.on { opacity:1; }              /* pinned → solid */
  #tabs .tab:hover .cntslot .fav:hover { opacity:1; }
  #tabs .tab.empty span:first-child { font-style:italic; opacity:.8; }
  /* Home tab: pinned first, an anchor for tiles needing attention. Divider sets
     it off from the workdir tabs; its count/glow reuse the .tab.bell styling. */
  #tabs .tab.home { cursor:pointer; padding-right:14px; margin-right:2px;
    border-right:1px solid var(--border); }
  #tabs .tab.home .home-ic { font-size:14px; line-height:1; }
  #tabs .tab.home .n { font-size:10px; padding:0 6px; border-radius:9px;
    background:#0006; color:var(--muted); }
  #tab-empty { position:fixed; left:0; right:0; top:78px; bottom:0; display:flex;
    align-items:center; justify-content:center; text-align:center; padding:24px;
    color:var(--muted); font-size:13px; pointer-events:none; }
  #grid { display:grid; gap:8px; padding:8px; height:calc(100vh - 42px);
    grid-auto-rows:1fr;
    /* Side-by-side when there's room; stack to a full-width column when the
       window is too narrow (otherwise tiles get cramped and terminals wrap
       text mid-word). */
    grid-template-columns:repeat(auto-fit, minmax(min(100%, 400px), 1fr)); }
  body.has-tabs #grid { height:calc(100vh - 42px - 36px); }
  /* Grid layout (≤2 tiles): make channel/note tiles half the width of a normal
     one. Normal tiles span two base columns, channel/note tiles span one. Gated
     to wide screens — below this the base full-width-stacking rule above wins, so
     a narrow window never overflows a double-span tile off-screen. */
  @media (min-width:820px) {
    #grid:not(.row) { grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); }
    #grid:not(.row) > .tile { grid-column:span 2; }
    #grid:not(.row) > .tile[data-kind="channel"],
    #grid:not(.row) > .tile[data-kind="note"] { grid-column:span 1; }
  }
  /* 3+ sessions in the active view: one non-wrapping row, each box 1/2.2 of the
     screen width, scrolled horizontally. */
  #grid.row { display:flex; flex-wrap:nowrap; align-items:stretch;
    overflow-x:auto; overflow-y:hidden; overscroll-behavior-x:none; }
  #grid.row > .tile { flex:0 0 calc(100vw / 2.2); }
  /* Channel (chatroom) and note tiles are skinny columns — render them at half
     the width of a normal tile. Row layout: halve the flex-basis. */
  #grid.row > .tile[data-kind="channel"],
  #grid.row > .tile[data-kind="note"] { flex:0 0 calc(100vw / 4.4); }
  .tile { position:relative; display:flex; flex-direction:column; min-width:0; min-height:0;
    max-width:900px; border:1px solid var(--border); border-radius:8px; overflow:hidden; background:#2b2b2b;
    /* Animate the condense/expand glide. We CAN animate flex-basis (the box
       shrinking to a spine) and margin (the tuck) because the iframe is pinned
       to a CONSTANT width in row mode (`#grid.row > .tile > iframe`, below) — it
       no longer follows the box at width:100%, so it never drags through
       intermediate widths and never re-fires fit→SIGWINCH. The box just clips
       more of a fixed-width iframe each frame. (An earlier design left the
       iframe at width:100% and HAD to snap, to avoid the SIGWINCH storm; pinning
       the iframe for every row tile is what makes the animation safe.) max-width
       is for grid mode. The .closing fold sets its own transition (later rule
       wins) — keep that in sync if you touch durations. */
    transition:flex-basis .24s ease, margin .24s ease, max-width .24s ease; }
  /* Condensed ("park as a card"): parked tiles shrink to a thin spine and stack
     like a deck. The card BOX is genuinely narrow (--cond-peek) so even the
     rightmost one (no neighbour to hide behind) condenses for real — but the
     iframe INSIDE keeps its full expanded width (pinned in CSS to match the
     un-condensed width exactly) and is clipped by the card's overflow:hidden.
     So the visible box shrinks (animated via the .tile flex-basis transition)
     while the PTY width never changes: no SIGWINCH, no reflow, no hard-wrapped
     scrollback (see setCondensed). Cards tuck
     --cond-tuck under their right neighbour for the overlapping-deck look; the
     rightmost shows its full spine, like the top card of the stack. The next
     bell springs a card back to full size (see markBell → setCondensed). */
  #grid.row > .tile.condensed {
    flex:0 0 var(--cond-peek);
    margin-right:calc(-1 * var(--cond-tuck)); }
  /* Pin EVERY row tile's iframe to a constant width — the un-condensed width —
     not just condensed ones. For a full tile this equals its box width, so it's
     a no-op (width:100% resolves to the same pixels: min(…,900px) mirrors the
     .tile max-width cap, -2px is the left+right border under box-sizing:border-
     box). For a condensed tile the box is narrower, so the fixed-width iframe is
     clipped by overflow:hidden. Because the iframe width never changes between
     the two states (or at any frame in between), the box can ANIMATE its
     flex-basis from full to spine without the iframe being dragged through
     intermediate widths — so no fit→SIGWINCH, no reflow, no hard-wrapped
     scrollback, while the condense still glides (see .tile transition). */
  #grid.row > .tile > iframe { width:calc(min(100vw / 2.2, 900px) - 2px); }
  #grid.row > .tile[data-kind="channel"] > iframe,
  #grid.row > .tile[data-kind="note"] > iframe { width:calc(min(100vw / 4.4, 900px) - 2px); }
  /* Boundary shadow that makes the tucked overlap read as stacked cards. The
     shadow is physically cast BY the covering tile (the condensed card's right
     neighbour, pulled left over the card by the negative margin and painted on
     top by its higher flex `order`) onto the card below — so it lives on the
     NEIGHBOUR as a leftward box-shadow, flush at its real left edge.
     Two false starts to remember: (a) `.tile.condensed + .tile` selects the
     DOM-next tile, but tiles are arranged by flex `order` (applyOrder sets
     el.style.order on every tile) which has no sibling selector — wrong tile in
     any reordered/multi-tab layout. (b) Anchoring a gradient to the CARD's own
     ::after at a fixed `right:--cond-tuck` offset leaves an 8px GAP: the
     neighbour's measured left edge (754) is NOT card.right - tuck (746), so a
     fixed offset can't track it. So markDeckShadows() (JS) walks the visible row
     in flex-order and tags any tile whose predecessor is condensed with
     .covers-card; the box-shadow then sits exactly at that tile's left edge.
     box-shadow (outset) escapes the tile's own overflow:hidden; the grid's
     overflow clips the harmless vertical bleed. */
  #grid.row > .tile.covers-card {
    box-shadow:-9px 0 17px -5px rgba(0,0,0,.78); }
  /* Fixed trailing spacer: a clear "stop here" gap past the last tile, even when
     it's a narrow condensed card whose negative --cond-tuck margin would
     otherwise pull the row's end flush. The huge order keeps it last regardless
     of the tiles' manual CSS order; it's empty + click-through. */
  #grid.row::after { content:''; flex:0 0 var(--row-end-gap); order:99999;
    align-self:stretch; pointer-events:none; }
  /* Grid mode (≤2 tiles): shrinking the tile WOULD reflow the PTY (the iframe
     pin above is row-only), so condense is visual state only — leave size be. */
  /* A condensed head is too narrow for the full control strip — keep only the
     essentials (icon, name, condense toggle, close); the cwd, badge and the
     rarely-needed-while-parked buttons come back on expand. */
  .tile.condensed > .head > .cwd, .tile.condensed > .head > .badge,
  .tile.condensed > .head > a.open, .tile.condensed > .head > button.reload,
  .tile.condensed > .head > button.chat, .tile.condensed > .head > button.fork,
  .tile.condensed > .head > button.dup, .tile.condensed > .head > button.stash { display:none; }
  .tile > .head { cursor:grab; }
  body.dragging .tile > .head { cursor:grabbing; }
  /* While dragging, let the tiles (not their cross-origin iframes, which would
     otherwise swallow the dragover) receive the drop events. */
  body.dragging .tile iframe { pointer-events:none; }
  .tile.drop-before { box-shadow:inset 3px 0 0 var(--accent); }
  .tile.drop-after { box-shadow:inset -3px 0 0 var(--accent); }
  .tile.selected { outline:2px solid var(--accent); outline-offset:-2px; }
  /* Closing: fold the tile horizontally to its left edge so the row closes the
     gap, plus a quick fade. Width/flex are driven inline by animateClose (inline
     beats the row's flex rule); the durations here must match its fallback. */
  .tile.closing { opacity:0; transform:scaleX(0); transform-origin:left center;
    overflow:hidden; pointer-events:none;
    transition:flex-basis .24s ease, max-width .24s ease, width .24s ease,
      opacity .2s ease, transform .24s ease, margin .24s ease, padding .24s ease; }
  /* Reduced-motion setting (#motionBtn): zero the CSS-driven tile transitions —
     the condense/expand flex glide and the close fold — so tiles snap. The JS
     FLIP (open/resize) is gated separately via the `reducedMotion` flag, and
     animateClose finishes instantly so removal doesn't hinge on a transitionend
     that never fires. */
  body.reduce-motion .tile, body.reduce-motion .tile.closing { transition:none !important; }
  .tile.bell > .head { box-shadow: inset 0 2px 0 0 var(--host); }
  .tile.bell .name { color: var(--host); }
  .tile.bell .name::before { content: '● '; color: var(--host); }
  .tile.armed { outline:2px solid #ff6b6b; outline-offset:-2px; }
  .tile.armed::after { content:'Ctrl+Q again to close — Esc to cancel';
    position:absolute; top:34px; left:50%; transform:translateX(-50%); z-index:5;
    background:#ff6b6b; color:#000; font-weight:600; font-size:11px;
    padding:3px 8px; border-radius:4px; white-space:nowrap; pointer-events:none; }
  /* Loading placeholder shown right after clicking duplicate, while the new
     session's ttyd boots. Replaced by the real tile once it registers. */
  @keyframes claude-spin { to { transform:rotate(360deg); } }
  .tile.loading > .loading-body { flex:1 1 auto; display:flex; flex-direction:column;
    align-items:center; justify-content:center; gap:12px;
    color:var(--muted); background:#2b2b2b; }
  .tile.loading .spinner { width:32px; height:32px;
    border:3px solid var(--border); border-top-color:var(--accent);
    border-radius:50%; animation:claude-spin .9s linear infinite; }
  .tile > .head { display:flex; align-items:center; gap:8px; padding:5px 9px;
    background:var(--panel); border-bottom:1px solid var(--border); flex:0 0 auto; }
  /* Tile title colour comes from a per-tile CSS custom property assigned in
     JS (stable hash of session id → one of the TITLE_PALETTE entries). Using a
     variable (not an inline `color: …` on .name) lets the bell-on rule below
     keep winning when a tile rings, since `.tile.bell .name` has higher
     specificity than `.tile .name` and overrides the var-derived colour. The
     fallback `var(--fg)` kicks in for the loading-placeholder tile (which has
     no sid yet) and for any future kind we forget to colourise. */
  .tile .name { color: var(--tile-title-color, var(--fg));
    font-weight:600; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis; min-width:0; flex:0 1 auto; }
  .tile .cwd { color:var(--muted); white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis; flex:1 1 auto; min-width:0; direction:rtl; text-align:left; }
  /* Keep the badge, port and control buttons at fixed size — only the name and
     cwd shrink/ellipsize — so a long title never pushes the buttons off. */
  .tile > .head > .badge, .tile > .head > .muted,
  .tile > .head > a.open, .tile > .head > button { flex:0 0 auto; }
  .badge { font-size:10px; padding:1px 6px; border-radius:10px;
    border:1px solid var(--border); text-transform:uppercase; letter-spacing:.4px; }
  /* per-tile glyph, leftmost in the head; kept fixed-size like the badge */
  .icon { flex:0 0 auto; display:inline-flex; align-items:center; }
  .icon svg { display:block; }
  .badge.host { color:var(--host); border-color:#1c3a26; }
  .badge.container { color:var(--container); border-color:#173049; }
  .badge.terminal { color:#f7c97a; border-color:#473820; }
  .badge.container-terminal { color:#7ed1c4; border-color:#1e3a36; }
  .badge.opencode { color:#f78a8a; border-color:#4a1e1e; }
  .badge.codex { color:#6cc7a8; border-color:#1d3a30; }
  .badge.custom { color:#e0a96d; border-color:#473320; }
  .badge.channel  { color:#f0b54e; border-color:#4a3618; }
  .badge.note { color:#f79ac0; border-color:#4a2336; }
  /* Channels menu wraps the same as the +New menu so we get the dropdown
     positioning + click-outside-to-close behavior for free. */
  .ch-wrap { position:relative; }
  .ch-wrap .new-menu { right:0; }
  .badge.webview { color:#d18cf7; border-color:#3a2347; }
  /* "+" new-tile menu in the header. */
  .new-wrap { position:relative; }
  button.new { background:none; border:1px solid var(--border); border-radius:6px;
    color:var(--fg); cursor:pointer; padding:3px 9px; font:inherit; line-height:1; }
  button.new:hover { border-color:var(--accent); color:var(--accent); }
  #soundBtn.off, #wakeBtn.off, #motionBtn.off { opacity:.5; }
  #wakeBtn:disabled { opacity:.4; cursor:not-allowed; }
  .new-menu { position:absolute; top:28px; right:0; min-width:190px; max-width:340px;
    z-index:10;
    background:var(--panel); border:1px solid var(--border); border-radius:6px;
    box-shadow:0 6px 18px #000a; padding:4px 0; display:none; }
  .new-menu.open { display:block; }
  .new-menu button { display:block; width:100%; text-align:left; background:none;
    border:0; color:var(--fg); cursor:pointer; padding:7px 12px; font:inherit;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .new-menu button:hover { background:#1b212c; color:var(--accent); }
  .new-menu .new-sep { height:1px; background:var(--border); margin:4px 0; }
  .new-menu .new-cfg { color:var(--muted); font-size:12px; }
  /* per-launcher badge dot, coloured by the launcher's program (claude/codex/…) */
  .new-menu .lc-dot { display:inline-block; width:7px; height:7px; border-radius:50%;
    margin-right:8px; vertical-align:middle; background:var(--muted); }
  .new-menu #launcherItems:empty::after { content:"no launchers — add one below";
    display:block; padding:7px 12px; color:var(--muted); font-size:11px; }
  /* Manage-launchers modal */
  .modal-backdrop { position:fixed; inset:0; z-index:1000; background:#0008;
    display:flex; align-items:flex-start; justify-content:center; padding:40px 16px;
    overflow:auto; }
  .modal { background:var(--panel); border:1px solid var(--border); border-radius:10px;
    box-shadow:0 12px 40px #000b; width:min(680px,100%); color:var(--fg);
    display:flex; flex-direction:column; }
  .modal-head { display:flex; align-items:center; justify-content:space-between;
    padding:12px 16px; border-bottom:1px solid var(--border); font-weight:600; }
  .modal-x { background:none; border:0; color:var(--muted); cursor:pointer;
    font-size:15px; padding:2px 6px; }
  .modal-x:hover { color:var(--fg); }
  .modal-help { padding:10px 16px 0; color:var(--muted); font-size:12px; line-height:1.5; }
  .lrows { padding:10px 16px; display:flex; flex-direction:column; gap:10px;
    max-height:55vh; overflow:auto; }
  .lrow { border:1px solid var(--border); border-radius:8px; padding:9px;
    background:#0e131b; }
  .lrow-top { display:flex; gap:8px; align-items:center; }
  .lrow input, .lrow textarea { background:#070a0f; border:1px solid var(--border);
    color:var(--fg); border-radius:6px; padding:6px 8px; font:inherit; }
  .lrow .l-label { width:150px; flex:0 0 auto; }
  .lrow .l-cmd { flex:1; font-family:ui-monospace,Menlo,monospace; font-size:12px; }
  .lrow .l-del { background:none; border:0; color:var(--muted); cursor:pointer;
    font-size:14px; padding:2px 4px; flex:0 0 auto; }
  .lrow .l-del:hover { color:#f78a8a; }
  .lrow-adv { display:flex; flex-direction:column; gap:6px; margin-top:8px; }
  .lrow .l-vchk { font-size:12px; color:var(--muted); display:flex; align-items:center;
    gap:6px; cursor:pointer; }
  .lrow .l-env { width:100%; box-sizing:border-box; resize:vertical;
    font-family:ui-monospace,Menlo,monospace; font-size:11px; }
  .modal-foot { display:flex; align-items:center; gap:8px; padding:12px 16px;
    border-top:1px solid var(--border); }
  .modal .btn { background:#1b212c; border:1px solid var(--border); color:var(--fg);
    border-radius:6px; padding:6px 12px; font:inherit; cursor:pointer; }
  .modal .btn:hover { border-color:var(--accent); color:var(--accent); }
  .modal .btn.primary { background:var(--accent); color:#06121d; border-color:var(--accent);
    font-weight:600; }
  .modal .tpl-wrap select.btn { appearance:auto; }
  /* Settings (gear) menu: rows of label + control (font selects, theme/sound
     toggles, restart). Reuses the new-menu box look but lays out rows instead
     of full-width buttons. */
  .set-wrap { position:relative; }
  .settings-menu { position:absolute; top:28px; right:0; min-width:248px; z-index:10;
    background:var(--panel); border:1px solid var(--border); border-radius:6px;
    box-shadow:0 6px 18px #000a; padding:6px 0; display:none; }
  .settings-menu.open { display:block; }
  .settings-menu .row { display:flex; align-items:center; justify-content:space-between;
    gap:10px; padding:6px 12px; }
  .settings-menu .row label { color:var(--muted); font-size:12px; white-space:nowrap; }
  .settings-menu .row .font-picker { flex:0 0 auto; min-width:110px; }
  .settings-menu .row.sep { border-top:1px solid var(--border); margin-top:4px; padding-top:8px; }
  .settings-menu #restartBtn { width:100%; }
  .settings-menu .danger:hover { border-color:#ff6b6b; color:#ff6b6b; }
  /* Webview tile: editable URL bar above the iframe. */
  .tile.webview .urlbar { display:flex; align-items:center; gap:6px;
    padding:4px 8px; background:#1a1f2a; border-bottom:1px solid var(--border);
    flex:0 0 auto; }
  .tile.webview .urlbar input { flex:1 1 auto; background:#0e131c; color:var(--fg);
    border:1px solid var(--border); border-radius:4px; padding:3px 7px;
    font:12px/1.3 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace; min-width:0; }
  .tile.webview .urlbar input:focus { outline:none; border-color:var(--accent); }
  .tile.webview .urlbar button { background:none; border:1px solid var(--border);
    border-radius:4px; color:var(--muted); cursor:pointer; padding:2px 7px;
    font:inherit; line-height:1; }
  .tile.webview .urlbar button:hover { color:var(--accent); border-color:var(--accent); }
  .tile.webview .urlbar button.proxy.on { color:#7ee787; border-color:#1c3a26;
    background:#0e1a14; }
  .tile.webview .urlbar .blocked { color:#ff8888; font-size:11px; padding:0 4px; }
  .tile iframe { flex:1 1 auto; border:0; width:100%; height:100%; background:#2b2b2b; }
  a.open { color:var(--accent); text-decoration:none; font-size:14px; }
  button.close, button.dup, button.fork, button.stash, button.reload, button.chat, button.cond { background:none; border:0; padding:0 2px; cursor:pointer;
    color:var(--muted); font-size:14px; line-height:1; }
  button.close:hover { color:#ff6b6b; }
  button.dup:hover, button.fork:hover, button.stash:hover, button.reload:hover, button.chat:hover, button.cond:hover { color:var(--accent); }
  button.chat.on { color:var(--accent); }   /* chat view active — terminal hidden behind it */
  button.cond.on { color:var(--accent); }   /* tile condensed — next bell (or click) expands it */
  button.dup:disabled, button.fork:disabled, button.stash:disabled { opacity:.4; cursor:default; }
  /* Discard-from-Home: only shown while the Home tab is active; accent (not red)
     to set it apart from the process-killing close ✕. */
  button.home-discard { background:none; border:0; padding:0 2px; cursor:pointer;
    color:var(--muted); font-size:14px; line-height:1; display:none; }
  button.home-discard:hover { color:var(--accent); }
  body.home-active .tile button.home-discard { display:inline-block; }
  /* New ring on the open Home tab: slide the card in from the right. */
  @keyframes homeEnter { from { opacity:0; transform:translateX(30px); }
                         to   { opacity:1; transform:none; } }
  .tile.home-enter { animation: homeEnter .26s cubic-bezier(.2,.7,.3,1); }
  /* Stash drawer (header pill + dropdown). Hidden when nothing's stashed. */
  .stash-wrap { position:relative; }
  .stash-wrap[hidden] { display:none; }
  button.stash-pill { background:#1a1f2a; color:var(--muted); border:1px solid var(--border);
    border-radius:6px; padding:4px 9px; font:inherit; cursor:pointer; }
  button.stash-pill:hover { border-color:var(--accent); color:var(--accent); }
  button.stash-pill .n { color:var(--accent); margin-left:5px; font-variant-numeric:tabular-nums; }
  .stash-menu { position:absolute; top:34px; right:0; min-width:280px; max-width:480px; z-index:10;
    background:var(--panel); border:1px solid var(--border); border-radius:6px;
    box-shadow:0 6px 18px #000a; padding:4px 0; display:none;
    max-height:60vh; overflow-y:auto; }
  .stash-menu.open { display:block; }
  .stash-menu .row { display:flex; align-items:center; gap:6px; padding:6px 10px;
    border-bottom:1px solid #1a212c; }
  .stash-menu .row:last-child { border-bottom:0; }
  .stash-menu .row:hover { background:#161c26; }
  .stash-menu .badge { font-size:10px; padding:1px 5px; border-radius:3px;
    border:1px solid var(--border); color:var(--muted); }
  .stash-menu .badge.host { color:var(--host); border-color:#1c3a26; }
  .stash-menu .badge.container { color:var(--container); border-color:#19324a; }
  .stash-menu .badge.terminal { color:#d9c47a; border-color:#3a3219; }
  .stash-menu .badge.container-terminal { color:#7ed1c4; border-color:#1e3a36; }
  .stash-menu .badge.opencode { color:#f78a8a; border-color:#4a1e1e; }
  .stash-menu .badge.codex { color:#6cc7a8; border-color:#1d3a30; }
  .stash-menu .badge.custom { color:#e0a96d; border-color:#473320; }
  .stash-menu .badge.channel  { color:#f0b54e; border-color:#4a3618; }
  .stash-menu .badge.note { color:#f79ac0; border-color:#3a1c2a; }
  .stash-menu .badge.webview { color:#c9a7eb; border-color:#2c1c3a; }
  .stash-menu .nm { font-weight:600; color:var(--fg); flex:0 1 auto; min-width:0;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .stash-menu .cd { color:var(--muted); font-size:11px; flex:1 1 auto; min-width:0;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .stash-menu .row button { background:none; border:0; color:var(--muted);
    cursor:pointer; padding:2px 4px; font-size:14px; line-height:1; }
  .stash-menu .row button.restore:hover { color:var(--accent); }
  .stash-menu .row button.kill:hover { color:#ff6b6b; }
  /* Font picker — swap the terminal face across every tile (header → broadcast
     via postMessage). The <select> inherits the page's monospace stack so it
     looks at home in the toolbar. */
  .font-picker { background:#0d1117; color:var(--fg); border:1px solid var(--border);
    border-radius:6px; padding:3px 6px; font:12px 'JetBrains Mono', ui-monospace, Menlo, monospace;
    cursor:pointer; outline:none; }
  .font-picker:focus { border-color:var(--accent); }
  .stash-menu .empty { padding:10px 12px; color:var(--muted); font-style:italic; }
  .empty { display:flex; height:calc(100vh - 42px); flex-direction:column;
    align-items:center; justify-content:center; gap:10px; color:var(--muted); }
  .empty[hidden] { display:none; }
  code { background:#0006; padding:1px 5px; border-radius:4px; color:var(--fg); }
</style>
<script>/* apply saved theme before first paint → no flash */try{if(localStorage.getItem('claude-sessions-theme')==='light')document.documentElement.classList.add('light')}catch(e){}</script>
</head>
<body>
<header class="bar">
  <span class="dot"></span>
  <h1>Claude Sessions</h1>
  <span class="muted" id="count"></span>
  <span class="spacer"></span>
  <span class="muted">Cmd+E new term &middot; Cmd+T note &middot; Cmd+&larr;/&rarr; reorder &middot; Cmd+X park &middot; Cmd+Shift+E refresh &middot; Ctrl+Q &times;2 closes</span>
  <button class="new" id="searchBtn" title="rebuild the Claude chat search index, then open it">&#128269; Search</button>
  <span class="stash-wrap" id="stashWrap" hidden>
    <button class="stash-pill" id="stashBtn"
      title="stashed sessions — tiles hidden, processes still running">
      Stashed<span class="n" id="stashN">0</span>
    </button>
    <div class="stash-menu" id="stashMenu"></div>
  </span>
  <span class="new-wrap">
    <button class="new" id="newBtn" title="add a new tile">+ New</button>
    <div class="new-menu" id="newMenu">
      <div id="launcherItems"></div>
      <div class="new-sep"></div>
      <button data-kind="terminal">Terminal (zsh in $HOME)</button>
      <button data-kind="container-terminal">Terminal in container</button>
      <button data-kind="webview">Web view…</button>
      <button data-kind="channel">Channel (chatroom)…</button>
      <button data-kind="note">Note (text + images)</button>
      <div class="new-sep"></div>
      <button data-act="manage-launchers" class="new-cfg">⚙ Manage launchers…</button>
    </div>
  </span>
  <span class="ch-wrap">
    <button class="new" id="chBtn" title="recent agent channels">Channels</button>
    <div class="new-menu" id="chMenu"></div>
  </span>
  <span class="set-wrap">
    <button class="new" id="setBtn" title="settings">&#9881;</button>
    <div class="settings-menu" id="settingsMenu">
      <div class="row"><label for="fontSel">Font</label>
        <select id="fontSel" class="font-picker" title="terminal font (applies to every tile)"></select></div>
      <div class="row"><label for="sizeSel">Size</label>
        <select id="sizeSel" class="font-picker" title="terminal font size — Auto uses each font's natural size"></select></div>
      <div class="row"><label for="lineHeightSel">Line height</label>
        <select id="lineHeightSel" class="font-picker" title="terminal line height (line spacing) — Auto is xterm's default 1.0"></select></div>
      <div class="row"><label>Theme</label>
        <button class="new" id="themeBtn" title="toggle light / dark mode">🌙</button></div>
      <div class="row"><label>Bell sound</label>
        <button class="new" id="soundBtn" title="play a sound when a tile rings (Claude bell)">🔔</button></div>
      <div class="row"><label>Keep awake</label>
        <button class="new" id="wakeBtn" title="keep this laptop awake (screen wake lock) while the dashboard is open">😴</button></div>
      <div class="row"><label>Reduced motion</label>
        <button class="new" id="motionBtn" title="reduce animation: tiles open/resize/close/condense without the glide">🎞️</button></div>
      <div class="row sep">
        <button class="new danger" id="restartBtn" title="restart/reload the dashboard server (serve.py)">&#8635; Restart server</button></div>
    </div>
  </span>
</header>
<div id="tabs" hidden></div>
<div id="grid"></div>
<div class="empty" id="empty" hidden>
  <div>No active sessions.</div>
  <div class="muted">Start one from the <strong>+ New</strong> menu above.</div>
</div>
<script>
const CSRF = document.querySelector('meta[name=csrf-token]').content;
// When the dashboard is reached over anything other than localhost (i.e. through
// a reverse proxy such as the nginx https vhost on your-proxy-host), the browser
// can't reach the per-session ttyd at http://127.0.0.1:<port>/ — that address is
// the VIEWER's own machine, and it's plaintext inside an https page (mixed
// content). So in that case we route each terminal tile through the dashboard's
// own /t/<port>/ reverse proxy, which lives at the same (https) origin. On real
// localhost we keep the direct cross-origin embed unchanged.
const PROXY_TTYD = location.hostname !== '127.0.0.1' &&
                   location.hostname !== 'localhost' && location.hostname !== '';
// Public URL prefix when served under a reverse-proxy subpath (serve.py injects
// e.g. '/dash'; empty for a root install). Every root-absolute, same-origin URL
// the page builds must carry it. Rather than touch ~25 call sites, we wrap fetch
// once to prepend BASE to leading-'/' paths; the few non-fetch URLs (iframe src,
// anchor href) prepend BASE explicitly below.
const BASE = "__BASE__";
(function () {
  const _origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    if (typeof input === 'string' && input.charAt(0) === '/' && input.charAt(1) !== '/')
      input = BASE + input;
    return _origFetch(input, init);
  };
})();
// Font picker catalog — emitted by serve.py from the FONTS tuple so the dashboard
// and term-client.js share one source of truth. Each entry shape:
// {id, label, family, size, weight}. id is the localStorage key + postMessage
// payload identifier (stable across label/family renames). See FONTS in serve.py.
// Tile title colour palette. Each tile gets one colour STABLE per its session
// id (so a tile is always the same colour across reloads, polls, and tab
// switches — recognisable at a glance). Hash → palette index via FNV-1a-ish
// over the sid string; cheap, deterministic, no Math.random()-flicker.
// Applied as a CSS variable on the .tile element so `.tile.bell .name`
// (higher specificity) keeps overriding the colour green when a tile rings.
const TITLE_PALETTE = ['#03AED2', '#F8DE22', '#F45B26', '#D12052'];
function tileTitleColor(sid) {
  let h = 2166136261 >>> 0;
  const s = String(sid || '');
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return TITLE_PALETTE[h % TITLE_PALETTE.length];
}

// --- tile glyphs -------------------------------------------------------
// A small Lucide icon per tile makes them recognisable at a glance. The SHAPE
// carries meaning, resolved cheapest-first: a fixed map for kinds whose icon is
// obvious, then a localStorage cache, then an offline keyword regex; only a
// title that matches nothing falls through to the server's cached AI pick. The
// COLOUR is the FNV-1a hash of the seed mapped onto the hue wheel, so two tiles
// that land on the same icon still read apart.
const LUCIDE_ICONS = __LUCIDE_ICONS_JSON__;
const ICON_SIZE = 16;
function iconHue(seed) {
  let h = 2166136261 >>> 0; const s = String(seed || '');
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
  return (h >>> 0) % 360;
}
function iconColor(seed) { return 'hsl(' + iconHue(seed) + ' 60% 64%)'; }
// Channel tiles get ONE fixed identity colour (matching the gold `channel`
// badge, .badge.channel) shared across the title-bar icon, the title-bar name
// and the chatroom header's `.nm` — so a chatroom reads as a coherent unit
// instead of three mismatched colours. Keep in sync with header .nm in the
// /channel page CSS and .badge.channel.
const CHANNEL_TITLE_COLOR = '#f0b54e';
// regex -> icon name, first match wins. Offline; covers the common cases so
// most tiles never hit the API.
const ICON_KEYWORDS = [
  [/dash|board/i, 'layout-dashboard'],
  [/crypto|coin|btc|eth|wallet|trade/i, 'coins'],
  [/jnb|notebook|jupyter|ipynb/i, 'notebook-text'],
  [/apt|dpkg|\bdeb\b|package|\bpkg\b/i, 'package'],
  [/test|spec|pytest|jest/i, 'flask-conical'],
  [/build|make|compile|cargo|gradle/i, 'hammer'],
  [/stm32|\bmcu\b|firmware|embed|\barm\b|esp32/i, 'cpu'],
  [/opencode|vscode|\bcode\b/i, 'code'],
  [/claude|\bllm\b|\bai\b|agent|\bbot\b/i, 'bot'],
  [/podman|docker|container/i, 'container'],
  [/\bgit\b|repo|branch/i, 'git-branch'],
  [/postgres|sqlite|mysql|\bsql\b|database|\bdb\b/i, 'database'],
  [/server|serve|\bapi\b|backend/i, 'server'],
  [/web|http|localhost|:\d{2,5}\b/i, 'globe'],
  [/shell|\bterm\b|\bsh\b|bash|\bzsh\b|console/i, 'terminal'],
];
function keywordIcon(text) {
  for (const [re, name] of ICON_KEYWORDS) if (re.test(text)) return name;
  return null;
}
// kinds whose icon is fixed — no point asking the model.
const KIND_ICON = { webview: 'globe', channel: 'hash', opencode: 'bot', codex: 'sparkles', custom: 'rocket', 'container-terminal': 'box' };
function iconSVG(name, color, size) {
  const inner = LUCIDE_ICONS[name] || LUCIDE_ICONS.terminal;
  // name is whitelist-bounded (a LUCIDE_ICONS key), color is 'hsl(int …)', size
  // is a number — nothing user-controlled reaches this markup.
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" ' +
    'fill="none" stroke="' + color + '" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round">' + inner + '</svg>';
}
const ICON_LS = 'claude-sessions-icon:';   // + normalized title -> resolved name
const _iconInflight = new Set();
// Paint a tile's icon span. seed drives the colour; the name resolves
// kind -> localStorage -> keyword map, with a one-shot AI fallback for unknowns.
function paintIcon(span, seed, title, cwd, kind) {
  const color = kind === 'channel' ? CHANNEL_TITLE_COLOR : iconColor(seed || title || kind);
  const norm = String(title || '').trim().toLowerCase();
  let name = KIND_ICON[kind] || null;
  if (!name && norm) { try { const c = localStorage.getItem(ICON_LS + norm); if (c && LUCIDE_ICONS[c]) name = c; } catch (e) {} }
  if (!name) name = keywordIcon((title || '') + ' ' + (cwd || ''));
  span.innerHTML = iconSVG(name || 'terminal', color, ICON_SIZE);
  span.dataset.title = norm;
  // Unknown title, no kind/cache/keyword hit → ask the server once (it caches
  // per distinct title). 'terminal' is already showing as the placeholder.
  if (!KIND_ICON[kind] && norm && !name && !_iconInflight.has(norm)) {
    _iconInflight.add(norm);
    fetch('/api/icon?csrf=' + encodeURIComponent(CSRF) +
          '&title=' + encodeURIComponent(title) + '&cwd=' + encodeURIComponent(cwd || ''))
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        const n = d && d.icon;
        if (n && LUCIDE_ICONS[n]) {
          try { localStorage.setItem(ICON_LS + norm, n); } catch (e) {}
          if (span.dataset.title === norm) span.innerHTML = iconSVG(n, color, ICON_SIZE);
        }
      })
      .catch(() => {})
      .finally(() => _iconInflight.delete(norm));
  }
}
function makeIconSpan() { const s = document.createElement('span'); s.className = 'icon'; return s; }

const FONTS = __FONTS_JSON__;
const DEFAULT_FONT_ID = '__DEFAULT_FONT_ID__';
const FONT_LSKEY = 'claude-sessions-font';
const SIZE_LSKEY = 'claude-sessions-font-size';
const LINEHEIGHT_LSKEY = 'claude-sessions-line-height';
// xterm lineHeight is a multiplier of the font's natural height (1.0 = default).
const LINE_HEIGHTS = ['', 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8];
// Size selector. '' (the first entry) means "Auto" — use the font catalog's
// natural size for the picked entry. Any other value overrides it. The covered
// range is the same one xterm renders crisply with a bitmap-derived TTF (below
// 9 px most faces become illegible; above 28 px Cozette/Terminus pixels look
// blocky and JBM dominates anyway).
const SIZES = ['', 9, 10, 11, 12, 13, 14, 16, 18, 20, 24, 28];
function getFontEntry(id) { return FONTS.find(f => f.id === id) || FONTS.find(f => f.id === DEFAULT_FONT_ID) || FONTS[0]; }
function currentFontId() {
  try { const v = localStorage.getItem(FONT_LSKEY); if (v && FONTS.some(f => f.id === v)) return v; } catch (e) {}
  return DEFAULT_FONT_ID;
}
function currentSizeOverride() {
  try { const v = localStorage.getItem(SIZE_LSKEY); if (v) { const n = parseInt(v, 10); if (n > 0) return n; } } catch (e) {}
  return 0;   // 0 = Auto / no override
}
function currentLineHeight() {
  try { const v = localStorage.getItem(LINEHEIGHT_LSKEY); if (v) { const n = parseFloat(v); if (n > 0) return n; } } catch (e) {}
  return 0;   // 0 = Auto (xterm default 1.0)
}
// Compose the entry that actually ships down to a tile: font catalog row,
// optionally with the size + line-height overrides layered on top. Always a
// fresh copy (never mutate the FONTS catalog row). Kept as ONE composer so the
// font/size/line-height selectors and the ready-handler push agree byte-for-byte.
function currentEntry() {
  const e = Object.assign({}, getFontEntry(currentFontId()));
  const sz = currentSizeOverride(); if (sz) e.size = sz;
  const lh = currentLineHeight(); if (lh) e.lineHeight = lh;
  return e;
}
// Resolve the iframe's actual origin (from its src) so we can postMessage with
// a PINNED targetOrigin instead of '*'. Using '*' on a message that carries
// secrets (CSRF) is a leak: if the iframe ever navigates elsewhere (about:blank
// after ttyd dies, src override, …) the token reaches whoever's there. Returns
// null if we can't derive the origin — callers must skip the send in that case.
function frameTargetOrigin(f) {
  try { return new URL(f.src).origin; } catch (e) { return null; }
}
function broadcastFont(entry) {
  if (!entry) return;
  for (const el of tiles.values()) {
    const f = el.querySelector('iframe'); if (!f) continue;
    const target = frameTargetOrigin(f); if (!target) continue;
    try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'font', font: entry }, target); } catch (e) {}
  }
}
// Push the light/dark mode to every tile so the terminals theme with the
// dashboard (the iframes are cross-origin, so they can't read our class/state —
// they apply an xterm palette + page background on receipt). Tiles also get the
// current theme on their 'ready' handshake (below), covering ones that mount
// after a toggle.
function broadcastTheme(t) {
  for (const el of tiles.values()) {
    const f = el.querySelector('iframe'); if (!f) continue;
    const target = frameTargetOrigin(f); if (!target) continue;
    try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'theme', theme: t }, target); } catch (e) {}
  }
}
(function setupFontPicker() {
  const sel = document.getElementById('fontSel');
  const sizeSel = document.getElementById('sizeSel');
  const lhSel = document.getElementById('lineHeightSel');
  if (!sel || !sizeSel || !lhSel) return;
  for (const f of FONTS) {
    const o = document.createElement('option');
    o.value = f.id; o.textContent = f.label;
    sel.appendChild(o);
  }
  sel.value = currentFontId();
  for (const s of SIZES) {
    const o = document.createElement('option');
    o.value = String(s); o.textContent = s === '' ? 'Auto' : (s + ' px');
    sizeSel.appendChild(o);
  }
  sizeSel.value = String(currentSizeOverride() || '');
  for (const h of LINE_HEIGHTS) {
    const o = document.createElement('option');
    o.value = String(h); o.textContent = h === '' ? 'Auto' : ('↕ ' + h);
    lhSel.appendChild(o);
  }
  lhSel.value = String(currentLineHeight() || '');
  function pushCurrent() { broadcastFont(currentEntry()); }
  sel.addEventListener('change', () => {
    try { localStorage.setItem(FONT_LSKEY, sel.value); } catch (e) {}
    pushCurrent();
  });
  sizeSel.addEventListener('change', () => {
    try {
      if (sizeSel.value === '') localStorage.removeItem(SIZE_LSKEY);
      else localStorage.setItem(SIZE_LSKEY, sizeSel.value);
    } catch (e) {}
    pushCurrent();
  });
  lhSel.addEventListener('change', () => {
    try {
      if (lhSel.value === '') localStorage.removeItem(LINEHEIGHT_LSKEY);
      else localStorage.setItem(LINEHEIGHT_LSKEY, lhSel.value);
    } catch (e) {}
    pushCurrent();
  });
})();
const grid = document.getElementById('grid');
const emptyEl = document.getElementById('empty');
const countEl = document.getElementById('count');
const tiles = new Map(); // id -> tile element (kept across polls so iframes don't reload)
// Stashed tiles stay in `tiles` but kept ALIVE and hidden (dataset.stashed==='1')
// so the session still receives its BEL and can auto-unstash on a ring — so any
// "how many tiles are really showing" / "is there exactly one tile" logic must
// look past the stashed ones. (See doStash / markBell / applyVisibility.)
function liveTileCount() { let n = 0; for (const [, el] of tiles) if (el.dataset.stashed !== '1') n++; return n; }
function theOnlyLiveTileId() {
  let id = null, n = 0;
  for (const [tid, el] of tiles) if (el.dataset.stashed !== '1') { id = tid; if (++n > 1) return null; }
  return n === 1 ? id : null;
}
// The shown tile whose box is most centered in the viewport — the one the user
// is most likely looking at. Used to anchor a NEW tile next to it (so it lands
// on-screen, in view, instead of appended off the right edge of the scroll row
// where its open animation is invisible). `tab` scopes the candidates to the
// active workdir tab (channel tiles float, so they always qualify); null/​Home
// considers every shown tile. Returns null when nothing qualifies.
function mostVisibleTileId(tab) {
  const vw = window.innerWidth, cx = vw / 2;
  let best = null, bestDist = Infinity;
  for (const [tid, el] of tiles) {
    if (el.dataset.stashed === '1' || el.style.display === 'none') continue;
    if (tab && el.dataset.kind !== 'channel' && el.dataset.tab !== tab) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 1 || r.right <= 0 || r.left >= vw) continue;   // off-screen / collapsed
    const d = Math.abs((r.left + r.right) / 2 - cx);
    if (d < bestDist) { bestDist = d; best = tid; }
  }
  return best;
}
// Ctrl+Tab / Ctrl+Shift+Tab — cycle the selected tile and move keyboard focus to
// it. Visible tiles only (skip stashed / hidden-tab), in display order.
function visibleTilesInOrder() {
  const ordered = orderList.filter(id => tiles.has(id));
  for (const [id] of tiles) if (!ordered.includes(id)) ordered.push(id);
  return ordered.filter(id => {
    const el = tiles.get(id);
    return el && el.dataset.stashed !== '1' && el.offsetParent !== null;
  });
}
function cycleTile(dir) {
  const vis = visibleTilesInOrder();
  if (!vis.length) return;
  let idx = vis.indexOf(selectedId);
  if (idx < 0) idx = dir > 0 ? -1 : 0;     // nothing selected → step in from an end
  const next = vis[(idx + dir + vis.length) % vis.length];
  selectTile(next);
  const el = tiles.get(next);
  if (!el) return;
  releasePin();
  try { el.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'smooth' }); } catch (e) {}
  const f = el.querySelector('iframe');
  if (f) { try { f.focus(); } catch (e) {} try { f.contentWindow.focus(); } catch (e) {} }
  else { try { el.focus(); } catch (e) {} }   // notes/channels have no iframe
}

// --- Horizontal row: start on the LEFT after a (re)load and stay there until
// the user actually intends to scroll. The problem: when terminal iframes
// connect (or the browser restores focus to the terminal that was focused
// before the reload), that focus auto-scrolls the row to reveal the focused
// tile — so a refresh lands on a seemingly random tile instead of the leftmost
// one, and a correctly-persisted tile order LOOKS wrong because the viewport is
// parked in the wrong place. We pin scrollLeft to 0 and snap back any
// programmatic focus-scroll, releasing the pin on the first genuine user
// gesture (wheel / pointer / touch / key) so we never fight real scrolling.
// Native scroll restoration is disabled for the same reason (it would re-apply
// a stale horizontal offset on reload).
try { if ('scrollRestoration' in history) history.scrollRestoration = 'manual'; } catch (e) {}
let pinLeft = true;
// A separate, short-lived "hold" pins the row at a SPECIFIC offset (not 0). When
// a fresh card is revealed on Home, its terminal iframe can grab focus and the
// browser scrolls the row to bring it into view — yanking the viewport out from
// under someone parked mid-row. holdRowScroll() parks scrollLeft for a beat so
// that focus-scroll can't land; it auto-expires and any real gesture lifts it.
let holdScrollX = null;
let _holdTimer = 0;
function releasePin() { pinLeft = false; holdScrollX = null; }
function holdRowScroll(x) {
  holdScrollX = x;
  grid.scrollLeft = x;
  if (_holdTimer) clearTimeout(_holdTimer);
  _holdTimer = setTimeout(() => { holdScrollX = null; }, 500);
}
// A focus-induced scroll fires this; while pinned we undo it. Setting scrollLeft
// to 0 when it's already 0 emits no further event, so this converges.
grid.addEventListener('scroll', () => {
  if (pinLeft) { grid.scrollLeft = 0; return; }
  if (holdScrollX !== null && grid.scrollLeft !== holdScrollX) grid.scrollLeft = holdScrollX;
}, { passive: true });
// These gestures fire BEFORE the scroll/focus they cause, so releasing here
// honors the same gesture rather than swallowing it. No timer: the pin only
// reverts *scroll* (terminal focus/input still work), and we want "start on the
// left" to hold even against a terminal that connects and grabs focus several
// seconds late — the first real scroll gesture lifts it.
for (const ev of ['wheel', 'pointerdown', 'touchstart', 'keydown'])
  window.addEventListener(ev, releasePin, { capture: true, passive: true });
// id -> last live tile title (the program-emitted one posted up by term.html).
// Kept here, not just in the tile's DOM, so the stash drawer can show the real
// session title after the tile has been removed. Backed by localStorage so it
// survives a dashboard reload.
const liveTitles = new Map();
try {
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k && k.indexOf('claude-sessions-title:') === 0) {
      const v = localStorage.getItem(k);
      if (v) liveTitles.set(k.slice('claude-sessions-title:'.length), v);
    }
  }
} catch (e) {}
const tabsEl = document.getElementById('tabs');

// "+ New" menu: spawn a plain terminal (ttyd + zsh) or register a webview
// (iframe to any URL with an editable address bar). Both land as tiles via
// the same registry mechanism that backs claude sessions.
const newBtn = document.getElementById('newBtn');
const newMenu = document.getElementById('newMenu');
// Close every header dropdown (+ New, Channels, settings) except `keep`. Used so
// opening one closes the others, an outside click closes all, and — crucially —
// clicking INTO a tile iframe closes them too: iframe clicks never reach the
// parent document's click listeners, but they blur the top window (caught below).
function closeMenus(keep) {
  if (keep !== 'new') newMenu.classList.remove('open');
  const cm = document.getElementById('chMenu');
  if (cm && keep !== 'ch') cm.classList.remove('open');
  const sm = document.getElementById('settingsMenu');
  if (sm && keep !== 'set') sm.classList.remove('open');
}
newBtn.onclick = (e) => {
  e.stopPropagation();
  const opening = !newMenu.classList.contains('open');
  closeMenus('new');
  newMenu.classList.toggle('open');
  if (opening) renderLauncherMenu();   // refresh launchers each time it opens
};
document.addEventListener('click', (e) => {
  if (!newMenu.contains(e.target) && e.target !== newBtn) newMenu.classList.remove('open');
});
// Focus leaving the top window means a click landed in a tile iframe (or the user
// tabbed away) — close the dropdowns, since their own outside-click listeners
// can't see clicks inside cross-document iframes.
window.addEventListener('blur', () => closeMenus());

// The configurable agent launchers shown at the top of "+ New". Fetched from
// /api/launchers (which falls back to common claude/codex/opencode presets on a
// fresh install) and rendered as buttons; a coloured dot hints the program.
const launcherItems = document.getElementById('launcherItems');
let launcherCache = [];
const LAUNCHER_DOT = { claude: '#b9a6f0', codex: '#6cc7a8', opencode: '#f78a8a' };
function launcherProg(cmd) {
  const m = (cmd || '').trim().match(/(?:^|\/)([A-Za-z0-9._-]+)/);
  return m ? m[1] : '';
}
async function renderLauncherMenu() {
  try {
    const r = await fetch('/api/launchers', { cache: 'no-store' });
    const d = await r.json();
    launcherCache = Array.isArray(d.launchers) ? d.launchers : [];
  } catch (e) { /* keep last cache on failure */ }
  launcherItems.innerHTML = '';
  for (const l of launcherCache) {
    const b = document.createElement('button');
    b.dataset.kind = 'launcher';
    b.dataset.launcherId = l.id;
    b.title = l.command + (l.provider === 'vertex' ? '   [Vertex AI]' : '')
            + (l.env ? '   [custom env]' : '');
    const dot = document.createElement('span');
    dot.className = 'lc-dot';
    dot.style.background = LAUNCHER_DOT[launcherProg(l.command)] || 'var(--muted)';
    b.appendChild(dot);
    b.appendChild(document.createTextNode(l.label));
    launcherItems.appendChild(b);
  }
}

// Settings (gear) menu — holds font/size/line-height pickers, the theme and
// bell-sound toggles, and Restart server. The controls inside keep their
// original ids, so their existing handlers (further down) bind unchanged; this
// block only manages open/close and the restart action. Clicks inside the menu
// don't close it (so you can flip a toggle and watch its icon update live).
const setBtn = document.getElementById('setBtn');
const settingsMenu = document.getElementById('settingsMenu');
if (setBtn && settingsMenu) {
  setBtn.onclick = (e) => { e.stopPropagation(); closeMenus('set'); settingsMenu.classList.toggle('open'); };
  document.addEventListener('click', (e) => {
    if (!settingsMenu.contains(e.target) && e.target !== setBtn) settingsMenu.classList.remove('open');
  });
}
const restartBtn = document.getElementById('restartBtn');
if (restartBtn) restartBtn.onclick = async () => {
  if (!confirm('Restart the dashboard server (serve.py)?\nRunning tiles keep their processes; the page reconnects automatically once the server is back.')) return;
  const label = restartBtn.textContent;
  restartBtn.disabled = true;
  restartBtn.textContent = '↻ Restarting…';
  try { await fetch('/api/restart', { method: 'POST', headers: { 'X-CSRF-Token': CSRF } }); } catch (e) {}
  // serve.py re-execs (~0.3 s). Poll /api/sessions until it answers again, then
  // reload so the page picks up the fresh build and re-attaches to the tiles.
  for (let i = 0; i < 80; i++) {
    await new Promise(r => setTimeout(r, 500));
    try {
      const r = await fetch('/api/sessions', { cache: 'no-store' });
      if (r.ok) { location.reload(); return; }
    } catch (e) {}
  }
  restartBtn.disabled = false;
  restartBtn.textContent = label;
  alert('Server did not come back within 40 s — check the serve.py logs.');
};
async function spawnTile(kind, opts) {
  opts = opts || {};
  let url = '/api/new?kind=' + encodeURIComponent(kind);
  // Home isn't a real workdir — spawning from it behaves like the untabbed case
  // (backend default dir, anchor placement on the current selection).
  const tab = activeTab === HOME_KEY ? null : activeTab;
  // Land the new tile in the user's currently-visible tab: terminals use
  // the tab as their spawn cwd, webviews carry it as a tab-grouping hint.
  // When fewer than 3 tiles are open there are no tabs (tab is null),
  // so we just fall back to the backend defaults ($HOME / no cwd).
  if (tab) url += '&cwd=' + encodeURIComponent(tab);
  // kind=launcher spawns a configured launcher preset (claude/codex/opencode/
  // custom command) by its id; the backend looks the command + env up.
  if (kind === 'launcher' && opts.launcherId) url += '&id=' + encodeURIComponent(opts.launcherId);
  if (kind === 'webview') {
    const u = opts.url || prompt('URL to open in the new tile:', 'https://');
    if (!u || u.trim() === '' || u.trim() === 'https://') return;
    url += '&url=' + encodeURIComponent(u.trim());
  } else if (kind === 'channel') {
    // Channel tiles need a channel name. opts.name comes from the
    // Channels menu (clicking a recent channel); without it we prompt
    // (the "+ New → Channel (chatroom)…" path). Name must match
    // /^[A-Za-z0-9_-]+$/ — the backend rejects anything else with 400,
    // but a friendly prompt-time check spares the user a network round-trip.
    let nm = opts.name;
    if (!nm) nm = (prompt('Channel name (letters/digits/_-, no spaces):', '') || '').trim();
    if (!nm || !/^[A-Za-z0-9_-]+$/.test(nm)) return;
    url += '&name=' + encodeURIComponent(nm);
  }
  // Register a pendingDup so the new tile lands right after the currently
  // focused/selected tile (same machinery the duplicate button uses). Without
  // this the new session lands at the end of the row, which feels backward
  // for "Cmd+E to open another shell next to this one". Falls through to
  // appended-at-end if no tile is selected or the match window expires.
  // In a tab, only anchor placement on the selected tile if it lives in the
  // ACTIVE tab — otherwise (e.g. opening into an empty favorite tab) the new
  // tile would be ordered next to a tile in some other tab. Untabbed
  // (activeTab null), keep the old behavior of anchoring on the selection.
  const sel = selectedId ? tiles.get(selectedId) : null;
  // Anchor placement on the selected tile (if it's in this tab), else on the
  // tile the user is most likely looking at — the most-centered visible one — so
  // the new tile lands ON-SCREEN next to it (and its open animation is seen)
  // rather than appended off the right edge. theOnlyLiveTileId is the trivial
  // single-tile case.
  const srcId = (sel && (!tab || sel.dataset.tab === tab)) ? selectedId
              : (mostVisibleTileId(tab) || theOnlyLiveTileId());
  releasePin();   // a deliberate spawn may scroll the row to reveal the new tile — lift the load-time left-pin
  if (srcId) {
    pendingDups.push({ srcId: srcId, cwd: tab || '', until: Date.now() + 90000 });
  }
  try { await fetch(url, { method: 'POST', headers: { 'X-CSRF-Token': CSRF } }); } catch (e2) {}
  // A new terminal needs ~1 s for ttyd to bind; webviews appear immediately on the next poll.
  setTimeout(poll, 200); setTimeout(poll, 900); setTimeout(poll, 1800);
}
// "Choose from common configurations" — prefills for the launcher editor,
// including custom endpoint providers (Google Vertex, an Anthropic-compatible
// proxy, an OpenAI-compatible base for codex).
const LAUNCHER_TEMPLATES = [
  { label: 'Claude', command: 'claude' },
  { label: 'Claude (skip-perms)', command: 'claude --dangerously-skip-permissions' },
  { label: 'Claude (Haiku)', command: 'claude --model haiku' },
  { label: 'Claude (Opus)', command: 'claude --model opus' },
  { label: 'Claude — Google Vertex AI', command: 'claude', provider: 'vertex' },
  { label: 'Claude — custom endpoint', command: 'claude',
    env: { ANTHROPIC_BASE_URL: 'https://your-proxy.example', ANTHROPIC_AUTH_TOKEN: 'sk-...' } },
  { label: 'Codex (ChatGPT)', command: 'codex' },
  { label: 'Codex (bypass sandbox)', command: 'codex --dangerously-bypass-approvals-and-sandbox' },
  { label: 'Codex — custom endpoint', command: 'codex',
    env: { OPENAI_BASE_URL: 'https://your-endpoint.example/v1', OPENAI_API_KEY: 'sk-...' } },
  { label: 'opencode', command: 'opencode' },
];
function envToText(env) {
  return env ? Object.entries(env).map(([k, v]) => k + '=' + v).join('\n') : '';
}
function textToEnv(t) {
  const o = {};
  (t || '').split('\n').forEach((line) => {
    line = line.trim();
    if (!line || line[0] === '#') return;
    const i = line.indexOf('=');
    if (i < 1) return;
    const k = line.slice(0, i).trim();
    if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(k)) o[k] = line.slice(i + 1);
  });
  return Object.keys(o).length ? o : undefined;
}

// The "Manage launchers…" editor: a modal listing every launcher as an editable
// row (label, command, optional Vertex toggle + env vars). Add/remove rows, seed
// from a template, then Save (POST /api/launchers) re-renders the + New menu.
let launcherModal = null;
function openLauncherManager() {
  if (launcherModal) launcherModal.remove();
  let rows = (launcherCache || []).map((l) => ({
    label: l.label || '', command: l.command || '',
    provider: l.provider === 'vertex', env: envToText(l.env),
  }));

  const back = document.createElement('div');
  back.className = 'modal-backdrop';
  const modal = document.createElement('div');
  modal.className = 'modal lmgr';
  back.appendChild(modal);

  function readRowsFromDom() {
    const out = [];
    modal.querySelectorAll('.lrow').forEach((el) => {
      out.push({
        label: el.querySelector('.l-label').value,
        command: el.querySelector('.l-cmd').value,
        provider: el.querySelector('.l-vertex').checked,
        env: el.querySelector('.l-env').value,
      });
    });
    return out;
  }

  function drawRows() {
    modal.innerHTML = '';
    const h = document.createElement('div');
    h.className = 'modal-head';
    h.innerHTML = '<span>Launchers</span>';
    const x = document.createElement('button');
    x.className = 'modal-x'; x.textContent = '✕'; x.title = 'Close';
    x.onclick = close; h.appendChild(x);
    modal.appendChild(h);

    const help = document.createElement('div');
    help.className = 'modal-help';
    help.textContent = 'Each launcher is a command line. Start it with claude, codex or '
      + 'opencode to keep fork/resume/chat; anything else runs as a plain command. '
      + 'Add env vars to point at a custom endpoint provider.';
    modal.appendChild(help);

    const list = document.createElement('div');
    list.className = 'lrows';
    rows.forEach((r, idx) => list.appendChild(rowEl(r, idx)));
    modal.appendChild(list);

    const foot = document.createElement('div');
    foot.className = 'modal-foot';
    const add = document.createElement('button');
    add.className = 'btn'; add.textContent = '+ Add launcher';
    add.onclick = () => { rows = readRowsFromDom(); rows.push({ label: '', command: '', provider: false, env: '' }); drawRows(); };
    const tplWrap = document.createElement('span'); tplWrap.className = 'tpl-wrap';
    const tpl = document.createElement('select');
    tpl.className = 'btn';
    tpl.innerHTML = '<option value="">+ From template…</option>'
      + LAUNCHER_TEMPLATES.map((t, i) => '<option value="' + i + '">' + t.label + '</option>').join('');
    tpl.onchange = () => {
      const t = LAUNCHER_TEMPLATES[tpl.value]; if (!t) return;
      rows = readRowsFromDom();
      rows.push({ label: t.label, command: t.command, provider: t.provider === 'vertex', env: envToText(t.env) });
      drawRows();
    };
    tplWrap.appendChild(tpl);
    const spacer = document.createElement('span'); spacer.style.flex = '1';
    const cancel = document.createElement('button');
    cancel.className = 'btn'; cancel.textContent = 'Cancel'; cancel.onclick = close;
    const save = document.createElement('button');
    save.className = 'btn primary'; save.textContent = 'Save';
    save.onclick = lmgrSave;
    foot.append(add, tplWrap, spacer, cancel, save);
    modal.appendChild(foot);
  }

  function rowEl(r, idx) {
    const el = document.createElement('div'); el.className = 'lrow';
    const top = document.createElement('div'); top.className = 'lrow-top';
    const label = document.createElement('input');
    label.className = 'l-label'; label.placeholder = 'Label'; label.value = r.label;
    const cmd = document.createElement('input');
    cmd.className = 'l-cmd'; cmd.placeholder = 'Command (e.g. claude --dangerously-skip-permissions)';
    cmd.value = r.command;
    const del = document.createElement('button');
    del.className = 'l-del'; del.textContent = '🗑'; del.title = 'Remove';
    del.onclick = () => { rows = readRowsFromDom(); rows.splice(idx, 1); drawRows(); };
    top.append(label, cmd, del);
    el.appendChild(top);

    const adv = document.createElement('div'); adv.className = 'lrow-adv';
    const vlab = document.createElement('label'); vlab.className = 'l-vchk';
    const vchk = document.createElement('input'); vchk.type = 'checkbox';
    vchk.className = 'l-vertex'; vchk.checked = !!r.provider;
    vlab.append(vchk, document.createTextNode(' Google Vertex AI (gcloud ADC)'));
    adv.appendChild(vlab);
    const env = document.createElement('textarea');
    env.className = 'l-env'; env.placeholder = 'Environment (KEY=value per line) — e.g. ANTHROPIC_BASE_URL=https://…';
    env.rows = r.env ? Math.min(6, r.env.split('\n').length + 1) : 2;
    env.value = r.env;
    adv.appendChild(env);
    el.appendChild(adv);
    return el;
  }

  async function lmgrSave() {
    const payload = readRowsFromDom().map((r) => {
      const o = { label: r.label.trim(), command: r.command.trim() };
      if (r.provider) o.provider = 'vertex';
      const env = textToEnv(r.env);
      if (env) o.env = env;
      return o;
    }).filter((r) => r.label && r.command);
    try {
      const r = await fetch('/api/launchers', {
        method: 'POST',
        headers: { 'X-CSRF-Token': CSRF, 'Content-Type': 'application/json' },
        body: JSON.stringify({ launchers: payload }),
      });
      const d = await r.json();
      if (d && Array.isArray(d.launchers)) launcherCache = d.launchers;
    } catch (e) { alert('Could not save launchers.'); return; }
    renderLauncherMenu();
    close();
  }

  function close() { back.remove(); launcherModal = null; }
  back.addEventListener('click', (e) => { if (e.target === back) close(); });
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape' && launcherModal === back) { close(); document.removeEventListener('keydown', esc); }
  });

  drawRows();
  document.body.appendChild(back);
  launcherModal = back;
  const first = modal.querySelector('.l-label');
  if (first) first.focus();
}

newMenu.addEventListener('click', (e) => {
  const act = e.target.closest('button[data-act]');
  if (act && act.dataset.act === 'manage-launchers') {
    newMenu.classList.remove('open');
    openLauncherManager();
    return;
  }
  const btn = e.target.closest('button[data-kind]'); if (!btn) return;
  newMenu.classList.remove('open');
  spawnTile(btn.dataset.kind, { launcherId: btn.dataset.launcherId });
});

// Channels menu: lists recent agent channels from /api/channels (read off
// /tmp/claude-channels/*.ndjson by the dashboard). Click a row → spawn a
// chatroom tile for that channel. Refreshes the list each time the menu
// opens so newly-created channels (from other claude sessions running the
// channel skill) show up without a dashboard reload.
const chBtn = document.getElementById('chBtn');
const chMenu = document.getElementById('chMenu');
async function renderChannelsMenu() {
  chMenu.innerHTML = '<div class="muted" style="padding:6px 10px">loading…</div>';
  let data = { channels: [] };
  try {
    const r = await fetch('/api/channels');
    if (r.ok) data = await r.json();
  } catch (e) {}
  chMenu.innerHTML = '';
  if (!data.channels.length) {
    const e = document.createElement('div');
    e.className = 'muted';
    e.style.cssText = 'padding:6px 10px;font-style:italic';
    e.textContent = 'no channels yet — use the `channel` skill to start one';
    chMenu.appendChild(e);
  } else {
    for (const c of data.channels) {
      const b = document.createElement('button');
      b.textContent = '#' + c.name;
      const meta = document.createElement('span');
      meta.className = 'muted';
      meta.style.cssText = 'margin-left:10px;font-size:11px';
      meta.textContent = c.count + ' msg';
      b.appendChild(meta);
      b.onclick = () => { chMenu.classList.remove('open'); spawnTile('channel', { name: c.name }); };
      chMenu.appendChild(b);
    }
  }
  const newBtn = document.createElement('button');
  newBtn.textContent = '+ new channel…';
  newBtn.style.borderTop = '1px solid var(--border)';
  newBtn.onclick = () => { chMenu.classList.remove('open'); spawnTile('channel'); };
  chMenu.appendChild(newBtn);
}
chBtn.onclick = (e) => {
  e.stopPropagation();
  closeMenus('ch');
  const willOpen = !chMenu.classList.contains('open');
  chMenu.classList.toggle('open');
  if (willOpen) renderChannelsMenu();
};
document.addEventListener('click', (e) => {
  if (!chMenu.contains(e.target) && e.target !== chBtn) chMenu.classList.remove('open');
});
// Bright/dark mode. The saved theme is applied to <html> by a tiny pre-paint
// script in <head> (so there's no flash); here we just keep the toggle button's
// --- Claude chat search ---
// Rebuild the full-text search index of every Claude chat (runs
// claude-chat-export.py server-side) then open the generated index.html. The
// dashboard serves the output dir at /chat-history/ so it loads over http
// (file:// is blocked from an http page). The export can take a few seconds —
// the button shows progress and re-enables when done.
const searchBtn = document.getElementById('searchBtn');
if (searchBtn) searchBtn.onclick = async () => {
  const label = searchBtn.textContent;
  searchBtn.disabled = true;
  searchBtn.textContent = '⏳ Exporting…';
  // The search index lives at the dashboard's OWN origin. Use an ABSOLUTE url:
  // a root-relative '/chat-history/…' can't resolve against the about:blank we
  // pre-open below (about:blank has no origin/authority), which left the popup
  // blank — the reported bug.
  const target = location.origin + BASE + '/chat-history/index.html';
  // Pre-open the window synchronously (inside the click gesture) so the popup
  // blocker lets it through, and show a placeholder while the export runs (it
  // can take a few seconds) instead of a blank window.
  const w = window.open('about:blank', 'claude-chat-search');
  if (w) { try {
    w.document.write('<!doctype html><meta charset=utf8><title>Claude chat search</title>' +
      '<body style="font:14px/1.5 system-ui,sans-serif;margin:0;padding:2.5rem;color:#9aa4b2;background:#1c1e26">' +
      'Building the Claude chat search index…</body>');
    w.document.close();
  } catch (e) {} }
  function fail(msg) {
    if (w) { try { w.document.body.textContent = msg; return; } catch (e) {} }
    alert(msg);
  }
  try {
    const r = await fetch('/api/chat-export', { method: 'POST', headers: { 'X-CSRF-Token': CSRF } });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok) {
      if (w) w.location.href = target; else window.open(target, 'claude-chat-search');
    } else {
      fail('Chat export failed: ' + (d.error || ('HTTP ' + r.status)));
    }
  } catch (e) {
    fail('Chat export request failed: ' + e);
  } finally {
    searchBtn.disabled = false;
    searchBtn.textContent = label;
  }
};

// glyph in sync and persist clicks. Per browser, like the font/tab state.
const themeBtn = document.getElementById('themeBtn');
let theme = 'dark';
try { theme = localStorage.getItem('claude-sessions-theme') || 'dark'; } catch (e) {}
function applyTheme(t) {
  const light = t === 'light';
  document.documentElement.classList.toggle('light', light);
  if (themeBtn) {
    themeBtn.textContent = light ? '☀' : '🌙';
    themeBtn.title = light ? 'switch to dark mode' : 'switch to light mode';
  }
}
applyTheme(theme);
if (themeBtn) themeBtn.onclick = () => {
  theme = theme === 'light' ? 'dark' : 'light';
  try { localStorage.setItem('claude-sessions-theme', theme); } catch (e) {}
  applyTheme(theme);        // chrome
  broadcastTheme(theme);    // terminals
};

// --- bell sound ---
// A tile rings (markBell) when Claude wants attention — a permission prompt, a
// finished turn, or a manual BEL from a terminal. The flash on the tile/tab is
// easy to miss when the dashboard isn't in front of you, so optionally play a
// short chime too. Synthesised with WebAudio (no asset to ship / 404). Default
// ON; persisted; muteable from the header. Browsers block audio until the page
// has had a user gesture, so the first chime may be silent until you've clicked
// once — that's a browser policy, not a bug.
const soundBtn = document.getElementById('soundBtn');
let soundOn = true;
try { soundOn = localStorage.getItem('claude-sessions-bell-sound') !== 'off'; } catch (e) {}
function applySoundBtn() {
  if (!soundBtn) return;
  soundBtn.textContent = soundOn ? '🔔' : '🔕';
  soundBtn.title = soundOn ? 'bell sound on — click to mute' : 'bell sound muted — click to enable';
  soundBtn.classList.toggle('off', !soundOn);
}
applySoundBtn();
if (soundBtn) soundBtn.onclick = () => {
  soundOn = !soundOn;
  try { localStorage.setItem('claude-sessions-bell-sound', soundOn ? 'on' : 'off'); } catch (e) {}
  applySoundBtn();
  if (soundOn) playBell();   // confirmation chime when (re)enabling, also primes the AudioContext
};

// --- keep awake (screen wake lock) ---
// Hold a Screen Wake Lock while enabled so the laptop display (and, with the lid
// open, the machine itself) doesn't sleep while you're watching long-running
// agents. The browser auto-releases the lock whenever the tab is hidden or the
// device sleeps anyway, so we re-acquire on visibilitychange. State is persisted
// and restored on load. Requires a secure context (https / localhost); on
// unsupported browsers the button disables itself.
const wakeBtn = document.getElementById('wakeBtn');
let wakeOn = false;
try { wakeOn = localStorage.getItem('claude-sessions-keep-awake') === 'on'; } catch (e) {}
let _wakeLock = null;
const wakeSupported = ('wakeLock' in navigator);
function applyWakeBtn() {
  if (!wakeBtn) return;
  if (!wakeSupported) {
    wakeBtn.textContent = '🚫';
    wakeBtn.title = 'wake lock not supported by this browser';
    wakeBtn.disabled = true;
    return;
  }
  const active = !!_wakeLock;
  wakeBtn.textContent = wakeOn ? '☕' : '😴';
  wakeBtn.title = wakeOn
    ? (active ? 'keeping the laptop awake — click to allow sleep' : 'keep-awake on (re-acquires when tab is focused) — click to allow sleep')
    : 'laptop may sleep — click to keep it awake';
  wakeBtn.classList.toggle('off', !wakeOn);
}
async function acquireWakeLock() {
  if (!wakeSupported || !wakeOn || _wakeLock) return;
  if (document.visibilityState !== 'visible') return;   // can only acquire while visible
  try {
    _wakeLock = await navigator.wakeLock.request('screen');
    _wakeLock.addEventListener('release', () => { _wakeLock = null; applyWakeBtn(); });
  } catch (e) { _wakeLock = null; }   // e.g. low battery — leave wakeOn so we retry on next focus
  applyWakeBtn();
}
async function releaseWakeLock() {
  if (!_wakeLock) return;
  try { await _wakeLock.release(); } catch (e) {}
  _wakeLock = null;
  applyWakeBtn();
}
if (wakeBtn) wakeBtn.onclick = () => {
  if (!wakeSupported) return;
  wakeOn = !wakeOn;
  try { localStorage.setItem('claude-sessions-keep-awake', wakeOn ? 'on' : 'off'); } catch (e) {}
  if (wakeOn) acquireWakeLock(); else releaseWakeLock();
  applyWakeBtn();
};
// The browser drops the lock when the tab is hidden; re-grab it when we're back.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') acquireWakeLock();
});
applyWakeBtn();
if (wakeOn) acquireWakeLock();

// --- reduced motion ---
// An explicit, dashboard-level toggle (NOT the OS prefers-reduced-motion, which
// we deliberately stopped honoring — see flipCapture). ON → tiles open/resize/
// close/condense WITHOUT the glide. `reducedMotion` gates the JS FLIP animations
// (flipCapture/flipEnter read it) and toggles `body.reduce-motion`, whose CSS
// zeroes the .tile transitions and makes the close fold instant. Default OFF.
const motionBtn = document.getElementById('motionBtn');
let reducedMotion = false;
try { reducedMotion = localStorage.getItem('claude-sessions-reduced-motion') === 'on'; } catch (e) {}
function applyReducedMotion() {
  document.body.classList.toggle('reduce-motion', reducedMotion);
  if (motionBtn) {
    motionBtn.textContent = reducedMotion ? '⏸️' : '🎞️';
    motionBtn.title = reducedMotion
      ? 'reduced motion ON — tiles snap with no animation; click to re-enable motion'
      : 'reduce animation: tiles open/resize/close/condense without the glide';
    motionBtn.classList.toggle('off', reducedMotion);
  }
}
applyReducedMotion();
if (motionBtn) motionBtn.onclick = () => {
  reducedMotion = !reducedMotion;
  try { localStorage.setItem('claude-sessions-reduced-motion', reducedMotion ? 'on' : 'off'); } catch (e) {}
  applyReducedMotion();
};
let _audioCtx = null;
let _lastChimeAt = 0;        // throttle: many tiles can ring in the same instant
function playBell() {
  if (!soundOn) return;
  const now = (performance && performance.now) ? performance.now() : Date.now();
  if (now - _lastChimeAt < 250) return;   // coalesce a burst into one chime
  _lastChimeAt = now;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!_audioCtx) _audioCtx = new AC();
    if (_audioCtx.state === 'suspended') _audioCtx.resume();
    const ctx = _audioCtx, t0 = ctx.currentTime;
    // Two short descending sine blips — distinct from a system beep, soft enough
    // not to be jarring when several land at once.
    [[880, 0], [660, 0.12]].forEach(([freq, dt]) => {
      const osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.type = 'sine'; osc.frequency.value = freq;
      const s = t0 + dt;
      gain.gain.setValueAtTime(0.0001, s);
      gain.gain.exponentialRampToValueAtTime(0.18, s + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, s + 0.11);
      osc.connect(gain).connect(ctx.destination);
      osc.start(s); osc.stop(s + 0.12);
    });
  } catch (e) {}
}

// --- macOS Dock badge (installed Chrome app / PWA) ---
// When the dashboard runs as a Chrome app ("open as window") the Badging API
// paints a red count on its Dock icon — so a ring is noticeable even when the
// window is unfocused or behind others. The count is the number of tiles
// currently flagged (unacknowledged bells); viewing a tile/tab clears its flag
// and we recount, clearing the badge once nothing is ringing. No-op in a plain
// browser tab (setAppBadge absent / throws) — purely additive to the in-page
// flash + chime.
function updateDockBadge() {
  if (!('setAppBadge' in navigator)) return;
  let n = 0;
  for (const [, el] of tiles) if (el.classList.contains('bell')) n++;
  try { if (n > 0) navigator.setAppBadge(n); else navigator.clearAppBadge(); } catch (e) {}
}

let activeTab = null;        // currently shown tab key (a base workdir)
// Remember each tab's horizontal scroll offset so switching away and back lands
// where you left it, instead of snapping the row to its leftmost tile. Keyed by
// tab key (HOME_KEY for Home); in-memory, reset on reload.
const tabScroll = new Map();
function rememberTabScroll() { if (activeTab != null) tabScroll.set(activeTab, grid.scrollLeft); }
function restoreTabScroll(key) { grid.scrollLeft = tabScroll.get(key) || 0; }
let homeDir = '';
let lastLayoutSessions = []; // sessions from the last layoutTabs() — replayed when
                             // a favorite toggle needs to re-group without a poll
// Remember the last-viewed tab across reloads, so a refresh lands back on the
// tab you were on rather than snapping to the first one.
let savedTab = null;
try { savedTab = localStorage.getItem('claude-sessions-active-tab') || null; } catch (e) {}
function saveActiveTab() { try { localStorage.setItem('claude-sessions-active-tab', activeTab || ''); } catch (e) {} }

// --- Home tab ("needs attention") ------------------------------------------
// A synthetic tab, pinned first in the bar, that gathers every tile currently
// ringing (the .bell class — a permission prompt, a finished turn, …) no matter
// which workdir tab it lives in. It's a triage view: glance here, click into the
// tile that wants you, answering it clears its bell. Its key can't collide with
// a real tab — those are absolute cwd paths.
const HOME_KEY = '::home::';
// Tiles surfaced on Home. Seeded from the currently-ringing tiles each time Home
// is opened, plus any tile that rings while Home is showing. A tile stays listed
// even after you answer it (its bell clears) until you leave and reopen Home — so
// working through the batch doesn't make rows vanish under the cursor. Rebuilt
// from scratch on entry, so it never carries stale (closed) ids across visits.
let homeShown = new Set();
// Set insertion order IS the on-screen order: newest ring sits last → rightmost.
// A discarded tile that rings again is re-inserted at the end, so it slides back
// in on the right. ids of tiles that rang while Home was open, awaiting their
// slide-in animation on the next applyVisibility.
let pendingHomeEnter = new Set();
let wasHome = false;   // were we on Home last applyVisibility? (restore order on leave)
function rebuildHomeShown() {
  homeShown = new Set();
  for (const [id, el] of tiles)
    if (el.dataset.stashed !== '1' && el.classList.contains('bell')) homeShown.add(id);
}
// Count of tiles wanting attention right now — drives the Home tab's count badge
// and glow (same semantics as the Dock badge: unacknowledged bells).
function homeBellCount() {
  let n = 0;
  for (const [, el] of tiles)
    if (el.dataset.stashed !== '1' && el.classList.contains('bell')) n++;
  return n;
}
// Order the Home cards by ring-arrival (homeShown insertion order) so new ones
// land on the right. Overrides the manual tile order while Home is active;
// applyOrder() restores the manual order when we leave Home.
function applyHomeOrder() {
  let i = 0;
  for (const id of homeShown) { const el = tiles.get(id); if (el) el.style.order = i; i++; }
}
// Slide the freshly-rung cards in from the right. Only fires for tiles that rang
// while Home was the active tab (collected in pendingHomeEnter); opening Home
// onto an existing batch doesn't re-animate the lot.
function flushHomeEnter() {
  if (activeTab !== HOME_KEY) { pendingHomeEnter.clear(); return; }
  for (const id of pendingHomeEnter) {
    const el = tiles.get(id);
    if (el && el.style.display !== 'none') {
      el.classList.remove('home-enter');
      void el.offsetWidth;   // restart the animation if it's mid-flight
      el.classList.add('home-enter');
      el.addEventListener('animationend', () => el.classList.remove('home-enter'), { once: true });
    }
  }
  pendingHomeEnter.clear();
}
// Keep the Home tab's count/glow in sync after any bell add or clear, and — when
// Home is the active tab — fold a fresh ring into the shown set (queuing its
// slide-in) and re-filter the grid so it appears live. Safe to call on any bell
// change.
function updateHome() {
  const n = homeBellCount();
  let added = false;   // did a NEW card just enter the Home batch this call?
  if (activeTab === HOME_KEY) {
    for (const [id, el] of tiles)
      if (el.dataset.stashed !== '1' && el.classList.contains('bell') && !homeShown.has(id)) {
        homeShown.add(id);          // newest → end of the set → rightmost slot
        pendingHomeEnter.add(id);   // …and slide it in
        added = true;
      }
  }
  for (const b of tabsEl.children) {
    if (b.dataset.key !== HOME_KEY) continue;
    b.classList.toggle('bell', n > 0);
    const cnt = b.querySelector('.n');
    if (cnt) { cnt.textContent = String(n); cnt.style.display = n > 0 ? '' : 'none'; }
  }
  if (activeTab === HOME_KEY) {
    // A fresh card landing must not scroll the row out from under the user. Note
    // where they're parked first; restore it after the relayout (and hold it for
    // a beat so the revealed tile's focus-scroll can't yank it). If they were at
    // the right edge — watching the newest — follow to the new end instead, so
    // the incoming card is visible. pinLeft already owns "stay at 0" post-load.
    const atEnd = added && (grid.scrollWidth - grid.clientWidth - grid.scrollLeft) <= 4;
    const prevLeft = grid.scrollLeft;
    applyVisibility();
    flushHomeEnter();
    if (added && !pinLeft)
      holdRowScroll(atEnd ? Math.max(0, grid.scrollWidth - grid.clientWidth) : prevLeft);
  }
}
// Dismiss a card from Home until its tile next needs attention: drop it from the
// batch and clear its (already-seen) bell so updateHome won't re-add it. The next
// ring re-inserts it at the right via markBell → updateHome.
function discardFromHome(id) {
  const el = tiles.get(id); if (!el) return;
  el.classList.remove('bell');
  homeShown.delete(id);
  pendingHomeEnter.delete(id);
  refreshTabBell(el.dataset.tab);
  updateDockBadge();
  updateHome();   // refresh count/glow + re-filter the grid (this card drops out)
}
// The Home tab button — pinned first, not draggable, no pin/favorite slot.
function buildHomeTab() {
  const b = document.createElement('button');
  b.className = 'tab home';
  b.dataset.key = HOME_KEY;
  b.title = 'Home — tiles that need attention (a ringing bell: permission prompt, finished turn)';
  const label = document.createElement('span'); label.className = 'home-ic'; label.textContent = '🏠';
  // Plain wrapper (no .cntslot) — Home has no pin, so the count shouldn't hide on hover.
  const slot = document.createElement('span');
  const cnt = document.createElement('span'); cnt.className = 'n';
  slot.append(cnt);
  b.append(label, slot);
  b.onclick = () => {
    if (activeTab === HOME_KEY) return;
    rememberTabScroll();   // stash where we left the current tab
    activeTab = HOME_KEY;
    savedTab = null;
    saveActiveTab();
    rebuildHomeShown();   // snapshot the current batch of rings to work through
    updateHome();         // paint the count, then applyVisibility for the grid
    restoreTabScroll(HOME_KEY);
  };
  return b;
}

// Manual tile order (drag-to-rearrange), persisted across reloads. Tiles are
// positioned with CSS `order` rather than by moving DOM nodes — moving an
// <iframe> in the DOM reloads it, which would drop the terminal/scrollback.
let orderList = [];
try { orderList = JSON.parse(localStorage.getItem('claude-sessions-order') || '[]'); } catch (e) { orderList = []; }
function saveOrder() { try { localStorage.setItem('claude-sessions-order', JSON.stringify(orderList)); } catch (e) {} }
function applyOrder() {
  // A live tile that isn't in orderList must NOT collapse to order:-1 — CSS
  // sorts order:-1 BEFORE every order:>=0 tile, so the tile jumps to the FAR
  // LEFT and sticks there across reloads ("I drag a tile to last, it always
  // comes back first"). orderList can lose a live id outside the render path:
  // the storage listener replaces it with another window's list, a session can
  // reappear with a new id, or a stale/older-version saved list omits it. Adopt
  // any such id at the END (append semantics — same as a brand-new session) and
  // fold it into orderList so the next saveOrder persists a complete, healed list.
  for (const [id] of tiles) if (!orderList.includes(id)) orderList.push(id);
  for (const [id, el] of tiles) el.style.order = orderList.indexOf(id);
  // loading placeholders sit in the order via their marker id so they slot in
  // right next to their source until the real tile takes over. A marker no
  // longer in the list (expired/cancelled) must also go to the end, not -1.
  for (const ph of grid.querySelectorAll('.tile.loading')) {
    if (ph.dataset.marker) {
      const mi = orderList.indexOf(ph.dataset.marker);
      ph.style.order = mi >= 0 ? mi : orderList.length;
    }
  }
  markDeckShadows();
}
// Deck shadow: a condensed card tucks UNDER its right neighbour (negative margin
// pulls that neighbour left over the card's edge). The shadow that sells the
// "stacked cards" look is cast BY that covering neighbour onto the card below,
// so it belongs on the NEIGHBOUR — a leftward box-shadow that always sits flush
// at the neighbour's real left edge. We can't pick "the tile after a condensed
// one" in CSS (tiles are laid out by flex `order`, which has no sibling
// selector, and the actual overlap doesn't match --cond-tuck exactly anyway —
// measured 754 vs the 746 the margin math predicts, an 8px gap). So tag it here:
// walk the VISIBLE row tiles in flex-order and flag any whose predecessor is a
// condensed card. Order-independent, overlap-independent, no gap.
function markDeckShadows() {
  const shown = [...grid.querySelectorAll('.tile')]
    .filter(el => el.style.display !== 'none')
    .sort((a, b) => (parseInt(a.style.order, 10) || 0) - (parseInt(b.style.order, 10) || 0));
  for (let i = 0; i < shown.length; i++) {
    shown[i].classList.toggle('covers-card', i > 0 && shown[i - 1].classList.contains('condensed'));
  }
}
function reorder(srcId, targetId, after) {
  orderList = orderList.filter(x => x !== srcId);
  let ti = orderList.indexOf(targetId);
  if (ti < 0) ti = orderList.length;
  orderList.splice(after ? ti + 1 : ti, 0, srcId);
  saveOrder();
  applyOrder();
}
let dragId = null;

// Pending duplicate requests. The duplicate button spawns a new session in a
// cwd; that session only appears on a later poll, and we want its tile to land
// immediately to the RIGHT of the source it was cloned from (not at the end of
// the row). Remember {srcId, cwd, until} and match the next fresh session that
// shows up in that cwd.
let pendingDups = [];
function placeNewInOrder(s) {
  // Splice a newly-appeared session into the slot reserved by its loading
  // placeholder (and remove the placeholder); return true if it matched a
  // pending duplicate, false if just appended at the end.
  const now = Date.now();
  pendingDups = pendingDups.filter(p => p.until > now);
  // Match the spawned session to the placeholder we're holding for it. cwd is
  // the obvious key, but the spawned session's RECORDED cwd often isn't a byte
  // match for the source's: the host launcher writes $PWD (symlink-resolved by the
  // fresh shell), the container launcher writes its own WORKSPACE path, and trailing
  // slashes creep in — so an exact compare misses and the clone drifts to the
  // right of its group. Compare with trailing slashes stripped, and, failing
  // that, fall back to the SOLE in-flight placeholder: if exactly one dup is
  // pending, the next genuinely-new session IS that clone whatever its cwd
  // normalized to. (Concurrent dups in different dirs still disambiguate by
  // cwd; only the rare unrelated session spawned mid-clone could mis-claim,
  // and it would merely land next to the source — never far right.)
  const norm = (c) => (c || '').replace(/\/+$/, '');
  const scwd = norm(s.cwd);
  let i = pendingDups.findIndex(p => norm(p.cwd) === scwd && p.srcId !== s.id);
  if (i < 0 && pendingDups.length === 1 && pendingDups[0].srcId !== s.id) i = 0;
  if (i >= 0) {
    const p = pendingDups[i];
    pendingDups.splice(i, 1);
    const mi = p.marker ? orderList.indexOf(p.marker) : -1;
    if (mi >= 0) {
      orderList.splice(mi, 1, s.id);   // real id takes the placeholder's slot
    } else {
      let si = orderList.indexOf(p.srcId);
      if (si < 0) si = orderList.length - 1;
      orderList.splice(si + 1, 0, s.id);
    }
    if (p.placeholder && p.placeholder.parentNode) p.placeholder.remove();
    return true;
  }
  // No pending-dup match — this session just appeared on a poll. That happens
  // for a clone whose placeholder window expired (a slow container/claude boot)
  // or was lost to a reload, and for any session another window spawned. Don't
  // dump it at the far RIGHT of the whole row: slot it right after the last tile
  // that shares its cwd, so a clone still lands next to its source's group.
  // This is deterministic — every window and every reload computes the same
  // slot from the same (saved order + live tiles) — so the row order stays
  // stable across reloads without having to persist this passive placement
  // (persisting it would let one window's end-append clobber another's
  // authoritative clone placement; see the storage-listener notes below).
  let ins = -1;
  if (scwd) {
    for (let k = 0; k < orderList.length; k++) {
      const oid = orderList[k];
      if (oid === s.id) continue;
      const oel = tiles.get(oid);
      if (oel && norm(oel.dataset.cwd) === scwd) ins = k;
    }
  }
  if (ins >= 0) orderList.splice(ins + 1, 0, s.id);
  else orderList.push(s.id);
  return false;
}

// Build the placeholder tile shown while a duplicate is spawning. It mirrors the
// source's badge + cwd so the user can tell which clone it belongs to, with a
// spinner where the terminal will appear. The ✕ button cancels the placeholder
// locally (the spawned session may still appear and just lands at the end).
function loadingTile(srcEl, s) {
  const el = document.createElement('div');
  el.className = 'tile loading';
  el.dataset.tab = srcEl.dataset.tab || '';
  el.dataset.cwd = srcEl.dataset.cwd || s.cwd || '';
  const head = document.createElement('div'); head.className = 'head';
  const srcBadge = srcEl.querySelector('.badge');
  const badge = document.createElement('span');
  badge.className = srcBadge ? srcBadge.className : 'badge ' + (s.kind || 'host');
  badge.textContent = srcBadge ? srcBadge.textContent : (s.kind || 'host');
  const name = document.createElement('span'); name.className = 'name'; name.textContent = 'cloning…';
  const cwd = document.createElement('span'); cwd.className = 'cwd'; cwd.textContent = el.dataset.cwd;
  const cancel = document.createElement('button');
  cancel.className = 'close'; cancel.textContent = '✕';
  cancel.title = 'cancel — the spawned session may still appear at the end';
  cancel.onclick = (e) => { e.stopPropagation(); cancelPendingDup(el); };
  head.append(badge, name, cwd, cancel);
  const body = document.createElement('div'); body.className = 'loading-body';
  const sp = document.createElement('div'); sp.className = 'spinner';
  const msg = document.createElement('div'); msg.className = 'muted'; msg.textContent = 'starting session…';
  body.append(sp, msg);
  el.append(head, body);
  return el;
}
function cancelPendingDup(ph) {
  const marker = ph.dataset.marker;
  const idx = marker ? pendingDups.findIndex(p => p.marker === marker) : -1;
  if (idx >= 0) pendingDups.splice(idx, 1);
  if (marker) { const mi = orderList.indexOf(marker); if (mi >= 0) orderList.splice(mi, 1); }
  if (ph.parentNode) ph.remove();
  applyOrder(); applyVisibility();
}

// ===========================================================================
// TAB SYSTEM (workdir grouping + favorites)  — grep "TAB SYSTEM" to find this.
// Tiles are grouped into tabs by working directory. Pipeline + key symbols:
//   tabKeyFor(cwd, anchors, home)  shallowest ancestor session-dir → tab key
//   tabLabel(key)                  last path segment, shown on the tab button
//   layoutTabs(sessions)           regroup every render → curTabCount (key→count)
//   buildTabBar()                  render the bar (manual order = tabOrder)
//   applyVisibility()              show only activeTab's tiles (channels float)
//   spawnTile(kind)                ＋ New → spawns into activeTab's cwd
//   favorites[] / toggleFavorite() bookmarked workdirs keep their tab at 0 tiles
//   _setTabEmpty()                 #tab-empty hint for an empty favorite tab
// localStorage keys: claude-sessions-active-tab, -tab-order, -order, -favorites.
// Tabs show with 3+ sessions OR any favorite; ≤2 & no favorites → untabbed.
// ===========================================================================
// Manual tab order (drag tabs to rearrange), persisted like the tile order.
let tabOrder = [];
try { tabOrder = JSON.parse(localStorage.getItem('claude-sessions-tab-order') || '[]'); } catch (e) { tabOrder = []; }
function saveTabOrder() { try { localStorage.setItem('claude-sessions-tab-order', JSON.stringify(tabOrder)); } catch (e) {} }
// Order the present tab keys by the saved order; new keys are appended (alpha).
function orderTabKeys(keys) {
  const fresh = keys.filter(k => !tabOrder.includes(k))
    .sort((a, b) => tabLabel(a).localeCompare(tabLabel(b)) || a.localeCompare(b));
  if (fresh.length) { tabOrder.push(...fresh); saveTabOrder(); }
  return [...keys].sort((a, b) => tabOrder.indexOf(a) - tabOrder.indexOf(b));
}
function reorderTab(srcKey, targetKey, after) {
  tabOrder = tabOrder.filter(k => k !== srcKey);
  let ti = tabOrder.indexOf(targetKey);
  if (ti < 0) ti = tabOrder.length;
  tabOrder.splice(after ? ti + 1 : ti, 0, srcKey);
  saveTabOrder();
  buildTabBar();
}
// Cross-window order sync. Several dashboard windows can be open at once (a
// second monitor, a separate browser window); each holds an IN-MEMORY copy of
// the manual orders and writes the WHOLE list back on any structural change
// (new tile → placeNewInOrder → saveOrder, a close → prune → saveOrder).
// Without adopting the other window's writes, a window's stale copy clobbers
// a reorder made elsewhere on its next save — and the next reload restores
// the clobbered list: "tile order is not persistent across reloads". The
// storage event fires only in the windows that did NOT write, so adoption
// can't loop; ids only this window knows about (e.g. a tile the other window
// pruned during a transient dropout) are re-appended, unsaved — the next
// local save persists them.
window.addEventListener('storage', (e) => {
  if (!e.newValue) return;
  try {
    if (e.key === 'claude-sessions-order') {
      const v = JSON.parse(e.newValue);
      if (!Array.isArray(v)) return;
      // Remember each tile's CURRENT visual slot so any id the remote list is
      // missing (a tile the writer pruned during a transient dropout that we
      // still hold) re-appends where it actually sits — not in Map-insertion
      // order, which would scramble the row.
      const pos = new Map();
      for (const [id, el] of tiles) pos.set(id, parseInt(el.style.order, 10) || orderList.indexOf(id));
      orderList = v.slice();
      // Re-insert THIS window's own in-flight duplicate placeholders: their
      // marker ids live only here, so a remote list never carries them. Slot
      // each back right after its source so the spinner stays next to its clone.
      for (const p of pendingDups) {
        if (p.marker && !orderList.includes(p.marker)) {
          let si = orderList.indexOf(p.srcId);
          if (si < 0) si = orderList.length - 1;
          orderList.splice(si + 1, 0, p.marker);
        }
      }
      // Re-append real tiles the remote list lacks, in current visual order.
      const missing = [...tiles.keys()].filter(id => !orderList.includes(id));
      missing.sort((a, b) => (pos.get(a) || 0) - (pos.get(b) || 0));
      for (const id of missing) orderList.push(id);
      applyOrder();
    } else if (e.key === 'claude-sessions-tab-order') {
      const v = JSON.parse(e.newValue);
      if (!Array.isArray(v)) return;
      tabOrder = v;
      if (lastLayoutSessions.length) layoutTabs(lastLayoutSessions);
    }
  } catch (err) {}
});
let tabDragKey = null;
let curTabCount = new Map();   // tab key -> session count, from the last layout

// --- Favorite (bookmarked) workdirs ------------------------------------------
// A favorited workdir keeps its tab alive even when it has ZERO open tiles — so
// you can close everything in a project and the tab (plus its "＋ New opens
// here" behavior) stays put. Persisted per browser like the tab order; purely
// client-side — the server has no session for an empty favorite, so its tab is
// synthesized here from this list. Stored as absolute cwd strings, the same
// value used as a tab key.
let favorites = [];
try { favorites = JSON.parse(localStorage.getItem('claude-sessions-favorites') || '[]'); } catch (e) { favorites = []; }
function saveFavorites() { try { localStorage.setItem('claude-sessions-favorites', JSON.stringify(favorites)); } catch (e) {} }
function isFavorite(key) { return !!key && favorites.indexOf(key) >= 0; }
function toggleFavorite(key) {
  if (!key) return;
  const i = favorites.indexOf(key);
  if (i >= 0) favorites.splice(i, 1); else favorites.push(key);
  saveFavorites();
  // Toggling changes BOTH which tabs exist (an empty favorite tab appears/
  // disappears) and the grouping anchors (a favorite dir absorbs sub-dir
  // tiles), so re-group against the last-seen sessions and refresh the chrome.
  layoutTabs(lastLayoutSessions);
  refreshGridChrome();
}

// Sessions are grouped into tabs by workdir. A session started in a sub-dir of
// another session's dir joins that session's tab; the tab key is the shallowest
// session-dir that is an ancestor of this cwd. Grouping never climbs to or past
// the home folder, so unrelated projects under ~ don't collapse into one tab.
function isAncestorOrEqual(a, b) { return b === a || b.startsWith(a.replace(/\/+$/, '') + '/'); }
function tabKeyFor(cwd, allCwds, home) {
  // A favorited workdir is a HARD tab root: if any favorite is an ancestor-or-
  // equal of cwd, cwd belongs to the DEEPEST such favorite's tab and grouping
  // must NOT climb above it into a shallower parent session's tab. Without this
  // floor, pinning a sub-dir of an active project (pin repo/sub while a session
  // runs in repo/) stranded the pinned tab permanently empty: the session at
  // repo/sub — and every ＋ New spawned "into" the pinned tab — got absorbed by
  // the parent repo/ tab, so the favorite could never be populated ("can't
  // start a new claude from an empty pinned tab"). Favorites only ever DEEPEN
  // (never shrink) the key, so a non-nested favorite is unaffected.
  let floor = '';
  for (const f of favorites) {
    if (f && isAncestorOrEqual(f, cwd) && f.length > floor.length) floor = f;
  }
  let key = cwd;
  for (const p of allCwds) {
    if (p === cwd || !p) continue;
    if (!isAncestorOrEqual(p, cwd)) continue;          // p must be an ancestor of cwd
    if (home && isAncestorOrEqual(p, home)) continue;  // never group at/above the home folder
    if (floor && p.length < floor.length) continue;    // never climb above a favorite tab-root
    if (p.length < key.length) key = p;                // prefer the shallowest such ancestor
  }
  return key;
}
function tabLabel(key) { return (key || '').replace(/\/+$/, '').split('/').pop() || key || '~'; }

let selectedId = null;       // the "current" session (last clicked/focused tile)
let armedId = null;          // tile awaiting a second Ctrl+Q to confirm close
let armTimer = null;

function disarm() {
  if (armedId) { const el = tiles.get(armedId); if (el) el.classList.remove('armed'); }
  armedId = null;
  if (armTimer) { clearTimeout(armTimer); armTimer = null; }
}

// Clear a tab's bell once none of its tiles are still belled, so acknowledging
// a tile's bell directly (not via its tab) doesn't leave a stale tab indicator.
function refreshTabBell(tabKey) {
  if (!tabKey) return;
  for (const [, el] of tiles) if (el.dataset.tab === tabKey && el.classList.contains('bell')) return;
  for (const b of tabsEl.children) if (b.dataset.key === tabKey) b.classList.remove('bell');
}

function selectTile(id) {
  selectedId = tiles.has(id) ? id : null;
  for (const [tid, el] of tiles) el.classList.toggle('selected', tid === selectedId);
  const sel = tiles.get(id);
  if (sel) { sel.classList.remove('bell'); refreshTabBell(sel.dataset.tab); updateDockBadge(); updateHome(); }   // acknowledge the bell on view
  if (armedId && armedId !== selectedId) disarm();
}

// Sessions whose tile is fading out / awaiting the server to drop its registry
// entry. render() must NOT re-create a tile for one of these: the close request
// deletes the registry file, but a poll can fire (the animation finishing, or
// the 3 s interval) while the server still lists it — re-adding the tile we just
// animated away, which then vanished again on the next poll. (That was the
// "box re-appears for a moment then disappears" bug.)
const closingIds = new Set();

async function doClose(id) {
  const t = tiles.get(id);
  if (selectedId === id) selectedId = null;
  if (armedId === id) disarm();
  if (t) { closingIds.add(id); animateClose(t, id); }
  let ok = false;
  try { const r = await fetch('/api/close?id=' + encodeURIComponent(id), { method: 'POST', headers: { 'X-CSRF-Token': CSRF } }); ok = r.ok; } catch (e) {}
  if (!ok) closingIds.delete(id);   // close didn't go through — let the tile come back
  poll();   // refresh once the server has committed the close
}

// Stash flips a server-side flag and HIDES the tile — but keeps its iframe ALIVE
// (dataset.stashed='1'), unlike close which tears it down. Keeping it alive lets
// the session keep receiving its BEL while stashed, so markBell can auto-unstash
// it on a ring (the user asked: a stashed claude that rings should pop back).
// The process keeps running either way; restore from the header drawer (or a
// bell) brings it back into its tab. We flip dataset.stashed optimistically so
// the hide/show is instant, then poll() reconciles tabs/counts from the server.
async function doStash(id, on) {
  const t = tiles.get(id);
  if (on && t) {
    if (selectedId === id) selectedId = null;
    if (armedId === id) disarm();
    t.classList.remove('bell'); refreshTabBell(t.dataset.tab); updateDockBadge(); updateHome();   // a stashed tile shouldn't keep a stale bell
    // Optimistic HIDE: applyVisibility forces display:none for a stashed tile in
    // either layout, so the tile vanishes instantly. (We do NOT optimistically
    // SHOW on unstash — in the untabbed ≤2-tile view layoutTabs, not
    // applyVisibility, owns display, so we leave the un-hide to render's
    // structural relayout below, which is correct in both views.)
    t.dataset.stashed = '1'; applyVisibility();
  }
  let ok = false;
  try {
    const r = await fetch('/api/stash?id=' + encodeURIComponent(id) + '&on=' + (on ? '1' : '0'),
      { method: 'POST', headers: { 'X-CSRF-Token': CSRF } });
    ok = r.ok;
  } catch (e) {}
  if (!ok && on && t) { t.dataset.stashed = ''; applyVisibility(); }   // server didn't flip it — un-hide
  // poll → render reads the server's stashed flag, flips dataset.stashed, and
  // (because that's a structural change) relayouts → hide/show lands correctly.
  poll();
}

// Header drawer listing currently-stashed sessions. Hidden when the list is
// empty so the chrome stays clean. Each row: kind badge + name + cwd, with an
// "↥" restore (unstash) button and "✕" close (terminate) button. Built fresh
// each render — these are infrequent operations, no need to diff entries.
const stashWrap = document.getElementById('stashWrap');
const stashBtnEl = document.getElementById('stashBtn');
const stashMenuEl = document.getElementById('stashMenu');
const stashCountEl = document.getElementById('stashN');
stashBtnEl.onclick = (e) => { e.stopPropagation(); stashMenuEl.classList.toggle('open'); };
document.addEventListener('click', (e) => {
  if (!stashMenuEl.contains(e.target) && e.target !== stashBtnEl) stashMenuEl.classList.remove('open');
});
function renderStashDrawer(stashed) {
  const n = stashed.length;
  stashCountEl.textContent = String(n);
  stashWrap.hidden = n === 0;
  if (n === 0) { stashMenuEl.classList.remove('open'); stashMenuEl.replaceChildren(); return; }
  // Sort by cwd then name so tiles from the same workdir group together —
  // mirrors how the tab bar groups live tiles.
  const sorted = [...stashed].sort((a, b) =>
    (a.cwd || '').localeCompare(b.cwd || '') ||
    (a.name || a.id || '').localeCompare(b.name || b.id || ''));
  const rows = sorted.map(s => {
    const row = document.createElement('div'); row.className = 'row';
    const isContainerTerm = s.kind === 'terminal' && s.container;
    const badge = document.createElement('span');
    badge.className = 'badge ' + (isContainerTerm ? 'container-terminal' : (s.kind || 'host'));
    badge.textContent = isContainerTerm ? 'sh-box' : (s.kind || 'host');
    const nm = document.createElement('span'); nm.className = 'nm';
    // Prefer the live title (what the program emitted — "claude: …",
    // "▲ stm32 build") over the registry name (cwd basename), so a stashed
    // tile reads as the actual session, not the workdir.
    nm.textContent = liveTitles.get(s.id) || s.name || s.id;
    if (s.dead) nm.style.opacity = '0.5';
    const cd = document.createElement('span'); cd.className = 'cd';
    cd.textContent = (s.dead ? '⚡ dead — ' : '') + (s.cwd || s.url || '');
    const restore = document.createElement('button');
    restore.className = 'restore';
    restore.title = s.dead ? 'process has exited — cannot restore' : 'restore — bring this tile back into the grid';
    restore.textContent = '↥';
    restore.disabled = !!s.dead;
    restore.onclick = (e) => { e.stopPropagation(); restore.disabled = true; doStash(s.id, false); };
    const kill = document.createElement('button');
    kill.className = 'kill'; kill.title = 'close (ends the underlying process)';
    kill.textContent = '✕';
    kill.onclick = (e) => {
      e.stopPropagation();
      const what = s.kind === 'webview' ? 'this web tile' : (s.kind === 'terminal' ? 'the shell' : 'the claude process');
      if (confirm('Close stashed "' + (s.name || s.id) + '"?\nThis ends ' + what + '.')) doClose(s.id);
    };
    row.append(badge, nm, cd, restore, kill);
    return row;
  });
  stashMenuEl.replaceChildren(...rows);
}

// Play a brief fade/shrink before removing a closed tile, so the session's
// disappearance reads as deliberate rather than a flicker. Flagged via
// dataset.closing so a concurrent render() won't yank it mid-animation; the id
// stays in closingIds (cleared in render once the server reaps it) so a poll
// that still lists the not-yet-reaped session can't re-create this tile.
function animateClose(el, id) {
  if (el.dataset.closing) return;
  el.dataset.closing = '1';
  let done = false;
  const finish = () => {
    if (done) return; done = true;
    const f = el.querySelector('iframe'); if (f) inputObserver.unobserve(f);
    el.remove();
    if (tiles.get(id) === el) tiles.delete(id);
    applyVisibility();   // recompute row/grid + visibility now the tile's truly gone
  };
  // A HIDDEN tile (stashed → closed from the drawer's ✕, or living in a
  // non-active tab) is display:none, so NO CSS transition runs and
  // 'transitionend' never fires — the fold can't play and removal would hinge on
  // the setTimeout fallback alone, which a busy main thread (many live terminal
  // iframes) can defer for seconds. Meanwhile render() won't reap a tile flagged
  // dataset.closing, so the dead tile lingers ("terminal won't close"). There's
  // nothing to animate off-screen: tear it down synchronously. Same for reduced
  // motion — the .closing transition is zeroed, so there's no fold to wait on.
  if (reducedMotion || el.offsetParent === null || getComputedStyle(el).display === 'none') { finish(); return; }
  // Freeze the current width so the fold animates from a real px value (flex/grid
  // items otherwise jump straight to 0), reflow, then collapse to 0 — the row's
  // remaining tiles slide left to close the gap. Inline styles beat the row's
  // flex rule, so this works in both the grid and horizontal-row layouts.
  const w = el.offsetWidth;
  el.style.flex = '0 0 ' + w + 'px';
  el.style.maxWidth = w + 'px';
  el.style.width = w + 'px';
  void el.offsetWidth;                       // reflow so the change below transitions
  el.classList.add('closing');               // applies the transition + opacity/fold target
  el.style.flex = '0 0 0px';
  el.style.maxWidth = '0px';
  el.style.width = '0px';
  el.style.margin = '0px';
  el.style.padding = '0px';
  el.addEventListener('transitionend', finish, { once: true });
  setTimeout(finish, 340);   // fallback if no transitionend fires
}

// Track which session iframe is focused so it becomes the "current" one (the
// highlighted tile + Ctrl+Q target). We poll document.activeElement rather than
// listen for window 'blur': blur fires only when focus first leaves the page
// into an iframe, NOT when it moves between two iframes (the page was already
// blurred), which left the highlight stuck on the first iframe clicked.
let lastActiveFrame = null;
setInterval(() => {
  const ae = document.activeElement;
  if (ae && ae.tagName === 'IFRAME' && ae !== lastActiveFrame) {
    lastActiveFrame = ae;
    for (const [id, el] of tiles) if (el.contains(ae)) { selectTile(id); break; }
  }
}, 250);

// Ctrl+Q closes a session, double-press to confirm. First press arms (red
// outline + hint), second within 3 s closes. (Used to be Ctrl+X but that
// collides with nano's exit when nano runs inside a tile.)
function armOrClose(id) {
  if (!id || !tiles.has(id)) return;
  if (armedId === id) { disarm(); doClose(id); return; }
  disarm();
  armedId = id;
  const el = tiles.get(id); if (el) el.classList.add('armed');
  armTimer = setTimeout(disarm, 3000);
}
// Tell a tile's terminal client to re-layout (fit.fit() + refresh visible
// rows). Cheap, preserves scrollback + cursor. Used as a manual escape
// hatch for renderer ghost glyphs (alt-buffer artifacts after Ctrl+O,
// WebGL frame drops, column mismatch after a silent iframe resize).
function doRefresh(id) {
  if (!id) return;
  const el = tiles.get(id); if (!el) return;
  const f = el.querySelector('iframe'); if (!f) return;
  const target = frameTargetOrigin(f); if (!target) return;
  try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'refresh' }, target); } catch (e) {}
}
// Lighter than doRefresh: ask the client for a pty-only program repaint (the
// _repaintWiggle, no renderer swap, no layout change). Used on expand so a frame
// claude painted while the card was occluded by its neighbour redraws cleanly.
function repaintTile(id) {
  if (!id) return;
  const el = tiles.get(id); if (!el) return;
  const f = el.querySelector('iframe'); if (!f) return;
  const target = frameTargetOrigin(f); if (!target) return;
  try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'repaint' }, target); } catch (e) {}
}
// Hard-reload a terminal tile's iframe: drops the ttyd connection (detaches the
// dtach CLIENT) and reconnects, which re-runs `dtach -A <sock>` and RE-ATTACHES
// to the same still-running claude — the process is never restarted. This is the
// "detach → kill old term → new term attached to the old process" flow, done at
// the ttyd-connection level. Scrollback restores from localStorage on the fresh
// load (the page reload fires pagehide → save first). Distinct from doRefresh
// (the Cmd+Shift+E in-place renderer swap), which deliberately keeps the socket
// — use reload when the tile/connection itself is wedged, or to pick up a
// rebuilt term.html. The iframe is cross-origin (ttyd port), so we reassign src
// rather than call contentWindow.location.reload(); the about:blank bounce
// forces a real reload even though the URL is unchanged.
// clean=true ("clean reload", Shift+click): first tell the client to forget its
// saved scrollback (so the reload restores nothing and rebuilds purely from the
// reattach replay) — useful to wipe pre-existing duplicated/garbled scrollback.
// We post the clear, then reload on a short delay so the message lands first.
function reloadTile(id, clean) {
  const el = tiles.get(id); if (!el) return;
  const f = el.querySelector('iframe'); if (!f) return;
  const src = f.src; if (!src || src === 'about:blank') return;
  const go = function () { f.src = 'about:blank'; setTimeout(function () { f.src = src; }, 0); };
  if (clean) {
    const target = frameTargetOrigin(f);
    if (target) { try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'clear-scrollback' }, target); } catch (e) {} }
    setTimeout(go, 60);   // let the client drop its localStorage before we reload
  } else {
    go();
  }
}
// Swap a tile one slot left (dir=-1) or right (dir=+1) inside its current
// tab. We skip over orderList entries whose tile is in a DIFFERENT tab — the
// user sees a reorder relative to what's visible, not relative to the global
// list (which would feel random when other tabs are populated). DOM nodes
// aren't moved: position is driven by CSS `order`, so an iframe move doesn't
// reload the terminal.
function moveTile(id, dir) {
  if (!id) return;
  const el = tiles.get(id); if (!el) return;
  const tab = el.dataset.tab || '';
  const i = orderList.indexOf(id);
  if (i < 0) return;
  const step = dir < 0 ? -1 : 1;
  let j = i + step;
  while (j >= 0 && j < orderList.length) {
    const other = tiles.get(orderList[j]);
    if (other && (other.dataset.tab || '') === tab) break;
    j += step;
  }
  if (j < 0 || j >= orderList.length) return;
  // Simple swap is enough: between i and j we only ever skip OTHER tabs'
  // tiles, whose order among themselves doesn't change relative to ours.
  const tmp = orderList[i]; orderList[i] = orderList[j]; orderList[j] = tmp;
  saveOrder(); applyOrder();
  // Follow the tile: in the horizontal row it may now sit off-screen, so scroll
  // it smoothly into view. releasePin() first so the startup scroll-pin (which
  // snaps the row back to 0) doesn't cancel this scroll right after a reload.
  releasePin();
  try { el.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'smooth' }); } catch (e) {}
}
// When the dashboard chrome (not a terminal) has focus, handle the shortcuts
// here on the current/selected tile. When a terminal iframe has focus it
// swallows the key — so the custom client forwards Ctrl+Q and Cmd+E up via
// postMessage (see below), which routes them to the focused session / spawns.
// Cmd+E NOTE: was Cmd+T originally, but Chrome on macOS hard-binds Cmd+T at
// the window-manager level (it opens a new tab in the regular Chrome window
// even from a PWA) — the page never sees the event, so preventDefault is
// useless. Cmd+E is free in Chrome, so the handler works there.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && armedId) { disarm(); return; }
  // Ctrl+Tab / Ctrl+Shift+Tab → cycle the selected tile (works when the chrome
  // has focus; terminals forward it up via postMessage, see the message handler).
  if (e.ctrlKey && !e.metaKey && !e.altKey && e.key === 'Tab') {
    e.preventDefault();
    cycleTile(e.shiftKey ? -1 : 1);
    return;
  }
  // Cmd/Ctrl+Left/Right → move selected tile one slot in its tab. preventDefault
  // suppresses Chrome's "history back/forward" on Cmd+Arrow (which would
  // otherwise leave the dashboard entirely — the beforeunload prompt fires but
  // the muscle-memory mismatch is jarring). Bare arrows (no modifier) are
  // left alone so scrolling/selection inside terminals still works.
  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    e.preventDefault();
    let id = selectedId;
    if (!id) id = theOnlyLiveTileId();   // single visible tile → auto-target (ignore stashed)
    if (!id) return;
    moveTile(id, e.key === 'ArrowLeft' ? -1 : 1);
    return;
  }
  // Cmd/Ctrl+Shift+E → refresh the selected tile's renderer (escape hatch
  // for ghost glyphs after Ctrl+O / WebGL state loss / resize-race column
  // mismatch). Checked BEFORE the plain Cmd+E case because the Shift'd
  // chord is a strict superset of modifier predicates.
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && (e.key === 'E' || e.key === 'e')) {
    e.preventDefault();
    // Same single-tile fallback as armOrClose: when nothing's explicitly
    // selected (e.g., you pressed the chord right after a reload) but
    // there's only one tile, refresh it. Otherwise the chord would feel
    // like a no-op in the common one-terminal case.
    let id = selectedId;
    if (!id) id = theOnlyLiveTileId();   // single visible tile → auto-target (ignore stashed)
    doRefresh(id);
    return;
  }
  // Cmd/Ctrl+E → new terminal in the current tab's cwd
  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'e' || e.key === 'E')) {
    e.preventDefault();
    spawnTile('terminal');
    return;
  }
  // Cmd/Ctrl+T → new note tile (scratchpad). Chrome hard-binds Cmd+T in a plain
  // browser tab (opens a new tab; the page never sees the event), so this only
  // fires in the installed PWA / standalone window — where Cmd+T is free.
  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 't' || e.key === 'T')) {
    e.preventDefault();
    spawnTile('note');
    return;
  }
  // Cmd/Ctrl+X → park (condense) the selected tile as a card; toggles back on
  // repeat. Single-tile fallback like the other chords so it isn't a no-op
  // right after a reload when nothing is explicitly selected.
  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'x' || e.key === 'X')) {
    let id = selectedId;
    if (!id) id = theOnlyLiveTileId();
    if (!id) return;
    e.preventDefault();
    toggleCondense(id);
    return;
  }
  // Ctrl+Q (no meta, no alt) → arm/close the selected tile
  if (!(e.ctrlKey && !e.metaKey && !e.altKey && (e.key === 'q' || e.key === 'Q'))) return;
  let id = selectedId;
  if (!id) id = theOnlyLiveTileId();   // single visible tile → auto-target (ignore stashed)
  if (!id) return;  // nothing current — let the browser have the keystroke
  e.preventDefault();
  armOrClose(id);
});

// Warn before a reload/close discards the terminals — their scrollback lives
// only in these iframes and can't be restored. This top-page handler fires
// regardless of which (cross-origin) iframe has focus; ttyd's own leave alert
// stays disabled so this is the single prompt, and only when sessions are open.
window.addEventListener('beforeunload', (e) => {
  if (tiles.size > 0) { e.preventDefault(); e.returnValue = ''; }
});

// Stop an accidental Back / two-finger swipe-back from discarding live
// terminals (their scrollback lives only in the iframes). Browsers don't allow
// truly disabling history, so we keep a sentinel entry in front of the page and
// silently re-push it on Back WHILE sessions are open — making back/swipe-back a
// no-op. With no tiles open we let Back pass through so navigation works normally.
history.pushState(null, '', location.href);
window.addEventListener('popstate', () => {
  if (tiles.size > 0) history.pushState(null, '', location.href);  // re-arm: trap here
  else history.back();                                             // nothing to protect — let Back leave
});

// --- condense ("park as a card") ---
// A condensed tile keeps running at full width but tucks under its right
// neighbour, showing only a thin spine — park a session you're waiting on
// without giving it a full slot, stacked like a deck of cards. The next bell
// (permission prompt / finished turn) springs it back to a full slot via
// markBell, so the row makes space exactly when the session wants attention.
// The condense/expand glides via the .tile flex-basis + margin transition (the
// row-mode iframe pin keeps the PTY width fixed, so the box can animate safely).
// State is per-session in localStorage so a dashboard reload keeps parked tiles
// parked.
let condensedIds = new Set();
try { condensedIds = new Set(JSON.parse(localStorage.getItem('claude-sessions-condensed') || '[]')); } catch (e) {}
function saveCondensed() {
  try { localStorage.setItem('claude-sessions-condensed', JSON.stringify([...condensedIds])); } catch (e) {}
}
function setCondensed(id, on, relayout) {
  const el = tiles.get(id); if (!el) return;
  // No iframe resizing here — the width is handled entirely in CSS. The card
  // box shrinks to a spine, but EVERY row tile's iframe is pinned by CSS
  // (`#grid.row > .tile > iframe`) to the SAME pixel width it has un-condensed
  // (calc(min(100vw/2.2,900px)-2px)), so it's clipped by overflow:hidden, never
  // resized: the PTY width never changes. Because that pin is constant in both
  // states, the box CAN animate its flex-basis (the .tile transition) without
  // dragging the iframe through intermediate widths. Earlier designs shrank the
  // tile with the iframe at width:100% (or pinned it to a measured px width) and
  // fired fit→SIGWINCH down to a sliver, hard-wrapping everything claude printed
  // while parked into real newlines no reflow can rejoin — baked into the live
  // buffer AND the persisted snapshot. The all-tiles CSS pin sidesteps that.
  // (Pre-scrambled history can't be repaired in place — Shift+click ↻ rebuilds
  // the tile from ttyd replay at full width.)
  el.classList.toggle('condensed', on);
  if (on) condensedIds.add(id); else condensedIds.delete(id);
  saveCondensed();
  markDeckShadows();   // (re)tag the covering neighbour so it casts the deck shadow
  const k = el.dataset.kind;
  if (k !== 'channel' && k !== 'note' && k !== 'webview') {
    // Tell the client it's parked, so a vertical wheel over the card's spine
    // fans the row horizontally instead of scrolling its hidden scrollback.
    const f = el.querySelector('iframe');
    const target = f && frameTargetOrigin(f);
    if (target) { try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'condensed', on }, target); } catch (e) {} }
    // On expand, nudge a pty-only repaint once the overlap clears — cheap
    // insurance for a frame painted while the card was tucked under its neighbour.
    if (!on) setTimeout(() => repaintTile(id), 320);
  }
  const b = el.querySelector('button.cond');
  if (b) {
    b.classList.toggle('on', on);
    b.textContent = on ? '⤢' : '⤡';
    b.title = on ? 'expand — bring this card back to a full slot (a bell does this automatically)'
                 : 'condense — park this tile as a card until it rings (bell restores it)';
  }
  // Recompute layout: a parked card forces row mode (anyCond in applyVisibility)
  // so the box can actually shrink, and expanding the last one drops back to grid.
  // Skipped from the render-time restore (relayout===false), which relayouts in
  // its own tail — calling applyVisibility mid-render would reenter it.
  if (relayout !== false) applyVisibility();
}

// Cmd+X (and the per-tile button) toggle a tile between parked-card and full.
function toggleCondense(id) {
  const el = tiles.get(id); if (!el) return;
  setCondensed(id, !el.classList.contains('condensed'));
}

// The custom terminal client (term.html) posts its live title and bell up to
// us. Show the title as the tile name, and flag the bell on the tile + its tab.
function markBell(id) {
  const el = tiles.get(id); if (!el) return;
  // A stashed (hidden-but-alive) session that rings wants attention — pop it
  // back into its tab so the user can see it, then fall through to flash + chime
  // + Dock badge like any other ring. doStash is async (server flip + poll) but
  // optimistically clears dataset.stashed + applyVisibility, so the tile is
  // already visible by the time we add the bell class below.
  if (el.dataset.stashed === '1') doStash(id, false);
  // A condensed tile that rings springs back to full width (animated) — the
  // whole point of parking it was "make room again when it needs me".
  if (el.classList.contains('condensed')) setCondensed(id, false);
  el.classList.add('bell');
  if (el.dataset.tab && el.dataset.tab !== activeTab) {
    for (const b of tabsEl.children) if (b.dataset.key === el.dataset.tab) b.classList.add('bell');
  }
  playBell();   // audible chime in addition to the tile/tab flash (muteable in header)
  updateDockBadge();   // and a count on the macOS Dock icon when run as a Chrome app
  updateHome();   // and the Home tab's count/glow (+ surface it live if Home is open)
}
window.addEventListener('message', (e) => {
  // Tiles post from http://127.0.0.1:<ttyd-port> when embedded directly, or from
  // our own origin when routed through the /t/<port>/ proxy (PROXY_TTYD mode).
  if (typeof e.origin !== 'string' ||
      (e.origin.indexOf('http://127.0.0.1:') !== 0 && e.origin !== location.origin)) return;
  const d = e.data;
  if (!d || d.type !== 'claude-term' || !d.sid) return;
  const el = tiles.get(d.sid); if (!el) return;
  if (d.ready) {
    const f = el.querySelector('iframe');
    if (f) {
      sendFrameInput(f, true);   // push current input-gate state
      const target = frameTargetOrigin(f);
      if (target) {
        // Push the current font choice — new iframes default to JBM on boot,
        // so without this a tile that mounts after the user picked a different
        // font (or size override) would render JBM-13 until the next change.
        try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'font', font: currentEntry() }, target); } catch (e) {}
        // Push the current light/dark mode so a freshly-mounted tile themes to
        // match (it boots from its own stored copy, but this covers a toggle
        // that happened while it was loading / a tile opened in light mode).
        try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'theme', theme: theme }, target); } catch (e) {}
        // Hand the CSRF token to the iframe so it can POST to /api/dropfile
        // for drag/paste file uploads. The iframe is on a DIFFERENT origin
        // (http://127.0.0.1:<ttyd-port>) so it can't read CSRF off our page;
        // it has to receive it via postMessage. We PIN targetOrigin to the
        // iframe's actual src origin — using '*' here would leak the token
        // to whoever happens to be loaded in the iframe at send time (e.g.
        // about:blank after a ttyd crash, or any cross-origin navigation).
        // The iframe origin lands automatically in e.origin on the iframe side.
        try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'csrf', token: CSRF }, target); } catch (e) {}
        // If this tile mounted already parked (reload re-apply path), the
        // condensed flag posted from setCondensed raced the iframe load and was
        // lost — re-push it here so parked-card wheel→H-scroll works on reload.
        if (el.classList.contains('condensed')) {
          try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'condensed', on: true }, target); } catch (e) {}
        }
      }
    }
  }
  // Use the live title, but ignore ttyd's command-derived one ("dtach … claude"
  // / "podman exec …") that it sends on every (re)connect — it would clobber the
  // tile name on reload. Real titles come from the program's OSC sequences.
  if (typeof d.title === 'string' && d.title.trim() && !/^(dtach|podman|ttyd)\b/.test(d.title.trim())) {
    const t = d.title.trim();
    const nm = el.querySelector('.name');
    if (nm) { nm.textContent = t; nm.title = t + '\n' + d.sid; }   // keep the session id in the hover
    // Re-resolve the glyph against the real program title (the creation-time
    // name was just the cwd basename). Colour seeds off the title too.
    const ic = el.querySelector('.icon');
    if (ic) paintIcon(ic, t, t, el.dataset.cwd || '', el.dataset.iconKind || el.dataset.kind);
    // Cache so the stash drawer can show the *live* title (e.g. "claude: …",
    // "▲ stm32 build") instead of the registry-only basename (cwd) once the
    // tile has been removed from the DOM. Also stays around for the rest of
    // this dashboard session if you stash → restore the same tile.
    liveTitles.set(d.sid, t);
    try { localStorage.setItem('claude-sessions-title:' + d.sid, t); } catch (e) {}
  }
  if (d.bell) markBell(d.sid);
  if (d.kind === 'suggestion') {
    // The terminal client scraped claude's dim input-box ghost suggestion. Stash
    // it under a per-sid key; the chat-panel iframe is SAME-ORIGIN, so writing
    // here fires a 'storage' event over there (same mechanism as line-height) and
    // it offers the text as a Tab-to-use ghost. Only touch the key on change so we
    // don't spam storage events on every identical redraw.
    const sk = 'claude-sessions-suggestion:' + d.sid;
    const sv = (typeof d.text === 'string' ? d.text : '').slice(0, 400);
    try {
      if (sv) { if (localStorage.getItem(sk) !== sv) localStorage.setItem(sk, sv); }
      else if (localStorage.getItem(sk) !== null) localStorage.removeItem(sk);
    } catch (e) {}
  }
  if (d.key === 'ctrl-q') armOrClose(d.sid);            // Ctrl+Q forwarded from a focused terminal
  if (d.key === 'cmd-e') spawnTile('terminal');         // Cmd+E forwarded from a focused terminal
  if (d.key === 'cmd-t') spawnTile('note');             // Cmd+T forwarded from a focused terminal → new note
  if (d.key === 'cmd-shift-e') doRefresh(d.sid);        // Cmd+Shift+E forwarded → refresh THIS sid
  if (d.key === 'cmd-left')  moveTile(d.sid, -1);       // Cmd+← forwarded → move THIS tile left in its tab
  if (d.key === 'cmd-right') moveTile(d.sid, +1);       // Cmd+→ forwarded → move THIS tile right in its tab
  if (d.key === 'cycle-next') cycleTile(1);             // Ctrl+Tab forwarded from a focused terminal
  if (d.key === 'cycle-prev') cycleTile(-1);            // Ctrl+Shift+Tab forwarded
  if (d.key === 'cmd-x') toggleCondense(d.sid);         // Cmd+X forwarded → park/expand THIS tile
  // First pointer/touch/key interaction INSIDE a terminal (cross-origin, so the
  // window-level gesture listeners never see it). Lift the startup scroll-pin,
  // exactly as a gesture over the dashboard chrome would — otherwise the row
  // stays locked to the left until the user happens to swipe horizontally.
  if (d.key === 'user-gesture') releasePin();
  if (d.key === 'wheel-x' && typeof d.dx === 'number') {
    // Horizontal trackpad swipe / shift+wheel over a terminal iframe — the
    // iframe is cross-origin so wheel never bubbles to us. The custom client
    // mirrors the horizontal delta up; we apply it to the row's scrollLeft
    // when the row mode is active. No-op otherwise (column grid has no
    // horizontal overflow), which is also fine.
    // A forwarded swipe IS a genuine user scroll gesture, so lift the startup
    // scroll-pin here too: the wheel happened over a cross-origin terminal, so
    // the window-level gesture listeners never saw it — without this the pin
    // would snap our scrollLeft change straight back to 0 and the row would
    // "stick" to the left border whenever the pointer sits over a terminal.
    releasePin();
    if (grid.classList.contains('row')) grid.scrollLeft += d.dx;
  }
});

// Gate keystroke input by visibility. A terminal scrolled out of the horizontal
// row (or sitting in a hidden tab) shouldn't receive typing — but we must NOT
// blur it, so its focus + selection survive and scrolling it back is instantly
// usable. Each terminal is a cross-origin iframe whose keys we can't intercept
// here, so the custom client gates its own stdin when we postMessage it.
const inputObserver = new IntersectionObserver((entries) => {
  for (const ent of entries) { ent.target.__inputWanted = ent.isIntersecting; sendFrameInput(ent.target); }
}, { threshold: 0 });
function sendFrameInput(f, force) {
  const want = f.__inputWanted !== false;   // default enabled until first observed
  if (!force && f.__inputSent === want) return;
  f.__inputSent = want;
  const target = frameTargetOrigin(f); if (!target) return;
  try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'input', enabled: want }, target); } catch (e) {}
}

// ── animate tile resizes (FLIP) ───────────────────────────────────────────
// When the visible set changes — a tile joins or leaves, or the grid↔row
// threshold (≥3 tiles) flips — the surviving tiles jump to a new size/position.
// Glide them instead. We use a transform-only FLIP: the layout settles to its
// FINAL geometry in one step, so each terminal iframe fits exactly ONCE (the
// same single SIGWINCH as without animation — NOT dragged through intermediate
// widths, so no SIGWINCH storm and no hard-wrapped scrollback, the hazard the
// row-mode iframe pin guards against), and the GPU interpolates the visual box
// from its old rect to the new. The iframe content is briefly scaled during the
// glide — the same trade-off the close-fold already makes with scaleX.
const FLIP_MS = 220;     // resize/move glide
const ENTER_MS = 340;    // tile open (unfold) — a touch slower so the pop-in reads
let _flipSeq = 0;
let _suppressFlip = false;   // render() owns the play; mute the nested applyVisibility one
// NOTE: these animations intentionally do NOT honor prefers-reduced-motion — the
// close-fold (animateClose) never did either, so gating only open/resize made the
// dashboard animate on close but snap on open, which read as "the open animation
// is broken" (it was the OS Reduce-Motion setting). All tile motion is uniform now.
// First: snapshot the on-screen geometry of every laid-out tile. Returns null
// (→ flipPlay is a no-op) while dragging, or when a parent render() owns the play.
function flipCapture() {
  if (_suppressFlip || reducedMotion || document.body.classList.contains('dragging')) return null;
  const first = new Map();
  for (const [, el] of tiles) {
    if (el.dataset.closing) continue;            // its own fold owns it
    const r = el.getBoundingClientRect();
    if (r.width > 1 && r.height > 1) first.set(el, r);   // skip hidden (0-size) tiles
  }
  return first;
}
// Last + Invert + Play: for each tile that survived and actually moved/resized,
// snap it back to its old rect with a transform, then transition it to identity.
function flipPlay(first) {
  if (!first || !first.size) return;
  const moved = [];
  for (const [el, f] of first) {
    if (el.dataset.closing) continue;
    const l = el.getBoundingClientRect();
    if (l.width <= 1 || l.height <= 1) continue;       // hidden now (e.g. tab switch)
    const dx = f.left - l.left, dy = f.top - l.top;
    const sx = f.width / l.width, sy = f.height / l.height;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1 &&
        Math.abs(sx - 1) < 0.01 && Math.abs(sy - 1) < 0.01) continue;   // didn't change
    el.style.transition = 'none';
    el.style.transformOrigin = 'top left';
    el.style.transform = 'translate(' + dx + 'px,' + dy + 'px) scale(' + sx + ',' + sy + ')';
    moved.push(el);
  }
  if (!moved.length) return;
  void grid.offsetWidth;   // commit the inverted transforms in one reflow before transitioning
  for (const el of moved) {
    const token = ++_flipSeq;
    el.dataset.flip = String(token);
    // Keep the base flex/margin/max-width transitions alive so a concurrent
    // condense/expand on the same tile still glides.
    el.style.transition = 'transform ' + FLIP_MS + 'ms ease, flex-basis .24s ease, margin .24s ease, max-width .24s ease';
    el.style.transform = 'none';
    const clear = () => {
      if (el.dataset.flip !== String(token)) return;   // a newer flip owns it now
      delete el.dataset.flip;
      el.style.transition = '';
      el.style.transform = '';
      el.style.transformOrigin = '';
    };
    el.addEventListener('transitionend', clear, { once: true });
    setTimeout(clear, FLIP_MS + 120);   // fallback if no transitionend fires
  }
}

// Entrance: a freshly-opened tile unfolds from a sliver to full width. Its layout
// slot is already final (siblings made room via flipPlay, the iframe fits once),
// so this is a pure transform/opacity glide — no layout churn. Skipped on the
// FIRST render (a reload recreates every tile at once — animate that and the
// whole grid flashes in, and it would collide with the reattach/repaint path),
// while dragging, or for a tile that landed hidden (other tab). Does NOT honor
// prefers-reduced-motion (see the note by flipCapture) — the close-fold doesn't.
let _firstRender = true;
function flipEnter(el) {
  if (reducedMotion || document.body.classList.contains('dragging') || el.dataset.closing) return;
  const r = el.getBoundingClientRect();
  if (r.width <= 1 || r.height <= 1) return;   // hidden (background tab) — nothing to animate
  // Unfold from a thin sliver to full width (scaleX 0→1, anchored at the left
  // edge) — the mirror of the close-fold, and dramatic enough to read as "a tile
  // opened". Transform-only, so the iframe still lays out at full size once (its
  // content is briefly squished as it unfolds; a connecting terminal is blank
  // anyway). A quick opacity fade softens the leading edge.
  el.style.transition = 'none';
  el.style.transformOrigin = 'left center';
  el.style.transform = 'scaleX(0)';
  el.style.opacity = '0';
  void el.offsetWidth;   // commit the start state before transitioning
  const token = ++_flipSeq;
  el.dataset.flip = String(token);
  el.style.transition = 'transform ' + ENTER_MS + 'ms cubic-bezier(.2,.7,.3,1), opacity ' + (ENTER_MS / 2) + 'ms ease';
  el.style.transform = 'none';
  el.style.opacity = '';
  const clear = () => {
    if (el.dataset.flip !== String(token)) return;   // a later flip/enter owns it now
    delete el.dataset.flip;
    el.style.transition = '';
    el.style.transform = '';
    el.style.transformOrigin = '';
    el.style.opacity = '';
  };
  el.addEventListener('transitionend', clear, { once: true });
  setTimeout(clear, ENTER_MS + 120);
}

// Consecutive polls that looked like a mass tile disappearance (see render()).
let _suspectDrops = 0;
const __dashDiag = (window.__dashDiag = window.__dashDiag || { suspectSkips: 0 });
// True when this poll's session set would reap several live (not-closing) tiles
// at once — the signature of a degraded /api/sessions response (every port_alive
// probe timed out under load), not of real user closes (which go via closingIds).
function _isSuspectMassDrop(sessionsAll) {
  const ids = new Set(sessionsAll.map(s => s.id));
  let live = 0, gone = 0;
  for (const [id, el] of tiles) {
    if (el.dataset.closing || closingIds.has(id)) continue;   // already on its way out
    live++;
    if (!ids.has(id)) gone++;
  }
  // Several live tiles, and at least half of them vanished in this single poll.
  return live >= 3 && gone >= 3 && gone * 2 >= live;
}

function render(sessionsAll, home) {
  // Degraded-poll guard: don't let one bad /api/sessions response reap every
  // tile. Each port_alive() probe is a 0.2 s connect run serially over every
  // session; under a CPU spike (~20 tiles + a concurrent heavy test run) they can
  // all time out in ONE poll, returning an empty/short list though the sessions
  // are alive — the "all terminals showed disconnecting and vanished" report.
  // Require the mass disappearance to repeat across two consecutive polls before
  // believing it: a one-cycle blip is ridden out; a genuine mass close still
  // clears after ~one extra poll. (Backend ALIVE_HYSTERESIS is the first line of
  // defence — keeping live sessions listed; this is the second.)
  if (_isSuspectMassDrop(sessionsAll)) {
    if (++_suspectDrops < 2) { __dashDiag.suspectSkips++; return; }
  } else {
    _suspectDrops = 0;
  }
  const prevHome = homeDir;
  homeDir = home || homeDir;
  const _flipFirst = flipCapture();   // pre-change geometry — survivors glide to their new size below
  const _entering = [];               // tiles created this render — animated in below (unless first render)
  // Split stashed entries off the top: the dashboard treats them as if they
  // weren't there (no tile, no tab counted), but the stash drawer still lists
  // them so the user can pop one back in. The backing ttyd/container keeps
  // running — stash is a UI-state flag, not a process operation.
  const sessions = sessionsAll.filter(s => !s.stashed);
  renderStashDrawer(sessionsAll.filter(s => s.stashed));
  // Only the active terminal and these tiles share one main thread (the ttyd
  // iframes are same-site), so doing layout work every 3 s poll competes with
  // xterm.js rendering and shows up as periodic lag. Track whether anything
  // actually changed and skip the relayout when it didn't.
  // orderChanged → relayout (applyOrder); orderPersist → also write the list
  // back (saveOrder). They differ for a brand-new session that just APPEARED on
  // a poll: it's appended in-memory so it renders, but a window that didn't
  // initiate it must NOT persist that end-append. Persisting it makes several
  // open windows race their saves — the window that placed a freshly-cloned
  // tile next to its source then ADOPTS another window's end-append via the
  // storage listener ("clone not next to the active tile; orders scrambled").
  // Only a genuine action persists: a user reorder, an AUTHORITATIVE duplicate
  // placement (a pendingDup match), or a close/prune.
  let orderChanged = false, orderPersist = false, structural = false, dupReveal = null;
  // Removal is keyed on the FULL live set (stashed included): a stashed tile is
  // kept ALIVE and hidden — not torn down — so the session keeps receiving its
  // BEL and can auto-unstash on a ring. A tile is only removed when its session
  // truly goes away (closed → absent from sessionsAll).
  const ids = new Set(sessionsAll.map(s => s.id));
  for (const id of closingIds) {
    if (!ids.has(id)) {   // server reaped the closed session — stop suppressing + relayout
      closingIds.delete(id); structural = true;
      if (orderList.includes(id)) { orderList = orderList.filter(x => x !== id); orderChanged = true; orderPersist = true; }
    }
  }
  for (const [id, el] of tiles) {
    if (!ids.has(id)) {
      // A tile still folding lets its animation own removal — but only while it's
      // actually ON SCREEN. A reaped tile that's flagged closing yet hidden
      // (offsetParent null) got no transitionend and may have lost its fallback
      // timer; reclaim it here instead of skipping it forever.
      if (el.dataset.closing && el.offsetParent !== null) continue;
      el.remove(); tiles.delete(id); structural = true;
      if (orderList.includes(id)) { orderList = orderList.filter(x => x !== id); orderChanged = true; orderPersist = true; }
      // Drop the reaped session's persisted condensed flag so the
      // localStorage set doesn't accumulate dead ids.
      if (condensedIds.has(id)) { condensedIds.delete(id); saveCondensed(); }
    }
  }
  // Iterate ALL sessions (stashed included) so a stashed session still gets a
  // live (hidden) tile — on a fresh load too — and can ring → auto-unstash.
  for (const s of sessionsAll) {
    if (closingIds.has(s.id)) continue;   // mid-close: its tile is animating away — don't recreate
    let el = tiles.get(s.id);
    if (el) {
      // Live-update mutable fields without disturbing focus or reloading iframes.
      if (s.kind === 'webview') {
        const nm = el.querySelector('.name'); if (nm) nm.textContent = s.name || s.url || s.id;
        const ub = el.querySelector('.urlbar input');
        if (ub && document.activeElement !== ub && ub.value !== (s.url || '')) ub.value = s.url || '';
      } else {
        el.dataset.cwd = s.cwd || '';
        const cw = el.querySelector('.cwd'); if (cw) cw.textContent = s.cwd || '';
      }
      // Keep the hidden/visible state in sync with the server's stash flag (it
      // can flip from the drawer, another client, or doStash). A change toggles
      // layout (visible count / row mode), so flag a relayout.
      const wasStashed = el.dataset.stashed === '1';
      el.dataset.stashed = s.stashed ? '1' : '';
      if (wasStashed !== !!s.stashed) structural = true;
      continue;
    }
    el = document.createElement('div');
    el.className = 'tile' + (s.kind === 'webview' ? ' webview' : '');
    el.dataset.kind = s.kind || 'host';   // read by applyVisibility (channel tiles bypass the tab filter)
    el.dataset.cwd = s.cwd || '';
    el.dataset.stashed = s.stashed ? '1' : '';   // born stashed (e.g. on a reload): alive but hidden
    // Assign the per-tile title colour once, via a CSS variable on the .tile
    // element. The .name rule reads var(--tile-title-color); the bell-on
    // rule overrides it with higher specificity so a ringing tile still
    // flashes green regardless of which colour it normally is.
    // Channel tiles use the fixed channel identity colour (CHANNEL_TITLE_COLOR)
    // rather than the per-tile palette, so the title-bar icon, the title-bar
    // name and the chatroom header all share one colour.
    el.style.setProperty('--tile-title-color',
      s.kind === 'channel' ? CHANNEL_TITLE_COLOR : tileTitleColor(s.id));
    const head = document.createElement('div');
    head.className = 'head';
    if (s.port) head.title = ':' + s.port;   // port shows on hover, not inline
    // The condense toggle sits at the right of the head, which the overlapping
    // neighbour covers once the card is parked — so a click anywhere on the
    // exposed spine expands it. Normal (uncondensed) clicks just select.
    head.onclick = () => {
      if (el.classList.contains('condensed')) { setCondensed(s.id, false); return; }
      selectTile(s.id);
    };
    // Drag the title bar to rearrange. Reorder via CSS `order` (see reorder/
    // applyOrder) so the iframe is never moved in the DOM and won't reload.
    head.draggable = true;
    head.addEventListener('dragstart', (e) => {
      dragId = s.id;
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', s.id); } catch (e2) {}
      document.body.classList.add('dragging');
    });
    head.addEventListener('dragend', () => {
      dragId = null;
      document.body.classList.remove('dragging');
      for (const [, t] of tiles) t.classList.remove('drop-before', 'drop-after');
    });
    el.addEventListener('dragover', (e) => {
      if (!dragId || dragId === s.id) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const r = el.getBoundingClientRect();
      const after = (e.clientX - r.left) > r.width / 2;
      el.classList.toggle('drop-after', after);
      el.classList.toggle('drop-before', !after);
    });
    el.addEventListener('dragleave', () => el.classList.remove('drop-before', 'drop-after'));
    el.addEventListener('drop', (e) => {
      el.classList.remove('drop-before', 'drop-after');
      if (!dragId || dragId === s.id) return;
      e.preventDefault();
      const r = el.getBoundingClientRect();
      const after = (e.clientX - r.left) > r.width / 2;
      reorder(dragId, s.id, after);
    });

    // Webview tile: no backing process, no ttyd. Just an editable URL bar above
    // an iframe pointing at whatever the user types. Cross-origin sites that
    // forbid framing (X-Frame-Options/CSP) will refuse to load — the address
    // bar still works; the ↗ link opens in a new tab as a fallback.
    if (s.kind === 'webview') {
      const wbadge = document.createElement('span');
      wbadge.className = 'badge webview'; wbadge.textContent = 'web';
      const wicon = makeIconSpan();
      el.dataset.iconKind = 'webview';
      paintIcon(wicon, s.name || s.url || s.id, s.name || s.url || s.id, '', 'webview');
      const wname = document.createElement('span');
      wname.className = 'name';
      wname.textContent = s.name || s.url || s.id;
      const wopen = document.createElement('a');
      wopen.className = 'open'; wopen.target = '_blank'; wopen.title = 'open in new tab';
      wopen.textContent = '↗'; wopen.href = s.url || 'about:blank'; wopen.draggable = false;
      const wstash = document.createElement('button');
      wstash.className = 'stash';
      wstash.title = 'stash — hide this tile (restore from header)';
      wstash.textContent = '≡';
      wstash.onclick = (e) => { e.stopPropagation(); wstash.disabled = true; doStash(s.id, true); };
      const wclose = document.createElement('button');
      wclose.className = 'close'; wclose.title = 'remove this tile'; wclose.textContent = '✕';
      wclose.onclick = (e) => { e.stopPropagation();
        if (confirm('Remove web tile "' + (s.name || s.url || s.id) + '"?')) doClose(s.id); };
      head.append(wicon, wbadge, wname, wopen, wstash, wclose);

      const bar = document.createElement('div'); bar.className = 'urlbar';
      const input = document.createElement('input');
      input.type = 'text'; input.value = s.url || ''; input.spellcheck = false;
      input.placeholder = 'https://…';
      const wf = document.createElement('iframe');
      wf.setAttribute('referrerpolicy', 'no-referrer');
      wf.setAttribute('allow', 'clipboard-read; clipboard-write; fullscreen');
      // Proxy mode: route the iframe through /proxy so the page loads under
      // http://127.0.0.1:7680. Use this when the target page is HTTPS but
      // needs to open ws:// to a LAN service (the browser otherwise blocks
      // insecure WebSockets from a secure page as mixed content).
      let proxyOn = !!s.proxy;
      // Proxy mode loads /proxy?id=<sid>&csrf=… (the server resolves the URL
      // from the registry — never trusts a raw `url=` param) so a malicious
      // page can't use the dashboard as an SSRF gadget against the user's LAN.
      const srcFor = (u) => {
        if (!u) return 'about:blank';
        if (!proxyOn) return u;
        return BASE + '/proxy?id=' + encodeURIComponent(s.id) + '&csrf=' + encodeURIComponent(CSRF);
      };
      wf.src = srcFor(s.url);
      const navigate = async () => {
        const v = input.value.trim(); if (!v) return;
        const normalized = (v.includes('://') || v.startsWith('//')) ? v : ('https://' + v);
        input.value = normalized; wopen.href = normalized;
        wf.src = srcFor(normalized);
        try { await fetch('/api/webview?id=' + encodeURIComponent(s.id) +
              '&url=' + encodeURIComponent(normalized),
              { method: 'POST', headers: { 'X-CSRF-Token': CSRF } }); } catch (e2) {}
      };
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); navigate(); }
      });
      const go = document.createElement('button'); go.textContent = 'Go';
      go.onclick = (e) => { e.stopPropagation(); navigate(); };
      const reload = document.createElement('button'); reload.textContent = '↻';
      reload.title = 'reload';
      reload.onclick = (e) => { e.stopPropagation();
        try { wf.contentWindow.location.reload(); } catch (e2) { wf.src = srcFor(input.value); } };
      const proxy = document.createElement('button'); proxy.className = 'proxy';
      proxy.textContent = 'P';
      proxy.title = 'proxy mode: load via dashboard so the iframe is plaintext (lets ws:// work from an https:// page)';
      if (proxyOn) proxy.classList.add('on');
      proxy.onclick = async (e) => {
        e.stopPropagation();
        proxyOn = !proxyOn;
        proxy.classList.toggle('on', proxyOn);
        wf.src = srcFor(input.value.trim() || s.url);
        try { await fetch('/api/webview?id=' + encodeURIComponent(s.id) +
              '&proxy=' + (proxyOn ? '1' : '0'),
              { method: 'POST', headers: { 'X-CSRF-Token': CSRF } }); } catch (e2) {}
      };
      bar.append(reload, input, go, proxy);
      el.append(head, bar, wf);
      grid.appendChild(el);
      tiles.set(s.id, el);
      _entering.push(el);
      structural = true;
      if (!orderList.includes(s.id)) { orderList.push(s.id); orderChanged = true; }
      continue;
    }

    // Channel tiles have no backing port — they're served from the dashboard
    // itself at /channel/<name> (same origin, no ttyd/iframe-port plumbing).
    // For everything else, iframe at the session's allocated ttyd port.
    const isChannel = s.kind === 'channel';
    const isNote = s.kind === 'note';
    // Channel + note tiles have no backing port — they're served from the
    // dashboard itself (same origin) at /channel/<name> resp. /note/<id>, no
    // ttyd/iframe-port plumbing.
    const url = isChannel
      ? (location.origin + BASE + '/channel/' + encodeURIComponent(s.channel || s.name || s.id))
      : isNote
      ? (location.origin + BASE + '/note/' + encodeURIComponent(s.id))
      : PROXY_TTYD
      ? (location.origin + BASE + '/t/' + s.port + '/')
      : ('http://127.0.0.1:' + s.port + '/');
    // sid + start time let the custom client key its scrollback storage and tag
    // the title/bell messages it posts back to us. Channels/notes don't use
    // those (they GET/POST same-origin), so the params would be harmless extras.
    // `dash` = this dashboard's own port, so the tile's term-client knows where to
    // poll /api/tile-image (out-of-band inline images). Passed explicitly because
    // document.referrer is unreliable (a self-reload makes it self-referential).
    const turl = (isChannel || isNote) ? url
      : (url + '?sid=' + encodeURIComponent(s.id) + '&ts=' + encodeURIComponent(s.started || '')
         + '&dash=' + encodeURIComponent(location.port || '7680')
         + '&kind=' + encodeURIComponent(s.kind || ''));

    const badge = document.createElement('span');
    // "Terminal in container" tiles are kind=terminal + container=true on the
    // wire — render them with the teal container-terminal badge so users can
    // distinguish them at a glance from plain host terminals.
    const isContainerTerm = s.kind === 'terminal' && s.container;
    const badgeKind = isContainerTerm ? 'container-terminal' : (s.kind || 'host');
    badge.className = 'badge ' + badgeKind;
    badge.textContent = isContainerTerm ? 'sh-box' : s.kind;
    const icon = makeIconSpan();
    el.dataset.iconKind = badgeKind;   // read on title-change to repaint consistently
    paintIcon(icon, s.name || s.id, s.name || s.id, s.cwd, badgeKind);
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = s.name || s.id;
    name.title = (s.name || s.id) + '\n' + s.id;   // hover shows the title + session id
    const cwd = document.createElement('span');
    cwd.className = 'cwd';
    cwd.textContent = s.cwd || '';
    const open = document.createElement('a');
    open.className = 'open';
    open.target = '_blank';
    open.title = 'open in new tab';
    open.textContent = '↗';
    open.href = turl;
    open.draggable = false;  // don't let the link hijack the title-bar drag
    const reloadBtn = document.createElement('button');
    reloadBtn.className = 'reload';
    reloadBtn.title = 're-attach — reload the terminal, reconnect to the same running process (claude is NOT restarted). Shift+click: clean reload (also forget saved scrollback — clears duplicated/garbled history)';
    reloadBtn.textContent = '↻';
    reloadBtn.onclick = (e) => { e.stopPropagation(); reloadTile(s.id, e.shiftKey); };
    const dupBtn = document.createElement('button');
    dupBtn.className = 'dup';
    dupBtn.title = 'duplicate — start another session in the same directory';
    dupBtn.textContent = '⧉';
    dupBtn.onclick = async (e) => {
      e.stopPropagation();
      dupBtn.disabled = true;
      // Drop a placeholder tile with a spinner RIGHT AWAY, slotted into the
      // order right after this source via a marker id, so the user gets instant
      // feedback instead of staring at an unchanged grid for the 1–3 s it takes
      // the new ttyd to boot. placeNewInOrder swaps the real id into the same
      // slot when the session registers.
      const marker = '__loading:' + s.id + ':' + Date.now();
      const placeholder = loadingTile(el, s);
      placeholder.dataset.marker = marker;
      let si = orderList.indexOf(s.id);
      if (si < 0) si = orderList.length - 1;
      orderList.splice(si + 1, 0, marker);
      grid.appendChild(placeholder);
      applyOrder(); applyVisibility();
      try { placeholder.scrollIntoView({ inline: 'nearest', block: 'nearest' }); } catch (e2) {}
      pendingDups.push({ srcId: s.id, cwd: s.cwd || '', until: Date.now() + 90000, marker: marker, placeholder: placeholder });
      try { await fetch('/api/duplicate?id=' + encodeURIComponent(s.id), { method: 'POST', headers: { 'X-CSRF-Token': CSRF } }); } catch (e) {}
      // the new session takes a moment to start its ttyd; poll a few times to pick it up
      setTimeout(poll, 800); setTimeout(poll, 1600); setTimeout(poll, 2800); setTimeout(poll, 4200);
      setTimeout(() => { dupBtn.disabled = false; }, 1500);
      // Stale-placeholder cleanup if no matching session ever shows up.
      setTimeout(() => {
        const idx = pendingDups.findIndex(p => p.marker === marker);
        if (idx < 0) return;
        pendingDups.splice(idx, 1);
        const mi = orderList.indexOf(marker);
        if (mi >= 0) orderList.splice(mi, 1);
        if (placeholder.parentNode) placeholder.remove();
        applyOrder(); applyVisibility();
      }, 90000);
    };
    // Fork: copies the current conversation state into a new session that
    // resumes from the same point — so both can diverge. Only meaningful for
    // claude tiles (host/container); terminals have no conversation, so the
    // button stays hidden there. Reuses the same loading-placeholder UX as dup
    // because spawning the new ttyd takes ~1–3 s.
    const forkBtn = document.createElement('button');
    forkBtn.className = 'fork';
    forkBtn.title = 'fork — copy the conversation and continue it in a new session, diverging from this point';
    forkBtn.textContent = '⑂';
    // Fork is "copy claude jsonl + --resume"; only makes sense for claude
    // sessions (host/container). Terminals/webviews/opencode don't have a
    // claude conversation to fork.
    if (s.kind === 'terminal' || s.kind === 'webview' || s.kind === 'opencode' || s.kind === 'codex' || s.kind === 'custom' || s.kind === 'note') forkBtn.style.display = 'none';
    // Notes have no process to "duplicate" — hide it (a note's content lives in
    // its own sidecar; cloning would need a body copy we don't do in v1).
    if (s.kind === 'note') dupBtn.style.display = 'none';
    forkBtn.onclick = async (e) => {
      e.stopPropagation();
      forkBtn.disabled = true;
      const marker = '__loading:fork:' + s.id + ':' + Date.now();
      const placeholder = loadingTile(el, s);
      placeholder.dataset.marker = marker;
      placeholder.querySelector('.name').textContent = 'forking…';
      let si = orderList.indexOf(s.id);
      if (si < 0) si = orderList.length - 1;
      orderList.splice(si + 1, 0, marker);
      grid.appendChild(placeholder);
      applyOrder(); applyVisibility();
      try { placeholder.scrollIntoView({ inline: 'nearest', block: 'nearest' }); } catch (e2) {}
      pendingDups.push({ srcId: s.id, cwd: s.cwd || '', until: Date.now() + 90000, marker: marker, placeholder: placeholder });
      try { await fetch('/api/fork?id=' + encodeURIComponent(s.id), { method: 'POST', headers: { 'X-CSRF-Token': CSRF } }); } catch (e3) {}
      setTimeout(poll, 800); setTimeout(poll, 1600); setTimeout(poll, 2800); setTimeout(poll, 4200);
      setTimeout(() => { forkBtn.disabled = false; }, 1500);
      setTimeout(() => {
        const idx = pendingDups.findIndex(p => p.marker === marker);
        if (idx < 0) return;
        pendingDups.splice(idx, 1);
        const mi = orderList.indexOf(marker);
        if (mi >= 0) orderList.splice(mi, 1);
        if (placeholder.parentNode) placeholder.remove();
        applyOrder(); applyVisibility();
      }, 90000);
    };
    // Chat: toggle an xterm-free view of the conversation. Overlays a second
    // iframe (/chat-panel, an SSE tail of claude's own .jsonl) on top of the
    // terminal iframe — the terminal keeps its ttyd websocket alive underneath,
    // so toggling back is instant (a repaint postMessage redraws it). Only claude
    // tiles have a transcript, so hide it where fork is hidden.
    let chatFrame = null, chatOn = false;
    const chatBtn = document.createElement('button');
    chatBtn.className = 'chat';
    chatBtn.title = 'chat — read the conversation without xterm.js (live, from claude’s transcript). Click again to return to the terminal.';
    chatBtn.textContent = '\u{1F4AC}';
    if (s.kind === 'terminal' || s.kind === 'webview' || s.kind === 'opencode' || s.kind === 'codex' || s.kind === 'custom' || s.kind === 'note') chatBtn.style.display = 'none';
    chatBtn.onclick = (e) => {
      e.stopPropagation();
      chatOn = !chatOn;
      chatBtn.classList.toggle('on', chatOn);
      if (chatOn) {
        if (!chatFrame) {
          chatFrame = document.createElement('iframe');
          chatFrame.src = 'chat-panel?id=' + encodeURIComponent(s.id);  // BASE_PATH-relative
          chatFrame.setAttribute('allow', 'clipboard-write');
          el.append(chatFrame);   // .tile iframe CSS makes it fill the body
        }
        f.style.display = 'none';
        chatFrame.style.display = '';
      } else {
        if (chatFrame) chatFrame.style.display = 'none';
        f.style.display = '';
        // Ask the client for a program repaint of the just-revealed terminal —
        // pty-only (see _repaintWiggle), so nothing visibly moves.
        setTimeout(askRepaint, 0); setTimeout(askRepaint, 300);
      }
    };
    // Stash: hide the tile, keep the backing process running. Restores via the
    // "Stashed (N)" drawer in the header. Webviews ARE stashable too — they
    // have no backing process, but stashing still lets the user declutter the
    // grid and bring the URL bar back later.
    // Condense: park the tile at ⅓ width until it rings (see setCondensed).
    // Lighter than stash — the terminal stays visible, just narrow.
    const condBtn = document.createElement('button');
    condBtn.className = 'cond';
    condBtn.textContent = '⤡';
    condBtn.title = 'condense — park this tile as a card until it rings (bell restores it)';
    condBtn.onclick = (e) => {
      e.stopPropagation();
      setCondensed(s.id, !el.classList.contains('condensed'));
    };
    const stashBtn = document.createElement('button');
    stashBtn.className = 'stash';
    stashBtn.title = 'stash — hide this tile (process keeps running, restore from header)';
    stashBtn.textContent = '≡';
    stashBtn.onclick = (e) => {
      e.stopPropagation();
      stashBtn.disabled = true;
      doStash(s.id, true);
    };
    // Discard from Home: drop this card off the Home tab until the tile next
    // rings. Only visible while Home is the active tab (CSS). NOT ✕ — that's
    // close, which kills the process; this just clears the attention flag.
    const discardBtn = document.createElement('button');
    discardBtn.className = 'home-discard';
    discardBtn.title = 'dismiss from Home — hide this card here until the tile next needs attention';
    discardBtn.textContent = '⊘';
    discardBtn.onclick = (e) => { e.stopPropagation(); discardFromHome(s.id); };
    const closeBtn = document.createElement('button');
    closeBtn.className = 'close';
    closeBtn.title = 'close (ends the underlying process)';
    closeBtn.textContent = '✕';
    closeBtn.onclick = (e) => {
      e.stopPropagation();
      const what = s.kind === 'terminal' ? 'the shell'
                 : s.kind === 'opencode' ? 'the opencode session'
                 : s.kind === 'codex' ? 'the codex session'
                 : s.kind === 'custom' ? 'this launcher session'
                 : s.kind === 'note' ? 'this note — its saved text/images are deleted'
                 : s.kind === 'channel' ? 'this chatroom tile'
                 : 'the claude process';
      const portStr = s.port ? ' :' + s.port : '';   // notes/channels have no port
      if (confirm('Close "' + (s.name || s.id) + '"' + portStr + '?\nThis ends ' + what + '.')) doClose(s.id);
    };
    head.append(icon, badge, name, cwd, open, reloadBtn, chatBtn, forkBtn, dupBtn, discardBtn, condBtn, stashBtn, closeBtn);

    const f = document.createElement('iframe');
    f.src = turl;
    f.setAttribute('allow', 'clipboard-read; clipboard-write');
    // Blank-on-reattach (claude, an Ink TUI, only repaints on an actual size
    // change) is handled INSIDE the custom client: term-client wiggles the
    // PTY rows (rows-1 → true size) right after each websocket open — see
    // _repaintWiggle. The dashboard used to bounce the iframe height (-48px
    // and back, twice, on load) for the same effect, which read as the tile
    // content jumping up and down for ~1s after every (re)load. The repaint
    // postMessage below (chat toggle) reuses the client's wiggle.
    const askRepaint = () => {
      const target = frameTargetOrigin(f); if (!target) return;
      try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'repaint' }, target); } catch (e) {}
    };
    el.append(head, f);
    grid.appendChild(el);
    inputObserver.observe(f);   // gate this terminal's input when it scrolls out of view
    // Hover-peek for parked cards: while condensed the tile shows a frozen
    // snapshot with the live canvas still painting underneath (term-client
    // setPeek), so revealing current content costs only DROPPING that overlay —
    // no WebGL re-acquire. Tell the client to drop it while the pointer is over
    // the card and re-freeze (on the now-current frame) when it leaves. Only
    // terminal tiles freeze (setCondensed gates the same kinds), so only they
    // get peek. mouseenter/leave fire on the .tile box even though its child is a
    // cross-origin iframe: moving head→iframe stays inside the box (no spurious
    // leave), so the pair cleanly brackets "pointer is over this card". The
    // client no-ops peek unless it's actually condensed, so a stale leave after
    // an expand/bell is harmless.
    if (s.kind !== 'channel' && s.kind !== 'note' && s.kind !== 'webview') {
      const peek = (on) => {
        if (!el.classList.contains('condensed')) return;
        const target = frameTargetOrigin(f); if (!target) return;
        try { f.contentWindow.postMessage({ type: 'claude-host', cmd: 'peek', on }, target); } catch (e) {}
      };
      el.addEventListener('mouseenter', () => peek(true));
      el.addEventListener('mouseleave', () => peek(false));
    }
    tiles.set(s.id, el);
    _entering.push(el);
    // Re-apply a persisted condensed state (tile elements are rebuilt from
    // scratch on a dashboard reload; the set survives in localStorage). Pass
    // relayout=false — render's tail relayouts; calling applyVisibility here
    // would reenter it mid-build.
    if (condensedIds.has(s.id)) setCondensed(s.id, true, false);
    structural = true;
    if (!orderList.includes(s.id)) {
      // A pendingDup match is THIS window's own clone landing next to its
      // source — authoritative, so persist it. A bare append (placeNewInOrder
      // returned false) is a session this window merely learned about; render
      // it but don't persist the end-append (another window may own a better
      // placement we'll adopt via the storage listener).
      if (placeNewInOrder(s)) { dupReveal = el; orderPersist = true; }
      orderChanged = true;
    }
  }
  // Apply the relayout with the per-call FLIP muted (this render owns the single
  // play below — otherwise the applyVisibility nested inside layoutTabs would try
  // to animate too, from a half-mutated state).
  _suppressFlip = true;
  try {
    if (orderPersist) saveOrder();
    if (structural || orderChanged) applyOrder();
    if (structural || homeDir !== prevHome) layoutTabs(sessions);
    // A duplicate / new tile just landed next to its anchor — scroll it into view
    // so the row layout doesn't hide it off-screen ("the clone did nothing"). We
    // scroll by LAYOUT geometry (rect.left — correct because the entrance unfold
    // is anchored left-center — plus offsetWidth, which transforms don't affect)
    // rather than scrollIntoView, which would chase the mid-unfold sliver and
    // land short. block:'nearest' vertical is irrelevant in the horizontal row.
    if (dupReveal) { const el = dupReveal; setTimeout(() => { try {
      const gr = grid.getBoundingClientRect(), er = el.getBoundingClientRect();
      const left = er.left - gr.left + grid.scrollLeft, right = left + el.offsetWidth;
      if (right > grid.scrollLeft + grid.clientWidth) grid.scrollLeft = right - grid.clientWidth + 8;
      else if (left < grid.scrollLeft) grid.scrollLeft = left - 8;
    } catch (e) {} }, 60); }
    refreshGridChrome();   // "N active" + global empty-state + grid show/hide (favorite-aware)
  } finally { _suppressFlip = false; }
  // Glide the survivors from their old size to the new one — only structural
  // changes (a tile joined/left, stash flip, grid↔row threshold) resize tiles.
  if (structural) flipPlay(_flipFirst);
  // Fade/scale freshly-opened tiles in — but not on the initial page render
  // (a load/reload recreates every tile; let those just appear).
  if (!_firstRender) for (const el of _entering) flipEnter(el);
  _firstRender = false;
}

// Group tiles into workdir tabs, (re)build the tab bar, and show only the
// active tab's tiles. Recomputed every render so sessions regroup live (e.g.
// when a new session at a parent dir appears and absorbs its sub-dir sessions).
function layoutTabs(sessions) {
  lastLayoutSessions = sessions;
  // Tabs earn their keep with several sessions OR whenever a workdir is
  // favorited (a favorite keeps its tab even with 0 tiles). With ≤2 sessions
  // and no favorites, just show them side by side, untabbed.
  if (sessions.length < 3 && favorites.length === 0) {
    activeTab = null;
    while (tabsEl.firstChild) tabsEl.removeChild(tabsEl.firstChild);
    tabsEl.hidden = true;
    document.body.classList.remove('has-tabs');
    for (const [, el] of tiles) el.style.display = el.dataset.stashed === '1' ? 'none' : '';  // stashed stay hidden
    grid.classList.remove('row');   // ≤2 sessions never use the horizontal-row layout
    _setTabEmpty(false);
    return;
  }
  tabsEl.hidden = false;
  document.body.classList.add('has-tabs');
  // Favorites join the grouping anchors so a tile in a SUB-dir of a favorite
  // groups under the favorite's tab even when the favorite itself has no tile.
  const cwds = sessions.map(s => s.cwd || '').concat(favorites);
  curTabCount = new Map();
  for (const f of favorites) curTabCount.set(f, 0);   // empty favorites still earn a tab
  for (const s of sessions) {
    const key = tabKeyFor(s.cwd || '', cwds, homeDir);
    const el = tiles.get(s.id); if (el) el.dataset.tab = key;
    curTabCount.set(key, (curTabCount.get(key) || 0) + 1);
  }
  buildTabBar();
}

// (Re)build the tab bar from curTabCount, honoring the manual tab order. Split
// out so a tab drag-reorder can rebuild without recomputing grouping.
function buildTabBar() {
  const keys = orderTabKeys([...curTabCount.keys()]);
  // After a reload, restore the last-viewed tab once it's present (it may take
  // a poll or two to appear); a tab click clears savedTab so it can't later
  // override the user's choice. Otherwise keep the current tab, falling back to
  // the first one when it's gone.
  if (savedTab !== null && (savedTab === HOME_KEY || keys.includes(savedTab))) {
    activeTab = savedTab; savedTab = null;
    if (activeTab === HOME_KEY) rebuildHomeShown();   // restore onto Home → seed its batch
  }
  else if (activeTab !== HOME_KEY && !keys.includes(activeTab)) activeTab = keys[0] || null;
  while (tabsEl.firstChild) tabsEl.removeChild(tabsEl.firstChild);
  tabsEl.appendChild(buildHomeTab());   // Home is always first; workdir tabs follow
  for (const k of keys) {
    const b = document.createElement('button');
    b.className = 'tab';
    b.dataset.key = k;
    b.title = k;
    b.draggable = true;
    const n = curTabCount.get(k) || 0;
    const label = document.createElement('span'); label.textContent = tabLabel(k);
    // The count bullet and the pin share one slot: the pin sits ABSOLUTELY over
    // the count (revealed on tab hover), so it never adds width or shifts the
    // label — it just appears on top of the little number.
    const slot = document.createElement('span'); slot.className = 'cntslot';
    const cnt = document.createElement('span'); cnt.className = 'n'; cnt.textContent = String(n);
    if (n === 0) cnt.style.display = 'none';   // empty favorite tab: the 0 is just noise
    // Pin toggles "favorite": a pinned tab survives its last tile closing.
    // Hidden at rest, revealed over the count on tab hover; solid when pinned,
    // faint when not. stopPropagation so the pin click toggles the bookmark
    // without also selecting the tab.
    const fav = document.createElement('span');
    fav.className = 'fav' + (isFavorite(k) ? ' on' : '');
    fav.textContent = '📌';
    fav.title = isFavorite(k)
      ? 'pinned — this workdir keeps its tab even with no tiles (click to unpin)'
      : 'pin this workdir — keep its tab (and “＋ New opens here”) even with no tiles';
    fav.onclick = (e) => { e.stopPropagation(); toggleFavorite(k); };
    slot.append(cnt, fav);
    b.append(label, slot);
    if (n === 0) b.classList.add('empty');   // favorite with no live tiles → italic/dim label
    b.onclick = () => {
      if (k === activeTab) return;
      rememberTabScroll();                                             // stash where we left the current tab
      activeTab = k;
      savedTab = null;
      saveActiveTab();
      b.classList.remove('bell');                                      // viewing the tab clears its bell…
      for (const [, el] of tiles) if (el.dataset.tab === k) el.classList.remove('bell');  // …and its tiles'
      updateDockBadge();                                               // …and the Dock count
      updateHome();                                                    // …and the Home tab's tally
      applyVisibility();
      restoreTabScroll(k);   // land back where this tab was last scrolled, not its leftmost
    };
    b.addEventListener('dragstart', (e) => {
      tabDragKey = k; e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', k); } catch (e2) {}
      b.classList.add('dragging');
    });
    b.addEventListener('dragend', () => {
      tabDragKey = null;
      for (const x of tabsEl.children) x.classList.remove('dragging', 'drop-before', 'drop-after');
    });
    b.addEventListener('dragover', (e) => {
      if (!tabDragKey || tabDragKey === k) return;
      e.preventDefault(); e.dataTransfer.dropEffect = 'move';
      const r = b.getBoundingClientRect();
      const after = (e.clientX - r.left) > r.width / 2;
      b.classList.toggle('drop-after', after);
      b.classList.toggle('drop-before', !after);
    });
    b.addEventListener('dragleave', () => b.classList.remove('drop-before', 'drop-after'));
    b.addEventListener('drop', (e) => {
      b.classList.remove('drop-before', 'drop-after');
      if (!tabDragKey || tabDragKey === k) return;
      e.preventDefault();
      const r = b.getBoundingClientRect();
      const after = (e.clientX - r.left) > r.width / 2;
      reorderTab(tabDragKey, k, after);
    });
    tabsEl.appendChild(b);
  }
  applyVisibility();
  updateHome();   // paint the Home tab's attention count/glow for this layout
}

// Empty-tab hint. A favorited workdir tab can be the active tab with no tiles;
// rather than a blank grid, point the user at ＋ New (which spawns into the
// active tab's workdir). Body-fixed so it shows even when #grid is display:none
// (no live sessions at all). Created lazily, toggled by applyVisibility.
let _tabEmptyEl = null;
function _setTabEmpty(on, home) {
  if (!on) { if (_tabEmptyEl) _tabEmptyEl.style.display = 'none'; return; }
  if (!_tabEmptyEl) {
    _tabEmptyEl = document.createElement('div');
    _tabEmptyEl.id = 'tab-empty';
    document.body.appendChild(_tabEmptyEl);
  }
  _tabEmptyEl.textContent = home
    ? 'Nothing needs attention right now. Tiles that ring — a permission prompt, a finished turn — show up here.'
    : 'No tiles in “' + tabLabel(activeTab) + '”. Use ＋ New to open one here — it starts in ' + activeTab + '.';
  _tabEmptyEl.style.display = '';
}
// Header "N active" + the global empty-state placeholder + grid show/hide. Split
// out of render() so a favorite toggle (which can leave 0 tiles but a live tab)
// refreshes this chrome without a full poll.
function refreshGridChrome() {
  const n = liveTileCount();   // stashed tiles are alive-but-hidden — don't count them as "active"
  const tabbed = !tabsEl.hidden;   // 3+ sessions or a favorite produced a tab bar
  emptyEl.hidden = n > 0 || tabbed; // a favorite tab shows its own in-grid hint instead
  grid.style.display = n > 0 ? '' : 'none';
  countEl.textContent = n ? n + ' active' : '';
}

function applyVisibility() {
  // Snapshot geometry first so tiles that resize when the visible set changes
  // (tab switch, stash, a close's relayout) glide instead of jumping. A no-op
  // when render() owns the play (_suppressFlip) or nothing moved.
  const _flipFirst = flipCapture();
  const home = activeTab === HOME_KEY;   // Home shows attention tiles, ignoring workdir
  let visible = 0;
  let anyCond = false;   // any SHOWN tile parked as a card → force row mode so it can shrink
  for (const [tid, el] of tiles) {
    // Stashed tiles are kept alive (so they still ring) but never shown and
    // never counted — they don't belong to any tab and don't affect row mode.
    if (el.dataset.stashed === '1') { el.style.display = 'none'; continue; }
    // Channel tiles are top-level chatrooms, not project-scoped — keep them
    // visible in every workdir tab. Without this, a channel tile spawned
    // while activeTab is set sits in the DOM with display:none (its
    // dataset.tab is "", so the activeTab equality check below fails) and
    // the user sees nothing happen when they click a row in the Channels
    // menu. Same idea would apply to any future top-level / non-workdir
    // tile kind — keep the check generous.
    const isFloating = el.dataset.kind === 'channel';
    // On Home, the only filter is the attention batch (homeShown) — a tile stays
    // listed while you work it even after its bell clears; workdir grouping and
    // the channel-floats-everywhere rule don't apply here.
    const show = home ? homeShown.has(tid)
                      : (isFloating || el.dataset.tab === activeTab);
    el.style.display = show ? '' : 'none';
    if (show) { visible++; if (el.classList.contains('condensed')) anyCond = true; }
  }
  // include any loading placeholders so they honor the active tab and count
  // toward the row-layout threshold like real tiles. They never ring, so none
  // belong on Home.
  for (const ph of grid.querySelectorAll('.tile.loading')) {
    const show = !home && (!ph.dataset.tab || ph.dataset.tab === activeTab);
    ph.style.display = show ? '' : 'none';
    if (show) visible++;
  }
  // Home always uses the fixed-width horizontal row (as if it had 3+ tiles), so
  // an attention card never stretches to fill when only one or two are ringing —
  // the cards stay a consistent size whether 1 or 6 are pending. Workdir tabs
  // keep the count-based rule (≤2 sit side by side, 3+ switch to the row) — but
  // a parked card also forces the row, since the condense-shrink (flex-basis +
  // the iframe-width pin that keeps it SIGWINCH-safe) only exists in row mode.
  // Without this, condensing in a ≤2-tile grid only restyled the head and the
  // box never shrank ("condense does nothing").
  grid.classList.toggle('row', home || visible >= 3 || anyCond);
  for (const b of tabsEl.children) b.classList.toggle('active', b.dataset.key === activeTab);
  document.body.classList.toggle('home-active', home);   // reveals each card's discard button
  // Home orders cards by ring-arrival (newest right); restore the manual order on leave.
  if (home) applyHomeOrder();
  else if (wasHome) applyOrder();
  wasHome = home;
  markDeckShadows();   // row<->grid toggle / tab switch can change who covers a card
  // Home with nothing pending → its own hint; a favorite workdir tab with no
  // tiles → the ＋ New hint.
  if (home) _setTabEmpty(visible === 0, true);
  else _setTabEmpty(!!activeTab && visible === 0, false);
  flipPlay(_flipFirst);   // glide survivors that changed size/position
}

async function poll() {
  try {
    const r = await fetch('/api/sessions', { cache: 'no-store' });
    const data = await r.json();
    render(data.sessions || [], data.home || '');
  } catch (e) { /* keep last render */ }
}
poll();
setInterval(poll, 3000);
</script>
</body>
</html>
"""


# --- Claude chat search ("Search" header button) ---------------------------
# The "Search" button (re)builds a full-text search index of every Claude chat
# transcript by running claude-chats/claude-chat-export.py, then the client opens
# the generated index.html. The script reads ~/.claude/projects and writes a
# static site (html/md) under CHAT_HISTORY_DIR; we serve that dir read-only at
# /chat-history/ so the iframe/new-window can load it over http (file:// is
# blocked from an http page).
# Both are env-overridable so a reverse-proxied / non-dev deployment can point at
# a deployed export script and a writable output dir (the defaults are the local
# dev-checkout locations). CHAT_HISTORY_DIR must match the script's OUT_ROOT so we
# serve the same dir the export writes.
CHAT_EXPORT_SCRIPT = os.environ.get("CHAT_EXPORT_SCRIPT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "claude-chat-export.py")
CHAT_HISTORY_DIR = os.environ.get("CHAT_HISTORY_DIR") or os.path.expanduser("~/claude-chat-history")

# Standalone live-chat viewer served at /chat-panel?id=<sid>. Self-contained
# (no build step, no xterm.js): opens an EventSource on /api/chat-stream and
# renders claude's transcript turns. Dedups by uuid so EventSource auto-reconnect
# (which replays from offset 0) doesn't double-render.
CHAT_PANEL_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="__CSRF_TOKEN__">
<title>chat</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.55 -apple-system,system-ui,"Segoe UI",sans-serif;
         background:#16181d; color:#e6e6e6; }
  #log { max-width:820px; margin:0 auto; padding:16px 14px 92px; }
  .turn { margin:10px 0; padding:10px 13px; border-radius:10px; overflow-wrap:anywhere; }
  .md > :first-child { margin-top:0; } .md > :last-child { margin-bottom:0; }
  .md p { margin:.5em 0; } .md h1,.md h2,.md h3,.md h4 { margin:.7em 0 .35em; line-height:1.25; }
  .md h1 { font-size:1.35em; } .md h2 { font-size:1.2em; } .md h3 { font-size:1.07em; }
  .md ul,.md ol { margin:.4em 0; padding-left:1.5em; } .md li { margin:.15em 0; }
  .md a { color:#79b8ff; } .md hr { border:0; border-top:1px solid #333; margin:.8em 0; }
  .md blockquote { margin:.5em 0; padding:.1em .9em; border-left:3px solid #3a4456;
                   color:#b8c0cc; }
  .md code { font:12.5px ui-monospace,Menlo,monospace; background:#0d1016;
             border:1px solid #2b3340; border-radius:4px; padding:.05em .35em; }
  .md pre { background:#0d1016; border:1px solid #2b3340; border-radius:7px;
            padding:9px 11px; overflow:auto; margin:.5em 0; }
  .md pre code { background:none; border:0; padding:0; }
  .md strong { color:#fff; }
  .md table { border-collapse:collapse; margin:.6em 0; font-size:13px;
              display:block; overflow-x:auto; max-width:100%; }
  .md th, .md td { border:1px solid #2f3742; padding:4px 11px; text-align:left; white-space:nowrap; }
  .md thead th { background:#1f2530; color:#dfe6f0; font-weight:600; }
  .md tbody tr:nth-child(even) { background:#171b21; }
  .md .task { margin:.6em 0; border:1px solid #343b46; background:#191d24;
              border-radius:8px; padding:8px 11px; border-left:3px solid #5c6675; }
  .md .task.ok { border-left-color:#3f9d57; } .md .task.bad { border-left-color:#d05858; }
  .md .task-h { display:flex; align-items:center; gap:8px; }
  .md .task-badge { font-size:10px; text-transform:uppercase; letter-spacing:.04em;
                    padding:1px 7px; border-radius:9px; background:#2c3340; color:#c8d0dc; }
  .md .task.ok .task-badge { background:#1d3324; color:#9fe0b0; }
  .md .task.bad .task-badge { background:#3a1f1f; color:#f0a8a8; }
  .md .task-id { font:11px ui-monospace,Menlo,monospace; opacity:.55; }
  .md .task-sum { margin:.35em 0 0; }
  .md .task-file { margin-top:6px; font:12px ui-monospace,Menlo,monospace; opacity:.9; }
  .md .task-file a { color:#79b8ff; word-break:break-all; }
  .user { background:#26303d; border:1px solid #34435a; }
  .assistant { background:#1d2127; border:1px solid #2b3038; }
  .role { font-size:11px; letter-spacing:.04em; text-transform:uppercase;
          opacity:.55; margin-bottom:4px; }
  .user .role { color:#9ec1ff; } .assistant .role { color:#9fe0b0; }
  .turn.pending { opacity:.55; }
  .turn.pending .role .ts { color:#e0b24e; opacity:1; }
  .turn.pending .role .ts::before { content:"●"; margin-right:4px;
                                    animation:wdot 1s infinite ease-in-out; display:inline-block; }
  .tools { margin-top:8px; display:flex; flex-direction:column; gap:4px; }
  .tool { font:11.5px ui-monospace,Menlo,monospace; background:#0d1016;
          border:1px solid #2b3340; border-radius:6px; padding:3px 8px;
          display:flex; gap:9px; align-items:baseline; overflow:hidden; }
  .tool b { color:#9ec1ff; font-weight:600; flex:0 0 auto; }
  .tool .tdetail { color:#c8d0dc; white-space:pre; overflow:hidden; text-overflow:ellipsis; }
  /* Edit/Write: click the row to expand its diff */
  .tool.diffable { display:block; padding:0; background:none; border:0; overflow:visible; }
  .tool.diffable > .toolhead { display:flex; gap:9px; align-items:baseline; cursor:pointer;
    background:#0d1016; border:1px solid #2b3340; border-radius:6px; padding:3px 8px; }
  .tool.diffable > .toolhead:hover { border-color:#3a4a66; }
  .tool.diffable .caret { margin-left:auto; opacity:.55; transition:transform .12s; }
  .tool.diffable.open .caret { transform:rotate(90deg); }
  .tool.diffable .diffbody { display:none; } .tool.diffable.open .diffbody { display:block; margin-top:4px; }
  pre.diff { margin:0 0 4px; background:#0b0e14; border:1px solid #222a35; border-radius:6px;
    padding:6px 0; overflow-x:auto; font:11.5px ui-monospace,Menlo,monospace; line-height:1.45; }
  pre.diff .dl { display:block; white-space:pre; padding:0 9px; }
  pre.diff .add { color:#9be9a8; background:#0f2f1a; } pre.diff .del { color:#ffb4ac; background:#3a1518; }
  pre.diff .ctx { color:#7d889a; }
  /* TodoWrite checklist */
  .todos { background:#0d1016; border:1px solid #2b3340; border-radius:6px; padding:6px 9px; }
  .todos-h { font-size:11px; color:#9ec1ff; margin-bottom:5px; }
  .todo { display:flex; gap:8px; align-items:baseline; padding:1.5px 0; font-size:13px; }
  .todo .tbox { flex:0 0 auto; width:11px; height:11px; border-radius:3px; border:1.5px solid #5a6678;
    position:relative; top:2px; }
  .todo.in_progress .tbox { background:#e0b24e; border-color:#e0b24e; }
  .todo.completed .tbox { background:#3f9d57; border-color:#3f9d57; }
  .todo.completed { color:#8b96a6; text-decoration:line-through; }
  .todo.in_progress { color:#fff; }
  /* AskUserQuestion: the prompt + its selectable options, plus the chosen answer. */
  .qcard { background:#0d1016; border:1px solid #34507a; border-left:3px solid #79b8ff;
    border-radius:6px; padding:8px 11px; margin:2px 0; }
  .qhdr { display:inline-block; font-size:10px; letter-spacing:.04em; text-transform:uppercase;
    color:#9ec1ff; background:#1b2740; border:1px solid #34507a; border-radius:4px; padding:1px 6px; }
  .qq { font-size:14px; font-weight:600; color:#e7ecf3; margin:5px 0 7px; }
  .qopts { display:flex; flex-direction:column; gap:5px; }
  .qopt { border:1px solid #2b3340; border-radius:5px; padding:5px 8px; background:#11151c; }
  .qlabel { font-size:13px; color:#cdd6e2; font-weight:600; }
  .qdesc { font-size:11.5px; color:#8b96a6; margin-top:2px; line-height:1.35; }
  .answers { margin-top:6px; }
  .ans { display:flex; align-items:baseline; gap:6px; flex-wrap:wrap; font-size:12.5px;
    background:#0e1a12; border:1px solid #244a30; border-radius:6px; padding:5px 9px; margin:2px 0; }
  .ans .anscheck { color:#7ee787; font-weight:700; }
  .ans .ansq { color:#8b96a6; } .ans .ansarrow { color:#5a6678; }
  .ans .ansa { color:#7ee787; font-weight:600; }
  /* A live (unanswered, latest) question: options become clickable buttons that
     drive the terminal's own selector via /api/chat-key. */
  .qcard.live { border-color:#79b8ff; box-shadow:0 0 0 1px #79b8ff33; }
  .qcard.live .qopt { cursor:pointer; border-color:#3a4a66; background:#141b26;
                      transition:border-color .1s, background .1s; }
  .qcard.live .qopt:hover { border-color:#79b8ff; background:#1b2940;
                            box-shadow:0 0 0 1px #79b8ff44; }
  .qcard.live .qopt:active { background:#22344f; }
  .qcard.live .qopt:hover .qlabel { color:#fff; }
  .qcard .qhint { display:none; margin-top:7px; font-size:11.5px; color:#79b8ff;
                  opacity:.85; }
  .qcard.live .qhint { display:block; }
  .qcard .qkey { display:none; float:right; font:11px ui-monospace,Menlo,monospace;
                 color:#5a7396; border:1px solid #2b3a52; border-radius:4px;
                 padding:0 5px; margin-left:8px; }
  .qcard.live .qkey { display:inline-block; }
  .qcard .qsubmit { display:none; margin-top:8px; background:#2b6cb0; color:#fff;
                    border:0; border-radius:6px; padding:4px 12px; cursor:pointer;
                    font-size:12px; font-weight:600; }
  .qcard.live .qsubmit { display:inline-block; }
  .qcard .qsubmit:hover { background:#3279c2; }
  /* Pending permission prompt (hook-reported): approval bar above the composer. */
  #permbar { display:none; position:fixed; left:0; right:0; bottom:66px;
             justify-content:center; z-index:6; }
  #permbar.on { display:flex; }
  #permbar .pill { background:#2d2417; border:1px solid #6b5320; border-radius:14px;
                   padding:6px 13px; font-size:12.5px; color:#f0d9a8; display:flex;
                   gap:10px; align-items:center; box-shadow:0 4px 14px #0006;
                   max-width:92%; }
  #permbar .ptext { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #permbar button { border:0; border-radius:6px; padding:3px 11px; cursor:pointer;
                    font-size:12px; font-weight:600; flex:0 0 auto; }
  #permAllow { background:#3f9d57; color:#fff; } #permAllow:hover { background:#46b061; }
  #permDeny { background:#553333; color:#f0b8b8; } #permDeny:hover { background:#693d3d; }
  #working.wait .dots { display:none; }
  #working.wait .pill { border-color:#34507a; color:#9ec1ff; }
  code, pre { font:12.5px ui-monospace,Menlo,monospace; }
  #status { position:fixed; top:8px; right:10px; font-size:11px; opacity:.6;
            background:#0008; padding:3px 8px; border-radius:6px; }
  #stats { position:fixed; top:7px; left:10px; z-index:6; display:none; gap:11px;
           font:11px ui-monospace,Menlo,monospace; color:#aeb8c6;
           background:#11141bcc; border:1px solid #262c36; border-radius:7px;
           padding:4px 10px; backdrop-filter:blur(5px); align-items:center; }
  #stats.on { display:flex; }
  #stats .k { color:#6f7b8c; }
  #stats .ctxbar { width:54px; height:6px; border-radius:3px; background:#222a35;
                   overflow:hidden; display:inline-block; vertical-align:middle; }
  #stats .ctxbar > i { display:block; height:100%; background:#4f8fd0; width:0; }
  #stats .ctxbar.hi > i { background:#d0a24f; } #stats .ctxbar.crit > i { background:#d05858; }
  .role .ts { float:right; font-weight:400; letter-spacing:0; text-transform:none;
              opacity:.6; font-size:10.5px; }
  .empty { opacity:.5; text-align:center; margin-top:30vh; }
  #composer { display:none; position:fixed; left:0; right:0; bottom:0; gap:8px;
              padding:9px 11px; background:#13151bee; border-top:1px solid #262a31;
              backdrop-filter:blur(6px); }
  #composer .inner { max-width:820px; margin:0 auto; width:100%; display:flex; gap:8px; position:relative; }
  #ac { display:none; position:absolute; bottom:100%; left:0; margin-bottom:6px;
        max-height:244px; overflow-y:auto; min-width:280px; max-width:96%;
        background:#0e131c; border:1px solid #33405a; border-radius:9px;
        box-shadow:0 8px 24px #000a; padding:4px; z-index:10; }
  #ac.on { display:block; }
  .ac-item { padding:4px 9px; border-radius:6px; font:12.5px ui-monospace,Menlo,monospace;
             color:#cdd6e0; cursor:pointer; white-space:nowrap; overflow:hidden;
             text-overflow:ellipsis; }
  .ac-item.sel { background:#26466e; color:#fff; }
  #ci { flex:1 1 auto; resize:none; min-height:40px; max-height:160px; color:#e6e6e6;
        background:#0e131c; border:1px solid #2b3340; border-radius:9px;
        padding:9px 11px; font:14px/1.4 inherit; outline:none; }
  #ci:focus { border-color:#3b6ea5; }
  #send { flex:0 0 auto; align-self:stretch; background:#2b6cb0; color:#fff; border:0;
          border-radius:9px; padding:0 17px; cursor:pointer; font-weight:600; font-size:14px; }
  #send:hover { background:#3279c2; } #send:disabled { opacity:.45; cursor:default; }
  #working { display:none; position:fixed; left:0; right:0; bottom:66px;
             justify-content:center; pointer-events:none; z-index:5; }
  #working.on { display:flex; }
  #working .pill { background:#1b2331; border:1px solid #2f3b50; border-radius:14px;
                   padding:5px 13px; font-size:12px; color:#cdd6e0; display:flex;
                   gap:8px; align-items:center; box-shadow:0 4px 14px #0006; }
  #working .dots { display:inline-flex; gap:4px; }
  #working .dots i { width:5px; height:5px; border-radius:50%; background:#79b8ff;
                     animation:wdot 1s infinite ease-in-out; }
  #working .dots i:nth-child(2){ animation-delay:.16s; }
  #working .dots i:nth-child(3){ animation-delay:.32s; }
  @keyframes wdot { 0%,80%,100%{ transform:translateY(0); opacity:.35; }
                    40%{ transform:translateY(-5px); opacity:1; } }
</style></head>
<body>
<div id="status">…</div>
<div id="stats"></div>
<div id="log"><div class="empty" id="empty">waiting for transcript…</div></div>
<div id="working"><div class="pill"><span class="dots"><i></i><i></i><i></i></span><span class="lbl">working…</span></div></div>
<div id="permbar"><div class="pill">🔐 <span class="ptext" id="permtext"></span>
  <button id="permAllow" title="answers the terminal's permission prompt with option 1">Allow once</button>
  <button id="permDeny" title="answers the terminal's permission prompt with Esc">Deny</button>
</div></div>
<div id="composer"><div class="inner">
  <div id="ac"></div>
  <textarea id="ci" rows="1" placeholder="Message this session…  (Enter to send, Shift+Enter for newline, @ for files)"></textarea>
  <button id="send">Send</button>
</div></div>
<script>
(function () {
  var params = new URLSearchParams(location.search);
  var id = params.get("id") || "";
  // The page is served under the dashboard's BASE_PATH (e.g. /dash/chat-panel);
  // strip the trailing /chat-panel to rebuild the API base for the same prefix.
  var base = location.pathname.replace(/\/chat-panel.*$/, "");
  var log = document.getElementById("log");
  var empty = document.getElementById("empty");
  var status = document.getElementById("status");
  var seen = new Set();   // uuid dedup across reconnect replays
  function esc(s){ return s.replace(/[&<>]/g, function(c){
    return c==="&"?"&amp;":c==="<"?"&lt;":"&gt;"; }); }
  function fmtTime(iso){
    try { var d = new Date(iso); if (isNaN(d.getTime())) return "";
      var hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      return d.toDateString() === new Date().toDateString() ? hm
        : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + hm;
    } catch (e) { return ""; }
  }
  function fmtTok(n){
    n = n || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e3) return Math.round(n / 1e3) + "k";
    return String(n);
  }
  function fmtDur(ms){
    if (ms == null) return "";
    var s = ms / 1000;
    return s < 60 ? s.toFixed(s < 10 ? 1 : 0) + "s"
                  : Math.floor(s / 60) + "m" + String(Math.round(s % 60)).padStart(2, "0") + "s";
  }
  // Minimal line diff for an Edit/Write: trim shared prefix/suffix, mark the rest.
  function renderDiff(oldv, newv){
    var o = oldv ? oldv.split("\n") : [], n = newv ? newv.split("\n") : [];
    var pre = 0; while (pre < o.length && pre < n.length && o[pre] === n[pre]) pre++;
    var suf = 0; while (suf < o.length - pre && suf < n.length - pre &&
                        o[o.length - 1 - suf] === n[n.length - 1 - suf]) suf++;
    var rows = [], i;
    for (i = 0; i < pre; i++) rows.push([" ", o[i]]);
    for (i = pre; i < o.length - suf; i++) rows.push(["-", o[i]]);
    for (i = pre; i < n.length - suf; i++) rows.push(["+", n[i]]);
    for (i = o.length - suf; i < o.length; i++) rows.push([" ", o[i]]);
    return '<pre class="diff">' + rows.map(function(r){
      var c = r[0] === "+" ? "add" : r[0] === "-" ? "del" : "ctx";
      return '<span class="dl ' + c + '">' + esc(r[0] + " " + (r[1] == null ? "" : r[1])) + "</span>";
    }).join("") + "</pre>";
  }
  // Minimal, self-contained Markdown -> HTML. Safety model: pull fenced code out
  // first, ESCAPE everything, then layer formatting on the escaped text — so no
  // transcript content can inject markup. Supports the subset claude emits:
  // fenced + inline code, headings, **bold**, *italic*, links, lists, quotes, hr.
  function mdToHtml(src){
    src = src.replace(/\r\n?/g, "\n");
    var blocks = [];
    // 1. fenced code blocks -> placeholders (content escaped now, restored later)
    src = src.replace(/```[^\n]*\n([\s\S]*?)```/g, function(_, code){
      blocks.push("<pre><code>" + esc(code.replace(/\n$/, "")) + "</code></pre>");
      return "B" + (blocks.length - 1) + "";
    });
    // 2. escape the rest
    src = esc(src);
    // 3. inline: code, links, bold, italic (operate on already-escaped text)
    src = src.replace(/`([^`\n]+)`/g, function(_, c){ return "<code>" + c + "</code>"; });
    src = src.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function(_, t, u){
      // only allow safe schemes; otherwise render the link text as plain
      return /^(https?:|mailto:|\/|#)/i.test(u)
        ? '<a href="' + u + '" target="_blank" rel="noopener">' + t + "</a>" : t;
    });
    src = src.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // italic with '*' only — '_' would mangle snake_case identifiers in prose
    src = src.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    // 4. block level, line by line
    var lines = src.split("\n"), out = [], para = [], i = 0;
    function flush(){ if (para.length){ out.push("<p>" + para.join("<br>") + "</p>"); para = []; } }
    while (i < lines.length){
      var ln = lines[i], m;
      if ((m = ln.match(/^B(\d+)$/))){ flush(); out.push(blocks[+m[1]]); i++; continue; }
      if ((m = ln.match(/^(#{1,6})\s+(.*)$/))){ flush();
        out.push("<h" + m[1].length + ">" + m[2] + "</h" + m[1].length + ">"); i++; continue; }
      // GFM table: a header row containing '|' immediately followed by a
      // |---|---| delimiter row. '|' survives esc() (only &<> are escaped).
      if (ln.indexOf("|") >= 0 && i + 1 < lines.length &&
          /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(lines[i + 1])){
        flush();
        var cut = function(row){
          return row.trim().replace(/^\||\|$/g, "").split("|").map(function(c){ return c.trim(); });
        };
        var head = cut(ln); i += 2;
        var rows = [];
        while (i < lines.length && lines[i].indexOf("|") >= 0 && lines[i].trim() !== ""){
          rows.push(cut(lines[i])); i++;
        }
        out.push("<table><thead><tr>" +
          head.map(function(c){ return "<th>" + c + "</th>"; }).join("") +
          "</tr></thead><tbody>" +
          rows.map(function(r){ return "<tr>" +
            r.map(function(c){ return "<td>" + c + "</td>"; }).join("") + "</tr>"; }).join("") +
          "</tbody></table>");
        continue;
      }
      if (/^\s*(---+|\*\*\*+)\s*$/.test(ln)){ flush(); out.push("<hr>"); i++; continue; }
      if (/^\s*([-*+]|\d+\.)\s+/.test(ln)){ flush();
        var ordered = /^\s*\d+\./.test(ln), items = [];
        while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])){
          items.push("<li>" + lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, "") + "</li>"); i++; }
        out.push((ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>")); continue; }
      // '>' was already escaped to '&gt;' in step 2, so match that form
      if (/^\s*&gt;\s?/.test(ln)){ flush();
        var q = [];
        while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])){ q.push(lines[i].replace(/^\s*&gt;\s?/, "")); i++; }
        out.push("<blockquote>" + q.join("<br>") + "</blockquote>"); continue; }
      if (ln.trim() === ""){ flush(); i++; continue; }
      para.push(ln); i++;
    }
    flush();
    // safety net: restore any code placeholder that did not sit alone on a line
    return out.join("\n").replace(/B(\d+)/g, function(_, n){ return blocks[+n]; });
  }
  // Background-task notifications arrive embedded in turn text as a
  // <task-notification> block. Render them as a compact card (status, id,
  // summary) with a link to the output file, instead of raw XML.
  function tnField(body, tag){
    var m = body.match(new RegExp("<" + tag + ">([\\s\\S]*?)<\\/" + tag + ">"));
    return m ? m[1].trim() : "";
  }
  function taskCard(body){
    var id = tnField(body, "task-id"), status = tnField(body, "status"),
        summary = tnField(body, "summary"), outf = tnField(body, "output-file");
    var ok = /complet|success|done|finish/i.test(status);
    var bad = /fail|error|kill|abort|cancel/i.test(status);
    var cls = ok ? " ok" : bad ? " bad" : "";
    var h = '<div class="task' + cls + '"><div class="task-h">' +
            '<span class="task-badge">' + esc(status || "task") + '</span>';
    if (id) h += '<span class="task-id">' + esc(id) + '</span>';
    h += '</div>';
    if (summary) h += '<div class="task-sum">' + esc(summary) + '</div>';
    if (outf) h += '<div class="task-file">\u{1F4C4} <a href="' + base +
      '/api/taskfile?path=' + encodeURIComponent(outf) +
      '" target="_blank" rel="noopener">' + esc(outf) + '</a></div>';
    return h + '</div>';
  }
  // Render a turn's text: split out task-notification blocks (card them) and
  // Markdown-render the prose around them.
  function renderBody(txt){
    var re = /<task-notification>([\s\S]*?)<\/task-notification>/g;
    var out = "", last = 0, m;
    while ((m = re.exec(txt))){
      if (m.index > last) out += mdToHtml(txt.slice(last, m.index));
      out += taskCard(m[1]);
      last = m.index + m[0].length;
    }
    out += mdToHtml(txt.slice(last));
    return out;
  }
  // Scroll model: stay glued to the bottom while the user is there, otherwise
  // keep their place — and survive the panel being hidden/shown (the chat<->
  // terminal toggle and dashboard tab switches both display:none the tile,
  // which otherwise snaps the view to the top on return).
  var stick = true, savedY = 0;
  function visible(){ return document.documentElement.clientHeight > 0; }
  function nearBottom(){ return window.innerHeight + window.scrollY
      >= document.body.scrollHeight - 60; }
  function toBottom(){ window.scrollTo(0, document.body.scrollHeight); }
  window.addEventListener("scroll", function(){
    if (!visible()) return;       // ignore the spurious scroll=0 fired while hidden
    stick = nearBottom();
    if (!stick) savedY = window.scrollY;
  }, { passive: true });
  // Re-assert position whenever layout changes — content reflow, or (the key
  // case) the panel regaining size after being shown again. Appended turns sit
  // below the viewport, so the saved offset still points at the same content.
  function restoreScroll(){ if (!visible()) return;
    if (stick) toBottom(); else window.scrollTo(0, savedY); }
  try { new ResizeObserver(restoreScroll).observe(document.documentElement); } catch (e) {}
  // In-flight echo: show a sent message immediately as a "sending…" bubble until
  // claude logs it to the transcript and the real turn streams back.
  var pending = [];   // [{el, text}]
  function addPending(text){
    var tx = (text || "").trim();
    // Dedup: a composer send already drew its own placeholder; the server's
    // 'pending' echo of the same prompt (from the UserPromptSubmit hook) must
    // not add a second one. One placeholder per text; reconcile clears it.
    for (var j = 0; j < pending.length; j++) if (pending[j].text && pending[j].text === tx) return;
    if (empty){ empty.remove(); empty = null; }
    var el = document.createElement("div");
    el.className = "turn user pending";
    el.innerHTML = '<div class="role">user<span class="ts">sending…</span></div>' +
                   '<div class="md">' + renderBody(text) + '</div>';
    log.appendChild(el);
    var rec = { el: el, text: (text || "").trim() };
    pending.push(rec);
    if (stick) toBottom();
    // Safety: if it never comes back (a slash-command/interrupt that logs no user
    // turn), drop the placeholder after a while so it isn't stuck "sending…".
    setTimeout(function(){
      var i = pending.indexOf(rec);
      if (i >= 0){ pending.splice(i, 1); if (rec.el.parentNode) rec.el.remove(); }
    }, 30000);
  }
  function reconcilePending(turn){
    if (turn.role !== "user" || !pending.length) return;
    var tx = (turn.text || "").trim();
    for (var i = 0; i < pending.length; i++){
      if (pending[i].text && (tx === pending[i].text || tx.indexOf(pending[i].text) >= 0)){
        if (pending[i].el.parentNode) pending[i].el.remove();
        pending.splice(i, 1);
        return;
      }
    }
  }
  function add(t){
    if (t.uuid && seen.has(t.uuid)) return;
    if (t.uuid) seen.add(t.uuid);
    reconcilePending(t);   // the real turn replaces its in-flight placeholder
    if (empty){ empty.remove(); empty = null; }
    var el = document.createElement("div");
    el.className = "turn " + (t.role === "user" ? "user" : "assistant");
    var html = '<div class="role">' + esc(t.role) +
               (t.ts ? '<span class="ts">' + fmtTime(t.ts) + '</span>' : '') + '</div>';
    if (t.text) html += '<div class="md">' + renderBody(t.text) + '</div>';
    if (t.tools && t.tools.length){
      html += '<div class="tools">' + t.tools.map(function(tl){
        var n = (tl && tl.name) || tl || "tool";       // tolerate old string form
        var d = tl && tl.detail;
        if (tl && tl.todos && tl.todos.length){        // TodoWrite → checklist
          return '<div class="todos"><div class="todos-h"><b>' + esc(n) + '</b></div>' +
            tl.todos.map(function(td){
              var st = td.status || "pending";
              return '<div class="todo ' + esc(st) + '"><span class="tbox"></span>' +
                     '<span>' + esc(td.content || "") + '</span></div>';
            }).join("") + '</div>';
        }
        if (tl && tl.diff && tl.diff.length){           // Edit/Write → click for diff
          var body = tl.diff.map(function(x){ return renderDiff(x.old || "", x.new || ""); }).join("");
          return '<div class="tool diffable"><div class="toolhead"><b>' + esc(n) + '</b>' +
            (d ? '<span class="tdetail">' + esc(d) + '</span>' : '') +
            '<span class="caret">▸</span></div><div class="diffbody">' + body + '</div></div>';
        }
        if (tl && tl.questions && tl.questions.length){ // AskUserQuestion → prompt + options
          return tl.questions.map(function(q){
            var opts = (q.options || []).map(function(o, oi){
              // While the question is live, clicking an option types its digit
              // into the terminal's selector (only 1-9 are addressable).
              var key = oi < 9 ? String(oi + 1) : "";
              return '<div class="qopt"' + (key ? ' data-key="' + key + '"' : '') + '>' +
                '<div class="qlabel">' + esc(o.label) +
                (key ? '<span class="qkey">' + key + '</span>' : '') + '</div>' +
                (o.description ? '<div class="qdesc">' + esc(o.description) + '</div>' : '') + '</div>';
            }).join("");
            return '<div class="qcard' + (q.multi ? ' multi' : '') + '">' +
              (q.header ? '<span class="qhdr">' + esc(q.header) + '</span>' : '') +
              '<div class="qq">' + esc(q.question) +
              (q.multi ? ' <span style="opacity:.55;font-weight:400">(multiple — click to toggle, then submit)</span>' : '') +
              '</div><div class="qopts">' + opts + '</div>' +
              '<div class="qhint">👆 ' + (q.multi ? 'click options to toggle, then Submit'
                                                  : 'click an option to answer') + '</div>' +
              (q.multi ? '<button class="qsubmit">Submit ⏎</button>' : '') + '</div>';
          }).join("");
        }
        return '<span class="tool"><b>' + esc(n) + '</b>' +
               (d ? '<span class="tdetail">' + esc(d) + '</span>' : '') + '</span>';
      }).join("") + '</div>';
    }
    if (t.answers && t.answers.length){               // AskUserQuestion → chosen answer
      html += '<div class="answers">' + t.answers.map(function(a){
        return '<div class="ans"><span class="anscheck">✓</span>' +
          (a.q ? '<span class="ansq">' + esc(a.q) + '</span><span class="ansarrow">→</span>' : '') +
          '<span class="ansa">' + esc(a.a) + '</span></div>';
      }).join("") + '</div>';
    }
    el.innerHTML = html;
    log.appendChild(el);
    // A question that streams in while we're already in the waiting state (a
    // back-to-back follow-up question) must become live immediately — the
    // server only re-emits 'state' on change.
    if (lastState && lastState.waiting === "question" && el.querySelector(".qcard"))
      markLiveQuestion(true);
    if (stick) toBottom();
  }
  // Respect the dashboard's custom line-height. The panel is same-origin with
  // the dashboard, so the setting (an xterm-style multiplier) is right there in
  // localStorage; the 'storage' event lets a change in Settings apply live.
  function applyLineHeight(){
    var lh = 0;
    try { var v = localStorage.getItem("claude-sessions-line-height");
      if (v) { var n = parseFloat(v); if (n > 0) lh = n; } } catch (e) {}
    document.body.style.lineHeight = lh ? String(lh) : "";   // "" → CSS default
  }
  applyLineHeight();
  window.addEventListener("storage", function(e){
    if (!e.key || e.key === "claude-sessions-line-height") applyLineHeight();
  });
  if (!id){
    // No tile selected (someone opened /chat-panel directly): show a picker of
    // the claude tiles so the bare URL is still useful.
    status.textContent = "pick a session";
    if (empty) empty.textContent = "loading sessions…";
    fetch(base + "/api/sessions").then(function(r){ return r.json(); }).then(function(d){
      var claude = (d.sessions || []).filter(function(s){
        return s.kind === "host" || s.kind === "container"; });
      log.innerHTML = "";
      if (!claude.length){ log.innerHTML = '<div class="empty">no claude sessions</div>'; return; }
      var h = '<div class="role" style="margin:6px 2px 12px">chat sessions — pick one</div>';
      claude.forEach(function(s){
        h += '<a class="turn assistant" style="display:block;text-decoration:none;color:inherit" href="?id=' +
          encodeURIComponent(s.id) + '"><div class="role">' + esc(s.kind) + '</div>' +
          esc(s.name || s.id) + (s.cwd ? '<div style="opacity:.5;font-size:12px">' + esc(s.cwd) + '</div>' : '') +
          '</a>';
      });
      log.innerHTML = h;
    }).catch(function(){ log.innerHTML = '<div class="empty">failed to load sessions</div>'; });
    return;
  }
  // Composer: send a typed message into the session's PTY via /api/chat-send.
  var CSRF = (document.querySelector("meta[name=csrf-token]") || {}).content || "";
  var composer = document.getElementById("composer");
  var ci = document.getElementById("ci");
  var sendBtn = document.getElementById("send");
  var working = document.getElementById("working");
  var workingLbl = working.querySelector(".lbl");
  var statsEl = document.getElementById("stats");
  composer.style.display = "flex";
  var busyStart = 0, busyTimer = null, wordTimer = null, busyWord = "Working",
      serverBusy = false, optimisticTimer = null;
  var WORDS = ["Schlepping", "Generating", "Pondering", "Brewing", "Conjuring",
    "Noodling", "Percolating", "Simmering", "Cooking", "Crunching", "Musing",
    "Churning", "Vibing", "Marinating", "Ruminating", "Synthesizing", "Forging",
    "Hatching", "Wrangling", "Computing", "Concocting", "Cerebrating", "Spinning",
    "Mulling", "Incubating", "Manifesting", "Puttering", "Smooshing", "Honking"];
  function pickWord(){ return WORDS[Math.floor(Math.random() * WORDS.length)]; }
  function setBusy(b){
    working.classList.toggle("on", !!b);
    if (b){
      working.classList.remove("wait");
      if (!busyTimer){
        busyStart = Date.now(); busyWord = pickWord();
        var tick = function(){ workingLbl.textContent = busyWord + "… " + fmtDur(Date.now() - busyStart); };
        tick(); busyTimer = setInterval(tick, 200);
        wordTimer = setInterval(function(){ busyWord = pickWord(); }, 4000);  // reshuffle
      }
    } else if (busyTimer){
      clearInterval(busyTimer); busyTimer = null;
      clearInterval(wordTimer); wordTimer = null;
    }
  }
  // Full server state: busy spinner / waiting-on-you states / pending permission
  // prompt. Waiting states show a calm pill instead of the spinner — claude
  // isn't computing anything, the user is the blocker.
  var lastState = null;
  var permbar = document.getElementById("permbar");
  var permtext = document.getElementById("permtext");
  function setState(st){
    lastState = st;
    if (st.perm)
      permtext.textContent = st.perm.message ||
        ("Claude needs your permission to use " + (st.perm.tool || "a tool"));
    permbar.classList.toggle("on", !!st.perm);
    if (st.waiting === "question"){
      setBusy(false);
      working.classList.add("on"); working.classList.add("wait");
      workingLbl.textContent = "❓ waiting for your answer — click an option";
    } else if (st.waiting === "permission"){
      setBusy(false);     // the permbar carries the message + buttons
    } else {
      setBusy(!!st.busy);
    }
    markLiveQuestion(st.waiting === "question");
  }
  // The live question = the newest unanswered AskUserQuestion. Its options
  // become clickable and answer the terminal's own selector via /api/chat-key.
  function markLiveQuestion(on){
    var cards = log.querySelectorAll(".qcard");
    for (var i = 0; i < cards.length; i++) cards[i].classList.remove("live");
    if (!on || !cards.length) return;
    // all question cards of the last turn that has one (multi-question calls)
    var turnEl = cards[cards.length - 1].closest(".turn") || cards[cards.length - 1];
    var live = turnEl.querySelectorAll(".qcard");
    for (i = 0; i < live.length; i++) live[i].classList.add("live");
  }
  function sendKey(k){
    fetch(base + "/api/chat-key?id=" + encodeURIComponent(id) + "&key=" + encodeURIComponent(k), {
      method: "POST", headers: { "X-CSRF-Token": CSRF }
    }).then(function(r){ return r.json().catch(function(){ return { ok: r.ok }; }); })
      .then(function(d){ if (!(d && d.ok)) status.textContent = "key failed: " + ((d && d.error) || "error"); })
      .catch(function(){ status.textContent = "key failed"; });
  }
  document.getElementById("permAllow").addEventListener("click", function(){
    sendKey("1"); permbar.classList.remove("on");   // optimistic; server confirms
  });
  document.getElementById("permDeny").addEventListener("click", function(){
    sendKey("esc"); permbar.classList.remove("on");
  });
  // claude's input-box ghost suggestion. The terminal tile scrapes the dim
  // placeholder/suggested-prompt text and the dashboard relays it here via
  // localStorage (same origin → a 'storage' event reaches this iframe, same as
  // line-height). We show it as the composer placeholder while idle + empty, and
  // Tab accepts it into the textarea (mirrors claude's own Tab-to-accept). Gated
  // on !serverBusy so a transient dim hint scraped mid-run never shows.
  var SUG_KEY = "claude-sessions-suggestion:" + id;
  var DEFAULT_PH = ci.getAttribute("placeholder") || "";
  var suggestion = "";
  function refreshPlaceholder(){
    ci.placeholder = (suggestion && !serverBusy)
      ? ("💡 " + suggestion + "    ·  Tab to use") : DEFAULT_PH;
  }
  function applySuggestion(){
    var v = ""; try { v = localStorage.getItem(SUG_KEY) || ""; } catch (e) {}
    suggestion = v; refreshPlaceholder();
  }
  applySuggestion();
  window.addEventListener("storage", function(e){
    if (!e.key || e.key === SUG_KEY) applySuggestion();
  });
  // Token / context / timing readout (top-left). The context window comes from
  // the server (model-aware: opus-4-8 / fable-5 run 1M here); the sticky-upgrade
  // is just a safety net if a prompt ever exceeds the reported window.
  var ctxWin = 200000;
  function renderStats(s){
    if (!s) return;
    if (s.ctxWindow) ctxWin = s.ctxWindow;
    if (s.promptTokens > ctxWin) ctxWin = 1000000;
    var pct = Math.min(100, Math.round(s.promptTokens / ctxWin * 100));
    var cls = pct >= 90 ? "crit" : pct >= 70 ? "hi" : "";
    var h = '<span class="ctxbar ' + cls + '"><i style="width:' + pct + '%"></i></span>' +
      '<span><span class="k">ctx</span> ' + fmtTok(s.promptTokens) + "/" + fmtTok(ctxWin) +
      " · " + pct + "%</span>";
    if (s.durationMs != null) h += '<span><span class="k">⏱</span> ' + fmtDur(s.durationMs) + "</span>";
    h += '<span><span class="k">out</span> ' + fmtTok(s.outputTokens) + "</span>";
    // Rolling 15-min generation speed + avg response latency (decode tok/s over
    // actual generation time; latency = avg user/tool→response wall-time).
    if (s.tokps15 != null)
      h += '<span title="avg generation speed over the last 15 min (' +
           (s.resp15 || 0) + ' responses)"><span class="k">tok/s</span> ' +
           s.tokps15 + "</span>";
    if (s.latencyMs15 != null)
      h += '<span title="avg response latency over the last 15 min"><span class="k">lat</span> ' +
           fmtDur(s.latencyMs15) + "</span>";
    if (s.sessionOut) h += '<span title="output tokens this session"><span class="k">Σ</span> ' +
                           fmtTok(s.sessionOut) + "</span>";
    statsEl.innerHTML = h;
    statsEl.classList.add("on");
  }
  function autogrow(){ ci.style.height = "auto"; ci.style.height = Math.min(160, ci.scrollHeight) + "px"; }
  // @-mention autocomplete: complete file/dir paths under the session cwd
  // (claude's @ file picker, server-backed by /api/complete).
  var ac = document.getElementById("ac");
  var acItems = [], acIdx = -1, acTok = null, acDir = "", acTimer = null;
  function atToken(){
    var pos = ci.selectionStart;
    var m = ci.value.slice(0, pos).match(/(?:^|\s)@([^\s@]*)$/);
    return m ? { start: pos - m[1].length, end: pos, query: m[1] } : null;  // start = just past '@'
  }
  function hideAC(){ ac.classList.remove("on"); acItems = []; acIdx = -1; acTok = null; }
  function paintAC(){
    var nodes = ac.querySelectorAll(".ac-item");
    for (var i = 0; i < nodes.length; i++) nodes[i].classList.toggle("sel", i === acIdx);
    if (nodes[acIdx]) nodes[acIdx].scrollIntoView({ block: "nearest" });
  }
  function showAC(items){
    acItems = items; acIdx = items.length ? 0 : -1;
    if (!items.length){ hideAC(); return; }
    ac.innerHTML = items.map(function(it, i){
      return '<div class="ac-item' + (i === 0 ? " sel" : "") + '" data-i="' + i + '">' +
        (it.dir ? "📁 " : "📄 ") + esc(it.name) + (it.dir ? "/" : "") + "</div>";
    }).join("");
    ac.classList.add("on");
  }
  function updateAC(){
    var tok = atToken();
    if (!tok){ hideAC(); return; }
    acTok = tok;
    clearTimeout(acTimer);
    acTimer = setTimeout(function(){
      fetch(base + "/api/complete?id=" + encodeURIComponent(id) + "&q=" + encodeURIComponent(tok.query))
        .then(function(r){ return r.json(); })
        .then(function(d){ acDir = d.dir || ""; if (atToken()) showAC(d.items || []); })
        .catch(function(){ hideAC(); });
    }, 80);
  }
  function acceptAC(i){
    if (i == null) i = acIdx;
    if (i < 0 || !acItems[i] || !acTok) return;
    var it = acItems[i], insert = acDir + it.name + (it.dir ? "/" : ""), v = ci.value;
    ci.value = v.slice(0, acTok.start) + insert + v.slice(acTok.end);
    var p = acTok.start + insert.length;
    ci.setSelectionRange(p, p); autogrow();
    if (it.dir) updateAC(); else hideAC();   // a dir → keep drilling into it
    ci.focus();
  }
  ac.addEventListener("mousedown", function(e){
    var el = e.target.closest(".ac-item"); if (!el) return;
    e.preventDefault();                      // keep focus in the textarea
    acceptAC(+el.dataset.i);
  });
  ci.addEventListener("input", function(){ autogrow(); updateAC(); });
  ci.addEventListener("blur", function(){ setTimeout(hideAC, 150); });
  function send(){
    var v = ci.value;
    if (!v.trim() || sendBtn.disabled) return;
    sendBtn.disabled = true;
    fetch(base + "/api/chat-send?id=" + encodeURIComponent(id), {
      method: "POST",
      headers: { "X-CSRF-Token": CSRF, "Content-Type": "text/plain; charset=utf-8" },
      body: v
    }).then(function(r){ return r.json().catch(function(){ return { ok: r.ok }; }); })
      .then(function(d){
        if (d && d.ok){ addPending(v); ci.value = ""; autogrow(); status.textContent = "sent";
          setBusy(true);   // optimistic — claude starts before the file confirms
          clearTimeout(optimisticTimer);
          // If the server doesn't confirm "busy" within 6s, the exchange was
          // instant or produced no turn — clear it so it can't get stuck.
          optimisticTimer = setTimeout(function(){ if (!serverBusy) setBusy(false); }, 6000);
        } else { status.textContent = "send failed: " + ((d && d.error) || "error"); }
      }).catch(function(){ status.textContent = "send failed"; })
      .then(function(){ sendBtn.disabled = false; ci.focus(); });
  }
  sendBtn.addEventListener("click", send);
  ci.addEventListener("keydown", function(e){
    if (ac.classList.contains("on") && acItems.length){
      if (e.key === "ArrowDown"){ e.preventDefault(); acIdx = (acIdx + 1) % acItems.length; paintAC(); return; }
      if (e.key === "ArrowUp"){ e.preventDefault(); acIdx = (acIdx - 1 + acItems.length) % acItems.length; paintAC(); return; }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)){ e.preventDefault(); acceptAC(); return; }
      if (e.key === "Escape"){ e.preventDefault(); hideAC(); return; }
    }
    // Tab accepts claude's ghost suggestion when the box is empty and the @-picker
    // isn't claiming Tab (mirrors claude's own Tab-to-accept).
    if (e.key === "Tab" && !e.shiftKey && !ci.value && suggestion && !serverBusy){
      e.preventDefault(); ci.value = suggestion; autogrow(); refreshPlaceholder(); return;
    }
    if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); send(); }
  });
  // Clicks in the log: answer a live question's option / submit a multi-select,
  // or expand/collapse an Edit/Write diff.
  log.addEventListener("click", function(e){
    var opt = e.target.closest(".qcard.live .qopt[data-key]");
    if (opt && log.contains(opt)){ sendKey(opt.getAttribute("data-key")); return; }
    var sub = e.target.closest(".qcard.live .qsubmit");
    if (sub && log.contains(sub)){ sendKey("enter"); return; }
    var h = e.target.closest(".diffable > .toolhead");
    if (h && log.contains(h)) h.parentNode.classList.toggle("open");
  });
  var es = new EventSource(base + "/api/chat-stream?id=" + encodeURIComponent(id));
  es.addEventListener("turn", function(e){ add(JSON.parse(e.data)); });
  // First-turn echo: the server saw a UserPromptSubmit hook and forwarded the
  // prompt text before claude logged it. Show it instantly as a placeholder;
  // the real turn arrives via 'turn' shortly after and reconciles it.
  es.addEventListener("pending", function(e){ try {
    var d = JSON.parse(e.data); if (d && d.text){ addPending(d.text); if (stick) toBottom(); }
  } catch (x) {} });
  es.addEventListener("state", function(e){ try {
    var st = JSON.parse(e.data);
    serverBusy = !!st.busy || !!st.waiting;   // server is authoritative
    clearTimeout(optimisticTimer);
    setState(st);
    refreshPlaceholder();   // show/hide the ghost suggestion with idle state
  } catch (x) {} });
  es.addEventListener("stats", function(e){ try { renderStats(JSON.parse(e.data)); } catch (x) {} });
  es.addEventListener("waiting", function(){ status.textContent = "no transcript yet"; });
  es.addEventListener("reset", function(){ log.innerHTML = ""; seen.clear();
    empty = null; status.textContent = "resynced"; });
  es.addEventListener("ping", function(){ status.textContent = "live"; });
  es.onopen = function(){ status.textContent = "live"; };
  es.onerror = function(){ status.textContent = "reconnecting…"; };
})();
</script>
</body></html>"""


def restart_server(delay=0.3):
    """Re-exec this process in place to reload serve.py (the dashboard's
    Settings ▸ Restart server button). Runs in a daemon thread after a short
    delay so the HTTP 200 reaches the browser first. os.execv replaces the
    process image with the same argv; the listening socket is rebound (the
    Server sets allow_reuse_address). ttyd/dtach tile children were started
    with start_new_session=True, so they keep running across the re-exec and
    the page reconnects to them after reload. Under launchd KeepAlive this is
    equivalent to (and coexists with) a respawn."""
    def _go():
        time.sleep(delay)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except OSError:
            # execv should not return; if it somehow fails, exit so a process
            # supervisor (launchd KeepAlive) respawns us cleanly.
            os._exit(0)
    threading.Thread(target=_go, daemon=True).start()


def run_chat_export():
    """Run the chat-export script to (re)build the search index. Returns
    (ok, message). Blocking, but the server is threaded so polls keep flowing."""
    if not os.path.isfile(CHAT_EXPORT_SCRIPT):
        return False, "export script not found at %s" % CHAT_EXPORT_SCRIPT
    try:
        p = subprocess.run([sys.executable, CHAT_EXPORT_SCRIPT],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=600)
    except subprocess.TimeoutExpired:
        return False, "export timed out (>600s)"
    except Exception as e:  # noqa: BLE001 — surface any spawn failure to the client
        return False, "export failed to start: %s" % e
    if p.returncode != 0:
        tail = (p.stdout or b"").decode("utf-8", "replace")[-2000:]
        return False, "export exited %d:\n%s" % (p.returncode, tail)
    return True, "ok"


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype, extra_headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        if self.headers.get("Host") in ALLOWED_HOSTS:
            return True
        self._send(403, b"forbidden", "text/plain")
        return False

    def _strip_base(self):
        """Remove the public BASE_PATH prefix (e.g. /dash) from self.path so all
        routing below stays prefix-free. nginx forwards the prefix to us intact;
        a direct (localhost, no-proxy) request has BASE_PATH='' and is untouched."""
        if not BASE_PATH:
            return
        if self.path == BASE_PATH:
            self.path = "/"
        elif self.path.startswith(BASE_PATH + "/"):
            self.path = self.path[len(BASE_PATH):]

    def _serve_chat_history(self):
        """Serve a file from CHAT_HISTORY_DIR (the chat-export output) read-only.
        `/chat-history` and any directory path map to that dir's index.html.
        Path-traversal guarded: the resolved real path must stay inside the dir."""
        rel = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        rel = rel[len("/chat-history"):].lstrip("/")
        target = os.path.normpath(os.path.join(CHAT_HISTORY_DIR, rel))
        # Confinement: target must be CHAT_HISTORY_DIR or a descendant.
        base = os.path.realpath(CHAT_HISTORY_DIR)
        real = os.path.realpath(target)
        if real != base and not real.startswith(base + os.sep):
            self._send(403, b"forbidden", "text/plain")
            return
        if os.path.isdir(real):
            real = os.path.join(real, "index.html")
        if not os.path.isfile(real):
            self._send(404, b"not found - run Search to build the index", "text/plain")
            return
        try:
            with open(real, "rb") as f:
                body = f.read()
        except OSError:
            self._send(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, body, ctype)

    def _cors_origin(self):
        """Return the request's Origin header iff it's a 127.0.0.1:<port> URL
        (any port). Used by /api/dropfile so a session iframe at
        http://127.0.0.1:<ttyd-port> can POST uploads to the dashboard at
        http://127.0.0.1:<dashboard-port> — the two are different origins
        even on the same host, so the browser enforces CORS. We only echo
        the Origin when it's a local URL, never `*`, so even if a malicious
        public page somehow hits us its preflight rejects."""
        origin = self.headers.get("Origin", "")
        if origin.startswith("http://127.0.0.1:"):
            return origin
        return ""

    def do_OPTIONS(self):
        # CORS preflight for /api/dropfile. The actual POST is CSRF-guarded
        # (same X-CSRF-Token check as every other state-changing endpoint),
        # but the preflight itself doesn't carry the token — the browser
        # sends it BEFORE running the request to check whether the headers
        # are allowed at all. So preflight just advertises what we accept;
        # the real auth happens on the POST.
        self._strip_base()
        if not self.path.startswith("/api/dropfile"):
            self._send(404, b"not found", "text/plain")
            return
        if not self._host_ok():
            return
        origin = self._cors_origin()
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "X-CSRF-Token, Content-Type")
        self.send_header("Access-Control-Max-Age", "3600")
        self.end_headers()

    def _proxy_ttyd(self):
        """Reverse-proxy /t/<port>/<rest> to the session ttyd on 127.0.0.1:<port>.

        Why this exists: terminal tiles embed a ttyd web TTY. On localhost the
        dashboard embeds it directly at http://127.0.0.1:<port>/, but a remote
        browser (behind the nginx https vhost) can't reach that — so the
        dashboard JS instead points those tiles at this same-origin path and we
        relay through to ttyd here. One proxied origin means the nginx layer only
        has to forward the single dashboard port (with WebSocket upgrade), and
        https + basic auth then cover the terminals too.

        Mechanics: we forward the request line (path-stripped) and headers to
        ttyd verbatim — crucially NOT touching Sec-WebSocket-Key, so ttyd's 101
        Sec-WebSocket-Accept is valid for the real client — then become a dumb
        bidirectional byte pipe. That transparently carries both ttyd's plain
        HTTP responses (the term.html page, /token) and the opaque framed
        WebSocket stream on /ws after the upgrade.

        SSRF guard: the port must belong to a live registered session, so this
        can't be driven into a generic localhost port scanner. (It's also behind
        basic auth in the deployed setup.)
        """
        m = re.match(r"^/t/(\d+)(/.*)?$", self.path)
        if not m:
            self._send(404, b"not found", "text/plain")
            return
        port = int(m.group(1))
        rest = m.group(2) or "/"
        if port not in _live_ttyd_ports():
            self._send(404, b"no such session", "text/plain")
            return
        try:
            up = socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError:
            self._send(502, b"ttyd unreachable", "text/plain")
            return
        try:
            req = ["%s %s %s\r\n" % (self.command, rest, self.request_version)]
            for k, v in self.headers.items():
                if k.lower() == "host":
                    v = "127.0.0.1:%d" % port
                req.append("%s: %s\r\n" % (k, v))
            req.append("\r\n")
            up.sendall("".join(req).encode("latin-1"))
            # Forward a request body if one was announced (ttyd's GET/ws have
            # none, but stay correct if that ever changes).
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n > 0:
                up.sendall(self.rfile.read(n))
        except OSError:
            try:
                up.close()
            except OSError:
                pass
            return
        # This connection is now a tunnel; don't let the handler try to serve
        # another request on it afterwards.
        self.close_connection = True

        def pump_up_to_client():
            # read1 isn't needed on the recv side; recv already returns as soon as
            # any bytes are available, which keeps the stream interactive.
            try:
                while True:
                    data = up.recv(65536)
                    if not data:
                        break
                    self.wfile.write(data)
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                try:
                    self.connection.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t = threading.Thread(target=pump_up_to_client, daemon=True)
        t.start()
        # client -> ttyd: read via rfile.read1 so any bytes already buffered past
        # the request headers are forwarded, and so we stream rather than block
        # for a full buffer.
        try:
            while True:
                data = self.rfile.read1(65536)
                if not data:
                    break
                up.sendall(data)
        except OSError:
            pass
        finally:
            try:
                up.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        t.join(timeout=2)
        try:
            up.close()
        except OSError:
            pass

    def _chat_stream(self, sid):
        """Server-Sent Events: replay a claude tile's conversation transcript,
        then tail it live. Reads claude's own .jsonl ground truth, so it's
        immune to the xterm.js rendering bugs that afflict the live TTY. One
        daemon thread per client (ThreadingMixIn); returns on disconnect."""
        fp = _tile_jsonl(sid)
        # Open-ended response: no Content-Length, disable proxy buffering.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")  # nginx: don't buffer SSE
        self.end_headers()

        def emit(event, obj):
            try:
                self.wfile.write(("event: %s\ndata: %s\n\n"
                                  % (event, json.dumps(obj))).encode())
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False  # client gone — caller stops the loop

        # First-turn echo: a UserPromptSubmit hook delivers the prompt text
        # BEFORE claude writes it to the .jsonl (and even before the file exists
        # on a brand-new tile). Emit it as a 'pending' bubble the moment it
        # arrives; the real user turn streams from the transcript moments later
        # and the client reconciles the placeholder by text. Gated on
        # `stream_start` so only prompts submitted after THIS client connected
        # echo (the real turn is then guaranteed to stream to us live too), and
        # once per prompt via the `emitted_prompt` cell.
        stream_start = time.time()
        emitted_prompt = [0.0]

        def echo_prompt(hsx):
            pr = hsx.get("prompt") if hsx else None
            if not isinstance(pr, dict):
                return True
            pts = pr.get("ts") or 0
            if pts >= stream_start and pts > emitted_prompt[0]:
                emitted_prompt[0] = pts
                return emit("pending", {"text": pr.get("text") or ""})
            return True

        offset, buf = 0, b""
        last_beat = time.time()
        # Busy/idle tracking for the "working" animation. claude is busy from a
        # user message until the assistant's final end_turn; each assistant
        # record carries a stop_reason ('tool_use' mid-work, 'end_turn' when
        # done). We watch the last real message and emit a 'state' event on
        # change — but only after the initial replay catches up to EOF, so the
        # historical scrollback doesn't flap the indicator.
        last_role = last_stop = None
        caught_up = False
        cur_state = None
        # Pending interactive question: claude asked via AskUserQuestion and is
        # blocked on the user. Set by the tool_use record, cleared by the next
        # user record (the answer / an interrupt) — claude writes nothing else
        # while it waits.
        pending_q = False
        # Timing + token/context stats from the latest assistant message.
        work_start_ms = None   # ts of the user msg that began the current run
        run_active = False
        latest_stats = cur_stats = None
        session_out = 0        # cumulative output tokens this session (consumption)
        # Rolling 15-min throughput/latency window. Each entry is
        # (asst_ms, output_tokens, resp_ms) for one assistant response, where
        # resp_ms is the gap from the triggering user/tool_result record to the
        # response — i.e. API round-trip + generation, with no human think-time
        # (that falls before the user message). Pruned to 15 min of wall-clock so
        # the readout decays even while the session sits idle.
        RATE_WIN_MS = 15 * 60 * 1000
        rate_window = []       # list of [start_ms, output_tokens, end_ms]
        prev_ts_ms = None      # ts of the previous real (non-sidechain) record
        cur_resp_id = None     # message id of the response currently streaming
        while True:
            # Resolve lazily: a just-spawned tile may not have a transcript yet,
            # and a forked/restarted tile can swap to a new session file.
            if not fp or not os.path.isfile(fp):
                fp = _tile_jsonl(sid)
                if not fp or not os.path.isfile(fp):
                    if not emit("waiting", {"reason": "no-transcript"}):
                        return
                    # No file yet, but the first prompt can already be echoed
                    # from the hook so the panel isn't blank while claude spins
                    # up and writes its first record.
                    if not echo_prompt(_hook_snapshot(
                            (_registry_record(sid) or {}).get("session_id"))):
                        return
                    time.sleep(0.5)
                    continue
                offset, buf = 0, b""
            try:
                size = os.path.getsize(fp)
                if size < offset:            # truncated/rotated → resync
                    offset, buf = 0, b""
                    if not emit("reset", {}):
                        return
                if size > offset:
                    with open(fp, "rb") as fh:
                        fh.seek(offset)
                        chunk = fh.read(size - offset)
                    offset += len(chunk)
                    buf += chunk
                    # Keep the trailing partial line buffered until its newline
                    # arrives — claude writes a record then its '\n' separately.
                    *lines, buf = buf.split(b"\n")
                    for ln in lines:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            o = json.loads(ln)
                        except ValueError:
                            continue
                        turn = _jsonl_turn(o)
                        if turn and not emit("turn", turn):
                            return
                        # Track the last real message for busy/idle (skip
                        # snapshots, attachments, sidechain/sub-agent records).
                        rt = o.get("type")
                        if rt in ("user", "assistant") and not o.get("isSidechain"):
                            last_role = rt
                            cur_ms = _iso_ms(o.get("timestamp"))
                            if rt == "user":
                                # Any user record (an answer, an interrupt, a tool
                                # result) means claude is no longer blocked on a
                                # pending question.
                                pending_q = False
                                # Start of a working run = the user msg that ends
                                # the idle period (not the mid-run tool_results).
                                if not run_active:
                                    work_start_ms = _iso_ms(o.get("timestamp"))
                                    run_active = True
                            else:  # assistant
                                m = o.get("message") if isinstance(o.get("message"), dict) else {}
                                mc = m.get("content")
                                if isinstance(mc, list) and any(
                                        isinstance(b, dict) and b.get("type") == "tool_use"
                                        and b.get("name") == "AskUserQuestion" for b in mc):
                                    pending_q = True
                                last_stop = m.get("stop_reason")
                                if last_stop in _IDLE_STOPS:
                                    run_active = False
                                u = m.get("usage") if isinstance(m.get("usage"), dict) else None
                                if u:
                                    asst_ms = cur_ms
                                    prompt = (u.get("input_tokens") or 0) \
                                        + (u.get("cache_creation_input_tokens") or 0) \
                                        + (u.get("cache_read_input_tokens") or 0)
                                    out_tok = u.get("output_tokens") or 0
                                    session_out += out_tok
                                    # Feed the rolling 15-min window. Claude writes
                                    # one response as several records (one per
                                    # streamed content block), each repeating the
                                    # same usage — so dedupe by message id: the
                                    # first record opens a [start, out, end] entry,
                                    # later same-id records just push `end` forward.
                                    # start = prev_ts_ms (the user/tool_result that
                                    # triggered it; no human think-time, that falls
                                    # before the user message). Skip 0-output turns
                                    # so a pure tool-call doesn't drag tok/s down.
                                    rid = m.get("id")
                                    if rid and rid == cur_resp_id and rate_window:
                                        if asst_ms:
                                            rate_window[-1][2] = asst_ms
                                    elif (asst_ms and prev_ts_ms and out_tok > 0
                                            and asst_ms > prev_ts_ms):
                                        rate_window.append([prev_ts_ms, out_tok, asst_ms])
                                        cur_resp_id = rid
                                    latest_stats = {
                                        "promptTokens": prompt,
                                        "outputTokens": out_tok,
                                        "sessionOut": session_out,
                                        "model": m.get("model") or "",
                                        "ctxWindow": _context_window(m.get("model")),
                                        "durationMs": (asst_ms - work_start_ms)
                                        if (asst_ms and work_start_ms) else None,
                                    }
                            # Advance the previous-record marker for the next
                            # response's latency gap (both user and assistant).
                            if cur_ms:
                                prev_ts_ms = cur_ms
            except OSError:
                pass
            # Once we've drained to EOF, start reporting busy/idle. busy = a user
            # message is the latest (claude about to work / mid tool loop) or the
            # last assistant message hasn't reached an idle stop_reason yet.
            if offset >= 0 and not caught_up:
                try:
                    caught_up = os.path.getsize(fp) <= offset
                except OSError:
                    caught_up = True
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                mtime = time.time()
            busy = _is_busy(last_role, last_stop, time.time() - mtime)
            # Hook events (when the dashboard-notify hook is installed) are
            # ground truth and override the heuristic — but only while newer
            # than the last transcript write, so a fresh write re-arms it.
            reg = _registry_record(sid) or {}
            hs = _hook_snapshot(reg.get("session_id")) or {}
            if not echo_prompt(hs):   # first-turn echo (live, file already exists)
                return
            hs_ts = hs.get("ts")
            if hs.get("phase") and isinstance(hs_ts, (int, float)) and hs_ts >= mtime:
                busy = hs.get("phase") == "busy"
            # A dead session can't be working: its dtach socket dies with it.
            sock = reg.get("sock")
            if busy and sock and not os.path.exists(sock):
                busy = False
            # Waiting-on-the-user states trump "working": a pending permission
            # prompt (hook-reported; auto-clears once the transcript moves on or
            # PreToolUse fires) or a pending AskUserQuestion. Claude isn't
            # computing anything during either — the user is the blocker.
            perm = None
            hperm = hs.get("perm")
            pts = hperm.get("ts") if isinstance(hperm, dict) else None
            if isinstance(hperm, dict) and isinstance(pts, (int, float)) and pts >= mtime - 1:
                perm = {"tool": hperm.get("tool") or "",
                        "message": hperm.get("message") or ""}
            waiting = "permission" if perm else ("question" if pending_q else None)
            if waiting:
                busy = False
            state = {"busy": busy, "waiting": waiting, "perm": perm}
            if caught_up and state != cur_state:
                cur_state = state
                if not emit("state", state):
                    return
            if caught_up and latest_stats is not None:
                # Decay the 15-min window against wall-clock (not just on new
                # turns) so an idle session's tok/s + latency age out, then fold
                # the aggregates into the stats payload. tok/s is decode speed:
                # output tokens over actual generation time (sum of per-response
                # gaps), not amortized over idle wall-clock.
                cutoff = time.time() * 1000 - RATE_WIN_MS
                while rate_window and rate_window[0][2] < cutoff:
                    rate_window.pop(0)
                sum_out = sum(w[1] for w in rate_window)
                sum_ms = sum(w[2] - w[0] for w in rate_window)
                n = len(rate_window)
                merged = dict(latest_stats)
                merged["tokps15"] = round(sum_out / (sum_ms / 1000), 1) if sum_ms > 0 else None
                merged["latencyMs15"] = int(sum_ms / n) if n else None
                merged["resp15"] = n
                if merged != cur_stats:
                    cur_stats = merged
                    if not emit("stats", merged):
                        return
            now = time.time()
            if now - last_beat > 15:
                if not emit("ping", {}):      # heartbeat: detect dead clients
                    return
                last_beat = now
            time.sleep(0.5)

    def do_GET(self):
        self._strip_base()
        if not self._host_ok():
            return
        if self.path.startswith("/t/"):
            # /t/<port>/... → reverse-proxy to that session's ttyd. Lets a remote
            # browser (behind the nginx https vhost) reach terminal tiles, which
            # otherwise embed http://127.0.0.1:<port>/ — unreachable off-host.
            self._proxy_ttyd()
            return
        if self.path.startswith("/api/chat-stream"):
            # Live SSE tail of a tile's claude transcript — an xterm-free read of
            # the conversation. Long-lived; _chat_stream writes its own response.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._chat_stream((q.get("id") or [""])[0])
            return
        elif self.path.startswith("/chat-panel"):
            # Standalone live-chat viewer: open in a tab or embed as a webview
            # tile. Reads ?id=<sid> and streams via /api/chat-stream.
            self._send(200, CHAT_PANEL_HTML.replace("__CSRF_TOKEN__", CSRF_TOKEN).encode(),
                       "text/html; charset=utf-8")
            return
        elif self.path.startswith("/api/taskfile"):
            # Chat-panel notification cards link here to view a background task's
            # output/log file. _read_taskfile scopes what's readable.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            data = _read_taskfile((q.get("path") or [""])[0])
            if data is None:
                self._send(404, b"not found or not permitted", "text/plain; charset=utf-8")
            else:
                self._send(200, data, "text/plain; charset=utf-8")
            return
        elif self.path.startswith("/api/complete"):
            # Composer @-mention completion: files/dirs under the session's cwd.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            res = _complete_paths((q.get("id") or [""])[0], (q.get("q") or [""])[0])
            self._send(200, json.dumps(res).encode(), "application/json")
            return
        if self.path.startswith("/api/agent-sessions"):
            # Like /api/sessions but each host tile is enriched with its
            # conversation TITLE (first user message / summary from the .jsonl)
            # and session_id, so an agent can tell same-cwd tiles apart by what
            # they're actually doing — not just the cwd basename. Read-only and
            # Host-gated like /api/sessions; titles cost one small file read per
            # tile (not on the hot poll path — this is a separate endpoint).
            rows = []
            for s in read_sessions():
                row = dict(s)
                if s.get("kind") == "host":
                    reg = _registry_record(s["id"]) or {}
                    row["session_id"] = reg.get("session_id")
                    jp = _session_jsonl(s.get("cwd", ""), bool(reg.get("container")),
                                        reg.get("session_id"))
                    row["title"] = _session_title(jp) or s.get("name")
                rows.append(row)
            self._send(200, json.dumps({"sessions": rows,
                                        "home": os.path.expanduser("~")}).encode(),
                       "application/json")
            return
        if self.path.startswith("/api/sessions"):
            body = json.dumps({"sessions": read_sessions(),
                               "home": os.path.expanduser("~")}).encode()
            self._send(200, body, "application/json")
        elif self.path.startswith("/api/tile-image"):
            # Out-of-band inline-image delivery. The image-mcp `show_image` tool
            # spools images to /tmp/claude-tile-images/<port>.ndjson keyed by the
            # tile's ttyd PORT (which it derives from its process ancestry). The
            # term-client (cross-origin, on that port) polls this with its own
            # location.port and renders each new line via the addon — the only
            # path that reaches a CLAUDE tile, since claude Stop hooks have no
            # controlling tty and can't write /dev/tty. Read-only + localhost +
            # non-sensitive (the user's own images), so no CSRF; CORS-echoed to
            # the local tile origin like /api/dropfile.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            port = (q.get("port") or [""])[0]
            origin = self._cors_origin()
            extra = [("Cache-Control", "no-store")]
            if origin:
                extra.append(("Access-Control-Allow-Origin", origin))
            if not port.isdigit():
                self._send(400, b'{"error":"bad port"}', "application/json",
                           extra_headers=extra)
                return
            try:
                since_n = int((q.get("since") or ["0"])[0])
            except ValueError:
                since_n = 0
            spool = os.path.join("/tmp/claude-tile-images", port + ".ndjson")
            imgs, total = [], 0
            # Cap the walk so a runaway spool can't make every poll re-read an
            # unbounded file. try/except: the file can vanish or change perms
            # between isfile() and open() (the writer rotates/removes it) — a
            # race there should yield an empty list, not a 500.
            try:
                with open(spool, "r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        total = i + 1
                        if i >= 100000:
                            break
                        if i < since_n:
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            imgs.append(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                imgs, total = [], 0
            self._send(200, json.dumps({"images": imgs, "total": total}).encode(),
                       "application/json", extra_headers=extra)
        elif self.path.startswith("/proxy"):
            # Webview proxy. Two non-obvious safeguards:
            #
            # 1. SSRF amplification: a malicious page the user visits (even
            #    cross-origin) can GET /proxy via <img>/<iframe>/window.open
            #    — Host validation lets it through (the browser sends the real
            #    Host: 127.0.0.1 since that's where the connection goes). To
            #    keep this from becoming a generic LAN scanner, we accept ONLY
            #    a registered webview sid (no raw `url=` parameter), require
            #    the dashboard's CSRF token in the query (cross-origin pages
            #    can't read it thanks to same-origin policy on the dashboard
            #    HTML), and block server-side redirects. The user can still
            #    fetch LAN URLs by registering them; that's the feature.
            #
            # 2. Same-origin escalation: the proxied page would run under
            #    http://127.0.0.1:7680 and could fetch('/') to scrape the CSRF
            #    token, then fire /api/close on every session. We defuse this
            #    with `Content-Security-Policy: sandbox` (no allow-same-origin),
            #    which forces an opaque origin — scripts still run and can open
            #    WebSockets, but can't read the dashboard's own endpoints.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not secrets.compare_digest((q.get("csrf") or [""])[0], CSRF_TOKEN):
                self._send(403, b"bad csrf token", "text/plain")
                return
            url = _lookup_webview_url((q.get("id") or [""])[0])
            if not url:
                self._send(404, b"no such webview", "text/plain")
                return
            try:
                code, body, ctype = proxy_fetch(url)
            except (ValueError, OSError) as e:
                self._send(502, ("proxy error: %s" % e).encode(), "text/plain")
                return
            self._send(code, body, ctype, extra_headers=[
                ("Content-Security-Policy",
                 "sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox"),
                ("X-Frame-Options", "SAMEORIGIN"),
                ("Referrer-Policy", "no-referrer"),
            ])
        elif self.path == "/api/channels":
            body = json.dumps({"channels": list_channels()}).encode()
            self._send(200, body, "application/json")
        elif self.path == "/api/launchers":
            # Configurable + New launcher presets (read by the menu + manager).
            body = json.dumps({"launchers": load_launchers()}).encode()
            self._send(200, body, "application/json")
        elif self.path.startswith("/api/icon"):
            # Title -> Lucide icon name, AI-resolved + disk-cached. This GET can
            # spend an API call, so it's CSRF-gated like /proxy: a cross-origin
            # page can't read the token, so it can't drive cost amplification.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not secrets.compare_digest((q.get("csrf") or [""])[0], CSRF_TOKEN):
                self._send(403, b"bad csrf token", "text/plain")
                return
            name = resolve_icon((q.get("title") or [""])[0], (q.get("cwd") or [""])[0])
            self._send(200, json.dumps({"icon": name}).encode(), "application/json")
        elif self.path.startswith("/api/channel/"):
            # /api/channel/<name>?since=N → read newer messages
            tail = self.path[len("/api/channel/"):]
            qsplit = tail.split("?", 1)
            name = urllib.parse.unquote(qsplit[0])
            q = urllib.parse.parse_qs(qsplit[1] if len(qsplit) > 1 else "")
            since = (q.get("since") or ["0"])[0]
            data, code = read_channel(name, since)
            self._send(code, json.dumps(data).encode(), "application/json")
        elif self.path.startswith("/channel/"):
            # /channel/<name> → chatroom HTML page (iframe target). Validate
            # the name before substituting it into the template; a stray
            # script-tag-via-name would already be html-escaped, but the
            # safe-charset check makes it impossible to even reach that
            # path.
            tail = self.path[len("/channel/"):].split("?", 1)[0]
            name = urllib.parse.unquote(tail)
            if not CHANNEL_NAME_RE.match(name):
                self._send(400, b"invalid channel name", "text/plain")
                return
            body = (CHANNEL_HTML
                    .replace("__CHANNEL_NAME__", name)
                    .replace("__CSRF_TOKEN__", CSRF_TOKEN)
                    .replace("__FONT_FACE__", FONT_FACE_CSS)
                    .replace("__DEFAULT_WHO__", DEFAULT_WHO)
                    .replace("__BASE__", BASE_PATH)
            ).encode()
            self._send(200, body, "text/html; charset=utf-8")
        elif self.path.startswith("/api/note/"):
            # /api/note/<id> → the note's saved body. The editor fetches it via
            # r.text() and re-sanitizes before applying it. Serve it as
            # text/plain + nosniff (NOT text/html): the body is user-authored and
            # this is the dashboard's own origin (which holds the CSRF token), so
            # rendering it as HTML would be stored XSS if the page were opened
            # directly or a local process wrote a <script> into the sidecar.
            # text/plain + nosniff makes the endpoint non-rendering regardless.
            sid = urllib.parse.unquote(self.path[len("/api/note/"):].split("?", 1)[0])
            if not NOTE_SID_RE.match(sid):
                self._send(400, b"invalid note id", "text/plain")
                return
            self._send(200, read_note_body(sid).encode("utf-8"),
                       "text/plain; charset=utf-8",
                       [("X-Content-Type-Options", "nosniff")])
        elif self.path.startswith("/note/"):
            # /note/<id> → the note editor HTML page (iframe target), served
            # same-origin so it can POST back its body without a CORS dance.
            sid = urllib.parse.unquote(self.path[len("/note/"):].split("?", 1)[0])
            if not NOTE_SID_RE.match(sid):
                self._send(400, b"invalid note id", "text/plain")
                return
            body = (NOTE_HTML
                    .replace("__NOTE_SID__", sid)
                    .replace("__CSRF_TOKEN__", CSRF_TOKEN)
                    .replace("__FONT_FACE__", FONT_FACE_CSS)
                    .replace("__BASE__", BASE_PATH)
            ).encode()
            self._send(200, body, "text/html; charset=utf-8")
        elif self.path == "/chat-history" or self.path.startswith("/chat-history/"):
            self._serve_chat_history()
        elif self.path == "/" or self.path.startswith("/index"):
            body = (HTML
                .replace("__CSRF_TOKEN__", CSRF_TOKEN)
                .replace("__FONT_FACE__", FONT_FACE_CSS)
                .replace("__FONTS_JSON__", FONTS_JSON)
                .replace("__LUCIDE_ICONS_JSON__", LUCIDE_ICONS_JSON)
                .replace("__DEFAULT_FONT_ID__", DEFAULT_FONT_ID)
                .replace("__BASE__", BASE_PATH)
            ).encode()
            self._send(200, body, "text/html; charset=utf-8")
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        self._strip_base()
        if not self._host_ok():
            return
        if self.path.startswith("/api/hook-event"):
            # Claude Code hook forwarder (hooks/dashboard-notify.sh). Hooks run
            # outside the browser, so they authenticate with the registry-scoped
            # token file instead of the page's per-process CSRF token.
            tok = _hook_token()
            if not tok or not secrets.compare_digest(
                    self.headers.get("X-Hook-Token", ""), tok):
                self._send(403, b"bad hook token", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if 0 < length <= 2_000_000 else b""
            # ?ppid=<hook script's $PPID> — lets hook_event map the event to
            # its tile by process ancestry and keep the registry session_id
            # tracking the tile's LIVE conversation. Absent from older hook
            # script installs; everything degrades to the previous behaviour.
            ppid = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query).get("ppid", [None])[0]
            try:
                ok = hook_event(json.loads(raw.decode("utf-8", "replace")), ppid=ppid)
            except ValueError:
                ok = False
            self._send(200, json.dumps({"ok": bool(ok)}).encode(), "application/json")
            return
        if self.path.startswith("/api/agent-close"):
            # An agent INSIDE a tile asking to close (or ?stash=1) its own tile
            # (hooks/close-tile.sh + the close-tile skill). Like /api/hook-event
            # it runs outside the browser, so it authenticates with the
            # registry-scoped .hook-token, not the page CSRF token. The tile is
            # resolved from the caller's process ancestry (?ppid=) exactly as
            # hook events are — the agent need not know its own sid. ?session_id=
            # is a fallback when the ancestry walk misses. The server reads the
            # whole request before close_session signals anything, so the
            # self-kill is safe (the agent's curl just sees a reset).
            tok = _hook_token()
            if not tok or not secrets.compare_digest(
                    self.headers.get("X-Hook-Token", ""), tok):
                self._send(403, b"bad hook token", "text/plain")
                return
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            sid = (_tile_for_pid((q.get("ppid") or [None])[0])
                   or _tile_for_session_id((q.get("session_id") or [""])[0]))
            if not sid:
                self._send(404, json.dumps(
                    {"ok": False, "error": "tile not found"}).encode(),
                    "application/json")
                return
            stash = (q.get("stash") or ["0"])[0] in ("1", "true")
            ok = stash_session(sid, True) if stash else close_session(sid)
            self._send(200, json.dumps(
                {"ok": bool(ok), "id": sid, "stashed": stash}).encode(),
                "application/json")
            return
        if self.path.startswith("/api/agent-new"):
            # Agent-spawned tile: an agent inside a tile asks to open a NEW tile
            # (terminal, claude, note, webview, etc). Token-authed like
            # /api/agent-close and /api/agent-msg. Reuses the same spawn_* functions
            # the dashboard /api/new uses, so all kinds work the same way.
            tok = _hook_token()
            if not tok or not secrets.compare_digest(
                    self.headers.get("X-Hook-Token", ""), tok):
                self._send(403, b"bad hook token", "text/plain")
                return
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            # Large webview content (inline image / html data: URLs) overflows the
            # query-string ceiling, so accept the url in the request body too.
            try:
                _clen = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                _clen = 0
            body_url = (self.rfile.read(_clen).decode("utf-8", "replace").strip()
                        if 0 < _clen <= 8_000_000 else "")
            kind = (q.get("kind") or [""])[0]
            cwd = (q.get("cwd") or [None])[0]
            # Default the new tile's cwd to the CALLER's tile cwd (resolved from
            # ?ppid= via process ancestry, like /api/agent-close) so the tile
            # lands in the agent's OWN tab, not the home (~) tab. An explicit
            # ?cwd= always wins.
            if not cwd:
                caller = _tile_for_pid((q.get("ppid") or [None])[0])
                rec = _registry_record(caller) if caller else None
                if rec and rec.get("cwd"):
                    cwd = rec["cwd"]
            name = (q.get("name") or [None])[0]
            sid = None
            err = None
            if kind == "launcher":
                lid = (q.get("id") or [""])[0]
                pre = next((l for l in load_launchers() if l["id"] == lid), None)
                if not pre:
                    err = "unknown launcher %r" % lid
                else:
                    sid = spawn_launcher(pre, cwd=cwd, name=name)
                    if not sid:
                        err = "failed to spawn launcher tile (check cwd?)"
            elif kind == "claude":
                provider = (q.get("provider") or [None])[0]
                sid = spawn_claude(cwd=cwd, name=name, provider=provider)
                if not sid:
                    err = "failed to spawn claude tile (check cwd?)"
            elif kind == "terminal":
                sid = spawn_terminal(cwd=cwd, name=name)
                if not sid:
                    err = "failed to spawn terminal tile (check cwd?)"
            elif kind == "opencode":
                sid = spawn_opencode(cwd=cwd, name=name)
                if not sid:
                    err = "failed to spawn opencode tile"
            elif kind == "note":
                sid = spawn_note(name=name, cwd=cwd)
                if not sid:
                    err = "failed to spawn note tile"
            elif kind == "channel":
                cname = (q.get("channel_name") or [""])[0]
                sid = spawn_channel_tile(cname)
                if not sid:
                    err = "failed to spawn channel tile"
            elif kind == "webview":
                url = (q.get("url") or [""])[0] or body_url
                proxy = (q.get("proxy") or ["0"])[0] in ("1", "true")
                sid = create_webview(url=url, name=name, cwd=cwd, proxy=proxy)
                if not sid:
                    err = "failed to spawn webview tile (bad url?)"
            else:
                err = "unknown kind %r" % kind
            if sid:
                self._send(200, json.dumps(
                    {"ok": True, "id": sid}).encode(), "application/json")
            else:
                self._send(400, json.dumps(
                    {"ok": False, "error": err or "unknown error"}).encode(),
                    "application/json")
            return
        if self.path.startswith("/api/agent-restore"):
            # Restore a PAST claude conversation into a new tile by keyword-
            # matching its title (?q=), or by exact ?session_id=. Token-authed
            # like the other agent-* endpoints. ?dry=1 only searches (returns
            # candidates, spawns nothing). Otherwise: 1 match → resume it via
            # spawn_claude(extra=["--resume", <id>]); >1 → 409 with candidates
            # for the caller to pick (re-call with session_id); 0 → 404.
            tok = _hook_token()
            if not tok or not secrets.compare_digest(
                    self.headers.get("X-Hook-Token", ""), tok):
                self._send(403, b"bad hook token", "text/plain")
                return
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            want_sid = (q.get("session_id") or [""])[0]
            kw = (q.get("q") or [""])[0].strip().lower()
            dry = (q.get("dry") or ["0"])[0] in ("1", "true")
            convs = _all_conversations()
            if want_sid:
                matches = [c for c in convs if c["session_id"] == want_sid]
            elif kw:
                matches = [c for c in convs
                           if kw in c["title"].lower() or kw in c["cwd"].lower()]
            else:
                self._send(400, json.dumps(
                    {"ok": False, "error": "need ?q=<keyword> or ?session_id="}).encode(),
                    "application/json")
                return
            if dry:
                self._send(200, json.dumps(
                    {"ok": True, "candidates": matches[:20]}).encode(),
                    "application/json")
                return
            if not matches:
                self._send(404, json.dumps(
                    {"ok": False, "candidates": [], "error":
                     "no conversation matches %r" % (want_sid or kw)}).encode(),
                    "application/json")
                return
            if len(matches) > 1:
                self._send(409, json.dumps(
                    {"ok": False, "candidates": matches[:20], "error":
                     "%d conversations match — re-call with an exact session_id"
                     % len(matches)}).encode(), "application/json")
                return
            m = matches[0]
            if not os.path.isdir(m["cwd"]):
                self._send(400, json.dumps(
                    {"ok": False, "error": "cwd no longer exists: %s" % m["cwd"]}).encode(),
                    "application/json")
                return
            sid = spawn_claude(cwd=m["cwd"], name=(m["title"] or None),
                               extra=["--resume", m["session_id"]])
            if sid:
                self._send(200, json.dumps(
                    {"ok": True, "id": sid, "restored": m}).encode(), "application/json")
            else:
                self._send(400, json.dumps(
                    {"ok": False, "error": "failed to spawn resumed tile"}).encode(),
                    "application/json")
            return
        if self.path.startswith("/api/agent-msg"):
            # Agent-to-agent: deliver a message into ANOTHER tile's claude
            # prompt, addressed by tile name (?to=) or sid. Token-authed like
            # /api/agent-close (runs outside the browser). The text is injected
            # via chat_send — bracketed paste, control bytes stripped — so it
            # lands as one prompt and WAKES an idle agent, which a channel poll
            # can't. This is the recruiting half of the channel bridge: the
            # actual back-and-forth then runs over the channel skill.
            tok = _hook_token()
            if not tok or not secrets.compare_digest(
                    self.headers.get("X-Hook-Token", ""), tok):
                self._send(403, b"bad hook token", "text/plain")
                return
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            to = (q.get("to") or [""])[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if 0 < length <= 256_000 else b""
            msg = raw.decode("utf-8", "replace")
            # An exact sid wins; otherwise resolve by tile name (which may be
            # ambiguous → 409 with candidates for the caller to pick from).
            if _registry_record(to):
                sid = to
            else:
                sid, cands = _tile_for_name(to)
                if not sid:
                    self._send(404 if not cands else 409, json.dumps(
                        {"ok": False, "candidates": cands,
                         "error": ("no tile matches %r" % to) if not cands
                         else ("ambiguous tile name %r" % to)}).encode(),
                        "application/json")
                    return
            ok, err = chat_send(sid, msg)
            self._send(200 if ok else 400, json.dumps(
                {"ok": bool(ok), "id": sid, "error": err}).encode(),
                "application/json")
            return
        # All other POSTs are state-changing and CSRF-guarded.
        if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), CSRF_TOKEN):
            self._send(403, b"bad csrf token", "text/plain")
            return
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if self.path.startswith("/api/close"):
            ok = close_session((q.get("id") or [""])[0])
            self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
        elif self.path.startswith("/api/stash"):
            ok = stash_session((q.get("id") or [""])[0],
                               (q.get("on") or ["1"])[0] in ("1", "true"))
            self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
        elif self.path.startswith("/api/duplicate"):
            ok = duplicate_session((q.get("id") or [""])[0])
            self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
        elif self.path.startswith("/api/chat-export"):
            # "Search" button: (re)build the Claude-chat search index by running
            # claude-chat-export.py, then the client opens /chat-history/index.html.
            ok, msg = run_chat_export()
            self._send(200 if ok else 500,
                       json.dumps({"ok": ok, "error": None if ok else msg}).encode(),
                       "application/json")
        elif self.path.startswith("/api/restart"):
            # Settings ▸ Restart server: ack first, then re-exec serve.py.
            self._send(200, json.dumps({"ok": True}).encode(), "application/json")
            restart_server()
        elif self.path.startswith("/api/fork"):
            ok = fork_session((q.get("id") or [""])[0])
            self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
        elif self.path.startswith("/api/chat-send"):
            # Chat panel composer: inject a typed message into the session's PTY.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if 0 < length <= 1_000_000 else b""
            ok, err = chat_send((q.get("id") or [""])[0], raw.decode("utf-8", "replace"))
            self._send(200 if ok else 400,
                       json.dumps({"ok": ok, "error": err}).encode(), "application/json")
        elif self.path.startswith("/api/chat-key"):
            # Chat panel: one keystroke (question-option digit, enter, esc) into
            # the session PTY — drives claude's own TUI selector for
            # AskUserQuestion cards and permission prompts.
            ok, err = chat_key((q.get("id") or [""])[0], (q.get("key") or [""])[0])
            self._send(200 if ok else 400,
                       json.dumps({"ok": ok, "error": err}).encode(), "application/json")
        elif self.path.startswith("/api/new"):
            kind = (q.get("kind") or [""])[0]
            if kind == "launcher":
                # Configurable agent launcher: look the preset up by id and spawn
                # it. spawn_launcher routes claude/codex/opencode to their smart
                # paths (resume/fork/chat) and anything else to a kind=custom tile.
                lid = (q.get("id") or [""])[0]
                pre = next((l for l in load_launchers() if l["id"] == lid), None)
                if not pre:
                    self._send(404, b"unknown launcher", "text/plain"); return
                sid = spawn_launcher(pre, cwd=(q.get("cwd") or [None])[0])
            elif kind == "claude":
                # Host claude tile: spawn_claude self-manages ttyd+dtach+registry
                # (mirrors spawn_opencode) and returns the sid, so it flows
                # through the shared {ok, id} response below like the other kinds.
                sid = spawn_claude(cwd=(q.get("cwd") or [None])[0],
                                   name=(q.get("name") or [None])[0],
                                   provider=(q.get("provider") or [None])[0])
            elif kind == "codex":
                sid = spawn_codex(cwd=(q.get("cwd") or [None])[0],
                                  name=(q.get("name") or [None])[0])
            elif kind == "terminal":
                sid = spawn_terminal(cwd=(q.get("cwd") or [None])[0],
                                     name=(q.get("name") or [None])[0])
            elif kind == "container-terminal":
                sid = spawn_container_terminal(cwd=(q.get("cwd") or [None])[0],
                                               name=(q.get("name") or [None])[0])
            elif kind == "opencode":
                sid = spawn_opencode(cwd=(q.get("cwd") or [None])[0],
                                     name=(q.get("name") or [None])[0])
            elif kind == "channel":
                sid = spawn_channel_tile((q.get("name") or [""])[0])
            elif kind == "note":
                sid = spawn_note(name=(q.get("name") or [None])[0],
                                 cwd=(q.get("cwd") or [None])[0])
            elif kind == "webview":
                sid = create_webview(url=(q.get("url") or [""])[0],
                                     name=(q.get("name") or [None])[0],
                                     cwd=(q.get("cwd") or [None])[0],
                                     proxy=(q.get("proxy") or ["0"])[0] in ("1", "true"))
            else:
                self._send(400, b"unknown kind", "text/plain")
                return
            body = json.dumps({"ok": bool(sid), "id": sid}).encode()
            self._send(200 if sid else 500, body, "application/json")
        elif self.path == "/api/launchers":
            # POST body {launchers:[{label,command,env?,provider?,icon?}, ...]} —
            # replace the whole list (CSRF enforced above). Returns the cleaned,
            # persisted list so the UI re-renders from the canonical form.
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                self._send(400, b"bad json", "text/plain"); return
            saved = save_launchers((payload or {}).get("launchers"))
            if saved is None:
                self._send(500, b"could not save launchers", "text/plain"); return
            self._send(200, json.dumps({"ok": True, "launchers": saved}).encode(),
                       "application/json")
        elif self.path.startswith("/api/channel/"):
            # POST /api/channel/<name> body={from, text} — append a message.
            # Same-origin call from the chatroom iframe (also at the
            # dashboard's origin), so CSRF is enforced normally; no CORS
            # plumbing needed.
            tail = self.path[len("/api/channel/"):]
            name = urllib.parse.unquote(tail.split("?", 1)[0])
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                self._send(400, b"bad json", "text/plain"); return
            sender = (payload or {}).get("from")
            text = (payload or {}).get("text")
            if not isinstance(sender, str) or not isinstance(text, str):
                self._send(400, b"need {from,text} strings", "text/plain"); return
            if len(text) > 64 * 1024:
                self._send(413, b"message too large", "text/plain"); return
            ok = append_channel(name, sender, text)
            self._send(200 if ok else 400,
                       json.dumps({"ok": ok}).encode(), "application/json")
        elif self.path.startswith("/api/note/"):
            # POST /api/note/<id> body={html} — save the note body. Same-origin
            # call from the note editor iframe, so CSRF is enforced above.
            sid = urllib.parse.unquote(self.path[len("/api/note/"):].split("?", 1)[0])
            if not NOTE_SID_RE.match(sid):
                self._send(400, b"invalid note id", "text/plain"); return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > NOTE_MAX_BYTES:
                self._send(413, b"note too large", "text/plain"); return
            try:
                raw = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                self._send(400, b"bad json", "text/plain"); return
            html = (payload or {}).get("html")
            if not isinstance(html, str):
                self._send(400, b"need {html} string", "text/plain"); return
            ok = write_note_body(sid, html)
            self._send(200 if ok else 400,
                       json.dumps({"ok": ok}).encode(), "application/json")
        elif self.path.startswith("/api/dropfile"):
            sid = (q.get("sid") or [""])[0]
            name = (q.get("name") or [""])[0]
            if not sid or not name:
                self._send(400, b"missing sid or name", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self._send(400, b"empty body", "text/plain")
                return
            if length > MAX_DROP_BYTES:
                self._send(413, b"too large", "text/plain")
                return
            data = self.rfile.read(length)
            path = save_dropped_file(sid, name, data)
            if not path:
                self._send(404, b"session not found or unwritable cwd", "text/plain")
                return
            # Echo Origin on the response so the cross-origin iframe's fetch
            # gets the CORS clearance — without it the browser would discard
            # the response and reject .json() with a CORS error.
            origin = self._cors_origin()
            extra = [("Access-Control-Allow-Origin", origin)] if origin else []
            self._send(200, json.dumps({"path": path}).encode(),
                       "application/json", extra_headers=extra)
        elif self.path.startswith("/api/webview"):
            proxy = None
            if "proxy" in q:
                proxy = q["proxy"][0] in ("1", "true")
            ok = update_webview((q.get("id") or [""])[0],
                                url=q["url"][0] if "url" in q else None,
                                name=q["name"][0] if "name" in q else None,
                                proxy=proxy)
            self._send(200, json.dumps({"ok": ok}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, format, *args):  # noqa: A002 - match base signature
        pass  # quiet


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    os.makedirs(REGISTRY, exist_ok=True)
    # Credentials for the dashboard-notify claude hook (see hooks/ and
    # /api/hook-event): the token authenticates it, the port file tells it
    # where this instance listens.
    try:
        _hook_token(create=True)
        with open(os.path.join(REGISTRY, ".hook-port"), "w") as f:
            f.write(str(PORT))
    except OSError:
        pass
    # Revive tiles that died with the host (reboot) before the first
    # /api/sessions can prune their registry entries. --no-resurrect opts out
    # (debugging, or deliberately starting with whatever is actually running).
    if "--no-resurrect" not in sys.argv:
        n = resurrect_sessions()
        if n:
            print("resurrected %d tile(s) from the registry" % n)
    httpd = Server(("127.0.0.1", PORT), Handler)
    url = "http://127.0.0.1:%d/" % PORT
    print("claude-sessions dashboard -> %s   (registry: %s)" % (url, REGISTRY))
    if "--no-open" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
