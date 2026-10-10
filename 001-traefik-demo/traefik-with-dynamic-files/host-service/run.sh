#!/usr/bin/env sh
# Serve the sample page on the host at http://localhost:8000.
# Traefik (in a container) reaches it at http://host.docker.internal:8000.
set -e
cd "$(dirname "$0")"
exec python3 -m http.server 8000 --bind 0.0.0.0
