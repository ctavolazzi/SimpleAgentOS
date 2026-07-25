"""
test_daily_note_hardening.py — Regression tests for the Daily Note OS hardening pass.

Covers: write_section self-heal + no-op guard, gap-tolerant handoff,
create_from_template rendering/idempotence, and the graceful missing-note CLI.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_daily_note_hardening.py -v
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import daily_note


MINIMAL_TEMPLATE = """\
---
type: daily
date: <% tp.date.now("YYYY-MM-DD") %>
tags:
  - daily
---

# Daily Note <% tp.date.now("dddd, MMMM Do") %>

**Yesterday:** [[<% tp.date.now("YYYY-MM-DD", -1) %>]] | **Tomorrow:** [[<% tp.date.now("YYYY-MM-DD", 1) %>]]

---

## Sitrep

**Status:**

---

## Tomorrow's Top 3

- [ ]
- [ ]
- [ ]
"""


class DailyNoteTestCase(unittest.TestCase):
    """Points daily_note at an isolated tmp vault for the duration of each test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.notes_dir = self.tmp / "Daily Notes"
        self.template_path = self.tmp / "template.md"
        self.template_path.write_text(MINIMAL_TEMPLATE, encoding="utf-8")

        self._orig_notes_dir = daily_note.DAILY_NOTES_DIR
        self._orig_template = daily_note.TEMPLATE_PATH
        daily_note.DAILY_NOTES_DIR = self.notes_dir
        daily_note.TEMPLATE_PATH = self.template_path

    def tearDown(self):
        daily_note.DAILY_NOTES_DIR = self._orig_notes_dir
        daily_note.TEMPLATE_PATH = self._orig_template
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_note(self, date_str: str, text: str) -> Path:
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        path = self.notes_dir / f"{date_str}.md"
        path.write_text(text, encoding="utf-8")
        return path


class TestCreateFromTemplate(DailyNoteTestCase):

    def test_renders_all_tokens_no_residue(self):
        path = daily_note.create_from_template("2099-03-01")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("<%", text)
        self.assertIn("date: 2099-03-01", text)

    def test_month_boundary_offsets(self):
        path = daily_note.create_from_template("2099-03-01")
        text = path.read_text(encoding="utf-8")
        self.assertIn("[[2099-02-28]]", text)  # yesterday, prior month
        self.assertIn("[[2099-03-02]]", text)  # tomorrow

    def test_ordinal_and_weekday_header(self):
        path = daily_note.create_from_template("2099-03-01")
        text = path.read_text(encoding="utf-8")
        self.assertIn("# Daily Note Sunday, March 1st", text)

    def test_idempotent_no_overwrite(self):
        path1 = daily_note.create_from_template("2099-03-01")
        path1.write_text(path1.read_text(encoding="utf-8") + "\nEDITED", encoding="utf-8")
        path2 = daily_note.create_from_template("2099-03-01")
        self.assertEqual(path1, path2)
        self.assertIn("EDITED", path2.read_text(encoding="utf-8"))

    def test_missing_template_raises(self):
        daily_note.TEMPLATE_PATH = self.tmp / "does_not_exist.md"
        with self.assertRaises(FileNotFoundError):
            daily_note.create_from_template("2099-03-02")


class TestWriteSectionSelfHeal(DailyNoteTestCase):

    def test_absent_header_appends_section(self):
        self._write_note("2099-04-01", MINIMAL_TEMPLATE.replace(
            '<% tp.date.now("YYYY-MM-DD") %>', "2099-04-01"
        ).replace(
            '<% tp.date.now("YYYY-MM-DD", -1) %>', "2099-03-31"
        ).replace(
            '<% tp.date.now("YYYY-MM-DD", 1) %>', "2099-04-02"
        ).replace(
            '<% tp.date.now("dddd, MMMM Do") %>', "Wednesday, April 1st"
        ).replace("## Sitrep\n\n**Status:**\n\n---\n\n", ""))  # drop the sitrep section entirely

        result = daily_note.write_section("sitrep", "**Status:** testing self-heal",
                                          actor="claude", mode="replace", date="2099-04-01")
        self.assertEqual(result["status"], "written")
        text = daily_note.read_full("2099-04-01")
        self.assertIn("## Sitrep", text)
        self.assertIn("testing self-heal", text)

    def test_identical_content_replace_does_not_raise(self):
        daily_note.create_from_template("2099-04-02")
        daily_note.write_section("sitrep", "**Status:** same\n", actor="claude",
                                 mode="replace", date="2099-04-02")
        # Writing the exact same content again must not raise the no-op guard.
        daily_note.write_section("sitrep", "**Status:** same\n", actor="claude",
                                 mode="replace", date="2099-04-02")

    def test_append_mode_still_works_with_present_header(self):
        daily_note.create_from_template("2099-04-03")
        daily_note.write_section("sitrep", "first entry", actor="claude",
                                 mode="append", date="2099-04-03")
        daily_note.write_section("sitrep", "second entry", actor="claude",
                                 mode="append", date="2099-04-03")
        text = daily_note.read_section("sitrep", "2099-04-03")
        self.assertIn("first entry", text)
        self.assertIn("second entry", text)


