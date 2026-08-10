"""
test_wheel_check_forward_dated.py — tomorrow's work efforts are not orphans.

Background: `wrap_up` runs `we_factory.create_for_top3(date=<tomorrow>)`, so
tonight's close-out writes work-effort files stamped with tomorrow's date and
aimed at a daily note that will not exist until the morning spin-up builds it.
`check_orphans` looks at files MODIFIED on the checked date and warns when they
have zero inbound wikilinks — which such a file always does, by construction.
Every wrap-up therefore ended with an orphan warning that described the pipeline
working correctly. The fix belongs in the checker, not the vault.

The exemption is deliberately narrow: it keys on the date embedded in the work
effort's own filename being strictly LATER than the day under check. A file
stamped today or earlier with no inbound links is still a real orphan, and the
negative controls below hold that line.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_wheel_check_forward_dated.py -v
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import wheel_check


class TestForwardDatedDetection(unittest.TestCase):
    """The predicate itself, isolated from vault I/O."""

    def test_tomorrows_effort_is_forward_dated(self):
        """The exact case wrap_up produces."""
        p = Path("10.108_20260809_ship_the_thing.md")
        self.assertTrue(wheel_check._is_forward_dated(p, "2026-08-08"))

    def test_todays_effort_is_not_forward_dated(self):
        """Same day is not the future. A real orphan must stay a real orphan."""
        p = Path("10.108_20260808_ship_the_thing.md")
        self.assertFalse(wheel_check._is_forward_dated(p, "2026-08-08"))

    def test_yesterdays_effort_is_not_forward_dated(self):
        p = Path("10.108_20260807_ship_the_thing.md")
        self.assertFalse(wheel_check._is_forward_dated(p, "2026-08-08"))

    def test_three_digit_minor_is_recognised(self):
        """Must agree with we_factory's widened prefix regex, or 10.100+ leaks."""
        p = Path("10.102_20260809_ship_the_thing.md")
        self.assertTrue(wheel_check._is_forward_dated(p, "2026-08-08"))

    def test_non_work_effort_files_are_never_exempt(self):
        """The blast radius must not reach ordinary vault notes."""
        for name in ["2026-08-09.md", "some_note.md", "40.13_sts_accounts.md",
                     "Hubs_2026-08-09_hub.md", "10.108_notadate_x.md"]:
            self.assertFalse(wheel_check._is_forward_dated(Path(name), "2026-08-08"),
                             f"{name} was wrongly exempted from orphan checking")

    def test_garbage_date_is_not_exempt(self):
        """An unparseable stamp must fail closed, not open."""
        p = Path("10.108_99999999_x.md")
        self.assertFalse(wheel_check._is_forward_dated(p, "2026-08-08"))

    def test_month_and_year_boundaries(self):
        self.assertTrue(wheel_check._is_forward_dated(
            Path("10.01_20260901_x.md"), "2026-08-31"))
        self.assertTrue(wheel_check._is_forward_dated(
            Path("10.01_20270101_x.md"), "2026-12-31"))
        self.assertFalse(wheel_check._is_forward_dated(
            Path("10.01_20260831_x.md"), "2026-09-01"))


class TestOrphanCheckIntegration(unittest.TestCase):
    """Drive check_orphans against a throwaway vault."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = wheel_check.daily_note.VAULT_DIR
        wheel_check.daily_note.VAULT_DIR = self.tmp

    def tearDown(self):
        wheel_check.daily_note.VAULT_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, filename, date_str="2026-08-08"):
        import os
        from datetime import datetime
        p = self.tmp / filename
        p.write_text("---\ntitle: x\n---\nbody\n")
        # Pin mtime into the checked day so the file is in scope.
        ts = datetime.strptime(date_str, "%Y-%m-%d").timestamp() + 3600
        os.utime(p, (ts, ts))
        r = wheel_check.Result()
        wheel_check.check_orphans(date_str, r)
        return r

    def test_forward_dated_effort_produces_no_warning(self):
        r = self._run("10.108_20260809_ship_the_thing.md")
        orphan_warnings = [w for w in r.warnings if "ORPHAN" in w]
        self.assertEqual(orphan_warnings, [],
                         f"tomorrow's effort still warned: {orphan_warnings}")

    def test_a_genuine_orphan_still_warns(self):
        """Guard the guard. If this stops failing, the exemption is too wide."""
        r = self._run("10.108_20260808_written_today_linked_nowhere.md")
        orphan_warnings = [w for w in r.warnings if "ORPHAN" in w]
        self.assertTrue(orphan_warnings,
                        "a real same-day orphan was silently exempted")

    def test_plain_note_orphan_still_warns(self):
        r = self._run("a_loose_thought.md")
        self.assertTrue([w for w in r.warnings if "ORPHAN" in w])


if __name__ == "__main__":
    unittest.main()
