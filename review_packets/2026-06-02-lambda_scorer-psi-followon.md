# Review Packet 2026-06-02 — lambda_scorer — PSI follow-on (compute_psi + edge-triggered SNS + STATE-row extension)

> Run via the reviewer cascade:
> `.\scripts\gemini_review.ps1 -Slug 2026-06-02-lambda_scorer-psi-followon`

## Role for the reviewer
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`. Background ADRs for this session: `docs/adr/0007-psi-implementation-and-cadence.md`, `docs/adr/0009-psi-surface-vs-scorer-feature-set.md`, `docs/adr/0010-dynamodb-schema-hot-state.md`, and the new `docs/adr/0012-edge-triggered-sns-alerts.md`.

## Summary of the change

This session completes the `lambda_scorer` hot path by closing the PSI + SNS deferral from the 2026-06-02 MVP session. Four PO-ratified decisions shape it: (1) the hot-path DynamoDB `Query` widens from `Limit=150` to `Limit=1800` — ONE read serves both windows, with the trailing 150-slice feeding `extract_features` and the full window feeding `compute_psi` (refinement within ADR 0010's pre-committed access pattern); (2) PSI is computed on EVERY invocation — ADR 0007's every-Nth-tick cadence is a local-mode InfluxDB-write throttle, not a compute constraint, and a stateless handler has no natural tick counter; (3) SNS publishing is EDGE-TRIGGERED (ADR 0012): the previous `alert_flag` is read back via `GetItem` on the STATE row before the overwrite, and a publish fires only on the False → True flip — protecting the SNS Always-Free 1000-email/month envelope from persistent-breach spam; (4) the STATE row carries TWO alert attributes — `alert_flag` (current-invocation breach, the dashboards' "red now" read) and `last_alert_sent_at` (set on publish, carried forward otherwise, absent until first publish).

Cold-start gains a required `SNS_TOPIC_ARN` env var (`KeyError` at init if unset — same fail-fast posture as the reference eager-load) and a module-level SNS client (same boto3 already in the deploy zip; ADR 0006 §Q4 footprint baseline re-verified). The parity boundary is untouched; `compute_psi` enters Lambda mode as a `shared/` peer import with a fourth structural-parity guard. Tests: 361 + 1 → **368 passed + 1 skipped** (+7, breakdown in the session log).

One empirical discovery worth your attention: the `_interfaces.md` example telemetry values fall OUTSIDE the operational reference's per-feature ranges, so a CONSTANT window of "healthy-looking" values breaches PSI hard (measured: 31 constant defaults → max PSI 2.358). The alert tests therefore seed windows that cycle the reference's own bin midpoints (30 spanning + 1 → max PSI 0.005). A warning landed in `_interfaces.md §Telemetry payload`.

## Key code (new hot-path tail, `lambda_scorer/handler.py`)

```python
# Single Query serves both windows (Limit widened 150 → 1800)
response = TABLE.query(
    KeyConditionExpression=(
        Key("pump_id").eq(pump_id) & Key("sk").begins_with("2")
    ),
    Limit=PSI_WINDOW_SAMPLES,
    ScanIndexForward=False,
)
...
window = window[-PSI_WINDOW_SAMPLES:]
features = extract_features(window[-WINDOW_SAMPLES:])
score_value = score_fn(features)
psi = compute_psi(window, reference=REFERENCE)   # every invocation

psi_breach = max(psi.values()) > PSI_ALERT_THRESHOLD       # 0.25
score_breach = score_value > SCORE_ALERT_THRESHOLD         # 0.7
alert_flag = psi_breach or score_breach

TABLE.put_item(Item=reading_item)                # unchanged from MVP

# Edge-trigger input (ADR 0012): previous flag + carry-forward
prev_state = TABLE.get_item(
    Key={"pump_id": pump_id, "sk": STATE_SK}
).get("Item") or {}
prev_alert_flag = bool(prev_state.get("alert_flag", False))
publish_alert = alert_flag and not prev_alert_flag

