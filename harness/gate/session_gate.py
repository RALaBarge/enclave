#!/usr/bin/env python3
"""sbx-session-gate: the one place that forces the `session` firewall profile.

Why this is a process and not a function inside each harness bridge: the
guarantee "a new agent session always starts on the session profile" has to be
implemented once. With the lockdown inside pi-bridge, a second harness in the
core gets no lockdown at all, and the obvious fix - copy the code - produces
two implementations of a security guarantee, one of which will drift.

The gate holds the tighten-only token and exposes exactly one action. It
cannot loosen anything: there is no endpoint for it, and the console's token
is not the console's main token, so even a compromised gate process cannot
open the firewall back up (console/sbx_console.py: the tighten token may call
/api/fw/lockdown and nothing else).

Transport is a unix socket, so "who may call this" is a file mode: the socket
is 0660 in the bridge's group, and a process outside that group cannot reach
it. No shared secret, nothing to rotate.

  GET  /status    -> {"ok": true, "profiles_forcible": ["session"], "last": {...}|null}
  POST /lockdown  -> {"active": "session", "changed": bool, ...} from the console
                     or 502 with the console's reason (never the token)
"""
import grp
import json
import os
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

SOCKET = os.environ.get("SBX_GATE_SOCKET", "/run/sbx-gate/gate.sock")
SOCKET_MODE = int(os.environ.get("SBX_GATE_SOCKET_MODE", "0660"), 8)
SOCKET_GROUP = os.environ.get("SBX_GATE_SOCKET_GROUP", "sbxgate")
CONSOLE = os.environ.get("SBX_GATE_CONSOLE", "http://127.0.0.1:8787").rstrip("/")
TOKEN_FILE = os.environ.get("SBX_GATE_TOKEN_FILE", "/etc/sbxfw/tighten-token")
TIMEOUT = float(os.environ.get("SBX_GATE_TIMEOUT", "8"))

_last = {"result": None, "at": None, "error": None}
_lock = threading.Lock()


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def lockdown():
    """Ask the console to make sure the session profile is active."""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError as e:
        raise RuntimeError("cannot read the tighten token: %s" % type(e).__name__)
    if not token:
        raise RuntimeError("the tighten token is empty")
    req = urllib.request.Request(
        CONSOLE + "/api/fw/lockdown", data=b"{}", method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")
        except ValueError:
            pass
        raise RuntimeError("console answered %d%s" % (e.code, ": " + detail if detail else ""))
    except (OSError, ValueError) as e:
        raise RuntimeError("%s: %s" % (type(e).__name__, e))


class H(BaseHTTPRequestHandler):
    server_version = "sbx-session-gate"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            with _lock:
                last = dict(_last)
            return self._json(200, {"ok": True, "profiles_forcible": ["session"], "last": last})
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/lockdown":
            # Deliberately no verb that relaxes anything: the whole point of a
            # separate, narrow token is that this process cannot widen access.
            return self._json(404, {"error": "not_found"})
        try:
            doc = lockdown()
        except RuntimeError as e:
            with _lock:
                _last.update(result=None, at=time.time(), error=str(e))
            log("lockdown failed: %s" % e)
            return self._json(502, {"error": str(e)})
        with _lock:
            _last.update(result=doc, at=time.time(), error=None)
        log("lockdown ok: %s" % json.dumps(doc)[:200])
        return self._json(200, doc)


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def get_request(self):
        conn, _ = super().get_request()
        return conn, ("", 0)


def main():
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)          # stale socket from a previous process
    os.makedirs(os.path.dirname(SOCKET) or ".", exist_ok=True)
    srv = UnixServer(SOCKET, H)
    try:
        os.chown(SOCKET, -1, grp.getgrnam(SOCKET_GROUP).gr_gid)
    except KeyError:
        log("WARN: group %s does not exist; the socket keeps this process's group" % SOCKET_GROUP)
    os.chmod(SOCKET, SOCKET_MODE)
    log("sbx-session-gate on %s (mode %04o) -> %s" % (SOCKET, SOCKET_MODE, CONSOLE))
    srv.serve_forever()


if __name__ == "__main__":
    main()
