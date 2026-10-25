# Note-Taking Stack — Setup Guide

Five containers, four hostnames, one directory of markdown files. This guide
takes you from nothing to a working setup, and explains how the pieces fit
together as it goes.

## What you get

| Service | Image | Its job |
| --- | --- | --- |
| `traefik` | `traefik:v3.7.13` | the only door in: TLS, routing, headers, and the auth gate |
| `authelia` | `authelia/authelia:4.39.24` | the login page, and the yes/no answer behind it |
| `ignis` | `nobbe/ignis:0.8.10` | Obsidian, running in the browser |
| `webdav` | `rclone/rclone:1.75.0` | the same vault over WebDAV, for phone and desktop |
| `git-ui` | `alpine/git:v2.54.0` | lazygit in the browser (lazygit 0.64.1 + ttyd 1.7.7) |

### How they work together

```
browser ─▶ traefik :443 ─▶ authelia  (login)     ─▶ ignis  :8080 ─┐
phone   ─▶ traefik :443 ─▶ (basic auth)          ─▶ webdav :8081 ─┼─▶ ./data/vaults
browser ─▶ traefik :443 ─▶ authelia  (login)     ─▶ git-ui :7681 ─┘
```

- **Traefik is the only container with published ports** (`:80` and `:443`). The
  other four are unreachable from outside the Docker network.
- **Authelia never touches your notes.** Before Traefik forwards a request it
  asks Authelia "may this through?", and copies the username it returns into the
  git UI so the terminal knows who is typing.
- **Three consumers, one directory.** `ignis`, `webdav`, and `git-ui` all mount
  the same host path, `./data/vaults`. That is the whole point: a note written in
  the browser is the same file your phone syncs over WebDAV and the same file the
  git UI commits.
- **Each consumer has its own way in**, which is why the auth differs:

| Host | Serves | Auth |
| --- | --- | --- |
| `ignis.<HOST_IP>.sslip.io` | Obsidian in the browser | Authelia login |
| `ignis-git.<HOST_IP>.sslip.io` | lazygit in the browser | Authelia login |
| `ignis-webdav.<HOST_IP>.sslip.io` | the vault over WebDAV | HTTP basic auth (WebDAV clients) |
| `auth.<HOST_IP>.sslip.io` | the login page itself | — |

## 1. What you need

- Docker Engine with the Compose plugin — `docker compose version`
- `mkcert` — and once per machine, `mkcert -install` so browsers trust it
- ports **80** and **443** free — `ss -ltn | grep -E ':(80|443)\s'` prints nothing
- internet on first start: Ignis downloads Obsidian, the git UI downloads lazygit
  and ttyd

### Why there is TLS at all

Not for privacy on your own LAN. Two practical reasons:

1. **Authelia refuses to run without it.** Given an `http://` address it stops
   with `option 'authelia_url' does not have a secure scheme`.
2. **Ignis needs a browser "secure context".** Without one, Obsidian silently
   loses graph view, backlinks, the outline, and Sync — it still looks like it
   works, which is worse.

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
| `AUTHELIA_SESSION_SECRET` | 64 random chars — `openssl rand -hex 32` |
| `AUTHELIA_STORAGE_ENCRYPTION_KEY` | 64 random chars, different from the above |
| `AUTHELIA_DEMO_PASSWORD` | the password you will log in with |
| `WEBDAV_USER`, `WEBDAV_PASS` | credentials for WebDAV clients (phone/desktop) |
| `GIT_REMOTE_BASE_URL`, `GIT_USER`, `GIT_TOKEN` | your git server (step 8; can stay unset at first) |

Find your address with:

```bash
ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p'
```

## 3. Create the directories

```bash
mkdir -p data/vaults data/ignis data/authelia certs
```

**Why this is a step:** Docker creates a missing bind-mount source as `root`, and
the containers run as uid 1000 — they would come up and then fail to write.
Creating the directories yourself avoids that.

## 4. Create the login

Authelia stores a *hash*, never the password. Generate it and paste it in:

```bash
docker run --rm authelia/authelia:4.39.24 \
  authelia crypto hash generate argon2 --password 'demo-pass' --no-confirm
```

```
Digest: $argon2id$v=19$m=65536,t=3,p=4$Sk9...$9fQ...
```

Copy everything **after `Digest: `** into `config/authelia/users_database.yml`,
replacing `CHANGE_ME_ARGON2ID_HASH`. The username in that file is `demo`.

To change the password later: edit `AUTHELIA_DEMO_PASSWORD` in `.env`, re-run the
command above, replace the hash, then `docker compose restart authelia`. Editing
`.env` alone changes nothing — the hash is what the login checks, and `.env` is
only there so the health check can log in and prove it works.

## 5. Create the certificate

mkcert signs a certificate for the four hostnames:

```bash
mkcert -cert-file certs/notes-demo.pem -key-file certs/notes-demo-key.pem \
  ignis.192.168.1.12.sslip.io ignis-webdav.192.168.1.12.sslip.io \
  ignis-git.192.168.1.12.sslip.io auth.192.168.1.12.sslip.io

mkcert -install      # once per machine
```

```
Created a new certificate valid for the following names 📜
 - "ignis.192.168.1.12.sslip.io"
 ...
```

