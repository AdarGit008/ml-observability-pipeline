# Review Packet 2026-06-14 — dashboards — close Infinity relative-URL + `$.fleet` items (panel-1 + null→never fixes)

Session log: `docs/sessions/2026-06-14-grafana-infinity-close.md`

## Role for the reviewer model

You are an adversarial-but-fair reviewer for a portfolio ML-observability pipeline. Do not rubber-stamp. Surface render-time failure modes, contract leaks, and demo-day risks. Cite files/lines. This change closes the last two verification-gap items in `context/dashboards.md §Open questions` (Infinity relative-URL-against-base join; `$.fleet` single-object render) and fixes two latent `dashboards/aws.json` render bugs caught while doing so. JSON-only; no Python, no Terraform.

## Project north stars (constraint anchors)

- **$0 lifetime cost.** Closed via a LOCAL MOCK adapter (`scripts/mock_adapter.py`, new) serving the exact ADR 0014/0018 wire envelope at `http://host.docker.internal:8899/`; Grafana OSS local Docker only. **No `terraform apply`** — Infinity's datasource mechanics are identical whether the base URL points at a live Lambda Function URL or a local server, so render questions are answerable for $0. `terraform.tfstate` empty throughout.
- **Mode parity.** `dashboards` parity surface = the panel-level FIELD VOCABULARY (`psi_<feature>` from `shared.features.PSI_FEATURE_NAMES`; ADR 0005 §3 / ADR 0009). This change touches only `root_selector` and value `mappings` — NO column-selector / field-name change — so it is NOT a parity change. `dashboards/tests/test_dashboard_vocabulary.py` (7) stays green (the test pins column selectors ⊆ ADR 0014 keys; unaffected).
- **ADR 0014:** the adapter/panels are a snapshot projection — no threshold logic, literal `alert_flag`/`last_alert_sent_at` passthrough (ADR 0012 §2C).

## Summary of the change

Live observation against the mock proved both open items WORK: empty panel `url: ""` joins onto the datasource base URL (item 1), and `root_selector: "$.fleet"` renders a single object as one table row / one gauge value each (item 2). Observing live also exposed two latent render bugs that the prior CONTRACT-ONLY closures had missed:

1. **Panel 1 "Pumps reporting" (`stat`) read `null`/`null`.** It read top-level scalars `pumps_reporting`/`fleet_size` with `root_selector: ""`. Infinity's default JSON parser auto-descends an empty root into the first child array (`pumps[]`), so the scalars resolved against pump objects → null. (A JSONata object-construction root `{...}` is unsupported by the default parser — rendered blank.) **Fix: `root_selector: "$"`** — the bare root path resolves to the root object as ONE row (same single-object mechanism `$.fleet` uses) → 15/15 live.
2. **`last_alert_sent_at` null rendered as the literal string `"null"`, not `"never"`.** The `special: match "null"` mapping does not fire because Infinity serialises the wire JSON `null` as the STRING `"null"` in a `type:string` column. This was the "bonus" item marked closed-by-contract 2026-06-07 — never actually exercised live (the 2026-06-07 PSI warmup storm had set the timestamp on every pump). **Fix: add a `type:"value"` mapping (`"null" → "never"`)** beside the special mapping on every `last_alert_sent_at` column. Applied to panels 8 + 13 (had the special mapping) AND panel 2 "Fleet snapshot" (had NO mapping → was showing `"null"`; added for cross-table consistency).

Re-observed live after each fix (screenshots in the session log): panel 1 = 15/15; panels 2/8/13 null rows = "never", real timestamps otherwise; fleet gauges + one-row fleet table intact.

## Changed files

- `dashboards/aws.json` (edited) — panel 1 `root_selector "" → "$"`; `null→never` value mapping added to the `last_alert_sent_at` override on panels 2, 8, 13 (special mapping kept, its result index bumped 0→1). Full net diff below. No field-vocabulary change.
- `context/dashboards.md` (edited) — both items struck CLOSED with evidence; the 2026-06-07 contract-only null closure CORRECTED (it was wrong live); panel-1 finding recorded; ✅ line in §Current state.
- `scripts/mock_adapter.py` (new) — stdlib `http.server` serving the ADR 0014/0018 envelope (breaching fleet + a null-`last_alert_sent_at` pump so both alert-render paths show in one view); binds `0.0.0.0` for `host.docker.internal`. Throwaway $0 verification harness; referenced by the session log + memory.
- `docs/sessions/2026-06-14-grafana-infinity-close.md` (new) — session log.

## Net diff — `dashboards/aws.json` (committed baseline → final)

