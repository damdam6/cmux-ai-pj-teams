"""Exercise the actual shell boundary: arbitrary prompt text must stay one literal argument."""
import importlib.util
import json
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "boot_transport", pathlib.Path(__file__).resolve().parents[1] / "scripts/pj-cmux.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


class BootPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pj boot ' ")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        patch = mock.patch.object(M, "TASKS_DIR", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.capture = self.root / "capture.py"
        self.capture.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")

    def command(self, prompt, role="planner", launcher="claude"):
        capture = f"{shlex.quote(sys.executable)} {shlex.quote(str(self.capture))}"
        with mock.patch.object(M, "launcher_shell_command", return_value=capture):
            return M.boot_command("sample", None if role == "board" else "task", role, launcher, prompt)

    def run_shell(self, command):
        return subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", command], text=True,
                              capture_output=True, timeout=10)

    def test_long_multiline_unicode_and_shell_syntax_round_trip_as_one_argument(self):
        sentinel = self.root / "MUST-NOT-EXIST"
        prompt = ("한글 'double\" quote>\n" * 100
                  + f"$(touch {shlex.quote(str(sentinel))}) `false` ; $HOME \\ END\n\n")
        for role in ("planner", "worker", "board"):
            with self.subTest(role=role):
                command = self.command(prompt, role)
                self.assertNotIn("\n", command)
                self.assertNotIn("quote>", command)
                result = self.run_shell(command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [prompt])
                self.assertFalse(sentinel.exists())
        for path in self.root.rglob("*.txt"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_each_launcher_family_keeps_its_configured_profile(self):
        for launcher in ("claude", "codex", "grok"):
            with self.subTest(launcher=launcher):
                command = M.boot_command("sample", "task", "planner", launcher, "first\nsecond")
                self.assertIn(M.launcher_shell_command(launcher), command)
                self.assertNotIn("first", command)
                self.assertNotIn("\n", command)

    def test_retry_reuses_file_and_changed_file_is_refused(self):
        command = self.command("original\n")
        self.assertEqual(self.command("original\n"), command)
        paths = list(self.root.rglob("planner-*.txt"))
        self.assertEqual(len(paths), 1)
        paths[0].write_text("tampered")
        with self.assertRaisesRegex(M.Fail, "내용이 바뀌었습니다"):
            self.command("original\n")

    def test_missing_file_does_not_start_launcher(self):
        command = self.command("prompt")
        next(self.root.rglob("planner-*.txt")).unlink()
        result = self.run_shell(command)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_symlink_file_is_refused(self):
        self.command("prompt")
        path = next(self.root.rglob("planner-*.txt"))
        target = self.root / "other"
        target.write_text("prompt")
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(M.Fail):
            self.command("prompt")
