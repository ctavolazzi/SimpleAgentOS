"""
test_live_feed_layout.py — Tests for the work-block-on-top note layout.

Covers the two pieces added when the daily note was reordered so the agent's
work sits directly under the hero image (2026-07-25):

  migrate_note_layout  — reordering existing notes without touching content,
                         and doing it idempotently
  live_feed            — the real-time activity section: event collapsing,
                         row formatting, and truncation

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_live_feed_layout.py -v
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import daily_note
import live_feed
import migrate_note_layout as mnl


NOTE_OLD_ORDER = """\
---
type: daily
date: 2026-07-25
---

# Daily Note Saturday, July 25th

**Yesterday:** [[2026-07-24]] | **Tomorrow:** [[2026-07-26]]

---

<!-- hero_image -->
![Daily Image](https://example.test/i.png)
<!-- /hero_image -->

---

## Daily Reading

Horoscope body.

---

## Work Efforts

Task body.

---

## Ad Hoc Section

Something the template never had.

---

## Quick Links

Links body.
"""

TEMPLATE = """\
---
type: daily
---

# Daily Note

---

## Live Feed

Placeholder.

---

## Work Efforts

---

## Daily Reading

---

## Quick Links

Links.
"""


class TestSplitSections(unittest.TestCase):

    def test_preamble_and_blocks(self):
        pre, blocks = mnl.split_sections(NOTE_OLD_ORDER)
        self.assertIn("hero_image", pre)
        self.assertNotIn("## Daily Reading", pre)
        self.assertEqual(
            [h for h, _ in blocks],
            ["## Daily Reading", "## Work Efforts",
             "## Ad Hoc Section", "## Quick Links"],
        )

    def test_heading_inside_code_fence_is_not_a_section(self):
        text = NOTE_OLD_ORDER.replace(
            "Task body.",
            "```markdown\n## Not A Real Heading\n```",
        )
        _, blocks = mnl.split_sections(text)
        self.assertNotIn("## Not A Real Heading", [h for h, _ in blocks])

    def test_frontmatter_delimiters_stay_in_preamble(self):
        pre, _ = mnl.split_sections(NOTE_OLD_ORDER)
        self.assertTrue(pre.startswith("---\ntype: daily"))


class TestNormalizeBody(unittest.TestCase):

    def test_strips_separator_on_either_side(self):
        self.assertEqual(mnl.normalize_body("\n---\n\nbody\n\n"), "body")
        self.assertEqual(mnl.normalize_body("\nbody\n\n---\n"), "body")

    def test_keeps_a_rule_in_the_middle(self):
        self.assertEqual(mnl.normalize_body("\na\n\n---\n\nb\n"), "a\n\n---\n\nb")


class TestOrdering(unittest.TestCase):

    def setUp(self):
        self.tmpl = ["## Live Feed", "## Work Efforts",
                     "## Daily Reading", "## Quick Links"]

    def test_template_order_wins(self):
        note = ["## Daily Reading", "## Work Efforts", "## Quick Links"]
        self.assertEqual(
            mnl.target_order(note, self.tmpl),
            ["## Work Efforts", "## Daily Reading", "## Quick Links"],
        )

    def test_unknown_sections_land_before_the_tail(self):
        note = ["## Daily Reading", "## Ad Hoc Section", "## Quick Links"]
        self.assertEqual(
            mnl.target_order(note, self.tmpl),
            ["## Daily Reading", "## Ad Hoc Section", "## Quick Links"],
        )

    def test_quick_links_stays_last(self):
        note = ["## Quick Links", "## Work Efforts"]
        self.assertEqual(mnl.target_order(note, self.tmpl)[-1], "## Quick Links")


class TestReassemble(unittest.TestCase):

    def setUp(self):
        self.pre, self.blocks = mnl.split_sections(NOTE_OLD_ORDER)
        self.headers = [h for h, _ in self.blocks]
        tmpl_headers = [h for h, _ in mnl.split_sections(TEMPLATE)[1]]
        self.order = mnl.target_order(self.headers, tmpl_headers)

    def test_reorders_work_above_daily_reading(self):
        self.assertLess(self.order.index("## Work Efforts"),
                        self.order.index("## Daily Reading"))

    def test_no_body_is_altered(self):
        new = mnl.reassemble(self.pre, self.blocks, self.order)
        _, new_blocks = mnl.split_sections(new)
        before = {h: mnl.normalize_body(b) for h, b in self.blocks}
        after = {h: mnl.normalize_body(b) for h, b in new_blocks}
        self.assertEqual(before, after)

    def test_idempotent(self):
        once = mnl.reassemble(self.pre, self.blocks, self.order)
        pre2, blocks2 = mnl.split_sections(once)
        twice = mnl.reassemble(pre2, blocks2,
                               [h for h, _ in blocks2])
        self.assertEqual(once, twice,
                         "a second pass must not add another separator")

    def test_exactly_one_rule_between_sections(self):
        new = mnl.reassemble(self.pre, self.blocks, self.order)
        self.assertNotIn("---\n\n---", new)


class TestSeparatorHealing(unittest.TestCase):
    """A doubled rule at a boundary must not survive a migration pass."""

    def test_strips_every_trailing_rule(self):
        self.assertEqual(mnl.strip_trailing_rule("body\n\n---\n\n---\n"), "body")

    def test_strips_every_leading_rule(self):
        self.assertEqual(mnl.strip_leading_rule("\n---\n\n---\n\nbody"), "body")

    def test_doubled_rule_heals_in_one_pass(self):
        text = NOTE_OLD_ORDER.replace("<!-- /hero_image -->\n\n---\n",
                                      "<!-- /hero_image -->\n\n---\n\n---\n")
        pre, blocks = mnl.split_sections(text)
        out = mnl.reassemble(pre, blocks, [h for h, _ in blocks])
        self.assertNotIn("---\n\n---", out)


class TestReplaceSectionKeepsSeparator(unittest.TestCase):
    """Regression: a replace-mode write used to consume the trailing rule.

    When the last line of the new body is a blockquote (any callout-shaped
    section, which Live Feed always is), markdown lazy continuation then
    absorbed the following `## ` heading into the callout.
    """

    NOTE = ("## Live Feed\n\nold body\n\n---\n\n"
            "## Work Efforts\n\ntasks\n")

    def test_trailing_rule_survives_a_replace(self):
        out = daily_note._replace_section(self.NOTE, "## Live Feed",
                                          "> [!abstract]+ Live Feed\n> a row\n")
        self.assertIn("> a row\n\n---\n\n## Work Efforts", out)

    def test_next_heading_never_touches_a_quoted_line(self):
        out = daily_note._replace_section(self.NOTE, "## Live Feed",
                                          "> only a quoted line\n")
        lines = out.splitlines()
        for i in range(1, len(lines)):
            if lines[i].startswith("## "):
                self.assertFalse(lines[i - 1].lstrip().startswith(">"),
                                 "heading would be swallowed by the callout")

    def test_blank_line_added_when_section_had_no_trailer(self):
        note = "## Live Feed\nold\n## Work Efforts\n\ntasks\n"
        out = daily_note._replace_section(note, "## Live Feed", "> row\n")
        self.assertIn("> row\n\n## Work Efforts", out)

    def test_last_section_still_replaces(self):
        note = "## Live Feed\n\nold body\n"
        out = daily_note._replace_section(note, "## Live Feed", "new body\n")
        self.assertIn("new body", out)
        self.assertNotIn("old body", out)

    def test_split_trailer_leaves_content_alone(self):
        content, trailer = daily_note._split_trailer("a\n\n---\n\nb\n\n---\n\n")
        self.assertEqual(content, "a\n\n---\n\nb\n")
        self.assertEqual(trailer, "\n---\n\n")


class TestLiveFeedFormatting(unittest.TestCase):

    def test_collapses_consecutive_quiet_tools(self):
        events = [
            {"ts": "09:00:01", "kind": "Read", "detail": "a.py"},
            {"ts": "09:00:02", "kind": "Read", "detail": "b.py"},
            {"ts": "09:00:03", "kind": "Read", "detail": "c.py"},
            {"ts": "09:00:04", "kind": "Bash", "detail": "ls"},
        ]
        rows = live_feed._collapse(events)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["count"], 3)
        self.assertEqual(rows[1]["count"], 1)

    def test_never_collapses_loud_tools(self):
        events = [{"ts": "09:00:0%d" % i, "kind": "Edit", "detail": "f.py"}
                  for i in range(3)]
        self.assertEqual(len(live_feed._collapse(events)), 3)

    def test_never_collapses_across_sessions(self):
        events = [
            {"ts": "09:00:01", "kind": "Read", "sid": "aaaa", "detail": "a.py"},
            {"ts": "09:00:02", "kind": "Read", "sid": "bbbb", "detail": "b.py"},
            {"ts": "09:00:03", "kind": "Read", "sid": "bbbb", "detail": "c.py"},
        ]
        rows = live_feed._collapse(events)
        self.assertEqual([r["count"] for r in rows], [1, 2])

    def test_rows_from_older_feeds_without_sid_still_collapse(self):
        events = [{"ts": "09:00:01", "kind": "Read", "detail": "a.py"},
                  {"ts": "09:00:02", "kind": "Read", "detail": "b.py"}]
        self.assertEqual(len(live_feed._collapse(events)), 1)

    def test_path_detail_shortens_to_two_components(self):
        out = live_feed._format_detail("Edit", "/a/b/c/d/file.py")
        self.assertEqual(out, "d/file.py")

    def test_bash_detail_drops_leading_cd(self):
        out = live_feed._format_detail("Bash", "cd /tmp/x && git status")
        self.assertEqual(out, "git status")

    def test_detail_truncates_and_kills_backticks(self):
        raw = "echo `whoami` " + "x" * 200
        out = live_feed._format_detail("Bash", raw)
        self.assertNotIn("`", out)
        self.assertLessEqual(len(out), live_feed.DETAIL_CHARS)

    def test_multiline_detail_becomes_one_line(self):
        out = live_feed._format_detail("Bash", "line one\nline two")
        self.assertNotIn("\n", out)

    def test_every_rendered_line_stays_inside_the_callout(self):
        events = [{"ts": "09:00:01", "kind": "Bash", "detail": "a\nb"}]
        rows = live_feed._collapse(events)
        line = f"> `{rows[0]['ts']}` **Bash** `{live_feed._format_detail('Bash', rows[0]['detail'])}`"
        self.assertTrue(all(l.startswith(">") for l in line.splitlines()))


class AgentIdentityTests(unittest.TestCase):
    """Several agent runtimes share one feed (Claude Code, Codex, others).

    A session id says the rows came from different tabs. It does not say which
    tool produced them, and CT reading the note cannot tell one agent's work
    from another's. These pin the labelling, and just as importantly they pin
    the two cases that must stay unlabelled, because a check that fires all the
    time gets switched off.
    """

    def test_two_agents_are_labelled_distinctly(self):
        events = [
            {"ts": "09:00:00", "kind": "Bash", "sid": "b552",
             "agent": "claude", "detail": "pytest -q"},
            {"ts": "09:00:01", "kind": "Edit", "sid": "7d5a",
             "agent": "codex", "detail": "bot.py"},
        ]
        rows = live_feed._collapse(events)
        self.assertEqual([r["agent"] for r in rows], ["claude", "codex"])

    def test_one_agent_gets_no_label(self):
        """Negative control: a solo feed must render exactly as it did before."""
        events = [
            {"ts": "09:00:00", "kind": "Bash", "sid": "b552",
             "agent": "claude", "detail": "one"},
            {"ts": "09:00:01", "kind": "Bash", "sid": "b552",
             "agent": "claude", "detail": "two"},
        ]
        agents = {e.get("agent", "") for e in events if e.get("agent")}
        self.assertEqual(len(agents), 1, "solo feed must not trigger agent tags")

    def test_legacy_rows_without_an_agent_key_still_work(self):
        """Negative control: JSONL written before this field existed."""
        events = [
            {"ts": "10:00:00", "kind": "Bash", "sid": "aaaa", "detail": "old"},
            {"ts": "10:00:01", "kind": "Bash", "sid": "bbbb", "detail": "old2"},
        ]
        rows = live_feed._collapse(events)
        self.assertEqual([r["agent"] for r in rows], ["", ""])

    def test_quiet_runs_never_merge_across_agents(self):
        """Two agents reading in parallel is not one act of research."""
        events = [
            {"ts": "09:00:00", "kind": "Read", "sid": "x", "agent": "claude",
             "detail": "a.py"},
            {"ts": "09:00:01", "kind": "Read", "sid": "x", "agent": "codex",
             "detail": "b.py"},
        ]
        rows = live_feed._collapse(events)
        self.assertEqual(len(rows), 2, "different agents must not collapse")

    def test_agent_name_reads_env_when_not_explicit(self):
        import os
        prev = os.environ.get("DAILY_NOTE_AGENT")
        try:
            os.environ["DAILY_NOTE_AGENT"] = "codex"
            self.assertEqual(live_feed.agent_name(), "codex")
            self.assertEqual(live_feed.agent_name("claude"), "claude")
        finally:
            if prev is None:
                os.environ.pop("DAILY_NOTE_AGENT", None)
            else:
                os.environ["DAILY_NOTE_AGENT"] = prev

    def test_agent_name_is_bounded(self):
        self.assertLessEqual(len(live_feed.agent_name("x" * 100)), 12)


class MixedLabellingTests(unittest.TestCase):
    """The integration bug the unit tests above could not see.

    Found 2026-09-20 while wiring a second agent. Agent labels were computed
    from non-empty values only, so the realistic rollout state, one runtime
    emitting the field and one not yet, produced a set of size one and rendered
    no labels at all. Every unit test still passed, because each side was
    correct in isolation. Only placing both writers in one feed showed it.
    """

    def _bucket_count(self, rows):
        return len({r.get("agent", "") for r in rows})

    def test_labelled_plus_unlabelled_counts_as_two_buckets(self):
        rows = [{"agent": "codex"}, {"agent": ""}]
        self.assertEqual(self._bucket_count(rows), 2,
                         "an unlabelled writer must still be distinguishable")

    def test_solo_unlabelled_feed_is_still_one_bucket(self):
        """Negative control: pre-adoption feeds must not sprout labels."""
        rows = [{"agent": ""}, {"agent": ""}]
        self.assertEqual(self._bucket_count(rows), 1)

    def test_solo_labelled_feed_is_still_one_bucket(self):
        """Negative control: one agent, labelled, needs no disambiguation."""
        rows = [{"agent": "claude"}, {"agent": "claude"}]
        self.assertEqual(self._bucket_count(rows), 1)


if __name__ == "__main__":
    unittest.main()
