#!/usr/bin/env bash
# sbx-node-test: verify the sandbox box. Run as root on the sandbox VM:
#   sudo sbx-node-test [--expect blocked|open] [host]
# --expect says what the core's allowlist should currently do with an HTTPS request to <host> (default example.com).
# A fresh setup allows nothing, so the default is "blocked". After allowing the host on the core, run it with --expect open.
# Prints PASS / FAIL per check; exits 1 if any FAIL.
set -uo pipefail

EXPECT=blocked; HOST=example.com
while [ $# -gt 0 ]; do
  case "$1" in
    --expect) EXPECT="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) HOST="$1"; shift ;;
  esac
done
case "$EXPECT" in blocked|open) ;; *) echo "--expect must be blocked or open" >&2; exit 2 ;; esac

CORE=10.77.0.1; ME=10.77.0.2
PASS_N=0; FAIL_N=0
pass() { PASS_N=$((PASS_N+1)); printf 'PASS  %-20s %s\n' "$1" "${2:-}"; }
fail() { FAIL_N=$((FAIL_N+1)); printf 'FAIL  %-20s %s\n' "$1" "${2:-}"; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo sbx-node-test)"; exit 1; }
echo "sbx sandbox box test  $(date -Is)  expect=$EXPECT host=$HOST"

for u in ssh nftables; do
  s=$(systemctl is-active "$u" 2>/dev/null); [ "$s" = active ] && pass "unit:$u" active || fail "unit:$u" "state=${s:-unknown}"
done

# Only sshd listens, and only on the private link (or loopback).
bad=""; n=0
while read -r a; do
  [ -z "$a" ] && continue; n=$((n+1)); h=${a%:*}; h=${h#[}; h=${h%]}
  case "$h" in 127.*|::1|"$ME") ;; *) bad="$bad $a" ;; esac
done < <(ss -ltnH | awk '{print $4}')
if [ "$n" -eq 0 ]; then fail listeners "no listeners (sshd should be on $ME:22)"
elif [ -n "$bad" ]; then fail listeners "unexpected:$bad"
else pass listeners "$n listeners, all on $ME or loopback"; fi

# Firewall: default drop everywhere.
rs=$(nft list ruleset 2>&1); miss=""
for h in input forward output; do printf '%s\n' "$rs" | grep -Eq "hook $h[^;]*;[[:space:]]*policy drop;" || miss="$miss $h"; done
[ -z "$miss" ] && pass nft-policy "input, forward and output default drop" || fail nft-policy "no default drop on:$miss"
printf '%s\n' "$rs" | grep -Eq 'oifname "eth0" accept' && pass nft-output "output only out the link to the core (eth0)" || fail nft-output "no rule for the link to the core"

# Addressing: one NIC, one address, the core is the gateway and the resolver.
addrs=$(ip -4 -o addr show scope global | awk '{print $4}' | tr '\n' ' ')
[ "$addrs" = "$ME/24 " ] && pass address "$addrs" || fail address "got: ${addrs:-none} (want $ME/24 only)"
gw=$(ip -4 route show default | awk '{print $3}' | tr '\n' ' ')
[ "$gw" = "$CORE " ] && pass gateway "default via $CORE" || fail gateway "default via: ${gw:-none}"
ns=$(awk '$1=="nameserver"{print $2}' /etc/resolv.conf | tr '\n' ' ')
[ "$ns" = "$CORE " ] && pass resolver "only $CORE" || fail resolver "nameservers: ${ns:-none}"
ping -c1 -W2 "$CORE" >/dev/null 2>&1 && pass core-reachable "ping $CORE" || fail core-reachable "no answer from $CORE"

# Nothing of the core lives here.
leak=""
for p in /usr/local/bin/pi /usr/bin/ollama /opt/loom /opt/sbx-broker /opt/sbx-console /etc/sbx/id_ed25519 /usr/local/bin/agent-desk; do
  [ -e "$p" ] && leak="$leak $p"
done
[ -z "$leak" ] && pass no-core-secrets "no Pi, Ollama, Loom, broker key or console" || fail no-core-secrets "found:$leak"

# Sandbox plumbing.
if [ -x /opt/sbx/sbx-run ] && [ -x /opt/sbx/tools/greet/run ]; then pass sbx-run "installed with the greet tool"; else fail sbx-run "missing /opt/sbx/sbx-run or the greet tool"; fi
grep -q 'command="/opt/sbx/sbx-run"' /home/sbx/.ssh/authorized_keys 2>/dev/null && grep -q restrict /home/sbx/.ssh/authorized_keys \
  && pass forced-command "sbx key is locked to sbx-run" || fail forced-command "authorized_keys is not forced/restricted"
timeout 20 bwrap --ro-bind / / --unshare-net true >/dev/null 2>&1 && pass bwrap "starts with --unshare-net" || fail bwrap "bwrap failed"

# What the core's allowlist does with this box's traffic.
if curl -sS --max-time 3 -o /dev/null "http://$CORE:8787/" 2>/dev/null; then fail core-services "reached $CORE:8787 (the core must not expose services to this box)"
else pass core-services "core console not reachable from here"; fi
code=$(curl -sS --max-time 8 -o /dev/null -w '%{http_code}' "https://$HOST/" 2>/dev/null); rc=$?
if [ "$EXPECT" = blocked ]; then
  [ "$rc" -ne 0 ] && pass "egress:$HOST" "blocked by the core, as expected" || fail "egress:$HOST" "reachable (HTTP $code) but should be blocked"
else
  [ "$rc" -eq 0 ] && pass "egress:$HOST" "reachable through the core (HTTP $code)" || fail "egress:$HOST" "blocked (curl rc=$rc) but should be open"
fi
# Straight to an address, no DNS: only an allowlisted address may pass, so 9.9.9.9 must be blocked in both modes.
curl -sS --max-time 5 -o /dev/null "https://9.9.9.9/" >/dev/null 2>&1 && fail egress:9.9.9.9 "reachable: the allowlist is too wide" || pass egress:9.9.9.9 "blocked"

echo "----"
echo "$PASS_N passed, $FAIL_N failed"
[ "$FAIL_N" -eq 0 ]
