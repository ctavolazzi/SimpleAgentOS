"""
test_session_audit_suite_check.py — The audit's test-suite check must be able to finish.

Background: `session_audit.py` runs the harness suite through `sh()`, which had
one timeout for every command: 30 seconds. The suite takes about 90. So the
check was killed on every run, found no "passed" or "failed" line in the empty
output, and reported WARN "no summary". It could not report a pass and it could
not report a failure. It also stopped the suite partway through each time,
after the tests that (until 2026-10-07) wrote a fixture into the real news
cache had already run.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_session_audit_suite_check.py -v
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import session_audit

# The suite ran in 89 to 214 seconds on 2026-10-07. Anything under this cannot hold it.
LONGEST_OBSERVED_SUITE_SECONDS = 214


def _check_with(sh_result):
    calls = []

    def fake_sh(args, cwd=None, timeout=30):
        calls.append({"args": list(args), "timeout": timeout})
        return sh_result

    with mock.patch.object(session_audit, "sh", side_effect=fake_sh):
        status, details = session_audit._check_tests()
    return status, details, calls


class TestSuiteCheckCanFinish(unittest.TestCase):
    def test_the_suite_gets_more_time_than_it_needs(self):
        _, _, calls = _check_with((0, "490 passed in 89.63s", ""))
        self.assertEqual(len(calls), 1)
        self.assertGreater(calls[0]["timeout"], LONGEST_OBSERVED_SUITE_SECONDS)

    def test_a_pass_is_reported_with_its_summary(self):
        status, details, _ = _check_with((0, "....\n490 passed in 89.63s", ""))
        self.assertEqual(status, session_audit.OK)
        self.assertIn("490 passed", details[0])

    def test_a_failure_names_the_failing_test(self):
        out = ("FAILED tests/test_smoke.py::test_model_file_exists - AssertionError\n"
               "1 failed, 388 passed in 60.0s")
        status, details, _ = _check_with((1, out, ""))
        self.assertEqual(status, session_audit.WARN)
        self.assertIn("1 failed", details[0])
        self.assertTrue(any("test_smoke" in d for d in details))


class TestTimeoutIsNotASummary(unittest.TestCase):
    def test_sh_reports_a_timeout_as_a_timeout(self):
        with mock.patch.object(session_audit.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["pytest"], 5)):
            rc, out, err = session_audit.sh(["pytest"], timeout=5)
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertIn("timed out after 5s", err)

    def test_a_suite_that_did_not_finish_says_so(self):
        status, details, _ = _check_with((1, "", "timed out after 900s"))
        self.assertEqual(status, session_audit.WARN)
        self.assertNotIn("no summary", " ".join(details))
        self.assertIn("did not finish", details[0])

    def test_other_commands_keep_the_short_default(self):
        with mock.patch.object(session_audit.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            session_audit.sh(["git", "status"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30)


if __name__ == "__main__":
    unittest.main()
