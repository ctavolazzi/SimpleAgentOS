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

import json
import os
import shutil
import sys
import tempfile
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


class TestFaultInjectionCannotPoisonCache(unittest.TestCase):
    """A test that stubs a feed dead must not persist its fake.

    On 2026-08-02 a negative control stubbed the arXiv feed empty and called
    `_fetch_arxiv(force=True)`. `force` skips the cache READ but not the cache
    WRITE, so the stubbed empty digest landed in ~/.cache/daily-harness and the
    next real spin-up would have served it as though it were a measurement. It
    was caught and repaired that day, but only because someone thought to look.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._patch = mock.patch.object(spin_up, "CACHE_DIR", self.tmp)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_cache_honours_the_redirect(self):
        spin_up._write_cache("probe", {"value": 1})
        written = list(self.tmp.glob("probe-*.json"))
        self.assertEqual(len(written), 1,
                         "cache write did not land in the redirected dir")
        self.assertEqual(json.loads(written[0].read_text())["value"], 1)

    def test_stubbed_dead_feed_writes_only_to_the_redirected_dir(self):
        """The exact 08-02 sequence, now sealed off from the real cache."""
        import arxiv

        def dead(*a, **kw):
            return {"categories": [], "fetched_at": "x",
                    "date_range": ("20260101", "20260102"), "papers": []}

        with mock.patch.object(arxiv, "fetch", side_effect=dead):
            _, status = spin_up._fetch_arxiv(force=True)

        self.assertTrue(status.startswith("suspicious"), status)
        poisoned = list(self.tmp.glob("arxiv-dual-*.json"))
        self.assertEqual(len(poisoned), 1,
                         "the fake should be written, but only into the tmpdir")
        payload = json.loads(poisoned[0].read_text())
        self.assertTrue(payload["suspicious_empty"])

    def test_real_cache_dir_is_not_the_test_dir(self):
        """Guard the guard: if the patch stopped working this test fails first."""
        self._patch.stop()
        try:
            self.assertNotEqual(spin_up.CACHE_DIR, self.tmp)
            self.assertIn("daily-harness", str(spin_up.CACHE_DIR))
        finally:
            self._patch.start()

    def test_cache_dir_is_env_overridable(self):
        """SPINUP_CACHE_DIR is read at import, so prove it via a fresh import."""
        import importlib
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-c",
             "import spin_up; print(spin_up.CACHE_DIR)"],
            cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "SPINUP_CACHE_DIR": str(self.tmp)},
        )
        self.assertEqual(proc.stdout.strip(), str(self.tmp),
                         f"env override ignored; stderr={proc.stderr[-400:]}")


if __name__ == "__main__":
    unittest.main()
