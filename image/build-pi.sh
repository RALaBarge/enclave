#!/usr/bin/env bash
# Build the pinned Pi release's JS bundle in /opt/pi-src and install /usr/local/bin/pi, a launcher that runs it under
# the pinned Node. Run as root inside Debian 13. provision.sh calls this;
# it is also runnable alone:  sudo bash image/build-pi.sh
# Node.js is unpacked to /opt/node-build and put on PATH only inside this script.
# Idempotent: re-runs skip the Node download, the source download and the build when already in place.
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

# ---- Offline build of the JS bundle (no compiled binary: Bun's needs SSE4.2, which the Wyse CPUs lack) ----
BUNDLE="$PI_SRC/packages/coding-agent/dist/bundle/cli.js"
STAMP="$PI_SRC/.build-stamp"
if [ ! -f "$BUNDLE" ] || [ "$(cat "$STAMP" 2>/dev/null || true)" != "$PI_SOURCE_SHA256" ]; then
  cd "$PI_SRC"
  npm install --ignore-scripts
  npm run build:offline
  [ -f "$BUNDLE" ] || { echo "ERROR: expected bundle not produced: $BUNDLE" >&2; exit 1; }
  echo "$PI_SOURCE_SHA256" > "$STAMP"
  cd /
fi

# ---- Install: a one-file launcher that runs the bundle under the pinned Node ----
# Pi sets process.title, which under Node overwrites /proc/<pid>/cmdline; the bridge and sbx-smoke-test check Pi's
# lockdown flags there, so the title is made a no-op.
rm -rf /opt/pi /opt/bun-build   # left by the old compiled-binary build
rm -f "$PI_INSTALL"
cat > "$PI_INSTALL" <<PI
#!$NODE_DIR/bin/node
// Written by enclave image/build-pi.sh: Pi $PI_VERSION's JS bundle under the pinned Node.
Object.defineProperty(process, "title", { configurable: true, get() { return "pi"; }, set() {} });
await import("$BUNDLE");
PI
chmod 755 "$PI_INSTALL"
ver=$(timeout 60 "$PI_INSTALL" --version 2>&1) || { echo "ERROR: pi --version failed: $ver" >&2; exit 1; }
echo "pi $ver"
