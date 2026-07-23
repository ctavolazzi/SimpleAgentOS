"""
capture_url.py — Fetch a URL, extract metadata, write a vault doc, and link it in today's daily note.

Part of the daily-note harness. Handles GitHub repos specially via the GitHub REST API.
All other URLs: stdlib urllib + <meta> tag extraction.

Usage:
    python3 capture_url.py <url> [--note "optional context"]
    python3 capture_url.py --help
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import atomic_io

# ── Vault config ──────────────────────────────────────────────────────────────

VAULT_ROOT = Path("/Users/ctavolazzi/Documents/Personal-Remote-Vault")
CAPTURED_DIR = VAULT_ROOT / "Captured"
DAILY_NOTES_DIR = VAULT_ROOT / "Daily Notes"
DAILY_NOTE_SECTION = "## Captured URLs"


# ── Slug ──────────────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    """Convert text to a safe filename slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:60]


# ── Fetchers ──────────────────────────────────────────────────────────────────

def _fetch_github(owner: str, repo: str) -> dict:
    """Hit the GitHub REST API for repo metadata."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    req = urllib.request.Request(api_url, headers={"User-Agent": "capture_url/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        return {"error": str(exc)}

    return {
        "type": "github_repo",
        "title": data.get("full_name", f"{owner}/{repo}"),
        "description": data.get("description") or "",
        "homepage": data.get("homepage") or "",
        "language": data.get("language") or "unknown",
        "stars": data.get("stargazers_count", 0),
        "forks": data.get("forks_count", 0),
        "topics": data.get("topics", []),
        "fork": data.get("fork", False),
        "parent": data.get("parent", {}).get("full_name") if data.get("fork") else None,
        "pushed_at": (data.get("pushed_at") or "")[:10],
        "license": (data.get("license") or {}).get("spdx_id") or "none",
        "html_url": data.get("html_url", ""),
        "default_branch": data.get("default_branch", "main"),
    }


def _fetch_generic(url: str) -> dict:
    """Fetch any URL and extract <title> + common <meta> tags."""
    req = urllib.request.Request(url, headers={"User-Agent": "capture_url/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(65536).decode("utf-8", errors="replace")
            final_url = resp.geturl()
    except urllib.error.HTTPError as exc:
        return {"type": "generic", "title": url, "description": f"HTTP {exc.code}", "error": str(exc), "html_url": url}
    except Exception as exc:
        return {"type": "generic", "title": url, "description": "", "error": str(exc), "html_url": url}

    def _meta(prop: str) -> str:
        m = re.search(rf'<meta[^>]+(?:name|property)=["\'](?:og:)?{prop}["\'][^>]+content=["\'](.*?)["\']', raw, re.I)
        if not m:
            m = re.search(rf'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:name|property)=["\'](?:og:)?{prop}["\']', raw, re.I)
        return m.group(1).strip() if m else ""

    title_m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    title = (title_m.group(1).strip() if title_m else "") or _meta("title") or url

    return {
        "type": "webpage",
        "title": re.sub(r"\s+", " ", title)[:120],
        "description": _meta("description") or _meta("og:description") or "",
        "image": _meta("og:image") or "",
        "html_url": final_url,
    }


def fetch_url(url: str) -> dict:
    """Route to the correct fetcher and return a metadata dict."""
    gh = re.match(r"https?://(?:www\.)?github\.com/([^/]+)/([^/?#]+)", url)
    if gh:
        return _fetch_github(gh.group(1), gh.group(2))
    return _fetch_generic(url)


# ── Vault doc builder ─────────────────────────────────────────────────────────

def _build_github_doc(meta: dict, url: str, note: str) -> tuple[str, str]:
    """Return (display_title, markdown_content) for a GitHub repo doc."""
    title = meta["title"]
    parent_line = f"\n**Forked from:** {meta['parent']}" if meta.get("fork") and meta.get("parent") else ""
    topics_line = f"\n**Topics:** {', '.join(meta['topics'])}" if meta.get("topics") else ""
    homepage_line = f"\n**Homepage:** {meta['homepage']}" if meta.get("homepage") else ""
    note_section = f"\n## Notes\n\n{note}" if note else "\n## Notes\n\n[Add your reason / context here]"

    content = f"""# {title}

**Source:** {url}
**Captured:** {date.today().isoformat()}
**Type:** GitHub Repository
**Language:** {meta['language']}
**Stars:** {meta['stars']} · **Forks:** {meta['forks']}
**License:** {meta['license']}
**Last push:** {meta['pushed_at']}{parent_line}{topics_line}{homepage_line}

---

## Description

