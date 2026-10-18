#!/usr/bin/env python3
"""sbx-broker: runs on the trusted harness box.

Accepts {"tool": ..., "input": {...}} on a unix socket from the *-sbx stub
tools, runs the matching tool on the sandbox box over SSH (harness initiates,
sandbox never connects back), and returns the tool's JSON stdout.

The socket is the point: every stub runs with no network at all (network:false
in its contract) and the desk binds this one socket into the job's sandbox, so
the stub's only route out is a tool call that the desk has already validated.
A loopback TCP listener would instead have meant giving each stub the host
network namespace, which is the whole core's internet.

Stdlib only. One job at a time (the sandbox is destroyed/reset per run).
Callers wait in a bounded FIFO lane (SBX_QUEUE_MAX, SBX_QUEUE_WAIT_S); GET
/status reports that lane.

Nothing the sandbox sends back is logged except size, hash, and exit status.
Sandbox stderr is discarded; error responses never contain sandbox text.
"""
import base64
import hashlib
import json
import os
import re
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.environ.get("SBX_COMMON", _here), os.path.join(_here, "..", "..", "common")]
import toollist  # noqa: E402

TOOLLIST = os.environ.get("SBX_TOOLLIST", "/etc/sbx/tools.list")
SOCKET_PATH = os.environ.get("SBX_SOCKET", "/run/sbx/broker.sock")  # "" = no unix listener
SOCKET_MODE = int(os.environ.get("SBX_SOCKET_MODE", "0660"), 8)
SOCKET_GROUP = os.environ.get("SBX_SOCKET_GROUP", "sbxdesk")  # the desk's account is in this group
LISTEN = os.environ.get("SBX_LISTEN", "")           # "" = no TCP listener (the default)
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
# One sandbox lane: callers that arrive while a job runs wait in arrival
# order, but only while fewer than QUEUE_MAX are already waiting and only for
# QUEUE_WAIT_S seconds. Passing either bound answers 503 lane_busy.
QUEUE_MAX = max(0, int(os.environ.get("SBX_QUEUE_MAX", "4")))
QUEUE_WAIT_S = max(0.0, float(os.environ.get("SBX_QUEUE_WAIT_S", "60")))

TOOL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
# sbx-run exit code -> reason (sandbox stderr text is never trusted or kept)
REASONS = {2: "bad_request", 3: "unknown_tool", 4: "stdout_too_big", 5: "tool_failed",
           6: "stdout_not_json", 7: "bwrap_failed", 8: "killed_by_signal",
           9: "sandbox_timeout", 10: "bad_output_files", 11: "request_too_big", 255: "ssh_failed"}
# Worth a human looking at: the sandbox layer itself misbehaved, or the tool died on a signal.
ALERT = {"bwrap_failed", "killed_by_signal", "ssh_failed", "bad_envelope"}


