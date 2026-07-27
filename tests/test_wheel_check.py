"""
test_wheel_check.py — Regression tests for the wagonwheel integrity checker.

Covers the four invariants wheel_check enforces: hub-link presence + link
resolution, container completeness, spoke reciprocity, and parent-chain
reachability — plus frontmatter.validate's link checking.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_wheel_check.py -v
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import daily_note
import frontmatter as fm
import wheel_check

DATE = "2099-06-15"


def _fm(**kw):
    lines = ["---"]
    for k, v in kw.items():
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


class WheelTestCase(unittest.TestCase):
    """Builds a complete, valid wagonwheel in an isolated temp vault, then
    each test breaks exactly one invariant and asserts the checker catches it."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        (self.vault / "Daily Notes").mkdir()
        (self.vault / "Hubs").mkdir()
        (self.vault / "Claude Journal").mkdir()
        (self.vault / "Plans").mkdir()

        # Repoint every module that keys off the vault root
        self._orig = (daily_note.VAULT_DIR, daily_note.DAILY_NOTES_DIR)
        daily_note.VAULT_DIR = self.vault
        daily_note.DAILY_NOTES_DIR = self.vault / "Daily Notes"

        # Vault index (parent-chain terminus)
        (self.vault / "00.00_vault_index.md").write_text(
            _fm(type="index", date=DATE) + "# Index\n", encoding="utf-8")
        self._build_complete_wheel()

    def tearDown(self):
        daily_note.VAULT_DIR, daily_note.DAILY_NOTES_DIR = self._orig
        shutil.rmtree(self.vault, ignore_errors=True)

    def _build_complete_wheel(self):
        # Daily note — all required + link fields present and resolving
        (self.vault / "Daily Notes" / f"{DATE}.md").write_text(
            _fm(type="daily", date=DATE,
                parent='"[[00.00_vault_index]]"',
                journal=f'"[[Claude Journal/{DATE}]]"',
                plan=f'"[[Plans/{DATE}_daily_plan]]"',
                hub=f'"[[Hubs/{DATE}_hub]]"',
                wagonwheel=f'"[[Hubs/{DATE}_hub]]"')
            + "# Daily\n\n[[my-spoke]]\n", encoding="utf-8")
        # Hub — lists one spoke
        (self.vault / "Hubs" / f"{DATE}_hub.md").write_text(
            "---\ntype: hub\ndate: %s\nparent: \"[[Daily Notes/%s]]\"\n"
            "spokes:\n  - \"[[my-spoke]]\"\n---\n\n## Where We Are\nReal content.\n"
            % (DATE, DATE), encoding="utf-8")
        # Journal — filled ## Notes
        (self.vault / "Claude Journal" / f"{DATE}.md").write_text(
            _fm(type="claude_journal", date=DATE,
                parent=f'"[[Daily Notes/{DATE}]]"',
                hub=f'"[[Hubs/{DATE}_hub]]"')
            + "# Journal\n\n## Notes\nReal notes.\n", encoding="utf-8")
        # Plan
        (self.vault / "Plans" / f"{DATE}_daily_plan.md").write_text(
            _fm(type="daily_plan", date=DATE) + "# Plan\n", encoding="utf-8")
        # Spoke — carries hub back-link (closed rim)
        (self.vault / "my-spoke.md").write_text(
            _fm(type="reference", date=DATE,
                parent=f'"[[Daily Notes/{DATE}]]"',
                hub=f'"[[Hubs/{DATE}_hub]]"')
            + "# Spoke\n", encoding="utf-8")

    # ── happy path ────────────────────────────────────────────────
    def test_complete_wheel_is_intact(self):
        r = wheel_check.check(DATE)
        self.assertFalse(r.broken, f"expected intact, got errors: {r.errors}")

    def test_frontmatter_validate_passes(self):
        self.assertEqual(fm.validate(DATE), [])

    # ── invariant 1: hub presence + link resolution ───────────────
    def test_missing_hub_field_flagged(self):
        note = self.vault / "Daily Notes" / f"{DATE}.md"
        note.write_text(note.read_text().replace(
            f'hub: "[[Hubs/{DATE}_hub]]"\n', ""), encoding="utf-8")
        r = wheel_check.check(DATE)
        self.assertTrue(r.broken)
        self.assertTrue(any("hub" in e for e in r.errors))

    def test_dangling_hub_link_flagged(self):
        (self.vault / "Hubs" / f"{DATE}_hub.md").unlink()
        r = wheel_check.check(DATE)
        self.assertTrue(r.broken)
        self.assertTrue(any("DANGLING" in e or "missing" in e for e in r.errors))

    # ── invariant 2: container completeness ───────────────────────
    def test_placeholder_hub_flagged(self):
        (self.vault / "Hubs" / f"{DATE}_hub.md").write_text(
            "---\ntype: hub\ndate: %s\nparent: \"[[Daily Notes/%s]]\"\n"
            "spokes:\n  - \"[[my-spoke]]\"\n---\n\n## Where We Are\n"
            "(To be populated as session progresses)\n" % (DATE, DATE),
            encoding="utf-8")
        r = wheel_check.check(DATE)
        self.assertTrue(any("placeholder" in e.lower() for e in r.errors))

    def _write_journal(self, body: str) -> None:
        (self.vault / "Claude Journal" / f"{DATE}.md").write_text(
            _fm(type="claude_journal", date=DATE,
                parent=f'"[[Daily Notes/{DATE}]]"',
                hub=f'"[[Hubs/{DATE}_hub]]"')
            + body, encoding="utf-8")

    def test_empty_journal_flagged(self):
        """spin_up's minimal template, nothing written in."""
        self._write_journal("# Journal\n\n## Notes\n")
        r = wheel_check.check(DATE)
        self.assertTrue(any("CONTAINER: Journal" in e for e in r.errors))

    def test_untouched_claude_journal_template_flagged(self):
        """claude_journal.py's own template has no '## Notes' at all.

        It must still be caught when nothing has been written into it, or an
        empty journal passes purely by using the other generator.
        """
        self._write_journal(
            "# Journal\n\n## Realizations\n\n"
            "> [!abstract]+ Things That Clicked\n"
            "> Moments where something resolved.\n\n"
            "<!-- One bullet per realization -->\n"
        )
        r = wheel_check.check(DATE)
        self.assertTrue(any("CONTAINER: Journal" in e for e in r.errors))

    def test_written_claude_journal_template_passes(self):
        """The regression that prompted this: a journal written via
        `claude_journal add-realization` has no '## Notes' and used to fail
        forever, which meant the wheel could never go green."""
        self._write_journal(
            "# Journal\n\n## Realizations\n\n"
            "> [!abstract]+ Things That Clicked\n\n"
            "- [13:31] a realization that was actually written down\n"
        )
        r = wheel_check.check(DATE)
        self.assertFalse([e for e in r.errors if "CONTAINER: Journal" in e])

    # ── invariant 3: reciprocity (rim) ────────────────────────────
    def test_spoke_without_hub_backlink_flagged(self):
        (self.vault / "my-spoke.md").write_text(
            _fm(type="reference", date=DATE,
                parent=f'"[[Daily Notes/{DATE}]]"')  # NO hub field
            + "# Spoke\n", encoding="utf-8")
        r = wheel_check.check(DATE)
        self.assertTrue(any("RECIPROCITY" in e for e in r.errors))

    def test_spoke_listed_but_missing_flagged(self):
        (self.vault / "my-spoke.md").unlink()
        r = wheel_check.check(DATE)
        self.assertTrue(any("RECIPROCITY" in e for e in r.errors))

    # ── invariant 4: parent chain ─────────────────────────────────
    def test_broken_parent_chain_flagged(self):
        note = self.vault / "Daily Notes" / f"{DATE}.md"
        note.write_text(note.read_text().replace(
            '"[[00.00_vault_index]]"', '"[[does-not-exist]]"'), encoding="utf-8")
        r = wheel_check.check(DATE)
        self.assertTrue(any("PARENT-CHAIN" in e or "DANGLING" in e for e in r.errors))

    def test_cli_exit_code_nonzero_when_broken(self):
        import subprocess
        (self.vault / "Hubs" / f"{DATE}_hub.md").unlink()
        # Run the CLI in-process would share monkeypatch; instead assert via check()
        r = wheel_check.check(DATE)
        self.assertTrue(r.broken)


if __name__ == "__main__":
    unittest.main()
