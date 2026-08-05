"""
test_repo_status.py — Tests for harness_lib's git status sweep.

Covers the gap that let four commits sit unpushed in the harness's own repo
on 2026-08-05 while a full preflight reported nothing: there was no check for
commits that exist only on this machine.

Builds real git repos in tmpdirs rather than mocking subprocess, because the
thing under test IS the parse of git's porcelain=v2 output. A mock would only
assert that the parser agrees with my memory of the format.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_repo_status.py -v
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import harness_lib


def git(repo, *args):
    """Run git in repo with identity forced, so tests work on any machine."""
    return subprocess.run(
        ["git", "-C", str(repo),
         "-c", "user.email=test@example.com",
         "-c", "user.name=Test",
         "-c", "commit.gpgsign=false",
         *args],
        capture_output=True, text=True, check=True,
    )


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)],
                   capture_output=True, check=True)
    return path


def commit(repo: Path, name="f.txt", text="x"):
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"add {name}")


class TestRepoStatus(unittest.TestCase):

    def test_fresh_repo_has_no_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = make_repo(Path(tmp) / "r")
            s = harness_lib.repo_status(r)
            self.assertFalse(s["has_commits"])
            self.assertIsNone(s["upstream"])
            self.assertIsNone(s["ahead"])
            self.assertIsNone(s["error"])

    def test_repo_with_commit_reports_has_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = make_repo(Path(tmp) / "r")
            commit(r)
            s = harness_lib.repo_status(r)
            self.assertTrue(s["has_commits"])
            self.assertEqual(s["branch"], "main")

    def test_ahead_is_none_without_upstream_not_zero(self):
        """A repo with nowhere to push is not the same as one that's pushed.

        Reporting 0 here would let `unpushed()` call an unreachable repo clean.
        """
        with tempfile.TemporaryDirectory() as tmp:
            r = make_repo(Path(tmp) / "r")
            commit(r)
            s = harness_lib.repo_status(r)
            self.assertIsNone(s["ahead"])
            self.assertIsNone(s["upstream"])

    def test_ahead_counts_unpushed_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bare = tmp / "origin.git"
            subprocess.run(["git", "init", "-q", "--bare", str(bare)],
                           capture_output=True, check=True)
            r = make_repo(tmp / "r")
            commit(r, "a.txt")
            git(r, "remote", "add", "origin", str(bare))
            git(r, "push", "-q", "-u", "origin", "main")

            s = harness_lib.repo_status(r)
            self.assertEqual(s["ahead"], 0, "just pushed, should be level")
            self.assertEqual(s["upstream"], "origin/main")

            commit(r, "b.txt")
            commit(r, "c.txt")
            s = harness_lib.repo_status(r)
            self.assertEqual(s["ahead"], 2)
            self.assertEqual(s["behind"], 0)

    def test_dirty_counts_modified_and_untracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = make_repo(Path(tmp) / "r")
            commit(r, "tracked.txt", "one")
            self.assertEqual(harness_lib.repo_status(r)["dirty"], 0)
            (r / "tracked.txt").write_text("two", encoding="utf-8")
            (r / "untracked.txt").write_text("new", encoding="utf-8")
            self.assertEqual(harness_lib.repo_status(r)["dirty"], 2)

    def test_non_repo_reports_error_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = harness_lib.repo_status(Path(tmp))
            self.assertIsNotNone(s["error"])
            self.assertEqual(s["dirty"], 0)

    def test_missing_path_reports_error(self):
        s = harness_lib.repo_status(Path("/nonexistent/nope"))
        self.assertIsNotNone(s["error"])


class TestUnpushed(unittest.TestCase):

    def test_selects_only_repos_ahead(self):
        st = [
            {"name": "a", "ahead": 3},
            {"name": "b", "ahead": 0},
            {"name": "c", "ahead": None},
            {"name": "d", "ahead": 1},
        ]
        self.assertEqual([s["name"] for s in harness_lib.unpushed(st)], ["a", "d"])

    def test_sorted_worst_first(self):
        st = [{"name": "a", "ahead": 1}, {"name": "b", "ahead": 9},
              {"name": "c", "ahead": 4}]
        self.assertEqual([s["ahead"] for s in harness_lib.unpushed(st)], [9, 4, 1])

    def test_empty_when_all_pushed(self):
        self.assertEqual(harness_lib.unpushed([{"name": "a", "ahead": 0}]), [])


class TestNoUpstream(unittest.TestCase):

    def _s(self, **kw):
        base = {"name": "x", "upstream": None, "error": None,
                "branch": "main", "has_commits": True}
        base.update(kw)
        return base

    def test_flags_repo_with_commits_and_no_upstream(self):
        out = harness_lib.no_upstream([self._s(name="orphan")])
        self.assertEqual([s["name"] for s in out], ["orphan"])

    def test_excludes_empty_scaffolds(self):
        """Half the no-upstream repos in ~/Code are empty `git init`s.

        They hold no work; listing them would bury the ones that do.
        """
        out = harness_lib.no_upstream([self._s(name="scaffold", has_commits=False)])
        self.assertEqual(out, [])

    def test_excludes_repos_that_have_an_upstream(self):
        out = harness_lib.no_upstream([self._s(name="fine", upstream="origin/main")])
        self.assertEqual(out, [])

    def test_excludes_unreadable_repos(self):
        out = harness_lib.no_upstream([self._s(name="broken", error="boom")])
        self.assertEqual(out, [])


class TestScanRepoStatuses(unittest.TestCase):

    def test_one_result_per_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for n in ("a", "b", "c"):
                commit(make_repo(tmp / n))
            out = harness_lib.scan_repo_statuses(workspace=tmp)
            self.assertEqual({s["name"] for s in out}, {"a", "b", "c"})

    def test_empty_workspace_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(harness_lib.scan_repo_statuses(workspace=Path(tmp)), [])

    def test_accepts_explicit_repo_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = make_repo(Path(tmp) / "solo")
            commit(r)
            out = harness_lib.scan_repo_statuses(repos=[r])
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["name"], "solo")

    def test_end_to_end_unpushed_detection(self):
        """The whole point, exercised through the real API on real repos."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bare = tmp / "origin.git"
            subprocess.run(["git", "init", "-q", "--bare", str(bare)],
                           capture_output=True, check=True)
            pushed = make_repo(tmp / "pushed")
            commit(pushed, "a.txt")
            git(pushed, "remote", "add", "origin", str(bare))
            git(pushed, "push", "-q", "-u", "origin", "main")

            behind_remote = make_repo(tmp / "has_unpushed")
            commit(behind_remote, "a.txt")
            git(behind_remote, "remote", "add", "origin", str(bare))
            git(behind_remote, "fetch", "-q", "origin")
            git(behind_remote, "branch", "-q", "--set-upstream-to", "origin/main")
            commit(behind_remote, "local_only.txt")

            st = harness_lib.scan_repo_statuses(workspace=tmp)
            names = [s["name"] for s in harness_lib.unpushed(st)]
            self.assertIn("has_unpushed", names)
            self.assertNotIn("pushed", names)


if __name__ == "__main__":
    unittest.main()
