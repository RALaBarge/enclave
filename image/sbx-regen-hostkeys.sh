#!/bin/sh
# First boot only (see sbx-ssh-hostkeys.service): generate this copy's SSH host keys, then re-pin the key the
# broker trusts for 127.0.0.1. The build-time pin is invalid by design, because the build deletes the host keys.
set -eu
ssh-keygen -A
printf '127.0.0.1 %s\n' "$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)" > /etc/sbx/known_hosts
chown root:sbxbroker /etc/sbx/known_hosts
chmod 640 /etc/sbx/known_hosts