state_item = {
    "pump_id": pump_id, "sk": STATE_SK,
    "latest_ts": ts, "latest_score": _to_decimal(score_value),
    "latest_psi": {name: _to_decimal(v) for name, v in psi.items()},
    "alert_flag": alert_flag,
}
if publish_alert:
    state_item["last_alert_sent_at"] = ts
elif "last_alert_sent_at" in prev_state:
    state_item["last_alert_sent_at"] = prev_state["last_alert_sent_at"]
TABLE.put_item(Item=state_item)

if publish_alert:                                # AFTER the STATE write
    payload = {
        "pump_id": pump_id, "ts": ts,
        "alert_type": _alert_type(psi_breach, score_breach),
        "score": float(score_value),
        "psi": {name: float(v) for name, v in psi.items()},
    }
    _SNS.publish(TopicArn=SNS_TOPIC_ARN, Message=json.dumps(payload))
```

## Questions for the reviewer

1. **Edge-trigger ordering (ADR 0012 §Consequences).** Publish-after-write makes a publish failure lose the alert (loud CloudWatch error, no retry-republish because the retry sees `prev alert_flag == True`). Publish-before-write would convert that to duplicate alerts on write failure. We chose lost-but-loud over silent-duplicate at demo scale. Is the reasoning sound, or does the alert path deserve the duplicate-tolerant ordering even here?
2. **Read-modify-write race on the STATE row.** The `GetItem` → `PutItem` pair is not transactional. We argue the only writer per pump partition is this handler at ~0.5 msg/sec serial, so the race is theoretical; ADR 0010's `TransactWriteItems` upgrade path covers the future. Push back if you think IoT Rule at-least-once delivery + retry overlap can interleave two invocations for the SAME pump closely enough to double-publish in practice.
3. **Every-invocation PSI.** ~2 ms numpy × 450/min fleet-wide. We dismissed the `latest_ts`-gated cadence as complexity without payoff. Anything we missed — e.g., Lambda CPU-time billing interactions at 512 MB?
4. **Constant-window PSI behaviour.** A pump flatlining at ANY constant value (including mid-reference values) breaches PSI within ~3 samples of window turnover, because all mass concentrates in one bin. We documented this as correct-by-design ("flatline IS distribution shift") rather than adding a variance floor. Agree?
5. **`SNS_TOPIC_ARN` required + conftest `setdefault` placeholder.** Is the module-level env mutation in `conftest.py` an acceptable test-infrastructure cost for the production fail-fast posture, or a debt signal like the `_reset_reference_cache` helper Gemini flagged in the 2026-06-01 review?
6. **Two-attribute alert state.** `alert_flag` (now) + `last_alert_sent_at` (last publish). Alternative shapes in ADR 0012 §Alternatives 2. Any consumer scenario we haven't anticipated where this still under-serves?

## Test evidence

- 368 passed + 1 skipped (sandbox, 18.14 s). +7 over the post-MVP baseline; all four structural-parity guards green.
- Measured PSI mechanics for the alert tests (probe against the committed reference): 11 extreme → max PSI 1.171 / score 0.824; 31 constant defaults → 2.358; 30 reference-spanning + 1 → 0.005 / 0.067; single reading → 0.057.
- Footprint: no new heavy imports; ADR 0006 §Q4 ~124 MB baseline holds.

## Files in this change

- `lambda_scorer/handler.py` (267 → 417)
- `lambda_scorer/tests/conftest.py` (68 → 96)
- `lambda_scorer/tests/test_handler.py` (337 → 602; 11 → 18 tests)
- `docs/adr/0012-edge-triggered-sns-alerts.md` (new)
- `context/_interfaces.md`, `context/lambda_scorer.md` (updated)
- `docs/sessions/2026-06-02-lambda_scorer-psi-followon.md` (session log)
