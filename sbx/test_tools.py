"""The workspace tools run in sbx-run's own bwrap layout, against a throwaway workspace: python3 -B sbx/test_tools.py
Linux with bubblewrap and unprivileged user namespaces (WSL works); no root, no box."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common"))
loader = importlib.machinery.SourceFileLoader("sbx_run", os.path.join(HERE, "sbx-run"))
spec = importlib.util.spec_from_loader("sbx_run", loader)
sbx_run = importlib.util.module_from_spec(spec)
loader.exec_module(sbx_run)


class Tools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work = os.path.join(self.tmp, "work")
        for d in ("work", "in", "out"):
            os.mkdir(os.path.join(self.tmp, d))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_tool(self, tool, doc, work=True):
        args = sbx_run.bwrap_args(os.path.join(HERE, "..", "tools", tool), os.path.join(self.tmp, "in"),
                                  os.path.join(self.tmp, "out"), False, self.work if work else None)
        p = subprocess.run(args, input=json.dumps(doc).encode(), capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        return json.loads(p.stdout)

    def test_write_read_edit(self):
        self.assertEqual(self.run_tool("write", {"file_path": "src/a.py", "content": "x = 1\ny = 2\nx = 1\n"})["bytes"], 18)
        self.assertEqual(self.run_tool("read", {"file_path": "src/a.py"}),
                         {"content": "     1\tx = 1\n     2\ty = 2\n     3\tx = 1", "total_lines": 3})
        self.assertEqual(self.run_tool("read", {"file_path": "/deskbox/work/src/a.py", "offset": 2, "limit": 1})["content"],
                         "     2\ty = 2")
        self.assertIn("2 times", self.run_tool("edit", {"file_path": "src/a.py", "old_string": "x = 1", "new_string": "x = 3"})["error"])
        self.assertEqual(self.run_tool("edit", {"file_path": "src/a.py", "old_string": "y = 2", "new_string": "y = 5"})["replacements"], 1)
        self.assertEqual(self.run_tool("edit", {"file_path": "src/a.py", "old_string": "x = 1", "new_string": "x = 3",
                                                "replace_all": True})["replacements"], 2)
        self.assertEqual(open(os.path.join(self.work, "src/a.py")).read(), "x = 3\ny = 5\nx = 3\n")
        self.assertIn("not found", self.run_tool("edit", {"file_path": "src/a.py", "old_string": "zzz", "new_string": "q"})["error"])

    def test_paths_stay_in_the_workspace(self):
        os.symlink("/etc/passwd", os.path.join(self.work, "link"))
        for tool, doc in (("read", {"file_path": "../in/x"}), ("read", {"file_path": "/etc/passwd"}),
                          ("read", {"file_path": "link"}), ("write", {"file_path": "../out/x", "content": "a"}),
                          ("write", {"file_path": "link", "content": "a"}),
                          ("edit", {"file_path": "/etc/passwd", "old_string": "root", "new_string": "x"}),
                          ("glob", {"pattern": "*", "path": "/etc"}), ("glob", {"pattern": "/etc/*"}),
                          ("grep", {"pattern": "root", "path": "/etc"}), ("bash", {"command": "true", "workdir": "/tmp"})):
            self.assertIn("error", self.run_tool(tool, doc), (tool, doc))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "out")), [])

    def test_glob_and_grep(self):
        for name, text in (("a.py", "import os\n"), ("sub/b.py", "x\nIMPORT re\n"), ("c.txt", "import\n"),
                           (".git/d.py", "import\n"), ("bin.py", "import\0\n")):
            os.makedirs(os.path.dirname(os.path.join(self.work, name)), exist_ok=True)
            with open(os.path.join(self.work, name), "w") as f:
                f.write(text)
        self.assertEqual(sorted(self.run_tool("glob", {"pattern": "**/*.py"})["files"]),
                         ["/deskbox/work/a.py", "/deskbox/work/bin.py", "/deskbox/work/sub/b.py"])
        self.assertEqual(self.run_tool("grep", {"pattern": "^import", "include": "*.py", "ignore_case": True})["matches"],
                         ["/deskbox/work/a.py:1:import os", "/deskbox/work/sub/b.py:2:IMPORT re"])
        self.assertIn("bad pattern", self.run_tool("grep", {"pattern": "("})["error"])

    def test_glob_and_grep_do_not_follow_a_symlink_out(self):
        os.symlink("/deskbox/tool/run", os.path.join(self.work, "tool-link"))
        self.assertEqual(self.run_tool("glob", {"pattern": "tool-link"})["files"], [])
        self.assertIn("error", self.run_tool("glob", {"pattern": "../tool/*"}))
        self.assertEqual(self.run_tool("grep", {"pattern": "#!"})["matches"], [])
        self.assertIn("error", self.run_tool("grep", {"pattern": "#", "path": "tool-link"}))

    def test_bash(self):
        r = self.run_tool("bash", {"command": "mkdir -p d && echo hi > d/f && cat d/f && pwd && echo err >&2; exit 3"})
        self.assertEqual(r, {"stdout": "hi\n/deskbox/work\n", "stderr": "err\n", "exit_code": 3, "timed_out": False})
        self.assertTrue(os.path.isfile(os.path.join(self.work, "d", "f")))          # the workspace persists
        self.assertEqual(self.run_tool("bash", {"command": "pwd", "workdir": "d"})["stdout"], "/deskbox/work/d\n")
        r = self.run_tool("bash", {"command": "touch /usr/x /tmp/y; ls /deskbox; getent hosts example.com || echo nonet"})
        self.assertIn("Read-only file system", r["stderr"])
        self.assertIn("nonet", r["stdout"])
        r = self.run_tool("bash", {"command": "sleep 5 & sleep 30", "timeout_ms": 1000})
        self.assertTrue(r["timed_out"])
        r = self.run_tool("bash", {"command": "head -c 40000 /dev/zero | tr '\\\\0' a"})
        self.assertIn("[cut: 10000 more characters]", r["stdout"])

    def test_no_work_flag_no_workspace(self):
        args = sbx_run.bwrap_args(os.path.join(HERE, "..", "tools", "greet"), os.path.join(self.tmp, "in"),
                                  os.path.join(self.tmp, "out"), False, None)
        self.assertNotIn("/deskbox/work", args)


if __name__ == "__main__":
    unittest.main()
