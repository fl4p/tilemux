#!/bin/sh
# Forward a Claude Code hook event (the stdin JSON) to the session dashboard's
# /api/hook-event, giving the chat panel ground-truth busy/idle (Stop,
# UserPromptSubmit, Pre/PostToolUse) and pending permission prompts
# (Notification) that the transcript .jsonl never records.
#
# Installed as a user-level hook in ~/.claude/settings.json for the events
# above (copy to ~/.claude/hooks/ and reference it there; see SPEC.md).
# Fails silent and fast: if the dashboard isn't running — or never ran, so no
# token file exists — claude is unaffected.
D="${CLAUDE_SESSIONS_DIR:-$HOME/.claude-sessions}"
T=$(cat "$D/.hook-token" 2>/dev/null)
[ -n "$T" ] || exit 0
P=$(cat "$D/.hook-port" 2>/dev/null)
# $PPID is the claude process this hook belongs to (or a shell claude spawned
# in between) — the dashboard walks its ancestry to the tile's dtach master to
# learn WHICH tile the event's session_id is the live conversation of.
curl -s -m 2 -X POST -H "X-Hook-Token: $T" -H "Content-Type: application/json" \
  --data-binary @- "http://127.0.0.1:${P:-7680}/api/hook-event?ppid=$PPID" >/dev/null 2>&1 || true
