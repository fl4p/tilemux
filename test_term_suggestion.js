#!/usr/bin/env node
// Tests for the input-box ghost-suggestion scraper in term-client.js.
//
// claude renders its placeholder / suggested-prompt as DIM (SGR 2) text inside
// the input box, positioning each word with an absolute-column cursor jump — so
// the dim cells are NOT contiguous (the gaps are default-attr blank cells). The
// scraper must reconstruct the text from the dim column span, ignore non-dim
// chrome (borders/footer/status), prefer the longest dim run, and return '' when
// the box is empty of dim text (i.e. the user has typed something).
//
// We load the REAL _scrapeSuggestion() out of term-client.js (brace-matched) and
// run it against a mock xterm `term`, so this exercises the shipped algorithm.
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, 'term-client.js'), 'utf8');
function extractFn(name) {
  const start = src.indexOf('function ' + name + '(');
  assert.ok(start >= 0, 'could not find ' + name + ' in term-client.js');
  let i = src.indexOf('{', start), depth = 0, end = -1;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  assert.ok(end > start, 'unbalanced braces for ' + name);
  return src.slice(start, end);
}
// Build a callable bound to an injected `term`.
const scrape = new Function('term', extractFn('_scrapeSuggestion') + '\nreturn _scrapeSuggestion();');

// --- mock xterm buffer -------------------------------------------------------
// rows: array of cell-arrays for the LIVE frame (baseY..baseY+rows-1). Each cell
// is [char, dim]. We pad to `cols` with non-dim blanks.
function mkTerm(rows, cols, frameRows, baseY, scrollbackRows) {
  baseY = baseY || 0;
  scrollbackRows = scrollbackRows || [];
  function line(cells) {
    const padded = cells.slice();
    while (padded.length < cols) padded.push([' ', 0]);
    return {
      getCell: function (x) {
        const c = padded[x];
        if (!c) return { getChars: () => ' ', isDim: () => 0 };
        return { getChars: () => c[0], isDim: () => c[1] };
      }
    };
  }
  const frame = frameRows.map(line);
  // Scrollback occupies the absolute rows just BELOW baseY (i.e. abs in
  // [baseY - scrollbackRows.length, baseY)); the live frame is [baseY, baseY+rows).
  const sb = scrollbackRows.map(line), sbBase = baseY - sb.length;
  return {
    rows: rows, cols: cols,
    buffer: { active: { baseY: baseY, getLine: function (abs) {
      const ry = abs - baseY;
      if (ry >= 0 && ry < frame.length) return frame[ry];
      const si = abs - sbBase;
      if (si >= 0 && si < sb.length) return sb[si];
      return null;
    } } }
  };
}

// Helper: place a dim word starting at column `col` on a row of blanks.
function row(cols, words) {
  const cells = new Array(cols).fill(0).map(() => [' ', 0]);
  for (const w of words) {            // {col, text, dim}
    for (let k = 0; k < w.text.length; k++) cells[w.col + k] = [w.text[k], w.dim ? 1 : 0];
  }
  return cells;
}

let pass = 0;
function t(name, fn) { fn(); pass++; console.log('  ok -', name); }

const COLS = 100, ROWS = 12;

// 1. claude's column-jumped dim placeholder is reconstructed across the gaps.
t('reconstructs column-jumped dim placeholder', () => {
  // "> " prompt glyph (non-dim) then dim words at jumped columns, blanks between.
  const input = row(COLS, [
    { col: 2, text: '>', dim: false },
    { col: 4, text: 'Try', dim: true },
    { col: 8, text: 'create', dim: true },
    { col: 15, text: 'a', dim: true },
    { col: 17, text: 'util', dim: true },
    { col: 22, text: 'logging.py', dim: true },
  ]);
  const border = row(COLS, [{ col: 0, text: '─'.repeat(40), dim: false }]); // non-dim chrome
  const footer = row(COLS, [{ col: 2, text: '↑ for agents', dim: false }]); // non-dim chrome
  const frame = [];
  for (let i = 0; i < ROWS - 3; i++) frame.push(row(COLS, []));
  frame.push(border); frame.push(input); frame.push(footer);
  const term = mkTerm(ROWS, COLS, frame, 7);
  assert.strictEqual(scrape(term), 'Try create a util logging.py');
});

// 2. Empty box (user typed normal-weight text) → no dim → ''.
t('returns empty when input has only normal-weight text', () => {
  const input = row(COLS, [
    { col: 2, text: '>', dim: false },
    { col: 4, text: 'hello world', dim: false },
  ]);
  const frame = [];
  for (let i = 0; i < ROWS - 1; i++) frame.push(row(COLS, []));
  frame.push(input);
  assert.strictEqual(scrape(mkTerm(ROWS, COLS, frame, 3)), '');
});

// 3. Longest dim run wins (a short dim hint above must not beat the suggestion).
t('prefers the longest dim run', () => {
  const hint = row(COLS, [{ col: 5, text: 'esc', dim: true }]);
  const sugg = row(COLS, [
    { col: 4, text: 'Refactor', dim: true },
    { col: 13, text: 'the', dim: true },
    { col: 17, text: 'parser', dim: true },
  ]);
  const frame = [];
  for (let i = 0; i < ROWS - 4; i++) frame.push(row(COLS, []));
  frame.push(hint); frame.push(row(COLS, [])); frame.push(sugg); frame.push(row(COLS, []));
  assert.strictEqual(scrape(mkTerm(ROWS, COLS, frame, 0)), 'Refactor the parser');
});

// 4a. The input box near the TOP of a short/fresh frame is found (the bug the
//     real-bytes e2e test caught: claude only pins the box to the bottom once the
//     transcript fills the screen).
t('finds the suggestion when the input box sits near the top', () => {
  const cols = 80, rows = 40;
  const frame = [];
  for (let i = 0; i < rows; i++) frame.push(row(cols, []));
  frame[6] = row(cols, [{ col: 0, text: '─'.repeat(60), dim: false }]);   // box top border
  frame[7] = row(cols, [                                                    // input line
    { col: 0, text: '>', dim: false },
    { col: 2, text: 'Add', dim: true }, { col: 6, text: 'a', dim: true },
    { col: 8, text: 'health', dim: true }, { col: 15, text: 'endpoint', dim: true },
  ]);
  frame[8] = row(cols, [{ col: 0, text: '─'.repeat(60), dim: false }]);   // box bottom border
  assert.strictEqual(scrape(mkTerm(rows, cols, frame, 0)), 'Add a health endpoint');
});

// 4b. Dim text in the SCROLLBACK (absolute rows below baseY) is ignored — the
//     scan is baseY-anchored, so a user scrolled up into history can't leak dim
//     transcript text into the suggestion.
t('ignores dim text in the scrollback (below baseY)', () => {
  const cols = 80, rows = 24;
  const frame = [];
  for (let i = 0; i < rows; i++) frame.push(row(cols, []));   // live frame: all blank
  const scrollback = [
    row(cols, [{ col: 0, text: 'old dim transcript line', dim: true }]),
    row(cols, [{ col: 0, text: 'another dim history row', dim: true }]),
  ];
  assert.strictEqual(scrape(mkTerm(rows, cols, frame, 200, scrollback)), '');
});

// 5. Result is length-capped to 400 chars.
t('caps result length at 400', () => {
  const big = 'x'.repeat(600);
  const input = row(620, [{ col: 0, text: big, dim: true }]);
  const frame = [row(620, []), input];
  const out = scrape(mkTerm(2, 620, frame, 0));
  assert.strictEqual(out.length, 400);
});

console.log(`\n${pass} tests passed`);
