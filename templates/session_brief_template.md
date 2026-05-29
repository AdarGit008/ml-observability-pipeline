# Session brief — paste at the start of every session

Use this to tell Claude what to load. Keep it to ≤6 lines so it's frictionless.

```
Component: <one of: simulator | model | drift | lambda_scorer | lambda_s3_batcher | local_runtime | infra | dashboards | dev_workflow>
Intent:    <one sentence — what this session will accomplish>
Loads:     _global, <component>, [_interfaces if work crosses components], [shared/ + ADR 0005 if parity-touching — see DEV_NORMS §5 Tier 2b]
Reference: [optional: link to specific ADRs or open §6 questions in HANDOFF.md]
Constraints: [optional: anything beyond _global that applies — e.g., "must not touch DynamoDB schema until Q5 is resolved"]
Definition of done: <one sentence — what state the repo is in when this session ends>
```

## Parity-touching check (DEV_NORMS §5 Tier 2b)

Before submitting the brief, answer this:

> **Does this session touch scoring, drift, feature extraction, or anything that imports from `shared/`?**

If **yes**, `Loads:` MUST include `shared/` source files + ADR 0005 (`docs/adr/0005-shared-mode-parity-package-and-subscriber-topology.md`). Components in the parity set as of 2026-05-29: `lambda_scorer`, `model`, `drift`, `local_runtime`, `dashboards`.

If the brief is for a parity-set component and Tier 2b loads are missing, Claude will refuse to start work and ask the PO to revise the brief. This is by design — silent divergence between local and AWS modes violates north star #6.

## Examples

```
Component: simulator
Intent:    Implement Pump physical model + state machine (HEALTHY → DEGRADING → FAILING → FAILED).
Loads:     _global, simulator
Reference: PLAN.md §2.2 for equations.
Constraints: No MQTT publishing yet — that's a separate session.
Definition of done: pump.py has Pump class with .step() returning a telemetry dict; unit tests cover all four states.
```

```
Component: lambda_scorer
Intent:    Implement DynamoDB read+append for the per-pump feature window; call shared.features.extract_features on the assembled window.
Loads:     _global, lambda_scorer, _interfaces, shared/features.py + shared/score.py + shared/drift.py + ADR 0005   # ← Tier 2b
Reference: HANDOFF.md §6 Q5 (DynamoDB schema, must be resolved first); ADR 0005 (mode-parity contract).
Constraints: extract_features call site MUST match local_runtime/service.py. No vendoring — see test_structural_parity_no_vendoring.
Definition of done: handler.py reads window from DynamoDB, calls shared.features.extract_features unchanged, writes scored row back. Parity tests still green.
```

## Anti-examples (don't do this)

- "Just keep going on the project." — no component, no intent, no done state.
- "Loads: everything." — defeats the purpose; load only what's needed.
- "Component: simulator + lambda_scorer + infra." — that's three sessions, not one.
- "Component: lambda_scorer / Loads: _global, lambda_scorer, _interfaces" — MISSING the Tier 2b parity loads. Claude will reject.
