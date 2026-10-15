#!/usr/bin/env bash
# Gateway ruleset lab: real nftables, three hosts in throwaway network namespaces.
#
#   sbx (10.77.0.2) --- core (10.77.0.1 | 203.0.113.1) --- world (203.0.113.2, resolver 203.0.113.53)
#
# The core is the namespace this script runs in, and the script first moves itself into a FRESH network namespace
# (unshare --net --mount), so the host's own networking is never touched. It refuses to run anywhere else.
# Run as root:   sudo bash console/test_netns.sh      (WSL is fine: nothing here leaves the private namespace)
set -u
if [ -z "${SBX_LAB_NS:-}" ]; then exec env SBX_LAB_NS=1 unshare --net --mount "$0" "$@"; fi
[ "$(ip -o link | wc -l)" = 1 ] || { echo "refusing to run: not in a fresh network namespace" >&2; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"
export SBX_COMMON="$HERE/../common"
mount -t tmpfs none /run
ip link set lo up
ip netns add sbx
ip netns add world
ip link add c-sbx type veth peer name s0 && ip link set s0 netns sbx
ip link add c-wan type veth peer name w0 && ip link set w0 netns world
ip addr add 10.77.0.1/24 dev c-sbx && ip link set c-sbx up
ip addr add 203.0.113.1/24 dev c-wan && ip link set c-wan up
ip -n sbx addr add 10.77.0.2/24 dev s0 && ip -n sbx link set s0 up && ip -n sbx link set lo up
ip -n sbx route add default via 10.77.0.1
ip -n world addr add 203.0.113.2/24 dev w0 && ip -n world addr add 203.0.113.53/24 dev w0
ip -n world link set w0 up && ip -n world link set lo up
sysctl -qw net.ipv4.ip_forward=1

TMP="$(mktemp -d)"
cat >"$TMP/servers.py" <<'PY'
import socket, sys, threading
def tcp(port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("0.0.0.0", port)); s.listen(8)
    while True:
        c, a = s.accept(); c.sendall(("tcp%d client=%s\n" % (port, a[0])).encode()); c.close()
def udp(port, host="0.0.0.0"):
    # bind to the address that was asked, so replies come from it (the NAT expects exactly that source)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind((host, port))
    while True:
        _, a = s.recvfrom(512); s.sendto(("dns-ok client=%s\n" % a[0]).encode(), a)
for spec in sys.argv[1:]:
    kind, port, *host = spec.split(":")
    threading.Thread(target=tcp if kind == "tcp" else udp, args=(int(port), *host) if kind == "udp" else (int(port),),
                     daemon=True).start()
threading.Event().wait()
PY
cat >"$TMP/probe.py" <<'PY'
import socket, sys
kind, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    if kind == "tcp":
        s = socket.create_connection((host, port), timeout=2); print(s.recv(100).decode().strip() or "EMPTY")
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2); s.sendto(b"q", (host, port)); print(s.recvfrom(100)[0].decode().strip())
except ConnectionRefusedError: print("REFUSED")
except OSError: print("TIMEOUT")
PY
ip netns exec world python3 -B "$TMP/servers.py" tcp:443 tcp:8080 udp:53:203.0.113.53 &
python3 -B "$TMP/servers.py" tcp:8787 &         # a service on the core that the sandbox must not reach
sleep 1

PASS=0; FAIL=0
expect() { # name want-substring got
  if [[ "$3" == *"$2"* ]]; then PASS=$((PASS+1)); printf 'PASS  %-46s %s\n' "$1" "$3"
  else FAIL=$((FAIL+1)); printf 'FAIL  %-46s want %q, got %q\n' "$1" "$2" "$3"; fi
}
from_sbx()  { ip netns exec sbx python3 -B "$TMP/probe.py" "$@"; }
from_core() { python3 -B "$TMP/probe.py" "$@"; }
apply() { # policy json -> render with the real console code, load with real nft
  SBXC_POLICY="$1" python3 -B - >"$TMP/rules.nft" <<'PY'
import json, os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.environ["SBX_COMMON"]); sys.path.insert(0, os.path.dirname(os.environ["SBX_COMMON"]) + "/console")
import sbx_console as c
text, warns = c.render_ruleset(c.validate_policy(json.loads(os.environ["SBXC_POLICY"])))
sys.stdout.write(text)
for w in warns: print("# warning:", w, file=sys.stderr)
PY
  nft -f "$TMP/rules.nft" || { echo "nft rejected the ruleset:"; cat "$TMP/rules.nft"; exit 1; }
}

