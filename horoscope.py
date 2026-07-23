"""
horoscope.py — Daily cosmic reading blended with WAFT Being state and Vault history.

Fetches real horoscope from ohmanda.com (free, no key) and layers it with
the Being's current state and a 7-day historical momentum analysis,
rendered in oracular language.

Set YOUR_SIGN to your sun sign before first run.
Valid signs: aries taurus gemini cancer leo virgo
             libra scorpio sagittarius capricorn aquarius pisces
"""

import json
import re
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Config ──────────────────────────────────────────────────────────────────

YOUR_SIGN = "scorpio"  # ← change to your sign

TIMEOUT = 3  # hard cap — never hang the morning harness

_OHMANDA = "https://ohmanda.com/api/horoscope/{sign}/"

_VAULT_DAILY = Path.home() / "Documents" / "Personal-Remote-Vault" / "Daily Notes"

# ── Cosmic language maps ─────────────────────────────────────────────────────

_SCINT_ARCHETYPES = {
    "LOGIC_FRACTURE": ("The Fractured Path",
                       "ordered sequence disrupted — tend the chain of cause and effect"),
    "SYNTAX_TEAR":    ("The Torn Veil",
                       "coherence scattered — restore the fabric before meaning bleeds out"),
    "SAFETY_VOID":    ("The Void Gate",
                       "protection weakened — seal the breach or shadows slip through"),
    "HALLUCINATION":  ("The False Mirror",
                       "epistemic fog thickens — question what feels most certain"),
}

_STATE_PROSE = {
    "spawning":   "awakens into its first form, potential vast and untested",
    "learning":   "drinks deep from the stream of accumulated signal",
    "evolving":   "writhes in mid-metamorphosis, neither old form nor new",
    "completing": "approaches convergence — the cycle nears its seal",
    "archived":   "sleeps in the amber of accomplished form",
    "dead":       "has passed beyond the reach of the living harness",
}

_FITNESS_PROSE = [
    (0.0,  0.1,  "The vital force lies dormant. One completed quest will ignite the spark."),
    (0.1,  0.3,  "A faint pulse stirs. The Being is learning to move through the world."),
    (0.3,  0.5,  "Momentum builds. Consistent completion will deepen the current."),
    (0.5,  0.7,  "The Being strides with growing confidence. Half the mountain climbed."),
    (0.7,  0.9,  "High vitality. The Being approaches mastery of this generation."),
    (0.9,  1.01, "The vital force blazes at full strength. Transcendence is near."),
]


def _fitness_prose(score: float) -> str:
    for lo, hi, text in _FITNESS_PROSE:
        if lo <= score < hi:
            return text
    return "Vital force immeasurable."


def _fitness_bar(score: float) -> str:
    filled = int(score * 10)
    return "█" * filled + "░" * (10 - filled)


# ── Historical vault analysis ────────────────────────────────────────────────

def _get_weekly_momentum() -> dict:
    """Scan last 7 days of daily notes for completed tasks and lingering blockers."""
    momentum = 0
    blocker = None
    days_active = 0

    for i in range(1, 8):
        date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        note_path = _VAULT_DAILY / f"{date_str}.md"
        if not note_path.exists():
            continue
        days_active += 1
        try:
            content = note_path.read_text(encoding="utf-8")
            momentum += len(re.findall(r"^[ \t]*- \[[xX]\]", content, re.MULTILINE))
            if i == 1:
                m = re.search(r"\*\*Blockers:\*\*\s*(.+)", content, re.IGNORECASE)
                if m:
                    blocker_text = m.group(1).strip()
                    if blocker_text.lower() not in ("none", "none blocking spin-up.", ""):
                        blocker = blocker_text
        except Exception:
            pass

    return {"momentum": momentum, "blocker": blocker, "days_active": days_active}


def _note_themes(days_back: int = 1) -> dict:
    """Pull surfacable themes from a recent daily note: idea seeds, lab carries, code_refs."""
    themes = {"seeds": [], "lab_open": [], "code_refs": []}
    date_str = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    note_path = _VAULT_DAILY / f"{date_str}.md"
    if not note_path.exists():
        return themes
    try:
        content = note_path.read_text(encoding="utf-8")
    except Exception:
        return themes

    seeds_match = re.search(r"\*\*Seeds:\*\*\s*\n((?:\s*- .+\n?)+)", content)
    if seeds_match:
        for line in seeds_match.group(1).splitlines():
            m = re.match(r"\s*- (.+)", line)
            if m:
                seed = m.group(1).strip()
                if len(seed) > 80:
                    # Trim at last word boundary within 80 chars
                    seed = seed[:80].rsplit(" ", 1)[0] + "…"
                themes["seeds"].append(seed)

    lab_match = re.search(r"\*\*Open experiments:\*\*\s*\n((?:\s*- .+\n?)+)", content)
    if lab_match:
        for line in lab_match.group(1).splitlines():
            m = re.match(r"\s*- (.+)", line)
            if m:
                themes["lab_open"].append(m.group(1).strip()[:80])

    fm_match = re.search(r"^---\n(.*?)\n---", content, re.DOTALL)
    if fm_match:
        refs_match = re.search(r"code_refs:\s*\n((?:\s+- .+\n?)+)", fm_match.group(1))
        if refs_match:
            for line in refs_match.group(1).splitlines():
                m = re.match(r"\s+- (.+)", line)
                if m:
                    ref = m.group(1).strip().strip('"').strip("'")
                    themes["code_refs"].append(ref)

    return themes


