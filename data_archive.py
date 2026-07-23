"""
data_archive.py — Append-only capture of every payload spin-up fetches.

Part of the daily-note harness. Each run writes ONE new timestamped JSON file
under the vault's telemetry tree:

    System/40-49_telemetry/spin_up_data/YYYY-MM/YYYY-MM-DD_HHMMSS.json

The vault is a private GitHub repo, so archived data rides the vault's normal
backup/commit flow. By construction nothing is ever overwritten: existing
files are never opened for write, and a filename collision (two runs in the
same second) gets a numeric suffix instead of a clobber.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

VAULT_DIR = Path.home() / "Documents" / "Personal-Remote-Vault"
ARCHIVE_DIR = VAULT_DIR / "System" / "40-49_telemetry" / "spin_up_data"


def _unique_path(directory: Path, stem: str) -> Path:
    """Return a path that does not exist yet — suffix -2, -3… on collision."""
    path = directory / f"{stem}.json"
    n = 2
    while path.exists():
        path = directory / f"{stem}-{n}.json"
        n += 1
    return path


def archive(payloads: dict, date: Optional[str] = None) -> Path:
    """
    Write one immutable snapshot of this run's gathered data.

    `payloads` is {source_name: data} — weather dict, news dict, arxiv digest,
    quote, on-this-day events, air quality, repo scan, etc. Anything JSON-
    serializable; Paths are stringified.

    Returns the written path. Raises on filesystem errors — caller decides
    whether to fail soft.
    """
    now = datetime.now()
    if date is None:
        date = now.strftime("%Y-%m-%d")

    month_dir = ARCHIVE_DIR / date[:7]
    month_dir.mkdir(parents=True, exist_ok=True)

    record = {
        "captured_at": now.isoformat(timespec="seconds"),
        "date": date,
        "sources": sorted(payloads.keys()),
        "data": payloads,
    }

    path = _unique_path(month_dir, f"{date}_{now.strftime('%H%M%S')}")
    path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return path


def runs_for(date: Optional[str] = None) -> list:
    """List archived snapshot paths for a date (default today), oldest first."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    month_dir = ARCHIVE_DIR / date[:7]
    if not month_dir.is_dir():
        return []
    return sorted(month_dir.glob(f"{date}_*.json"))


if __name__ == "__main__":
    p = archive({"smoke_test": {"ok": True}})
    print(f"wrote {p}")
    print(f"runs today: {[str(x.name) for x in runs_for()]}")
