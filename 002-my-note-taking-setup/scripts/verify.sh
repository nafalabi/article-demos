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

: "${MCP_AUTH_TOKEN:?set MCP_AUTH_TOKEN in .env}"
if [ "${#MCP_AUTH_TOKEN}" -lt 32 ] || [ "${MCP_AUTH_TOKEN#CHANGE_ME}" != "$MCP_AUTH_TOKEN" ]; then
  echo "FAIL: MCP_AUTH_TOKEN must be 32+ random characters (openssl rand -hex 32)"
  exit 1
fi

zone="$HOST_IP.sslip.io"
IGNIS_HOST="ignis.$zone"
IGNIS_WEBDAV_HOST="ignis-webdav.$zone"
IGNIS_GIT_HOST="ignis-git.$zone"
MCP_HOST="ignis-mcp.$zone"

CA="$(mkcert -CAROOT 2>/dev/null)/rootCA.pem"
if [ ! -f "$CA" ]; then
  echo "FAIL: no mkcert CA; install mkcert and run 'mkcert -install'"
  exit 1
fi

WEBDAV_AUTH="$WEBDAV_USER:$WEBDAV_PASS"
MCP_AUTH="$MCP_AUTH_TOKEN"

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
for svc in traefik ignis webdav git-ui git-mcp; do
  state="$(docker compose ps --format '{{.Service}} {{.Health}}' | awk -v s="$svc" '$1==s{print $2}')"
  check "service $svc healthy" "healthy" "${state:-missing}"
done
if docker compose ps -a --format '{{.Service}}' | grep -qx 'openai-tunnel'; then
  state="$(docker compose ps --format '{{.Service}} {{.Health}}' | awk '$1=="openai-tunnel"{print $2}')"
  check "service openai-tunnel healthy" "healthy" "${state:-missing}"
fi

echo "== configuration"
check "compose config renders" "0" "$(docker compose config --quiet >/dev/null 2>&1; echo $?)"
check "dashboard is loopback-only" "1" "$(docker compose config | grep -B2 'published: \"8080\"' | grep -c 'host_ip: 127.0.0.1')"

echo "== routing"
check "ignis serves the app" "200" "$(code "https://$IGNIS_HOST/")"
check "git ui serves the terminal" "200" "$(code "https://$IGNIS_GIT_HOST/")"
check "plain http redirects" "302" "$(curl -s -o /dev/null -w '%{http_code}' "http://$IGNIS_HOST/")"

echo "== mcp"
mcp_init_body='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}'
check "mcp health" "200" "$(code "https://$MCP_HOST/health")"
check "mcp rejects missing and wrong tokens" "401 401" \
  "$(code -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$mcp_init_body" "https://$MCP_HOST/mcp") $(code -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H 'Authorization: Bearer wrong-token' -d "$mcp_init_body" "https://$MCP_HOST/mcp")"
init_code="$(curl -s -o /tmp/verify-mcp-init -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H "Authorization: Bearer $MCP_AUTH" -d "$mcp_init_body" --cacert "$CA" "https://$MCP_HOST/mcp")"
if [ "$init_code" = "200" ] && grep -q '"protocolVersion"' /tmp/verify-mcp-init && grep -q '"name":"git-mcp"' /tmp/verify-mcp-init; then
  check "mcp answers an authenticated initialize" "yes" "yes"
else
  check "mcp answers an authenticated initialize" "yes" "no"
fi
rm -f /tmp/verify-mcp-init

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

echo "== dotgit is not exposed"
vault_name="$(awk -F'|' '!/^[[:space:]]*(#|$)/ {print $1; exit}' config/git-ui/repos.conf 2>/dev/null)"
vault_name="${vault_name:-My Vault}"
vault_url="${vault_name// /%20}"
guard_dir="verify-guard"
mkdir -p "data/vaults/$guard_dir"
dotgit_read="$(code -u "$WEBDAV_AUTH" "https://$IGNIS_WEBDAV_HOST/$vault_url/.git/config")"
check "webdav denies .git reads" "403" "$dotgit_read"
dotgit_writes="$(
  printf 'x\n' >/tmp/verify-dotgit-write
  curl -s -o /dev/null -w '%{http_code} ' -T /tmp/verify-dotgit-write -u "$WEBDAV_AUTH" --cacert "$CA" "https://$IGNIS_WEBDAV_HOST/$vault_url/.git/verify-should-not-exist"
  curl -s -o /dev/null -w '%{http_code} ' -T /tmp/verify-dotgit-write -u "$WEBDAV_AUTH" --cacert "$CA" "https://$IGNIS_WEBDAV_HOST/$vault_url/.git/hooks/pre-commit"
  curl -s -o /dev/null -w '%{http_code}' -X MKCOL -u "$WEBDAV_AUTH" --cacert "$CA" "https://$IGNIS_WEBDAV_HOST/$guard_dir/.git"
)"
check "webdav denies .git writes" "403 403 403" "$dotgit_writes"
rm -f /tmp/verify-dotgit-write "data/vaults/$vault_name/.git/verify-should-not-exist"
rm -rf "data/vaults/$guard_dir"

leaked="$(grep -REl '^[[:space:]]*(url|pushurl) = https?://[^/@]+:[^/@]*@' data/vaults 2>/dev/null || true)"
check "no credentials in vault remotes" "" "$leaked"

echo "== one directory, four consumers"
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
