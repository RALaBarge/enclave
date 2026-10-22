#!/bin/sh
# sbx-fetch-gate: write browserbox's gate address and token into the page and fetch tools (/opt/sbx/tools/<t>/gate).
# Installed as /usr/local/sbin/sbx-fetch-gate; sbx-push-tools runs it before every push, so the box gets the current
# token. It comes from the Bitwarden secret SBX_GATE ("http://HOST:9011 TOKEN") in the same project as the model keys;
# sbx-fetch-secrets leaves SBX_GATE* out of Pi's environment, so only these two tools ever hold it.
#
# The file is 0644: jobs run as the box's sbx user, and each job's bwrap binds only its own tool's directory, so no
# other tool can read it. What it unlocks is the gate's host allowlist (job sites), not the browser.
# No token or no SBX_GATE secret: says so and leaves any existing file alone; page and fetch then fail loudly.
set -eu
umask 022
[ "$(id -u)" = 0 ] || { echo "sbx-fetch-gate: run as root" >&2; exit 1; }
token=$(grep -sv '^[[:space:]]*#' /etc/sbx/bws-token | tr -d '[:space:]' || true)
[ -n "$token" ] || { echo "sbx-fetch-gate: no /etc/sbx/bws-token, gate files unchanged" >&2; exit 0; }
# shellcheck disable=SC1091
. /etc/sbx/bws.env
val=$(BWS_ACCESS_TOKEN=$token bws secret list "${BWS_PROJECT_ID:?}" --output json | python3 -c '
import json, re, sys
v = [s.get("value", "") for s in json.load(sys.stdin) if s.get("key") == "SBX_GATE"]
if len(v) != 1 or not re.fullmatch(r"http://[A-Za-z0-9.-]+:[0-9]{1,5} [A-Za-z0-9]{32,128}", v[0].strip()):
    sys.exit("SBX_GATE missing, duplicated, or not \"http://HOST:PORT TOKEN\"")
print(v[0].strip())')
for t in page fetch; do
  [ -d "/opt/sbx/tools/$t" ] || continue
  tmp=$(mktemp "/opt/sbx/tools/$t/.gate.XXXXXX")
  printf '%s\n' "$val" >"$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "/opt/sbx/tools/$t/gate"
done
echo "sbx-fetch-gate: gate file written for page and fetch"
