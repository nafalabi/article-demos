# Note-Taking Stack — Setup Guide

Five containers, four hostnames, one directory of markdown files. This guide
takes you from nothing to a working setup, and explains how the pieces fit
together as it goes.

Authentication is deliberately out of scope for the browser apps. The demo
assumes a trusted LAN; WebDAV keeps its own basic auth because its clients need
a username and password, and the MCP endpoint requires a bearer token because it
can push. SSO is a separate article, so the pieces here stay focused on Ignis,
WebDAV, the git UI, and the MCP endpoint.

## What you get

| Service | Image | Its job |
| --- | --- | --- |
| `traefik` | `traefik:v3.7.13` | the only door in: TLS, routing, and headers |
| `ignis` | `nobbe/ignis:0.8.10` | Obsidian, running in the browser |
| `webdav` | `rclone/rclone:1.75.0` | the same vault over WebDAV, for phone and desktop |
| `git-ui` | `alpine/git:v2.54.0` | lazygit in the browser (lazygit 0.64.1 + ttyd 1.7.7) |
| `git-mcp` | built locally from `git-mcp/` | MCP git tools for external agents |

### How they work together

```
browser ─▶ traefik :443 ─▶ ignis  :8080 ─┐
phone   ─▶ traefik :443 ─▶ webdav :8081 ─┼─▶ ./data/vaults
browser ─▶ traefik :443 ─▶ git-ui :7681 ─┤
agent   ─▶ traefik :443 ─▶ git-mcp :8080 ─┘
```

- **Traefik is the only container with published ports** (`:80` and `:443`). The
  other four are unreachable from outside the Docker network.
- **Four consumers, one directory.** `ignis`, `webdav`, `git-ui`, and `git-mcp`
  all mount the same host path, `./data/vaults`. That is the whole point: a note
  written in the browser is the same file your phone syncs over WebDAV and the
  same file the git UI or an agent commits.
- **WebDAV asks for a password and MCP asks for a bearer token.** The two browser
  apps are open, which is the trade-off this demo makes to keep the configuration
  readable:

| Host | Serves | Auth |
| --- | --- | --- |
| `ignis.<HOST_IP>.sslip.io` | Obsidian in the browser | — |
| `ignis-git.<HOST_IP>.sslip.io` | lazygit in the browser | — |
| `ignis-webdav.<HOST_IP>.sslip.io` | the vault over WebDAV | HTTP basic auth (WebDAV clients) |
| `ignis-mcp.<HOST_IP>.sslip.io` | vault git tools over MCP | Bearer token |

## 1. What you need

- Docker Engine with the Compose plugin — `docker compose version`
- `mkcert` — and once per machine, `mkcert -install` so browsers trust it
- ports **80** and **443** free — `ss -ltn | grep -E ':(80|443)\s'` prints nothing
- internet on first start: Ignis downloads Obsidian, the git UI downloads lazygit
  and ttyd, and the MCP image installs its Python dependencies

### Why there is TLS at all

Not for privacy on your own LAN. Two practical reasons:

1. **Ignis needs a browser "secure context".** Without one, Obsidian silently
   loses graph view, backlinks, the outline, and Sync — it still looks like it
   works, which is worse.
2. **WebDAV sends a password with every request.** Plain HTTP would put it on the
   wire in the clear.

So each client machine must trust mkcert's CA. On this machine `mkcert -install`
does it; on another machine, copy `mkcert -CAROOT`'s `rootCA.pem` over and import
it as a trusted root.

## 2. Configure `.env`

```bash
cp .env.sample .env
$EDITOR .env
```

| Variable | What it is |
| --- | --- |
| `HOST_IP` | this machine's LAN address; every hostname and the certificate come from it |
| `TZ` | your timezone |
| `WEBDAV_USER`, `WEBDAV_PASS` | credentials for WebDAV clients (phone/desktop) |
| `GIT_REMOTE_BASE_URL`, `GIT_USER`, `GIT_TOKEN` | your git server for the git UI (step 7; can stay unset at first) |
| `MCP_AUTH_TOKEN` | bearer token clients present to the MCP endpoint; required, 32+ random characters |
| `MCP_GIT_TOKEN` | the MCP server's own push token; separate from `GIT_TOKEN` so it can be revoked independently (empty is allowed until a remote is used) |
| `MCP_GIT_AUTHOR_NAME`, `MCP_GIT_AUTHOR_EMAIL` | author identity attached to agent commits (defaults: `hermes`, `hermes@homelab.local`) |
| `CONTROL_PLANE_TUNNEL_ID`, `CONTROL_PLANE_API_KEY` | optional OpenAI Secure MCP Tunnel ID and restricted runtime key (`Tunnels Read + Use`), required only when starting with `--profile openai` |

