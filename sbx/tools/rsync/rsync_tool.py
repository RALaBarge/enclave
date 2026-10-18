"""rsync-sbx: a DeskBox-backed enclave tool, not a Pi extension (TODO task 6).

Shape (b) was chosen.  Pi 1.2.0 does have a real, documented extension API: a
TypeScript module at ``~/.pi/agent/extensions/`` or ``--extension`` exports a
default factory receiving ``ExtensionAPI``, and ``pi.registerTool()`` takes a
``ToolDefinition`` with TypeBox ``parameters`` and an ``execute()`` method
(``packages/coding-agent/docs/extensions.md`` and
``src/core/extensions/types.ts`` at release commit
06a466935536f8c9bcacb2d5efc0fc021be1bdff, version 1.2.0).  So (a) is
writable; it is not chosen, for two reasons:

  * The same document says an extension "runs inside the Pi process with the
    same operating-system permissions".  An rsync tool there would be a
    network primitive owned by the ``pibridge`` account, in the core, where
    the output chain is ``policy accept``.  ``docs/HARNESS-ADMISSION.md``
    section 2 item 1/2 and section 3 ("It needs general network egress from
    the core") reject exactly that.  The sandbox's ``bwrap`` and the core's
    forward-chain allowlist would not see the bytes at all.
  * The 2026-10-18 "Where tools live" decision and admission item 2 say the
    desk is the only executor, and task 17's adapter is what exposes desk
    tools to a harness.  A Pi extension that called the desk would be a
    second copy of that adapter, not a tool.

The TODO decision row's "harness-side Pi extension recommended" is the older
shape; the same day's "Where tools live" decision and the admission criterion
supersede it.  A Pi extension is still the right place for the generated tool
*adapter* (task 17), not for the transport that moves the bytes.

So this is the same shape as every other tool: ``sbx/tools/rsync/run`` runs
inside the sandbox job, the desk holds ``harness/tools/rsync-sbx/tcs.yaml``,
and ``tools.list`` gives the job the network it needs.

Network, stated plainly: this tool moves bytes itself.  It invokes the
sandbox's ``rsync`` binary, so its ``tools.list`` entry says ``net`` and the
job gets ``bwrap --share-net``.  The *stub* is still ``network: false``: it
only posts to the broker over the unix socket the contract binds.  "The tool
has network" and "the stub has network" are different jobs in this repo.

Allowlist: a remote side is written ``<name>:<path>`` where ``<name>`` is an
entry in ``/opt/sbx/tools/rsync/allowlist`` on the sandbox box (inside the job
it is ``/deskbox/tool/allowlist``).  An entry is
``<name> = [user@]<host>[:port]``.  Names match exactly; the user and port come
only from the file, never from the model's path.  There are no wildcards, no
CIDR ranges and no default entry: an empty, missing or malformed allowlist
permits no remote host.  Local paths must be absolute and are limited to the
job's data directories: ``/deskbox/in`` (desk input files), ``/deskbox/out``
(desk output files) and ``/tmp``.  The tool's own directory is read-only and
holds the allowlist and SSH key, so it is not an allowed local path.

Dry-run first: ``apply`` defaults to false, and false means ``rsync --dry-run``.
Only ``"apply": true`` removes the flag.  A refusal is returned as a normal
JSON report with ``ok: false`` rather than a non-zero exit: the broker discards
a sandbox tool's stderr and turns a non-zero exit into the opaque
``tool_failed``, which would erase the reason the agent needs to see.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass

ALLOWLIST_PATH = "/deskbox/tool/allowlist"
ALLOWLIST_ENV = "SBX_RSYNC_ALLOWLIST"
LOCAL_ROOTS = ("/deskbox/in", "/deskbox/out", "/tmp")
MAX_PATH = 4096
MAX_ALLOWLIST_BYTES = 64 * 1024
OUTPUT_LIMIT = 65536
DETAIL_LIMIT = 500

# Exact names only: no "*", "?", "[" or "]" and no ":" (that starts the path).
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
# A DNS name or an IPv4 literal.  IPv6 literals are not supported: rsync's
# host:path syntax has no bracket form in the rsh transport this tool uses.
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,31}$")
# The prefix is deliberately permissive enough to catch a literal host or a
# user@host written in the path, so the refusal is "host not allowed" rather
# than a confusing "local path must be absolute".
REMOTE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,62}):(.*)$", re.S)
WILDCARDS = frozenset("*?[]")


class AllowlistError(ValueError):
    """The allowlist file is malformed; fail closed, permitting nothing."""


class InputError(ValueError):
    """The tool call itself is malformed."""


class Refusal(Exception):
    """A remote name is not allowed by the allowlist."""

    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class HostEntry:
    name: str
    host: str
    user: str | None = None
    port: int | None = None


@dataclass(frozen=True)
class Endpoint:
    spec: str
    remote: bool
    name: str | None = None
    path: str | None = None


def parse_allowlist(text, source="<allowlist>"):
    """Return {name: HostEntry}.  Raises AllowlistError on the first bad line.

    The whole file is parsed before any entry is used, so a typo on line 20
    cannot leave lines 1-19 silently active.
    """
    entries = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, value = line.partition("=")
        if not sep:
            raise AllowlistError(
                f"{source}:{lineno}: expected '<name> = [user@]<host>[:port]'"
            )
        name, value = name.strip(), value.strip()
        if not name or not value:
            raise AllowlistError(f"{source}:{lineno}: empty name or value")
        if not NAME_RE.fullmatch(name):
            raise AllowlistError(
                f"{source}:{lineno}: bad name {name!r} (letters, digits, '.', '_' and '-')"
            )
        if name in entries:
            raise AllowlistError(f"{source}:{lineno}: duplicate name {name!r}")
        if any(ch in value for ch in WILDCARDS):
            raise AllowlistError(
                f"{source}:{lineno}: wildcards are not permitted in {value!r}"
            )

        user = None
        hostport = value
        if "@" in hostport:
            user, _, hostport = hostport.partition("@")
            if not USER_RE.fullmatch(user):
                raise AllowlistError(f"{source}:{lineno}: bad user {user!r}")

        port = None
        if ":" in hostport:
            host, _, port_text = hostport.rpartition(":")
            if not port_text.isdigit():
                raise AllowlistError(
                    f"{source}:{lineno}: bad port {port_text!r} (expected 1-65535)"
                )
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise AllowlistError(
                    f"{source}:{lineno}: port {port} is outside 1-65535"
                )
        else:
            host = hostport

        if not HOST_RE.fullmatch(host):
            raise AllowlistError(
                f"{source}:{lineno}: bad host {host!r} (an exact name or IPv4 address; no wildcards or ranges)"
            )
        entries[name] = HostEntry(name=name, host=host, user=user, port=port)
    return entries


def load_allowlist(path=None):
    """Return (entries, path, error).  entries is None for an invalid file.

    A missing file is an empty allowlist, not an error: missing therefore
    permits nothing, the same as empty.  Unreadable and malformed files are
    errors and also permit nothing.
    """
    path = path or os.environ.get(ALLOWLIST_ENV) or ALLOWLIST_PATH
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(MAX_ALLOWLIST_BYTES + 1)
    except FileNotFoundError:
        return {}, path, None
    except UnicodeDecodeError:
        return None, path, f"{path}: allowlist is not valid UTF-8"
    except OSError as e:
        return None, path, f"{path}: cannot read allowlist: {e.strerror or e}"
    if text.startswith("\ufeff"):
        text = text[1:]
    if len(text) > MAX_ALLOWLIST_BYTES:
        return None, path, f"{path}: allowlist is larger than {MAX_ALLOWLIST_BYTES} bytes"
    try:
        return parse_allowlist(text, path), path, None
    except AllowlistError as e:
        return None, path, str(e)


def parse_endpoint(spec):
    """Parse '<name>:<path>' (remote) or a local path under an allowed root.

    Local paths are limited to the directories the job actually exposes for
    data.  The tool's own directory is read-only and contains the allowlist and
    the SSH key, so allowing an arbitrary absolute path would let a caller
    upload /deskbox/tool/id_ed25519 to a remote host.
    """
    if not isinstance(spec, str) or not spec:
        raise InputError("source and destination must be non-empty strings")
    if len(spec) > MAX_PATH:
        raise InputError(f"path is longer than {MAX_PATH} characters")
    if any(ch in spec for ch in "\x00\n\r"):
        raise InputError("paths may not contain NUL, newline or carriage return")
    match = REMOTE_RE.match(spec)
    if match:
        name, path = match.group(1), match.group(2)
        if not path:
            raise InputError(f"remote path is empty in {spec!r}")
        return Endpoint(spec=spec, remote=True, name=name, path=path)
    if not spec.startswith("/"):
        raise InputError(
            f"local paths must be absolute (write ./{spec} as /tmp/{spec}): {spec!r}"
        )
    local = posixpath.normpath(spec)
    if not any(local == root or local.startswith(root + "/") for root in LOCAL_ROOTS):
        raise InputError(
            "local paths must be under /deskbox/in, /deskbox/out or /tmp: "
            f"{spec!r}"
        )
    return Endpoint(spec=local, remote=False)


def resolve_remote(endpoint, entries):
    """Resolve an allowlisted name to rsync's [user@]host:path argument."""
    entry = entries.get(endpoint.name)
    if entry is None:
        known = ", ".join(sorted(entries)) if entries else "none"
        raise Refusal(
            "host_not_allowed",
            f"{endpoint.name!r} is not in the rsync allowlist (permitted names: {known})",
        )
    prefix = f"{entry.user}@{entry.host}" if entry.user else entry.host
    return f"{prefix}:{endpoint.path}", entry


