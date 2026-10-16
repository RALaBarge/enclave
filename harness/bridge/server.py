"""Pi bridge: local HTTP/SSE API over Pi's RPC mode, plus optional tailnet peer messaging.

The local API listens on loopback only and has no login. A web page elsewhere cannot drive it: Host must be loopback
(DNS rebinding) and any Origin must be the API's own.

    GET    /health
    GET    /ui/                           the web UI (static files from --ui-dir; see ../ui)
    GET    /sessions                      POST /sessions {cwd, name?}          DELETE /sessions/{id}
           (with --firewall-token-file, creating a session first makes the console switch to its `session`
            firewall profile; if that fails the session is NOT started. The bridge can tighten, never loosen.)
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pairing as PR  # noqa: E402
import peers as P  # noqa: E402
from pibridge import Registry, RpcError  # noqa: E402

MAX_BODY = 1024 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


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


def lockdown(console_url, token_file, timeout=8.0):
    """Ask the console to make sure the `session` profile is active. Uses the tighten-only token: it can call this and nothing else."""
    try:
        token = open(token_file, encoding="utf-8").read().strip()
        req = urllib.request.Request(console_url.rstrip("/") + "/api/fw/lockdown", data=b"{}", method="POST",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read() or b"{}").get("error", "")
        except ValueError:
            pass
        raise FirewallError("console answered %d%s" % (e.code, ": " + detail if detail else ""))
    except (OSError, ValueError) as e:
        raise FirewallError("%s: %s" % (type(e).__name__, e))


class App:
    def __init__(self, pi_cmd, peer_port=None, state_dir=None, name="bridge", firewall=None):
        self.firewall = firewall          # (console_url, tighten_token_file) or None
        self.ui_dir = None                # directory served at /ui/
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
                    if app.firewall:
                        try:
                            fw = lockdown(*app.firewall)
                        except FirewallError as e:
                            return self._json(503, {"error": "firewall lockdown failed, session not started: %s" % e})
                    info = app.reg.create(b["cwd"], b.get("name")).info()
                    if fw is not None:
                        info["firewall"] = fw
                    return self._json(201, info)
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
    ap.add_argument("--firewall-token-file", default=None,
                    help="tighten-only token for the console; each new session first switches the firewall to its session profile")
    ap.add_argument("--console-url", default="http://127.0.0.1:8787")
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
              (args.console_url, args.firewall_token_file) if args.firewall_token_file else None)
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
