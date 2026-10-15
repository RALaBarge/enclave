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
need OLLAMA_VERSION OLLAMA_TGZ_SHA256 LOOM_REPO LOOM_COMMIT DESKBOX_COMMIT PI_REPO PI_VERSION PI_SOURCE_SHA256 NODE_VERSION NODE_SHA256

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
install -d /opt/deskbox/tools
# Built on the build host: GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build ./cmd/{agent-desk,tcs-shim,tcs-verify}
for b in agent-desk tcs-shim tcs-verify; do
  [ -f "$HERE/artifacts/$b" ] || { echo "ERROR: image/artifacts/$b missing (build DeskBox first)" >&2; exit 1; }
done
install -m 755 "$HERE/artifacts/agent-desk" /usr/local/bin/agent-desk
install -m 755 "$HERE/artifacts/tcs-verify" /usr/local/bin/tcs-verify
install -m 755 "$HERE/artifacts/tcs-shim" /usr/bin/tcs-shim    # shimmed tools expect it under /usr

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
# ---- Bitwarden Secrets Manager CLI. The access token is NOT baked in: put it in /etc/sbx/bws-token by hand (see SETUP.md).
tmpz=$(mktemp); curl -fsSL -o "$tmpz" "https://github.com/bitwarden/sdk-sm/releases/download/bws-v${BWS_VERSION}/bws-x86_64-unknown-linux-gnu-${BWS_VERSION}.zip"
[ "${ALLOW_UNPINNED:-0}" = 1 ] || echo "${BWS_SHA256}  $tmpz" | sha256sum -c -
unzip -o -q "$tmpz" bws -d /usr/local/bin; chmod 755 /usr/local/bin/bws; rm -f "$tmpz"
install -m 755 "$HERE/sbx-with-secrets.sh" /usr/local/bin/sbx-with-secrets
install -d -m 755 /etc/sbx
# ---- pi-bridge: local HTTP/SSE API over Pi's RPC mode (loopback; peer messaging stays off, no Tailscale in the image) ----
id pibridge &>/dev/null || useradd --system --home /var/lib/pi-bridge --create-home --shell /usr/sbin/nologin pibridge
install -d -o pibridge -g pibridge /var/lib/pi-bridge/work /var/lib/pi-bridge/state
install -d /opt/pi-bridge
for f in pairing peers pibridge server; do install -m 644 "$ROOT/harness/bridge/$f.py" /opt/pi-bridge/; done
install -m 644 "$ROOT/harness/systemd/pi-bridge.service" /etc/systemd/system/
install -m 755 "$HERE/smoke-test.sh" /usr/local/sbin/sbx-smoke-test
install -m 755 "$HERE/sbx-mode.sh" /usr/local/sbin/sbx-mode
install -m 755 "$HERE/sbx-regen-hostkeys.sh" /usr/local/sbin/sbx-regen-hostkeys
[ -f /etc/sbx/broker.env ] || install -m 640 "$ROOT/harness/broker/broker.env.example" /etc/sbx/broker.env
runuser -u sbxconsole -- env SBXC_ETC=/etc/sbxfw SBX_COMMON=/opt/sbx-console/common python3 /opt/sbx-console/sbx_console.py --init
# The pi-bridge may read ONLY the tighten-only token (POST /api/fw/lockdown): a new agent session always starts on the
# session firewall profile. It cannot read the console's main token, so it can never loosen the firewall.
# Bitwarden token and project id (created by the operator; the bridge's service wrapper reads them).
touch /etc/sbx/bws-token /etc/sbx/bws.env; chown root:pibridge /etc/sbx/bws-token /etc/sbx/bws.env; chmod 640 /etc/sbx/bws-token /etc/sbx/bws.env
chgrp pibridge /etc/sbxfw/tighten-token
chmod 640 /etc/sbxfw/tighten-token
systemctl daemon-reload
systemctl enable nftables sbx-console loom ollama pi-bridge

# ---- gateway for the separate sandbox box: private link on eth1 (10.77.0.1), forwarding on. The firewall forwards
# nothing until the console allowlist says so. A one-NIC VM simply never brings eth1 up.
grep -q "interfaces.d" /etc/network/interfaces || echo "source /etc/network/interfaces.d/*" >> /etc/network/interfaces
cat >/etc/network/interfaces.d/sbx-link <<'EOF'
allow-hotplug eth1
iface eth1 inet static
    address 10.77.0.1/24
EOF
echo "net.ipv4.ip_forward=1" >/etc/sysctl.d/90-sbx-gateway.conf

bash "$HERE/setup-lite.sh"      # sbx user + forced-command key + loopback sshd + broker env + first-boot banner
echo "Provisioned. Run: bash image/smoke-test.sh"
