#!/bin/sh
# Agent renders an image, a URL, or an HTML snippet as a live webview tile in
# the dashboard. Token-authed like spawn-tile.sh.
#
# Usage:
#   show-tile.sh --image /path/to/pic.png [--name "label"]   # show an image file
#   cat pic.png | show-tile.sh --image - [--name "label"]    # image from stdin
#   show-tile.sh --url "https://example.com/report" [--name "label"]
#   show-tile.sh --url "http://localhost:3000" --proxy [--name "..."]
#   echo "<h1>hi</h1>" | show-tile.sh --html [--name "label"]
#   show-tile.sh --html --name "report" < /tmp/report.html
#
# Image and HTML content is sent as a data: URL in the request BODY (not the
# query string), so it sidesteps URL-length limits. Prints the JSON result.
D="${CLAUDE_SESSIONS_DIR:-$HOME/.claude-sessions}"
T=$(cat "$D/.hook-token" 2>/dev/null)
[ -n "$T" ] || { echo '{"ok": false, "error": "dashboard not running"}'; exit 1; }
P=$(cat "$D/.hook-port" 2>/dev/null)

URL="" NAME="" MODE="url" SRC="" PROXY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --url)    URL="$2"; MODE="url";   shift 2 ;;
    --image)  SRC="$2"; MODE="image"; shift 2 ;;
    --html)   MODE="html"; shift ;;
    --name)   NAME="$2"; shift 2 ;;
    --proxy)  PROXY=1; shift ;;
    *)        echo "{\"ok\": false, \"error\": \"unknown arg $1\"}"; exit 2 ;;
  esac
done

# Build the query string (kind, name, proxy). The url itself may go in the body.
enc() { python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$1"; }
# ppid lets the server resolve OUR tile by process ancestry and drop the new
# tile in the agent's own tab (not the home ~ tab).
QS="kind=webview&ppid=$$"
[ -n "$NAME" ] && QS="$QS&name=$(enc "$NAME")"
[ "$PROXY" = "1" ] && QS="$QS&proxy=1"

BODY=""
case "$MODE" in
  image)
    if [ "$SRC" = "-" ]; then
      # stdin -> temp file so `file` can sniff the mime type
      TMPF=$(mktemp /tmp/show-tile.XXXXXX)
      cat > "$TMPF"; SRC="$TMPF"
    fi
    [ -f "$SRC" ] || { echo "{\"ok\": false, \"error\": \"no such image: $SRC\"}"; exit 2; }
    MIME=$(file -b --mime-type "$SRC" 2>/dev/null)
    case "$MIME" in image/*) : ;; *) MIME="image/png" ;; esac
    B64=$(base64 < "$SRC" | tr -d '\n')
    BODY="data:$MIME;base64,$B64"
    [ -n "$TMPF" ] && rm -f "$TMPF"
    ;;
  html)
    HTML=$(cat)
    B64=$(printf '%s' "$HTML" | base64 | tr -d '\n')
    BODY="data:text/html;base64,$B64"
    ;;
  url)
    [ -n "$URL" ] || { echo '{"ok": false, "error": "need --url, --image, or --html"}'; exit 2; }
    # Short and safe in the query string.
    QS="$QS&url=$(enc "$URL")"
    ;;
esac

if [ -n "$BODY" ]; then
  printf '%s' "$BODY" | curl -s -m 20 -X POST -H "X-Hook-Token: $T" \
    --data-binary @- "http://127.0.0.1:${P:-7680}/api/agent-new?$QS"
else
  curl -s -m 8 -X POST -H "X-Hook-Token: $T" \
    "http://127.0.0.1:${P:-7680}/api/agent-new?$QS"
fi
echo
