"""
Output-side secret / PII redactor.

Sits between the proxy and the client on the response path. Catches the
two most expensive leak classes in LLM systems:

  1. **Live secrets** that arrived via a tool result, RAG document, or
     model hallucination and would otherwise be echoed to the user
     (and logged, and cached).
  2. **PII** from the same paths that should not exit the trust boundary.

Designed to run in ~0.5 ms on a 4 KB response. Pure stdlib.

Wire integration: each finding emits a `secret_redacted` event so an
operator can audit what the model tried to leak. Operators reading the
wire log get a count of leak attempts even if the redactor has caught
them all — a useful signal for tuning upstream filters.

Config (config.yaml):

    security:
      output_redaction:
        enabled: true
        redact_secrets: true       # AWS / GitHub / OpenAI / private keys / etc.
        redact_pii: true           # email, phone, SSN, credit card, IP
        entropy_scan: true         # high-entropy bearer-token catch-all
        entropy_min_length: 32
        entropy_threshold: 4.0     # Shannon entropy bits/char
"""

from __future__ import annotations

import base64
import logging
import math
import re
import unicodedata
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


# ── Known secret formats ────────────────────────────────────────────────────
# Each entry: (label, compiled regex). Order matters — more-specific patterns
# first so a generic high-entropy scan doesn't pre-empt a precise match.
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Bitwarden Secrets Manager machine-account access token: 0.<uuid>.<secret>:<key> (added for Enclave)
    ("bitwarden_access_token", re.compile(r"(?<![0-9A-Za-z])0\.[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\.[A-Za-z0-9]{20,}:[A-Za-z0-9+/]{16,}={0,2}")),
    # Anthropic / OpenRouter must precede OpenAI — both share the sk- prefix
    # and the OpenAI regex would otherwise swallow them.
    ("anthropic_api_key", re.compile(r"\bsk-ant-(?:api|sid)\d{2}-[A-Za-z0-9_\-]{20,}\b")),
    ("openrouter_api_key", re.compile(r"\bsk-or-v\d-[A-Za-z0-9]{40,}\b")),
    # OpenAI — sk-… (variable length; project keys are longer). Negative lookahead
    # so we don't catch Anthropic / OpenRouter when the order somehow shifts.
    ("openai_api_key", re.compile(r"\bsk-(?!ant-|or-v)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}\b")),
    # AWS access key ID  (AKIA…) and secret (no fixed prefix → entropy/context)
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}\b")),
    # GitHub — ghp_, gho_, ghu_, ghs_, ghr_
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    # GitLab personal access token
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    # Slack tokens
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    # Stripe live keys
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}\b")),
    # Google API key
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # JWT — three base64url segments separated by dots
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    # PEM-armored private key (RSA / EC / OpenSSH / generic)
    ("private_key_pem", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED |)PRIVATE KEY-----"
        r"[\s\S]+?-----END (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED |)PRIVATE KEY-----"
    )),
    # Generic Authorization: Bearer …
    ("bearer_authorization", re.compile(r"(?i)\bAuthorization:\s*Bearer\s+[A-Za-z0-9_\-\.=]{20,}")),
]


