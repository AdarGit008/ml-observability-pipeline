# Session brief — paste at the start of every session

Use this to tell Claude what to load. Keep it to ≤6 lines so it's frictionless.

```
Component: <one of: simulator | model | drift | lambda_scorer | lambda_s3_batcher | local_runtime | infra | dashboards | dev_workflow>
Intent:    <one sentence — what this session will accomplish>
Loads:     _global, <component>, [_interfaces if work crosses components]
Reference: [optional: link to specific ADRs or open §6 questions in HANDOFF.md]
Constraints: [optional: anything beyond _global that applies — e.g., "must not touch DynamoDB schema until Q5 is resolved"]
Definition of done: <one sentence — what state the repo is in when this session ends>
```

## Example

```
Component: simulator
Intent:    Implement Pump physical model + state machine (HEALTHY → DEGRADING → FAILING → FAILED).
Loads:     _global, simulator
Reference: PLAN.md §2.2 for equations.
Constraints: No MQTT publishing yet — that's a separate session.
Definition of done: pump.py has Pump class with .step() returning a telemetry dict; unit tests cover all four states.
```

## Anti-examples (don't do this)

- "Just keep going on the project." — no component, no intent, no done state.
- "Loads: everything." — defeats the purpose; load only what's needed.
- "Component: simulator + lambda_scorer + infra." — that's three sessions, not one.
