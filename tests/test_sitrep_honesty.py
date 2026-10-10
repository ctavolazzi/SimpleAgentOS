"""
test_sitrep_honesty.py — The sitrep must not print template scaffolding as work,
or label a local time as UTC.

Background: the 2026-10-07 evening spin-up produced a sitrep that read

    **Status:** Wednesday, October 7 · 84.9°F, Clear · session 19:29 UTC
    **Plan focus:**
    - ---

Two separate defects:

  1. `_read_plan_tasks` matched checkboxes with `\\s+`, which crosses line
     breaks. The blank plan template ends its only thread with an empty
     `- [ ]`, so the pattern ran on past the blank lines and captured the
     horizontal rule below it as the task text.
  2. `_scaffold_journal` stamped `datetime.now()` (local) with a hardcoded
     "UTC" suffix. 19:29 was Pacific time. The sitrep copies that line.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_sitrep_honesty.py -v
"""

import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import daily_plan
import sitrep_gen
import spin_up

DAY = "2026-10-07"


def _plan_text(threads_section: str) -> str:
    """Render a plan with the real template, so the fixture cannot drift from it."""
    return daily_plan._PLAN_TEMPLATE.format(
        date=DAY, day_label="Wednesday, October 7th 2026",
        rolled_over_from='""', threads_section=threads_section,
    )


class _TmpVault(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        (self.vault / "Plans").mkdir()
        patch = mock.patch.object(sitrep_gen, "VAULT_DIR", self.vault)
        patch.start()
        self.addCleanup(shutil.rmtree, self.vault, True)
        self.addCleanup(patch.stop)

    def _write_plan(self, threads_section: str) -> None:
        (self.vault / "Plans" / f"{DAY}_daily_plan.md").write_text(
            _plan_text(threads_section), encoding="utf-8")


class TestPlanFocus(_TmpVault):
    def test_blank_template_has_no_tasks(self):
        self._write_plan(daily_plan._BLANK_THREADS)
        self.assertEqual(sitrep_gen._read_plan_tasks(DAY), [])

    def test_blank_plan_prints_no_plan_focus_block(self):
        self._write_plan(daily_plan._BLANK_THREADS)
        md = sitrep_gen.generate(DAY)
        self.assertNotIn("- ---", md.splitlines())
        self.assertNotIn("**Plan focus:**", md)

    def test_real_tasks_are_still_read_in_order(self):
        self._write_plan("- [ ] Rule on the editorial note\n"
                         "- [x] Already done\n"
                         "- [ ] Second open item\n")
        self.assertEqual(sitrep_gen._read_plan_tasks(DAY),
                         ["Rule on the editorial note", "Second open item"])

    def test_empty_checkbox_does_not_swallow_the_next_line(self):
        self._write_plan("- [ ]\n- [ ] The real one\n")
        self.assertEqual(sitrep_gen._read_plan_tasks(DAY), ["The real one"])


class TestSessionStartLabel(unittest.TestCase):
    """The zone printed beside the session start must be the zone of that time."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        patch = mock.patch.object(spin_up, "VAULT_DIR", self.vault)
        patch.start()
        self.addCleanup(shutil.rmtree, self.vault, True)
        self.addCleanup(patch.stop)

    def test_label_matches_the_clock_it_was_read_from(self):
        with mock.patch("claude_journal.create_entry",
                        return_value={"created": True}) as create:
            ok, _ = spin_up._scaffold_journal(DAY, "Wednesday, October 7th", {})
        self.assertTrue(ok)
        recap = create.call_args.kwargs["session_recap"]
        m = re.search(r"\*\*Session start:\*\* (\d\d):(\d\d) (\S+)", recap)
        self.assertIsNotNone(m, f"unexpected session line: {recap!r}")

        local = datetime.now().astimezone()
        self.assertEqual(m.group(3), local.strftime("%Z"),
                         "session start is labelled with a zone the clock is not in")
        stamped = int(m.group(1)) * 60 + int(m.group(2))
        now = local.hour * 60 + local.minute
        self.assertLessEqual(min((now - stamped) % 1440, (stamped - now) % 1440), 1,
                             "session start is not the local time")


if __name__ == "__main__":
    unittest.main()
