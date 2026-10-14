#!/usr/bin/env python3
"""sbx-console: local control panel for the deskbox sandbox bridge.

Tabs: Firewall (nftables allowlist), Tools (tools.list health, enable/disable, timeouts), Settings.
Tools are deliberately NOT installable from the UI: adding one means writing sandbox code, a
contract and a pinned stub by hand (see sync.py).
Stdlib only. Binds 127.0.0.1; every /api call needs the bearer token in
$SBXC_ETC/token. Runs as an unprivileged user with CAP_NET_ADMIN (see the unit).

Firewall: default-deny in AND out. Only the allowlist, DNS to named resolvers and
management sources get through. Applying a ruleset is two-phase: it goes live,
then reverts after REVERT_S seconds unless confirmed. Only a confirmed ruleset is
written to $SBXC_ETC/ruleset.nft, which Debian's nftables.service loads at boot.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.environ.get("SBX_COMMON", os.path.join(_here, "..", "common"))]
import toollist  # noqa: E402

E = os.environ.get
ETC = E("SBXC_ETC", "/etc/sbxfw")
TOOLLIST = E("SBX_TOOLLIST", "/etc/sbx/tools.list")
BROKER_ENV = E("SBXC_BROKER_ENV", "/etc/sbx/broker.env")
SBX_TOOLS = E("SBXC_SBX_TOOLS", "/opt/sbx/tools")              # sandbox-side tools (lite mode)
HARNESS_TOOLS = E("SBXC_HARNESS_TOOLS", "/opt/deskbox/tools")  # where agent-desk finds <name>-sbx
LISTEN = E("SBXC_LISTEN", "127.0.0.1")
PORT = int(E("SBXC_PORT", "8787"))
REVERT_S = int(E("SBXC_REVERT_S", "60"))
NFT = E("SBXC_NFT", "nft")

NAME_RE = toollist.TOOL_RE
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
COMMENT_RE = re.compile(r"^[A-Za-z0-9 ._:/-]{0,80}$")
MAX_BODY = 256 * 1024

_fw_lock = threading.Lock()
_pending = {"timer": None, "prev": None, "text": None, "deadline": 0}


class Bad(Exception):
    """User-visible validation error (HTTP 400)."""


# ------------------------------------------------------------------ firewall

def default_policy():
    return {"dns": [], "mgmt": [], "allow": []}


def _port(v):
    s = str(v).strip()
    m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", s)
    if not m:
        raise Bad(f"bad port: {s!r}")
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    if not (1 <= a <= b <= 65535):
        raise Bad(f"bad port: {s!r}")
    return f"{a}-{b}" if a != b else str(a)


def _addr(v, allow_name):
    s = str(v).strip().lower()
    try:
        return ipaddress.ip_network(s, strict=False), None
    except ValueError:
        pass
    if allow_name and HOST_RE.match(s):
        return None, s
    raise Bad(f"bad host (IP, CIDR or hostname): {s!r}")


def validate_policy(p):
    """Returns a normalized copy or raises Bad. Only validated values reach the ruleset text."""
    if not isinstance(p, dict):
        raise Bad("policy must be an object")
    out = {"dns": [], "mgmt": [], "allow": []}
    for r in p.get("dns", []):
        net, _ = _addr(r, False)
        if net.num_addresses != 1:
            raise Bad("resolvers must be single IPs")
        out["dns"].append(str(net.network_address))
    for r in p.get("mgmt", []):
        net, _ = _addr(r.get("cidr"), False)
        proto = r.get("proto", "tcp")
        if proto not in ("tcp", "udp"):
            raise Bad("proto must be tcp or udp")
        out["mgmt"].append({"cidr": str(net), "port": _port(r.get("port")), "proto": proto})
    for r in p.get("allow", []):
        net, name = _addr(r.get("host"), True)
        proto = r.get("proto", "tcp")
        if proto not in ("tcp", "udp"):
            raise Bad("proto must be tcp or udp")
        c = str(r.get("comment", ""))
        if not COMMENT_RE.match(c):
            raise Bad("comment: letters, digits, space and ._:/- only (max 80)")
        out["allow"].append({"host": name or str(net), "port": _port(r.get("port")),
                             "proto": proto, "comment": c})
    if len(out["allow"]) > 500 or len(out["mgmt"]) > 50 or len(out["dns"]) > 8:
        raise Bad("too many rules")
    return out


def resolve(host):
    """Hostname -> sorted unique IPs (v4, v6). Raw IPs/CIDRs pass through."""
    net, name = _addr(host, True)
    if net is not None:
        return [str(net)], []
    v4, v6 = set(), set()
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP):
            (v4 if fam == socket.AF_INET else v6).add(sa[0])
    except socket.gaierror:
        pass
    return sorted(v4), sorted(v6)


def _split(addrs):
    v4, v6 = [], []
    for a in addrs:
        (v6 if ":" in a else v4).append(a)
    return v4, v6


def _daddr_rules(addrs, proto, port, comment, direction="daddr"):
    v4, v6 = _split(addrs)
    c = f' comment "{comment}"' if comment else ""
    lines = []
    if v4:
        lines.append(f"ip {direction} {{ {', '.join(v4)} }} {proto} dport {port} accept{c}")
    if v6:
        lines.append(f"ip6 {direction} {{ {', '.join(v6)} }} {proto} dport {port} accept{c}")
    return lines


def render_ruleset(policy, resolver=resolve):
    """policy (validated) -> (nft text, warnings). Deterministic for a given resolution."""
    warns, out_rules, in_rules = [], [], []
    for ip in policy["dns"]:
        for proto in ("udp", "tcp"):
            out_rules += _daddr_rules([ip], proto, 53, "dns")
    for r in policy["allow"]:
        v4, v6 = resolver(r["host"])
        if not v4 and not v6:
            warns.append(f"{r['host']}: did not resolve, rule skipped")
            continue
        out_rules += _daddr_rules(v4 + v6, r["proto"], r["port"], r["comment"])
    for r in policy["mgmt"]:
        in_rules += _daddr_rules([r["cidr"]], r["proto"], r["port"], "mgmt", direction="saddr")
    ind = "\n    "
    text = f"""# Generated by sbx-console. Do not edit; changes are overwritten.
