"""
yaml_io.py — Round-trip YAML frontmatter manipulation for Obsidian markdown.

Solves two problems with python-frontmatter / PyYAML:
  1. Standard parsers strip comments, normalize flow-style sequences,
     reorder keys, and erase formatting on every load/dump cycle.
  2. Markdown bodies frequently contain lines that look like YAML
     (bullets, --- horizontal rules), confusing multi-document parsers.

This module:
  - Uses ruamel.yaml in RoundTrip mode (CommentedMap AST preserves
    comments, anchors, quote styles, key order).
  - Bifurcates the markdown file into (frontmatter, body) along the
    first '\\n---' delimiter and never feeds body text to the YAML
    parser.

Public API:
  - parse(text)              -> (CommentedMap | None, body_str)
  - serialize(fm, body)      -> reconstructed markdown string
  - update_fields(text, kv)  -> new markdown with fields merged into fm
"""

from __future__ import annotations

import io
from typing import Optional, Tuple

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


def _yaml() -> YAML:
    """Build a YAML instance configured for round-trip preservation."""
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096  # avoid line-wrapping long values
    y.allow_unicode = True
    return y


def parse(text: str) -> Tuple[Optional[CommentedMap], str]:
    """Bifurcate markdown into (frontmatter_map, body_text).

    A document without a frontmatter block returns (None, text).
    The frontmatter block is detected only when the file STARTS with
    '---\\n'. The closing delimiter is the first '\\n---' that follows.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None, text

    # Strip the opening fence
    after_open = text[4:] if text.startswith("---\n") else text[5:]

    # Find closing fence: '\n---' followed by newline or EOF
    end_idx = -1
    search_from = 0
    while True:
        idx = after_open.find("\n---", search_from)
        if idx == -1:
            break
        # Confirm it's a line by itself: next char after '---' is \n or EOF
        tail_pos = idx + 4
        if tail_pos == len(after_open) or after_open[tail_pos] in ("\n", "\r"):
            end_idx = idx
            break
        search_from = idx + 1

    if end_idx == -1:
        # No closing fence — treat whole file as body, leave frontmatter None
        return None, text

    fm_text = after_open[:end_idx]
    body_start = end_idx + 4  # past '\n---'
    if body_start < len(after_open) and after_open[body_start] == "\n":
        body_start += 1
    body_text = after_open[body_start:]

    fm_map = _yaml().load(fm_text) if fm_text.strip() else CommentedMap()
    if fm_map is None:
        fm_map = CommentedMap()
    return fm_map, body_text


def serialize(fm: Optional[CommentedMap], body: str) -> str:
    """Recombine (frontmatter, body) into a markdown string.

    If fm is None, returns body unchanged.
    """
    if fm is None:
        return body

    buf = io.StringIO()
    _yaml().dump(fm, buf)
    fm_text = buf.getvalue()
    # ruamel may emit a trailing newline; we want exactly one before the close fence
    fm_text = fm_text.rstrip("\n") + "\n"

    return f"---\n{fm_text}---\n{body}"


def update_fields(text: str, fields: dict) -> str:
    """Merge `fields` into the frontmatter of `text` and return new markdown.

    Preserves all existing keys, comments, anchors, formatting. Adds new
    keys at the end of the map (ruamel's CommentedMap honors insertion
    order and existing position metadata).

    If the document has no frontmatter, a new block is prepended.
    """
    fm, body = parse(text)
    if fm is None:
        fm = CommentedMap()
        body = text

    for key, value in fields.items():
        fm[key] = value

    return serialize(fm, body)
