# Fieldwork permutation testing: results and opportunities

2026-09-21. Tested checkpoint 3 without changing production code or the preview database. All storage and browser runs used isolated temporary databases. Raw results and scripts are retained in this repository.

## Scope and counts

10,010 exploratory cases were executed in the final matrices. Cases are configurations, action sequences, layouts or validation probes, not 10,010 independent unit tests. Ordinary regression tests are counted separately.

| Layer | Cases | Work and outcome |
| --- | ---: | --- |
| Policy grid | 9,720 | 4,665,600 ticks; zero caught resource-bound, energy/position-bound or dangling-evidence failures |
| SQLite sequences | 36 | 5,760 step/intervention calls plus setup; populations 1/3/6/12; retry, restore, reopen, pagination, full export and comparison checks passed |
| Browser surfaces | 168 | Chromium; 4 populations × 6 widths × 7 surfaces; 8 dialog overflow findings |
| HTTP validation | 54 | 40 malformed domain commands rejected without state mutation; 14 non-boolean run flags accepted unexpectedly |
| Controlled follow-ups | 15 | 12 position/update-order controls and three 5,000-tick resource-starvation runs |
| Concurrent retries | 15 | 1/2/4/8/16 workers × 1/8/96 distinct IDs; 1,440 requests, exact expected delivery counts |
| Concrete flag reproductions | 2 | String false starts simulation; string false permits takeover |

Policy axes: all 27 ordered three-agent role assignments; initial water 0/5/30; timber 0/1/2/3; food 0/4; moisture 0/35/100; none/rain/drought/break-pump/timber intervention at tick 120; 480 ticks per configuration. Policy resources and interventions are injected into in-memory fixtures, so this layer tests policy behavior rather than HTTP command wiring. Separate storage and HTTP probes cover real commands.

Browser axes: populations 1/3/6/12; widths 320/390/700/768/1150/1440; workspace, journal, comparison, checkpoints, diagnostics, system map and guide. Input variants include a supported 40-character unbroken name and HTML-looking text. No browser page errors or external requests were observed. These fixtures do not isolate population from name length; the overflow evidence concerns the combined 12-agent/long-name fixture.

## Highest-priority defects

### 1. Run flags accept arbitrary JSON and reverse the meaning of string false

`POST /api/run` with `running: "false"` returned 200 and `running: true`. With an owner already running, an observer request containing `takeover: "false"` returned 200 and made that observer the controller.

Both fields accepted null, arrays, objects, numbers and strings in the validation matrix. This is a strict-type contract expectation derived from the UI's boolean usage; the semantic reproductions demonstrate why truthiness is unsafe even without a published schema. Existing same-origin guards still apply. This is not evidence of a remote authentication bypass.

**Opportunity:** validate booleans with exact types, validate numeric speed without accepting Python booleans, and add API schema tests around every control mutation. Acceptance: malformed flags produce 400 and leave runner ownership and state unchanged; real false never starts a run or takes control.

Evidence: `proof/permutations/surface-summary.json`, `concurrency-summary.json`. Implementation location: `server.py`, `/api/run` branch.

### 2. Supported long names break dialog usability

A 40-character unbroken agent name overflowed the journal dialog at all six widths. Comparison overflowed at 320 and 390. Document-level overflow remained false, so a test checking only document width would miss the defect. Some screenshot contents are shifted out of view horizontally when the close button receives focus.

**Opportunity:** allow long titles and agent text to wrap, give flex children appropriate minimum widths, constrain horizontal scrolling, and keep close controls reachable. Acceptance: both document and dialog scroll widths fit at all supported sizes using maximum-length names, while content and close controls remain visible.

Evidence: `proof/permutations/overflow-12-390-journal.png` and other `overflow-*` screenshots. This extends the earlier mobile checks with actual boundary-length inputs.

## Simulation opportunities

### 3. Busy agents can make no useful progress indefinitely

4,340 of 9,720 configurations produced no harvest in 480 ticks. Some lack a gardener, so this aggregate is not a failure rate. Among the 2,160 configurations containing all three roles, 304 had no harvest. Every one of those 304 started with fewer than two timber; no balanced configuration with at least two timber missed harvest within this grid and horizon.

An extended all-role scenario with zero initial water, timber, food and moisture produced **748 completed tasks, 2,791 events, 1,290 exchanges, zero repairs and zero harvests over 5,000 ticks**. The pump remained broken. Source inspection confirms there is no timber-acquisition action in current policies.

