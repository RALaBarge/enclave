"""Shared parser for tools.list. Stdlib only; deployed next to both the broker
and sbx-run so neither carries its own copy of the tool names."""
import re

TOOL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
NET_FIELD = "net"
FLAGS = (NET_FIELD, "work", "keep")
# net   the job gets the sandbox box's network namespace (what it reaches is the core firewall's call)
# work  the job gets the box's workspace (/deskbox/work, persistent, also its cwd); never together with net
# keep  the broker skips the reset after this tool: only for tools that run no code they are handed


def load(path):
    """Returns {name: {"timeout_s": int, "network": bool, "work": bool, "keep": bool}}. Raises ValueError on a
    malformed line."""
    tools = {}
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            name = parts[0]
            if not TOOL_RE.match(name) or name in tools:
                raise ValueError(f"{path}:{n}: bad or duplicate entry")
            timeout, flags = None, set()
            for field in parts[1:]:
                if field in FLAGS and field not in flags:
                    flags.add(field)
                elif timeout is None:
                    timeout = int(field)      # ValueError on anything that is not a timeout
                else:
                    raise ValueError(f"{path}:{n}: bad or duplicate entry")
            if NET_FIELD in flags and "work" in flags:
                raise ValueError(f"{path}:{n}: net and work together: the workspace never meets a network")
            tools[name] = {"timeout_s": max(1, min(DEFAULT_TIMEOUT if timeout is None else timeout, MAX_TIMEOUT)),
                           "network": NET_FIELD in flags, "work": "work" in flags, "keep": "keep" in flags}
    return tools
