#!/usr/bin/env python3
"""Generic sbx stub. Identical in every *-sbx tool dir (sync.py copies it; don't
symlink: the desk's bwrap only binds the tool's own directory). Pin with agent-desk -pin.

Forwards the desk's stdin JSON plus any declared input files (/deskbox/in/*,
base64) to the local sbx-broker, prints the tool's JSON, and writes returned
files into /deskbox/out/. Tool name = DESKBOX_TOOL minus a trailing "-sbx".
The desk still validates everything against tcs.yaml afterwards; the stub only
refuses names/types the desk would refuse anyway.
"""
import base64
import json
import os
import re
import stat
import sys
import urllib.request

BROKER = "http://127.0.0.1:8099/run"
IN_DIR, OUT_DIR = "/deskbox/in", "/deskbox/out"
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")

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
    req = urllib.request.Request(
        BROKER, data=json.dumps({"tool": tool, "input": payload, "files": files}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=150) as r:
        resp = json.load(r)
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
