#!/usr/bin/env bash
# Provision the lean SANDBOX box (the second VM / device of the strongest setup). Run as root from the repo root:
#   sudo bash image/provision-sbx.sh          then, as the last build step:   sudo bash image/finalize-sbx.sh
# It carries none of the core's software or secrets: no Loom, Ollama, Pi, DeskBox, broker key or console. Only
# sshd (forced command), bubblewrap, the tools, and a firewall that lets it talk to the core and nothing else.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
KEYS="$ROOT/keys"                   # staged by Packer from build/keys/sbx (see image/gen-keys.sh)
die() { echo "ERROR: $*" >&2; exit 1; }
for f in broker_ed25519.pub sbx_host_ed25519 sbx_host_ed25519.pub; do [ -f "$KEYS/$f" ] || die "missing $KEYS/$f (run image/gen-keys.sh)"; done
[ ! -f "$KEYS/broker_ed25519" ] || die "the broker PRIVATE key must not be staged into the sandbox image"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends nftables python3 bubblewrap openssh-server ca-certificates curl iproute2

# ---- sandbox user: key login only, forced into sbx-run ----
SBX_HOME=/home/sbx
FORCED='command="/opt/sbx/sbx-run",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty,restrict'
id sbx >/dev/null 2>&1 || useradd --create-home --home-dir "$SBX_HOME" --shell /bin/sh sbx
usermod -s /bin/sh -p '!' sbx
chmod 755 "$SBX_HOME"
install -d -m 700 -o sbx -g sbx "$SBX_HOME/.ssh"
printf '%s %s\n' "$FORCED" "$(cat "$KEYS/broker_ed25519.pub")" >"$SBX_HOME/.ssh/authorized_keys"
chown sbx:sbx "$SBX_HOME/.ssh/authorized_keys"
chmod 600 "$SBX_HOME/.ssh/authorized_keys"

# ---- sandbox side of the tool bridge ----
install -d /opt/sbx/tools
install -m 644 "$ROOT/common/toollist.py" /opt/sbx/
install -m 644 "$ROOT/tools.list" /opt/sbx/tools.list
install -m 755 "$ROOT/sbx/sbx-run" /opt/sbx/sbx-run
cp -r "$ROOT/sbx/tools/." /opt/sbx/tools/
chmod 755 /opt/sbx/sbx-run /opt/sbx/tools/*/run     # uploads from Windows lose Unix modes

# ---- host key: fixed at build time so the core can pin it (the core's known_hosts.box has this public key) ----
rm -f /etc/ssh/ssh_host_*
install -m 600 "$KEYS/sbx_host_ed25519" /etc/ssh/ssh_host_ed25519_key
install -m 644 "$KEYS/sbx_host_ed25519.pub" /etc/ssh/ssh_host_ed25519_key.pub

# ---- staged runtime config, applied by finalize-sbx.sh (the build still needs DHCP and a normal sshd until then) ----
install -d /usr/local/share/sbx-node
cat >/usr/local/share/sbx-node/10-sbx-box.conf <<'EOF'
# sandbox box (managed by image/provision-sbx.sh)
ListenAddress 10.77.0.2
AddressFamily inet
HostKey /etc/ssh/ssh_host_ed25519_key
AllowUsers sbx@10.77.0.1
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitEmptyPasswords no
AllowTcpForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
GatewayPorts no
PermitTTY no
PermitUserEnvironment no
EOF
cat >/usr/local/share/sbx-node/interfaces <<'EOF'
# sandbox box: one link, to the core. No DHCP, and no other address or route.
source /etc/network/interfaces.d/*
auto lo
iface lo inet loopback
auto eth0
iface eth0 inet static
    address 10.77.0.2/24
    gateway 10.77.0.1
EOF
printf 'nameserver 10.77.0.1\n' >/usr/local/share/sbx-node/resolv.conf     # the core redirects this to its chosen resolver
cat >/etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
# Sandbox box firewall: it may talk to the core and to nothing else. The core's own rules decide what is forwarded.
flush ruleset
table inet sbxnode {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    ct state invalid drop
    ip saddr 10.77.0.1 tcp dport 22 accept
    ip saddr 10.77.0.1 icmp type echo-request accept
    limit rate 10/second log prefix "sbxnode-drop: " drop
  }
  chain forward {
    type filter hook forward priority 0; policy drop;
  }
  chain output {
    type filter hook output priority 0; policy drop;
    oif lo accept
    ct state established,related accept
    ip daddr 10.77.0.1 accept
    limit rate 10/second log prefix "sbxnode-drop: " drop
  }
}
EOF
chmod 644 /etc/nftables.conf

# ---- banners, test script ----
install -m 644 "$HERE/issue-sbx.txt" /etc/issue
install -m 644 "$HERE/issue-sbx.txt" /etc/motd
install -m 755 "$HERE/smoke-test-sbx.sh" /usr/local/sbin/sbx-node-test
systemctl daemon-reload
systemctl enable nftables
systemctl disable ssh.socket >/dev/null 2>&1 || true
systemctl enable ssh
echo "Sandbox box provisioned. Next: image/finalize-sbx.sh"
