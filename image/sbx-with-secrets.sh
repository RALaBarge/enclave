#!/bin/bash
# Run a command with the core's secrets (OpenRouter, Requesty keys, ...) in its environment, fetched from
# Bitwarden Secrets Manager. Installed as /usr/local/bin/sbx-with-secrets and used by pi-bridge.service.
#
#   /etc/sbx/bws-token  machine-account access token (root:pibridge 0640). Put there by hand, never in git or the image.
#   /etc/sbx/bws.env    BWS_PROJECT_ID=<uuid of the Bitwarden project holding the secrets>
#
# No token file: the command just runs without secrets (offline image, first boot). Token present but Bitwarden
# unreachable or the token wrong: bws exits non-zero and so does this, so the service fails visibly instead of
# starting without its keys. The token itself is stripped from the command's environment.
set -euo pipefail
token_file=/etc/sbx/bws-token
env_file=/etc/sbx/bws.env
if [ -s "$token_file" ]; then
  [ -r "$env_file" ] && . "$env_file"
  : "${BWS_PROJECT_ID:?BWS_PROJECT_ID missing in $env_file}"
  BWS_ACCESS_TOKEN=$(tr -d '[:space:]' < "$token_file")
  export BWS_ACCESS_TOKEN
  exec bws run --project-id "$BWS_PROJECT_ID" -- env -u BWS_ACCESS_TOKEN "$@"
fi
exec "$@"
