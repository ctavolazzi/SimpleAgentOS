"""
test_plan_rollover_gap.py — Unfinished work must survive a gap between plans.

Background: on 2026-10-07 spin-up created the day's plan with "0 items rolled
over" and `rolled_over_from: "[[Plans/2026-10-06_daily_plan]]"`. No plan
existed for 10-06, or for any day back to 09-21. Two defects:

  1. `analyze_rollover` treated "yesterday" as a calendar date. With no file
     for that date the seed was empty, and the 09-21 plan's two unfinished
     items sat outside the 7-day window, so they were dropped without a word.
  2. `create_today` wrote `source_date or _yesterday()` into the frontmatter,
     so a day with no source still linked to a plan that was never written.

The plan's own header says "nothing lost". The rollover source is therefore
the most recent plan that exists, however old, and a link is written only when
there is something on the other end of it.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_plan_rollover_gap.py -v
"""

import re
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import daily_plan
import plan_rollover

TODAY = date.today()


def _day(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


class _TmpPlans(unittest.TestCase):
    """A private Plans/ dir. Nothing here touches the vault or its lock."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        self.plans = self.vault / "Plans"
        self.plans.mkdir()
        for target, name, value in (
            (plan_rollover, "PLANS_DIR", self.plans),
            (daily_plan, "PLANS_DIR", self.plans),
            (daily_plan, "DAILY_NOTES_DIR", self.vault / "Daily Notes"),
        ):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(
            daily_plan.atomic_io, "vault_write",
            lambda path, content, encoding="utf-8": Path(path).write_text(content, encoding))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.vault, True)

    def _write_plan(self, days_ago: int, threads_section: str) -> str:
        d = _day(days_ago)
        (self.plans / f"{d}_daily_plan.md").write_text(
            daily_plan._PLAN_TEMPLATE.format(
                date=d, day_label=d, rolled_over_from='""',
                threads_section=threads_section),
            encoding="utf-8")
        return d

    def _todays_plan(self) -> str:
        return (self.plans / f"{_day(0)}_daily_plan.md").read_text(encoding="utf-8")

    def _rolled_over_from(self) -> str:
        m = re.search(r"^rolled_over_from:\s*(.*)$", self._todays_plan(), re.M)
        self.assertIsNotNone(m, "plan frontmatter lost its rolled_over_from key")
        return m.group(1).strip()

    def assertLinkResolves(self):
        """Whatever the frontmatter links to must exist."""
        m = re.search(r"\[\[Plans/([^\]|]+)\]\]", self._rolled_over_from())
        if m:
            self.assertTrue((self.plans / f"{m.group(1)}.md").exists(),
                            f"rolled_over_from links to a plan that does not exist: {m.group(1)}")


class TestGapLongerThanTheWindow(_TmpPlans):
    def setUp(self):
        super().setUp()
        self.old = self._write_plan(16, "- [ ] Prose, after the memo\n"
                                        "- [ ] Is Simeon a real worker\n"
                                        "- [x] Finished already\n")

    def test_source_is_the_most_recent_plan_that_exists(self):
        analysis = plan_rollover.analyze_rollover(days=7)
        self.assertEqual(analysis["source_date"], self.old)
        self.assertEqual(analysis["gap_days"], 16)

    def test_unfinished_items_are_seeded(self):
        seed = plan_rollover.build_seed_items(plan_rollover.analyze_rollover(days=7))
        self.assertEqual(seed, ["Prose, after the memo", "Is Simeon a real worker"])

    def test_todays_plan_carries_them_and_links_their_source(self):
        result = plan_rollover.create_today_from_rollover(days=7)
        self.assertTrue(result["created"])
        self.assertEqual(result["seed_count"], 2)
        text = self._todays_plan()
        self.assertIn("- [ ] Prose, after the memo", text)
        self.assertIn("- [ ] Is Simeon a real worker", text)
        self.assertNotIn("Finished already", text)
        self.assertIn(f"[[Plans/{self.old}_daily_plan]]", self._rolled_over_from())
        self.assertLinkResolves()


class TestNoEarlierPlanAtAll(_TmpPlans):
    def test_no_source_and_no_link(self):
        analysis = plan_rollover.analyze_rollover(days=7)
        self.assertIsNone(analysis["source_date"])
        result = plan_rollover.create_today_from_rollover(days=7)
        self.assertTrue(result["created"])
        self.assertEqual(result["seed_count"], 0)
        self.assertNotIn("[[", self._rolled_over_from(),
                         "linked to a previous plan when there is none")
        self.assertLinkResolves()


class TestYesterdayStillWins(_TmpPlans):
    """The ordinary case must behave exactly as before."""

    def test_yesterday_is_the_source_and_older_items_stay_out(self):
        self._write_plan(16, "- [ ] Old and unrelated\n")
        yesterday = self._write_plan(1, "- [ ] From yesterday\n")
        analysis = plan_rollover.analyze_rollover(days=7)
        self.assertEqual(analysis["source_date"], yesterday)
        self.assertEqual(analysis["gap_days"], 1)
        self.assertEqual(plan_rollover.build_seed_items(analysis), ["From yesterday"])
        plan_rollover.create_today_from_rollover(days=7)
        self.assertLinkResolves()

    def test_recurring_items_are_still_counted_inside_the_window_only(self):
        self._write_plan(16, "- [ ] Keeps coming up\n")
        self._write_plan(3, "- [ ] Keeps coming up\n")
        self._write_plan(1, "- [ ] Keeps coming up\n")
        recurring = plan_rollover.analyze_rollover(days=7)["recurring"]
        self.assertEqual([(r["item"], r["days_rolling"]) for r in recurring],
                         [("Keeps coming up", 2)])


class TestStatusLineSaysWhereTheSeedCameFrom(_TmpPlans):
    def test_gap_is_named(self):
        old = self._write_plan(16, "- [ ] Left open\n")
        result = plan_rollover.create_today_from_rollover(days=7)
        self.assertEqual(result["rollover_status"],
                         f"1 items rolled over from {old} (16 days back)")

    def test_no_earlier_plan_is_not_reported_as_a_clean_slate(self):
        result = plan_rollover.create_today_from_rollover(days=7)
        self.assertEqual(result["rollover_status"],
                         "0 items rolled over (no earlier plan to roll from)")

    def test_ordinary_day_keeps_the_plain_wording(self):
        self._write_plan(1, "- [ ] From yesterday\n")
        result = plan_rollover.create_today_from_rollover(days=7)
        self.assertEqual(result["rollover_status"], "1 items rolled over")


class TestShortGapInsideTheWindow(_TmpPlans):
    def test_a_plan_three_days_back_is_the_source(self):
        three = self._write_plan(3, "- [ ] Left open on Sunday\n")
        analysis = plan_rollover.analyze_rollover(days=7)
        self.assertEqual(analysis["source_date"], three)
        self.assertEqual(plan_rollover.build_seed_items(analysis), ["Left open on Sunday"])


if __name__ == "__main__":
    unittest.main()
