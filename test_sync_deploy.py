#!/usr/bin/env python3
"""sync.py --deploy must not follow a symlink planted in the deploy tree.

The tree belongs to the desk's account while deploy runs as root. POSIX only.
Run from the repo root:  python3 test_sync_deploy.py
"""
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))


def deploy(target):
    return subprocess.run([sys.executable, os.path.join(ROOT, "sync.py"), "--deploy", target],
                          capture_output=True, text=True)


@unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "POSIX only")
class DeployDoesNotFollowLinks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = os.path.join(self.tmp.name, "tools")
        self.victim = os.path.join(self.tmp.name, "victim")
        with open(self.victim, "w") as f:
            f.write("ORIGINAL\n")
        os.chmod(self.victim, 0o644)

    def test_clean_deploy_sets_modes(self):
        r = deploy(self.dest)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(oct(os.stat(os.path.join(self.dest, "greet-sbx", "run.py")).st_mode & 0o777), "0o700")
        self.assertEqual(oct(os.stat(os.path.join(self.dest, "greet-sbx", "tcs.yaml")).st_mode & 0o777), "0o600")

    def test_planted_file_link_is_replaced_not_followed(self):
        self.assertEqual(deploy(self.dest).returncode, 0)
        run = os.path.join(self.dest, "greet-sbx", "run.py")
        os.remove(run)
        os.symlink(self.victim, run)
        self.assertEqual(deploy(self.dest).returncode, 0)
        self.assertEqual(open(self.victim).read(), "ORIGINAL\n")
        self.assertEqual(oct(os.stat(self.victim).st_mode & 0o777), "0o644")
        self.assertFalse(os.path.islink(run))

    def test_planted_directory_link_is_refused(self):
        os.makedirs(self.dest)
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(self.dest, "greet-sbx"))
        r = deploy(self.dest)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_deploy_dir_itself_a_link_is_refused(self):
        real = os.path.join(self.tmp.name, "real")
        os.makedirs(real)
        os.symlink(real, self.dest)
        r = deploy(self.dest)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.listdir(real), [])



def tree_digest(top):
    out = {}
    for d, _, files in os.walk(top):
        for f in files:
            p = os.path.join(d, f)
            st = os.lstat(p)
            out[os.path.relpath(p, top)] = (stat.S_IMODE(st.st_mode),
                                            os.readlink(p) if stat.S_ISLNK(st.st_mode) else
                                            hashlib.sha256(open(p, "rb").read()).hexdigest())
    return out


@unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "POSIX only")
class DeployNeverWritesTheCheckout(unittest.TestCase):
    """--deploy runs as root over a user's checkout: it validates and copies, and the checkout stays as it was."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        for part in ("sync.py", "tools.list", "common", "harness/stub", "harness/tools", "sbx/tools"):
            src, dst = os.path.join(ROOT, part), os.path.join(self.repo, part)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)
        self.dest = os.path.join(self.tmp.name, "tools")

    def run_deploy(self):
        return subprocess.run([sys.executable, "-B", os.path.join(self.repo, "sync.py"), "--deploy", self.dest],
                              capture_output=True, text=True)

    def test_deploy_from_a_read_only_checkout_changes_nothing(self):
        before = tree_digest(self.repo)
        for d, dirs, files in os.walk(self.repo):
            for n in dirs + files:
                os.chmod(os.path.join(d, n), os.stat(os.path.join(d, n)).st_mode & ~0o222)
        self.addCleanup(lambda: [os.chmod(os.path.join(d, n), 0o755) for d, dirs, _ in os.walk(self.repo) for n in dirs])
        r = self.run_deploy()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = {k: (m | 0o222 & before[k][0], h) for k, (m, h) in tree_digest(self.repo).items()}
        self.assertEqual({k: h for k, (_, h) in after.items()}, {k: h for k, (_, h) in before.items()})
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "greet-sbx", "run.py")))

    def test_a_stale_stub_is_refused_not_regenerated(self):
        stub = os.path.join(self.repo, "harness", "tools", "greet-sbx", "run.py")
        with open(stub, "a") as f:
            f.write("# edited\n")
        r = self.run_deploy()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stub out of date", r.stdout)
        self.assertTrue(open(stub).read().endswith("# edited\n"))
        self.assertFalse(os.path.exists(self.dest))

    def test_a_link_in_the_checkout_is_not_followed(self):
        secret = os.path.join(self.tmp.name, "secret")
        with open(secret, "w") as f:
            f.write("SECRET\n")
        tcs = os.path.join(self.repo, "harness", "tools", "greet-sbx", "tcs.yaml")
        real = open(tcs).read()
        os.remove(tcs)
        with open(secret, "a") as f:            # passes the textual checks, so only the open can refuse it
            f.write(real)
        os.symlink(secret, tcs)
        r = self.run_deploy()
        self.assertNotEqual(r.returncode, 0)
        copied = os.path.join(self.dest, "greet-sbx", "tcs.yaml")
        self.assertFalse(os.path.exists(copied) and "SECRET" in open(copied).read())


if __name__ == "__main__":
    unittest.main()
