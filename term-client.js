// Custom ttyd web client (served to ttyd via `-I term.html`).
//
// Why we own the client instead of using ttyd's built-in one: it lets the page
// (a) persist its own scrollback to localStorage and restore it across reloads,
// and (b) postMessage its live title / bell up to the dashboard. Both are
// impossible from the dashboard itself because each terminal is a cross-origin
// iframe. This page runs at the session's own origin, so it owns its buffer,
// its localStorage, and its title.
//
// It speaks ttyd's small WebSocket protocol directly (see connect()).
(function () {
  'use strict';
  // NOTE: the createImageBitmap robustness shim lives in image-decode-shim.js,
  // inlined by build-term.sh as its OWN <script> BEFORE xterm-addon-image, so the
  // addon's eval-time references resolve to the wrapped global. It can't live
  // here — term-client.js is inlined AFTER the addon, too late to be captured.
  var params = new URLSearchParams(location.search);
  // The dashboard's public base path (e.g. '/dash') when we're served through its
  // reverse proxy at <base>/t/<port>/, else '' (direct ttyd embed, or proxied at
  // the origin root). Used to prefix same-origin calls back to the dashboard.
  var _basePathMatch = location.pathname.match(/^(.*)\/t\/\d+(?:\/|$)/);
  var _basePath = _basePathMatch ? _basePathMatch[1] : '';
  // Stable per-session id, passed by the dashboard as ?sid=<registry id>. Falls
  // back to the port for standalone use. Used to key localStorage + postMessage.
  var sid = params.get('sid') || ('port-' + (location.port || '0'));
  // Key persistence by sid + the session's start time, so a *reload of the same
  // session* restores, but a new session that reuses the same port (different
  // start time) doesn't restore the previous one's stale scrollback.
  var ts = params.get('ts') || '';
  // Two namespaces:
  //  v3 (current) — gzipped via CompressionStream, ~10× smaller. Fits the 5 MB
  //  localStorage quota with ~30k lines comfortably.
  //  v2 (legacy)  — uncompressed; we still read it once if v3 is missing, so
  //  existing entries from before this build don't lose their history. The
  //  next persist tick writes v3, then sweeps the v2 key for THIS session.
  var LSPREFIX_V2 = 'claude-term-scrollback:v2:' + sid + '|';
  var LSPREFIX_V3 = 'claude-term-scrollback:v3:' + sid + '|';
  var LSKEY_V2 = LSPREFIX_V2 + ts;
  var LSKEY_V3 = LSPREFIX_V3 + ts;
  // Last known terminal size ("<cols>x<rows>"), persisted alongside the
  // scrollback snapshot. A hidden (0×0) tile connects WITHOUT ever being
  // fitted, and the ttyd handshake would carry xterm's 80×24 DEFAULT — the
  // dtach client then resizes the SHARED session pty to 80×24, and a busy
  // claude repaints every real-size view of the session into wrapped garbage
  // ("log buffer garbage" on each dashboard load with busy tiles in inactive
  // tabs). _applySavedSize() pre-sizes the hidden view to this value so its
  // attach is a no-op resize for the running program.
  var LSPREFIX_SIZE = 'claude-term-size:' + sid + '|';
  var LSSIZE = LSPREFIX_SIZE + ts;
  // Cross-session fallback size. The per-session LSSIZE is keyed on sid|ts, so it
  // is missing whenever a session has never been shown in THIS browser (a fresh
  // +New/dup/fork or CLI session that first appears in an INACTIVE tab), when its
  // ts drifted, or when it was only ever opened elsewhere. In all those the
  // hidden connect used to fall back to xterm's 80×24 default and shrink the
  // SHARED dtach pty — corrupting every wider live view (status line stair-
  // stepping into scrollback; see _applySavedSize). Every visible row tile uses
  // the SAME CSS-pinned width, so the last size ANY authoritative tile rendered
  // at is a far better default than 80×24: a hidden connect adopts it and its
  // attach is a (near-)no-op resize for the running program. Not per-session.
  var LSSIZE_DEFAULT = 'claude-term-size-default';
  var LSTITLE = 'claude-term-title:' + sid;   // last good title, so a reload shows the real name (not the workdir fallback)
  // Cap persisted/restored history. With gzip the 5 MB localStorage quota holds
  // ~30k lines of typical styled output, so 25k leaves headroom for outliers.
  // The in-memory `scrollback` (Terminal option, below) is bigger than this
  // intentionally — the buffer can grow during the session even if we only
  // persist a window of it.
  var MAX_LINES = 25000;
  // Quota retry: if setItem throws QuotaExceededError we re-serialize with
  // progressively smaller line counts until it fits. Avoids the alternative
  // of silently dropping the whole blob on a single overflow.
  var QUOTA_RETRY_LINES = [12500, 6000, 3000, 1500];

  // When embedded in the dashboard, it tells us (via postMessage) to stop
  // accepting keystrokes while this terminal is scrolled out of view / in a
  // hidden tab — without blurring it, so focus survives. Standalone stays true.
  var inputEnabled = true;

  // Test/automation diagnostics (same spirit as window.__claudeTerm below):
  // counters the browser regression tests read to assert otherwise-invisible
  // behaviour — that a re-show repaints via the visibility gate specifically
  // (gateHeals, isolated from every other heal path), that a resize BURST
  // coalesces into at most one PTY SIGWINCH (ptyResizes / fits), and the raw
  // repaint count (burstHeals). Harmless in production (localhost, same-origin).
  // reblits      — every post-reattach re-blit (the two timer passes + the
  //                restore-completion heal); the raw "did we repaint after attach"
  //                count.
  // reblitPasses — ONLY the two scheduled timer passes (700/1400 ms). Isolated so
  //                a test can assert BOTH fire (==2): the gray-on-reload latch bug
  //                suppressed the second pass (==1), and the restore-completion
  //                heal would otherwise mask that in the combined `reblits`.
  // renderer / condFrozen: live state for tests + debugging. renderer is the
  // active text renderer ('webgl' | 'canvas'); condFrozen is true while a parked
  // card is showing its static snapshot overlay (and therefore holds no GL context).
  // glRecover — times we detected a SILENTLY-lost WebGL context (evicted under the
  //   per-page cap with no webglcontextlost event) and demoted to canvas to cure
  //   the resulting blank tile (see _glLost / _recoverLostGl).
  var _diag = { burstHeals: 0, ptyResizes: 0, fits: 0, gateHeals: 0, gateSwaps: 0, reblits: 0, reblitPasses: 0, renderer: null, condFrozen: false, condensed: false, condRefreshes: 0, glRecover: 0 };
  try { window.__tileDiag = _diag; } catch (e) {}

  // The dashboard sets this while this tile is condensed (parked as a card). A
  // parked card is a thin spine with nothing useful to vertical-scroll, so we
  // repurpose a vertical wheel over it into a horizontal scroll of the tile row
  // (forwarded as wheel-x) — letting the user fan through the stacked deck.
  var condensed = false;

  var enc = new TextEncoder();
  var dec = new TextDecoder();
  // Streaming decoder dedicated to the cmd '0' OUTPUT stream (the prompt-bell
  // scan). ttyd splits frames at arbitrary byte offsets, so a multi-byte glyph
  // — notably claude's selection-menu arrow `❯` (U+276F, 3 bytes) — can straddle
  // a frame boundary. A stateless decode would emit U+FFFD for the split halves
  // and the `❯ N.` bell pattern would never match. `{stream:true}` holds the
  // partial bytes until the next frame completes the codepoint. Kept SEPARATE
  // from `dec` because title ('1') / prefs ('2') are independent logical streams
  // — sharing one streaming decoder would splice their bytes together. Reset on
  // each (re)connect so a partial byte left dangling at a disconnect can't
  // corrupt the first decode of the fresh stream.
  var outDec = new TextDecoder();

  // Font picker: the dashboard pushes the selected entry via postMessage
  // (cmd:'font'). We mirror it into localStorage so a tile reload picks up the
  // current choice on cold-boot (before the dashboard has had a chance to push
  // it) — otherwise every reload would briefly flash in JBM. Standalone (no
  // dashboard) just uses the stored value, or the hard-coded default below.
  var LSFONT = 'claude-term-font';
  var SYS_MONO_CHAIN = ", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
  function _readStoredFont() {
    try {
      var raw = localStorage.getItem(LSFONT); if (!raw) return null;
      var f = JSON.parse(raw);
      if (f && typeof f.family === 'string' && f.size > 0) return f;
    } catch (e) {}
    return null;
  }
  var _bootFont = _readStoredFont() || { family: 'JetBrains Mono', size: 13, weight: 'normal' };

  // Light/dark theme. The dashboard pushes the current mode via postMessage
  // (cmd:'theme'); mirror it into localStorage so a tile reload boots in the
  // right colors (no dark flash before the dashboard pushes). The tile is a
  // separate origin (its ttyd port) from the dashboard, so it can't read the
  // dashboard's localStorage — it keeps its own copy here.
  var LSTHEME = 'claude-term-theme';
  function _readStoredTheme() {
    try { return localStorage.getItem(LSTHEME) === 'light' ? 'light' : 'dark'; } catch (e) { return 'dark'; }
  }
  var _bootTheme = _readStoredTheme();
  // xterm theme objects. Dark = just a bg override (xterm fills the rest with its
  // dark-appropriate defaults). Light = a full GitHub-light-ish ANSI palette so
  // app colors stay legible on a pale background. Setting term.options.theme
  // REPLACES the theme, so toggling back to dark cleanly restores the defaults.
  function _xtermTheme(mode) {
    if (mode === 'light') return {
      background: '#fbfbfb', foreground: '#24292e', cursor: '#24292e', cursorAccent: '#fbfbfb',
      selectionBackground: '#bcd7fb',
      black: '#24292e', red: '#cf222e', green: '#116329', yellow: '#7d4e00',
      blue: '#0969da', magenta: '#8250df', cyan: '#1b7c83', white: '#6e7781',
      brightBlack: '#57606a', brightRed: '#a40e26', brightGreen: '#1a7f37', brightYellow: '#633c01',
      brightBlue: '#218bff', brightMagenta: '#a475f9', brightCyan: '#3192aa', brightWhite: '#8c959f'
    };
    return { background: '#2b2b2b' };   // dark → ttyd's default gray + xterm defaults
  }
  function _pageBg(mode) { return mode === 'light' ? '#fbfbfb' : '#2b2b2b'; }
  function _applyPageBg(mode) {
    var c = _pageBg(mode);
    try { document.documentElement.style.background = c; document.body.style.background = c; } catch (e) {}
  }
  _applyPageBg(_bootTheme);   // paint the page chrome before xterm mounts → minimal flash

  var term = new Terminal({
    cursorBlink: true,
    fontSize: _bootFont.size,
    fontWeight: _bootFont.weight || 'normal',
    fontFamily: "'" + _bootFont.family + "'" + SYS_MONO_CHAIN,
    lineHeight: (typeof _bootFont.lineHeight === 'number' && _bootFont.lineHeight > 0) ? _bootFont.lineHeight : 1.0,
    // 10k lines of in-memory scrollback. Rendering perf only sees the viewport
    // so this doesn't slow frames; the cost is roughly proportional memory
    // per tile. Search (Cmd+F) walks the whole buffer but stays well under
    // 50 ms at this size. We persist a window of it (MAX_LINES, gzipped) to
    // localStorage so reloads keep history.
    scrollback: 10000,
    allowProposedApi: true,
    theme: _xtermTheme(_bootTheme)   // dark (ttyd gray) or the pushed light palette
  });
  // Apply a pushed light/dark mode: swap the xterm palette + page background and
  // remember it for the next cold boot. A theme change recolors glyphs, so the
  // GPU atlas must be rebuilt — _burstHeal (clearTextureAtlas + repaint) does it.
  function applyTermTheme(mode) {
    mode = mode === 'light' ? 'light' : 'dark';
    try { localStorage.setItem(LSTHEME, mode); } catch (e) {}
    try { term.options.theme = _xtermTheme(mode); } catch (e) {}
    _applyPageBg(mode);
    try { _burstHeal(); } catch (e) {}
  }
  var fit = new FitAddon.FitAddon();
  var serializer = new SerializeAddon.SerializeAddon();
  var search = new SearchAddon.SearchAddon();
  term.loadAddon(fit);
  term.loadAddon(serializer);
  term.loadAddon(search);
  term.open(document.getElementById('term'));

  // Linkify URLs in terminal output. The web-links addon detects http(s) URLs
  // as the program writes them and overlays a hover-underlined, clickable link
  // region — a DOM layer, so it's renderer-independent (works under both WebGL
  // and canvas, survives the renderer swaps). We pass our own activate handler
  // so a click (a) is hard-gated to http/https — defence-in-depth, though the
  // addon's own matcher already only spots those — and (b) opens with
  // noopener,noreferrer so the new tab can't reach back into this localhost
  // page via window.opener. Guarded: a vendored-addon failure must not take the
  // terminal down. Each tile is a cross-origin iframe with no sandbox attr, so
  // window.open lands a real top-level tab.
  try {
    term.loadAddon(new WebLinksAddon.WebLinksAddon(function (event, uri) {
      if (!/^https?:\/\//i.test(uri)) return;
      try { window.open(uri, '_blank', 'noopener,noreferrer'); } catch (e) {}
    }));
  } catch (e) {}

  // Connection-status banner. Reconnect/disconnect notices used to be written
  // straight into the xterm scrollback as styled text. For a
  // normal-buffer TUI like claude (Ink), the \r\n around the notice pushes the
  // line up into scrollback where the SerializeAddon captures it — so every
  // network blip permanently embedded a "[reconnecting…]" line in the restored
  // history, accumulating on each reload. We render to a DOM overlay instead so
  // status NEVER touches the buffer and can't leak into the persisted snapshot.
  var _statusEl = null;
  function _setStatus(msg, kind) {
    if (!_statusEl) {
      _statusEl = document.createElement('div');
      _statusEl.style.cssText = 'position:fixed;top:6px;right:8px;z-index:9999;'
        + 'font:12px/1.4 ui-monospace,Menlo,monospace;padding:2px 8px;border-radius:4px;'
        + 'pointer-events:none;opacity:0.92;transition:opacity .15s;';
      (document.body || document.documentElement).appendChild(_statusEl);
    }
    if (!msg) { _statusEl.style.display = 'none'; return; }
    _statusEl.textContent = msg;
    _statusEl.style.color = kind === 'error' ? '#ff6b6b' : '#cfcfcf';
    _statusEl.style.background = kind === 'error' ? 'rgba(60,20,20,0.85)' : 'rgba(40,40,40,0.85)';
    _statusEl.style.display = 'block';
  }

  // Inline images: Sixel + the iTerm2 inline-image protocol (IIP). The addon
  // keeps decoded images in its OWN storage and overlays them on a separate
  // canvas (decoupled from the text renderer), so they survive the WebGL
  // release/re-acquire swap. storageLimit caps decoded-image memory PER tile
  // (MB) so many open tiles can't blow up the page. Loaded once — not part of
  // the renderer swap. Caveat: serialize + localStorage scrollback only capture
  // text/SGR, so images do NOT restore after a tile reload (they blank out).
  try {
    term.loadAddon(new ImageAddon.ImageAddon({
      sixelSupport: true, iipSupport: true, storageLimit: 50
    }));
  } catch (e) {}

  // --- inline-image PERSISTENCE (IIP only) -------------------------------
  // The image addon renders live, but its storage isn't serialized, so images
  // vanish on reload (text scrollback restores; images don't). We persist IIP
  // images ourselves: capture each one + a buffer Marker that tracks its row
  // across scroll/trim, transcode to lossy WebP, stash the bytes in IndexedDB
  // (localStorage is too small + string-only), and on restore splice an
  // IIP(WebP) escape back into the serialized blob at the image's line so it
  // re-renders in place. The serialize addon delimits hard lines with "\r\n"
  // (wrapped rows are concatenated), and an IIP image always sits at column 0
  // of a fresh row, so its anchor is a hard-line start — splice = insert at the
  // matching split index. ALL fail-safe: any error here leaves normal text
  // scrollback untouched. Sixel is NOT persisted yet (needs rasterizing the
  // addon's decoded canvas — a follow-up).
  var IMG_DB = 'claude-term-images', IMG_STORE = 'img';
  var IMG_META_KEY = 'claude-term-images-meta:' + sid + '|' + ts;
  // Sibling keys for the OUT-OF-BAND image path (setupTileImagePoll): its images
  // are row-anchored DECORATIONS, not spliced IIP text, so they persist as their
  // own metadata list + IndexedDB bytes and are re-created (not re-spliced) on
  // restore. The poll cursor is persisted too so a reload doesn't re-fetch and
  // double-render images the restore already brought back.
  var IMG_OOB_META_KEY = 'claude-term-images-oob:' + sid + '|' + ts;
  var IMG_OOB_CURSOR_KEY = 'claude-term-images-oobcur:' + sid + '|' + ts;
  var _imgKeyPrefix = sid + '|' + ts + '|';
  var _oobKeyPrefix = sid + '|' + ts + '|oob|';   // distinct IDB namespace from stream-IIP
  var _imgSeq = 0;
  var _imgMarks = [];   // { seq, marker, args } for live (untrimmed) images this run
  var _oobSeq = 0;
  var _oobMarks = [];   // { seq, marker, dw, dh, rows } for live out-of-band images this run
  function _idb() {
    return new Promise(function (res, rej) {
      var r = indexedDB.open(IMG_DB, 1);
      r.onupgradeneeded = function () { r.result.createObjectStore(IMG_STORE); };
      r.onsuccess = function () { res(r.result); };
      r.onerror = function () { rej(r.error); };
    });
  }
  function _idbDo(mode, fn) {
    return _idb().then(function (db) {
      return new Promise(function (res, rej) {
        var tx = db.transaction(IMG_STORE, mode), out;
        var os = tx.objectStore(IMG_STORE);
        out = fn(os);
        tx.oncomplete = function () { db.close(); res(out && out.result !== undefined ? out.result : null); };
        tx.onerror = function () { db.close(); rej(tx.error); };
      });
    });
  }
  function _idbPut(key, val) { return _idbDo('readwrite', function (os) { return os.put(val, key); }); }
  function _idbGet(key) { return _idbDo('readonly', function (os) { return os.get(key); }); }
  function _idbDel(key) { return _idbDo('readwrite', function (os) { return os.delete(key); }).catch(function () {}); }
  // Decoded bytes → lossy WebP base64 (falls back to PNG if the browser can't
  // encode WebP — e.g. older Safari — so the image still persists, just bigger).
  function _toWebpB64(srcB64) {
    return new Promise(function (res) {
      try {
        var bin = atob(srcB64), n = bin.length, bytes = new Uint8Array(n);
        for (var i = 0; i < n; i++) bytes[i] = bin.charCodeAt(i);
        var url = URL.createObjectURL(new Blob([bytes]));   // type sniffed by <img>
        var img = new Image();
        img.onload = function () {
          try {
            var c = document.createElement('canvas');
            c.width = img.naturalWidth || 1; c.height = img.naturalHeight || 1;
            c.getContext('2d').drawImage(img, 0, 0);
            URL.revokeObjectURL(url);
            var d = c.toDataURL('image/webp', 0.85);
            if (d.indexOf('image/webp') < 0) d = c.toDataURL('image/png');
            res(d.slice(d.indexOf(',') + 1) || null);
          } catch (e2) { URL.revokeObjectURL(url); res(null); }
        };
        img.onerror = function () { URL.revokeObjectURL(url); res(null); };
        img.src = url;
      } catch (e) { res(null); }
    });
  }
  function _cleanIipArgs(args) {
    // Drop size= — the byte length changes after the WebP re-encode.
    return args.replace(/;?\s*size=\d+/i, '');
  }
  // Finalize one complete IIP payload "File=<args>:<base64>" anchored at an
  // absolute buffer row: transcode to WebP, stash in IndexedDB, and register a
  // Marker (relative to the current cursor) that tracks the row across
  // scroll/trim and evicts the bytes when the row is trimmed out of scrollback.
  function _captureIIP(data, anchorRow) {
    try {
      var ci = data.indexOf(':');
      if (ci < 0) return;
      var args = _cleanIipArgs(data.slice(0, ci)), b64 = data.slice(ci + 1);
      if (!b64) return;
      var seq = _imgSeq++, key = _imgKeyPrefix + seq;
      var marker = null;
      try {
        var b = term.buffer.active;
        marker = term.registerMarker(anchorRow - (b.baseY + b.cursorY));
      } catch (e) {}
      if (marker) {
        _imgMarks.push({ seq: seq, marker: marker, args: args });
        marker.onDispose(function () {
          for (var i = 0; i < _imgMarks.length; i++) if (_imgMarks[i].seq === seq) { _imgMarks.splice(i, 1); break; }
          _idbDel(key);
        });
      }
      _toWebpB64(b64).then(function (webp) {
        if (webp) return _idbPut(key, { args: args, webp: webp });
      }).catch(function () {});
    } catch (e) {}
  }
  // We OBSERVE the output byte stream for IIP sequences rather than registering
  // an OSC handler: the image addon registers a *streaming* OscHandler that
  // needs its .put() chunks during parsing, so a co-registered function handler
  // starves it (the image then never renders). Instead we scan a latin1 view of
  // each chunk (bytes map 1:1; base64 + escapes are ASCII), accumulate across
  // chunks until the terminator, and pass the original bytes through untouched
  // so the addon still renders live. The anchor is the cursor row at the moment
  // the sequence starts (captured pre-write in _writeAndScan) — exact for an
  // image emitted on its own line (the norm), approximate if text precedes it in
  // the same write.
  var _latin1 = (function () { try { return new TextDecoder('latin1'); } catch (e) { return null; } })();
  var IIP_START = '\x1b]1337;';
  var IIP_MAX = 8 * 1024 * 1024;   // cap accumulation so a stray ESC]1337 can't grow unbounded
  var _iip = null;                 // { buf, anchor } while accumulating across chunks
  function _scanForIIP(payload, anchorRow) {
    if (!_latin1) return;
    try {
      var s = _latin1.decode(payload), i = 0;
      while (i < s.length) {
        if (!_iip) {
          var mi = s.indexOf(IIP_START, i);
          if (mi < 0) break;
          _iip = { buf: '', anchor: anchorRow };
          i = mi + IIP_START.length;
        } else {
          var bel = s.indexOf('\x07', i), st = s.indexOf('\x1b\\', i), end = -1, tlen = 1;
          if (bel >= 0 && (st < 0 || bel < st)) { end = bel; tlen = 1; }
          else if (st >= 0) { end = st; tlen = 2; }
          if (end < 0) { _iip.buf += s.slice(i); if (_iip.buf.length > IIP_MAX) _iip = null; break; }
          _iip.buf += s.slice(i, end);
          var done = _iip; _iip = null;
          _captureIIP(done.buf, done.anchor);
          i = end + tlen;
        }
      }
    } catch (e) { _iip = null; }
  }
  // Hard-line index (\r\n-delimited) of an absolute buffer row, counting from
  // row 0 — valid because persist() serializes the whole buffer (scrollback
  // 25000 >= the 10000-line in-memory cap). Under localStorage-quota truncation
  // (a smaller window) this can drift; out-of-range images are skipped on
  // restore rather than misplaced.
  function _imgHardLineIndex(absRow) {
    var b = term.buffer.active, idx = -1, last = Math.min(absRow, b.length - 1);
    for (var i = 0; i <= last; i++) { var ln = b.getLine(i); if (!ln || !ln.isWrapped) idx++; }
    return idx;
  }
  // Inverse of _imgHardLineIndex: the absolute buffer row whose hard-line index
  // (\r\n-delimited, counting from row 0) equals `hardIdx`. Used to re-anchor an
  // out-of-band decoration after restore. Returns -1 if past the buffer end.
  function _absRowForHardLine(hardIdx) {
    var b = term.buffer.active, idx = -1;
    for (var i = 0; i < b.length; i++) {
      var ln = b.getLine(i);
      if (!ln || !ln.isWrapped) idx++;
      if (idx === hardIdx) return i;
    }
    return -1;
  }
  function _persistImageMeta() {
    try {
      var meta = [];
      for (var i = 0; i < _imgMarks.length; i++) {
        var m = _imgMarks[i];
        if (!m.marker || m.marker.line < 0) continue;
        meta.push({ seq: m.seq, line: _imgHardLineIndex(m.marker.line) });
      }
      if (meta.length) localStorage.setItem(IMG_META_KEY, JSON.stringify(meta));
      else localStorage.removeItem(IMG_META_KEY);
    } catch (e) {}
  }
  // Splice persisted IIP(WebP) images back into a restored scrollback blob.
  function _spliceImagesInto(saved) {
    var meta;
    try { meta = JSON.parse(localStorage.getItem(IMG_META_KEY) || 'null'); } catch (e) { meta = null; }
    if (!meta || !meta.length) return Promise.resolve(saved);
    return Promise.all(meta.map(function (m) {
      return _idbGet(_imgKeyPrefix + m.seq).then(function (rec) {
        return rec && rec.webp ? { line: m.line, args: rec.args || 'File=inline=1', webp: rec.webp } : null;
      }).catch(function () { return null; });
    })).then(function (imgs) {
      imgs = imgs.filter(Boolean);
      if (!imgs.length) return saved;
      var lines = saved.split('\r\n');
      imgs.sort(function (a, b) { return b.line - a.line; });   // high→low so earlier inserts don't shift later ones
      imgs.forEach(function (im) {
        if (im.line < 0 || im.line >= lines.length) return;     // out of restored window → skip
        lines[im.line] = '\x1b]1337;' + im.args + ':' + im.webp + '\x07' + lines[im.line];
      });
      return lines.join('\r\n');
    }, function () { return saved; });
  }
  // --- out-of-band image persistence (sibling of the stream-IIP layer above) ---
  // The out-of-band path renders each image as a row-anchored Decoration over
  // reserved blank lines (see setupTileImagePoll). Those blank lines are part of
  // the serialized scrollback, so they restore on reload; here we persist each
  // image's bytes (WebP in IndexedDB) + its anchor (hard-line index) + display
  // dims, and on restore re-CREATE the decoration at the same line so the image
  // reappears inline. _oobRestore is wired by setupTileImagePoll (it owns the
  // decoration-creation + copy-button machinery). ALL fail-safe: any error leaves
  // the text scrollback untouched.
  var _oobRestore = null;   // set by setupTileImagePoll: function() -> Promise
  function _persistOobMeta() {
    try {
      var b = term.buffer.active, bySeq = {};
      for (var i = 0; i < _oobMarks.length; i++) {
        var m = _oobMarks[i];
        // Anchor: prefer the LIVE marker's current hard-line index while it's still
        // on a real, growing buffer (shell/TUI tiles keep scrollback, so the marker
        // tracks accurately). When the marker has drifted out of range — which on a
        // claude tile happens almost immediately, Ink scrolling the reserved rows
        // off the tiny non-growing buffer so marker.line goes NEGATIVE — fall back
        // to the values CAPTURED AT PLACEMENT (line0 + distEnd). We do NOT skip a
        // negative marker any more: skipping was the bug (empty meta → no restore).
        var live = !!(m.marker && m.marker.line >= 0);
        var line = live ? _imgHardLineIndex(m.marker.line) : (typeof m.line0 === 'number' ? m.line0 : -1);
        var distEnd = live ? (b.length - m.marker.line) : (typeof m.distEnd === 'number' ? m.distEnd : 1);
        var entry = { seq: m.seq, line: line, distEnd: distEnd, dw: m.dw, dh: m.dh, rows: m.rows, _live: live };
        // De-dup by seq: a restored mark and a same-seq live mark can briefly
        // co-exist; keep the live one (accurate anchor) so meta never grows
        // unbounded with duplicates across reloads.
        var prev = bySeq[m.seq];
        if (!prev || (entry._live && !prev._live)) bySeq[m.seq] = entry;
      }
      var meta = [];
      Object.keys(bySeq).forEach(function (k) { var e = bySeq[k]; delete e._live; meta.push(e); });
      meta.sort(function (a, b2) { return a.seq - b2.seq; });
      if (meta.length) localStorage.setItem(IMG_OOB_META_KEY, JSON.stringify(meta));
      else localStorage.removeItem(IMG_OOB_META_KEY);
    } catch (e) {}
  }

  // --- renderer: prefer the GPU (WebGL) renderer for VISIBLE terminals ---
  // A dashboard opens one terminal per tile and browsers cap WebGL contexts per
  // page (~16, fewer on Safari). So when the dashboard tells us this terminal is
  // off-screen / in a hidden tab we DISPOSE the WebGL context (drop to the canvas
  // renderer) and re-acquire it when shown — staying under the cap no matter how
  // many sessions are open. Also fall back to canvas on a context-loss event or
  // if WebGL can't initialise (and to xterm's DOM renderer if even canvas fails).
  var webglAddon = null, canvasAddon = null;
  // After a renderer swap the freshly-loaded addon owns the canvas, but
  // viewport CELLS written *before* the swap (e.g. while we were on the old
  // renderer, or while no renderer was attached during the gap) are NOT
  // automatically repainted — the addon paints what it sees written, not
  // what's sitting in the buffer. Symptom: a tile goes blank while claude
  // is working.
  //
  // We must re-blit WITHOUT changing cols/rows. An earlier version forced a
  // reflow with `resize(c+1,r); resize(c,r)`, but `term.resize` fires onResize →
  // a real PTY SIGWINCH — and at TWO widths (c+1 then c). A TUI like claude
  // (Ink) then re-renders its frame twice at two different widths; if its
  // frame-erase miscounts, you get the SAME message duplicated with mismatched
  // wrapping. Renderer swaps happen on every show/hide and on the ↻ reload, so
  // this duplicated on each — the user-reported "dupe scroll lines". Instead we
  // rebuild the renderer's glyph cache (`clearTextureAtlas`) and repaint visible
  // rows (`refresh`) — both client-only, no SIGWINCH, so the TUI never
  // re-renders. A genuine size change still gets corrected, but via the SHARED
  // debounced fit (_scheduleFit), NOT a direct fit.fit() here: _burstHeal runs
  // this heal THREE times (0/80/220 ms) and a direct fit on each pass is three
  // fit.fit() calls across 220 ms — if the layout is still settling (a flicker /
  // renderer swap relayout), they compute three different column counts and fire
  // three SIGWINCHes at three widths, so claude (Ink) re-renders three frames
  // woven together: the char-by-char "double content / garbled width" the user
  // hit while idle. Routing through _scheduleFit coalesces every fit request
  // (heals AND window resizes) into ONE SIGWINCH per settle. The explicit
  // Cmd+Shift+E refresh keeps its own resize-trick — user-triggered recovery
  // where a redraw is expected.
  function _healAfterRendererSwap() {
    requestAnimationFrame(function () {
      _scheduleFit();                                          // genuine size change → ONE debounced SIGWINCH (never 3)
      // Recompute the renderer's canvas/framebuffer dimensions from a fresh cell
      // measurement and re-blit EVERY row from the buffer. This is the render
      // half of the Cmd+Shift+E resize-trick (term.resize +1/-1) — the part that
      // cures the "ghost / content duplicated from the bottom up" a freshly
      // attached or just-un-hidden GPU renderer paints — but driven straight
      // through the render service so it does NOT fire term.onResize: no PTY
      // SIGWINCH, so a TUI (claude/Ink) never re-renders and can't duplicate its
      // frame (the dup b30d3cb removed the resize-trick to fix). clearTextureAtlas
      // + refresh alone don't recompute dimensions, so a renderer whose cached
      // dims went stale while detached from layout keeps ghosting without this.
      // Private-but-stable on the pinned xterm build; each call defensively guarded.
      try {
        var core = term._core, rs = core && core._renderService;
        if (rs) {
          try { core._charSizeService.measure(); } catch (e) {}      // re-measure cell (valid again once laid out)
          try { rs.handleCharSizeChanged(); } catch (e) {}           // propagate cell size into render dimensions
          try { rs.handleResize(term.cols, term.rows); } catch (e) {} // recompute canvas/framebuffer dims (no onResize)
        }
      } catch (e) {}
      try { term.clearTextureAtlas(); } catch (e) {}           // rebuild glyphs on the fresh GPU/canvas context
      try { term.refresh(0, (term.rows || 1) - 1); } catch (e) {}
    });
  }
  // Run the heal SEVERAL times across the next few frames, not just once. A
  // freshly created or browser-restored WebGL context warms its glyph atlas
  // over a handful of frames; a single heal can land before the GPU is ready
  // and leave the gray/partial tile a manual ↻ refresh otherwise cures. Doing
  // the repaint again as the context settles is "the reload, more often" —
  // cheap because every pass is client-only (no term.resize → no PTY SIGWINCH),
  // so a TUI (claude/Ink) never re-renders and can't duplicate its frame. The
  // backstop timings straddle the flicker mask's ~250 ms lifetime so the last
  // repaint lands right as the mask lifts.
  function _burstHeal() {
    _diag.burstHeals++;
    _healAfterRendererSwap();
    setTimeout(_healAfterRendererSwap, 80);
    setTimeout(_healAfterRendererSwap, 220);
  }
  // Renderer-swap flicker mask. A freshly-attached WebGL renderer paints its
  // first frame(s) before its glyph texture atlas is populated, so the tile
  // briefly flashes blank/partial until _healAfterRendererSwap repaints — the
  // "warmup flicker" you see when a GL context is restored (tile shown again,
  // or all contexts reclaimed on a tab switch). We hide it by freezing the last
  // good frame as an overlay during the swap. The outgoing renderer here is the
  // 2D canvas, whose pixels drawImage() can read reliably (a WebGL canvas can't
  // without preserveDrawingBuffer), which is exactly the visible promote path.
  var _swapMask = null;
  function _clearSwapMask() { if (_swapMask) { try { _swapMask.remove(); } catch (e) {} _swapMask = null; } }
  // Build a static snapshot of the current frame as an absolutely-positioned
  // overlay on .xterm-screen and return it (or null). Shared by the renderer-swap
  // flicker mask (_freezeFrame) and the condensed-tile freeze (_freezeForCondense).
  // Reads ONLY the 2D canvas — a WebGL canvas drawImage()s blank without
  // preserveDrawingBuffer — so the caller must be on the canvas renderer when it
  // snapshots, not WebGL.
  function _makeFrozenOverlay(z) {
    try {
      var screen = term.element && term.element.querySelector('.xterm-screen');
      if (!screen) return null;
      var cs = screen.querySelectorAll('canvas');
      var w = screen.clientWidth, h = screen.clientHeight;
      if (!cs.length || !w || !h) return null;
      var dpr = window.devicePixelRatio || 1;
      var snap = document.createElement('canvas');
      snap.width = Math.round(w * dpr); snap.height = Math.round(h * dpr);
      var ctx = snap.getContext('2d');
      if (!ctx) return null;
      var drew = false;
      cs.forEach(function (c) {
        // Skip our OWN snapshot overlays (they live in .xterm-screen too): a
        // re-snapshot must capture the LIVE renderer canvas, not a stale frozen
        // frame stacked on top — otherwise the periodic refresh would copy the
        // old image forward forever.
        if (c.dataset && c.dataset.frozenOverlay) return;
        if (c.width && c.height) { try { ctx.drawImage(c, 0, 0, snap.width, snap.height); drew = true; } catch (e) {} }
      });
      if (!drew) return null;
      snap.dataset.frozenOverlay = '1';
      snap.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:' + (z || 30) + ';';
      screen.appendChild(snap);
      return snap;
    } catch (e) { return null; }
  }
  function _freezeFrame() {
    _clearSwapMask();   // never stack masks
    _swapMask = _makeFrozenOverlay(30);
  }
  // Condensed-tile freeze overlay. Independent of the swap mask (its own z so the
  // two never clobber each other): a parked card is shown as this frozen image
  // while its terminal holds NO WebGL context. See _freezeForCondense /
  // setCondensedState below.
  var _condMask = null;
  function _clearCondMask() { if (_condMask) { try { _condMask.remove(); } catch (e) {} _condMask = null; } _diag.condFrozen = false; }
  function _thawFrame() {
    if (!_swapMask) return;
    // Drop the overlay only after the new renderer has had a few frames to warm
    // its atlas and repaint (the heal runs on the next frame). A timer backstop
    // guarantees removal even if rAF is starved, so the mask can never stick.
    var done = false, remove = function () { if (done) return; done = true; _clearSwapMask(); };
    requestAnimationFrame(function () { requestAnimationFrame(function () { requestAnimationFrame(remove); }); });
    setTimeout(remove, 250);
  }
  function useCanvas() {
    if (webglAddon) { try { webglAddon.dispose(); } catch (e) {} webglAddon = null; }
    if (canvasAddon) return;
    try {
      canvasAddon = new CanvasAddon.CanvasAddon(); term.loadAddon(canvasAddon);
      _diag.renderer = 'canvas';
      _burstHeal();
    } catch (e) { canvasAddon = null; }
  }
  function useWebgl() {
    if (webglAddon) return;
    // Snapshot the live 2D-canvas frame BEFORE disposing it, so the overlay
    // shows real content (after dispose the renderer falls back to DOM and the
    // canvas pixels are gone). No-op on first load when there's no canvas yet.
    if (canvasAddon) { _freezeFrame(); try { canvasAddon.dispose(); } catch (e) {} canvasAddon = null; }
    try {
      var w = new WebglAddon.WebglAddon();
      w.onContextLoss(function () {
        // Test seam: model the SILENT eviction (browser drops the context WITHOUT
        // a usable loss event) by skipping the event-driven demote, leaving the
        // dead-but-valid-looking addon that only the isContextLost() poll cures.
        try { if (window.__forceSilentGlLoss) return; } catch (e) {}
        try { w.dispose(); } catch (e) {} webglAddon = null; useCanvas();
      });
      term.loadAddon(w); webglAddon = w;
      _diag.renderer = 'webgl';
      _burstHeal();
      _thawFrame();
    } catch (e) { webglAddon = null; _clearSwapMask(); useCanvas(); }
  }
  // Asymmetric debounce: ensure WebGL promptly when shown (so a tile coming into
  // view doesn't sit on canvas), but HOLD the GL context for a while after going
  // hidden — a tile scrolling briefly off-screen keeps its context, so it doesn't
  // re-init WebGL (the visible "warmup flicker") when it comes back. Only
  // sustained hidden time (~12 s) actually releases the context.
  var rendererTimer = null;
  // Did the tile go hidden since it was last shown? A re-show then has to repaint:
  // while hidden its iframe is display:none (0×0 layout), so the renderer's cached
  // canvas/framebuffer dims go stale and the first frame after re-show is gray /
  // partial until something re-blits. Distinguishes a real hide→show from the
  // boot gate (never hidden → no needless heal).
  // Start TRUE: the iframe begins hidden (display:none) in the dashboard, so the
  // first visibility message must heal. On standalone (no dashboard), this is a
  // harmless no-op (tile is already visible on boot).
  var _hiddenSinceShown = true;
  function setRendererVisible(visible) {
    // A condensed (parked-card) tile is shown as a frozen snapshot and must NEVER
    // hold a WebGL context — not even when scrolled into view (the dashboard's
    // IntersectionObserver still sends enabled:true for the clipped spine). Force
    // the hidden branch so the parked deck can't trigger N GL recreations as its
    // cards scroll into the viewport (the parked-deck scroll lag). Un-condensing
    // (setCondensedState) re-promotes via setRendererVisible(inputEnabled).
    if (condensed) visible = false;
    if (rendererTimer) { clearTimeout(rendererTimer); rendererTimer = null; }
    if (visible) {
      // Re-shown WITHIN the 12 s GL-hold window: the WebGL context is still loaded,
      // so the useWebgl() swap never runs on its own. The tile must still be
      // repaired — while display:none its renderer's framebuffer/atlas went stale
      // (the iframe 'resize' listener can't catch the 0→visible transition: a
      // display:none iframe does NOT reliably fire resize on its contentWindow).
      // Heal explicitly off the dashboard's visibility gate. We RECREATE the
      // renderer here (not just a render-only _burstHeal): see below — a stale GL
      // context ghosts, and only a dispose+recreate clears it.
      if (webglAddon) {
        if (_hiddenSinceShown) {
          _hiddenSinceShown = false; _diag.gateHeals++;
          // A render-only _burstHeal (re-measure + clearTextureAtlas + refresh) is
          // NOT enough when the tile's GL context went stale while hidden. With
          // many tiles, Chrome reclaims hidden tiles' WebGL contexts under the
          // per-page cap (and can silently reset/restore one) WITHOUT firing the
          // contextlost we demote on — so webglAddon still looks valid but its
          // framebuffer/atlas are stale, and refresh composites a GHOST: the buffer
          // is intact (no marker dup) yet the canvas shows content duplicated at an
          // offset (overlapping glyphs / doubled status line). clearTextureAtlas +
          // refresh can't reset that; only a full renderer RECREATE does — which is
          // exactly why the manual ↻ / Cmd+Shift+E (cmd:'refresh' →
          // useCanvas();useWebgl()) clears it every time. So escalate the gate heal
          // to that same recreate. It's self-masked (useWebgl freezes the last good
          // 2D-canvas frame during GL warmup), fires only on the hide→show
          // transition (not per frame), and no term.resize → no PTY SIGWINCH → a TUI
          // never re-renders, so the recreate can't itself duplicate the frame.
          _diag.gateSwaps++;
          useCanvas(); useWebgl();
        }
        return;
      }
      _hiddenSinceShown = false;
      rendererTimer = setTimeout(function () { rendererTimer = null; useWebgl(); }, 120);
    } else {
      _hiddenSinceShown = true;
      if (!webglAddon) return;  // already on canvas — nothing to release
      rendererTimer = setTimeout(function () { rendererTimer = null; useCanvas(); }, 12000);
    }
  }
  // Park this tile as a STATIC IMAGE and drop its WebGL context. Why a freeze and
  // not just "demote to canvas": the data stream keeps flowing while parked
  // (scrollback + bell detection live in the write-queue parse, NOT the renderer —
  // see scanBel), so a live canvas would keep repainting a clipped sliver nobody
  // reads. Instead we snapshot once and let the user see that frozen frame until
  // the card rings (the dashboard un-condenses it) or is expanded.
  //
  // Snapshot SOURCE must be the 2D canvas — a WebGL canvas reads blank without
  // preserveDrawingBuffer — so we swap GL->canvas first, then snapshot on the next
  // frame once it has painted. If the snapshot can't be taken (tile not laid out,
  // e.g. condensed while in a hidden tab) we simply stay on the live canvas: still
  // GL-free, just not frozen, and the next visible re-park will freeze it.
  function _freezeForCondense() {
    _clearCondMask();
    // Park glued to the BOTTOM so the frozen preview shows the latest output, and
    // new lines arriving while parked keep the viewport pinned there (xterm sticks
    // to bottom once at bottom) — a parked card "follows" its session. The wheel
    // over a parked spine is repurposed to row-scroll (never scrolls the terminal
    // up), so nothing fights this.
    try { term.scrollToBottom(); } catch (e) {}
    useCanvas();   // GL -> readable 2D canvas (also drops the scarce WebGL context)
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        if (!condensed) return;   // expanded again before we could snapshot
        _condMask = _makeFrozenOverlay(31);   // above the swap mask (z 30) if both exist
        _diag.condFrozen = !!_condMask;
      });
    });
  }
  // Dashboard tells us the tile parked/un-parked (Cmd+X, the per-tile button, or a
  // bell auto-expand). On park: freeze + drop GL. On expand: drop the frozen image
  // and let the renderer gate restore WebGL if this tile is also input-visible.
  // `condensed` also repurposes a vertical wheel over the spine into row H-scroll.
  function setCondensedState(on) {
    if (condensed === on) return;
    condensed = on;
    _peeking = false;            // leaving either state cancels any hover-peek
    _diag.condensed = on;
    if (on) {
      _freezeForCondense();
      // Refresh the frozen preview while the card is parked IN VIEW, so a deck
      // you're watching isn't a wall of stale snapshots. Gated hard (see
      // _refreshCondSnapshot): off-screen/hidden cards (inputEnabled false) and
      // hovered cards (peeking, already live) cost nothing. One cheap drawImage
      // per in-view card every 2 s; an idle program dirties no rows so the live
      // canvas underneath isn't even repainting — the snapshot just re-copies an
      // unchanged frame.
      if (!_condRefreshTimer) _condRefreshTimer = setInterval(_refreshCondSnapshot, 2000);
    } else {
      if (_condRefreshTimer) { clearInterval(_condRefreshTimer); _condRefreshTimer = null; }
      _clearCondMask();
      setRendererVisible(inputEnabled);   // condensed is now false → guard won't veto
    }
  }
  // Re-snapshot a parked card's static overlay from the current live-canvas frame.
  // No-op unless it's condensed, in view (inputEnabled), and not being hovered
  // (peek already shows live). Snapshot the fresh frame FIRST, then drop the old
  // overlay — no gap where the bare canvas flashes through. If the snapshot can't
  // be taken (not laid out yet) we keep the existing image. Also self-heals a card
  // condensed while in a hidden tab (no initial snapshot) once its tab is shown.
  var _condRefreshTimer = null;
  function _refreshCondSnapshot() {
    if (!condensed || !inputEnabled || _peeking) return;
    // Follow the tail: re-pin to the bottom so the preview tracks new output even
    // if something nudged the viewport. Snapshot on the NEXT frame so the scroll's
    // repaint has landed (a same-frame snapshot would capture the pre-scroll view).
    try { term.scrollToBottom(); } catch (e) {}
    requestAnimationFrame(function () {
      if (!condensed || !inputEnabled || _peeking) return;
      var fresh = _makeFrozenOverlay(31);   // captures the live canvas only (skips old overlay)
      if (!fresh) return;
      if (_condMask) { try { _condMask.remove(); } catch (e) {} }
      _condMask = fresh;
      _diag.condFrozen = true;
      _diag.condRefreshes = (_diag.condRefreshes || 0) + 1;
    });
  }
  // Hover-peek: while a card is parked the live canvas keeps painting UNDER the
  // frozen overlay (we only swapped GL→canvas, the canvas renderer is live), so
  // showing current content costs nothing more than dropping the overlay. The
  // dashboard sends peek on:true when the pointer enters the card and on:false
  // when it leaves; on leave we re-snapshot the (now-current) frame so the static
  // image reflects whatever happened while hovering. No-op unless condensed —
  // peek never touches the renderer, so it can't re-acquire a WebGL context.
  var _peeking = false;
  function setPeek(on) {
    if (!condensed || _peeking === on) return;
    _peeking = on;
    if (on) {
      try { term.scrollToBottom(); } catch (e) {}   // peek shows the live tail
      _clearCondMask();          // reveal the live canvas underneath
    } else {
      _condMask = _makeFrozenOverlay(31);   // re-freeze on the latest painted frame
      _diag.condFrozen = !!_condMask;
    }
  }
  // Start on WebGL — visible-on-load tiles never flash through a canvas->WebGL
  // swap, and any session the dashboard later flags as hidden gets demoted to
  // canvas after the grace above (so the GL-context cap still holds).
  useWebgl();
  // Self-heal across WebGL context churn. Browsers lose GL contexts (GPU reset,
  // memory pressure, evicting the oldest when a page exceeds the per-page cap,
  // tab back/foregrounding) and sometimes auto-restore them. xterm's WebglAddon
  // drops us to canvas on loss (onContextLoss above) but nothing repaints a
  // browser-RESTORED context — the gray tile a manual ↻ refresh fixes. These
  // canvas events don't bubble, so we listen in the CAPTURE phase on the
  // terminal element, which still sees an event targeted at whichever inner
  // <canvas> owns the context. On loss: preventDefault() so the browser is
  // permitted to fire a later 'restored' (without it the context stays dead) —
  // we leave the actual dispose/demote to xterm's onContextLoss, and do NOT
  // re-acquire WebGL here (a loss under cap pressure would just thrash). On
  // restore: re-promote to WebGL if this tile is in view (heals via the burst),
  // else just repaint the current renderer.
  if (term.element) {
    term.element.addEventListener('webglcontextlost', function (ev) {
      try { ev.preventDefault(); } catch (e) {}
    }, true);
    term.element.addEventListener('webglcontextrestored', function () {
      try {
        if (inputEnabled && !condensed && !webglAddon) useWebgl();   // back on the GPU + burst-heal (never while parked)
        else _burstHeal();                             // repaint whatever's active now
      } catch (e) {}
    }, true);
  }
  // Detect a SILENTLY-lost WebGL context. The webglcontextlost/restored listeners
  // above only help when the browser actually FIRES those events; under the
  // per-page GL-context cap (a dashboard with many tiles, especially right after a
  // reload when every tile re-acquires a context at once) Chrome can reclaim a
  // tile's context with NO event at all. onContextLoss then never runs, so
  // webglAddon still looks valid — but its context is dead, and every _burstHeal
  // (clearTextureAtlas + refresh) paints onto it for nothing: the tile shows BLANK
  // even though its buffer is full. That's the "an actively-working claude tile
  // comes up blank after a page reload, while idle ones recover, and a manual ↻
  // fixes it" report. gl.isContextLost() reports the REAL state even when the
  // event never fired, so it's the reliable detector. A test seam force-trips it.
  function _glLost() {
    try { if (window.__forceGlLostForTest) return true; } catch (e) {}
    if (!webglAddon || !term.element) return false;
    try {
      var scr = term.element.querySelector('.xterm-screen');
      var cs = scr ? scr.querySelectorAll('canvas') : [];
      for (var i = 0; i < cs.length; i++) {
        if (cs[i].dataset && cs[i].dataset.frozenOverlay) continue;   // skip our snapshot overlays
        var gl = null;
        try { gl = cs[i].getContext('webgl2'); } catch (e) {}
        if (!gl) { try { gl = cs[i].getContext('webgl'); } catch (e) {} }
        if (gl) return gl.isContextLost();
      }
    } catch (e) {}
    return false;
  }
  // Cure a silently-lost GL context. A render-only _burstHeal can't repaint a dead
  // context, so demote to the 2D canvas — exactly what onContextLoss does when the
  // event DOES fire: canvas has no per-page cap to contend for, always paints, and
  // useCanvas() disposes the dead addon + _burstHeals the full buffer back onto the
  // canvas. We deliberately do NOT re-acquire WebGL here (a loss under cap pressure
  // would just thrash, per the contextlost note above); the hide→show gate and a
  // real webglcontextrestored event re-promote later. No term.resize → no SIGWINCH
  // → no Ink reflow/dup; scrollback untouched.
  function _recoverLostGl() {
    if (!_glLost()) return false;
    try { if (window.__disableGlRecover) return false; } catch (e) {}   // test seam: leave it broken to prove the bug
    _diag.glRecover++;
    try { useCanvas(); } catch (e) {}
    return true;
  }
  // Watchdog for silent GL-context loss outside the reattach window — e.g. opening
  // one more heavy tile evicts an existing visible tile's context with no event,
  // blanking it with no reload/hide-show to trigger recovery. Cheap: one
  // getContext + isContextLost per VISIBLE WebGL tile every few seconds; skips
  // canvas/parked/hidden tiles (no live GL context to lose, or nothing on screen
  // to fix). Once demoted to canvas the !webglAddon guard makes this a no-op.
  setInterval(function () {
    try { if (inputEnabled && !condensed && webglAddon) _recoverLostGl(); } catch (e) {}
  }, 3000);
  window.__claudeTerm = term;  // handle for debugging/automation; harmless (localhost, same-origin only)
  // Test/automation handle for the image-persistence layer (same rationale).
  window.__claudeImages = {
    scan: function (payload, anchor) { return _scanForIIP(payload, anchor); },
    capture: function (d, anchor) { return _captureIIP(d, anchor); },
    splice: function (s) { return _spliceImagesInto(s); },
    persistMeta: function () { return _persistImageMeta(); },
    toWebpB64: function (b) { return _toWebpB64(b); },
    marks: _imgMarks, metaKey: IMG_META_KEY, keyPrefix: _imgKeyPrefix,
    // out-of-band image persistence (setupTileImagePoll)
    oobMarks: _oobMarks, oobMetaKey: IMG_OOB_META_KEY, oobCursorKey: IMG_OOB_CURSOR_KEY,
    oobKeyPrefix: _oobKeyPrefix,
    persistOobMeta: function () { return _persistOobMeta(); },
    restoreOob: function () { return _oobRestore ? _oobRestore() : Promise.resolve(); }
  };

  // --- out-of-band inline images (claude tiles) --------------------------
  // A claude session can't paint images into its own tile: the image-mcp tool
  // runs mid-response (Ink clobbers any IIP it writes) and a Stop hook has no
  // controlling tty (so /dev/tty writes fail, ENXIO). Instead `show_image` spools
  // the image to /tmp/claude-tile-images/<ttyd-port>.ndjson; we poll the dashboard
  // for our OWN port and write each NEW image into the terminal as an IIP escape,
  // which the addon renders (the createImageBitmap shim makes that robust). The
  // cursor starts at the current backlog length so a fresh tile shows images that
  // arrive WHILE it's open, not a replay of history. Same-host cross-origin GET
  // (tile port -> dashboard port), so the endpoint CORS-echoes our origin.
  (function setupTileImagePoll() {
    // Dashboard port: the dashboard passes ?dash=<port> (reliable). Fall back to
    // the referrer's port, then 7680. (referrer alone breaks on a self-reload,
    // which makes it self-referential -> we'd poll our own ttyd and 404.)
    // Two layouts:
    //  - direct ttyd embed: this page is at http://127.0.0.1:<ttyd-port>/, the
    //    dashboard is a DIFFERENT origin (its own port), so we cross-origin GET
    //    http://127.0.0.1:<dash>/api/tile-image and the endpoint CORS-echoes us.
    //    Our spool key is location.port (== our ttyd port).
    //  - reverse-proxy embed: this page is at <dashboard-origin><base>/t/<port>/,
    //    i.e. SAME origin as the dashboard, so the fetch is a plain same-origin
    //    GET. Our ttyd port and the dashboard's public base path (e.g. '/dash',
    //    '' at root) both come from the pathname; location.port is the proxy's.
    var proxied = location.pathname.match(/^(.*)\/t\/(\d+)(?:\/|$)/);
    var url;
    if (proxied) {
      var myPort = proxied[2];
      url = proxied[1] + '/api/tile-image?port=' + encodeURIComponent(myPort);
    } else {
      var dash = params.get('dash') || '';
      if (!dash) { try { if (document.referrer) dash = new URL(document.referrer).port; } catch (e) {} }
      if (!dash) dash = '7680';
      var myPort = location.port || '0';
      url = 'http://127.0.0.1:' + dash + '/api/tile-image?port=' + encodeURIComponent(myPort);
    }
    var cursor = null;   // null until the first poll establishes the backlog length

    // Each image renders as a ROW-ANCHORED DOM OVERLAY: we reserve blank lines for
    // its height, anchor an xterm Marker at the top of them, and attach a
    // Decoration (top layer) holding the <img>. xterm repositions the decoration
    // as the buffer scrolls and hides it off-screen, so the image looks INLINE
    // (sits in the flow, scrolls with the text) yet lives on a separate DOM layer
    // the TUI's canvas repaint can't overwrite — no "text overlapping", no panel,
    // and it's a real <img> (copyable). We only reserve the blank lines when the
    // terminal is QUIESCENT (no ws data for a beat) so injecting them doesn't
    // disrupt a live TUI mid-render. window.__lastTermDataAt is bumped per ws chunk.
    var QUIET_MS = 1200;
    function quiet() { return Date.now() - (window.__lastTermDataAt || 0) >= QUIET_MS; }

    // most-recent <img> + a copy-to-clipboard button (canvas -> PNG -> clipboard;
    // localhost http is a secure context, so clipboard.write works on the click).
    var _last = null, _btn = null;
    function _copyLast() {
      if (!_last || !navigator.clipboard || !window.ClipboardItem) return;
      var c = document.createElement('canvas');
      c.width = _last.naturalWidth; c.height = _last.naturalHeight;
      c.getContext('2d').drawImage(_last, 0, 0);
      c.toBlob(function (b) {
        if (!b) return;
        navigator.clipboard.write([new ClipboardItem({ 'image/png': b })]).then(function () {
          _btn.textContent = '✓ copied'; setTimeout(function () { _btn.textContent = '⧉ copy image'; }, 1200);
        }).catch(function () {});
      }, 'image/png');
    }
    function _showCopyBtn() {
      if (!_btn) {
        _btn = document.createElement('button');
        _btn.textContent = '⧉ copy image';
        _btn.title = 'Copy the most recent image to the clipboard';
        _btn.style.cssText = 'position:fixed;bottom:8px;right:12px;z-index:99;' +
          'background:#1c1c1c;color:#ddd;border:1px solid #444;border-radius:6px;padding:4px 8px;' +
          'font:12px ui-monospace,Menlo,monospace;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.5);';
        _btn.onclick = _copyLast;
        document.body.appendChild(_btn);
      }
      _btn.style.display = 'block';
    }
    function cellDims() {
      try { var d = term._core._renderService.dimensions.css.cell; if (d && d.width) return d; } catch (e) {}
      var el = document.querySelector('.xterm-screen');
      return { width: el ? el.clientWidth / term.cols : 8, height: el ? el.clientHeight / term.rows : 17 };
    }
    // Attach an image decoration over a block of `rows` rows whose top sits at
    // absolute buffer row `absTop`. Registers an xterm Marker (so xterm tracks
    // the row across scroll/trim/resize and hides it off-screen) + a top-layer
    // Decoration holding the <img>. Returns the marker (or null on failure).
    function attachDecoration(absTop, src, dw, dh, rows, name) {
      try {
        var b = term.buffer.active, cols = term.cols;
        var marker = term.registerMarker(absTop - (b.baseY + b.cursorY));
        if (!marker) return null;
        var deco = term.registerDecoration({ marker: marker, x: 0, width: cols, height: rows, layer: 'top' });
        if (!deco) return { marker: marker, deco: null };
        deco.onRender(function (el) {
          if (el.__filled) return; el.__filled = true;
          el.style.pointerEvents = 'none';      // let terminal interaction pass through
          var node = document.createElement('img');
          node.src = src;
          node.title = name || 'image';
          node.style.cssText = 'display:block;width:' + dw + 'px;height:' + dh + 'px;' +
            'pointer-events:auto;cursor:pointer;';
          node.onclick = function () { _last = node; _copyLast(); };   // click image to copy
          el.appendChild(node);
        });
        return { marker: marker, deco: deco };
      } catch (e) { return null; }
    }
    // Track + persist one live out-of-band image: register it in _oobMarks (so
    // _persistOobMeta writes its anchor on the next tick) and stash its WebP bytes
    // in IndexedDB, evicting the bytes if the anchored row is trimmed out.
    //
    // CRITICAL for claude (kind=host) tiles: their buffer is the Ink-managed live
    // viewport with little/no retained scrollback, so the decoration's marker.line
    // drifts NEGATIVE within a beat of placement (Ink repaints/scrolls the reserved
    // rows out the top). If we only read marker.line at persist time (15s tick /
    // pagehide) it's already < 0 and the old guard skipped EVERY image → no meta key
    // was ever written → nothing to restore (the reported bug). So we CAPTURE the
    // anchor HERE, at placement, while it's still valid: both the hard-line index
    // (good for shell/TUI tiles that keep scrollback) AND the row distance from the
    // buffer's bottom (`distEnd`, the stable reference on a claude tile, where the
    // image rides just above Ink's live region). _persistOobMeta writes the captured
    // values when the live marker has drifted out of range, so the image survives.
    function trackOob(handle, absTop, dw, dh, rows, srcB64, mime, src, name) {
      try {
        var seq = _oobSeq++, key = _oobKeyPrefix + seq;
        var b = term.buffer.active;
        var line0 = _imgHardLineIndex(absTop);                 // hard-line idx at placement
        var distEnd = b.length - absTop;                       // rows from the BUFFER END (not the
                                                               // cursor: on a claude idle prompt the
                                                               // cursor sits mid-screen, but the image
                                                               // sits near the buffer's last row).
        // Keep src/name so the watchdog can rebuild the decoration after Ink scrolls
        // its marker off. We do NOT delete the IDB bytes on marker-dispose: on a
        // claude tile the marker dies routinely (scrolled out of the non-growing
        // buffer) yet the image must survive for restore — eviction happens only on
        // an explicit clear-scrollback. The mark also STAYS in _oobMarks across a
        // dispose so the watchdog can re-anchor it.
        _oobMarks.push({ seq: seq, marker: handle.marker, deco: handle.deco, dw: dw, dh: dh, rows: rows,
                         line0: line0, distEnd: distEnd, src: src || ('data:' + (mime || 'image/png') + ';base64,' + srcB64) });
        _toWebpB64(srcB64).then(function (webp) {
          if (webp) return _idbPut(key, { webp: webp });
        }).catch(function () {});
        // Persist immediately while the anchor is fresh — on a claude tile the
        // next scheduled persist tick would see a negative marker.line and lose it.
        try { _persistOobMeta(); } catch (e) {}
      } catch (e) {}
    }
    // Re-anchor watchdog. On an active claude (Ink) tile the buffer is the live
    // viewport and Ink's full-screen repaints scroll a decoration's marker off the
    // top (marker.line < 0 → xterm disposes the decoration → the image vanishes),
    // both for a freshly-placed image and for one we just restored. We can't stop
    // Ink, so we re-pin: any tracked OOB image whose marker has died or drifted
    // negative is re-attached in the STABLE blank region just above the buffer's
    // last row (verified to hold on a live claude tile). Cheap (only acts when a
    // marker is actually dead) and fail-safe. Keeps the image a real xterm
    // Decoration (so it scrolls with / hides off-screen like inline content).
    function _reanchorOob() {
      try {
        var b = term.buffer.active;
        for (var i = 0; i < _oobMarks.length; i++) {
          var m = _oobMarks[i];
          if (m.marker && m.marker.line >= 0) continue;        // still anchored fine
          // marker dead/negative → rebuild near the bottom blank area
          try { if (m.deco) m.deco.dispose(); } catch (e) {}
          try { if (m.marker) m.marker.dispose(); } catch (e) {}
          var absTop = Math.max(0, b.length - m.rows - 1);
          var h = attachDecoration(absTop, m.src, m.dw, m.dh, m.rows, 'image');
          if (h && h.marker) { m.marker = h.marker; m.deco = h.deco; }
        }
      } catch (e) {}
    }
    setInterval(_reanchorOob, 700);
    function place(im) {
      var img = new Image();
      img.onload = function () {
        _last = img; _showCopyBtn();
        try {
          var cell = cellDims(), cols = term.cols;
          var scale = Math.min(1, ((cols - 1) * cell.width) / img.naturalWidth);
          var dw = Math.max(1, Math.round(img.naturalWidth * scale));
          var dh = Math.max(1, Math.round(img.naturalHeight * scale));
          var rows = Math.max(1, Math.ceil(dh / cell.height));
          // reserve `rows` blank lines so the image owns vertical space in the flow.
          // term.write is ASYNC (parser-queued); read the cursor/anchor only AFTER
          // it lands (callback) — reading synchronously gave a stale buffer, so the
          // computed `top` and the captured distEnd were wrong (off by the rows we
          // just wrote, sometimes negative on a busy claude tile).
          term.write('\r\n' + new Array(rows + 1).join('\n'), function () {
            try {
              var b = term.buffer.active, top = (b.baseY + b.cursorY) - rows;   // top of the reserved block
              var h = attachDecoration(top, img.src, dw, dh, rows, im && im.name);
              if (!h || !h.marker) return;
              // keep the bytes + render params on the mark so the re-anchor watchdog
              // can rebuild the decoration if Ink scrolls this marker off the buffer.
              trackOob(h, top, dw, dh, rows, im.b64, im.mime, img.src, im && im.name);
            } catch (e) {}
          });
        } catch (e) {}
      };
      img.src = 'data:' + (im.mime || 'image/png') + ';base64,' + im.b64;
    }
    // Re-create persisted out-of-band image decorations after a reload, re-anchoring
    // each to a row in the (settled) buffer. Two anchors are tried in order:
    //   1. the saved hard-line index (`line`) → its current absolute row, IF that
    //      resolves inside the buffer (the reliable path on shell/TUI tiles whose
    //      scrollback restored 1:1);
    //   2. otherwise the saved distance-from-bottom (`distEnd`): absTop =
    //      (baseY + cursorY) - distEnd, the stable reference on a CLAUDE tile where
    //      the image rode just above Ink's live region and the hard-line index is
    //      meaningless after Ink repaints.
    // The result is clamped into [0, length-1] so we NEVER drop the image.
    //
    // TIMING is the crux on a claude (kind=host) tile. restoreOob() is invoked from
    // _applyRestored the instant the restored scrollback lands — but ttyd's reattach
    // replay + Ink's full-screen repaint arrive RIGHT AFTER and SCROLL the buffer,
    // which would drag a freshly-anchored marker negative (off the tiny non-growing
    // buffer) and xterm disposes it → the image vanishes again, exactly like it does
    // a beat after a live placement. So we DEFER the attach until the terminal goes
    // QUIESCENT (same gate place() uses), and anchor against the SETTLED live bottom.
    // On an idle tile nothing scrolls after that, so the decoration sticks. Bytes
    // load from IndexedDB (WebP). Wired via _oobRestore. Fail-safe throughout.
    var _oobRestored = false;
    function _attachRestored(meta) {
      return Promise.all(meta.map(function (m) {
        return _idbGet(_oobKeyPrefix + m.seq).then(function (rec) {
          if (!rec || !rec.webp) return;
          var b = term.buffer.active, src = 'data:image/webp;base64,' + rec.webp;
          // Anchor in the stable blank region just above the buffer's last row: on a
          // claude tile the saved hard-line / distEnd may land inside Ink's actively
          // repainted top frame and get scrolled off immediately, whereas the bottom
          // blank area holds. A shell/TUI tile with a faithfully restored hard-line
          // uses that exact row instead (it stays put there).
          var absTop = (typeof m.line === 'number' && m.line >= 0) ? _absRowForHardLine(m.line) : -1;
          if (absTop < 0) absTop = Math.max(0, b.length - m.rows - 1);
          if (absTop > b.length - 1) absTop = Math.max(0, b.length - 1);   // clamp into the buffer
          var h = attachDecoration(absTop, src, m.dw, m.dh, m.rows, 'image');
          if (h && h.marker) {
            _last = null;   // restored image becomes the copy target lazily on click
            _showCopyBtn();
            _oobMarks.push({ seq: m.seq, marker: h.marker, deco: h.deco, dw: m.dw, dh: m.dh, rows: m.rows,
                             line0: m.line, distEnd: m.distEnd, src: src, _restored: true });
          }
        }).catch(function () {});
      })).then(function () { try { _persistOobMeta(); } catch (e) {} }, function () {});
    }
    function restoreOob() {
      if (_oobRestored) return Promise.resolve();   // once per page load
      var meta;
      try { meta = JSON.parse(localStorage.getItem(IMG_OOB_META_KEY) || 'null'); } catch (e) { meta = null; }
      if (!meta || !meta.length) return Promise.resolve();
      _oobRestored = true;
      // Seed the live seq counter past every restored seq so a NEW image placed
      // after restore can't collide with a restored one (same seq → same IDB key →
      // overwritten bytes + duplicate meta entries). Seqs are stable across reloads.
      try {
        var mx = -1;
        for (var si = 0; si < meta.length; si++) if (typeof meta[si].seq === 'number' && meta[si].seq > mx) mx = meta[si].seq;
        if (mx + 1 > _oobSeq) _oobSeq = mx + 1;
      } catch (e) {}
      // Wait for the reattach replay / Ink repaint to settle before anchoring, so
      // the marker isn't immediately scrolled off. Poll quiescence with a hard
      // backstop so we attach even on a tile that never fully quiesces.
      return new Promise(function (res) {
        var tries = 0, MAX = 40;   // ~40 × 250ms = 10s backstop
        (function waitQuiet() {
          if (quiet() || tries++ >= MAX) { _attachRestored(meta).then(res, res); return; }
          setTimeout(waitQuiet, 250);
        })();
      });
    }
    _oobRestore = restoreOob;
    function poll() {
      var next = 1000;
      fetch(url + '&since=' + (cursor == null ? 0 : cursor), { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (d) {
            if (cursor == null) {
              // First poll after (re)load. If we restored images from a prior run,
              // resume the saved cursor so we don't re-fetch + double-render them;
              // otherwise skip the pre-open backlog. Clamp to the current total so
              // a shrunk/rotated ndjson can't leave us stuck in the past.
              var saved = null;
              try { saved = localStorage.getItem(IMG_OOB_CURSOR_KEY); } catch (e) {}
              cursor = (saved != null && _oobMarks.length) ? Math.min(parseInt(saved, 10) || 0, d.total) : d.total;
              try { localStorage.setItem(IMG_OOB_CURSOR_KEY, String(cursor)); } catch (e) {}
            } else if (d.images && d.images.length) {
              if (quiet()) {
                d.images.forEach(place);
                cursor = d.total;
                try { localStorage.setItem(IMG_OOB_CURSOR_KEY, String(cursor)); } catch (e) {}
              } else next = 400;                             // TUI busy: hold, retry soon
            }
          }
        })
        .catch(function () {})
        .then(function () { setTimeout(poll, next); });
    }
    poll();
  })();

  // --- find / search (Cmd+F) ---
  // The GPU/canvas renderer paints text to a surface, so the browser's native
  // Find can't see terminal text. xterm's search addon scans the buffer instead;
  // this wires it to a small overlay box. Self-contained — styles + DOM injected
  // here so build-term.sh only has to inline the addon. We bind Cmd+F only, not
  // Ctrl+F, since Ctrl+F is a live key in the shell (readline forward-char, vim).
  (function setupSearch() {
    var DECOR = { decorations: {
      matchBackground: '#5a4a00', matchBorder: '#8a7400', matchOverviewRuler: '#8a7400',
      activeMatchBackground: '#c08a00', activeMatchBorder: '#ffd24d', activeMatchColorOverviewRuler: '#ffd24d'
    } };
    var style = document.createElement('style');
    style.textContent =
      "#find{position:fixed;top:8px;right:14px;z-index:99;display:none;align-items:center;gap:6px;" +
      "background:#1c1c1c;border:1px solid #444;border-radius:6px;padding:4px 6px;" +
      "box-shadow:0 4px 14px rgba(0,0,0,.5);font:12px ui-monospace,Menlo,monospace;color:#ddd;}" +
      "#find.on{display:flex;}" +
      "#find input{background:#2b2b2b;border:1px solid #555;border-radius:4px;color:#eee;" +
      "padding:3px 6px;width:180px;outline:none;font:inherit;}" +
      "#find input:focus{border-color:#7aa2f7;}" +
      "#find .count{color:#888;min-width:46px;text-align:right;font-variant-numeric:tabular-nums;}" +
      "#find button{background:none;border:0;color:#aaa;cursor:pointer;padding:2px 5px;border-radius:4px;font:inherit;line-height:1;}" +
      "#find button:hover{background:#333;color:#fff;}";
    document.head.appendChild(style);

    var box = document.createElement('div');
    box.id = 'find';
    box.innerHTML =
      '<input type="text" placeholder="Find" spellcheck="false" autocomplete="off">' +
      '<span class="count"></span>' +
      '<button type="button" title="Previous (Shift+Enter)">↑</button>' +
      '<button type="button" title="Next (Enter)">↓</button>' +
      '<button type="button" title="Close (Esc)">✕</button>';
    document.body.appendChild(box);
    var input = box.querySelector('input');
    var count = box.querySelector('.count');
    var btns = box.querySelectorAll('button');

    search.onDidChangeResults(function (r) {
      if (!r || r.resultCount === undefined) { count.textContent = ''; return; }
      count.textContent = r.resultCount ? (r.resultIndex + 1) + '/' + r.resultCount : 'no match';
    });

    function find(forward, incremental) {
      var q = input.value;
      if (!q) { search.clearDecorations(); count.textContent = ''; return; }
      var opts = Object.assign({ incremental: !!incremental }, DECOR);
      if (forward) search.findNext(q, opts); else search.findPrevious(q, opts);
    }
    function open() { box.classList.add('on'); input.focus(); input.select(); if (input.value) find(true, true); }
    function close() { box.classList.remove('on'); search.clearDecorations(); try { term.focus(); } catch (e) {} }

    input.addEventListener('input', function () { find(true, true); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); find(!e.shiftKey, false); }
      else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
    btns[0].addEventListener('click', function () { find(false, false); });
    btns[1].addEventListener('click', function () { find(true, false); });
    btns[2].addEventListener('click', close);

    // Capture phase so xterm doesn't consume Cmd+F first; preventDefault also
    // stops the browser's native (useless here) Find.
    window.addEventListener('keydown', function (e) {
      if (e.metaKey && !e.ctrlKey && !e.altKey && (e.key === 'f' || e.key === 'F')) {
        e.preventDefault(); e.stopPropagation(); open();
      }
    }, true);
  })();

  // --- PROTOTYPE: sub-row smooth scroll via CSS transform ---
  // xterm's canvas/WebGL renderers draw the grid row-aligned: the visible text
  // can only jump a whole row at a time, even though the native .xterm-viewport
  // scrolls in pixels. So the text snaps while the scrollbar glides. We close that
  // gap by translating the rendered screen layer by the sub-row remainder
  // (scrollTop minus the nearest whole-row position), so text tracks the actual
  // pixel scroll. xterm still re-renders at each row boundary; in between, this
  // transform supplies the fractional movement. Toggle with window.__smoothScroll.
  //
  // Default OFF: at the bottom-of-buffer boundary the transform can land on
  // delta≈0 (rounded-aligned) and then jump to a multi-px translate on the
  // very first scroll-up event, which feels like the viewport "sticks" to the
  // bottom before releasing. The sub-row improvement only really helps with
  // continuous trackpad gestures; for one-notch wheel scrolls the row snap is
  // smoother *without* it. Opt in with `window.__smoothScroll = true` in
  // devtools if you want to try it.
  (function smoothScrollProto() {
    var screen = term.element && term.element.querySelector('.xterm-screen');
    var viewport = term.element && term.element.querySelector('.xterm-viewport');
    if (!screen || !viewport) return;
    // Default ON, persisted per-browser via localStorage. The previous behavior
    // was opt-in (hard-coded `false`), so a user who turned it on via devtools
    // lost the setting on every reload and reported "smooth scroll is gone".
    // To disable: `localStorage.setItem('claude-term-smooth-scroll', '0')`.
    // To re-enable: `localStorage.removeItem('claude-term-smooth-scroll')`.
    try { window.__smoothScroll = (localStorage.getItem('claude-term-smooth-scroll') !== '0'); }
    catch (e) { window.__smoothScroll = true; }
    function rowH() {
      try {
        var h = term._core._renderService.dimensions.css.cell.height;
        if (h > 0) return h;
      } catch (e) {}
      var area = viewport.querySelector('.xterm-scroll-area');
      var n = term.buffer.active.length;
      return (area && n) ? area.offsetHeight / n : 0;
    }
    function apply() {
      if (!window.__smoothScroll) { screen.style.transform = ''; return; }
      var h = rowH();
      if (!h) { screen.style.transform = ''; return; }
      var st = viewport.scrollTop;
      var delta = st - Math.round(st / h) * h;   // remainder in [-h/2, +h/2]
      screen.style.transform = delta ? 'translateY(' + (-delta) + 'px)' : '';
    }
    viewport.addEventListener('scroll', apply, { passive: true });
  })();

  // --- gzip helpers (CompressionStream is sync-of-async, no library needed) ---
  // Terminal serialize output is dense SGR escape codes ("\x1b[38;2;R;G;Bm…")
  // that gzip compresses 8–15×. Net effect with the latin1 string overhead
  // (each compressed byte stored as one UTF-16 char, high byte 0 → 2× in
  // localStorage's UTF-16): roughly 4× more usable space for the same quota.
  function _gzipAvailable() {
    return typeof CompressionStream !== 'undefined' && typeof DecompressionStream !== 'undefined';
  }
  // Pack a Uint8Array into a latin1 JS string (one byte per char). Faster than
  // base64 and skips the +33% base64 overhead — every byte still fits in a
  // single UTF-16 code unit's low half. Chunked because String.fromCharCode
  // can stack-overflow on multi-MB inputs.
  function _bytesToLatin1(buf) {
    var s = '', chunk = 0x8000;
    for (var i = 0; i < buf.length; i += chunk) {
      s += String.fromCharCode.apply(null, buf.subarray(i, Math.min(i + chunk, buf.length)));
    }
    return s;
  }
  function _latin1ToBytes(s) {
    var buf = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) buf[i] = s.charCodeAt(i) & 0xff;
    return buf;
  }
  function gzipToString(str) {
    if (!_gzipAvailable()) return Promise.reject(new Error('CompressionStream unavailable'));
    var stream = new Blob([str]).stream().pipeThrough(new CompressionStream('gzip'));
    return new Response(stream).arrayBuffer().then(function (ab) { return _bytesToLatin1(new Uint8Array(ab)); });
  }
  function gunzipFromString(s) {
    if (!_gzipAvailable()) return Promise.reject(new Error('DecompressionStream unavailable'));
    var stream = new Blob([_latin1ToBytes(s)]).stream().pipeThrough(new DecompressionStream('gzip'));
    return new Response(stream).text();
  }

  // --- scrollback restore (gates the connection so history sits above live) ---
  function isSized() {
    var el = term.element;
    return !!el && el.clientWidth > 8 && el.clientHeight > 8;
  }
  // Decorative markers an older build wrote into the buffer on each restore.
  // They got serialized + re-loaded + re-marked next reload — ouroboros-style
  // — accumulating duplicate content. Strip them from any legacy v2 blob so
  // existing entries self-clean on first load after this fix.
  var RESTORE_MARKER_RE = /\r?\n?\x1b\[2m─── restored ───\x1b\[0m\r?\n?/g;
  function _applyRestored(saved) {
    if (!saved) return Promise.resolve();
    saved = saved.replace(RESTORE_MARKER_RE, '');
    // Write the serialized buffer — escape codes inside reconstruct cursor
    // position, colors, alt-buffer state, etc. ttyd's reattach replay paints
    // the live program over whatever viewport region it touches; the restored
    // scrollback above stays intact. We DO NOT write a marker or push blank
    // rows here: anything we add ends up persisted on the next setInterval
    // tick and re-restored next reload, doubling content.
    //
    // Trailing `\x1b[0m` resets all SGR attributes — if the saved blob ended
    // mid-attribute (e.g. persist() snapshotted while claude had underline
    // on for its input prompt line, with no matching \x1b[24m closer), the
    // open attribute would leak into every byte ttyd's reattach replay (and
    // every byte of subsequent live output) wrote — making "all text now
    // underlined" the visible symptom. \x1b[0m only updates the renderer's
    // current-attribute state; it doesn't paint any visible cells.
    //
    // Splice any persisted IIP images back in at their hard-line anchors first
    // (async: fetches WebP bytes from IndexedDB). On any failure we fall back to
    // the text-only write so image persistence can never break scrollback.
    return _spliceImagesInto(saved).then(function (withImgs) {
      return new Promise(function (res) { term.write(withImgs + '\x1b[0m', res); });
    }, function () {
      return new Promise(function (res) { term.write(saved + '\x1b[0m', res); });
    }).then(function () {
      // Buffer is now populated (term.write callback) — the reserved blank lines
      // of any out-of-band images are back, so re-create their decorations at the
      // saved anchors. Fail-safe: never lets an image error break restore.
      try { if (_oobRestore) return _oobRestore(); } catch (e) {}
    });
  }
  function restoreScrollback() {
    // Try v3 (gzipped) first; fall back to v2 (uncompressed legacy) if the
    // current run hasn't been persisted yet OR the browser lacks gzip.
    // Returns a Promise so startOnce only fires AFTER the synchronous-looking
    // write actually lands in the buffer.
    try {
      var gz = localStorage.getItem(LSKEY_V3);
      if (gz && _gzipAvailable()) {
        return gunzipFromString(gz).then(_applyRestored, function () {
          // Corrupt gzip blob — try v2 as a last resort, then drop the bad key.
          try { localStorage.removeItem(LSKEY_V3); } catch (e) {}
          try { return _applyRestored(localStorage.getItem(LSKEY_V2)); } catch (e) {}
        });
      }
      return _applyRestored(localStorage.getItem(LSKEY_V2));
    } catch (e) {}
    return Promise.resolve();
  }
  // Restore at the correct width THEN connect. The iframe may not be laid out the
  // instant this script runs (notably in Firefox), so WAIT briefly for a real
  // size before restoring rather than skipping it — skipping is why Firefox saw
  // almost no scrollback. A genuinely hidden-tab iframe (0×0) never gets a size:
  // after a short grace we connect without a restore (restoring at 0 width would
  // garble it; the tab fills with live output once shown).
  var started = false;
  // Persistence authority gate. A view may only persist (overwrite the stored
  // snapshot) once it has actually RESTORED — i.e. it is the authoritative live
  // view of this session's history. Stays false for a tile that skipped restore
  // (hidden/never-sized → go(false)) or whose program re-dumps its own history
  // (a shell, see mayRestore below): such a view has a buffer it never loaded
  // the saved history into, and letting it persist would clobber the good
  // snapshot with nothing. That was the "scrollback buffer gone after reload"
  // bug — a dashboard reload fires pagehide in every iframe at once, and empty
  // hidden tiles overwrote their own history (pagehide also removed v3). Set
  // true only by the restore-on-first-burst path in connect().
  var canPersist = false;
  // Whether restore is ALLOWED for this view (terminal is sized). The actual
  // restore is deferred to the first replay burst in connect(), which decides
  // — based on the burst's shape — whether restoring would duplicate ttyd's
  // own replay (see _looksLikeTuiRepaint).
  var mayRestore = false;
  function startOnce() { if (started) return; started = true; startSession(); }
  // Pre-size a hidden (never-fitted) view to the session's last persisted size
  // before it connects. Without this, the ttyd handshake carries xterm's 80×24
  // default, the spawned dtach client resizes the SHARED pty to 80×24, and a
  // busy claude repaints every real-size view of this session into wrapped
  // garbage — fragments of its status line stair-stepping into scrollback (and
  // from there into the persisted snapshot). Observed live as three dtach
  // clients on one session: two sized views at 116×42 plus one zombie at 80×24
  // from a hidden-tab connect. Sanity-bounded so a corrupt key can't wedge the
  // terminal; with no saved size yet (first run) the old behaviour stands.
  // term.resize → onResize → sendResize is a no-op here (socket not open yet);
  // the size reaches ttyd via the connect handshake instead.
  function _applySavedSize() {
    try {
      // Prefer this session's own last size; fall back to the global last-tile
      // size so a never-seen session (no per-session key) still attaches at the
      // real tile width instead of 80×24 and clobbering the shared pty.
      var m = /^(\d+)x(\d+)$/.exec(localStorage.getItem(LSSIZE) || '')
           || /^(\d+)x(\d+)$/.exec(localStorage.getItem(LSSIZE_DEFAULT) || '');
      if (!m) return;
      var c = +m[1], r = +m[2];
      if (c >= 20 && c <= 500 && r >= 5 && r <= 200 && (c !== term.cols || r !== term.rows))
        term.resize(c, r);
    } catch (e) {}
  }
  function restoreThenStart() {
    var ro = null, grace = null;
    function go(doRestore) {
      if (started) return;   // startOnce latches started=true, so a 2nd ResizeObserver tick is a no-op
      if (ro) { try { ro.disconnect(); } catch (e) {} ro = null; }
      if (grace) { clearTimeout(grace); grace = null; }
      mayRestore = doRestore;
      if (doRestore) { try { fit.fit(); } catch (e) {} }
      else _applySavedSize();   // hidden connect: attach at the session's last known size, not 80×24
      startOnce();   // → startSession → connect; restore (if any) runs on the first replay burst
    }
    if (isSized()) { go(true); return; }
    try { ro = new ResizeObserver(function () { if (isSized()) go(true); }); ro.observe(term.element); } catch (e) {}
    // Hidden/0×0 at boot (a non-active tab, or still laying out): after a short
    // grace, CONNECT anyway so live output + the prompt bell keep flowing while
    // hidden — but WITHOUT restoring (a restore at 0 width would garble, so
    // go(false) leaves the buffer empty). The old code stopped there, which is
    // exactly why a tile sitting in another tab came up with ~0 scrollback on a
    // dashboard load and only a manual ↻ reload (now visible + a fresh attach)
    // brought its history back. Instead, after go(false) we keep watching: the
    // first time the tile is actually shown and sized, reload it once so the
    // now-sized fresh attach restores scrollback automatically — same effect as
    // that manual reload, without the user clicking every tile. (Shells aren't
    // affected: ttyd replays their text on attach. It's alt-screen TUIs like
    // claude, whose history lives only in our snapshot, that came up blank.)
    grace = setTimeout(function () { go(false); restoreOnReveal(); }, 1500);
  }
  // One-shot: after a tile connected without restoring (go(false)), reload it the
  // first time it becomes visible + sized so the fresh attach restores scrollback.
  // Latched + guarded on isSized() so layout jitter can't turn it into a reload
  // loop. No-op if the tile is never shown (stays connected/blank, as before).
  function restoreOnReveal() {
    var ro2 = null, done = false;
    function fire() {
      if (done || !isSized()) return;   // ignore the ticks fired while still 0×0
      done = true;
      if (ro2) { try { ro2.disconnect(); } catch (e) {} ro2 = null; }
      try { location.reload(); } catch (e) {}
    }
    try { ro2 = new ResizeObserver(fire); ro2.observe(term.element); } catch (e) {}
  }

  // --- dashboard messaging (when embedded in an iframe) ---
  function post(msg) {
    try { if (window.parent && window.parent !== window) { msg.type = 'claude-term'; msg.sid = sid; window.parent.postMessage(msg, '*'); } } catch (e) {}
  }
  function postTitle(t) { post({ title: t }); }
  function postBell() { post({ bell: true }); }

  // --- surface claude's input-box ghost suggestion to the dashboard ---
  // claude renders its placeholder / suggested prompt as DIM (SGR 2) text inside
  // the input box, and ONLY while the box is empty (typing replaces it). xterm
  // flags those cells via cell.isDim(); the box border + footer + status line use
  // 256-colour / plain white (NOT the dim attribute), so dim cleanly isolates the
  // suggestion. We scan the whole LIVE frame (baseY-anchored, so a user-scrolled
  // viewport can't make us read dim text out of the scrollback) and reconstruct
  // the dim text by its column span — claude positions each word with an
  // absolute-column cursor jump, so the dim cells are NOT contiguous (the gaps are
  // default-attr blanks). We scan the FULL frame, not just the bottom rows,
  // because claude only pins the input box to the bottom once the transcript fills
  // the screen — in a short/fresh session the box (and its placeholder) sits right
  // below the content, near the top. We post the longest dim run; the dashboard
  // stashes it in localStorage where the same-origin chat-panel offers it as a
  // Tab-to-use ghost. Best-effort and cosmetic: if a transient dim hint is ever
  // picked up, the chat-panel only shows it while idle and never auto-sends it.
  var _lastSuggestion = null, _sugTimer = null;
  function _scrapeSuggestion() {
    try {
      var b = term.buffer && term.buffer.active, rows = term.rows, cols = term.cols;
      if (!b || !rows || !cols) return '';
      var base = b.baseY, best = '';
      for (var ry = 0; ry < rows; ry++) {
        var line = b.getLine(base + ry);
        if (!line) continue;
        var minC = -1, maxC = -1, chars = [];
        for (var x = 0; x < cols; x++) {
          var cell = line.getCell(x);
          if (!cell) { chars.push(' '); continue; }
          chars.push(cell.getChars() || ' ');
          if (cell.isDim()) { if (minC < 0) minC = x; maxC = x; }
        }
        if (minC < 0) continue;                       // no dim text on this row
        var s = chars.slice(minC, maxC + 1).join('').replace(/\s+/g, ' ').trim();
        if (s.length > best.length) best = s;
      }
      return best.slice(0, 400);
    } catch (e) { return ''; }
  }
  function _scanSuggestionSoon() {
    if (_sugTimer) return;                             // debounce: one scan per burst of writes
    _sugTimer = setTimeout(function () {
      _sugTimer = null;
      var s = _scrapeSuggestion();
      if (s !== _lastSuggestion) { _lastSuggestion = s; post({ kind: 'suggestion', text: s }); }
    }, 350);
  }

  // --- prompt-detection bell ---
  // Claude doesn't emit BEL when it shows a permission/yes-no prompt, so a
  // user who walks away can miss the question. Two paths feed into our bell:
  //  1. The PRIMARY path is a user-configured Claude Code Notification hook
  //     that runs `printf '\a' >/dev/tty`,
  //     which writes BEL into claude's pty → ttyd forwards it → scanBel spots
  //     the byte in the websocket chunk. Clean, decoupled, but requires
  //     per-user setup. Detected synchronously in scanBel, NOT via
  //     term.onBell — see the note on scanBel.
  //  2. This FALLBACK pattern-matches claude's prompt sigils in the data
  //     stream. Less reliable (breaks if claude restyles its UI), but works
  //     out of the box. Both paths debounce against each other via the
  //     same `_bellAt` timestamp so we don't fire twice for one prompt.
  var _bellAt = 0;                                    // last bell timestamp (any path)
  var _patBuf = '';                                   // rolling window of recent decoded output
  var _bellMuteUntil = 0;                             // bells before this are attach-replay echo
  // Dedup window. Its ONLY job is to merge the two signals for ONE event — the
  // hook BEL (Stop/Notification → printf '\a') and the PROMPT_RE pattern match
  // arrive within milliseconds of each other, so a small window collapses them
  // into a single bell. It must NOT be large: at 4000ms it also swallowed
  // deliberate, distinct bells — a finished turn followed by a quick permission
  // prompt, or simply hammering `printf '\a'` in a terminal tile to test, dropped
  // ~half the bells and read as "the bell only works half the time". 600ms is
  // wider than any hook-vs-pattern skew yet narrow enough that real back-to-back
  // prompts each ring.
  var BELL_DEDUP_MS = 600;                            // don't re-fire within this window
  // Mute window after a (re)connect. The attach replay re-sends content the
  // user already saw — a TUI repaint can include an old, already-answered
  // "Do you want to proceed?" still visible in the transcript, and a shell
  // tile's bare-text re-dump can include literal BEL bytes from past rings.
  // Scanning that replay rang stale bells on every reattach: laptop wake,
  // server restart, dashboard reload, and restoreOnReveal()'s automatic
  // reload when a hidden-at-boot tile is first shown ("a bell rings when I
  // just switch to a tab"). 1500ms covers auth + repaint + the async-gzip
  // restore flush, while a genuinely new prompt after a reconnect still rings.
  var BELL_CONNECT_MUTE_MS = 1500;
  // Claude's prompt openers — distinctive enough to be near-zero false-positive
  // rate in normal shell output. Add to the union as claude adds new prompt
  // styles. The leading word boundary keeps "ido you want" (a hypothetical
  // false positive) from matching.
  //   • yes/no & permission prompts: "Do you want to …", "(y/n)", "[y/N]", …
  //   • selection menus (AskUserQuestion AND permission menus): the highlighted
  //     option renders as "❯ 1." / "❯ 2)" etc. The permission-prompt text above
  //     only covered the y/n style, so a multi-option *question* (which has no
  //     "Do you want to" line) never tripped the fallback — its bell depended
  //     entirely on the Notification hook, and was missed whenever the hook
  //     didn't fire. `❯` + a digit + `.`/`)` is unique to claude's selector;
  //     a bare `❯` shell prompt is never followed by a digit-dot, and SGR/CSI
  //     codes are stripped before matching so colourised arrows still match.
  //     The leading \b guards ONLY the word-prefixed phrases below. The
  //     symbol-prefixed sigils ((y/n)/[y/N]/[Y/n]) MUST stay outside that \b
  //     group: they start with `(`/`[` (non-word) and are always preceded by a
  //     space, so a word boundary can never precede them — wrapping them under
  //     \b made those three branches DEAD (never matched). Keep them bare.
  var PROMPT_RE = /(?:\b(?:Do you want to (?:proceed|continue|allow|run|create|delete|overwrite|trust)\b|Allow Claude to |Continue\?)|\(y\/n\)|\[y\/N\]|\[Y\/n\]|❯[ \t]*\d+[.)])/i;
  function _bellNow() {
    return (typeof performance !== 'undefined' && performance.now)
      ? performance.now() : new Date().getTime();
  }
  function bellOnce() {
    // Any trigger consumes the pattern window. Without this, a sigil that
    // just rang stays inside the trailing 2 KB and every later chunk more
    // than BELL_DEDUP_MS apart re-matched it — repeat rings for a prompt
    // already answered, until 2 KB of fresh output finally pushed it out.
    // A NEW prompt re-draws its sigil, refilling the buffer, so nothing
    // real is lost.
    _patBuf = '';
    var now = _bellNow();
    if (now < _bellMuteUntil) return;                 // attach replay — stale content
    if (now - _bellAt < BELL_DEDUP_MS) return;
    _bellAt = now;
    postBell();
  }
  function maybePromptBell(decoded) {
    if (!decoded) return;
    scanBel(decoded);                                 // hook-BEL path, raw chunk (sync)
    _patBuf = (_patBuf + decoded).slice(-2048);       // keep last 2 KB
    // Strip CSI / OSC sequences so SGR colour codes don't break the regex.
    // Cheap regex strip — not a full VT parser, but good enough since the
    // prompt strings themselves contain no escape codes mid-word.
    var plain = _patBuf.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '')
                        .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '');
    if (PROMPT_RE.test(plain)) bellOnce();
  }
  // Synchronous BEL detection on the freshly decoded chunk — the hook-BEL
  // path. This replaces term.onBell: xterm parses its write queue via
  // setTimeout, which Chrome throttles in backgrounded tabs and in hidden
  // cross-origin iframes (a tile in a non-active dashboard tab) down to once
  // a MINUTE — so a hook bell parsed by xterm rang minutes late, or in a
  // burst when the tab was refocused. The websocket onmessage handler is
  // never throttled, so scanning the chunk here rings on time. BEL also
  // legitimately terminates OSC sequences (titles, IIP inline images) and can
  // sit inside other ESC-string payloads, so a tiny string-mode tracker skips
  // those: only a BARE \x07 rings. State persists across chunks (an OSC may
  // span websocket frames — IIP base64 images run to megabytes) and resets on
  // reconnect (bellOnConnect).
  var _strMode = 0;        // 0 plain · 1 OSC (BEL or ST ends it) · 2 DCS/SOS/PM/APC (ST only)
  var _afterEsc = false;   // previous char was a bare ESC (a sequence may span chunks)
  function scanBel(s) {
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      if (_afterEsc) {
        _afterEsc = false;
        if (_strMode) { if (c === 0x5c) _strMode = 0; }     // ESC \ (ST) ends any string
        else if (c === 0x5d) _strMode = 1;                  // ESC ] → OSC
        else if (c === 0x50 || c === 0x58 || c === 0x5e || c === 0x5f) _strMode = 2; // ESC P/X/^/_
        continue;
      }
      if (c === 0x1b) { _afterEsc = true; continue; }
      if (c === 0x18 || c === 0x1a) { _strMode = 0; continue; }  // CAN/SUB abort a string
      if (c === 0x07) {
        if (_strMode === 1) _strMode = 0;                   // OSC terminator — silent
        else if (!_strMode) bellOnce();                     // bare BEL — ring
      }
    }
  }
  // Called from socket.onopen: arm the attach-replay mute and drop the
  // cross-connect pattern/parse state — the reattach repaint re-sends content
  // the old buffer already saw, and a half-open ESC sequence from the dead
  // stream must not bleed into the new one.
  function bellOnConnect() {
    _bellMuteUntil = _bellNow() + BELL_CONNECT_MUTE_MS;
    _patBuf = '';
    _strMode = 0; _afterEsc = false;
  }
  // --- end prompt-detection bell --- (test_term_bell.js slices to this marker)

  term.onTitleChange(function (t) {
    document.title = t; postTitle(t);
    // Remember the real (program-emitted) title for THIS run so a reload can
    // show it immediately instead of falling back to the workdir basename.
    try { localStorage.setItem(LSTITLE, JSON.stringify({ ts: ts, title: t })); } catch (e) {}
  });
  // NOTE: deliberately NO term.onBell handler — BEL is detected synchronously
  // in scanBel (see above) so it can't be delayed by xterm's throttleable
  // write queue, and a second onBell ring for the same byte would re-fire
  // after the 600ms dedup window in a throttled frame.

  // On load, re-announce the last good title (if it belongs to this run) so the
  // dashboard tile shows the real name straight away — the program may not
  // re-emit its title on reattach.
  try {
    var st = JSON.parse(localStorage.getItem(LSTITLE) || 'null');
    if (st && st.ts === ts && st.title) { document.title = st.title; postTitle(st.title); }
  } catch (e) {}

  // When embedded in the dashboard, forward its global chords up to it and
  // swallow them here so they don't also reach the shell:
  //   Ctrl+Q  → close-session arm/confirm (was Ctrl+X — collided with nano)
  //   Cmd+E   → spawn a new terminal tile (was Cmd+T, but Chrome on macOS
  //             intercepts Cmd+T at the window-manager level and the page
  //             never sees the event — so we use Cmd+E which is free)
  // Capture phase so xterm doesn't get them first. Standalone, leave both alone.
  if (window.parent && window.parent !== window) {
    window.addEventListener('keydown', function (e) {
      if (e.ctrlKey && !e.metaKey && !e.altKey && (e.key === 'q' || e.key === 'Q')) {
        e.preventDefault(); e.stopPropagation();
        post({ key: 'ctrl-q' });
        return;
      }
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'e' || e.key === 'E')) {
        e.preventDefault(); e.stopPropagation();
        post({ key: 'cmd-e' });
        return;
      }
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 't' || e.key === 'T')) {
        e.preventDefault(); e.stopPropagation();
        post({ key: 'cmd-t' });
        return;
      }
      // Cmd+Shift+E (or Ctrl+Shift+E) — escape hatch when the renderer has
      // ghost glyphs / character overlap (Ctrl+O alt-buffer artifacts, WebGL
      // frame drops, post-resize stale cells). Forwards to the dashboard
      // which posts {cmd:'refresh'} back at the SELECTED tile (which may or
      // may not be this one — Cmd+Shift+E from anywhere refreshes the
      // currently-selected terminal, not necessarily the focused iframe).
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && (e.key === 'E' || e.key === 'e')) {
        e.preventDefault(); e.stopPropagation();
        post({ key: 'cmd-shift-e' });
        return;
      }
      // Cmd/Ctrl + ← / → → move THIS tile one slot in its tab. Forwarded to
      // the dashboard so it can splice the orderList. Safe to swallow here:
      // readline word-jump on macOS is Option+arrow, and terminal apps don't
      // generally see Cmd+arrow (the OS / browser eats it).
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
        e.preventDefault(); e.stopPropagation();
        post({ key: e.key === 'ArrowLeft' ? 'cmd-left' : 'cmd-right' });
        return;
      }
      // Ctrl+Tab / Ctrl+Shift+Tab → cycle to the next/prev tile. Swallow here so
      // the shell never sees it; bare Tab stays the app's completion key.
      if (e.ctrlKey && !e.metaKey && !e.altKey && e.key === 'Tab') {
        e.preventDefault(); e.stopPropagation();
        post({ key: e.shiftKey ? 'cycle-prev' : 'cycle-next' });
        return;
      }
      // Cmd/Ctrl+X → park (condense) THIS tile as a card, toggle on repeat.
      // Forwarded to the dashboard so it targets this sid. Safe to swallow:
      // terminals have no native "cut" (copy is Cmd+C / selection), so Cmd+X
      // over a terminal is otherwise a no-op.
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && (e.key === 'x' || e.key === 'X')) {
        e.preventDefault(); e.stopPropagation();
        post({ key: 'cmd-x' });
        return;
      }
    }, true);

    // The dashboard pins the tile row to its leftmost on reload and releases
    // that pin on the first genuine user gesture. A click / vertical scroll /
    // keypress INSIDE this cross-origin terminal never bubbles to the dashboard
    // window, so without forwarding a signal the row stays "locked to the left"
    // until the user happens to swipe horizontally (the only gesture already
    // forwarded, as wheel-x). Post a one-shot 'user-gesture' on the first
    // pointer/touch/key interaction so the pin lifts the moment the user
    // touches a tile. One-shot per page load (the iframe reloads with the
    // dashboard, re-arming it for the next reload's pin).
    var _gestureSent = false;
    function _signalUserGesture() {
      if (_gestureSent) return;
      _gestureSent = true;
      post({ key: 'user-gesture' });
    }
    window.addEventListener('pointerdown', _signalUserGesture, { capture: true, passive: true });
    window.addEventListener('touchstart', _signalUserGesture, { capture: true, passive: true });
    window.addEventListener('keydown', _signalUserGesture, { capture: true, passive: true });

    // Capture-phase, non-passive WINDOW wheel hook. Deliberate: it must see the
    // wheel BEFORE xterm's own viewport handler so it can claim an event a
    // mouse-tracking app would otherwise consume. (This xterm 5.3 build has no
    // attachCustomWheelEventHandler — added in 5.4 — so a window capture listener
    // is the only pre-xterm hook.) It handles two things:
    //
    // 1. HORIZONTAL swipe / shift+wheel → forward to the dashboard so it scrolls
    //    the tile ROW (cross-origin iframes don't bubble wheel to the parent;
    //    user-reported "can't h-scroll over an opencode tile"). A mouse-tracking
    //    app (newer claude / opencode) would otherwise eat the swipe, hence the
    //    capture phase.
    //
    // 2. VERTICAL wheel over a MOUSE-TRACKING tile → scroll xterm's OWN scrollback.
    //    With mouse tracking on (claude's fixed-prompt UI, opencode, other TUIs)
    //    xterm forwards each wheel notch to the app instead of pixel-scrolling its
    //    viewport, so a trackpad scroll goes from continuous to line-QUANTIZED
    //    (user: "we had smooth scrolling, doesn't work anymore" — the new claude
    //    UI is normal-screen but turns mouse tracking ON; the old one didn't).
    //    When the tile HAS scrollback to move, we claim the wheel and pixel-scroll
    //    the viewport ourselves, so the sub-row smooth-scroll transform applies
    //    and it glides exactly like a non-mouse tile (old AND new claude, and any
    //    normal-screen TUI). Left untouched so prior behavior stands: shift+wheel
    //    (the conventional "send the wheel to the program" modifier), tiles with
    //    mouse tracking OFF (xterm already scrolls its scrollback smoothly), and a
    //    viewport with nothing to scroll (a true alternate-screen full-screen TUI
    //    has no scrollback, so the wheel must still reach the app).
    //
    // Claimed events get preventDefault + stopPropagation so xterm / the app never
    // also sees them.
    window.addEventListener('wheel', function (e) {
      // --- 1. horizontal → dashboard row scroll ---
      var horiz = e.deltaX || (e.shiftKey ? e.deltaY : 0);
      if (horiz && (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY))) {
        var dx = horiz;
        // deltaMode: 0=pixel, 1=line, 2=page. Normalize lines/pages to a sensible
        // pixel count regardless of the input device's reporting style.
        if (e.deltaMode === 1) dx *= 16;
        else if (e.deltaMode === 2) dx *= window.innerWidth || 800;
        try { e.preventDefault(); e.stopPropagation(); } catch (_) {}
        post({ key: 'wheel-x', dx: dx });
        return;
      }
      // --- 1b. while parked (condensed): vertical wheel → horizontal row scroll ---
      // The card is a sliver; scrolling its hidden scrollback is pointless, so a
      // plain vertical wheel fans the deck instead. Shift+wheel already went to
      // the row above; bare deltaY gets forwarded as wheel-x here.
      if (condensed && e.deltaY && !e.shiftKey) {
        var cdx = e.deltaY;
        if (e.deltaMode === 1) cdx *= 16;
        else if (e.deltaMode === 2) cdx *= window.innerWidth || 800;
        try { e.preventDefault(); e.stopPropagation(); } catch (_) {}
        post({ key: 'wheel-x', dx: cdx });
        return;
      }
      // --- 2. vertical over a mouse-tracking tile → smooth-scroll xterm scrollback ---
      if (e.shiftKey || !e.deltaY) return;            // shift+wheel goes to the app
      var mouseOn = false;
      try { mouseOn = !!term._core.coreMouseService.areMouseEventsActive; } catch (_) {}
      if (!mouseOn) return;                           // mouse off → xterm already scrolls smoothly
      var vp = term.element && term.element.querySelector('.xterm-viewport');
      if (!vp || vp.scrollHeight - vp.clientHeight <= 1) return;   // nothing to scroll → leave to the app
      var dy = e.deltaY;
      if (e.deltaMode === 1) dy *= 16;
      else if (e.deltaMode === 2) dy *= vp.clientHeight || 600;
      try { e.preventDefault(); e.stopPropagation(); } catch (_) {}
      vp.scrollTop += dy;                             // fires viewport 'scroll' → xterm repaint + sub-row transform
    }, { capture: true, passive: false });

    // The dashboard tells us when this terminal is scrolled off-screen / hidden
    // so we can gate stdin (see inputEnabled / sendInput) without losing focus.
    // It also sends {cmd:'refresh'} for the user-triggered re-layout chord.
    window.addEventListener('message', function (e) {
      // The dashboard is on http://127.0.0.1:<port> when this tile is embedded
      // directly, or on our own origin when we're served through its
      // /t/<port>/ reverse proxy (same-origin in that mode).
      if (typeof e.origin !== 'string' ||
          (e.origin.indexOf('http://127.0.0.1:') !== 0 && e.origin !== location.origin)) return;
      var d = e.data;
      if (!d || d.type !== 'claude-host') return;
      if (d.cmd === 'input') { inputEnabled = !!d.enabled; setRendererVisible(!!d.enabled); }
      else if (d.cmd === 'condensed') { setCondensedState(!!d.on); }   // freeze + drop GL; wheel → row H-scroll
      else if (d.cmd === 'peek') { setPeek(!!d.on); }                  // hover over a parked card → drop overlay → live canvas
      else if (d.cmd === 'font') { applyFont(d.font); }
      else if (d.cmd === 'theme') { applyTermTheme(d.theme); }
      else if (d.cmd === 'csrf') {
        // Dashboard pushes its CSRF token + we capture its origin from the
        // postMessage event itself (e.origin) so drag/paste uploads (POST
        // /api/dropfile) can hit the dashboard from this cross-origin iframe.
        //
        // Origin gating: the outer router already rejected non-127.0.0.1:*
        // senders, but we tighten further here:
        //   - first csrf message: PIN _dashboardOrigin to that exact origin.
        //   - later csrf messages: only accept if e.origin matches the pinned
        //     value. Prevents a lateral attack where some OTHER service on
        //     localhost (a CTF target, dev server, …) could overwrite our
        //     token mid-session. Token + origin bind atomically — never store
        //     d.token unless the source matches the (newly or already)
        //     pinned dashboard origin.
        if (typeof e.origin !== 'string' ||
            (e.origin.indexOf('http://127.0.0.1:') !== 0 && e.origin !== location.origin)) return;
        if (_dashboardOrigin && e.origin !== _dashboardOrigin) return;
        if (typeof d.token === 'string') {
          _csrfToken = d.token;
          _dashboardOrigin = e.origin;
        }
      }
      else if (d.cmd === 'refresh') {
        // Recovery for a wedged tile, without touching scrollback. Steps:
        //   1. fit.fit() — re-measure container, sync cols/rows
        //                   (PTY gets SIGWINCH only on a real size change;
        //                   app redraws fresh content)
        //   2. _healAfterRendererSwap — recompute the renderer's canvas/
        //                   framebuffer dims from a fresh cell measurement and
        //                   re-blit EVERY row, driven straight through the
        //                   render service so it does NOT fire term.onResize.
        //                   We used to perturb the grid with a +1 / restore
        //                   resize pair here, but that fires two SIGWINCHes: a TUI like
        //                   claude (Ink) re-renders its frame twice at two
        //                   widths, and a miscounted frame-erase leaves the
        //                   SAME output duplicated with mismatched wrapping
        //                   (the dup b30d3cb removed from the renderer-swap
        //                   path — same hazard, so we avoid it here too).
        //   3. term.refresh — belt-and-suspenders repaint in case cols/rows
        //                   were 0 before fit.fit() set them.
        //   4. swap renderer — dispose the current GPU/canvas context and
        //                   re-attach. Texture-atlas corruption or partial
        //                   GL context loss is the usual cause of "tile
        //                   stays gray despite live writes"; a fresh
        //                   context paints the buffer from scratch.
        //                   _healAfterRendererSwap fires on each useWebgl/
        //                   useCanvas so repaint timing is right.
        // We deliberately do NOT close the WebSocket here. ttyd's reattach
        // replay starts with a terminal-init sequence (RIS / \x1bc / clear-
        // scrollback) and that DESTROYS our in-memory scrollback — the
        // user-reported "scrollback again gone" was traced to that. The
        // user-discovered "open external view → close" recovery works
        // without a reconnect because the external tab triggers fresh PTY
        // output that the original tile's still-live socket also picks up
        // (or because of side-effects we don't fully control). If steps
        // 1-4 aren't enough, the existing escape valve is still there:
        // user can manually reload the tile (scrollback restores from
        // localStorage) or close-and-reopen.
        try { fit.fit(); } catch (e2) {}
        try { _healAfterRendererSwap(); } catch (e2) {}
        try { term.refresh(0, (term.rows || 1) - 1); } catch (e2) {}
        try {
          if (webglAddon) { useCanvas(); useWebgl(); }
          else if (canvasAddon) { useWebgl(); useCanvas(); }
        } catch (e2) {}
        // Final touch: fire a synthetic window 'resize' so any listener
        // we don't own (xterm.js internals, addons) gets the same kick
        // a real browser-window resize would deliver. Our own
        // `window.addEventListener('resize', …)` also re-runs fit.fit(),
        // which is harmless after the explicit fit above.
        try { window.dispatchEvent(new Event('resize')); } catch (e2) {}
      }
      else if (d.cmd === 'repaint') {
        // Dashboard asks for a program repaint (e.g. terminal re-revealed
        // after the chat-panel overlay) — pty-only, no layout change.
        _repaintWiggle();
      }
      else if (d.cmd === 'clear-scrollback') {
        // "Clean reload" (Shift+click the ↻ button): forget THIS session's saved
        // scrollback so the imminent reload restores nothing and the buffer is
        // rebuilt purely from ttyd's reattach replay. Also drop canPersist so the
        // about:blank-bounce pagehide can't re-save the (possibly duped) buffer
        // before we reload. The dashboard reloads the iframe right after.
        canPersist = false;
        try { localStorage.removeItem(LSKEY_V2); localStorage.removeItem(LSKEY_V3); } catch (e2) {}
        try { localStorage.removeItem(IMG_META_KEY); } catch (e2) {}
        try { localStorage.removeItem(IMG_OOB_META_KEY); localStorage.removeItem(IMG_OOB_CURSOR_KEY); } catch (e2) {}
        // OOB bytes aren't evicted on marker-dispose (the marker dies routinely on a
        // claude tile but the image must survive for restore), so a clean reload
        // sweeps them here instead.
        try { for (var _oi = 0; _oi < _oobMarks.length; _oi++) _idbDel(_oobKeyPrefix + _oobMarks[_oi].seq); } catch (e2) {}
      }
    });
    // Announce we're up so the dashboard pushes our current gate state, covering
    // the case where we loaded already scrolled out of view.
    post({ ready: true });
  }

  // --- persistence ---
  // Sweep older runs of THIS session in BOTH v2 (legacy) and v3 (current)
  // namespaces, plus any pre-v2 corrupted blobs. Scoped by LSPREFIX_V*
  // (sid-bound) so we never delete OTHER sessions' scrollback — that bug
  // wiped every other tile's storage every 15s.
  function _sweepOldKeys(currentKey) {
    try {
      for (var i = localStorage.length - 1; i >= 0; i--) {
        var k = localStorage.key(i);
        if (!k || k === currentKey || k === LSSIZE) continue;
        if (k.indexOf(LSPREFIX_V2) === 0 || k.indexOf(LSPREFIX_V3) === 0
            || k.indexOf(LSPREFIX_SIZE) === 0) {
          localStorage.removeItem(k);
        } else if (k.indexOf('claude-term-scrollback:') === 0
                && k.indexOf('claude-term-scrollback:v2:') !== 0
                && k.indexOf('claude-term-scrollback:v3:') !== 0) {
          // Pre-v2 corrupted blob from any sid — safe to drop.
          localStorage.removeItem(k);
        }
      }
    } catch (e) {}
  }
  function _quotaExceeded(e) {
    // Browsers report quota overruns differently (DOMException codes 22 / 1014,
    // various names). Check broadly.
    return e && (e.name === 'QuotaExceededError' || e.code === 22 || e.code === 1014
              || /quota/i.test(String(e && e.message)));
  }
  // Persisting is async because gzip is async. Re-entrancy guard: if a tick
  // is still finishing when the next one starts, skip — the next 15 s tick
  // will cover it. (Persist on visibilitychange + interval; we deliberately
  // do NOT subscribe to `beforeunload` for the async path — browsers kill
  // pending promises on unload. Stale-by-≤15s is acceptable.)
  var persisting = false;
  var _lastPersistDataAt = 0;   // __lastTermDataAt captured at the last serialize
  function persist() {
    if (persisting || !canPersist) return;   // never overwrite a good snapshot from a view that skipped restore
    // Dirty check: serializer.serialize() walks the WHOLE buffer and is the
    // single biggest synchronous main-thread stall a tile has (measured 50–100 ms
    // for a full scrollback — and it grows with how much you've pasted). The
    // dashboard runs every tile in ONE renderer thread, so an unconditional 15 s
    // tick had all tiles re-serializing UNCHANGED buffers in lockstep — the
    // user-reported "terminal freezes when pasting a lot of lines" (the paste
    // fattens the buffer that serialize then walks, every 15 s, on every tile).
    // _writeAndScan stamps __lastTermDataAt on every byte of real output; if it
    // hasn't advanced since our last save the stored snapshot is already current,
    // so skip the whole serialize+gzip. Idle / background tiles (the common case)
    // then cost ~nothing per tick instead of a full re-serialize.
    var dataAt = window.__lastTermDataAt || 0;
    if (dataAt === _lastPersistDataAt) return;
    _lastPersistDataAt = dataAt;
    // Record the size this snapshot was rendered at (cheap sync write; same
    // canPersist gate as the snapshot itself, so only the authoritative view
    // writes it). Read back by _applySavedSize() on a hidden connect — both the
    // per-session key and the shared default (every row tile is the same width).
    try { localStorage.setItem(LSSIZE, term.cols + 'x' + term.rows); } catch (e) {}
    try { localStorage.setItem(LSSIZE_DEFAULT, term.cols + 'x' + term.rows); } catch (e) {}
    persisting = true;
    try { _persistImageMeta(); } catch (e) {}   // image anchors alongside the scrollback snapshot
    try { _persistOobMeta(); } catch (e) {}     // out-of-band image anchors too
    Promise.resolve().then(function () {
      if (!_gzipAvailable()) {
        // Ancient browser: write uncompressed v2 so we still have SOME save.
        try {
          var v2 = serializer.serialize({ scrollback: MAX_LINES });
          if (v2) { localStorage.setItem(LSKEY_V2, v2); _sweepOldKeys(LSKEY_V2); }
        } catch (e) {}
        return;
      }
      var attempts = [MAX_LINES].concat(QUOTA_RETRY_LINES);
      function attempt(idx) {
        if (idx >= attempts.length) return;
        var raw = serializer.serialize({ scrollback: attempts[idx] });
        if (!raw) return;   // empty buffer — don't clobber the stored snapshot
        return gzipToString(raw).then(function (gz) {
          try {
            localStorage.setItem(LSKEY_V3, gz);
            _sweepOldKeys(LSKEY_V3);
          } catch (e) {
            if (_quotaExceeded(e)) return attempt(idx + 1);
            // Other errors: give up silently — try again next tick.
          }
        });
      }
      return attempt(0);
    }).then(function () { persisting = false; }, function () { persisting = false; });
  }
  document.addEventListener('visibilitychange', function () { if (document.hidden) persist(); });
  // Periodic safety save. The dirty-check above already skips unchanged tiles,
  // but tiles that ARE continuously busy would still tick in lockstep if they
  // booted together — stacking their serialize stalls on one frame. A small
  // random phase offset on the first tick decorrelates them so the cost spreads
  // across frames instead of landing all at once.
  setTimeout(function () {
    persist();
    setInterval(persist, 15000);
  }, 15000 + Math.floor(Math.random() * 5000));
  // Sync v2 save on pagehide: persist() above is async (gzip is a Promise) so
  // a quick Cmd+R between ticks lets the browser kill the in-flight write,
  // leaving the last localStorage snapshot 0-15s stale — user-reported as
  // "scroll buffers gone after reload". This sync write costs the gzip ratio
  // (v2 is uncompressed) but lands BEFORE the page unloads, so on the next
  // load restoreScrollback() falls back to v2 if v3 is older or missing.
  // pagehide is the cross-browser unload hook that respects BFCache; the
  // browser is guaranteed to run sync work in it before tearing down.
  window.addEventListener('pagehide', function () {
    if (!canPersist) return;   // skipped-restore view: its empty buffer must not clobber the snapshot
    try { localStorage.setItem(LSSIZE, term.cols + 'x' + term.rows); } catch (e) {}
    try { localStorage.setItem(LSSIZE_DEFAULT, term.cols + 'x' + term.rows); } catch (e) {}
    try {
      // Sync serialize can be large (uncompressed). Walk the same quota ladder
      // as persist() so a big buffer still lands SOME fresh snapshot instead of
      // throwing and saving nothing. Bail on an empty buffer — overwriting a
      // good snapshot with "" is exactly the data-loss we're guarding against.
      var attempts = [MAX_LINES].concat(QUOTA_RETRY_LINES), wrote = false;
      for (var i = 0; i < attempts.length; i++) {
        var raw = serializer.serialize({ scrollback: attempts[i] });
        if (!raw) return;                                   // empty → keep existing v2/v3 untouched
        try { localStorage.setItem(LSKEY_V2, raw); wrote = true; break; }
        catch (e) { if (!_quotaExceeded(e)) return; /* else: retry a smaller window */ }
      }
      // Only NOW drop the gzipped v3 — restoreScrollback prefers v3, and we want
      // it to fall through to the fresh v2 we just wrote. If every write failed
      // (quota even at the smallest window), keep v3: a ≤15s-stale gzipped copy
      // beats no copy at all. The next 15s tick on the new page repopulates v3.
      if (wrote) localStorage.removeItem(LSKEY_V3);
      try { _persistImageMeta(); } catch (e) {}   // keep image anchors in sync with this final save
      try { _persistOobMeta(); } catch (e) {}     // and the out-of-band image anchors
    } catch (e) {}
  });

  // --- ttyd protocol ---
  // server->client first byte: '0' output, '1' set-title, '2' set-prefs.
  // client->server first byte: '0' input, '1' resize(JSON), '2' pause, '3' resume.
  var socket = null, token = '', reconnects = 0, MAX_RECONNECT = 30, reconnectTimer = null;

  // First-replay classification (per page load, NOT reset on reconnect). ttyd's
  // initial burst tells us how to avoid duplicating scrollback:
  //   • A TUI program (claude, vim, …) repaints from a cleared screen on attach
  //     — its first burst leads with a cursor-home / erase-display / alt-screen
  //     sequence (after any title/colour preamble). That repaint OVERWRITES the
  //     viewport, so the restored scrollback sitting ABOVE it is safe. → restore.
  //   • A plain shell has ttyd re-dump its entire scrollback as bare text. The
  //     dump APPENDS, so restoring the same history on top of it duplicates the
  //     buffer — and it compounds on every reload (200→400→600 lines). → do NOT
  //     restore; ttyd's replay is already the full history.
  // We default to restore (TUI) whenever sized, and only skip it when the burst
  // is positively a bare-text dump, so a claude tile can never lose history.
  var firstBurstHandled = false, queuingForRestore = false, queuedOutput = [];
  // A repaint burst leads with a cursor-home / erase-display / alt-screen /
  // RIS. Erase-display covers ED 0–3: `[3J` (clear scrollback) is included so a
  // reattach that opens by wiping scrollback still classifies as a TUI repaint
  // and triggers restore (the wipe itself is then neutralized — see
  // _stripScrollbackClear).
  var TUI_LEAD_RE = /^\x1b(?:\[[0-9;]*[Hf]|\[[0-3]?J|\[\?(?:1049|1047|47)[hl]|c)/;
  // Neutralize the scrollback-erasing sequences in a reattach repaint so they
  // can't wipe the history we just restored from localStorage. On reattach,
  // claude (and other TUIs) repaint the visible screen — fine, restored history
  // lives ABOVE it in scrollback — but the repaint can also carry `ESC[3J`
  // (erase scrollback) or `ESC c` (RIS, a full reset that also clears
  // scrollback). Either one erases the restore and the tile comes up blank
  // ("scrollback gone again, even ↻ reload doesn't bring it back"). We drop
  // `ESC[3J` outright and rewrite `ESC c` → `ESC[2J ESC[H` (clear screen + home,
  // scrollback kept): claude re-establishes its modes/SGR in the same repaint,
  // so the rest of RIS's reset is immediately replaced anyway. Applied ONLY to
  // the burst(s) that arrive during the restore window (the reattach repaint),
  // so a mid-session `clear`/`tput reset` later still behaves normally.
  function _stripScrollbackClear(payload) {
    var has = false;
    for (var i = 0; i < payload.length; i++) { if (payload[i] === 0x1b) { has = true; break; } }
    if (!has) return payload;
    var out = [];
    for (var j = 0; j < payload.length; j++) {
      // ESC [ 3 J  → drop (erase scrollback)
      if (payload[j] === 0x1b && payload[j + 1] === 0x5b && payload[j + 2] === 0x33 && payload[j + 3] === 0x4a) {
        j += 3; continue;
      }
      // ESC c (RIS) → ESC [ 2 J  ESC [ H  (clear screen + home, keep scrollback)
      if (payload[j] === 0x1b && payload[j + 1] === 0x63) {
        out.push(0x1b, 0x5b, 0x32, 0x4a, 0x1b, 0x5b, 0x48); j += 1; continue;
      }
      out.push(payload[j]);
    }
    return new Uint8Array(out);
  }
  function _looksLikeTuiRepaint(payload) {
    try {
      // Decode generously (512B, not 128B): a TUI may emit a long title OSC
      // (deeply-nested cwd) + SGR colour setup BEFORE its first clear sequence.
      // If that preamble overruns the window, `head` truncates mid-sequence,
      // TUI_LEAD_RE misses, restore is skipped, and the session silently loses
      // its scrollback on every reload. 512B stays well clear of any real
      // preamble while remaining negligible to decode.
      var head = dec.decode(payload.subarray(0, Math.min(payload.length, 512)));
      // Strip a leading OSC (title) / SGR (colour) / whitespace preamble — a TUI
      // may set its title or colours before the first clear, and that mustn't
      // disqualify it. What remains must START with a home/clear/alt-screen.
      head = head.replace(/^(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;]*m|[\r\n\s])+/, '');
      return TUI_LEAD_RE.test(head);
    } catch (e) { return false; }
  }
  function _writeAndScan(payload) {
    // Observe IIP image sequences (anchor = cursor row BEFORE this write), then
    // write unchanged so the image addon renders normally.
    try { var _b = term.buffer.active; _scanForIIP(payload, _b.baseY + _b.cursorY); } catch (e) {}
    window.__lastTermDataAt = Date.now();   // activity clock for inline-image quiescence
    term.write(payload);
    // Scan the freshly arrived chunk for claude prompt sigils as the fallback
    // bell-trigger (see PROMPT_RE in maybePromptBell). The claude Notification
    // hook is the cleaner signal when configured, but this catches the case
    // where the user hasn't set it up.
    try { maybePromptBell(outDec.decode(payload, { stream: true })); } catch (e) {}
  }
  function writeOutput(payload) {
    if (!firstBurstHandled) {
      firstBurstHandled = true;
      if (mayRestore && _looksLikeTuiRepaint(payload)) {
        // TUI: restore history first, queue live output until the (async gzip)
        // restore lands, then flush — so history sits above the live repaint.
        queuingForRestore = true;
        queuedOutput.push(payload);
        restoreScrollback().then(function () {
          canPersist = true;            // we restored → authoritative view, safe to persist
          queuingForRestore = false;
          var q = queuedOutput; queuedOutput = [];
          // Merge the bursts that arrived during the (async) restore — together
          // they ARE the reattach repaint — into one blob, then neutralize any
          // scrollback-erase before writing. Merging first means an `ESC[3J`
          // split across two WS frames can't slip through a per-frame scan.
          var total = 0, k;
          for (k = 0; k < q.length; k++) total += q[k].length;
          var merged = new Uint8Array(total), off = 0;
          for (k = 0; k < q.length; k++) { merged.set(q[k], off); off += q[k].length; }
          _writeAndScan(_stripScrollbackClear(merged));
          // The restored history + reattach repaint are now QUEUED into the buffer.
          // This is the authoritative "content has landed" moment — unlike the
          // fixed 700/1400 ms timer passes, it can't fire before the (async-gzip)
          // restore completes. On a heavy dashboard reload the GL context that
          // term.write's own render targets may still be cold/contended under the
          // page's WebGL-context cap, so the content paints gray; force a
          // render-only re-blit once the write drains (term.write('', cb) runs cb
          // after the parser flushes the merged write). No term.resize → no
          // SIGWINCH → no Ink reflow/dup; scrollback untouched. If the reload
          // silently lost our GL context, _burstHeal can't repaint a dead context —
          // recover to canvas first (same check the timer passes use).
          try {
            term.write('', function () { _diag.reblits++; if (_recoverLostGl()) return; try { _burstHeal(); } catch (e) {} });
          } catch (e) { _diag.reblits++; if (!_recoverLostGl()) { try { _burstHeal(); } catch (e2) {} } }
        });
        return;
      }
      // Bare-text dump (shell) or restore not allowed (hidden tile): ttyd's
      // replay IS the history. Don't restore (would duplicate it) and leave
      // canPersist false (a saved copy restore would never read is pointless).
      _writeAndScan(payload);
      return;
    }
    if (queuingForRestore) { queuedOutput.push(payload); return; }
    _writeAndScan(payload);
  }

  function sendInput(data) {
    if (!inputEnabled) return;   // off-screen/hidden per dashboard — drop typing so it never reaches the PTY
    if (!socket || socket.readyState !== 1) return;
    var bytes = enc.encode(data);
    var msg = new Uint8Array(bytes.length + 1);
    msg[0] = 48; // '0'
    msg.set(bytes, 1);
    socket.send(msg);
  }
  function sendResize() {
    if (!socket || socket.readyState !== 1) return;
    _diag.ptyResizes++;
    socket.send(enc.encode('1' + JSON.stringify({ columns: term.cols, rows: term.rows })));
  }
  // Timestamp (ms) of the most recent program output, and the wiggle's retry
  // count for the current open. Gates _repaintWiggle: what matters is whether
  // output is STILL flowing at wiggle time, not whether ANY arrived — the
  // dtach attach-replay always emits a burst, even for an idle program.
  var _lastOutputTs = 0;
  var _wiggleTries = 0;
  // How long output must be silent before the wiggle is allowed to fire. Long
  // enough that the program has flushed its current frame (so a SIGWINCH
  // redraws a settled screen, not a half-written one), short enough that an
  // idle reattach repaints promptly.
  var WIGGLE_QUIET_MS = 250;
  // Repaint trigger for a freshly-attached dtach session. claude is an Ink
  // TUI and only repaints on an actual size CHANGE, so attaching at the same
  // size used to come up blank. The dashboard's old fix bounced the IFRAME
  // height (-48px and back, twice, on load) — user-visible as the tile
  // content jumping up and down for ~1s. This wiggles ONE ROW instead.
  //
  // Two hard-won constraints:
  //  - The grid and the pty must resize TOGETHER (term.resize drives both via
  //    onResize → sendResize). A first cut reported rows-1 to the pty only,
  //    leaving xterm at rows: Ink then erased/positioned its frame against a
  //    screen one row shorter than the real grid, and the miscounted erase
  //    DUPLICATED in-flight output (user screenshot: the same Bash lines
  //    twice). Consistency is what the old iframe bounce was implicitly
  //    buying; keep it, just one row instead of 2-3.
  //  - Fire only once output has gone QUIET, never while it is still streaming:
  //    a busy claude is painting anyway (no blank to fix) and resizing
  //    mid-frame is the duplication hazard. But the dtach attach-replay emits a
  //    burst on EVERY reattach — including for an idle program — so a boolean
  //    "any output arrived since open" gate skipped the wiggle exactly when it
  //    was needed and the tile came up blank until the user typed. Instead we
  //    wait for WIGGLE_QUIET_MS of silence after the last output: the
  //    replay burst settles, then we wiggle the idle frame back; a genuinely
  //    busy program never goes quiet and we stand down after a few retries.
  //  - ONLY when the screen is actually blank. The size-change trick makes a
  //    bottom-anchored TUI (claude's status line + input box + footer) redraw
  //    across the one-row delta, and if content was ALREADY on screen the old
  //    copy is left orphaned — user-reported "stacked input boxes / doubled
  //    status line" after a reattach that replayed fine. So when a reattach
  //    already shows content, do nothing; only an empty viewport gets wiggled.
  //  - Rows-only on purpose: width changes make Ink re-wrap its frame (the
  //    b30d3cb dup hazard noted in the 'refresh' handler).
  function _viewportLooksBlank() {
    // True only if EVERY visible row is empty. On any uncertainty (API shape,
    // exception) return false: a needless wiggle risks the duplication above,
    // whereas a missed wiggle merely leaves a blank that the first keystroke
    // (a real SIGWINCH-free redraw) clears anyway — so bias to "not blank".
    try {
      var b = term.buffer.active;
      var top = b.viewportY;
      if (typeof top !== 'number') return false;
      for (var i = 0; i < term.rows; i++) {
        var ln = b.getLine(top + i);
        if (ln && ln.translateToString(true).trim() !== '') return false;
      }
      return true;
    } catch (e) { return false; }
  }
  function _repaintWiggle() {
    if (!socket || socket.readyState !== 1 || term.rows < 5) return;
    // Still actively painting? Wait for it to settle. If it never does (a
    // long-running stream), it isn't blank — stand down after ~3s of retries.
    if (_lastOutputTs && (Date.now() - _lastOutputTs) < WIGGLE_QUIET_MS) {
      if (_wiggleTries++ < 12) setTimeout(_repaintWiggle, WIGGLE_QUIET_MS);
      return;
    }
    // Content already on screen → nothing to fix, and the size change would
    // duplicate a bottom-anchored UI. Only repaint a genuinely blank viewport.
    if (!_viewportLooksBlank()) return;
    var c = term.cols, r = term.rows;
    try { term.resize(c, r - 1); } catch (e) { return; }
    setTimeout(function () {
      // Restore only if nothing else (a real fit) already changed the size.
      try { if (term.cols === c && term.rows === r - 1) term.resize(c, r); } catch (e) {}
    }, 150);
  }
  // Re-blit the buffer onto the renderer after a reattach, curing the
  // "buffer-full-but-unpainted" tile (restore + reattach repaint wrote real
  // content, but the WebGL context wasn't ready to paint it — common on a
  // dashboard reload where many tiles contend for GL contexts). Unlike
  // _repaintWiggle this is NOT gated on _viewportLooksBlank: the whole point is
  // a NON-empty buffer that didn't reach the screen, which the blank-check
  // would (correctly, for its own purpose) skip. _burstHeal is renderer-only
  // (clearTextureAtlas + refresh, no term.resize), so it's safe to run even
  // mid-stream — no SIGWINCH, no Ink reflow, no duplication, scrollback intact.
  // Called by the two scheduled passes (700 / 1400 ms after socket.onopen); both
  // run — see below for why the old per-open latch was the gray-on-reload bug.
  function _reblitAfterReattach() {
    if (!socket || socket.readyState !== 1) return;
    // NO early-return latch here. The two scheduled passes (700 ms + 1400 ms)
    // must BOTH heal: on a heavy dashboard reload (15+ tiles all gunzipping their
    // restore + contending for WebGL contexts) the async restore can land AFTER
    // the 700 ms pass, so the first heal repaints an EMPTY buffer. A latch that
    // fired on the first pass (the old `_reblitDone` guard) then suppressed the
    // 1400 ms pass — the content landed unpainted and the tile stayed gray until
    // a manual ↻ / keystroke, defeating the very "two passes straddle a slow
    // gunzip" intent the comment claimed. _burstHeal is render-only (no
    // term.resize → no SIGWINCH → no Ink reflow/dup), so a second pass over an
    // already-good frame is a harmless idempotent repaint.
    _diag.reblits++; _diag.reblitPasses++;
    // If this reload silently lost our GL context (no contextlost event — common
    // when every tile re-acquires a context at once and the page is over the
    // per-page cap), a render-only _burstHeal repaints onto a dead context and the
    // tile stays blank. Recover to canvas first; otherwise just re-blit.
    if (_recoverLostGl()) return;
    try { _burstHeal(); } catch (e) {}
  }
  // Switch the terminal face/size/weight. Mutates xterm.options live and re-fits
  // so cols/rows reflow to the new cell metrics (different glyphs → different
  // cell width → different column count for the same iframe pixel width). The
  // PTY learns the new size via term.onResize → sendResize → SIGWINCH. We also
  // persist the entry so a tile reload keeps the chosen font on cold-boot
  // (avoids a brief flash of JBM before the dashboard pushes it via postMessage).
  function applyFont(entry) {
    if (!entry || typeof entry.family !== 'string') return;
    var family = "'" + entry.family + "'" + SYS_MONO_CHAIN;
    var size = (typeof entry.size === 'number' && entry.size > 0) ? entry.size : 13;
    var weight = entry.weight || 'normal';
    // xterm lineHeight is a multiplier of the font's natural cell height (1.0 =
    // default). Auto / unset → 1.0. Persisted with the font so a cold-boot tile
    // comes up at the same spacing (see the Terminal() boot above).
    var lineHeight = (typeof entry.lineHeight === 'number' && entry.lineHeight > 0) ? entry.lineHeight : 1.0;
    try { term.options.fontFamily = family; } catch (e) {}
    try { term.options.fontSize = size; } catch (e) {}
    try { term.options.fontWeight = weight; } catch (e) {}
    try { term.options.lineHeight = lineHeight; } catch (e) {}
    try { fit.fit(); } catch (e) {}
    try { localStorage.setItem(LSFONT, JSON.stringify({ family: entry.family, size: size, weight: weight, lineHeight: lineHeight })); } catch (e) {}
  }

  function applyPrefs(prefs) {
    ['fontSize', 'fontFamily', 'scrollback', 'cursorBlink', 'cursorStyle', 'theme'].forEach(function (k) {
      if (prefs && k in prefs) { try { term.options[k] = prefs[k]; } catch (e) {} }
    });
    try { fit.fit(); } catch (e) {}
  }

  function connect() {
    var proto = location.protocol === 'https:' ? 'wss' : 'ws';
    // Resolve /ws relative to whatever path THIS page is served under. Served
    // directly by ttyd the page is at '/', so this is '/ws'. Served through the
    // dashboard's reverse proxy it's at '/t/<port>/', so this becomes
    // '/t/<port>/ws' — the proxy then strips the prefix back to ttyd's '/ws'.
    var base = location.pathname.replace(/[^/]*$/, '');
    socket = new WebSocket(proto + '://' + location.host + base + 'ws', ['tty']);
    socket.binaryType = 'arraybuffer';
    socket.onopen = function () {
      reconnects = 0;
      _setStatus(null);   // clear any reconnect/disconnect banner — overlay only, never the buffer
      try { outDec.decode(); } catch (e) {}   // flush/reset any dangling partial byte from the old stream
      bellOnConnect();    // mute the attach replay + drop cross-connect bell state
      _lastOutputTs = 0; _wiggleTries = 0;
      socket.send(enc.encode(JSON.stringify({ AuthToken: token, columns: term.cols, rows: term.rows })));
      // Force the Ink repaint after the dtach attach settles — but only if the
      // program turns out to be idle (no output by then; see _repaintWiggle).
      // Running on every open also covers reconnects (laptop wake, server
      // restart) — paths the old iframe-load nudge never reached.
      setTimeout(_repaintWiggle, 600);
      // Re-blit the buffer onto the GPU renderer once the reattach burst has
      // landed. The boot-time _burstHeal (in useWebgl) ran on an EMPTY buffer —
      // before the async-gzip restore + reattach repaint wrote anything — so on
      // a dashboard reload, where 15+ tiles all (re)acquire a WebGL context at
      // once and some are still warming / get evicted under the page's GL cap
      // right as the restore writes land, the restored history paints onto a
      // not-yet-ready context and shows blank. The buffer is FULL, so the
      // _repaintWiggle blank-check can't see it and won't fire; the tile sits
      // unpainted until a manual ↻. A renderer-only re-blit fixes it: no
      // term.resize → no SIGWINCH → no Ink reflow/dup, scrollback untouched
      // (same safe heal the renderer-swap and un-hide paths use). Two passes
      // straddle a slow gunzip so a large restored blob that lands after the
      // first pass still gets repainted by the second — BOTH passes heal now
      // (see _reblitAfterReattach; the old per-open latch wrongly suppressed the
      // second pass, which was the gray-on-reload bug). The TUI restore path also
      // heals the instant its content actually lands (see writeOutput), so this
      // timer pair is the backstop for the shell/bare-text path and for renderer
      // warmup after the write.
      setTimeout(_reblitAfterReattach, 700);
      setTimeout(_reblitAfterReattach, 1400);
    };
    socket.onmessage = function (ev) {
      var buf = new Uint8Array(ev.data);
      if (!buf.length) return;
      var cmd = String.fromCharCode(buf[0]);
      var payload = buf.subarray(1);
      if (cmd === '0') {
        _lastOutputTs = Date.now();   // program is painting — _repaintWiggle waits for quiet
        writeOutput(payload);
        _scanSuggestionSoon();   // refresh the chat-panel's ghost suggestion after each redraw
      }
      // cmd '1' is ttyd's own SET_WINDOW_TITLE — the launch command
      // ("dtach … claude"), not the program's title. Set our local tab title but
      // DON'T propagate it to the dashboard: it would clobber the tile name with
      // "dtach …" on every (re)connect. The real title arrives as OSC bytes in
      // the output stream and fires term.onTitleChange (above).
      else if (cmd === '1') { document.title = dec.decode(payload); }
      else if (cmd === '2') { try { applyPrefs(JSON.parse(dec.decode(payload))); } catch (e) {} }
    };
    socket.onclose = function () {
      if (reconnects >= MAX_RECONNECT) { _setStatus('disconnected', 'error'); return; }
      if (reconnectTimer) return;
      reconnects++;
      _setStatus('reconnecting…');
      reconnectTimer = setTimeout(function () { reconnectTimer = null; connect(); }, 1500);
    };
    socket.onerror = function () { try { socket.close(); } catch (e) {} };
  }

  term.onData(sendInput);
  term.onResize(sendResize);
  // A tile in an inactive tab is display:none → its iframe viewport is 0×0. xterm's
  // GPU/canvas renderer caches its dimensions (and the GL context can be reclaimed
  // under the page's WebGL-context cap) while detached from layout, so the first
  // paint after the tab is shown again ghosts / duplicates rows from the bottom up
  // — the user-reported "terminals duplicate content on tab switch, a refresh hotkey
  // fixes it". fit.fit() alone can't cure it: cols/rows don't change across the hide
  // so it no-ops. Detect the 0→visible transition (the iframe viewport snapping back
  // from 0, which fires a resize inside the frame) and run the same no-SIGWINCH heal
  // the renderer swap uses to re-blit the whole buffer with freshly-measured
  // dimensions. Needed because a tile re-shown within the GL-hold window stays on
  // WebGL (no renderer swap → setRendererVisible never heals it), so this is the only
  // repaint it gets.
  function _termPxWidth() { return (term.element && term.element.clientWidth) || window.innerWidth || 0; }
  var _lastPxWidth = _termPxWidth();
  // Coalesce resize-driven fits. A screen unlock / display(-config) wake fires a
  // BURST of `resize` events while the window manager re-lays-out the page, and
  // each one recomputes cols/rows. fit.fit() pushes any WIDTH change to the PTY
  // as a SIGWINCH (via term.resize → onResize → sendResize), so a burst that
  // lands on a transient width W1 and then settles on W2 makes claude (Ink)
  // render its frame at W1 and re-render it at W2. Because the W1 frame-erase
  // miscounts against the W2 grid, the two renders INTERLEAVE rather than
  // overwrite — the user-reported "double content after screen unlock" (status
  // line + input box doubled, with stray single cells like `remove0thate`
  // betraying the two column widths). Debounce so only the FINAL, settled size
  // is ever applied: one clean SIGWINCH, claude renders once, no interleave.
  // (A rapid multi-width burst is the hazard — a single settled resize is
  // exactly what Ink is built to absorb.) This is the same dup family the
  // renderer-swap path and the `refresh`/`_repaintWiggle` handlers already
  // guard against; the bare window-resize listener was the one unguarded
  // SIGWINCH source firing on every event.
  var _fitTimer = null;
  function _scheduleFit() {
    if (_fitTimer) clearTimeout(_fitTimer);
    _fitTimer = setTimeout(function () { _fitTimer = null; _diag.fits++; try { fit.fit(); } catch (e) {} }, 180);
  }
  window.addEventListener('resize', function () {
    var w = _termPxWidth();
    // Un-hidden (0 → visible): re-blit straight from the buffer NOW. Render-only
    // (no term.resize → no SIGWINCH), so it's safe to run immediately and must
    // NOT wait on the debounce — otherwise the tile sits blank until the settled
    // fit lands ~180 ms later.
    if (w > 0 && _lastPxWidth === 0) _burstHeal();   // just un-hidden → re-blit from buffer (burst: catch GL warmup)
    _lastPxWidth = w;
    _scheduleFit();
  });
  setTimeout(function () { try { fit.fit(); } catch (e) {} }, 60);

  // --- drag & drop / paste: upload file bytes, type the resulting path ---
  // Dropping or pasting files into the terminal uploads their CONTENT to the
  // dashboard, which saves each to `<session-cwd>/.vibe-drops/<uid>-<name>`
  // and returns the path. The iframe then shell-quotes and types the path at
  // the cursor — so claude sees them as if you'd typed them in.
  //
  // Why upload (not just read .name): Chrome on macOS (and most browsers
  // since ~2017) deliberately do NOT expose absolute host paths from drag /
  // paste of Finder files to web pages. The Files API gives basenames only;
  // text/uri-list isn't populated for security. So the ONLY way to give
  // claude a real path it can `cat` is for us to put the file *somewhere
  // claude can reach* and tell it where. The dashboard knows the session's
  // cwd and (for container sessions) the host↔/workspace mapping, so it
  // returns the right path for whichever kind of shell is running there.
  var _csrfToken = '';
  var _dashboardOrigin = '';
  function _quoteShellArg(s) {
    // POSIX-safe single-quote wrap with `'\''` escape for embedded quotes.
    return "'" + s.replace(/'/g, "'\\''") + "'";
  }
  // Small bottom-right overlay shown while an upload is in flight — large
  // files take seconds, and without feedback the user wonders if anything
  // happened. Auto-hides on resolve/reject.
  var _uploadOverlay = null;
  function _showUploadStatus(text) {
    if (!_uploadOverlay) {
      _uploadOverlay = document.createElement('div');
      _uploadOverlay.style.cssText = 'position:fixed;bottom:10px;right:10px;' +
        'background:rgba(28,28,28,.95);color:#ddd;padding:6px 10px;border-radius:6px;' +
        'border:1px solid #444;font:12px ui-monospace,Menlo,monospace;z-index:99;' +
        'box-shadow:0 4px 14px rgba(0,0,0,.5);';
      document.body.appendChild(_uploadOverlay);
    }
    _uploadOverlay.textContent = text;
    _uploadOverlay.style.display = 'block';
  }
  function _hideUploadStatus() {
    if (_uploadOverlay) _uploadOverlay.style.display = 'none';
  }
  function _uploadFile(file) {
    if (!_csrfToken || !_dashboardOrigin) {
      return Promise.reject(new Error('upload not configured (no dashboard token yet)'));
    }
    var url = _dashboardOrigin + _basePath + '/api/dropfile?sid=' + encodeURIComponent(sid) +
              '&name=' + encodeURIComponent(file.name || 'file');
    return fetch(url, {
      method: 'POST',
      mode: 'cors',
      credentials: 'omit',
      headers: { 'X-CSRF-Token': _csrfToken, 'Content-Type': 'application/octet-stream' },
      body: file,
    }).then(function (r) {
      if (!r.ok) return Promise.reject(new Error('upload failed: HTTP ' + r.status));
      return r.json();
    }).then(function (j) { return j && j.path; });
  }
  // Upload files SEQUENTIALLY (not parallel): claude commonly treats argument
  // order as semantically meaningful ("compare A and B"), and sequential keeps
  // the typed-path order matching the user's selection order. The overlay's
  // "i of N" counter is the visible side-benefit of doing one at a time.
  function _uploadFilesAndTypePaths(files) {
    var list = Array.from(files);
    if (!list.length) return Promise.resolve();
    var total = list.length;
    var done = 0;
    var paths = [];
    _showUploadStatus('uploading 1/' + total + '…');
    return list.reduce(function (chain, f) {
      return chain.then(function () {
        _showUploadStatus('uploading ' + (done + 1) + '/' + total + ' — ' + (f.name || 'file') + '…');
        return _uploadFile(f).then(function (p) {
          done++;
          if (p) paths.push(p);
        }, function (err) {
          done++;
          // Surface upload errors inline so the user sees them in the term.
          try { term.write('\r\n\x1b[31m[dropfile] ' + (err && err.message || err) + '\x1b[0m\r\n'); } catch (e2) {}
        });
      });
    }, Promise.resolve()).then(function () {
      _hideUploadStatus();
      if (!paths.length) return;
      sendInput(paths.map(_quoteShellArg).join(' '));
      try { term.focus(); } catch (e2) {}
    });
  }
  document.addEventListener('dragover', function (e) {
    // preventDefault is what tells the browser this element is a drop target.
    // Without it the drop event never fires and the browser opens the file in
    // a new tab (or saves it). Setting dropEffect='copy' gives the correct
    // mouse cursor (plus sign) during the drag.
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  });
  document.addEventListener('drop', function (e) {
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    e.preventDefault();
    _uploadFilesAndTypePaths(e.dataTransfer.files);
  });
  // Paste hijacks ONLY when actual files are on the clipboard; plain text
  // paste must still flow through to xterm. Capture phase so we beat xterm's
  // hidden-textarea listener (we need to preventDefault first).
  document.addEventListener('paste', function (e) {
    if (!e.clipboardData || !e.clipboardData.files || !e.clipboardData.files.length) return;
    e.preventDefault();
    _uploadFilesAndTypePaths(e.clipboardData.files);
  }, true);

  function startSession() {
    fetch('token').then(function (r) { return r.json(); })
      .then(function (j) { token = (j && j.token) || ''; connect(); })
      .catch(function () { token = ''; connect(); });
  }
  restoreThenStart();   // wait for a real size, restore history, then open the socket
})();
