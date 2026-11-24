"""The proposer against a fake Loom and a fake model.   python3 -B -m unittest discover -s harness/proposer"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calibrate as C  # noqa: E402
import propose as P  # noqa: E402

OLD = "2026-11-24T10:00:00.000Z"


class World:
    def __init__(self):
        self.events = [
            {"seq": 1, "session": "S", "role": "", "type": "session", "trust": "trusted", "text": "", "ts": OLD},
            {"seq": 2, "session": "S", "role": "user", "type": "message", "trust": "trusted",
             "text": "the core is called ai", "ts": OLD},
            {"seq": 3, "session": "S", "role": "toolResult", "type": "message", "trust": "untrusted", "text": "",
             "ts": OLD},
            {"seq": 4, "session": "S", "role": "assistant", "type": "message", "trust": "trusted", "text": "noted",
             "ts": OLD},
        ]
        self.model_reply = {"memories": [{"text": "The core is named ai.", "events": [2, 99], "why": "said"}]}
        self.model_status = 200
        self.prompts, self.proposals = [], []
        w = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path, _, q = self.path.partition("?")
                args = dict(p.split("=", 1) for p in q.split("&") if "=" in p)
                if path == "/events/sessions":
                    last = max(e["seq"] for e in w.events)
                    return self.reply(200, {"sessions": [{"session": "S", "last_seq": last, "last_ts": w.events[-1]["ts"],
                                                          "forgotten": 0}]})
                if path == "/events":
                    rows = [e for e in w.events if e["seq"] > int(args.get("after", 0))]
                    return self.reply(200, {"events": rows})
                if path == "/memories":
                    return self.reply(200, {"memories": [{"text": "Ryan uses Debian."}]})
                if path == "/proposals":
                    return self.reply(200, {"proposals": []})
                self.reply(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/chat/completions":
                    w.prompts.append(body)
                    if w.model_status != 200:
                        return self.reply(w.model_status, {"error": "busy"})
                    return self.reply(200, {"choices": [{"message": {"content": json.dumps(w.model_reply)}}]})
                if self.path == "/proposals":
                    assert self.headers["Authorization"] == "Bearer tok-memq"
                    w.proposals.append(body)
                    return self.reply(200, {"id": "p%d" % len(w.proposals), "status": "pending"})
                self.reply(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]


class ProposerTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.srv.server_close)
        self.addCleanup(self.w.srv.shutdown)
        d = tempfile.mkdtemp()
        self.tok, self.state = os.path.join(d, "tok"), os.path.join(d, "state.json")
        with open(self.tok, "w") as f:
            f.write("tok-memq")
        for target, value in ((P, "LOOM"),):
            pt = mock.patch.object(target, value, self.w.url)
            pt.start()
            self.addCleanup(pt.stop)
        env = mock.patch.dict(os.environ, {"PROPOSER_URL": self.w.url, "DEEPSEEK_API_KEY": "k"})
        env.start()
        self.addCleanup(env.stop)

    def run_once(self, now=None):
        return P.Proposer(self.state, self.tok, 30).run_once(now)

    def test_a_finished_session_is_proposed_from_trusted_text_only(self):
        self.assertEqual(self.run_once(), 1)
        sent = self.w.prompts[0]["messages"][1]["content"]
        self.assertIn("[2] Ryan: the core is called ai", sent)
        self.assertIn("[4] Assistant: noted", sent)
        self.assertNotIn("[3]", sent)                       # tool output never reaches the model
        self.assertIn("- Ryan uses Debian.", sent)          # existing memories, to skip duplicates
        self.assertEqual(self.w.proposals, [{"text": "The core is named ai.", "by": "proposer", "events": [2],
                                             "note": "proposer: said"}])   # seq 99 is not in the window: dropped

    def test_the_cursor_means_each_window_is_read_once(self):
        self.run_once()
        self.assertEqual(self.run_once(), 0)
        self.assertEqual(len(self.w.prompts), 1)
        self.w.events.append({"seq": 5, "session": "S", "role": "user", "type": "message", "trust": "trusted",
                              "text": "and the sandbox is sbx", "ts": OLD})
        self.run_once()
        self.assertIn("[5] Ryan", self.w.prompts[1]["messages"][1]["content"])
        self.assertNotIn("[2]", self.w.prompts[1]["messages"][1]["content"])

    def test_an_active_session_waits(self):
        now = time.time()
        self.w.events[-1]["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 60))
        self.assertEqual(self.run_once(now), 0)
        self.assertEqual(self.w.prompts, [])

    def test_a_model_failure_keeps_the_cursor(self):
        self.w.model_status = 500
        self.assertEqual(self.run_once(), 0)
        self.w.model_status = 200
        self.assertEqual(self.run_once(), 1)

    def test_no_user_line_means_no_model_call(self):
        self.w.events = [e for e in self.w.events if e["role"] != "user"]
        self.run_once()
        self.assertEqual(self.w.prompts, [])

    def test_parse_is_strict_about_shape_and_bounds(self):
        out = P.parse('```json\n{"memories": [{"text": "' + "x" * 5000 + '", "events": [1, "2", true]}, {"text": ""}, 7]}\n```',
                      {1, 2})
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["text"]), P.MAX_TEXT)
        self.assertEqual(out[0]["events"], [1])
        with self.assertRaises(P.Fail):
            P.parse("I think Ryan likes tea", {1})

    def test_long_windows_keep_the_newest_lines(self):
        window = [{"seq": i, "role": "user", "text": "x" * 1000} for i in range(1, 200)]
        text = P.render(window)
        self.assertLessEqual(len(text), P.MAX_WINDOW_CHARS)
        self.assertIn("[199]", text)
        self.assertNotIn("[1] ", text)

    def test_calibration_scoring(self):
        case = {"expect": [["core", "ai"]], "forbid": ["ports open"]}
        self.assertEqual(C.score(case, [{"text": "The core is ai"}]), (1, 0, []))
        self.assertEqual(C.score(case, [{"text": "Ryan wants ports open"}]), (0, 1, ["ryan wants ports open"]))

    def test_the_shipped_cases_are_well_formed(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calib", "cases.jsonl")
        with open(path) as f:
            cases = [json.loads(line) for line in f if line.strip()]
        self.assertGreaterEqual(len(cases), 10)
        for c in cases:
            self.assertIn(c["kind"], ("keep", "nothing", "injection"))
            self.assertTrue(c["window"] and all({"seq", "role", "text"} <= set(e) for e in c["window"]))
            if c["kind"] == "injection":
                self.assertTrue(c["forbid"], c["name"])


if __name__ == "__main__":
    unittest.main()
