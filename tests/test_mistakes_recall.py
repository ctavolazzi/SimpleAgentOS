"""
test_mistakes_recall.py - pins for the mistakes-recall system (v0.0.1).

Background: CT decided 2026-08-15 that the mistakes corpus lives in the vault
as work-effort spokes, mirrored from ~/.claude/MISTAKES.md by
`mistakes_recall sync`, with an FTS5 index for `query` and a curated-pattern
PreToolUse hook. These tests pin the load-bearing behaviors, each with a
negative control, because a green check nobody has watched go red is not
evidence (ledger, short version, line 1).

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_mistakes_recall.py -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mistakes_recall as mr  # noqa: E402

MODULE = Path(mr.__file__).resolve()

FIXTURE_LEDGER = """# MISTAKES.md

**Last updated:** 2026-08-14
**Entries:** 3

---

## 1. False positives: fake family

### M-02. Read a tweened number with dump-dom

**What:** Read stat tiles via `--dump-dom` and filed a wrong bug.

**Why:** virtual-time-budget never runs the frame clock.

**Rule:** Drive real Chrome and sample at wall-clock delays.

**Check:** Is the number tweened? Then this method is invalid.

### M-05. A dry-run that was a false pass

**What:** rsync backup reported thousands of files in dry-run, then moved zero.

**Why:** dry-run never calls mkdir, so it never hit the real error.

**Rule:** A dry run is not a rehearsal of the real run.

**Check:** Does the safe mode exercise the same code as the real operation?

### M-07. Wiped frontmatter lists

**What:** update_frontmatter_fields deleted thirteen values.

**Why:** The API replaces list fields, it does not merge.

**Rule:** Read current values, merge, write, read back from disk.

**Check:** Does this write replace or merge?

---

## The short version

Nothing here.
"""


class Sandbox:
    """Temp ledger + vault + cache wired into the module (and env for subprocesses)."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.ledger = root / "MISTAKES.md"
        self.vault = root / "vault"
        self.cache = root / "cache"
        self.ledger.write_text(FIXTURE_LEDGER, encoding="utf-8")
        self.vault.mkdir()
        self._saved = (mr.LEDGER, mr.VAULT, mr.CACHE)
        mr.LEDGER, mr.VAULT, mr.CACHE = self.ledger, self.vault, self.cache

    def env(self):
        env = dict(os.environ)
        env.update(MISTAKES_RECALL_LEDGER=str(self.ledger),
                   MISTAKES_RECALL_VAULT=str(self.vault),
                   MISTAKES_RECALL_CACHE=str(self.cache))
        return env

    def close(self):
        mr.LEDGER, mr.VAULT, mr.CACHE = self._saved
        self.dir.cleanup()


class TestParseLedger(unittest.TestCase):
    def test_fixture_parses_entries_with_fields(self):
        entries = mr.parse_ledger(FIXTURE_LEDGER)
        self.assertEqual([e["id"] for e in entries], ["M-02", "M-05", "M-07"])
        self.assertEqual(entries[0]["family"],
                         "False positives: fake family")
        self.assertIn("wall-clock delays", entries[0]["rule"])
        self.assertIn("rehearsal of the real run", entries[1]["rule"])
        self.assertIn("replace or merge", entries[2]["check"])

    def test_real_ledger_matches_its_own_header_count(self):
        real = Path.home() / ".claude" / "MISTAKES.md"
        if not real.exists():
            self.skipTest("no real ledger on this machine")
        text = real.read_text(encoding="utf-8")
        entries = mr.parse_ledger(text)
        self.assertEqual(len(entries), mr.ledger_header_count(text))
        for e in entries:
            self.assertTrue(e["rule"], f"{e['id']} parsed with empty rule")

    def test_negative_control_garbage_yields_nothing(self):
        self.assertEqual(mr.parse_ledger("# nothing\n\nplain prose\n"), [])


