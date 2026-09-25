from __future__ import annotations
import importlib.util
import os
from unittest.mock import patch
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

    def test_offline_evidence_never_promotes_unrun_model_results(self) -> None:
        result = branch.evidence()
        self.assertEqual(result["status"], "published_B_gate_not_met")
        self.assertEqual(result["published"]["B"]["cases"], [52, 66])
        self.assertEqual(result["published"]["C"]["status"], "not_run")
        self.assertEqual(result["new_model_evaluation"], "not_run")
        self.assertEqual(result["model_invocations"], 0)
        self.assertEqual(result["offline_checks"]["generated_rows"],
                         {"issue_bank": 66, "prompt_bank": 360})
        self.assertEqual(result["offline_checks"]["source_context_required"], ["route.en.06"])
        self.assertEqual(result["B_case_deficits"][0],
                         {"family": "fiction_feedback", "missing_cases": 6})

    def test_preview_deploy_and_reset_only_in_isolated_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            codex = Path(td) / "codex"
            codex.mkdir()
            config = codex / "config.toml"
            config.write_text('model = "test-model"\n', encoding="utf-8")
            baseline = branch.sha256(config)
            preview = branch.installer_action("preview", codex, "gpt-6-v1")
            self.assertEqual(preview["installer_exit_status"], 0)
            self.assertEqual(branch.sha256(config), baseline)
            with self.assertRaises(ValueError):
                branch.installer_action("deploy", codex, "gpt-6-v1")
            deployed = branch.installer_action("deploy", codex, "gpt-6-v1", True)
            self.assertEqual(deployed["installer_exit_status"], 0)
            self.assertIn('model_instructions_file', config.read_text(encoding="utf-8"))
            self.assertIn('model = "test-model"', config.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                branch.installer_action("reset", codex)
            reset = branch.installer_action("reset", codex, confirm_live_config=True)
            self.assertEqual(reset["installer_exit_status"], 0)
            self.assertNotIn('model_instructions_file', config.read_text(encoding="utf-8"))
            self.assertIn('model = "test-model"', config.read_text(encoding="utf-8"))
            self.assertFalse((branch.SKILL_ROOT / "gpt-6-astra-v1.md").exists())

    def test_cli_refuses_unconfirmed_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            codex = Path(td) / "codex"
            codex.mkdir()
            command = [sys.executable, str(MODULE_PATH), "deploy", "--version", "gpt-6-v1",
                       "--codex-dir", str(codex)]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 2)
            self.assertIn("--confirm-live-config", result.stderr)
            self.assertEqual(list(codex.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows-only native-sandbox compatibility")
    def test_opt_in_windows_evaluator_copy_runs_self_test(self) -> None:
        source_hash = branch.sha256(branch.PROJECT / "scripts/run_gpt56_sol_issue_regression.zip")
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "project"
            result = branch.prepare(output, windows_eval_compat=True)
            self.assertIn("applied", result["windows_eval_compat"])
            runner = output / "scripts/run_gpt56_sol_issue_regression.py"
            isolation = output / "scripts/codex_test_isolation.py"
            self.assertIn("acl_compatible_temporary_directory", runner.read_text(encoding="utf-8"))
            self.assertIn("CodexSandboxUsers", isolation.read_text(encoding="utf-8"))
            run = subprocess.run([sys.executable, str(runner), "--self-test"],
                                 cwd=output, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("self-test: pass", run.stdout)
        self.assertEqual(branch.sha256(branch.PROJECT / "scripts/run_gpt56_sol_issue_regression.zip"), source_hash)

    @unittest.skipUnless(os.name == "nt", "Windows-only checkout newline compatibility")
    def test_windows_evaluator_patch_accepts_crlf_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            patch_file = Path(td) / "windows-eval-crlf.patch"
            patch_file.write_bytes(branch.WINDOWS_EVAL_PATCH.read_bytes().replace(b"\n", b"\r\n"))
            with patch.object(branch, "WINDOWS_EVAL_PATCH", patch_file):
                output = Path(td) / "project"
                result = branch.prepare(output, windows_eval_compat=True)
            self.assertIn("applied", result["windows_eval_compat"])
            self.assertFalse((output / ".gpt-instruct-eval-compat.patch").exists())


if __name__ == "__main__":
    unittest.main()
