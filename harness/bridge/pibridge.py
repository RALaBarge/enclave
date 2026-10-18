"""Pi session manager: one `pi --mode rpc` subprocess per session, JSONL over stdio.

Framing is LF-only (Pi's RPC doc is explicit: generic line readers that also split on U+2028/2029 break it),
so the reader splits the binary stream on b"\\n" and on nothing else.

Sessions also outlive this process. On POSIX each one is given a pair of FIFOs under the state dir instead of
anonymous pipes; the Pi child keeps its own read+write ends open, so it does not see EOF when the bridge
dies. A later bridge reopens the same FIFOs and speaks JSONL to the child again. If the child is gone, or its
FIFOs cannot be opened, the record is kept as `dead` and listed; no new Pi is started.
"""
from __future__ import annotations

import collections
import json
import os
import select
import signal
import subprocess
import tempfile
import threading
import time
import uuid

try:
    import fcntl
except ImportError:            # Windows has no FIFOs, so its sessions cannot be reattached; they use pipes.
    fcntl = None

EVENT_RING = 5000          # events kept per session for reconnect/replay
STDERR_RING = 200
MAX_LINE = 64 * 1024 * 1024  # long-session get_messages replies are big (piface hit hangs below this)
CALL_PREFIX = "br-"  # ids the bridge mints for its own request/response matching
READ_CHUNK = 64 * 1024
REATTACH_TIMEOUT = 3.0     # how long a freshly started registry waits for a reply from an existing Pi
FIFO_CHANNEL = os.name == "posix" and hasattr(os, "mkfifo") and fcntl is not None
RECORD_VERSION = 1


class RpcError(Exception):
    pass


def under_roots(path: str, roots) -> bool:
    """True when path's realpath is one of roots or below one.

    `roots` are expected resolved (the CLI resolves --cwd-root once at startup and the registry stores
    that form). Only the path being tested is resolved here, so a symlinked cwd counts as the directory
    it really is and every later comparison uses the same form.
    """
    p = os.path.normcase(os.path.realpath(path))
    for root in roots:
        r = os.path.normcase(root)
        if p == r or p.startswith(r + os.sep):
            return True
    return False


def _atomic_json(path, obj) -> None:
    """Write one small JSON file in place: temp file in the same directory, then rename.

    mkstemp creates the temp file 0600; os.replace makes the new contents visible atomically, so a crash
    leaves either the old file or the new one, never a half-written record list.
    """
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".sessions-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pid_alive(pid) -> bool:
    """True when `pid` names a live process we could signal. Only POSIX is probed: os.kill on Windows
    would terminate the target instead of testing it."""
    if os.name != "posix" or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SessionStore:
    """The session records under --state-dir: one JSON file, read once, rewritten atomically on change."""

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "sessions.json")
        self.lock = threading.Lock()
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        try:
            with open(self.path) as f:
                doc = json.load(f)
            items = doc.get("sessions") if isinstance(doc, dict) else None
        except (OSError, ValueError):
            items = None
        self.records: dict[str, dict] = {}
        for r in items or []:
            if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]:
                self.records[r["id"]] = r

    def put(self, rec: dict) -> None:
        with self.lock:
            self.records[rec["id"]] = dict(rec)
            _atomic_json(self.path, {"version": RECORD_VERSION, "sessions": list(self.records.values())})

    def delete(self, sid: str) -> bool:
        with self.lock:
            if self.records.pop(sid, None) is None:
                return False
            _atomic_json(self.path, {"version": RECORD_VERSION, "sessions": list(self.records.values())})
            return True

    def all(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.records.values()]


