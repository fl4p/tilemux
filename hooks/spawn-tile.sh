#!/bin/sh
# Agent spawns a NEW session-dashboard tile (terminal, claude, opencode, note,
# webview, channel). Token-authed like close-tile.sh / msg-tile.sh.
#
# Usage:
#   spawn-tile.sh --kind terminal --cwd /path
#   spawn-tile.sh --kind claude --cwd /path --name "my session"
#   spawn-tile.sh --kind note --name "title"
#   spawn-tile.sh --kind webview --url "https://example.com"
#   spawn-tile.sh --kind opencode --cwd /path
#
# Prints JSON result: {"ok": true, "id": "host-1234"} or error.
D="${CLAUDE_SESSIONS_DIR:-$HOME/.claude-sessions}"
T=$(cat "$D/.hook-token" 2>/dev/null)
[ -n "$T" ] || { echo '{"ok": false, "error": "dashboard not running"}'; exit 1; }
P=$(cat "$D/.hook-port" 2>/dev/null)

# Parse args: --flag value pairs
KIND="" CWD="" NAME="" URL="" PROXY="0" PROVIDER=""
while [ $# -gt 0 ]; do
  case "$1" in
    --kind)       KIND="$2"; shift 2 ;;
    --cwd)        CWD="$2"; shift 2 ;;
    --name)       NAME="$2"; shift 2 ;;
    --url)        URL="$2"; shift 2 ;;
    --proxy)      PROXY="$2"; shift 2 ;;
    --provider)   PROVIDER="$2"; shift 2 ;;
    *)            echo "{\"ok\": false, \"error\": \"unknown arg $1\"}"; exit 2 ;;
  esac
done

[ -n "$KIND" ] || { echo '{"ok": false, "error": "missing --kind"}'; exit 2; }

# Build query. ppid lets the server resolve OUR tile by process ancestry and,
# when no --cwd is given, drop the new tile in the agent's own tab (not ~).
QS="kind=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$KIND")&ppid=$$"
[ -n "$CWD" ] && QS="$QS&cwd=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$CWD")"
[ -n "$NAME" ] && QS="$QS&name=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$NAME")"
[ -n "$URL" ] && QS="$QS&url=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$URL")"
[ "$PROXY" = "1" ] && QS="$QS&proxy=1"
[ -n "$PROVIDER" ] && QS="$QS&provider=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$PROVIDER")"

curl -s -m 8 -X POST -H "X-Hook-Token: $T" \
  "http://127.0.0.1:${P:-7680}/api/agent-new?$QS"
echo
