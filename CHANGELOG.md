# Changelog

## [0.3.0] — 2026-07-25

### Changed
- **Daily note layout: work on top.** The template now puts the agent's work
  block (Live Feed, Work Efforts, In the Lab, Commits Today, Claude Code
  Session Log) directly under the hero image, above Daily Reading. The context
  block (Daily Reading, Location, Sitrep, Research Feed, Idea Dump) moved below
  it. Rationale: the top of the note is the part you can see without scrolling,
  so it should show what is happening now, not the morning's horoscope. Makes
  the note screen-recordable as a live work window.
- `daily_note.SECTIONS` reordered to match, with `live_feed` added and marked
  AI-writable.
- `spin_up._write_daily_reading` no longer injects a missing Daily Reading
  section under `<!-- /hero_image -->`. That space belongs to the work block
  now, so it anchors above `## Location` instead, and appends rather than
  dropping the write when no anchor exists.

### Added
- **`live_feed.py`** — real-time activity feed for the Live Feed section.
  Records one JSONL row per agent tool call and rebuilds the section from that
  file (full replace, never append, so double fires and races converge). Runs
  of consecutive read-only tools collapse into a single counted row; the window
  holds 14 rows and reports how many were trimmed. Supports `--note` for a
  manual entry and `--focus` for a "Now:" headline. Rows carry a short session
  id: several Claude Code tabs share one feed, so when more than one is in the
  window each row is tagged and the header counts the tabs. Runs from different
  sessions never collapse into each other.
- **`~/.claude/hooks/live-feed.sh`** — PostToolUse hook (record) and Stop hook
  (flush). The record path is pure bash + jq so it costs milliseconds; the
  render runs detached and debounced, so no tool call waits on a ~650ms Python
  startup. Stale render locks are cleared on the next call and on flush.
- **`migrate_note_layout.py`** — reorders notes already on disk to match the
  template. Content preserving (verifies every section body is byte-identical
  before writing), idempotent, dry run by default. `--all` migrates the vault.
- **`session_audit.py`** — scans for loose ends a session left behind: template
  and registry drift, notes still on the old layout, headings swallowed by a
  callout, hook install and jq availability, a Live Feed section behind its
  feed, stale render locks, work done but never written to the session log,
  changed files the note never references, uncommitted trees, strays, tests.
  `--since HH:MM` scopes file-level checks to one session, because several
  agents share these repos and another tab's dirty tree is not this session's
  loose end. Exit code 1 on any failure, so it can gate a wrap-up.
- `tests/test_live_feed_layout.py` — 29 tests covering section splitting
  (including headings inside code fences), separator normalization, ordering
  rules, reassembly idempotence, separator preservation, and live feed row
  formatting.

### Fixed
- **`_replace_section` no longer eats the separator between sections.** A
  replace-mode write left the new body butted against the next `## ` header.
  When the body's last line is a blockquote — every callout-shaped section,
  and Live Feed always — markdown lazy continuation absorbed the following
  heading INTO the callout, collapsing the rest of the note into one block.
  Same failure `_join_appended` fixed on the append path, from the other
  direction. The trailing rule is now captured and re-emitted, and a section
  with no trailer at all gets a blank line so the heading still escapes.
  The `write_section` no-op guard compares content against content instead of
  against the body-plus-trailer, so an identical rewrite is not misreported as
  a silent failure.
- `migrate_note_layout` now strips ALL trailing rules at a boundary, not one.
  Stripping one could not heal a note that already had `---\n\n---`: it removed
  a rule, the rejoin added one back, and the doubled rule looked like a stable
  state on every subsequent pass.

## [0.2.0] — 2026-04-05

### Added
- **Self-Explorer**: OODA-loop agent that reads its own source code and journals about its architecture
- **Real-time dashboard** (`self_explore.html`): 3-column terminal UI with SSE streaming
- **SSE endpoint** (`/api/explorer/stream`): Server-pushed journal updates every 500ms
- **Warmup query**: Primes Gemma4 on startup so first real tokens arrive faster
- **Prefill timer**: Shows elapsed seconds while waiting for CPU inference
- **Copy session button**: One-click clipboard export of full journal
- **Connection indicator**: Green/red dot shows server connectivity
- **Stop actually works**: Cancels in-flight HTTP requests via `task.cancel()`
- **Direct query panel**: Ask Gemma4 anything, streaming response
- `requirements.txt` for Python dependencies
- `tests/test_smoke.py` — pre-flight checks (deps, files, ports, binaries)
- `tests/test_explorer.py` — unit tests for SelfExplorer (no LLM needed)
- `.gitignore` for `__pycache__`, `node_modules`, `pocketbase`, `.self_explorer/`

### Architecture
```
Browser (:1010) --SSE--> self_explore_server.py --HTTP--> llama-server (:8080)
```
- FastAPI serves HTML + API on port 1010
- llama-server runs Gemma-4 E4B (Q4_K_M, 5GB) on CPU
- `GGML_METAL=0` disables Metal GPU (required for Intel Iris Pro)
- OODA loop: Observe (read file) → Orient (analyze via LLM) → Decide (next file) → Act (journal)

## [0.1.0] — 2026-04-04

### Added
- Original SimpleAgentOS: React HUD + PocketBase + llama.cpp
- `rebuild_AgentOS.py`: System architect/builder
- `build_os.py`: Alternative builder
- `seed_engine.py`: Test data injection
- `core_engine/`: React frontend + PocketBase backend + Makefile
- `core_engine/nerve_center.py`: FastAPI relay to llama-server
