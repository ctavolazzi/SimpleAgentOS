"""
test_harness_lib.py — Tests for harness_lib.py shared helpers.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_harness_lib.py -v
"""

import os
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import harness_lib


class TestDiscoverRepos(unittest.TestCase):

    def test_returns_list(self):
        result = harness_lib.discover_repos()
        self.assertIsInstance(result, list)

    def test_all_results_are_paths(self):
        result = harness_lib.discover_repos()
        for r in result:
            self.assertIsInstance(r, Path)

    def test_all_results_have_git_dir(self):
        result = harness_lib.discover_repos()
        for r in result:
            self.assertTrue((r / ".git").exists(), f"{r} has no .git dir")

    def test_fake_workspace(self):
        """Only dirs with .git are returned; non-repo dirs ignored."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            # Two repos
            for name in ("alpha", "beta"):
                (ws / name / ".git").mkdir(parents=True)
            # One non-repo
            (ws / "not_a_repo").mkdir()

            result = harness_lib.discover_repos(ws)
            names = {r.name for r in result}
            self.assertIn("alpha", names)
            self.assertIn("beta", names)
            self.assertNotIn("not_a_repo", names)

    def test_active_subdir_scanned(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "active" / "sub_repo" / ".git").mkdir(parents=True)
            result = harness_lib.discover_repos(ws)
            names = {r.name for r in result}
            self.assertIn("sub_repo", names)

    def test_active_itself_not_included(self):
        """The active/ container dir is skipped, only its children included."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "active" / ".git").mkdir(parents=True)
            result = harness_lib.discover_repos(ws)
            names = {r.name for r in result}
            # active/ itself should NOT appear — it's the scan container
            self.assertNotIn("active", names)

    def test_container_with_git_is_still_descended(self):
        """~/Code/active is an accidental `git init` sitting above ~40 repos.

        Excluding it must not mean pruning it: a stop-at-first-repo walk would
        drop every project underneath.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "active" / ".git").mkdir(parents=True)
            (ws / "active" / "real_project" / ".git").mkdir(parents=True)
            names = {r.name for r in harness_lib.discover_repos(ws)}
            self.assertNotIn("active", names)
            self.assertIn("real_project", names)

    def test_nested_repo_below_top_level_is_found(self):
        """The regression this suite missed for months.

        ~/Code/_experiments/SimpleAgentOS is the harness's own repo, two levels
        down under a non-repo parent. The old two-level scan (root + ~/Code/* +
        active/*) could not see it, so a day spent working on the harness
        tallied as zero commits in the daily note.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "_experiments" / "SimpleAgentOS" / ".git").mkdir(parents=True)
            names = {r.name for r in harness_lib.discover_repos(ws)}
            self.assertIn("SimpleAgentOS", names)

    def test_repo_nested_inside_another_repo_is_found(self):
        """Independent project checked out inside a parent repo still counts."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "parent" / ".git").mkdir(parents=True)
            (ws / "parent" / "child_project" / ".git").mkdir(parents=True)
            names = {r.name for r in harness_lib.discover_repos(ws)}
            self.assertIn("parent", names)
            self.assertIn("child_project", names)

    def test_vendored_subtrees_are_pruned(self):
        """Descending past repos must not drag in third-party clones.

        Without the skip list a depth-limited walk of the real ~/Code surfaces
        ~100 vendored .git dirs (LaTeX template packs, MCP sample repos), and
        every consumer that reports "dirty repos" drowns in them.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "proj" / ".git").mkdir(parents=True)
            for vendored in ("_external", "node_modules", "archived",
                             "templates", "lib", "mcp-jungle-gym"):
                (ws / "proj" / vendored / "clone" / ".git").mkdir(parents=True)
            (ws / "proj" / "_temp_scratch" / "clone" / ".git").mkdir(parents=True)
            (ws / "proj" / "_worktree_pr1" / ".git").mkdir(parents=True)
            names = {r.name for r in harness_lib.discover_repos(ws)}
            self.assertEqual(names, {"proj"})

    def test_dotdirs_are_pruned(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / ".ai_tmp" / "scratch_clone" / ".git").mkdir(parents=True)
            names = {r.name for r in harness_lib.discover_repos(ws)}
            self.assertNotIn("scratch_clone", names)

    def test_depth_limit_respected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            deep = ws / "a" / "b" / "c" / "d" / "e" / "too_deep"
            (deep / ".git").mkdir(parents=True)
            names = {r.name for r in harness_lib.discover_repos(ws)}
            self.assertNotIn("too_deep", names)

    def test_real_workspace_sees_the_harness_itself(self):
        """End-to-end against the actual ~/Code, not a fixture.

        Skips rather than fails when run outside this workspace.
        """
        ws = harness_lib.WORKSPACE
        me = ws / "_experiments" / "SimpleAgentOS"
        if not (me / ".git").exists():
            self.skipTest("not running inside the real ~/Code workspace")
        found = {r.resolve() for r in harness_lib.discover_repos(ws)}
        self.assertIn(me.resolve(), found)


class TestFindReposDelegation(unittest.TestCase):
    """daily_note_update.find_repos must stay in agreement with harness_lib."""

    def test_find_repos_matches_discover_repos(self):
        import daily_note_update
        ws = harness_lib.WORKSPACE
        if not ws.is_dir():
            self.skipTest("no ~/Code workspace")
        a = {Path(p).resolve() for p in daily_note_update.find_repos([str(ws)])}
        b = {p.resolve() for p in harness_lib.discover_repos(ws)}
        self.assertEqual(a, b)

    def test_find_repos_returns_strings(self):
        """Callers pass these straight to `git -C`; Path would be a silent API break."""
        import daily_note_update
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "r" / ".git").mkdir(parents=True)
            out = daily_note_update.find_repos([tmp])
            self.assertTrue(out)
            for p in out:
                self.assertIsInstance(p, str)


class TestClassifyMtime(unittest.TestCase):

    def test_fresh_file_is_wip(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            result = harness_lib.classify_mtime(Path(f.name), threshold_hours=24)
            self.assertEqual(result, "wip")

    def test_old_file_is_stale(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            # Backdate mtime by 48 hours
            old_time = time.time() - (48 * 3600)
            os.utime(f.name, (old_time, old_time))
            result = harness_lib.classify_mtime(Path(f.name), threshold_hours=24)
            self.assertEqual(result, "stale")

    def test_missing_file_is_stale(self):
        result = harness_lib.classify_mtime(Path("/nonexistent/file.txt"))
        self.assertEqual(result, "stale")

    def test_custom_threshold(self):
        import tempfile
        with tempfile.NamedTemporaryFile() as f:
            # Backdate by 2 hours
            old_time = time.time() - (2 * 3600)
            os.utime(f.name, (old_time, old_time))
            # Threshold 1h → stale
            self.assertEqual(harness_lib.classify_mtime(Path(f.name), 1), "stale")
            # Threshold 3h → wip
            self.assertEqual(harness_lib.classify_mtime(Path(f.name), 3), "wip")


class TestIsoNow(unittest.TestCase):

    def test_returns_string(self):
        result = harness_lib.iso_now()
        self.assertIsInstance(result, str)

    def test_format(self):
        from datetime import datetime
        result = harness_lib.iso_now()
        # Must be parseable as ISO datetime
        datetime.fromisoformat(result)

    def test_second_precision(self):
        result = harness_lib.iso_now()
        # Should not have fractional seconds
        self.assertNotIn(".", result)


if __name__ == "__main__":
    unittest.main()
