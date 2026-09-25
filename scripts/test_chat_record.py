import json
from pathlib import Path
import tempfile
import unittest

from chat_record import chat_directory, link_record, set_record_name, unlink_record, validate_name


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

    def test_explicit_link_shares_existing_record_and_unlink_restores_own(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner = chat_directory("model-a", "thread-one", root)
            guest = chat_directory("model-a", "thread-two", root)
            owner.mkdir(parents=True)
            guest.mkdir()
            (owner / "action-controller.json").write_text("owner", encoding="utf-8")
            (guest / "action-controller.json").write_text("guest", encoding="utf-8")
            self.assertEqual(link_record("model-a", "thread-two", to_thread_id="thread-one",
                                         skill_root=root)["status"], "linked")
            self.assertEqual(chat_directory("model-a", "thread-two", root), owner)
            self.assertEqual(link_record("model-a", "thread-two", to_thread_id="thread-one",
                                         skill_root=root)["status"], "unchanged")
            self.assertEqual(unlink_record("model-a", "thread-two", root)["status"], "unlinked")
            self.assertEqual(chat_directory("model-a", "thread-two", root), guest)
            self.assertEqual((guest / "action-controller.json").read_text(encoding="utf-8"), "guest")
            self.assertEqual((owner / "action-controller.json").read_text(encoding="utf-8"), "owner")

    def test_link_by_name_follows_renames_and_rejects_unknown_targets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner = chat_directory("model-a", "thread-one", root)
            owner.mkdir(parents=True)
            set_record_name("model-a", "thread-one", "项目讨论", root)
            self.assertEqual(link_record("model-a", "thread-two", to_name="项目讨论",
                                         skill_root=root)["owner_thread_id"], "thread-one")
            with self.assertRaises(ValueError):
                set_record_name("model-a", "thread-two", "错误改名", root)
            set_record_name("model-a", "thread-one", "项目新名", root)
            self.assertEqual(chat_directory("model-a", "thread-two", root).name,
                             "chat-thread-one--项目新名")
            with self.assertRaises(ValueError):
                link_record("model-a", "thread-three", to_name="不存在", skill_root=root)
            with self.assertRaises(ValueError):
                link_record("model-a", "thread-three", to_thread_id="missing", skill_root=root)
            with self.assertRaises(ValueError):
                link_record("model-a", "thread-one", to_thread_id="thread-one", skill_root=root)
            self.assertFalse(chat_directory("model-b", "thread-two", root).exists())

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
