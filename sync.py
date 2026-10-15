#!/usr/bin/env python3
"""Reads tools.list and keeps the repo consistent with it.

  python3 sync.py          generate stubs, then validate
  python3 sync.py --check  validate only (CI / pre-deploy)

For each tool in tools.list it checks sbx/tools/<name>/run and
harness/tools/<name>-sbx/tcs.yaml exist, and (re)writes the generic stub
harness/stub/run.py into harness/tools/<name>-sbx/run.py. Reports -sbx tool
dirs that are no longer listed. Never deletes anything.
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "common"))
import toollist  # noqa: E402

check_only = "--check" in sys.argv
tools = toollist.load(os.path.join(ROOT, "tools.list"))
problems = []

for name in tools:
    run = os.path.join(ROOT, "sbx", "tools", name, "run")
    hdir = os.path.join(ROOT, "harness", "tools", f"{name}-sbx")
    if not os.path.isfile(run):
        problems.append(f"missing sandbox tool: {os.path.relpath(run, ROOT)}")
    if not os.path.isfile(os.path.join(hdir, "tcs.yaml")):
        problems.append(f"missing contract:     {os.path.relpath(hdir, ROOT)}/tcs.yaml")
    elif not check_only:
        shutil.copyfile(os.path.join(ROOT, "harness", "stub", "run.py"),
                        os.path.join(hdir, "run.py"))

hroot = os.path.join(ROOT, "harness", "tools")
for d in sorted(os.listdir(hroot)):
    if d.endswith("-sbx") and d[:-4] not in tools:
        problems.append(f"unlisted tool dir:   harness/tools/{d} (not in tools.list)")

print(f"{len(tools)} tool(s): {', '.join(tools)}")
for p in problems:
    print("PROBLEM:", p)
sys.exit(1 if problems else 0)
