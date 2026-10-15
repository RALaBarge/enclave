"""Pi bridge: local HTTP/SSE API over Pi's RPC mode, plus optional tailnet peer messaging.

Local API: 127.0.0.1 only, bearer token, Host/Origin checks (DNS-rebinding and CSRF).
Peer API:  Tailscale address only, off unless --peers; see peers.py for the trust model.

    GET    /health
    GET    /sessions                      POST /sessions {cwd, name?}
    DELETE /sessions/{id}
    POST   /sessions/{id}/command         raw Pi RPC command object; returns Pi's response record
    POST   /sessions/{id}/ui-response     fire-and-forget (extension_ui_response)
    GET    /sessions/{id}/events?since=N  SSE, resumable via Last-Event-ID
    GET    /sessions/{id}/stderr
    GET    /peers                         discovery on the active tailnet (+ other profiles, unreachable)
    POST   /peers/{node}/send             {text, to_session?, from_session?}
    GET    /inbox[?state=pending]         POST /inbox/{id}/deliver {session}   POST /inbox/{id}/dismiss
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import signal
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import peers as P  # noqa: E402
from pibridge import Registry, RpcError  # noqa: E402

MAX_BODY = 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
SID = r"([0-9a-f]{12})"


def load_token(path: str) -> str:
    if os.path.exists(path):
        return open(path).read().strip()
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


class App:
    def __init__(self, pi_cmd, token, peer_port=None, peer_auth=None):
        self.reg = Registry(pi_cmd)
        self.token = token
        self.peer_port = peer_port
        self.peer_auth = peer_auth
        self.inbox = P.Inbox()
        self.instance = uuid.uuid4().hex[:12]
        self.auto_deliver: dict[str, str] = {}       # peer node -> session id (explicit opt-in only)

    def on_peer_message(self, item):
        sid = self.auto_deliver.get(item["from_node"]) or None
        if sid and self.reg.get(sid):
            self.deliver(item, sid)

    def deliver(self, item, sid):
        s = self.reg.get(sid)
        if not s:
            raise KeyError("no such session")
        r = s.call({"type": "prompt", "message": P.envelope(item), "streamingBehavior": "followUp"})
        if r.get("success"):
            item["state"] = "delivered"
            item["delivered_to"] = sid
        return r


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "pi-bridge"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # ---- helpers ----
        def _json(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _guard(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if ":" in (self.headers.get("Host") or "") else self.headers.get("Host", "")
            if host not in LOOPBACK_HOSTS:
                self._json(403, {"error": "bad Host header"})
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in {"127.0.0.1", "localhost", "::1"}:
                self._json(403, {"error": "cross-origin request refused"})
                return False
            auth = self.headers.get("Authorization", "")
            if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), app.token.encode())):
                self._json(401, {"error": "missing or bad bearer token"})
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
            q = parse_qs(u.query)
            p = u.path.rstrip("/") or "/"
            try:
                self._dispatch(method, p, q)
            except KeyError as e:
                self._json(404, {"error": str(e).strip("'")})
            except RpcError as e:
                self._json(502, {"error": str(e)})
            except (ValueError, TypeError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:      # tailscale CLI missing, network, etc.
                self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})

        do_GET = lambda self: self._route("GET")        # noqa: E731
        do_POST = lambda self: self._route("POST")      # noqa: E731
        do_DELETE = lambda self: self._route("DELETE")  # noqa: E731

        def _session(self, sid):
            s = app.reg.get(sid)
            if not s:
                raise KeyError("no such session")
            return s

        def _dispatch(self, m, p, q):
            if p == "/health" and m == "GET":
                return self._json(200, {"ok": True, "app": P.APP, "version": P.VERSION, "instance": app.instance,
                                        "peers": app.peer_port is not None})
            if p == "/sessions":
                if m == "GET":
                    return self._json(200, app.reg.list())
                if m == "POST":
                    b = self._body()
                    cwd = b.get("cwd")
                    if not cwd or not os.path.isdir(cwd):
                        raise ValueError("cwd must be an existing directory")
                    s = app.reg.create(cwd, b.get("name"))
                    return self._json(201, s.info())
            mm = re.fullmatch(r"/sessions/" + SID, p)
            if mm and m == "DELETE":
                return self._json(200, {"removed": app.reg.remove(mm.group(1))})
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
                if app.peer_port is None:
                    raise ValueError("peer messaging is disabled; start the bridge with --peers")
                d = P.discover(app.peer_port)
                try:
                    d["profiles"] = P.ts_profiles()
                except Exception:
                    d["profiles"] = []
                return self._json(200, d)
            mm = re.fullmatch(r"/peers/([A-Za-z0-9._-]+)/send", p)
            if mm and m == "POST":
                if app.peer_port is None:
                    raise ValueError("peer messaging is disabled; start the bridge with --peers")
                b = self._body()
                text = b.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("need non-empty 'text'")
                st = P.ts_status()
                peer = next((x for x in st["peers"] if x["name"] == mm.group(1) and x["online"] and x["ips"]), None)
                if not peer:
                    raise KeyError("no online peer named %s on %s" % (mm.group(1), st["tailnet"]))
                return self._json(200, P.send(peer["ips"][0], app.peer_port, text, b.get("to_session"), b.get("from_session")))
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
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return H


def serve(args) -> int:
    token = load_token(args.token_file)
    pi_cmd = [args.pi_bin, "--mode", "rpc", *args.pi_arg]
    peer_auth = None
    if args.peers:
        st = P.ts_status()
        if st["state"] != "Running":
            print("tailscale is not running (state=%s)" % st["state"], file=sys.stderr)
            return 2
        allow = set(args.peer_allow) or {st["self"]["login"]}
        peer_auth = P.TailscaleAuth(allow)
    app = App(pi_cmd, token, args.peer_port if args.peers else None, peer_auth)
    host, _, port = args.listen.rpartition(":")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("refusing to bind the control API to %s: loopback only (use an SSH tunnel)" % host, file=sys.stderr)
        return 2
    api = ThreadingHTTPServer((host, int(port)), make_handler(app))
    api.daemon_threads = True
    servers = [api]
    if args.peers:
        ip = next(i for i in st["self"]["ips"] if "." in i)
        ps = P.make_peer_server(ip, args.peer_port, app.inbox, peer_auth, st["self"]["name"], app.instance,
                                lambda: len(app.reg.sessions), app.on_peer_message)
        ps.daemon_threads = True
        servers.append(ps)
        print("peer listener on %s:%d for %s as %s" % (ip, args.peer_port, sorted(peer_auth.allow), st["self"]["name"]))
    for sv in servers:
        threading.Thread(target=sv.serve_forever, daemon=True).start()
    print("pi-bridge api on http://%s:%s (token in %s)" % (host, port, args.token_file), flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    stop.wait()
    for sv in servers:
        sv.shutdown()
    app.reg.stop_all()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="127.0.0.1:9760")
    ap.add_argument("--token-file", default=os.path.expanduser("~/.pi-bridge-token"))
    ap.add_argument("--pi-bin", default=os.environ.get("PI_BIN", "pi"))
    ap.add_argument("--pi-arg", action="append", default=[], help="extra argument for pi (repeatable)")
    ap.add_argument("--peers", action="store_true", help="enable tailnet peer messaging (off by default)")
    ap.add_argument("--peer-port", type=int, default=P.DEFAULT_PEER_PORT)
    ap.add_argument("--peer-allow", action="append", default=[], help="allowed tailnet login (default: this node's own)")
    return serve(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
