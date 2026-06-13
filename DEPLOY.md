# Deploying session-dashboard behind nginx (HTTPS + basic auth)

This documents how the dashboard is served remotely on a server behind an nginx
reverse proxy with TLS and HTTP basic auth, and how to reproduce it for future
deployments. The first deployment (the worked example below) is
**`https://dashboard.example.com/dash/`**, running as the **`dashuser`** user.

> The dashboard binds `127.0.0.1` only and is designed for a browser on the same
> host. The two features that make a *remote* browser work — an in-process ttyd
> reverse proxy and reverse-proxy-subpath support — are described first, because
> understanding them is what makes the nginx config make sense.

---

## 1. Why remote access needs more than "proxy the port"

Each session tile (a host/container claude, or a `+New` terminal) is a live
**ttyd** web TTY. ttyd binds `127.0.0.1:<port>` and the tile is an `<iframe>`.
On localhost the dashboard embeds that iframe **directly** at
`http://127.0.0.1:<port>/`. For a *remote* browser that address means the
**viewer's own machine**, over plaintext inside an HTTPS page (mixed content) —
so the terminals would never render. Two mechanisms fix this:

### a) In-dashboard ttyd reverse proxy — `/t/<port>/`

`serve.py` proxies `GET /t/<port>/...` to `127.0.0.1:<port>` (`_proxy_ttyd`). It
forwards the request (path-stripped, `Host` rewritten, `Sec-WebSocket-Key`
untouched) to ttyd and then becomes a transparent bidirectional byte pipe —
which carries both ttyd's HTTP responses (the term page, `/token`) **and** the
opaque WebSocket frame stream on `/ws` after the `101` upgrade. The port must
belong to a *live registered* session (SSRF guard).

When the dashboard is reached over anything other than localhost, the client
points each terminal tile at `location.origin + BASE + '/t/<port>/'` instead of
`http://127.0.0.1:<port>/` (the `PROXY_TTYD` switch). So **a single proxied
origin serves the grid and every terminal** — nginx only forwards one port.

### b) Reverse-proxy subpath — `DASHBOARD_BASE`

To coexist with other vhosts, the dashboard can live under a subpath (e.g.
`/dash`) rather than the site root. Set `DASHBOARD_BASE=/dash`:

- **nginx forwards the prefix intact** (no `proxy_pass` trailing slash).
- `serve.py` strips it at request entry (`_strip_base`), so all internal routing
  stays prefix-free.
- `serve.py` injects the prefix into every served page as a JS `const BASE`, and
  a small `fetch` shim prepends it to root-absolute, same-origin URLs. The few
  non-fetch URLs (iframe `src`, `<a href>`) prepend `BASE` explicitly.
- The ttyd client (`term.html`) derives the prefix from its own path
  (`<base>/t/<port>/`): the WebSocket URL is path-relative, and tile-image /
  drop-file calls back to the dashboard carry the derived base.

Leave `DASHBOARD_BASE` unset to serve at the origin root (the default — every
mechanism above no-ops, and localhost installs are unchanged).

---

## 2. Prerequisites on the server

```bash
sudo apt-get install -y ttyd dtach lsof
```

- **ttyd** — the web TTY for every terminal/claude tile.
- **dtach** — optional but recommended; lets a session's pty survive a ttyd
  restart (the dashboard uses it automatically when present).
- **lsof** — **required for session teardown.** `close_session` finds the ttyd
  pid via `lsof`; without it, closing a tile removes the registry entry but
  **leaves the ttyd process orphaned**. (This bit us on first deploy.)

The `ttyd` apt package **auto-enables a `ttyd.service`** that runs a login shell
on `127.0.0.1:7681`. The dashboard manages its own ttyd processes and uses 7681
as its first session port, so disable the packaged one:

```bash
sudo systemctl disable --now ttyd
```

`claude` itself is installed separately (per-user). The dashboard runs fine
without it — `+New terminal` tiles work — but claude tiles need it on `PATH` for
the service user.

---

## 3. Install the app

Copy the runtime files to a directory owned by the service user. Required at
runtime: `serve.py`, `term.html`, `fonts/`. Also copy `term-client.js`,
`build-term.sh`, `image-decode-shim.js` if you want to rebuild `term.html` on the
server.

```bash
# from a dev checkout of session-dashboard/
rsync -az serve.py term.html term-client.js build-term.sh image-decode-shim.js fonts \
      SERVER:/home/USER/sd-staging/
ssh SERVER '
  sudo install -d -o USER -g USER /home/USER/session-dashboard
  sudo cp -r /home/USER/sd-staging/. /home/USER/session-dashboard/
  sudo chown -R USER:USER /home/USER/session-dashboard
'
```

`term.html` is a build artifact (inlines `term-client.js` + xterm + fonts). If
you edit `term-client.js`, re-run `./build-term.sh` and re-copy `term.html`.

