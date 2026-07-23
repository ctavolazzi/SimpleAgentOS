"""
test_fill_sections.py — Tests for fill_sections.py.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_fill_sections.py -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import fill_sections


class TestImportClean(unittest.TestCase):
    """Module must be importable without side effects."""

    def test_import_no_side_effects(self):
        # Already imported — just verify key attributes exist
        self.assertTrue(hasattr(fill_sections, "TARGETS"))
        self.assertTrue(hasattr(fill_sections, "fill"))
        self.assertTrue(callable(fill_sections.fill))


class TestFillIdempotency(unittest.TestCase):

    def test_filled_section_not_overwritten(self):
        """fill() skips sections with status 'filled' unless force=True."""
        with patch("fill_sections.daily_note.section_status") as mock_status, \
             patch("fill_sections.daily_note.write_section") as mock_write:
            mock_status.return_value = {
                "commits_today": "filled",
                "work_efforts": "filled",
                "tomorrows_top_3": "filled",
                "sitrep": "filled",
            }
            result = fill_sections.fill()
            mock_write.assert_not_called()
            self.assertEqual(len(result["skipped"]), 4)
            self.assertEqual(len(result["filled"]), 0)

    def test_force_overwrites_filled(self):
        """With force=True, filled sections are overwritten."""
        with patch("fill_sections.daily_note.section_status") as mock_status, \
             patch("fill_sections.daily_note.write_section") as mock_write:
            mock_status.return_value = {"commits_today": "filled"}
            with patch.dict(fill_sections._BUILDERS, {
                "commits_today": lambda: "test content"
            }):
                result = fill_sections.fill(sections=["commits_today"], force=True)
            mock_write.assert_called_once()

    def test_empty_section_gets_filled(self):
        """'empty' status triggers fill."""
        with patch("fill_sections.daily_note.section_status") as mock_status, \
             patch("fill_sections.daily_note.write_section") as mock_write:
            mock_status.return_value = {"commits_today": "empty"}
            with patch.dict(fill_sections._BUILDERS, {
                "commits_today": lambda: "test commits"
            }):
                result = fill_sections.fill(sections=["commits_today"])
            mock_write.assert_called_once()

    def test_template_section_gets_filled(self):
        """'template' status is treated same as 'empty'."""
        with patch("fill_sections.daily_note.section_status") as mock_status, \
             patch("fill_sections.daily_note.write_section") as mock_write:
            mock_status.return_value = {"commits_today": "template"}
            with patch.dict(fill_sections._BUILDERS, {
                "commits_today": lambda: "commits content"
            }):
                result = fill_sections.fill(sections=["commits_today"])
            mock_write.assert_called_once()

    def test_absent_section_skipped(self):
        """'absent' sections are skipped without error."""
        with patch("fill_sections.daily_note.section_status") as mock_status, \
             patch("fill_sections.daily_note.write_section") as mock_write:
            mock_status.return_value = {"commits_today": "absent"}
            result = fill_sections.fill(sections=["commits_today"])
            mock_write.assert_not_called()
            self.assertTrue(any(s["section"] == "commits_today"
                                for s in result["skipped"]))


class TestInTheLabOptIn(unittest.TestCase):

    def test_in_the_lab_not_in_default_targets(self):
        self.assertNotIn("in_the_lab", fill_sections.TARGETS)

    def test_in_the_lab_excluded_without_flag(self):
        with patch("fill_sections.daily_note.section_status") as mock_status, \
             patch("fill_sections.daily_note.write_section") as mock_write:
            mock_status.return_value = {s: "empty" for s in fill_sections.TARGETS}
            with patch.dict(fill_sections._BUILDERS,
                            {s: lambda: "x" for s in fill_sections.TARGETS}):
                result = fill_sections.fill(include_lab=False)
            written = [c[0][0] for c in mock_write.call_args_list]
            self.assertNotIn("in_the_lab", written)

    def test_in_the_lab_included_with_flag(self):
        with patch("fill_sections.daily_note.section_status") as mock_status, \
             patch("fill_sections.daily_note.write_section") as mock_write:
            statuses = {s: "empty" for s in fill_sections.TARGETS}
            statuses["in_the_lab"] = "empty"
            mock_status.return_value = statuses
            all_builders = {s: (lambda: "x") for s in list(fill_sections.TARGETS) + ["in_the_lab"]}
            with patch.dict(fill_sections._BUILDERS, all_builders):
                result = fill_sections.fill(include_lab=True)
            written = [c[0][0] for c in mock_write.call_args_list]
            self.assertIn("in_the_lab", written)


class TestTomorrowsTop3Fallback(unittest.TestCase):

    def test_falls_back_to_blank_checkboxes_when_yesterday_empty(self):
        with patch("fill_sections.daily_note.read_yesterday", return_value=""):
            content = fill_sections._build_tomorrows_top_3()
        self.assertIn("- [ ]", content)
        self.assertEqual(content.count("- [ ]"), 3)

    def test_carries_forward_unchecked_items(self):
        yesterday = "- [ ] Do thing A\n- [x] Done thing\n- [ ] Do thing B\n"
        with patch("fill_sections.daily_note.read_yesterday", return_value=yesterday):
            content = fill_sections._build_tomorrows_top_3()
        self.assertIn("Do thing A", content)
        self.assertIn("Do thing B", content)
        self.assertNotIn("Done thing", content)

    def test_dry_run_does_not_write(self):
        with patch("fill_sections.daily_note.section_status") as mock_status, \
             patch("fill_sections.daily_note.write_section") as mock_write:
            mock_status.return_value = {"commits_today": "empty"}
            with patch.dict(fill_sections._BUILDERS, {"commits_today": lambda: "x"}):
                fill_sections.fill(sections=["commits_today"], dry_run=True)
            mock_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
