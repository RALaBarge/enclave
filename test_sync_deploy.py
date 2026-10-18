#!/usr/bin/env python3
"""sync.py --deploy must not follow a symlink planted in the deploy tree.

The tree belongs to the desk's account while deploy runs as root. POSIX only.
Run from the repo root:  python3 test_sync_deploy.py
"""
import os
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


if __name__ == "__main__":
    unittest.main()
