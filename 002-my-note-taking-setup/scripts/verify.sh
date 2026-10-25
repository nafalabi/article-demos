#!/usr/bin/env bash
# Optional end-to-end check of the running demo. Prints one PASS/FAIL line per
# check and exits non-zero if anything fails. It reads .env itself, the same way
# Compose does; it changes nothing.
#
#   ./scripts/verify.sh
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -f .env ]; then
  echo "FAIL: no .env; copy .env.sample to .env and fill it in"
  exit 1
fi
set -a
# shellcheck disable=SC1090
. ./.env
set +a

zone="$HOST_IP.sslip.io"
IGNIS_HOST="ignis.$zone"
IGNIS_WEBDAV_HOST="ignis-webdav.$zone"
IGNIS_GIT_HOST="ignis-git.$zone"

CA="$(mkcert -CAROOT 2>/dev/null)/rootCA.pem"
if [ ! -f "$CA" ]; then
  echo "FAIL: no mkcert CA; install mkcert and run 'mkcert -install'"
  exit 1
fi

WEBDAV_AUTH="$WEBDAV_USER:$WEBDAV_PASS"

failures=0
check() { # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then
    printf 'PASS %s (%s)\n' "$1" "$3"
  else
    printf 'FAIL %s (expected %s, got %s)\n' "$1" "$2" "$3"
    failures=$((failures + 1))
  fi
}
code() { curl -s -o /dev/null -w '%{http_code}' --cacert "$CA" "$@"; }

echo "== containers"
for svc in traefik ignis webdav git-ui; do
  state="$(docker compose ps --format '{{.Service}} {{.Health}}' | awk -v s="$svc" '$1==s{print $2}')"
  check "service $svc healthy" "healthy" "${state:-missing}"
done

echo "== configuration"
check "compose config renders" "0" "$(docker compose config --quiet >/dev/null 2>&1; echo $?)"
check "dashboard is loopback-only" "1" "$(docker compose config | grep -B2 'published: \"8080\"' | grep -c 'host_ip: 127.0.0.1')"

echo "== routing"
check "ignis serves the app" "200" "$(code "https://$IGNIS_HOST/")"
check "git ui serves the terminal" "200" "$(code "https://$IGNIS_GIT_HOST/")"
check "plain http redirects" "302" "$(curl -s -o /dev/null -w '%{http_code}' "http://$IGNIS_HOST/")"

echo "== isolation headers"
headers="$(curl -s -o /dev/null -D - --cacert "$CA" "https://$IGNIS_HOST/")"
check "ignis sets COOP" "same-origin" "$(printf '%s' "$headers" | sed -n 's/^[Cc]ross-[Oo]rigin-[Oo]pener-[Pp]olicy: //p' | tr -d '\r')"
check "ignis sets COEP" "credentialless" "$(printf '%s' "$headers" | sed -n 's/^[Cc]ross-[Oo]rigin-[Ee]mbedder-[Pp]olicy: //p' | tr -d '\r')"
git_headers="$(curl -s -o /dev/null -D - --cacert "$CA" "https://$IGNIS_GIT_HOST/")"
check "git ui sets CORP" "same-site" "$(printf '%s' "$git_headers" | sed -n 's/^[Cc]ross-[Oo]rigin-[Rr]esource-[Pp]olicy: //p' | tr -d '\r')"

echo "== webdav"
check "webdav rejects missing credentials" "401" "$(code -X PROPFIND -H 'Depth: 1' "https://$IGNIS_WEBDAV_HOST/")"
check "webdav rejects a wrong password" "401" "$(code -X PROPFIND -H 'Depth: 1' -u "$WEBDAV_USER:wrong-password" "https://$IGNIS_WEBDAV_HOST/")"
check "webdav lists vaults with credentials" "207" "$(code -X PROPFIND -H 'Depth: 1' -u "$WEBDAV_AUTH" "https://$IGNIS_WEBDAV_HOST/")"
listing="$(curl -s -X PROPFIND -H 'Depth: 1' -u "$WEBDAV_AUTH" --cacert "$CA" "https://$IGNIS_WEBDAV_HOST/")"
case "$listing" in
  *"My Vault"*) check "webdav exposes the vault Ignis created" "yes" "yes" ;;
  *) check "webdav exposes the vault Ignis created (open Ignis once so it creates 'My Vault')" "yes" "no" ;;
esac

echo "== one directory, three consumers"
printf '# verify\n' > /tmp/verify-note.md
put="$(curl -s -o /dev/null -w '%{http_code}' -T /tmp/verify-note.md -u "$WEBDAV_AUTH" --cacert "$CA" "https://$IGNIS_WEBDAV_HOST/My%20Vault/verify-note.md")"
case "$put" in
  200|201|204)
    if [ -f "data/vaults/My Vault/verify-note.md" ]; then check "a WebDAV write lands in the directory Ignis serves" "yes" "yes"; else check "a WebDAV write lands in the directory Ignis serves" "yes" "no"; fi ;;
  *) check "a WebDAV write lands in the directory Ignis serves" "2xx" "$put" ;;
esac
rm -f /tmp/verify-note.md "data/vaults/My Vault/verify-note.md"

echo
if [ "$failures" -eq 0 ]; then
  echo "all checks passed"
  exit 0
fi
echo "$failures check(s) failed"
exit 1