class Session:
    def __init__(self, pi_cmd: list[str], cwd: str, name: str | None = None, sid: str | None = None,
                 channel: tuple[str, str] | None = None, state_dir: str | None = None):
        self._init(sid or uuid.uuid4().hex[:12], cwd, name, time.time(), state_dir)
        self._spawn(pi_cmd, channel)

    @classmethod
    def from_record(cls, pi_cmd: list[str], rec: dict, state_dir: str | None,
                    cwd_roots=()) -> "Session":
        """Rebuild a session from its record. It is `dead` unless the old Pi is alive and answers its FIFOs.

        A cwd outside the current roots is `refused` instead: see below. Otherwise the probe is a real
        RPC round trip (get_state), not just a pid check: a stale pid can be reused and a live process
        is useless to the bridge if its channel cannot be reopened.

        The cwd is checked against the current --cwd-root allowlist first. A record from a bridge that
        allowed a wider root stays listed but is `refused`: it is not probed, not reattached, and its
        record is not deleted. Adding the root is what brings it back on a later start.
        """
        s = cls.__new__(cls)
        s._init(str(rec["id"]), str(rec.get("cwd") or os.getcwd()), rec.get("name"),
                float(rec.get("created_at") or time.time()), state_dir)
        s.reattached = True
        s.pid = rec.get("pid") if isinstance(rec.get("pid"), int) and not isinstance(rec.get("pid"), bool) else None
        ch = rec.get("channel") if isinstance(rec.get("channel"), dict) else {}
        if not under_roots(s.cwd, cwd_roots):
            # Keep the channel names so a later start with the root added can still reattach; this
            # process only refuses to use them. Nothing is unlinked and the record is not removed.
            s.channel = ({"in": ch["in"], "out": ch["out"]}
                         if isinstance(ch.get("in"), str) and isinstance(ch.get("out"), str) else None)
            s.refused_reason = ("session cwd %s is outside the bridge's allowed roots (%s); "
                                "add --cwd-root if this location is intended"
                                % (s.cwd, ", ".join(cwd_roots)))
            s.dead_reason = s.refused_reason
            s.exited = -1
            return s
        if not _pid_alive(s.pid):
            # Liveness and a working channel decide this, not the recorded state: a process that survived
            # must be reattached even if an earlier start failed to reach it.
            reason = "pi process %s is gone" % s.pid
        elif not (isinstance(ch.get("in"), str) and isinstance(ch.get("out"), str)):
            reason = "no rpc channel survived"
        else:
            try:
                s._attach_fifo(ch["in"], ch["out"])
                s._probe()
            except (OSError, RpcError, ValueError) as e:
                s._close_channel()
                s._unlink_channel()
                reason = "cannot reattach: %s" % e
            else:
                reason = None
        if reason:
            # A record that was alive now that its channel is unusable: drop the stale FIFO names so a
            # later start does not try them again.
            s._unlink_paths(ch.get("in"), ch.get("out"))
            s.channel = None
            s.dead_reason = reason
            ec = rec.get("exit_code")
            s.exited = ec if isinstance(ec, int) and not isinstance(ec, bool) else -1
        else:
            s._start_threads()
        return s

    def _init(self, sid: str, cwd: str, name: str | None, created: float, state_dir: str | None) -> None:
        self.id = sid
        self.cwd = cwd
        self.name = name or sid
        self.created = created
        self.state_dir = state_dir
        self.events: collections.deque[tuple[int, dict]] = collections.deque(maxlen=EVENT_RING)
        self.stderr: collections.deque[str] = collections.deque(maxlen=STDERR_RING)
        self.cond = threading.Condition()
        self._seq = 0
        self._pending: dict[str, dict] = {}
        self._pending_cv = threading.Condition()
        self._wlock = threading.Lock()
        self.exited: int | None = None
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.channel: dict | None = None
        self.reattached = False
        self.removed = False
        self.dead_reason: str | None = None
        self.refused_reason: str | None = None
        self.on_exit = None
        self._in_fd: int | None = None
        self._out_fd: int | None = None
        self._rbuf = bytearray()
        self._pipe = False
        self._reader: threading.Thread | None = None
        self._err_reader: threading.Thread | None = None

    # ---- process and channel --------------------------------------------
    def _spawn(self, pi_cmd: list[str], channel: tuple[str, str] | None) -> None:
        if channel and FIFO_CHANNEL:
            self._spawn_fifo(pi_cmd, *channel)
        else:
            self._spawn_pipe(pi_cmd)

    def _spawn_pipe(self, pi_cmd: list[str]) -> None:
        self._pipe = True
        self.proc = subprocess.Popen(
            pi_cmd, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
        self.pid = self.proc.pid
        self._in_fd = self.proc.stdin.fileno()
        self._out_fd = self.proc.stdout.fileno()
        self._start_threads()

    def _spawn_fifo(self, pi_cmd: list[str], in_path: str, out_path: str) -> None:
        """Spawn Pi with FIFO stdio, then drop our copies of the ends the child must keep.

        Bootstrap with O_RDWR on each FIFO so no open blocks before the child exists. The child inherits
        the O_RDWR descriptor, which makes it a writer on its own stdin and a reader on its own stdout:
        that is what stops it from seeing EOF/EPIPE when this process dies, and it is why stop() signals
        instead of closing stdin.
        """
        os.makedirs(os.path.dirname(in_path), mode=0o700, exist_ok=True)
        try:
            for p in (in_path, out_path):
                try:
                    os.mkfifo(p, 0o600)
                except FileExistsError:
                    os.unlink(p)
                    os.mkfifo(p, 0o600)
        except BaseException:
            self._unlink_paths(in_path, out_path)
            raise
        in_rdwr = in_write = out_rdwr = out_read = None
        try:
            in_rdwr = os.open(in_path, os.O_RDWR)
            in_write = os.open(in_path, os.O_WRONLY)
            out_rdwr = os.open(out_path, os.O_RDWR)
            out_read = os.open(out_path, os.O_RDONLY)
            self.proc = subprocess.Popen(
                pi_cmd, cwd=self.cwd, stdin=in_rdwr, stdout=out_rdwr, stderr=subprocess.PIPE, bufsize=0,
            )
        except BaseException:
            for fd in (in_rdwr, in_write, out_rdwr, out_read):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self._unlink_paths(in_path, out_path)
            raise
        os.close(in_rdwr)
        os.close(out_rdwr)
        self.channel = {"in": in_path, "out": out_path}
        self.pid = self.proc.pid
        self._in_fd = in_write
        self._out_fd = out_read
        self._start_threads()

    def _attach_fifo(self, in_path: str, out_path: str) -> None:
        # O_NONBLOCK makes the open fail with ENXIO instead of hanging forever when no Pi holds the
        # read end; the descriptor is made blocking again before any command is written.
        in_fd = os.open(in_path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            flags = fcntl.fcntl(in_fd, fcntl.F_GETFL)
            fcntl.fcntl(in_fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            out_fd = os.open(out_path, os.O_RDONLY)
        except BaseException:
            os.close(in_fd)
            raise
        self.channel = {"in": in_path, "out": out_path}
        self._in_fd, self._out_fd = in_fd, out_fd

    def _start_threads(self) -> None:
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        if self.proc is not None and self.proc.stderr is not None:
            self._err_reader = threading.Thread(target=self._read_stderr, daemon=True)
            self._err_reader.start()

    def _probe(self, timeout: float = REATTACH_TIMEOUT) -> dict:
        """One get_state round trip on the reopened FIFOs, tolerating events already buffered in them."""
        cid = CALL_PREFIX + uuid.uuid4().hex
        self._write({"type": "get_state", "id": cid})
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise RpcError("no reply to reattach probe within %ss" % timeout)
            r, _, _ = select.select([self._out_fd], [], [], left)
            if not r:
                raise RpcError("no reply to reattach probe within %ss" % timeout)
            chunk = os.read(self._out_fd, READ_CHUNK)      # select said readable, so this does not block
            if not chunk:
                raise RpcError("rpc channel closed")
            self._rbuf += chunk
            while True:
                nl = self._rbuf.find(b"\n")
                if nl < 0:
                    break
                raw = bytes(self._rbuf[:nl])
                del self._rbuf[:nl + 1]
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "response" and msg.get("id") == cid:
                    return msg

    # ---- reader threads -------------------------------------------------
    def _read_stdout(self) -> None:
        while True:
            try:
                chunk = os.read(self._out_fd, READ_CHUNK)
            except OSError:
                break
            if not chunk:
                break
            self._rbuf += chunk
            while True:
                nl = self._rbuf.find(b"\n")
                if nl < 0:
                    break
                raw = bytes(self._rbuf[:nl])
                del self._rbuf[:nl + 1]
                self._handle_line(raw)
            if len(self._rbuf) > MAX_LINE:
                self.stderr.append("stdout line over %d bytes; discarded" % MAX_LINE)
                self._rbuf.clear()
        code = self.proc.wait() if self.proc is not None else -1
        self.exited = code
        self.publish({"type": "bridge_session_exit", "code": code})
        with self._pending_cv:
            self._pending_cv.notify_all()
        if self.on_exit is not None:
            try:
                self.on_exit(self)
            except Exception as e:                         # an exit notification must not kill the reader
                self.stderr.append("could not save session record: %s" % e)
        self._unlink_channel()      # the process that owned the channel is gone; nothing can reattach to it

    def _handle_line(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            self.stderr.append("non-JSON on stdout: " + raw[:200].decode("utf-8", "replace"))
            return
        if not isinstance(msg, dict):
            self.stderr.append("non-object on stdout: " + raw[:200].decode("utf-8", "replace"))
            return
        if msg.get("type") == "response" and str(msg.get("id", "")).startswith(CALL_PREFIX):
            with self._pending_cv:
                if msg["id"] in self._pending:      # a late reply to a call that already timed out is dropped
                    self._pending[msg["id"]] = msg
                    self._pending_cv.notify_all()
            return
        self.publish(msg)

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
            raise RpcError(self.dead_reason or "session process has exited (code %s)" % self.exited)
        line = (json.dumps(cmd, separators=(",", ":")) + "\n").encode("utf-8")
        with self._wlock:
            if self._in_fd is None:
                raise RpcError("session has no rpc channel")
            view = memoryview(line)
            try:
                while view:
                    n = os.write(self._in_fd, view)
                    if n <= 0:
                        raise OSError("short write to pi")
                    view = view[n:]
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

    # ---- shutdown and records -------------------------------------------
    def stop(self) -> None:
        if self.exited is None:
            if self._pipe and self.proc is not None and self.proc.stdin is not None:
                try:
                    self.proc.stdin.close()      # Pi's documented orderly shutdown, anonymous pipes only
                except (OSError, ValueError):
                    pass
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
            else:
                # A FIFO child holds a writer on its own stdin, so closing our end cannot mean EOF;
                # signal it the way systemd would.
                self._terminate()
        if self._reader is not None:
            self._reader.join(2.0)
        if self._err_reader is not None:
            self._err_reader.join(2.0)
        self._close_channel()
        self._unlink_channel()

    def _terminate(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
        elif self.pid and _pid_alive(self.pid):
            try:
                os.kill(self.pid, signal.SIGTERM)
            except OSError:
                return
            deadline = time.time() + 5
            while time.time() < deadline:
                if not _pid_alive(self.pid):
                    return
                time.sleep(0.05)
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass

    def _close_channel(self) -> None:
        if self._pipe:
            if self.proc is not None:
                for f in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                    try:
                        f.close()
                    except (OSError, ValueError, AttributeError):
                        pass
            self._in_fd = self._out_fd = None
        else:
            for name in ("_in_fd", "_out_fd"):
                fd = getattr(self, name)
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    setattr(self, name, None)

    def _unlink_channel(self) -> None:
        ch = self.channel
        if ch:
            self._unlink_paths(ch.get("in"), ch.get("out"))
            self.channel = None

    @staticmethod
    def _unlink_paths(*paths) -> None:
        for p in paths:
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def info(self) -> dict:
        alive = self.exited is None
        state = "refused" if self.refused_reason else ("alive" if alive else "dead")
        out = {"id": self.id, "name": self.name, "cwd": self.cwd, "created": self.created,
               "alive": alive, "exit_code": None if self.exited in (None, -1) else self.exited,
               "state": state, "pid": self.pid, "last_seq": self.last_seq()}
        if self.refused_reason:
            out["reason"] = self.refused_reason
        return out

    def record(self) -> dict:
        alive = self.exited is None
        rec = {"id": self.id, "cwd": self.cwd, "name": self.name, "pid": self.pid,
               "created_at": self.created, "state_dir": self.state_dir,
               "state": "refused" if self.refused_reason else ("alive" if alive else "dead"),
               "exit_code": self.exited if self.exited not in (None, -1) else None}
        if self.refused_reason:
            rec["reason"] = self.refused_reason
        if self.channel and (alive or self.refused_reason):
            rec["channel"] = dict(self.channel)
        return rec


class Registry:
    def __init__(self, pi_cmd: list[str], state_dir: str | None = None, cwd_roots=()):
        self.pi_cmd = pi_cmd
        self.state_dir = os.path.abspath(state_dir) if state_dir else None
        self.cwd_roots = tuple(os.path.realpath(r) for r in cwd_roots)
        self.store = SessionStore(self.state_dir) if state_dir else None
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        if self.store:
            self._restore()

    def _restore(self) -> None:
        for rec in self.store.all():
            s = Session.from_record(self.pi_cmd, rec, self.state_dir, self.cwd_roots)
            s.state_dir = self.state_dir
            s.on_exit = self._persist
            with self.lock:
                self.sessions[s.id] = s
            # Rewrites a stale "alive" record as dead, and clears a channel that was already unusable.
            self._persist(s)

    def _channel_paths(self, sid: str) -> tuple[str, str] | None:
        if not (self.store and FIFO_CHANNEL):
            return None
        d = os.path.join(self.state_dir, "sessions")
        return os.path.join(d, sid + ".in"), os.path.join(d, sid + ".out")

    def _persist(self, s: Session) -> None:
        # The registry lock makes "check removed, then write" atomic against remove(), which sets removed
        # under the same lock before deleting the record. Without it an exit notification already in
        # flight could write the record back after DELETE.
        if not self.store:
            return
        with self.lock:
            if s.removed:
                return
            try:
                self.store.put(s.record())
            except OSError as e:
                s.stderr.append("could not save session record: %s" % e)

    def create(self, cwd: str, name: str | None = None) -> Session:
        sid = uuid.uuid4().hex[:12]
        s = Session(self.pi_cmd, cwd, name, sid=sid, channel=self._channel_paths(sid), state_dir=self.state_dir)
        s.on_exit = self._persist
        with self.lock:
            self.sessions[s.id] = s
        self._persist(s)
        return s

    def get(self, sid: str) -> Session | None:
        with self.lock:
            return self.sessions.get(sid)

    def remove(self, sid: str) -> bool:
        with self.lock:
            s = self.sessions.pop(sid, None)
            if s is not None:
                s.removed = True
                s.on_exit = None
        if not s:
            return False
        if self.store:
            self.store.delete(sid)
        s.stop()
        return True

    def list(self) -> list[dict]:
        with self.lock:
            return [s.info() for s in self.sessions.values()]

    def stop_all(self) -> None:
        """Stop every Pi but keep the records: on the next start they are listed as dead."""
        with self.lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            s.stop()
