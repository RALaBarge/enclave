"""Tests for the desk -> harness tool adapter.

The fixture is a stand-in for agent-desk: it serves `GET /tools`, `GET
/tools/{name}` and `POST /tools/{name}` on loopback, so these tests need no
running desk and no network. The shapes it returns follow the handlers in
DeskBox at DESKBOX_COMMIT (cmd/agent-desk/main.go), including the fact that
`GET /tools` carries no schemas and the contract lives at `GET /tools/{name}`.
"""
import io
import json
import os
import socket
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import desk_tools as DT  # noqa: E402

GREET = {
    "name": "greet-sbx",
    "summary": "greet-python, but executed on the sandbox box via sbx-broker.\n",
    "input": {
        "type": "object",
        "additionalProperties": False,
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "excited": {"type": "boolean"},
        },
    },
    "output": {
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["greeting", "length"],
            "properties": {"greeting": {"type": "string", "maxLength": 400}, "length": {"type": "integer"}},
        }
    },
    "allowed_side_effects": {"files": [], "network": False},
    "execution": {"mode": "queued", "max_retries": 0, "timeout_ms": 150000},
}


class Desk:
    """The three routes the adapter uses, with overridable responses."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.tools = {}
        self.submit = None
        self.routes = {}
        self.requests = []

    def serve(self, name, contract):
        self.tools[name] = contract
        self.routes[("GET", "/tools/" + urllib.parse.quote(name, safe=""))] = (200, contract)

    def respond(self, method, path):
        if (method, path) in self.routes:
            return self.routes[(method, path)]
        if method == "GET" and path == "/tools":
            return 200, [
                {
                    "name": name,
                    "summary": contract.get("summary", ""),
                    "execution_mode": "queued",
                    "max_retries": 0,
                    "timeout_ms": 150000,
                    "allowed_side_effects": contract.get("allowed_side_effects", {"files": [], "network": False}),
                }
                for name, contract in sorted(self.tools.items())
            ]
        if method == "POST" and path.startswith("/tools/"):
            if self.submit is not None:
                value = self.submit
                return value() if callable(value) else value
            return 404, {"error": "tool not found: " + path[len("/tools/"):]}
        return 404, {"error": "not found"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        desk = self.server.desk
        split = urllib.parse.urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        desk.requests.append(
            {
                "method": method,
                "path": split.path,
                "query": split.query,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        status, payload = desk.respond(method, split.path)
        if isinstance(payload, bytes):
            raw = payload
        elif isinstance(payload, str):
            raw = payload.encode()
        else:
            raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def unused_url():
    """A loopback URL on a port that was just closed, so connects are refused."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()
    return "http://127.0.0.1:%d" % port


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.desk = Desk()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.desk = self.desk
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    # -- fetch -----------------------------------------------------------

    def test_fetch_normalises_the_list_and_contract(self):
        self.desk.serve("greet-sbx", GREET)
        tools = DT.fetch(self.base)
        self.assertEqual(
            tools,
            [
                {
                    "name": "greet-sbx",
                    "summary": GREET["summary"],
                    "input_schema": GREET["input"],
                    "output_schema": GREET["output"]["schema"],
                    "invoke": {"method": "POST", "path": "/tools/greet-sbx"},
                }
            ],
        )
        self.assertEqual([r["path"] for r in self.desk.requests], ["/tools", "/tools/greet-sbx"])

    def test_fetch_reads_each_detail_and_sorts_by_name(self):
        self.desk.serve("zeta", {"name": "zeta"})
        self.desk.serve("alpha", {"name": "alpha"})
        # The desk happens to sort its list today; the adapter does not rely on
        # it, because a stable order keeps a prompt listing from churning.
        entries = [
            {"name": "zeta", "summary": ""},
            {"name": "alpha", "summary": ""},
        ]
        self.desk.routes[("GET", "/tools")] = (200, entries)
        tools = DT.fetch(self.base)
        self.assertEqual([tool["name"] for tool in tools], ["alpha", "zeta"])
        self.assertEqual(
            [r["path"] for r in self.desk.requests],
            ["/tools", "/tools/zeta", "/tools/alpha"],
        )

    def test_fetch_missing_input_schema_becomes_valid_empty_object(self):
        cases = {
            "missing": {"name": "no-input"},
            "null": {"name": "no-input", "input": None},
            "empty": {"name": "no-input", "input": {}},
        }
        for label, contract in cases.items():
            with self.subTest(label):
                self.desk.reset()
                self.desk.serve("no-input", contract)
                tool = DT.fetch(self.base)[0]
                self.assertEqual(tool["input_schema"], {"type": "object", "properties": {}})
                self.assertEqual(tool["output_schema"], {})

    def test_fetch_gives_each_tool_its_own_empty_schema(self):
        self.desk.serve("a", {"name": "a"})
        self.desk.serve("b", {"name": "b"})
        tools = DT.fetch(self.base)
        self.assertEqual([tool["input_schema"] for tool in tools], [DT.EMPTY_OBJECT_SCHEMA] * 2)
        self.assertIsNot(tools[0]["input_schema"], tools[1]["input_schema"])

    def test_fetch_fills_an_absent_input_type_with_object(self):
        contract = {
            "name": "typeless",
            "input": {"properties": {"x": {"type": "string"}}, "required": ["x"]},
        }
        self.desk.serve("typeless", contract)
        schema = DT.fetch(self.base)[0]["input_schema"]
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["properties"], {"x": {"type": "string"}})
        self.assertNotIn("type", contract["input"])  # the contract is not rewritten

    def test_fetch_rejects_input_schemas_it_cannot_express(self):
        cases = {
            "array type": {"type": "array", "items": {"type": "string"}},
            "properties not an object": {"type": "object", "properties": ["name"]},
            "required not a string list": {"type": "object", "required": "name"},
        }
        for label, schema in cases.items():
            with self.subTest(label):
                self.desk.reset()
                self.desk.serve("bad", {"name": "bad", "input": schema})
                with self.assertRaises(DT.DeskDataError) as ctx:
                    DT.fetch(self.base)
                self.assertIn("/tools/bad", str(ctx.exception))

    def test_fetch_rejects_malformed_detail(self):
        self.desk.serve("broken", {"name": "broken"})
        self.desk.routes[("GET", "/tools/broken")] = (200, b"<html>not json")
        with self.assertRaises(DT.DeskDataError) as ctx:
            DT.fetch(self.base)
        self.assertIn("/tools/broken", str(ctx.exception))
        self.assertIn("not JSON", str(ctx.exception))

    def test_fetch_reports_the_detail_url_on_http_error(self):
        self.desk.serve("gone", {"name": "gone"})
        self.desk.routes[("GET", "/tools/gone")] = (404, {"error": "tool not found"})
        with self.assertRaises(DT.DeskError) as ctx:
            DT.fetch(self.base)
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertIn("/tools/gone", str(ctx.exception))

    def test_fetch_rejects_a_list_that_is_not_an_array(self):
        self.desk.routes[("GET", "/tools")] = (200, {"tools": []})
        with self.assertRaises(DT.DeskDataError) as ctx:
            DT.fetch(self.base)
        self.assertIn(self.base + "/tools", str(ctx.exception))

    def test_fetch_rejects_duplicate_names(self):
        self.desk.serve("a", {"name": "a"})
        entry = {"name": "a", "summary": ""}
        self.desk.routes[("GET", "/tools")] = (200, [entry, entry])
        with self.assertRaises(DT.DeskDataError) as ctx:
            DT.fetch(self.base)
        self.assertIn("twice", str(ctx.exception))

    def test_fetch_rejects_a_contract_name_mismatch(self):
        self.desk.serve("real", {"name": "other"})
        with self.assertRaises(DT.DeskDataError) as ctx:
            DT.fetch(self.base)
        self.assertIn("other", str(ctx.exception))
        self.assertIn("real", str(ctx.exception))

    def test_fetch_rejects_a_bad_output_section(self):
        cases = {
            "output not an object": {"name": "bad", "output": []},
            "schema not an object": {"name": "bad", "output": {"schema": []}},
        }
        for label, contract in cases.items():
            with self.subTest(label):
                self.desk.reset()
                self.desk.serve("bad", contract)
                with self.assertRaises(DT.DeskDataError) as ctx:
                    DT.fetch(self.base)
                self.assertIn("/tools/bad", str(ctx.exception))

    # -- call ------------------------------------------------------------

    def test_call_posts_input_with_wait_and_returns_the_result(self):
        self.desk.serve("greet-sbx", GREET)
        self.desk.submit = (200, {"id": "job-1", "status": "done", "result": {"greeting": "hi", "length": 2}})
        result = DT.call(self.base, "greet-sbx", {"name": "Ada"})
        self.assertEqual(result, {"greeting": "hi", "length": 2})
        request = self.desk.requests[-1]
        self.assertEqual((request["method"], request["path"]), ("POST", "/tools/greet-sbx"))
        self.assertEqual(urllib.parse.parse_qs(request["query"]), {"wait": ["45s"]})
        self.assertEqual(request["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(request["body"]), {"input": {"name": "Ada"}})

    def test_call_without_wait_does_not_block(self):
        self.desk.submit = (200, {"id": "job-1", "status": "done", "result": None})
        DT.call(self.base, "greet-sbx", {}, wait="")
        self.assertEqual(self.desk.requests[-1]["query"], "")

    def test_call_http_error_carries_the_desk_error(self):
        violations = ["$.name: expected string, got number"]
        self.desk.submit = (
            400,
            {"error": "contract violation: input does not conform to the tool contract", "violations": violations},
        )
        with self.assertRaises(DT.DeskError) as ctx:
            DT.call(self.base, "greet-sbx", {"name": 3})
        message = str(ctx.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("contract violation", message)
        self.assertIn(violations[0], message)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.payload["violations"], violations)

    def test_call_failed_job_raises_with_the_desk_error(self):
        self.desk.submit = (200, {"id": "job-9", "status": "failed", "error": "sandbox exited 1"})
        with self.assertRaises(DT.DeskError) as ctx:
            DT.call(self.base, "greet-sbx", {"name": "Ada"})
        self.assertIn("job-9", str(ctx.exception))
        self.assertIn("sandbox exited 1", str(ctx.exception))

    def test_call_job_still_queued_after_wait_raises(self):
        self.desk.submit = (202, {"id": "job-2", "status": "queued"})
        with self.assertRaises(DT.DeskError) as ctx:
            DT.call(self.base, "greet-sbx", {"name": "Ada"}, wait="1s")
        message = str(ctx.exception)
        self.assertIn("job-2", message)
        self.assertIn("still queued", message)
        self.assertIn("GET /jobs/job-2", message)

    def test_call_done_without_a_result_returns_none(self):
        self.desk.submit = (200, {"id": "job-4", "status": "done"})
        self.assertIsNone(DT.call(self.base, "greet-sbx", {}))

    def test_call_rejects_non_object_arguments_without_asking_the_desk(self):
        with self.assertRaises(ValueError):
            DT.call(self.base, "greet-sbx", ["name"])
        self.assertEqual(self.desk.requests, [])

    def test_connection_refused_is_a_named_error_with_the_url(self):
        url = unused_url()
        with self.assertRaises(DT.DeskUnreachable) as ctx:
            DT.fetch(url, timeout=1)
        self.assertIn(url, str(ctx.exception))
        with self.assertRaises(DT.DeskUnreachable) as ctx:
            DT.call(url, "greet-sbx", {"name": "Ada"}, wait="1s")
        self.assertIn(url, str(ctx.exception))

    # -- emitted shapes --------------------------------------------------

    def test_as_openapi_functions(self):
        self.desk.serve("greet-sbx", GREET)
        self.assertEqual(
            DT.as_openapi_functions(DT.fetch(self.base)),
            [
                {
                    "type": "function",
                    "function": {
                        "name": "greet-sbx",
                        "description": GREET["summary"],
                        "parameters": GREET["input"],
                    },
                }
            ],
        )

    def test_as_openapi_rejects_a_name_it_cannot_express(self):
        self.desk.serve("greet.sbx", {"name": "greet.sbx"})
        with self.assertRaises(DT.DeskDataError) as ctx:
            DT.as_openapi_functions(DT.fetch(self.base))
        self.assertIn("greet.sbx", str(ctx.exception))

    def test_as_text_lists_name_summary_and_schema(self):
        self.desk.serve("greet-sbx", GREET)
        text = DT.as_text(DT.fetch(self.base))
        self.assertTrue(text.startswith("Tools (executed by the desk; one tool per call):"))
        self.assertIn("greet-sbx: greet-python, but executed on the sandbox box via sbx-broker.", text)
        self.assertIn(json.dumps(GREET["input"], sort_keys=True), text)
        self.assertEqual(DT.as_text([]), "Tools: none.\n")

    def test_cli_emits_each_mode(self):
        self.desk.serve("greet-sbx", GREET)
        for mode in ("list", "openapi", "text"):
            with self.subTest(mode):
                out, err = io.StringIO(), io.StringIO()
                self.assertEqual(DT.main(["--desk", self.base, mode], out=out, err=err), 0)
                self.assertEqual(err.getvalue(), "")
                if mode == "list":
                    self.assertEqual(json.loads(out.getvalue())[0]["name"], "greet-sbx")
                elif mode == "openapi":
                    self.assertEqual(json.loads(out.getvalue())[0]["type"], "function")
                else:
                    self.assertIn("greet-sbx:", out.getvalue())

    def test_cli_reports_an_unreachable_desk(self):
        url = unused_url()
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(DT.main(["--desk", url, "--timeout", "1", "list"], out=out, err=err), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(url, err.getvalue())


if __name__ == "__main__":
    unittest.main()