{meta['description'] or '_No description provided._'}
{note_section}
"""
    return title, content


def _build_generic_doc(meta: dict, url: str, note: str) -> tuple[str, str]:
    """Return (display_title, markdown_content) for a generic webpage doc."""
    title = meta.get("title") or url
    desc_section = f"\n## Description\n\n{meta['description']}" if meta.get("description") else ""
    error_section = f"\n> ⚠️ Fetch error: {meta['error']}" if meta.get("error") else ""
    note_section = f"\n## Notes\n\n{note}" if note else "\n## Notes\n\n[Add your reason / context here]"

    content = f"""# {title}

**Source:** {url}
**Captured:** {date.today().isoformat()}
**Type:** Webpage
{error_section}
---
{desc_section}
{note_section}
"""
    return title, content


def build_doc(meta: dict, url: str, note: str) -> tuple[str, str]:
    if meta.get("type") == "github_repo":
        return _build_github_doc(meta, url, note)
    return _build_generic_doc(meta, url, note)


# ── Vault write ───────────────────────────────────────────────────────────────

def write_captured_doc(title: str, content: str) -> Path:
    """Write the doc to Captured/ and return its Path."""
    CAPTURED_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    filename = f"{today}_{_slug(title)}.md"
    path = CAPTURED_DIR / filename
    if path.exists():
        # Avoid clobbering; append timestamp to filename
        ts = datetime.now().strftime("%H%M%S")
        path = CAPTURED_DIR / f"{today}_{_slug(title)}_{ts}.md"
    atomic_io.vault_write(path, content)
    return path


def link_to_daily_note(title: str, doc_path: Path) -> bool:
    """
    Append a wikilink under DAILY_NOTE_SECTION in today's daily note.
    Creates the section if absent. Returns True on success.
    """
    today = date.today().isoformat()
    note_path = DAILY_NOTES_DIR / f"{today}.md"

    if not note_path.exists():
        print(f"  ⚠️  Daily note not found: {note_path}", file=sys.stderr)
        return False

    # Vault-relative path for wikilink (no .md extension, forward slash)
    rel = doc_path.relative_to(VAULT_ROOT)
    wikilink = f"[[{rel.with_suffix('')}|{title}]]"
    bullet = f"- {wikilink}"

    text = note_path.read_text(encoding="utf-8")

    if DAILY_NOTE_SECTION in text:
        # Insert bullet right after the section heading (before next --- or ##)
        pattern = re.compile(
            rf"({re.escape(DAILY_NOTE_SECTION)}\n)(.*?)(^---$|^## )",
            re.M | re.S,
        )
        def _insert(m):
            existing = m.group(2).rstrip("\n")
            return f"{m.group(1)}{existing}\n{bullet}\n\n{m.group(3)}"

        new_text, count = pattern.subn(_insert, text, count=1)
        if not count:
            # Section exists but nothing after it before EOF
            new_text = text.rstrip("\n") + f"\n{bullet}\n"
    else:
        # No section yet — append one before Quick Links or at EOF
        anchor = "## Quick Links"
        if anchor in text:
            new_text = text.replace(anchor, f"{DAILY_NOTE_SECTION}\n\n{bullet}\n\n---\n\n{anchor}", 1)
        else:
            new_text = text.rstrip("\n") + f"\n\n{DAILY_NOTE_SECTION}\n\n{bullet}\n"

    atomic_io.vault_write(note_path, new_text)
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def capture(url: str, note: str = "") -> dict:
    """Full pipeline: fetch → build doc → write to vault → link in daily note."""
    print(f"  Fetching: {url}")
    meta = fetch_url(url)

    if meta.get("error") and meta.get("type") != "github_repo":
        print(f"  ⚠️  Fetch warning: {meta['error']}", file=sys.stderr)

    title, content = build_doc(meta, url, note)
    doc_path = write_captured_doc(title, content)
    linked = link_to_daily_note(title, doc_path)

    result = {
        "url": url,
        "title": title,
        "doc_path": str(doc_path),
        "linked_to_daily_note": linked,
        "meta": meta,
    }

    print(f"  ✓ Saved: {doc_path.name}")
    print(f"  ✓ Linked in daily note: {linked}")
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Capture a URL into the vault and link it in today's daily note."
    )
    parser.add_argument("url", nargs="?", help="URL to capture")
    parser.add_argument("--note", default="", help="Optional context note to embed in the doc")
    parser.add_argument("--json", action="store_true", help="Print JSON result to stdout")
    args = parser.parse_args()

    if not args.url:
        parser.print_help()
        sys.exit(1)

    result = capture(args.url, note=args.note)

    if args.json:
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
