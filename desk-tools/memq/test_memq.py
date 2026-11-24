"""memq against a fake Loom on loopback.   python3 -B -m unittest discover -s desk-tools/memq"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("memq", os.path.join(HERE, "run.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


class Fake(BaseHTTPRequestHandler):
    seen = []
    reply = {}

    def log_message(self, *a):
        pass

    def _go(self, body):
        Fake.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
        code, out = Fake.reply.get(self.path.split("?")[0], (200, {}))
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._go(None)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._go(json.loads(self.rfile.read(n)) if n else None)


class MemqTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        M.LOOM = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        Fake.seen.clear()
        Fake.reply = {
            "/proposals": (200, {"id": "p1", "status": "pending", "proposals": [
                {"id": "p1", "text": "t", "status": "pending", "by": "agent", "events": [3], "note": "",
                 "created_at": "x", "memory_id": None, "decided_at": None}]}),
            "/proposals/pop": (200, {"applied": [{"proposal": "p1", "memory": "p-p1", "text": "t"}]}),
            "/memories/search": (200, {"results": [{"id": "p-p1", "text": "t", "distance": 0.1, "version": 1,
                                                    "updated_at": "x"}]}),
        }
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "memq.token"), "w") as f:
            f.write("tok-memq\n")

    def desk(self, args):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"DESKBOX_TOOL_DIR": self.dir}), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(args))), redirect_stdout(out):
            self.assertEqual(M.desk(), 0)
        return json.loads(out.getvalue())

    def test_agent_push_is_labelled_agent_and_carries_the_token(self):
        out = self.desk({"op": "push", "text": "core is ai", "events": [3], "note": "said by Ryan"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["id"], "p1")
        method, path, auth, body = Fake.seen[-1]
        self.assertEqual((method, path, auth), ("POST", "/proposals", "Bearer tok-memq"))
        self.assertEqual(body, {"text": "core is ai", "by": "agent", "events": [3], "note": "said by Ryan"})

    def test_the_input_cannot_claim_to_be_ryan(self):
        self.desk({"op": "push", "text": "x", "by": "ryan"})
        self.assertEqual(Fake.seen[-1][3]["by"], "agent")

    def test_list_pop_and_search(self):
        self.assertEqual(self.desk({"op": "list"})["entries"][0]["id"], "p1")
        self.assertEqual(Fake.seen[-1][1], "/proposals?status=pending")
        self.assertEqual(self.desk({"op": "pop"})["applied"][0]["memory"], "p-p1")
        self.assertEqual(Fake.seen[-1][2], "Bearer tok-memq")
        found = self.desk({"op": "search", "query": "core"})
        self.assertEqual(found["memories"], [{"id": "p-p1", "text": "t", "distance": 0.1, "updated_at": "x"}])
        self.assertIsNone(Fake.seen[-1][2])                 # reads carry no token

    def test_loom_errors_come_back_as_ok_false(self):
        Fake.reply["/proposals"] = (401, {"error": "memq token required"})
        out = self.desk({"op": "push", "text": "x"})
        self.assertFalse(out["ok"])
        self.assertIn("401", out["error"])

    def test_missing_token_is_reported(self):
        os.unlink(os.path.join(self.dir, "memq.token"))
        out = self.desk({"op": "pop"})
        self.assertFalse(out["ok"])
        self.assertIn("memq token", out["error"])

    def test_cli_pushes_as_ryan_with_its_own_token(self):
        tok = os.path.join(self.dir, "cli.token")
        with open(tok, "w") as f:
            f.write("tok-ryan")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MEMQ_TOKEN_FILE": tok}), redirect_stdout(out):
            self.assertEqual(M.cli(["push", "hello", "--event", "4"]), 0)
        self.assertEqual(Fake.seen[-1][2:], ("Bearer tok-ryan", {"text": "hello", "by": "ryan", "events": [4],
                                                                 "note": ""}))
        self.assertIn("p1 queued", out.getvalue())

    def test_cli_failure_exits_nonzero(self):
        Fake.reply["/proposals/pop"] = (503, {"error": "embedding unavailable"})
        with mock.patch.dict(os.environ, {"MEMQ_TOKEN_FILE": os.path.join(self.dir, "memq.token")}), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(M.cli(["pop"]), 1)
        self.assertIn("embedding unavailable", err.getvalue())


if __name__ == "__main__":
    unittest.main()
