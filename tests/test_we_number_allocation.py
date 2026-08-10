"""
test_we_number_allocation.py — Johnny Decimal IDs must be unique.

Background: on 2026-08-08 a scan of the vault found 177 work-effort files
carrying 23 files across 7 colliding IDs, with `10.102` used by seven separate
efforts. An ID reused seven times is not an identifier.

Root cause is one character class. `WE_PREFIX_RE` was:

    ^(\\d{2})\\.(\\d{2})_\\d{8}_.+\\.md$

The minor group matches EXACTLY two digits, then demands `_`. So
`10.102_20260808_foo.md` fails the match entirely: the regex takes "10" as the
minor and then finds "2" where it wants "_". Every file numbered 100 or above is
invisible to `next_we_number`, which caps `max_minor` at 99 and returns "10.100"
forever. The batch creators then lay down 10.100, 10.101, 10.102... on top of the
previous run's files, every single run.

Measured before the fix, against the real vault:
    total .md under _work_efforts_ : 177
    invisible to WE_PREFIX_RE      : 28
    next_we_number() returned      : 10.100
    highest minor actually on disk : 107

The pre-100 pairs (10.40, 10.52) are from April/May and a different, historical
cause — a directory move, not this allocator. They are not pinned here. Nor are
the four `00.00_index.md` files, which are per-folder indexes and correct.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_we_number_allocation.py -v
"""

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import we_factory as wf


def touch(dir_: Path, name: str):
    (dir_ / name).write_text("---\ntitle: \"x\"\n---\n")


class TestPrefixRegexSeesLargeMinors(unittest.TestCase):
    """The regex is the root cause. Pin it directly."""

    def test_two_digit_minor_matches(self):
        self.assertTrue(wf.WE_PREFIX_RE.match("10.42_20260808_something.md"))

    def test_three_digit_minor_matches(self):
        """The exact filename shape that went invisible."""
        m = wf.WE_PREFIX_RE.match("10.102_20260808_commit_clean_code.md")
        self.assertIsNotNone(m, "a 3-digit minor is still invisible to the scanner")
        self.assertEqual(m.group(2), "102",
                         f"minor parsed as {m.group(2)!r}, not '102'")

    def test_four_digit_minor_matches(self):
        """This will bite again at 1000 if the group is bounded."""
        m = wf.WE_PREFIX_RE.match("10.1042_20260808_something.md")
        self.assertIsNotNone(m, "the same wall exists one order of magnitude up")
        self.assertEqual(m.group(2), "1042")

    def test_still_rejects_non_we_files(self):
        """Negative control: loosening the minor must not swallow everything."""
        for bad in ["README.md", "00.00_index.md", "notes.md",
                    "10.42_notadate_x.md", "10.42_2026_x.md"]:
            self.assertIsNone(wf.WE_PREFIX_RE.match(bad),
                              f"regex wrongly claimed {bad!r}")


class TestNextNumberOnDisk(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_dir_starts_at_one(self):
        self.assertEqual(wf.next_we_number(self.tmp), "10.01")

    def test_increments_past_ninety_nine(self):
        """The reported bug, reproduced at its boundary."""
        touch(self.tmp, "10.99_20260808_a.md")
        self.assertEqual(wf.next_we_number(self.tmp), "10.100")

    def test_does_not_restart_after_crossing_one_hundred(self):
        """The failure that produced seven copies of 10.102."""
        for n in ("10.99", "10.100", "10.101", "10.102"):
            touch(self.tmp, f"{n}_20260808_a.md")
        self.assertEqual(wf.next_we_number(self.tmp), "10.103")

    def test_real_vault_shape(self):
        """Highest on disk is 107; the next number must be 108, not 100."""
        for n in (40, 52, 99, 100, 101, 102, 107):
            touch(self.tmp, f"10.{n}_20260808_a.md")
        self.assertEqual(wf.next_we_number(self.tmp), "10.108")

    def test_ignores_other_majors(self):
        touch(self.tmp, "40.500_20260808_a.md")
        touch(self.tmp, "10.05_20260808_a.md")
        self.assertEqual(wf.next_we_number(self.tmp), "10.06")


class TestBatchAllocationIsCollisionFree(unittest.TestCase):
    """Fixing the regex is necessary but not sufficient.

    `create_for_quests` and `create_for_top3` each computed `start_num` once and
    then added the loop index, so any ID already taken inside that span was
    silently overwritten rather than skipped.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_allocates_a_contiguous_block_when_free(self):
        touch(self.tmp, "10.10_20260808_a.md")
        self.assertEqual(wf.allocate_we_numbers(3, self.tmp),
                         ["10.11", "10.12", "10.13"])

    def test_skips_ids_already_on_disk(self):
        """A gap in the middle must be stepped over, never written through."""
        for n in ("10.10", "10.12", "10.13"):
            touch(self.tmp, f"{n}_20260808_a.md")
        got = wf.allocate_we_numbers(3, self.tmp)
        self.assertNotIn("10.12", got)
        self.assertNotIn("10.13", got)
        self.assertEqual(len(set(got)), 3, "allocator returned duplicates")

    def test_never_returns_an_id_that_exists(self):
        for n in (99, 100, 101, 102, 107):
            touch(self.tmp, f"10.{n}_20260808_a.md")
        existing = {p.name.split("_")[0] for p in self.tmp.iterdir()}
        for got in wf.allocate_we_numbers(5, self.tmp):
            self.assertNotIn(got, existing,
                             f"allocator handed back {got}, which is already taken")

    def test_zero_and_negative_counts_are_empty(self):
        self.assertEqual(wf.allocate_we_numbers(0, self.tmp), [])
        self.assertEqual(wf.allocate_we_numbers(-1, self.tmp), [])


class TestCollisionAudit(unittest.TestCase):
    """A reusable detector, so this cannot rot back in unnoticed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean_dir_reports_nothing(self):
        for n in ("10.01", "10.02", "10.100"):
            touch(self.tmp, f"{n}_20260808_a.md")
        self.assertEqual(wf.find_number_collisions(self.tmp), {})

    def test_detects_the_ten_one_oh_two_case(self):
        touch(self.tmp, "10.102_20260808_commit_clean_code.md")
        touch(self.tmp, "10.102_20260726_sts_deploy.md")
        touch(self.tmp, "10.102_20260729_commit_clean.md")
        found = wf.find_number_collisions(self.tmp)
        self.assertIn("10.102", found)
        self.assertEqual(len(found["10.102"]), 3)


if __name__ == "__main__":
    unittest.main()