Generate each token with:

```bash
openssl rand -hex 32
```

If an earlier version of this stack ran on this host, rotate `GIT_TOKEN` after
upgrading. The old `run.sh` wrote it into every vault's `.git/config`, and WebDAV
served that directory. Startup now strips embedded credentials automatically, but
only rotation makes a previously exposed token safe.

Find your address with:

```bash
ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p'
```

## 3. Create the directories

```bash
mkdir -p data/vaults data/ignis certs
```

**Why this is a step:** Docker creates a missing bind-mount source as `root`, and
the containers run as uid 1000 — they would come up and then fail to write.
Creating the directories yourself avoids that.

## 4. Create the certificate

mkcert signs a certificate for the four hostnames:

```bash
mkcert -cert-file certs/notes-demo.pem -key-file certs/notes-demo-key.pem \
  ignis.192.168.1.12.sslip.io ignis-webdav.192.168.1.12.sslip.io \
  ignis-git.192.168.1.12.sslip.io ignis-mcp.192.168.1.12.sslip.io

mkcert -install      # once per machine
```

If a certificate from an earlier version already exists, reissue it: the MCP
hostname must be in the SAN list or clients reject the connection.

```
Created a new certificate valid for the following names 📜
 - "ignis.192.168.1.12.sslip.io"
 ...
```

`sslip.io` resolves `<ip>.sslip.io` to that IP, which is why no DNS or hosts-file
editing is needed. Replace `192.168.1.12` with your `HOST_IP`.

## 5. Start it

The git UI reads `config/git-ui/repos.conf` at startup. It is gitignored, so
create it from the sample first; otherwise `git-ui` restarts and never becomes
healthy:

```bash
cp config/git-ui/repos.conf.sample config/git-ui/repos.conf
```

The sample already lists `My Vault`. With the placeholder remote in `.env`, the
git UI starts and logs `skip My Vault: remote not configured`; configure the
remote in step 7.

```bash
docker compose up -d --build
docker compose ps
```

```
NAME             IMAGE                       SERVICE    STATUS                    PORTS
notes-git        alpine/git:v2.54.0          git-ui     Up 1 minute (healthy)
notes-ignis      nobbe/ignis:0.8.10          ignis      Up 1 minute (healthy)     8080/tcp
notes-mcp        notes-demo-git-mcp:latest   git-mcp    Up 1 minute (healthy)     8080/tcp
notes-traefik    traefik:v3.7.13             traefik    Up 1 minute (healthy)     :80->80/tcp, :443->443/tcp, 127.0.0.1:8080->8080/tcp
notes-webdav     rclone/rclone:1.75.0        webdav     Up 1 minute (healthy)
```

Two things to read in that table: every service says `(healthy)`, and only
`notes-traefik` publishes ports — that is the single door. The five `STATUS`
lines are what `docker compose ps` prints on one line each if the table is too
wide for your terminal:

```bash
docker compose ps --format '{{.Name}} {{.Status}}'
```

The first start is slower — Ignis pulls Obsidian from upstream, the git UI pulls
lazygit and ttyd, and Compose builds the MCP image. Watch it happen with
`docker compose logs -f`.

## 6. First run: write a note

1. Open `https://ignis.192.168.1.12.sslip.io/` — there is no login, the app opens
   straight away.
2. Ignis creates a vault named **My Vault** on its first start; open it.
3. Write something, then look on the host:

```bash
ls "data/vaults/My Vault/"
```

That file is the demo's whole claim: it now exists once, and four different
clients can read and write it. WebDAV is not allowed to touch a vault's `.git`
directory: Traefik rejects any request whose path contains a `.git` component,
and rclone filters the directory as a second layer. Clients see notes and
attachments, not repository internals or credentials.

## 7. Connect your devices

**Phone / desktop Obsidian (WebDAV).** Point any WebDAV client at:

```
https://ignis-webdav.192.168.1.12.sslip.io/
user:     WEBDAV_USER   (ignis)
password: WEBDAV_PASS
```