def build_argv(source_arg, destination_arg, apply, port=None):
    """Build the rsync command.  Dry-run is the default; apply removes it."""
    argv = ["rsync"]
    if not apply:
        argv.append("--dry-run")
    argv += [
        "--recursive",  # directories, but not -a: no symlinks, owners or permissions
        "--protect-args",
        "--itemize-changes",
        "--human-readable",
        "--stats",
    ]
    ssh = (
        "ssh -o BatchMode=yes -o ConnectTimeout=15 "
        "-o StrictHostKeyChecking=yes "
        "-o UserKnownHostsFile=/deskbox/tool/known_hosts "
        "-o IdentitiesOnly=yes -i /deskbox/tool/id_ed25519"
    )
    if port:
        ssh += f" -p {port}"
    argv += ["-e", ssh, source_arg, destination_arg]
    return argv


def _clip(text, limit=OUTPUT_LIMIT):
    """Bound a field that the contract's output schema also bounds."""
    if len(text) <= limit:
        return text
    marker = "\n[truncated]"
    return text[: limit - len(marker)] + marker


def _decode(raw):
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _echo(value):
    """Echo a path without breaking the output schema's maxLength."""
    return value[:MAX_PATH] if isinstance(value, str) else ""


def _empty_refusal(reason, detail):
    return {
        "ok": False,
        "dry_run": True,
        "applied": False,
        "source": "",
        "destination": "",
        "command": [],
        "reason": reason,
        "detail": _clip(detail, DETAIL_LIMIT),
    }


