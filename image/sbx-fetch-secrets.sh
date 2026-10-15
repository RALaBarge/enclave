#!/bin/bash
# Fetch the core's secrets (OpenRouter, Requesty keys, ...) from Bitwarden Secrets Manager into a RAM-only env file.
# Installed as /usr/local/bin/sbx-fetch-secrets and run by pi-bridge.service as ExecStartPre=+ (that is, as root, before the
# service user's process exists). The service then loads the file with EnvironmentFile=.
#
#   /etc/sbx/bws-token  machine-account access token. root:root 0600, so the account Pi runs as can NOT read it.
#   /etc/sbx/bws.env    BWS_PROJECT_ID=<uuid of the Bitwarden project holding the secrets>
#   /run/sbx/secrets.env  output: KEY="value" lines, root:pibridge 0640, on tmpfs (gone at reboot or service stop)
#
# No token file (or empty): writes an empty env file and succeeds, so an offline image still boots.
# Token present but Bitwarden unreachable or the token wrong: exits non-zero, so the service does not start without its keys.
# What Pi can read is therefore the fetched keys (they are in its environment) and nothing that mints more of them.
set -euo pipefail
umask 077
token_file=/etc/sbx/bws-token
env_file=/etc/sbx/bws.env
out_dir=/run/sbx
out=$out_dir/secrets.env

install -d -m 755 "$out_dir"
tmp=$(mktemp "$out_dir/.secrets.XXXXXX")
trap 'rm -f "$tmp"' EXIT

if [ -s "$token_file" ]; then
  [ -r "$env_file" ] && . "$env_file"
  : "${BWS_PROJECT_ID:?BWS_PROJECT_ID missing in $env_file}"
  BWS_ACCESS_TOKEN=$(tr -d '[:space:]' < "$token_file")
  export BWS_ACCESS_TOKEN
  json=$(bws secret list "$BWS_PROJECT_ID" --output json)
  printf '%s' "$json" | python3 -c '
import json, re, sys
items = json.load(sys.stdin)
if not isinstance(items, list):
    sys.exit("unexpected response from bws")
seen = set()
for it in items:
    k, v = it.get("key", ""), it.get("value", "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
        sys.exit("secret name %r is not a valid environment variable name" % k)
    if k in seen:
        sys.exit("secret name %s appears twice in the project" % k)
    if "\n" in v or "\r" in v or "\0" in v:
        sys.exit("secret %s contains a newline or NUL, which an env file cannot hold" % k)
    seen.add(k)
    print("%s=\"%s\"" % (k, v.replace("\\", "\\\\").replace("\"", "\\\"")))
' > "$tmp"
fi

chown root:pibridge "$tmp"
chmod 640 "$tmp"
mv -f "$tmp" "$out"
trap - EXIT
