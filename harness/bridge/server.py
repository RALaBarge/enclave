"""Pi bridge: local HTTP/SSE API over Pi's RPC mode, plus optional tailnet peer messaging.

The local API listens on loopback only and has no login. A web page elsewhere cannot drive it: Host must be loopback
(DNS rebinding) and any Origin must be the API's own.

    GET    /health
    GET    /ui/                           the web UI (static files from --ui-dir; see ../ui)
    GET    /sessions                      POST /sessions {cwd, name?}          DELETE /sessions/{id}
           (with --gate-socket, creating a session first has sbx-session-gate force the console's `session`
            firewall profile; if that fails the session is NOT started. The bridge cannot loosen it.)
    POST   /sessions/{id}/command         any Pi RPC command object; returns Pi's response record
    POST   /sessions/{id}/ui-response     fire-and-forget (extension_ui_response)
    GET    /sessions/{id}/events?since=N  SSE, resumable via Last-Event-ID
    GET    /sessions/{id}/stderr
    POST   /sessions/{id}/files?path=rel  raw body (Content-Length required, 8 MiB cap) -> {path, bytes}
    GET    /sessions/{id}/files?path=rel  bytes of a file under the session's cwd (8 MiB cap)
  with --peers:
    GET    /peers                         tailnet nodes, which run a bridge, which are paired
    POST   /peers/{node}/send             {text, to_session?, from_session?}   (pairs on first contact)
    GET    /inbox[?state=pending]         POST /inbox/{id}/deliver {session}    POST /inbox/{id}/dismiss
    GET    /pairings                      DELETE /pairings/{instance}  (forget a peer)
  with --loom-url URL:
    every settled turn is POSTed to Loom's /log (best effort, no retries; Loom being down is logged once
    per session and never stops the session or its SSE stream)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for _redact_dir in (os.path.join(HERE, "..", "redact"), os.path.join(HERE, "redact")):
    sys.path.insert(0, _redact_dir)
try:
    import output_redactor as REDACT  # noqa: E402
except ImportError:                 # a bare /opt/pi-bridge install ships only bridge/*.py
    REDACT = None
import pairing as PR  # noqa: E402
import peers as P  # noqa: E402
from pibridge import Registry, RpcError  # noqa: E402

MAX_BODY = 1024 * 1024
MAX_FILE = 8 * 1024 * 1024   # one upload/download; a larger file is refused, not streamed through
READ_CHUNK = 64 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
LOOM_TEXT = 120_000          # one turn's transcript; a runaway tool loop is cut, not sent whole
LOOM_USER = 16_000
LOOM_LINE = 600              # an unknown event or tool argument summary
LOOM_ARGS = 500
# OutputRedactor is disabled unless its config says otherwise, and its PII sweep is left off here: Loom
# memory needs the names and addresses that appear in the work, while the secret patterns and the
# entropy sweep are what must not cross into storage. redact() is the whole API we need.
# It is optional at import only because image/provision.sh installs bridge/*.py without harness/redact;
# with --loom-url and no redactor the bridge still runs but sends no turns (fail closed, warned at start).
REDACTOR = REDACT.OutputRedactor({"security": {"output_redaction": {"enabled": True, "redact_pii": False}}}) if REDACT else None


class Forbidden(Exception):
    """A path or origin the bridge refuses to touch."""


class LengthRequired(Exception):
    """An upload without Content-Length, which this API requires so the size is bounded before reading."""


class TooLarge(Exception):
    """A file over MAX_FILE, in either direction."""


# Directories a session may not use as its cwd. The listener is loopback-only
# and unauthenticated, so any local process may create a session; the file
# endpoints serve whatever the session's cwd contains, as the bridge's own
# account. Without this, `{"cwd": "/run/sbx"}` plus
# `/sessions/<id>/files?path=secrets.env` reads the model keys, and
# `/etc/sbxfw` reads the console tokens - a confused-deputy read of exactly
# the files docs/HARNESS-ADMISSION.md says the account boundary protects.
# It is a deny-list, so it narrows the obvious path rather than proving there
# is none: a file the pibridge account can read elsewhere is still readable by
# a local caller who names its directory.
PROTECTED_ROOTS = (
    "/run/sbx", "/run/sbx-gate", "/etc/sbxfw", "/etc/sbx", "/etc/ssh",
    "/etc/shadow", "/etc/sudoers", "/root", "/var/lib/pi-bridge",
    "/var/lib/deskbox", "/var/lib/loom", "/proc", "/sys", "/dev", "/boot",
)


def protected_root(path: str) -> bool:
    p = os.path.normcase(os.path.realpath(path))
    for root in PROTECTED_ROOTS:
        r = os.path.normcase(os.path.realpath(root))
        if p == r or p.startswith(r + os.sep):
            return True
    return False


def session_path(cwd: str, rel: str) -> tuple[str, str]:
    """(absolute resolved path, normalised relative path) for a file under a session's cwd.

    Absolute paths and any `..` component are refused before joining. The joined path is then resolved
    with realpath() and checked again: that is what stops a symlink inside the cwd (as a directory or as
    the file itself) from reaching outside it. realpath() also normalises the final path used for open(),
    so the check and the use agree. A race remains between the check and open().
    """
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("path is required")
    if "\x00" in rel:
        raise ValueError("path contains a NUL byte")
    norm = rel.replace("\\", "/")           # accept Windows-style separators from a browser
    if norm.startswith("/") or os.path.isabs(rel) or os.path.splitdrive(norm)[0] or re.match(r"^[A-Za-z]:", norm):
        raise Forbidden("absolute paths are not allowed")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError("path is required")
    if ".." in parts:
        raise Forbidden("'..' is not allowed in a path")
    root = os.path.realpath(cwd)
    target = os.path.realpath(os.path.join(root, *parts))
    # Both ends: a refused cwd is not enough, because a cwd one level above a
    # secret directory (`/run` with `sbx/secrets.env`, `/` with
    # `etc/sbxfw/token`) resolves inside it. The target is what gets opened.
    if protected_root(root) or protected_root(target):
        raise Forbidden("that directory holds the core's secrets and cannot host a session")
    try:
        inside = os.path.commonpath([root, target])
    except ValueError:                      # different drives on Windows
        inside = None
    if inside is None or os.path.normcase(inside) != os.path.normcase(root):
        raise Forbidden("path leaves the session directory")
    return target, "/".join(parts)


def host_only(hostport: str) -> str:
    """Host part of a Host header or listen spec: handles "[::1]:9760", "::1", "127.0.0.1:9760", "localhost"."""
    h = hostport.strip()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h
SID = r"([0-9a-f]{12})"
UI_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".json": "application/json"}
INLINE = re.compile(r"<(script|style)[^>]*>(.*?)</\1>", re.S)


class FirewallError(Exception):
    pass


class _UnixConnection(HTTPConnection):
    """http.client over a unix socket: the session gate has no address."""

    def __init__(self, path, timeout=10.0):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def gate_lockdown(socket_path, timeout=10.0):
    """Ask sbx-session-gate to force the `session` profile before a session starts.

    The gate, not this bridge, holds the tighten-only token: one process owns
    the guarantee, so a second harness in the core calls the same code instead
    of growing its own copy of it. Unreachable gate is a refusal, never a
    session that quietly starts on whatever profile happens to be active.
    """
    conn = _UnixConnection(socket_path, timeout=timeout)
    try:
        conn.request("POST", "/lockdown", body=b"{}", headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        raw = r.read()
    except OSError as e:
        raise FirewallError("session gate unreachable at %s (%s)" % (socket_path, type(e).__name__))
    finally:
        conn.close()
    try:
        doc = json.loads(raw or b"{}")
    except ValueError:
        raise FirewallError("session gate answered with something that is not JSON")
    if r.status != 200:
        raise FirewallError("session gate answered %d%s" % (r.status, ": " + doc["error"] if doc.get("error") else ""))
    return doc


def _clip(value, limit):
    s = value if isinstance(value, str) else str(value)
    if len(s) <= limit:
        return s
    return s[:limit] + "…[%d chars]" % (len(s) - limit)


def _text_of(message):
    """Text from a Pi message, following the same defensive shapes as ui/modules/chat.js."""
    if not isinstance(message, dict):
        return ""
    if isinstance(message.get("text"), str):
        return message["text"]
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b if isinstance(b, str) else (b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else "")
                       for b in content)
    return ""


def _text_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _text_of({"content": value})
    return _text_of(value) if isinstance(value, dict) else ""


def _bounded_json(value, limit=LOOM_LINE):
    """One compact line of JSON, cut at `limit`. JSON escapes newlines, so the result never breaks a line."""
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = " ".join(repr(value).split())
    return _clip(raw, limit)


class TurnRecorder:
    """One session's memory of what Pi did, POSTed to Loom when the turn settles.

    It reads Session.events_since(), the same event ring the SSE relay serves. Consuming the ring rather
    than hooking Pi's reader means the recorder and the SSE relay see the same ordered events, and a slow
    POST to Loom cannot stall the reader that both depend on.

    Loom's /log keeps exactly two texts: the last user message in `request.messages` and the first
    assistant `content` in `response.choices`. Everything else this turn produced is therefore rendered
    into that one assistant content: assistant text first, then tool lines, then unknown-event lines.
    """

    def __init__(self, session, loom_url, timeout=5.0):
        self.session = session
        self.loom_url = loom_url.rstrip("/")
        self.timeout = timeout
        self.lock = threading.Lock()
        self.user = ""
        self.open = False
        self.index = 0
        self.current = ""
        self.parts = []
        self.tool_lines = []
        self.unknown_lines = []
        self.failures = 0
        self.error_logged = False
        self._since = session.last_seq()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="loom-%s" % self.session.id, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        since = self._since
        while not self._stop.is_set():
            events = self.session.events_since(since, wait=5.0)
            for seq, event in events:
                try:
                    self.feed(event)
                except Exception as e:      # one malformed event must not stop the rest of the turn
                    self._stderr("could not record a Pi event for session %s: %s" % (self.session.id, e))
                since = seq
            if self.session.exited is not None and not events:
                return

    def _stderr(self, line):
        print("pi-bridge: " + line, file=sys.stderr, flush=True)

    def note_user(self, text):
        """Remember the last user message seen for the session; this is what Loom stores as the prompt."""
        value = _text_value(text)
        if value:
            with self.lock:
                self.user = _clip(value, LOOM_USER)

    def feed(self, event):
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "agent_start":
            with self.lock:
                if not self.open:           # a repeated start is not a second turn
                    self.open = True
                    self.current, self.parts = "", []
                    self.tool_lines, self.unknown_lines = [], []
            return
        if kind in ("agent_settled", "agent_end"):
            # agent_end is the other end marker chat.js understands; treating it as a fallback close
            # means a Pi that sends only agent_end still produces one memory per turn.
            self._settle()
            return
        if kind == "message_update":
            self._message_update(event)
            return
        if kind == "tool_execution_start":
            self._tool_event(event, start=True)
            return
        if kind == "tool_execution_end":
            self._tool_event(event, start=False)
            return
        with self.lock:
            if not self.open:
                return
            # A Pi event type this bridge does not know yet is recorded rather than dropped.
            self.unknown_lines.append("unknown event %s: %s" % (kind or "?", _bounded_json(event)))

    def _flush_locked(self):
        if self.current:
            self.parts.append(self.current)
            self.current = ""

    def _message_update(self, event):
        ame = event.get("assistantMessageEvent")
        delta = ame.get("delta") if isinstance(ame, dict) else None
        message = event.get("message")
        role = message.get("role") if isinstance(message, dict) else event.get("role")
        text = _text_of(message) or _text_of(event)
        if role == "user":                  # some Pi versions replay the prompt as a message event
            if text:
                self.note_user(text)
            return
        if not isinstance(delta, str):
            delta = None
        with self.lock:
            if not self.open:
                return
            if delta is not None:
                self.current += delta
            elif text:
                # No delta means Pi is repeating the whole message. A growing prefix is the same
                # message updated in place; anything else starts a new one (chat.js replaces its
                # live bubble on this shape too).
                if self.current and text.startswith(self.current):
                    self.current = text
                elif self.current and self.current.endswith(text):
                    pass
                else:
                    self._flush_locked()
                    self.current = text

    def _tool_event(self, event, start):
        name = event.get("toolName") or event.get("name") or event.get("tool") or "?"
        if start:
            args = event.get("args", event.get("arguments", event.get("input")))
            summary = _text_value(args) if isinstance(args, str) else (_bounded_json(args, LOOM_ARGS)
                                                                      if args not in (None, {}, []) else "")
            line = "tool: %s%s" % (name, " " + _clip(" ".join(summary.split()), LOOM_ARGS) if summary else "")
        else:
            result = event.get("result", event.get("output", event.get("content", event.get("message"))))
            summary = _text_value(result) if isinstance(result, str) else (_bounded_json(result, LOOM_ARGS)
                                                                           if result not in (None, {}, []) else "")
            failed = bool(event.get("isError") or event.get("error"))
            line = "tool result: %s%s%s" % (name, " error" if failed else "",
                                            ": " + _clip(" ".join(summary.split()), LOOM_ARGS) if summary else "")
        with self.lock:
            if not self.open:
                return
            self._flush_locked()
            self.tool_lines.append(line)

    def _redact(self, text):
        if REDACTOR is None:            # App warned once at start; no uninspected turn is sent
            return None
        try:
            return REDACTOR.redact(text).text
        except Exception as e:
            # Failing closed is deliberate: a turn with a secret the redactor could not inspect is
            # better left out of memory than stored raw.
            self._stderr("redactor failed, turn not sent to Loom: %s" % e)
            return None

    def _settle(self):
        with self.lock:
            if not self.open:
                return
            self.open = False
            self.index += 1
            self._flush_locked()
            assistant = "\n\n".join(p for p in self.parts if p)
            lines = [assistant] if assistant else []
            lines.extend(self.tool_lines)
            lines.extend(self.unknown_lines)
            transcript = _clip("\n".join(lines), LOOM_TEXT)
            user, index = self.user, self.index
            self.current, self.parts = "", []
            self.tool_lines, self.unknown_lines = [], []
        if not transcript and not user:
            return                          # nothing happened; an empty turn is not a memory
        user, transcript = self._redact(user), self._redact(transcript)
        if user is None or transcript is None:
            return
        payload = {
            "request": {"messages": [{"role": "user", "content": user}] if user else []},
            "response": {"choices": [{"message": {"role": "assistant", "content": transcript}}]},
            "metadata": {
                "conversation_id": self.session.id,
                "provider": "pi",
                "session_name": self.session.name,
                "cwd": self.session.cwd,
                "turn_index": index,
            },
        }
        self._post(payload)

    def _post(self, payload):
        """Best effort, no retry: one attempt per turn, and a failure says so once per session."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.loom_url + "/log", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read(4096)         # drain the small reply so the connection closes cleanly
        except Exception as e:
            self.failures += 1
            if not self.error_logged:
                self.error_logged = True
                self._stderr("could not log a turn to Loom at %s for session %s (%s); further failures for "
                             "this session are silent" % (self.loom_url, self.session.id, e))


