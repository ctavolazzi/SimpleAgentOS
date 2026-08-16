"""
test_arxiv_window.py — Regression tests for the arXiv silent-zero fix.

Background: the daily note printed "No papers" on 2026-07-25, 07-27, 07-31 and
08-02 while spin_up recorded the fetcher as "ok". The fetcher was fine. Its
2-day window simply lands inside arXiv's indexing gap, which trails the wall
clock by 2 to 3 days across a weekend. Measured 2026-08-02 (Sunday): newest
indexed cs.AI submission was 2026-07-30, so [today-2, today] was empty while
[today-5, today] returned 30 papers per pane.

These tests pin both halves of the fix: widen-and-retry, and refusing to call an
empty digest "ok".

Second silent failure, same shape, found 2026-08-06: four of the nine category
feeds timed out. fetch() logged them to stderr and dropped them, so a 5-of-9
digest returned the same shape as a clean one and spin_up reported "ok" again.
TestPartialFetch pins the fix -- failures ride out in the result.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_arxiv_window.py -v
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import arxiv


def _paper(pid, title="Agent reasoning benchmark", published="2026-07-30"):
    return {
        "id": pid,
        "title": title,
        "authors": ["A. Author", "B. Author"],
        "published": published,
        "summary": "An agent language model reasoning benchmark.",
        "category": "cs.AI",
        "url": f"https://arxiv.org/abs/{pid}",
    }


def _feed(papers, days):
    return {
        "categories": ["cs.AI"],
        "fetched_at": "2026-08-02T09:00:00",
        "date_range": (f"2026080{max(1, 3 - days)}", "20260802"),
        "papers": list(papers),
    }


class TestAdaptiveWindow(unittest.TestCase):
    """fetch_dual_pane widens once when both panes come back empty."""

    def test_widens_when_first_pass_is_empty(self):
        """The weekend case: nothing at 2d, papers at 6d."""
        calls = []

        def fake_fetch(categories=None, days=1, max_results=20, timeout=15):
            calls.append(days)
            # Mirrors real arXiv behaviour on a Sunday: the index is 3 days behind.
            papers = [] if days < 4 else [_paper("2607.00001"), _paper("2607.00002")]
            return _feed(papers, days)

        with mock.patch.object(arxiv, "fetch", side_effect=fake_fetch):
            digest = arxiv.fetch_dual_pane(days=2, top_n=5)

        self.assertTrue(digest["window_widened"])
        self.assertEqual(digest["days_requested"], 2)
        self.assertEqual(digest["days"], 6, "should widen 2d by WIDEN_FACTOR to 6d")
        self.assertFalse(digest["suspicious_empty"])
        self.assertEqual(digest["physics"]["total_fetched"], 2)
        self.assertEqual(digest["ai"]["total_fetched"], 2)
        # Two panes at the narrow window, then two panes at the wide one.
        self.assertEqual(calls, [2, 2, 6, 6])

    def test_does_not_widen_when_first_pass_has_papers(self):
        """A normal weekday must not pay for a second round trip."""
        calls = []

        def fake_fetch(categories=None, days=1, max_results=20, timeout=15):
            calls.append(days)
            return _feed([_paper("2607.00003")], days)

        with mock.patch.object(arxiv, "fetch", side_effect=fake_fetch):
            digest = arxiv.fetch_dual_pane(days=2, top_n=5)

        self.assertFalse(digest["window_widened"])
        self.assertEqual(digest["days"], 2)
        self.assertEqual(calls, [2, 2], "must not retry when papers were found")

    def test_widening_is_capped(self):
        """A large request must not widen past MAX_WIDEN_DAYS."""
        with mock.patch.object(arxiv, "fetch",
                               side_effect=lambda *a, **kw: _feed([], kw.get("days", 1))):
            digest = arxiv.fetch_dual_pane(days=5, top_n=5)

        self.assertEqual(digest["days"], arxiv.MAX_WIDEN_DAYS)
        self.assertLessEqual(digest["days"], arxiv.MAX_WIDEN_DAYS)

    def test_no_second_pass_when_widening_would_not_help(self):
        """days already at the cap means there is no wider window to try."""
        calls = []

        def fake_fetch(categories=None, days=1, max_results=20, timeout=15):
            calls.append(days)
            return _feed([], days)

        with mock.patch.object(arxiv, "fetch", side_effect=fake_fetch):
            digest = arxiv.fetch_dual_pane(days=arxiv.MAX_WIDEN_DAYS, top_n=5)

        self.assertFalse(digest["window_widened"])
        self.assertEqual(calls, [arxiv.MAX_WIDEN_DAYS] * 2)


class TestSuspiciousEmpty(unittest.TestCase):
    """An empty result that survived widening is a fault, not a finding."""

    def _empty_digest(self):
        with mock.patch.object(arxiv, "fetch",
                               side_effect=lambda *a, **kw: _feed([], kw.get("days", 1))):
            return arxiv.fetch_dual_pane(days=2, top_n=5)

    def test_flag_is_set_when_even_widened_window_is_empty(self):
        digest = self._empty_digest()
        self.assertTrue(digest["suspicious_empty"])
        self.assertTrue(digest["window_widened"])

    def test_flag_is_clear_when_papers_exist(self):
        with mock.patch.object(arxiv, "fetch",
                               side_effect=lambda *a, **kw: _feed([_paper("2607.1")], kw.get("days", 1))):
            digest = arxiv.fetch_dual_pane(days=2, top_n=5)
        self.assertFalse(digest["suspicious_empty"])

    def test_markdown_footer_explains_a_widened_window(self):
        calls = []

        def fake_fetch(categories=None, days=1, max_results=20, timeout=15):
            calls.append(days)
            return _feed([] if days < 4 else [_paper("2607.9")], days)

        with mock.patch.object(arxiv, "fetch", side_effect=fake_fetch):
            digest = arxiv.fetch_dual_pane(days=2, top_n=5)
        footer = arxiv.format_dual_pane_md(digest).splitlines()[-1]

        self.assertIn("widened from 2d to 6d", footer)

    def test_markdown_footer_flags_a_real_zero(self):
        """The note must not present an outage as a quiet research day."""
        footer = arxiv.format_dual_pane_md(self._empty_digest()).splitlines()[-1]
        self.assertIn("still returned nothing", footer)
        self.assertIn("worth checking", footer)


class TestPartialFetch(unittest.TestCase):
    """A digest built from some of the feeds must not report as a full one.

    Measured 2026-08-06: astro-ph.GA, cs.AI, cs.CL and cs.MA all timed out.
    fetch() printed four warnings to stderr, swallowed the exceptions, and
    returned a dict indistinguishable from a clean run, so spin_up said "ok"
    and the note carried a 5-of-9 digest as though it were the day's reading.
    """

    def _fetch_with_failures(self, failing):
        """Run the real fetch() with urlopen raising for the named categories."""
        class FakeResponse:
            def read(self_inner):
                return (b'<?xml version="1.0"?>'
                        b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>')

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        def fake_urlopen(req, timeout=15):
            for cat in failing:
                if f"cat%3A{cat}" in req.full_url or f"cat:{cat}" in req.full_url:
                    raise TimeoutError("The read operation timed out")
            return FakeResponse()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            return arxiv.fetch(["cs.AI", "cs.LG", "cs.CL"], days=2, max_results=1)

    def test_failed_categories_are_recorded(self):
        result = self._fetch_with_failures(["cs.AI", "cs.CL"])
        failed = [f["category"] for f in result["failed_categories"]]
        self.assertEqual(sorted(failed), ["cs.AI", "cs.CL"])
        self.assertEqual(result["categories_ok"], 1)

    def test_clean_fetch_records_no_failures(self):
        """Positive control. Without this the test above proves nothing."""
        result = self._fetch_with_failures([])
        self.assertEqual(result["failed_categories"], [])
        self.assertEqual(result["categories_ok"], 3)

    def test_error_detail_is_carried_not_just_the_name(self):
        result = self._fetch_with_failures(["cs.AI"])
        self.assertIn("TimeoutError", result["failed_categories"][0]["error"])

    def test_dual_pane_aggregates_failures_across_both_panes(self):
        def fake_fetch(categories=None, days=1, max_results=20, timeout=15):
            feed = _feed([_paper("2608.0001")], days)
            # One failure in each pane, so the merge is actually exercised.
            feed["failed_categories"] = [{"category": categories[0], "error": "TimeoutError: x"}]
            feed["categories_ok"] = len(categories) - 1
            return feed

        with mock.patch.object(arxiv, "fetch", side_effect=fake_fetch):
            digest = arxiv.fetch_dual_pane(days=2, top_n=5)

        self.assertTrue(digest["partial"])
        self.assertEqual(len(digest["failed_categories"]), 2)
        self.assertEqual(digest["categories_total"],
                         len(arxiv.PHYSICS_CATEGORIES) + len(arxiv.AI_CATEGORIES))
        self.assertEqual(digest["categories_ok"], digest["categories_total"] - 2)

    def test_dual_pane_is_not_partial_when_every_feed_answers(self):
        """Positive control for the aggregate layer."""
        with mock.patch.object(arxiv, "fetch",
                               side_effect=lambda *a, **kw: _feed([_paper("2608.2")],
                                                                 kw.get("days", 1))):
            digest = arxiv.fetch_dual_pane(days=2, top_n=5)

        self.assertFalse(digest["partial"])
        self.assertEqual(digest["failed_categories"], [])
        self.assertEqual(digest["categories_ok"], digest["categories_total"])

    def test_widen_retry_does_not_double_count_failures(self):
        """The widen path re-pulls. Only the surviving attempt's failures count."""
        def fake_fetch(categories=None, days=1, max_results=20, timeout=15):
            papers = [] if days < 4 else [_paper("2608.3")]
            feed = _feed(papers, days)
            feed["failed_categories"] = [{"category": categories[0], "error": "TimeoutError: x"}]
            return feed

        with mock.patch.object(arxiv, "fetch", side_effect=fake_fetch):
            digest = arxiv.fetch_dual_pane(days=2, top_n=5)

        self.assertTrue(digest["window_widened"])
        # Four fetch() calls happen (2 narrow + 2 wide) but only the wide pair
        # produced this digest, so two failures, not four.
        self.assertEqual(len(digest["failed_categories"]), 2)


class TestEndpointScheme(unittest.TestCase):
    """export.arxiv.org answers plain http with a 301 and a zero-byte body."""

    def test_query_url_is_https(self):
        seen = {}

        class FakeResponse:
            def read(self_inner):
                return b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        def fake_urlopen(req, timeout=15):
            seen["url"] = req.full_url
            return FakeResponse()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            arxiv.fetch(["cs.AI"], days=2, max_results=1)

        self.assertTrue(
            seen["url"].startswith("https://export.arxiv.org/api/query"),
            f"expected an https endpoint, got {seen['url']}",
        )


if __name__ == "__main__":
    unittest.main()
