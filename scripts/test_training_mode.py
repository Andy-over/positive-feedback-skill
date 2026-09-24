import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from policy import session_command
from training_mode import mode_status, record_turn, set_mode, validate_entry


ENTRY = {
    "turn_id": "turn-1", "task_kind": "code", "task_summary": "增加受控的数据记录",
    "response_summary": "交付经过测试的 Skill 修改", "actions": ["edit_content", "validate_requirements"],
    "outcome": "verified", "verification": "单元测试通过",
    "feedback": {"kind": "request", "summary": "用户要求显式启用"},
}


class TrainingModeTests(unittest.TestCase):
    def test_opt_in_and_one_record_per_turn(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session = root / "session.json"
            data = root / "training-data"
            self.assertFalse(mode_status("thread-test", session, data)["recording"])
            self.assertFalse(data.exists())
            with self.assertRaises(ValueError):
                set_mode("enable", "thread-test", "启用训练模式", session, data)
            self.assertFalse(data.exists())
            session_command("enable", "thread-test", session, "使用 skill")
            self.assertTrue(set_mode("enable", "thread-test", "启用训练模式", session, data)["recording"])
            self.assertEqual(record_turn("thread-test", ENTRY, session, data)["status"], "recorded")
            self.assertEqual(record_turn("thread-test", ENTRY, session, data)["status"], "duplicate")
            rows = (data / "thread-test" / "turns.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0])["task_summary"], ENTRY["task_summary"])
            self.assertFalse(set_mode("disable", "thread-test", "停用训练模式", session, data)["recording"])
            with self.assertRaises(ValueError):
                record_turn("thread-test", {**ENTRY, "turn_id": "turn-2"}, session, data)

    def test_skill_disable_and_reenable_do_not_restore_old_training_consent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session = root / "session.json"
            data = root / "data"
            session_command("enable", "thread-test", session, "使用 skill")
            set_mode("enable", "thread-test", "启用训练模式", session, data)
            session_command("disable", "thread-test", session, "停用 skill")
            self.assertFalse(mode_status("thread-test", session, data)["recording"])
            session_command("enable", "thread-test", session, "再次使用 skill")
            self.assertFalse(mode_status("thread-test", session, data)["recording"])
            self.assertTrue(set_mode("enable", "thread-test", "再次启用训练模式", session, data)["recording"])

    def test_rejects_extra_fields_and_obvious_secrets(self):
        with self.assertRaises(ValueError):
            validate_entry({**ENTRY, "hidden_reasoning": "private"})
        with self.assertRaises(ValueError):
            validate_entry({**ENTRY, "task_summary": "contact a.person@example.com"})
        with self.assertRaises(ValueError):
            validate_entry({**ENTRY, "task_summary": "-----BEGIN PRIVATE KEY-----"})

    def test_cli_status_and_record(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session = root / "session.json"
            data = root / "data"
            entry = root / "entry.json"
            entry.write_text(json.dumps(ENTRY, ensure_ascii=False), encoding="utf-8")
            script = Path(__file__).with_name("training_mode.py")
            def run(*args):
                return subprocess.run([sys.executable, "-X", "utf8", str(script), "--thread-id",
                                       "thread-test", "--session-state", str(session), "--data-dir",
                                       str(data), *args], capture_output=True, text=True)
            self.assertFalse(json.loads(run("mode", "status").stdout)["recording"])
            session_command("enable", "thread-test", session, "使用 skill")
            self.assertTrue(json.loads(run("mode", "enable", "--source-excerpt", "启用训练模式").stdout)["recording"])
            result = run("record", "--entry-json", str(entry))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "recorded")

    def test_policy_verify_automatically_captures_only_with_double_opt_in(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session = root / "session.json"
            data = root / "training-data"
            state = root / "controller.json"
            artifact = root / "result.txt"
            checks = root / "checks.json"
            artifact.write_text("verified output", encoding="utf-8")
            checks.write_text('{"checked": true}', encoding="utf-8")
            policy = Path(__file__).with_name("policy.py")
            def run(*args):
                completed = subprocess.run(
                    [sys.executable, "-X", "utf8", str(policy), "--thread-id", "thread-test",
                     "--session-state", str(session), "--training-data-dir", str(data),
                     "--state", str(state), *args], capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(completed.stdout)
            run("prepare", "--task-id", "task-1", "--task-kind", "code",
                "--required-actions", "edit_content", "--target-spec", "修复代码错误",
                "--target-source-id", "source-1")
            run("complete", "--task-id", "task-1", "--output-id", "output-1",
                "--actions", "edit_content", "--artifact", str(artifact))
            result = run("verify", "--task-id", "task-1", "--result", "pass",
                         "--checks-json", str(checks))
            self.assertNotIn("training_capture", result)
            self.assertFalse(data.exists())
            session_command("enable", "thread-test", session, "使用 skill")
            set_mode("enable", "thread-test", "启用训练模式", session, data)
            run("prepare", "--task-id", "task-2", "--task-kind", "code",
                "--required-actions", "edit_content", "--target-spec", "修复函数错误",
                "--target-source-id", "source-2")
            run("complete", "--task-id", "task-2", "--output-id", "output-2",
                "--actions", "edit_content", "--artifact", str(artifact))
            result = run("verify", "--task-id", "task-2", "--result", "pass",
                         "--checks-json", str(checks))
            self.assertEqual(result["training_capture"]["entries"], ["recorded"])
            journal = data / "thread-test" / "turns.jsonl"
            row = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["origin"], "policy.verify")
            self.assertEqual(row["outcome"], "verified")
            self.assertNotIn("修复函数错误", row["task_summary"])
            self.assertIn("intent_tags=code,correction", row["task_summary"])
            self.assertEqual(len(row["task_features"]), 16)
            result = run("feedback", "--event-id", "event-2", "--kind", "positive",
                         "--task-id", "task-2", "--source-id", "feedback-2",
                         "--label", "很好", "--quote", "很好")
            self.assertEqual(result["training_capture"]["entries"], ["recorded"])
            rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["origin"], "policy.feedback")
            self.assertEqual(rows[1]["feedback"]["kind"], "positive")
            plan = root / "plan.json"
            plan.write_text(json.dumps({"batch_id": "batch-3", "steps": [
                {"op": "prepare", "task_id": "task-3", "task_kind": "code",
                 "required_actions": "edit_content", "target_spec": "核查代码测试",
                 "target_source_id": "source-3"},
                {"op": "complete", "task_id": "task-3", "output_id": "output-3",
                 "actions": "edit_content", "artifacts": [str(artifact)]},
                {"op": "verify", "task_id": "task-3", "passed": True,
                 "checks_json": str(checks)}]}, ensure_ascii=False), encoding="utf-8")
            result = run("batch", "--plan-json", str(plan))
            self.assertEqual(result["training_capture"]["entries"], ["recorded"])
            self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 3)
            session_command("disable", "thread-test", session, "停用 skill")
            self.assertFalse(mode_status("thread-test", session, data)["recording"])


if __name__ == "__main__":
    unittest.main()
