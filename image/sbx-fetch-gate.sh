#!/bin/sh
# sbx-fetch-gate: write browserbox's gate address and token into the page and fetch tools (/opt/sbx/tools/<t>/gate).
# Installed as /usr/local/sbin/sbx-fetch-gate. sbx-push-tools runs it before every push (full mode: the box gets the
# current token) and pi-bridge.service runs it after sbx-fetch-secrets (lite mode: the tools run from this /opt/sbx).
# It comes from the Bitwarden secret SBX_GATE ("http://HOST:9011 TOKEN") in the same project as the model keys;
# sbx-fetch-secrets leaves SBX_GATE* out of Pi's environment, so only these two tools ever hold it.
#
# The file is 0640 root:sbx: jobs run as sbx, each job's bwrap binds only its own tool's directory, and no other
# account (pibridge included) can read it. What it unlocks is the gate's host allowlist (job sites), not the browser.
# No Bitwarden token, or no valid SBX_GATE secret: the gate files are removed, so page and fetch fail loudly, and this
# exits 0. Bitwarden unreachable: the files are left as they are and this exits 1.
set -eu
umask 077
[ "$(id -u)" = 0 ] || { echo "sbx-fetch-gate: run as root" >&2; exit 1; }
getent group sbx >/dev/null || { echo "sbx-fetch-gate: no sbx group; refusing to write a file only root could read" >&2; exit 1; }

clear_gate() {
  rm -f /opt/sbx/tools/page/gate /opt/sbx/tools/fetch/gate
  echo "sbx-fetch-gate: $1; gate files removed, page and fetch will fail until SBX_GATE is set" >&2
  exit 0
}

token=$(grep -sv '^[[:space:]]*#' /etc/sbx/bws-token | tr -d '[:space:]' || true)
[ -n "$token" ] || clear_gate "no /etc/sbx/bws-token"
# shellcheck disable=SC1091
. /etc/sbx/bws.env
list=$(BWS_ACCESS_TOKEN=$token bws secret list "${BWS_PROJECT_ID:?}" --output json) \
  || { echo "sbx-fetch-gate: Bitwarden failed, gate files unchanged" >&2; exit 1; }
val=$(printf '%s' "$list" | python3 -c '
import json, re, sys
v = [s.get("value", "") for s in json.load(sys.stdin) if s.get("key") == "SBX_GATE"]
if len(v) == 1 and re.fullmatch(r"http://[A-Za-z0-9.-]+:[0-9]{1,5} \S{32,128}", v[0].strip()):
    print(v[0].strip())') || true
[ -n "$val" ] || clear_gate "SBX_GATE missing, duplicated, or not \"http://HOST:PORT TOKEN\""
for t in page fetch; do
  [ -d "/opt/sbx/tools/$t" ] || continue
  tmp=$(mktemp "/opt/sbx/tools/$t/.gate.XXXXXX")
  printf '%s\n' "$val" >"$tmp"
  chown root:sbx "$tmp"
  chmod 640 "$tmp"
  mv -f "$tmp" "/opt/sbx/tools/$t/gate"
done
echo "sbx-fetch-gate: gate file written for page and fetch"