```diff
--- /tmp/aws.json.orig	2026-06-14 07:35:20.690411400 +0000
+++ dashboards/aws.json	2026-06-14 07:49:24.000000000 +0000
@@ -25,7 +25,7 @@
           "format": "table",
           "url": "",
           "url_options": { "method": "GET", "data": "" },
-          "root_selector": "",
+          "root_selector": "$",
           "columns": [
             { "selector": "pumps_reporting", "text": "pumps_reporting", "type": "number" },
             { "selector": "fleet_size", "text": "fleet_size", "type": "number" }
@@ -128,6 +128,29 @@
                 }
               }
             ]
+          },
+          {
+            "matcher": { "id": "byName", "options": "last_alert_sent_at" },
+            "properties": [
+              {
+                "id": "mappings",
+                "value": [
+                  {
+                    "type": "value",
+                    "options": {
+                      "null": { "index": 0, "text": "never" }
+                    }
+                  },
+                  {
+                    "type": "special",
+                    "options": {
+                      "match": "null",
+                      "result": { "index": 1, "text": "never" }
+                    }
+                  }
+                ]
+              }
+            ]
           }
         ]
       },
@@ -408,10 +431,16 @@
                 "id": "mappings",
                 "value": [
                   {
+                    "type": "value",
+                    "options": {
+                      "null": { "index": 0, "text": "never" }
+                    }
+                  },
+                  {
                     "type": "special",
                     "options": {
                       "match": "null",
-                      "result": { "index": 0, "text": "never" }
+                      "result": { "index": 1, "text": "never" }
                     }
                   }
                 ]
@@ -655,10 +684,16 @@
                 "id": "mappings",
                 "value": [
                   {
+                    "type": "value",
+                    "options": {
+                      "null": { "index": 0, "text": "never" }
+                    }
+                  },
+                  {
                     "type": "special",
                     "options": {
                       "match": "null",
-                      "result": { "index": 0, "text": "never" }
+                      "result": { "index": 1, "text": "never" }
                     }
                   }
                 ]
```

## Specific questions for the reviewer

1. **`root_selector: "$"` for top-level scalars.** Is `"$"` (bare root path → root object as one row) the right, version-stable fix for reading top-level scalars from an object that also contains arrays — or is there a more idiomatic Infinity selector (e.g. a UQL `parse-json | project ...` target) you'd prefer? Any case where `"$"` regresses to the empty-root auto-descend behaviour?
2. **`null → "never"` via a string value-map.** The fix maps the literal STRING `"null"` to "never" (because Infinity stringifies the wire null in a `type:string` column) AND keeps the `special: match null` mapping. Is mapping the string `"null"` brittle — could a legitimate `last_alert_sent_at` ever equal the string `"null"`? (It is otherwise an ISO-8601 timestamp.) Is keeping both mappings (value + special) sound, or should the special one be dropped as dead?
3. **Panel 2 scope.** I extended the null→never mapping to panel 2 ("Fleet snapshot"), which previously had no mapping and showed `"null"`, for cross-table consistency. Correct call, or scope creep beyond the two named items?
4. **Contract-vs-render lesson.** Two items previously "closed by contract" were wrong/incomplete when finally observed live. Should ADR 0014 (or `context/dashboards.md`) carry an explicit guard — e.g. "string-typed nullable wire columns require a `value` map on the stringified null, not just `special: match null`" — so the next nullable column doesn't repeat this?
5. **Mock-vs-live equivalence.** Both items are closed on a LOCAL MOCK serving the exact wire shape, on the premise that Infinity renders identically against a live Function URL (only the datasource base URL differs). Is that equivalence safe to CLOSE on, or should a live re-confirm remain a tracked demo-day gate?
6. **Untested null-fleet path.** The mock served a populated (breaching) `fleet`, so the empty-fleet `{}` → "No data" render was not re-exercised this session. It stays contractually handled (empty `{}`, DeepSeek review §3, 2026-06-10). Acceptable to leave, or worth a second mock pass with `fleet: {}`?

## What I'm NOT looking for in this review

- Re-litigating ADR 0014 / ADR 0018 (AuthType=NONE, Infinity choice, snapshot contract, fleet pooling).
- `scripts/mock_adapter.py` code style — it is a throwaway local harness, not shipped runtime.
- Panel aesthetics / gridPos / colour-band taste.
- Terraform / deploy — out of scope ($0, no apply); the out-of-scope follow-ups (F1 INFO-log fix, reserved concurrency -1, `FLEET_PUMP_IDS` SSOT dedup) are tracked elsewhere.

## Resolution (filled in by Claude after the reviewer responds)

Reviewer: **deepseek** (`deepseek-reasoner`), 2026-06-14. Strong pass — core fixes accepted. One follow-on code change folded (dead `special`-mapping removal); one tracked follow-up (Infinity plugin pin).

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. `root_selector: "$"` | Accepted | Idiomatic; no empty-root auto-descend regression (`"$"` is an explicit path). Recommends pinning the Infinity plugin version → **tracked follow-up** (`docker-compose.yml` `GF_INSTALL_PLUGINS` is unpinned; needs the live-installed version via `grafana cli plugins ls`). |
| 2. null→never — drop the special mapping | **Folded** | `special: match null` is dead on a `type:string` column (Infinity stringifies the wire null; we observed it never fires). REMOVED from panels 2/8/13; the `type:"value"` map on `"null"` alone is retained. `aws.json` re-diffed; vocab tests 7/7 green. |
| 3. panel-2 scope | Accepted, no change | Correct — same bug, all affected panels; not creep. |
| 4. contract-vs-render guard rule | **Folded** | Explicit rule added to `context/dashboards.md §Open questions` + memory [[ml-obs-pipeline-infinity-render-gotchas]]: a `type:string` column shows a wire `null` as the string `"null"`; `special: match null` does NOT fire — use a `type:"value"` map, never `special`. |
| 5. mock-vs-live equivalence | Accepted, no change | Safe to close on; session log documents the closure used a live-shaped mock (no implicit e2e-live promise). |
| 6. empty `fleet: {}` path | Tracked | Not re-exercised (mock served a populated fleet); verified-by-contract, marked not-re-tested 2026-06-14 — optional cheap pre-demo mock pass with `fleet:{}`. |
| Extra: `"$"` on a non-object root | N/A | Envelope is contractually a JSON object (ADR 0014); array-root regression can't arise from the contract. |
| Extra: mapping order / panel-1 stat layout | N/A | Ordering moot once special removed; panel-1 two-column stat (value + suffix) is correct. |
