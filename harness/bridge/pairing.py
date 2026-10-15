"""First-contact key exchange and request signing between bridge instances. Stdlib only.

First contact:  client POST /peer/pair {instance, name, pub, nonce}  ->  server {instance, name, pub, nonce, confirm}
  key     = HKDF-SHA256(X25519(priv, peer_pub), salt=client_nonce||server_nonce, info=idc||ids)
  confirm = HMAC(key, "server-confirm"||client_pub||server_pub), checked by the client.
Afterwards every request carries  X-Bridge-Auth: v1 <instance>.<ts>.<nonce>.<mac>  where
  mac = HMAC(key, method\\npath\\nts\\nnonce\\nsha256(body)), with a 60 s clock window and a replay cache.

The exchange itself is unauthenticated (trust on first use); the caller decides whether to accept a first contact.
X25519 is the RFC 7748 ladder in pure Python: fine here, not constant-time.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import uuid

P25519 = 2**255 - 19
SKEW = 60.0
BASE = (9).to_bytes(32, "little")


def x25519(k: bytes, u: bytes) -> bytes:
    kb = bytearray(k)
    kb[0] &= 248
    kb[31] = (kb[31] & 127) | 64
    kn = int.from_bytes(kb, "little")
    x1 = (int.from_bytes(u, "little") & ((1 << 255) - 1)) % P25519
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        kt = (kn >> t) & 1
        swap ^= kt
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = kt
        a, b = (x2 + z2) % P25519, (x2 - z2) % P25519
        aa, bb = a * a % P25519, b * b % P25519
        e = (aa - bb) % P25519
        c, d = (x3 + z3) % P25519, (x3 - z3) % P25519
        da, cb = d * a % P25519, c * b % P25519
        x3, z3 = (da + cb) ** 2 % P25519, x1 * (da - cb) ** 2 % P25519
        x2, z2 = aa * bb % P25519, e * (aa + 121665 * e) % P25519
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * pow(z2, P25519 - 2, P25519) % P25519).to_bytes(32, "little")


def keypair() -> tuple[bytes, bytes]:
    priv = os.urandom(32)
    return priv, x25519(priv, BASE)


def hkdf(secret: bytes, salt: bytes, info: bytes, n: int = 32) -> bytes:
    prk = hmac.new(salt, secret, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < n:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out, i = out + t, i + 1
    return out[:n]


def derive(priv: bytes, peer_pub: bytes, cnonce: str, snonce: str, idc: str, ids: str) -> bytes:
    shared = x25519(priv, peer_pub)
    if shared == bytes(32):
        raise ValueError("low-order public key")
    return hkdf(shared, bytes.fromhex(cnonce) + bytes.fromhex(snonce), ("pi-bridge/v1" + idc + ids).encode())


def confirm_tag(key: bytes, cpub: bytes, spub: bytes) -> str:
    return hmac.new(key, b"server-confirm" + cpub + spub, hashlib.sha256).hexdigest()


def _hexn(s, n) -> bytes:
    if not isinstance(s, str) or len(s) != n * 2:
        raise ValueError("bad hex field")
    return bytes.fromhex(s)


# ---- signing -------------------------------------------------------------
def _mac(key: bytes, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    msg = "\n".join([method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest()]).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def sign(key: bytes, instance: str, method: str, path: str, body: bytes) -> str:
    ts, nonce = str(int(time.time())), os.urandom(8).hex()
    return "v1 %s.%s.%s.%s" % (instance, ts, nonce, _mac(key, method, path, ts, nonce, body))


class NonceCache:
    def __init__(self):
        self.seen: dict[str, float] = {}
        self.lock = threading.Lock()

    def fresh(self, k: str) -> bool:
        now = time.time()
        with self.lock:
            for old in [x for x, t in self.seen.items() if now - t > 2 * SKEW]:
                del self.seen[old]
            if k in self.seen:
                return False
            self.seen[k] = now
            return True


# ---- state ---------------------------------------------------------------
def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)       # mkstemp creates 0600


def load_identity(state_dir: str, name: str) -> dict:
    path = os.path.join(state_dir, "identity.json")
    try:
        d = json.load(open(path))
        if d.get("instance"):
            return d
    except (OSError, ValueError):
        pass
    d = {"instance": uuid.uuid4().hex[:16], "name": name}
    _write(path, d)
    return d


class PeerStore:
    """instance -> {name, key(hex), paired_at}. Keys are only returned by key()."""

    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, "peers.json")
        self.lock = threading.Lock()
        try:
            self.d = json.load(open(self.path))
        except (OSError, ValueError):
            self.d = {}

    def put(self, instance: str, name: str, key: bytes) -> None:
        with self.lock:
            self.d[instance] = {"name": name, "key": key.hex(), "paired_at": time.time()}
            _write(self.path, self.d)

    def key(self, instance: str) -> bytes | None:
        with self.lock:
            r = self.d.get(instance)
            return bytes.fromhex(r["key"]) if r else None

    def name(self, instance: str) -> str | None:
        with self.lock:
            return (self.d.get(instance) or {}).get("name")

    def delete(self, instance: str) -> bool:
        with self.lock:
            ok = self.d.pop(instance, None) is not None
            if ok:
                _write(self.path, self.d)
            return ok

    def list(self) -> list[dict]:
        with self.lock:
            return [{"instance": i, "name": r["name"], "paired_at": r["paired_at"]} for i, r in self.d.items()]


def verify(store: PeerStore, nonces: NonceCache, header, method: str, path: str, body: bytes) -> str | None:
    """The paired peer's instance id if the request is correctly signed and fresh, else None."""
    try:
        scheme, rest = (header or "").split(" ", 1)
        inst, ts, nonce, mac = rest.split(".")
        if scheme != "v1" or abs(time.time() - int(ts)) > SKEW:
            return None
        key = store.key(inst)
        if not key or not hmac.compare_digest(mac, _mac(key, method, path, ts, nonce, body)):
            return None
        return inst if nonces.fresh(inst + nonce) else None
    except (ValueError, AttributeError):
        return None