The URL root lists your vaults (`My Vault/`). The usual route inside Obsidian is
the *Remotely Save* community plugin with WebDAV selected; on desktop you can
also mount it — but note that mounting a WebDAV folder this way gives you the
files, not Obsidian's own sync UI.

**Git version control.** Put your server in `.env`:

```
GIT_REMOTE_BASE_URL=https://gitea.example.com
GIT_USER=your-username
GIT_TOKEN=a-token-with-repo-scope
```

Then edit the `config/git-ui/repos.conf` you created in step 5 (one line per
vault, `name|path-on-the-server|branch`):

```
My Vault|owner/notes.git|main
```

and pick it up:

```bash
docker compose up -d --force-recreate git-ui git-mcp
docker compose logs git-ui | grep -E 'initializing|skip'
```

`initializing My Vault -> main` means the vault is now a git repo whose `origin`
is your server. Credentials are supplied by a helper when git talks to the
remote, not embedded in `.git/config`. Open
`https://ignis-git.192.168.1.12.sslip.io/`, choose the vault, and commit and push
in lazygit. `skip … not configured` means the remote is still a placeholder.

## 8. Connect an AI agent over MCP

External agents reach the same vaults over MCP. The endpoint is:

```
https://ignis-mcp.192.168.1.12.sslip.io/mcp
Authorization: Bearer <MCP_AUTH_TOKEN>
```

A generic client configuration looks like this:

```json
{
  "mcpServers": {
    "vaults": {
      "type": "http",
      "url": "https://ignis-mcp.192.168.1.12.sslip.io/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_AUTH_TOKEN>"
      }
    }
  }
}
```

The server exposes fifteen tools — five for reading and editing notes/configs,
four for binary/SVG attachments, and six for git operations:

| Tool | What it does |
| --- | --- |
| `list_vaults` | lists configured vaults, branch, and whether a remote is configured |
| `list_notes` | lists note files (`.md`, `.markdown`, `.txt`, `.canvas`) in a vault, optionally under a `prefix` |
| `read_note` | reads a UTF-8 note file (or explicit config like `.gitignore`, `.obsidian/workspace.json`) from a vault (up to 256 KiB by default) |
| `search_notes` | case-insensitive search across note paths and UTF-8 contents |
| `write_note` | creates or overwrites a UTF-8 note file or explicit config file (supports `overwrite=false` for create-only) |
| `delete_note` | deletes a regular note file or explicit config file (pair with `git_commit` to record the deletion) |
| `list_attachments` | lists attachment files (`.png`, `.jpg`, `.jpeg`, `.svg`, `.pdf`) in a vault |
| `read_attachment` | reads an attachment file from a vault as base64 |
| `upload_attachment` | uploads a new attachment via base64 or ChatGPT `openai/fileParams` streaming download without overwriting |
| `delete_attachment` | deletes an existing attachment file (pair with `git_commit` to record the deletion) |
| `git_status` | branch, upstream, ahead/behind, and changed paths |
| `git_diff` | working-tree or staged diff, optionally for one path |
| `git_log` | recent commits |
| `git_commit` | commits only the named files with the agent author identity |
| `git_push` | pushes the configured branch; never force-pushes |

The intended workflow is: read or search notes with `list_notes` / `read_note` /
`search_notes`, create or edit a note with `write_note` (or write over Ignis /
WebDAV), inspect changes with `git_status` / `git_diff`, commit them with
`git_commit`, then `git_push`. `git_status` does not fetch; ahead/behind counts
reflect the last fetch or successful push. For example, an agent that created
`agent-note.md` calls:

```json
{"vault": "My Vault", "message": "add agent note", "paths": ["agent-note.md"]}
```

`git_commit` commits exactly those literal file paths. Globs, pathspec magic,
directories, and symlinks are rejected, and anything you have staged in lazygit
stays staged and out of the agent's commit. Commits are authored as
`MCP_GIT_AUTHOR_NAME <MCP_GIT_AUTHOR_EMAIL>` (default
`hermes <hermes@homelab.local>`), and `git_push` targets the branch configured in
`repos.conf`. If the remote moved in the meantime, the push fails with
`push_rejected`; pull in lazygit, then retry.

