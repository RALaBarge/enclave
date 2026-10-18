#!/usr/bin/env bash
# Provision a fresh Debian 13 install into the sbx VM image. Run as root from the repo root:
#   sudo bash image/provision.sh
# Idempotent enough to re-run. Needs network at build time only; the finished image needs none.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
# shellcheck source=manifest.env
. "$HERE/manifest.env"

need() {
  local v
  for v in "$@"; do
    if [ -z "${!v:-}" ]; then
      if [ "${ALLOW_UNPINNED:-0}" = 1 ]; then echo "WARN: $v unpinned" >&2
      else echo "ERROR: $v is empty in manifest.env (pin it, or ALLOW_UNPINNED=1)" >&2; exit 1; fi
    fi
  done
}
need OLLAMA_VERSION OLLAMA_TGZ_SHA256 LOOM_REPO LOOM_COMMIT DESKBOX_COMMIT PI_REPO PI_VERSION PI_SOURCE_SHA256 NODE_VERSION NODE_SHA256 TS_VERSION TS_TGZ_SHA256

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  nftables python3 python3-venv python3-pip bubblewrap openssh-server \
  git curl ca-certificates xz-utils zstd unzip

pin_clone() { # repo commit dest
  rm -rf "$3"; git clone --quiet "$1" "$3"; git -C "$3" checkout --quiet "$2"
}

# ---- Ollama + embedding model (loopback only) ----
id ollama &>/dev/null || useradd --system --home /var/lib/ollama --create-home --shell /usr/sbin/nologin ollama
tgz=$(mktemp); curl -fsSL -o "$tgz" "https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/ollama-linux-amd64.tar.zst"
[ "${ALLOW_UNPINNED:-0}" = 1 ] || echo "${OLLAMA_TGZ_SHA256}  $tgz" | sha256sum -c -
tar --zstd -C /usr -xf "$tgz"; rm -f "$tgz"
cat >/etc/systemd/system/ollama.service <<'EOF'
[Unit]
Description=Ollama (embeddings for Loom, loopback only)
After=network.target
[Service]
User=ollama
Environment=OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MODELS=/var/lib/ollama/models
ExecStart=/usr/bin/ollama serve
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/ollama
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload; systemctl enable --now ollama
for _ in $(seq 30); do curl -fs http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
runuser -u ollama -- env OLLAMA_HOST=127.0.0.1:11434 ollama pull "$EMBED_MODEL"

# ---- Loom ----
# Findings from reading ralabarge/loom (main tree; verify against LOOM_COMMIT once pinned):
#  - Flat repo, no loom/ package and no __main__.py, no pyproject/console script. So `python -m loom`
#    and a `loom` binary do NOT exist. Correct entry point: `python /opt/loom/run.py serve`
#    (install.sh's systemd unit uses the same form). run.py -> cli.main() -> main.serve().
#  - Bind: serve() uses LOOM_BIND_HOST, default 127.0.0.1 (set explicitly below). Port: LOOM_PORT, default 9741.
#  - Env read: LOOM_DB_PATH (default ~/.loom/loom.db), LOOM_OLLAMA_URL (default http://localhost:11434),
#    LOOM_EMBED_MODEL (default nomic-embed-text), LOOM_PORT, LOOM_BIND_HOST. (LOOM_URL/LOOM_MODEL are CLI-only.)
#  - Store.__init__ mkdirs the DB parent dir (parents=True), so /var/lib/loom is fine either way.
#  - sqlite-vec: pip wheel only (sqlite_vec.load(conn) after enable_load_extension(True)); no apt package needed.
#    Needs Python sqlite3 built with extension loading (Debian python3 is; unverified on this image).
#  - requirements.txt: fastapi==0.139.0 uvicorn==0.49.0 httpx==0.28.1 pydantic==2.13.4 sqlite-vec==0.1.9
#  - Python 3.13 comes from Debian 13 python3 (README says 3.13+).
#  - Auth: none on any route (POST /log, /search, GET /conversations, /stats, /health). Loopback bind is the only access control.
#  - Outbound: only POST ${LOOM_OLLAMA_URL}/api/embed (httpx, 30s timeout). Nothing else.
#  - UNVERIFIED: whether the GitHub-fetched summaries were complete (install.sh and cli.py fetches were summarised, not raw).
id loom &>/dev/null || useradd --system --home /var/lib/loom --create-home --shell /usr/sbin/nologin loom
pin_clone "$LOOM_REPO" "$LOOM_COMMIT" /opt/loom
python3 -m venv /opt/loom/.venv
/opt/loom/.venv/bin/pip install --no-cache-dir -r /opt/loom/requirements.txt
cat >/etc/systemd/system/loom.service <<EOF
[Unit]
Description=Loom (local LLM memory)
After=ollama.service
Wants=ollama.service
[Service]
User=loom
WorkingDirectory=/opt/loom
Environment=LOOM_OLLAMA_URL=http://127.0.0.1:11434 LOOM_EMBED_MODEL=${EMBED_MODEL} LOOM_BIND_HOST=127.0.0.1 LOOM_PORT=9741 LOOM_DB_PATH=/var/lib/loom/loom.db
ExecStart=/opt/loom/.venv/bin/python /opt/loom/run.py serve
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/loom
[Install]
WantedBy=multi-user.target
EOF

