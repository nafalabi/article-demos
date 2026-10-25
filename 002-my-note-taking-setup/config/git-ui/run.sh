#!/bin/sh
# Prepares the browser git UI: installs lazygit and ttyd, points each vault at
# its git remote, then serves lazygit over ttyd.
#
# Authentication is not handled here: this demo leaves the terminal open on the
# LAN, behind Traefik's TLS entrypoint.
set -eu

if ! command -v git >/dev/null 2>&1; then echo "git not found in image" >&2; exit 1; fi
if command -v curl >/dev/null 2>&1; then FETCH="curl -fsSL -o"; elif command -v wget >/dev/null 2>&1; then FETCH="wget -O"; else echo "need curl or wget in image" >&2; exit 1; fi

export HOME=/tmp/git-home
mkdir -p "$HOME" /tmp/git-ui-bin
export PATH="/tmp/git-ui-bin:$PATH"

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) LG_ARCH=x86_64; TTYD_BIN=ttyd.x86_64 ;;
  aarch64) LG_ARCH=arm64; TTYD_BIN=ttyd.aarch64 ;;
  *) echo "unsupported arch: $ARCH" >&2; exit 1 ;;
esac

if ! lazygit --version 2>/dev/null | grep -q "$LAZYGIT_VERSION"; then
  echo "installing lazygit $LAZYGIT_VERSION for $LG_ARCH"
  $FETCH /tmp/lazygit.tar.gz "https://github.com/jesseduffield/lazygit/releases/download/v${LAZYGIT_VERSION}/lazygit_${LAZYGIT_VERSION}_Linux_${LG_ARCH}.tar.gz"
  tar -xzf /tmp/lazygit.tar.gz -C /tmp/git-ui-bin lazygit
  rm -f /tmp/lazygit.tar.gz
fi

if ! ttyd --version 2>/dev/null | grep -q "$TTYD_VERSION"; then
  echo "installing ttyd $TTYD_VERSION ($TTYD_BIN)"
  $FETCH /tmp/git-ui-bin/ttyd "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/${TTYD_BIN}"
  chmod +x /tmp/git-ui-bin/ttyd
fi

lazygit --version
ttyd --version

git config --global user.name "$GIT_NAME"
git config --global user.email "$GIT_EMAIL"
git config --global --add safe.directory '*'
git config --global init.defaultBranch main

inject_creds() {
  case "$1" in
    https://*) printf '%s' "$1" | sed "s#^https://#https://${GIT_USER}:${GIT_TOKEN}@#" ;;
    *) printf '%s' "$1" ;;
  esac
}

while IFS='|' read -r name path branch; do
  case "$name" in ''|\#*) continue ;; esac
  dir="/vaults/$name"
  remote="${GIT_REMOTE_BASE_URL%/}/$path"
  if [ ! -d "$dir" ]; then echo "skip missing vault dir: $dir"; continue; fi
  if [ ! -d "$dir/.git" ]; then
    case "$remote" in *CHANGE_ME*) echo "skip $name: remote not configured"; continue ;; esac
    echo "initializing $name -> $branch"
    git -C "$dir" init -b "$branch"
    git -C "$dir" remote add origin "$(inject_creds "$remote")"
  else
    if git -C "$dir" remote get-url origin >/dev/null 2>&1; then
      git -C "$dir" remote set-url origin "$(inject_creds "$remote")"
    else
      case "$remote" in *CHANGE_ME*) echo "skip $name: remote not configured" ;; *) git -C "$dir" remote add origin "$(inject_creds "$remote")" ;; esac
    fi
  fi
done < /opt/git-ui/repos.conf

exec ttyd -W -p "$GIT_UI_PORT" -t "titleFixed=Ignis Git" /opt/git-ui/menu.sh
