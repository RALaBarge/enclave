#!/usr/bin/env bash
# smoke-test.sh: verify a finished sbx VM. Run as root inside the VM:
#   sudo bash smoke-test.sh [--quick]
# Prints PASS / FAIL / SKIP per check. Exits 1 if any check FAILs, 0 otherwise.
# --quick skips the core-egress probe and the end-to-end greet call.
# Works in both modes: lite (sandbox inside this VM) and box (separate sandbox VM at 10.77.0.2; see `sbx-mode`).
set -uo pipefail

QUICK=0
case "${1:-}" in
  --quick) QUICK=1 ;;
  -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown option: $1 (try --quick)" >&2; exit 2 ;;
esac

EMBED_MODEL="${EMBED_MODEL:-nomic-embed-text}"   # matches image/manifest.env
BROKER_SOCK="${SBX_SOCKET:-/run/sbx/broker.sock}" # sbx_broker.py default
DESK=http://127.0.0.1:8080
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

MODE=$(/usr/local/sbin/sbx-mode 2>/dev/null || echo lite)

check_mode() { pass mode "$MODE (sandbox ${MODE/lite/in this VM})"; }

check_units() {
  local u state
  for u in ollama loom sbx-console sbx-broker sbx-session-gate deskbox pi-bridge nftables; do
    state=$(systemctl is-active "$u" 2>/dev/null)
    if [ "$state" = active ]; then pass "unit:$u" "active"
    else fail "unit:$u" "state=${state:-unknown}"; fi
  done
  state=$(systemctl is-active ssh 2>/dev/null)
  if [ "$MODE" = box ]; then   # the sandbox is another machine: nothing here should accept SSH
    if [ "$state" = active ]; then fail "unit:ssh" "active in box mode (should be off)"; else pass "unit:ssh" "off in box mode"; fi
  elif [ "$state" = active ]; then pass "unit:ssh" "active"
  else fail "unit:ssh" "state=${state:-unknown}"; fi
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

# This host is the sandbox's gateway: input and forward default-drop, masquerade for sandbox traffic, own output open.
check_nft_policy() {
  if ! have nft; then fail nft-policy "nft not installed"; return; fi
  local rs missing="" h
  rs=$(nft list ruleset 2>&1) || { fail nft-policy "nft list ruleset failed: $(printf '%s' "$rs" | head -c 200)"; return; }
  for h in input forward; do
    printf '%s\n' "$rs" | grep -Eq "hook $h[^;]*;[[:space:]]*policy drop;" || missing="$missing $h"
  done
  printf '%s\n' "$rs" | grep -Eq "hook output[^;]*;[[:space:]]*policy accept;" || missing="$missing output(open)"
  if [ -z "$missing" ]; then pass nft-policy "input and forward default drop; core output open by design"
  else fail nft-policy "unexpected policy on:$missing"; fi
}

check_gateway() {
  local rs fwd
  rs=$(nft list ruleset 2>&1)
  fwd=$(sysctl -n net.ipv4.ip_forward 2>/dev/null)
  if [ "$fwd" != 1 ]; then fail gateway "net.ipv4.ip_forward=$fwd (want 1)"; return; fi
  if ! printf '%s\n' "$rs" | grep -Eq 'oifname != (\{ )?"eth1"( \})? masquerade'; then fail gateway "no masquerade rule for the sandbox link (eth1)"; return; fi
  # Forwarding must be allowlist-only: every accept in the forward chain is tied to the sandbox interface or to established flows.
  local loose
  loose=$(printf '%s\n' "$rs" | awk '/chain forward/{f=1} f&&/}/{f=0} f&&/accept/' | grep -v -E 'iifname|ct state established' || true)
  if [ -n "$loose" ]; then fail gateway "forward chain has accepts not tied to the sandbox interface: $(printf '%s' "$loose" | head -c 160)"; return; fi
  pass gateway "forwarding on, sandbox traffic masqueraded, forward chain is allowlist-only"
}

# The core is not locked down (by design). Informational: pass if it can reach out, skip if the host has no upstream.
check_outbound() {
  if [ "$QUICK" = 1 ]; then skip core-egress "--quick"; return; fi
  if ! have curl; then fail core-egress "curl not installed"; return; fi
  if curl -sS --max-time 4 -o /dev/null https://example.com 2>/dev/null; then pass core-egress "core can reach the internet (open by design)"
  else skip core-egress "no upstream network from this VM"; fi
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

# A new bridge session must put the firewall on its session profile first. Opens a short build window to prove it,
# so it only runs when the session profile is active (it never touches a build window you opened on purpose).
check_lockdown() {
  local tok=/etc/sbxfw/token con=http://127.0.0.1:8787 br=http://127.0.0.1:9760 T cur out sid after
  [ -r "$tok" ] || { skip session-lockdown "console token not readable"; return; }
  T=$(cat "$tok")
  active() { curl -fsS --max-time 8 -H "Authorization: Bearer $T" "$con/api/fw" 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["policy"]["active"])' 2>/dev/null; }
  cur=$(active)
  [ -n "$cur" ] || { fail session-lockdown "console not answering on $con"; return; }
  [ "$cur" = session ] || { skip session-lockdown "a build window is open; not touching it"; return; }
  curl -fsS --max-time 8 -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"active":"build","for_s":60}' \
      "$con/api/fw/profile" >/dev/null 2>&1 || { fail session-lockdown "could not open the test build window"; return; }
  out=$(curl -sS --max-time 20 -H 'Content-Type: application/json' -d '{"cwd":"/var/lib/pi-bridge-work","name":"lockdown"}' "$br/sessions" 2>&1)
  sid=$(printf '%s' "$out" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)
  after=$(active)
  [ -n "$sid" ] && curl -s --max-time 10 -X DELETE "$br/sessions/$sid" >/dev/null 2>&1
  if [ "$after" = session ] && printf '%s' "$out" | grep -q '"changed": true'; then
    pass session-lockdown "a new session closed an open build window"
  else
    curl -s --max-time 8 -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"active":"session"}' "$con/api/fw/profile" >/dev/null 2>&1
    fail session-lockdown "profile after session start: ${after:-?}; response: $(printf '%s' "$out" | head -c 160)"
  fi
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

# The desk runs -strict: contract validation, bwrap and the syscall filter are
# what "every tool call goes through the desk" actually rests on, so a desk
# that came up degraded is a failed check, not a note.
check_desk_service() {
  local body
  body=$(curl -fsS --max-time 5 "$DESK/" 2>/dev/null) \
    || { fail desk-service "GET $DESK/ failed (unit:deskbox)"; return; }
  if printf '%s' "$body" | python3 -c '
import json, sys
e = json.load(sys.stdin)["enforcement"]
assert e.get("strict") is True, "not running -strict"
for k in ("sandbox", "seccomp", "resource_limits"):
    assert e.get(k) is True, k + " not enforced"
assert not e.get("root"), "running as root"
' 2>/dev/null; then
    pass desk-service "strict, sandboxed, seccomp, per-job limits"
  else
    fail desk-service "enforcement block: $(printf '%s' "$body" | head -c 240)"
  fi
}

# greet the whole way round, from the side an agent sees it: desk -> stub
# (inside bwrap, network:false) -> broker socket -> ssh -> sbx-run -> greet.
check_desk_e2e() {
  if [ "$QUICK" = 1 ]; then skip desk-e2e "--quick"; return; fi
  if ! systemctl is-active --quiet deskbox; then skip desk-e2e "deskbox not active (see unit:deskbox)"; return; fi
  local body
  body=$(curl -sS --max-time 90 -H 'Content-Type: application/json' \
          -d '{"input":{"name":"smoke-test"}}' "$DESK/tools/greet-sbx?wait=60s" 2>&1) \
    || { fail desk-e2e "desk request failed: $(printf '%s' "$body" | head -c 200)"; return; }
  if printf '%s' "$body" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("status") == "done", d.get("status") or d.get("error")
assert d["result"]["greeting"] == "Hello, smoke-test.", d.get("result")
' 2>/dev/null; then
    pass desk-e2e "desk -> stub -> broker -> sbx-run -> greet OK"
  else
    fail desk-e2e "unexpected response: $(printf '%s' "$body" | head -c 240)"
  fi
}

check_bwrap() {
  if ! have bwrap; then fail bwrap "bwrap missing"; return; fi
  local out rc
  out=$(timeout 20 bwrap --ro-bind / / --unshare-net true 2>&1); rc=$?
  if [ "$rc" -eq 0 ]; then pass bwrap "starts with --unshare-net"
  else fail bwrap "rc=$rc: $(printf '%s' "$out" | head -c 200)"; fi
}

# greet through the full path: curl -> broker socket -> ssh -> sbx-run -> bwrap -> greet.
check_greet_e2e() {
  if [ "$QUICK" = 1 ]; then skip greet-e2e "--quick"; return; fi
  if ! systemctl is-active --quiet sbx-broker; then skip greet-e2e "sbx-broker not active (see unit:sbx-broker)"; return; fi
  if [ ! -x /opt/sbx/sbx-run ] || [ ! -x /opt/sbx/tools/greet/run ]; then
    skip greet-e2e "/opt/sbx/sbx-run or /opt/sbx/tools/greet not installed"; return
  fi
  local body
  body=$(curl -sS --max-time 40 --unix-socket "$BROKER_SOCK" -H 'Content-Type: application/json' \
          -d '{"tool":"greet","input":{"name":"smoke-test"}}' \
          "http://localhost/run" 2>&1) \
    || { fail greet-e2e "broker request failed: $(printf '%s' "$body" | head -c 200)"; return; }
  if printf '%s' "$body" | python3 -c \
      'import json,sys; d=json.load(sys.stdin); assert d["output"]["greeting"]=="Hello, smoke-test."' 2>/dev/null; then
    pass greet-e2e "broker -> sbx-run -> greet OK"
  else fail greet-e2e "unexpected response: $(printf '%s' "$body" | head -c 200)"; fi
}

# The gate holds the only readable copy of the tighten-only token, and its socket is the only way in. Mode, group and
# the running bridge's real groups are the access control, so they are what gets checked. `runuser -u pibridge` would
# not do for the last: it applies /etc/group, not the SupplementaryGroups= the unit gives the process.
check_session_gate() {
  local sock=/run/sbx-gate/gate.sock mode grp out pid tokgid callgid groups
  if [ ! -S "$sock" ]; then fail session-gate "$sock missing"; return; fi
  mode=$(stat -c '%a' "$sock" 2>/dev/null); grp=$(stat -c '%G' "$sock" 2>/dev/null)
  if [ "$mode" != 660 ] || [ "$grp" != sbxgatecall ]; then
    fail session-gate "socket is ${mode:-?} ${grp:-?} (want 660 sbxgatecall)"; return
  fi
  pid=$(systemctl show -p MainPID --value pi-bridge 2>/dev/null)
  if [ -z "$pid" ] || [ "$pid" = 0 ] || [ ! -r "/proc/$pid/status" ]; then
    fail session-gate "pi-bridge has no running main process to inspect"; return
  fi
  groups=" $(awk '/^Groups:/{for(i=2;i<=NF;i++)printf "%s ", $i}' "/proc/$pid/status") "
  tokgid=$(stat -c '%g' /etc/sbxfw/tighten-token 2>/dev/null)
  callgid=$(stat -c '%g' "$sock" 2>/dev/null)
  case "$groups" in *" $tokgid "*)
    fail session-gate "pi-bridge (pid $pid) is in the tighten token's group (gid $tokgid); only the gate should be"; return;; esac
  case "$groups" in *" $callgid "*) ;;
    *) fail session-gate "pi-bridge (pid $pid) is not in the socket's group (gid $callgid), so it cannot reach the gate"; return;; esac
  if runuser -u pibridge -- test -r /etc/sbxfw/tighten-token 2>/dev/null; then
    fail session-gate "pibridge can read the tighten token (only sbxgate should)"; return
  fi
  out=$(curl -sS --max-time 10 --unix-socket "$sock" -X POST http://localhost/lockdown 2>&1) \
    || { fail session-gate "POST /lockdown failed: $(printf '%s' "$out" | head -c 160)"; return; }
  if printf '%s' "$out" | python3 -c \
      'import json,sys; d=json.load(sys.stdin); assert d.get("profile")=="session", d' 2>/dev/null; then
    pass session-gate "socket 660 sbxgatecall, bridge outside the token group, lockdown -> session"
  else fail session-gate "unexpected answer: $(printf '%s' "$out" | head -c 160)"; fi
}

