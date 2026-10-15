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
    def test_default_forwards_nothing_and_leaves_core_output_open(self):
        text, w = c.render_ruleset(c.default_policy(), fake_resolve)
        self.assertEqual(text.count("policy drop"), 2)           # input and forward
        out = text.split("chain output")[1].split("}")[0]
        self.assertIn("policy accept", out)                      # the core itself is not locked down
        self.assertIn("# nothing from the sandbox is forwarded", text)
        self.assertNotIn("masquerade", text)
        self.assertEqual(len(w), 1)
        self.assertIn("no sandbox interface", w[0])

    def test_gateway_rules_for_sandbox_traffic(self):
        p = c.validate_policy({"sbx_ifaces": ["eth1"], "dns": ["1.1.1.1"],
                               "allow": [{"host": "openrouter.ai", "port": 443, "comment": "llm api"}],
                               "mgmt": [{"cidr": "192.168.122.0/24", "port": 22}]})
        text, w = c.render_ruleset(p, fake_resolve)
        fwd = text.split("chain forward")[1].split("chain output")[0]
        self.assertIn('iifname { "eth1" } ip daddr { 104.18.2.115, 104.18.3.115 } tcp dport 443 accept comment "llm api"', fwd)
        self.assertIn('iifname { "eth1" } ip6 daddr { 2606:4700::6812:273 } tcp dport 443 accept', fwd)
        self.assertIn('iifname { "eth1" } ip daddr { 1.1.1.1 } udp dport 53 accept', fwd)
        # the sandbox only knows this host: its DNS is redirected to the resolver, and egress is masqueraded
        self.assertIn('iifname { "eth1" } fib daddr type local udp dport 53 dnat ip to 1.1.1.1', text)
        self.assertIn('meta mark %s oifname != { "eth1" } masquerade' % c.SBX_MARK, text)
        self.assertIn("ip saddr { 192.168.122.0/24 } tcp dport 22 accept", text.split("chain forward")[0])
        # allow rules never apply to the core's own traffic: nothing in the output chain
        self.assertNotIn("dport 443", text.split("chain output")[1].split("chain prerouting")[0])
        self.assertEqual(w, [])

    def test_sandbox_interface_validation(self):
        for bad in (["eth1; flush ruleset"], ["a" * 16], [""], ["eth 1"], [5], ["e"] * 5):
            with self.assertRaises(c.Bad, msg=str(bad)):
                c.validate_policy({"sbx_ifaces": bad})
        self.assertEqual(c.validate_policy({"sbx_ifaces": ["eth1", "br-sbx.10"]})["sbx_ifaces"], ["eth1", "br-sbx.10"])

    def test_apply_keeps_interfaces_when_the_caller_omits_them(self):
        saved = {"sbx_ifaces": ["eth1"], "dns": [], "mgmt": [], "allow": []}
        old_load, old_nft = c.load_policy, c._nft
        c.load_policy = lambda: saved
        c._nft = lambda args, stdin=None: (0, "")
        try:
            c._pending.update(timer=None, prev=None, text=None, deadline=0, policy=None)
            c.fw_apply({"dns": [], "mgmt": [], "allow": []})
            self.assertEqual(c._pending["policy"]["sbx_ifaces"], ["eth1"])
            c.fw_cancel()
        finally:
            c.load_policy, c._nft = old_load, old_nft

    def test_allow_renders_both_families(self):
        p = c.validate_policy({"sbx_ifaces": ["eth1"], "dns": ["1.1.1.1"],
                               "allow": [{"host": "openrouter.ai", "port": 443, "comment": "llm api"}]})
        text, w = c.render_ruleset(p, fake_resolve)
        self.assertIn('ip daddr { 104.18.2.115, 104.18.3.115 } tcp dport 443 accept comment "llm api"', text)
        self.assertIn("ip6 daddr { 2606:4700::6812:273 } tcp dport 443 accept", text)
        self.assertEqual(w, [])

    # ---- profiles, block list, instant apply ------------------------------------------------------------
    def test_block_rules_come_first_and_win(self):
        p = c.validate_policy({"sbx_ifaces": ["eth1"], "dns": [], "profiles": {"session": {
            "allow": [{"host": "10.0.0.0/8", "port": 443}],
            "block": [{"host": "10.0.0.5", "proto": "any", "comment": "no way"}, {"host": "10.9.9.9", "proto": "tcp", "port": "22"},
                      {"host": "10.8.8.8", "proto": "udp"}]}}})
        text, w = c.render_ruleset(p, fake_resolve)
        fwd = text.split("chain forward")[1].split("chain output")[0]
        self.assertLess(fwd.index("10.0.0.5/32"), fwd.index("tcp dport 443 accept"))      # block before allow
        self.assertIn('ip daddr { 10.0.0.5/32 } drop comment "no way"', fwd)                 # any protocol, any port
        self.assertIn("ip daddr { 10.9.9.9/32 } tcp dport 22 drop", fwd)
        self.assertIn("ip daddr { 10.8.8.8/32 } meta l4proto udp drop", fwd)                 # whole protocol
        # every drop has its own rate-limited log rule before it, so over-rate packets still hit the drop
        self.assertEqual(fwd.count('limit rate 10/second log prefix "sbxfw-drop: "\n'), 3)
        self.assertEqual(w, [])

    def test_only_the_active_profile_is_rendered(self):
        pol = {"sbx_ifaces": ["eth1"], "profiles": {"session": {"allow": [{"host": "openrouter.ai", "port": 443}]},
                                                    "build": {"allow": [{"host": "10.1.2.3", "port": 443}]}}}
        sess, _ = c.render_ruleset(c.validate_policy(pol), fake_resolve)
        self.assertIn("104.18.2.115", sess)
        self.assertNotIn("10.1.2.3", sess)
        build, _ = c.render_ruleset(c.validate_policy({**pol, "active": "build"}), fake_resolve)
        self.assertIn("10.1.2.3", build)
        self.assertNotIn("104.18.2.115", build)
        self.assertIn("Active profile: build", build)

    def test_old_policy_shape_migrates_to_the_session_profile(self):
        p = c.validate_policy({"dns": ["1.1.1.1"], "allow": [{"host": "openrouter.ai", "port": 443}]})
        self.assertEqual(p["profiles"]["session"]["allow"][0]["host"], "openrouter.ai")
        self.assertEqual((p["active"], p["profiles"]["build"]), ("session", {"allow": [], "block": []}))

    def test_profile_and_block_validation(self):
        bad = [
            {"profiles": {"staging": {}}},
            {"profiles": {"session": {}}, "allow": []},
            {"active": "root"},
            {"active_until": -5},
            {"active_until": True},
            {"block": [{"host": "evil.example.com; drop", "proto": "any"}]},
            {"block": [{"host": "10.0.0.1", "proto": "icmp"}]},
            {"block": [{"host": "10.0.0.1", "proto": "any", "port": 80}]},
            {"allow": [{"host": "10.0.0.1", "proto": "any", "port": 80}]},          # allow rules never take proto any
            {"allow": [{"host": "10.0.0.1"}]},                                       # allow rules need a port
            {"block": [{"host": "10.0.0.1", "comment": 'x" drop #'}]},
        ]
        for p in bad:
            with self.assertRaises(c.Bad, msg=str(p)):
                c.validate_policy(p)
        self.assertEqual(c.validate_policy({"active": "session", "active_until": 99})["active_until"], 0)   # only build expires

    def _with_fake_nft(self, fn):
        old = (c._nft, c.ETC)
        c.ETC = tempfile.mkdtemp()
        calls = []
        c._nft = lambda args, stdin=None: (calls.append((args, stdin)) or (0, ""))
        c._pending.update(timer=None, prev=None, text=None, deadline=0, policy=None)
        try:
            return fn(calls)
        finally:
            c._nft, c.ETC = old
            if c._pending["timer"]:
                c._pending["timer"].cancel()
            c._pending.update(timer=None, prev=None, text=None, deadline=0, policy=None)

    def test_instant_apply_saves_at_once_and_starts_no_timer(self):
        def run(calls):
            base = c.validate_policy({"sbx_ifaces": ["eth1"]})
            c._save(c.render_ruleset(base)[0], base)
            r = c.fw_apply({"profiles": {"session": {"allow": [{"host": "1.2.3.4", "port": 443}]}}}, instant=True)
            self.assertEqual((r["applied"], r["pending"]), (True, False))
            self.assertIsNone(c._pending["timer"])
            self.assertEqual(c.load_policy()["profiles"]["session"]["allow"][0]["host"], "1.2.3.4/32")
            self.assertIn("1.2.3.4", open(os.path.join(c.ETC, "ruleset.nft")).read())
        self._with_fake_nft(run)

    def test_instant_refuses_anything_that_could_lock_you_out(self):
        def run(calls):
            base = c.validate_policy({"sbx_ifaces": ["eth1"]})
            c._save(c.render_ruleset(base)[0], base)
            with self.assertRaises(c.Bad):                                 # management access changed
                c.fw_apply({"mgmt": [{"cidr": "10.0.0.0/8", "port": 22}]}, instant=True)
            with self.assertRaises(c.Bad):                                 # sandbox interface changed
                c.fw_apply({"sbx_ifaces": ["eth2"]}, instant=True)
            c.fw_apply({"mgmt": [{"cidr": "10.0.0.0/8", "port": 22}]})     # the confirmed path still works
            with self.assertRaises(c.Bad):                                 # and blocks instant edits while it is pending
                c.fw_apply({"mgmt": [{"cidr": "10.0.0.0/8", "port": 22}]}, instant=True)
        self._with_fake_nft(run)

    def test_profile_switch_is_instant_and_build_expires(self):
        def run(calls):
            base = c.validate_policy({"sbx_ifaces": ["eth1"], "profiles": {"build": {"allow": [{"host": "1.2.3.4", "port": 443}]}}})
            c._save(c.render_ruleset(base)[0], base)
            c.fw_set_profile("build", 600)
            p = c.load_policy()
            self.assertEqual(p["active"], "build")
            self.assertAlmostEqual(p["active_until"], __import__("time").time() + 600, delta=5)
            self.assertIn("1.2.3.4", open(os.path.join(c.ETC, "ruleset.nft")).read())
            self.assertFalse(c.fw_check_expiry())                          # not yet
            p["active_until"] = 1                                          # the window ran out
            c._save(c.render_ruleset(p)[0], p)
            self.assertTrue(c.fw_check_expiry())
            back = c.load_policy()
            self.assertEqual((back["active"], back["active_until"]), ("session", 0))
            self.assertNotIn("1.2.3.4", open(os.path.join(c.ETC, "ruleset.nft")).read())
            with self.assertRaises(c.Bad):
                c.fw_set_profile("build", 5)                               # under the minimum
            with self.assertRaises(c.Bad):
                c.fw_set_profile("nonsense")
            c.fw_set_profile("build", 0)                                   # no time limit
            self.assertEqual(c.load_policy()["active_until"], 0)
            self.assertFalse(c.fw_check_expiry())
        self._with_fake_nft(run)

    def test_unresolvable_host_is_skipped_with_warning(self):
        p = c.validate_policy({"sbx_ifaces": ["eth1"], "allow": [{"host": "nope.example.com", "port": 443}]})
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
        p = c.validate_policy({"sbx_ifaces": ["eth1"], "allow": [{"host": "10.0.0.0/24", "port": "8000-8010", "proto": "udp"}]})
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