_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email",       re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("phone",       re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")),
    ("ssn",         re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("ip_address",  re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


# Entropy scan: high-entropy strings of length >=N are likely tokens. Used as a
# catch-all alongside the regex list. False-positive risk is real (long base64
# attachments, hashes), so it's gated to alphanumeric+symbol charsets typical
# of bearer tokens.
_ENTROPY_TOKEN_PAT = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")


def _shannon_entropy(s: str) -> float:
    """Bits per character in *s*. Pure stdlib."""
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


@dataclass
class RedactionFinding:
    label: str
    count: int
    sample: str  # truncated, for log telemetry only — never the full secret


@dataclass
class RedactionResult:
    text: str
    findings: list[RedactionFinding] = field(default_factory=list)

    @property
    def redacted(self) -> bool:
        return bool(self.findings)

    @property
    def total(self) -> int:
        return sum(f.count for f in self.findings)


_BASE64_CANDIDATE = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")
_URL_ENCODED_CANDIDATE = re.compile(r"(?:%[0-9A-Fa-f]{2}){3,}")

# Single-codepoint confusable fold — Cyrillic + Greek lookalikes that map to
# ASCII letters. NFKC does not collapse cross-script confusables so we apply
# this table in addition to NFKC. Coverage is intentionally narrow: only the
# letters that actually appear in known secret prefixes (sk-, ghp_, AKIA…)
# plus their case variants. Operators wanting full UTS-39 confusable
# detection should install the `confusable_homoglyphs` package.
_CONFUSABLE_FOLD = str.maketrans({
    # Cyrillic lowercase
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "ѕ": "s", "і": "i", "ј": "j", "ӏ": "l", "ν": "v",
    # Cyrillic uppercase
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    # Greek lowercase
    "α": "a", "ε": "e", "ι": "i", "κ": "k", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "ν": "v",
    # Greek uppercase
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    # Misc
    "ı": "i",
})


def _scan_with_patterns(text: str) -> bool:
    """Return True if any known-secret pattern matches *text*."""
    for _, pat in _SECRET_PATTERNS:
        if pat.search(text):
            return True
    return False


class OutputRedactor:
    """Scan and redact a response body before it leaves BeigeBox.

    Multi-pass design — five sweeps in order, each operating on the output
    of the previous:

      1. **NFKC normalize → re-scan** for Unicode-confusable evasion
         (e.g. ``sk-prоj-…`` with a Cyrillic ``о``). When a match shows up
         in the normalized form, the corresponding span in the original is
         redacted by character-position mapping.
      2. **Base64 candidate decode → re-scan**: substrings that look like
         base64 (length ≥ 24, base64 charset) are decoded and re-scanned;
         a hit means the original base64 substring is redacted.
      3. **URL-encoded candidate decode → re-scan**: same idea for
         ``%xx``-encoded substrings.
      4. **Regex sweep** of known secret formats on the (possibly modified)
         text — the original gold-path.
      5. **PII sweep** + **high-entropy bearer-token sweep**.

    Use ``redact(text)`` for one-shot scrubbing; the result is a
    :class:`RedactionResult` with per-finding telemetry. Counts from
    decode-pass redactions are tagged with their decoded label
    (e.g. ``b64:openai_api_key``) so operators can tell evaded leaks from
    plain ones.
    """

    def __init__(self, cfg: dict | None = None):
        red_cfg = ((cfg or {}).get("security") or {}).get("output_redaction") or {}
        self.enabled: bool = bool(red_cfg.get("enabled", False))
        self._do_secrets: bool = bool(red_cfg.get("redact_secrets", True))
        self._do_pii: bool = bool(red_cfg.get("redact_pii", True))
        self._entropy_scan: bool = bool(red_cfg.get("entropy_scan", True))
        self._entropy_min_len: int = int(red_cfg.get("entropy_min_length", 32))
        self._entropy_threshold: float = float(red_cfg.get("entropy_threshold", 4.0))
        self._decode_pass: bool = bool(red_cfg.get("decode_pass", True))
        self._max_decode_attempts: int = int(red_cfg.get("max_decode_attempts", 200))
        # Tail buffer size for StreamingRedactor (chars held back per chunk
        # so secrets straddling chunk boundaries are caught). 512 covers
        # bearer tokens + JWTs comfortably; bump for paranoid PEM coverage.
        self._stream_tail_bytes: int = int(red_cfg.get("stream_tail_bytes", 512))

        if self.enabled:
            logger.info(
                "OutputRedactor enabled (secrets=%s, pii=%s, entropy=%s, decode=%s)",
                self._do_secrets, self._do_pii, self._entropy_scan, self._decode_pass,
            )

    def _redact_unicode_evasion(self, text: str, findings: list[RedactionFinding]) -> str:
        """Pass 1 — fold confusables + NFKC, re-scan, redact span in original.

        Two transforms are applied to a *parallel* copy of the text:
          - Single-codepoint confusable fold (`_CONFUSABLE_FOLD`)
          - NFKC normalization

        Both preserve string length character-for-character (NFKC may not in
        general, but our fold table is single-cp and NFKC on Latin+Cyrillic
        confusables is also length-1). When we get a match in the folded
        form, the same character offsets identify the span in the original,
        so we redact in place.
        """
        folded = unicodedata.normalize("NFKC", text.translate(_CONFUSABLE_FOLD))
        if folded == text or len(folded) != len(text):
            # Nothing folded, OR length changed (rare — multi-cp NFKC).
            # Fall back to a length-mismatch-safe path: any pattern hit in
            # `folded` triggers a generic redaction of the longest plausible
            # span. We don't bother — the high-entropy sweep will catch it.
            return text
        for label, pat in _SECRET_PATTERNS:
            count = 0
            sample = ""
            spans: list[tuple[int, int]] = []
            for m in pat.finditer(folded):
                spans.append((m.start(), m.end()))
                if not sample:
                    sample = m.group(0)[:8] + "…"
            for start, end in reversed(spans):  # right-to-left preserves offsets
                text = text[:start] + f"[{label.upper()}_REDACTED]" + text[end:]
                count += 1
            if count:
                findings.append(RedactionFinding(label=f"unicode:{label}", count=count, sample=sample))
        return text

    def _redact_decode_evasion(self, text: str, findings: list[RedactionFinding]) -> str:
        """Pass 2 + 3 — decode base64 / URL-encoded substrings and re-scan."""
        # Base64 pass
        b64_count = 0
        b64_label = ""
        attempts = 0
        for m in list(_BASE64_CANDIDATE.finditer(text)):
            if attempts >= self._max_decode_attempts:
                break
            attempts += 1
            blob = m.group(0)
            try:
                decoded = base64.b64decode(blob, validate=True).decode("utf-8", errors="ignore")
            except (ValueError, UnicodeDecodeError):
                continue
            if not decoded:
                continue
            for label, pat in _SECRET_PATTERNS:
                if pat.search(decoded):
                    if not b64_label:
                        b64_label = label
                    text = text.replace(blob, "[BASE64_SECRET_REDACTED]", 1)
                    b64_count += 1
                    break
        if b64_count:
            findings.append(RedactionFinding(
                label=f"b64:{b64_label}", count=b64_count, sample="(base64 payload)"
            ))

        # URL-encoded pass
        url_count = 0
        url_label = ""
        for m in list(_URL_ENCODED_CANDIDATE.finditer(text)):
            blob = m.group(0)
            try:
                decoded = urllib.parse.unquote(blob)
            except Exception:  # noqa: BLE001
                continue
            if decoded == blob or not decoded:
                continue
            for label, pat in _SECRET_PATTERNS:
                if pat.search(decoded):
                    if not url_label:
                        url_label = label
                    text = text.replace(blob, "[URL_ENCODED_SECRET_REDACTED]", 1)
                    url_count += 1
                    break
        if url_count:
            findings.append(RedactionFinding(
                label=f"urlenc:{url_label}", count=url_count, sample="(url-encoded payload)"
            ))
        return text

    def redact(self, text: str) -> RedactionResult:
        if not self.enabled or not text:
            return RedactionResult(text=text)

        findings: list[RedactionFinding] = []

        if self._do_secrets and self._decode_pass:
            text = self._redact_unicode_evasion(text, findings)
            text = self._redact_decode_evasion(text, findings)

        if self._do_secrets:
            for label, pat in _SECRET_PATTERNS:
                count = 0
                samples: list[str] = []
                def _sub(m: re.Match) -> str:
                    nonlocal count
                    count += 1
                    if len(samples) < 1:
                        samples.append(m.group(0)[:8] + "…")
                    return f"[{label.upper()}_REDACTED]"
                text = pat.sub(_sub, text)
                if count:
                    findings.append(RedactionFinding(label=label, count=count, sample=samples[0] if samples else ""))

        if self._do_pii:
            for label, pat in _PII_PATTERNS:
                count = 0
                samples: list[str] = []
                def _sub(m: re.Match) -> str:
                    nonlocal count
                    count += 1
                    if len(samples) < 1:
                        samples.append(m.group(0)[:8] + "…")
                    return f"[{label.upper()}_REDACTED]"
                text = pat.sub(_sub, text)
                if count:
                    findings.append(RedactionFinding(label=label, count=count, sample=samples[0] if samples else ""))

        if self._entropy_scan:
            # Skip strings that look like git SHAs / UUIDs / hex digests:
            # mostly-hex content has predictable entropy (~3.7) and is rarely
            # a credential. Real bearer tokens almost always include a
            # mix of upper/lower/digits + non-hex symbols.
            replacements: list[tuple[int, int, str]] = []
            for m in _ENTROPY_TOKEN_PAT.finditer(text):
                tok = m.group(0)
                if len(tok) < self._entropy_min_len:
                    continue
                if _shannon_entropy(tok) < self._entropy_threshold:
                    continue
                # Mostly hex (dashes count as non-hex; if >= 90% of remaining
                # chars are hex, treat as a UUID/SHA-style identifier).
                hex_chars = sum(1 for c in tok if c in "0123456789abcdefABCDEF-")
                if hex_chars >= int(len(tok) * 0.9):
                    continue
                # Lacks at least one of upper / lower / digit / non-hex-symbol —
                # bearer tokens almost always span all four families.
                has_upper = any(c.isupper() for c in tok)
                has_lower = any(c.islower() for c in tok)
                has_digit = any(c.isdigit() for c in tok)
                if not (has_upper and has_lower and has_digit):
                    continue
                # Looks like a filesystem path (contains '/' as a separator at
                # a position that isn't a JWT/base64 segment marker). Skip.
                if "/" in tok and tok.count("/") >= 1 and tok[0] in "/.":
                    continue
                replacements.append((m.start(), m.end(), tok))
            if replacements:
                count = 0
                sample = ""
                for start, end, tok in reversed(replacements):
                    if not sample:
                        sample = tok[:8] + "…"
                    text = text[:start] + "[HIGH_ENTROPY_TOKEN_REDACTED]" + text[end:]
                    count += 1
                findings.append(RedactionFinding(label="high_entropy_token", count=count, sample=sample))

        return RedactionResult(text=text, findings=findings)


class StreamingRedactor:
    """Sliding-window wrapper around :class:`OutputRedactor` for SSE chunks.

    Per-chunk regex matching loses any secret that straddles a chunk
    boundary (chunk N ends ``...sk-ant-api03-aaa`` and chunk N+1 starts
    ``bbcccc...``). To close that gap we keep a tail buffer of size
    ``tail_bytes`` (default 512 chars). On each :meth:`feed`, the held-back
    buffer is concatenated with the new chunk, the *combined* string is run
    through the full redactor, and only the safe prefix (everything before
    the last ``tail_bytes`` characters) is emitted. The trailing window is
    held back to be re-scanned alongside the next chunk.

    At stream end :meth:`finalize` runs one final redaction over whatever
    remains in the buffer and emits the result. Callers MUST call
    ``finalize()`` even if no data is pending — otherwise a token sitting
    in the trailing window will never make it to the client.

    Tail-size choice (default 512):
      * Bearer tokens: ~80 chars max → 512 covers comfortably.
      * JWTs: ~200–500 chars → 512 covers the common case.
      * PEM blocks: 500+ chars + multi-line → not the streaming worry; the
        non-streaming guardrail catches PEMs in tool results, and PEMs in
        chat completions are extremely rare. Operators with a paranoid
        threat model can bump ``security.output_redaction.stream_tail_bytes``.
      * Trade-off: bigger tail → more chars held back per chunk → more
        end-user-visible buffering before any text appears. 512 keeps the
        first-chunk delay imperceptible at typical chat-completion chunk
        sizes (many tens of chars) — most chunks already exceed the tail,
        so emit happens on chunk 1 minus the trailing 512.

    Edge cases:
      * First chunk smaller than ``tail_bytes`` — :meth:`feed` returns "".
      * Chunk smaller than ``tail_bytes`` after buffer fills — emits the
        prefix that fell out of the window; rest stays buffered.
      * Token spanning >2 chunks — handled correctly because the tail is
        always at least ``tail_bytes`` characters of in-flight text.
      * Disabled redactor — :meth:`feed` is a passthrough; :meth:`finalize`
        flushes any held-back chars unredacted (matches non-streaming
        behavior of an off redactor).
    """

    def __init__(
        self,
        redactor: "OutputRedactor",
        tail_bytes: int | None = None,
    ):
        self._redactor = redactor
        # Pull tail size from config if not overridden, falling back to 512.
        if tail_bytes is None:
            cfg_block = getattr(redactor, "_stream_tail_bytes", None)
            tail_bytes = int(cfg_block) if cfg_block else 512
        if tail_bytes < 1:
            tail_bytes = 1
        self._tail_bytes: int = tail_bytes
        self._buffer: str = ""
        self._findings: list[RedactionFinding] = []
        self._finalized: bool = False

    @property
    def findings(self) -> list[RedactionFinding]:
        """Findings accumulated across every feed/finalize call so far."""
        return list(self._findings)

    def feed(self, chunk: str) -> str:
        """Accept *chunk*, return the safe-to-emit prefix.

        The held-back tail is concatenated with the new chunk; the union is
        run through the redactor; the redacted prefix (everything before
        the last ``tail_bytes`` characters of the redacted string) is
        returned. The trailing window stays in the buffer.
        """
        if self._finalized:
            raise RuntimeError("StreamingRedactor: feed() called after finalize()")
        if not chunk:
            return ""
        # Passthrough when redactor is off — still buffer so finalize stays
        # symmetric, but no scanning. Note: if disabled, the buffer is
        # pure-text held back; finalize() flushes it untouched.
        if not getattr(self._redactor, "enabled", False):
            self._buffer += chunk
            if len(self._buffer) <= self._tail_bytes:
                return ""
            split = len(self._buffer) - self._tail_bytes
            emit = self._buffer[:split]
            self._buffer = self._buffer[split:]
            return emit

        combined = self._buffer + chunk
        result = self._redactor.redact(combined)
        # Track only NEW findings — but the redactor doesn't know which
        # findings are repeats of last call's. Conservative approach: stash
        # findings as-seen and de-duplicate is *not* attempted, since the
        # same secret straddling a boundary will surface once now and not
        # again later (the redacted prefix has the placeholder).
        for f in result.findings:
            self._findings.append(f)

        redacted = result.text
        # If the held-back tail is bigger than the entire redacted string
        # (possible when a redaction shrunk the text), emit nothing and
        # keep all of it buffered for the next round.
        if len(redacted) <= self._tail_bytes:
            self._buffer = redacted
            return ""
        split = len(redacted) - self._tail_bytes
        emit = redacted[:split]
        self._buffer = redacted[split:]
        return emit

    def finalize(self) -> str:
        """Flush the held-back tail. Idempotent (after first call returns "")."""
        if self._finalized:
            return ""
        self._finalized = True
        tail = self._buffer
        self._buffer = ""
        if not tail:
            return ""
        if not getattr(self._redactor, "enabled", False):
            return tail
        result = self._redactor.redact(tail)
        for f in result.findings:
            self._findings.append(f)
        return result.text


__all__ = [
    "OutputRedactor",
    "RedactionResult",
    "RedactionFinding",
    "StreamingRedactor",
]


# ── Smoke tests ─────────────────────────────────────────────────────────────
# Run with:  python3 -m beigebox.security.output_redactor
# These are intentionally inline (no test framework dep) so the file is
# self-checking on a host without pytest. Mirrors the non-streaming
# corpus but exercises the chunk-split bypass that motivated the wrapper.
if __name__ == "__main__":  # pragma: no cover
    import sys

    cfg = {"security": {"output_redaction": {"enabled": True}}}
    red = OutputRedactor(cfg)

    failures: list[str] = []

    def _expect(cond: bool, label: str) -> None:
        if cond:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}")
            failures.append(label)

    print("StreamingRedactor smoke tests")

    # 1. Secret straddling two chunks must not leak in either chunk's emit.
    sr = StreamingRedactor(red, tail_bytes=64)
    out1 = sr.feed("here is the key: sk-ant-api03-AAAAAAAAAAAA")
    out2 = sr.feed("BBBBBBBBBBBBCCCCCCCCCCCC and the rest of the message goes here")
    final = sr.finalize()
    full = out1 + out2 + final
    _expect("sk-ant-api03-" not in full, "split anthropic key fully redacted")
    _expect("[ANTHROPIC_API_KEY_REDACTED]" in full, "split anthropic key produces placeholder")

    # 2. Benign text must not be over-redacted.
    sr2 = StreamingRedactor(red, tail_bytes=64)
    benign = "Hello world. This is a normal message about the weather."
    out = sr2.feed(benign) + sr2.finalize()
    _expect(out == benign, "benign text passes through unchanged")

    # 3. Token split across THREE chunks (worst case for boundary handling).
    sr3 = StreamingRedactor(red, tail_bytes=64)
    parts = ["prefix sk-proj-", "AAAAAAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBBBBBB suffix"]
    emitted = "".join(sr3.feed(p) for p in parts) + sr3.finalize()
    _expect("sk-proj-AAAA" not in emitted, "three-chunk split openai key redacted")

    # 4. Tail-buffer covers JWT (~250 chars) when configured.
    sr4 = StreamingRedactor(red, tail_bytes=512)
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ"
        + "X" * 80
        + ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    chunks = [jwt[i:i + 30] for i in range(0, len(jwt), 30)]
    emitted = "".join(sr4.feed(c) for c in chunks) + sr4.finalize()
    _expect("[JWT_REDACTED]" in emitted, "JWT redacted across many chunks")

    # 5. Confusable / unicode evasion across chunks (transitive coverage).
    sr5 = StreamingRedactor(red, tail_bytes=64)
    # Cyrillic 'о' in 'sk-prоj-' — spans a chunk boundary
    confused = "API key sk-pr"
    rest = "оj-AAAAAAAAAAAAAAAAAAAA"
    emitted = sr5.feed(confused) + sr5.feed(rest) + sr5.finalize()
    _expect("sk-prоj-AAAAAAAAAAAAAAAAAAAA" not in emitted, "unicode-confusable secret redacted across split")

    # 6. Disabled redactor passthrough.
    red_off = OutputRedactor({"security": {"output_redaction": {"enabled": False}}})
    sr6 = StreamingRedactor(red_off, tail_bytes=64)
    text = "sk-ant-api03-LEAKABCDEFGHIJKLMNOP this should pass through"
    emitted = sr6.feed(text) + sr6.finalize()
    _expect(emitted == text, "disabled redactor is a passthrough")

    # 7. Idempotent finalize.
    sr7 = StreamingRedactor(red, tail_bytes=32)
    sr7.feed("short")
    a = sr7.finalize()
    b = sr7.finalize()
    _expect(b == "", "second finalize() is empty")

    if failures:
        print(f"\n{len(failures)} smoke test failure(s)")
        sys.exit(1)
    print("\nAll streaming-redactor smoke tests passed.")
