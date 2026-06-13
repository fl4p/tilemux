#!/usr/bin/env python3
"""Native WezTerm session backend — runnable slice (Plan B2, phases 0-1).

Drives a headless `wezterm-mux-server` as the persistent PTY layer (ttyd+dtach's
job today). Sessions become native WezTerm tabs. This is standalone — it does not
touch serve.py, so the existing ttyd dashboard keeps working alongside it.

CLI:
    python3 wzmux.py new   --kind claude|opencode|terminal [--cwd DIR] [--name N]
    python3 wzmux.py list
    python3 wzmux.py close --pane PANE_ID
    python3 wzmux.py projects        # for the wezterm.lua launcher palette
    python3 wzmux.py gui             # attach a native GUI window to the mux
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

WEZTERM = shutil.which("wezterm") or "wezterm"
MUX_SERVER = shutil.which("wezterm-mux-server") or "wezterm-mux-server"
STATE = os.path.expanduser("~/.local/share/wezterm")
REGISTRY = os.path.expanduser("~/.claude-sessions")
CLAUDE = shutil.which("claude")
OPENCODE = shutil.which("opencode")

_TS = re.compile(r"^\d\d:\d\d:\d\d\.\d+\s+(WARN|ERROR|INFO)\b")


# --- mux lifecycle ---------------------------------------------------------
def _prune_stale_socklink():
    """The CLI follows ~/.local/share/wezterm/default-org… to a gui-sock-<pid>;
    when that GUI is gone the link dangles and every `wezterm cli` call fails.
    Remove it so calls fall back to the mux socket. (Hit live during prototyping.)"""
    link = os.path.join(STATE, "default-org.wezfurlong.wezterm")
    try:
        if os.path.islink(link) and not os.path.exists(os.path.realpath(link)):
            os.remove(link)
    except OSError:
        pass


def _mux_running():
    try:
        return subprocess.run(["pgrep", "-f", "wezterm-mux-server"],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def ensure_mux():
    _prune_stale_socklink()
    if _mux_running() and _list_raw() is not None:
        return True
    # daemonize on unix; on Windows start detached (no --daemonize flag).
    if os.name == "posix":
        subprocess.run([MUX_SERVER, "--daemonize"], check=False)
    else:
        subprocess.Popen([MUX_SERVER], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    for _ in range(50):
        if _list_raw() is not None:
            return True
        time.sleep(0.1)
    return False


# --- cli wrapper -----------------------------------------------------------
def wz(*args):
    """Run `wezterm cli <args>`; return stdout with wezterm's leading log lines
    stripped, or None on failure."""
    try:
        p = subprocess.run([WEZTERM, "cli", *args], capture_output=True, text=True)
    except OSError:
        return None
    if p.returncode != 0:
        return None
    return "\n".join(ln for ln in p.stdout.splitlines() if not _TS.match(ln))


def _list_raw():
    out = wz("list", "--format", "json")
    if out is None:
        return None
    out = out.strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except ValueError:
        return None


# --- sidecar registry (only metadata wezterm doesn't track) ----------------
def _sidecar_path(pane_id):
    return os.path.join(REGISTRY, "pane-%s.json" % pane_id)


def _write_sidecar(pane_id, meta):
    os.makedirs(REGISTRY, exist_ok=True)
    try:
        with open(_sidecar_path(pane_id), "w") as f:
            json.dump(meta, f)
    except OSError:
        pass


def _read_sidecar(pane_id):
    try:
        with open(_sidecar_path(pane_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --- public API ------------------------------------------------------------
def list_panes():
    """Live sessions = wezterm panes joined with sidecar metadata."""
    raw = _list_raw() or []
    # prune sidecars for panes that no longer exist
    live = {str(p["pane_id"]) for p in raw}
    try:
        for fn in os.listdir(REGISTRY):
            m = re.match(r"pane-(\d+)\.json$", fn)
            if m and m.group(1) not in live:
                os.remove(os.path.join(REGISTRY, fn))
    except OSError:
        pass
    out = []
    for p in raw:
        pid = str(p["pane_id"])
        meta = _read_sidecar(pid)
        cwd = p.get("cwd", "")
        if cwd.startswith("file://"):
            cwd = urllib.parse.unquote(urllib.parse.urlparse(cwd).path)
        out.append({
            "pane_id": p["pane_id"], "tab_id": p.get("tab_id"),
            "window_id": p.get("window_id"), "cwd": cwd,
            "title": p.get("title", ""), "is_active": p.get("is_active", False),
            "kind": meta.get("kind", "shell"), "name": meta.get("name", ""),
        })
    return out


def _anchor_pane():
    raw = _list_raw() or []
    return str(min(p["pane_id"] for p in raw)) if raw else None


def _prog_for(kind, cwd):
    if kind == "claude":
        if not CLAUDE:
            return None
        return [CLAUDE, "--dangerously-skip-permissions"]
    if kind == "opencode":
        if not OPENCODE:
            return None
        return [OPENCODE, cwd]
    return [os.environ.get("SHELL", "/bin/zsh"), "-i"]  # terminal


def new(kind="terminal", cwd=None, name=None):
    """Spawn a session as a new native tab in the mux. Returns pane_id or None."""
    cwd = cwd or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        return None
    prog = _prog_for(kind, cwd)
    if prog is None:
        return None
    ensure_mux()
    args = ["spawn"]
    anchor = _anchor_pane()
    if anchor:
        args += ["--pane-id", anchor]   # spawn as a TAB in the dashboard window
    args += ["--cwd", cwd, "--", *prog]
    out = wz(*args)
    if out is None:
        return None
    m = re.search(r"\d+", out)
    if not m:
        return None
    pane_id = m.group(0)
    name = name or os.path.basename(cwd.rstrip("/")) or kind
    _write_sidecar(pane_id, {"kind": kind, "cwd": cwd, "name": name})
    wz("set-tab-title", "--pane-id", pane_id, "%s · %s" % (kind, name))
    return pane_id


def _panes_in_tab(tab_id, exclude=None):
    return [p for p in (_list_raw() or [])
            if p.get("tab_id") == tab_id and p["pane_id"] != exclude]


def _split_axis(pane):
    """Return '--right' or '--bottom' to split a pane along its longer axis,
    so repeated splits converge on a balanced grid."""
    sz = pane.get("size", {}) or {}
    w = sz.get("pixel_width") or sz.get("cols", 80)
    h = sz.get("pixel_height") or (sz.get("rows", 24) * 2)  # ~cell aspect
    return "--right" if w >= h else "--bottom"


def tile(layout="grid"):
    """Pull every live session pane into ONE window as a tiled layout — the
    native equivalent of the old dashboard grid. layout: grid | col | row."""
    raw = _list_raw() or []
    if len(raw) < 2:
        return False
    base = raw[0]
    tab = base["tab_id"]                       # consolidate everything INTO this tab
    for p in [x for x in raw if x["pane_id"] != base["pane_id"]]:
        tiles = _panes_in_tab(tab, exclude=p["pane_id"])  # hosts already in the tab
        if not tiles:
            continue
        if layout == "col":
            host, direction = tiles[-1], "--bottom"
        elif layout == "row":
            host, direction = tiles[-1], "--right"
        else:  # grid: split the largest current tile along its longer axis
            host = max(tiles, key=lambda t: (t.get("size", {}).get("pixel_width", 80)
                                             * t.get("size", {}).get("pixel_height", 240)))
            direction = _split_axis(host)
        wz("split-pane", "--pane-id", str(host["pane_id"]),
           "--move-pane-id", str(p["pane_id"]), direction)
    return True


def stress(panes=4, cwd=None):
    """Spawn N tiled terminals for assessing snappiness: lots of live panes
    rendering at once. Returns the list of pane-ids."""
    cwd = cwd or os.getcwd()
    ids = []
    for i in range(panes):
        pid = new("terminal", cwd, name="stress-%d" % (i + 1))
        if pid:
            ids.append(pid)
    tile("grid")
    return ids


def close(pane_id):
    pane_id = str(pane_id)
    ok = wz("kill-pane", "--pane-id", pane_id) is not None
    try:
        os.remove(_sidecar_path(pane_id))
    except OSError:
        pass
    return ok


def projects():
    """Candidate cwds for the launcher palette: claude project dirs + git repos
    under ~/dev. Returns [{label, cwd}]."""
    seen, out = set(), []
    pj = os.path.expanduser("~/.claude/projects")
    try:
        for slug in sorted(os.listdir(pj)):
            # slug is the cwd with '/' -> '-'
            path = "/" + slug.lstrip("-").replace("-", "/")
            if os.path.isdir(path) and path not in seen:
                seen.add(path)
                out.append({"label": os.path.basename(path) + "  (" + path + ")", "cwd": path})
    except OSError:
        pass
    return out


def gui():
    """Attach a native GUI window to the running mux."""
    ensure_mux()
    _prune_stale_socklink()
    subprocess.Popen([WEZTERM, "connect", "unix"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- cli -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new"); p.add_argument("--kind", default="terminal")
    p.add_argument("--cwd"); p.add_argument("--name")
    sub.add_parser("list")
    p = sub.add_parser("close"); p.add_argument("--pane", required=True)
    p = sub.add_parser("tile"); p.add_argument("--layout", default="grid",
                                               choices=["grid", "col", "row"])
    p = sub.add_parser("stress"); p.add_argument("--panes", type=int, default=4)
    p.add_argument("--cwd")
    sub.add_parser("projects")
    sub.add_parser("gui")
    a = ap.parse_args()

    if a.cmd == "new":
        pid = new(a.kind, a.cwd, a.name)
        if pid is None:
            print("spawn failed", file=sys.stderr); sys.exit(1)
        print(pid)
    elif a.cmd == "list":
        for s in list_panes():
            print("%-4s %-9s %-22s %s" % (s["pane_id"], s["kind"], s["name"], s["cwd"]))
    elif a.cmd == "close":
        sys.exit(0 if close(a.pane) else 1)
    elif a.cmd == "tile":
        sys.exit(0 if tile(a.layout) else 1)
    elif a.cmd == "stress":
        ids = stress(a.panes, a.cwd)
        print("tiled %d panes: %s" % (len(ids), " ".join(ids)))
    elif a.cmd == "projects":
        print(json.dumps(projects()))
    elif a.cmd == "gui":
        gui()


if __name__ == "__main__":
    main()
