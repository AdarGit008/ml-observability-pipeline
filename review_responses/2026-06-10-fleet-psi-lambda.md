## Review: `lambda_fleet_psi` â Pooled Fleet Drift Detector

### Overall Assessment

The implementation is clean, well-documented, and correctly follows established patterns (ADR 0011, 0012, 0017, 0018). The pooling logic, alert gating, and table writes are logically sound from a coding perspective. My concerns are primarily around the **statistical semantics** of pooling, **payload consistency**, and a **subtle pagination assumption**.

---

### 1. Pooling Statistics â **Mixed Signal Risk** (â ï¸)

You ask the right questions. Let me break them down:

- **(a) Masking a single drifting pump:** Yes, if one pump drifts and the other 14 are healthy, its contribution to the pooled window is ~1/15 (assuming equal sampling rates). A PSI of ~0.25 (your threshold) on a fleet-wide distribution could easily be missed if the drifting pump is small relative to the pooled mass. The fleet PSI will be less sensitive than per-pump PSI. This is a deliberate trade-off â you get a âplant-wideâ health signal, but it will be a **late indicator** for individual pump issues. ADR 0018 should document: *âFleet PSI is not a substitute for per-pump drift detection; it detects systemic distribution shifts across the plant (e.g., process-wide changes). Single-pump failures are expected to be caught by per-pump alerts before fleet PSI crosses threshold.â* If thatâs the intent, fine â but currently the ADR says âplant-wide drift detectorâ without that caveat. Add it.

- **(b) Heterogeneous-but-healthy pumps pooling into a wider distribution:** This is the more dangerous risk. Each pump i has its own healthy reference distribution \( R_i \) (built from HEALTHY data per ADR 0008). By pooling raw readings from all pumps and comparing to a single per-pump reference (assumed from pump-01? From any single pump?), you are implicitly assuming that all pumps are identically distributed when healthy. If pump-01 runs at 45Â°C and pump-07 at 55Â°C (both healthy), pooling them yields a bimodal distribution that will **compare poorly** against a reference built from pump-01 alone. This will generate false positive fleet drift even though nothing is broken.

**Crucial question:** What is `REFERENCE` in your code? From `context/drift.md`, `load_reference` returns the single reference JSON for the *pump_id* passed. But in the fleet handler, you call `compute_psi(pooled, reference=REFERENCE)` where `REFERENCE` is a global loaded at module level. Which pumpâs reference? If itâs e.g. pump-01 only, that is statistically invalid for the pooled fleet. If itâs a **pooled reference** (e.g., built by concatenating HEALTHY rows from all pumps), that would be sound but requires additional reference generation logic. ADR 0018 doesnât specify what `REFERENCE` contains. I suspect youâve inadvertently used a single-pump reference.

**Recommendation:** Clarify `REFERENCE` in code comments and ADR. If itâs a pooled reference, document the generation (likely from `lambda_scorer`âs health run). If itâs per-pump, you must either (a) build a pooled reference, or (b) run per-pump PSI and combine them (e.g., max PSI). The current approach is **unsound as described**. I consider this a blocking issue for statistical correctness.

---

### 2. FLEET SNS Payload Omits `score` â **Minor Inconsistency** (â ï¸)

The per-pump alert payload in `_interfaces.md Â§SNS alert payload` includes `"score"`. The fleet alert drops it. This will break any consumer that expects a standard schema. Worse, the payload includes `"alert_type": "psi_breach"` but per-pump uses `"alert_type": "drift_breach"`? Actually per-pump uses `"alert_type": "psi_breach"` too from `psi_alert_should_fire`. So the `alert_type` is consistent. Still, the absence of `score` is a deviation.

**Options:**
1. Include `"score": None` for fleet. Adds one byte, consumer can distinguish by `pump_id=="FLEET"` anyway. Simple.
2. Add an explicit `"scope": "fleet"` field. Helps any generic subscriber filter. Costs nothing.
3. Do nothing and rely on `pump_id`. Thatâs acceptable but brittle â if a consumer ever needs to know itâs a fleet alert without parsing `pump_id`, they canât.

**Recommendation:** At minimum, add `"score": null` for schema consistency. Adding `"scope": "fleet"` would be a nice touch but not required. Iâd lean toward option 2 (both) for future-proofing.

---

### 3. Shared SNS Topic â **Cost vs. Consumer Impact** (â)

Per-pump and fleet alerts share the same SNS topic. This is fine from a **cost** perspective: one topic, one subscription (email). The email body will contain `pump_id`, so the reader can distinguish. The only risk: if the email filter rule (e.g., âonly if pump_id == P-05â) is done client-side, users might get spammed with fleet alerts. But thatâs a user configuration issue â not a code flaw.