# ---- DeskBox (prebuilt static binaries from the build host) ----
# The desk runs as its own account, not as pibridge. It executes tool code and
# its job workspaces are where anything that escapes a sandbox lands; sharing
# the agent's account would also make every run script executable from the
# agent's own shell, which is the one bypass a contract cannot close from the
# inside. 0700 on tools/ is what turns that into a permission.
getent group sbxdesk >/dev/null || groupadd -r sbxdesk
id deskbox &>/dev/null || useradd --system --home /var/lib/deskbox --create-home --shell /usr/sbin/nologin deskbox
install -d -m 0700 -o deskbox -g deskbox /var/lib/deskbox
install -d -m 0700 -o deskbox -g deskbox /opt/deskbox
install -d -m 0700 -o deskbox -g deskbox /opt/deskbox/tools
# Built on the build host: GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build ./cmd/{agent-desk,tcs-shim,tcs-verify}
for b in agent-desk tcs-shim tcs-verify; do
  [ -f "$HERE/artifacts/$b" ] || { echo "ERROR: image/artifacts/$b missing (build DeskBox first)" >&2; exit 1; }
done
install -m 755 "$HERE/artifacts/agent-desk" /usr/local/bin/agent-desk
install -m 755 "$HERE/artifacts/tcs-verify" /usr/local/bin/tcs-verify
install -m 755 "$HERE/artifacts/tcs-shim" /usr/bin/tcs-shim    # shimmed tools expect it under /usr
# The tools the desk actually serves: each contract plus the generated stub,
# run scripts 0700 so only that account can execute them. sync.py refuses to
# ship a contract whose stub would need the network, which is what keeps every
# call on the broker socket.
# The repo was uploaded from a Windows host, which drops Unix execute bits, and
# tcs-verify (run by sync.py) rejects a contract whose run.py is not executable.
chmod 755 "$ROOT"/harness/tools/*/run.py "$ROOT"/harness/stub/run.py
python3 "$ROOT/sync.py" --deploy /opt/deskbox/tools
chown -R deskbox:deskbox /opt/deskbox/tools
install -m 644 "$ROOT/harness/systemd/deskbox.service" /etc/systemd/system/
install -m 755 "$HERE/deskbox-run.sh" /usr/local/bin/deskbox-run
# The desk runs -strict, and its per-job memory/task limits go through
# `systemd-run --user --scope`: that needs this account's own session bus to
# exist without a login, which is what linger buys.
loginctl enable-linger deskbox

# ---- Pi: build from pinned commit ----
bash "$HERE/build-pi.sh"

# ---- sbx bridge: broker + console + firewall (lite mode, per console/README.md) ----
id sbxconsole &>/dev/null || useradd --system --home /nonexistent --shell /usr/sbin/nologin sbxconsole
id sbxbroker  &>/dev/null || useradd --system --home /nonexistent --shell /usr/sbin/nologin sbxbroker
install -d -o sbxconsole -g sbxconsole /etc/sbxfw
install -d /opt/sbx-console/common /opt/sbx-broker /etc/sbx /opt/sbx/tools
install -m 644 "$ROOT/console/sbx_console.py" "$ROOT/console/ui.html" /opt/sbx-console/
install -m 644 "$ROOT/common/toollist.py" /opt/sbx-console/common/
install -m 644 "$ROOT/common/toollist.py" /opt/sbx-broker/       # broker imports it from its own dir
install -m 644 "$ROOT/common/toollist.py" /opt/sbx/              # sbx-run imports it from /opt/sbx
install -m 644 "$ROOT/harness/broker/sbx_broker.py" /opt/sbx-broker/
install -m 644 "$ROOT/console/nftables.conf" /etc/nftables.conf
install -m 644 "$ROOT/console/sbx-console.service" "$ROOT/harness/systemd/sbx-broker.service" /etc/systemd/system/
install -m 644 "$ROOT/tools.list" /etc/sbx/tools.list           # harness side (broker, console)
install -m 644 "$ROOT/tools.list" /opt/sbx/tools.list            # sandbox side (sbx-run hardcodes this path)
install -m 755 "$ROOT/sbx/sbx-run" /opt/sbx/sbx-run
cp -r "$ROOT/sbx/tools/." /opt/sbx/tools/
# Uploads from a Windows build host lose Unix modes; tool entry points must be executable for sbx-run.
chmod 755 /opt/sbx/sbx-run /opt/sbx/tools/*/run

