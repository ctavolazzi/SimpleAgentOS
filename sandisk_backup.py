"""
sandisk_backup.py — Vault backup to the local SanDisk drive. Append-only.

Follows the structure already established at
/Volumes/SanDisk/backups/Personal-Remote-Vault/:

  vault-YYYY-MM-DD.bundle   — full git history (git bundle --all)
  working-tree/             — rsync mirror of the working tree (no deletes)

Overwrite policy: a bundle name is never reused — if today's bundle exists,
the new one gets an HHMMSS suffix. The working-tree rsync runs WITHOUT
--delete, so files removed from the vault stay recoverable on the drive.

Fails soft when the drive is not mounted: returns a status dict, raises
nothing — spin-up and wrap-up must not care whether the SanDisk is plugged in.

Each bundle is `git bundle --all` — full history, not incremental — so every
day's bundle is roughly the size of the whole repo. `prune()` implements the
retention policy flagged in the 2026-07-18 build session TODO: keep the most
recent N daily bundles plus one (the newest) per calendar month before that
window. It never deletes silently — call it with `dry_run=True` (the CLI
default) to preview, `dry_run=False` to actually remove files.
"""

import subprocess
from datetime import datetime
from pathlib import Path

VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
BACKUP_ROOT = Path("/Volumes/SanDisk/backups/Personal-Remote-Vault")
KEEP_DAILY = 14


def drive_mounted() -> bool:
    return BACKUP_ROOT.parent.parent.is_dir()  # /Volumes/SanDisk


def backup() -> dict:
    """Run bundle + working-tree backup. Returns a status dict."""
    result = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "mounted": drive_mounted(),
        "bundle": None,
        "rsync": None,
    }
    if not result["mounted"]:
        result["status"] = "skipped (SanDisk not mounted)"
        return result

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. Git bundle — full history, append-only naming
    bundle_path = BACKUP_ROOT / f"vault-{today}.bundle"
    if bundle_path.exists():
        stamp = datetime.now().strftime("%H%M%S")
        bundle_path = BACKUP_ROOT / f"vault-{today}_{stamp}.bundle"
    try:
        subprocess.run(
            ["git", "bundle", "create", str(bundle_path), "--all"],
            cwd=VAULT_DIR, check=True, capture_output=True, text=True,
            timeout=300,
        )
        size_mb = bundle_path.stat().st_size / 1_048_576
        result["bundle"] = f"{bundle_path.name} ({size_mb:.0f} MB)"
    except Exception as e:
        result["bundle"] = f"failed ({type(e).__name__}): {e}"

    # 2. Working-tree mirror — additive rsync, never deletes on the drive
    try:
        proc = subprocess.run(
            ["rsync", "-a", "--exclude", ".git",
             f"{VAULT_DIR}/", str(BACKUP_ROOT / "working-tree/")],
            # First full pass to a slow USB drive can exceed 10 minutes;
            # incremental passes after that are fast.
            check=True, capture_output=True, text=True, timeout=1800,
        )
        result["rsync"] = "ok"
    except Exception as e:
        result["rsync"] = f"failed ({type(e).__name__}): {e}"

    ok = (result["bundle"] and "failed" not in str(result["bundle"])
          and result["rsync"] == "ok")
    result["status"] = "ok" if ok else "partial failure"
    return result


def _bundle_date(path: Path) -> str:
    """`vault-2026-07-19.bundle` / `vault-2026-07-19_061602.bundle` -> '2026-07-19'."""
    return path.stem[len("vault-"):][:10]


def prune(keep_daily: int = KEEP_DAILY, dry_run: bool = True) -> dict:
    """Enforce retention: keep the newest `keep_daily` dates in full, and for
    every older calendar month keep only its single newest bundle.

    Never touches `working-tree/` — only `vault-*.bundle` files. Returns a
    report; nothing is deleted unless `dry_run=False`.
    """
    report = {"mounted": drive_mounted(), "kept": [], "deleted": [], "dry_run": dry_run}
    if not report["mounted"]:
        report["status"] = "skipped (SanDisk not mounted)"
        return report

    bundles = sorted(BACKUP_ROOT.glob("vault-*.bundle"), key=_bundle_date, reverse=True)
    dates_seen = []
    for b in bundles:
        d = _bundle_date(b)
        if d not in dates_seen:
            dates_seen.append(d)

    keep_dates = set(dates_seen[:keep_daily])
    older_dates = dates_seen[keep_daily:]

    # One survivor per calendar month among the older dates — the newest
    # bundle in each YYYY-MM (dates_seen is already newest-first).
    seen_months = set()
    keep_one_per_month = set()
    for d in older_dates:
        month = d[:7]
        if month not in seen_months:
            seen_months.add(month)
            keep_one_per_month.add(d)

    to_delete = []
    for b in bundles:
        d = _bundle_date(b)
        if d in keep_dates or d in keep_one_per_month:
            report["kept"].append(b.name)
        else:
            to_delete.append(b)

    freed = 0
    for b in to_delete:
        size = b.stat().st_size
        if not dry_run:
            b.unlink()
        report["deleted"].append(b.name)
        freed += size
    report["freed_mb"] = round(freed / 1_048_576, 1)
    report["status"] = "ok"
    return report


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="SanDisk vault backup")
    parser.add_argument("--prune", action="store_true", help="run retention prune instead of backup")
    parser.add_argument("--apply", action="store_true", help="with --prune, actually delete (default is dry-run)")
    args = parser.parse_args()

    if args.prune:
        print(json.dumps(prune(dry_run=not args.apply), indent=2))
    else:
        print(json.dumps(backup(), indent=2))
