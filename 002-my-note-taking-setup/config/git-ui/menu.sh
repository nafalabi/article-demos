#!/bin/sh
# Numbered vault menu, then lazygit in the chosen vault directory.
while true; do
  echo "=== Ignis vaults ==="
  i=0
  while IFS='|' read -r name remote branch; do
    case "$name" in ''|\#*) continue ;; esac
    i=$((i+1))
    eval "menu_$i=\"$name\""
    echo "  $i) $name"
  done < /opt/git-ui/repos.conf
  [ "$i" -eq 0 ] && { echo "no vaults configured"; sleep 30; continue; }
  printf 'Pick [1..%s] (Ctrl+C has no effect here): ' "$i"
  read -r pick
  name="$(eval "printf '%s' \"\$menu_$pick\"")"
  if [ -z "$name" ] || [ ! -d "/vaults/$name" ]; then echo "invalid pick"; continue; fi
  ( cd "/vaults/$name" && lazygit )
  echo "lazygit exited, back to menu..."
done