---

## 4. systemd service

`/etc/systemd/system/session-dashboard.service`:

```ini
[Unit]
Description=Claude session dashboard (behind nginx /dash, https + basic auth)
After=network.target

[Service]
Type=simple
User=dashuser
Group=dashuser
WorkingDirectory=/home/dashuser/session-dashboard
Environment=DASHBOARD_BASE=/dash
# PATH MUST include the user's ~/.local/bin (where `claude` installs) and /snap/bin
# — serve.py's _which() resolves claude/opencode/ttyd from PATH. Omitting
# ~/.local/bin is why "+New → Claude" silently 500s (spawn returns None).
Environment=PATH=/home/dashuser/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
# KillMode=process so a stop/restart (incl. Restart=on-failure) kills ONLY
# serve.py, not the ttyd/dtach/claude children. They keep running and serve.py
# re-adopts them from the on-disk registry on next start — otherwise the default
# control-group kill nukes every live terminal on any restart.
KillMode=process
# Binds 127.0.0.1:7680 only; nginx terminates TLS + basic auth in front of it.
ExecStart=/usr/bin/python3 /home/dashuser/session-dashboard/serve.py 7680 --no-open
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now session-dashboard
systemctl is-active session-dashboard            # active
ss -ltnp | grep :7680                            # 127.0.0.1:7680
```

`PATH` is set explicitly so the service finds `ttyd`/`dtach`/`claude`/`opencode`
— **include `/home/<user>/.local/bin`** or claude/opencode tiles silently fail.
Omit the `DASHBOARD_BASE` line to serve at the origin root.

> **Failed spawns are silent.** `+New → Claude/opencode/Terminal-in-container`
> calls `POST /api/new`, which returns **500 `{"ok":false}`** when the binary
> isn't found (or podman/claude-box isn't set up) — and the frontend shows **no
> error**, so it just looks like nothing happened. If a tile doesn't appear,
> check the binary is on the service PATH (`sudo -u dashuser env PATH=… which claude`)
> and watch `journalctl -u session-dashboard`.

---

## 5. Basic-auth credentials

No `apache2-utils`/`htpasswd` needed — use `openssl`:

```bash
PASS=$(openssl rand -base64 18 | tr -d '=+/' | cut -c1-22)
echo "dashuser:$(openssl passwd -apr1 "$PASS")" | sudo tee /etc/nginx/.htpasswd-dashboard >/dev/null
sudo chown root:www-data /etc/nginx/.htpasswd-dashboard
sudo chmod 640 /etc/nginx/.htpasswd-dashboard
echo "user=dashuser pass=$PASS"   # record this once
```

---

## 6. nginx

### WebSocket upgrade map (http context)

`/etc/nginx/conf.d/ws-upgrade.conf`:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ""      close;
}
```

### The `/dash/` location

Add **inside the existing TLS (`listen 443 ssl`) `server` block** for the host
(the one with the `ssl_certificate` lines). `/dash/` is more specific than any
existing `location /`, so it wins for these paths and leaves the rest of the
vhost untouched:

```nginx
location = /dash { return 301 /dash/; }
location /dash/ {
    auth_basic "Claude Dashboard";
    auth_basic_user_file /etc/nginx/.htpasswd-dashboard;

    proxy_pass http://127.0.0.1:7680;     # NO trailing slash → prefix forwarded intact
    proxy_http_version 1.1;
    proxy_set_header Host 127.0.0.1:7680; # so serve.py's Host allow-list passes
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_buffering off;
    proxy_read_timeout 86400;             # long-lived terminal WebSockets
    client_max_body_size 110m;            # matches the 100 MB drop-upload ceiling
}
```

Key points:

- **No trailing slash** on `proxy_pass` so the `/dash` prefix reaches `serve.py`
  (which strips it). `DASHBOARD_BASE` must equal the location prefix.
- **`Host 127.0.0.1:7680`** because `serve.py` validates `Host` against
  `{127.0.0.1:PORT, localhost:PORT}` (DNS-rebinding guard). The browser's address
  bar still drives `location.origin`, so `BASE`/iframe URLs are unaffected.
- **`Connection $connection_upgrade`** (from the map) so the same location serves
  both normal HTTP (polling, the grid) and the terminal WebSockets.

### Apply

Back up first, and keep backups **out of** `sites-enabled/` (nginx globs that
dir — a `*.bak` there becomes a duplicate `server` and breaks `nginx -t`):

```bash
sudo mkdir -p /etc/nginx/dashboard-backups
sudo cp -a /etc/nginx/sites-enabled/default /etc/nginx/dashboard-backups/default.bak-$(date +%s)
# …edit the vhost…
sudo nginx -t && sudo systemctl reload nginx
```

TLS reuses the host's existing Let's Encrypt cert
(`/etc/letsencrypt/live/<host>/`). No new cert needed when serving under a
subpath of an already-certified host.

---

## 7. Verify

```bash
AUTH='dashuser:THEPASSWORD'; U='https://dashboard.example.com/dash'
curl -s -o /dev/null -w '%{http_code}\n' "$U/"                 # 401 (no auth)
curl -s -u "$AUTH" "$U/" | grep -o 'const BASE = "[^"]*"'      # const BASE = "/dash"
curl -s -u "$AUTH" "$U/api/sessions"                           # {"sessions": [...], ...}

