# Session 2026-06-10 — dashboards — FLEET PSI panel + adapter `fleet` object

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** DeepSeek (`deepseek-reasoner`) — see response file footer (ADR 0011 §Addendum 2026-06-10)
- **Context loaded:** `_global`, `dashboards`, `_interfaces`, `lambda_fleet_psi`; `shared/features.py` + ADR 0005 (Tier 2b — dashboards is in the parity set)
- **Duration:** ~1h

## Intent
Surface the pooled plant-wide FLEET PSI row (built last session, ADR 0018) on the AWS-mode dashboard: extend the adapter's `BatchGetItem` + JSON envelope to carry the `pump_id="FLEET"` STATE row, and add an AWS-only FLEET PSI + alert panel to `dashboards/aws.json`. Dashboard + adapter-contract only — **NO `terraform apply` ($0)**.

## What changed
- `dashboards_adapter/handler.py` — `FLEET_PK="FLEET"`; `_STATE_ROW_IDS = FLEET_PUMP_IDS + (FLEET_PK,)` (one BatchGetItem, now 16 keys); `_batch_get_state_rows(state_ids)` (param renamed — no longer pump-only); new `_fleet_entry` projection; `_snapshot` partitions the FLEET row out of `pumps` and adds the top-level `fleet` object (or `null`). Still no `shared/` import, no threshold literal.
- `dashboards_adapter/tests/conftest.py` — `put_fleet_state_row` helper (FLEET shape: no `latest_score`, + `pumps_reporting`).
- `dashboards_adapter/tests/test_adapter.py` — §FLEET row section (5 new tests); read-efficiency test → 16 keys incl. FLEET; cold-start test asserts `_STATE_ROW_IDS`. Adapter package 16 → 22 tests.
- `dashboards/aws.json` — panels 9–13 (AWS-only): 4× fleet-PSI `gauge` (`root_selector:"$.fleet"`, 0.10/0.25 bands) + fleet alert-state `table` (`pumps_reporting`, OK/ALERT, null→"never").
- Docs: ADR 0014 §Addendum 2026-06-10 (additive `fleet` contract bump); `context/_interfaces.md §FLEET object`; `context/dashboards.md` current-state + open questions + ADR 0018 cross-ref.
- PR: _(fill in at commit)_

## Decisions
- **Additive contract bump, not a new ADR** — folded into ADR 0014 §Addendum, as ADR 0018 §Follow-ups anticipated ("a small, additive change to ADR 0014's contract"). The `fleet` object is a sibling of `pumps`; `pumps[]` and all existing keys are byte-for-byte unchanged.
- **FLEET projected by a dedicated `_fleet_entry`, never `_pump_entry`** — the FLEET row has no `latest_score` (no model on the fleet path, ADR 0018 §5), which `_pump_entry` hard-reads. `fleet` omits `latest_score` entirely (not null) to make "no model here" explicit; adds `pumps_pooled` (pooled-window count, renamed on the wire from the row's `pumps_reporting` after DeepSeek §2).
- **`fleet` is an empty object `{}` when the FLEET row is absent** (fleet Lambda not run / empty no-op; PO call after DeepSeek §3) — the single-object analogue of the per-pump omit-the-row rule; keeps a `null` off Infinity's `$.fleet` root selector and fabricates no `alert_flag` all-clear.
- **FLEET is AWS-only** — local InfluxDB has no FLEET row / alert fields (ADR 0005 §3), so the panel is in `aws.json` only; the panel-vocabulary parity test needs no new allowed name (selectors already in `_ADAPTER_KEYS`).

## Trade-offs surfaced
- **`pumps_reporting` name overload** — top-level (`pumps with a STATE row`) vs `fleet.pumps_reporting` (`pumps in the pooled window`). Kept the verbatim storage-attribute name and relied on nesting to disambiguate, over renaming the wire field. (Reviewer question #2.)
- **One BatchGetItem for FLEET too** — FLEET shares `sk="STATE"`, so it rides the existing batch for free rather than a separate GetItem; trades partition isolation for one round trip (ADR 0013 cost win). (Reviewer question #6.)
- **`_fleet_entry` hard-reads** the FLEET attributes (KeyError → 500), consistent with `_pump_entry`, despite the row coming from a different Lambda. (Reviewer question #4.)

## Reviewer feedback highlights
- Reviewer: **deepseek** (`deepseek-reasoner`), 2026-06-11. Architecture passed; two wire-contract calls escalated to PO.
- **§2 renamed** the fleet pooled count to `pumps_pooled` on the wire (PO call) — disambiguates from the top-level `pumps_reporting`.
- **§3 absent fleet → empty `{}`** (PO call) — not `null` (Infinity null-at-root risk) and not the reviewer's default-object (it would synth a false `alert_flag:false` all-clear).
- §4 added `test_fleet_row_missing_required_field_is_500` (kept hard-reads; declined the per-path `.get`). §1 documented the `as_of`/`latest_ts` skew. C was already covered.
- Packet (with dispositions): `review_packets/2026-06-10-dashboards-fleet-panel.md` → Response: `review_responses/2026-06-10-dashboards-fleet-panel.md`.

## State at end of session
- Tests (post-review): adapter `dashboards_adapter/tests/test_adapter.py` **23** + dashboards vocabulary **7** = 30 passed (sandbox); 3 structural-parity tests **green** — parity intact (no `shared/` touched). Full-suite re-run is PO-side.
- Open follow-ups: (1) `$.fleet` single-object + `null` render — live-verify against a running adapter (folds into demo-day rehearsal with the relative-URL item); (2) `pumps_reporting==15` adapter redeploy-verify still belongs to the live session; (3) `lambda_fleet_psi` enumerates `P-01..P-NN` (1-indexed) — same single-source-of-truth concern as the adapter's old off-by-one, a separate component, flagged in `context/dashboards.md`.
- `context/dashboards.md` updated? yes.

## Note for next session
The FLEET row is now visible end-to-end in the contract + dashboard, but unverified against a LIVE adapter (this session was sandbox + $0, no apply). At the next live apply / demo rehearsal: open Grafana against the live Function URL and confirm (a) the 4 fleet gauges + alert table render from `$.fleet`, (b) a `null` `fleet` shows "No data" not an error, and (c) `pumps_reporting==15` (the off-by-one redeploy-verify). Separately, if a fleet session reopens, reconcile `lambda_fleet_psi`'s 1-indexed `P-01..P-NN` enumeration with the adapter's 0-indexed `P-00..P-{NN-1}`.