`sslip.io` resolves `<ip>.sslip.io` to that IP, which is why no DNS or hosts-file
editing is needed. Replace `192.168.1.12` with your `HOST_IP` (it appears here
and in `config/authelia/configuration.yml`).

## 6. Start it

```bash
docker compose up -d
docker compose ps
```

```
NAME             IMAGE                       SERVICE    STATUS                    PORTS
notes-authelia   authelia/authelia:4.39.24   authelia   Up 1 minute (healthy)     9091/tcp
notes-git        alpine/git:v2.54.0          git-ui     Up 1 minute (healthy)
notes-ignis      nobbe/ignis:0.8.10          ignis      Up 1 minute (healthy)     8080/tcp
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

The first start is slower — Ignis pulls Obsidian from upstream and the git UI
pulls lazygit and ttyd. Watch it happen with `docker compose logs -f`.

## 7. First run: log in and write a note

1. Open `https://ignis.192.168.1.12.sslip.io/` — you are bounced to the login page.
2. Log in as **`demo`** with your `AUTHELIA_DEMO_PASSWORD`. There is no second factor.
3. Ignis creates a vault named **My Vault** on its first start; open it.
4. Write something, then look on the host:

```bash
ls "data/vaults/My Vault/"
```

That file is the demo's whole claim: it now exists once, and three different
clients can read and write it.

## 8. Connect your devices

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

Then list the vaults in `config/git-ui/repos.conf` (one per line,
`name|path-on-the-server|branch`):

```
My Vault|owner/notes.git|main
```

and pick it up:

```bash
docker compose up -d --force-recreate git-ui
docker compose logs git-ui | grep -E 'initializing|skip'
```

`initializing My Vault -> main` means the vault is now a git repo whose `origin`
is your server, with the token embedded in the remote URL. Open
`https://ignis-git.192.168.1.12.sslip.io/`, choose the vault, and commit and push
in lazygit. `skip … not configured` means the remote is still a placeholder.

## 9. Check it yourself

```bash
./scripts/verify.sh
```

27 checks, one `PASS`/`FAIL` line each, non-zero exit if anything fails. It reads
`.env` the same way Compose does and changes nothing. It covers: all five
containers healthy; the Traefik dashboard reachable only on `127.0.0.1`; both
browser apps redirecting to the login; a real login, plus a rejected wrong
password; the session cookie's domain; the isolation headers; WebDAV rejecting
missing and wrong credentials and listing your vault; and a WebDAV write landing
in the directory Ignis serves.

One thing it cannot check is what the browser does with the certificate: **graph
view rendering is the proof that the secure context is real.** If graph view is
missing, the client does not trust mkcert's CA — install `rootCA.pem` there.

## 10. Everyday commands

```bash
docker compose up -d      # start
docker compose ps         # what is running, and whether each one is healthy
docker compose logs -f    # follow everything
docker compose logs -f ignis
docker compose down       # stop; your notes and config stay
```

"Healthy" is each container's own self-test: Traefik's ping endpoint, Authelia's
`/api/health`, WebDAV's port, Ignis's HTTP root, and ttyd answering (with the
header the proxy injects).

## 11. When it goes wrong

- **`Bind for 0.0.0.0:443 failed: port is already allocated`** — something else
  holds the port. After freeing it, run `docker compose down && docker compose up -d`.
  A `restart` is not enough: a container left by a failed port bind still reports
  *healthy*, because its health check runs inside it while nothing answers on the
  published port.
- **`git-ui` restarts in a loop: `could not lock config file .git/config`** — a
  vault's `.git` is owned by another user, usually because git ran as root. Fix:
  `sudo chown -R 1000:1000 data/vaults`.
- **A vault is in the menu but has no remote** — if a vault already has a repo,
  `run.sh` rewrites `origin` silently at startup, so no `skip` message appears.
  Check with `git -C "data/vaults/My Vault" remote -v`.
- **Authelia restarts: `user authentication database file doesn't exist`** — step 4
  was skipped. Create `config/authelia/users_database.yml` from the sample.
- **Login page loads, login fails** — the hash in `users_database.yml` and the
  password you are typing come from different runs of step 4. Regenerate.
- **Graph view is empty, Sync is missing** — the browser does not trust the
  certificate. Run `mkcert -install` on that machine, or import its CA.

## 12. The files

| File | What it is |
| --- | --- |
| `docker-compose.yml` | the entire stack: services, volumes, routing, headers, auth |
| `.env` | secrets and `HOST_IP` (gitignored; `.env.sample` is the template) |
| `config/traefik/tls.yml` | which certificate Traefik serves |
| `config/authelia/configuration.yml` | who may log in, and the cookie's domain |
| `config/authelia/users_database.yml` | the user and password hash (gitignored) |
| `config/git-ui/run.sh` | prepares the git UI: installs lazygit/ttyd, wires remotes |
| `config/git-ui/menu.sh` | the vault menu the terminal shows |
| `config/git-ui/repos.conf` | vault → git repo mapping (gitignored) |
| `data/vaults/` | your vaults — the only thing here worth backing up |
| `scripts/verify.sh` | the optional 27-check health check |

`docker compose config` prints the whole stack as Compose resolves it — every
routing rule, every mounted path — which is the fastest way to see what is
actually running rather than what you think is running.
