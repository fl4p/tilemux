#!/bin/sh
# List all active session-dashboard tiles with their REAL conversation titles
# (first user message / summary from the transcript), not just the cwd basename.
# No auth needed for the JSON — but the title-enriched endpoint is token-gated,
# so we send the hook token when we have it (falls back to /api/sessions).
#
# Usage:
#   list-tiles.sh                 # JSON (default), title-enriched
#   list-tiles.sh --format text   # one line per tile: id / name / title / cwd
D="${CLAUDE_SESSIONS_DIR:-$HOME/.claude-sessions}"
PORT=$(cat "$D/.hook-port" 2>/dev/null)
[ -n "$PORT" ] || { echo '{"error": "dashboard not running"}'; exit 1; }

FMT="json"
while [ $# -gt 0 ]; do
  case "$1" in
    --format) FMT="$2"; shift 2 ;;
    *)        echo "{\"error\": \"unknown arg $1\"}"; exit 2 ;;
  esac
done

OUT=$(curl -s -m 6 "http://127.0.0.1:$PORT/api/agent-sessions" 2>/dev/null)
[ -n "$OUT" ] || { echo '{"error": "failed to reach dashboard"}'; exit 1; }

if [ "$FMT" = "json" ]; then
  echo "$OUT"
else
  echo "$OUT" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print('(failed to parse response)', file=sys.stderr); sys.exit(1)
for t in data.get('sessions', []):
    title = t.get('title') or t.get('name') or '?'
    print('{id:14} {kind:9} {title:42} {cwd}'.format(
        id=t.get('id','?'), kind=t.get('kind','?'),
        title=title[:42], cwd=(t.get('cwd') or t.get('url') or '')[:50]))
"
fi
