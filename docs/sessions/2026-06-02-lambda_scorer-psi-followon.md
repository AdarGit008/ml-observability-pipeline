# 2026-06-02 — lambda_scorer: PSI follow-on (compute_psi + edge-triggered SNS + STATE-row extension)

## Component
`lambda_scorer` (Tier 2b parity-touching: imports `shared.features.extract_features`, `shared.score.score`, `shared.drift.load_reference`, and — new in Lambda mode this session — `shared.drift.compute_psi`, all as peers per ADR 0005). Parity boundary unchanged — no edits to `shared/{features,score,drift}.py`. A fourth structural-parity guard (`test_structural_parity_compute_psi_loads_from_shared`) joins the existing three.

## Intent
Close the PSI + SNS deferral from the 2026-06-02 MVP session: widen the hot-path DynamoDB read to the 1-hour PSI window, call `shared.drift.compute_psi` against the cold-start-loaded reference, land the ADR-0010-pre-authorized STATE-row extension (`latest_psi`, `alert_flag`, `last_alert_sent_at`), and publish to SNS on threshold breach per `_interfaces.md §SNS alert payload`. Schema-additive only — no DynamoDB migration; ADR 0010 carries unchanged.

## PO decisions at plan-step
Plan-step walkthrough was delivered in the prior sitting (2026-06-02); PO deferred greenlight, then ratified all four recommended defaults at this session's open:

