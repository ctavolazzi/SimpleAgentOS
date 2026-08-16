"""
shell_audit.py — what is actually inside the Bash calls in the Live Feed.

"62% of tool calls are Bash" is only interesting once you know whether those
calls needed a shell. This splits every Bash command into its constituent
commands and sorts them into three groups:

    ceremony   `echo` separators and `cd` preamble. Scaffolding, not work.
               The byproduct of batching many probes into one call.
    covered    Work a dedicated tool already does directly (Read, Grep, Glob).
               This is the real gap.
    shell      git, python, node, curl, process management, the empirica CLI.
               No tool equivalent exists. Correct usage.

Measured over the first 8 days of the archive: ceremony 44%, covered 15%,
shell 40%. So the headline number is mostly not "reached for the wrong tool".

Used two ways:
    import shell_audit; shell_audit.analyze(details)   # live_feed_viewer panel
    python3 shell_audit.py                             # standalone report

Related: live_feed_viewer.py (the panel), ~/.claude/hooks/prefer-tools.sh
(the nudge that fires at call time), live_feed.py (writes the archive).
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

FEED_ROOT = (Path.home() / "Documents/Personal-Remote-Vault"
             / "System/40-49_telemetry/live_feed")

# Ordered: first match wins.
BUCKETS = [
    ("git-read",    r'^git\s+(status|log|diff|show|blame|rev-parse|rev-list|for-each-ref|branch|ls-files|remote|fetch|stash\s+list|config\s+--get|describe|shortlog|count-objects|fsck|notes\s+show)'),
    ("git-write",   r'^git\s+(commit|push|add|checkout|switch|merge|rebase|reset|stash(?!\s+list)|tag|init|clone|restore|cherry-pick|notes\s+add)'),
    ("file-read",   r'^(cat|head|tail|less|bat|wc|nl|stat|file|tree|column|cut|sort|uniq|tr|comm|diff|xxd|od|strings)\b'),
    ("search",      r'^(grep|rg|egrep|fgrep|find|fd|ag|ast-grep|sg)\b'),
    ("listing",     r'^(ls|du|df)\b'),
    ("structured",  r'^(jq|yq|gron|plutil)\b'),
    ("py-inline",   r'^python3?(\.\d+)?\s+(-c|-)\s'),
    ("py-inline",   r'^python3?(\.\d+)?\s+-\s*<<'),
    ("py-script",   r'^python3?(\.\d+)?\s+[\w./~-]+\.py'),
    ("py-module",   r'^python3?(\.\d+)?\s+-m\b'),
    ("node-npm",    r'^(node|npm|npx|pnpm|yarn|bun|deno|tsc|vite)\b'),
    ("network",     r'^(curl|wget|nc|ping|dig|host|nslookup|ssh|scp|rsync)\b'),
    ("browser",     r'^("?/Applications/Google Chrome|chrome|chromium|open)\b'),
    ("process",     r'^(ps|pgrep|pkill|kill|lsof|nohup|sleep|jobs|wait|trap|timeout)\b'),
    ("echo",        r'^(echo|printf)\b'),
    ("navigate",    r'^cd\b'),
    ("mutate-fs",   r'^(rm|mv|cp|mkdir|touch|chmod|chown|ln|tee)\b'),
    ("pkg",         r'^(pip3?|brew|apt|gem|cargo|go)\b'),
    ("empirica",    r'^empirica\b'),
    ("gh",          r'^gh\b'),
    ("sqlite",      r'^sqlite3?\b'),
    ("shell-logic", r'^(for|while|until|if|then|else|elif|fi|do|done|case|esac|export|source|\.|set|eval|declare|local|read|shift)\b'),
    ("stream-edit", r'^(sed|awk|perl)\b'),
]

CEREMONY = {"echo", "navigate"}
COVERED = {"file-read": "Read", "search": "Grep / Glob", "listing": "Glob"}

GROUP_LABELS = {
    "ceremony": "Ceremony",
    "covered": "Tool-covered",
    "shell": "Shell-only",
}
GROUP_ORDER = ["ceremony", "covered", "shell"]


def group_of(bucket_name: str) -> str:
    if bucket_name in CEREMONY:
        return "ceremony"
    if bucket_name in COVERED:
        return "covered"
    return "shell"


def split_cmds(detail: str) -> list:
    """Split one compound command line into its head commands.

    Splits on ; && || but not inside quotes, and strips heredoc BODIES first:
    an embedded Python or JS program is one argument to one command, and
    counting its lines invents `const`/`await`/`do` buckets out of nothing.
    That bug produced a visibly wrong first run of this audit, hence the two
    passes below (terminator on its own line, then the collapsed form).
    """
    detail = re.sub(r'<<-?\s*[\'"]?(\w+)[\'"]?\n.*?^\s*\1\s*$', ' HEREDOC', detail,
                    flags=re.S | re.M)
    detail = re.sub(r'<<-?\s*[\'"]?(\w+)[\'"]?.*?\b\1\b', ' HEREDOC', detail, flags=re.S)

    parts, buf, quote, i = [], [], None, 0
    while i < len(detail):
        c = detail[i]
        if quote:
            if c == quote and detail[i - 1] != "\\":
                quote = None
            buf.append(c)
        elif c in "\"'":
            quote = c
            buf.append(c)
        elif c == ";" or detail[i:i + 2] in ("&&", "||"):
            parts.append("".join(buf))
            buf = []
            i += 2 if detail[i:i + 2] in ("&&", "||") else 1
            continue
        elif c == "|":
            # A pipe means the head command still leads; keep only the head.
            parts.append("".join(buf))
            buf = []
            while i < len(detail) and detail[i] not in ";&\n":
                i += 1
            continue
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))

    out = []
    for p in parts:
        p = p.strip().lstrip("(){} \t")
        p = re.sub(r'^\S+=\S+\s+', '', p)          # strip VAR=x prefixes
        if p:
            out.append(p)
    return out


def bucket(cmd: str) -> str:
    for name, pat in BUCKETS:
        if re.match(pat, cmd):
            return name
    return "other"


def analyze(details) -> dict:
    """Bucket and group every command inside an iterable of Bash detail strings."""
    buckets = Counter()
    groups = Counter()
    calls = 0
    single = 0
    for detail in details:
        calls += 1
        pieces = split_cmds(detail)
        if len(pieces) <= 1:
            single += 1
        for p in pieces:
            name = bucket(p)
            buckets[name] += 1
            groups[group_of(name)] += 1
    total = sum(buckets.values())
    return {
        "calls": calls,
        "single": single,
        "commands": total,
        "groups": {g: groups.get(g, 0) for g in GROUP_ORDER},
        "buckets": dict(buckets.most_common(14)),
        "ceremonyShare": (groups.get("ceremony", 0) / total) if total else 0.0,
        "coveredShare": (groups.get("covered", 0) / total) if total else 0.0,
    }


# ── Standalone report ──────────────────────────────────────────────────

def _load_days():
    days = {}
    for path in sorted(FEED_ROOT.glob("*/*.jsonl")):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', path.stem):
            continue
        bash, total = [], 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or not row.get("kind"):
                continue
            total += 1
            if row["kind"] == "Bash":
                bash.append(str(row.get("detail") or ""))
        if total:
            days[path.stem] = (bash, total)
    return days


def main(argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="What is inside the Bash calls")
    ap.add_argument("--date", help="one day (YYYY-MM-DD); default is all days")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    days = _load_days()
    if args.date:
        days = {k: v for k, v in days.items() if k == args.date}
    if not days:
        print("no feed data", file=sys.stderr)
        return 1

    all_bash = [d for bash, _ in days.values() for d in bash]
    rows_total = sum(t for _, t in days.values())
    result = analyze(all_bash)
    result["bashShare"] = result["calls"] / rows_total if rows_total else 0.0
    result["rows"] = rows_total
    result["perDay"] = {}
    for date, (bash, total) in days.items():
        a = analyze(bash)
        result["perDay"][date] = {
            "bashShare": a["calls"] / total if total else 0.0,
            "ceremonyShare": a["ceremonyShare"],
            "coveredShare": a["coveredShare"],
        }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    r = result
    print(f"rows total      : {r['rows']:,}")
    print(f"Bash calls      : {r['calls']:,}  ({r['bashShare']:.1%} of all tool calls)")
    print(f"commands inside : {r['commands']:,}  ({r['commands'] / max(r['calls'], 1):.1f} per call)")
    print(f"single-command  : {r['single'] / max(r['calls'], 1):.1%} of Bash calls")
    print()
    for g in GROUP_ORDER:
        n = r["groups"][g]
        print(f"  {GROUP_LABELS[g]:<14}{n:>8,}{n / max(r['commands'], 1):>8.1%}")
    print()
    print(f"  {'bucket':<14}{'commands':>9}{'share':>8}   covered by")
    print("  " + "-" * 52)
    for name, n in r["buckets"].items():
        print(f"  {name:<14}{n:>9,}{n / max(r['commands'], 1):>8.1%}   {COVERED.get(name, '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