**Security.** The endpoint is token-gated and has no shell. Note tools and git
tools can only reach vaults listed in `repos.conf`, reject `..`, `.git`, and
symlinks, restrict note reads/writes/deletes to `.md`, `.markdown`, `.txt`, and
`.canvas` files (256 KiB UTF-8 limit by default) plus explicit root/Obsidian
config files (`.gitignore`, `.gitattributes`, and top-level `.obsidian/*.json`
such as `.obsidian/workspace.json`), restrict attachment tools to `.png`,
`.jpg`, `.jpeg`, `.svg`, and `.pdf` (preventing overwrites, enforcing
OpenAI `oaiusercontent.com` / `(oai|sdmnt)*.blob.core.windows.net` HTTPS +
global-IP SSRF checks on `file.download_url`, and streaming in 64 KiB chunks with an optional `ATTACHMENT_SIZE_LIMIT`), cannot
reset, checkout, or force-push, and ignore git hooks, fsmonitor, textconv, and
gpg signing. A `.git` file or symlink inside a vault is refused rather than
followed. Anyone with `MCP_AUTH_TOKEN` (or access to an attached tunnel) can
read/edit notes, upload/delete attachments, commit, and push those vaults, so
treat both `MCP_AUTH_TOKEN` and `CONTROL_PLANE_API_KEY` as secrets.

### Connect ChatGPT via OpenAI Secure MCP Tunnel

Because ChatGPT runs in OpenAI's cloud, it cannot reach a private LAN IP or
trust a local `mkcert` certificate. Instead of exposing ports to the public
internet, this stack includes an optional `openai-tunnel` service running
OpenAI's official outbound-only `tunnel-client` (`ghcr.io/openai/tunnel-client`):

```
ChatGPT ─▶ OpenAI Control Plane ◀═(outbound HTTPS)═ openai-tunnel ─▶ http://git-mcp:8080/mcp
```

`openai-tunnel` connects outbound to `api.openai.com:443` and forwards MCP calls
over the private `notes` Docker network to `git-mcp`, injecting
`Authorization: Bearer <MCP_AUTH_TOKEN>` via `MCP_EXTRA_HEADERS` (with
`MCP_OAUTH_DISCOVERY=disabled`). That header is sent only to `git-mcp` and never
forwarded to OpenAI.

1. In the OpenAI Platform (**Organization Settings -> Tunnels**), create a
   tunnel and copy its `tunnel_id` (`tunnel_...`). Link it to the ChatGPT
   workspace you want to use.
2. Create a **Restricted API Key** with **Tunnels Read + Use** permissions.
3. Put both in `.env`:

   ```dotenv
   CONTROL_PLANE_TUNNEL_ID=tunnel_...
   CONTROL_PLANE_API_KEY=sk-...
   ```

4. Start (or recreate) the stack with the `openai` profile enabled:

   ```bash
   docker compose --profile openai up -d --build
   docker compose --profile openai ps
   ```

   Wait until `notes-openai-tunnel` shows `(healthy)` (checked via
   `http://127.0.0.1:8080/readyz` inside the container). If it stays unhealthy,
   inspect `docker compose --profile openai logs openai-tunnel`.
5. In ChatGPT (**Settings -> Connectors / Apps -> Developer Mode**), add a
   custom MCP connector using **Tunnel** and enter your `CONTROL_PLANE_TUNNEL_ID`.
6. Test in ChatGPT by asking it to list your vaults, write a test note in
   `My Vault`, and commit and push it (if a remote is configured in step 7).

To rotate, replace `MCP_AUTH_TOKEN` in `.env` and recreate the service:

```bash
docker compose up -d --force-recreate git-mcp
```

`MCP_GIT_TOKEN` rotates separately at your git host; update `.env` and recreate
`git-mcp`. It can be empty while you work without a remote, in which case push
returns `no_credentials`.

## 9. Check it yourself

```bash
./scripts/verify.sh
```

24 core checks (plus an `openai-tunnel` health check when `--profile openai` is
running), one `PASS`/`FAIL` line each, non-zero exit if anything fails. It reads
`.env` the same way Compose does and changes nothing. It covers: all five core
containers healthy (and `openai-tunnel` if present); the Traefik dashboard
reachable only on `127.0.0.1`; both browser apps answering directly; plain HTTP
redirecting to HTTPS; MCP health, token rejection, and an authenticated
initialize; the isolation headers; WebDAV rejecting missing and wrong
credentials and listing your vault; `.git` being unreadable and unwritable over
WebDAV; no credentials left in vault remotes; and a WebDAV write landing in the
directory Ignis serves.

