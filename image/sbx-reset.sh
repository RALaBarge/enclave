#!/bin/sh
# sbx-reset: reboot the sandbox box, run as root by the reset channel (sbx/sbx-channel).
# Installed on the sandbox box only, by image/provision-sbx.sh.
# --no-block matters: the forced SSH command has to return so sbx-reboot-and-wait on the core can
# begin its wait. A blocking systemctl would hold the session until the shutdown kills sshd, which
# looks like a failed reset.
set -eu
[ "$(id -u)" = 0 ] || { echo "sbx-reset: must run as root" >&2; exit 1; }
exec systemctl reboot --no-block