# ---- session gate: the one place a new session's firewall profile is forced ----
# Its own process so the guarantee has one implementation: with the lockdown
# inside pi-bridge, a second harness in the core gets none at all. Two groups:
# sbxgate reads the tighten-only token and only the gate is in it; sbxgatecall
# is on the socket and holds the bridge (via its unit) and the gate. There is no
# verb that loosens anything.
getent group sbxgatecall >/dev/null || groupadd -r sbxgatecall
id sbxgate &>/dev/null || useradd --system --home /nonexistent --shell /usr/sbin/nologin sbxgate
install -d /opt/sbx-gate
install -m 644 "$ROOT/harness/gate/session_gate.py" /opt/sbx-gate/
install -m 644 "$ROOT/harness/systemd/sbx-session-gate.service" /etc/systemd/system/
# The adapter a second harness points its tool definitions at. Installed so the
# image does not depend on a checkout being present at /repo.
install -d /usr/local/lib/enclave
install -m 644 "$ROOT/harness/toolsadapter/desk_tools.py" /usr/local/lib/enclave/
# ---- Bitwarden Secrets Manager CLI. The access token is NOT baked in: put it in /etc/sbx/bws-token by hand (see SETUP.md).
tmpz=$(mktemp); curl -fsSL -o "$tmpz" "https://github.com/bitwarden/sdk-sm/releases/download/bws-v${BWS_VERSION}/bws-x86_64-unknown-linux-gnu-${BWS_VERSION}.zip"
[ "${ALLOW_UNPINNED:-0}" = 1 ] || echo "${BWS_SHA256}  $tmpz" | sha256sum -c -
unzip -o -q "$tmpz" bws -d /usr/local/bin; chmod 755 /usr/local/bin/bws; rm -f "$tmpz"
install -m 755 "$HERE/sbx-fetch-secrets.sh" /usr/local/bin/sbx-fetch-secrets
install -d -m 755 /etc/sbx

# ---- Tailscale: node agent only. pi-bridge still starts without --peers, so peers stay OFF until the operator logs in
# and opts in (see SETUP.md). No auth key is baked in; `tailscale up` is the operator's step, like the Bitwarden token.
# Static tarball, not the apt repo: same shape as bws/ollama (one pinned artifact, one pinned hash). Binaries land in
# /usr/local/bin, which is where harness/bridge/peers.py looks for the CLI.
tstgz=$(mktemp)
curl -fsSL -o "$tstgz" "https://pkgs.tailscale.com/stable/tailscale_${TS_VERSION}_amd64.tgz"
[ "${ALLOW_UNPINNED:-0}" = 1 ] || echo "${TS_TGZ_SHA256}  $tstgz" | sha256sum -c -
tar -C /usr/local/bin --strip-components=1 -xzf "$tstgz" \
  "tailscale_${TS_VERSION}_amd64/tailscale" "tailscale_${TS_VERSION}_amd64/tailscaled"
