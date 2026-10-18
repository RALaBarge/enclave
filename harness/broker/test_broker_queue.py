#!/usr/bin/env python3
"""Tests for the broker's serial lane.

Run from the repo root:
    python -m unittest discover -s harness/broker -p "test_*.py" -v

The broker is a script, not a package. This sets the settings it reads at
import time, imports it from this directory, and replaces run_on_sbx with a
stub so no SSH or sandbox is involved.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["SBX_HOST"] = "192.0.2.10"
os.environ["SBX_KEY"] = os.path.join(HERE, "test-key")
os.environ["SBX_RESET_CMD"] = ""
sys.path.insert(0, HERE)
import sbx_broker  # noqa: E402

OK_ENVELOPE = b'{"output": {}, "files": {}}'
KEY = b"0123456789abcdef-test-key"
_job_counter = [0]


def signed(doc, key=KEY, now=None, job_id=None, attempt=1, caller=None):
    """doc plus the job token the desk would have put in the job's environment."""
    out = dict(doc)
    _job_counter[0] += 1
    job_id = job_id or "job-%d" % _job_counter[0]
    caller = caller if caller is not None else out.get("caller", "")
    ts = int(now if now is not None else time.time())
    out.update({"job_id": job_id, "attempt": attempt, "ts": ts,
                "mac": sbx_broker.job_token_mac(key, caller, job_id, attempt, ts)})
    return out