**Recommendation:** None. Shared topic is the right call given north star #1 ($0 cost). If future consumers need separate routing, they can subscribe with filter policies on `pump_id`.

---

### 4. Hot-Table Read at 5-min Cadence â **Fine, but Pagination Assumption is Risky** (â ï¸)

- **Cost:** 15 Queries (RCU based on item size) + 1 GetItem + 1 PutItem every 5 minutes. At 0.5Hz data rate (1 row per 2 sec), a pump generates ~150 rows in 5 minutes. Each query uses `Limit=150, ScanIndexForward=False`. This will return the 150 most recent items. If a pump occasionally spikes to 160 rows in 5 min (e.g., early rate burst or clock skew), the query might return only the **newest 150**, effectively discarding the oldest 10 rows. Thatâs a small window truncation â probably acceptable for drift detection over 5 min, but worth noting.

- **Pagination:** DynamoDB `Query` returns at most 1 MB of data, not just limit. With 150 items of ~200 bytes each, youâre well under. No pagination loop is needed. **But** â if an item is very large (e.g., many features), you could exceed 1 MB. Unlikely given your feature set, but defensively you should add a comment or a `while 'LastEvaluatedKey' in response:` guard for correctness. I know youâre reading *trailing 150*, not all items; but the safest approach is to either (a) assert no pagination needed, or (b) loop until you have 150 or exhausted. Given your single-page assumption, Iâd add a unit test that mocks a response with `LastEvaluatedKey` to ensure it fails noisily, proving the assumption holds.

**Recommendation:** Keep the no-pagination loop, but add a comment explaining the limit/volume guarantee and a defensive `if response.get('LastEvaluatedKey'): raise` in development for early warning. Acceptable as is for production given the 0.5Hz constraint.

---

### 5. Empty vs Always-Write â **Trade-off Acceptable** (â)

The batcherâs empty-no-op precedent (ADR 0018 cites it) is followed here. A missing FLEET row means no pumps reporting â likely during startup or outage. A dashboard can handle missing data (show âNo Dataâ). Writing a zero-PSI row would be misleading (it suggests drift = 0, which is a âno driftâ signal rather than âno dataâ). **The current approach is correct.** The only downside: a dashboard that always expects a FLEET row. Thatâs a UI concern, not a storage concern. Keep as is.

---

### 6. Warmup Gate at Fleet Scale â **Vestigial but Harmless** (â ï¸)

`psi_is_armed` inside `psi_alert_should_fire` requires at least 150 points in the combined window. Since the pooled window will almost always exceed 150 (any single pumpâs 150 rows already passes the gate), the gate effectively never blocks at fleet scale. It is **vestigial** but adds no cost (one extra comparison). Keeping it maintains parity with per-pump logic, simplifying future consolidation.

**Risk:** If you ever refer to âarmedâ status in dashboards, it would be misleading for fleet. But I see no such usage. Keep it.

**Recommendation:** Add a comment in the handler: âWarmup gate (`psi_is_armed`) is trivially satisfied for fleet due to pooling; kept for parity with per-pump implementation.â This saves future debuggers time.

---

## Resolution

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. Pooling statistics | **Blocking â fix OR document** | Need explicit clarification of `REFERENCE`. If per-pump, algorithm is unsound. Must either build pooled reference or switch to max-per-pump aggregation. |
| 2. Score-less FLEET payload | **Minor â add `score: null`** | Add for schema consistency; consider `scope: "fleet"` for robustness. |
| 3. Shared SNS topic | **Accept** | Lowest cost; adequate differentiation via `pump_id`. |
| 4. Hot-table read cost / pagination | **Accept with note** | Add defensive guard for `LastEvaluatedKey` in development. |
| 5. Empty vs always-write | **Accept** | Follows batcher precedent; correct behavior. |
| 6. Warmup gate at fleet scale | **Accept with note** | Add comment explaining vestigial nature. |

---

## Additional Minor Concerns

- **`FLEET_PUMP_IDS` source:** How is this list populated? If hardcoded, it will miss new pumps. Should come from a DynamoDB scan or SSM parameter. Not in scope of this session, but add a TODO.
- **`load_reference` for fleet:** In the handler you use `REFERENCE` global â but if `load_reference` is called with `pump_id="FLEET"`, the S3 key might be `references/FLEET.json`. Does that file exist? If not, an exception will crash the handler. Make sure itâs handled gracefully (or generate pooled reference on the fly). This ties back to point 1.
- **`pumps_reporting` count:** If a pump returns 0 rows, it is not counted. Thatâs correct. But if a pump returns 1 row (shouldnât happen, but possible due to clock edge), itâs counted. Thatâs fine.

Overall, the code is well-structured. Resolve the reference issue and add the small documentation notes, and itâs ready for production.

---
_Generated by **deepseek** (`deepseek-reasoner`) on 2026-06-10 14:24:58._

