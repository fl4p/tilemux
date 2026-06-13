#!/usr/bin/env node
// End-to-end test for the input-box ghost-suggestion scraper, against REAL claude
// output bytes replayed through a real (headless) xterm terminal.
//
// testdata/claude_input_box.cast is a raw PTY capture of `claude` drawing its
// input box with the dim (SGR 2) placeholder `Try "fix typecheck errors"` — the
// words positioned with absolute-column cursor jumps, exactly as claude emits
// them. We replay it into @xterm/headless and run the REAL _scrapeSuggestion()
// from term-client.js against the resulting cell buffer, so this exercises the
// shipped algorithm end to end (claude's escape sequences → xterm's cell model →
// our scraper) rather than a hand-built mock.
//
// This is what caught the original bug: the first cut of the scraper only looked
// at the bottom ~10 rows, but claude draws the input box near the TOP of a short
// frame (it pins to the bottom only once the transcript fills the screen), so the
// suggestion was missed. The scraper now scans the whole live frame.
//
// @xterm/headless isn't a declared dependency of this repo (the browser xterm is
// inlined into term.html at build time). We locate it via Node resolution or a
// couple of well-known install locations (e.g. the one bundled with VS Code) and
// SKIP cleanly if it can't be found, so this test never fails for lack of a dep.
import fs from 'fs';
import path from 'path';
import url from 'url';
import assert from 'assert';

const __dirname = path.dirname(url.fileURLToPath(import.meta.url));

async function loadHeadless() {
  const candidates = [];
  try { candidates.push(import.meta.resolve('@xterm/headless')); } catch {}
  const guesses = [
    '/Applications/Visual Studio Code.app/Contents/Resources/app/node_modules/@xterm/headless/lib-headless/xterm-headless.mjs',
    path.join(process.env.HOME || '', '.vscode/extensions'), // (not a module, kept for documentation)
  ];
  for (const g of guesses) { if (g.endsWith('.mjs') && fs.existsSync(g)) candidates.push(url.pathToFileURL(g).href); }
  for (const c of candidates) {
    try { const m = await import(c); if (m && m.Terminal) return m.Terminal; } catch {}
  }
  return null;
}

function extractFn(src, name) {
  const s = src.indexOf('function ' + name + '(');
  assert.ok(s >= 0, 'could not find ' + name + ' in term-client.js');
  let i = src.indexOf('{', s), depth = 0, end = -1;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  return src.slice(s, end);
}

const Terminal = await loadHeadless();
if (!Terminal) {
  console.log('SKIP: @xterm/headless not found — install it to run the real-bytes e2e.');
  process.exit(0);
}

const src = fs.readFileSync(path.join(__dirname, 'term-client.js'), 'utf8');
const scrape = new Function('term', extractFn(src, '_scrapeSuggestion') + '\nreturn _scrapeSuggestion();');
const raw = fs.readFileSync(path.join(__dirname, 'testdata', 'claude_input_box.cast'));

// 1. With the dim placeholder live in the buffer, the scraper extracts it.
{
  const close = Buffer.from('\x1b[22m');
  const dimStart = raw.indexOf(Buffer.from('\x1b[2mTry'));
  const cut = raw.indexOf(close, dimStart) + close.length;
  assert.ok(dimStart >= 0 && cut > dimStart, 'fixture missing the dim placeholder span');
  const term = new Terminal({ cols: 100, rows: 40, allowProposedApi: true });
  await new Promise(r => term.write(raw.subarray(0, cut), r));
  const got = scrape(term);
  console.log('  scraped:', JSON.stringify(got));
  assert.match(got, /fix.*typecheck.*errors/, 'expected the dim placeholder text');
  console.log('  ok - real claude bytes -> headless xterm -> real scraper extracts the suggestion');
}

// 2. Replaying the full capture (the placeholder was redrawn away to an empty
//    input by the final frame) yields no suggestion — i.e. an empty box scrapes
//    to ''.
{
  const term = new Terminal({ cols: 100, rows: 40, allowProposedApi: true });
  await new Promise(r => term.write(raw, r));
  const got = scrape(term);
  assert.strictEqual(got, '', 'empty input box must scrape to "" (got ' + JSON.stringify(got) + ')');
  console.log('  ok - empty input box (full replay) scrapes to ""');
}

console.log('\n2 e2e tests passed');