One thing it cannot check is what the browser does with the certificate: **graph
view rendering is the proof that the secure context is real.** If graph view is
missing, the client does not trust mkcert's CA — install `rootCA.pem` there.

## 10. Everyday commands

```bash
docker compose up -d                    # start core stack
docker compose --profile openai up -d   # start core stack + OpenAI tunnel
docker compose ps                       # what is running, and whether each one is healthy
docker compose logs -f                  # follow everything
docker compose logs -f ignis
docker compose down                     # stop; your notes and config stay
```

"Healthy" is each container's own self-test: Traefik's ping endpoint, WebDAV's
port, Ignis's HTTP root, ttyd answering, the MCP `/health` endpoint, and (when
enabled) the tunnel client's `/readyz` endpoint.

## 11. When it goes wrong

- **`Bind for 0.0.0.0:443 failed: port is already allocated`** — something else
  holds the port. After freeing it, run `docker compose down && docker compose up -d`.
  A `restart` is not enough: a container left by a failed port bind still reports
  *healthy*, because its health check runs inside it while nothing answers on the
  published port.
- **`git-ui` restarts in a loop: `could not lock config file .git/config`** — a
  vault's `.git` is owned by another user, usually because git ran as root. Fix:
  `sudo chown -R 1000:1000 data/vaults`.
- **A vault is in the menu but has no remote** — when `GIT_REMOTE_BASE_URL` is
  configured, `run.sh` rewrites `origin` silently at startup, so no `skip` message
  appears; with the placeholder it leaves an existing origin alone. Check with
  `git -C "data/vaults/My Vault" remote -v`.
- **The git UI opens straight into a shell with no login** — that is expected here;
  keep the stack on a trusted network, or put an auth layer in front of it. The
  MCP endpoint is token-gated, but it has no user accounts.
- **MCP returns `401 Unauthorized`** — the client is missing
  `Authorization: Bearer <MCP_AUTH_TOKEN>` or the token does not match `.env`.
  After changing the token, recreate `git-mcp`.
- **MCP returns `421 Misdirected Request` or a TLS error** — the certificate does
  not include `ignis-mcp.<HOST_IP>.sslip.io`, or the client does not trust the
  mkcert CA. Reissue the certificate (step 4) and install `rootCA.pem` on the
  client.
- **MCP returns `no_remote` or `no_credentials`** — the vault's remote is still
  the `CHANGE_ME` placeholder, or `MCP_GIT_TOKEN` is empty. Configure the remote in
  `.env` and `repos.conf`, then recreate `git-mcp`.
- **MCP returns `branch_mismatch`** — the vault is checked out on a branch other
  than the one in `repos.conf`. Switch it back in lazygit; MCP does not switch
  branches.
- **MCP returns `push_rejected`** — the remote has commits you do not have. Pull in
  lazygit, then retry the push.
- **Changes to `git-mcp/` do not appear** — `docker compose up -d` can reuse the
  old image. Run `docker compose up -d --build` after editing the server.
- **Graph view is empty, Sync is missing** — the browser does not trust the
  certificate. Run `mkcert -install` on that machine, or import its CA.

## 12. The files

| File | What it is |
| --- | --- |
| `docker-compose.yml` | the entire stack: services, volumes, routing, headers, WebDAV auth |
| `.env` | secrets and `HOST_IP` (gitignored; `.env.sample` is the template) |
| `config/traefik/tls.yml` | which certificate Traefik serves |
| `config/git-ui/run.sh` | prepares the git UI: installs lazygit/ttyd, wires remotes |
| `config/git-ui/menu.sh` | the vault menu the terminal shows |
| `config/git-ui/repos.conf` | vault → git repo mapping (gitignored) |
| `git-mcp/` | MCP server: vault-scoped git tools, bearer auth, tests |
| `data/vaults/` | your vaults — the only thing here worth backing up |
| `scripts/verify.sh` | the optional 24-check health check |

`docker compose config` prints the whole stack as Compose resolves it — every
routing rule, every mounted path — which is the fastest way to see what is
actually running rather than what you think is running.

**Removing the MCP layer.** Stop and remove the `git-mcp` service and its
`MCP_*` variables. The clean `origin` URLs and the credential helper are
backward compatible with lazygit, and the WebDAV `.git` exclusion can be removed
independently.
