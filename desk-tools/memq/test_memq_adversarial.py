"""memq with hostile or incomplete input and a misbehaving Loom; its output checked against its own tcs.yaml.

    python3 -B -m unittest discover -s desk-tools/memq
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_memq import Fake, M, MemqTest  # noqa: E402

try:
    import yaml
except ImportError:                     # the schema checks need it; DeskBox itself is Go
    yaml = None


def schema_errors(schema, value, path="$"):
    """The subset of JSON Schema tcs.yaml uses. Returns a list of what does not fit."""
    errs = []
    types = schema.get("type")
    if types:
        names = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list,
                 "object": dict, "null": type(None)}
        allowed = types if isinstance(types, list) else [types]
        ok = any(isinstance(value, names[t]) and not (t in ("integer", "number") and isinstance(value, bool))
                 for t in allowed)
        if not ok:
            return ["%s: %r is not %s" % (path, value, types)]
    if "enum" in schema and value not in schema["enum"]:
        errs.append("%s: %r not in enum" % (path, value))
    if isinstance(value, str):
        if len(value) > schema.get("maxLength", 1 << 60):
            errs.append("%s: longer than %d" % (path, schema["maxLength"]))
        if len(value) < schema.get("minLength", 0):
            errs.append("%s: shorter than %d" % (path, schema["minLength"]))
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", 1 << 60):
            errs.append("%s: more than %d items" % (path, schema["maxItems"]))
        for i, v in enumerate(value):
            errs += schema_errors(schema.get("items", {}), v, "%s[%d]" % (path, i))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                errs.append("%s: missing %s" % (path, k))
        for k, v in value.items():
            if k in props:
                errs += schema_errors(props[k], v, "%s.%s" % (path, k))
            elif schema.get("additionalProperties") is False:
                errs.append("%s: unexpected key %s" % (path, k))
    return errs


def tcs():
    with open(os.path.join(HERE, "tcs.yaml")) as f:
        return yaml.safe_load(f)


class Hostile(MemqTest):
    def raw_desk(self, stdin):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"DESKBOX_TOOL_DIR": self.dir}), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(out):
            self.assertEqual(M.desk(), 0)
        return json.loads(out.getvalue())                 # always one JSON object, never a traceback

    def test_incomplete_or_malformed_input_is_reported_not_raised(self):
        for stdin in ('{"op": "push"}', '{"op": "search"}', '{"op": "search", "query": "x", "limit": "abc"}',
                      "not json", "[]", "null", '{"op": "' + "x" * 500 + '"}', '{"op": ["push"]}', ""):
            with self.subTest(stdin=stdin[:40]):
                out = self.raw_desk(stdin)
                self.assertIs(out["ok"], False)
                self.assertTrue(out["error"])
                self.assertLessEqual(len(out["op"]), 16)

    def test_input_cannot_redirect_the_call_or_bring_its_own_token_or_author(self):
        self.desk({"op": "push", "text": "x", "by": "ryan", "url": "http://evil.example/", "loom": "http://evil/",
                   "token": "forged", "Authorization": "Bearer forged", "headers": {"X": "1"}})
        method, path, auth, body = Fake.seen[-1]
        self.assertEqual((path, auth), ("/proposals", "Bearer tok-memq"))
        self.assertEqual(set(body), {"text", "by", "events", "note"})
        self.assertEqual(body["by"], "agent")

    def test_a_status_cannot_smuggle_a_query_string(self):
        out = self.desk({"op": "list", "status": "pending&status=all"})
        self.assertFalse(out["ok"])
        self.assertEqual(Fake.seen, [])

    def test_reads_never_carry_the_token(self):
        self.desk({"op": "list"})
        self.desk({"op": "search", "query": "x"})
        self.assertEqual([a for _, _, a, _ in Fake.seen], [None, None])

    def test_loom_replies_without_the_expected_fields_are_reported(self):
        for path, reply, args in (("/proposals", {}, {"op": "push", "text": "x"}),
                                  ("/proposals", {"proposals": None}, {"op": "list"}),
                                  ("/proposals/pop", {}, {"op": "pop"}),
                                  ("/memories/search", [], {"op": "search", "query": "q"})):
            with self.subTest(path=path, reply=reply):
                Fake.reply[path] = (200, reply)
                out = self.desk(args)
                self.assertIs(out["ok"], False)

    @unittest.expectedFailure
    def test_a_pushed_secret_does_not_reach_the_queue_verbatim(self):
        """Design open: nothing redacts proposals (loomship redacts events only). Whether memq, Loom or the
        console's review should redact or refuse them is not decided."""
        key = "sk-ant-api03-" + "C" * 90
        self.desk({"op": "push", "text": "the deepseek key is " + key})
        self.assertNotIn(key, json.dumps(Fake.seen))


@unittest.skipIf(yaml is None, "PyYAML not installed")
class OutputSchema(MemqTest):
    """Every reply memq builds from what Loom can send (Loom's own limits) must pass tcs.yaml's output schema,
    or DeskBox refuses the result."""

    def check(self, args):
        out = self.desk(args)
        self.assertEqual(schema_errors(tcs()["output"]["schema"], out), [], args)
        return out

    def test_input_schema_matches_what_run_py_reads(self):
        props = tcs()["input"]["properties"]
        self.assertEqual(set(props), {"op", "text", "events", "note", "status", "query", "limit"})
        self.assertEqual(props["op"]["enum"], ["push", "list", "pop", "search"])
        self.assertEqual(props["status"]["enum"], list(M.STATUSES))

    def test_replies_at_looms_limits_fit(self):
        big = {"id": "f" * 16, "text": "t" * 8000, "status": "accepted", "by": "proposer",
               "events": list(range(1, 201)), "note": "n" * 2000, "created_at": "2026-11-24T10:00:00Z",
               "memory_id": None, "decided_at": "2026-11-24T10:00:00Z", "applied_at": None}
        Fake.reply["/proposals"] = (200, {"id": "f" * 16, "status": "pending", "proposals": [big] * 100})
        Fake.reply["/proposals/pop"] = (200, {"applied": [{"proposal": "f" * 16, "memory": "p-" + "f" * 16,
                                                           "text": "t" * 8000}] * 500})
        Fake.reply["/memories/search"] = (200, {"results": [{"id": "m" * 64, "text": "t" * 8000, "distance": 1.5,
                                                             "version": 9, "updated_at": None}] * 50})
        self.check({"op": "push", "text": "x"})
        self.check({"op": "list", "status": "all"})
        self.check({"op": "pop"})
        self.check({"op": "search", "query": "q", "limit": 50})

    def test_failures_fit(self):
        Fake.reply["/proposals"] = (500, {"error": "e" * 5000})
        out = self.check({"op": "push", "text": "x"})
        self.assertFalse(out["ok"])
        self.check({"op": "nope"})

    @unittest.expectedFailure
    def test_replies_beyond_the_schema_still_fit(self):
        """Design open: if Loom ever sends more than the schema allows (a longer note, more entries), run.py passes it
        through and DeskBox rejects the whole result. Clip, or report ok=false: not decided."""
        Fake.reply["/proposals"] = (200, {"proposals": [{"id": "x", "text": "t", "status": "pending", "by": "agent", "events": [],
                                                         "note": "n" * 3000, "created_at": "t", "memory_id": None}] * 501})
        out = self.check({"op": "list"})
        self.assertTrue(out["ok"])


del MemqTest                                              # its tests run from test_memq.py, not twice

if __name__ == "__main__":
    unittest.main()