class TestSync(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def corpus(self):
        return self.sb.vault / mr.CORPUS_DIR_REL

    def test_sync_writes_spokes_and_index(self):
        summary = mr.sync(verbose=False)
        self.assertEqual(summary["written"], ["M-02", "M-05", "M-07"])
        spokes = sorted(p.name for p in self.corpus().glob("M-*.md"))
        self.assertEqual(len(spokes), 3)
        idx = (self.corpus() / f"{mr.INDEX_NOTE_BASENAME}.md").read_text()
        for stem in (p.replace(".md", "") for p in spokes):
            self.assertIn(f"[[{stem}", idx)

    def test_resync_is_a_no_op_including_mtimes(self):
        mr.sync(verbose=False)
        before = {p: p.stat().st_mtime_ns
                  for p in self.corpus().rglob("*.md")}
        summary = mr.sync(verbose=False)
        self.assertEqual(summary["written"], [])
        self.assertFalse(summary["index_rewritten"])
        after = {p: p.stat().st_mtime_ns
                 for p in self.corpus().rglob("*.md")}
        self.assertEqual(before, after)

    def test_ledger_edit_rewrites_only_that_spoke(self):
        mr.sync(verbose=False)
        self.sb.ledger.write_text(
            FIXTURE_LEDGER.replace("filed a wrong bug", "filed a WRONG bug"),
            encoding="utf-8")
        summary = mr.sync(verbose=False)
        self.assertEqual(summary["written"], ["M-02"])
        self.assertEqual(summary["unchanged"], ["M-05", "M-07"])


class TestQuery(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        we_dir = self.sb.vault / "_work_efforts_" / "10-19_development"
        we_dir.mkdir(parents=True)
        (we_dir / "10.42_20260801_stripe_cutover.md").write_text(
            "# WE 10.42\nStripe live cutover, webhook secret rotation.")
        journal = self.sb.vault / "Claude Journal"
        journal.mkdir()
        (journal / "2026-08-01.md").write_text(
            "Long day on the stripe webhook and the sandisk backup.")
        mr.build(do_sync=True)

    def test_query_finds_mistake_by_distinctive_term(self):
        hits = mr.query(["frontmatter", "merge"], doc_type="mistake")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["doc_type"], "mistake")
        self.assertIn("M-07", hits[0]["doc_id"])

    def test_query_reaches_work_efforts_and_journal(self):
        types = {h["doc_type"] for h in mr.query(["stripe", "webhook"])}
        self.assertIn("work_effort", types)
        self.assertIn("journal", types)

    def test_negative_control_nonsense_returns_nothing(self):
        self.assertEqual(mr.query(["zzqqxxblorp"]), [])

    def test_stopword_only_query_returns_nothing(self):
        self.assertEqual(mr.query(["the", "and", "of"]), [])


class TestHook(unittest.TestCase):
    """Hook runs as a subprocess: the real delivery path, env-configured."""

    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)

    def run_hook(self, payload):
        proc = subprocess.run(
            [sys.executable, str(MODULE), "hook"],
            input=json.dumps(payload), capture_output=True,
            text=True, env=self.sb.env(), timeout=30)
        return proc

    def test_dump_dom_triggers_m02_with_its_rule(self):
        proc = self.run_hook({
            "session_id": "s1", "tool_name": "Bash",
            "tool_input": {"command": "chrome --headless --dump-dom x.html"}})
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("MISTAKES RECALL", ctx)
        self.assertIn("M-02", ctx)
        self.assertIn("wall-clock delays", ctx)

    def test_same_session_emits_once(self):
        payload = {"session_id": "s2", "tool_name": "Bash",
                   "tool_input": {"command": "x --dump-dom"}}
        first = self.run_hook(payload)
        second = self.run_hook(payload)
        self.assertTrue(first.stdout.strip())
        self.assertEqual(second.stdout.strip(), "")

    def test_new_session_emits_again(self):
        p = {"tool_name": "Bash", "tool_input": {"command": "x --dump-dom"}}
        first = self.run_hook({**p, "session_id": "s3"})
        second = self.run_hook({**p, "session_id": "s4"})
        self.assertTrue(first.stdout.strip())
        self.assertTrue(second.stdout.strip())

    def test_interleaved_sessions_keep_suppression(self):
        # Observed live 2026-08-15: with single-session state, tab B's write
        # dropped tab A's seen-list and A re-emitted an already-shown entry.
        p = {"tool_name": "Bash", "tool_input": {"command": "x --dump-dom"}}
        self.assertTrue(self.run_hook({**p, "session_id": "tabA"}).stdout.strip())
        self.assertTrue(self.run_hook({**p, "session_id": "tabB"}).stdout.strip())
        again = self.run_hook({**p, "session_id": "tabA"})
        self.assertEqual(again.stdout.strip(), "",
                         "tabA suppression was clobbered by tabB's write")

    def test_negative_control_benign_command_is_silent(self):
        proc = self.run_hook({
            "session_id": "s5", "tool_name": "Bash",
            "tool_input": {"command": "ls -la && git status"}})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_negative_control_dry_run_without_rsync_is_silent(self):
        proc = self.run_hook({
            "session_id": "s6", "tool_name": "Bash",
            "tool_input": {"command": "terraform plan --dry-run"}})
        self.assertEqual(proc.stdout.strip(), "")

    def test_rsync_dry_run_fires(self):
        proc = self.run_hook({
            "session_id": "s7", "tool_name": "Bash",
            "tool_input": {"command": "rsync -av --dry-run a/ b/"}})
        self.assertIn("MISTAKES RECALL", proc.stdout)

    def test_garbage_stdin_fails_open(self):
        proc = subprocess.run(
            [sys.executable, str(MODULE), "hook"],
            input="not json at all", capture_output=True,
            text=True, env=self.sb.env(), timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
