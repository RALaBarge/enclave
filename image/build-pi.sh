#!/usr/bin/env bash
# Build the pinned Pi standalone binary (linux-x64) and install it to /usr/local/bin/pi.
# Run as root inside Debian 13. provision.sh calls this via PI_BUILD_CMD (eval'd in /opt/pi-src);
# it is also runnable alone:  sudo bash image/build-pi.sh
# Node.js is unpacked to /opt/node-build and put on PATH only inside this script.
# Idempotent: re-runs skip the Node download, the clone and the build when already in place.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=manifest.env
. "$HERE/manifest.env"

NODE_DIR=/opt/node-build
PI_SRC=/opt/pi-src
PI_INSTALL=/usr/local/bin/pi

if [ "$(id -u)" != 0 ]; then echo "ERROR: run as root" >&2; exit 1; fi
: "${NODE_VERSION:?NODE_VERSION is empty in manifest.env}"
: "${NODE_SHA256:?NODE_SHA256 is empty in manifest.env}"
: "${PI_REPO:?PI_REPO is empty in manifest.env}"
: "${PI_VERSION:?PI_VERSION is empty in manifest.env}"
: "${PI_SOURCE_SHA256:?PI_SOURCE_SHA256 is empty in manifest.env}"
: "${PI_BIN:?PI_BIN is empty in manifest.env}"
for v in BUN_VERSION BUN_SHA256; do : "${!v:?$v is empty in manifest.env}"; done

# ---- Node.js: pinned tarball, sha256 verified, extracted to /opt/node-build ----
node_ok() {
  [ -x "$NODE_DIR/bin/node" ] && [ "$("$NODE_DIR/bin/node" -p 'process.versions.node')" = "$NODE_VERSION" ]
}
if ! node_ok; then
  tmpd="$(mktemp -d)"
  trap 'rm -rf "${tmpd:-}"' EXIT
  url="https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz"
  curl -fsSL -o "$tmpd/node.tar.gz" "$url"
  printf '%s  %s\n' "$NODE_SHA256" "$tmpd/node.tar.gz" > "$tmpd/node.sha256"
  sha256sum -c "$tmpd/node.sha256"   # non-zero exit (set -e) on mismatch
  rm -rf "$NODE_DIR"; install -d "$NODE_DIR"
  tar -C "$NODE_DIR" --strip-components=1 -xzf "$tmpd/node.tar.gz"
  rm -rf "$tmpd"; trap - EXIT
fi
export PATH="$NODE_DIR/bin:$PATH"   # this script only; not persisted to the system PATH
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22||(a===22&&b>=19)?0:1)' \
  || { echo "ERROR: Node $(node -v) is older than 22.19 (Pi requires >= 22.19)" >&2; exit 1; }

# ---- Bun: pinned zip, sha256 verified; only used by build-binaries.sh (`bun build --compile`) ----
BUN_DIR=/opt/bun-build
if ! { [ -x "$BUN_DIR/bun" ] && [ "$("$BUN_DIR/bun" --version)" = "$BUN_VERSION" ]; }; then
  tmpb="$(mktemp -d)"
  curl -fsSL -o "$tmpb/bun.zip" "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-x64-baseline.zip"
  printf '%s  %s
' "$BUN_SHA256" "$tmpb/bun.zip" | sha256sum -c -
  unzip -q -o "$tmpb/bun.zip" -d "$tmpb"
  rm -rf "$BUN_DIR"; install -d "$BUN_DIR"
  install -m 755 "$tmpb"/bun-linux-x64-baseline/bun "$BUN_DIR/bun"
  rm -rf "$tmpb"
fi
export PATH="$BUN_DIR:$PATH"

# ---- Source: pinned release archive (bundles the model data; a git checkout does not) ----
cd /
if [ "$(cat "$PI_SRC/.source-sha256" 2>/dev/null || true)" != "$PI_SOURCE_SHA256" ]; then
  tmps="$(mktemp -d)"
  curl -fsSL -o "$tmps/pi.tar.gz" "$PI_REPO/releases/download/v${PI_VERSION}/pi-${PI_VERSION}-source.tar.gz"
  printf '%s  %s
' "$PI_SOURCE_SHA256" "$tmps/pi.tar.gz" | sha256sum -c -
  rm -rf "$PI_SRC"; install -d "$PI_SRC"
  tar -C "$PI_SRC" --strip-components=1 -xzf "$tmps/pi.tar.gz"
  echo "$PI_SOURCE_SHA256" > "$PI_SRC/.source-sha256"
  rm -rf "$tmps"
fi

# ---- Offline build of the standalone linux-x64 binary ----
OUT_BIN="$PI_SRC/$PI_BIN"
STAMP="$PI_SRC/.build-stamp"
if [ ! -x "$OUT_BIN" ] || [ "$(cat "$STAMP" 2>/dev/null || true)" != "$PI_SOURCE_SHA256" ]; then
  cd "$PI_SRC"
  npm install --ignore-scripts
  npm run build:offline
  # Pi's script passes --target=bun-linux-x64-baseline, which makes bun download a runtime blob at build time.
  # Our pinned Bun already IS the baseline build, so drop --target and it embeds itself: nothing unpinned is fetched.
  sed -i 's/ --target="\$bun_target"//' scripts/build-binaries.sh
  grep -q -- '--target' scripts/build-binaries.sh && { echo "ERROR: --target still present in build-binaries.sh" >&2; exit 1; }
  ./scripts/build-binaries.sh --offline-model-data --platform linux-x64 --out "$PI_SRC/out"
  [ -x "$OUT_BIN" ] || { echo "ERROR: expected binary not produced: $OUT_BIN" >&2; exit 1; }
  echo "$PI_SOURCE_SHA256" > "$STAMP"
  cd /
fi

# ---- Install ----
install -m 755 "$OUT_BIN" "$PI_INSTALL"
# Time-boxed: on a CPU model without the instructions the binary wants, this hung for an hour and stalled the build.
if ver=$(timeout 30 "$PI_INSTALL" --version 2>&1); then
  echo "pi $ver"
else
  echo "WARN: pi --version did not finish cleanly (rc=$?); installed at $PI_INSTALL" >&2
fi
