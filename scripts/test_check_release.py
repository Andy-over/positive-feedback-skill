import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from check_release import audit, package


class ReleaseTests(unittest.TestCase):
    def test_audit_and_package_exclude_ignored_training_data(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "SKILL.md").write_text("skill", encoding="utf-8")
            (root / ".gitignore").write_text("training-data/\n", encoding="utf-8")
            private = root / "training-data" / "thread" / "turns.jsonl"
            private.parent.mkdir(parents=True)
            private.write_text("secret sample", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "SKILL.md", ".gitignore"], check=True)
            result = audit(root)
            self.assertTrue(result["ok"])
            archive = root / "release-test.skill"
            package(root, archive, result["files"])
            with zipfile.ZipFile(archive) as zipped:
                self.assertNotIn("training-data/thread/turns.jsonl", zipped.namelist())
            subprocess.run(["git", "-C", str(root), "add", "-f", "training-data/thread/turns.jsonl"], check=True)
            self.assertFalse(audit(root)["ok"])

    def test_forced_profile_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            state = root / ".positive-feedback" / "model-a" / "action-controller.json"
            state.parent.mkdir(parents=True)
            state.write_text('{"private":true}', encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-f", ".positive-feedback/model-a/action-controller.json"], check=True)
            result = audit(root)
            self.assertFalse(result["ok"])
            self.assertIn("tracked private path: .positive-feedback/model-a/action-controller.json",
                          result["issues"])


if __name__ == "__main__":
    unittest.main()
