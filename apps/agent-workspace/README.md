# Fieldwork

A local browser workspace for observing agents and preserving their journals.

## Run Fieldwork

```sh
python3 apps/agent-workspace/server.py
open http://127.0.0.1:8787
```

Python standard library only at runtime. No build step, CDN, model credentials, or external service. Default database: `~/Library/Application Support/SimpleAgentWorkspace/workspace.sqlite3`. Override with `--db PATH`; use `--port` for a different local port. Keep one server process per database.

Create a workspace or open a saved workspace. Follow or skip the introduction, create an agent with a name and personality, and choose its explicit local behavior. Select an agent to inspect its journal. Step advances one tick while paused; Resume runs at 1x, 2x or 4x until paused or the controlling browser stops heartbeating for five seconds. Reopening in the controlling tab pauses execution. Observer tabs do not renew the lease or pause another tab; Take control explicitly transfers ownership. Changes save automatically. Save checkpoint captures state and journal together; restoring creates a new workspace branch and preserves the original.

## Three checkpoints

1. **Available:** saved workspaces, independent tutorial profile, agent creation and persona revisions, pixel world, local exploration, append-only journals, transactional checkpoints and branch restore.
2. **Available offline slice:** scout/engineer/gardener simulation, field-radio knowledge exchange, evidence-dependent planning, repairs, watering, harvesting, energy and meals, weather interventions, causal trace inspection, controller ownership, speed control and saved branch continuation. Portable journal swaps and provider jobs remain later work.
3. **Artwork available; evolution later:** Aseprite sources and bundled PixelLab assets now ship. Versioned proposed policy changes, evaluation and rollback remain future work. Executable self-modification is not implemented.

Personality prose is retained as authored. Current movement is driven by the selected rule, not interpretation by a language model. No model or PixelLab calls occur at runtime. PixelLab was used explicitly during checkpoint 2 asset production. Agent deletion and journal editing are intentionally absent from this application checkpoint; the earlier isolated browser CRUD fixture remains separate.

## Structure

- `store.py`: SQLite repository, controller commands, identity, journal and checkpoints.
- `server.py`: local HTTP hub, single-process command lock and simulation lease.
- `web/app.js`: forms, saved state, tutorial and inspector.
- `web/world.js`: replaceable pixel renderer; owns no authoritative world state.
- `tests/test_workspace.py`: persistence and real Chromium browser tests.
- `proof/`: desktop/mobile screenshots and local demonstration URL.

The application is isolated from NovaSystem infrastructure pending an explicit dependency decision. Journals currently live in SQLite, not portable ledger files. The interface displays the most recent 100 events overall and up to 20 for the selected agent; older entries remain in storage. The full memory inspector adds per-agent search and cursor pagination beyond the compact sidebar. Import/export, multi-process writers, and schema migration tooling remain future work.

## Verification

With Playwright and its Chromium installed in the test environment:

```sh
python3 -m unittest discover -s apps/agent-workspace/tests -v
```

Eight tests cover tutorial flows, mobile layout, save/reload/restore, live stepping and pausing, journal immutability, failed transaction rollback, and persona identity. The `WORKSPACE_MUTATION=drop-journal` negative control intentionally damages only an isolated test checkpoint. Its persistence assertion was observed failing, then the normal suite passed.

## Nova Process checkpoint review

- **DCE:** the smallest complete vertical slice is create, observe, persist, restore. Keep rendering separate from state.
- **CAE:** a moving sprite is not intelligent behavior. The interface identifies local rules, recorded observations, and future provider work explicitly.
- **Persistence review:** copy journal and state in one transaction. Restore into a new branch instead of overwriting history. Test rollback and missing-journal failure.
- **Interaction review:** persisted tutorial completion belongs to the local user profile, independently of workspace saves.
- **Unresolved:** portable ledger bindings and a provider job contract must precede claims of hot swapping or autonomous evolution.

These are review perspectives applied by one implementation session, not independent agent votes.

## Aseprite drawing controls

Verified locally on 2026-09-20: `/usr/local/bin/aseprite`, Aseprite 1.3.17.2. Application at `/Applications/Aseprite.app`.

Existing MCP registration lives in `~/.claude.json` under `mcpServers.aseprite`. It invokes `/Users/ctavolazzi/.local/bin/uv --directory /Users/ctavolazzi/Code/.mcp-servers/aseprite-mcp run -m aseprite_mcp`. This session does not expose that MCP as a callable tool, but its source and batch controls are available.

Local references:

- `/Users/ctavolazzi/Code/.mcp-servers/aseprite-mcp/README.md`: tool catalog, drawing, animation, palettes and exports.
- `aseprite_mcp/core/commands.py`: batch execution and checked Lua errors.
- `aseprite_mcp/tools/script.py`: raw Lua escape hatch.

