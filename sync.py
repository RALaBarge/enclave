#!/usr/bin/env python3
"""Reads tools.list and keeps the repo consistent with it.

  python3 sync.py                generate stubs, then validate
  python3 sync.py --check        validate only (CI / pre-deploy)
  python3 sync.py --deploy DIR   validate, then install into DIR (the desk's tools dir)

For each tool in tools.list it checks sbx/tools/<name>/run and
harness/tools/<name>-sbx/tcs.yaml exist, (re)writes the generic stub
harness/stub/run.py into harness/tools/<name>-sbx/run.py, and checks the
contract still says what the stub assumes: network:false, and a bind of the
broker's socket. That pair is what makes "all tool calls go through the desk"
true without giving the job the host network. Reports -sbx tool dirs that are
no longer listed. Never deletes anything.

--deploy is the step that was missing: the desk loads /opt/deskbox/tools, and
nothing used to put anything there. It copies each contract's folder in with
the run script at 0700, so only the desk's account can execute it.
"""
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "common"))
import toollist  # noqa: E402

check_only = "--check" in sys.argv
deploy_dir = None
if "--deploy" in sys.argv:
    i = sys.argv.index("--deploy")
    if i + 1 >= len(sys.argv):
        print("--deploy needs a directory", file=sys.stderr)
        sys.exit(2)
    deploy_dir = sys.argv[i + 1]
    # --deploy regenerates and validates first, then installs: nothing ships
    # that this run would have refused.
    check_only = False

tools = toollist.load(os.path.join(ROOT, "tools.list"))
problems = []

# The two facts a generated stub relies on. Textual on purpose: sync.py stays
# stdlib-only, so it cannot parse YAML the way the desk does. tcs-verify is
# the thing that validates the contract properly; this is the cheap check
# that the invariant is still written down, and it is written to fail on the
# shapes a YAML parser would also refuse to read the way the author meant:
# two `network:` keys (the last one wins in YAML, so counting matters), or a
# value that is not the literal `false`.
BIND_RE = re.compile(r"^[ \t]*-[ \t]*src:[ \t]*\S*/broker\.sock[ \t]*(?:#.*)?$", re.M)
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
    if not BIND_RE.search(text):
        out.append(
            f"{path}: sandbox.bind must mount sbx-broker's socket (src .../broker.sock) — "
            f"without it a network:false stub cannot call the broker at all")
    return out


for name in tools:
    run = os.path.join(ROOT, "sbx", "tools", name, "run")
    hdir = os.path.join(ROOT, "harness", "tools", f"{name}-sbx")
    tcs = os.path.join(hdir, "tcs.yaml")
    if not os.path.isfile(run):
        problems.append(f"missing sandbox tool: {os.path.relpath(run, ROOT)}")
    if not os.path.isfile(tcs):
        problems.append(f"missing contract:     {os.path.relpath(hdir, ROOT)}/tcs.yaml")
    else:
        problems.extend(contract_problems(os.path.relpath(tcs, ROOT)))
        stub = os.path.join(ROOT, "harness", "stub", "run.py")
        copy = os.path.join(hdir, "run.py")
        if not check_only:
            shutil.copyfile(stub, copy)
        elif not os.path.isfile(copy) or open(copy, "rb").read() != open(stub, "rb").read():
            # A copy that has drifted from the stub is a tool running edited
            # code that nobody reviewed as a stub change. --check is what CI
            # runs, so it has to notice.
            problems.append(
                f"stub out of date:    {os.path.relpath(copy, ROOT)} differs from "
                f"harness/stub/run.py (run `python sync.py`)")

hroot = os.path.join(ROOT, "harness", "tools")
for d in sorted(os.listdir(hroot)):
    if d.endswith("-sbx") and d[:-4] not in tools:
        problems.append(f"unlisted tool dir:   harness/tools/{d} (not in tools.list)")

print(f"{len(tools)} tool(s): {', '.join(tools)}")
for p in problems:
    print("PROBLEM:", p)

if problems:
    sys.exit(1)

if deploy_dir:
    os.makedirs(deploy_dir, mode=0o700, exist_ok=True)
    for name in tools:
        src = os.path.join(hroot, f"{name}-sbx")
        dst = os.path.join(deploy_dir, f"{name}-sbx")
        os.makedirs(dst, mode=0o700, exist_ok=True)
        for f in ("run.py", "tcs.yaml"):
            target = os.path.join(dst, f)
            shutil.copyfile(os.path.join(src, f), target)
            os.chmod(target, 0o700 if f == "run.py" else 0o600)
        print(f"deployed {dst}")
    print("Re-pin after any edit to a run script: agent-desk -pin <tool dir>, then paste "
          "integrity.run_sha256 into that tool's tcs.yaml and re-deploy.")
