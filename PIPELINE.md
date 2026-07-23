# Daily Note Harness — Pipeline Reference

**The one document that explains, end to end, how a daily note comes into being,
gets filled, gets closed, and gets verified.** If you touch any harness file,
read this first. If the API ever feels ambiguous, this is the source of truth.

Last updated 2026-07-10 (added the integrity layer: `frontmatter.validate` link
checks + `wheel_check.py` + wrap-up Phase 5).

---

## 0. Mental model

A single day is not one file. It is a **wagonwheel** of connected files:

```
                 00.00_vault_index          ← the hub of the whole vault
                        ▲ parent
                        │
        ┌───────────────┴───────────────┐
        │     Daily Notes/<date>.md      │   ← THE ENTRY POINT
        │  (hero, weather, news, arxiv,  │
        │   sitrep, in-the-lab, commits, │
        │   tomorrow's-top-3, recap …)   │
        └───┬─────────┬─────────┬────────┘
   hub:     │  journal:│  plan:  │  wagonwheel:
            ▼         ▼         ▼         ▼
   Hubs/<date>_hub   Claude    Plans/<date>   (points at the hub —
        │            Journal/  _daily_plan     the crystallized state)
        │            <date>
        │ spokes: (frontmatter list)
        ▼
   [[StS-Complete-Draft]] · [[SOURCE-MANIFEST]] · [[…evolution]] · …
        ▲ each spoke carries  hub: [[Hubs/<date>_hub]]  (the RIM — reciprocity)
```

Four **invariants** must always hold. The integrity layer (§5) exists to enforce them:

1. **Every daily note has a `hub:` link** to a hub file that exists. A note with
   no hub is an unreachable continuation brief. (`hub` and `journal` are
   `required` in the frontmatter schema.)
2. **Every frontmatter link resolves** — `hub`, `journal`, `plan`, `wagonwheel`,
   `parent` point at real files, not dangling names, and are proper `[[wikilinks]]`.
3. **Reciprocity (the rim):** every spoke listed in a hub's `spokes:` carries a
   `hub:` back-link to that same hub.
4. **The parent chain is walkable:** `parent:` from the daily note reaches
   `00.00_vault_index` with no broken hop or cycle.

---

## 1. The data model & the core API — `daily_note.py`

The lowest layer. Everything else calls into it.

### 1.1 Paths & the section registry
- `VAULT_DIR = ~/Documents/Personal-Remote-Vault`
- `DAILY_NOTES_DIR = VAULT_DIR/"Daily Notes"`, note path = `<date>.md`.
- `SECTIONS` — the canonical dict mapping a **section key** →
  its **markdown header** (e.g. `"research_feed" → "## Research Feed"`). This is
  the registry every reader/writer keys off. Legacy headers are kept for
  backward-compat with old notes.

### 1.2 Reading
- `read_full(date)` — whole note as string (raises if absent).
- `read_section(key, date)` / `read_all_sections(date)` — body between a header
  and the next header, via `_extract_section`.
- `section_status(date)` — per section, one of:
  - `filled` — real content,
  - `empty` — nothing after boilerplate strip,
  - `template` — placeholder-only (`_is_template_only`),
  - `absent` — the header isn't even in the note.

### 1.3 Cross-day continuity (gap-tolerant)
- `most_recent_note()` — scans the directory and returns the latest note strictly
  before today. **No lookback horizon** — a 3-day, month, or year gap all resolve
  to the actual last note (this was the 0.0.2 hardening fix; do not reintroduce a
  fixed "yesterday" probe).
- `read_yesterday(section)` / `last_handoff()` — build on it.

### 1.4 Writing — `write_section(key, content, actor, mode, date)`
The single mutation path for note bodies. Guarantees:
- **Permission model:** `actor` ∈ {claude, gemma, waft-daemon, cron, user}.
  AI actors may only write `AI_WRITABLE` sections; Gemma is further restricted to
  `GEMMA_WRITABLE`. Violations raise `PermissionError`.
- **Self-heal:** if the section header is missing (legacy/trimmed note), the
  section is **appended** rather than the write being silently dropped.
- **No-op guard:** if the computed new text equals the old and the content
  differs from what's there, it **raises** instead of reporting a false success.
- **Atomicity:** writes go through `atomic_io.vault_lock()` + `atomic_write`.
- `mode="append"` adds an attributed `**[actor @ HH:MM]**` entry (used by
  `append_session_log`); `mode="replace"` overwrites the section body.

