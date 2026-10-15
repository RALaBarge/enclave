"""Minimal stand-in for `pi --mode rpc`: JSONL on stdin/stdout, enough for the bridge tests."""
import json
import sys

msgs = []
for line in sys.stdin.buffer:
    cmd = json.loads(line)
    t, cid = cmd.get("type"), cmd.get("id")

    def out(o):
        sys.stdout.write(json.dumps(o) + "\n")
        sys.stdout.flush()

    if t == "prompt":
        msgs.append({"role": "user", "text": cmd["message"]})
        out({"type": "response", "id": cid, "command": t, "success": True})
        out({"type": "agent_start"})
        out({"type": "message_update", "text": "echo: " + cmd["message"][:40] + " tail"})
        out({"type": "agent_settled"})
    elif t == "get_messages":
        out({"type": "response", "id": cid, "command": t, "success": True, "data": {"messages": msgs}})
    elif t == "get_state":
        out({"type": "response", "id": cid, "command": t, "success": True, "data": {"model": "fake"}})
    elif t == "crash":
        sys.exit(3)
    else:
        out({"type": "response", "id": cid, "command": t, "success": False, "error": "unknown " + str(t)})
