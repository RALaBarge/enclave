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
# The build used DHCP (dhcpcd). When it stops at power-off its hook rewrites /etc/resolv.conf with no nameservers, after
# this script has run. This box has one static address and one resolver (the core): keep dhcpcd's hands off resolv.conf.
# IPv6 is off: the core only NATs IPv4.
grep -q '^nohook resolv.conf' /etc/dhcpcd.conf 2>/dev/null || echo 'nohook resolv.conf' >> /etc/dhcpcd.conf
echo "net.ipv6.conf.all.disable_ipv6=1" >/etc/sysctl.d/90-sbx-box.conf
rm -f /etc/resolv.conf
install -m 644 "$S/resolv.conf" /etc/resolv.conf
# The DHCP daemon that is still running from the build keeps its OLD config and rewrites resolv.conf at power-off, so
# `nohook` alone is not enough for this boot. Make the file immutable; an admin who wants to change DNS runs `chattr -i` first.
chattr +i /etc/resolv.conf
echo "sandbox box finalized (static 10.77.0.2, sshd on 10.77.0.2 for sbx@10.77.0.1 only)"