class App:
    def __init__(self, pi_cmd, peer_port=None, state_dir=None, name="bridge", gate=None,
                 loom_url=None, loom_timeout=5.0):
        self.gate = gate          # unix socket of sbx-session-gate, or None
        self.ui_dir = None                # directory served at /ui/
        self.state_dir = state_dir or tempfile.mkdtemp(prefix="pi-bridge-")
        self.reg = Registry(pi_cmd, self.state_dir)
        self.peer_port = peer_port
        self.me = PR.load_identity(self.state_dir, name)
        self.store = PR.PeerStore(self.state_dir)
        self.inbox = P.Inbox()
        self.nonces = PR.NonceCache()
        self.loom_url = loom_url.rstrip("/") if loom_url else None
        self.loom_timeout = loom_timeout
        self.recorders = {}
        self._rec_lock = threading.Lock()
        if self.loom_url and REDACTOR is None:
            print("pi-bridge: --loom-url is set but the secret redactor is not installed next to the bridge; "
                  "no turns will be sent (install harness/redact/output_redactor.py beside server.py)",
                  file=sys.stderr, flush=True)
        if self.loom_url:
            for info in self.reg.list():    # sessions restored from sessions.json still get a recorder
                s = self.reg.get(info["id"])
                if s is not None:
                    self.observe(s)

    def observe(self, session):
        """The session's turn recorder, started on first use; None when --loom-url is unset."""
        if not self.loom_url:
            return None
        with self._rec_lock:
            rec = self.recorders.get(session.id)
            if rec is None:
                rec = TurnRecorder(session, self.loom_url, self.loom_timeout)
                self.recorders[session.id] = rec
                rec.start()
            return rec

    def note_user(self, session, text):
        rec = self.observe(session)
        if rec is not None:
            rec.note_user(text)

    def forget(self, sid):
        with self._rec_lock:
            rec = self.recorders.pop(sid, None)
        if rec is not None:
            rec.stop()

    def deliver(self, item, sid):
        s = self.reg.get(sid)
        if not s:
            raise KeyError("no such session")
        text = P.envelope(item)
        self.note_user(s, text)
        r = s.call({"type": "prompt", "message": text, "streamingBehavior": "followUp"})
        if r.get("success"):
            item["state"], item["delivered_to"] = "delivered", sid
        return r


