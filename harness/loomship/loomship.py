#!/usr/bin/env python3
"""loomship: ship Pi's session files to Loom, redacted, with a receipt for every entry.

Pi appends one JSON entry per line to ~/.pi/agent/sessions/<cwd>/<time>_<id>.jsonl. That file is the outbox: loomship
keeps a byte cursor per file and moves it only past entries Loom has acknowledged after its commit. If Loom is down,
nothing moves and the same entries go again later; Loom drops what it already has by (source, session, id), so a reply
lost on the way changes nothing either.

Every string in an entry is run through the secret redactor before it leaves (fail closed: no redactor, no shipping;
an entry the redactor cannot handle stops its file until the next pass). Tool results and anything else that came from
outside the conversation are marked untrusted, so Loom stores them for audit but never embeds or recalls them. Only
Ryan's prompts and the assistant's final text are offered as `text` for embedding.

    loomship.py --sessions DIR --state FILE --loom URL --token-file FILE [--once] [--interval SECONDS]
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (os.path.join(HERE, "..", "redact"), os.path.join(HERE, "redact"), HERE):
    sys.path.insert(0, _d)
import output_redactor as REDACT  # noqa: E402  (fail closed: no redactor, no start)

REDACTOR = REDACT.OutputRedactor({"security": {"output_redaction": {"enabled": True, "redact_pii": False}}})
with open(REDACT.__file__, "rb") as _f:
    REDACTOR_ID = "or-" + hashlib.sha256(_f.read()).hexdigest()[:12]

SOURCE = "pi"
MAX_RAW = 4 * 1024 * 1024 - 4096      # Loom's limit, less room for the cut marker
MAX_TEXT = 60_000
BATCH_EVENTS = 200
BATCH_BYTES = 8 * 1024 * 1024
READ_LIMIT = 32 * 1024 * 1024         # one pass reads at most this much of one file
TRUSTED_TYPES = {"session", "model_change", "thinking_level_change"}
TRUSTED_ROLES = {"user", "assistant", "system"}
FINAL_STOPS = {"stop", "length"}
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
WORD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")    # Loom's alphabet for type, role, provider


def log(msg):
    print("loomship: " + msg, file=sys.stderr, flush=True)


def redact(value):
    """Every string in a JSON value, redacted; the structure is kept, so the result is still valid JSON."""
    if isinstance(value, str):
        return REDACTOR.redact(value).text
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {redact(k): redact(v) for k, v in value.items()}   # keys too: a secret can be a key
    return value


def _texts(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


def _word(value) -> str:
    """Loom refuses the whole batch over one bad word, so anything outside its alphabet becomes empty."""
    s = str(value or "")
    return s if WORD_RE.match(s) else ""


def safe_id(value) -> str:
    """Loom's id alphabet; anything else becomes a stable hash so one odd id cannot block the file."""
    s = str(value)
    if ID_RE.match(s):
        return s
    return "h-" + hashlib.sha256(s.encode()).hexdigest()[:32]


def to_event(entry: dict, session: str) -> dict:
    """One redacted Pi entry as a Loom event."""
    etype = str(entry.get("type") or "unknown")
    msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    role = str(msg.get("role") or "")
    trusted = etype in TRUSTED_TYPES or (etype == "message" and role in TRUSTED_ROLES)
    text = ""
    if trusted and etype == "message" and role == "user":
        text = _texts(msg.get("content"))
    elif trusted and etype == "message" and role == "assistant" and msg.get("stopReason") in FINAL_STOPS:
        text = _texts(msg.get("content"))
    raw = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode()) > MAX_RAW:
        raw = json.dumps({"loomship": "cut", "bytes": len(raw.encode()), "head": raw[:65536]}, ensure_ascii=False)
    parent = entry.get("parentId")
    return {
        "source": SOURCE, "session": safe_id(session), "id": safe_id(entry.get("id")),
        "parent": safe_id(parent) if parent else None,
        "type": _word(etype) or "unknown", "role": _word(role),
        "ts": str(entry.get("timestamp") or "")[:128],
        "model": str(msg.get("model") or entry.get("modelId") or "")[:128],
        "provider": _word(msg.get("provider") or entry.get("provider")),
        "trust": "trusted" if trusted else "untrusted", "redactor": REDACTOR_ID, "raw": raw,
        "text": text.strip()[:MAX_TEXT],
    }


