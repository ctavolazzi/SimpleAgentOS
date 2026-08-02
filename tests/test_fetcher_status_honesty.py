"""
test_fetcher_status_honesty.py — A fetcher must not report "ok" on an empty result.

Background: spin_up's per-fetcher status line is the only signal that something
went wrong, because the daily note itself renders an empty result as prose ("No
papers in cs.AI...", "no stories") which reads like a finding rather than a
fault. arXiv sat empty for three separate days while the status said "ok".

Most fetchers already self-disclose by putting a count in the status
("ok (3 events)", "ok (AQI 46)"). These tests pin the two that did not.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_fetcher_status_honesty.py -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import git_scanner
import local_news
import spin_up


class TestNewsStatus(unittest.TestCase):
    def test_empty_feed_is_suspicious_not_ok(self):
        with mock.patch.object(local_news, "fetch",
                               return_value={"count": 0, "stories": []}):
            _, status = spin_up._fetch_news(force=True)
        self.assertTrue(status.startswith("suspicious"),
                        f"an empty news feed reported {status!r}")

    def test_populated_feed_reports_its_count(self):
        payload = {"count": 3, "stories": [{"title": "a"}, {"title": "b"}, {"title": "c"}]}
        with mock.patch.object(local_news, "fetch", return_value=payload):
            _, status = spin_up._fetch_news(force=True)
        self.assertIn("3 stories", status)
        self.assertFalse(status.startswith("suspicious"))

    def test_count_falls_back_to_len_of_stories(self):
        """Payload without an explicit count must still report honestly."""
        with mock.patch.object(local_news, "fetch",
                               return_value={"stories": [{"title": "a"}]}):
            _, status = spin_up._fetch_news(force=True)
        self.assertIn("1 stories", status)


class TestGitScanStatus(unittest.TestCase):
    def test_zero_repos_is_suspicious(self):
        """Zero repos under ~/Code is a broken walk, never a real reading."""
        with mock.patch.object(git_scanner, "scan_workspace", return_value=[]):
            _, status = spin_up._fetch_git_scan()
        self.assertTrue(status.startswith("suspicious"),
                        f"an empty repo scan reported {status!r}")

    def test_populated_scan_reports_its_count(self):
        with mock.patch.object(git_scanner, "scan_workspace",
                               return_value=[{"name": "a"}, {"name": "b"}]):
            _, status = spin_up._fetch_git_scan()
        self.assertIn("2 repos", status)


class TestArxivStatus(unittest.TestCase):
    """The original offender, pinned at the spin_up layer rather than in arxiv.py."""

    def _digest(self, suspicious, widened=True, days=6):
        return {
            "suspicious_empty": suspicious,
            "window_widened": widened,
            "days": days,
            "physics": {"papers": [], "total_fetched": 0, "categories": []},
            "ai": {"papers": [], "total_fetched": 0, "categories": []},
        }

    def test_empty_after_widening_is_suspicious(self):
        import arxiv
        with mock.patch.object(arxiv, "fetch_dual_pane",
                               return_value=self._digest(True)):
            with mock.patch.object(spin_up, "_write_cache", lambda *a, **kw: None):
                _, status = spin_up._fetch_arxiv(force=True)
        self.assertTrue(status.startswith("suspicious"), status)

    def test_widened_but_populated_says_so(self):
        import arxiv
        with mock.patch.object(arxiv, "fetch_dual_pane",
                               return_value=self._digest(False, widened=True, days=6)):
            with mock.patch.object(spin_up, "_write_cache", lambda *a, **kw: None):
                _, status = spin_up._fetch_arxiv(force=True)
        self.assertIn("widened to 6d", status)
        self.assertFalse(status.startswith("suspicious"))

    def test_normal_day_is_plain_ok(self):
        import arxiv
        with mock.patch.object(arxiv, "fetch_dual_pane",
                               return_value=self._digest(False, widened=False, days=2)):
            with mock.patch.object(spin_up, "_write_cache", lambda *a, **kw: None):
                _, status = spin_up._fetch_arxiv(force=True)
        self.assertEqual(status, "ok")


if __name__ == "__main__":
    unittest.main()