class TestSessionLogCalloutSeparation(DailyNoteTestCase):
    """
    Successive session-log entries must render as siblings, not nest.

    Regression: append glued the new attribution line directly onto the
    previous entry's last `>>` line. Markdown lazy continuation then absorbed
    the whole new entry into the previous callout, so the 08:20 entry rendered
    as body text inside the 07:45 one.
    """

    DATE = "2099-05-01"

    def _append_two(self):
        daily_note.create_from_template(self.DATE)
        daily_note.append_session_log(
            focus="first entry", changes=["alpha"], date=self.DATE)
        daily_note.append_session_log(
            focus="second entry", changes=["beta"], date=self.DATE)
        return daily_note.read_section("claude_session_log", self.DATE)

    def test_blank_line_precedes_each_attribution(self):
        lines = self._append_two().splitlines()
        attributions = [i for i, ln in enumerate(lines) if ln.startswith("**[")]
        self.assertEqual(len(attributions), 2, "expected two attributed entries")
        for i in attributions:
            if i == 0:
                continue  # first line of the section body; header supplies the break
            self.assertEqual(
                lines[i - 1].strip(), "",
                f"line {i} ({lines[i]!r}) must be preceded by a blank line; "
                f"got {lines[i - 1]!r}; the new entry will nest in the previous callout",
            )

    def test_no_quoted_line_directly_precedes_an_attribution(self):
        """The precise failure mode: a `>`-prefixed line immediately above `**[`."""
        lines = self._append_two().splitlines()
        for prev, cur in zip(lines, lines[1:]):
            if cur.startswith("**["):
                self.assertFalse(
                    prev.lstrip().startswith(">"),
                    f"quoted line {prev!r} directly precedes {cur!r}",
                )

    def test_multiline_values_cannot_escape_the_callout(self):
        daily_note.create_from_template("2099-05-02")
        daily_note.append_session_log(
            focus="headline\nsmuggled out",
            changes=["repo - fix\nunquoted line"],
            next_steps="do\nthing",
            date="2099-05-02",
        )
        body = daily_note.read_section("claude_session_log", "2099-05-02")
        for ln in body.splitlines():
            if ln.strip() and not ln.startswith("**["):
                self.assertTrue(
                    ln.lstrip().startswith(">") or ln.startswith("---"),
                    f"line escaped the callout block: {ln!r}",
                )
        self.assertIn("headline smuggled out", body)
        self.assertIn("repo - fix unquoted line", body)

    def test_first_entry_has_no_leading_blank_run(self):
        """Empty section + append must not open with stacked blank lines."""
        daily_note.create_from_template("2099-05-03")
        daily_note.append_session_log(focus="only entry", date="2099-05-03")
        body = daily_note.read_section("claude_session_log", "2099-05-03")
        self.assertFalse(body.startswith("\n\n"), repr(body[:20]))


class TestMostRecentNoteAndHandoff(DailyNoteTestCase):

    def _iso(self, days_ago: int) -> str:
        from datetime import datetime, timedelta
        return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    def test_finds_note_across_a_short_gap(self):
        gap_date = self._iso(3)
        daily_note.create_from_template(gap_date)
        found = daily_note.most_recent_note()
        self.assertEqual(found, gap_date)

    def test_finds_note_across_a_month_long_gap(self):
        """No coding for a month, then coming back — still finds the last note."""
        gap_date = self._iso(35)
        daily_note.create_from_template(gap_date)
        found = daily_note.most_recent_note()  # unbounded by default
        self.assertEqual(found, gap_date)

    def test_finds_note_across_a_year_long_gap(self):
        gap_date = self._iso(400)
        daily_note.create_from_template(gap_date)
        found = daily_note.most_recent_note()
        self.assertEqual(found, gap_date)

    def test_picks_the_latest_of_multiple_prior_notes(self):
        for d in (self._iso(10), self._iso(3), self._iso(60)):
            daily_note.create_from_template(d)
        found = daily_note.most_recent_note()
        self.assertEqual(found, self._iso(3))

    def test_returns_none_when_vault_empty(self):
        found = daily_note.most_recent_note()
        self.assertIsNone(found)

    def test_explicit_max_back_still_bounds_when_requested(self):
        """max_back is opt-in only — passing it explicitly still works."""
        gap_date = self._iso(60)
        daily_note.create_from_template(gap_date)
        self.assertIsNone(daily_note.most_recent_note(max_back=14))
        self.assertEqual(daily_note.most_recent_note(max_back=90), gap_date)

    def test_last_handoff_reports_gap_days_across_a_month(self):
        gap_date = self._iso(35)
        daily_note.create_from_template(gap_date)
        daily_note.write_section("tomorrows_top_3", "- [ ] carried over item",
                                 actor="claude", mode="replace", date=gap_date)
        handoff = daily_note.last_handoff()
        self.assertTrue(handoff["found"])
        self.assertEqual(handoff["date"], gap_date)
        self.assertEqual(handoff["gap_days"], 35)
        self.assertIn("carried over item", handoff["tomorrows_top_3"])


class TestCLIGracefulMissingNote(unittest.TestCase):
    """Runs the real CLI as a subprocess with HOME redirected to an empty
    tmp dir — VAULT_DIR derives from Path.home(), so this isolates the
    module's real startup path without needing to patch internals."""

    def setUp(self):
        self.fake_home = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.fake_home, ignore_errors=True)

    def _run(self, *args) -> subprocess.CompletedProcess:
        import os
        env = dict(os.environ, HOME=str(self.fake_home))
        return subprocess.run(
            [sys.executable, str(ROOT / "daily_note.py"), *args],
            capture_output=True, text=True, timeout=15, env=env,
        )

    def test_status_on_missing_note_exits_cleanly(self):
        result = self._run("status")
        self.assertEqual(result.returncode, 0)
        self.assertIn("create", result.stdout.lower())

    def test_read_on_missing_note_exits_cleanly(self):
        result = self._run("read", "sitrep")
        self.assertEqual(result.returncode, 0)
        self.assertIn("create", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
