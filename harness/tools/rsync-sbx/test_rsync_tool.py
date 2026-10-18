"""Tests for the rsync-sbx tool: allowlist parsing, resolution/refusal, argv
building, and the dry-run/apply decision.  No network and no real rsync.

Run: python harness/tools/rsync-sbx/test_rsync_tool.py -v
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(ROOT, "sbx", "tools", "rsync"))
import rsync_tool as R  # noqa: E402

ALLOW = """\
# a comment, then three named hosts
backup = ryan@backup.example.com
nas = 10.0.0.10:22
plain = files.example.com
"""


class FakeRunner:
    """Stands in for subprocess.run; records the argv and never touches the net."""

    def __init__(self, returncode=0, stdout=b"itemized\n", stderr=b"", exc=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.exc = exc
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.allow = os.path.join(self.tmp.name, "allowlist")
        self.write_allow(ALLOW)

    def write_allow(self, text):
        with open(self.allow, "w", encoding="utf-8") as f:
            f.write(text)

    def call(self, *, runner=None, **overrides):
        runner = runner or FakeRunner()
        payload = {"source": "/tmp/a", "destination": "backup:/srv/a"}
        payload.update(overrides)
        return R.handle(payload, allowlist_path=self.allow, runner=runner), runner


class AllowlistParsing(unittest.TestCase):
    def test_parses_name_user_host_port(self):
        entries = R.parse_allowlist(ALLOW)
        self.assertEqual(entries["backup"], R.HostEntry("backup", "backup.example.com", "ryan", None))
        self.assertEqual(entries["nas"], R.HostEntry("nas", "10.0.0.10", None, 22))
        self.assertEqual(entries["plain"], R.HostEntry("plain", "files.example.com", None, None))

    def test_comments_and_blank_lines_ignored(self):
        self.assertEqual(R.parse_allowlist("# only a comment\n\n   \n"), {})

    def test_empty_file_permits_nothing(self):
        self.assertEqual(R.parse_allowlist(""), {})

    def test_wildcard_host_is_rejected(self):
        with self.assertRaises(R.AllowlistError):
            R.parse_allowlist("backup = *.example.com")

    def test_cidr_is_not_a_named_host(self):
        with self.assertRaises(R.AllowlistError):
            R.parse_allowlist("lan = 10.0.0.0/8")

    def test_duplicate_name_is_rejected(self):
        with self.assertRaises(R.AllowlistError):
            R.parse_allowlist("nas = 10.0.0.10\nnas = 10.0.0.11\n")

    def test_bad_port_is_rejected(self):
        with self.assertRaises(R.AllowlistError):
            R.parse_allowlist("nas = 10.0.0.10:0\n")

    def test_bad_user_is_rejected(self):
        with self.assertRaises(R.AllowlistError):
            R.parse_allowlist("nas = bad user@10.0.0.10\n")

    def test_user_may_not_start_with_a_dash(self):
        # A user that starts with "-" would put an option-looking string at the
        # front of rsync's remote argument.
        with self.assertRaises(R.AllowlistError):
            R.parse_allowlist("nas = -oProxyCommand@10.0.0.10\n")

    def test_missing_line_separator_is_rejected(self):
        with self.assertRaises(R.AllowlistError):
            R.parse_allowlist("nas 10.0.0.10\n")

    def test_missing_file_is_an_empty_allowlist(self):
        entries, path, error = R.load_allowlist(os.path.join(tempfile.gettempdir(), "does-not-exist-rsync"))
        self.assertEqual(entries, {})
        self.assertIsNone(error)
        self.assertTrue(path.endswith("does-not-exist-rsync"))

    def test_utf8_bom_is_accepted(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".allow", delete=False) as f:
            f.write("\ufeff" + ALLOW)
            path = f.name
        self.addCleanup(os.unlink, path)
        entries, _, error = R.load_allowlist(path)
        self.assertIsNone(error)
        self.assertIn("backup", entries)

    def test_non_utf8_allowlist_is_invalid(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".allow", delete=False) as f:
            f.write(b"\xff\xfe")
            path = f.name
        self.addCleanup(os.unlink, path)
        entries, _, error = R.load_allowlist(path)
        self.assertIsNone(entries)
        self.assertIn("UTF-8", error)


class EndpointParsing(unittest.TestCase):
    def test_remote_name_path(self):
        ep = R.parse_endpoint("backup:/srv/data")
        self.assertTrue(ep.remote)
        self.assertEqual((ep.name, ep.path), ("backup", "/srv/data"))

    def test_remote_path_must_be_absolute(self):
        # ~ and relative paths mean the remote account's home over rsh, which
        # the allowlist (host only) never meant to hand out.
        for spec in ("nas:data/x", "nas:~/.ssh/authorized_keys", "nas:.ssh/authorized_keys"):
            with self.assertRaises(R.InputError, msg=spec):
                R.parse_endpoint(spec)

    def test_absolute_local_path(self):
        ep = R.parse_endpoint("/tmp/a:b")
        self.assertFalse(ep.remote)
        self.assertEqual(ep.spec, "/tmp/a:b")

    def test_local_paths_are_limited_to_data_directories(self):
        for path in ("/deskbox/in/a", "/deskbox/out/a", "/tmp/a"):
            self.assertFalse(R.parse_endpoint(path).remote, path)

    def test_tool_directory_and_other_roots_are_refused(self):
        for path in (
            "/deskbox/tool/id_ed25519",
            "/deskbox/tool/allowlist",
            "/deskbox/in/../tool/id_ed25519",
            "/etc/passwd",
            "/usr/bin/rsync",
        ):
            with self.assertRaises(R.InputError, msg=path):
                R.parse_endpoint(path)

    def test_relative_local_path_is_rejected(self):
        with self.assertRaises(R.InputError):
            R.parse_endpoint("relative/path")
        # "./a:b" is the rsync spelling for a local path containing a colon;
        # this tool still requires an absolute local path.
        with self.assertRaises(R.InputError):
            R.parse_endpoint("./a:b")

    def test_empty_remote_path_is_rejected(self):
        with self.assertRaises(R.InputError):
            R.parse_endpoint("backup:")

    def test_newline_is_rejected(self):
        with self.assertRaises(R.InputError):
            R.parse_endpoint("/tmp/a\nb")

    def test_user_at_host_is_not_an_endpoint_name(self):
        # The user belongs in the allowlist, not in the model-written path.
        with self.assertRaises(R.InputError):
            R.parse_endpoint("ryan@backup:/srv/a")


class RefusalAndResolution(Base):
    def test_unknown_host_refused_before_rsync(self):
        report, runner = self.call(destination="other:/srv/a")
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "host_not_allowed")
        self.assertEqual(report["command"], [])
        self.assertEqual(runner.calls, [])

    def test_literal_host_is_not_a_name(self):
        # Only names in the file are accepted; the resolved host is not a key.
        report, runner = self.call(destination="backup.example.com:/srv/a")
        self.assertEqual(report["reason"], "host_not_allowed")
        self.assertEqual(runner.calls, [])

    def test_empty_allowlist_refuses_every_remote(self):
        self.write_allow("")
        report, runner = self.call()
        self.assertEqual(report["reason"], "host_not_allowed")
        self.assertIn("none", report["detail"])
        self.assertEqual(runner.calls, [])

    def test_malformed_allowlist_fails_closed(self):
        self.write_allow(ALLOW + "this line has no equals sign\n")
        report, runner = self.call()
        self.assertEqual(report["reason"], "allowlist_invalid")
        self.assertEqual(report["command"], [])
        self.assertEqual(runner.calls, [])

    def test_missing_allowlist_refuses(self):
        self.allow = os.path.join(self.tmp.name, "missing")
        report, runner = self.call()
        self.assertEqual(report["reason"], "host_not_allowed")
        self.assertIn("missing", report["detail"])
        self.assertEqual(runner.calls, [])

    def test_allowed_name_resolves_user_and_host(self):
        report, _ = self.call()
        self.assertTrue(report["ok"])
        self.assertEqual(report["command"][-2], "/tmp/a")
        self.assertEqual(report["command"][-1], "ryan@backup.example.com:/srv/a")

    def test_remote_source_and_local_destination(self):
        report, _ = self.call(source="nas:/srv/data", destination="/deskbox/out")
        self.assertTrue(report["ok"])
        self.assertEqual(report["command"][-2], "10.0.0.10:/srv/data")
        self.assertEqual(report["command"][-1], "/deskbox/out")

    def test_port_comes_only_from_the_file(self):
        report, _ = self.call(destination="nas:/srv/a")
        ssh = report["command"][report["command"].index("-e") + 1]
        self.assertIn("-p 22", ssh)
        # The model cannot put a port in the path and have it interpreted.
        report2, runner2 = self.call(destination="nas:2222:/srv/a")
        self.assertFalse(report2["ok"])
        self.assertEqual(runner2.calls, [])

    def test_both_local_refused(self):
        report, runner = self.call(source="/tmp/a", destination="/tmp/b")
        self.assertEqual(report["reason"], "no_remote_endpoint")
        self.assertEqual(runner.calls, [])

    def test_local_key_path_is_refused_before_rsync(self):
        report, runner = self.call(source="/deskbox/tool/id_ed25519")
        self.assertEqual(report["reason"], "invalid_input")
        self.assertIn("local paths must be under", report["detail"])
        self.assertEqual(report["command"], [])
        self.assertEqual(runner.calls, [])

    def test_both_remote_refused(self):
        report, runner = self.call(source="backup:/a", destination="nas:/b")
        self.assertEqual(report["reason"], "both_remote_unsupported")
        self.assertEqual(runner.calls, [])


class DryRunAndApply(Base):
    def test_dry_run_is_the_default(self):
        report, runner = self.call()
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["applied"])
        self.assertIn("--dry-run", report["command"])
        self.assertEqual(len(runner.calls), 1)

    def test_apply_removes_dry_run(self):
        report, _ = self.call(apply=True)
        self.assertFalse(report["dry_run"])
        self.assertTrue(report["applied"])
        self.assertNotIn("--dry-run", report["command"])

    def test_refused_apply_does_not_run(self):
        report, runner = self.call(apply=True, destination="other:/srv/a")
        self.assertFalse(report["dry_run"])
        self.assertFalse(report["applied"])
        self.assertEqual(report["command"], [])
        self.assertEqual(runner.calls, [])

    def test_apply_must_be_a_json_boolean(self):
        report, runner = self.call(apply="true")
        self.assertEqual(report["reason"], "invalid_input")
        self.assertTrue(report["dry_run"])
        self.assertEqual(runner.calls, [])

    def test_missing_apply_means_dry_run(self):
        runner = FakeRunner()
        report = R.handle(
            {"source": "/tmp/a", "destination": "backup:/srv/a"},
            allowlist_path=self.allow,
            runner=runner,
        )
        self.assertTrue(report["dry_run"])
        self.assertIn("--dry-run", report["command"])


class ArgumentBuilding(unittest.TestCase):
    def test_argv_order_and_safety_flags(self):
        argv = R.build_argv("/tmp/a", "host:/srv/a", False, None)
        self.assertEqual(argv[0], "rsync")
        self.assertEqual(argv[1], "--dry-run")
        self.assertIn("--protect-args", argv)
        self.assertIn("--recursive", argv)
        self.assertIn("--itemize-changes", argv)
        self.assertIn("--stats", argv)
        self.assertEqual(argv[-2:], ["/tmp/a", "host:/srv/a"])

    def test_ssh_command_is_batch_mode_and_pinned(self):
        argv = R.build_argv("/tmp/a", "host:/srv/a", True, None)
        ssh = argv[argv.index("-e") + 1]
        for expected in (
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            "UserKnownHostsFile=/deskbox/tool/known_hosts",
            "IdentitiesOnly=yes",
            "-i /deskbox/tool/id_ed25519",
        ):
            self.assertIn(expected, ssh)

    def test_port_adds_p_to_ssh(self):
        argv = R.build_argv("/tmp/a", "host:/srv/a", False, 2222)
        ssh = argv[argv.index("-e") + 1]
        self.assertIn("-p 2222", ssh)


class RsyncResult(Base):
    def test_rsync_is_run_with_a_timeout_and_expiry_is_reported(self):
        # Without timeout= the TimeoutExpired branch could never be reached.
        report, runner = self.call()
        self.assertEqual(runner.calls[0][1].get("timeout"), R.RSYNC_TIMEOUT_S)
        report, _ = self.call(runner=FakeRunner(exc=subprocess.TimeoutExpired("rsync", 1)))
        self.assertEqual(report["reason"], "rsync_timeout")

    def test_a_home_relative_remote_path_never_reaches_rsync(self):
        report, runner = self.call(destination="nas:~/.ssh/authorized_keys", apply=True)
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "invalid_input")
        self.assertEqual(runner.calls, [])

    def test_missing_rsync_is_reported_with_the_command(self):
        report, _ = self.call(runner=FakeRunner(exc=FileNotFoundError("rsync")))
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "rsync_not_installed")
        self.assertTrue(report["command"])

    def test_nonzero_rsync_is_reported(self):
        report, _ = self.call(runner=FakeRunner(returncode=23, stderr=b"some files vanished\n"))
        self.assertFalse(report["ok"])
        self.assertFalse(report["applied"])
        self.assertEqual(report["exit_code"], 23)
        self.assertEqual(report["reason"], "rsync_failed")
        self.assertIn("vanished", report["stderr"])

    def test_signalled_rsync_uses_128_plus_signal(self):
        report, _ = self.call(runner=FakeRunner(returncode=-9))
        self.assertEqual(report["exit_code"], 137)
        self.assertEqual(report["reason"], "rsync_killed")

    def test_stdout_is_truncated_to_the_contract_limit(self):
        report, _ = self.call(runner=FakeRunner(stdout=b"x" * (R.OUTPUT_LIMIT + 1000)))
        self.assertLessEqual(len(report["stdout"]), R.OUTPUT_LIMIT)
        self.assertTrue(report["stdout"].endswith("[truncated]"))


class JsonBoundary(Base):
    def test_main_emits_one_json_document(self):
        out = io.StringIO()
        rc = R.main(
            stdin=io.StringIO(json.dumps({"source": "/tmp/a", "destination": "backup:/srv/a"})),
            stdout=out,
            allowlist_path=self.allow,
            runner=FakeRunner(),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertTrue(json.loads(out.getvalue())["ok"])

    def test_main_reports_bad_stdin_as_json(self):
        out = io.StringIO()
        rc = R.main(stdin=io.StringIO("not json"), stdout=out, runner=FakeRunner())
        self.assertEqual(rc, 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["reason"], "invalid_input")
        self.assertFalse(doc["ok"])

    def test_main_accepts_a_utf8_bom(self):
        out = io.StringIO()
        body = "\ufeff" + json.dumps({"source": "/tmp/a", "destination": "backup:/srv/a"})
        rc = R.main(stdin=io.StringIO(body), stdout=out, allowlist_path=self.allow, runner=FakeRunner())
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])

    def test_non_object_input_is_refused(self):
        report = R.handle([], allowlist_path=self.allow, runner=FakeRunner())
        self.assertEqual(report["reason"], "invalid_input")

    def test_overlong_path_is_refused_without_breaking_the_schema(self):
        report, runner = self.call(source="/" + "a" * (R.MAX_PATH + 100))
        self.assertEqual(report["reason"], "invalid_input")
        self.assertLessEqual(len(report["source"]), R.MAX_PATH)
        self.assertEqual(runner.calls, [])


@unittest.skipIf(os.name == "nt", "uses a POSIX shell stub for rsync; run under WSL/Linux")
class PosixSubprocess(Base):
    """Exercise the real subprocess path with a fake rsync on PATH (no network)."""

    def _fake_rsync(self):
        bindir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, bindir, True)
        args_file = os.path.join(bindir, "args")
        path = os.path.join(bindir, "rsync")
        with open(path, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$@" > "$RSYNC_ARGS"\necho itemized\n')
        os.chmod(path, 0o755)
        return bindir, args_file

    def test_real_subprocess_gets_the_built_argv(self):
        bindir, args_file = self._fake_rsync()
        env = {
            "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
            "RSYNC_ARGS": args_file,
        }
        with mock.patch.dict(os.environ, env, clear=False):
            report = R.handle(
                {"source": "/tmp/a", "destination": "backup:/srv/a"},
                allowlist_path=self.allow,
                runner=subprocess.run,
            )
        self.assertTrue(report["ok"])
        self.assertEqual(report["stdout"], "itemized\n")
        with open(args_file, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertIn("--dry-run", lines)
        self.assertEqual(lines[-2:], ["/tmp/a", "ryan@backup.example.com:/srv/a"])

    def test_run_wrapper_emits_json(self):
        bindir, _ = self._fake_rsync()
        env = os.environ.copy()
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["SBX_RSYNC_ALLOWLIST"] = self.allow
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "sbx", "tools", "rsync", "run")],
            input=json.dumps({"source": "/tmp/a", "destination": "backup:/srv/a"}),
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertTrue(doc["ok"])
        self.assertIn("--dry-run", doc["command"])


if __name__ == "__main__":
    unittest.main()
