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

git config --global credential.helper '!f() { printf "username=%s\npassword=%s\n" "$GIT_USER" "$GIT_TOKEN"; }; f'
clean_url() { printf '%s' "$1" | sed -E 's#^(https?://)[^/@:]+:[^/@]*@#\1#'; }

# Older versions embedded credentials in remote URLs. Scrub every URL,
# pushurl, and insteadOf value before anything else, including vaults that are
# no longer in repos.conf.
find /vaults -path '*/.git/config' -type f 2>/dev/null | while IFS= read -r config; do
  keys=$(git config --file "$config" --name-only --get-regexp \
    '^(remote\..*\.(url|pushurl)|url\..*\.(insteadOf|pushInsteadOf))$' 2>/dev/null) || continue
  for key in $keys; do
    value=$(git config --file "$config" --get "$key")
    cleaned=$(clean_url "$value")
    [ "$cleaned" = "$value" ] || git config --file "$config" "$key" "$cleaned"
  done
done

while IFS='|' read -r name path branch; do
  name=$(printf '%s' "$name" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  path=$(printf '%s' "$path" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  branch=$(printf '%s' "$branch" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  case "$name" in ''|\#*) continue ;; esac
  dir="/vaults/$name"
  remote=$(clean_url "${GIT_REMOTE_BASE_URL%/}/$path")
  case "$remote" in
    *CHANGE_ME*) remote_configured=false ;;
    *) remote_configured=true ;;
  esac
  if [ ! -d "$dir" ]; then echo "skip missing vault dir: $dir"; continue; fi
  if [ -L "$dir/.git" ] || { [ -e "$dir/.git" ] && [ ! -d "$dir/.git" ]; }; then
    echo "refusing $name: .git is not a directory" >&2
    continue
  fi
  if [ ! -d "$dir/.git" ]; then
    if [ "$remote_configured" = false ]; then echo "skip $name: remote not configured"; continue; fi
    echo "initializing $name -> $branch"
    git -C "$dir" init -b "$branch"
    git -C "$dir" remote add origin "$remote" 2>/dev/null \
      || git -C "$dir" remote set-url origin "$remote"
  else
    if current=$(git -C "$dir" remote get-url origin 2>/dev/null); then
      if [ "$remote_configured" = true ]; then
        git -C "$dir" remote set-url origin "$remote"
      else
        cleaned=$(clean_url "$current")
        if [ "$cleaned" != "$current" ]; then
          git -C "$dir" remote set-url origin "$cleaned"
        fi
      fi
    elif [ "$remote_configured" = true ]; then
      git -C "$dir" remote add origin "$remote" 2>/dev/null \
        || git -C "$dir" remote set-url origin "$remote"
    else
      echo "skip $name: remote not configured"
    fi
  fi
done < /opt/git-ui/repos.conf

exec ttyd -W -p "$GIT_UI_PORT" -t "titleFixed=Ignis Git" /opt/git-ui/menu.sh
