import json, re, sys
d = json.load(open(sys.argv[1]))
ids = {s["id"]: s for s in d["symbols"]}
d["calls"] = [c for c in d["calls"] if len(ids[c[1]]["name"]) > 1]          # one-letter names are locals, not targets
inn, out = {}, {}
for a, b in d["calls"]:
    inn.setdefault(b, []).append(a); out.setdefault(a, []).append(b)
mods = {f: set(v) for f, v in d.pop("modcalls", {}).items()}
tests = [s["id"] for s in d["symbols"] if s["test"]]
reach, stack = set(tests), list(tests)
while stack:
    for b in out.get(stack.pop(), []):
        if b not in reach: reach.add(b); stack.append(b)
spans = {}
for s in d["symbols"]:
    if s["kind"] in ("function", "method"): spans.setdefault(s["file"], []).append((s["line"], s["end"], s["id"]))
for s in d["symbols"]:
    callers = inn.get(s["id"], [])
    s["nested"] = any(a < s["line"] and s["end"] <= b and i != s["id"] for a, b, i in spans.get(s["file"], []))
    s["module_use"] = s["name"] in mods.get(s["file"], set())
    s["callers"], s["callees"] = len(callers), len(out.get(s["id"], []))
    s["tested"] = s["id"] in reach and not s["test"]
    lg = s["file"]
    entry = (s["test"] or s["name"].startswith(("do_", "test", "setUp", "tearDown", "__", "Test", "Benchmark")) or
             s["name"] in ("main", "init", "handle", "log_message", "get_request", "server_bind", "run", "ServeHTTP", "Error", "String")
             or s["kind"] in ("class", "type"))
    dead_lang = lg.endswith(".py") or lg.endswith(".go") or lg.endswith(".sh") or "/" in lg and not "." in lg.split("/")[-1]
    if lg.endswith(".go") and s["name"][:1].isupper(): dead_lang = False   # exported: may be used outside this view
    s["unused"] = bool(dead_lang and not entry and not callers and not s["module_use"] and not s["nested"] and s["kind"] in ("function", "shell function")
                       and s["parent"] is None)
FIX = {  # security review 2026-11-15: where each fix landed
    "image/sbx-fetch-secrets.sh": "Secrets written by root into a root-owned folder (/run/pi-bridge-secrets)",
    "harness/systemd/pi-bridge.service": "Loads secrets from the new root-owned folder",
    "harness/broker/sbx_broker.py#gate_lockdown": "Broker forces the session profile before every net job",
    "harness/broker/sbx_broker.py#valid_envelope": "Deeply nested JSON is a bad envelope, not a dead thread",
    "harness/systemd/sbx-broker.service": "Broker joins sbxgatecall (socket), never sbxgate (token)",
    "console/sbx_console.py#render_ruleset": "Sandbox interface dropped ahead of management rules",
    "console/sbx_console.py#fw_lockdown": "Lockdown keeps a pending change, never restores a wider table",
    "console/sbx_console.py#_addr": "Validators use fullmatch",
    "console/nftables-sbx-forward.conf": "Forwarding on only after the ruleset loads",
    "image/provision.sh": "Forwarding off at boot; Loom deps hash-locked; Ollama digest checked",
    "image/build-pi.sh": "Pi built with npm ci as the unprivileged pibuild account",
    "image/loom-requirements.lock": "Hash-pinned Loom dependencies",
    "harness/bridge/pairing.py#safe_name": "Peer names limited to safe characters",
    "harness/bridge/pairing.py#accept_pair": "A known instance is not re-paired by asking",
    "harness/bridge/peers.py#envelope": "Envelope sender name sanitised",
    "harness/redact/output_redactor.py": "named_secret catches hex and base32 values next to credential names",
    "tools/netcheck/run": "Host is a fixed list (no DNS channel)",
    "tools/netcheck/tcs.yaml": "Contract enforces the host list on the core",
}
for f in d["files"]:
    if f["id"] in FIX: f["fix"] = FIX[f["id"]]
for s in d["symbols"]:
    k = s["file"] + "#" + s["name"]
    if k in FIX: s["fix"] = FIX[k]
json.dump(d, sys.stdout, separators=(",", ":"))