### 1.5 Creating — `create_from_template(date)`
Headless Templater renderer. Reads the vault's `Daily_Note_Template.md` and
substitutes `<% tp.date.now("FMT"[, offset]) %>` for the tokens the template
uses (`YYYY MM DD dddd MMMM Do`). No-op if the note already exists;
raises `FileNotFoundError` if the template is missing. Because the template now
carries `hub:`/`wagonwheel:`, a note born this way is hub-linked from birth.

### 1.6 Frontmatter — `_write_frontmatter` / (via) `frontmatter.py`
Body writes never touch the YAML block; frontmatter has its own module (§4).

---

## 2. Concurrency — `atomic_io.py`

Every mutation of a shared vault file is serialized. Three primitives:
- `vault_lock()` — a `portalocker` flock at `VAULT_DIR/.vault_daily_note.lock`,
  8s timeout. All mutating processes (spin_up, wrap_up, daily_note, we_factory,
  the docs-maintainer MCP) acquire it first.
- `atomic_write(path, content)` — temp-file → `fsync` → `os.replace` (atomic on
  POSIX; readers see old or new, never partial).
- `vault_write(path, content)` — lock + atomic_write in one call (**preferred**
  for single-shot writes).
- `DelayedKeyboardInterrupt` — defers Ctrl-C until the critical section ends, so
  a lock is never abandoned mid-write.

Rule: **never** `path.write_text()` a vault file directly. Use `vault_write`.
(The 0.0.2 pass retrofitted 11 raw writes across 7 modules for exactly this.)

---

## 3. The morning pipeline — `spin_up.py` (6 phases)

`python3 spin_up.py [--force] [--dry-run]`. Orchestrates gather → fill → scaffold.

- **Phase 1 — Gather** (network/IO, all cached with TTLs, all failure-tolerant):
  weather, local news, arXiv dual-pane, music pick, git scan, WAFT state,
  we_factory quests, horoscope, daily image, image quip. Each returns
  `(data, status)`; a failure degrades to cache or a placeholder, never aborts.
- **Phase 2 — Fill note.** If the note is missing, `create_from_template()`
  (abort if that fails — nothing to fill). Then write each section via
  `write_section(actor="claude")`: hero_image, daily_reading, location,
  research_feed, sitrep, work_efforts, waft_workspace. Each writer **skips if the
  section is already `filled`** (unless `--force`).
  - `research_feed` guards a degraded arXiv digest (missing `physics`/`ai` or
    `papers`/`categories` keys) and writes an honest "unavailable" placeholder
    instead of raising `KeyError` — the 0.0.3 fix.
- **Phase 3 — Daily plan.** Create `Plans/<date>_daily_plan.md` if missing,
  seeded from yesterday's unchecked rollover (`daily_plan.py` / `plan_rollover.py`).
- **Phase 4 — Frontmatter sync.** Populate `code_refs` with the harness modules;
  set `focus` default.
- **Phase 5 — Vault scaffold + link wiring.** Create the **Journal** and **Hub**
  for today if missing (`_scaffold_journal`, `_scaffold_hub`; hub's `axle:` wired
  to the previous hub via `_find_prev_hub`). Then the **code backstop**: for
  `hub`, `wagonwheel`, `journal`, `plan`, set the frontmatter field if empty *or*
  not a proper wikilink — so even a non-template note ends hub-linked. Finally
  `frontmatter.validate()` runs **fail-loud**, printing any dangling/missing link.
- **Phase 6 — Sitrep generation** from hub + plan + journal (`sitrep_gen.py`).

Ordering matters: the note exists by end of Phase 2, the plan by Phase 3, the hub
by Phase 5 — so the Phase-5 wiring can point the note at files that now exist.

---

## 4. Frontmatter — `frontmatter.py`

List-aware YAML read/write that never clobbers sibling fields.

