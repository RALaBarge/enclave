"""Pi bridge: local HTTP/SSE API over Pi's RPC mode, plus optional tailnet peer messaging.

The local API listens on loopback only and has no login. A web page elsewhere cannot drive it: Host must be loopback
(DNS rebinding) and any Origin must be the API's own.

    GET    /health
    GET    /sessions                      POST /sessions {cwd, name?}          DELETE /sessions/{id}
    POST   /sessions/{id}/command         any Pi RPC command object; returns Pi's response record
    POST   /sessions/{id}/ui-response     fire-and-forget (extension_ui_response)
    GET    /sessions/{id}/events?since=N  SSE, resumable via Last-Event-ID
    GET    /sessions/{id}/stderr
  with --peers:
    GET    /peers                         tailnet nodes, which run a bridge, which are paired
    POST   /peers/{node}/send             {text, to_session?, from_session?}   (pairs on first contact)
    GET    /inbox[?state=pending]         POST /inbox/{id}/deliver {session}    POST /inbox/{id}/dismiss
    GET    /pairings                      DELETE /pairings/{instance}  (forget a peer)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pairing as PR  # noqa: E402
import peers as P  # noqa: E402
from pibridge import Registry, RpcError  # noqa: E402

MAX_BODY = 1024 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
SID = r"([0-9a-f]{12})"


class App:
    def __init__(self, pi_cmd, peer_port=None, state_dir=None, name="bridge"):
        self.reg = Registry(pi_cmd)
        self.peer_port = peer_port
        self.state_dir = state_dir or tempfile.mkdtemp(prefix="pi-bridge-")
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
            if host.rsplit(":", 1)[0] not in LOOPBACK:
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
            try:
                self._dispatch(method, u.path.rstrip("/") or "/", parse_qs(u.query))
            except KeyError as e:
                self._json(404, {"error": str(e).strip("'")})
            except RpcError as e:
                self._json(502, {"error": str(e)})
            except (ValueError, TypeError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})

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
                    return self._json(201, app.reg.create(b["cwd"], b.get("name")).info())
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
    ap.add_argument("--state-dir", default="~/.pi-bridge", help="identity and paired-peer keys")
    ap.add_argument("--peers", action="store_true", help="enable tailnet peer messaging")
    ap.add_argument("--peer-port", type=int, default=P.DEFAULT_PEER_PORT)
    args = ap.parse_args(argv)

    host, _, port = args.listen.rpartition(":")
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
              os.path.abspath(os.path.expanduser(args.state_dir)), name)
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