table inet sbxfw
delete table inet sbxfw
table inet sbxfw {{
  chain input {{
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    ct state invalid drop
    {ind.join(in_rules) if in_rules else '# no management sources allowed'}
    limit rate 10/second log prefix "sbxfw-drop: " drop
  }}
  chain forward {{
    type filter hook forward priority 0; policy drop;
  }}
  chain output {{
    type filter hook output priority 0; policy drop;
    oif lo accept
    ct state established,related accept
    ct state invalid drop
    {ind.join(out_rules) if out_rules else '# no egress allowed'}
    limit rate 10/second log prefix "sbxfw-drop: " drop
  }}
}}
"""
    return text, warns


def _nft(args, stdin=None):
    p = subprocess.run([NFT] + args, input=stdin, capture_output=True, text=True, timeout=20)
    return p.returncode, (p.stdout if p.returncode == 0 else p.stderr).strip()


def load_policy():
    try:
        with open(os.path.join(ETC, "policy.json"), encoding="utf-8") as f:
            return validate_policy(json.load(f))
    except FileNotFoundError:
        return default_policy()


def _atomic_write(path, data, mode=0o644):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".sbxc-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def fw_apply(policy):
    """Validate, check, make live, start the revert timer. Returns status dict."""
    policy = validate_policy(policy)
    text, warns = render_ruleset(policy)
    with _fw_lock:
        rc, msg = _nft(["-c", "-f", "-"], text)
        if rc:
            raise Bad(f"nft rejected the ruleset: {msg}")
        if _pending["timer"]:                       # re-apply keeps the original rollback point
            _pending["timer"].cancel()
        else:
            rc, prev = _nft(["list", "table", "inet", "sbxfw"])
            _pending["prev"] = prev if rc == 0 else None
        rc, msg = _nft(["-f", "-"], text)
        if rc:
            raise Bad(f"nft failed to apply: {msg}")
        _pending["text"], _pending["policy"] = text, policy
        _pending["deadline"] = time.time() + REVERT_S
        t = threading.Timer(REVERT_S, _revert)
        t.daemon = True
        _pending["timer"] = t
        t.start()
    return {"pending": True, "revert_in_s": REVERT_S, "warnings": warns}


def _revert():
    with _fw_lock:
        if not _pending["timer"]:
            return
        prev = _pending["prev"]
        if prev:
            _nft(["delete", "table", "inet", "sbxfw"])
            _nft(["-f", "-"], prev)
        else:
            _nft(["delete", "table", "inet", "sbxfw"])
        _pending.update(timer=None, prev=None, text=None, deadline=0)


def fw_confirm():
    with _fw_lock:
        if not _pending["timer"]:
            raise Bad("nothing pending")
        _pending["timer"].cancel()
        os.makedirs(ETC, exist_ok=True)
        _atomic_write(os.path.join(ETC, "ruleset.nft"), _pending["text"])
        _atomic_write(os.path.join(ETC, "policy.json"), json.dumps(_pending["policy"], indent=2) + "\n")
        _pending.update(timer=None, prev=None, text=None, deadline=0)
    return {"saved": True}


def fw_cancel():
    if not _pending["timer"]:
        raise Bad("nothing pending")
    _revert()
    return {"reverted": True}


def fw_status():
    rc, live = _nft(["list", "table", "inet", "sbxfw"])
    left = max(0, int(_pending["deadline"] - time.time())) if _pending["timer"] else 0
    return {"policy": load_policy(), "live": rc == 0, "pending": bool(_pending["timer"]),
            "revert_in_s": left, "pending_policy": _pending.get("policy") if _pending["timer"] else None}


DROP_RE = re.compile(r"sbxfw-drop: .*?OUT=(\S*) .*?SRC=(\S+) DST=(\S+).*?PROTO=(\S+)(?: SPT=\d+ DPT=(\d+))?")


def parse_drops(lines):
    """Journal MESSAGE lines -> [{dir, src, dst, proto, dport, count}] outbound first, by count."""
    agg = {}
    for ln in lines:
        m = DROP_RE.search(ln)
        if not m:
            continue
        out_if, src, dst, proto, dport = m.groups()
        d = "out" if out_if else "in"
        k = (d, dst if d == "out" else src, proto.lower(), dport or "")
        agg[k] = agg.get(k, 0) + 1
    rows = [{"dir": k[0], "peer": k[1], "proto": k[2], "dport": k[3], "count": n} for k, n in agg.items()]
    return sorted(rows, key=lambda r: (r["dir"] != "out", -r["count"]))[:100]


def fw_drops():
    try:
        p = subprocess.run(["journalctl", "-k", "-g", "sbxfw-drop", "-n", "500", "-o", "cat", "--no-pager"],
                           capture_output=True, text=True, timeout=10)
        return {"drops": parse_drops(p.stdout.splitlines())}
    except (OSError, subprocess.TimeoutExpired):
        return {"drops": [], "error": "journal unavailable (is the service user in group systemd-journal?)"}


# --------------------------------------------------------------------- tools

def _read_toollist_lines():
    try:
        with open(TOOLLIST, encoding="utf-8") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


OFF_RE = re.compile(r"^#\s*off:\s*([a-z0-9][a-z0-9-]{0,63})(?:\s+(\d+))?\s*$")


def parse_tools():
    """-> [{name, enabled, timeout_s}] from tools.list incl. '# off: name t' disabled lines."""
    tools = []
    for ln in _read_toollist_lines():
        m = OFF_RE.match(ln.strip())
        if m:
            tools.append({"name": m.group(1), "enabled": False,
                          "timeout_s": int(m.group(2) or toollist.DEFAULT_TIMEOUT)})
            continue
        body = ln.split("#", 1)[0].strip()
        if body:
            parts = body.split()
            if NAME_RE.match(parts[0]):
                t = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else toollist.DEFAULT_TIMEOUT
                tools.append({"name": parts[0], "enabled": True, "timeout_s": max(1, min(t, toollist.MAX_TIMEOUT))})
    return tools


def _sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def tool_status(t):
    n = t["name"]
    run = os.path.join(SBX_TOOLS, n, "run")
    hdir = os.path.join(HARNESS_TOOLS, f"{n}-sbx")
    d = dict(t)
    d["sbx_run"] = os.path.isfile(run)
    d["contract"] = os.path.isfile(os.path.join(hdir, "tcs.yaml"))
    d["stub_sha256"] = _sha(os.path.join(hdir, "run.py"))      # what agent-desk -pin should record
    d["run_sha256"] = _sha(run)
    d["ok"] = d["sbx_run"] and d["contract"] and bool(d["stub_sha256"])
    return d


def tools_list():
    return {"tools": [tool_status(t) for t in parse_tools()], "mode": get_mode()}


def _write_toollist(mutator):
    lines = _read_toollist_lines()
    lines = mutator(lines)
    os.makedirs(os.path.dirname(TOOLLIST), exist_ok=True)
    _atomic_write(TOOLLIST, "\n".join(lines) + "\n")
    toollist.load(TOOLLIST)         # raises if we produced something malformed


def tool_toggle(name, enabled):
    if not any(t["name"] == name for t in parse_tools()):
        raise Bad("unknown tool")

    def mut(lines):
        out = []
        for ln in lines:
            m = OFF_RE.match(ln.strip())
            body = ln.split("#", 1)[0].split()
            if enabled and m and m.group(1) == name:
                out.append(f"{name} {m.group(2)}" if m.group(2) else name)
            elif not enabled and body and body[0] == name and not ln.lstrip().startswith("#"):
                out.append(f"# off: {name}" + (f" {body[1]}" if len(body) > 1 else ""))
            else:
                out.append(ln)
        return out
    _write_toollist(mut)
    return {"ok": True, "note": "Deploy tools.list to the sandbox box too (full mode); the broker reads it per request."}


def tool_timeout(name, t):
    t = int(t)
    if not 1 <= t <= toollist.MAX_TIMEOUT:
        raise Bad(f"timeout must be 1..{toollist.MAX_TIMEOUT}")
    if not any(x["name"] == name for x in parse_tools()):
        raise Bad("unknown tool")

    def mut(lines):
        out = []
        for ln in lines:
            m = OFF_RE.match(ln.strip())
            body = ln.split("#", 1)[0].split()
            if m and m.group(1) == name:
                out.append(f"# off: {name} {t}")
            elif body and body[0] == name and not ln.lstrip().startswith("#"):
                out.append(f"{name} {t}")
            else:
                out.append(ln)
        return out
    _write_toollist(mut)
    return {"ok": True}


# ------------------------------------------------------------------ settings

# Lite mode needs no reset: sbx-run gives every job a fresh bwrap tmpfs. These are hooks YOU install.
PRESET_RESETS = {"": "no reset (lite: fresh bwrap tmpfs per job)",
                 "/usr/local/bin/sbx-vm-revert": "revert VM snapshot",
                 "/usr/local/bin/sbx-reboot-and-wait": "reboot sandbox box and wait"}


def _v_host(v):
    if v == "localhost":
        return v
    net, name = _addr(v, True)
    if net is not None and net.num_addresses != 1:
        raise Bad("single host only")
    return name or str(net.network_address)


def _v_int(lo, hi):
    def f(v):
        if not re.fullmatch(r"\d{1,9}", str(v)) or not lo <= int(v) <= hi:
            raise Bad(f"must be an integer {lo}..{hi}")
        return str(int(v))
    return f


def _v_user(v):
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(v)):
        raise Bad("bad username")
    return v


def _v_reset(v):
    if v not in PRESET_RESETS:
        raise Bad("reset command must be one of the installed presets")
    return v


SETTINGS = {  # broker.env key -> validator. Anything else is refused, never written.
    "SBX_HOST": _v_host, "SBX_USER": _v_user, "SBX_PORT": _v_int(1, 65535),
    "SBX_MAX_TIMEOUT": _v_int(1, 120), "SBX_MAX_REQ": _v_int(1024, 16 << 20),
    "SBX_MAX_OUT": _v_int(1024, 16 << 20), "SBX_RESET_CMD": _v_reset,
}


def _read_env():
    vals, lines = {}, []
    try:
        with open(BROKER_ENV, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        pass
    for ln in lines:
        m = re.match(r"^([A-Z_]+)=(.*)$", ln)
        if m:
            vals[m.group(1)] = m.group(2)
    return vals, lines


def get_mode():
    try:
        with open(os.path.join(ETC, "console.json"), encoding="utf-8") as f:
            return "full" if json.load(f).get("mode") == "full" else "lite"
    except (OSError, ValueError):
        return "lite"


def settings_get():
    vals, _ = _read_env()
    return {"mode": get_mode(), "broker": {k: vals.get(k, "") for k in SETTINGS},
            "reset_presets": PRESET_RESETS, "fw_revert_s": REVERT_S}


def settings_set(body):
    changes = {}
    for k, v in (body.get("broker") or {}).items():
        if k not in SETTINGS:
            raise Bad(f"unknown setting {k}")
        changes[k] = SETTINGS[k](str(v).strip())
    mode = body.get("mode")
    if mode is not None and mode not in ("lite", "full"):
        raise Bad("mode must be lite or full")
    if changes:
        vals, lines = _read_env()
        seen, out = set(), []
        for ln in lines:
            m = re.match(r"^([A-Z_]+)=", ln)
            if m and m.group(1) in changes:
                out.append(f"{m.group(1)}={changes[m.group(1)]}")
                seen.add(m.group(1))
            else:
                out.append(ln)
        out += [f"{k}={v}" for k, v in changes.items() if k not in seen]
        os.makedirs(os.path.dirname(BROKER_ENV), exist_ok=True)
        _atomic_write(BROKER_ENV, "\n".join(out) + "\n", 0o640)
    if mode is not None:
        os.makedirs(ETC, exist_ok=True)
        _atomic_write(os.path.join(ETC, "console.json"), json.dumps({"mode": mode}) + "\n")
    return {"saved": True, "restart_required": bool(changes),
            "hint": "sudo systemctl restart sbx-broker" if changes else ""}


# ----------------------------------------------------------------------- http

ROUTES = {
    ("GET", "/api/fw"): lambda b: fw_status(),
    ("GET", "/api/fw/drops"): lambda b: fw_drops(),
    ("POST", "/api/fw/apply"): lambda b: fw_apply(b.get("policy")),
    ("POST", "/api/fw/confirm"): lambda b: fw_confirm(),
    ("POST", "/api/fw/cancel"): lambda b: fw_cancel(),
    ("GET", "/api/tools"): lambda b: tools_list(),
    ("POST", "/api/tools/toggle"): lambda b: tool_toggle(b.get("name"), bool(b.get("enabled"))),
    ("POST", "/api/tools/timeout"): lambda b: tool_timeout(b.get("name"), b.get("timeout_s")),
    ("GET", "/api/settings"): lambda b: settings_get(),
    ("POST", "/api/settings"): lambda b: settings_set(b),
}


def load_token():
    p = os.path.join(ETC, "token")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "sbx-console"
    token = None

    def log_message(self, fmt, *a):          # no request bodies / paths with data in logs
        print(time.strftime("%H:%M:%S"), self.command, self.path.split("?")[0], file=sys.stderr, flush=True)

    def _send(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):                      # blocks DNS-rebinding from a web page
        h = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return h in ("127.0.0.1", "localhost", "::1")

    def _handle(self, method):
        path = self.path.split("?")[0]
        if not self._host_ok():
            return self._send(403, {"error": "bad host header"})
        if method == "GET" and path == "/":
            with open(os.path.join(_here, "ui.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        route = ROUTES.get((method, path))
        if not route:
            return self._send(404, {"error": "not found"})
        auth = self.headers.get("Authorization", "")
        if not self.token or not hmac.compare_digest(auth, f"Bearer {self.token}"):
            return self._send(401, {"error": "bad or missing token"})
        body = {}
        if method == "POST":
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._send(415, {"error": "application/json required"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                assert isinstance(body, dict)
            except (ValueError, AssertionError):
                return self._send(400, {"error": "bad json"})
        try:
            return self._send(200, route(body))
        except Bad as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:               # never leak internals/paths
            print("ERROR", type(e).__name__, e, file=sys.stderr, flush=True)
            return self._send(500, {"error": "internal error"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def init():
    """First-run setup: token + a default-deny ruleset that nftables.service loads at boot."""
    import secrets
    os.makedirs(ETC, exist_ok=True)
    tp = os.path.join(ETC, "token")
    if not os.path.exists(tp):
        _atomic_write(tp, secrets.token_urlsafe(24) + "\n", 0o640)
    rp = os.path.join(ETC, "ruleset.nft")
    if not os.path.exists(rp):
        _atomic_write(rp, render_ruleset(default_policy())[0])
        _atomic_write(os.path.join(ETC, "policy.json"), json.dumps(default_policy(), indent=2) + "\n")
    print(f"initialised {ETC} (token: {tp})")


def main():
    if "--init" in sys.argv:
        return init()
    Handler.token = load_token()
    if not Handler.token:
        sys.exit(f"no token at {os.path.join(ETC, 'token')}; run: sbx_console.py --init")
    print(f"sbx-console on http://{LISTEN}:{PORT}", file=sys.stderr, flush=True)
    ThreadingHTTPServer((LISTEN, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