def handle(payload, allowlist_path=None, runner=subprocess.run):
    """Turn one input object into one JSON-serialisable report object."""
    if not isinstance(payload, dict):
        return _empty_refusal("invalid_input", "input must be a JSON object")

    source = payload.get("source")
    destination = payload.get("destination")
    apply = payload.get("apply", False)
    apply_ok = isinstance(apply, bool)
    base = {
        "ok": False,
        "dry_run": (not apply) if apply_ok else True,
        "applied": False,
        "source": _echo(source),
        "destination": _echo(destination),
        "command": [],
    }

    def refuse(reason, detail, command=None):
        out = dict(base)
        out["command"] = list(command or [])
        out["reason"] = reason
        if detail:
            out["detail"] = _clip(detail, DETAIL_LIMIT)
        return out

    if not isinstance(source, str) or not source:
        return refuse("invalid_input", "source must be a non-empty string")
    if not isinstance(destination, str) or not destination:
        return refuse("invalid_input", "destination must be a non-empty string")
    if not apply_ok:
        return refuse("invalid_input", "apply must be the JSON boolean true or false")

    try:
        src = parse_endpoint(source)
        dst = parse_endpoint(destination)
    except InputError as e:
        return refuse("invalid_input", str(e))

    if src.remote and dst.remote:
        return refuse(
            "both_remote_unsupported",
            "rsync cannot copy directly between two remote hosts; make one side local",
        )
    if not src.remote and not dst.remote:
        return refuse(
            "no_remote_endpoint",
            "one side must name an allowlisted remote host as <name>:<path>",
        )

    remote = src if src.remote else dst
    entries, allowlist_path_used, error = load_allowlist(allowlist_path)
    if entries is None:
        return refuse("allowlist_invalid", error or "allowlist is invalid")
    try:
        remote_arg, entry = resolve_remote(remote, entries)
    except Refusal as e:
        detail = e.detail + f" (allowlist {allowlist_path_used}"
        if not os.path.exists(allowlist_path_used):
            detail += " is missing"
        detail += ")"
        return refuse(e.reason, detail)

    source_arg = remote_arg if src.remote else src.spec
    destination_arg = remote_arg if dst.remote else dst.spec
    argv = build_argv(source_arg, destination_arg, apply, entry.port)
    base["command"] = argv

    try:
        proc = runner(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return refuse(
            "rsync_not_installed",
            "rsync is not installed in the sandbox job (install it on the sandbox box)",
            command=argv,
        )
    except subprocess.TimeoutExpired:
        return refuse("rsync_timeout", "rsync did not finish", command=argv)
    except OSError as e:
        return refuse("rsync_start_failed", f"could not start rsync: {e}", command=argv)

    stdout = _decode(getattr(proc, "stdout", b""))
    stderr = _decode(getattr(proc, "stderr", b""))
    returncode = int(getattr(proc, "returncode", -1))
    # subprocess reports a signalled child as a negative number; the contract's
    # exit_code is 0..255, so use the shell's 128+signal spelling.
    exit_code = returncode if returncode >= 0 else 128 + abs(returncode)
    out = dict(base)
    out["ok"] = exit_code == 0
    out["applied"] = bool(apply and exit_code == 0)
    out["exit_code"] = exit_code
    if stdout:
        out["stdout"] = _clip(stdout)
    if stderr:
        out["stderr"] = _clip(stderr)
    if exit_code != 0:
        out["reason"] = "rsync_killed" if returncode < 0 else "rsync_failed"
        out["detail"] = (
            f"rsync was killed by signal {abs(returncode)}"
            if returncode < 0
            else f"rsync exited with status {exit_code}"
        )
    return out


def main(stdin=None, stdout=None, allowlist_path=None, runner=subprocess.run):
    """Read one JSON document, write one JSON report, return 0.

    A refusal or a failed rsync still exits 0 so the report survives the
    broker's envelope.  The report's ``ok`` field is the success signal.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        raw = stdin.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig", "replace")
        if raw.startswith("\ufeff"):
            raw = raw[1:]
        payload = json.loads(raw)
    except (ValueError, OSError) as e:
        result = _empty_refusal("invalid_input", f"stdin is not one JSON document: {e}")
    else:
        try:
            result = handle(payload, allowlist_path=allowlist_path, runner=runner)
        except Exception as e:  # a report is better than a traceback the broker drops
            result = _empty_refusal("internal_error", f"{type(e).__name__}: {e}")
    json.dump(result, stdout, separators=(",", ":"))
    stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
