#!/usr/bin/env bash
# smoke-test.sh: verify a finished sbx VM. Run as root inside the VM:
#   sudo bash smoke-test.sh [--quick]
# Prints PASS / FAIL / SKIP per check. Exits 1 if any check FAILs, 0 otherwise.
# --quick skips the outbound-egress probe and the end-to-end greet call.
set -uo pipefail

QUICK=0
case "${1:-}" in
  --quick) QUICK=1 ;;
  -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown option: $1 (try --quick)" >&2; exit 2 ;;
esac

EMBED_MODEL="${EMBED_MODEL:-nomic-embed-text}"   # matches image/manifest.env
BROKER_PORT="${SBX_BROKER_PORT:-8099}"           # sbx_broker.py default
LOOM_PORT=9741
PASS_N=0; FAIL_N=0; SKIP_N=0

pass() { PASS_N=$((PASS_N+1)); printf 'PASS  %-18s %s\n' "$1" "${2:-}"; }
fail() { FAIL_N=$((FAIL_N+1)); printf 'FAIL  %-18s %s\n' "$1" "${2:-}"; }
skip() { SKIP_N=$((SKIP_N+1)); printf 'SKIP  %-18s %s\n' "$1" "${2:-}"; }
have() { command -v "$1" >/dev/null 2>&1; }

check_root() {
  if [ "$(id -u)" -eq 0 ]; then pass root "running as root"
  else fail root "must run as root (sudo bash smoke-test.sh)"; exit 1; fi
}

check_units() {
  local u state
  for u in ollama loom sbx-console sbx-broker nftables ssh; do
    state=$(systemctl is-active "$u" 2>/dev/null)
    if [ "$state" = active ]; then pass "unit:$u" "active"
    else fail "unit:$u" "state=${state:-unknown}"; fi
  done
}

# Every TCP listener must be bound to loopback (127.0.0.0/8 or ::1).
check_listeners() {
  if ! have ss; then fail listeners "ss not installed"; return; fi
  local local_addr addr offenders="" count=0
  while read -r local_addr; do
    [ -z "$local_addr" ] && continue
    count=$((count+1))
    addr=${local_addr%:*}      # strip :port (the last colon)
    addr=${addr#[}; addr=${addr%]}
    case "$addr" in
      127.*|::1) ;;
      *) offenders="$offenders $local_addr" ;;
    esac
  done < <(ss -ltnH | awk '{print $4}')
  if [ "$count" -eq 0 ]; then fail listeners "ss reported no TCP listeners (unexpected)"
  elif [ -n "$offenders" ]; then fail listeners "non-loopback listeners:$offenders"
  else pass listeners "$count TCP listeners, all loopback"; fi
}

# nftables must carry a default-drop policy on both input and output.
check_nft_policy() {
  if ! have nft; then fail nft-policy "nft not installed"; return; fi
  local rs missing="" h
  rs=$(nft list ruleset 2>&1) || { fail nft-policy "nft list ruleset failed: $(printf '%s' "$rs" | head -c 200)"; return; }
  for h in input output; do
    printf '%s\n' "$rs" | grep -Eq "hook $h[^;]*;[[:space:]]*policy drop;" || missing="$missing $h"
  done
  if [ -z "$missing" ]; then pass nft-policy "input and output default drop"
  else fail nft-policy "no default-drop policy on:$missing"; fi
}

check_outbound() {
  if [ "$QUICK" = 1 ]; then skip outbound "--quick"; return; fi
  if ! have curl; then fail outbound "curl not installed"; return; fi
  if curl -sS --max-time 3 -o /dev/null https://example.com 2>/dev/null; then
    fail outbound "https://example.com reachable: egress is NOT blocked"
  else pass outbound "https://example.com blocked"; fi
}

check_ollama_model() {
  if ! have ollama; then fail ollama-model "ollama CLI missing"; return; fi
  if OLLAMA_HOST=127.0.0.1:11434 timeout 15 ollama list 2>/dev/null \
      | awk 'NR>1 {print $1}' | grep -Eq "^${EMBED_MODEL}(:|\$)"; then
    pass ollama-model "$EMBED_MODEL present"
  else fail ollama-model "$EMBED_MODEL not listed by 'ollama list'"; fi
}

