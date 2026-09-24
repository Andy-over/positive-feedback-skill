import tempfile
import unittest
from pathlib import Path

from feedback import (
    EVALUATION_LABELS, POSITIVE_LABELS, REQUEST_LABELS, add, default_directory,
    number, report, signed_number,
)


class LedgerTests(unittest.TestCase):
    def test_documented_label_scores_match_executable_mappings(self):
        design = (Path(__file__).resolve().parents[1] / "references" / "design.md")
        documented = {kind: {} for kind in ("positive", "evaluation", "request")}
        for line in design.read_text(encoding="utf-8").splitlines():
            cells = [part.strip() for part in line.split("|")]
            if len(cells) < 5 or cells[1] not in documented:
                continue
            for label in cells[2].split("、"):
                self.assertNotIn(label, documented[cells[1]])
                documented[cells[1]][label] = float(cells[3])
        self.assertEqual(documented["positive"], POSITIVE_LABELS)
        self.assertEqual(documented["evaluation"], EVALUATION_LABELS)
        self.assertEqual(documented["request"], REQUEST_LABELS)

    def test_signed_mappings(self):
        self.assertEqual(POSITIVE_LABELS["很好"], 1)
        self.assertEqual(POSITIVE_LABELS["谢谢"], .6)
        self.assertEqual(EVALUATION_LABELS["垃圾"], -1)
        self.assertEqual(EVALUATION_LABELS["不太满意"], -.2)
        self.assertEqual(REQUEST_LABELS["希望你"], -.15)
        self.assertEqual(REQUEST_LABELS["命令你"], -1)

    def test_kinds_are_separate_and_replacements_are_kind_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            add(directory, "response-1", "source-positive", "positive", label="不错")
            add(directory, "response-1", "source-request", "request", label="需要你")
            add(directory, "response-2", "source-evaluation", "evaluation", label="不满意")
            result = report(directory)
            self.assertEqual(result["event_count"], 3)
            self.assertEqual(result["by_kind"]["positive"]["mean"], .8)
            self.assertEqual(result["by_kind"]["request"]["mean"], -.25)
            self.assertEqual(result["by_kind"]["evaluation"]["mean"], -.4)
            add(directory, "response-1", "source-positive-fix", "positive", label="很好", replace=True)
            result = report(directory)
            self.assertEqual(result["event_count"], 4)
            self.assertEqual(result["effective_events"], 3)
            self.assertEqual(result["by_kind"]["positive"]["mean"], 1)
            self.assertEqual(result["by_kind"]["request"]["mean"], -.25)

    def test_idempotency_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(add(directory, "r", "s", "positive", score=0)["status"], "recorded")
            self.assertEqual(add(directory, "r", "s", "positive", score=0)["status"], "duplicate")
            with self.assertRaises(ValueError):
                add(directory, "r", "s", "positive", score=.1)
            with self.assertRaises(ValueError):
                add(directory, "x", "x1", "request", score=0)
            with self.assertRaises(ValueError):
                add(directory, "x", "x2", "positive", score=-.1)
            with self.assertRaises(ValueError):
                add(directory, "x", "x3", "evaluation", label="很好")

    def test_label_never_masquerades_as_source_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            result = add(directory, "r", "s", "positive", label="很好")
            self.assertEqual(result["event"]["quote"], "")
            self.assertEqual(result["event"]["label"], "很好")

    def test_numeric_bounds_and_empty_report(self):
        self.assertEqual(signed_number(-1), -1)
        self.assertEqual(signed_number(1), 1)
        for value in (-1.1, 1.1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                signed_number(value)
        with self.assertRaises(ValueError):
            number(-.1)
        with tempfile.TemporaryDirectory() as directory:
            result = report(directory)
            self.assertEqual(result["effective_events"], 0)
            self.assertIsNone(result["by_kind"]["positive"]["mean"])

    def test_default_directory_is_working_directory_and_profile_scoped(self):
        with tempfile.TemporaryDirectory() as raw:
            first = default_directory("model-a", raw)
            second = default_directory("model-b", raw)
            self.assertNotEqual(first, second)
            self.assertEqual(first.name, "events")
            self.assertEqual(first.parent.name, "model-a")
            for bad in ("", "../escape", "has space"):
                with self.assertRaises(ValueError):
                    default_directory(bad, raw)


if __name__ == "__main__":
    unittest.main()
