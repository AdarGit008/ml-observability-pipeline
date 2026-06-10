# Review Packet 2026-06-10 — lambda_fleet_psi — fleet-psi-lambda

> Run via: `.\scripts\run_review.ps1 -Slug fleet-psi-lambda` (DeepSeek, ADR 0011 §Addendum 2026-06-10)

## Role for the reviewer model
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.
6. (Operational corollary) Any local/AWS divergence in scoring/drift is a bug or an ADR.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`.

## Summary of the change

Builds the last pipeline component (PLAN.md §2.7): `lambda_fleet_psi`, an EventBridge-scheduled (`rate(5 minutes)`) plant-wide drift detector. It reads the trailing 5-minute window (150 rows) from each pump `P-01..P-NN` via the scorer's `Query` pattern, **pools** them into ONE window, runs `shared.drift.compute_psi` once → a single fleet PSI, and writes it to a `pump_id="FLEET"` STATE row. It edge-triggers SNS on the False→True `alert_flag` flip (ADR 0012) via the shared `shared.drift.psi_alert_should_fire` (ADR 0017 — warmup gate + 0.25 threshold), reusing the scorer's topic; the FLEET payload is PSI-only (no `score`). Drift-only deployment: the zip ships numpy + `shared/{features,drift}.py` + the reference JSON, no sklearn/model.pkl (`load_reference` skips the version check when `model.pkl` is absent, ADR 0007 §4). Scope this session: handler + tests + ADR 0018 + context; Terraform/build/teardown deferred to an infra session.

## Changed / new files

- `lambda_fleet_psi/handler.py` — new. Cold start + `compute_fleet_psi(now_iso)` core (clock injected) + `handler` entry.
- `lambda_fleet_psi/tests/{conftest,test_handler}.py` — new. 9 moto tests (3 structural-parity, cold-start, pooling/healthy, empty no-op, drifting edge-publish + no-republish, warmup-gate).
- `docs/adr/0018-fleet-psi-eventbridge-lambda.md`, `context/lambda_fleet_psi.md` — new.
- `context/_interfaces.md`, `context/drift.md`, `context/infra.md` — cross-refs + the deferred-Terraform note.

## Core hunk — the pooling + alert core

```python
def compute_fleet_psi(now_iso: str) -> dict[str, Any]:
    pooled: list[dict[str, float]] = []
    pumps_reporting = 0
    for pump_id in FLEET_PUMP_IDS:
        window = _read_pump_window(pump_id)          # Query trailing 150, reversed
        if window:
            pumps_reporting += 1
            pooled.extend(window)

    if not pooled:
        return {"fleet_max_psi": None, "pumps_reporting": 0,
                "alert_flag": False, "published": False}     # empty fleet = no-op

    psi = compute_psi(pooled, reference=REFERENCE)            # ONE pooled fleet PSI
    alert_flag = psi_alert_should_fire(pooled, psi)          # shared gate + threshold

    prev_state = TABLE.get_item(Key={"pump_id": FLEET_PK, "sk": STATE_SK}).get("Item") or {}
    publish_alert = alert_flag and not bool(prev_state.get("alert_flag", False))

    state_item = {"pump_id": FLEET_PK, "sk": STATE_SK, "latest_ts": now_iso,
                  "latest_psi": {n: _to_decimal(v) for n, v in psi.items()},
                  "alert_flag": alert_flag, "pumps_reporting": pumps_reporting}
    if publish_alert:
        state_item["last_alert_sent_at"] = now_iso
    elif "last_alert_sent_at" in prev_state:
        state_item["last_alert_sent_at"] = prev_state["last_alert_sent_at"]
    TABLE.put_item(Item=state_item)                          # write THEN publish (ADR 0012)

    if publish_alert:
        payload = {"pump_id": FLEET_PK, "ts": now_iso, "alert_type": "psi_breach",
                   "psi": {n: float(v) for n, v in psi.items()},
                   "pumps_reporting": pumps_reporting}        # no score field
        _SNS.publish(TopicArn=SNS_TOPIC_ARN, Message=json.dumps(payload))
    ...
