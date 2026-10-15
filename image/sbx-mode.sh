#!/bin/sh
# sbx-mode [lite|box]: where the broker sends tool calls. Installed on the core as /usr/local/sbin/sbx-mode.
#   lite  the sandbox runs in this VM (one kernel shared with the core): convenient, not the locked-down option
#   box   the sandbox is the separate VM/device at 10.77.0.2, reached over the private link; this host is its gateway
# With no argument, prints the current mode.
set -eu
ENV=/etc/sbx/broker.env
cur=$(sed -n 's/^SBX_HOST=//p' "$ENV" | head -1)
if [ $# -eq 0 ]; then
  case "$cur" in 127.0.0.1) echo lite ;; 10.77.0.2) echo box ;; *) echo "custom ($cur)" ;; esac
  exit 0
fi
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
case "$1" in
  lite) host=127.0.0.1 ;;
  box)  host=10.77.0.2
        [ -s /etc/sbx/known_hosts.box ] || { echo "no sandbox-box host key installed (/etc/sbx/known_hosts.box)" >&2; exit 1; } ;;
  *) echo "usage: sbx-mode [lite|box]" >&2; exit 2 ;;
esac
sed -i "s|^SBX_HOST=.*|SBX_HOST=$host|" "$ENV"
if [ "$1" = box ]; then
  systemctl disable --now ssh >/dev/null 2>&1 || true      # nothing on this host should accept SSH once the sandbox is elsewhere
else
  systemctl enable --now ssh >/dev/null 2>&1
fi
systemctl restart sbx-broker
echo "sbx mode: $1 (SBX_HOST=$host)"
