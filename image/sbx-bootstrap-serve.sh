#!/bin/bash
# On the core, as root: serve image/sbx-bootstrap.sh and the files it fetches to the sandbox box, for a limited time.
#   sudo sbx-bootstrap-serve [seconds]     (default 1200)
# The core's input chain drops everything from the sandbox interface, so for the window one rule is inserted at the
# top of it: TCP 8000 from 10.77.0.2 on eth1, nothing else. It is removed when the server exits (timeout, ^C, or once
# the box has asked for /done). Files come from the deployed tree (/srv/enclave); the server runs as nobody.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
R=${ENCLAVE_TREE:-/srv/enclave}; SECS=${1:-1200}
d=$(mktemp -d); chmod 755 "$d"
install -m 644 "$R/image/sbx-bootstrap.sh" "$d/b.sh"
install -m 644 "$R/sbx/sbx-channel" "$R/sbx/sbx-run" "$R/image/sbx-install.py" "$R/image/smoke-test-sbx.sh" "$R/common/toollist.py" "$d/"
log=$d.log; : >"$log"
nft insert rule inet sbxfw input iifname "eth1" ip saddr 10.77.0.2 tcp dport 8000 accept comment '"sbx bootstrap (temporary)"'
cleanup() {
  h=$(nft -a list chain inet sbxfw input | sed -n 's/.*sbx bootstrap (temporary).*# handle \([0-9]*\).*/\1/p')
  for x in $h; do nft delete rule inet sbxfw input handle "$x"; done
  [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null || true
  rm -rf "$d"
}
trap cleanup EXIT
runuser -u nobody -- timeout "$SECS" python3 -m http.server 8000 --bind 10.77.0.1 --directory "$d" >"$log" 2>&1 &
pid=$!
echo "serving for ${SECS}s. On the box: curl -fsS 10.77.0.1:8000/b.sh | sudo sh"
while kill -0 "$pid" 2>/dev/null; do
  if grep -q 'GET /done' "$log"; then echo "box reports: bootstrap done"; sleep 1; exit 0; fi
  sleep 2
done
echo "window closed without /done"; cat "$log"; exit 1
