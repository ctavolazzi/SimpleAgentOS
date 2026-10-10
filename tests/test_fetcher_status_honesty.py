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

# The cache dir spin_up resolved at import. No test in this file may write here.
IMPORT_TIME_CACHE_DIR = spin_up.CACHE_DIR

# Faithful to local_news.fetch(): the keys format_news_md actually indexes.
GOOD_NEWS = {
    "query": "Chico California",
    "fetched_at": "2026-10-07T19:40:00",
    "count": 2,
    "stories": [
        {"title": "Council votes", "link": "https://example.com/1",
         "published": "Wed, 07 Oct 2026 18:00:00 GMT", "source": "Example News"},
        {"title": "Creek cleanup", "link": "https://example.com/2",
         "published": "Wed, 07 Oct 2026 17:00:00 GMT", "source": "Example News"},
    ],
}

# What TestNewsStatus feeds the fetcher: enough for a status line, not for a note.
TITLE_ONLY_NEWS = {"count": 3, "stories": [{"title": "a"}, {"title": "b"}, {"title": "c"}]}


class _SealedCache(unittest.TestCase):
    """Point spin_up.CACHE_DIR at a tmpdir for every test in the class.

    Any test that calls a caching `_fetch_*` with a stubbed feed inherits this.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patch = mock.patch.object(spin_up, "CACHE_DIR", self.tmp)
        patch.start()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(patch.stop)


class TestNewsStatus(_SealedCache):
    def test_these_tests_cannot_reach_the_import_time_cache(self):
        """A stubbed fetch must land in a tmpdir, never in the real cache.

        `_fetch_news(force=True)` skips the cache read, not the cache write.
        From 2026-09-20 to 2026-10-07 the three tests below wrote their
        title-only fixture into ~/.cache/daily-harness on every suite run.
        Spin-up then served it as "cached", `format_news_md` raised
        KeyError('link'), and the note said local news was unavailable while
        the status line said "News: 3 stories".
        """
        with mock.patch.object(local_news, "fetch", return_value=TITLE_ONLY_NEWS):
            spin_up._fetch_news(force=True)
        self.assertNotEqual(
            spin_up._cache_path("news").parent, IMPORT_TIME_CACHE_DIR,
            "TestNewsStatus is writing its fixture into the cache spin_up really uses")

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


class TestNewsCacheIsValidated(_SealedCache):
    """A cached payload the formatter cannot render is a miss, not a hit."""

    def test_unrenderable_cached_payload_is_not_served(self):
        spin_up._write_cache("news", TITLE_ONLY_NEWS)
        with mock.patch.object(local_news, "fetch", return_value=GOOD_NEWS) as fetch:
            n, status = spin_up._fetch_news(force=False)
        self.assertNotEqual(status, "cached",
                            "served a cached payload that format_news_md cannot render")
        fetch.assert_called_once()
        local_news.format_news_md(n)  # the note's formatter must accept what we return

    def test_good_cached_payload_is_still_served(self):
        """The validation must not turn every cache hit into a network call."""
        spin_up._write_cache("news", GOOD_NEWS)
        with mock.patch.object(local_news, "fetch",
                               side_effect=AssertionError("network was hit")):
            n, status = spin_up._fetch_news(force=False)
        self.assertEqual(status, "cached")
        self.assertEqual(n["stories"][0]["link"], "https://example.com/1")

    def test_dead_feed_does_not_fall_back_to_an_unrenderable_cache(self):
        spin_up._write_cache("news", TITLE_ONLY_NEWS)
        with mock.patch.object(local_news, "fetch", side_effect=TimeoutError("down")):
            n, status = spin_up._fetch_news(force=True)
        self.assertTrue(status.startswith("failed (no cache)"), status)
        self.assertEqual(n, {})


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
