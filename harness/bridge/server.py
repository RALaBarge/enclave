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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pairing as PR  # noqa: E402
import peers as P  # noqa: E402
from pibridge import Registry, RpcError  # noqa: E402

MAX_BODY = 1024 * 1024
MAX_FILE = 8 * 1024 * 1024   # one upload/download; a larger file is refused, not streamed through
READ_CHUNK = 64 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class Forbidden(Exception):
    """A path or origin the bridge refuses to touch."""


class LengthRequired(Exception):
    """An upload without Content-Length, which this API requires so the size is bounded before reading."""


class TooLarge(Exception):
    """A file over MAX_FILE, in either direction."""


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


class App:
    def __init__(self, pi_cmd, peer_port=None, state_dir=None, name="bridge", gate=None):
        self.gate = gate          # unix socket of sbx-session-gate, or None
        self.ui_dir = None                # directory served at /ui/
        self.state_dir = state_dir or tempfile.mkdtemp(prefix="pi-bridge-")
        self.reg = Registry(pi_cmd, self.state_dir)
        self.peer_port = peer_port
        self.me = PR.load_identity(self.state_dir, name)
        self.store = PR.PeerStore(self.state_dir)
        self.inbox = P.Inbox()
        self.nonces = PR.NonceCache()

    def deliver(self, item, sid):
        s = self.reg.get(sid)
        if not s:
            raise KeyError("no such session")
        r = s.call({"type": "prompt", "message": P.envelope(item), "streamingBehavior": "followUp"})
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
                    fw = None
                    if app.gate:
                        try:
                            fw = gate_lockdown(app.gate)
                        except FirewallError as e:
                            return self._json(503, {"error": "firewall lockdown failed, session not started: %s" % e})
                    info = app.reg.create(b["cwd"], b.get("name")).info()
                    if fw is not None:
                        info["firewall"] = fw
                    return self._json(201, info)
            mm = re.fullmatch(r"/sessions/" + SID, p)
            if mm and m == "DELETE":
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
    args = ap.parse_args(argv)

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
              args.gate_socket)
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
