#!/bin/sh
# Start the desk as its own account with that account's session bus.
#
# Why this is not in the unit: in a *system* unit `%U` is the manager's UID
# (0), not the `User=` account, so `XDG_RUNTIME_DIR=/run/user/%U` pointed at
# root's runtime dir and the pre-start waited for a bus that never appears.
# Resolving the uid here is the difference between a desk that starts and a
# restart loop.
set -eu
uid=$(id -u deskbox)
export XDG_RUNTIME_DIR="/run/user/$uid"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus"
i=0
bus="/run/user/$uid/bus"    # a path: $DBUS_SESSION_BUS_ADDRESS is "unix:path=...", never a socket for -S
while [ "$i" -lt 60 ] && [ ! -S "$bus" ]; do
  i=$((i + 1))
  sleep 1
done
if [ ! -S "$bus" ]; then
  echo "no user bus for deskbox at /run/user/$uid/bus; run: loginctl enable-linger deskbox" >&2
  exit 1
fi
# -job-key: the desk signs every job it starts with the key the broker created in /run/sbx; the broker
# refuses a request whose signature it cannot recompute. Read per job, so a broker restart is harmless.
exec /usr/local/bin/agent-desk -addr 127.0.0.1:8080 -tools /opt/deskbox/tools -data /var/lib/deskbox -strict -job-key /run/sbx/job.key
