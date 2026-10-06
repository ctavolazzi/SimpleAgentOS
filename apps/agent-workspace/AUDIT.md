# Fieldwork application audit and checkpoint 3

Reviewed 2026-09-21. Scope: home and onboarding, workspace navigation, pixel scene, agent profiles, journals and evidence, simulation policies and resources, interventions, checkpoints, comparison, local HTTP host, storage and optional telemetry. This is a source and Chromium interaction review, not a formal accessibility or penetration audit.

## Critique

Fieldwork has a coherent small-world visual identity and unusually inspectable persistence for a prototype. Warm paper, serif headings, restrained green controls and real pixel assets fit together. The strongest feature is the connection between saved observations, plans, exchanges and outcomes. Preserve that connection as the app grows.

Its weakest area was the experiment workflow. A user could intervene and restore, but had to remember the resource baseline and manually reconstruct differences from journals. The screen also treated low-frequency developer tools and everyday controls as peers. It has accumulated useful panels faster than navigation has adapted.

## Findings and disposition

| Priority | Area | Evidence and consequence | Disposition |
| --- | --- | --- | --- |
| High | Keyboard navigation | Focused an agent button, waited 1.1 seconds, and found focus lost after roster reconstruction during polling. | Fixed: preserve focused agent by stable ID. Browser regression waits across polling. |
| High | Mobile navigation | At 390 px wide the page was 2,713 px tall; selected inspector began at y=1,816.5. Selecting a name offered little visible feedback near the roster. | Fixed: sticky section links and focus/scroll to selected profile on mobile. Workspace URL is preserved. |
| High | Experiment review | Intervention form suggested comparing branches, but no comparison interface existed. | Implemented baseline comparison for current workspace checkpoints and immediate branch source checkpoint. |
| Medium | Checkpoint intent | Save button produced tick labels; backend supported a label but UI did not expose it. | Implemented named checkpoint form and immediate comparison after saving. One-click save remains. |
| Medium | Empty selection | Existing agents with no selection still prompted users to create their first agent. | Fixed: distinguish no population from no selection. |
| Medium | Control sizing | Default label margins stretched the desktop speed toolbar and buttons. | Fixed compact alignment; retained existing palette and typography. |
| Medium | Readability | Resource numbers used proportional figures; raw data could dominate journal view. | Added tabular figures; retained progressive JSON disclosures and readable entries. |
| Medium | Event signal | Main activity feed shows only five newest events; exchanges can crowd out outcomes. Journal filtering is text-only. | Next: event-kind/agent filters and direct event inspection with durable links. |
| Medium | Evidence freshness | Some decisions are evidence-gated, but policies still read shared simulation state. Remembered reports can be old; no freshness UI. | Next: show observed-at tick, current plan and evidence age; define what each role is allowed to observe. |
| Medium | Resource explanation | Values are visible but trends and change attribution are absent. Baseline deltas now show change but cannot establish causality. | Next: recorded resource time series and equal-duration branch comparison. |
| Medium | Long-running storage | Journals are append-only. Full export, checkpoint creation and comparison materialize history; queries use JSON extraction. Checkpoint payload duplicates full journals. | Next: benchmark 100k records, index scoped journal access, stream export and define migration/import contracts. Do not silently prune history. |
| Medium | Transport errors | Browser teardown can produce BrokenPipe logs caught as generic request failures. All assertions pass, but diagnostics can imply a command/storage failure after a response disconnect. | Follow-up: distinguish transport disconnect from committed command failure and test retry receipts across response loss. |
| Medium | Multi-tab affordances | Server guards mutations, but some edit/intervention buttons still appear usable to observers and then return an error. Only one workspace runs globally. | Next: consistent observer-state controls and explicit single-runner scope. |
| Low | Scene legibility | Agents share one researcher asset; names overlap when starting together. Sidebar portraits use the procedural character while scene uses generated sprites. | Next: role silhouettes/accessories, collision-aware labels and matching portraits. |
| Low | Animation/accessibility | Canvas offers roster keyboard alternative, but redraw continues while paused; scene motion does not yet honor reduced-motion preference. | Next: pause-aware redraw and reduced-motion canvas behavior. |
| Low | Information hierarchy | Diagnostics and System map occupy primary workspace toolbar; simulation guide still describes original MVP. Several dialogs grow vertically. | Next: group developer tools and add task-based introduction for repair, harvest, checkpoint and compare. |

## Checkpoint 3: experiment review

1. Open **Checkpoints**, name a baseline, then **Save named checkpoint**. Existing one-click Save checkpoint still works.
2. Apply an intervention or advance the simulation.
3. Open **Compare checkpoint**. Read baseline/current resource values, signed numeric differences, task counts and new journal event counts.
4. Inspect recent records as stored JSON, or download the comparison snapshot.
5. Restore a checkpoint as a new branch. Comparison can still use that branch's source checkpoint.

Comparison is a read-only SQLite snapshot. Eligibility is limited to checkpoints owned by current workspace plus its immediate source checkpoint. Unrelated workspace baselines are rejected. New events are determined by entry IDs, including changes recorded at the same tick as baseline. Metrics and counts cover full saved history; the displayed/exported recent-record excerpt is capped at 20. Full journal export remains available from Inspect journal. Comparison is refreshed explicitly and labels its read time.

This checkpoint adds no model calls, new service, storage migration, or policy changes. Deltas are descriptive. A before/after change while time advances is not a controlled intervention effect.

## Verification

- 32 application tests pass, covering original flows plus named baseline creation, comparison download, same-tick intervention, branch-source baseline, rejection of foreign checkpoints, 110-record counts, mobile navigation and focus retention.
- Same-tick negative control deliberately omitted new records and produced the expected assertion failure. Existing evidence, outbox and export negative controls also failed as expected.
- Before-change keyboard focus loss was observed in Chromium; after-change regression retains focus across polling.
- Browser checks cover desktop 1440x1000 and mobile 390x844; comparison has no horizontal overflow at mobile size. Screenshots are in `proof/audit-before-*` and `proof/checkpoint-3-*`.
- Browser test teardown emitted intermittent BrokenPipe logs; they are recorded above rather than treated as storage test failures.
- Source architecture hashes refreshed after review. No commit made.

## Best next developmental steps

1. **Make an agent's current work understandable.** Add a compact current-plan inspector with evidence age, progress and direct source-record links. Filter world events by agent and kind. Acceptance: a user can trace a repair outcome back to the shared guide without manually searching IDs.
2. **Make experiments controlled and portable.** Support two branches from a named baseline with equal tick budgets, persisted resource samples, and versioned workspace export/import validation. Acceptance: the control branch remains unchanged and both results survive restart.
3. **Harden the runtime before larger or paid runs.** Test lost-response retries, telemetry offline recovery, large histories, observer UI, schema upgrades and reduced motion. Benchmark first. Only then add an optional model provider with explicit budgets and cancellation.

Do not add a model provider simply to make agents appear more intelligent. Current policies are inspectable and deterministic; paid inference needs a defined decision boundary and evaluation that can demonstrate improvement.
