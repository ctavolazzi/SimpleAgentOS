"""
test_claude_journal_sections.py — Regression tests for the silent journal writes.

Background: every `claude_journal.py add-*` verb was returning {"ok": false} and
exiting 0. Three separate faults stacked:

  1. Two writers, two templates. spin_up._scaffold_journal wrote a file with only
     "## Session Log" and "## Notes"; the add-* verbs target "## Realizations",
     "## Open Questions", "## Threads I'm Holding" and "## What I Find
     Interesting". create_entry is idempotent, so it saw the file already existed
     and never repaired the headings.
  2. The last section in a file was unwritable. _append_to_section inserted
     before the next "---" or "## ", and at EOF there was nothing to insert
     before, so it returned False forever.
  3. Placeholder removal shifted the insertion offset. insert_pos was computed
     against the pre-substitution string and then used to slice the
     post-substitution string.

The failure went unnoticed for a session because the output was piped through
`tail -2`, which cropped the `ok` field and left a timestamp that read as success.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_claude_journal_sections.py -v
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import claude_journal


# The exact shape spin_up used to scaffold: none of the add-* target headings.
SPINUP_TEMPLATE = """\
---
type: claude_journal
date: 2026-08-02
---

# Claude's Journal — Sunday, August 2nd

## Session Log

**Session start:** 09:12 UTC

---

## Notes

"""

# A file whose target section is last, with nothing after it to insert before.
TRAILING_SECTION = """\
# Journal

## Session Log

something

## Realizations
"""


class JournalTestCase(unittest.TestCase):
    """Redirects the module at a temp dir so no test touches the real vault."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.journal_dir = Path(self.tmp) / "Claude Journal"
        self.journal_dir.mkdir(parents=True)
        self._patches = [
            mock.patch.object(claude_journal, "JOURNAL_DIR", self.journal_dir),
            mock.patch.object(claude_journal, "DAILY_NOTES_DIR", Path(self.tmp) / "Daily Notes"),
            # Journaling to PocketBase is not what these tests are about.
            mock.patch.object(claude_journal, "_remember", lambda *a, **kw: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def write(self, date, content):
        path = self.journal_dir / f"{date}.md"
        path.write_text(content, encoding="utf-8")
        return path


class TestMissingSectionSelfHeal(JournalTestCase):
    """Fault 1: a journal written from the other template must still accept writes."""

    def test_add_realization_creates_the_missing_section(self):
        path = self.write("2026-08-02", SPINUP_TEMPLATE)
        result = claude_journal.add_realization("the thing that clicked", d="2026-08-02")

        self.assertTrue(result["ok"], "write must succeed against a spin_up-shaped file")
        text = path.read_text(encoding="utf-8")
        self.assertIn("## Realizations", text)
        self.assertIn("the thing that clicked", text)

    def test_self_heal_preserves_existing_content(self):
        path = self.write("2026-08-02", SPINUP_TEMPLATE)
        claude_journal.add_question("what is still unresolved", d="2026-08-02")
        text = path.read_text(encoding="utf-8")

        self.assertIn("## Session Log", text)
        self.assertIn("**Session start:** 09:12 UTC", text)
        self.assertIn("## Notes", text)
        self.assertIn("## Open Questions", text)

    def test_each_verb_lands_in_its_own_section(self):
        path = self.write("2026-08-02", SPINUP_TEMPLATE)
        claude_journal.add_realization("R", d="2026-08-02")
        claude_journal.add_question("Q", d="2026-08-02")
        claude_journal.add_thread("T", d="2026-08-02")
        claude_journal.add_interesting("I", d="2026-08-02")
        text = path.read_text(encoding="utf-8")

        for header, bullet in [
            ("## Realizations", "R"),
            ("## Open Questions", "Q"),
            ("## Threads I'm Holding", "T"),
            ("## What I Find Interesting", "I"),
        ]:
            self.assertIn(header, text)
            section = text.split(header, 1)[1].split("\n## ", 1)[0]
            self.assertIn(f"] {bullet}", section,
                          f"{bullet!r} should be under {header!r}")


class TestTrailingSection(JournalTestCase):
    """Fault 2: EOF is a valid insertion boundary."""

    def test_last_section_in_file_is_writable(self):
        path = self.write("2026-08-02", TRAILING_SECTION)
        result = claude_journal.add_realization("written at EOF", d="2026-08-02")

        self.assertTrue(result["ok"], "a trailing section used to be unwritable")
        self.assertIn("written at EOF", path.read_text(encoding="utf-8"))

    def test_repeated_writes_to_trailing_section_accumulate(self):
        path = self.write("2026-08-02", TRAILING_SECTION)
        claude_journal.add_realization("first", d="2026-08-02")
        claude_journal.add_realization("second", d="2026-08-02")
        text = path.read_text(encoding="utf-8")

        self.assertIn("first", text)
        self.assertIn("second", text)
        self.assertLess(text.index("first"), text.index("second"),
                        "bullets should append in order")


class TestPlaceholderOffset(JournalTestCase):
    """Fault 3: stripping placeholders must not move the insertion point."""

    def test_bullet_lands_in_the_right_section_despite_earlier_placeholder(self):
        """A placeholder BEFORE the target section used to shift the split."""
        content = (
            "# Journal\n\n"
            "## Session Recap\n\n"
            "<!-- Fill in during/after session -->\n\n"
            "---\n\n"
            "## Realizations\n\n"
            "<!-- One bullet per realization -->\n\n"
            "---\n\n"
            "## Open Questions\n\n"
            "<!-- One bullet per question -->\n"
        )
        path = self.write("2026-08-02", content)
        claude_journal.add_realization("correctly placed", d="2026-08-02")
        text = path.read_text(encoding="utf-8")

        realizations = text.split("## Realizations", 1)[1].split("## Open Questions", 1)[0]
        self.assertIn("correctly placed", realizations,
                      "bullet leaked out of its section, offset bug is back")

    def test_placeholders_are_removed(self):
        path = self.write("2026-08-02",
                          "# J\n\n## Realizations\n\n<!-- One bullet per realization -->\n")
        claude_journal.add_realization("real content", d="2026-08-02")
        text = path.read_text(encoding="utf-8")

        self.assertNotIn("<!-- One bullet per realization -->", text)
        self.assertIn("real content", text)


class TestMissingFile(JournalTestCase):
    """A journal that does not exist is still a failure, and must say so."""

    def test_returns_not_ok(self):
        result = claude_journal.add_realization("nowhere to go", d="1999-01-01")
        self.assertFalse(result["ok"])


class TestCliExitCode(unittest.TestCase):
    """The CLI must not exit 0 on a failed write."""

    def test_failed_write_exits_nonzero(self):
        proc = subprocess.run(
            [sys.executable, "claude_journal.py", "add-realization", "x",
             "--date", "1999-01-01"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1,
                         "a failed write that exits 0 is how this stayed hidden")
        self.assertIn('"ok": false', proc.stdout)

    def test_status_verb_still_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, "claude_journal.py", "status", "--date", "1999-01-01"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(json.loads(proc.stdout)["exists"])


if __name__ == "__main__":
    unittest.main()
