# Session — Close the Grafana/Infinity open items (2026-06-14)

**Component:** dashboards (AWS-mode, Infinity datasource) · **Cost:** $0 (Path A local mock; no AWS apply) · **Not a parity session** (no field-vocabulary change; `dashboards/tests/test_dashboard_vocabulary.py` green throughout).

## Goal
Close the last two verification-gap items in `context/dashboards.md §Open questions`:
1. **Infinity relative-URL-against-datasource-base join** — panels use empty `url: ""` + the `infinity-aws` datasource base (`$ADAPTER_FUNCTION_URL`). Unverified that Infinity joins empty/relative panel URLs to the base.
2. **`$.fleet` single-object → one-row render** — the 4 fleet PSI gauges + the fleet alert-state table bind `root_selector: "$.fleet"` (a single JSON object, not an array).

Both had been un-observable at prior live applies (Grafana was never opened against a live adapter; 2026-06-11 part-2 couldn't open Grafana at all).

## Approach — Path A (local mock, $0, no re-apply)
Both are Grafana/Infinity-side **rendering** questions; Infinity behaves identically whether the datasource base points at a live Lambda Function URL or a local server. So instead of a live re-apply, I served the real envelope shape from the host:

- **`scripts/mock_adapter.py`** (new) — ~15-line `http.server` returning the ADR 0014 + ADR 0018 envelope `{fleet_size, pumps_reporting, as_of, pumps[], fleet}`. Binds `0.0.0.0:8899` so the Grafana container reaches it via `host.docker.internal`. Logs every request (the path proves how Infinity joined the empty panel URL). Deliberately a **breaching fleet** (`alert_flag:true`, `psi_bearing_temp:0.31 > 0.25`, `pumps_pooled:15`, `last_alert_sent_at` set) and 14 pumps with `last_alert_sent_at: null` + P-14 alerting with a real timestamp — so the null→"never" path and the populated-alert path render in the same view.
- Sandbox pre-flight before any Docker: validated the envelope shape against the contract and cross-checked **all 13 `aws.json` panel selectors resolve** (the 5 `$.fleet` panels each resolve to one row); GET→200 / POST→405.
- Run: Terminal 1 `python scripts/mock_adapter.py`; Terminal 2 (repo root) `$env:ADAPTER_FUNCTION_URL="http://host.docker.internal:8899/"` then `docker compose up -d --force-recreate grafana`.

## Pre-flight gotcha (gotcha #1, again)
First `docker ps` / `docker compose` 500'd on every API call (`...dockerDesktopLinuxEngine/v1.54/... 500 ... check if the server supports the requested API version`) — the Docker Desktop **engine was wedged**, not a compose/config issue. `docker version` showed Client populated but the failure was engine-side; a Docker Desktop restart cleared it (Client+Server both API 1.54, no 500). This is the same "Grafana wouldn't open" blocker as 2026-06-11 — diagnose the engine first.

## Findings (live)
- **Item 1 — CLOSED.** Every panel's empty `url: ""` joined onto the datasource base URL and returned data (snapshot table + all bar gauges + fleet gauges populated). Infinity **does** join empty/relative panel URLs to the datasource base. Verified against the local mock; identical mechanics against a live Function URL.
- **Item 2 — CLOSED.** The breaching FLEET object rendered the four fleet gauges as one value each (0.140 / **0.310 red** / 0.190 / 0.120) and the fleet alert-state table (panel 13) as exactly **one clean row** (pumps pooled 15 / ALERT / `2026-06-14T13:55:00.000Z`). `root_selector: "$.fleet"` single-object → one-row works.

Two **latent render bugs** the contract-only closures had missed surfaced once observed live:

- **Panel 1 "Pumps reporting" → `null`/`null`** (should be 15/15). The lone `root_selector: ""` panel: Infinity's default parser **auto-descends an empty root into the first child array** (`pumps[]`), so the top-level scalars `pumps_reporting`/`fleet_size` resolved against pump objects → null. A JSONata object-construction root (`{...}`) is **unsupported** by the default parser (rendered blank). **Fix: `root_selector: "$"`** — the bare root path resolves to the root object as a single row (same single-object mechanism `$.fleet` uses) → 15/15. *(One live iteration: tried JSONata first, it blanked, then `$`.)*
- **`last_alert_sent_at` null → literal `"null"`, not `"never"`** (the "bonus" the brief marked contract-closed 2026-06-07 — never actually exercised live, because the 2026-06-07 PSI warmup storm set the timestamp on every pump). The `special: match "null"` mapping does **not** fire because Infinity serialises the wire `null` as the literal STRING `"null"` in a `type:string` column. **Fix: add a `type:"value"` mapping (`"null" → "never"`)** on every `last_alert_sent_at` column (the dead `special: match null` mapping was then REMOVED in the review fold — see Review dispositions). Applied to **panels 8 + 13** (had the special mapping) and **panel 2** (the "Fleet snapshot" table — had no mapping at all, was showing `"null"`; added for cross-table consistency). Re-observed: all three tables show "never" on null rows, real timestamps otherwise.

## Changes
- `dashboards/aws.json` — panel 1 `root_selector "" → "$"`; `null→never` value mapping added to `last_alert_sent_at` overrides on panels 2, 8, 13. **No field-vocabulary change** (column selectors untouched).
- `context/dashboards.md` — both items struck CLOSED with evidence; the contract-only null closure corrected; panel-1 finding recorded; ✅ line in §Current state.
- `scripts/mock_adapter.py` — new local verification tool (commit optional — it's a throwaway harness, but harmless and reusable for the next demo rehearsal).

## Verification
- `dashboards/tests/test_dashboard_vocabulary.py` — **7 passed** after every edit (parity-surface guard: psi tokens, Infinity selectors ⊆ ADR 0014 keys, uid pairing, 5 s refresh).
- Live re-observation after each fix (screenshots): panel 1 = 15/15; panels 2/8/13 null rows = "never"; fleet gauges + one-row fleet table intact.
- `terraform.tfstate` empty (zero resources) — $0 maintained; no AWS touched this session.

## Handoff (git is PO-side)
Commit: `dashboards/aws.json` (the 3-panel fix) + `context/dashboards.md` (closure) + this session log; `scripts/mock_adapter.py` optional. Suggested message: `dashboards: close Infinity relative-URL + $.fleet items; fix panel-1 scalar read + null→never mapping`. Recommend a DeepSeek review pass on the `aws.json` diff (small, JSON-only, no parity impact).

## Cleanup
Ctrl-C the mock (Terminal 1); `docker compose stop grafana` (optional — all local, $0 either way).

## Out of scope (unchanged separate follow-ups)
F1 INFO-log suppression · reserved concurrency `-1` (Service Quotas bump) · SSOT dedup of `FLEET_PUMP_IDS` (3 copies). The empty-fleet `{}` → "No data" render was not re-exercised (mock served a populated fleet) — contractually handled, low-risk.

## Lessons
- **Contract reasoning ≠ live render behaviour.** Two items "closed by contract" were wrong/incomplete when finally observed: the special-null mapping is a trap for **stringified** JSON nulls, and an **empty Infinity `root_selector` auto-descends** into a child array. A $0 local mock that reproduces the exact wire shape (incl. nulls + top-level scalars) catches these without an apply.
- Diagnose the **Docker engine** (`docker version` Server section) before blaming compose/config — the 500-on-every-call signature is a wedged Desktop engine.

## Review dispositions (DeepSeek `deepseek-reasoner`, 2026-06-14)

Packet `review_packets/2026-06-14-grafana-infinity-close.md` → response `review_responses/2026-06-14-grafana-infinity-close.md`. Strong pass — core fixes accepted (`"$"` root selector, panel-2 inclusion, mock-vs-live equivalence).

- **Q1 `root_selector:"$"`** — accepted, idiomatic, no auto-descend regression. Recommends pinning the Infinity plugin version (`docker-compose.yml` `GF_INSTALL_PLUGINS: yesoreyeram-infinity-datasource` is unpinned). **Tracked follow-up** — needs the live-installed version (`docker compose exec grafana grafana cli plugins ls`); not folded this pass.
- **Q2 null→never** — **FOLDED:** the `special: match null` mapping is dead on a `type:string` column (never fires); removed from panels 2/8/13, the `type:"value"` mapping alone retained. `aws.json` re-diffed, vocab tests 7/7 green.
- **Q3 panel-2 scope** — accepted (correct, not creep).
- **Q4 guard rule** — **FOLDED:** explicit string-null rule added to `context/dashboards.md §Open questions` ("a `type:string` column renders a wire `null` as the string `"null"`; `special: match null` does NOT fire — use a `type:"value"` map, never `special`") + memory [[ml-obs-pipeline-infinity-render-gotchas]].
- **Q5 mock-vs-live** — safe to close on; documented that closure used a live-shaped mock (no implicit e2e-live promise).
- **Q6 empty `fleet:{}`** — left untested (mock served a populated fleet); verified-by-contract, not re-tested 2026-06-14 (low-risk demo-day gate).
- Extra notes: `"$"` assumes an object root (true per the ADR 0014 envelope); value-mapping ordering moot post-removal; panel-1 stat two-column layout fine.

**Net follow-on change:** `dashboards/aws.json` — removed 3 dead `special` mappings (value mapping only). One optional re-observe (render-neutral; the value mapping at index 0 is the path that was already confirmed firing).
