"""Shared parser for tools.list. Stdlib only; deployed next to both the broker
and sbx-run so neither carries its own copy of the tool names."""
import re

TOOL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
NET_FIELD = "net"


def load(path):
    """Returns {name: {"timeout_s": int, "network": bool}}. Raises ValueError on a malformed line."""
    tools = {}
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            name = parts[0]
            if not TOOL_RE.match(name) or len(parts) > 3 or name in tools:
                raise ValueError(f"{path}:{n}: bad or duplicate entry")
            timeout, network = None, False
            for field in parts[1:]:
                if field == NET_FIELD:
                    if network:
                        raise ValueError(f"{path}:{n}: bad or duplicate entry")
                    network = True
                elif timeout is None:
                    timeout = int(field)      # ValueError on anything that is not a timeout
                else:
                    raise ValueError(f"{path}:{n}: bad or duplicate entry")
            tools[name] = {"timeout_s": max(1, min(DEFAULT_TIMEOUT if timeout is None else timeout, MAX_TIMEOUT)),
                           "network": network}
    return tools