class Shipper:
    def __init__(self, sessions, state_path, loom, token_file, timeout=30):
        self.sessions = sessions
        self.state_path = state_path
        self.loom = loom.rstrip("/")
        self.token_file = token_file
        self.timeout = timeout
        self.state = self._load()
        self.failing = False

    def _load(self):
        try:
            with open(self.state_path) as f:
                st = json.load(f)
        except FileNotFoundError:
            return {"files": {}}
        except ValueError:
            st = None
        files = st.get("files") if isinstance(st, dict) else None
        if not isinstance(files, dict):
            log("state %s unreadable; shipping everything again (Loom drops what it has)" % self.state_path)
            return {"files": {}}
        return {"files": {p: r for p, r in files.items() if isinstance(r, dict) and isinstance(r.get("offset"), int)
                          and r["offset"] >= 0 and isinstance(r.get("session"), (str, type(None)))}}

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def _post(self, events):
        """POST one batch; returns True only if every event came back acknowledged, in order."""
        with open(self.token_file) as f:
            token = f.read().strip()
        body = json.dumps({"events": events}, ensure_ascii=False).encode()
        req = urllib.request.Request(self.loom + "/events", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                reply = json.loads(r.read(16 * 1024 * 1024))
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            detail = e.read(500).decode(errors="replace") if isinstance(e, urllib.error.HTTPError) else ""
            self._fail("Loom refused or unreachable: %s %s" % (e, detail))
            return False
        acks = reply.get("acks") if isinstance(reply, dict) else None
        want = [(e["session"], e["id"]) for e in events]
        # seq is the receipt; hash is null for an event whose content was purged after it was forgotten
        if not isinstance(acks, list) or not all(isinstance(a, dict) for a in acks) or \
                [(a.get("session"), a.get("id")) for a in acks] != want or \
                not all(isinstance(a.get("seq"), int) for a in acks):
            self._fail("Loom's acks do not match the batch; cursor kept")
            return False
        if self.failing:
            log("Loom is taking entries again")
            self.failing = False
        return True

    def _fail(self, msg):
        if msg != self.failing:          # each distinct failure once, not every retry
            log(msg)
        self.failing = msg

    def ship_file(self, path) -> bool:
        """Ship what is new in one file. False when something stopped it (the cursor stays at the last receipt)."""
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self.state["files"].pop(path, None)
            return True
        rec = self.state["files"].get(path)
        if rec is None or rec.get("ino") != st.st_ino or st.st_size < rec.get("offset", 0):
            rec = {"ino": st.st_ino, "offset": 0, "session": None}   # new, replaced or shrunk: from the top
            self.state["files"][path] = rec
        if st.st_size == rec["offset"]:
            return True
        base = rec["offset"]
        with open(path, "rb") as f:
            f.seek(base)
            data = f.read(READ_LIMIT)
            end = data.rfind(b"\n")
            if end < 0 and len(data) == READ_LIMIT:
                return self._ship_long_line(f, path, rec, base)
        if end < 0:
            return True                                   # a line Pi is still writing
        events, pos, stopped = [], 0, False               # events: (event, file offset just past its line)
        for line in data[:end + 1].split(b"\n")[:-1]:
            start, pos = pos, pos + len(line) + 1
            try:
                entry = json.loads(line)
            except ValueError:
                entry = None
            if not isinstance(entry, dict) or not entry.get("id"):
                # Not an entry Pi would write; keep it as an untrusted record rather than lose or trust it.
                entry = {"type": "unparsed", "id": "off-%d" % (base + start), "line": line.decode(errors="replace")}
            if rec["session"] is None:
                if entry.get("type") != "session":
                    log("%s does not start with a session header; skipped" % path)
                    rec["offset"] = st.st_size
                    self._save()
                    return True
                rec["session"] = str(entry["id"])
            try:
                events.append((to_event(redact(entry), rec["session"]), base + pos))
            except Exception as e:                        # fail closed: an entry not inspected is not sent
                log("redactor failed on %s at byte %d (%s); this file waits" % (path, base + start, e))
                stopped = True
                break
        i = 0
        while i < len(events):
            batch, size = [], 16
            while i < len(events) and len(batch) < BATCH_EVENTS:
                n = len(json.dumps(events[i][0], ensure_ascii=False).encode()) + 2    # as sent, escapes included
                if batch and size + n > BATCH_BYTES:
                    break
                batch.append(events[i][0])
                size += n
                i += 1
            if not self._post(batch):
                return False
            rec["offset"] = events[i - 1][1]
            self._save()
        return not stopped

    def _ship_long_line(self, f, path, rec, base) -> bool:
        """A line longer than one read: ship a record of its size (not its content) and move past it."""
        n = READ_LIMIT
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                return True                               # still being written
            i = chunk.find(b"\n")
            if i >= 0:
                n += i
                break
            n += len(chunk)
        if rec["session"] is None:
            log("%s does not start with a session header; skipped" % path)
            rec["offset"] = os.fstat(f.fileno()).st_size
        else:
            if not self._post([to_event({"type": "unparsed", "id": "off-%d" % base, "loomship": "cut", "bytes": n},
                                        rec["session"])]):
                return False
            rec["offset"] = base + n + 1
        self._save()
        return True

    def run_once(self) -> bool:
        ok = True
        for path in sorted(glob.glob(os.path.join(self.sessions, "*", "*.jsonl"))):
            if not self.ship_file(path):
                ok = False
                if self.failing:
                    break                                 # Loom is down: no point trying the other files now
        for gone in [p for p in self.state["files"] if not os.path.exists(p)]:
            del self.state["files"][gone]
        return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sessions", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--loom", default="http://127.0.0.1:9741")
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    if not os.access(a.token_file, os.R_OK):
        log("cannot read the ingest token %s" % a.token_file)
        return 2
    s = Shipper(a.sessions, a.state, a.loom, a.token_file)
    log("shipping %s to %s (redactor %s)" % (a.sessions, a.loom, REDACTOR_ID))
    if a.once:
        return 0 if s.run_once() else 1
    backoff = a.interval
    while True:
        ok = s.run_once()
        backoff = a.interval if ok else min(backoff * 2, 300)
        time.sleep(backoff)


if __name__ == "__main__":
    sys.exit(main())