chmod 755 /usr/local/bin/tailscale /usr/local/bin/tailscaled
rm -f "$tstgz"
# tailscaled makes the TUN device with TUNSETIFF; load the module at every boot so /dev/net/tun exists (harmless if built in).
install -d /etc/modules-load.d
printf '%s\n' tun >/etc/modules-load.d/tailscale.conf
modprobe tun 2>/dev/null || true
# Not the release's own unit: that one runs with the full root capability set. This one keeps root (tailscaled sets up a
# TUN device, routes and the local API socket) but bounds it to what a node agent needs, like the other units here.
cat >/etc/systemd/system/tailscaled.service <<'EOF'
[Unit]
Description=Tailscale node agent (tailnet peers for pi-bridge; not logged in until the operator runs tailscale up)
Documentation=https://tailscale.com/docs/
Wants=network-pre.target
After=network-pre.target network.target
[Service]
Type=notify
ExecStart=/usr/local/bin/tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/run/tailscale/tailscaled.sock --port=41641
ExecStopPost=-/usr/local/bin/tailscaled --cleanup
Restart=on-failure
# Node identity and WireGuard keys live here (0700).
StateDirectory=tailscale
StateDirectoryMode=0700
RuntimeDirectory=tailscale
RuntimeDirectoryMode=0755
ReadWritePaths=/var/lib/tailscale
# TUN + routes only. No module loading, no ptrace, no setuid, no device nodes beyond what /dev already exposes.
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now tailscaled
# ---- pi-bridge: local HTTP/SSE API over Pi's RPC mode (loopback; Tailscale installed above, but the bridge starts without
#      --peers, so peer messaging stays off until the operator opts in) ----
id pibridge &>/dev/null || useradd --system --home /var/lib/pi-bridge --create-home --shell /usr/sbin/nologin pibridge
install -d -o pibridge -g pibridge /var/lib/pi-bridge/state
# Session root: a sibling of the home, never inside it (the bridge protects the whole home).
install -d -m 0700 -o pibridge -g pibridge /var/lib/pi-bridge-work
install -d /opt/pi-bridge
for f in pairing peers pibridge server; do install -m 644 "$ROOT/harness/bridge/$f.py" /opt/pi-bridge/; done
# The bridge imports the redactor from ./redact next to itself (or ../redact in the
# repo). Without the file, --loom-url refuses to post rather than storing raw text.
install -d /opt/pi-bridge/redact
install -m 644 "$ROOT/harness/redact/output_redactor.py" /opt/pi-bridge/redact/
install -d /opt/pi-bridge/ui/modules
for f in index.html view.js host.js worker.js modules/modules.json modules/sessions.js modules/chat.js modules/dirs.js modules/tree.js modules/inspect.js; do install -m 644 "$ROOT/harness/ui/$f" "/opt/pi-bridge/ui/$f"; done
install -m 644 "$ROOT/harness/systemd/pi-bridge.service" /etc/systemd/system/
install -m 755 "$HERE/smoke-test.sh" /usr/local/sbin/sbx-smoke-test
install -m 755 "$HERE/sbx-mode.sh" /usr/local/sbin/sbx-mode
install -m 755 "$HERE/sbx-regen-hostkeys.sh" /usr/local/sbin/sbx-regen-hostkeys
[ -f /etc/sbx/broker.env ] || install -m 640 "$ROOT/harness/broker/broker.env.example" /etc/sbx/broker.env
runuser -u sbxconsole -- env SBXC_ETC=/etc/sbxfw SBX_COMMON=/opt/sbx-console/common python3 /opt/sbx-console/sbx_console.py --init
# The tighten-only token belongs to sbx-session-gate, which is the only thing that may call /api/fw/lockdown. It is NOT
# readable by pibridge: pibridge is in sbxgatecall (the socket), never in sbxgate (the token). The bridge asks the gate over
# its socket, and Pi has no route to the console's firewall API at all.
getent group sbxgate >/dev/null || groupadd -r sbxgate
chgrp sbxgate /etc/sbxfw/tighten-token
chmod 640 /etc/sbxfw/tighten-token
# Bitwarden token and project id (created by the operator; sbx-fetch-secrets reads them as root before pi-bridge starts).
touch /etc/sbx/bws-token /etc/sbx/bws.env; chown root:root /etc/sbx/bws-token /etc/sbx/bws.env; chmod 600 /etc/sbx/bws-token /etc/sbx/bws.env   # root only: Pi's user never reads the token
systemctl daemon-reload
systemctl enable nftables sbx-console loom ollama pi-bridge sbx-session-gate deskbox

# ---- gateway for the separate sandbox box: private link on eth1 (10.77.0.1), forwarding on. The firewall forwards
# nothing until the console allowlist says so. A one-NIC VM simply never brings eth1 up.
grep -q "interfaces.d" /etc/network/interfaces || echo "source /etc/network/interfaces.d/*" >> /etc/network/interfaces
cat >/etc/network/interfaces.d/sbx-link <<'EOF'
allow-hotplug eth1
iface eth1 inet static
    address 10.77.0.1/24
EOF
echo "net.ipv4.ip_forward=1" >/etc/sysctl.d/90-sbx-gateway.conf

install -m 755 "$HERE/sbx-reboot-and-wait.sh" /usr/local/bin/sbx-reboot-and-wait   # full-mode SBX_RESET_CMD
bash "$HERE/setup-lite.sh"      # sbx user + forced-command key + loopback sshd + broker env + first-boot banner
echo "Provisioned. Run: bash image/smoke-test.sh"
