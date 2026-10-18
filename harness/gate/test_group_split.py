#!/usr/bin/env python3
"""The gate's two groups must stay two groups.

The token group (sbxgate) may hold only the gate; the socket group (sbxgatecall)
holds the bridge. These read the unit files, the provision script and the
gate's default, so a rename or a merge of the two groups fails here before it
reaches a VM.

Run from the repo root:  python3 harness/gate/test_group_split.py
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def unit_groups(text):
    """Groups named by Group= and SupplementaryGroups= (uncommented lines only)."""
    out = {"Group": [], "SupplementaryGroups": []}
    for line in text.splitlines():
        m = re.match(r"^(Group|SupplementaryGroups)=(.*)$", line.strip())
        if m:
            out[m.group(1)] += m.group(2).split()
    return out


class GroupSplit(unittest.TestCase):
    def setUp(self):
        self.bridge = unit_groups(read("harness", "systemd", "pi-bridge.service"))
        self.gate = unit_groups(read("harness", "systemd", "sbx-session-gate.service"))
        self.gate_py = read("harness", "gate", "session_gate.py")
        self.provision = read("image", "provision.sh")

    def socket_group(self):
        m = re.search(r'SBX_GATE_SOCKET_GROUP",\s*"([^"]+)"', self.gate_py)
        self.assertTrue(m, "session_gate.py no longer names a default socket group")
        return m.group(1)

    def test_the_bridge_is_not_in_the_token_group(self):
        # The token file is chgrp'd to this group in provision.sh.
        m = re.search(r"chgrp (\S+) /etc/sbxfw/tighten-token", self.provision)
        self.assertTrue(m, "provision.sh no longer sets the tighten token's group")
        token_group = m.group(1)
        self.assertNotIn(token_group, self.bridge["SupplementaryGroups"] + self.bridge["Group"])

    def test_the_socket_group_is_not_the_token_group(self):
        m = re.search(r"chgrp (\S+) /etc/sbxfw/tighten-token", self.provision)
        self.assertNotEqual(self.socket_group(), m.group(1))

    def test_the_bridge_is_in_the_socket_group(self):
        self.assertIn(self.socket_group(), self.bridge["SupplementaryGroups"])

    def test_the_gate_is_in_the_socket_group_so_it_may_chgrp_to_it(self):
        self.assertIn(self.socket_group(), self.gate["SupplementaryGroups"] + self.gate["Group"])

    def test_the_gate_runs_in_the_token_group(self):
        m = re.search(r"chgrp (\S+) /etc/sbxfw/tighten-token", self.provision)
        self.assertEqual(self.gate["Group"], [m.group(1)])

    def test_provision_creates_the_socket_group(self):
        self.assertIn("groupadd -r " + self.socket_group(), self.provision)


if __name__ == "__main__":
    unittest.main()
