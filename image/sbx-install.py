#!/usr/bin/python3 -IB
"""sbx-install: replace the sandbox box's tools with the tar on stdin. Root helper behind sbx-channel's "install" verb,
run through sudo like sbx-reset. Installed on the sandbox box only, as /usr/local/sbin/sbx-install.

The tar holds tools.list and tools/<name>/run (plus anything else a tool keeps in its directory), as the core's
sbx-push-tools sends it. Every listed tool must have an executable run; nothing is changed unless all of it checks out.
A tool's gate file (page, fetch: the browserbox gate's URL and token) is made 0640 root:sbx, readable only by jobs.
sbx-run reads tools.list and binds the tool's directory per job, so the new set applies from the next job on, and a job
already running keeps the files it bound."""
import grp
import io
import os
import shutil
import subprocess
import sys
import tarfile

sys.path.insert(0, "/opt/sbx")
import toollist  # noqa: E402

SBX, MAX = "/opt/sbx", 16 << 20
NEW = SBX + "/tools.new"


def die(msg):
    print("sbx-install: " + msg, file=sys.stderr)
    sys.exit(1)


if os.getuid() != 0:
    die("must run as root")
try:
    SBX_GID = grp.getgrnam("sbx").gr_gid
except KeyError:
    die("no sbx group; a gate file would be readable by root only")
data = sys.stdin.buffer.read(MAX + 1)
if len(data) > MAX:
    die("tar larger than %d bytes" % MAX)

shutil.rmtree(NEW, ignore_errors=True)
os.mkdir(NEW, 0o755)
try:
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        for m in t.getmembers():
            if not (m.isfile() or m.isdir()) or not (m.name == "tools.list" or m.name == "tools" or m.name.startswith("tools/")):
                die("refusing %r: only tools.list and regular files under tools/" % m.name)
        t.extractall(NEW, filter="data")      # no absolute paths, no "..", no setuid bits, owned by root
    listed = toollist.load(NEW + "/tools.list")
except (tarfile.TarError, OSError, ValueError) as e:
    shutil.rmtree(NEW, ignore_errors=True)
    die(str(e))
for name in listed:
    run = "%s/tools/%s/run" % (NEW, name)
    if not os.path.isfile(run) or not os.access(run, os.X_OK):
        shutil.rmtree(NEW, ignore_errors=True)
        die("listed tool %s has no executable run" % name)

for name in os.listdir(NEW + "/tools"):
    gate = "%s/tools/%s/gate" % (NEW, name)
    if os.path.isfile(gate):
        os.chown(gate, 0, SBX_GID)
        os.chmod(gate, 0o640)

# Swap the directory in one step (renameat2 RENAME_EXCHANGE), then the list.
subprocess.run(["mv", "--exchange", "-T", NEW + "/tools", SBX + "/tools"], check=True)
os.replace(NEW + "/tools.list", SBX + "/tools.list")
shutil.rmtree(NEW)
print("sbx-install: %d tools: %s" % (len(listed), " ".join(sorted(listed))))
