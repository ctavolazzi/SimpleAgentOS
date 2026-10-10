"""
test_license_boundary.py — the MIT surface must never import the GPL one.

Why this file exists
--------------------
SimpleAgentOS is published under MIT. NovaSystem is published under GPL-3.0.
The plan is that a user can take SimpleAgentOS, use it commercially, and owe
nothing, while NovaSystem is an optional backend whose use is a deliberate
opt-in to copyleft.

That promise rests on one property and nothing else: **SimpleAgentOS must run
standalone.** If it imports NovaSystem, the thing people actually download is a
derivative work of GPL-3.0 code, anyone redistributing the combination has to
comply with GPL-3.0 for the whole of it, and the MIT label stops protecting the
people it was meant to protect. The copyright holder is never in violation of
their own licence, so this failure is invisible from the inside. It lands on
users.

A sentence in a README cannot hold that line. One `import novasystem` added in
a hurry, eighteen months from now, by someone who has never read the README,
silently converts the licensing posture of the whole project. So the boundary
is asserted here instead, where it fails loudly.

The FSF's position on why the arm's length matters: pipes and sockets normally
mean separate programs, but "if the semantics of the communication are intimate
enough, exchanging complex internal data structures, that too could be a basis
to consider the two parts as combined into a larger program." An import is not
arm's length by any reading. A published protocol with a defined schema is the
defensible boundary, which is why the two projects already talk over HTTP.

Measured 2026-09-20: zero files import across the line in either direction.
This file exists to keep that number at zero.

Not legal advice. It encodes a decision, it does not make one.

Run:
    cd _experiments/SimpleAgentOS
    python3 -m pytest tests/test_license_boundary.py -v
"""

import ast
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent

# Package roots that carry a copyleft licence. Importing any of these from
# SimpleAgentOS would pull its licence across the boundary.
COPYLEFT_ROOTS = {"novasystem"}

# Directories that are not part of the shipped MIT surface.
EXCLUDED_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    "archive", "archived", ".pytest_cache", "docs",
}


def python_files(root: Path):
    for p in root.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in p.relative_to(root).parts):
            continue
        yield p


def imported_roots(path: Path):
    """Top-level package names imported by one file.

    AST rather than a regex, so a module named in a comment, a docstring, or a
    string literal does not register as an import. This test must not fire on
    prose that merely discusses the boundary, including the prose above.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        return set()

    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, which cannot reach another package
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def offenders(root: Path, forbidden=COPYLEFT_ROOTS):
    hits = []
    for f in python_files(root):
        bad = imported_roots(f) & forbidden
        if bad:
            hits.append((f.relative_to(root), sorted(bad)))
    return hits


class LicenseBoundaryTests(unittest.TestCase):

    def test_no_module_imports_a_copyleft_package(self):
        hits = offenders(ROOT)
        self.assertEqual(
            hits, [],
            "SimpleAgentOS is MIT and must run standalone. These files import "
            "a GPL-3.0 package, which makes the distributed combination a "
            "derivative work and voids the MIT promise for users:\n  "
            + "\n  ".join(f"{f}: {mods}" for f, mods in hits))

    def test_the_scanner_actually_detects_an_import(self):
        """Negative control.

        Without this, the test above passes just as happily when the scanner is
        broken as when the boundary is intact, and a green tick would mean
        nothing. Same failure shape as a probe that cannot observe the defect
        it is named after.
        """
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "clean.py").write_text("import json\nfrom pathlib import Path\n")
            (d / "violation.py").write_text("import novasystem\n")
            hits = offenders(d)
            self.assertEqual(len(hits), 1, "scanner missed a real import")
            self.assertEqual(hits[0][0], Path("violation.py"))

    def test_the_scanner_detects_a_from_import_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "v.py").write_text("from novasystem.persona import Ledger\n")
            self.assertEqual(len(offenders(d)), 1,
                             "scanner missed 'from novasystem.x import y'")

    def test_the_scanner_ignores_the_word_in_prose(self):
        """Positive control: discussing the boundary must not trip it.

        This file's own docstring names the package repeatedly. A regex-based
        scanner would fail on itself, and the usual response to a check that
        fires on nothing is to switch it off.
        """
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "prose.py").write_text(
                '"""We must never import novasystem here."""\n'
                'NOTE = "novasystem is GPL"\n'
                '# import novasystem\n'
                'import json\n')
            self.assertEqual(offenders(d), [],
                             "scanner fired on prose, not on an import")

    def test_relative_imports_are_not_mistaken_for_the_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "rel.py").write_text("from . import novasystem\n")
            self.assertEqual(offenders(d), [],
                             "a relative import cannot cross a package boundary")

    def test_license_file_exists_and_is_mit(self):
        """A public repo with no LICENSE is all rights reserved.

        SimpleAgentOS was public and unlicensed until 2026-09-20, which meant
        nobody could legally fork or use it. The whole open-source plan rests
        on this file being present.
        """
        lic = ROOT / "LICENSE"
        self.assertTrue(lic.is_file(), "LICENSE is missing; the repo reads as "
                                       "all rights reserved")
        text = lic.read_text()
        self.assertIn("MIT License", text)
        self.assertIn("WITHOUT WARRANTY OF ANY KIND", text)

    def test_declared_dependencies_stay_permissive(self):
        """Runtime dependencies must not drag copyleft in through the back door.

        Listed by name rather than resolved at runtime so this stays a fast,
        offline check. Adding a dependency is the moment to think about its
        licence, so a new name failing here is the intended behaviour.
        """
        req = ROOT / "requirements.txt"
        if not req.is_file():
            self.skipTest("no requirements.txt")
        known_permissive = {
            "fastapi", "uvicorn", "httpx", "pyyaml", "requests",
            "pytest", "anthropic", "arxiv", "feedparser", "python-dateutil",
            "rich", "typer", "jinja2", "pydantic", "starlette", "markdown",
        }
        unknown = []
        for line in req.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = line.split("[")[0]
            for sep in (">=", "<=", "==", "~=", ">", "<", "!="):
                name = name.split(sep)[0]
            name = name.strip().lower()
            if name and name not in known_permissive:
                unknown.append(name)
        self.assertEqual(
            unknown, [],
            "new dependencies are not on the reviewed-permissive list. Check "
            f"each licence, then add it here if it is compatible: {unknown}")


if __name__ == "__main__":
    unittest.main()
