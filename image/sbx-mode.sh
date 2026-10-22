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
# The reset goes with the mode: box reboots the sandbox box after every job (the console can switch that off, never
# on to a different command); lite needs none, and a reboot there would take the core down with it.
case "$1" in
  lite) host=127.0.0.1 reset= ;;
  box)  host=10.77.0.2 reset=/usr/local/bin/sbx-reboot-and-wait
        [ -s /etc/sbx/known_hosts.box ] || { echo "no sandbox-box host key installed (/etc/sbx/known_hosts.box)" >&2; exit 1; } ;;
  *) echo "usage: sbx-mode [lite|box]" >&2; exit 2 ;;
esac
sed -i -e "s|^SBX_HOST=.*|SBX_HOST=$host|" -e "/^SBX_RESET_CMD=/d" "$ENV"
echo "SBX_RESET_CMD=$reset" >>"$ENV"
# sshd stays up in both modes: it listens on loopback only and is the operator's way in (tailscale serve, SETUP.md).
# The sandbox link cannot reach it: the console's input chain accepts nothing but ping from the sandbox interface.
systemctl restart sbx-broker
echo "sbx mode: $1 (SBX_HOST=$host SBX_RESET_CMD=${reset:-none})"
