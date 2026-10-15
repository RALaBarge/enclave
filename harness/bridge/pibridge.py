"""Pi session manager: one `pi --mode rpc` subprocess per session, JSONL over stdio.

Framing is LF-only (Pi's RPC doc is explicit: generic line readers that also split on U+2028/2029 break it),
so we read the binary stream with readline(), which splits on b"\\n" only.
"""
from __future__ import annotations

import collections
import json
import subprocess
import threading
import time
import uuid

EVENT_RING = 5000          # events kept per session for reconnect/replay
STDERR_RING = 200
MAX_LINE = 64 * 1024 * 1024  # long-session get_messages replies are big (piface hit hangs below this)
CALL_PREFIX = "br-"  # ids the bridge mints for its own request/response matching


class RpcError(Exception):
    pass


class Session:
    def __init__(self, pi_cmd: list[str], cwd: str, name: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.cwd = cwd
        self.name = name or self.id
        self.created = time.time()
        self.events: collections.deque[tuple[int, dict]] = collections.deque(maxlen=EVENT_RING)
        self.stderr: collections.deque[str] = collections.deque(maxlen=STDERR_RING)
        self.cond = threading.Condition()
        self._seq = 0
        self._pending: dict[str, dict] = {}
        self._pending_cv = threading.Condition()
        self._wlock = threading.Lock()
        self.exited: int | None = None
        self.proc = subprocess.Popen(
            pi_cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    # ---- reader threads -------------------------------------------------
    def _read_stdout(self) -> None:
        out = self.proc.stdout
        while True:
            raw = out.readline(MAX_LINE)
            if not raw:
                break
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                self.stderr.append("non-JSON on stdout: " + raw[:200].decode("utf-8", "replace"))
                continue
            if msg.get("type") == "response" and str(msg.get("id", "")).startswith(CALL_PREFIX):
                with self._pending_cv:
                    if msg["id"] in self._pending:      # a late reply to a call that already timed out is dropped
                        self._pending[msg["id"]] = msg
                        self._pending_cv.notify_all()
                continue
            self.publish(msg)
        self.exited = self.proc.wait()
        self.publish({"type": "bridge_session_exit", "code": self.exited})
        with self._pending_cv:
            self._pending_cv.notify_all()

    def _read_stderr(self) -> None:
        for raw in iter(self.proc.stderr.readline, b""):
            self.stderr.append(raw.decode("utf-8", "replace").rstrip())

    def publish(self, msg: dict) -> int:
        with self.cond:
            self._seq += 1
            self.events.append((self._seq, msg))
            self.cond.notify_all()
            return self._seq

    # ---- commands -------------------------------------------------------
    def _write(self, cmd: dict) -> None:
        if self.exited is not None:
            raise RpcError("session process has exited (code %s)" % self.exited)
        line = (json.dumps(cmd, separators=(",", ":")) + "\n").encode("utf-8")
        with self._wlock:
            try:
                self.proc.stdin.write(line)
            except OSError as e:
                raise RpcError("write to pi failed: %s" % e)

    def call(self, cmd: dict, timeout: float = 30.0) -> dict:
        """Send a command and wait for its response record.

        Correlation uses an id minted here, never the caller's: two callers sharing an id must not share a slot.
        The caller's own id, if any, is put back on the response.
        """
        client_id = cmd.get("id")
        cid = CALL_PREFIX + uuid.uuid4().hex
        cmd = {**cmd, "id": cid}
        with self._pending_cv:
            self._pending[cid] = {}
        try:
            self._write(cmd)
            deadline = time.time() + timeout
            with self._pending_cv:
                while not self._pending[cid]:
                    left = deadline - time.time()
                    if left <= 0:
                        raise RpcError("timeout waiting for response to %s" % cmd.get("type"))
                    if self.exited is not None and not self._pending[cid]:
                        raise RpcError("session process exited")
                    self._pending_cv.wait(left)
                resp = dict(self._pending[cid])
            if client_id is not None:
                resp["id"] = client_id
            return resp
        finally:
            with self._pending_cv:
                self._pending.pop(cid, None)

    def fire(self, cmd: dict) -> None:
        """Send a command without waiting (extension_ui_response and similar)."""
        self._write(cmd)

    # ---- events ---------------------------------------------------------
    def events_since(self, since: int, wait: float = 15.0) -> list[tuple[int, dict]]:
        with self.cond:
            if not any(s > since for s, _ in self.events) and self.exited is None:
                self.cond.wait(wait)
            return [(s, m) for s, m in self.events if s > since]

    def last_seq(self) -> int:
        with self.cond:
            return self._seq

    def stop(self) -> None:
        try:
            self.proc.stdin.close()      # Pi's documented orderly shutdown
        except OSError:
            pass
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        time.sleep(0.05)
        for f in (self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except OSError:
                pass

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "cwd": self.cwd, "created": self.created,
                "alive": self.exited is None, "exit_code": self.exited, "last_seq": self.last_seq()}


class Registry:
    def __init__(self, pi_cmd: list[str]):
        self.pi_cmd = pi_cmd
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()

    def create(self, cwd: str, name: str | None = None) -> Session:
        s = Session(self.pi_cmd, cwd, name)
        with self.lock:
            self.sessions[s.id] = s
        return s

    def get(self, sid: str) -> Session | None:
        with self.lock:
            return self.sessions.get(sid)

    def remove(self, sid: str) -> bool:
        with self.lock:
            s = self.sessions.pop(sid, None)
        if s:
            s.stop()
        return s is not None

    def list(self) -> list[dict]:
        with self.lock:
            return [s.info() for s in self.sessions.values()]

    def stop_all(self) -> None:
        for sid in list(self.sessions):
            self.remove(sid)