```

## Specific questions for the reviewer

1. **Pooling statistics.** Pooling all pumps' readings into one window then PSI-ing against a reference built from per-pump HEALTHY data (ADR 0008) — is that statistically sound? Concerns to probe: (a) does pooling 15 pumps mask a single badly-drifting pump (it contributes ~1/15 of the mass)? (b) could heterogeneous-but-healthy pumps pool into a *wider* distribution that reads as drift vs a per-pump reference? Is "one pooled PSI vs the per-pump reference" the right comparison, or should the fleet compare against a *pooled* reference?

2. **FLEET SNS payload omits `score`.** The per-pump `_interfaces.md §SNS alert payload` always carries `score`; the FLEET payload drops it and relies on `pump_id=="FLEET"` to signal scope. Acceptable, or should it carry an explicit `scope:"fleet"` field / `score:null` for consumer robustness?

3. **Shared SNS topic.** Fleet alerts publish to the scorer's topic. Any problem with per-pump and fleet alerts interleaving on one subscription (email), or should the fleet have its own topic? Cost/north-star-#1 implications either way.

4. **Hot-table read at 5-min cadence.** 15 Queries (Limit=150) + 1 GetItem + 1 PutItem per run, every 5 min. Cost fine vs ADR 0013? The `_read_pump_window` assumes Limit=150 is a single page (no pagination loop) — safe given ScanIndexForward=False returns the newest 150? Any correctness risk if a pump has >150 rows in 5 min (it shouldn't at 0.5 Hz, but)?

5. **Empty vs always-write.** Empty fleet is a no-op (no FLEET row). Should the dashboard instead always get a FLEET row (even zero/low PSI) so a panel never reads "missing"? Trade-off vs the batcher's empty-no-op precedent.

6. **Warmup gate at fleet scale.** The pooled window is normally huge (≥150 trivially once any pump reports >150), so `psi_is_armed` is almost always satisfied — is the gate meaningful here, or purely vestigial-for-parity? Is keeping it (vs a fleet-specific arming rule) the right call?

## What I'm NOT looking for
- Terraform / IAM / packaging — explicitly deferred to a follow-on infra session (ADR 0018 §Follow-ups).
- Re-litigating ADR 0007/0008/0009/0012/0017 — accepted and unchanged.
- Style / formatting — linter.

## Resolution (filled in by Claude after the reviewer responds)

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. Pooling statistics (flagged "blocking") | **Resolved — premise corrected + caveat** | Premise (per-pump REFERENCE) is wrong: the reference is the SINGLE 15-pump pooled operational artifact (`OPERATIONAL_REFERENCE_PUMPS=15`, ADR 0008); `load_reference` has no `pump_id`. Pooling vs it is apples-to-apples → sound, no refactor. Added the late-indicator/systemic-shift caveat to handler+ADR+context. |
| 2. Score-less FLEET payload | **Accepted** | Added `scope:"fleet"` + `score:null`; `_interfaces` note + test updated. |
| 3. Shared SNS topic | **Accept** | Lowest cost; `scope`/`pump_id` differentiate. |
| 4. Hot-table read / pagination | **Accept w/ guard** | `log.warning` on `LastEvaluatedKey` + 1MB/150-row comment; warn-not-fail matches the scorer. |
| 5. Empty vs always-write | **Accept** | No-op correct (missing = "no data", not "no drift"); batcher precedent. |
| 6. Warmup gate at fleet scale | **Accept w/ comment** | Vestigial-but-parity; annotated in the handler. |
| minor: FLEET_PUMP_IDS hardcode / references/FLEET.json | **Rejected / N-A** | FLEET_SIZE→P-NN is the batcher convention; no per-pump reference exists (see §1). |
