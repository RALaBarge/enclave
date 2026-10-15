#!/usr/bin/env bash
# Last build step for the sandbox box: switch from the build's DHCP network to the real one. Nothing is restarted;
# the changes take effect on the next boot. Run as root after image/provision-sbx.sh.
set -euo pipefail
S=/usr/local/share/sbx-node
[ -d "$S" ] || { echo "run provision-sbx.sh first" >&2; exit 1; }
install -m 644 "$S/interfaces" /etc/network/interfaces
install -m 644 "$S/10-sbx-box.conf" /etc/ssh/sshd_config.d/10-sbx-box.conf
rm -f /etc/ssh/sshd_config.d/10-sbx-keys-only.conf
sshd -t
rm -f /etc/resolv.conf
install -m 644 "$S/resolv.conf" /etc/resolv.conf
echo "sandbox box finalized (static 10.77.0.2, sshd on 10.77.0.2 for sbx@10.77.0.1 only)"
