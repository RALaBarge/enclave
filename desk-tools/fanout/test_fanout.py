"""Tests for fanout, against a fake OpenAI-compatible server on loopback. No network, no real keys.

  python3 -B -m unittest discover -s desk-tools/fanout
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "run.py")


class Fake(BaseHTTPRequestHandler):
    seen = []          # (path, headers, body) of every request, shared with the tests

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append((self.path, dict(self.headers), body))
        model = body["model"]
        if model == "bad/500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error":"boom"}')
            return
        if model == "judge/x":
            # The judge sees Answer 1..n shuffled; pick whichever one contains "GOOD".
            user = body["messages"][-1]["content"]
            parts = user.split("--- Answer ")[1:]
            good = next((int(p.split(" ")[0]) for p in parts if "GOOD" in p), 1)
            text = json.dumps({"winner": good, "scores": {str(i + 1): (9 if i + 1 == good else 2) for i in range(len(parts))},
                               "rationale": "because", "synthesis": "final"})
        elif model == "judge/prose":
            text = "I cannot produce JSON, sorry"
        else:
            text = "GOOD answer" if model == "m/good" else "meh"
        out = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class FanoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.keys = os.path.join(cls.tmp.name, "keys.json")
        with open(cls.keys, "w") as fh:
            json.dump({"openrouter": {"base_url": f"http://127.0.0.1:{cls.srv.server_port}/v1",
                                      "headers": {"Authorization": "Bearer SECRET-1"}},
                       "other": {"base_url": f"http://127.0.0.1:{cls.srv.server_port}/v1",
                                 "headers": {"x-api-key": "SECRET-2"}}}, fh)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def run_tool(self, req, keys=None):
        Fake.seen.clear()
        env = dict(os.environ, FANOUT_KEYS=keys or self.keys)
        p = subprocess.run([sys.executable, "-B", RUN], input=json.dumps(req), capture_output=True, text=True, env=env, timeout=60)
        return p

    def ok(self, req):
        p = self.run_tool(req)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_judge_picks_the_good_answer_and_names_come_back(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}, {"name": "g", "model": "m/good"}, {"name": "c", "model": "m/c"}],
                       "judge": {"model": "judge/x"}})
        self.assertEqual(out["ok_count"], 3)
        self.assertEqual(out["judge"]["winner"], "g")
        self.assertEqual({s["name"]: s["score"] for s in out["judge"]["scores"]}, {"a": 2, "g": 9, "c": 2})
        self.assertEqual(out["judge"]["synthesis"], "final")

    def test_request_bodies_are_identical_except_for_model(self):
        self.ok({"prompt": "q", "system": "be brief", "max_tokens": 50, "temperature": 0.3,
                 "stubs": [{"name": "a", "model": "m/a"}, {"name": "b", "model": "m/b", "key": "other"}], "judge": {"model": "judge/x"}})
        bodies = [(h, b) for p, h, b in Fake.seen if b["model"].startswith("m/")]
        self.assertEqual(len(bodies), 2)
        strip = lambda b: {k: v for k, v in b.items() if k != "model"}
        self.assertEqual(strip(bodies[0][1]), strip(bodies[1][1]))
        self.assertEqual(bodies[0][1]["messages"][0], {"role": "system", "content": "be brief"})
        self.assertEqual(bodies[0][1]["max_tokens"], 50)

    def test_only_headers_differ_per_key_and_path_is_chat_completions(self):
        self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}, {"name": "b", "model": "m/b", "key": "other"}], "judge": {"model": "judge/x"}})
        by_model = {b["model"]: (p, {k.lower(): v for k, v in h.items()}) for p, h, b in Fake.seen}
        self.assertEqual(by_model["m/a"][0], "/v1/chat/completions")
        self.assertEqual(by_model["m/a"][1].get("authorization"), "Bearer SECRET-1")
        self.assertEqual(by_model["m/b"][1].get("x-api-key"), "SECRET-2")
        self.assertIsNone(by_model["m/b"][1].get("authorization"))

    def test_judge_sees_no_names_or_models(self):
        self.ok({"prompt": "q", "stubs": [{"name": "SECRETNAME", "model": "m/good"}, {"name": "b", "model": "m/b"}], "judge": {"model": "judge/x"}})
        judge_body = next(b for p, h, b in Fake.seen if b["model"] == "judge/x")
        text = json.dumps(judge_body)
        self.assertNotIn("SECRETNAME", text)
        self.assertNotIn("m/b", text)

    def test_a_failing_model_is_reported_and_the_rest_are_judged(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "dead", "model": "bad/500"}, {"name": "g", "model": "m/good"}], "judge": {"model": "judge/x"}})
        self.assertEqual(out["ok_count"], 1)
        dead = next(a for a in out["answers"] if a["name"] == "dead")
        self.assertFalse(dead["ok"])
        self.assertIn("500", dead["error"])
        self.assertEqual(out["judge"]["winner"], "g")

    def test_all_models_failing_skips_the_judge(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "dead", "model": "bad/500"}]})
        self.assertEqual(out["ok_count"], 0)
        self.assertEqual(out["judge"]["error"], "no model returned an answer")
        self.assertFalse(any(b["model"].startswith("anthropic") for p, h, b in Fake.seen))

    def test_judge_that_returns_prose_is_an_error_not_a_crash(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}], "judge": {"model": "judge/prose"}})
        self.assertEqual(out["judge"]["error"], "judge did not return JSON")
        self.assertEqual(out["judge"]["winner"], "")

    def test_default_spread_is_the_eight_models_and_default_judge(self):
        out = self.ok({"prompt": "q"})
        self.assertEqual(len(out["answers"]), 8)
        self.assertEqual(out["judge"]["model"], "anthropic/claude-fable-5.2")
        self.assertIn("google/gemini-3.8-flash", {a["model"] for a in out["answers"]})

    def test_unknown_key_name_is_an_answer_error(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a", "key": "nope"}, {"name": "g", "model": "m/good"}], "judge": {"model": "judge/x"}})
        a = next(x for x in out["answers"] if x["name"] == "a")
        self.assertIn("no keys.json entry", a["error"])

    def test_no_secret_ever_appears_in_output(self):
        p = self.run_tool({"prompt": "q", "stubs": [{"name": "dead", "model": "bad/500"}]})
        self.assertNotIn("SECRET", p.stdout + p.stderr)

    def test_input_cannot_choose_the_url(self):
        # base_url in a stub is not part of the contract; the tool must ignore it rather than send a key there.
        out = self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a", "base_url": "http://evil.invalid"}], "judge": {"model": "judge/x"}})
        self.assertTrue(out["answers"][0]["ok"])

    def test_bad_input_exits_nonzero(self):
        self.assertNotEqual(self.run_tool({"prompt": ""}).returncode, 0)
        self.assertNotEqual(self.run_tool({"prompt": "q", "stubs": [{"name": "a", "model": "x"}, {"name": "a", "model": "y"}]}).returncode, 0)

    def test_missing_keys_file_exits_nonzero_without_a_traceback(self):
        p = self.run_tool({"prompt": "q"}, keys=os.path.join(self.tmp.name, "absent.json"))
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
