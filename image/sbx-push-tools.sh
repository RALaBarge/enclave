#!/bin/sh
# sbx-push-tools: send this core's /opt/sbx/tools, tools.list, toollist.py and sbx-run to the sandbox box (full mode). Run as root after
# `sync.py --deploy` and installing the tools here. The box's sbx-install checks the set and swaps it in; the next job
# uses it. Same SSH options and key as the broker (harness/broker/sbx_broker.py).
set -eu
ENV=/etc/sbx/broker.env
[ "$(id -u)" = 0 ] || { echo "sbx-push-tools: run as root" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$ENV"
set +a
[ "${SBX_HOST:-}" != 127.0.0.1 ] || { echo "sbx-push-tools: lite mode, the tools already run from /opt/sbx here" >&2; exit 1; }
/usr/local/sbin/sbx-fetch-gate      # the page/fetch tools' gate token, current from Bitwarden
tar -C /opt/sbx -cf - tools.list toollist.py sbx-run tools |
  ssh -T -i "${SBX_KEY:-/etc/sbx/id_ed25519}" \
    -o BatchMode=yes -o IdentitiesOnly=yes -o ForwardAgent=no -o ForwardX11=no -o RequestTTY=no \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="${SBX_KNOWN_HOSTS:-/etc/sbx/known_hosts}" \
    -o ConnectTimeout=5 -p "${SBX_PORT:-22}" "${SBX_USER:-sbx}@${SBX_HOST:?}" install
