#!/bin/sh
# Send a message INTO another session-dashboard tile's claude prompt, addressed
# by its tile NAME (or sid). Token-authed like close-tile.sh; the dashboard
# resolves the name and injects the text via chat_send, so it arrives as one
# prompt and wakes the peer even if it's sitting idle (a channel poll can't do
# that). Prints the JSON result.
#
# Usage:
#   msg-tile.sh "<tile name or sid>" "<message text>"
#
# Exit non-zero if the dashboard isn't running or the target can't be resolved
# (the JSON body carries `candidates` when a name is ambiguous, so you can pick
# the exact one and retry with its id).
D="${CLAUDE_SESSIONS_DIR:-$HOME/.claude-sessions}"
T=$(cat "$D/.hook-token" 2>/dev/null)
[ -n "$T" ] || { echo "session dashboard not running (no hook token)"; exit 1; }
P=$(cat "$D/.hook-port" 2>/dev/null)
TO="$1"; shift 2>/dev/null
MSG="$*"
[ -n "$TO" ] || { echo 'usage: msg-tile.sh "<tile name or sid>" "<message>"'; exit 2; }
[ -n "$MSG" ] || { echo 'refusing to send an empty message'; exit 2; }
# Query-encode the target name (it can contain spaces); the message rides in the
# request body untouched so newlines survive (chat_send bracketed-pastes it).
TO_ENC=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$TO")
CODE=$(curl -s -m 8 -o /tmp/.msg-tile.out -w '%{http_code}' \
  -X POST -H "X-Hook-Token: $T" --data-binary "$MSG" \
  "http://127.0.0.1:${P:-7680}/api/agent-msg?to=$TO_ENC")
cat /tmp/.msg-tile.out; echo; rm -f /tmp/.msg-tile.out
[ "$CODE" = "200" ] || exit 1
