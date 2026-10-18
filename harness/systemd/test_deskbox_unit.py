#!/usr/bin/env python3
"""Two things the first real boot found wrong with the desk's start-up.

Run from the repo root:  python3 harness/systemd/test_deskbox_unit.py
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class DeskStartup(unittest.TestCase):
    def test_the_unit_allows_netlink_because_bwrap_needs_it_for_loopback(self):
        m = re.search(r"^RestrictAddressFamilies=(.*)$", read("harness", "systemd", "deskbox.service"), re.M)
        self.assertTrue(m, "deskbox.service no longer restricts address families")
        self.assertIn("AF_NETLINK", m.group(1).split())

    def test_the_bus_wait_tests_a_path_not_the_address_string(self):
        # -S on "unix:path=/run/user/N/bus" is never true, so the loop always ran its full 60 s.
        text = read("image", "deskbox-run.sh")
        self.assertNotRegex(text, r'-S\s+"\$DBUS_SESSION_BUS_ADDRESS"')
        self.assertRegex(text, r'-S\s+"\$bus"')


if __name__ == "__main__":
    unittest.main()