- `read_fm` / `write_fm` / `get_field` / `set_field` / `add_to_list` / `remove_from_list`.
- **`SCHEMA`** — the field contract. Key points:
  - `type`, `date`, `parent`, `journal`, `hub` are **required**.
  - `parent`, `plan`, `journal`, `hub`, `wagonwheel` are **`link: True`** —
    validated for target existence.
  - the list field is **`work_efforts_touched`** (was mis-named `work_efforts`;
    fixed 2026-07-10 so `sync` can't inject a bogus duplicate).
- **`validate(date, check_links=True)`** → list of issues. Checks required-field
  presence, list types, date format, **and** that every `link:True` field is a
  wikilink whose target resolves on disk (`_resolve_wikilink`, which accepts both
  pathed `Hubs/<date>_hub` and bare basenames the way Obsidian does).
- `sync_defaults` — fills missing fields from schema defaults; never overwrites.
- CLI: `python3 frontmatter.py {status|get|set|add|remove|validate|sync}`.

---

## 5. The integrity layer — `wheel_check.py` (the guard)

The thing that makes "files got dropped" impossible to miss. Importable
(`wheel_check.check(date) -> Result`) and a fail-loud CLI (`/wheel-check`).

Five checks, each ERROR (wheel broken) or WARN (worth a look):
1. **Frontmatter** — delegates to `frontmatter.validate`: required fields + link
   resolution on the daily note.
2. **Containers** — Hub and Journal exist, contain no surviving spin-up
   placeholder text (`To be populated`, `(none yet)`, …), and the Journal's
   `## Notes` is non-empty.
3. **Reciprocity** — each spoke in the hub's `spokes:` exists and carries a
   `hub:` back-link to today's hub.
4. **Parent chain** — walk `parent:` from the daily note to `00.00_vault_index`;
   flag a dangling hop or a cycle.
5. **Orphans** — vault `.md` modified on this date with **0** inbound wikilinks
   (WARN; entry/container files are exempt).

CLI exit code is **1** on any ERROR, else 0 — so a commit/backup can gate on it.
`wrap_up.py` **Phase 5** calls `wheel_check.check(today)` automatically and prints
the result loudly; a broken wheel is written into the wrap-up journal entry too.

---

## 6. The evening pipeline — `wrap_up.py` (5 phases)

`python3 wrap_up.py [--dry-run] [--no-backup] [--force]`.

- **Phase 1 — Gather:** commits since midnight (`commit_summary`), note section
  state, dirty-repo list.
- **Phase 2 — Write:** `commits_today` (**always refreshed** — it's the EOD tally,
  not a morning provisional; 0.0.3 fix), `tomorrows_top_3` (carries yesterday's
  unchecked items + dirty repos), `eod_summary`/`session_recap`; WAFT session-end
  journal entry + quest fitness.
- **Phase 3 — Lock plan:** `daily_plan.lock()` freezes the plan and computes the
  rollover for tomorrow.
- **Phase 4 — Backup:** `tools/vault-backup.sh` if present (Phase-0 stub today).
- **Phase 5 — Wheel integrity:** `wheel_check.check(today)`, fail-loud. Falls back
  to the narrow sibling-file guard if the module can't import.
- Then one consolidated `append_session_log` entry (records skipped sections,
  dirty repos, and any wheel errors/warnings).

---

## 7. Command surface

| Command | Runs | Purpose |
|--------|------|---------|
| `/spin-up` | `spin_up.py` | Boot the day: gather + fill + scaffold + wire links |
| `/wrap-up` | `wrap_up.py` | Close the day: commits, tomorrow seed, lock, backup, **wheel check** |
| `/wagonwheel` | (skill) | Crystallize session state into hub + spokes (fills the wheel) |
| `/wheel-check` | `wheel_check.py` | Verify the wheel is fully wired (§5) |
| `frontmatter.py validate` | — | Frontmatter-only subset (schema + link resolution) |
| `/daily-note-os-check` | (skill) | Audit workflow compliance + Hub/Journal completeness |

**Golden path:** `/spin-up` in the morning → work (wikilink every artifact) →
`/wagonwheel` when a stretch is worth crystallizing → `/wrap-up` at night, which
runs `/wheel-check` for you. If anything is ever in doubt, `python3 wheel_check.py`.

---

## 8. If you change the harness

- Adding a frontmatter field that links somewhere → add it to `SCHEMA` with
  `link: True` (and `required` if a note is invalid without it). That single edit
  makes `validate` and `wheel_check` enforce it everywhere.
- Adding a section → add it to `SECTIONS` (and `AI_WRITABLE` if a writer fills it).
- Adding a spoke type → make sure whatever creates it writes
  `hub: [[Hubs/<date>_hub]]` so reciprocity holds, and wikilink it from the note.
- Never write a vault file except through `atomic_io.vault_write`.
- Run `python3 -m pytest tests/test_daily_note_hardening.py tests/test_wheel_check.py`
  after any change to this layer.
