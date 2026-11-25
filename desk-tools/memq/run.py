#!/usr/bin/env python3
"""memq: the memory review queue in Loom. The only way anything is written to memory.

  push   queue a candidate memory for review          {"op": "push", "text": "...", "events"?: [seq], "note"?: "..."}
  list   show the queue (default: pending)            {"op": "list", "status"?: "pending|accepted|rejected|applied|all"}
  pop    move every ACCEPTED entry into memory        {"op": "pop"}
  search look up memories already accepted            {"op": "search", "query": "...", "limit"?: n}

Nothing here can accept an entry: that happens only in the console's Memory tab, with the operator's token. So the
agent may push and pop freely; pop only ever moves what Ryan accepted.

As a DeskBox tool (no arguments, JSON on stdin) entries are pushed as `by: agent` and the token is memq.token beside
this file ($DESKBOX_TOOL_DIR). On the core as Ryan's command, `memq push TEXT [--event SEQ]... [--note N]`,
`memq list [STATUS]`, `memq pop`, `memq search QUERY`: entries are `by: ryan` and the token is $MEMQ_TOKEN_FILE
(default /etc/loom/memq.token). Loom is always 127.0.0.1:9741; the caller cannot point the token elsewhere.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

LOOM = "http://127.0.0.1:9741"
STATUSES = ("pending", "accepted", "rejected", "applied", "all")


class Fail(Exception):
    pass


def call(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(LOOM + path, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # loopback: never through a proxy
    try:
        with opener.open(req, timeout=600) as r:   # pop embeds on a slow CPU: minutes per entry
            return json.loads(r.read(4 * 1024 * 1024))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read(4096)).get("error", "")
        except ValueError:
            msg = ""
        raise Fail("Loom said %d %s" % (e.code, msg))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Fail("Loom unreachable: %s" % e)


def need(args, key):
    if not isinstance(args.get(key), str) or not args[key].strip():
        raise Fail("%s needs %s" % (args.get("op"), key))
    return args[key]


def run(op, args, token, by):
    if op == "push":
        need(args, "text")
        out = call("POST", "/proposals", token, {"text": args["text"], "by": by, "events": args.get("events") or [],
                                                 "note": args.get("note") or ""})
        return {"op": "push", "id": out["id"], "status": out["status"],
                "note": "queued for Ryan's review in the console; it is not memory until accepted and popped"}
    if op == "list":
        status = args.get("status") or "pending"
        if status not in STATUSES:
            raise Fail("status must be one of " + ", ".join(STATUSES))
        items = call("GET", "/proposals?" + urllib.parse.urlencode({"status": status}))["proposals"]
        return {"op": "list", "status": status, "entries": [
            {k: p.get(k) for k in ("id", "text", "status", "by", "events", "note", "created_at", "memory_id")}
            for p in items]}
    if op == "pop":
        out = call("POST", "/proposals/pop", token)
        return {"op": "pop", "applied": out["applied"]}
    if op == "search":
        need(args, "query")
        out = call("POST", "/memories/search", None, {"query": args["query"], "limit": int(args.get("limit") or 5)})
        return {"op": "search", "memories": [{k: m.get(k) for k in ("id", "text", "distance", "updated_at")}
                                             for m in out["results"]]}
    raise Fail("op must be push, list, pop or search")


def read_token(path):
    try:
        with open(path) as f:
            tok = f.read().strip()
    except OSError as e:
        raise Fail("cannot read the memq token %s (%s)" % (path, e.strerror))
    if not tok:
        raise Fail("the memq token %s is empty" % path)
    return tok


def desk():
    """DeskBox mode: one JSON object in, one out. Failures are reported, not raised, so the agent sees why."""
    args = None
    try:
        try:
            args = json.load(sys.stdin)
        except ValueError:
            args = None
        if not isinstance(args, dict):
            raise Fail("the input must be one JSON object")
        op = args.get("op")
        token = read_token(os.path.join(os.environ.get("DESKBOX_TOOL_DIR", os.path.dirname(__file__)), "memq.token")) \
            if op in ("push", "pop") else None
        out = run(op, args, token, "agent")
        out["ok"], out["error"] = True, ""
    except (Fail, KeyError, TypeError, ValueError, AttributeError) as e:   # the rest: a field missing in a reply
        msg = str(e) if isinstance(e, Fail) else "bad input or Loom reply (%s: %s)" % (type(e).__name__, e)
        out = {"op": str(args.get("op", ""))[:16] if isinstance(args, dict) else "", "ok": False, "error": msg[:300]}
    json.dump(out, sys.stdout)
    return 0


def cli(argv):
    ap = argparse.ArgumentParser(prog="memq", description="Loom's memory review queue (accept entries in the console)")
    sub = ap.add_subparsers(dest="op", required=True)
    p = sub.add_parser("push", help="queue a memory for review")
    p.add_argument("text")
    p.add_argument("--event", type=int, action="append", dest="events", default=[], help="source event seq")
    p.add_argument("--note", default="")
    p = sub.add_parser("list", help="show the queue")
    p.add_argument("status", nargs="?", default="pending", choices=STATUSES)
    sub.add_parser("pop", help="move accepted entries into memory")
    p = sub.add_parser("search", help="search accepted memories")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    a = vars(ap.parse_args(argv))
    try:
        token = read_token(os.environ.get("MEMQ_TOKEN_FILE", "/etc/loom/memq.token")) \
            if a["op"] in ("push", "pop") else None
        out = run(a["op"], a, token, "ryan")
    except Fail as e:
        print("memq: %s" % e, file=sys.stderr)
        return 1
    if a["op"] == "list":
        for e in out["entries"]:
            print("%s  %-8s %-8s %s" % (e["id"], e["status"], e["by"], e["text"].replace("\n", " ")[:120]))
    elif a["op"] == "pop":
        for e in out["applied"]:
            print("%s -> %s  %s" % (e["proposal"], e["memory"], e["text"].replace("\n", " ")[:100]))
        print("%d moved into memory" % len(out["applied"]))
    elif a["op"] == "search":
        for m in out["memories"]:
            print("%.3f  %s  %s" % (m["distance"], m["id"], m["text"].replace("\n", " ")[:120]))
    else:
        print("%s queued for review" % out["id"])
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]) if len(sys.argv) > 1 else desk())
