#!/bin/sh
# Restore a PAST claude conversation into a new dashboard tile by keyword-
# matching its title (or by exact session id). Token-authed like the other
# agent-* hooks. Spawns `claude --resume <session_id>` in the conversation's
# original cwd, so the tile lands in that project's tab.
#
# Usage:
#   restore-session.sh "ewmac breadth"          # restore the matching session
#   restore-session.sh --search "docker"        # just list matches, restore nothing
#   restore-session.sh --session-id <uuid>       # restore an exact session
#
# On multiple matches it prints the candidates (id + title + cwd) so you can
# re-run with --session-id <uuid> to pick one.
D="${CLAUDE_SESSIONS_DIR:-$HOME/.claude-sessions}"
T=$(cat "$D/.hook-token" 2>/dev/null)
[ -n "$T" ] || { echo '{"ok": false, "error": "dashboard not running"}'; exit 1; }
P=$(cat "$D/.hook-port" 2>/dev/null)

KW="" SID="" DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --search)     KW="$2"; DRY=1; shift 2 ;;
    --session-id) SID="$2"; shift 2 ;;
    --*)          echo "{\"ok\": false, \"error\": \"unknown arg $1\"}"; exit 2 ;;
    *)            KW="$1"; shift ;;
  esac
done

enc() { python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$1"; }
QS=""
[ -n "$SID" ] && QS="session_id=$(enc "$SID")"
[ -n "$KW" ]  && QS="${QS:+$QS&}q=$(enc "$KW")"
[ "$DRY" = "1" ] && QS="${QS:+$QS&}dry=1"
[ -n "$QS" ] || { echo 'usage: restore-session.sh "<keyword>" | --search <kw> | --session-id <uuid>'; exit 2; }

curl -s -m 15 -X POST -H "X-Hook-Token: $T" \
  "http://127.0.0.1:${P:-7680}/api/agent-restore?$QS"
echo
