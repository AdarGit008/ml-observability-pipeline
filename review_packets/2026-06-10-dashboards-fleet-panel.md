# Review Packet 2026-06-10 — dashboards — FLEET PSI panel + adapter `fleet` object

Session log: `docs/sessions/2026-06-10-dashboards-fleet-panel.md`

## Role for the reviewer model

You are an adversarial-but-fair reviewer for a portfolio ML-observability pipeline. Your job is not to rubber-stamp. Surface risks, contract leaks, and demo-day failure modes. Cite files/lines. This change surfaces the pooled plant-wide FLEET PSI row (built last session, ADR 0018) on the AWS-mode Grafana dashboard via a small additive bump to the ADR 0014 adapter contract.

## Project north stars (constraint anchors)

- $0 lifetime cost — Grafana OSS local only; **NO `terraform apply` this session** (dashboard + adapter-contract only). One extra BatchGetItem key (15 → 16) is noise on ADR 0013's math.
- Mode parity — local (InfluxDB) and AWS (adapter) share one panel-level field vocabulary (`psi_<feature>` from `shared.features.PSI_FEATURE_NAMES`, ADR 0005 §3 / ADR 0009). FLEET is **AWS-only** (local InfluxDB has no FLEET row / alert fields, ADR 0005 §3).
- ADR 0014: the adapter is a snapshot **projection, not a brain** — imports no `shared/`, evaluates no threshold, computes nothing.
- ADR 0012 §2C: `alert_flag` + `last_alert_sent_at` are literal passthroughs — consumers NEVER re-derive breach state.
- ADR 0018: `lambda_fleet_psi` writes a `pump_id="FLEET", sk="STATE"` row (`latest_psi` 4-key Map, `alert_flag`, `pumps_reporting`, `[last_alert_sent_at]`; NO `latest_score` — no model on the fleet path).

## Summary of the change

The adapter now requests the `FLEET` aggregate STATE row alongside the 15 pump rows in the SAME `BatchGetItem` (16 keys) and surfaces it as a new top-level `fleet` object beside `pumps` — **additive**: `pumps[]` and every existing key are untouched. The FLEET row is projected by a dedicated `_fleet_entry` (same flatten/Decimal/null rules as `_pump_entry`, MINUS `latest_score`, PLUS `pumps_reporting`); `fleet` is JSON `null` when the row is absent (fleet Lambda not run / empty no-op). `dashboards/aws.json` gains 5 AWS-only panels (4 fleet-PSI gauges on `$.fleet` + a fleet alert-state table). Contract bump folded into ADR 0014 §Addendum 2026-06-10 + `_interfaces.md`. No `terraform apply`. Adapter tests 16 → 22; dashboards vocabulary tests (7) unchanged + green; 3 structural-parity tests green.

## Changed files

- `dashboards_adapter/handler.py` (edited) — `FLEET_PK="FLEET"`, `_STATE_ROW_IDS = FLEET_PUMP_IDS + (FLEET_PK,)`, `_batch_get_state_rows(state_ids)` (renamed param), new `_fleet_entry`, `_snapshot` partitions FLEET out of `pumps` and adds `fleet`. Still no `shared/` import, no threshold literal.
- `dashboards_adapter/tests/conftest.py` (edited) — `put_fleet_state_row` helper (pump_id="FLEET", no `latest_score`, + `pumps_reporting`).
- `dashboards_adapter/tests/test_adapter.py` (edited) — §FLEET row section (surfaced-as-object, excluded-from-`pumps`+top-level-count, absent→null, alert passthrough, never-alerted→null); read-efficiency test updated to 16 keys incl. FLEET; cold-start test asserts `_STATE_ROW_IDS`.
- `dashboards/aws.json` (edited) — panels 9–13: 4× `gauge` (one per pooled `psi_<feature>`, `root_selector:"$.fleet"`, 0.10/0.25 bands) + `table` (FLEET alert state: `pumps_reporting`, OK/ALERT map, null→"never").
- `docs/adr/0014-grafana-adapter-api-contract.md` (appended) — §Addendum 2026-06-10 (FLEET object, additive contract bump).
- `context/_interfaces.md` (appended) — §FLEET object under §Grafana → DynamoDB adapter.
- `context/dashboards.md` (edited) — current-state bullet + 2 open questions (`$.fleet` single-object live render; fleet-Lambda 1-indexed note) + ADR 0018 in Related ADRs.

## Key code (the review-worthy core)

BatchGetItem key set (one round trip, FLEET appended):
```python
FLEET_PK: str = "FLEET"
_STATE_ROW_IDS: tuple[str, ...] = FLEET_PUMP_IDS + (FLEET_PK,)
# handler(): items = _batch_get_state_rows(_STATE_ROW_IDS)
```

FLEET projection — separate from `_pump_entry` (no `latest_score`, + `pumps_reporting`):
```python
def _fleet_entry(item):
    entry = {"latest_ts": item["latest_ts"]}
    for feature, value in item["latest_psi"].items():
        entry[f"psi_{feature}"] = float(value)
    entry["alert_flag"] = bool(item["alert_flag"])
    entry["last_alert_sent_at"] = item.get("last_alert_sent_at")
    entry["pumps_reporting"] = int(item["pumps_reporting"])
    return entry
```

