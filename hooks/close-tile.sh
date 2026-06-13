#!/bin/sh
# Ask the session dashboard to close the tile this agent is running in (or
# --stash to hide it without ending the process). Mirrors dashboard-notify.sh:
# authenticates with the registry-scoped .hook-token, and lets the SERVER work
# out WHICH tile to close from this script's process ancestry ($$ walks up to
# the tile's dtach master) — so the agent never has to know its own tile id.
#
# Usage:
#   close-tile.sh            # hard close: terminate ttyd + dtach + the agent
#   close-tile.sh --stash    # stash: hide the tile, leave the process running
#
# Fails silent and fast if the dashboard isn't running (no token file), so a
# session that isn't dashboard-managed is unaffected.
D="${CLAUDE_SESSIONS_DIR:-$HOME/.claude-sessions}"
T=$(cat "$D/.hook-token" 2>/dev/null)
[ -n "$T" ] || { echo "session dashboard not running (no hook token); nothing to close"; exit 0; }
P=$(cat "$D/.hook-port" 2>/dev/null)
STASH=0
[ "$1" = "--stash" ] && STASH=1
# $$ is this script's pid; the server walks its ancestry (sh -> claude -> dtach
# master named in a registered tile socket) to resolve the tile. session_id is
# a best-effort fallback for when that walk misses (e.g. extra wrapper layers).
curl -s -m 5 -X POST -H "X-Hook-Token: $T" \
  "http://127.0.0.1:${P:-7680}/api/agent-close?ppid=$$&stash=$STASH&session_id=${CLAUDE_CODE_SESSION_ID:-}"
echo