# Any HTTP answer counts as "up". Connection refused / timeout (000) is a failure.
check_loom_http() {
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${LOOM_PORT}/" 2>/dev/null)
  case "$code" in
    200|404|401) pass loom-http "GET / -> $code" ;;
    *) fail loom-http "no HTTP answer on 127.0.0.1:${LOOM_PORT} (code ${code:-none})" ;;
  esac
}

check_pi() {
  if [ ! -x /usr/local/bin/pi ]; then fail pi "/usr/local/bin/pi missing"; return; fi
  local out rc
  out=$(timeout 15 /usr/local/bin/pi --version 2>&1); rc=$?
  if [ "$rc" -eq 0 ] && [ -n "$out" ]; then pass pi "$(printf '%s' "$out" | head -n1)"
  else fail pi "pi --version rc=$rc: $(printf '%s' "$out" | head -c 200)"; fi
}

# agent-desk -check only runs if this build advertises the flag.
check_deskbox() {
  if ! have agent-desk; then fail deskbox "agent-desk binary missing"; return; fi
  local help out rc
  help=$(timeout 5 agent-desk -h 2>&1 || true)
  if ! printf '%s\n' "$help" | grep -Eq -- '(^|[[:space:]])-check([[:space:]=]|$)'; then
    skip deskbox-check "this deskbox build has no -check flag"; return
  fi
  out=$(timeout 60 agent-desk -check 2>&1); rc=$?
  if [ "$rc" -eq 0 ]; then pass deskbox-check "exit 0"
  else fail deskbox-check "rc=$rc: $(printf '%s' "$out" | head -c 200)"; fi
}

check_bwrap() {
  if ! have bwrap; then fail bwrap "bwrap missing"; return; fi
  local out rc
  out=$(timeout 20 bwrap --ro-bind / / --unshare-net true 2>&1); rc=$?
  if [ "$rc" -eq 0 ]; then pass bwrap "starts with --unshare-net"
  else fail bwrap "rc=$rc: $(printf '%s' "$out" | head -c 200)"; fi
}

# greet through the full path: curl -> sbx-broker -> ssh -> sbx-run -> bwrap -> greet.
check_greet_e2e() {
  if [ "$QUICK" = 1 ]; then skip greet-e2e "--quick"; return; fi
  if ! systemctl is-active --quiet sbx-broker; then skip greet-e2e "sbx-broker not active (see unit:sbx-broker)"; return; fi
  if [ ! -x /opt/sbx/sbx-run ] || [ ! -x /opt/sbx/tools/greet/run ]; then
    skip greet-e2e "/opt/sbx/sbx-run or /opt/sbx/tools/greet not installed"; return
  fi
  local body
  body=$(curl -sS --max-time 40 -H 'Content-Type: application/json' \
          -d '{"tool":"greet","input":{"name":"smoke-test"}}' \
          "http://127.0.0.1:${BROKER_PORT}/run" 2>&1) \
    || { fail greet-e2e "broker request failed: $(printf '%s' "$body" | head -c 200)"; return; }
  if printf '%s' "$body" | python3 -c \
      'import json,sys; d=json.load(sys.stdin); assert d["output"]["greeting"]=="Hello, smoke-test."' 2>/dev/null; then
    pass greet-e2e "broker -> sbx-run -> greet OK"
  else fail greet-e2e "unexpected response: $(printf '%s' "$body" | head -c 200)"; fi
}

echo "sbx VM smoke test  $(date -Is)  quick=$QUICK"
check_root
check_units
check_listeners
check_nft_policy
check_outbound
check_ollama_model
check_loom_http
check_pi
check_deskbox
check_bwrap
check_greet_e2e
echo "----"
echo "$PASS_N passed, $FAIL_N failed, $SKIP_N skipped"
[ "$FAIL_N" -eq 0 ]
