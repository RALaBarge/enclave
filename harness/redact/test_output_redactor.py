"""Tests for output_redactor on the credential shapes Enclave handles. Run: python test_output_redactor.py"""
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from output_redactor import OutputRedactor, StreamingRedactor  # noqa: E402

CFG = {"security": {"output_redaction": {"enabled": True}}}
# Fake values built at runtime so no real-looking secret sits in the source.
OR_KEY = "sk-or-v1-" + "a1b2c3d4e5" * 6
GH = "ghp_" + "Ab3dE5gH7j" * 4
BWS = "0." + "11111111-2222-3333-4444-555555555555" + "." + "Zk39QmWp27LxTb84NcVa" + ":" + "Qm4ZcVb8NxLk2WpT7yRs9A=="


class Redact(unittest.TestCase):
    def setUp(self):
        self.r = OutputRedactor(CFG)

    def gone(self, secret, text=None):
        out = self.r.redact(text or f"here is the key {secret} ok")
        self.assertNotIn(secret, out.text)
        self.assertTrue(out.redacted)
        return out

    def test_openrouter_key(self):
        self.gone(OR_KEY)

    def test_github_token(self):
        self.gone(GH)

    def test_base64_wrapped_key(self):
        self.gone(base64.b64encode(OR_KEY.encode()).decode())

    def test_unicode_confusable_key(self):
        self.gone("sk-or-v1-" + "a1b2c3d4e5" * 6, "key: ѕk-or-v1-" + "a1b2c3d4e5" * 6)

    def test_bitwarden_access_token_shape(self):
        out = self.r.redact(f"BWS_ACCESS_TOKEN={BWS}")
        self.assertNotIn("Qm4ZcVb8NxLk2WpT7yRs9A", out.text)

    def test_plain_text_is_untouched(self):
        s = "the firewall console applies a profile and the bridge starts a session"
        out = self.r.redact(s)
        self.assertEqual(out.text, s)
        self.assertFalse(out.redacted)

    def test_streaming_catches_a_key_split_across_chunks(self):
        s = StreamingRedactor(self.r)
        text = "prefix " + OR_KEY + " suffix " + "x" * 600
        parts = [text[i:i + 17] for i in range(0, len(text), 17)]
        out = "".join(s.feed(p) for p in parts) + s.finalize()
        self.assertNotIn(OR_KEY, out)
        self.assertNotIn(OR_KEY[:30], out)

    def test_disabled_by_default(self):
        self.assertEqual(OutputRedactor({}).redact(OR_KEY).text, OR_KEY)


if __name__ == "__main__":
    unittest.main()
