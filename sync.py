#!/usr/bin/env python3
"""Checks the tools (the DeskboxTools submodule at tools/) and installs them into the desk.

  python3 sync.py                validate (the same as --check)
  python3 sync.py --check        validate only (CI / pre-deploy)
  python3 sync.py --deploy DIR   validate, then install into DIR (the desk's tools dir); never writes the checkout

For each tool in tools/tools.list it checks tools/<name>/run and tools/<name>/tcs.yaml exist, and that the contract
still says what the stub assumes: network:false, and a bind of the broker's socket. That pair is what makes "all tool
calls go through the desk" true without giving the job the host network. Reports tool dirs that are not listed.

The desk's run.py for every tool is the generic stub harness/stub/run.py with its client timeout sized for that tool;
it is generated here, at check and deploy time, and never committed, so it cannot drift from the stub.

--deploy is the step that puts tools where the desk loads them (/opt/deskbox/tools): DIR/<name>-sbx/tcs.yaml (0600)
and the generated run.py (0700), so only the desk's account can execute it.
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True     # --deploy runs as root over a user's checkout: not even __pycache__
sys.path.insert(0, os.path.join(ROOT, "common"))
import toollist  # noqa: E402

deploy_dir = None
if "--deploy" in sys.argv:
    i = sys.argv.index("--deploy")
    if i + 1 >= len(sys.argv):
        print("--deploy needs a directory", file=sys.stderr)
        sys.exit(2)
    deploy_dir = sys.argv[i + 1]
    # --deploy validates exactly as --check does, then installs: nothing ships that --check would refuse, and nothing
    # in the checkout is written. It runs as root over a tree a user owns.

TOOLS = os.path.join(ROOT, "tools")
tools = toollist.load(os.path.join(TOOLS, "tools.list"))
problems = []

QUEUE_WAIT_S = 300           # sbx_broker.py default: covers the previous job's reset, which runs after its reply
RESET_BUDGET_S = 300         # sbx_broker.py: reset wall timeout (a failed reset is retried before the next job)
MARGIN_S = 30
STUB_TIMEOUT_RE = re.compile(r"^CLIENT_TIMEOUT = \d+$", re.M)
TIMEOUT_MS_RE = re.compile(r"^\s*timeout_ms:[ \t]*(\d+)\s*(?:#.*)?$", re.M)


def stub_timeout(name):
    """The broker's worst case for one call, in seconds: wait in the queue (which includes the previous job's reset),
    retry a failed reset, run the tool for its tools.list budget plus sbx-run's 15 s, and a margin. The stub's client
    timeout is this; the contract's timeout_ms must cover it too, or the desk kills a job the broker is still running
    and records a failure for work that succeeded."""
    return int(QUEUE_WAIT_S + RESET_BUDGET_S + tools[name]["timeout_s"] + 15 + MARGIN_S)


def contract_timeout_s(tcs_path):
    with open(tcs_path, encoding="utf-8") as f:
        m = TIMEOUT_MS_RE.search(f.read())
    return int(m.group(1)) / 1000 if m else 0


# The two facts a generated stub relies on. Textual on purpose: sync.py stays
# stdlib-only, so it cannot parse YAML the way the desk does. tcs-verify is
# the thing that validates the contract properly; this is the cheap check
# that the invariant is still written down, and it is written to fail on the
# shapes a YAML parser would also refuse to read the way the author meant:
# two `network:` keys (the last one wins in YAML, so counting matters), or a
# value that is not the literal `false`.
BIND_RE = re.compile(r"^[ \t]*-[ \t]*src:[ \t]*\S*/broker\.sock[ \t]*(?:#.*)?$", re.M)
BIND_DST_RE = re.compile(r"^[ \t]+dst:[ \t]*/deskbox/broker\.sock[ \t]*(?:#.*)?$", re.M)
NET_KEY_RE = re.compile(r"^[ \t]*network:[ \t]*(\S+)[ \t]*(?:#.*)?$", re.M)


def contract_problems(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    out = []
    nets = NET_KEY_RE.findall(text)
    if nets != ["false"]:
        out.append(
            f"{path}: allowed_side_effects.network must be exactly one `network: false` "
            f"(found {nets or 'none'}) — the stub reaches the broker over a bound unix "
            f"socket, never the network")
    if not BIND_RE.search(text) or not BIND_DST_RE.search(text):
        out.append(
            f"{path}: sandbox.bind must mount sbx-broker's socket at /deskbox/broker.sock "
            f"(src .../broker.sock, dst /deskbox/broker.sock) — without both, a network:false "
            f"stub cannot call the broker")
    return out


with open(os.path.join(ROOT, "harness", "stub", "run.py"), encoding="utf-8") as fh:
    STUB = fh.read()


def stub_for(name):
    """The desk's run.py for one tool: the stub, with the client timeout sized to the broker's worst case."""
    return STUB_TIMEOUT_RE.sub(f"CLIENT_TIMEOUT = {stub_timeout(name)}", STUB).encode()


for name in tools:
    run = os.path.join(TOOLS, name, "run")
    tcs = os.path.join(TOOLS, name, "tcs.yaml")
    if not os.path.isfile(run):
        problems.append(f"missing sandbox tool: {os.path.relpath(run, ROOT)}")
    if not os.path.isfile(tcs):
        problems.append(f"missing contract:     {os.path.relpath(tcs, ROOT)}")
        continue
    problems.extend(contract_problems(tcs))
    # The desk sets DESKBOX_TOOL to the contract's name, the stub sends it as `caller`, and the broker accepts only
    # "<tool>-sbx". The contract must say the same.
    with open(tcs, encoding="utf-8") as fh:
        m = re.search(r"^name:[ \t]*['\"]?([^\s'\"#]+)", fh.read(), re.M)
    if not m or m.group(1) != f"{name}-sbx":
        problems.append(
            f"contract name:      {os.path.relpath(tcs, ROOT)} says {m.group(1) if m else 'nothing'!r}, must be "
            f"'{name}-sbx' (the broker only accepts a caller named <tool>-sbx)")
    if contract_timeout_s(tcs) < stub_timeout(name) + 5:
        problems.append(
            f"contract timeout:   {os.path.relpath(tcs, ROOT)} timeout_ms is {contract_timeout_s(tcs):g}s, needs "
            f">= {stub_timeout(name) + 5}s (the stub waits {stub_timeout(name)}s for the broker)")

if not STUB_TIMEOUT_RE.search(STUB):
    problems.append("harness/stub/run.py has no `CLIENT_TIMEOUT = <n>` line to size")

for d in sorted(os.listdir(TOOLS)):
    if os.path.isdir(os.path.join(TOOLS, d)) and not d.startswith(".") and d not in tools:
        problems.append(f"unlisted tool dir:   tools/{d} (not in tools/tools.list)")

# If the desk's own linter is on PATH, use it on each tool as the desk will load it (the contract plus the generated
# stub, in a scratch dir). The checks above are textual and deliberately cheap; tcs-verify parses the contract the way
# the gate does, so it is what catches a schema keyword the validator would silently ignore (a `default:` is the one
# that got through once). Absent, this step is skipped rather than failing: a build host without the Go binary should
# still be able to run sync.py.
verifier = shutil.which("tcs-verify")
if verifier and not problems:
    with tempfile.TemporaryDirectory() as scratch:
        for name in tools:
            tdir = os.path.join(scratch, f"{name}-sbx")
            os.mkdir(tdir)
            shutil.copyfile(os.path.join(TOOLS, name, "tcs.yaml"), os.path.join(tdir, "tcs.yaml"))
            with open(os.path.join(tdir, "run.py"), "wb") as fh:
                fh.write(stub_for(name))
            os.chmod(os.path.join(tdir, "run.py"), 0o755)
            r = subprocess.run([verifier, tdir], capture_output=True, text=True)
            if r.returncode != 0:
                detail = "; ".join(ln.strip() for ln in (r.stdout or "").splitlines() if "FAIL" in ln)
                problems.append(f"tcs-verify:         tools/{name}: {detail or 'rejected'}")

print(f"{len(tools)} tool(s): {', '.join(tools)}")
for p in problems:
    print("PROBLEM:", p)

if problems:
    sys.exit(1)

if deploy_dir:
    # The destination tree is owned by the desk's account while this runs as
    # root, so every step is relative to a directory fd opened with
    # O_NOFOLLOW: a symlink planted where a directory or a file belongs makes
    # the open fail instead of redirecting a root write (and chmod) elsewhere.
    # Files are created O_EXCL under a temporary name with their final mode,
    # then renamed over the old one, which replaces a link rather than
    # following it.
    if not hasattr(os, "O_NOFOLLOW"):
        print("--deploy needs a POSIX system", file=sys.stderr)
        sys.exit(2)
    DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    os.makedirs(deploy_dir, mode=0o700, exist_ok=True)
    try:
        pfd = os.open(deploy_dir, DIR_FLAGS)
    except OSError as e:
        sys.exit(f"refusing --deploy: {deploy_dir}: {e.strerror}")
    for name in tools:
        src = os.path.join(TOOLS, name)
        dst = os.path.join(deploy_dir, f"{name}-sbx")
        try:
            try:
                os.mkdir(f"{name}-sbx", 0o700, dir_fd=pfd)
            except FileExistsError:
                pass
            dfd = os.open(f"{name}-sbx", DIR_FLAGS, dir_fd=pfd)
        except OSError as e:
            sys.exit(f"refusing --deploy: {dst}: {e.strerror}")
        try:
            for f in ("run.py", "tcs.yaml"):
                if f == "run.py":
                    data = stub_for(name)
                else:
                    # The source is a user's checkout: a link there must not make root copy some other file in.
                    sfd = os.open(os.path.join(src, f), os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(sfd, "rb") as fh:
                        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                            sys.exit(f"refusing --deploy: {src}/{f} is not a regular file")
                        data = fh.read()
                tmp = f".{f}.deploy-{os.getpid()}"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o700 if f == "run.py" else 0o600, dir_fd=dfd)
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                os.rename(tmp, f, src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)
        print(f"deployed {dst}")
    os.close(pfd)
    print("Re-pin after any change to the stub or a timeout: agent-desk -pin <tool dir>, then paste "
          "integrity.run_sha256 into that tool's tcs.yaml and re-deploy.")
