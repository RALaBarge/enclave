#!/usr/bin/env bash
# Generate the two keys the two-VM layout shares (once; existing keys are kept). Output is build/keys (gitignored):
#   build/keys/core/  broker private+public key, sandbox host PUBLIC key   -> uploaded into the core image
#   build/keys/sbx/   broker PUBLIC key, sandbox host private+public key   -> uploaded into the sandbox image
# Least privilege: the sandbox image never sees the broker's private key, the core never sees the sandbox's host key.
set -euo pipefail
OUT="$(cd "$(dirname "$0")/.." && pwd)/build/keys"
mkdir -p "$OUT/core" "$OUT/sbx" "$OUT/.src"
[ -f "$OUT/.src/broker_ed25519" ] || ssh-keygen -q -t ed25519 -N '' -C sbx-broker -f "$OUT/.src/broker_ed25519"
[ -f "$OUT/.src/sbx_host_ed25519" ] || ssh-keygen -q -t ed25519 -N '' -C sbx-box-host -f "$OUT/.src/sbx_host_ed25519"
cp "$OUT/.src/broker_ed25519" "$OUT/.src/broker_ed25519.pub" "$OUT/core/"
cp "$OUT/.src/sbx_host_ed25519.pub" "$OUT/core/"
cp "$OUT/.src/broker_ed25519.pub" "$OUT/sbx/"
cp "$OUT/.src/sbx_host_ed25519" "$OUT/.src/sbx_host_ed25519.pub" "$OUT/sbx/"
echo "keys ready in $OUT (core/ and sbx/)"