def ui_file(ui_dir, rel):
    """(path, content type) for a file under ui_dir, or None. Only .html/.js/.json, never outside the directory or in test/."""
    root = os.path.realpath(ui_dir)
    path = os.path.realpath(os.path.join(root, rel or "index.html"))
    ctype = UI_TYPES.get(os.path.splitext(path)[1])
    if not ctype or os.path.commonpath([root, path]) != root or not os.path.isfile(path) or os.path.relpath(path, root).startswith("test"):
        return None
    return path, ctype


def ui_csp(html):
    """script-src and style-src pin the page's own inline blocks by hash; everything else must come from this origin."""
    def pins(tag):
        return " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(b.replace("\r\n", "\n").encode()).digest()).decode()
                        for t, b in INLINE.findall(html) if t == tag)
    return ("default-src 'none'; script-src 'self' %s; style-src 'self' %s; connect-src 'self'; worker-src 'self'; base-uri 'none'"
            % (pins("script"), pins("style")))


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "pi-bridge"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _guard(self) -> bool:
            host = self.headers.get("Host", "")
            if host_only(host) not in LOOPBACK:
                self._json(403, {"error": "bad Host header"})
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != host:
                self._json(403, {"error": "cross-origin request refused"})
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _route(self, method):
            if not self._guard():
                return
            u = urlparse(self.path)
            if method == "GET" and u.path.startswith("/ui"):
                return self._ui(u.path)
            try:
                self._dispatch(method, u.path.rstrip("/") or "/", parse_qs(u.query))
            except KeyError as e:
                self._json(404, {"error": str(e).strip("'")})
            except Forbidden as e:
                self._json(403, {"error": str(e)})
            except LengthRequired as e:
                self._json(411, {"error": str(e)})
            except TooLarge as e:
                self._json(413, {"error": str(e)})
            except RpcError as e:
                self._json(502, {"error": str(e)})
            except (ValueError, TypeError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})

        def _ui(self, p):
            if p == "/ui":
                self.send_response(301)
                self.send_header("Location", "/ui/")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            f = app.ui_dir and p.startswith("/ui/") and ui_file(app.ui_dir, p[4:])
            if not f:
                return self._json(404, {"error": "not found"})
            raw = open(f[0], "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", f[1])
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if f[1].startswith("text/html"):
                self.send_header("Content-Security-Policy", ui_csp(raw.decode()))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

        def _session(self, sid):
            s = app.reg.get(sid)
            if not s:
                raise KeyError("no such session")
            return s

        def _need_peers(self):
            if app.peer_port is None:
                raise ValueError("peer messaging is disabled; start the bridge with --peers")

        def _upload(self, s, rel):
            target, shown = session_path(s.cwd, rel)
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                raise LengthRequired("Content-Length is required")
            try:
                n = int(raw_len)
            except ValueError:
                raise ValueError("bad Content-Length")
            if n < 0:
                raise ValueError("bad Content-Length")
            if n > MAX_FILE:
                raise TooLarge("file is larger than %d bytes" % MAX_FILE)
            if os.path.isdir(target):
                raise ValueError("path is a directory")
            parent = os.path.dirname(target)
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                raise ValueError("cannot create directory: %s" % e)
            left = n
            try:
                with open(target, "wb") as f:
                    while left:
                        chunk = self.rfile.read(min(READ_CHUNK, left))
                        if not chunk:
                            raise ValueError("request body ended early")
                        f.write(chunk)
                        left -= len(chunk)
            except OSError as e:
                raise ValueError("cannot write %s: %s" % (shown, e))
            return self._json(201, {"path": shown, "bytes": n})

        def _download(self, s, rel):
            target, _ = session_path(s.cwd, rel)
            if os.path.isdir(target):
                raise ValueError("path is a directory")
            if not os.path.isfile(target):
                raise KeyError("no such file")
            try:
                if os.path.getsize(target) > MAX_FILE:
                    raise TooLarge("file is larger than %d bytes" % MAX_FILE)
                with open(target, "rb") as f:
                    raw = f.read(MAX_FILE + 1)
            except OSError as e:
                raise ValueError("cannot read file: %s" % e)
            if len(raw) > MAX_FILE:            # the file grew between the size check and the read
                raise TooLarge("file is larger than %d bytes" % MAX_FILE)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _dispatch(self, m, p, q):
            if p == "/health" and m == "GET":
                return self._json(200, {"ok": True, "app": P.APP, "version": P.VERSION, "instance": app.me["instance"],
                                        "peers": app.peer_port is not None})
            if p == "/sessions":
                if m == "GET":
                    return self._json(200, app.reg.list())
                if m == "POST":
                    b = self._body()
                    if not b.get("cwd") or not os.path.isdir(b["cwd"]):
                        raise ValueError("cwd must be an existing directory")
                    if protected_root(b["cwd"]):
                        return self._json(403, {"error": "that directory holds the core's secrets and cannot host a session"})
                    fw = None
                    if app.gate:
                        try:
                            fw = gate_lockdown(app.gate)
                        except FirewallError as e:
                            return self._json(503, {"error": "firewall lockdown failed, session not started: %s" % e})
                    info = app.reg.create(b["cwd"], b.get("name")).info()
                    s = app.reg.get(info["id"])
                    if s is not None:
                        app.observe(s)
                    if fw is not None:
                        info["firewall"] = fw
                    return self._json(201, info)
            mm = re.fullmatch(r"/sessions/" + SID, p)
            if mm and m == "DELETE":
                app.forget(mm.group(1))
                return self._json(200, {"removed": app.reg.remove(mm.group(1))})
            mm = re.fullmatch(r"/sessions/" + SID + r"/files", p)
            if mm:
                s = self._session(mm.group(1))
                rel = (q.get("path") or [""])[0]
                if m == "POST":
                    try:
                        return self._upload(s, rel)
                    except (Forbidden, LengthRequired, TooLarge, ValueError):
                        # The body was not consumed, so this connection cannot be reused for another request.
                        self.close_connection = True
                        raise
                if m == "GET":
                    return self._download(s, rel)
            mm = re.fullmatch(r"/sessions/" + SID + r"/(command|ui-response|events|stderr)", p)
            if mm:
                s, what = self._session(mm.group(1)), mm.group(2)
                if what == "command" and m == "POST":
                    cmd = self._body()
                    if not isinstance(cmd.get("type"), str):
                        raise ValueError("command needs a string 'type'")
                    if cmd["type"] in ("prompt", "steer"):
                        app.note_user(s, cmd.get("message"))
                    return self._json(200, s.call(cmd, timeout=float(q.get("timeout", ["30"])[0])))
                if what == "ui-response" and m == "POST":
                    s.fire(self._body())
                    return self._json(202, {"ok": True})
                if what == "stderr" and m == "GET":
                    return self._json(200, list(s.stderr))
                if what == "events" and m == "GET":
                    return self._sse(s, q)
            if p == "/peers" and m == "GET":
                self._need_peers()
                return self._json(200, P.discover(app.peer_port, app.store))
            mm = re.fullmatch(r"/peers/([A-Za-z0-9._-]+)/send", p)
            if mm and m == "POST":
                self._need_peers()
                b = self._body()
                if not isinstance(b.get("text"), str) or not b["text"].strip():
                    raise ValueError("need non-empty 'text'")
                peer = next((x for x in P.ts_status()["peers"] if x["name"] == mm.group(1) and x["online"] and x["ips"]), None)
                if not peer:
                    raise KeyError("no online peer named %s" % mm.group(1))
                return self._json(200, P.send(app.me, app.store, peer["ips"][0], app.peer_port, b["text"],
                                              b.get("to_session"), b.get("from_session")))
            if p == "/pairings" and m == "GET":
                return self._json(200, app.store.list())
            mm = re.fullmatch(r"/pairings/([0-9a-f]{8,64})", p)
            if mm and m == "DELETE":
                return self._json(200, {"removed": app.store.delete(mm.group(1))})
            if p == "/inbox" and m == "GET":
                return self._json(200, app.inbox.list((q.get("state") or [None])[0]))
            mm = re.fullmatch(r"/inbox/([0-9a-f]{12})/(deliver|dismiss)", p)
            if mm and m == "POST":
                item = app.inbox.get(mm.group(1))
                if not item:
                    raise KeyError("no such inbox item")
                if mm.group(2) == "dismiss":
                    item["state"] = "dismissed"
                    return self._json(200, item)
                sid = self._body().get("session") or item.get("to_session")
                if not sid:
                    raise ValueError("need 'session' to deliver to")
                r = app.deliver(item, sid)
                return self._json(200 if r.get("success") else 502, {"item": item, "pi": r})
            self._json(404, {"error": "not found"})

        def _sse(self, s, q):
            since = int((q.get("since") or [self.headers.get("Last-Event-ID") or "0"])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    evs = s.events_since(since, wait=15.0)
                    if not evs:
                        self.wfile.write(b": keepalive\n\n")
                    for seq, msg in evs:
                        self.wfile.write(("id: %d\ndata: %s\n\n" % (seq, json.dumps(msg))).encode())
                        since = seq
                    self.wfile.flush()
                    if s.exited is not None and not evs:
                        return
            except OSError:
                return

    return H


def loom_url_arg(value):
    """--loom-url is an http(s) origin, not a file or an arbitrary scheme urllib would also accept."""
    u = urlparse(value)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise argparse.ArgumentTypeError("need an http(s) URL, for example http://127.0.0.1:9741")
    return value.rstrip("/")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="127.0.0.1:9760")
    ap.add_argument("--pi-bin", default=os.environ.get("PI_BIN", "pi"))
    ap.add_argument("--pi-arg", action="append", default=[], help="extra argument for pi (repeatable)")
    ap.add_argument("--state-dir", default="~/.pi-bridge", help="identity, paired-peer keys and session records")
    ap.add_argument("--gate-socket", default=None,
                    help="sbx-session-gate socket; each new session first forces the session firewall profile, "
                         "and a session is not started if that fails")
    ap.add_argument("--ui-dir", default=None, help="web UI files (default: ./ui or ../ui next to this file)")
    ap.add_argument("--peers", action="store_true", help="enable tailnet peer messaging")
    ap.add_argument("--peer-port", type=int, default=P.DEFAULT_PEER_PORT)
    ap.add_argument("--loom-url", type=loom_url_arg, default=None,
                    help="POST each settled turn to this Loom, e.g. http://127.0.0.1:9741 (default: off)")
    ap.add_argument("--loom-timeout", type=float, default=5.0,
                    help="seconds to wait for Loom before giving up on a turn (default: 5)")
    args = ap.parse_args(argv)
    if args.loom_timeout <= 0:
        print("--loom-timeout must be greater than zero", file=sys.stderr)
        return 2

    host, _, port = args.listen.rpartition(":")
    host = host_only(host) if host.startswith("[") else host
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("refusing to bind the local API to %s: loopback only (use an SSH tunnel)" % host, file=sys.stderr)
        return 2
    st = None
    if args.peers:
        try:
            st = P.ts_status()
        except Exception as e:
            print("--peers needs a running Tailscale: %s" % e, file=sys.stderr)
            return 2
        if st["state"] != "Running":
            print("tailscale is not running (state=%s)" % st["state"], file=sys.stderr)
            return 2
    name = st["self"]["name"] if st else socket.gethostname().split(".")[0]
    app = App([args.pi_bin, "--mode", "rpc", *args.pi_arg], args.peer_port if args.peers else None,
              os.path.abspath(os.path.expanduser(args.state_dir)), name,
              args.gate_socket, args.loom_url, args.loom_timeout)
    here = os.path.dirname(os.path.abspath(__file__))
    app.ui_dir = args.ui_dir or next((d for d in (os.path.join(here, "ui"), os.path.join(here, "..", "ui")) if os.path.isdir(d)), None)
    servers = [ThreadingHTTPServer((host, int(port)), make_handler(app))]
    if args.peers:
        ip = next(i for i in st["self"]["ips"] if "." in i)
        servers.append(P.make_peer_server(ip, args.peer_port, app.me, app.store, app.inbox, app.nonces,
                                          P.OwnLoginOnly(st["self"]["login"]),
                                          None))
        print("peer listener on %s:%d as %s (instance %s)" % (ip, args.peer_port, name, app.me["instance"]))
    for sv in servers:
        sv.daemon_threads = True
        threading.Thread(target=sv.serve_forever, daemon=True).start()
    print("pi-bridge api on http://%s:%s" % (host, port), flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    stop.wait()
    for sv in servers:
        sv.shutdown()
    app.reg.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