# pi-bridge against the REAL pi binary: create a session, ask pi for its state over RPC, delete it. No model call.
check_bridge() {
  local api=http://127.0.0.1:9760 sid out
  if ! curl -fsS --max-time 5 "$api/health" 2>/dev/null | grep -q '"app": "pi-bridge"'; then
    fail pi-bridge "GET /health failed on $api"; return
  fi
  sid=$(curl -fsS --max-time 10 -H 'Content-Type: application/json' -d '{"cwd":"/var/lib/pi-bridge-work","name":"smoke"}' \
          "$api/sessions" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)
  if [ -z "$sid" ]; then fail pi-bridge "could not create a session"; return; fi
  out=$(curl -sS --max-time 40 -H 'Content-Type: application/json' -d '{"type":"get_state"}' "$api/sessions/$sid/command" 2>&1)
  curl -s --max-time 10 -X DELETE "$api/sessions/$sid" >/dev/null 2>&1
  if printf '%s' "$out" | python3 -c 'import json,sys; assert json.load(sys.stdin)["success"] is True' 2>/dev/null; then
    pass pi-bridge "session -> real pi get_state OK"
  else fail pi-bridge "get_state failed: $(printf '%s' "$out" | head -c 200)"; fi
}

# Bitwarden: the CLI is installed, the token is unreadable to the service user, and the fetch step works. Without a token
# file the fetch step must succeed with an empty env file; a configured token is reported (live fetch needs the network).
check_secrets() {
  if ! bws --version >/dev/null 2>&1; then fail secrets "bws is not installed"; return; fi
  if runuser -u pibridge -- test -r /etc/sbx/bws-token 2>/dev/null; then
    fail secrets "the pibridge user can read /etc/sbx/bws-token"; return; fi
  if grep -qsv '^[[:space:]]*\(#\|$\)' /etc/sbx/bws-token; then
    pass secrets "bws $(bws --version | awk '{print $2}'); token present and unreadable to pibridge (live fetch not tested here)"
  elif sbx-fetch-secrets; then
    pass secrets "bws $(bws --version | awk '{print $2}'); no token yet, fetch step succeeds with an empty env file"
  else fail secrets "sbx-fetch-secrets failed"; fi
}

echo "sbx VM smoke test  $(date -Is)  quick=$QUICK"
check_root
check_mode
check_units
check_listeners
check_nft_policy
check_gateway
check_outbound
check_ollama_model
check_loom_http
check_pi
check_bridge
check_secrets
check_lockdown
check_session_gate
check_deskbox
check_desk_service
check_bwrap
check_greet_e2e
check_desk_e2e
echo "----"
echo "$PASS_N passed, $FAIL_N failed, $SKIP_N skipped"
[ "$FAIL_N" -eq 0 ]
