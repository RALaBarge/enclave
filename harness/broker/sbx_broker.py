#!/usr/bin/env python3
"""sbx-broker: runs on the trusted harness box.

Accepts {"tool": ..., "input": {...}} on 127.0.0.1 from the *-sbx stub tools,
runs the matching tool on the sandbox box over SSH (harness initiates, sandbox
never connects back), and returns the tool's JSON stdout.

Stdlib only. One job at a time (the sandbox is destroyed/reset per run).

Nothing the sandbox sends back is logged except size, hash, and exit status.
Sandbox stderr is discarded; error responses never contain sandbox text.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.environ.get("SBX_COMMON", _here), os.path.join(_here, "..", "..", "common")]
import toollist  # noqa: E402

TOOLLIST = os.environ.get("SBX_TOOLLIST", "/etc/sbx/tools.list")
LISTEN = os.environ.get("SBX_LISTEN", "127.0.0.1")
PORT = int(os.environ.get("SBX_BROKER_PORT", "8099"))
SBX_HOST = os.environ["SBX_HOST"]                      # e.g. 10.77.0.2
SBX_USER = os.environ.get("SBX_USER", "sbx")
SBX_PORT = os.environ.get("SBX_PORT", "22")
SBX_KEY = os.environ["SBX_KEY"]                        # private key path
SBX_KNOWN_HOSTS = os.environ.get("SBX_KNOWN_HOSTS", "/etc/sbx/known_hosts")
MAX_REQ = int(os.environ.get("SBX_MAX_REQ", str(1 << 20)))     # 1 MiB in
MAX_OUT = int(os.environ.get("SBX_MAX_OUT", str(1 << 20)))     # 1 MiB out
MAX_TIMEOUT = int(os.environ.get("SBX_MAX_TIMEOUT", "120"))
# Run after every job to destroy/reset the sandbox (e.g. revert snapshot,
# reboot + wait). Empty = no reset. The sandbox is unusable until it returns.
RESET_CMD = os.environ.get("SBX_RESET_CMD", "")

TOOL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
# sbx-run exit code -> reason (sandbox stderr text is never trusted or kept)
REASONS = {2: "bad_request", 3: "unknown_tool", 4: "stdout_too_big", 5: "tool_failed",
           6: "stdout_not_json", 7: "bwrap_failed", 8: "killed_by_signal",
           9: "sandbox_timeout", 10: "bad_output_files", 255: "ssh_failed"}
# Worth a human looking at: the sandbox layer itself misbehaved, or the tool died on a signal.
ALERT = {"bwrap_failed", "killed_by_signal", "ssh_failed", "bad_envelope"}
_lock = threading.Lock()  # one job at a time


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, file=sys.stderr, flush=True)


def ssh_cmd():
    return [
        "ssh", "-T", "-i", SBX_KEY,
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "RequestTTY=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={SBX_KNOWN_HOSTS}",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-p", SBX_PORT,
        f"{SBX_USER}@{SBX_HOST}",
    ]  # no remote command: the sandbox forces /opt/sbx/sbx-run


def run_on_sbx(request, wall_timeout):
    """Returns (status, body_bytes). status: 'ok' | 'fail' | 'timeout' | 'toobig' | 'badjson'."""
    p = subprocess.Popen(ssh_cmd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    buf = bytearray()
    overflow = threading.Event()

    def reader():
        while True:
            chunk = p.stdout.read(65536)
            if not chunk:
                return
            buf.extend(chunk)
            if len(buf) > MAX_OUT:
                overflow.set()
                p.kill()
                return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        p.stdin.write(json.dumps(request).encode())
        p.stdin.close()
    except BrokenPipeError:
        pass
    try:
        p.wait(timeout=wall_timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        t.join(1)
        return "timeout", b""
    t.join(2)
    if overflow.is_set():
        return "toobig", b""
    if p.returncode != 0:
        return REASONS.get(p.returncode, f"rc{p.returncode}"), b""
    if not valid_envelope(bytes(buf)):
        return "bad_envelope", b""
    return "ok", bytes(buf)


def valid_envelope(raw):
    """{"output": <any JSON>, "files": {name: base64}} and nothing else."""
    try:
        doc = json.loads(raw)
        files = doc["files"]
        if set(doc) != {"output", "files"} or not isinstance(files, dict) or len(files) > 16:
            return False
        for name, b64 in files.items():
            if not FILE_RE.match(name):
                return False
            base64.b64decode(b64, validate=True)
        return True
    except (ValueError, KeyError, TypeError):
        return False


def reset_sbx():
    if not RESET_CMD:
        return
    r = subprocess.run(RESET_CMD, shell=True, timeout=300)
    log(f"reset rc={r.returncode}")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default access log
        pass

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, why):
        self._send(code, json.dumps({"error": why}).encode())

    def do_POST(self):
        if self.path != "/run":
            return self._err(404, "not_found")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_REQ:
            return self._err(413, "bad_size")
        try:
            req = json.loads(self.rfile.read(n))
            tool = req["tool"]
            if not isinstance(tool, str) or not TOOL_RE.match(tool):
                raise ValueError
            inp = req.get("input", {})
            files = req.get("files", {})
            if not isinstance(files, dict) or len(files) > 16 or not all(
                    FILE_RE.match(k) and isinstance(v, str) for k, v in files.items()):
                raise ValueError
        except (ValueError, KeyError):
            return self._err(400, "bad_request")

        try:
            entry = toollist.load(TOOLLIST).get(tool)  # re-read: edits apply without restart
        except (OSError, ValueError):
            return self._err(500, "toollist_error")
        if entry is None:
            return self._err(403, "unknown_tool")
        timeout = min(entry["timeout_s"], MAX_TIMEOUT)

        started = time.time()
        with _lock:
            try:
                status, body = run_on_sbx(
                    {"tool": tool, "input": inp, "files": files, "timeout_s": timeout},
                    wall_timeout=timeout + 15)
            finally:
                try:
                    reset_sbx()
                except Exception as e:  # reset failure must not hide the result
                    log(f"reset failed: {e}")
        log(f"{'ALERT ' if status in ALERT else ''}tool={tool} status={status} bytes={len(body)} "
            f"sha256={hashlib.sha256(body).hexdigest()[:16]} "
            f"dur={time.time() - started:.1f}s")
        if status == "ok":
            return self._send(200, body)
        return self._err(502, status)


if __name__ == "__main__":
    log(f"sbx-broker on {LISTEN}:{PORT} -> {SBX_USER}@{SBX_HOST}")
    ThreadingHTTPServer((LISTEN, PORT), H).serve_forever()