def ok_run(request, wall_timeout):
    return "ok", OK_ENVELOPE


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tool_list = os.path.join(self.tmp.name, "tools.list")
        with open(self.tool_list, "w", encoding="utf-8") as f:
            f.write("greet 5\n")
        self._saved = (sbx_broker.TOOLLIST, sbx_broker.run_on_sbx,
                       sbx_broker.reset_sbx, sbx_broker.lane,
                       sbx_broker.RESET_CMD, sbx_broker._run_reset,
                       sbx_broker._reset_state)
        sbx_broker.TOOLLIST = self.tool_list
        sbx_broker.run_on_sbx = ok_run
        self._saved_key = (sbx_broker.JOB_KEY_PATH, sbx_broker.used_tokens)
        sbx_broker.JOB_KEY_PATH = os.path.join(self.tmp.name, "job.key")
        with open(sbx_broker.JOB_KEY_PATH, "wb") as f:
            f.write(KEY + b"\n")
        sbx_broker.used_tokens = sbx_broker.UsedTokens()
        sbx_broker.RESET_CMD = ""
        sbx_broker._reset_state = {"ok": True, "rc": 0, "at": None}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), sbx_broker.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        sbx_broker.JOB_KEY_PATH, sbx_broker.used_tokens = self._saved_key
        (sbx_broker.TOOLLIST, sbx_broker.run_on_sbx,
         sbx_broker.reset_sbx, sbx_broker.lane,
         sbx_broker.RESET_CMD, sbx_broker._run_reset,
         sbx_broker._reset_state) = self._saved

    def use_lane(self, depth_max, wait_s):
        sbx_broker.lane = sbx_broker.SerialLane(depth_max, wait_s)
        return sbx_broker.lane

    def post(self, timeout=10):
        return self.post_body(signed({"tool": "greet", "caller": "greet-sbx", "input": {}}), timeout)

    def post_body(self, doc, timeout=10):
        data = json.dumps(doc).encode()
        req = urllib.request.Request(self.base + "/run", data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get_status(self):
        with urllib.request.urlopen(self.base + "/status", timeout=5) as r:
            return r.status, json.loads(r.read())

    def post_async(self, results, key):
        def call():
            try:
                results[key] = self.post()
            except BaseException as e:  # surfaced by the caller's assertion
                results[key] = ("EXC", repr(e))
        t = threading.Thread(target=call, daemon=True)
        t.start()
        return t

    def wait_for(self, predicate, what, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what}")

    # -- the caller must be the contract of the tool it asks for -----------

    def test_caller_must_match_the_tool(self):
        self.use_lane(1, 5)
        ran = []
        def record(request, wall_timeout):
            ran.append(request["tool"])
            return "ok", OK_ENVELOPE
        sbx_broker.run_on_sbx = record
        with open(self.tool_list, "w", encoding="utf-8") as f:
            f.write("greet 5\nrsync 5 net\n")
        for label, doc, want in [
            ("matching caller", {"tool": "greet", "caller": "greet-sbx", "input": {}}, 200),
            ("a non-net stub asking for a net tool", {"tool": "rsync", "caller": "greet-sbx", "input": {}}, 403),
            ("caller without the -sbx suffix", {"tool": "greet", "caller": "greet", "input": {}}, 403),
            ("caller of another tool", {"tool": "greet", "caller": "rsync-sbx", "input": {}}, 403),
            ("no caller at all", {"tool": "greet", "input": {}}, 400),
            ("caller that is not a name", {"tool": "greet", "caller": ["greet-sbx"], "input": {}}, 400),
        ]:
            with self.subTest(label):
                before = len(ran)
                code, body = self.post_body(signed(doc) if "caller" in doc and isinstance(doc["caller"], str) else doc)
                self.assertEqual(code, want, body)
                self.assertEqual(len(ran) - before, 1 if want == 200 else 0)
        self.assertEqual(ran, ["greet"])

    def test_a_caller_mismatch_is_logged_as_an_alert(self):
        self.use_lane(1, 5)
        lines = []
        saved = sbx_broker.log
        sbx_broker.log = lines.append
        self.addCleanup(setattr, sbx_broker, "log", saved)
        code, _ = self.post_body(signed({"tool": "greet", "caller": "rsync-sbx", "input": {}}))
        self.assertEqual(code, 403)
        self.assertTrue(any(l.startswith("ALERT caller_mismatch") and "tool=greet" in l and "caller=rsync-sbx" in l
                            for l in lines), lines)

    # -- the desk's token: what makes the caller something a job was given ---

    def test_token_checks(self):
        self.use_lane(1, 5)
        ran = []

        def record(request, wall_timeout):
            ran.append(request["tool"])
            return "ok", OK_ENVELOPE
        sbx_broker.run_on_sbx = record
        base = {"tool": "greet", "caller": "greet-sbx", "input": {}}
        now = time.time()
        for label, doc, want in [
            ("a token the desk made", signed(base), 200),
            ("a token made with another key", signed(base, key=b"another-key-0123456789"), 403),
            ("a token made for another tool's caller",
             dict(signed(base), mac=sbx_broker.job_token_mac(KEY, "rsync-sbx", "job-x", 1, int(now))), 403),
            ("a token a minute past its life", signed(base, now=now - sbx_broker.TOKEN_MAX_AGE_S - 60), 403),
            ("a token from the future", signed(base, now=now + sbx_broker.TOKEN_MAX_AGE_S + 60), 403),
            ("no token at all", dict(base), 400),
            ("an empty mac", dict(signed(base), mac=""), 400),
            ("a mac that is not hex", dict(signed(base), mac="z" * 64), 403),
            ("attempt as a string", dict(signed(base), attempt="1"), 400),
        ]:
            with self.subTest(label):
                before = len(ran)
                code, body = self.post_body(doc)
                self.assertEqual(code, want, body)
                self.assertEqual(len(ran) - before, 1 if want == 200 else 0)

    def test_a_token_works_once(self):
        self.use_lane(1, 5)
        doc = signed({"tool": "greet", "caller": "greet-sbx", "input": {}}, job_id="job-once")
        self.assertEqual(self.post_body(doc)[0], 200)
        code, body = self.post_body(doc)
        self.assertEqual((code, body.get("error")), (403, "token_replayed"))
        # A retry is a new attempt with its own token.
        retry = signed({"tool": "greet", "caller": "greet-sbx", "input": {}}, job_id="job-once", attempt=2)
        self.assertEqual(self.post_body(retry)[0], 200)

    def test_a_missing_key_refuses_everything(self):
        self.use_lane(1, 5)
        os.unlink(sbx_broker.JOB_KEY_PATH)
        code, body = self.post()
        self.assertEqual((code, body.get("error")), (500, "job_key_error"))

    def test_the_key_is_created_once_and_kept(self):
        path = os.path.join(self.tmp.name, "new.key")
        sbx_broker.ensure_job_key(path, group="")
        first = sbx_broker.read_job_key(path)
        self.assertIsNotNone(first)
        self.assertGreaterEqual(len(first), 16)
        sbx_broker.ensure_job_key(path, group="")
        self.assertEqual(sbx_broker.read_job_key(path), first)

    def test_the_python_and_go_implementations_agree(self):
        # cmd/agent-desk/jobtoken_test.go (TestJobTokenKnownVector) pins the same value.
        self.assertEqual(
            sbx_broker.job_token_mac(b"0123456789abcdef-secret", "greet-sbx", "job-1", 1, 1000),
            "c9cc4c00384942a57393589f4d4b0e1af71045787e690e648ed8357aee7d5ff1")

    def test_the_shipped_stub_sends_its_contract_as_caller(self):
        # The broker now refuses a request without a matching caller, so a stub
        # that stopped sending one would fail every call. Text check only: the
        # stub hardcodes /deskbox paths and cannot be run here.
        with open(os.path.join(HERE, "..", "stub", "run.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn('caller = os.environ.get("DESKBOX_TOOL", "")', text)
        self.assertIn('"caller": caller', text)
        for env in ("DESKBOX_JOB_ID", "DESKBOX_ATTEMPT", "DESKBOX_JOB_TS", "DESKBOX_JOB_MAC"):
            self.assertIn(env, text)

    # -- malformed requests answer, they do not drop the connection ------

    def raw_post(self, body, content_length=None):
        import http.client
        host, port = self.base.rsplit("//", 1)[1].split(":")
        c = http.client.HTTPConnection(host, int(port), timeout=5)
        try:
            c.putrequest("POST", "/run")
            c.putheader("Content-Length", content_length if content_length is not None else str(len(body)))
            c.endheaders()
            c.send(body)
            r = c.getresponse()
            return r.status, json.loads(r.read())
        finally:
            c.close()

    def test_malformed_requests_get_an_answer(self):
        self.use_lane(1, 5)
        for label, body, clen, want in [
            ("content-length not a number", b"{}", "abc", 400),
            ("body is a list", b"[]", None, 400),
            ("body is a string", b'"hi"', None, 400),
            ("body is null", b"null", None, 400),
            ("missing tool", b"{}", None, 400),
        ]:
            with self.subTest(label):
                code, body_out = self.raw_post(body, clen)
                self.assertEqual(code, want, body_out)

    # -- /status ---------------------------------------------------------

    def test_status_idle_shape(self):
        self.use_lane(3, 5)
        code, body = self.get_status()
        self.assertEqual(code, 200)
        self.assertEqual(set(body), {"busy", "queued", "depth_max", "last",
                                     "lane", "reset"})
        self.assertEqual(body, {"busy": False, "queued": 0, "depth_max": 3,
                                "last": None, "lane": "serial",
                                "reset": {"ok": True, "rc": 0, "at": None}})

    def test_status_records_last_job(self):
        self.use_lane(1, 5)
        code, body = self.post()
        self.assertEqual(code, 200)
        self.assertEqual(body, {"output": {}, "files": {}})

        code, body = self.get_status()
        self.assertEqual(code, 200)
        self.assertFalse(body["busy"])
        self.assertEqual(body["queued"], 0)
        self.assertEqual(body["depth_max"], 1)
        last = body["last"]
        self.assertEqual(set(last), {"tool", "status", "dur_s", "started_at"})
        self.assertEqual(last["tool"], "greet")
        self.assertEqual(last["status"], "ok")
        self.assertIsInstance(last["dur_s"], float)
        self.assertGreaterEqual(last["dur_s"], 0.0)
        self.assertRegex(last["started_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    # -- failed reset ----------------------------------------------------

    def test_failed_reset_gates_the_next_job(self):
        self.use_lane(2, 5)
        sbx_broker.RESET_CMD = "reset"
        rc = {"value": 1}
        reset_calls = []

        def fake_reset():
            reset_calls.append(rc["value"])
            return rc["value"]

        sbx_broker._run_reset = fake_reset

        # The job that was running when the reset failed is unaffected.
        code, body = self.post()
        self.assertEqual(code, 200)
        self.assertEqual(body, {"output": {}, "files": {}})
        code, status = self.get_status()
        self.assertEqual(code, 200)
        self.assertFalse(status["reset"]["ok"])
        self.assertEqual(status["reset"]["rc"], 1)
        self.assertRegex(status["reset"]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(status["last"]["status"], "ok")
        self.assertEqual(reset_calls, [1])

        # The next job retries the reset; when that fails too, it is refused
        # before run_on_sbx is called.
        ran = []

        def tracked_run(request, wall_timeout):
            ran.append(1)
            return "ok", OK_ENVELOPE

        sbx_broker.run_on_sbx = tracked_run
        code, body = self.post()
        self.assertEqual(code, 503)
        self.assertEqual(set(body), {"error", "reset"})
        self.assertEqual(body["error"], "sandbox_not_reset")
        self.assertFalse(body["reset"]["ok"])
        self.assertEqual(body["reset"]["rc"], 1)
        self.assertRegex(body["reset"]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(ran, [])
        self.assertEqual(reset_calls, [1, 1])

        # A later successful reset lets the lane run jobs again.
        rc["value"] = 0
        code, body = self.post()
        self.assertEqual(code, 200)
        self.assertEqual(body, {"output": {}, "files": {}})
        self.assertEqual(ran, [1])
        code, status = self.get_status()
        self.assertTrue(status["reset"]["ok"])
        self.assertEqual(status["reset"]["rc"], 0)

    # -- queue limits ----------------------------------------------------

    def test_queue_full_answers_503_with_depth(self):
        self.use_lane(1, 10)
        started = threading.Event()
        release = threading.Event()

        def blocking_run(request, wall_timeout):
            started.set()
            release.wait(10)
            return "ok", OK_ENVELOPE

        sbx_broker.run_on_sbx = blocking_run
        results = {}
        first = self.post_async(results, "first")
        self.assertTrue(started.wait(5), "the first job did not start")
        second = self.post_async(results, "second")
        self.wait_for(lambda: self.get_status()[1]["queued"] == 1,
                      "the second caller to queue")

        code, body = self.post()
        self.assertEqual(code, 503)
        self.assertEqual(body, {"error": "lane_busy", "reason": "queue_full",
                                "lane": "serial", "busy": True,
                                "queued": 1, "depth_max": 1})

        release.set()
        first.join(10)
        second.join(10)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(results["first"][0], 200)
        self.assertEqual(results["second"][0], 200)

    def test_queue_wait_timeout_answers_503(self):
        self.use_lane(2, 0.2)
        started = threading.Event()
        release = threading.Event()

        def blocking_run(request, wall_timeout):
            started.set()
            release.wait(10)
            return "ok", OK_ENVELOPE

        sbx_broker.run_on_sbx = blocking_run
        results = {}
        first = self.post_async(results, "first")
        self.assertTrue(started.wait(5), "the first job did not start")

        code, body = self.post(timeout=5)
        self.assertEqual(code, 503)
        self.assertEqual(body, {"error": "lane_busy", "reason": "queue_timeout",
                                "lane": "serial", "busy": True, "queued": 0,
                                "depth_max": 2, "wait_s": 0.2})

        release.set()
        first.join(10)
        self.assertEqual(results["first"][0], 200)

    def test_zero_depth_refuses_while_busy(self):
        lane = self.use_lane(0, 5)
        started = threading.Event()
        release = threading.Event()

        def job():
            started.set()
            release.wait(5)
            return "ok"

        first = threading.Thread(target=lambda: lane.run("greet", job), daemon=True)
        first.start()
        self.assertTrue(started.wait(5))
        with self.assertRaises(sbx_broker.LaneFull) as caught:
            lane.run("greet", lambda: "ok")
        self.assertEqual((caught.exception.queued, caught.exception.depth_max), (0, 0))
        release.set()
        first.join(5)

    # -- ordering and exclusion -----------------------------------------

    def test_waiting_callers_served_in_order(self):
        lane = self.use_lane(4, 10)
        order = []
        started = threading.Event()
        release = threading.Event()

        def job(label):
            def run():
                if label == 1:
                    started.set()
                    release.wait(10)
                order.append(label)
                return "ok"
            return run

        first = threading.Thread(target=lambda: lane.run("greet", job(1)), daemon=True)
        first.start()
        self.assertTrue(started.wait(5), "the first job did not start")
        waiters = []
        for label in (2, 3, 4):
            t = threading.Thread(target=lambda n=label: lane.run("greet", job(n)), daemon=True)
            t.start()
            waiters.append(t)
        self.wait_for(lambda: lane.status()["queued"] == 3, "three waiters to queue")

        release.set()
        first.join(10)
        for t in waiters:
            t.join(10)
        self.assertFalse(first.is_alive() or any(t.is_alive() for t in waiters))
        self.assertEqual(order, [1, 2, 3, 4])

    def test_one_at_a_time_under_concurrent_submissions(self):
        self.use_lane(8, 10)
        guard = threading.Lock()
        state = {"active": 0, "max": 0, "resets": 0, "reset_overlap": 0}

        def fake_run(request, wall_timeout):
            with guard:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.02)
            with guard:
                state["active"] -= 1
            return "ok", OK_ENVELOPE

        def fake_reset():
            with guard:
                state["resets"] += 1
                if state["active"] != 0:
                    state["reset_overlap"] += 1

        sbx_broker.run_on_sbx = fake_run
        sbx_broker.reset_sbx = fake_reset
        results = {}
        threads = [self.post_async(results, i) for i in range(8)]
        for t in threads:
            t.join(15)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(len(results), 8)
        self.assertTrue(all(code == 200 for code, _ in results.values()), results)
        self.assertEqual(state["max"], 1)
        self.assertEqual(state["resets"], 8)
        self.assertEqual(state["reset_overlap"], 0)


@unittest.skipIf(os.name == "nt", "AF_UNIX: run this under Linux")
class SocketOwnership(unittest.TestCase):
    """Two brokers must never share a path.

    A second broker that unlinked a live socket would leave the first serving
    an unlinked inode: everything looks up, and nothing is reachable.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "broker.sock")
        self.srv = None

    def tearDown(self):
        if self.srv:
            # No shutdown(): serve_forever was never started, and shutdown()
            # waits for it to return. Closing the listener is what this needs.
            self.srv.server_close()

    def test_second_broker_refuses_a_live_socket(self):
        self.srv = sbx_broker.open_socket(self.path)
        before = os.lstat(self.path).st_ino
        with self.assertRaises(SystemExit) as cm:
            sbx_broker.open_socket(self.path)
        self.assertIn("another broker is listening", str(cm.exception))
        self.assertEqual(os.lstat(self.path).st_ino, before, "the live socket was replaced")

    def test_a_dead_socket_is_reclaimed_not_refused(self):
        self.srv = sbx_broker.open_socket(self.path)
        self.srv.server_close()
        self.srv = None                      # the file is left behind, as after a crash
        self.assertTrue(os.path.exists(self.path))
        self.srv = sbx_broker.open_socket(self.path)
        self.assertTrue(os.path.exists(self.path))

    def test_a_regular_file_is_never_deleted(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not a socket")
        with self.assertRaises(SystemExit) as cm:
            sbx_broker.open_socket(self.path)
        self.assertIn("is not a socket", str(cm.exception))
        self.assertTrue(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
