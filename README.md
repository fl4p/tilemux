# session-dashboard

> ⚠️ **Early draft.** Extracted from a personal monorepo and lightly cleaned up
> for publishing. Expect rough edges, sparse docs, and assumptions baked in from
> the author's setup. Issues and PRs welcome, but treat it as work-in-progress.

A single-file Python web dashboard that shows your running terminal sessions —
especially [Claude Code](https://claude.com/claude-code) agents — as live tiles
in a browser grid. Each tile is a real, interactive web terminal; you can watch
several agents at once, search scrollback, condense/stash/close tiles, and (with
hooks installed) see each agent's busy/idle state and pending permission prompts.

The server is `serve.py` — pure Python standard library, no pip dependencies.

## How it works

- Each session tile is a live [**ttyd**](https://github.com/tsl0922/ttyd) web TTY
  bound to `127.0.0.1`. The dashboard embeds it and, when reached over anything
  other than localhost, proxies it through its own `/t/<port>/` reverse proxy so
  a single origin serves the grid and every terminal (see `DEPLOY.md`).
- `term.html` is a self-contained xterm.js client (fit + serialize + a custom
  buffer-scanning search) inlined into one file by `build-term.sh`. Re-run that
  script after editing `term-client.js`.
- Claude Code **hooks** (`hooks/`) forward agent events to the dashboard so the
  chat panel has ground-truth busy/idle and permission-prompt state that the
  transcript `.jsonl` alone doesn't capture. They fail silent if the dashboard
  isn't running.

## Launchers & providers

The **+ New** menu is driven by configurable launcher presets — each a raw
command line (`claude`, `claude --dangerously-skip-permissions`, `codex`,
`opencode`, or any custom command) with optional per-launcher env vars. Presets
are edited from the dashboard's "Manage launchers…" modal and persisted as plain
JSON at `~/.config/session-dashboard/launchers.json` (override with
`LAUNCHERS_CONFIG`). A missing file falls back to sensible defaults
(claude/codex/opencode), so first run just works.

A preset can point an agent at a custom endpoint via its env vars
(`ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, …), or use `"provider": "vertex"` to
route Claude through **Google Vertex AI** (gcloud Application Default
Credentials + `~/.config/session-dashboard/vertex.json`, override with
`VERTEX_CONFIG`). Set your GCP project via that file or
`ANTHROPIC_VERTEX_PROJECT_ID` — there is no project baked into the code.

These config files live under `~/.config/session-dashboard/` and are
`.gitignore`d, so your project ids / endpoints never end up in the repo.

## Chat history & search

`claude-chat-export.py` renders your Claude Code transcripts (`~/.claude/projects`)
into a searchable HTML history under `~/claude-chat-history`, and the dashboard
serves that for its in-app chat search. Both sides read the same env vars so they
stay in sync:

| Env var | Default | Used by |
|---------|---------|---------|
| `CHAT_HISTORY_DIR` | `~/claude-chat-history` | `serve.py` (serves it) **and** the export script (`OUT_ROOT`, writes it) |
| `CLAUDE_PROJECTS_ROOT` | `~/.claude/projects` | export script (transcript source) |
| `CLAUDE_CONTAINERS_ROOT` | _(unset → skipped)_ | export script — optional registry of sandboxed-session symlinks |

Run it once to populate the history:

```sh
python3 claude-chat-export.py
```

The dashboard also re-runs it on demand, but for a continuously fresh history
schedule it periodically — e.g. a launchd agent on macOS or a cron job on Linux
that runs `python3 /path/to/claude-chat-export.py` every few minutes. (Search
degrades gracefully if the history hasn't been generated yet.)

## Requirements

- Python 3 (3.10+ recommended) — standard library only
- [`ttyd`](https://github.com/tsl0922/ttyd) and [`dtach`](https://github.com/crigler/dtach) on `PATH`
- Optional: the `claude` CLI, for launching/attaching Claude Code sessions

## Quick start

```sh
# Build the inlined terminal client (fetches xterm from a CDN into a temp dir)
./build-term.sh

# Run the dashboard (defaults to port 7680, opens your browser)
python3 serve.py
# or pick a port / skip auto-open:
python3 serve.py 7680 --no-open
```

Then open `http://127.0.0.1:7680/`. The dashboard binds `127.0.0.1` only and is
designed for a browser on the same host. To reach it from another machine, put
it behind a TLS reverse proxy with auth — see **[`DEPLOY.md`](DEPLOY.md)** for a
worked nginx + HTTPS + basic-auth example.

## Tests

Browser-level regression tests use Playwright; unit/server tests use pytest.

```sh
python3 -m venv .venv-test && . .venv-test/bin/activate
pip install pytest playwright && playwright install chromium
pytest test_serve.py
```

(Several `test_tile_*` / `test_*_browser.py` suites drive a headless Chromium
against a live `serve.py`.)

## Layout

| Path | What |
|------|------|
| `serve.py` | The dashboard HTTP server (stdlib only) |
| `term-client.js` | xterm.js-based web terminal client (source) |
| `term.html` | Built, self-contained terminal page (`build-term.sh` output) |
| `claude-chat-export.py` | Renders Claude Code transcripts into the searchable chat history |
| `build-term.sh` | Inlines xterm + addons + `term-client.js` into `term.html` |
| `hooks/` | Claude Code hook scripts (event forwarding, self-close) |
| `etc/wezterm-proto/` | Early WezTerm-mux prototype (not used by the server) |
| `etc/` | Misc dev odds and ends (icon playground, prototypes) |
| `fonts/` | Self-hosted terminal/UI webfonts |
| `DEPLOY.md` | Remote deployment behind nginx (HTTPS + basic auth) |

## License

[MIT](LICENSE)
