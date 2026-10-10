"""
test_feed_hook_contract.py — the shell hook and its Python twin must agree.

Why this file exists
--------------------
The Live Feed has two independent implementations of one row schema:

  ~/.claude/hooks/live-feed.sh   bash + jq, runs on every tool call
  live_feed.record_payload()     Python, called the reference implementation
                                 in its own docstring

They are separate on purpose. The hook is bash because importing Python costs
about 650ms and no tool call should wait on that. The cost of that choice is
that nothing forced the two to stay in step.

On 2026-09-20 an `agent` field was added to both, by hand, in two languages,
with no check that the results matched. That is exactly the shape of defect a
unit test cannot see: each side was internally consistent and individually
correct, and a divergence would only have surfaced as rows that quietly failed
to group, days later, with no error anywhere.

The same session also found that the hook honoured $VAULT_DIR while every
Python reader hardcoded the path, so setting that variable sent the hook's
writes to a tree nothing read. The feed would have gone silent with nothing
reporting a failure. `test_vault_dir_override_is_honoured_by_both` pins that.

These tests run the real hook as a subprocess. They are skipped rather than
failed when the hook or jq is absent, because this repository is published and
a fresh clone has neither.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_feed_hook_contract.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import live_feed


HOOK = Path(
    os.environ.get("LIVE_FEED_HOOK") or (Path.home() / ".claude" / "hooks" / "live-feed.sh")
)

SKIP_REASON = None
if not HOOK.is_file():
    SKIP_REASON = f"hook not installed at {HOOK}"
elif shutil.which("jq") is None:
    SKIP_REASON = "jq not on PATH; the hook is bash + jq by design"


def run_hook(payload, vault, agent=None):
    """Drive the real hook in record mode and return the row it appended.

    Returns (row_or_None, completed_process) so a caller can assert on the
    exit status as well as the output.
    """
    env = dict(os.environ)
    env["VAULT_DIR"] = str(vault)
    env["LIVE_FEED_PY"] = str(ROOT / "live_feed.py")
    if agent is None:
        env.pop("DAILY_NOTE_AGENT", None)
    else:
        env["DAILY_NOTE_AGENT"] = agent

    body = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run(
        ["bash", str(HOOK), "record"],
        input=body, capture_output=True, text=True, env=env, timeout=30,
    )

    rows = []
    feed_root = Path(vault) / "System" / "40-49_telemetry" / "live_feed"
    for f in sorted(feed_root.rglob("*.jsonl")) if feed_root.exists() else []:
        for line in f.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return (rows[-1] if rows else None), proc


@unittest.skipIf(SKIP_REASON, SKIP_REASON or "")
class HookPythonContractTests(unittest.TestCase):
    """The two implementations must produce the same row for the same payload."""

    PAYLOADS = [
        {"tool_name": "Bash", "session_id": "b552abcd",
         "tool_input": {"command": "git status"}},
        {"tool_name": "Edit", "session_id": "7d5a0000",
         "tool_input": {"file_path": "/tmp/x/bot.py"}},
        {"tool_name": "Grep", "session_id": "aaaa1111",
         "tool_input": {"pattern": "def test_"}},
        {"tool_name": "WebFetch", "session_id": "bbbb2222",
         "tool_input": {"url": "https://example.com"}},
        {"tool_name": "Task", "session_id": "cccc3333",
         "tool_input": {"description": "go look at a thing"}},
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="feedcontract-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # keep record_payload's own append away from the real vault
        self._real_root = live_feed.FEED_ROOT
        live_feed.FEED_ROOT = Path(self.tmp) / "py" / "live_feed"
        self.addCleanup(setattr, live_feed, "FEED_ROOT", self._real_root)

    def test_same_keys_for_every_payload_shape(self):
        for p in self.PAYLOADS:
            with self.subTest(tool=p["tool_name"]):
                shell_row, _ = run_hook(p, Path(self.tmp) / p["tool_name"])
                py_row = live_feed.record_payload(p)
                self.assertIsNotNone(shell_row, "hook wrote no row")
                self.assertEqual(
                    set(shell_row) - {"ts"}, set(py_row) - {"ts"},
                    "shell and Python rows disagree on which keys exist")

    def test_same_values_for_every_payload_shape(self):
        """ts is excluded: the two run a moment apart and will differ."""
        for p in self.PAYLOADS:
            with self.subTest(tool=p["tool_name"]):
                shell_row, _ = run_hook(p, Path(self.tmp) / ("v" + p["tool_name"]))
                py_row = live_feed.record_payload(p)
                for key in ("kind", "sid", "detail"):
                    self.assertEqual(
                        shell_row[key], py_row[key],
                        f"{key} differs between shell hook and record_payload")

    def test_session_id_truncated_identically(self):
        p = {"tool_name": "Bash", "session_id": "0123456789abcdef",
             "tool_input": {"command": "true"}}
        shell_row, _ = run_hook(p, Path(self.tmp) / "sid")
        py_row = live_feed.record_payload(p)
        self.assertEqual(shell_row["sid"], "0123")
        self.assertEqual(shell_row["sid"], py_row["sid"])

    def test_agent_defaults_to_claude_in_the_hook(self):
        """The hook is Claude's, so an unset environment still attributes."""
        shell_row, _ = run_hook(self.PAYLOADS[0], Path(self.tmp) / "ag0")
        self.assertEqual(shell_row["agent"], "claude")

    def test_agent_env_override_reaches_the_row(self):
        """This is how a second runtime identifies itself in a shared feed."""
        shell_row, _ = run_hook(self.PAYLOADS[0], Path(self.tmp) / "ag1", agent="codex")
        self.assertEqual(shell_row["agent"], "codex")

    def test_python_side_honours_the_same_variable(self):
        prev = os.environ.get("DAILY_NOTE_AGENT")
        try:
            os.environ["DAILY_NOTE_AGENT"] = "codex"
            self.assertEqual(live_feed.agent_name(), "codex")
        finally:
            if prev is None:
                os.environ.pop("DAILY_NOTE_AGENT", None)
            else:
                os.environ["DAILY_NOTE_AGENT"] = prev

    def test_vault_dir_override_is_honoured_by_both(self):
        """The regression that made the hook write where nothing read.

        The hook has always taken $VAULT_DIR. daily_note hardcoded the path
        until 2026-09-20, so the two disagreed silently.
        """
        vault = Path(self.tmp) / "override"
        shell_row, _ = run_hook(self.PAYLOADS[0], vault)
        self.assertIsNotNone(shell_row, "hook did not write under $VAULT_DIR")
        written = list((vault / "System" / "40-49_telemetry" / "live_feed").rglob("*.jsonl"))
        self.assertTrue(written, "hook wrote nothing under the overridden vault")

        out = subprocess.run(
            [sys.executable, "-c",
             "import daily_note; print(daily_note.VAULT_DIR)"],
            capture_output=True, text=True, cwd=str(ROOT),
            env={**os.environ, "VAULT_DIR": str(vault)}, timeout=30,
        )
        self.assertEqual(out.stdout.strip(), str(vault),
                         "daily_note ignored $VAULT_DIR; the hook would write "
                         "where no Python reader looks")


