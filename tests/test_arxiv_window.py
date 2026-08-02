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
