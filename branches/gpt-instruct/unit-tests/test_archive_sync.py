from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sync_archives",
    PROJECT_ROOT / "sync-archives.py",
)
assert SPEC and SPEC.loader
sync_archives = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_archives)


class ArchiveSyncTests(unittest.TestCase):
    # The retired sample directory must never re-enter archive synchronization.
    def test_archive_specs_never_target_examples(self) -> None:
        for source, destination, _archive_name in sync_archives.archive_specs():
            self.assertNotIn("examples", source.relative_to(PROJECT_ROOT).parts)
            self.assertNotIn("examples", destination.relative_to(PROJECT_ROOT).parts)

    def test_matching_archive_is_detected_without_metadata_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            archive = root / "source.zip"
            source.write_text("stable release bytes\n", encoding="utf-8")

            sync_archives.write_single_file_archive(source, archive, source.name)
            before = archive.read_bytes()

            self.assertTrue(
                sync_archives.archive_matches_source(source, archive, source.name)
            )
            # The normal sync path skips write_single_file_archive when this
            # predicate is true, preserving an existing release ZIP byte-for-byte.
            self.assertEqual(archive.read_bytes(), before)

    def test_changed_source_marks_archive_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.py"
            archive = root / "source.zip"
            source.write_text("print('one')\n", encoding="utf-8")
            sync_archives.write_single_file_archive(source, archive, source.name)

            source.write_text("print('two')\n", encoding="utf-8")

            self.assertFalse(
                sync_archives.archive_matches_source(source, archive, source.name)
            )


if __name__ == "__main__":
    unittest.main()
