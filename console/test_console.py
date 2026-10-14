"""Run: python3 console/test_console.py   (stdlib unittest; no root needed)"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

TMP = tempfile.mkdtemp()
os.environ.update(
    SBXC_ETC=os.path.join(TMP, "etc"), SBX_TOOLLIST=os.path.join(TMP, "sbx", "tools.list"),
    SBXC_BROKER_ENV=os.path.join(TMP, "sbx", "broker.env"), SBXC_SBX_TOOLS=os.path.join(TMP, "sbxtools"),
    SBXC_HARNESS_TOOLS=os.path.join(TMP, "htools"), SBXC_PORT="0")
os.makedirs(os.environ["SBXC_ETC"])
os.makedirs(os.path.dirname(os.environ["SBX_TOOLLIST"]))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbx_console as c  # noqa: E402

FAKE = {"openrouter.ai": (["104.18.2.115", "104.18.3.115"], ["2606:4700::6812:273"]), "1.2.3.4": (["1.2.3.4"], [])}


def fake_resolve(h):
    if h in FAKE:
        return FAKE[h]
    if h.startswith("10."):
        return [h], []
    return [], []


class Firewall(unittest.TestCase):
    def test_default_is_deny_everything(self):
        text, w = c.render_ruleset(c.default_policy(), fake_resolve)
        self.assertEqual(text.count("policy drop"), 3)
        self.assertIn("# no egress allowed", text)
        self.assertNotIn(" accept\n    limit", text.split("chain output")[1].split("oif lo accept")[0])

    def test_allow_renders_both_families(self):
        p = c.validate_policy({"dns": ["1.1.1.1"], "allow": [{"host": "openrouter.ai", "port": 443, "comment": "llm api"}],
                               "mgmt": [{"cidr": "192.168.122.0/24", "port": 22}]})
        text, w = c.render_ruleset(p, fake_resolve)
        self.assertIn('ip daddr { 104.18.2.115, 104.18.3.115 } tcp dport 443 accept comment "llm api"', text)
        self.assertIn("ip6 daddr { 2606:4700::6812:273 } tcp dport 443 accept", text)
        self.assertIn("ip daddr { 1.1.1.1 } udp dport 53 accept", text)
        self.assertIn("ip saddr { 192.168.122.0/24 } tcp dport 22 accept", text)
        self.assertEqual(w, [])

    def test_unresolvable_host_is_skipped_with_warning(self):
        p = c.validate_policy({"allow": [{"host": "nope.example.com", "port": 443}]})
        text, w = c.render_ruleset(p, fake_resolve)
        self.assertNotIn("nope", text)
        self.assertEqual(len(w), 1)

    def test_validation_blocks_injection(self):
        bad = [
            {"allow": [{"host": "a.com; flush ruleset", "port": 1}]},
            {"allow": [{"host": "openrouter.ai", "port": "443 accept; drop"}]},
            {"allow": [{"host": "openrouter.ai", "port": 443, "comment": 'x" accept #'}]},
            {"allow": [{"host": "openrouter.ai", "port": 443, "proto": "icmp"}]},
            {"allow": [{"host": "openrouter.ai", "port": 70000}]},
            {"allow": [{"host": "openrouter.ai", "port": "9-2"}]},
            {"dns": ["10.0.0.0/8"]},
            {"mgmt": [{"cidr": "any", "port": 22}]},
            {"allow": [{"host": "localhost", "port": 1}]},
        ]
        for p in bad:
            with self.assertRaises(c.Bad, msg=str(p)):
                c.validate_policy(p)

    def test_port_range_and_cidr_ok(self):
        p = c.validate_policy({"allow": [{"host": "10.0.0.0/24", "port": "8000-8010", "proto": "udp"}]})
        text, _ = c.render_ruleset(p, fake_resolve)
        self.assertIn("udp dport 8000-8010 accept", text)

    def test_cancel_and_confirm_need_pending(self):
        for fn in (c.fw_cancel, c.fw_confirm):
            with self.assertRaises(c.Bad):
                fn()

    def test_parse_drops(self):
        lines = [
            "sbxfw-drop: IN= OUT=eth0 SRC=10.0.2.15 DST=93.184.216.34 LEN=60 PROTO=TCP SPT=40000 DPT=443 WINDOW=1",
            "sbxfw-drop: IN= OUT=eth0 SRC=10.0.2.15 DST=93.184.216.34 LEN=60 PROTO=TCP SPT=40001 DPT=443 WINDOW=1",
            "sbxfw-drop: IN=eth0 OUT= SRC=10.0.2.2 DST=10.0.2.15 LEN=60 PROTO=TCP SPT=5 DPT=22",
            "sbxfw-drop: IN= OUT=eth0 SRC=10.0.2.15 DST=9.9.9.9 LEN=60 PROTO=UDP SPT=1 DPT=53",
            "unrelated line",
        ]
        rows = c.parse_drops(lines)
        self.assertEqual(rows[0], {"dir": "out", "peer": "93.184.216.34", "proto": "tcp", "dport": "443", "count": 2})
        self.assertEqual(rows[-1]["dir"], "in")
        self.assertEqual(len(rows), 3)


class Tools(unittest.TestCase):
    def setUp(self):
        with open(c.TOOLLIST, "w") as f:
            f.write("# header\ngreet 10   # note\nother\n")

    def test_parse_toggle_timeout_roundtrip(self):
        self.assertEqual([(t["name"], t["enabled"], t["timeout_s"]) for t in c.parse_tools()], [("greet", True, 10), ("other", True, 30)])
        c.tool_toggle("greet", False)
        self.assertIn("# off: greet 10", open(c.TOOLLIST).read())
        self.assertEqual(c.parse_tools()[0]["enabled"], False)
        c.tool_timeout("greet", 45)
        self.assertIn("# off: greet 45", open(c.TOOLLIST).read())
        c.tool_toggle("greet", True)
        text = open(c.TOOLLIST).read()
        self.assertIn("greet 45\n", text)
        self.assertIn("# header", text)
        c.c = None
        import toollist
        self.assertEqual(toollist.load(c.TOOLLIST)["greet"]["timeout_s"], 45)   # broker still parses it

    def test_disabled_tool_invisible_to_broker(self):
        import toollist
        c.tool_toggle("other", False)
        self.assertNotIn("other", toollist.load(c.TOOLLIST))

    def test_rejects(self):
        with self.assertRaises(c.Bad):
            c.tool_toggle("nope", True)
        with self.assertRaises(c.Bad):
            c.tool_timeout("greet", 999)

    def test_status_shows_pin_hash(self):
        os.makedirs(os.path.join(c.SBX_TOOLS, "greet"), exist_ok=True)
        os.makedirs(os.path.join(c.HARNESS_TOOLS, "greet-sbx"), exist_ok=True)
        open(os.path.join(c.SBX_TOOLS, "greet", "run"), "w").write("#!/bin/sh\n")
        open(os.path.join(c.HARNESS_TOOLS, "greet-sbx", "tcs.yaml"), "w").write("name: greet-sbx\n")
        open(os.path.join(c.HARNESS_TOOLS, "greet-sbx", "run.py"), "w").write("stub")
        s = c.tool_status(c.parse_tools()[0])
        self.assertTrue(s["ok"])
        self.assertEqual(len(s["stub_sha256"]), 64)


class Settings(unittest.TestCase):
    def test_set_and_preserve(self):
        open(c.BROKER_ENV, "w").write("# keep me\nSBX_HOST=10.77.0.2\nSBX_KEY=/etc/sbx/id_ed25519\n")
        r = c.settings_set({"broker": {"SBX_HOST": "127.0.0.1", "SBX_PORT": "2222", "SBX_RESET_CMD": ""}, "mode": "full"})
        self.assertTrue(r["restart_required"])
        t = open(c.BROKER_ENV).read()
        self.assertIn("# keep me", t)
        self.assertIn("SBX_KEY=/etc/sbx/id_ed25519", t)
        self.assertIn("SBX_HOST=127.0.0.1", t)
        self.assertIn("SBX_PORT=2222", t)
        self.assertEqual(c.get_mode(), "full")

    def test_refuses_dangerous(self):
        for body in ({"broker": {"SBX_KEY": "/etc/shadow"}}, {"broker": {"SBX_RESET_CMD": "rm -rf /"}},
                     {"broker": {"SBX_HOST": "a b"}}, {"broker": {"SBX_PORT": "0"}},
                     {"broker": {"SBX_USER": "root; id"}}, {"mode": "weird"}):
            with self.assertRaises(c.Bad, msg=str(body)):
                c.settings_set(body)


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open(os.path.join(c.ETC, "token"), "w").write("tok123\n")
        c.Handler.token = c.load_token()
        cls.srv = c.ThreadingHTTPServer(("127.0.0.1", 0), c.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, method, path, body=None, headers=None, host=None):
        h = http.client.HTTPConnection("127.0.0.1", self.port)
        hd = {"Host": host or f"127.0.0.1:{self.port}"}
        hd.update(headers or {})
        h.request(method, path, body=body, headers=hd)
        r = h.getresponse()
        return r.status, r.read()

    def test_auth_and_host(self):
        self.assertEqual(self.req("GET", "/api/tools")[0], 401)
        self.assertEqual(self.req("GET", "/api/tools", headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.req("GET", "/api/tools", headers={"Authorization": "Bearer tok123"}, host="evil.example.com")[0], 403)
        open(c.TOOLLIST, "w").write("greet 10\n")
        code, body = self.req("GET", "/api/tools", headers={"Authorization": "Bearer tok123"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["tools"][0]["name"], "greet")

    def test_post_needs_json_type_and_size_cap(self):
        a = {"Authorization": "Bearer tok123"}
        self.assertEqual(self.req("POST", "/api/settings", "{}", {**a, "Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req("POST", "/api/settings", "x" * 10, {**a, "Content-Type": "application/json", "Content-Length": str(c.MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.req("POST", "/api/settings", '{"mode":"nope"}', {**a, "Content-Type": "application/json"})[0], 400)

    def test_ui_served_without_token(self):
        code, body = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"SBX Console", body)

    def test_no_tool_install_routes(self):
        self.assertEqual(self.req("POST", "/api/tools/stage", "{}", {"Authorization": "Bearer tok123", "Content-Type": "application/json"})[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=1)
