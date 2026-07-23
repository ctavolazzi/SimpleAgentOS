"""
plan_rollover.py — Roll forward incomplete work from locked daily plans.

Reads the N most recent locked plan files, synthesizes:
  - Recurring items (appeared across multiple days → high priority)
  - Fresh rollover (yesterday's incomplete → seed today's plan)
  - Preserved thoughts (saved insights never acted on)
  - Satellites history (what got spun off, when)
  - Velocity snapshot (completion rate trend)

Usage:
    python3 plan_rollover.py [--days N] [--json] [--create-today]
    python3 plan_rollover.py --create-today   # rolls over + creates today's plan
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

VAULT_ROOT = Path("/Users/ctavolazzi/Documents/Personal-Remote-Vault")
PLANS_DIR = VAULT_ROOT / "Plans"


def _plan_path(d: str) -> Path:
    return PLANS_DIR / f"{d}_daily_plan.md"


def _date_range(days: int) -> list[str]:
    """Return the last N calendar dates (not counting today), most recent first."""
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(1, days + 1)]


def load_plan_data(d: str) -> Optional[dict]:
    """Load and parse a plan file. Returns None if not found or not locked."""
    path = _plan_path(d)
    if not path.exists():
        return None
    try:
        import daily_plan as dp
        return dp.extract_rollover(d) | {"meta": dp.get_plan(d)}
    except Exception:
        return None


def analyze_rollover(days: int = 7) -> dict:
    """
    Read last N days of locked plans, build rollover report.

    Returns:
        {
          "yesterday": {incomplete_items, completed_items, ...},
          "recurring": [{item, seen_on: [dates], days_rolling: int}, ...],
          "preserved_thoughts": [{thought, date}, ...],
          "satellites": [{entry, date}, ...],
          "velocity": [{date, total, completed, pct}, ...],
          "analysis_dates": [...],
          "generated_at": iso,
        }
    """
    from datetime import datetime

    dates = _date_range(days)
    yesterday = dates[0] if dates else None

    # Gather data across all days
    all_data: dict[str, dict] = {}
    for d in dates:
        data = load_plan_data(d)
        if data:
            all_data[d] = data

    # Yesterday's rollover (primary seed for today)
    yesterday_data = all_data.get(yesterday, {})

    # Recurring items: appear as incomplete across multiple days
    item_appearances: dict[str, list[str]] = {}
    for d, data in all_data.items():
        for item in data.get("incomplete_items", []):
            item_appearances.setdefault(item.strip(), []).append(d)

    recurring = sorted(
        [
            {"item": item, "seen_on": sorted(dates_seen, reverse=True),
             "days_rolling": len(dates_seen)}
            for item, dates_seen in item_appearances.items()
            if len(dates_seen) > 1
        ],
        key=lambda x: x["days_rolling"],
        reverse=True,
    )

    # All preserved thoughts with dates
    preserved_thoughts = []
    for d, data in sorted(all_data.items(), reverse=True):
        for t in data.get("preserved_thoughts", []):
            preserved_thoughts.append({"thought": t, "date": d})

    # All satellites with dates
    satellites = []
    for d, data in sorted(all_data.items(), reverse=True):
        for s in data.get("satellites", []):
            satellites.append({"entry": s, "date": d})

    # Velocity (completion rate per day)
    velocity = []
    for d in sorted(all_data.keys(), reverse=True):
        data = all_data[d]
        total = len(data.get("incomplete_items", [])) + len(data.get("completed_items", []))
        completed = len(data.get("completed_items", []))
        velocity.append({
            "date": d,
            "total": total,
            "completed": completed,
            "pct": int(completed / total * 100) if total else 0,
        })

    return {
        "yesterday": yesterday_data,
        "recurring": recurring,
        "preserved_thoughts": preserved_thoughts,
        "satellites": satellites,
        "velocity": velocity,
        "analysis_dates": sorted(all_data.keys(), reverse=True),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def format_rollover_md(analysis: dict) -> str:
    """Format rollover analysis as markdown for daily note or terminal."""
    lines = ["## Rollover Analysis\n"]

    # Recurring items (highest priority)
    if analysis.get("recurring"):
        lines.append("### Recurring (Multi-Day Blockers)")
        for item in analysis["recurring"]:
            days = item["days_rolling"]
            lines.append(f"- [ ] {item['item']}  _(rolling {days}d)_")
        lines.append("")

    # Yesterday incomplete
    yesterday_items = analysis.get("yesterday", {}).get("incomplete_items", [])
    if yesterday_items:
        lines.append("### From Yesterday")
        for item in yesterday_items:
            lines.append(f"- [ ] {item}")
        lines.append("")

    # Preserved thoughts
    thoughts = analysis.get("preserved_thoughts", [])[:5]  # last 5
    if thoughts:
        lines.append("### Preserved Thoughts (Recent)")
        for t in thoughts:
            lines.append(f"- [{t['date']}] {t['thought']}")
        lines.append("")

    # Velocity
    velocity = analysis.get("velocity", [])[:5]
    if velocity:
        lines.append("### Velocity")
        for v in velocity:
            bar = "█" * (v["pct"] // 20) + "░" * (5 - v["pct"] // 20)
            lines.append(f"- `{v['date']}` {bar} {v['pct']}% ({v['completed']}/{v['total']})")
        lines.append("")

    return "\n".join(lines)


def build_seed_items(analysis: dict) -> list[str]:
    """
    Return prioritized list of items to seed into today's plan.
    Order: recurring (worst blockers first) → yesterday incomplete (unique).
    """
    recurring_texts = {item["item"] for item in analysis.get("recurring", [])}
    yesterday_incomplete = analysis.get("yesterday", {}).get("incomplete_items", [])

    # Recurring items first (already deduplicated)
    seed = [item["item"] for item in analysis.get("recurring", [])]

    # Then yesterday-only items (not in recurring)
    for item in yesterday_incomplete:
        if item.strip() not in recurring_texts:
            seed.append(item.strip())

    return seed


def create_today_from_rollover(days: int = 7, force: bool = False) -> dict:
    """Full pipeline: analyze → seed → create today's plan."""
    import daily_plan as dp
    analysis = analyze_rollover(days=days)
    seed_items = build_seed_items(analysis)
    yesterday_dates = analysis.get("analysis_dates", [])
    source_date = yesterday_dates[0] if yesterday_dates else None

    result = dp.create_today(
        rollover_items=seed_items or None,
        source_date=source_date,
        force=force,
    )
    result["seed_count"] = len(seed_items)
    result["rollover_analysis"] = analysis
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Analyze locked plans and build rollover for today."
    )
    parser.add_argument("--days", type=int, default=7,
                        help="How many past days to analyze (default: 7)")
    parser.add_argument("--json", action="store_true",
                        help="Output full JSON analysis")
    parser.add_argument("--md", action="store_true",
                        help="Output markdown-formatted rollover report")
    parser.add_argument("--create-today", action="store_true",
                        help="Create today's plan seeded from rollover (idempotent)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite today's plan if it already exists")
    args = parser.parse_args()

    if args.create_today:
        result = create_today_from_rollover(days=args.days, force=args.force)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            status = "created" if result.get("created") else "already exists"
            print(f"  Today's plan: {status} — {result.get('path', '?')}")
            print(f"  Seeds: {result.get('seed_count', 0)} items rolled over")
        return

    analysis = analyze_rollover(days=args.days)

    if args.json:
        print(json.dumps(analysis, indent=2, default=str))
    elif args.md:
        print(format_rollover_md(analysis))
    else:
        # Compact terminal summary
        yesterday = analysis.get("yesterday", {})
        incomplete = len(yesterday.get("incomplete_items", []))
        recurring = len(analysis.get("recurring", []))
        thoughts = len(analysis.get("preserved_thoughts", []))
        satellites = len(analysis.get("satellites", []))
        print(f"  Rollover ({args.days}d lookback):")
        print(f"    Yesterday incomplete : {incomplete}")
        print(f"    Recurring blockers   : {recurring}")
        print(f"    Preserved thoughts   : {thoughts}")
        print(f"    Satellites logged    : {satellites}")
        if recurring:
            print(f"\n  Top blockers:")
            for item in analysis["recurring"][:3]:
                print(f"    [{item['days_rolling']}d] {item['item']}")


if __name__ == "__main__":
    main()
