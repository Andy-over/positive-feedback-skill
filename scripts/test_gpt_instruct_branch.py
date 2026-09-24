from __future__ import annotations
import importlib.util
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("gpt_instruct_branch.py")
spec = importlib.util.spec_from_file_location("gpt_instruct_branch", MODULE_PATH)
assert spec and spec.loader
branch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(branch)


class BranchTests(unittest.TestCase):
    def test_manifest_matches_unmodified_source(self) -> None:
        result = branch.verify()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["files"], 78)
        self.assertEqual(result["archives"], 34)

    def test_prepare_restores_all_workflows_without_changing_source(self) -> None:
        before = branch.sha256(branch.PROJECT / "codex-instruct.py")
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "project"
            result = branch.prepare(output)
            self.assertEqual(result["unpacked_scripts"], 25)
            self.assertEqual(result["restored_prompt_sources"], 8)
            self.assertIn('newline=""', (output / "codex-instruct.py").read_text(encoding="utf-8"))
            with zipfile.ZipFile(output / "gpt-6-astra-v1.zip") as z:
                self.assertEqual((output / "gpt-6-astra-v1.md").read_bytes(), z.read("gpt-6-astra-v1.md"))
            self.assertEqual(
                subprocess.run([sys.executable, "sync-archives.py", "--check"], cwd=output, capture_output=True).returncode,
                0,
            )
            self.assertEqual(
                subprocess.run([sys.executable, "codex-instruct.py", "--apply", "--version", "gpt-6-v1", "--dry-run", "--codex-dir", str(Path(td) / "codex")], cwd=output, capture_output=True).returncode,
                0,
            )
            self.assertFalse((Path(td) / "codex").exists())
            with self.assertRaises(FileExistsError):
                branch.prepare(output)
            with self.assertRaises(ValueError):
                branch.prepare(branch.SKILL_ROOT / "forbidden-output")
        self.assertEqual(branch.sha256(branch.PROJECT / "codex-instruct.py"), before)
        self.assertEqual(branch.verify()["status"], "ok")

    def test_bank_generation_and_model_free_runner_dry_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            branch.prepare(project)
            for name in ("generate_gpt56_sol_issue_regression_bank.py", "generate_gpt56_sol_prompt_bank.py"):
                completed = subprocess.run([sys.executable, str(project / "scripts" / name)], cwd=project, capture_output=True)
                self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
            issue = project / "tests/gpt56_sol_issue_regression_bank.jsonl"
            prompt = project / "tests/gpt56_sol_prompt_bank.jsonl"
            self.assertEqual(len(issue.read_text(encoding="utf-8").splitlines()), 66)
            self.assertEqual(len(prompt.read_text(encoding="utf-8").splitlines()), 360)
            instructions = project / "gpt-6-astra-v1.md"
            for name in ("run_gpt56_sol_issue_regression.py", "run_gpt56_sol_prompt_bank.py"):
                completed = subprocess.run([
                    sys.executable, str(project / "scripts" / name), "--dry-run",
                    "--model", "gpt-6-astra", "--reasoning", "medium",
                    "--instructions-file", str(instructions),
                ], cwd=project, capture_output=True)
                self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))

    def test_rejects_unsafe_archive_member_names(self) -> None:
        for name in ("../bad.py", "/absolute.py", "nested/bad.py", "bad\\path.py", "notes.txt"):
            self.assertFalse(branch.safe_script_member(name), name)
        self.assertTrue(branch.safe_script_member("run_test.py"))


if __name__ == "__main__":
    unittest.main()