Direct drawing workflow: `aseprite --batch --script path/to/draw.lua`. Preserve editable `.aseprite` sources with exported PNG sheets in the application repository. Inspect exports and animation frames before wiring them into the renderer. Checkpoint 1 uses the procedural Canvas scene. Living simulations now overlay Aseprite-authored crops and pump animation plus PixelLab-generated directional characters and a rainwater tank. This path allows the next art iteration without provisioning another service.

## Integrated memory and observability slice

Select an agent, then **Explore full memory**. Search and load earlier records from that agent's complete journal. The memory timeline navigates records; decisions link to their supporting observations. Choose one or more observations, write a decision, and save. Human decisions carry `actor=human` and `source=human`; local-rule entries retain agent identity. Historical entries are displayed without rewriting them.

The evidence gate requires at least one observation from the same agent and workspace. It rejects unknown IDs and non-observations without partially saving a decision. It checks provenance and nonempty evidence, not truth, relevance, or contradiction semantics. This is a narrow adaptation of Claimground/Sightglass ideas, not a complete argument engine. Decisions do not yet alter movement.

**Diagnostics** displays retained command and failure events. Step events are sampled every 20 ticks. Wiretap forwarding is off by default. If Wiretap is already running, opt in when launching:

```sh
FIELDWORK_WIRETAP=1 python3 apps/agent-workspace/server.py
```

Forwarding runs outside the simulation loop with a 0.5-second timeout, every five seconds, up to 20 pending events. SQLite acknowledges individual IDs only after HTTP 200; validation rejection remains pending, and a concurrent append is not overwritten. Delivery is at least once: a lost acknowledgement can duplicate an event in Wiretap, which does not implement idempotency. IDs allow correlation. The retained local diagnostics table is not automatically pruned. Queue forwarding is telemetry only, not synchronization into PocketBase or NovaSystem's memory bridge.

**System map** lists the implemented components, source paths and relationships. This keeps the code-atlas concept inside the current application without requiring another service. After reviewing changed sources:

```sh
python3 apps/agent-workspace/build_map.py
python3 apps/agent-workspace/build_map.py --check
```

Integration contracts and negative controls:

```sh
python3 -m unittest discover -s apps/agent-workspace/tests -v
python3 apps/agent-workspace/tests/mutation_check.py
python3 -m pytest tests/test_license_boundary.py -q
```

Current verification: 17 application tests, 7 standalone-boundary tests, and two observed failing negative controls (bypassed evidence gate; discarded failed telemetry). Browser tests cover memory/decision/diagnostics/map flows and mobile overflow; screenshots are in `proof/memory.png` and `proof/integrations-mobile.png`.

Runtime remains standard-library Python plus bundled JavaScript and Canvas. No cross-repository package imports, automatic services, external models, generated assets, or shared production-journal writes are added. Saved place definitions now drive continued movement. The independent NovaSystem memory-bridge defects remain outside this Fieldwork integration.

## Checkpoint 2: living field station

Choose **Start a ready-made field station** on the home screen for Mira (scout), Sol (engineer) and Fern (gardener), or add your own agents and choose **Start living simulation**. Simulation roles are explicit policy settings; personality prose remains authored description. Existing checkpoint 1 worlds continue using their original exploration rules until explicitly enabled.

Press **Resume**, optionally choose **4x**, and watch:

1. Scout visits the library and observes the pump repair instructions.
2. Field radio records an attributed exchange in the engineer's journal.
3. Engineer changes its plan based on that remembered record, travels to the workshop and spends two timber repairing the pump.
4. The pump replenishes the reservoir. Gardener observes soil conditions, uses water, tends crops and harvests food.
5. Work consumes energy. Agents rest at the library and eat one food when available, or recover less energy without a meal.

The world persists water, timber, food, moisture, crop growth, pump condition, weather, agent energy, roles, knowledge, plans and task progress. Day/night visuals follow a 240-tick cycle. Rain lasts 120 ticks. No sleep, hunger or weather model beyond the rules in `simulation.py` is implied.

**Intervene:** bring rain, dry soil, break the pump, or deliver timber. Save a checkpoint before changing conditions, then restore it as a separate branch to compare outcomes. Role assignment resets that agent's future plan and records a policy revision. **Explore full memory** and **Trace record** follow outcomes through plans and exchanges to the original observation, including another agent's journal within the same world.

A single tab owns a running simulation. Observer reads cannot keep it running. Explicit takeover transfers the lease. Persisted command IDs prevent an identical command retry from repeating an intervention; reusing an ID with different content is rejected. This covers workspace commands, not creation or restore requests. Runtime provider jobs are not part of this checkpoint, so faster simulation cannot increase paid API usage.