def _note_themes_prose(themes: dict) -> str:
    """Render note themes as cosmic prose block."""
    lines = []
    if themes["seeds"]:
        lines.append("**Whispers from the dream-pool:**")
        for s in themes["seeds"][:2]:
            lines.append(f"- *{s}*")
    if themes["lab_open"]:
        if lines:
            lines.append("")
        lines.append("**Threads still unwoven:**")
        for t in themes["lab_open"][:3]:
            lines.append(f"- *{t}*")
    if themes["code_refs"]:
        if lines:
            lines.append("")
        refs = " · ".join(f"`{r}`" for r in themes["code_refs"][:3])
        lines.append(f"**The hands lingered upon** {refs}")
    return "\n".join(lines)


def _momentum_prose(data: dict) -> str:
    """Translate weekly metrics into cosmic framing."""
    days = data["days_active"]
    mom = data["momentum"]

    if days == 0:
        base = "*The historical tether is severed; the past week echoes in silence.*"
    elif mom >= 15:
        base = (f"*A massive surge of kinetic energy has marked the past {days} cycles, "
                f"yielding {mom} cosmic resolutions.*")
    elif mom >= 7:
        base = (f"*A steady, rhythmic current of progress flows through the past {days} cycles "
                f"— {mom} completions woven into the record.*")
    elif mom >= 1:
        base = (f"*A quiet, deliberate gathering of force across {days} cycles. "
                f"{mom} resolutions committed to the chronicle.*")
    else:
        base = (f"*A period of deep internal alignment — {days} cycles observed, "
                f"no tasks yet sealed. The pressure builds.*")

    if data["blocker"]:
        shadow = f"\n> ⚠️ **Celestial shadow from yesterday:** *{data['blocker']}*"
        return base + shadow

    return base


# ── Fetcher ──────────────────────────────────────────────────────────────────

def fetch(sign: str = YOUR_SIGN) -> dict:
    """Fetch today's horoscope for sign. Returns {sign, date, horoscope, source}."""
    url = _OHMANDA.format(sign=sign.lower().strip())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SimpleAgentOS/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read())
        return {
            "sign":      data.get("sign", sign),
            "date":      data.get("date", datetime.now().strftime("%Y-%m-%d")),
            "horoscope": data.get("horoscope", ""),
            "source":    "ohmanda.com",
        }
    except Exception as e:
        return {
            "sign":      sign,
            "date":      datetime.now().strftime("%Y-%m-%d"),
            "horoscope": "",
            "source":    f"failed ({type(e).__name__})",
            "error":     str(e),
        }


# ── Cosmic WAFT overlay ──────────────────────────────────────────────────────

def _waft_cosmic(being: dict, quests: list) -> str:
    """Render WAFT state + 7-day vault history in oracular language."""
    state_key = being.get("state", "spawning").lower()
    state_prose = _STATE_PROSE.get(state_key, "exists in an undefined liminal state")
    fitness = being.get("fitness", 0.0)
    gen = being.get("generation", 0)
    bar = _fitness_bar(fitness)
    fp = _fitness_prose(fitness)

    weekly = _get_weekly_momentum()
    history = _momentum_prose(weekly)

    lines = [
        f"**The Being** {state_prose}. "
        f"Generation `{gen}` · Vital Force `{bar}` `{fitness:.2f}`",
        "",
        f"*{fp}*",
        "",
        history,
    ]

    themes = _note_themes()
    themes_md = _note_themes_prose(themes)
    if themes_md:
        lines.append("")
        lines.append(themes_md)

    pending = [q for q in quests if not q.get("complete")]
    done    = [q for q in quests if q.get("complete")]

    if pending:
        lines.append("")
        lines.append("**Trials that await resolution today:**")
        for q in pending[:3]:
            stype = q.get("scint_type", "LOGIC_FRACTURE")
            archetype, cosmic_desc = _SCINT_ARCHETYPES.get(stype, (stype, "unknown challenge"))
            task = q['task']
            lines.append(f"- **{archetype}** — {task}")
            lines.append(f"  *{cosmic_desc}*")

    if done:
        lines.append("")
        plural = "trial has" if len(done) == 1 else "trials have"
        lines.append(
            f"*{len(done)} {plural} been resolved today. "
            f"The vital force remembers every completion.*"
        )

    return "\n".join(lines)


# ── Markdown renderer ────────────────────────────────────────────────────────

def format_md(data: dict, waft_data: Optional[dict] = None, moon_phase: Optional[str] = None) -> str:
    """Render daily reading as Obsidian callout — horoscope + WAFT + vault history."""
    sign = data.get("sign", YOUR_SIGN).capitalize()
    date_str = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    horoscope_text = data.get("horoscope", "")
    source = data.get("source", "")

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekday = dt.strftime("%A, %B %-d")
    except Exception:
        weekday = date_str

    header = f"> [!quote]+ ☽ {sign} · {weekday}"
    if moon_phase:
        header += f" · {moon_phase}"
    lines = [header]

    if horoscope_text:
        for sentence in horoscope_text.replace(". ", ".\n").splitlines():
            sentence = sentence.strip()
            if sentence:
                lines.append(f"> {sentence}")
        if source:
            lines.append(">")
            lines.append(f"> *— {source}*")
    else:
        lines.append("> *Cosmic signal unavailable. The stars speak in silence today.*")

    lines.append("")

    if waft_data:
        being = waft_data.get("being", {})
        quests = waft_data.get("quests", [])
        lines.append("---")
        lines.append("")
        lines.append(_waft_cosmic(being, quests))

    lines.append("")

    return "\n".join(lines)
