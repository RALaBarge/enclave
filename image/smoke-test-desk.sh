#!/usr/bin/env bash
# smoke-test-desk.sh: the desk half of the core, on its own.
#
# smoke-test.sh checks the whole box; this checks the pieces that only exist
# because every tool call goes through DeskBox - the desk's posture, the broker
# socket it reaches, the account it runs as, and the adapter a second harness
# would use. Run as root on the core:  sudo bash smoke-test-desk.sh
# Prints PASS / FAIL per check; exits 1 if any check fails.
set -uo pipefail

DESK="${DESK:-http://127.0.0.1:8080}"
SOCK="${SBX_SOCKET:-/run/sbx/broker.sock}"
TOOLS="${DESKBOX_TOOLS:-/opt/deskbox/tools}"
ADAPTER="${ADAPTER:-/usr/local/lib/enclave/desk_tools.py}"
PASS_N=0; FAIL_N=0; SKIP_N=0
pass() { PASS_N=$((PASS_N+1)); printf 'PASS  %-20s %s\n' "$1" "${2:-}"; }
fail() { FAIL_N=$((FAIL_N+1)); printf 'FAIL  %-20s %s\n' "$1" "${2:-}"; }
skip() { SKIP_N=$((SKIP_N+1)); printf 'SKIP  %-20s %s\n' "$1" "${2:-}"; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
have curl && have python3 || { echo "curl and python3 are required" >&2; exit 2; }

# The desk must be strict, sandboxed, filtered, capped, and NOT root. A desk
# that came up degraded is the failure this check exists for.
check_posture() {
  local body
  body=$(curl -fsS --max-time 5 "$DESK/" 2>/dev/null) || { fail desk-posture "GET $DESK/ failed"; return; }
  if printf '%s' "$body" | python3 -c '
import json, sys
e = json.load(sys.stdin)["enforcement"]
assert e.get("strict") is True, "not -strict"
for k in ("sandbox", "seccomp", "resource_limits"):
    assert e.get(k) is True, k + " not enforced"
assert e.get("root") is False, "running as root"
assert e.get("user") == "deskbox", "user is %r, not deskbox" % e.get("user")
' 2>/dev/null; then
    pass desk-posture "strict, sandboxed, seccomp, limits, user deskbox, not root"
  else
    fail desk-posture "enforcement: $(printf '%s' "$body" | head -c 240)"
  fi
}

# The socket is the access control: mode 0660 in the desk's group, and nothing
# listening on the old TCP port.
check_broker_socket() {
  if [ ! -S "$SOCK" ]; then fail broker-socket "$SOCK missing"; return; fi
  local mode grp
  mode=$(stat -c '%a' "$SOCK" 2>/dev/null); grp=$(stat -c '%G' "$SOCK" 2>/dev/null)
  if [ "$mode" != 660 ] || [ "$grp" != sbxdesk ]; then
    fail broker-socket "$SOCK is ${mode:-?} ${grp:-?} (want 660 sbxdesk)"; return
  fi
  if curl -fsS --max-time 2 "http://127.0.0.1:${SBX_BROKER_PORT:-8099}/status" >/dev/null 2>&1; then
    fail broker-socket "a TCP listener answered on :${SBX_BROKER_PORT:-8099}"; return
  fi
  pass broker-socket "0660 sbxdesk, no TCP listener"
}

# Every deployed contract must keep the stub off the network and give it the
# socket instead. This is the invariant sync.py enforces in the repo; this is
# the same fact checked where it is actually served.
check_contracts() {
  local n=0 bad=""
  for d in "$TOOLS"/*-sbx; do
    [ -d "$d" ] || continue
    n=$((n+1))
    grep -Eq '^[[:space:]]*network:[[:space:]]*false[[:space:]]*$' "$d/tcs.yaml" || bad="$bad ${d##*/}:network"
    grep -Eq 'src:.*/broker\.sock' "$d/tcs.yaml" || bad="$bad ${d##*/}:bind"
    [ "$(stat -c '%a' "$d/run.py" 2>/dev/null)" = 700 ] || bad="$bad ${d##*/}:mode"
  done
  if [ "$n" -eq 0 ]; then fail contracts "no *-sbx contracts under $TOOLS"; return; fi
  if [ -n "$bad" ]; then fail contracts "problems:$bad"; else pass contracts "$n deployed, network:false + bind + 0700"; fi
}

# The whole path, as an agent sees it.
check_call() {
  local body
  body=$(curl -sS --max-time 90 -H 'Content-Type: application/json' \
          -d '{"input":{"name":"smoke-test"}}' "$DESK/tools/greet-sbx?wait=60s" 2>&1) \
    || { fail desk-call "request failed: $(printf '%s' "$body" | head -c 200)"; return; }
  if printf '%s' "$body" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("status") == "done", d.get("status") or d.get("error")
assert d["result"]["greeting"] == "Hello, smoke-test.", d.get("result")
' 2>/dev/null; then
    pass desk-call "desk -> stub -> broker -> sandbox -> greet"
  else
    fail desk-call "unexpected: $(printf '%s' "$body" | head -c 240)"
  fi
}

# The adapter a second harness would point at, and the tools it sees.
check_adapter() {
  if [ ! -f "$ADAPTER" ]; then skip adapter "$ADAPTER not installed"; return; fi
  local body
  body=$(python3 "$ADAPTER" --desk "$DESK" list 2>&1) \
    || { fail adapter "adapter failed: $(printf '%s' "$body" | head -c 200)"; return; }
  if printf '%s' "$body" | python3 -c '
import json, sys
tools = json.load(sys.stdin)
assert isinstance(tools, list) and tools, "no tools"
for t in tools:
    assert t.get("input_schema", {}).get("type") == "object", t.get("name")
' 2>/dev/null; then
    pass adapter "$(printf '%s' "$body" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') tools, schemas shaped"
  else
    fail adapter "unexpected: $(printf '%s' "$body" | head -c 200)"
  fi
}

echo "desk smoke test  $(date -Is)"
check_posture
check_broker_socket
check_contracts
check_call
check_adapter
echo "----"
echo "$PASS_N passed, $FAIL_N failed, $SKIP_N skipped"
[ "$FAIL_N" -eq 0 ]