### Art production

- `art/draw_fieldwork.lua`: authored pixel drawing executed by Aseprite, producing four crop states, tools, timber, food, weather and a four-frame flowing pump.
- `art/fieldwork-props.aseprite`, `art/pump-flow.aseprite`: editable original sources.
- `art/researcher.aseprite`, `art/water-tank.aseprite`: editable imports of PixelLab output.
- `art/pixellab-*-job.json` and status receipts: two initial generation jobs plus one four-direction template animation request (four animation jobs). Standard character generation returned four 48x48 directions despite the requested 32-pixel size. Actual dimensions were inspected and recorded.
- `art/pixellab-researcher.zip`: original generated download retained.
- `art/manifest.json`: dimensions, provenance and SHA-256 hashes.
- `web/assets/fieldwork-v2/`: immutable runtime asset version selected by saved simulation state. Original exports remain in `web/assets/` for editing.

The character uses four directional idle poses and four PixelLab-generated walk cycles, four frames per direction. Walk frames are assembled into editable Aseprite timelines and horizontal sheets by `art/import_walk.lua`. Pump animation has four Aseprite-authored frames. The terrain background remains procedural Canvas. Assets load only from the local server; procedural sprites remain the fallback if images fail.

To reproduce authored exports on this workstation:

```sh
aseprite --batch --script apps/agent-workspace/art/draw_fieldwork.lua
aseprite --batch --script apps/agent-workspace/art/import_generated.lua
aseprite --batch --script apps/agent-workspace/art/import_walk.lua
python3 apps/agent-workspace/art/package_art.py
python3 apps/agent-workspace/build_map.py
```

Lua scripts currently name this checkout's absolute root. Adjust it if relocating. Packaging refuses to change existing versioned pixels; create a new art version for future visual changes. `pixellab_client.py` is an explicit development tool that reads the existing local MCP configuration. It is never imported by the application and never runs generation automatically.

### Checkpoint 2 verification

28 application tests cover the existing MVP plus a complete causal loop, missing-evidence counterfactual, exchange-driven replanning, deterministic resource continuation after restore, isolated interventions, command replay, rollback, controller/observer behavior, the scenario launcher, source tracing, asset hashes and mobile layout. Seven standalone-boundary tests pass. Four negative controls were observed failing before the unmodified tests passed:

```sh
python3 apps/agent-workspace/tests/mutation_check.py
python3 apps/agent-workspace/tests/simulation_mutation_check.py
```

The 12-agent, 1,200-tick soak produced four harvests and 6,670 entries. Observed p95 tick time: 13.05 ms; database size: 5,263,360 bytes. This is one local measurement, not a performance guarantee. `proof/simulation-soak.json` retains the result. Histories grow without automatic deletion; checkpoint payloads contain complete journals. Longer deployments still need a retention/export design.

Remaining scope: optional model-provider jobs and budgets, portable journal binding swaps, public import/export, advanced navigation, evaluated policy evolution and additional character animation. Runtime remains offline and deterministic.


### Inspect journals

Select an agent and choose **Inspect journal**. Browse or search saved entries, expand **Inspect stored entry JSON** for every persisted field, or follow evidence links to their source records. IDs, recorded timestamps, source and actor are visible. These are actual local journal records.

**Inspect complete journal JSON** reads a consistent snapshot of the entire selected journal, oldest first. **Download complete journal JSON** downloads a fresh snapshot, including older entries outside the visible page and regardless of search. The snapshot timestamp indicates when it was read; reopen the disclosure to refresh. Neither inspection nor export modifies entries.

`GET /api/journal/export?id=WORKSPACE_ID&agent=AGENT_ID` returns the same `fieldwork-journal-v1` document. Original entry fields are preserved. Import is not implemented.


### Checkpoint 3: experiment review

Open **Checkpoints** to save a named baseline. Apply an intervention or run the simulation, then use **Compare checkpoint** to inspect resource differences, completed tasks and new journal records. A restored branch can compare against its source checkpoint. Comparison snapshots are read-only and downloadable; their recent-record excerpt contains at most 20 records, while counts include complete history. Use Inspect journal for full journal export.

Mobile section navigation jumps to agents, world or profile. Selecting an agent on mobile brings its profile into view. Keyboard focus on agents survives background refresh.

`GET /api/comparison?id=WORKSPACE_ID&checkpoint=CHECKPOINT_ID` returns eligible baselines and a consistent comparison. Omit checkpoint to select the latest eligible baseline. Unrelated workspace checkpoints are rejected. Differences describe changes, not causal effects.

32 application tests pass. See [application audit and next steps](AUDIT.md) for scope, critique, evidence and remaining limitations.
