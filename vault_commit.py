"""
vault_commit.py — One-shot mid-day vault commit with smart message.

Clears preflight warning B7 (vault has dirty/uncommitted paths).

Usage:
  python3 vault_commit.py                    # add + commit + push
  python3 vault_commit.py --no-push          # local commit only
  python3 vault_commit.py --message "msg"    # override smart message
  python3 vault_commit.py --dry-run          # print plan, no git ops
  python3 vault_commit.py --json             # machine-readable output
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import harness_lib
import atomic_io

VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"


# ── Smart message heuristic ──────────────────────────────────────────────────

def build_smart_message(changed_paths: list[str]) -> str:
    """Bucket changed paths and emit a conventional commit message."""
    ts = datetime.now().strftime("%H:%M")
    n = len(changed_paths)

    buckets: dict[str, int] = {}
    for p in changed_paths:
        top = p.split("/")[0] if "/" in p else p
        buckets[top] = buckets.get(top, 0) + 1

    tops = set(buckets)

    if tops <= {"Daily Notes"}:
        return f"chore(vault): daily note updates [{n} file(s), {ts}]"
    if tops <= {"Captured"}:
        captured = buckets.get("Captured", 0)
        misc = n - captured
        if misc:
            return f"chore(vault): {captured} capture(s) + {misc} misc [{ts}]"
        return f"chore(vault): {captured} capture(s) [{ts}]"
    if tops <= {"Audits"}:
        date_str = datetime.now().strftime("%Y-%m-%d")
        return f"chore(vault): git audit {date_str}"
    if tops <= {"Plans"}:
        return f"chore(vault): plan update [{n} file(s), {ts}]"
    if tops <= {"Hubs"}:
        return f"chore(vault): hub sync [{n} file(s), {ts}]"
    return f"chore(vault): mid-day sync [{n} file(s), {ts}]"


# ── Core API ─────────────────────────────────────────────────────────────────

def commit_vault(
    *,
    push: bool = True,
    message: str | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Stage, commit, and optionally push the vault repo.

    Returns:
      {status: "committed"|"clean"|"failed",
       sha, files_changed, message, pushed, stderr, generated_at}
    """
    result: dict = {
        "status": "unknown",
        "sha": None,
        "files_changed": 0,
        "message": None,
        "pushed": False,
        "stderr": "",
        "generated_at": harness_lib.iso_now(),
    }

    if not (VAULT_DIR / ".git").exists():
        result["status"] = "failed"
        result["stderr"] = f"No git repo at {VAULT_DIR}"
        return result

    # Porcelain check — what's dirty?
    try:
        porcelain = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=VAULT_DIR, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except subprocess.CalledProcessError as e:
        result["status"] = "failed"
        result["stderr"] = str(e)
        return result

    if not porcelain:
        result["status"] = "clean"
        return result

    changed_paths = [line[3:].strip() for line in porcelain.splitlines() if line.strip()]
    result["files_changed"] = len(changed_paths)
    commit_msg = message or build_smart_message(changed_paths)
    result["message"] = commit_msg

    if dry_run:
        result["status"] = "dry_run"
        result["files_changed"] = len(changed_paths)
        return result

    # Stage + commit under vault lock (lock covers git add → commit, not push)
    try:
        with atomic_io.vault_lock():
            subprocess.check_call(
                ["git", "add", "."],
                cwd=VAULT_DIR, stderr=subprocess.DEVNULL
            )
            subprocess.check_call(
                ["git", "commit", "-m", commit_msg],
                cwd=VAULT_DIR, stderr=subprocess.DEVNULL
            )
    except subprocess.CalledProcessError as e:
        result["status"] = "failed"
        result["stderr"] = str(e)
        return result

    # Get SHA
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=VAULT_DIR, text=True
        ).strip()
        result["sha"] = sha
    except subprocess.CalledProcessError:
        pass

    result["status"] = "committed"

    # Push outside the lock — network latency shouldn't hold it
    if push:
        try:
            subprocess.check_call(
                ["git", "push"],
                cwd=VAULT_DIR,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            result["pushed"] = True
        except subprocess.CalledProcessError as e:
            result["pushed"] = False
            result["stderr"] = f"push failed: {e}"

    # Telemetry
    try:
        import harness_log
        harness_log.log_op(
            "vault_commit", "claude", str(VAULT_DIR),
            result["status"],
            content=commit_msg,
        )
    except Exception:
        pass

    return result


def format_result_md(result: dict) -> str:
    status = result["status"]
    if status == "clean":
        return "**Vault:** clean — nothing to commit."
    if status == "dry_run":
        return (
            f"**Vault (dry-run):** {result['files_changed']} file(s) would be committed.\n"
            f"Message: `{result['message']}`"
        )
    if status == "committed":
        pushed = "pushed ✓" if result["pushed"] else "local only"
        return (
            f"**Vault:** committed `{result['sha']}` — {result['files_changed']} file(s). "
            f"{pushed}\nMessage: `{result['message']}`"
        )
    return f"**Vault:** FAILED — {result.get('stderr', 'unknown error')}"


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Commit vault dirty paths.")
    parser.add_argument("--no-push", action="store_true", help="Commit only, no push.")
    parser.add_argument("--message", "-m", default=None, help="Override commit message.")
    parser.add_argument("--dry-run", action="store_true", help="Show plan, no git ops.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    result = commit_vault(
        push=not args.no_push,
        message=args.message,
        dry_run=args.dry_run,
    )

    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(format_result_md(result))


if __name__ == "__main__":
    main()