# ---- the two halves of first contact -------------------------------------
def accept_pair(me: dict, store: PeerStore, body: dict) -> tuple[int, dict]:
    """Server half. The caller has already decided this first contact is allowed."""
    try:
        inst = str(body["instance"])
        if not (8 <= len(inst) <= 64) or any(c not in "0123456789abcdef" for c in inst) or inst == me["instance"]:
            raise ValueError
        cpub, cnonce = _hexn(body["pub"], 32), _hexn(body["nonce"], 16).hex()
        name = str(body.get("name") or inst)[:64]
        spriv, spub = keypair()
        snonce = os.urandom(16).hex()
        key = derive(spriv, cpub, cnonce, snonce, inst, me["instance"])
    except (KeyError, ValueError, TypeError):
        return 400, {"error": "need {instance, name, pub(32B hex), nonce(16B hex)}"}
    store.put(inst, name, key)
    return 200, {"instance": me["instance"], "name": me["name"], "pub": spub.hex(), "nonce": snonce,
                 "confirm": confirm_tag(key, cpub, spub)}


def pair_with(me: dict, store: PeerStore, post) -> str:
    """Client half. `post(path, obj) -> (status, dict)`. Returns the other side's instance id."""
    priv, pub = keypair()
    nonce = os.urandom(16).hex()
    code, r = post("/peer/pair", {"instance": me["instance"], "name": me["name"], "pub": pub.hex(), "nonce": nonce})
    if code != 200:
        raise RuntimeError("pairing refused (%s): %s" % (code, r.get("error", "")))
    spub = _hexn(r["pub"], 32)
    key = derive(priv, spub, nonce, r["nonce"], me["instance"], r["instance"])
    if not hmac.compare_digest(r["confirm"], confirm_tag(key, pub, spub)):
        raise RuntimeError("pairing confirmation failed (key mismatch)")
    store.put(r["instance"], r["name"], key)
    return r["instance"]
