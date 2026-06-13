#!/usr/bin/env bash
# Build session-dashboard/term.html — a self-contained custom ttyd web client
# (xterm.js + fit + serialize addons + term-client.js, all inlined) so the page
# works standalone with no network or dashboard dependency. ttyd serves it via
# `-I term.html` and only that one file is served, hence everything is inlined.
#
# Re-run after editing term-client.js, or to bump xterm. The xterm libs are
# fetched from a CDN into a temp dir; only the built term.html is committed.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
base=https://cdn.jsdelivr.net/npm
curl -fsSL "$base/xterm@5.3.0/lib/xterm.js"                                  -o "$TMP/xterm.js"
curl -fsSL "$base/xterm@5.3.0/css/xterm.css"                                 -o "$TMP/xterm.css"
curl -fsSL "$base/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"              -o "$TMP/fit.js"
curl -fsSL "$base/xterm-addon-serialize@0.11.0/lib/xterm-addon-serialize.js" -o "$TMP/serialize.js"
# Search addon: the GPU/canvas renderers paint to a surface so the browser's
# native Find can't see terminal text; this scans the buffer instead (Cmd+F UI
# lives in term-client.js).
curl -fsSL "$base/xterm-addon-search@0.13.0/lib/xterm-addon-search.js"        -o "$TMP/search.js"
# Web-links addon: detects http(s) URLs as the program writes them and overlays
# a hover-underlined, clickable link region (a DOM layer, so it works under both
# the WebGL and canvas renderers). term-client.js opens clicks in a new tab.
curl -fsSL "$base/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.js"   -o "$TMP/weblinks.js"
# Renderers: xterm 5.x defaults to the DOM renderer (a <span> per cell), which is
# painfully slow with several live terminals — especially in Safari. We prefer
# the GPU (webgl) renderer for visible terminals and fall back to canvas.
curl -fsSL "$base/xterm-addon-canvas@0.5.0/lib/xterm-addon-canvas.js"        -o "$TMP/canvas.js"
curl -fsSL "$base/xterm-addon-webgl@0.16.0/lib/xterm-addon-webgl.js"          -o "$TMP/webgl.js"
# Inline-image addon: renders Sixel + the iTerm2 inline-image protocol (IIP).
# Decoded images live in the addon's own storage and re-composite on each
# render, so they survive our WebGL release/re-acquire. NOTE: the serialize
# addon + localStorage scrollback only capture text/SGR, so images do NOT
# come back after a tile reload/restore (they blank out) — live only.
curl -fsSL "$base/xterm-addon-image@0.5.0/lib/xterm-addon-image.js"           -o "$TMP/image.js"

# Fonts — all inlined as @font-face base64 blobs so the picker can switch
# instantly without any further network requests. Each file lives under fonts/
# and is SHARED with the dashboard (serve.py reads the same files), so a font
# bump stays a single step here. Sizes: jbm ~21k, terminus ~73k, cozette ~120k
# per weight (woff2). Total term.html is ~1.2 MB — fine over localhost.
FONTDIR="$DIR/fonts"; mkdir -p "$FONTDIR"
# JetBrains Mono is also fetched fresh on each build to keep this script
# offline-rerunnable; the other faces (terminus, cozette) are checked into
# fonts/ — terminus comes from `brew install --cask font-terminus`
# (TerminusTTF → woff2_compress), cozette from the upstream GitHub release.
fbase="$base/@fontsource/jetbrains-mono/files"
curl -fsSL "$fbase/jetbrains-mono-latin-400-normal.woff2" -o "$FONTDIR/jbm-400.woff2"
curl -fsSL "$fbase/jetbrains-mono-latin-700-normal.woff2" -o "$FONTDIR/jbm-700.woff2"

