import json
from pathlib import Path
import tempfile
import unittest

from chat_record import chat_directory, set_record_name, validate_name


class ChatRecordTests(unittest.TestCase):
    def test_default_names_isolate_chats_without_creating_files_on_read(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = chat_directory("model-a", "thread-one", root)
            second = chat_directory("model-a", "thread-two", root)
            self.assertNotEqual(first, second)
            self.assertEqual(first.name, "chat-thread-one")
            self.assertFalse((root / ".positive-feedback").exists())

    def test_custom_name_moves_existing_record_without_losing_data(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            old = chat_directory("model-a", "thread-one", root)
            (old / "events").mkdir(parents=True)
            (old / "action-controller.json").write_text('{"tasks":[1]}', encoding="utf-8")
            (old / "events" / "event.json").write_text('{"score":1}', encoding="utf-8")
            result = set_record_name("model-a", "thread-one", "需求讨论", root)
            new = chat_directory("model-a", "thread-one", root)
            self.assertEqual(result["status"], "named")
            self.assertEqual(new.name, "chat-thread-one--需求讨论")
            self.assertFalse(old.exists())
            self.assertEqual((new / "action-controller.json").read_text(encoding="utf-8"), '{"tasks":[1]}')
            self.assertEqual((new / "events" / "event.json").read_text(encoding="utf-8"), '{"score":1}')
            self.assertEqual(set_record_name("model-a", "thread-one", "需求讨论", root)["status"], "unchanged")
            self.assertEqual(chat_directory("model-a", "thread-two", root).name, "chat-thread-two")
            registry = json.loads((root / ".positive-feedback" / "model-a" / "record-names.json")
                                  .read_text(encoding="utf-8"))
            self.assertEqual(registry["names"], {"thread-one": "需求讨论"})
            self.assertTrue((root / ".positive-feedback" / ".gitignore").is_file())

    def test_missing_id_and_unsafe_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaises(ValueError):
                chat_directory("model-a", "", root)
            for name in ("", "../escape", "a/b", "name.", "CON", "a:" , "bad\\name", " leading"):
                with self.assertRaises(ValueError, msg=name):
                    validate_name(name)
            self.assertFalse((root / ".positive-feedback").exists())

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root / "outside"
            outside.mkdir()
            try:
                (root / ".positive-feedback").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(ValueError):
                chat_directory("model-a", "thread-one", root)


if __name__ == "__main__":
    unittest.main()
