# SimpleAgent code atlas

Canonical data: `graph.json`. Presentation: `template.html`. Traversal and publishing: `map.py`.

The generated HTML is self-contained, requires no server or network, and embeds exactly the canonical graph in `script#graph-data`. It lives in the vault at `Research/2026-09-20_simpleagent-map.html`, linked from the daily note. Do not edit the generated HTML directly.

From SimpleAgentOS root:

```sh
python3 docs/simpleagent-map/map.py list
python3 docs/simpleagent-map/map.py show journal
python3 docs/simpleagent-map/map.py walk journal --depth 2 --direction both
python3 docs/simpleagent-map/map.py validate
python3 docs/simpleagent-map/map.py check-sources
python3 docs/simpleagent-map/map.py build
```

`show`, `walk`, and `list` emit JSON. Walk is breadth-first, bounded by depth, and cycle-safe. Edges point from source to target; `kind` distinguishes observed, proposed, and unresolved relationships. A relationship is not necessarily a runtime dependency. Read its label.

To update:

1. Read the linked report and inspect affected source files.
2. Edit node summaries, detail, evidence, and relationships in `graph.json`. Keep existing IDs stable for durable links. Add new IDs rather than reusing an old ID for a different entity.
3. Use `observed` for source inspection, `reproduced` only for behavior actually demonstrated, `proposed` for design, and `unverified` for unresolved claims. Update the snapshot date. Do not mark proposed components implemented merely because a file exists.
4. Set evidence paths, line numbers, and SHA-256 hashes from inspected files. `check-sources` detects changes and missing paths; it cannot judge whether a conclusion remains valid. Do not refresh hashes without reviewing the changes.
5. Place every new node in at least one view's `positions`, using a 900 by 620 canvas and 230 by 86 nodes. Prefer at most nine nodes per view. Update or add directed edges.
6. Run validation and build. Open the HTML, test navigation and search, and update the daily note with what changed. Vault output uses the shared lock and atomic writer.

Deep links use `#view=findings&node=sync-race`. The interface supports browser history, keyboard node activation, search, status filtering, evidence paths, graph export, and connected-node navigation. The JSON can also be extracted from the HTML if the source checkout is unavailable.

Local absolute evidence paths are machine-specific. Moving the map alone preserves the graph but may break source links. These paths and hashes are intentional metadata for agent traversal. No credentials or source-file contents are embedded.

Run the offline browser smoke check with `python3 docs/simpleagent-map/check.py` after building. It needs an already installed Playwright Chromium. No live model or database is used.