Partition (FLEET never enters `pumps`; envelope top-level `pumps_reporting` counts pumps only):
```python
def _snapshot(items):
    fleet_item, pump_items = None, []
    for item in items:
        (fleet_item := item) if item["pump_id"] == FLEET_PK else pump_items.append(item)
    pumps = sorted((_pump_entry(i) for i in pump_items), key=lambda p: p["pump_id"])
    return {"fleet_size": FLEET_SIZE, "pumps_reporting": len(pumps),
            "as_of": _iso_now(), "pumps": pumps,
            "fleet": _fleet_entry(fleet_item) if fleet_item is not None else None}
```
(actual source uses an explicit if/else, not the walrus — shown compact here.)

## Specific questions for the reviewer

1. **Additive-ness.** Is `fleet` as a sibling key of `pumps` (object-or-null) the right additive shape, vs. e.g. nesting fleet inside the envelope differently? Any Infinity consumer that would break on a NEW top-level key? (We believe none — Infinity selects by explicit `root_selector`/column.)
2. **`pumps_reporting` name overload.** Top-level `pumps_reporting` = pumps with a STATE row; `fleet.pumps_reporting` = pumps in the pooled 5-min window. Same name, different scopes, different nesting. Is the verbatim-passthrough naming worth the collision risk, or should the fleet one be renamed (e.g. `pumps_pooled`) at the wire — at the cost of diverging from the storage attribute name?
3. **`null` fleet render.** When no FLEET row exists, `fleet` is JSON `null`. The 4 gauges + table bind `root_selector:"$.fleet"`. Does Infinity degrade cleanly to "No data" on a null object, or could it error? (Flagged as a live-verify open question; is contract-level handling enough pre-demo?)
4. **`_fleet_entry` hard-reads `pumps_reporting`/`latest_psi`/`alert_flag`.** A malformed FLEET row → KeyError → 500 (generic body). Consistent with `_pump_entry`'s hard reads, but the FLEET row is written by a different Lambda (`lambda_fleet_psi`) — acceptable, or should the adapter be defensive (`.get`) on the fleet path?
5. **AWS-only asymmetry.** FLEET panel is in `aws.json` only (no local counterpart). Same asymmetry as the existing alert-state panels. Anything that should still appear in `local.json` for parity, or is the ADR 0005 §3 "local has no FLEET" the clean line?
6. **One BatchGetItem vs. splitting FLEET.** FLEET shares `sk="STATE"`, so it rides the existing batch for free. Any reason to read it separately (isolation, partial-failure semantics) that outweighs the one-round-trip win?

## What I'm NOT looking for in this review

- Re-litigating ADR 0014 (AuthType=NONE, Infinity, snapshot contract) or ADR 0018 (fleet pooling, the 1-indexed `lambda_fleet_psi` enumeration — that's a separate component, noted in `context/dashboards.md`).
- Panel aesthetics / gridPos taste.
- Terraform / deploy — explicitly out of scope this session ($0, no apply); the `pumps_reporting==15` redeploy-verify and any fleet-PSI infra wiring belong to the live/infra sessions.

## Resolution (filled in by Claude after the reviewer responds)

Reviewer: **deepseek** (`deepseek-reasoner`), 2026-06-11. Architecture passed
(additive, single-batch, no `shared/`, AWS-only line). Two wire-contract calls
went to the PO (points 2 + 3); the rest were folded directly.

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. additive shape | Accepted | `fleet` sibling of `pumps` kept. Skew caveat (`as_of` vs `latest_ts`) now documented in the handler docstring + `_interfaces.md` + ADR addendum. |
| 2. `pumps_reporting` overload | **Accepted — renamed** (PO call) | Wire field is now `pumps_pooled` (storage attr stays `pumps_reporting`); `_fleet_entry`, aws.json panel 13, `_ADAPTER_KEYS`, tests + docs updated. |
| 3. null fleet render | **Accepted — empty `{}`** (PO call) | Absent FLEET row → `fleet: {}` (not `null`, not a fabricated default object — the reviewer's default-object would synth `alert_flag:false`, a false all-clear). Dodges Infinity null-at-root; mirrors the per-pump omit-the-row rule. |
| 4. hard-read vs defensive | Accepted (test added; no `.get`) | Kept hard-reads symmetric with `_pump_entry`; added `test_fleet_row_missing_required_field_is_500` pinning the 500. Declined the per-path `.get`+warn (would diverge from `_pump_entry` and mask a malformed row from the writer). |
| 5. AWS-only asymmetry | Accepted, no change | ADR 0005 §3 is the clean line; FLEET stays out of `local.json`. |
| 6. single BatchGetItem | Accepted, no change | One batch; FLEET shares `sk="STATE"`. Reviewer's unordered-response note confirmed handled by the partition-by-`pump_id` in `_snapshot`. |
| C. assert FLEET key in read-eff test | Already present | `test_single_batch_get_item_per_invocation` already asserts `{"pump_id":"FLEET","sk":"STATE"} in keys`. |
| A / B / D | Accepted, no change | Top-level `pumps_reporting` correctly excludes FLEET; `lambda_fleet_psi` writes `Decimal` floats (verified, separate component); table boolean/null mappings covered by tests. |
| E. resolve open questions pre-demo | Tracked | `$.fleet` live render + fleet-Lambda 1-indexed note remain in `context/dashboards.md §Open questions` for the demo-day rehearsal (no live stack this $0 session). |
