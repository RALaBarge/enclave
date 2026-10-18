#!/usr/bin/env python3
"""Tests for sbx-session-gate.

Linux only (AF_UNIX + grp). Run:  python3 -m unittest discover -s harness/gate
The gate is exercised as a real process against a stand-in console, because the
things worth testing are the socket mode, the refusal path and the absence of
any loosening verb -- none of which a mocked function call would show.
"""
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "session_gate.py")


class FakeConsole(BaseHTTPRequestHandler):
    """Answers /api/fw/lockdown the way console/sbx_console.py does."""

    mode = "ok"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path != "/api/fw/lockdown":
            return self._send(404, {"error": "not_found"})
        if not (self.headers.get("Authorization") or "").startswith("Bearer "):
            return self._send(401, {"error": "unauthorized"})
        if FakeConsole.mode == "fail":
            return self._send(500, {"error": "nftables rejected the ruleset"})
        return self._send(200, {"profile": "session", "changed": True})

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def http_unix(sock_path, req_path="/", method="GET", body=None, timeout=10):
    """Minimal unix-socket HTTP client, so the tests do not import the gate."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    try:
        req = "%s %s HTTP/1.0\r\nHost: localhost\r\n" % (method, req_path)
        data = body or b""
        req += "Content-Length: %d\r\n\r\n" % len(data)
        s.sendall(req.encode() + data)
        chunks = []
        while True:
            c = s.recv(65536)
            if not c:
                break
            chunks.append(c)
        raw = b"".join(chunks).split(b"\r\n\r\n", 1)
        code = int(raw[0].split(b" ")[1])
        return code, json.loads(raw[1] or b"{}")
    finally:
        s.close()


@unittest.skipIf(os.name == "nt", "AF_UNIX + grp: run this under Linux")
class SessionGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = ThreadingHTTPServer(("127.0.0.1", 0), FakeConsole)
        threading.Thread(target=cls.console.serve_forever, daemon=True).start()
        cls.cport = cls.console.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.console.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sock = os.path.join(self.tmp, "gate.sock")
        self.token = os.path.join(self.tmp, "tighten-token")
        with open(self.token, "w", encoding="utf-8") as f:
            f.write("tok123\n")
        os.chmod(self.token, 0o600)
        FakeConsole.mode = "ok"
        env = dict(os.environ,
                   SBX_GATE_SOCKET=self.sock,
                   SBX_GATE_CONSOLE="http://127.0.0.1:%d" % self.cport,
                   SBX_GATE_TOKEN_FILE=self.token,
                   SBX_GATE_SOCKET_GROUP=os.environ.get("SBX_GATE_TEST_GROUP", "nogroup-xyz"))
        self.proc = subprocess.Popen([sys.executable, GATE], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            if os.path.exists(self.sock):
                break
            time.sleep(0.05)
        else:
            self.proc.kill()
            raise AssertionError("gate never created its socket")

    def tearDown(self):
        self.proc.kill()
        self.proc.wait(timeout=5)
        self.proc.stderr.close()

    def test_socket_is_group_scoped_not_world_writable(self):
        mode = stat.S_IMODE(os.stat(self.sock).st_mode)
        self.assertEqual(mode, 0o660, "socket mode is %04o" % mode)

    def test_lockdown_forwards_the_console_answer(self):
        code, doc = http_unix(self.sock, "/lockdown", "POST", b"{}")
        self.assertEqual(code, 200)
        self.assertEqual(doc["profile"], "session")
        self.assertTrue(doc["changed"])

    def test_console_failure_is_a_refusal_with_its_reason(self):
        FakeConsole.mode = "fail"
        code, doc = http_unix(self.sock, "/lockdown", "POST", b"{}")
        self.assertEqual(code, 502)
        self.assertIn("nftables rejected", doc["error"])

    def test_there_is_no_verb_that_loosens_anything(self):
        for path in ("/unlock", "/loosen", "/profile", "/api/fw/profile"):
            code, _ = http_unix(self.sock, path, "POST",
                                b"{}" if path != "/api/fw/profile" else b'{"active":"build"}')
            self.assertEqual(code, 404, "%s was accepted" % path)

    def test_status_reports_what_it_can_actually_guarantee(self):
        code, doc = http_unix(self.sock, "/status")
        self.assertEqual(code, 200)
        self.assertEqual(doc["profiles_forcible"], ["session"])
        self.assertIsNone(doc["last"]["result"])
        http_unix(self.sock, "/lockdown", "POST", b"{}")
        code, doc = http_unix(self.sock, "/status")
        self.assertEqual(doc["last"]["result"]["profile"], "session")
        self.assertIsNone(doc["last"]["error"])

    def test_missing_token_fails_closed(self):
        os.unlink(self.token)
        code, doc = http_unix(self.sock, "/lockdown", "POST", b"{}")
        self.assertEqual(code, 502)
        self.assertIn("tighten token", doc["error"])
        self.assertNotIn("tok123", json.dumps(doc), "the token must never appear in a response")


if __name__ == "__main__":
    unittest.main()