echo "== allowlist: 203.0.113.2:443, resolver 203.0.113.53 =="
apply '{"sbx_ifaces":["c-sbx"],"dns":["203.0.113.53"],"allow":[{"host":"203.0.113.2","port":443,"comment":"world https"}]}'
expect "sbx -> allowed host:443"                     "tcp443 client=203.0.113.1" "$(from_sbx tcp 203.0.113.2 443)"
expect "  source was masqueraded to the core's WAN"  "203.0.113.1"               "$(from_sbx tcp 203.0.113.2 443)"
expect "sbx -> same host, other port (8080)"         "TIMEOUT"                   "$(from_sbx tcp 203.0.113.2 8080)"
expect "sbx -> other host on the allowed port"       "TIMEOUT"                   "$(from_sbx tcp 203.0.113.99 443)"
expect "sbx -> resolver on a non-DNS port"           "TIMEOUT"                   "$(from_sbx tcp 203.0.113.53 8080)"
expect "sbx DNS asked of the core is redirected"     "dns-ok client=203.0.113.1" "$(from_sbx udp 10.77.0.1 53)"
expect "sbx -> a service on the core (input drop)"   "TIMEOUT"                   "$(from_sbx tcp 10.77.0.1 8787)"
expect "core -> world:8080 (core output is open)"    "tcp8080"                   "$(from_core tcp 203.0.113.2 8080)"
expect "core -> its own service on loopback"         "tcp8787"                   "$(from_core tcp 127.0.0.1 8787)"
ping_out="$(ip netns exec sbx ping -c1 -W2 10.77.0.1 2>&1 | grep -c '1 received')"
expect "sbx can ping the core (diagnostics)"         "1"                         "$ping_out"
ping_out="$(ip netns exec sbx ping -c1 -W2 203.0.113.2 2>&1 | grep -c '1 received')"
expect "sbx cannot ping beyond the core"             "0"                         "$ping_out"

echo "== allowlist emptied =="
apply '{"sbx_ifaces":["c-sbx"],"dns":["203.0.113.53"],"allow":[]}'
expect "sbx -> formerly allowed host:443"            "TIMEOUT"                   "$(from_sbx tcp 203.0.113.2 443)"
expect "sbx DNS still answered"                      "dns-ok"                    "$(from_sbx udp 10.77.0.1 53)"

echo "== no sandbox interface configured =="
apply '{"dns":["203.0.113.53"],"allow":[{"host":"203.0.113.2","port":443}]}' 2>"$TMP/warn.txt"
expect "sbx -> world:443 with no sandbox iface"      "TIMEOUT"                   "$(from_sbx tcp 203.0.113.2 443)"
expect "warning explains why"                        "no sandbox interface"      "$(cat "$TMP/warn.txt")"
expect "core -> world:8080 still works"             "tcp8080"                   "$(from_core tcp 203.0.113.2 8080)"

echo "== revert (what the console does when a change is not confirmed) =="
apply '{"sbx_ifaces":["c-sbx"],"dns":[],"allow":[{"host":"203.0.113.2","port":443}]}'
nft list table inet sbxfw >"$TMP/prev.nft"
apply '{"sbx_ifaces":["c-sbx"],"dns":[],"allow":[]}'
expect "tightened ruleset blocks"                    "TIMEOUT"                   "$(from_sbx tcp 203.0.113.2 443)"
nft delete table inet sbxfw && nft -f "$TMP/prev.nft"
expect "restored ruleset allows again"               "tcp443"                    "$(from_sbx tcp 203.0.113.2 443)"

echo "----"
echo "$PASS passed, $FAIL failed"
kill $(jobs -p) 2>/dev/null
rm -rf "$TMP"
[ "$FAIL" -eq 0 ]
