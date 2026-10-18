#!/usr/bin/env bash
# sbx-reboot-and-wait: the full-mode SBX_RESET_CMD for a separate sandbox box.
#
# The broker runs this after every job, as user sbxbroker, with SBX_RESET_CMD set in
# /etc/sbx/broker.env, and holds its one-job lock until it returns. The next job starts the moment
# it exits, so returning 0 before the box is usable again would hand work to a box in an unknown
# state. This exits 0 only after the box has dropped SSH and the broker's own channel answers
# again; on a timeout it prints one reason to stderr and exits 1.
#
# How the reboot travels: the broker key (/etc/sbx/id_ed25519) is the only key the core holds. On
# the box its authorized_keys entry is forced to /opt/sbx/sbx-channel (installed by
# image/provision-sbx.sh): with no remote command the channel execs sbx-run unchanged (the tool
# channel), and with the single extra verb "reset" it runs the root helper /usr/local/sbin/sbx-reset
# through sudo. This reuses the key, host, port, user and known_hosts the broker already uses; a
# second key or account would be a second convention. Two authorized_keys lines for one key with
# different command= options cannot work: sshd takes the options from the first matching line and
# never looks at later ones (auth_check_authkeys_file() stops at found_key).
#
# The wait probes that same tool channel with empty input. sbx-run answers exit 2 to a malformed
# request, which proves sshd accepted the key and ran the forced command -- the exact path the
# broker's next job needs. Any other result is "not back yet".
#
# Sibling hook, sbx-vm-revert: reverting a VM disk is a host-side QEMU operation (stop the sandbox
# VM, discard build\run\sbx.qcow2, recreate the overlay from the built image, start it again). The
# core cannot do that: it has no channel to the hypervisor, and RESET_CMD runs here. A core-side
# script under that name could only be a no-op or a failure; either way the disk is not reverted.
# It is therefore deliberately not shipped. Its contract is in image/SETUP.md (Full mode).
#
# Lite mode does not use this: the sandbox is the core, so a reboot takes the broker with it.
# QEMU sandbox VMs can use it for RAM/tmpfs state, but it does not revert the disk.
#
# Exit: 0 box dropped SSH and the tool channel answers again; 1 anything else.
set -uo pipefail

ENV_FILE=/etc/sbx/broker.env
# In production systemd has already loaded the unit's EnvironmentFile into this process, so the
# variables are present. When run by hand as root the file is readable; as sbxbroker it is 0640
# root:root and cannot be read, which is fine because the variables are already there.
if [ -r "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

: "${SBX_HOST:?SBX_HOST is not set (run this from sbx-broker, or as root so $ENV_FILE is readable)}"
SBX_USER="${SBX_USER:-sbx}"
SBX_PORT="${SBX_PORT:-22}"
SBX_KEY="${SBX_KEY:-/etc/sbx/id_ed25519}"
SBX_KNOWN_HOSTS="${SBX_KNOWN_HOSTS:-/etc/sbx/known_hosts}"

# Must fit inside the broker's 300 s reset timeout: 60 + 180 plus SSH overhead.
DOWN_SECONDS=60
UP_SECONDS=180
SSH_CONNECT_TIMEOUT=5

die() { echo "sbx-reboot-and-wait: $*" >&2; exit 1; }

[ -r "$SBX_KEY" ] || die "cannot read the broker key $SBX_KEY"
[ -r "$SBX_KNOWN_HOSTS" ] || die "cannot read the known_hosts file $SBX_KNOWN_HOSTS"

# Same options as harness/broker/sbx_broker.py: batch only, no tty/agent, strict host key. Remote
# stderr is discarded: the box is untrusted and its text is not this script's output.
ssh_box() {
  ssh -T -i "$SBX_KEY" \
    -o BatchMode=yes -o IdentitiesOnly=yes \
    -o ForwardAgent=no -o ForwardX11=no -o RequestTTY=no \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$SBX_KNOWN_HOSTS" \
    -o ConnectTimeout="$SSH_CONNECT_TIMEOUT" -o ServerAliveInterval=5 \
    -p "$SBX_PORT" "$SBX_USER@$SBX_HOST" "$@" </dev/null 2>/dev/null
}

# True only when the broker's channel is back: sbx-run's exit 2 for an empty request.
probe() {
  local rc
  ssh_box >/dev/null
  rc=$?
  [ "$rc" -eq 2 ]
}

echo "sbx-reboot-and-wait: rebooting $SBX_USER@$SBX_HOST:$SBX_PORT and waiting" >&2
ssh_box reset >/dev/null
rc=$?
[ "$rc" -eq 0 ] || die "reboot request failed (ssh rc=$rc); the box needs the reset channel installed (see image/SETUP.md, Full mode), or it dropped the connection before the helper returned"

# Wait for the box to go down first. Without this, a probe could hit the old sshd a second after
# the request and report success for a reboot that has not happened yet.
deadline=$((SECONDS + DOWN_SECONDS))
dropped=0
while [ "$SECONDS" -lt "$deadline" ]; do
  if ! probe; then dropped=1; break; fi
  sleep 1
done
[ "$dropped" -eq 1 ] || die "$SBX_HOST still accepts the broker key after ${DOWN_SECONDS}s; the box did not reboot"

# Then wait for the tool channel to answer again. 2 s polling is enough for sshd and the forced
# command to come up; a fast boot is not a reason to poll harder.
deadline=$((SECONDS + UP_SECONDS))
until probe; do
  [ "$SECONDS" -lt "$deadline" ] || die "$SBX_HOST has not accepted the broker key again after ${UP_SECONDS}s; the sandbox is not usable"
  sleep 2
done

echo "sbx-reboot-and-wait: $SBX_HOST is back" >&2
exit 0
