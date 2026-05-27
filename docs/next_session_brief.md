# Next session brief — AWS-IoT publisher

Paste the block below at the start of the next conversation. The
free-form notes section underneath is for the human reading — Claude
gets only the fenced block.

```
Component: simulator
Intent:    Implement AwsIotPublisher (mTLS via aiomqtt against AWS IoT Core), drop the Fleet.from_config reject for target=aws-iot, prove a single pump P-00 publishes to IoT Core end-to-end.
Loads:     _global, simulator
Reference: ADR 0003 (full doc + 2026-05-27 Addendum on Windows loop-factory), simulator/publisher.py::AwsIotPublisher stub, docs/sessions/2026-05-25-simulator-mqtt-publishing.md (incl. 2026-05-27 smoke-test footnote).
Constraints:
  - $0 AWS spend cap is hard (per ml-obs-pipeline-context). Provision Thing+cert+policy via Console, NOT Terraform — Terraform state on IoT is a follow-up.
  - Region: eu-central-1 only.
  - Shape-only TLS schema validation in load_config stays untouched (ADR 0003 §Decision 5). File-existence + cert-parsing live in AwsIotPublisher.__aenter__.
  - Mode parity preserved: same Publisher ABC, same per-pump connection topology (one Thing = one client_id = one TCP connection).
  - Windows: loop_factory=SelectorEventLoop is already wired in __main__.py — do not regress.
Definition of done:
  1. AWS IoT Thing "P-00" provisioned with attached cert + per-Thing policy (publish-only to factory/pumps/P-00/telemetry).
  2. AwsIotPublisher.__aenter__ opens disk certs, configures aiomqtt's TLS, connects to the AWS IoT Core ATS endpoint, and publishes one real message visible in the IoT Core MQTT test client.
  3. Fleet.from_config no longer rejects target=aws-iot; the publisher itself is the gate.
  4. New tests cover the cert-loading code paths (existence checks, malformed-path rejection) by monkeypatching disk reads — do NOT pull real certs into the test tree.
  5. ADR 0003 status of AwsIotPublisher updated from "stub" to "implemented" with a note pointing to the session log.
  6. Smoke test rerun: a 2-pump fleet (P-00 = aws-iot, OR full fleet = aws-iot if cost permits) publishes to IoT Core, observed via the AWS console subscriber. Single-pump-only is fine if the cost math says so.
```

## Notes for the human

**Why one pump, not the full fleet:** AWS IoT Core's per-message price is ~$1/million. 15 pumps × 0.5 Hz × 24 h = ~650k msg/day = ~$0.65/day. Within $0 only if the session is brief and the fleet is stopped. Single pump for the smoke test is the safer default; full-fleet runs are demos, not routine.

**Provisioning walkthrough — what to expect:**
Phase A (~10 min, AWS Console): IoT Core > Manage > Things > Create > single Thing "P-00" > auto-generate cert > download cert + private key + AmazonRootCA1 + IoT endpoint. Save under `simulator/.secrets/P-00/` (already gitignored per `.gitignore` line "simulator/.secrets/").
Phase B (~5 min): create per-Thing policy allowing iot:Connect with client_id P-00 + iot:Publish on the P-00 telemetry topic only. Attach to the cert.
Phase C: smoke-test the cert with the AWS IoT MQTT test client (or `mosquitto_pub --cert ... --key ... --cafile ... --host ...`) BEFORE you put it through AwsIotPublisher — eliminates "is it the cert or is it the code" ambiguity.

**Files that will likely change in this session:**
- `simulator/publisher.py` — flesh out `AwsIotPublisher.__aenter__` / `publish` / `__aexit__`.
- `simulator/runner.py` — drop the `target=aws-iot` reject in `Fleet.from_config`.
- `simulator/tests/test_publisher.py` — add tests for cert-loading paths (monkeypatched).
- `docs/adr/0003-asyncio-mqtt-per-pump-aiomqtt.md` — note implementation status.
- `context/simulator.md` — tick the "AWS IoT path" box.
- (probably) `.gitignore` — already excludes `simulator/.secrets/` and `*.pem` / `*.key` / `*.crt`. Verify cert filenames match.

**What's NOT in scope for this session:**
- Terraform-managing IoT Things/policies/certs (follow-up — too much surface for one session).
- Switching from per-pump certs to a fleet-wide cert (premature; ADR 0003's per-pump decision should stand until we have actual reasons to change it).
- The scenario runner (`seasonal_drift` etc.) — separate session.
- Building a subscriber that does anything with the IoT-Core-side telemetry (lambda_scorer or local_runtime session).

**Watch items:**
- aiomqtt's TLS configuration interface — confirm shape against current docs before writing the code; v2.x reshuffled some kwargs from v1.
- AWS IoT Core's "Send a test message" flow has changed twice in the last year. If the Console UI doesn't match what walkthroughs say, drop into Claude-in-Chrome to navigate it interactively.
- Cost dashboard check at session end. The Budgets armed for `pdm-portfolio` will email at $1 / SMS at $5 — but eyeball the actual spend in Billing > Cost Explorer before declaring victory.
