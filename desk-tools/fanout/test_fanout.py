"""Tests for fanout, against a fake OpenAI-compatible server on loopback. No network, no real keys.

  python3 -B -m unittest discover -s desk-tools/fanout
"""
import json
import os
import re
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
        if model == "m/long":
            out = json.dumps({"choices": [{"message": {"content": "x" * 20000}, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if model == "m/reasoning":      # spent the whole budget thinking: no content, finish_reason length
            out = json.dumps({"choices": [{"message": {"content": "", "reasoning": "hmm"}, "finish_reason": "length"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if model == "synth/x":
            # The synthesizer is shown "--- name (model) ---" headers; echo the names so tests can see what it was given.
            user = body["messages"][-1]["content"]
            names = re.findall(r"^--- (\S+) \(", user, re.M)
            text = json.dumps({"synthesis": "combined: " + ",".join(names), "agreement": "agree", "differences": "differ",
                               "two_cents": "mine"})
        elif model == "synth/prose":
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
        # A stand-in for `claude -p`: records its argv, reads the prompt on stdin, answers with the synthesizer JSON.
        cls.argv_log = os.path.join(cls.tmp.name, "claude-argv.json")
        cls.fake_claude = os.path.join(cls.tmp.name, "claude")
        with open(cls.fake_claude, "w") as fh:
            fh.write("#!%s\nimport json, sys\n"
                     "json.dump(sys.argv[1:], open(%r, 'w'))\n"
                     "import os\n"
                     "json.dump({k: os.environ.get(k) for k in ('CLAUDE_CODE_OAUTH_TOKEN', 'DISABLE_AUTOUPDATER', 'ANTHROPIC_API_KEY')},"
                     " open(%r, 'w'))\n"
                     "user = sys.stdin.read()\n"
                     "print(json.dumps({'synthesis': 'from cli', 'agreement': 'a', 'differences': 'd', 'two_cents': 't'}))\n"
                     % (sys.executable, cls.argv_log, cls.argv_log + ".env"))
        os.chmod(cls.fake_claude, 0o755)
        with open(cls.keys, "w") as fh:
            json.dump({"openrouter": {"base_url": f"http://127.0.0.1:{cls.srv.server_port}/v1",
                                      "headers": {"Authorization": "Bearer SECRET-1"}},
                       "other": {"base_url": f"http://127.0.0.1:{cls.srv.server_port}/v1",
                                 "headers": {"x-api-key": "SECRET-2"}},
                       "claude_cli": {"path": cls.fake_claude, "env": {"CLAUDE_CODE_OAUTH_TOKEN": "TOKEN-3"}}}, fh)

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

    def test_synthesizer_combines_the_answers_adds_its_two_cents_and_does_not_rank(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}, {"name": "g", "model": "m/good"}, {"name": "c", "model": "m/c"}],
                       "synthesizer": {"model": "synth/x"}})
        self.assertEqual(out["ok_count"], 3)
        s = out["synthesizer"]
        self.assertEqual((s["synthesis"], s["agreement"], s["differences"], s["two_cents"]), ("combined: a,g,c", "agree", "differ", "mine"))
        self.assertNotIn("winner", s)
        self.assertNotIn("scores", s)
        # every model's full reply is returned, in the order asked, for side-by-side comparison
        self.assertEqual([(x["name"], x["text"]) for x in out["answers"]], [("a", "meh"), ("g", "GOOD answer"), ("c", "meh")])
    def test_request_bodies_are_identical_except_for_model(self):
        self.ok({"prompt": "q", "system": "be brief", "max_tokens": 50, "temperature": 0.3,
                 "stubs": [{"name": "a", "model": "m/a"}, {"name": "b", "model": "m/b", "key": "other"}], "synthesizer": {"model": "synth/x"}})
        bodies = [(h, b) for p, h, b in Fake.seen if b["model"].startswith("m/")]
        self.assertEqual(len(bodies), 2)
        strip = lambda b: {k: v for k, v in b.items() if k != "model"}
        self.assertEqual(strip(bodies[0][1]), strip(bodies[1][1]))
        sys_msg = bodies[0][1]["messages"][0]
        self.assertEqual(sys_msg["role"], "system")
        self.assertTrue(sys_msg["content"].startswith("be brief\n\n"))   # the caller's system prompt comes first
        self.assertEqual(bodies[0][1]["max_tokens"], 50)

    def test_models_are_told_the_length_cap(self):
        self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}], "synthesizer": {"backend": "none"}})
        content = Fake.seen[0][2]["messages"][0]["content"]       # no caller system prompt: the note stands alone
        self.assertIn("cut off after 16000 characters", content)

    def test_thinking_is_off_by_default_and_on_when_asked(self):
        self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}, {"name": "b", "model": "m/b"}], "synthesizer": {"backend": "none"}})
        self.assertTrue(all(b["reasoning"] == {"enabled": False} for p, h, b in Fake.seen))
        self.ok({"prompt": "q", "thinking": True, "stubs": [{"name": "a", "model": "m/a"}], "synthesizer": {"backend": "none"}})
        self.assertNotIn("reasoning", Fake.seen[0][2])

    def test_an_answer_over_the_cap_is_cut_at_the_cap(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/long"}], "synthesizer": {"backend": "none"}})
        self.assertEqual(len(out["answers"][0]["text"]), 16000)

    def test_only_headers_differ_per_key_and_path_is_chat_completions(self):
        self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}, {"name": "b", "model": "m/b", "key": "other"}], "synthesizer": {"model": "synth/x"}})
        by_model = {b["model"]: (p, {k.lower(): v for k, v in h.items()}) for p, h, b in Fake.seen}
        self.assertEqual(by_model["m/a"][0], "/v1/chat/completions")
        self.assertEqual(by_model["m/a"][1].get("authorization"), "Bearer SECRET-1")
        self.assertEqual(by_model["m/b"][1].get("x-api-key"), "SECRET-2")
        self.assertIsNone(by_model["m/b"][1].get("authorization"))

    def test_cli_synthesizer_runs_claude_headless_and_never_calls_the_api_for_it(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}, {"name": "g", "model": "m/good"}],
                       "synthesizer": {"backend": "cli"}})
        self.assertEqual(out["synthesizer"]["synthesis"], "from cli")
        self.assertEqual(out["synthesizer"]["two_cents"], "t")
        self.assertEqual(out["synthesizer"]["model"], "claude-fable-5-2")
        self.assertEqual(len(Fake.seen), 2, "only the two stubs may hit the API")
        with open(self.argv_log) as fh:
            argv = json.load(fh)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-fable-5-2")
        self.assertEqual(argv[argv.index("--tools") + 1], "")           # the synthesizer gets no tools
        self.assertIn("-p", argv)
        self.assertNotIn("--bare", argv)                                  # --bare would skip the Claude Code login
    def test_cli_gets_the_subscription_token_and_no_api_key(self):
        self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}], "synthesizer": {"backend": "cli"}})
        with open(self.argv_log + ".env") as fh:
            env = json.load(fh)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "TOKEN-3")
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
        self.assertIsNone(env["ANTHROPIC_API_KEY"])        # the job env is built from scratch; nothing leaks in

    def test_cli_synthesizer_missing_binary_is_an_error_not_a_crash(self):
        keys = os.path.join(self.tmp.name, "keys-nocli.json")
        with open(keys, "w") as fh:
            json.dump({"openrouter": {"base_url": f"http://127.0.0.1:{self.srv.server_port}/v1", "headers": {}},
                       "claude_cli": {"path": os.path.join(self.tmp.name, "nope")}}, fh)
        p = self.run_tool({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}], "synthesizer": {"backend": "cli"}}, keys=keys)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertIn("claude CLI not found", out["synthesizer"]["error"])
        self.assertEqual(out["ok_count"], 1)

    def test_backend_none_returns_answers_only(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}], "synthesizer": {"backend": "none"}})
        self.assertEqual(out["ok_count"], 1)
        self.assertEqual(out["synthesizer"]["synthesis"], "")
        self.assertEqual(out["synthesizer"]["error"], "")
        self.assertEqual(len(Fake.seen), 1)       # only the one stub was called
    def test_unknown_backend_exits_nonzero(self):
        self.assertNotEqual(self.run_tool({"prompt": "q", "synthesizer": {"backend": "magic"}}).returncode, 0)

    def test_synthesizer_sees_each_models_name_and_full_answer(self):
        self.ok({"prompt": "q", "stubs": [{"name": "alpha", "model": "m/good"}, {"name": "beta", "model": "m/b"}], "synthesizer": {"model": "synth/x"}})
        body = next(b for p, h, b in Fake.seen if b["model"] == "synth/x")
        text = body["messages"][-1]["content"]
        self.assertIn("--- alpha (m/good) ---\nGOOD answer", text)
        self.assertIn("--- beta (m/b) ---\nmeh", text)
        self.assertNotIn("winner", body["messages"][0]["content"])      # it is asked to synthesize, not to rank
    def test_a_failing_model_is_reported_and_the_rest_are_synthesized(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "dead", "model": "bad/500"}, {"name": "g", "model": "m/good"}], "synthesizer": {"model": "synth/x"}})
        self.assertEqual(out["ok_count"], 1)
        dead = next(a for a in out["answers"] if a["name"] == "dead")
        self.assertFalse(dead["ok"])
        self.assertIn("500", dead["error"])
        self.assertEqual(out["synthesizer"]["synthesis"], "combined: g")
    def test_a_reasoning_model_out_of_tokens_says_so(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "r", "model": "m/reasoning"}, {"name": "g", "model": "m/good"}], "synthesizer": {"backend": "none"}})
        r = next(a for a in out["answers"] if a["name"] == "r")
        self.assertFalse(r["ok"])
        self.assertIn("raise max_tokens", r["error"])

    def test_all_models_failing_skips_the_synthesizer(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "dead", "model": "bad/500"}]})
        self.assertEqual(out["ok_count"], 0)
        self.assertEqual(out["synthesizer"]["error"], "no model returned an answer")
        self.assertFalse(any(b["model"].startswith("anthropic") for p, h, b in Fake.seen))
    def test_a_synthesizer_that_returns_prose_keeps_its_text(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a"}], "synthesizer": {"model": "synth/prose"}})
        self.assertEqual(out["synthesizer"]["error"], "synthesizer did not return JSON; its raw text is in synthesis")
        self.assertEqual(out["synthesizer"]["synthesis"], "I cannot produce JSON, sorry")
    def test_default_spread_is_the_eight_models_and_default_synthesizer(self):
        out = self.ok({"prompt": "q"})
        self.assertEqual(len(out["answers"]), 8)
        self.assertEqual(out["synthesizer"]["model"], "anthropic/claude-fable-5.2")
        self.assertIn("google/gemini-3.8-flash", {a["model"] for a in out["answers"]})
    def test_unknown_key_name_is_an_answer_error(self):
        out = self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a", "key": "nope"}, {"name": "g", "model": "m/good"}], "synthesizer": {"model": "synth/x"}})
        a = next(x for x in out["answers"] if x["name"] == "a")
        self.assertIn("no keys.json entry", a["error"])

    def test_no_secret_ever_appears_in_output(self):
        p = self.run_tool({"prompt": "q", "stubs": [{"name": "dead", "model": "bad/500"}]})
        self.assertNotIn("SECRET", p.stdout + p.stderr)

    def test_input_cannot_choose_the_url(self):
        # base_url in a stub is not part of the contract; the tool must ignore it rather than send a key there.
        out = self.ok({"prompt": "q", "stubs": [{"name": "a", "model": "m/a", "base_url": "http://evil.invalid"}], "synthesizer": {"model": "synth/x"}})
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