def _utc_stamp(when=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


class LaneFull(Exception):
    """The wait queue is at SBX_QUEUE_MAX; this caller was not admitted."""

    def __init__(self, queued, depth_max):
        self.queued = queued
        self.depth_max = depth_max
        super().__init__(f"lane busy: {queued} queued (max {depth_max})")


class LaneTimeout(Exception):
    """The caller waited SBX_QUEUE_WAIT_S and was not served."""

    def __init__(self, queued, depth_max, wait_s):
        self.queued = queued
        self.depth_max = depth_max
        self.wait_s = wait_s
        super().__init__(f"lane busy: waited {wait_s:g}s ({queued} queued)")


class SandboxNotReset(Exception):
    """The previous reset failed and this call's retry did not fix it."""


class SerialLane:
    """One job at a time, with a bounded FIFO wait queue.

    The lane stays held for the whole job, including any reset the job runs,
    so the next job never starts on a sandbox in an unknown state. Callers are
    served in arrival order; one is refused when depth_max callers are already
    waiting, or when wait_s passes before it is served.
    """

    def __init__(self, depth_max, wait_s):
        self.depth_max = max(0, int(depth_max))
        self.wait_s = max(0.0, float(wait_s))
        self._cv = threading.Condition()
        self._tickets = deque()
        self._busy = False
        self._last = None

    def status(self):
        with self._cv:
            return {"busy": self._busy, "queued": len(self._tickets),
                    "depth_max": self.depth_max,
                    "last": dict(self._last) if self._last else None,
                    "lane": "serial"}

    def run(self, tool, job):
        """Run job() alone and return its status.

        job() is called only when the lane is held, so anything it does
        (including a reset) happens before the next caller can start. If it
        raises SandboxNotReset, no job record is written: nothing ran.
        """
        ticket = object()
        with self._cv:
            if self._busy or self._tickets:
                if len(self._tickets) >= self.depth_max:
                    raise LaneFull(len(self._tickets), self.depth_max)
                if self.wait_s <= 0:
                    raise LaneTimeout(len(self._tickets), self.depth_max, self.wait_s)
                self._tickets.append(ticket)
                deadline = time.monotonic() + self.wait_s
                while ticket in self._tickets:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._tickets.remove(ticket)
                        raise LaneTimeout(len(self._tickets), self.depth_max, self.wait_s)
                    self._cv.wait(remaining)
            self._busy = True
            started = time.time()
        try:
            status = job()
        except SandboxNotReset:
            self._handoff()
            raise
        except BaseException:
            self._finish(tool, "error", started)
            raise
        self._finish(tool, status if isinstance(status, str) else "error", started)
        return status

    def _finish(self, tool, status, started):
        with self._cv:
            self._last = {"tool": tool, "status": status,
                          "dur_s": round(time.time() - started, 3),
                          "started_at": _utc_stamp(started)}
            self._handoff_locked()

    def _handoff(self):
        with self._cv:
            self._handoff_locked()

    def _handoff_locked(self):
        if self._tickets:
            self._tickets.popleft()  # hand the lane to the oldest waiter
            self._busy = True
            self._cv.notify_all()
        else:
            self._busy = False


lane = SerialLane(QUEUE_MAX, QUEUE_WAIT_S)

# What the last reset did. A failed reset means the sandbox is in an unknown
# state, so the next job is refused until a reset succeeds. "at" is null until
# a reset has run. The lane is held whenever this is written.
_reset_lock = threading.Lock()
_reset_state = {"ok": True, "rc": 0, "at": None}


def reset_status():
    with _reset_lock:
        return dict(_reset_state)


def _record_reset(ok, rc):
    with _reset_lock:
        _reset_state["ok"] = bool(ok)
        _reset_state["rc"] = int(rc)
        _reset_state["at"] = _utc_stamp()


def broker_status():
    doc = lane.status()
    doc["reset"] = reset_status()
    return doc


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


def _run_reset():
    """Run the configured reset once; returns its exit status."""
    return subprocess.run(RESET_CMD, shell=True, timeout=300).returncode


def reset_sbx():
    """Reset the sandbox and record whether it is safe for the next job.

    Returns True when there is nothing to reset (lite mode, where each job
    already gets a fresh bwrap) or when the command exits 0. A non-zero exit,
    timeout or failure to start is recorded; the next job is then refused
    until a reset succeeds. Called only while the lane is held.
    """
    if not RESET_CMD:
        return True
    try:
        rc = _run_reset()
        ok = rc == 0
        log(f"reset rc={rc}")
    except subprocess.TimeoutExpired:
        rc, ok = -1, False
        log("reset timed out")
    except Exception as e:
        rc, ok = -1, False
        log(f"reset could not run: {e}")
    _record_reset(ok, rc)
    return ok


def ensure_reset():
    """Called with the lane held: retry a failed reset once before the job."""
    if reset_status()["ok"]:
        return True
    return reset_sbx()


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

    def _lane_busy(self, reason, exc, wait_s=None):
        body = {"error": "lane_busy", "reason": reason, "lane": "serial",
                "busy": True, "queued": exc.queued, "depth_max": exc.depth_max}
        if wait_s is not None:
            body["wait_s"] = wait_s
        self._send(503, json.dumps(body).encode())

    def do_GET(self):
        if self.path != "/status":
            return self._err(404, "not_found")
        self._send(200, json.dumps(broker_status()).encode())

    def do_POST(self):
        if self.path != "/run":
            return self._err(404, "not_found")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._err(400, "bad_request")
        if n <= 0 or n > MAX_REQ:
            return self._err(413, "bad_size")
        try:
            req = json.loads(self.rfile.read(n))
            if not isinstance(req, dict):
                raise ValueError
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
        network = bool(entry["network"])

        enqueued = time.time()
        out = {}
        job_started = []

        def job():
            job_started.append(time.time())
            if not ensure_reset():  # the previous reset failed: heal or refuse
                raise SandboxNotReset()
            try:
                out["status"], out["body"] = run_on_sbx(
                    {"tool": tool, "input": inp, "files": files, "timeout_s": timeout},
                    wall_timeout=timeout + 15)
            finally:
                reset_sbx()  # a failure is recorded and gates the next job
            return out["status"]

        try:
            status = lane.run(tool, job)
        except SandboxNotReset:
            reset = reset_status()
            log(f"SANDBOX NOT RESET tool={tool} rc={reset['rc']} at={reset['at']}")
            return self._send(503, json.dumps(
                {"error": "sandbox_not_reset", "reset": reset}).encode())
        except LaneFull as q:
            log(f"QUEUE FULL tool={tool} queued={q.queued} depth_max={q.depth_max}")
            return self._lane_busy("queue_full", q)
        except LaneTimeout as q:
            log(f"QUEUE TIMEOUT tool={tool} queued={q.queued} depth_max={q.depth_max} wait_s={q.wait_s:g}")
            return self._lane_busy("queue_timeout", q, q.wait_s)
        body = out.get("body", b"")
        waited = (job_started[0] - enqueued) if job_started else 0.0
        log(f"{'ALERT ' if status in ALERT else ''}tool={tool} net={int(network)} status={status} bytes={len(body)} "
            f"sha256={hashlib.sha256(body).hexdigest()[:16]} "
            f"dur={time.time() - enqueued:.1f}s wait={waited:.1f}s")
        if status == "ok":
            return self._send(200, body)
        return self._err(502, status)


# Only constructed on Linux. The fallback base lets this module be imported on
# a test host without AF_UNIX, where the queue and reset logic are tested
# without ever opening a socket.
_UnixBase = getattr(socketserver, "ThreadingUnixStreamServer",
                    socketserver.ThreadingTCPServer)


class UnixHTTPServer(_UnixBase):
    """A unix socket in front of the same handler.

    A unix socket has no peer address, so give the handler a stand-in for the
    one BaseHTTPRequestHandler expects to find on self.client_address.
    """

    daemon_threads = True

    def get_request(self):
        conn, _ = super().get_request()
        return conn, ("", 0)


def open_socket(path):
    """Bind path, replacing a socket left behind by a dead broker.

    Only a socket is reclaimed, and only after a connect attempt fails: a
    second broker that unlinked a live socket would leave the first serving an
    unlinked inode, which looks like "the broker is up" and behaves like
    nothing is listening. If something that is not a socket sits at that path,
    refuse rather than delete a file the operator did not mean to lose.
    """
    import grp  # Unix-only; kept out of the module import so tests can load it
    if os.path.exists(path):
        mode = os.lstat(path).st_mode
        if not stat.S_ISSOCK(mode):
            raise SystemExit(f"refusing to start: {path} exists and is not a socket")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(path)
        except OSError:
            os.unlink(path)  # nothing answered: stale
        else:
            raise SystemExit(f"refusing to start: another broker is listening on {path}")
        finally:
            probe.close()
    srv = UnixHTTPServer(path, H)
    # The socket is the gate: mode 0660 and only the desk's group, so a
    # process that is not the desk cannot submit a job here. chgrp needs the
    # broker to be a member of SOCKET_GROUP (SupplementaryGroups=sbxdesk in
    # the unit) — chowning to a group it does not belong to is not permitted.
    if SOCKET_GROUP:
        try:
            gid = grp.getgrnam(SOCKET_GROUP).gr_gid
            os.chown(path, -1, gid)
        except KeyError:
            log(f"WARN: socket group {SOCKET_GROUP} does not exist; leaving the socket at the broker's own group")
    os.chmod(path, SOCKET_MODE)
    return srv


if __name__ == "__main__":
    servers = []
    if SOCKET_PATH:
        servers.append((f"unix {SOCKET_PATH} (mode {SOCKET_MODE:04o})", open_socket(SOCKET_PATH)))
    if LISTEN:
        servers.append((f"tcp {LISTEN}:{PORT}", ThreadingHTTPServer((LISTEN, PORT), H)))
    if not servers:
        raise SystemExit("refusing to start: neither SBX_SOCKET nor SBX_LISTEN is set")

    log(f"sbx-broker -> {SBX_USER}@{SBX_HOST} using {', '.join(name for name, _ in servers)}")
    for _, srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0][1].serve_forever()
