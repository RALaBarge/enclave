#!/usr/bin/env bash
# Lite mode: the sandbox runs in this VM. The broker (user sbxbroker) SSHes to 127.0.0.1 as user sbx;
# that key is locked by authorized_keys to /opt/sbx/sbx-run. Also installs the first-boot warning.
# Run as root after image/provision.sh:   sudo bash image/setup-lite.sh
# Idempotent: existing key and broker.env values are kept; everything else is rewritten.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SBX_HOME=/home/sbx
KEY=/etc/sbx/id_ed25519
KH=/etc/sbx/known_hosts
ENV_FILE=/etc/sbx/broker.env
SSHD_SNIP=/etc/ssh/sshd_config.d/10-sbx-lite.conf
FORCED='command="/opt/sbx/sbx-run",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty,restrict'

die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
[ -x /opt/sbx/sbx-run ] || die "/opt/sbx/sbx-run missing (run image/provision.sh first)"
[ -x /usr/sbin/sshd ] || die "openssh-server not installed"
[ -f /etc/ssh/ssh_host_ed25519_key.pub ] || die "no sshd ed25519 host key"
grep -q '^Include /etc/ssh/sshd_config.d/' /etc/ssh/sshd_config || die "sshd_config lacks sshd_config.d Include"

# ---- users ----
id sbxbroker >/dev/null 2>&1 || useradd --system --home /nonexistent --shell /usr/sbin/nologin sbxbroker
id sbx >/dev/null 2>&1 || useradd --create-home --home-dir "$SBX_HOME" --shell /bin/sh sbx
# Shell must be a real one: sshd runs the forced command through the login shell. No password login.
usermod -s /bin/sh -p '!' sbx
chown sbx:sbx "$SBX_HOME"
chmod 755 "$SBX_HOME"

# ---- key pair (broker side private key, sbx side authorized key) ----
install -d -m 755 -o root -g root /etc/sbx
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -C sbx-broker-lite -f "$KEY"
chown sbxbroker:sbxbroker "$KEY"
chmod 600 "$KEY"
ssh-keygen -y -f "$KEY" >"$KEY.pub"
chown sbxbroker:sbxbroker "$KEY.pub"
chmod 644 "$KEY.pub"

install -d -m 700 -o sbx -g sbx "$SBX_HOME/.ssh"
printf '%s %s\n' "$FORCED" "$(cat "$KEY.pub")" >"$SBX_HOME/.ssh/authorized_keys"
chown sbx:sbx "$SBX_HOME/.ssh/authorized_keys"
chmod 600 "$SBX_HOME/.ssh/authorized_keys"

# ---- known_hosts: pin the local sshd host key for 127.0.0.1 ----
printf '127.0.0.1 %s\n' "$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)" >"$KH"
chown root:sbxbroker "$KH"
chmod 640 "$KH"

# ---- broker env ----
if [ ! -f "$ENV_FILE" ]; then
  install -m 640 -o root -g root "$HERE/../harness/broker/broker.env.example" "$ENV_FILE"
fi
set_kv() { # key value: replace the line if present, else append
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    echo "$1=$2" >>"$ENV_FILE"
  fi
}
set_kv SBX_HOST 127.0.0.1
set_kv SBX_USER sbx
set_kv SBX_KEY "$KEY"
set_kv SBX_KNOWN_HOSTS "$KH"
chown root:root "$ENV_FILE"
chmod 640 "$ENV_FILE"

# ---- sshd: loopback only, key auth only, sbx only ----
cat >"$SSHD_SNIP" <<'EOF'
# sbx lite mode (managed by image/setup-lite.sh)
ListenAddress 127.0.0.1
AddressFamily inet
AllowUsers sbx
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
chmod 644 "$SSHD_SNIP"
sshd -t || die "sshd config invalid (see $SSHD_SNIP)"

# ---- firstboot warning banner (runs once, see firstboot-warning.service) ----
install -d -m 755 /usr/local/share/sbx
install -m 644 "$HERE/issue.txt" "$HERE/motd.txt" /usr/local/share/sbx/
install -m 755 "$HERE/firstboot-warning.sh" /usr/local/sbin/firstboot-warning.sh
install -m 644 "$HERE/firstboot-warning.service" /etc/systemd/system/firstboot-warning.service

# ---- services ----
systemctl daemon-reload
systemctl disable --now ssh.socket >/dev/null 2>&1 || true   # socket activation would bypass ListenAddress
systemctl enable ssh
systemctl restart ssh
systemctl enable firstboot-warning.service
systemctl enable sbx-broker
systemctl restart sbx-broker

echo "Lite mode set up. sshd on 127.0.0.1 only; broker -> sbx@127.0.0.1 via forced command /opt/sbx/sbx-run."
