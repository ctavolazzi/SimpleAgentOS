"""
image_quip.py — Cheeky one-line caption tying the daily hero image to today's work.

Thin wrapper over `haiku_minion.quip()` with date-keyed caching via the minion.
Fails soft: empty string on any error. Caller should render a placeholder in
that case so a later session can fill it in.
"""

from datetime import datetime

import haiku_minion


def generate(image_caption: str, focus: str = "", top_quest: str = "",
             force: bool = False) -> str:
    """Return a one-line quip linking the image to today's work. Empty on failure."""
    # Captionless images (e.g. the Lorem Picsum fallback) still deserve a quip —
    # without this the hero block shipped a "<!-- quip -->" placeholder all day.
    caption = (image_caption or "").strip() or "an unlabeled abstract photograph"

    focus_line = focus.strip() or "general daily harness work"
    quest_line = top_quest.strip() or "no specific quest queued"
    context = f"{focus_line} · top quest: {quest_line}"
    today = datetime.now().strftime("%Y-%m-%d")

    return haiku_minion.quip(
        subject=caption,
        context=context,
        style="dry wit, observational, playful — not cringey, not epic, not corporate",
        cache_key=f"image_quip-{today}",
        force=force,
    )


if __name__ == "__main__":
    import sys
    cap = sys.argv[1] if len(sys.argv) > 1 else "Rapanui Rock during sunset, Sumner, Christchurch, New Zealand"
    foc = sys.argv[2] if len(sys.argv) > 2 else "daily spin-up"
    qst = sys.argv[3] if len(sys.argv) > 3 else "Harness consolidation — commit 6 dirty harness files"
    print(generate(cap, foc, qst, force=True))
