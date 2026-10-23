"""The SBX_GATE format sbx-fetch-gate accepts (the regex is read from the script): python -B image/test_fetch_gate.py"""
import os
import re
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sbx-fetch-gate.sh")
PATTERN = re.search(r're\.fullmatch\(r"([^"]+)"', open(SCRIPT, encoding="utf8").read()).group(1)


def ok(v):
    return re.fullmatch(PATTERN, v.strip()) is not None


class Format(unittest.TestCase):
    def test_accepted(self):
        self.assertTrue(ok("http://192.168.1.199:9011 " + "a" * 48))
        self.assertTrue(ok("http://win11.lan:9011 " + "Ab0-_" * 8))             # URL-safe base64
        self.assertTrue(ok("http://h:1 " + "x" * 32 + "\n"))
        self.assertTrue(ok("http://h:1 " + "x" * 128))

    def test_refused(self):
        for v in ("", "SBX_GATE", "http://h:9011", "http://h:9011 " + "a" * 31, "http://h:9011 " + "a" * 129,
                  "https://h:9011 " + "a" * 40, "http://h " + "a" * 40, "http://h:123456 " + "a" * 40,
                  "http://u@h:9011 " + "a" * 40, "http://h:9011 " + "a" * 20 + " " + "a" * 20,
                  "http://h:9011  " + "a" * 40, "http://h:9011 " + "a" * 40 + " extra"):
            self.assertFalse(ok(v), repr(v))


if __name__ == "__main__":
    unittest.main()