1. **Query shape — single `Query Limit=1800`.** One DynamoDB read per invocation; the trailing 150-slice feeds `extract_features`, the full window feeds `compute_psi`. At ~15 pumps × ~0.5 RPS the marginal read-unit cost of the wider payload is invisible; one read beats two on control-flow simplicity and DynamoDB-latency contribution. **Captured as a refinement note within ADR 0010** (the §Access patterns "PSI follow-on" row anticipated exactly this; structurally nothing new) — per plan-step Q5, no new ADR for this part.
2. **PSI cadence — every invocation.** ADR 0007's every-Nth-tick cadence exists to throttle InfluxDB *writes* in local mode (PSI lands as point fields), not because the computation is expensive (~2 ms numpy on an 1800-sample window). Lambda has no per-tick write to throttle and a stateless handler has no natural tick counter. ~450 PSI computes/min fleet-wide is free CPU.
3. **Alert dedupe — edge-trigger.** Publish only on the False → True `alert_flag` flip; previous flag read via `GetItem` on the STATE row before the overwrite. Protects the SNS Always-Free 1000-email/month envelope (always-publish ≈ 30 alerts/min/pump under persistent breach). **Structurally novel (GetItem-before-overwrite) → ADR 0012.**
4. **Alert-state attributes — two.** `alert_flag` = current-invocation breach (dashboards' "red now" read, overwritten every call); `last_alert_sent_at` = ts of last publish (dedupe/audit trail, carried forward, absent until first publish). Resolves plan-step Q4's current-vs-last-published tension by not conflating them.

## What changed

### Code
- `lambda_scorer/handler.py` (267 → 417 lines) — module docstring rewritten for the full flow + §Alerting + the parity nuance; cold-start adds required `SNS_TOPIC_ARN` (`KeyError` if unset — matches the reference eager-load fail-fast posture) + module-level `boto3.client("sns")`; new constants `PSI_WINDOW_SAMPLES=1800`, `PSI_ALERT_THRESHOLD=0.25`, `SCORE_ALERT_THRESHOLD=0.7`; hot path: Query widened to 1800 → window capped at 1800 → `extract_features(window[-150:])` → `compute_psi(window, reference=REFERENCE)` every invocation → breach booleans → reading-row PutItem (unchanged) → STATE GetItem (edge-trigger input + `last_alert_sent_at` carry) → STATE PutItem with the three new attributes → `_SNS.publish` on the flip, AFTER the STATE write. New `_alert_type` helper maps breach booleans to the payload's `"both" | "psi_breach" | "high_failure_prob"`. Handler return adds `alert_flag` (additive; CloudWatch visibility).
- `lambda_scorer/tests/conftest.py` (68 → 96 lines) — fixture creates a moto SNS topic + sets `SNS_TOPIC_ARN` to its ARN before the reload; module-level `os.environ.setdefault("SNS_TOPIC_ARN", <placeholder>)` so handler imports OUTSIDE the moto fixture (structural-parity + cold-start tests) don't crash on the now-required env var. Docstring explains both.
- `lambda_scorer/tests/test_handler.py` (337 → 602 lines, 11 → 18 tests) — see §Tests state.

### Documentation
- `docs/adr/0012-edge-triggered-sns-alerts.md` (new, 228 lines) — edge-trigger + two-attribute alert state + publish-after-write ordering. §Alternatives covers always-publish/downstream-dedupe, time-gated re-page, single-attribute and last-published-only shapes, and folding the STATE row into the window Query (rejected — re-opens the ADR 0010 reserved-SK filter invariant). §Consequences carries the at-most-once-per-edge trade-off (lost-on-publish-failure chosen over duplicate-on-write-failure because the failure is loud) and the no-hysteresis demo-scale acceptance.
- `context/_interfaces.md` — STATE-row block lands the three attributes as committed (the "(PSI follow-on adds)" comment removed); §Access patterns updated (single 1800 Query + edge-trigger GetItem row); §Lambda scorer DynamoDB writes updated to the five-operation shape; §SNS alert payload gains the edge-trigger + `SNS_TOPIC_ARN` note; §Telemetry payload gains a warning that the example values sit OUTSIDE the operational reference ranges (see §Trade-offs); §Grafana adapter gains the two-attribute read pointer.
- `context/lambda_scorer.md` — §Current state, §Interfaces, §Resource sizing, §Environment variables (+`SNS_TOPIC_ARN`), §Related ADRs (+0012) updated; §Open questions unchanged (cold-start latency measurement remains the only open item — now noting the extra SNS client construction).
- ADR 0010 — deliberately unedited; the STATE-row extension was pre-authorized in §Decision and the Limit=1800 widening in §Access patterns.

## Decisions
- **Single-Query window shape** — refinement within ADR 0010, recorded here (plan-step Q5 disposition: no GetItem-then-Query pattern emerged *for the window*; the edge-trigger GetItem is an alert-path concern and lives in ADR 0012).
- **Every-invocation PSI** — recorded here + handler docstring step 6; ADR 0007 unedited (its cadence decision was always local-mode-write-shaped).
- **ADR 0012** — edge-triggered SNS + two-attribute alert state. Status: Accepted (PO sign-off 2026-06-02; reviewer-cascade review pending).

## Trade-offs surfaced
- **Mode-parity nuance: raw telemetry vs feature dicts into `compute_psi`.** Local mode feeds the per-pump `_feature_history` deque (extracted-feature dicts); Lambda feeds the raw-telemetry reading rows. Equivalent on the PSI surface — `compute_psi` reads only `PSI_FEATURE_NAMES` (the 4 raw signals), whose values are identical in both representations (the feature dict's raw-signal entries ARE the latest raw reading). Structurally different objects, same shared function, same result. Documented in the handler module docstring; flagged at plan-step and carried per the brief.
- **`_interfaces.md` example telemetry is NOT PSI-healthy.** Empirically measured while building the alert tests: the doc's example values (bearing_temp 68.3, motor_current 4.7) fall OUTSIDE the operational reference's per-feature ranges (bearing_temp 56.1–60.1, motor_current 3.58–4.38 — the reference is demo-paced HEALTHY per ADR 0008). Consequence: a CONSTANT window of "healthy-looking" values concentrates all mass in one clipped bin and breaches PSI hard (31 constant defaults → max PSI 2.358). Healthy-LOOKING ≠ healthy-DISTRIBUTED. The test helper `_seed_spanning_readings` cycles the reference's own bin midpoints (30 spanning + 1 default → max PSI 0.005, score 0.067); a warning landed in `_interfaces.md §Telemetry payload`. This also documents expected demo behaviour: a warm pump that suddenly flatlines at ANY constant value will (correctly) read as distribution shift.
- **At-most-once per edge (publish-after-write).** A publish failure after the STATE row lands loses the email but errors loudly in CloudWatch; the IoT-Rule retry sees `prev alert_flag == True` and doesn't double-publish. The inverse ordering trades lost-for-duplicate. ADR 0012 §Consequences carries the full argument.
- **`SNS_TOPIC_ARN` required vs testability.** Making the env var required (`os.environ[...]`) is the right production posture but would crash every non-moto handler import in the suite; the conftest module-level `setdefault` placeholder threads the needle, and `test_cold_start_missing_sns_topic_arn_raises_keyerror` deletes it deliberately to pin the production `KeyError`.
- **Sandbox dep re-install.** Fresh sandbox this sitting; `pip install -r requirements.txt --break-system-packages` restored the suite. Committed artifacts (sklearn-1.7.2-pickled model.pkl) loaded cleanly — no repeat of the MVP session's version-mismatch regen.

## Tests state
**368 passed + 1 skipped** in 18.14 s (sandbox). Net delta from the post-MVP baseline (361 + 1): **+7 tests**, all under `lambda_scorer/tests/test_handler.py`. Item 4's brief expected +3 to +5; the two extras are the fourth structural-parity guard (Definition-of-done item, not Item 4) and the `SNS_TOPIC_ARN` KeyError pin (new env-var posture, surfaced during implementation). No regressions elsewhere; all four structural-parity guards green.

| New test | What it pins |
|---|---|
| `test_structural_parity_compute_psi_loads_from_shared` | `compute_psi` resolves to `shared/drift.py` — fourth ADR 0005 guard, added as `compute_psi` enters Lambda mode |
| `test_cold_start_missing_sns_topic_arn_raises_keyerror` | Required-env fail-fast: reload without `SNS_TOPIC_ARN` raises `KeyError` at module scope |
| `test_handler_psi_lands_on_state_row` | 200-row warm window → `latest_psi` lands as a Decimal Map keyed by `PSI_FEATURE_NAMES`; `PSI_WINDOW_SAMPLES == 1800` pinned |
| `test_psi_surface_pinned_at_four_keys` | ADR 0009: `len(latest_psi) == 4` and key set == `PSI_FEATURE_NAMES` |
| `test_sns_publish_on_threshold_breach` | 10 extreme rows + extreme invocation → exactly one publish; payload shape per `_interfaces.md` (TopicArn, pump_id, ts, alert_type ∈ {psi_breach, both}, 4-key psi, float score); STATE gets `alert_flag=True` + `last_alert_sent_at=ts` |
| `test_sns_no_publish_when_healthy` | 30 reference-spanning rows + default invocation → no publish, `alert_flag=False`, no `last_alert_sent_at` attribute |
| `test_sns_no_republish_when_still_breached` | ADR 0012 edge semantics: two consecutive breaching invocations → one publish; `last_alert_sent_at` keeps the FIRST ts while `latest_ts` advances |

Measured PSI mechanics backing the alert tests (probe run against the committed reference): 11 extreme samples → max PSI 1.171, score 0.824 ("both"); 31 constant defaults → max PSI 2.358; 30 spanning + 1 default → max PSI 0.005, score 0.067; 1 default → max PSI 0.057, score 0.087. The test-file docstring §PSI window mechanics carries these.

## Deploy-zip footprint verification
- New top-level imports in `handler.py`: stdlib `json` + `shared.drift.compute_psi` (module already bundled). SNS client is the same boto3 already counted.
- Heavy modules reachable from `import lambda_scorer.handler`: `boto3, botocore, joblib, numpy, scipy, sklearn` (+ sandbox-only `pandas`, same as the MVP measurement). ADR 0006 §Q4 ~124 MB baseline holds.

## Open follow-ups
- **Cold-start latency measurement (post-deploy, IaC session).** Now includes the SNS client construction; expected negligible.
- **IaC session.** DynamoDB table + SNS topic (+ email subscription for the demo) + IoT Rule + IAM: `dynamodb:Query/GetItem/PutItem` + `sns:Publish` scoped to the single table/topic ARNs; `SNS_TOPIC_ARN` env wiring on the Lambda.
- **Dashboards adapter (Grafana → DynamoDB).** Consumes `alert_flag` + `last_alert_sent_at` via the existing BatchGetItem pattern. With this session, the AWS-mode hot path is feature-complete except the adapter + IaC.
- **PO Windows-side regen of canonical artifacts at 30 pumps** — carried from the MVP session, unchanged.

## Context files updated
- `context/_interfaces.md` — STATE-row attributes landed as committed; access patterns; writes section; SNS payload edge-trigger note; telemetry-example warning.
- `context/lambda_scorer.md` — current state, interfaces, sizing, env vars, related ADRs.

## Note for next session
The hot path is done. The two remaining AWS-mode work fronts are independent: (a) IaC — Terraform modules for table/topic/rule/IAM, where the `SNS_TOPIC_ARN` + `DDB_TABLE_NAME` env contract and the four DynamoDB operations in `context/lambda_scorer.md` §Interfaces are the requirements list; (b) dashboards adapter — `BatchGetItem` over 15 STATE rows, every attribute it needs is now on the row. Neither touches the parity boundary.

## Reviewer feedback highlights

Review packet ran through the cascade; Gemini unavailable again, **Groq** picked it up on `llama-3.3-70b-versatile`. Response file: `review_responses/2026-06-02-lambda_scorer-psi-followon.md`. Provenance footer renders correctly this time (the 2026-06-02 escape fix held).

**Calibration caveat (ADR 0011 §Consequences, consistent with the MVP review):** Llama-3.3-70b validated all six packet questions without substantive pushback and its three additional suggestions were generic-to-wrong; weight accordingly. The packet's questions 1–6 (edge-trigger ordering, RMW race, every-invocation PSI, constant-window behaviour, conftest setdefault, two-attribute state) were each endorsed with boilerplate "monitor and revisit" caveats — no findings, no change required.

| Reviewer point | Disposition | Notes |
|---|---|---|
| Q1–Q6 endorsements | **Validated** | All six design questions endorsed; the "consider retry mechanism / TransactWriteItems if scaling" caveats restate ADR 0012's own §Consequences and ADR 0010's upgrade path. No change. |
| "Add more comments explaining design decisions" | **Rejected** | Generic. `handler.py` carries a 120-line module docstring + per-block comments citing the governing ADRs; the comment density was itself a review-feedback artifact of the MVP session. |
| "Avoid magic numbers (e.g., `PSI_WINDOW_SAMPLES`, `PSI_ALERT_THRESHOLD`) — define named constants instead" | **Rejected (internally contradictory)** | The items named ARE named module constants with derivation comments. The suggestion asks for what already exists. |
| "SNS publish error handling not explicitly mentioned; ensure errors are logged" | **Rejected as designed** | Letting the publish exception propagate IS the error handling — fail-loud per ADR 0012 §Decision #3: the Lambda invocation error lands in CloudWatch metrics + logs, which is strictly louder than a caught-and-logged warning. Documented in ADR 0012 §Consequences (at-most-once per edge). |
| "Additional test: SNS publish failure path" | **Deferred** | A test pinning "publish raises → invocation errors AND STATE row already carries last_alert_sent_at" would pin the at-most-once ordering. Marginal value (it mostly tests boto3 raising); queued for a future test-quality pass alongside the MVP review's deferred autouse-reload marker. |

**Diff from review:** none — no code changes warranted.
