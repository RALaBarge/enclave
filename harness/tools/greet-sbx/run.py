#!/usr/bin/env python3
"""Generic sbx stub. Identical in every *-sbx tool dir (sync.py copies it; don't
symlink: the desk's bwrap only binds the tool's own directory). Pin with agent-desk -pin.

Forwards the desk's stdin JSON plus any declared input files (/deskbox/in/*,
base64) to the local sbx-broker over the unix socket the desk binds into this
sandbox, prints the tool's JSON, and writes returned files into /deskbox/out/.
Tool name = DESKBOX_TOOL minus a trailing "-sbx". The desk still validates
everything against tcs.yaml afterwards; the stub only refuses names/types the
desk would refuse anyway.

The socket, not TCP: this stub's contract says network:false, so there is no
network namespace to reach a loopback port from, and none is wanted. The only
route out of the job is the one path the contract declared.
"""
import base64
import http.client
import json
import os
import re
import socket
import stat
import sys

SOCKET = "/deskbox/broker.sock"
IN_DIR, OUT_DIR = "/deskbox/in", "/deskbox/out"
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")


class UnixConnection(http.client.HTTPConnection):
    """http.client over a unix socket; there is no address to resolve."""

    def __init__(self, path, timeout=150):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


tool = os.environ.get("DESKBOX_TOOL", "")
if tool.endswith("-sbx"):
    tool = tool[:-4]

try:
    payload = json.load(sys.stdin)
    files = {}
    if os.path.isdir(IN_DIR):
        for name in sorted(os.listdir(IN_DIR)):
            path = os.path.join(IN_DIR, name)
            if NAME_RE.match(name) and stat.S_ISREG(os.lstat(path).st_mode):
                with open(path, "rb") as f:
                    files[name] = base64.b64encode(f.read()).decode()
    body = json.dumps({"tool": tool, "input": payload, "files": files}).encode()
    conn = UnixConnection(SOCKET)
    try:
        conn.request("POST", "/run", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"broker status {resp.status}")
    finally:
        conn.close()
    resp = json.loads(raw)
    for name, b64 in resp["files"].items():
        if not NAME_RE.match(name):
            raise ValueError("bad name")
        with open(os.path.join(OUT_DIR, name), "wb") as f:
            f.write(base64.b64decode(b64, validate=True))
    if resp["output"] is not None or not resp["files"]:
        json.dump(resp["output"], sys.stdout)  # output-file tools print nothing, like the originals
except Exception as e:
    print(f"sbx stub failed: {type(e).__name__}", file=sys.stderr)
    sys.exit(1)