@unittest.skipIf(SKIP_REASON, SKIP_REASON or "")
class HookNeverBreaksTheToolCallTests(unittest.TestCase):
    """The hook's stated contract: it never blocks or fails a tool call.

    A feed is a convenience. If a malformed payload could make the hook exit
    non-zero, a logging nicety would start failing real work, which is a far
    worse outcome than a missing row.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="feedgarbage-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    GARBAGE = [
        ("empty string", ""),
        ("not json", "this is not json at all"),
        ("truncated json", '{"tool_name": "Bash"'),
        ("json array", "[1,2,3]"),
        ("json null", "null"),
        ("no tool_name", '{"session_id": "abcd"}'),
        ("no session_id", '{"tool_name": "Bash"}'),
        ("tool_input not an object", '{"tool_name":"Bash","tool_input":"nope"}'),
        ("deeply nested", '{"tool_name":"Bash","tool_input":{"command":{"a":{"b":1}}}}'),
    ]

    def test_exits_zero_on_every_garbage_payload(self):
        for label, body in self.GARBAGE:
            with self.subTest(payload=label):
                _, proc = run_hook(body, Path(self.tmp) / label.replace(" ", "_"))
                self.assertEqual(
                    proc.returncode, 0,
                    f"hook exited {proc.returncode} on {label}; this would "
                    f"surface as a failing tool call")

    def test_missing_fields_still_produce_a_usable_row(self):
        """Absent values become defaults rather than crashing or writing null."""
        row, proc = run_hook('{"session_id":"abcd"}', Path(self.tmp) / "defaults")
        self.assertEqual(proc.returncode, 0)
        if row is not None:
            self.assertEqual(row["kind"], "?")
            self.assertEqual(row["detail"], "")

    def test_a_row_that_is_written_is_always_valid_json(self):
        """The renderer skips malformed lines, so a bad write is invisible."""
        for label, body in self.GARBAGE:
            with self.subTest(payload=label):
                vault = Path(self.tmp) / ("j_" + label.replace(" ", "_"))
                run_hook(body, vault)
                root = vault / "System" / "40-49_telemetry" / "live_feed"
                for f in root.rglob("*.jsonl") if root.exists() else []:
                    for line in f.read_text().splitlines():
                        if line.strip():
                            json.loads(line)  # raises if the hook wrote junk


@unittest.skipIf(SKIP_REASON, SKIP_REASON or "")
class EndToEndPipelineTests(unittest.TestCase):
    """hook -> JSONL -> render, as one path rather than three units.

    Every piece of this was individually tested before today and the assembled
    behaviour was still wrong: agent labels computed correctly, from a set that
    could not contain two members in the real rollout state, so they rendered
    on nothing.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="feede2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._real_root = live_feed.FEED_ROOT
        self.addCleanup(setattr, live_feed, "FEED_ROOT", self._real_root)

    def _render_from(self, vault):
        root = vault / "System" / "40-49_telemetry" / "live_feed"
        live_feed.FEED_ROOT = root
        files = list(root.rglob("*.jsonl"))
        self.assertTrue(files, "hook produced no feed file to render")
        date = files[0].stem
        return live_feed.render_markdown(date)

    def test_two_agents_written_by_the_hook_render_distinctly(self):
        vault = Path(self.tmp) / "two"
        run_hook({"tool_name": "Bash", "session_id": "b552zzzz",
                  "tool_input": {"command": "pytest -q"}}, vault, agent="claude")
        run_hook({"tool_name": "Edit", "session_id": "7d5azzzz",
                  "tool_input": {"file_path": "bot.py"}}, vault, agent="codex")
        out = self._render_from(vault)
        self.assertIn("claude:b552", out)
        self.assertIn("codex:7d5a", out)

    def test_one_agent_written_by_the_hook_renders_without_tags(self):
        """Positive control: the ordinary single-agent day must not change."""
        vault = Path(self.tmp) / "one"
        run_hook({"tool_name": "Bash", "session_id": "b552zzzz",
                  "tool_input": {"command": "one"}}, vault, agent="claude")
        run_hook({"tool_name": "Bash", "session_id": "b552zzzz",
                  "tool_input": {"command": "two"}}, vault, agent="claude")
        out = self._render_from(vault)
        self.assertNotIn("⟨", out, "solo feed grew a tag it did not need")

    def test_every_rendered_line_stays_inside_the_callout(self):
        """A line escaping the callout breaks the note's markdown silently."""
        vault = Path(self.tmp) / "callout"
        run_hook({"tool_name": "Bash", "session_id": "abcd0000",
                  "tool_input": {"command": "echo one\necho two"}}, vault)
        out = self._render_from(vault)
        for line in out.splitlines():
            if line.strip():
                self.assertTrue(line.startswith(">"),
                                f"line escaped the callout: {line!r}")


if __name__ == "__main__":
    unittest.main()
