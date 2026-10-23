#!/usr/bin/python3 -IB
"""sbx-install: replace the sandbox box's tools with the tar on stdin. Root helper behind sbx-channel's "install" verb,
run through sudo like sbx-reset. Installed on the sandbox box only, as /usr/local/sbin/sbx-install.

The tar holds tools.list, toollist.py, sbx-run and tools/<name>/run (plus anything else a tool keeps in its directory),
as the core's sbx-push-tools sends it. Nothing in the tar runs here as root: the tool names are read from tools.list
with a pattern (the core's sync.py and broker have parsed it in full, and sbx-run parses it again, as sbx, with the
toollist.py that comes with it), sbx-run must compile, and every listed tool must have an executable run. Nothing is
changed unless all of that checks out; the install itself (four renames) is not one transaction. So a change to sbx-run
or to the tools.list format reaches the box with a normal push. sbx-channel is not in it: that stays a
change made at the box's keyboard. The `work` tools' workspace, /var/lib/sbx-work, is made if missing.
A tool's gate file (page, fetch: the browserbox gate's URL and token) is made 0640 root:sbx, readable only by jobs.
sbx-run reads tools.list and binds the tool's directory per job, so the new set applies from the next job on, and a job
already running keeps the files it bound."""
import grp
import io
import os
import pwd
import py_compile
import re
import shutil
import subprocess
import sys
import tarfile

SBX, MAX = "/opt/sbx", 16 << 20
NEW = SBX + "/tools.new"
FILES = ("tools.list", "toollist.py", "sbx-run")      # top-level files the tar must carry, besides tools/
WORK = "/var/lib/sbx-work"
NAME_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,63})(?:\s|$)")      # toollist.TOOL_RE, as the first word of a line


def names(path):
    """The tool names in a tools.list, without running its parser."""
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.split("#", 1)[0].strip()
            if ln:
                m = NAME_RE.match(ln)
                if not m:
                    raise ValueError("tools.list: bad line %r" % ln[:80])
                out.append(m.group(1))
    return out


def die(msg):
    shutil.rmtree(NEW, ignore_errors=True)    # never leave a half-checked tree behind
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
            if not (m.isfile() and m.name in FILES or (m.isfile() or m.isdir()) and
                    (m.name == "tools" or m.name.startswith("tools/"))):
                die("refusing %r: only %s and regular files under tools/" % (m.name, ", ".join(FILES)))
        t.extractall(NEW, filter="data")      # no absolute paths, no "..", no setuid bits, owned by root
    for f in FILES:
        if not os.path.isfile(os.path.join(NEW, f)):
            raise ValueError("the tar has no %s" % f)
    listed = names(NEW + "/tools.list")
    py_compile.compile(NEW + "/sbx-run", cfile=NEW + "/.sbx-run.pyc", doraise=True)
except (tarfile.TarError, OSError, ValueError, py_compile.PyCompileError) as e:
    die(str(e))
for name in listed:
    run = "%s/tools/%s/run" % (NEW, name)
    if not os.path.isfile(run) or not os.access(run, os.X_OK):
        die("listed tool %s has no executable run" % name)

for name in os.listdir(NEW + "/tools"):
    gate = "%s/tools/%s/gate" % (NEW, name)
    if os.path.isfile(gate):
        os.chown(gate, 0, SBX_GID)
        os.chmod(gate, 0o640)

if not os.path.isdir(WORK):
    os.mkdir(WORK, 0o700)
    os.chown(WORK, pwd.getpwnam("sbx").pw_uid, SBX_GID)

# The parser first (it reads the old list too), then the tool directory in one step (renameat2 RENAME_EXCHANGE),
# the list, and sbx-run last.
os.chmod(NEW + "/toollist.py", 0o644)
os.replace(NEW + "/toollist.py", SBX + "/toollist.py")
subprocess.run(["mv", "--exchange", "-T", NEW + "/tools", SBX + "/tools"], check=True)
os.replace(NEW + "/tools.list", SBX + "/tools.list")
os.chmod(NEW + "/sbx-run", 0o755)
os.replace(NEW + "/sbx-run", SBX + "/sbx-run")
shutil.rmtree(NEW)
print("sbx-install: %d tools: %s" % (len(listed), " ".join(sorted(listed))))