**Opportunity:** add explicit blocked states with reasons, distinguish productive work from repeated observations, and introduce a resource-acquisition or resupply request action. UI should say what intervention would unblock the station.

### 4. Short horizons can reward temporary recovery over sustained recovery

Using the same resource-starved scenario and intervention at tick 120:

| Intervention | First harvest | Harvests by tick 5,000 | Final outcome |
| --- | ---: | ---: | --- |
| None | None | 0 | Pump broken, resources exhausted |
| Rain once | 416 | 1 | Pump still broken, resources exhausted |
| Three timber | 560 | 21 | Pump repaired; water 30, food 14 |

A 480-tick test would count rain as successful and timber as unsuccessful in this scenario. That would be an instrument error if interpreted as sustainable recovery.

**Opportunity:** report time-to-first-harvest, production over a longer horizon, time spent blocked, and resource sustainability. Do not collapse these into a single success flag.

### 5. Spatial setup is a confound in role permutation experiments

Four of 360 balanced-role resource/intervention groups differed in harvest counts between roster assignments. The initial grid also changes each role's starting x position when it changes roster order. We therefore did not label this an update-order defect.

For the reproduced group, six execution-order permutations with positions fixed by role all yielded zero harvests. Six colocated-start permutations all yielded one harvest. This supports a spatial/timing explanation for that example. It does not prove every possible scenario is order-independent.

**Opportunity:** save explicit spawn configuration with each experiment and separate role assignment, initial placement, update ordering and random seed as independent controls. Branch comparisons should share all uncontrolled initial conditions.

### 6. Journal volume needs signal controls

The highest-event short run produced 379 records in 480 ticks with no harvest; 178 were exchanges. The extended blocked scenario accumulated thousands of records while doing no productive work.

**Opportunity:** filter by event type, summarize repeated unchanged observations, track meaningful state transitions, and avoid counting journal volume or completed tasks as intelligence or progress. Preserve original entries even when the UI summarizes them.

## Durable behavior that held up

- Restored branches continued with matching simulation resources, agent energy, task counts, positions and journal counts across 36 seeded sequences.
- Reopening storage and comparing branch source checkpoints preserved the expected differences.
- Journal pagination and full export produced identical sets of entry IDs across population sizes.
- Concurrent command receipts prevented duplicate timber delivery: 1,440 requests represented 525 distinct commands in total, with 525 deliveries.
- Malformed domain commands tested in the HTTP matrix produced 400 without changing saved workspace state.

## Instrument checks and limits

A deliberately negative resource was rejected by the invariant checker before the sweep. Existing mutation tests cover evidence, outbox, complete export and same-tick comparison. Browser instrumentation was corrected to wait for dialogs to become visible before measuring their internal width; that correction exposed an additional comparison overflow. The earlier attempt is preserved for transparency and is not counted as the final matrix.

An initial browser setup attempted hash-only navigation without reloading the document. The app reads its workspace hash on initial load, so the harness never opened its intended fixture. The harness now forces document navigation with a query parameter. This setup failure is not counted as an application permutation failure.

This is a finite deterministic grid, not exhaustive state-space exploration. Invariant checks cannot establish whether policy decisions are sensible. The browser matrix is Chromium only, not cross-browser or screen-reader certification. Only shared-process command contention was tested; multi-process database access was not. No production behavior was fixed in this testing pass.

## Reproduce

From SimpleAgentOS root:

```sh
python3 apps/agent-workspace/tests/permutation_probe.py
python3 apps/agent-workspace/tests/surface_probe.py
python3 apps/agent-workspace/tests/followup_probe.py
python3 apps/agent-workspace/tests/concurrency_probe.py
python3 -m unittest discover -s apps/agent-workspace/tests -v
```

Exploration runners record findings rather than converting every undesirable outcome into an assertion. Invariant, storage and concurrency violations raise failures; surface findings live in JSON and must be inspected. Runs overwrite their own latest-result files. Source hashes, runner hashes, Python/platform and exact policy case inputs are recorded in `proof/permutations/summary.json`, `aggregate.json` and `policy-cases.jsonl`.

Recommended next checkpoint: strict control schemas and long-name layout fixes first, then blocked-state explanations and sustainable-recovery metrics. These changes address observed failures and would make the next round of experiments easier to interpret.