# full terminal path: spawn a tile, then upgrade its ttyd WebSocket
CSRF=$(curl -s -u "$AUTH" "$U/" | grep -oE 'name="csrf-token" content="[^"]*"' | sed -E 's/.*content="([^"]*)".*/\1/')
curl -s -u "$AUTH" -X POST -H "X-CSRF-Token: $CSRF" "$U/api/new?kind=terminal"
PORT=$(curl -s -u "$AUTH" "$U/api/sessions" | python3 -c "import sys,json;print(json.load(sys.stdin)['sessions'][0]['port'])")
curl -s -i -u "$AUTH" --http1.1 -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
     -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
     --max-time 4 "$U/t/$PORT/ws" | head -1                    # HTTP/1.1 101 Switching Protocols
```

A browser at `https://dashboard.example.com/dash/` should prompt for basic auth, render the
grid, and the terminal tile should be interactive (keystrokes echo).

---

## 8. This deployment's facts (dashboard.example.com)

| Thing | Value |
|---|---|
| URL | `https://dashboard.example.com/dash/` |
| Service user | `dashuser` |
| Dashboard bind | `127.0.0.1:7680` (`DASHBOARD_BASE=/dash`) |
| systemd unit | `session-dashboard.service` |
| Basic auth | user `dashuser` (password stored out-of-band; regenerate per §5) |
| htpasswd file | `/etc/nginx/.htpasswd-dashboard` |
| nginx vhost | `/dash/` location added to the `dashboard.example.com` 443 server in `sites-enabled/default` |
| ws map | `/etc/nginx/conf.d/ws-upgrade.conf` |
| nginx backups | `/etc/nginx/dashboard-backups/` |
| TLS cert | existing `…/letsencrypt/live/dashboard.example.com/` (certbot-managed) |

### Caveats / notes

- The site **root `/`** on `dashboard.example.com` proxies a long-dead `localhost:8080`
  ("streama") and currently returns 502. The dashboard was placed at `/dash/`
  specifically to leave that untouched. If you ever want the dashboard at the
  root instead, repoint `location /` and drop `DASHBOARD_BASE`.
- The cert covers `dashboard.example.com` only; a separate subdomain would need its own
  cert (and `dash.example.com` currently points at a *different* host).

### Bug sweep — found & fixed during the first deploy

A full browser pass over every tile type surfaced these (all addressed):

- **`+New → Claude` silently failed** — the systemd `PATH` omitted
  `~/.local/bin`, so `_which("claude")` found nothing and the spawn 500'd with no
  UI feedback. Fixed by adding `~/.local/bin` (and `/snap/bin`) to the unit PATH.
  Same applies to `opencode` (must be installed + on PATH) and
  *Terminal-in-container* (needs podman + claude-box).
- **Closing a tile orphaned its `ttyd`** — `close_session` finds the pid via
  `lsof`, which wasn't installed. Fixed by `apt install lsof` (see §2).
- **Service restart killed every live session** — default `KillMode` is
  `control-group`, which reaps the ttyd children. Fixed with `KillMode=process`
  (§4); sessions now survive `systemctl restart` and are re-adopted.
- **`/favicon.ico` 502 console noise** — the page auto-requested a root-level
  favicon (→ streama 502). Fixed in `serve.py` with an inline
  `<link rel="icon" href="data:,">`.

### Search / chat history — not wired here

The **🔍 Search** button runs `claude-chats/claude-chat-export.py` to build a
static index, then opens `/chat-history/`. On this host it returns a clean
`{"ok":false}` 500 (and a blank popup) because:

- `serve.py`'s `CHAT_HISTORY_DIR` / `CHAT_EXPORT_SCRIPT` are now env-overridable
  (defaults are the dev-checkout macOS paths) — but
- the **export script itself hardcodes `/Users/you/...`** (`PROJECTS_ROOT`,
  `OUT_ROOT`, `CONTAINERS_ROOT`) and isn't deployed.

To enable Search on a server: deploy a portable `claude-chat-export.py`
(env-ify its three roots), then set `CHAT_EXPORT_SCRIPT` and `CHAT_HISTORY_DIR`
in the systemd unit to the deployed script and a writable output dir. Everything
else (Note, Channel, Web view + proxy toggle, Terminal, Claude) works.