# Catalog of font faces inlined into term.html. Each entry is
# "<family>|<weight>|<file>". Keep in lockstep with FONTS in serve.py +
# term-client.js — the IDs are the contract.
FACES=(
  "JetBrains Mono|400|jbm-400.woff2"
  "JetBrains Mono|700|jbm-700.woff2"
  "Terminus|400|terminus-400.woff2"
  "Terminus|700|terminus-700.woff2"
  "Cozette|400|cozette-400.woff2"
  "Cozette|700|cozette-700.woff2"
  "Fira Code|400|fira-code-400.woff2"
  "Fira Code|700|fira-code-700.woff2"
  "Bitstream Charter|400|charter-400.woff2"
  "Bitstream Charter|700|charter-700.woff2"
  "Source Serif 4|400|source-serif-4-400.woff2"
  "Source Serif 4|700|source-serif-4-700.woff2"
  # Georgia: proprietary system font, not embedded — picker entry falls through
  # to the OS copy (serve.py FONTS).
)
FONT_FACE_CSS=""
for face in "${FACES[@]}"; do
  IFS='|' read -r family weight file <<<"$face"
  path="$FONTDIR/$file"
  if [ ! -r "$path" ]; then
    echo "build-term.sh: missing font file $file — see fonts/ README / serve.py FONTS" >&2
    exit 1
  fi
  b64="$(base64 < "$path" | tr -d '\n')"
  FONT_FACE_CSS+=$'@font-face{font-family:"'"$family"'";font-style:normal;font-weight:'"$weight"';font-display:swap;src:url(data:font/woff2;base64,'"$b64"') format("woff2");}\n'
done

# Guard against the only inline hazard: a literal </script> inside a lib.
if grep -l '</script' "$TMP"/*.js >/dev/null 2>&1; then
  echo "refusing to inline: a vendored lib contains </script>" >&2; exit 1
fi

OUT="$DIR/term.html"
{
  cat <<'HTML'
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude session</title>
<style>
HTML
  cat "$TMP/xterm.css"
  printf '%s' "$FONT_FACE_CSS"
  cat <<'HTML'
  html,body{margin:0;height:100%;background:#2b2b2b;overflow:hidden;}
  #term{position:absolute;inset:0;padding:4px 6px;}
  .xterm{height:100%;}
  /* Dark scrollbar for the terminal's scrollback viewport, plus
     overscroll-behavior:contain — without it, hitting the bottom on macOS
     triggers the OS rubber-band animation, which eats the first few pixels
     of any subsequent scroll-up (the "doesn't detach from bottom" feel).
     `contain` keeps the scroll local to the viewport and prevents both the
     bounce AND any parent-frame scroll-chaining. */
  .xterm-viewport{scrollbar-width:thin;scrollbar-color:#555 transparent;
    overscroll-behavior:contain;}
  .xterm-viewport::-webkit-scrollbar{width:8px;height:8px;}
  .xterm-viewport::-webkit-scrollbar-track{background:transparent;}
  .xterm-viewport::-webkit-scrollbar-thumb{background:#555;border-radius:6px;border:2px solid #2b2b2b;}
  .xterm-viewport::-webkit-scrollbar-thumb:hover{background:#6b6b6b;}
</style>
</head><body>
<div id="term"></div>
<script>
HTML
  cat "$TMP/xterm.js";     printf '\n</script>\n<script>\n'
  cat "$TMP/fit.js";       printf '\n</script>\n<script>\n'
  cat "$TMP/serialize.js"; printf '\n</script>\n<script>\n'
  cat "$TMP/search.js";    printf '\n</script>\n<script>\n'
  cat "$TMP/weblinks.js";  printf '\n</script>\n<script>\n'
  cat "$TMP/canvas.js";    printf '\n</script>\n<script>\n'
  cat "$TMP/webgl.js";     printf '\n</script>\n<script>\n'
  # createImageBitmap robustness shim — MUST precede the image addon so the addon
  # captures the wrapped global (xterm-addon-image falls back to <img> only when
  # createImageBitmap is ABSENT, not when it throws — e.g. headless Chromium).
  cat "$DIR/image-decode-shim.js"; printf '\n</script>\n<script>\n'
  cat "$TMP/image.js";     printf '\n</script>\n<script>\n'
  cat "$DIR/term-client.js"
  printf '\n</script>\n</body></html>\n'
} > "$OUT"
echo "built $OUT ($(wc -c <"$OUT") bytes)"
