"""
test_section_separator.py — A section write must not stack separator rules.

Background: the template puts one `---` between sections, and the layout tests
say what that means: exactly one rule between sections, and a second pass adds
none. `_replace_section` kept that promise only for bodies that do not end in a
rule of their own. Location and Sitrep bodies do. Each write then left the
body's rule AND the note's rule, and because the next write treated every
trailing rule as "the trailer" and preserved it, the count grew by one per
write. On 2026-10-07 the Sitrep was written three times and ended on four
rules in a row; `session_audit.py` failed the note for it.

The separator belongs to the note. Where the note supplies one, the body's own
closing rule is dropped and the trailer is reset to a single rule.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_section_separator.py -v
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import daily_note

DOUBLED = "---\n\n---"

# Section bodies shaped like the ones spin_up really writes: a leading rule,
# content, and a closing rule.
SITREP_1 = "---\n\n**Status:** Wednesday\n\n**Blockers:** None.\n\n---"
SITREP_2 = "---\n\n**Status:** Wednesday, later\n\n**Blockers:** None.\n\n---"

NOTE = ("## Location\n\n**Chico, CA**\n\n---\n\n"
        "## Sitrep\n\n**Status:**\n\n---\n\n"
        "## Research Feed\n\n---\n\n"
        "## Idea Dump\n\nnotes\n")


def _write(note: str, body: str) -> str:
    return daily_note._replace_section(note, "## Sitrep", body)


class TestOneRuleBetweenSections(unittest.TestCase):
    def test_first_write_of_a_body_with_its_own_rule(self):
        out = _write(NOTE, SITREP_1)
        self.assertNotIn(DOUBLED, out)
        self.assertIn("**Blockers:** None.\n\n---\n\n## Research Feed", out)

    def test_rewriting_does_not_add_a_rule_each_time(self):
        out = NOTE
        for body in (SITREP_1, SITREP_2, SITREP_1, SITREP_2):
            out = _write(out, body)
        self.assertNotIn(DOUBLED, out)
        self.assertEqual(out.count("---"), _write(NOTE, SITREP_1).count("---"),
                         "the number of rules changed across rewrites")

    def test_a_note_that_already_stacked_rules_heals_on_the_next_write(self):
        stacked = NOTE.replace("**Status:**\n\n---\n\n",
                               "**Status:**\n\n---\n\n---\n\n---\n\n---\n\n")
        self.assertIn(DOUBLED, stacked)
        self.assertNotIn(DOUBLED, _write(stacked, SITREP_1))

    def test_identical_rewrite_is_stable(self):
        once = _write(NOTE, SITREP_1)
        self.assertEqual(_write(once, SITREP_1), once)


class TestNothingElseMoves(unittest.TestCase):
    def test_leading_rule_of_the_body_is_kept(self):
        out = _write(NOTE, SITREP_1)
        self.assertIn("## Sitrep\n\n---\n\n**Status:** Wednesday", out)

    def test_other_sections_are_untouched(self):
        out = _write(NOTE, SITREP_1)
        self.assertIn("## Location\n\n**Chico, CA**\n\n---\n\n## Sitrep", out)
        self.assertTrue(out.endswith("## Idea Dump\n\nnotes\n"))

    def test_body_without_its_own_rule_is_written_as_before(self):
        out = _write(NOTE, "plain body\n")
        self.assertIn("## Sitrep\n\nplain body\n\n---\n\n## Research Feed", out)

    def test_a_rule_inside_the_body_is_content(self):
        out = _write(NOTE, "above\n\n---\n\nbelow\n")
        self.assertIn("above\n\n---\n\nbelow\n\n---\n\n## Research Feed", out)


class TestThroughTheRealWriter(unittest.TestCase):
    """`write_section` wraps `_replace_section` in a no-op guard. Prove the pair."""

    DAY = "2026-10-07"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        notes = self.tmp / "Daily Notes"
        notes.mkdir()
        (notes / f"{self.DAY}.md").write_text(
            "---\ntype: daily\ndate: 2026-10-07\n---\n\n# Daily Note\n\n---\n\n" + NOTE,
            encoding="utf-8")
        patch = mock.patch.object(daily_note, "DAILY_NOTES_DIR", notes)
        patch.start()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(patch.stop)

    def _note(self) -> str:
        return daily_note.daily_path(self.DAY).read_text(encoding="utf-8")

    def test_three_writes_leave_one_rule(self):
        for body in (SITREP_1, SITREP_2, SITREP_1):
            daily_note.write_section("sitrep", body, actor="claude", date=self.DAY)
        self.assertNotIn(DOUBLED, self._note())
        self.assertIn("**Blockers:** None.\n\n---\n\n## Research Feed", self._note())

    def test_identical_rewrite_is_not_mistaken_for_a_failed_write(self):
        """Now that a rewrite is stable, the no-op guard must compare like with like."""
        daily_note.write_section("sitrep", SITREP_1, actor="claude", date=self.DAY)
        before = self._note()
        result = daily_note.write_section("sitrep", SITREP_1, actor="claude", date=self.DAY)
        self.assertEqual(result["status"], "written")
        self.assertEqual(self._note(), before)


class TestWhereTheNoteHasNoSeparator(unittest.TestCase):
    """Old-layout notes and last sections supply no rule. Leave the body alone."""

    def test_bodys_own_rule_survives_when_the_note_supplies_none(self):
        note = "## Sitrep\n\nold\n\n## Research Feed\n\nfeed\n"
        out = daily_note._replace_section(note, "## Sitrep", SITREP_1)
        self.assertIn("**Blockers:** None.\n\n---\n\n## Research Feed", out)
        self.assertNotIn(DOUBLED, out)

    def test_last_section_keeps_its_body_verbatim(self):
        note = "## Location\n\nx\n\n---\n\n## Sitrep\n\nold\n"
        out = daily_note._replace_section(note, "## Sitrep", SITREP_1)
        self.assertTrue(out.rstrip("\n").endswith("**Blockers:** None.\n\n---"))


if __name__ == "__main__":
    unittest.main()
